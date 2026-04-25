"""Tests for community/cluster detection."""

import tempfile
from pathlib import Path

import pytest

from code_review_graph.communities import (
    IGRAPH_AVAILABLE,
    _compute_cohesion,
    _detect_file_based,
    _ensure_unique_names,
    _generate_community_name,
    detect_communities,
    get_architecture_overview,
    get_communities,
    store_communities,
)
from code_review_graph.graph import GraphEdge, GraphNode, GraphStore
from code_review_graph.parser import EdgeInfo, NodeInfo


class TestCommunities:
    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.store = GraphStore(self.tmp.name)

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _seed_two_clusters(self):
        """Seed two distinct clusters: auth (auth.py) and db (db.py)."""
        # Auth cluster
        self.store.upsert_node(
            NodeInfo(
                kind="File", name="auth.py", file_path="auth.py",
                line_start=1, line_end=100, language="python",
            ), file_hash="a1"
        )
        self.store.upsert_node(
            NodeInfo(
                kind="Function", name="login", file_path="auth.py",
                line_start=5, line_end=20, language="python",
            ), file_hash="a1"
        )
        self.store.upsert_node(
            NodeInfo(
                kind="Function", name="logout", file_path="auth.py",
                line_start=25, line_end=40, language="python",
            ), file_hash="a1"
        )
        self.store.upsert_node(
            NodeInfo(
                kind="Function", name="check_token", file_path="auth.py",
                line_start=45, line_end=60, language="python",
            ), file_hash="a1"
        )
        self.store.upsert_edge(EdgeInfo(
            kind="CALLS", source="auth.py::login",
            target="auth.py::check_token", file_path="auth.py", line=10,
        ))
        self.store.upsert_edge(EdgeInfo(
            kind="CALLS", source="auth.py::logout",
            target="auth.py::check_token", file_path="auth.py", line=30,
        ))

        # DB cluster
        self.store.upsert_node(
            NodeInfo(
                kind="File", name="db.py", file_path="db.py",
                line_start=1, line_end=100, language="python",
            ), file_hash="b1"
        )
        self.store.upsert_node(
            NodeInfo(
                kind="Function", name="connect", file_path="db.py",
                line_start=5, line_end=20, language="python",
            ), file_hash="b1"
        )
        self.store.upsert_node(
            NodeInfo(
                kind="Function", name="query", file_path="db.py",
                line_start=25, line_end=40, language="python",
            ), file_hash="b1"
        )
        self.store.upsert_node(
            NodeInfo(
                kind="Function", name="close", file_path="db.py",
                line_start=45, line_end=60, language="python",
            ), file_hash="b1"
        )
        self.store.upsert_edge(EdgeInfo(
            kind="CALLS", source="db.py::query",
            target="db.py::connect", file_path="db.py", line=30,
        ))
        self.store.upsert_edge(EdgeInfo(
            kind="CALLS", source="db.py::close",
            target="db.py::connect", file_path="db.py", line=50,
        ))

        # One cross-cluster edge
        self.store.upsert_edge(EdgeInfo(
            kind="CALLS", source="auth.py::login",
            target="db.py::query", file_path="auth.py", line=15,
        ))
        self.store.commit()

    def test_detect_communities_returns_list(self):
        """detect_communities returns a list."""
        self._seed_two_clusters()
        result = detect_communities(self.store, min_size=2)
        assert isinstance(result, list)

    @pytest.mark.skipif(not IGRAPH_AVAILABLE, reason="igraph not installed")
    def test_detect_finds_clusters(self):
        """With clear clusters and igraph, finds >= 2 communities."""
        self._seed_two_clusters()
        result = detect_communities(self.store, min_size=2)
        assert len(result) >= 2

    def test_community_has_required_fields(self):
        """Each community dict has required fields: name, size, cohesion, members."""
        self._seed_two_clusters()
        result = detect_communities(self.store, min_size=2)
        assert len(result) > 0
        for comm in result:
            assert "name" in comm
            assert "size" in comm
            assert "cohesion" in comm
            assert "members" in comm
            assert isinstance(comm["name"], str)
            assert isinstance(comm["size"], int)
            assert isinstance(comm["cohesion"], (int, float))
            assert isinstance(comm["members"], list)

    def test_store_and_retrieve_communities(self):
        """Communities can be stored and retrieved round-trip."""
        self._seed_two_clusters()
        communities = detect_communities(self.store, min_size=2)
        assert len(communities) > 0

        count = store_communities(self.store, communities)
        assert count == len(communities)

        retrieved = get_communities(self.store)
        assert len(retrieved) == len(communities)
        for comm in retrieved:
            assert "id" in comm
            assert "name" in comm
            assert "size" in comm

    def test_architecture_overview(self):
        """Architecture overview has required keys."""
        self._seed_two_clusters()
        communities = detect_communities(self.store, min_size=2)
        store_communities(self.store, communities)

        overview = get_architecture_overview(self.store)
        assert "communities" in overview
        assert "cross_community_edges" in overview
        assert "warnings" in overview
        assert isinstance(overview["communities"], list)
        assert isinstance(overview["cross_community_edges"], list)
        assert isinstance(overview["warnings"], list)

    def test_fallback_file_communities(self):
        """File-based fallback produces communities grouped by file."""
        self._seed_two_clusters()
        # Gather nodes and edges for file-based detection
        all_edges = self.store.get_all_edges()
        nodes = []
        for fp in self.store.get_all_files():
            nodes.extend(self.store.get_nodes_by_file(fp))

        result = _detect_file_based(nodes, all_edges, min_size=2)
        assert isinstance(result, list)
        assert len(result) >= 2
        for comm in result:
            assert "name" in comm
            assert "size" in comm
            assert comm["size"] >= 2

    def test_community_naming(self):
        """Community naming produces non-empty names."""
        self._seed_two_clusters()
        result = detect_communities(self.store, min_size=2)
        for comm in result:
            assert comm["name"]
            assert len(comm["name"]) > 0

    def test_community_naming_with_dominant_class(self):
        """When a class dominates (>40%), it appears in the name."""
        nodes = [
            GraphNode(
                id=1, kind="Class", name="AuthService", qualified_name="auth.py::AuthService",
                file_path="auth.py", line_start=1, line_end=100, language="python",
                parent_name=None, params=None, return_type=None, is_test=False,
                file_hash="x", extra={},
            ),
            GraphNode(
                id=2, kind="Function", name="login", qualified_name="auth.py::AuthService.login",
                file_path="auth.py", line_start=10, line_end=20, language="python",
                parent_name="AuthService", params=None, return_type=None, is_test=False,
                file_hash="x", extra={},
            ),
        ]
        name = _generate_community_name(nodes)
        assert name  # non-empty
        assert "authservice" in name.lower() or "auth" in name.lower()

    def test_community_naming_empty(self):
        """Empty member list produces 'empty' name."""
        name = _generate_community_name([])
        assert name == "empty"

    def test_cohesion_computation(self):
        """Cohesion is correctly computed as internal/(internal+external)."""
        member_qns = {"a", "b"}
        edges = [
            GraphEdge(
                id=1, kind="CALLS", source_qualified="a",
                target_qualified="b", file_path="f.py", line=1, extra={},
            ),
            GraphEdge(
                id=2, kind="CALLS", source_qualified="a",
                target_qualified="c", file_path="f.py", line=2, extra={},
            ),
        ]
        cohesion = _compute_cohesion(member_qns, edges)
        # 1 internal (a->b), 1 external (a->c) => 0.5
        assert cohesion == pytest.approx(0.5)

    def test_cohesion_all_internal(self):
        """All edges internal => cohesion = 1.0."""
        member_qns = {"a", "b"}
        edges = [
            GraphEdge(
                id=1, kind="CALLS", source_qualified="a",
                target_qualified="b", file_path="f.py", line=1, extra={},
            ),
        ]
        cohesion = _compute_cohesion(member_qns, edges)
        assert cohesion == pytest.approx(1.0)

    def test_cohesion_no_edges(self):
        """No edges => cohesion = 0.0."""
        member_qns = {"a", "b"}
        cohesion = _compute_cohesion(member_qns, [])
        assert cohesion == pytest.approx(0.0)

    def test_get_communities_sort_by(self):
        """get_communities respects sort_by parameter."""
        self._seed_two_clusters()
        communities = detect_communities(self.store, min_size=2)
        store_communities(self.store, communities)

        by_size = get_communities(self.store, sort_by="size")
        assert len(by_size) > 0
        # Sizes should be in descending order
        sizes = [c["size"] for c in by_size]
        assert sizes == sorted(sizes, reverse=True)

        by_name = get_communities(self.store, sort_by="name")
        names = [c["name"] for c in by_name]
        assert names == sorted(names)

    def test_get_communities_min_size_filter(self):
        """get_communities with min_size filters small communities."""
        self._seed_two_clusters()
        communities = detect_communities(self.store, min_size=1)
        store_communities(self.store, communities)

        # With very high min_size, should get empty
        result = get_communities(self.store, min_size=999)
        assert len(result) == 0

    def test_store_communities_clears_previous(self):
        """Storing communities clears previous community data."""
        self._seed_two_clusters()
        communities = detect_communities(self.store, min_size=2)
        store_communities(self.store, communities)

        first_count = len(get_communities(self.store))
        assert first_count > 0

        # Store again with empty list
        store_communities(self.store, [])
        assert len(get_communities(self.store)) == 0

    def test_detect_communities_empty_graph(self):
        """Detect on empty graph returns empty list."""
        result = detect_communities(self.store, min_size=2)
        assert result == []

    def test_igraph_available_is_bool(self):
        """IGRAPH_AVAILABLE is a boolean."""
        assert isinstance(IGRAPH_AVAILABLE, bool)

    def test_ensure_unique_names_deduplicates(self):
        """H-03: _ensure_unique_names appends -2, -3 for duplicate names."""
        communities = [
            {"name": "tests-no", "size": 3},
            {"name": "tests-no", "size": 2},
            {"name": "tests-no", "size": 1},
            {"name": "other", "size": 4},
        ]
        _ensure_unique_names(communities)
        names = [c["name"] for c in communities]
        # First keeps original, duplicates get -2, -3
        assert names == ["tests-no", "tests-no-2", "tests-no-3", "other"]

    def test_ensure_unique_names_no_duplicates_unchanged(self):
        """H-03: _ensure_unique_names leaves already-unique names intact."""
        communities = [
            {"name": "auth", "size": 3},
            {"name": "db", "size": 2},
            {"name": "tests", "size": 1},
        ]
        _ensure_unique_names(communities)
        names = [c["name"] for c in communities]
        assert names == ["auth", "db", "tests"]

    def test_detect_communities_names_are_unique(self):
        """H-03: detect_communities returns communities with unique names."""
        self._seed_two_clusters()
        result = detect_communities(self.store, min_size=1)
        names = [c["name"] for c in result]
        # All names must be unique
        assert len(names) == len(set(names)), f"Duplicate names found: {names}"


