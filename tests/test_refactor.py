"""Tests for graph-powered refactoring operations."""

import tempfile
import threading
import time
from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.parser import EdgeInfo, NodeInfo
from code_review_graph.refactor import (
    REFACTOR_EXPIRY_SECONDS,
    _pending_refactors,
    _refactor_lock,
    apply_refactor,
    find_dead_code,
    rename_preview,
    suggest_refactorings,
)


class TestRenamePreview:
    """Tests for rename_preview."""

    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.store = GraphStore(self.tmp.name)
        self._seed()

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)
        # Clean up pending refactors.
        with _refactor_lock:
            _pending_refactors.clear()

    def _seed(self):
        """Seed the store with test data for rename tests."""
        # File nodes
        self.store.upsert_node(
            NodeInfo(
                kind="File",
                name="/repo/utils.py",
                file_path="/repo/utils.py",
                line_start=1,
                line_end=50,
                language="python",
            )
        )
        self.store.upsert_node(
            NodeInfo(
                kind="File",
                name="/repo/main.py",
                file_path="/repo/main.py",
                line_start=1,
                line_end=30,
                language="python",
            )
        )
        # Function to rename
        self.store.upsert_node(
            NodeInfo(
                kind="Function",
                name="helper",
                file_path="/repo/utils.py",
                line_start=10,
                line_end=20,
                language="python",
            )
        )
        # Caller function
        self.store.upsert_node(
            NodeInfo(
                kind="Function",
                name="run",
                file_path="/repo/main.py",
                line_start=5,
                line_end=15,
                language="python",
            )
        )
        # CALLS edge: run -> helper
        self.store.upsert_edge(
            EdgeInfo(
                kind="CALLS",
                source="/repo/main.py::run",
                target="/repo/utils.py::helper",
                file_path="/repo/main.py",
                line=10,
            )
        )
        # IMPORTS_FROM edge: main.py imports helper
        self.store.upsert_edge(
            EdgeInfo(
                kind="IMPORTS_FROM",
                source="/repo/main.py",
                target="/repo/utils.py::helper",
                file_path="/repo/main.py",
                line=1,
            )
        )
        self.store.commit()

    def test_rename_preview_returns_edits_with_refactor_id(self):
        """rename_preview returns a dict with refactor_id and edits."""
        result = rename_preview(self.store, "helper", "new_helper")
        assert result is not None
        assert "refactor_id" in result
        assert len(result["refactor_id"]) == 8
        assert result["type"] == "rename"
        assert result["old_name"] == "helper"
        assert result["new_name"] == "new_helper"
        assert isinstance(result["edits"], list)
        assert len(result["edits"]) > 0
        assert "stats" in result
        assert result["stats"]["high"] > 0

    def test_rename_finds_callers(self):
        """rename_preview finds definition + call sites."""
        result = rename_preview(self.store, "helper", "new_helper")
        assert result is not None
        edits = result["edits"]
        # Should have at least: 1 definition + 1 call + 1 import = 3
        assert len(edits) >= 3
        files = {e["file"] for e in edits}
        assert "/repo/utils.py" in files  # definition
        assert "/repo/main.py" in files  # call site + import site

    def test_rename_not_found(self):
        """rename_preview returns None if symbol not found."""
        result = rename_preview(self.store, "nonexistent_function", "new_name")
        assert result is None

    def test_rename_stores_in_pending(self):
        """rename_preview stores the preview in _pending_refactors."""
        result = rename_preview(self.store, "helper", "new_helper")
        assert result is not None
        rid = result["refactor_id"]
        with _refactor_lock:
            assert rid in _pending_refactors

    def test_rename_preview_includes_possible_misses_for_docstrings(self, tmp_path):
        """Occurrences in docstrings/comments not in edits appear in possible_misses."""
        # Create a real Python file with a function definition and a docstring mention.
        src_file = tmp_path / "mymodule.py"
        src_file.write_text(
            'def helper():\n'
            '    """Call helper to do work.\n'
            '\n'
            '    Example::\n'
            '\n'
            '        helper()  # docstring example\n'
            '    """\n'
            '    pass\n',
            encoding="utf-8",
        )
        # Upsert a node pointing at the real temp file.
        self.store.upsert_node(
            NodeInfo(
                kind="Function",
                name="helper_doc",
                file_path=str(src_file),
                line_start=1,
                line_end=8,
                language="python",
            )
        )
        self.store.commit()

        result = rename_preview(self.store, "helper_doc", "new_helper_doc")
        # The file doesn't actually contain "helper_doc" in the docstring body,
        # so use a simpler approach: create a file with the exact old_name in a comment.
        src_file2 = tmp_path / "mymodule2.py"
        src_file2.write_text(
            'def my_func():\n'
            '    # Uses my_func internally\n'
            '    pass\n',
            encoding="utf-8",
        )
        self.store.upsert_node(
            NodeInfo(
                kind="Function",
                name="my_func",
                file_path=str(src_file2),
                line_start=1,
                line_end=3,
                language="python",
            )
        )
        self.store.commit()

        result2 = rename_preview(self.store, "my_func", "renamed_func")
        assert result2 is not None
        assert "possible_misses" in result2
        # Line 2 has "my_func" in a comment — not in edits (which cover line 1 only).
        miss_lines = {m["line"] for m in result2["possible_misses"]}
        assert 2 in miss_lines
        # Each miss must have required fields.
        for miss in result2["possible_misses"]:
            assert "file" in miss
            assert "line" in miss
            assert "text" in miss
            assert miss["confidence"] == "low"
            assert "reason" in miss

    def test_rename_preview_possible_misses_excludes_already_in_edits(self, tmp_path):
        """Lines already present in edits must NOT appear in possible_misses."""
        src_file = tmp_path / "fn.py"
        src_file.write_text(
            'def compute():\n'
            '    # compute does the work\n'
            '    pass\n',
            encoding="utf-8",
        )
        self.store.upsert_node(
            NodeInfo(
                kind="Function",
                name="compute",
                file_path=str(src_file),
                line_start=1,
                line_end=3,
                language="python",
            )
        )
        self.store.commit()

        result = rename_preview(self.store, "compute", "calculate")
        assert result is not None

        edit_positions = {(e["file"], e["line"]) for e in result["edits"]}
        for miss in result["possible_misses"]:
            assert (miss["file"], miss["line"]) not in edit_positions, (
                f"possible_miss at {miss['file']}:{miss['line']} is already in edits"
            )


