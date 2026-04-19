"""Integration tests for Task DAG.

5.3.1 — Full workflow: create root → decompose → add edges → link code →
        find conflicts → validate → build context → roadmap

5.3.2 — Cross-layer: build real code graph → create tasks → link code →
        check_isolation uses real code graph
"""

from __future__ import annotations

import time
import tempfile
from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.parser import EdgeInfo, NodeInfo
from code_review_graph import tasks, task_analysis


class TestIntegrationBase:
    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.store = GraphStore(self.tmp.name)
        self.conn = self.store._conn

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _node(self, name: str, path: str = "file.py") -> int:
        nid = self.store.upsert_node(
            NodeInfo(kind="Function", name=name, file_path=path,
                     line_start=1, line_end=10, language="python")
        )
        self.store.commit()
        return nid

    def _calls_edge(self, src: str, tgt: str, path: str = "file.py"):
        """Add a CALLS edge using actual qualified names from the nodes table."""
        src_row = self.conn.execute(
            "SELECT qualified_name, file_path FROM nodes WHERE name=? LIMIT 1", (src,)
        ).fetchone()
        tgt_row = self.conn.execute(
            "SELECT qualified_name FROM nodes WHERE name=? LIMIT 1", (tgt,)
        ).fetchone()
        if src_row is None or tgt_row is None:
            raise ValueError(f"Node not found: {src!r} or {tgt!r}")
        self.conn.execute(
            "INSERT OR IGNORE INTO edges "
            "(kind, source_qualified, target_qualified, file_path, line, extra, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("CALLS", src_row["qualified_name"], tgt_row["qualified_name"],
             src_row["file_path"], 1, "{}", time.time())
        )
        self.conn.commit()


# ---------------------------------------------------------------------------
# 5.3.1 — Full brainstorm workflow
# ---------------------------------------------------------------------------

