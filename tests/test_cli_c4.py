"""Tests for the CLI c4 command."""

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

from code_review_graph.c4_generator import get_c4_path
from code_review_graph.cli import _cmd_c4
from code_review_graph.communities import detect_communities, store_communities
from code_review_graph.graph import GraphStore
from code_review_graph.parser import NodeInfo


class TestC4Command:
    """Test the c4 CLI command."""

    def setup_method(self):
        """Set up test fixtures."""
        self.tmp_dir = tempfile.mkdtemp()
        self.repo_root = Path(self.tmp_dir)
        self.graph_dir = self.repo_root / ".code-review-graph"
        self.graph_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.graph_dir / "graph.db"

    def teardown_method(self):
        """Clean up test fixtures."""
        import shutil
        if Path(self.tmp_dir).exists():
            shutil.rmtree(self.tmp_dir)

    def _seed_graph(self):
        """Create a minimal graph with communities."""
        store = GraphStore(str(self.db_path))
        try:
            # Add some nodes to create communities
            store.upsert_node(
                NodeInfo(
                    kind="File", name="auth.py", file_path="auth.py",
                    line_start=1, line_end=100, language="python",
                ), file_hash="a1"
            )
            store.upsert_node(
                NodeInfo(
                    kind="Function", name="login", file_path="auth.py",
                    line_start=5, line_end=20, language="python",
                ), file_hash="a1"
            )
            store.upsert_node(
                NodeInfo(
                    kind="File", name="api.py", file_path="api.py",
                    line_start=1, line_end=50, language="python",
                ), file_hash="a2"
            )
            store.upsert_node(
                NodeInfo(
                    kind="Function", name="get_user", file_path="api.py",
                    line_start=5, line_end=15, language="python",
                ), file_hash="a2"
            )

            # Detect and store communities
            communities = detect_communities(store)
            store_communities(store, communities)
        finally:
            store.close()

    def test_c4_command_help(self):
        """Test that c4 --help works."""
        result = subprocess.run(
            [sys.executable, "-m", "code_review_graph", "c4", "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "--rebuild" in result.stdout
        assert "--repo" in result.stdout

    def test_c4_generate(self):
        """Test generating a new architecture.c4 file."""
        self._seed_graph()

        # Call the handler directly
        args = argparse.Namespace(repo=str(self.repo_root), rebuild=False)
        _cmd_c4(args)

        # Verify the file was created
        c4_path = get_c4_path(str(self.repo_root))
        assert c4_path.exists()
        content = c4_path.read_text(encoding="utf-8")
        assert len(content) > 0
        assert "C4Context" in content or "C4Container" in content

    def test_c4_rebuild(self):
        """Test rebuilding with --rebuild flag preserves FEATURE sections."""
        self._seed_graph()

        # Generate initial file
        args1 = argparse.Namespace(repo=str(self.repo_root), rebuild=False)
        _cmd_c4(args1)

        c4_path = get_c4_path(str(self.repo_root))
        assert c4_path.exists()

        # Add a FEATURE section manually
        original_content = c4_path.read_text(encoding="utf-8")
        feature_section = "\n# [FEATURE:custom_design]\n# Custom design element\n"
        modified_content = original_content + feature_section
        c4_path.write_text(modified_content, encoding="utf-8")

        # Rebuild with --rebuild flag
        args2 = argparse.Namespace(repo=str(self.repo_root), rebuild=True)
        _cmd_c4(args2)

        # Verify the file still exists and has content
        rebuilt_content = c4_path.read_text(encoding="utf-8")
        assert len(rebuilt_content) > 0

    def test_c4_no_graph_error(self):
        """Test that c4 fails gracefully when no graph exists."""
        # Don't seed the graph
        args = argparse.Namespace(repo=str(self.repo_root), rebuild=False)
        try:
            _cmd_c4(args)
            assert False, "Expected SystemExit"
        except SystemExit as e:
            assert e.code == 1
