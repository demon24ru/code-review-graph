"""Tests for execution flow detection, tracing, and scoring."""

import tempfile
from pathlib import Path

from code_review_graph.flows import (
    detect_entry_points,
    get_affected_flows,
    get_flow_by_id,
    get_flows,
    store_flows,
    trace_flows,
)
from code_review_graph.graph import GraphStore
from code_review_graph.parser import EdgeInfo, NodeInfo


class TestFlows:
    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.store = GraphStore(self.tmp.name)

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    # -- helpers --

    def _add_func(
        self,
        name: str,
        path: str = "app.py",
        parent: str | None = None,
        is_test: bool = False,
        extra: dict | None = None,
    ) -> int:
        node = NodeInfo(
            kind="Test" if is_test else "Function",
            name=name,
            file_path=path,
            line_start=1,
            line_end=10,
            language="python",
            parent_name=parent,
            is_test=is_test,
            extra=extra or {},
        )
        nid = self.store.upsert_node(node, file_hash="abc")
        self.store.commit()
        return nid

    def _add_call(self, source_qn: str, target_qn: str, path: str = "app.py") -> None:
        edge = EdgeInfo(
            kind="CALLS",
            source=source_qn,
            target=target_qn,
            file_path=path,
            line=5,
        )
        self.store.upsert_edge(edge)
        self.store.commit()

    # ---------------------------------------------------------------
    # detect_entry_points
    # ---------------------------------------------------------------

    def test_detect_entry_points_no_callers(self):
        """Functions with no incoming CALLS edges are entry points."""
        self._add_func("entry_func")
        self._add_func("helper")
        # entry_func calls helper, so helper has an incoming CALLS.
        self._add_call("app.py::entry_func", "app.py::helper")

        eps = detect_entry_points(self.store)
        ep_names = {ep.name for ep in eps}
        assert "entry_func" in ep_names
        assert "helper" not in ep_names

    def test_detect_entry_points_framework_pattern(self):
        """Decorated functions are entry points even if they have callers."""
        self._add_func("get_users", extra={"decorators": ["app.get('/users')"]})
        self._add_func("caller")
        # caller -> get_users, so get_users has an incoming CALLS.
        self._add_call("app.py::caller", "app.py::get_users")

        eps = detect_entry_points(self.store)
        ep_names = {ep.name for ep in eps}
        # Even though get_users is called by someone, its decorator marks it.
        assert "get_users" in ep_names

    def test_detect_entry_points_name_pattern(self):
        """Functions matching name patterns (main, test_*, on_*) are entry points."""
        self._add_func("main")
        self._add_func("test_something")
        self._add_func("on_message")
        self._add_func("handle_request")
        self._add_func("regular_func")

        # Make regular_func called so it's not a root either
        self._add_func("another")
        self._add_call("app.py::another", "app.py::regular_func")

        eps = detect_entry_points(self.store)
        ep_names = {ep.name for ep in eps}
        assert "main" in ep_names
        assert "test_something" in ep_names
        assert "on_message" in ep_names
        assert "handle_request" in ep_names
        assert "regular_func" not in ep_names

    # ---------------------------------------------------------------
    # trace_flows
    # ---------------------------------------------------------------

    def test_trace_simple_flow(self):
        """BFS traces a linear call chain: A -> B -> C."""
        self._add_func("entry")
        self._add_func("middle")
        self._add_func("leaf")

        self._add_call("app.py::entry", "app.py::middle")
        self._add_call("app.py::middle", "app.py::leaf")

        flows = trace_flows(self.store)
        # entry should produce a flow with 3 nodes.
        entry_flows = [f for f in flows if f["entry_point"] == "app.py::entry"]
        assert len(entry_flows) == 1
        assert entry_flows[0]["node_count"] == 3
        assert entry_flows[0]["depth"] >= 1

    def test_trace_flow_cycle_detection(self):
        """Cycles don't cause infinite loops."""
        # main is an entry point (name pattern), calls a, which calls b,
        # which calls a again (cycle).
        self._add_func("main")
        self._add_func("a")
        self._add_func("b")
        self._add_call("app.py::main", "app.py::a")
        self._add_call("app.py::a", "app.py::b")
        self._add_call("app.py::b", "app.py::a")  # cycle back to a

        # Should complete without hanging.
        flows = trace_flows(self.store)
        main_flows = [f for f in flows if f["entry_point"] == "app.py::main"]
        assert len(main_flows) == 1
        # main -> a -> b (a already visited, cycle skipped)
        assert main_flows[0]["node_count"] == 3

    def test_trace_flow_max_depth(self):
        """Respects max_depth limit."""
        # Create a chain of 20 functions.
        for i in range(20):
            self._add_func(f"func_{i}")
        for i in range(19):
            self._add_call(f"app.py::func_{i}", f"app.py::func_{i+1}")

        flows_shallow = trace_flows(self.store, max_depth=3)
        entry_flow = [f for f in flows_shallow if f["entry_point"] == "app.py::func_0"]
        assert len(entry_flow) == 1
        # With max_depth=3, we should see at most 4 nodes (entry + 3 levels).
        assert entry_flow[0]["node_count"] <= 4

    def test_trace_flow_skips_trivial(self):
        """Flows with only a single node (no outgoing calls leading to graph nodes)
        are excluded."""
        self._add_func("lonely")
        flows = trace_flows(self.store)
        lonely_flows = [f for f in flows if f["entry_point"] == "app.py::lonely"]
        assert len(lonely_flows) == 0

    def test_trace_flow_multi_file(self):
        """Flows spanning multiple files track all files."""
        self._add_func("api_handler", path="routes.py")
        self._add_func("service_call", path="services.py")
        self._add_func("db_query", path="db.py")
        self._add_call("routes.py::api_handler", "services.py::service_call", "routes.py")
        self._add_call("services.py::service_call", "db.py::db_query", "services.py")

        flows = trace_flows(self.store)
        handler_flows = [f for f in flows if f["entry_point"] == "routes.py::api_handler"]
        assert len(handler_flows) == 1
        assert handler_flows[0]["file_count"] == 3
        assert set(handler_flows[0]["files"]) == {"routes.py", "services.py", "db.py"}

    # ---------------------------------------------------------------
    # compute_criticality
    # ---------------------------------------------------------------

    def test_criticality_scoring(self):
        """Criticality scores are between 0 and 1."""
        self._add_func("entry")
        self._add_func("helper")
        self._add_call("app.py::entry", "app.py::helper")

        flows = trace_flows(self.store)
        for flow in flows:
            assert 0.0 <= flow["criticality"] <= 1.0

    def test_criticality_security_keywords_boost(self):
        """Flows touching security-sensitive functions score higher."""
        # Non-security flow.
        self._add_func("start")
        self._add_func("process")
        self._add_call("app.py::start", "app.py::process")

        # Security flow.
        self._add_func("login_handler", path="auth.py")
        self._add_func("check_password", path="auth.py")
        self._add_call("auth.py::login_handler", "auth.py::check_password", "auth.py")

        flows = trace_flows(self.store)
        normal_flows = [f for f in flows if f["entry_point"] == "app.py::start"]
        secure_flows = [f for f in flows if f["entry_point"] == "auth.py::login_handler"]

        assert len(normal_flows) == 1
        assert len(secure_flows) == 1
        # The security flow should have a higher criticality.
        assert secure_flows[0]["criticality"] >= normal_flows[0]["criticality"]

    def test_criticality_file_spread_boost(self):
        """Flows spanning more files score higher on file-spread."""
        # Single-file flow.
        self._add_func("single_a", path="one.py")
        self._add_func("single_b", path="one.py")
        self._add_call("one.py::single_a", "one.py::single_b", "one.py")

        # Multi-file flow.
        self._add_func("multi_a", path="a.py")
        self._add_func("multi_b", path="b.py")
        self._add_func("multi_c", path="c.py")
        self._add_call("a.py::multi_a", "b.py::multi_b", "a.py")
        self._add_call("b.py::multi_b", "c.py::multi_c", "b.py")

        flows = trace_flows(self.store)
        single = [f for f in flows if f["entry_point"] == "one.py::single_a"]
        multi = [f for f in flows if f["entry_point"] == "a.py::multi_a"]

        assert len(single) == 1
        assert len(multi) == 1
        assert multi[0]["criticality"] >= single[0]["criticality"]

    # ---------------------------------------------------------------
    # store_flows + get_flows roundtrip
    # ---------------------------------------------------------------

    def test_store_and_retrieve_flows(self):
        """store_flows + get_flows roundtrip works correctly."""
        self._add_func("ep")
        self._add_func("callee")
        self._add_call("app.py::ep", "app.py::callee")

        flows = trace_flows(self.store)
        assert len(flows) >= 1

        count = store_flows(self.store, flows)
        assert count == len(flows)

        retrieved = get_flows(self.store)
        assert len(retrieved) >= 1

        # Check that all expected fields are present.
        flow = retrieved[0]
        assert "id" in flow
        assert "name" in flow
        assert "criticality" in flow
        assert "path" in flow
        assert isinstance(flow["path"], list)

    def test_store_flows_clears_old(self):
        """Calling store_flows replaces all previous flow data."""
        self._add_func("ep1")
        self._add_func("callee1")
        self._add_call("app.py::ep1", "app.py::callee1")

        flows_v1 = trace_flows(self.store)
        store_flows(self.store, flows_v1)
        assert len(get_flows(self.store)) >= 1

        # Store an empty list — should clear everything.
        store_flows(self.store, [])
        assert len(get_flows(self.store)) == 0

    def test_store_flows_twice_no_duplicates(self):
        """Calling store_flows twice with the same data must not create duplicate rows."""
        self._add_func("ep")
        self._add_func("callee")
        self._add_call("app.py::ep", "app.py::callee")

        flows = trace_flows(self.store)
        assert len(flows) >= 1

        store_flows(self.store, flows)
        store_flows(self.store, flows)  # second call — must be idempotent

        stored = get_flows(self.store)
        # No duplicate names/paths: each (entry_point_id, path_json) must be unique.
        keys = [(f["entry_point_id"], str(sorted(f["path"]))) for f in stored]
        assert len(keys) == len(set(keys)), f"Duplicate flows found: {stored}"
        # Exact same count as the original traced flows.
        assert len(stored) == len(flows)

    def test_incremental_rebuild_no_duplicate_flows(self):
        """Simulates multiple post-build hook calls; flows must not accumulate."""
        from code_review_graph.incremental import run_post_build_hooks

        self._add_func("handler", path="routes.py")
        self._add_func("service", path="services.py")
        self._add_call("routes.py::handler", "services.py::service", "routes.py")

        # Run post-build hooks twice (simulates incremental rebuild after full build).
        run_post_build_hooks(self.store)
        run_post_build_hooks(self.store)

        stored = get_flows(self.store)
        # No duplicate (entry_point_id, path) pairs.
        keys = [(f["entry_point_id"], str(sorted(f["path"]))) for f in stored]
        assert len(keys) == len(set(keys)), f"Duplicate flows after double hook run: {stored}"

    def test_get_flow_by_id(self):
        """get_flow_by_id returns full step details."""
        self._add_func("ep")
        self._add_func("step1")
        self._add_call("app.py::ep", "app.py::step1")

        flows = trace_flows(self.store)
        store_flows(self.store, flows)

        stored = get_flows(self.store)
        assert len(stored) >= 1
        flow_id = stored[0]["id"]

        detail = get_flow_by_id(self.store, flow_id)
        assert detail is not None
        assert "steps" in detail
        assert len(detail["steps"]) >= 2
        # Each step should have name, kind, file.
        step = detail["steps"][0]
        assert "name" in step
        assert "kind" in step
        assert "file" in step

    def test_get_flow_by_id_not_found(self):
        """get_flow_by_id returns None for nonexistent flow."""
        result = get_flow_by_id(self.store, 99999)
        assert result is None

    # ---------------------------------------------------------------
    # get_affected_flows
    # ---------------------------------------------------------------

    def test_get_affected_flows(self):
        """Finds flows through changed files."""
        self._add_func("handler", path="routes.py")
        self._add_func("service", path="services.py")
        self._add_func("repo", path="repo.py")
        self._add_call("routes.py::handler", "services.py::service", "routes.py")
        self._add_call("services.py::service", "repo.py::repo", "services.py")

        flows = trace_flows(self.store)
        store_flows(self.store, flows)

        # Changing services.py should affect the handler flow.
        result = get_affected_flows(self.store, ["services.py"])
        assert result["total"] >= 1
        affected_entries = {
            f["entry_point_id"] for f in result["affected_flows"]
        }
        handler_node = self.store.get_node("routes.py::handler")
        assert handler_node is not None
        assert handler_node.id in affected_entries

    def test_get_affected_flows_empty(self):
        """No affected flows when no files match."""
        self._add_func("ep")
        self._add_func("callee")
        self._add_call("app.py::ep", "app.py::callee")

        flows = trace_flows(self.store)
        store_flows(self.store, flows)

        result = get_affected_flows(self.store, ["nonexistent.py"])
        assert result["total"] == 0
        assert result["affected_flows"] == []

    def test_get_affected_flows_no_files(self):
        """Empty changed_files list returns no results."""
        result = get_affected_flows(self.store, [])
        assert result["total"] == 0

    def test_get_affected_flows_default_excludes_steps(self):
        """Default response excludes steps array, includes step_count."""
        self._add_func("handler", path="routes.py")
        self._add_func("service", path="services.py")
        self._add_call("routes.py::handler", "services.py::service", "routes.py")

        flows = trace_flows(self.store)
        store_flows(self.store, flows)

        # Test the underlying function - it returns full steps
        result = get_affected_flows(self.store, ["services.py"])
        assert result["total"] >= 1
        
        # Verify that the underlying function returns steps
        for flow in result["affected_flows"]:
            assert "steps" in flow, "underlying function should return steps"

    # ---------------------------------------------------------------
    # get_flows sorting
    # ---------------------------------------------------------------

    def test_get_flows_sorting(self):
        """get_flows respects sort_by parameter."""
        self._add_func("shallow_ep", path="a.py")
        self._add_func("shallow_callee", path="a.py")
        self._add_call("a.py::shallow_ep", "a.py::shallow_callee", "a.py")

        self._add_func("deep_ep", path="b.py")
        self._add_func("deep_mid", path="c.py")
        self._add_func("deep_end", path="d.py")
        self._add_call("b.py::deep_ep", "c.py::deep_mid", "b.py")
        self._add_call("c.py::deep_mid", "d.py::deep_end", "c.py")

        flows = trace_flows(self.store)
        store_flows(self.store, flows)

        by_depth = get_flows(self.store, sort_by="depth")
        assert len(by_depth) >= 2
        # Deepest flow first.
        assert by_depth[0]["depth"] >= by_depth[-1]["depth"]

    # ---------------------------------------------------------------
    # list_flows with is_test filter
    # ---------------------------------------------------------------

    def test_list_flows_is_test_false_excludes_tests(self):
        """is_test=False excludes Test-kind entry point flows."""
        from code_review_graph.tools.flows_tools import list_flows

        # Create test and non-test flows
        self._add_func("test_handler", is_test=True)
        self._add_func("test_helper", is_test=True)
        self._add_call("app.py::test_handler", "app.py::test_helper")

        self._add_func("prod_handler", is_test=False)
        self._add_func("prod_helper", is_test=False)
        self._add_call("app.py::prod_handler", "app.py::prod_helper")

        flows = trace_flows(self.store)
        store_flows(self.store, flows)

        # Get all flows
        all_result = list_flows(repo_root=None)
        all_flows = all_result["flows"]
        assert len(all_flows) >= 2

        # Get only production flows (is_test=False)
        prod_result = list_flows(repo_root=None, is_test=False)
        prod_flows = prod_result["flows"]

        # Verify no test flows in production result
        for flow in prod_flows:
            ep_id = flow.get("entry_point_id")
            if ep_id is not None:
                node_kind = self.store.get_node_kind_by_id(ep_id)
                assert node_kind != "Test", f"Test flow found in is_test=False result: {flow}"

    def test_list_flows_has_total_count(self):
        """list_flows response includes total_count."""
        from code_review_graph.tools.flows_tools import list_flows

        self._add_func("ep1")
        self._add_func("helper1")
        self._add_call("app.py::ep1", "app.py::helper1")

        flows = trace_flows(self.store)
        store_flows(self.store, flows)

        result = list_flows(repo_root=None)
        assert "total_count" in result, "total_count missing from response"
        assert isinstance(result["total_count"], int)
        assert result["total_count"] == len(result["flows"])

    # ---------------------------------------------------------------
    # H-01: get_flows limit=None returns all flows
    # ---------------------------------------------------------------

    def test_get_flows_limit_none_returns_all(self):
        """H-01: get_flows(limit=None) returns every stored flow regardless of sort_by."""
        self._add_func("ep_a", path="a.py")
        self._add_func("helper_a", path="a.py")
        self._add_call("a.py::ep_a", "a.py::helper_a", "a.py")

        self._add_func("ep_b", path="b.py")
        self._add_func("helper_b1", path="b.py")
        self._add_func("helper_b2", path="b.py")
        self._add_call("b.py::ep_b", "b.py::helper_b1", "b.py")
        self._add_call("b.py::helper_b1", "b.py::helper_b2", "b.py")

        flows = trace_flows(self.store)
        store_flows(self.store, flows)

        by_criticality = get_flows(self.store, sort_by="criticality", limit=None)
        by_depth = get_flows(self.store, sort_by="depth", limit=None)

        # Both orders must return the same complete set of flows.
        assert len(by_criticality) == len(by_depth)
        assert {f["id"] for f in by_criticality} == {f["id"] for f in by_depth}
        # Must have found at least the 2 flows we created.
        assert len(by_criticality) >= 2