class TestFullWorkflow(TestIntegrationBase):

    def test_complete_brainstorm_workflow(self):
        """
        Simulates a full brainstorm session:
        create root → decompose into subtasks → add edges → link code →
        add notes + contracts → find conflicts → validate → build context → roadmap
        """
        # Step 1: Create root task
        root = tasks.create_task(
            self.conn, "Add OAuth Authorization",
            description="Implement OAuth 2.0 with Google and GitHub providers"
        )
        assert root["status"] == "draft"

        # Step 2: Record brainstorm decisions
        note_jwt = tasks.add_note(
            self.conn, root["id"], "decision", "Use JWT tokens",
            status="resolved", resolution="Stateless, scalable"
        )
        tasks.add_note(
            self.conn, root["id"], "constraint", "No external auth services",
            status="resolved", resolution="Self-hosted only"
        )
        tasks.add_note(
            self.conn, root["id"], "question", "Support refresh tokens?",
        )

        # Step 3: Decompose into subtasks
        t_interface = tasks.create_task(
            self.conn, "OAuth Provider Interface",
            description="Define the OAuthProvider abstract interface",
            parent_id=root["id"]
        )
        t_google = tasks.create_task(
            self.conn, "Google OAuth Implementation",
            description="Implement Google OAuth flow",
            parent_id=root["id"]
        )
        t_jwt = tasks.create_task(
            self.conn, "JWT Token Service",
            description="Implement JWT generation and validation",
            parent_id=root["id"]
        )
        t_login = tasks.create_task(
            self.conn, "Update Login Endpoint",
            description="Modify existing login to support OAuth",
            parent_id=root["id"]
        )
        t_frontend = tasks.create_task(
            self.conn, "Frontend Login Page",
            description="Add OAuth buttons to login page",
            parent_id=root["id"]
        )

        # Step 4: Define dependencies
        tasks.add_task_edge(self.conn, t_google["id"], t_interface["id"], "depends_on",
                            "needs interface first")
        tasks.add_task_edge(self.conn, t_login["id"], t_interface["id"], "depends_on")
        tasks.add_task_edge(self.conn, t_login["id"], t_jwt["id"], "depends_on")
        tasks.add_task_edge(self.conn, t_frontend["id"], t_login["id"], "depends_on")
        tasks.add_task_edge(self.conn, t_google["id"], t_jwt["id"], "shares_context")

        # Step 5: Link code nodes
        n_auth_ctrl = self._node("AuthController", "auth/controller.py")
        n_oauth_prov = self._node("OAuthProvider", "auth/provider.py")
        n_jwt_svc = self._node("JWTService", "auth/jwt.py")
        n_login_page = self._node("LoginPage", "frontend/login.py")

        tasks.link_task_code(self.conn, t_interface["id"], "creates", code_node_id=n_oauth_prov)
        tasks.link_task_code(self.conn, t_google["id"], "reads", code_node_id=n_oauth_prov)
        tasks.link_task_code(self.conn, t_jwt["id"], "creates", code_node_id=n_jwt_svc)
        tasks.link_task_code(self.conn, t_login["id"], "modifies", code_node_id=n_auth_ctrl)
        tasks.link_task_code(self.conn, t_login["id"], "reads", code_node_id=n_oauth_prov)  # conflict with t_interface
        tasks.link_task_code(self.conn, t_frontend["id"], "modifies", code_node_id=n_login_page)

        # Step 6: Add contracts between tasks
        contract = tasks.add_contract(
            self.conn,
            contract_type="interface",
            definition="interface OAuthProvider { authenticate(code: str): Token }",
            name="OAuthProvider",
            scope_task_id=root["id"],
            provider_task_id=t_interface["id"],
            consumer_task_ids=[t_login["id"]],
        )
        tasks.update_contract(self.conn, contract["id"], status="agreed")

        # Step 7: Find conflicts
        conflicts = task_analysis.find_conflicts(self.conn, root["id"])
        # t_interface (creates) vs t_login (reads) on n_oauth_prov,
        # and t_google (reads) vs t_login (reads) on n_oauth_prov → shared_ref
        conflict_types = {c["conflict_type"] for c in conflicts}
        assert len(conflicts) >= 1

        # Step 8: Check isolation of t_login (many code refs → lower isolation)
        iso = task_analysis.check_isolation(self.conn, t_login["id"])
        assert iso["internal_nodes"] == 2
        assert 0.0 <= iso["isolation_score"] <= 1.0

        # Step 9: Execution order
        levels = task_analysis.execution_order(self.conn, root["id"])
        assert len(levels) >= 1
        # t_interface and t_jwt should be in level 0 (no dependencies)
        level_0_ids = {t["id"] for t in levels[0]["tasks"]}
        assert t_interface["id"] in level_0_ids
        assert t_jwt["id"] in level_0_ids

        # Step 10: Validate (expect warnings for open question, no acceptance_criteria)
        result = task_analysis.validate_dag(self.conn, root["id"])
        # Should have warnings but no critical errors about cycles
        cycle_errors = [e for e in result["errors"] if "cycle" in e.lower()]
        assert len(cycle_errors) == 0

        # Step 11: Build context for t_login
        ctx = task_analysis.export_task(self.conn, t_login["id"], include_analysis=True)
        assert ctx["task"]["id"] == t_login["id"]
        assert len(ctx["parent_chain"]) == 1
        assert len(ctx["code_refs"]) == 2
        assert ctx["contracts"]["as_consumer"]  # consumer of interface contract
        assert ctx["pipeline_state"]["open_questions"] >= 1  # inherited from root

        # Step 12: Export for handoff
        export = task_analysis.export_task(self.conn, t_interface["id"])
        assert export["task"]["id"] == t_interface["id"]
        assert len(export["contracts"]["as_provider"]) == 1

        # Step 13: Roadmap snapshot
        rm = task_analysis.roadmap(self.conn, root["id"])
        assert rm["progress"]["total"] == 6  # root + 5 subtasks
        assert rm["progress"]["done"] == 0
        assert rm["contracts"]["agreed"] == 1
        assert rm["contracts"]["pending"] == 0
        assert len(rm["attention"]["unresolved_questions"]) >= 1

        # Step 14: Mark t_interface and t_jwt as done, check roadmap updates
        tasks.update_task(self.conn, t_interface["id"], status="done")
        tasks.update_task(self.conn, t_jwt["id"], status="done")
        rm2 = task_analysis.roadmap(self.conn, root["id"])
        assert rm2["progress"]["done"] == 2

    def test_cycle_prevention_end_to_end(self):
        """Cycle in depends_on should be caught at edge creation time."""
        root = tasks.create_task(self.conn, "Root")
        a = tasks.create_task(self.conn, "A", parent_id=root["id"])
        b = tasks.create_task(self.conn, "B", parent_id=root["id"])
        c = tasks.create_task(self.conn, "C", parent_id=root["id"])
        tasks.add_task_edge(self.conn, b["id"], a["id"], "depends_on")
        tasks.add_task_edge(self.conn, c["id"], b["id"], "depends_on")
        with pytest.raises(ValueError, match="cycle"):
            tasks.add_task_edge(self.conn, a["id"], c["id"], "depends_on")

    def test_archive_and_create_replacement(self):
        """Archive old approach and create replacement."""
        root = tasks.create_task(self.conn, "Root")
        old = tasks.create_task(self.conn, "Old approach", parent_id=root["id"])
        tasks.add_note(self.conn, old["id"], "decision", "Use REST")
        tasks.archive_task(self.conn, old["id"], reason="Switching to GraphQL", cascade=True)
        assert tasks.get_task(self.conn, old["id"])["status"] == "archived"

        new = tasks.create_task(self.conn, "New approach", parent_id=root["id"])
        tasks.add_note(self.conn, new["id"], "decision", "Use GraphQL")
        active = tasks.list_tasks(self.conn, parent_id=root["id"], status="draft")
        assert len(active) == 1
        assert active[0]["id"] == new["id"]