class TestFindDeadCode:
    """Tests for find_dead_code."""

    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.store = GraphStore(self.tmp.name)
        self._seed()

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _seed(self):
        """Seed with a mix of used and unused functions."""
        # File
        self.store.upsert_node(
            NodeInfo(
                kind="File",
                name="/repo/app.py",
                file_path="/repo/app.py",
                line_start=1,
                line_end=100,
                language="python",
            )
        )
        # A function that IS called
        self.store.upsert_node(
            NodeInfo(
                kind="Function",
                name="used_func",
                file_path="/repo/app.py",
                line_start=10,
                line_end=20,
                language="python",
            )
        )
        # A function that is NOT called (dead code)
        self.store.upsert_node(
            NodeInfo(
                kind="Function",
                name="dead_func",
                file_path="/repo/app.py",
                line_start=30,
                line_end=40,
                language="python",
            )
        )
        # An entry point function (should be excluded)
        self.store.upsert_node(
            NodeInfo(
                kind="Function",
                name="main",
                file_path="/repo/app.py",
                line_start=50,
                line_end=60,
                language="python",
            )
        )
        # A test function (should be excluded)
        self.store.upsert_node(
            NodeInfo(
                kind="Test",
                name="test_something",
                file_path="/repo/test_app.py",
                line_start=1,
                line_end=10,
                language="python",
                is_test=True,
            )
        )

        # Caller for used_func
        self.store.upsert_node(
            NodeInfo(
                kind="Function",
                name="caller",
                file_path="/repo/app.py",
                line_start=70,
                line_end=80,
                language="python",
            )
        )
        self.store.upsert_edge(
            EdgeInfo(
                kind="CALLS",
                source="/repo/app.py::caller",
                target="/repo/app.py::used_func",
                file_path="/repo/app.py",
                line=75,
            )
        )
        self.store.commit()

    def test_find_dead_code(self):
        """find_dead_code detects unreferenced functions."""
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "dead_func" in dead_names

    def test_find_dead_code_excludes_called(self):
        """find_dead_code does NOT include functions with callers."""
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "used_func" not in dead_names

    def test_find_dead_code_excludes_entry_points(self):
        """Entry points (like 'main') are not flagged as dead code."""
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "main" not in dead_names

    def test_find_dead_code_excludes_tests(self):
        """Test nodes are not flagged as dead code."""
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "test_something" not in dead_names

    def test_find_dead_code_kind_filter(self):
        """kind filter restricts results."""
        dead = find_dead_code(self.store, kind="Class")
        # We have no Class nodes, so should be empty
        assert len(dead) == 0

    def test_find_dead_code_file_pattern(self):
        """file_pattern filter works."""
        dead = find_dead_code(self.store, file_pattern="nonexistent")
        assert len(dead) == 0

    def test_find_dead_code_exclude_paths(self):
        """exclude_paths removes nodes whose file_path contains any excluded substring."""
        # Seed an unreferenced function in an excluded file
        self.store.upsert_node(
            NodeInfo(
                kind="Function",
                name="excluded_dead_func",
                file_path="/repo/excluded_file.py",
                line_start=1,
                line_end=10,
                language="python",
            )
        )
        # Seed an unreferenced function in a kept file
        self.store.upsert_node(
            NodeInfo(
                kind="Function",
                name="kept_dead_func",
                file_path="/repo/kept_file.py",
                line_start=1,
                line_end=10,
                language="python",
            )
        )
        self.store.commit()

        dead = find_dead_code(self.store, exclude_paths=["excluded_file"])
        dead_names = {d["name"] for d in dead}

        assert "excluded_dead_func" not in dead_names
        assert "kept_dead_func" in dead_names

    def test_find_dead_code_result_has_file_path_key(self):
        """Dead code results have both 'file' and 'file_path' keys."""
        dead = find_dead_code(self.store)
        assert len(dead) > 0, "Expected at least one dead code result"
        for result in dead:
            assert "file" in result, "Result missing 'file' key"
            assert "file_path" in result, "Result missing 'file_path' key"
            assert result["file"] is not None, "'file' key should not be None"
            assert result["file_path"] is not None, "'file_path' key should not be None"
            assert result["file"] == result["file_path"], "'file' and 'file_path' should match"