class TestGetCommunity:
    """Tests for get_community_func tool."""

    def setup_method(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.repo_root = self.tmpdir.name
        # Create .code-review-graph directory
        crg_dir = Path(self.repo_root) / ".code-review-graph"
        crg_dir.mkdir(exist_ok=True)
        db_path = crg_dir / "graph.db"
        self.store = GraphStore(str(db_path))

    def teardown_method(self):
        self.store.close()
        import time
        time.sleep(0.1)  # Give file system time to release lock
        try:
            self.tmpdir.cleanup()
        except Exception:
            pass  # Ignore cleanup errors on Windows

    def _seed_two_similar_communities(self):
        """Seed two communities with similar names: 'task-core' and 'task-analysis'."""
        # First community: task-core
        self.store.upsert_node(
            NodeInfo(
                kind="File", name="task_core.py", file_path="task_core.py",
                line_start=1, line_end=100, language="python",
            ), file_hash="tc1"
        )
        self.store.upsert_node(
            NodeInfo(
                kind="Function", name="create_task", file_path="task_core.py",
                line_start=5, line_end=20, language="python",
            ), file_hash="tc1"
        )
        self.store.upsert_node(
            NodeInfo(
                kind="Function", name="update_task", file_path="task_core.py",
                line_start=25, line_end=40, language="python",
            ), file_hash="tc1"
        )

        # Second community: task-analysis
        self.store.upsert_node(
            NodeInfo(
                kind="File", name="task_analysis.py", file_path="task_analysis.py",
                line_start=1, line_end=100, language="python",
            ), file_hash="ta1"
        )
        self.store.upsert_node(
            NodeInfo(
                kind="Function", name="analyze_task", file_path="task_analysis.py",
                line_start=5, line_end=20, language="python",
            ), file_hash="ta1"
        )
        self.store.upsert_node(
            NodeInfo(
                kind="Function", name="score_task", file_path="task_analysis.py",
                line_start=25, line_end=40, language="python",
            ), file_hash="ta1"
        )
        self.store.commit()

        # Detect and store communities
        communities = detect_communities(self.store, min_size=1)
        # Manually set names to ensure they match the pattern
        for i, comm in enumerate(communities):
            if i == 0:
                comm["name"] = "task-core"
            elif i == 1:
                comm["name"] = "task-analysis"
        store_communities(self.store, communities)

    def test_get_community_ambiguous_name_returns_matches_list(self):
        """When multiple communities match a name, return ambiguous status with matches list."""
        from code_review_graph.tools.community_tools import get_community_func

        self._seed_two_similar_communities()

        # Call with ambiguous name "task" that matches both "task-core" and "task-analysis"
        result = get_community_func(community_name="task", repo_root=self.repo_root)

        # Should return ambiguous status
        assert result["status"] == "ambiguous"
        assert "Multiple communities match" in result["summary"]
        assert "task" in result["summary"]
        assert "Use community_id to select one" in result["summary"]

        # Should have matches list with both communities
        assert "matches" in result
        assert isinstance(result["matches"], list)
        # Should have at least 2 matches (may have more if other communities match)
        assert len(result["matches"]) >= 2

        # Check that our two communities are in the matches
        match_names = [m["name"] for m in result["matches"]]
        assert "task-core" in match_names
        assert "task-analysis" in match_names

        # Each match should have id and name
        for match in result["matches"]:
            assert "id" in match
            assert "name" in match

    def test_get_community_single_match_works_as_before(self):
        """When only one community matches, return it normally."""
        from code_review_graph.tools.community_tools import get_community_func

        self._seed_two_similar_communities()

        # Call with specific name that matches only one
        result = get_community_func(community_name="task-core", repo_root=self.repo_root)

        # Should return ok status with the community
        assert result["status"] == "ok"
        assert "community" in result
        assert result["community"]["name"] == "task-core"

    def test_get_community_no_match_returns_not_found(self):
        """When no communities match, return not_found status."""
        from code_review_graph.tools.community_tools import get_community_func

        self._seed_two_similar_communities()

        # Call with name that doesn't match anything
        result = get_community_func(community_name="nonexistent", repo_root=self.repo_root)

        # Should return not_found status
        assert result["status"] == "not_found"
        assert "No community found" in result["summary"]

    def _seed_two_clusters(self):
        """Seed two distinct clusters: auth (auth.py) and db (db.py) with a cross-cluster edge."""
        from code_review_graph.parser import EdgeInfo, NodeInfo
        # Auth cluster
        self.store.upsert_node(NodeInfo(kind="File", name="auth.py", file_path="auth.py", line_start=1, line_end=100, language="python"), file_hash="a1")
        self.store.upsert_node(NodeInfo(kind="Function", name="login", file_path="auth.py", line_start=5, line_end=20, language="python"), file_hash="a1")
        self.store.upsert_node(NodeInfo(kind="Function", name="logout", file_path="auth.py", line_start=25, line_end=40, language="python"), file_hash="a1")
        self.store.upsert_node(NodeInfo(kind="Function", name="check_token", file_path="auth.py", line_start=45, line_end=60, language="python"), file_hash="a1")
        self.store.upsert_edge(EdgeInfo(kind="CALLS", source="auth.py::login", target="auth.py::check_token", file_path="auth.py", line=10))
        self.store.upsert_edge(EdgeInfo(kind="CALLS", source="auth.py::logout", target="auth.py::check_token", file_path="auth.py", line=30))
        # DB cluster
        self.store.upsert_node(NodeInfo(kind="File", name="db.py", file_path="db.py", line_start=1, line_end=100, language="python"), file_hash="b1")
        self.store.upsert_node(NodeInfo(kind="Function", name="connect", file_path="db.py", line_start=5, line_end=20, language="python"), file_hash="b1")
        self.store.upsert_node(NodeInfo(kind="Function", name="query", file_path="db.py", line_start=25, line_end=40, language="python"), file_hash="b1")
        self.store.upsert_node(NodeInfo(kind="Function", name="close", file_path="db.py", line_start=45, line_end=60, language="python"), file_hash="b1")
        self.store.upsert_edge(EdgeInfo(kind="CALLS", source="db.py::query", target="db.py::connect", file_path="db.py", line=30))
        self.store.upsert_edge(EdgeInfo(kind="CALLS", source="db.py::close", target="db.py::connect", file_path="db.py", line=50))
        # Cross-cluster edge: login calls db.connect
        self.store.upsert_edge(EdgeInfo(kind="CALLS", source="auth.py::login", target="db.py::connect", file_path="auth.py", line=15))
        self.store.commit()

    def test_overview_communities_no_members(self):
        """Architecture overview communities have member_count but no members/member_qns."""
        from code_review_graph.tools.community_tools import get_architecture_overview_func

        self._seed_two_clusters()
        communities = detect_communities(self.store, min_size=2)
        store_communities(self.store, communities)

        result = get_architecture_overview_func(repo_root=self.repo_root)

        assert result["status"] == "ok"
        assert "communities" in result
        communities_list = result["communities"]
        assert isinstance(communities_list, list)
        assert len(communities_list) > 0

        # Each community should have member_count but NOT members or member_qns
        for comm in communities_list:
            assert "member_count" in comm
            assert isinstance(comm["member_count"], int)
            assert "members" not in comm
            assert "member_qns" not in comm
            # Verify lightweight fields are present
            assert "id" in comm
            assert "name" in comm
            assert "size" in comm
            assert "cohesion" in comm
            assert "dominant_language" in comm

    def test_overview_has_cross_community_coupling(self):
        """Architecture overview has cross_community_coupling (aggregated pairs) not individual edges."""
        from code_review_graph.tools.community_tools import get_architecture_overview_func

        self._seed_two_clusters()
        communities = detect_communities(self.store, min_size=2)
        store_communities(self.store, communities)

        result = get_architecture_overview_func(repo_root=self.repo_root)

        assert result["status"] == "ok"
        # Should NOT have individual cross_community_edges
        assert "cross_community_edges" not in result
        # Should have scalar total_cross_pairs (always present even if 0)
        assert "total_cross_pairs" in result
        assert isinstance(result["total_cross_pairs"], int)
        # With cross-cluster edge seeded, there should be at least 1 pair
        assert result["total_cross_pairs"] >= 1

        # cross_community_coupling is present when non-empty (pruned if empty)
        coupling = result.get("cross_community_coupling", [])
        assert isinstance(coupling, list)
        assert len(coupling) >= 1

        # Each coupling item should have communities (string) and edge_count (int)
        for item in coupling:
            assert "communities" in item
            assert "edge_count" in item
            assert isinstance(item["communities"], str)
            assert isinstance(item["edge_count"], int)
            assert " <-> " in item["communities"]  # Bidirectional pair format

    def test_get_community_include_members_true_has_hints(self):
        """P-03: get_community_tool(include_members=True) has non-empty _hints.next_steps."""
        from code_review_graph.hints import reset_session
        from code_review_graph.tools.community_tools import get_community_func

        reset_session()
        self._seed_two_similar_communities()

        result = get_community_func(community_name="task-core", repo_root=self.repo_root)
        assert result["status"] == "ok"

        # Now call with include_members=True
        reset_session()
        result = get_community_func(
            community_name="task-core",
            include_members=True,
            repo_root=self.repo_root,
        )
        assert result["status"] == "ok"
        hints = result.get("_hints", {})
        next_steps = hints.get("next_steps", [])
        assert len(next_steps) > 0, "_hints.next_steps should be non-empty when include_members=True"
        tool_names = [s["tool"] for s in next_steps]
        assert "query_graph_tool" in tool_names
        assert "trace_dataflow_tool" in tool_names
        assert "get_flow_tool" in tool_names

    def test_get_community_not_found_has_hints(self):
        """P-04: get_community_tool not_found response has _hints.next_actions."""
        from code_review_graph.tools.community_tools import get_community_func

        self._seed_two_similar_communities()

        result = get_community_func(community_name="nonexistent_xyz", repo_root=self.repo_root)
        assert result["status"] == "not_found"
        hints = result.get("_hints", {})
        next_actions = hints.get("next_actions", [])
        assert "list_communities_tool" in next_actions

    def test_is_test_community_filters_fixture(self):
        """_is_test_community returns True for communities with 'fixture' in name."""
        from code_review_graph.tools.community_tools import _is_test_community

        assert _is_test_community({"name": "fixtures-db"}) is True
        assert _is_test_community({"name": "my-fixtures"}) is True
        assert _is_test_community({"name": "fixture-helpers"}) is True
        assert _is_test_community({"name": "FIXTURE-set"}) is True  # case-insensitive
        # Should NOT filter non-test communities
        assert _is_test_community({"name": "auth-core"}) is False
        assert _is_test_community({"name": "graph-db"}) is False

    def test_is_test_community_filters_test_prefix(self):
        """_is_test_community returns True for communities starting with 'test'."""
        from code_review_graph.tools.community_tools import _is_test_community

        assert _is_test_community({"name": "tests-core"}) is True
        assert _is_test_community({"name": "tests-no"}) is True
        assert _is_test_community({"name": "test_helpers"}) is True
        assert _is_test_community({"name": "auth-core"}) is False

    def test_list_communities_exclude_tests_filters_test_communities(self):
        """list_communities_func(exclude_tests=True) should exclude test/fixture communities."""
        from code_review_graph.tools.community_tools import list_communities_func

        self._seed_two_similar_communities()

        # Manually override community names to include test and fixture entries
        all_comms = get_communities(self.store)
        # Rename them: one production, one test, one fixture
        for i, comm in enumerate(all_comms):
            if i == 0:
                comm["name"] = "auth-core"
            elif i == 1:
                comm["name"] = "tests-db"
        store_communities(self.store, all_comms)

        # With exclude_tests=False: should see all communities
        result_all = list_communities_func(repo_root=self.repo_root, exclude_tests=False)
        assert result_all["status"] == "ok"
        all_names = {c["name"] for c in result_all["communities"]}
        assert "auth-core" in all_names
        assert "tests-db" in all_names

        # With exclude_tests=True: should exclude test community
        result_filtered = list_communities_func(repo_root=self.repo_root, exclude_tests=True)
        assert result_filtered["status"] == "ok"
        filtered_names = {c["name"] for c in result_filtered["communities"]}
        assert "auth-core" in filtered_names
        assert "tests-db" not in filtered_names

    def test_list_communities_exclude_tests_filters_fixture_communities(self):
        """list_communities_func(exclude_tests=True) should exclude fixture communities."""
        from code_review_graph.tools.community_tools import list_communities_func

        self._seed_two_similar_communities()

        # Add a fixture community by renaming
        all_comms = get_communities(self.store)
        for i, comm in enumerate(all_comms):
            if i == 0:
                comm["name"] = "graph-utils"
            elif i == 1:
                comm["name"] = "fixtures-helpers"
        store_communities(self.store, all_comms)

        result = list_communities_func(repo_root=self.repo_root, exclude_tests=True)
        assert result["status"] == "ok"
        names = {c["name"] for c in result["communities"]}
        assert "graph-utils" in names
        assert "fixtures-helpers" not in names
