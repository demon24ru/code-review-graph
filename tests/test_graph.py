"""Tests for the graph storage and query engine."""

import tempfile
from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.parser import EdgeInfo, NodeInfo


class TestGraphStore:
    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.store = GraphStore(self.tmp.name)

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _make_file_node(self, path="/test/file.py"):
        return NodeInfo(
            kind="File", name=path, file_path=path,
            line_start=1, line_end=100, language="python",
        )

    def _make_func_node(self, name="my_func", path="/test/file.py", parent=None, is_test=False):
        return NodeInfo(
            kind="Test" if is_test else "Function",
            name=name, file_path=path,
            line_start=10, line_end=20, language="python",
            parent_name=parent, is_test=is_test,
        )

    def _make_class_node(self, name="MyClass", path="/test/file.py"):
        return NodeInfo(
            kind="Class", name=name, file_path=path,
            line_start=5, line_end=50, language="python",
        )

    def test_upsert_and_get_node(self):
        node = self._make_file_node()
        self.store.upsert_node(node)
        self.store.commit()

        result = self.store.get_node("/test/file.py")
        assert result is not None
        assert result.kind == "File"
        assert result.name == "/test/file.py"

    def test_upsert_function_node(self):
        func = self._make_func_node()
        self.store.upsert_node(func)
        self.store.commit()

        result = self.store.get_node("/test/file.py::my_func")
        assert result is not None
        assert result.kind == "Function"
        assert result.name == "my_func"

    def test_upsert_method_node(self):
        method = self._make_func_node(name="do_thing", parent="MyClass")
        self.store.upsert_node(method)
        self.store.commit()

        result = self.store.get_node("/test/file.py::MyClass.do_thing")
        assert result is not None
        assert result.parent_name == "MyClass"

    def test_upsert_edge(self):
        edge = EdgeInfo(
            kind="CALLS",
            source="/test/file.py::func_a",
            target="/test/file.py::func_b",
            file_path="/test/file.py",
            line=15,
        )
        self.store.upsert_edge(edge)
        self.store.commit()

        edges = self.store.get_edges_by_source("/test/file.py::func_a")
        assert len(edges) == 1
        assert edges[0].kind == "CALLS"
        assert edges[0].target_qualified == "/test/file.py::func_b"

    def test_remove_file_data(self):
        node = self._make_file_node()
        func = self._make_func_node()
        self.store.upsert_node(node)
        self.store.upsert_node(func)
        self.store.commit()

        self.store.remove_file_data("/test/file.py")
        self.store.commit()

        assert self.store.get_node("/test/file.py") is None
        assert self.store.get_node("/test/file.py::my_func") is None

    def test_store_file_nodes_edges(self):
        nodes = [self._make_file_node(), self._make_func_node()]
        edges = [
            EdgeInfo(
                kind="CONTAINS", source="/test/file.py",
                target="/test/file.py::my_func", file_path="/test/file.py",
            )
        ]
        self.store.store_file_nodes_edges("/test/file.py", nodes, edges)

        result = self.store.get_nodes_by_file("/test/file.py")
        assert len(result) == 2

    def test_search_nodes(self):
        self.store.upsert_node(self._make_func_node("authenticate"))
        self.store.upsert_node(self._make_func_node("authorize"))
        self.store.upsert_node(self._make_func_node("process"))
        self.store.commit()

        results = self.store.search_nodes("auth")
        names = {r.name for r in results}
        assert "authenticate" in names
        assert "authorize" in names
        assert "process" not in names

    def test_get_stats(self):
        self.store.upsert_node(self._make_file_node())
        self.store.upsert_node(self._make_func_node())
        self.store.upsert_node(self._make_class_node())
        self.store.upsert_edge(EdgeInfo(
            kind="CONTAINS", source="/test/file.py",
            target="/test/file.py::my_func", file_path="/test/file.py",
        ))
        self.store.commit()

        stats = self.store.get_stats()
        assert stats.total_nodes == 3
        assert stats.total_edges == 1
        assert stats.nodes_by_kind["File"] == 1
        assert stats.nodes_by_kind["Function"] == 1
        assert stats.nodes_by_kind["Class"] == 1
        assert "python" in stats.languages

    def test_impact_radius(self):
        # Create a chain: file_a -> func_a -> (calls) -> func_b in file_b
        self.store.upsert_node(self._make_file_node("/a.py"))
        self.store.upsert_node(self._make_func_node("func_a", "/a.py"))
        self.store.upsert_node(self._make_file_node("/b.py"))
        self.store.upsert_node(self._make_func_node("func_b", "/b.py"))
        self.store.upsert_edge(EdgeInfo(
            kind="CALLS", source="/a.py::func_a",
            target="/b.py::func_b", file_path="/a.py", line=10,
        ))
        self.store.commit()

        result = self.store.get_impact_radius(["/a.py"], max_depth=2)
        assert len(result["changed_nodes"]) > 0
        # func_b in /b.py should be impacted
        impacted_qns = {n.qualified_name for n in result["impacted_nodes"]}
        assert "/b.py::func_b" in impacted_qns or "/b.py" in impacted_qns

    def test_upsert_edge_preserves_multiple_call_sites(self):
        """Multiple CALLS edges to the same target from the same source on different lines."""
        edge1 = EdgeInfo(
            kind="CALLS", source="/test/file.py::caller",
            target="/test/file.py::helper", file_path="/test/file.py", line=10,
        )
        edge2 = EdgeInfo(
            kind="CALLS", source="/test/file.py::caller",
            target="/test/file.py::helper", file_path="/test/file.py", line=20,
        )
        self.store.upsert_edge(edge1)
        self.store.upsert_edge(edge2)
        self.store.commit()

        edges = self.store.get_edges_by_source("/test/file.py::caller")
        assert len(edges) == 2
        lines = {e.line for e in edges}
        assert lines == {10, 20}

    def test_metadata(self):
        self.store.set_metadata("test_key", "test_value")
        assert self.store.get_metadata("test_key") == "test_value"