class TestImportClassification:
    """C-08: import statements in rename preview → high_confidence edits, not possible_misses."""

    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.store = GraphStore(self.tmp.name)

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)
        with _refactor_lock:
            _pending_refactors.clear()

    def test_from_import_goes_to_high_confidence(self, tmp_path):
        """'from module import old_name' lines appear in high-confidence edits, not possible_misses."""
        # The definition file also contains an import line with old_name.
        # The definition file is always in files_to_scan, so the import line will be scanned.
        src_file = tmp_path / "utils.py"
        src_file.write_text(
            "from other_module import old_name\n"
            "# old_name is also used here\n"
            "def old_name():\n"
            "    pass\n",
            encoding="utf-8",
        )
        self.store.upsert_node(
            NodeInfo(
                kind="Function",
                name="old_name",
                file_path=str(src_file),
                line_start=3,
                line_end=4,
                language="python",
            )
        )
        self.store.commit()

        result = rename_preview(self.store, "old_name", "new_name")
        assert result is not None

        # Line 1: import statement → must be in high-confidence edits
        high_positions = {(e["file"], e["line"]) for e in result["edits"] if e["confidence"] == "high"}
        assert (str(src_file), 1) in high_positions, (
            f"Import line not in high-confidence edits. edits={result['edits']}"
        )

        # Line 1 must NOT appear in possible_misses
        miss_positions = {(m["file"], m["line"]) for m in result["possible_misses"]}
        assert (str(src_file), 1) not in miss_positions, (
            "Import statement incorrectly classified as possible_miss"
        )

    def test_bare_import_goes_to_high_confidence(self, tmp_path):
        """'import old_name' lines appear in high-confidence edits, not possible_misses."""
        src_file = tmp_path / "mod.py"
        src_file.write_text(
            "import old_name\n"
            "def old_name():\n"
            "    pass\n",
            encoding="utf-8",
        )
        self.store.upsert_node(
            NodeInfo(
                kind="Function",
                name="old_name",
                file_path=str(src_file),
                line_start=2,
                line_end=3,
                language="python",
            )
        )
        self.store.commit()

        result = rename_preview(self.store, "old_name", "new_name")
        assert result is not None

        high_positions = {(e["file"], e["line"]) for e in result["edits"] if e["confidence"] == "high"}
        assert (str(src_file), 1) in high_positions, (
            f"Bare import not in high-confidence edits. edits={result['edits']}"
        )
        miss_positions = {(m["file"], m["line"]) for m in result["possible_misses"]}
        assert (str(src_file), 1) not in miss_positions

    def test_comment_reason_set(self, tmp_path):
        """Lines starting with '#' get reason='comment' in possible_misses."""
        src_file = tmp_path / "utils.py"
        src_file.write_text(
            "def old_name():\n"
            "    # old_name is documented here\n"
            "    pass\n",
            encoding="utf-8",
        )
        self.store.upsert_node(
            NodeInfo(
                kind="Function",
                name="old_name",
                file_path=str(src_file),
                line_start=1,
                line_end=3,
                language="python",
            )
        )
        self.store.commit()

        result = rename_preview(self.store, "old_name", "new_name")
        assert result is not None

        comment_misses = [
            m for m in result["possible_misses"]
            if m.get("reason") == "comment"
        ]
        assert len(comment_misses) >= 1, (
            f"Expected at least one miss with reason='comment'. misses={result['possible_misses']}"
        )
        # Must not have the old generic reason
        for m in result["possible_misses"]:
            assert m.get("reason") != "docstring/comment/string literal — manual review required", (
                "Old generic reason still present"
            )

    def test_string_literal_reason_set(self, tmp_path):
        """String literals get reason='string_literal' in possible_misses."""
        src_file = tmp_path / "utils.py"
        src_file.write_text(
            'def old_name():\n'
            '    return "old_name"\n',
            encoding="utf-8",
        )
        self.store.upsert_node(
            NodeInfo(
                kind="Function",
                name="old_name",
                file_path=str(src_file),
                line_start=1,
                line_end=2,
                language="python",
            )
        )
        self.store.commit()

        result = rename_preview(self.store, "old_name", "new_name")
        assert result is not None

        string_misses = [m for m in result["possible_misses"] if m.get("reason") == "string_literal"]
        assert len(string_misses) >= 1, (
            f"Expected at least one miss with reason='string_literal'. misses={result['possible_misses']}"
        )