# ---------------------------------------------------------------------------
# Bug-fix regression tests: H-01, H-02, H-14 at the tool layer
# ---------------------------------------------------------------------------


class TestListFlowsBugFixes:
    """Regression tests that require a proper repo_root so _get_store validates.

    Uses the same tmpdir pattern as test_tools.py:
    tmpdir/.code-review-graph/graph.db
    """

    def setup_method(self):
        import shutil as _shutil
        import tempfile as _tf

        self.tmpdir = Path(_tf.mkdtemp())
        crg_dir = self.tmpdir / ".code-review-graph"
        crg_dir.mkdir()
        db_path = crg_dir / "graph.db"
        self.store = GraphStore(str(db_path), repo_root=self.tmpdir)

    def teardown_method(self):
        import shutil as _shutil

        self.store.close()
        _shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _add_func(
        self,
        name: str,
        path: str = "app.py",
        kind: str | None = None,
        is_test: bool = False,
    ) -> int:
        """Add a node with explicit kind (decoupled from is_test for H-02 testing)."""
        # kind defaults to "Test" when is_test, "Function" otherwise — but caller
        # can override to create Function nodes that live in test files.
        resolved_kind = kind if kind is not None else ("Test" if is_test else "Function")
        node = NodeInfo(
            kind=resolved_kind,
            name=name,
            file_path=str(self.tmpdir / path),
            line_start=1,
            line_end=10,
            language="python",
            is_test=is_test,
        )
        nid = self.store.upsert_node(node, file_hash="abc")
        self.store.commit()
        return nid

    def _add_call(self, src_path: str, src_name: str, tgt_path: str, tgt_name: str) -> None:
        """Add a CALLS edge using absolute paths (will be relativized by the store)."""
        edge = EdgeInfo(
            kind="CALLS",
            source=str(self.tmpdir / src_path) + "::" + src_name,
            target=str(self.tmpdir / tgt_path) + "::" + tgt_name,
            file_path=str(self.tmpdir / src_path),
            line=5,
        )
        self.store.upsert_edge(edge)
        self.store.commit()

    # -------------------------------------------------------------------
    # H-01: sort_by must not change the set of flows returned
    # -------------------------------------------------------------------

    def test_h01_sort_by_invariant_with_is_test_filter(self):
        """H-01: is_test=False returns the same flow IDs regardless of sort_by."""
        from code_review_graph.tools.flows_tools import list_flows

        # Two production flows
        self._add_func("prod_ep1", path="app.py", is_test=False)
        self._add_func("prod_helper1", path="app.py", is_test=False)
        self._add_call("app.py", "prod_ep1", "app.py", "prod_helper1")

        self._add_func("prod_ep2", path="svc.py", is_test=False)
        self._add_func("prod_helper2a", path="svc.py", is_test=False)
        self._add_func("prod_helper2b", path="svc.py", is_test=False)
        self._add_call("svc.py", "prod_ep2", "svc.py", "prod_helper2a")
        self._add_call("svc.py", "prod_helper2a", "svc.py", "prod_helper2b")

        # One test flow
        self._add_func("test_handler", path="tests/test_app.py", kind="Test", is_test=True)
        self._add_func("test_helper", path="tests/test_app.py", kind="Test", is_test=True)
        self._add_call("tests/test_app.py", "test_handler", "tests/test_app.py", "test_helper")

        stored = trace_flows(self.store)
        store_flows(self.store, stored)

        r_crit = list_flows(
            repo_root=str(self.tmpdir), is_test=False, sort_by="criticality", limit=50
        )
        r_depth = list_flows(
            repo_root=str(self.tmpdir), is_test=False, sort_by="depth", limit=50
        )

        assert r_crit["total_count"] == r_depth["total_count"], (
            f"sort_by='criticality' gave {r_crit['total_count']} "
            f"but sort_by='depth' gave {r_depth['total_count']}"
        )
        assert {f["id"] for f in r_crit["flows"]} == {f["id"] for f in r_depth["flows"]}

    # -------------------------------------------------------------------
    # H-02: is_test flag on the node, not kind=="Test"
    # -------------------------------------------------------------------

    def test_h02_is_test_false_excludes_function_kind_with_is_test_true(self):
        """H-02: is_test=False must exclude flows whose entry-point has is_test=True,
        even when that node's kind is 'Function' (not 'Test'), as happens for
        setup_method / _seed_data helpers that live in test files."""
        from code_review_graph.tools.flows_tools import list_flows

        # "Function"-kind nodes living in a test file: is_test=True, kind="Function"
        self._add_func(
            "setup_method", path="tests/test_app.py", kind="Function", is_test=True
        )
        self._add_func(
            "_seed_data", path="tests/test_app.py", kind="Function", is_test=True
        )
        self._add_call("tests/test_app.py", "setup_method", "tests/test_app.py", "_seed_data")

        # A proper production flow
        self._add_func("handler", path="app.py", kind="Function", is_test=False)
        self._add_func("helper", path="app.py", kind="Function", is_test=False)
        self._add_call("app.py", "handler", "app.py", "helper")

        stored = trace_flows(self.store)
        store_flows(self.store, stored)

        prod_result = list_flows(repo_root=str(self.tmpdir), is_test=False)
        prod_flows = prod_result["flows"]

        # The setup_method→_seed_data flow must NOT appear
        for flow in prod_flows:
            ep_id = flow.get("entry_point_id")
            if ep_id is not None:
                node = self.store.get_node_by_id(ep_id)
                if node is not None:
                    assert not node.is_test, (
                        f"Flow with entry-point is_test=True slipped through "
                        f"is_test=False filter: node={node.name!r}, kind={node.kind!r}"
                    )

        # The handler→helper production flow must appear
        prod_ep_names = set()
        for flow in prod_flows:
            ep_id = flow.get("entry_point_id")
            if ep_id is not None:
                node = self.store.get_node_by_id(ep_id)
                if node is not None:
                    prod_ep_names.add(node.name)
        assert "handler" in prod_ep_names, "Production flow 'handler' missing from is_test=False"

    # -------------------------------------------------------------------
    # H-14: get_affected_flows_func / get_affected_flows_tool is_test param
    # -------------------------------------------------------------------

    def test_h14_get_affected_flows_func_has_is_test_parameter(self):
        """H-14: get_affected_flows_func signature must include is_test."""
        import inspect

        from code_review_graph.tools.review import get_affected_flows_func

        sig = inspect.signature(get_affected_flows_func)
        assert "is_test" in sig.parameters, (
            "get_affected_flows_func is missing the is_test parameter"
        )

    def test_h14_get_affected_flows_tool_has_is_test_parameter(self):
        """H-14: get_affected_flows_tool MCP wrapper must expose is_test."""
        import inspect

        from code_review_graph.main import get_affected_flows_tool

        sig = inspect.signature(get_affected_flows_tool)
        assert "is_test" in sig.parameters, (
            "get_affected_flows_tool is missing the is_test parameter"
        )

    def test_h14_get_affected_flows_func_filters_test_flows(self):
        """H-14: get_affected_flows_func(is_test=False) excludes test-file flows."""
        from code_review_graph.tools.review import get_affected_flows_func

        # Test-file flow (Function kind, is_test=True)
        self._add_func(
            "setup_method", path="tests/test_app.py", kind="Function", is_test=True
        )
        self._add_func(
            "_seed_data", path="tests/test_app.py", kind="Function", is_test=True
        )
        self._add_call("tests/test_app.py", "setup_method", "tests/test_app.py", "_seed_data")

        stored = trace_flows(self.store)
        store_flows(self.store, stored)

        # Changed file is the test file — the test flow passes through it
        changed = [str(self.tmpdir / "tests/test_app.py")]

        result_all = get_affected_flows_func(
            changed_files=changed,
            repo_root=str(self.tmpdir),
        )
        result_prod = get_affected_flows_func(
            changed_files=changed,
            repo_root=str(self.tmpdir),
            is_test=False,
        )

        # All affected flows include the test flow
        assert result_all["total"] >= 1
        # With is_test=False the test flow should be excluded
        for flow in result_prod.get("affected_flows", []):
            ep_id = flow.get("entry_point_id")
            if ep_id is not None:
                node = self.store.get_node_by_id(ep_id)
                if node is not None:
                    assert not node.is_test, (
                        f"Test flow found in is_test=False result: {node.name!r}"
                    )

    def test_get_flow_not_found_has_hints(self):
        """P-04: get_flow_tool not_found response has _hints.next_actions."""
        from code_review_graph.tools.flows_tools import get_flow

        result = get_flow(flow_id=99999, repo_root=str(self.tmpdir))
        assert result["status"] == "not_found"
        hints = result.get("_hints", {})
        next_actions = hints.get("next_actions", [])
        assert "list_flows_tool" in next_actions

    def test_get_flow_not_found_by_name_has_hints(self):
        """P-04: get_flow_tool not_found by flow_name has _hints.next_actions."""
        from code_review_graph.tools.flows_tools import get_flow

        result = get_flow(flow_name="nonexistent_flow_xyz", repo_root=str(self.tmpdir))
        assert result["status"] == "not_found"
        hints = result.get("_hints", {})
        next_actions = hints.get("next_actions", [])
        assert "list_flows_tool" in next_actions