class TestRelativePaths:
    """Verify that repo-relative path normalisation works correctly
    on both Windows and POSIX-style paths."""

    def setup_method(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name).resolve()
        crg_dir = self.root / ".code-review-graph"
        crg_dir.mkdir()
        self.store = GraphStore(str(crg_dir / "graph.db"), repo_root=self.root)

    def teardown_method(self):
        self.store.close()
        try:
            self.tmpdir.cleanup()
        except PermissionError:
            pass

    def _abs(self, rel: str) -> str:
        """Build absolute path string for this platform."""
        return str(self.root / rel)

    def test_node_file_path_is_relative(self):
        node = NodeInfo(
            kind="File", name="src/app.py",
            file_path=self._abs("src/app.py"),
            line_start=1, line_end=50, language="python",
        )
        self.store.upsert_node(node)
        self.store.commit()
        result = self.store.get_node("src/app.py")
        assert result is not None
        assert result.file_path == "src/app.py"

    def test_function_qualified_name_is_relative(self):
        node = NodeInfo(
            kind="Function", name="handle",
            file_path=self._abs("src/app.py"),
            line_start=5, line_end=20, language="python",
        )
        self.store.upsert_node(node)
        self.store.commit()
        result = self.store.get_node("src/app.py::handle")
        assert result is not None
        assert result.qualified_name == "src/app.py::handle"
        assert result.file_path == "src/app.py"

    def test_edge_all_paths_are_relative(self):
        edge = EdgeInfo(
            kind="CALLS",
            source=self._abs("src/app.py") + "::caller",
            target=self._abs("src/auth.py") + "::check",
            file_path=self._abs("src/app.py"),
            line=10,
        )
        self.store.upsert_edge(edge)
        self.store.commit()
        edges = self.store.get_edges_by_source("src/app.py::caller")
        assert len(edges) == 1
        assert edges[0].source_qualified == "src/app.py::caller"
        assert edges[0].target_qualified == "src/auth.py::check"
        assert edges[0].file_path == "src/app.py"

    def test_posix_forward_slash_input(self):
        """Forward-slash absolute paths (Linux-style) normalise correctly."""
        posix_path = self.root.as_posix() + "/pkg/mod.py"
        node = NodeInfo(
            kind="Function", name="run",
            file_path=posix_path,
            line_start=1, line_end=10, language="python",
        )
        self.store.upsert_node(node)
        self.store.commit()
        result = self.store.get_node("pkg/mod.py::run")
        assert result is not None
        assert result.file_path == "pkg/mod.py"

    def test_file_outside_repo_stored_as_posix_absolute(self):
        """Files outside repo_root are stored with their POSIX absolute path."""
        # Use resolved root.parent so Windows 8.3/long-name aliasing doesn't bite
        outside_path = str(self.root.parent / "outside_ext.py")
        node = NodeInfo(
            kind="Function", name="external_fn",
            file_path=outside_path,
            line_start=1, line_end=5, language="python",
        )
        self.store.upsert_node(node)
        self.store.commit()
        # qualified_name should be POSIX absolute (file is outside repo)
        posix_outside = Path(outside_path).as_posix()
        result = self.store.get_node(f"{posix_outside}::external_fn")
        assert result is not None
        assert "/" in result.file_path  # POSIX separator, not backslash

    def test_graphstore_without_crg_layout_no_inference(self):
        """GraphStore with flat DB path (no .code-review-graph dir) must not
        infer repo_root — paths stored as-is."""
        import tempfile as _tf
        tmp = _tf.NamedTemporaryFile(suffix=".db", delete=False)
        tmp_path = tmp.name
        tmp.close()
        store = GraphStore(tmp_path)  # no repo_root, not .code-review-graph layout
        try:
            assert store.repo_root is None
            node = NodeInfo(
                kind="Function", name="fn",
                file_path="/abs/path/file.py",
                line_start=1, line_end=5, language="python",
            )
            store.upsert_node(node)
            store.commit()
            result = store.get_node("/abs/path/file.py::fn")
            assert result is not None  # stored as-is, no relativisation
        finally:
            store.close()
            Path(tmp_path).unlink(missing_ok=True)
        assert self.store.get_metadata("nonexistent") is None