class TestDeadCodeFalsePositives:
    """H-05: __init__ and abstract methods must not be flagged as dead code."""

    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.store = GraphStore(self.tmp.name)
        self._seed()

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _seed(self):
        """Seed store with __init__, abstract method, and a real dead function."""
        self.store.upsert_node(
            NodeInfo(
                kind="Function",
                name="__init__",
                file_path="/repo/myclass.py",
                line_start=5,
                line_end=10,
                language="python",
            )
        )
        self.store.upsert_node(
            NodeInfo(
                kind="Function",
                name="abstract_method",
                file_path="/repo/myclass.py",
                line_start=12,
                line_end=15,
                language="python",
                modifiers="abstract",
            )
        )
        self.store.upsert_node(
            NodeInfo(
                kind="Function",
                name="truly_dead_func",
                file_path="/repo/myclass.py",
                line_start=20,
                line_end=25,
                language="python",
            )
        )
        self.store.commit()

    def test_init_not_flagged_as_dead(self):
        """__init__ methods are never reported as dead code."""
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "__init__" not in dead_names, (
            "__init__ constructor should not be flagged as dead code"
        )

    def test_abstract_method_not_flagged_as_dead(self):
        """Functions with modifiers='abstract' are never reported as dead code."""
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "abstract_method" not in dead_names, (
            "Abstract methods should not be flagged as dead code"
        )

    def test_truly_dead_still_flagged(self):
        """Genuinely unreferenced functions are still reported."""
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "truly_dead_func" in dead_names, (
            "Unreferenced function should still be flagged as dead code"
        )