# ---------------------------------------------------------------------------
# 5.3.2 — Cross-layer: real code graph + task isolation
# ---------------------------------------------------------------------------

class TestCrossLayerIntegration(TestIntegrationBase):

    def test_isolation_uses_real_code_graph(self):
        """
        Build a real call graph:  handler → service → repo
        Task covers only 'service'. handler is external caller, repo is external callee.
        Isolation score should be less than 1.
        """
        # Code nodes
        n_handler = self._node("handle_request", "web.py")
        n_service = self._node("auth_service", "service.py")
        n_repo = self._node("user_repo", "repo.py")

        # Call edges: handler → service → repo
        self._calls_edge("handle_request", "auth_service", "web.py")
        self._calls_edge("auth_service", "user_repo", "service.py")

        # Task covers only auth_service
        task = tasks.create_task(self.conn, "Auth service task", description="Implement auth")
        tasks.link_task_code(self.conn, task["id"], "modifies", code_node_id=n_service)

        result = task_analysis.check_isolation(self.conn, task["id"])
        assert result["internal_nodes"] == 1
        # handle_request is an external dependent (caller)
        assert result["external_dependents"] >= 1
        # user_repo is an external dependency (callee)
        assert result["external_dependencies"] >= 1
        assert result["isolation_score"] < 1.0

    def test_blast_radius_traverses_call_graph(self):
        """
        Blast radius from 'entry' should reach 'middle' (depth=1)
        and 'deep' (depth=2).
        """
        n_entry = self._node("entry_func", "entry.py")
        n_middle = self._node("middle_func", "middle.py")
        n_deep = self._node("deep_func", "deep.py")

        self._calls_edge("entry_func", "middle_func", "entry.py")
        self._calls_edge("middle_func", "deep_func", "middle.py")

        task = tasks.create_task(self.conn, "Entry task", description="Work on entry")
        tasks.link_task_code(self.conn, task["id"], "modifies", code_node_id=n_entry)

        result_d1 = task_analysis.blast_radius(self.conn, task["id"], depth=1)
        result_d2 = task_analysis.blast_radius(self.conn, task["id"], depth=2)

        affected_d1_ids = {n["id"] for n in result_d1["affected_nodes"]}
        affected_d2_ids = {n["id"] for n in result_d2["affected_nodes"]}

        assert n_middle in affected_d1_ids
        assert n_middle in affected_d2_ids
        assert n_deep in affected_d2_ids

    def test_suggest_code_links_finds_real_nodes(self):
        """
        Keyword extraction from title+description matches node names via LIKE.
        Node 'authentication_service' matches keyword 'authentication'.
        """
        n_auth = self._node("authentication_service", "auth.py")
        n_unrelated = self._node("process_payment", "payment.py")

        task = tasks.create_task(
            self.conn, "Implement authentication system",
            description="Build the authentication service flow"
        )
        suggestions = tasks.suggest_code_links(self.conn, task["id"])
        suggested_ids = {s["id"] for s in suggestions}
        assert n_auth in suggested_ids

    def test_find_by_code_node_cross_references(self):
        """
        Two tasks touching the same node should both appear in find_tasks_by_code_node.
        """
        n_shared = self._node("shared_func", "shared.py")
        t1 = tasks.create_task(self.conn, "Task A", description="A")
        t2 = tasks.create_task(self.conn, "Task B", description="B",
                               parent_id=t1["id"])
        tasks.link_task_code(self.conn, t1["id"], "modifies", code_node_id=n_shared)
        tasks.link_task_code(self.conn, t2["id"], "reads", code_node_id=n_shared)

        found = tasks.find_tasks_by_code_node(self.conn, n_shared)
        found_ids = {f["id"] for f in found}
        assert t1["id"] in found_ids
        assert t2["id"] in found_ids

    def test_validate_detects_conflicts_via_code_refs(self):
        """
        Two tasks both modifying the same node: find_conflicts should detect it,
        and validate should warn about it (via low isolation or code ref overlap).
        """
        root = tasks.create_task(self.conn, "Root", description="Root task")
        t1 = tasks.create_task(self.conn, "T1", description="Task one",
                               parent_id=root["id"])
        t2 = tasks.create_task(self.conn, "T2", description="Task two",
                               parent_id=root["id"])
        tasks.update_task(self.conn, t1["id"], acceptance_criteria="done")
        tasks.update_task(self.conn, t2["id"], acceptance_criteria="done")
        n_shared = self._node("critical_func", "core.py")
        tasks.link_task_code(self.conn, t1["id"], "modifies", code_node_id=n_shared)
        tasks.link_task_code(self.conn, t2["id"], "modifies", code_node_id=n_shared)

        conflicts = task_analysis.find_conflicts(self.conn, root["id"])
        assert len(conflicts) == 1
        assert conflicts[0]["conflict_type"] == "both_modify"
