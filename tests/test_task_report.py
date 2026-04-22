"""Tests for task_report.py — markdown task-tree report generator."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest

from code_review_graph import tasks
from code_review_graph.graph import GraphStore
from code_review_graph.task_report import (
    generate_task_report,
    _render_header,
    _render_task,
)


class TestTaskReportBase:
    """Base class: creates a fresh GraphStore + conn per test."""

    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.store = GraphStore(self.tmp.name)
        self.conn = self.store._conn

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _task(self, title: str, parent_id: str | None = None, **kwargs) -> dict:
        result = tasks.create_task(
            self.conn, tasks=[{"title": title, **kwargs}], parent_id=parent_id
        )
        return result["tasks"][0]

    def _note(self, task_id: str, note_type: str, content: str, **kwargs) -> dict:
        result = tasks.add_note(
            self.conn, task_id, notes=[{"note_type": note_type, "content": content, **kwargs}]
        )
        return result["notes"][0]

    def _contract(self, provider_id: str, consumer_id: str,
                  scope_task_id: str = "", **kwargs) -> dict:
        # scope_task_id must be provided; fall back to provider_id's root
        if not scope_task_id:
            # Walk to root
            current = provider_id
            while True:
                row = self.conn.execute(
                    "SELECT parent_id FROM tasks WHERE id=?", (current,)
                ).fetchone()
                if row is None or row[0] is None:
                    break
                current = row[0]
            scope_task_id = current
        return tasks.add_contract(
            self.conn,
            contract_type="api",
            definition="interface definition",
            name="ApiContract",
            scope_task_id=scope_task_id,
            provider_task_id=provider_id,
            consumer_task_ids=[consumer_id],
            **kwargs,
        )


# ---------------------------------------------------------------------------
# generate_task_report
# ---------------------------------------------------------------------------

class TestGenerateTaskReport(TestTaskReportBase):

    def test_creates_output_file(self, tmp_path):
        root = self._task("My Root Task")
        result = generate_task_report(self.conn, root["id"], tmp_path)
        assert result["tasks_rendered"] == 1
        assert not result["skipped"]
        out = Path(result["output_path"])
        assert out.exists()
        assert out.name == "task-report.md"

    def test_output_contains_root_title(self, tmp_path):
        root = self._task("AuthService Implementation")
        generate_task_report(self.conn, root["id"], tmp_path)
        content = (tmp_path / "task-report.md").read_text(encoding="utf-8")
        assert "AuthService Implementation" in content

    def test_output_contains_status(self, tmp_path):
        root = self._task("Root Task")
        tasks.update_task(self.conn, root["id"], status="in_progress")
        generate_task_report(self.conn, root["id"], tmp_path)
        content = (tmp_path / "task-report.md").read_text(encoding="utf-8")
        assert "in_progress" in content

    def test_subtasks_rendered_recursively(self, tmp_path):
        root = self._task("Root")
        child = self._task("Child Task", parent_id=root["id"])
        grandchild = self._task("Grandchild Task", parent_id=child["id"])
        result = generate_task_report(self.conn, root["id"], tmp_path)
        assert result["tasks_rendered"] == 3
        content = (tmp_path / "task-report.md").read_text(encoding="utf-8")
        assert "Child Task" in content
        assert "Grandchild Task" in content

    def test_notes_appear_in_output(self, tmp_path):
        root = self._task("Root")
        self._note(root["id"], "decision", "Use JWT for auth")
        self._note(root["id"], "question", "Should we use Redis?")
        generate_task_report(self.conn, root["id"], tmp_path)
        content = (tmp_path / "task-report.md").read_text(encoding="utf-8")
        assert "Use JWT for auth" in content
        assert "Should we use Redis?" in content

    def test_contracts_appear_in_output(self, tmp_path):
        root = self._task("Root")
        provider = self._task("Provider", parent_id=root["id"])
        consumer = self._task("Consumer", parent_id=root["id"])
        self._contract(provider["id"], consumer["id"])
        generate_task_report(self.conn, root["id"], tmp_path)
        content = (tmp_path / "task-report.md").read_text(encoding="utf-8")
        assert "provider" in content.lower() or "consumer" in content.lower()

    def test_skipped_when_content_unchanged(self, tmp_path):
        root = self._task("Root")
        generate_task_report(self.conn, root["id"], tmp_path)
        result2 = generate_task_report(self.conn, root["id"], tmp_path)
        assert result2["skipped"]

    def test_force_regenerates(self, tmp_path):
        root = self._task("Root")
        generate_task_report(self.conn, root["id"], tmp_path)
        result2 = generate_task_report(self.conn, root["id"], tmp_path, force=True)
        assert not result2["skipped"]

    def test_nonexistent_root_raises(self, tmp_path):
        with pytest.raises(KeyError):
            generate_task_report(self.conn, "no-such-id", tmp_path)

    def test_output_contains_progress_bar(self, tmp_path):
        root = self._task("Root")
        generate_task_report(self.conn, root["id"], tmp_path)
        content = (tmp_path / "task-report.md").read_text(encoding="utf-8")
        # Progress bar uses block chars
        assert "█" in content or "░" in content or "%" in content

    def test_output_has_execution_phases_section(self, tmp_path):
        root = self._task("Root")
        t1 = self._task("T1", parent_id=root["id"])
        t2 = self._task("T2", parent_id=root["id"])
        tasks.add_task_edge(self.conn, edges=[{"source_id": t2["id"], "target_id": t1["id"]}], edge_type="depends_on")
        generate_task_report(self.conn, root["id"], tmp_path)
        content = (tmp_path / "task-report.md").read_text(encoding="utf-8")
        assert "Phase" in content or "phase" in content

    def test_output_dir_created_if_not_exists(self, tmp_path):
        nested = tmp_path / "deep" / "nested"
        root = self._task("Root")
        generate_task_report(self.conn, root["id"], nested)
        assert (nested / "task-report.md").exists()

    def test_markdown_has_task_id(self, tmp_path):
        root = self._task("Root Task")
        generate_task_report(self.conn, root["id"], tmp_path)
        content = (tmp_path / "task-report.md").read_text(encoding="utf-8")
        assert root["id"] in content

    def test_contracts_summary_in_report(self, tmp_path):
        """Contracts summary table with IDs appears when contracts exist."""
        root = self._task("Root")
        provider = self._task("Provider", parent_id=root["id"])
        consumer = self._task("Consumer", parent_id=root["id"])
        c = self._contract(provider["id"], consumer["id"])
        generate_task_report(self.conn, root["id"], tmp_path)
        content = (tmp_path / "task-report.md").read_text(encoding="utf-8")
        # Summary table
        assert "Contracts" in content
        assert "Pending" in content
        # Contract ID and task IDs must appear
        assert c["id"] in content
        assert provider["id"] in content
        assert consumer["id"] in content


# ---------------------------------------------------------------------------
# _render_header internals
# ---------------------------------------------------------------------------

class TestRenderHeader(TestTaskReportBase):

    def test_header_contains_task_title(self):
        root = self._task("My Feature")
        lines = _render_header(self.conn, root["id"])
        text = "\n".join(lines)
        assert "My Feature" in text

    def test_header_contains_root_task_id(self):
        root = self._task("Feature X")
        lines = _render_header(self.conn, root["id"])
        text = "\n".join(lines)
        assert root["id"] in text

    def test_header_contains_summary_section(self):
        root = self._task("Root")
        lines = _render_header(self.conn, root["id"])
        text = "\n".join(lines)
        assert "Summary" in text

    def test_attention_block_shows_open_questions(self):
        root = self._task("Root")
        child = self._task("Child", parent_id=root["id"])
        tasks.add_note(self.conn, child["id"], notes=[{"note_type": "question", "content": "What DB to use?"}])
        lines = _render_header(self.conn, root["id"])
        text = "\n".join(lines)
        assert "What DB to use?" in text or "question" in text.lower()


# ---------------------------------------------------------------------------
# _render_task internals
# ---------------------------------------------------------------------------

class TestRenderTask(TestTaskReportBase):

    def test_renders_task_title_at_level(self):
        root = self._task("My Task")
        lines = _render_task(self.conn, root["id"], level=2)
        text = "\n".join(lines)
        assert "My Task" in text
        # Level 2 → ## heading
        assert text.startswith("## ")

    def test_renders_description(self):
        root = self._task("Task")
        tasks.update_task(self.conn, root["id"], description="Implement auth flow")
        lines = _render_task(self.conn, root["id"], level=2)
        text = "\n".join(lines)
        assert "Implement auth flow" in text

    def test_renders_subtasks_at_deeper_level(self):
        root = self._task("Parent")
        self._task("Child", parent_id=root["id"])
        lines = _render_task(self.conn, root["id"], level=2)
        text = "\n".join(lines)
        assert "Child" in text
        # Child should be at level 3 → ###
        assert "### " in text

    def test_circular_reference_guard(self):
        """Visited set prevents infinite recursion on corrupt data."""
        root = self._task("Root")
        visited = {root["id"]}
        lines = _render_task(self.conn, root["id"], level=2, visited=visited)
        text = "\n".join(lines)
        assert "Circular" in text

    def test_spec_rendered_as_code_block(self):
        root = self._task("Task")
        tasks.update_task(self.conn, root["id"],
                          spec="def authenticate(user):\n    return jwt.sign(user)")
        lines = _render_task(self.conn, root["id"], level=2)
        text = "\n".join(lines)
        assert "```" in text
        assert "authenticate" in text

    def test_notes_rendered(self):
        root = self._task("Task")
        tasks.add_note(self.conn, root["id"], notes=[{"note_type": "risk", "content": "Might break prod"}])
        lines = _render_task(self.conn, root["id"], level=2)
        text = "\n".join(lines)
        assert "Might break prod" in text
        assert "risk" in text

    def test_edges_rendered(self):
        root = self._task("Root")
        t1 = self._task("Blocker", parent_id=root["id"])
        t2 = self._task("Dependent", parent_id=root["id"])
        tasks.add_task_edge(self.conn, edges=[{"source_id": t2["id"], "target_id": t1["id"]}], edge_type="depends_on")
        lines = _render_task(self.conn, t2["id"], level=2)
        text = "\n".join(lines)
        assert "depends_on" in text
        assert "Blocker" in text

    def test_code_refs_rendered_with_line_range(self):
        """Code refs show file path and line range — no source code embedded."""
        root = self._task("Task")
        self.conn.execute(
            "INSERT INTO nodes (kind, name, qualified_name, file_path, "
            "line_start, line_end, language, updated_at) "
            "VALUES ('Function', 'auth_fn', 'auth.py::auth_fn', 'auth.py', "
            "10, 20, 'python', ?)",
            (time.time(),),
        )
        self.conn.commit()
        node_id = self.conn.execute(
            "SELECT id FROM nodes WHERE qualified_name = 'auth.py::auth_fn'"
        ).fetchone()[0]
        tasks.link_task_code(self.conn, root["id"], links=[{"ref_type": "modifies", "code_node_id": node_id}])
        lines = _render_task(self.conn, root["id"], level=2)
        text = "\n".join(lines)
        assert "auth_fn" in text
        assert "modifies" in text
        # Must show file path and line range
        assert "auth.py" in text
        assert "L10" in text
        # Must NOT embed source code
        assert "```python" not in text