class TestAuditHealthScore:
    """M-05: health_score must reflect the post-exclude_paths filtered dead_code count."""

    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.store = GraphStore(self.tmp.name)
        self._seed()

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _seed(self):
        """Seed with dead functions spread across two paths."""
        # 5 dead functions in /repo/included/
        for i in range(5):
            self.store.upsert_node(
                NodeInfo(
                    kind="Function",
                    name=f"included_dead_{i}",
                    file_path=f"/repo/included/file_{i}.py",
                    line_start=1,
                    line_end=5,
                    language="python",
                )
            )
        # 5 dead functions in /repo/excluded/
        for i in range(5):
            self.store.upsert_node(
                NodeInfo(
                    kind="Function",
                    name=f"excluded_dead_{i}",
                    file_path=f"/repo/excluded/file_{i}.py",
                    line_start=1,
                    line_end=5,
                    language="python",
                )
            )
        self.store.commit()

    def test_health_score_uses_filtered_count(self):
        """health_score computed from filtered dead_code is higher than from unfiltered."""
        dead_unfiltered = find_dead_code(self.store)
        dead_filtered = find_dead_code(self.store, exclude_paths=["/repo/excluded/"])

        # Verify we have fewer items after filtering
        assert len(dead_filtered) < len(dead_unfiltered), (
            "exclude_paths should reduce dead_code count"
        )
        assert len(dead_filtered) == 5   # only included/ functions remain
        assert len(dead_unfiltered) == 10

        # Compute health_score using the same formula as audit_workspace_tool
        def compute_score(dead: list) -> int:
            dead_penalty = min(len(dead) * 2, 30)
            return max(0, 100 - dead_penalty)

        score_unfiltered = compute_score(dead_unfiltered)
        score_filtered = compute_score(dead_filtered)

        assert score_filtered > score_unfiltered, (
            f"Health score with exclude ({score_filtered}) should be higher than "
            f"without ({score_unfiltered})"
        )

    def test_excluded_symbols_not_in_filtered_results(self):
        """Symbols in excluded paths do not appear in filtered dead_code results."""
        dead = find_dead_code(self.store, exclude_paths=["/repo/excluded/"])
        dead_names = {d["name"] for d in dead}

        for i in range(5):
            assert f"excluded_dead_{i}" not in dead_names
        for i in range(5):
            assert f"included_dead_{i}" in dead_names


class TestSuggestRefactorings:
    """Tests for suggest_refactorings."""

    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.store = GraphStore(self.tmp.name)
        self._seed()

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _seed(self):
        """Seed with dead code to generate suggestions."""
        self.store.upsert_node(
            NodeInfo(
                kind="File",
                name="/repo/lib.py",
                file_path="/repo/lib.py",
                line_start=1,
                line_end=50,
                language="python",
            )
        )
        # Unreferenced function -> removal suggestion
        self.store.upsert_node(
            NodeInfo(
                kind="Function",
                name="orphan_func",
                file_path="/repo/lib.py",
                line_start=10,
                line_end=20,
                language="python",
            )
        )
        self.store.commit()

    def test_suggest_refactorings(self):
        """suggest_refactorings returns a list of suggestions."""
        suggestions = suggest_refactorings(self.store)
        assert isinstance(suggestions, list)
        # Should have at least the dead-code removal suggestion
        assert len(suggestions) >= 1
        types = {s["type"] for s in suggestions}
        assert "remove" in types

    def test_suggestion_structure(self):
        """Each suggestion has the required fields."""
        suggestions = suggest_refactorings(self.store)
        for s in suggestions:
            assert "type" in s
            assert "description" in s
            assert "symbols" in s
            assert "rationale" in s
            assert s["type"] in ("move", "remove")


class TestApplyRefactor:
    """Tests for apply_refactor."""

    def setup_method(self):
        with _refactor_lock:
            _pending_refactors.clear()

    def teardown_method(self):
        with _refactor_lock:
            _pending_refactors.clear()

    def test_apply_refactor_validates_id(self):
        """apply_refactor rejects nonexistent refactor_id."""
        # Use a real temp dir as repo_root (needs .git or .code-review-graph)
        tmp_dir = Path(tempfile.mkdtemp())
        (tmp_dir / ".git").mkdir()
        try:
            result = apply_refactor("nonexistent_id", tmp_dir)
            assert result["status"] == "error"
            # graph_error uses "summary" field; legacy tests use "error"
            msg = (result.get("summary") or result.get("error") or "").lower()
            assert "not found" in msg or "expired" in msg
        finally:
            (tmp_dir / ".git").rmdir()
            tmp_dir.rmdir()

    def test_apply_refactor_expiry(self):
        """apply_refactor rejects expired previews."""
        tmp_dir = Path(tempfile.mkdtemp())
        (tmp_dir / ".git").mkdir()
        try:
            # Insert a preview that is already expired.
            rid = "expired1"
            with _refactor_lock:
                _pending_refactors[rid] = {
                    "refactor_id": rid,
                    "type": "rename",
                    "old_name": "old",
                    "new_name": "new",
                    "edits": [],
                    "stats": {"high": 0, "medium": 0, "low": 0},
                    "created_at": time.time() - REFACTOR_EXPIRY_SECONDS - 10,
                }
            result = apply_refactor(rid, tmp_dir)
            assert result["status"] == "error"
            msg = (result.get("summary") or result.get("error") or "").lower()
            assert "expired" in msg
        finally:
            (tmp_dir / ".git").rmdir()
            tmp_dir.rmdir()

    def test_apply_refactor_path_traversal(self):
        """apply_refactor blocks edits outside repo root."""
        tmp_dir = Path(tempfile.mkdtemp())
        (tmp_dir / ".git").mkdir()
        try:
            rid = "traversal"
            with _refactor_lock:
                _pending_refactors[rid] = {
                    "refactor_id": rid,
                    "type": "rename",
                    "old_name": "old",
                    "new_name": "new",
                    "edits": [
                        {
                            "file": "/etc/passwd",
                            "line": 1,
                            "old": "old",
                            "new": "new",
                            "confidence": "high",
                        }
                    ],
                    "stats": {"high": 1, "medium": 0, "low": 0},
                    "created_at": time.time(),
                }
            result = apply_refactor(rid, tmp_dir)
            assert result["status"] == "error"
            msg = (result.get("summary") or result.get("error") or "").lower()
            assert "outside repo root" in msg
        finally:
            (tmp_dir / ".git").rmdir()
            tmp_dir.rmdir()

    def test_apply_refactor_success(self):
        """apply_refactor applies string replacement to a real file."""
        tmp_dir = Path(tempfile.mkdtemp())
        (tmp_dir / ".git").mkdir()
        target_file = tmp_dir / "example.py"
        target_file.write_text("def old_func():\n    pass\n", encoding="utf-8")
        try:
            rid = "success1"
            with _refactor_lock:
                _pending_refactors[rid] = {
                    "refactor_id": rid,
                    "type": "rename",
                    "old_name": "old_func",
                    "new_name": "new_func",
                    "edits": [
                        {
                            "file": str(target_file),
                            "line": 1,
                            "old": "old_func",
                            "new": "new_func",
                            "confidence": "high",
                        }
                    ],
                    "stats": {"high": 1, "medium": 0, "low": 0},
                    "created_at": time.time(),
                }
            result = apply_refactor(rid, tmp_dir)
            assert result["status"] == "ok"
            assert result["edits_applied"] == 1
            assert len(result["files_modified"]) == 1
            # Verify file content was changed.
            content = target_file.read_text(encoding="utf-8")
            assert "new_func" in content
            assert "old_func" not in content
        finally:
            target_file.unlink(missing_ok=True)
            (tmp_dir / ".git").rmdir()
            tmp_dir.rmdir()


class TestApplyRefactorGraphDBUpdate:
    """Tests for graph DB updates after apply_refactor_func (MCP tool)."""

    def setup_method(self):
        """Set up test database and temp directory."""
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.store = GraphStore(self.tmp_db.name)
        self.tmp_dir = Path(tempfile.mkdtemp())
        (self.tmp_dir / ".git").mkdir()
        (self.tmp_dir / ".code-review-graph").mkdir()
        # Copy the test DB to the .code-review-graph directory so _get_store finds it
        import shutil
        self.graph_db = self.tmp_dir / ".code-review-graph" / "graph.db"
        shutil.copy(self.tmp_db.name, str(self.graph_db))
        self._seed()

    def teardown_method(self):
        """Clean up database and temp directory."""
        try:
            self.store.close()
        except Exception:
            pass
        # Try to delete temp DB file, ignore if locked
        try:
            Path(self.tmp_db.name).unlink(missing_ok=True)
        except (PermissionError, OSError):
            pass
        # Clean up temp files
        for f in self.tmp_dir.glob("*.py"):
            try:
                f.unlink(missing_ok=True)
            except (PermissionError, OSError):
                pass
        import shutil
        try:
            shutil.rmtree(self.tmp_dir / ".code-review-graph", ignore_errors=True)
        except Exception:
            pass
        try:
            (self.tmp_dir / ".git").rmdir()
        except (PermissionError, OSError):
            pass
        try:
            self.tmp_dir.rmdir()
        except (PermissionError, OSError):
            pass
        # Clean up pending refactors
        with _refactor_lock:
            _pending_refactors.clear()

    def _seed(self):
        """Seed the store with a function node to rename."""
        self.store.upsert_node(
            NodeInfo(
                kind="File",
                name=str(self.tmp_dir / "example.py"),
                file_path=str(self.tmp_dir / "example.py"),
                line_start=1,
                line_end=10,
                language="python",
            )
        )
        self.store.upsert_node(
            NodeInfo(
                kind="Function",
                name="old_func",
                file_path=str(self.tmp_dir / "example.py"),
                line_start=1,
                line_end=5,
                language="python",
            )
        )
        self.store.commit()
        # Sync to the .code-review-graph copy
        import shutil
        shutil.copy(self.tmp_db.name, str(self.graph_db))

    def test_apply_refactor_updates_graph_db_node_name(self):
        """After apply_refactor_func, graph DB node names are updated."""
        from code_review_graph.tools.refactor_tools import apply_refactor_func

        # Create a real file to rename
        target_file = self.tmp_dir / "example.py"
        target_file.write_text("def old_func():\n    pass\n", encoding="utf-8")

        # Create a refactor preview
        rid = "test_graph_update"
        with _refactor_lock:
            _pending_refactors[rid] = {
                "refactor_id": rid,
                "type": "rename",
                "old_name": "old_func",
                "new_name": "new_func",
                "edits": [
                    {
                        "file": str(target_file),
                        "line": 1,
                        "old": "old_func",
                        "new": "new_func",
                        "confidence": "high",
                    }
                ],
                "stats": {"high": 1, "medium": 0, "low": 0},
                "created_at": time.time(),
            }

        # Apply the refactor using the MCP tool function
        result = apply_refactor_func(rid, str(self.tmp_dir))
        assert result["status"] == "ok"
        assert result["applied"] == 1
        # Verify graph_updated flag is set (indicates DB was updated)
        assert result.get("graph_updated") is True, (
            f"Expected graph_updated=True, got {result}. "
            "This indicates the graph DB update after apply_refactor succeeded."
        )

        # Verify file was updated
        content = target_file.read_text(encoding="utf-8")
        assert "new_func" in content
        assert "old_func" not in content


class TestPendingRefactorsThreadSafe:
    """Tests for thread-safety of the pending refactors storage."""

    def test_pending_refactors_thread_safe(self):
        """The _refactor_lock is a threading.Lock instance."""
        assert isinstance(_refactor_lock, type(threading.Lock()))

    def test_concurrent_access(self):
        """Multiple threads can safely access _pending_refactors."""
        results = []

        def writer(rid: str):
            with _refactor_lock:
                _pending_refactors[rid] = {
                    "refactor_id": rid,
                    "created_at": time.time(),
                }
                results.append(rid)

        threads = [threading.Thread(target=writer, args=(f"t{i}",)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        with _refactor_lock:
            assert len(results) == 10
            assert len(_pending_refactors) >= 10
            # Clean up
            _pending_refactors.clear()


class TestRefactorFuncLimit:
    """C-06, C-07: refactor_func limit parameter truncates dead_code and suggest results."""

    def setup_method(self):
        import shutil
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.store = GraphStore(self.tmp_db.name)
        self.tmp_dir = Path(tempfile.mkdtemp())
        (self.tmp_dir / ".git").mkdir()
        (self.tmp_dir / ".code-review-graph").mkdir()
        self.graph_db = self.tmp_dir / ".code-review-graph" / "graph.db"
        self._seed()

    def teardown_method(self):
        import shutil
        if self.store is not None:
            try:
                self.store.close()
            except Exception:
                pass
            self.store = None
        try:
            Path(self.tmp_db.name).unlink(missing_ok=True)
        except (PermissionError, OSError):
            pass
        shutil.rmtree(str(self.tmp_dir), ignore_errors=True)

    def _seed(self):
        """Seed 5 dead functions so limit tests can truncate."""
        import shutil
        for i in range(5):
            self.store.upsert_node(
                NodeInfo(
                    kind="Function",
                    name=f"dead_func_{i}",
                    file_path=f"/repo/file_{i}.py",
                    line_start=1,
                    line_end=5,
                    language="python",
                )
            )
        self.store.commit()
        # Close before copying to ensure WAL is checkpointed to the main db file
        self.store.close()
        self.store = None
        shutil.copy(self.tmp_db.name, str(self.graph_db))

    def test_dead_code_limit_truncates(self):
        """refactor_func(mode='dead_code', limit=2) returns at most 2 items."""
        from code_review_graph.tools.refactor_tools import refactor_func

        result = refactor_func(mode="dead_code", limit=2, repo_root=str(self.tmp_dir))
        assert result["status"] == "ok"
        assert len(result.get("dead_code", [])) <= 2

    def test_dead_code_limit_sets_truncated_true(self):
        """When limit < total, truncated=True and total shows full count."""
        from code_review_graph.tools.refactor_tools import refactor_func

        result = refactor_func(mode="dead_code", limit=2, repo_root=str(self.tmp_dir))
        assert result["truncated"] is True
        assert result["total"] == 5

    def test_dead_code_no_truncation_when_limit_large(self):
        """When limit >= total, truncated=False."""
        from code_review_graph.tools.refactor_tools import refactor_func

        result = refactor_func(mode="dead_code", limit=100, repo_root=str(self.tmp_dir))
        assert result["truncated"] is False
        assert result["total"] == 5
        assert len(result.get("dead_code", [])) == 5


class TestAuditWorkspaceLimit:
    """C-05: audit_workspace limit parameter truncates dead_code and large_functions."""

    def setup_method(self):
        import shutil
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.store = GraphStore(self.tmp_db.name)
        self.tmp_dir = Path(tempfile.mkdtemp())
        (self.tmp_dir / ".git").mkdir()
        (self.tmp_dir / ".code-review-graph").mkdir()
        self.graph_db = self.tmp_dir / ".code-review-graph" / "graph.db"
        self._seed()

    def teardown_method(self):
        import shutil
        if self.store is not None:
            try:
                self.store.close()
            except Exception:
                pass
            self.store = None
        try:
            Path(self.tmp_db.name).unlink(missing_ok=True)
        except (PermissionError, OSError):
            pass
        shutil.rmtree(str(self.tmp_dir), ignore_errors=True)

    def _seed(self):
        """Seed 5 dead functions and 5 large functions."""
        import shutil
        for i in range(5):
            self.store.upsert_node(
                NodeInfo(
                    kind="Function",
                    name=f"dead_func_{i}",
                    file_path=f"/repo/dead_{i}.py",
                    line_start=1,
                    line_end=5,
                    language="python",
                )
            )
        for i in range(5):
            self.store.upsert_node(
                NodeInfo(
                    kind="Function",
                    name=f"big_func_{i}",
                    file_path=f"/repo/big_{i}.py",
                    line_start=1,
                    line_end=200,
                    language="python",
                )
            )
        self.store.commit()
        # Close before copying to ensure WAL is checkpointed to the main db file
        self.store.close()
        self.store = None
        shutil.copy(self.tmp_db.name, str(self.graph_db))

    def test_limit_truncates_dead_code(self):
        """audit_workspace(limit=2) returns at most 2 dead_code items."""
        from code_review_graph.tools.review import audit_workspace

        result = audit_workspace(
            include_cycles=False, limit=2, repo_root=str(self.tmp_dir)
        )
        assert result["status"] == "ok"
        assert len(result.get("dead_code", [])) <= 2

    def test_limit_sets_truncated_and_totals(self):
        """When truncated, total_dead_code and total_large_functions show full counts."""
        from code_review_graph.tools.review import audit_workspace

        result = audit_workspace(
            include_cycles=False, limit=2, repo_root=str(self.tmp_dir)
        )
        assert result["truncated"] is True
        # All 10 seeded functions are unreferenced (dead code)
        assert result["total_dead_code"] == 10
        # Only big_func_* (5 functions, 200 lines) exceed the default min_lines=50
        assert result["total_large_functions"] == 5

    def test_no_truncation_when_limit_large(self):
        """audit_workspace(limit=100) sets truncated=False."""
        from code_review_graph.tools.review import audit_workspace

        result = audit_workspace(
            include_cycles=False, limit=100, repo_root=str(self.tmp_dir)
        )
        assert result["truncated"] is False
        # All 10 seeded functions are unreferenced (dead code)
        assert result["total_dead_code"] == 10
