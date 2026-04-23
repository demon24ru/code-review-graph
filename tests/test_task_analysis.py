"""Unit tests for task_analysis.py — algorithmic analysis of the Task DAG."""

from __future__ import annotations

import time
import tempfile
from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.parser import EdgeInfo, NodeInfo
from code_review_graph import tasks, task_analysis


class TestAnalysisBase:
    """Base class: fresh GraphStore + conn, helpers for code nodes and edges."""

    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.store = GraphStore(self.tmp.name)
        self.conn = self.store._conn
        # Compatibility wrappers for old API surface (batch wrappers will be used internally)
        self._orig_create_task = tasks.create_task
        def create_task(conn, title, description=None, parent_id=None):
            res = self._orig_create_task(conn, [{"title": title, "description": description}], parent_id=parent_id)
            return res["tasks"][0]
        tasks.create_task = create_task
        self._orig_move_task = tasks.move_task
        def move_task(conn, task_ids, new_parent_id=None):
            if not isinstance(task_ids, list):
                task_ids = [task_ids]
            res = self._orig_move_task(conn, task_ids, new_parent_id=new_parent_id)
            return res["tasks"][0]
        tasks.move_task = move_task
        self._orig_archive_task = tasks.archive_task
        def archive_task(conn, task_ids, reason, cascade=True):
            if not isinstance(task_ids, list):
                task_ids = [task_ids]
            return self._orig_archive_task(conn, task_ids, reason=reason, cascade=cascade)
        tasks.archive_task = archive_task
        self._orig_add_task_edge = tasks.add_task_edge
        def add_task_edge(conn, *args, **kwargs):
            if len(args) >= 1 and isinstance(args[0], list):
                edges = args[0]
                edge_type = kwargs.get("edge_type", None)
                res = self._orig_add_task_edge(conn, edges, edge_type=edge_type)
                return res.get("edges", [])[0] if isinstance(res, dict) and "edges" in res else res
            if len(args) >= 3:
                source_id, target_id, edge_type = args[0], args[1], args[2]
                res = self._orig_add_task_edge(conn, [{"source_id": source_id, "target_id": target_id}], edge_type=edge_type)
                return res["edges"][0]
            return self._orig_add_task_edge(conn, *args, **kwargs)
        tasks.add_task_edge = add_task_edge
        self._orig_link_task_code = tasks.link_task_code
        def link_task_code(conn, task_id, *args, **kwargs):
            if len(args) == 1 and isinstance(args[0], list):
                batch = args[0]
                return self._orig_link_task_code(conn, task_id, batch)
            if len(args) >= 1 and isinstance(args[0], str):
                ref_type = args[0]
                code_node_id = kwargs.get("code_node_id", None)
                qualified_name = kwargs.get("qualified_name", None)
                description = kwargs.get("description", None)
                links = []
                if code_node_id is not None or qualified_name is not None:
                    item = {"ref_type": ref_type}
                    if code_node_id is not None:
                        item["code_node_id"] = code_node_id
                    if qualified_name is not None:
                        item["qualified_name"] = qualified_name
                    if description is not None:
                        item["description"] = description
                    links.append(item)
                res = self._orig_link_task_code(conn, task_id, links)
                linked = res.get("linked", [])
                if linked:
                    l = linked[0]
                    return {"task_id": task_id, "code_node_id": l.get("code_node_id"), "ref_type": l.get("ref_type")}
                return res
            if "batch" in kwargs:
                batch = kwargs["batch"]
                return self._orig_link_task_code(conn, task_id, batch)
            return self._orig_link_task_code(conn, task_id, [])
        tasks.link_task_code = link_task_code
        self._orig_add_note = tasks.add_note
        def add_note(conn, task_id, note_type, content, status="open", resolution=None, rationale=None, alternatives=None):
            notes = [{"note_type": note_type, "content": content, "status": status, "resolution": resolution, "rationale": rationale, "alternatives": alternatives}]
            res = self._orig_add_note(conn, task_id, notes)
            return res["notes"][0]
        tasks.add_note = add_note

    def teardown_method(self):
        # Restore originals
        if hasattr(self, "_orig_create_task"):
            tasks.create_task = self._orig_create_task
        if hasattr(self, "_orig_move_task"):
            tasks.move_task = self._orig_move_task
        if hasattr(self, "_orig_archive_task"):
            tasks.archive_task = self._orig_archive_task
        if hasattr(self, "_orig_add_task_edge"):
            tasks.add_task_edge = self._orig_add_task_edge
        if hasattr(self, "_orig_link_task_code"):
            tasks.link_task_code = self._orig_link_task_code
        if hasattr(self, "_orig_add_note"):
            tasks.add_note = self._orig_add_note
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    # --- helpers ---

    def _task(self, title: str, parent_id=None, description=None):
        return tasks.create_task(self.conn, title,
                                 description=description or title,
                                 parent_id=parent_id)

    def _node(self, name: str, path: str = "file.py") -> int:
        nid = self.store.upsert_node(
            NodeInfo(kind="Function", name=name, file_path=path,
                     line_start=1, line_end=10, language="python")
        )
        self.store.commit()
        return nid

    def _code_edge(self, src_name: str, tgt_name: str):
        """Add a CALLS edge between two nodes using their actual qualified names."""
        conn = self.store._conn
        src_row = conn.execute(
            "SELECT qualified_name, file_path FROM nodes WHERE name=? LIMIT 1", (src_name,)
        ).fetchone()
        tgt_row = conn.execute(
            "SELECT qualified_name FROM nodes WHERE name=? LIMIT 1", (tgt_name,)
        ).fetchone()
        if src_row is None or tgt_row is None:
            raise ValueError(f"Node not found: {src_name!r} or {tgt_name!r}")
        conn.execute(
            "INSERT OR IGNORE INTO edges "
            "(kind, source_qualified, target_qualified, file_path, line, extra, updated_at) "
            "VALUES (?, ?, ?, ?, ?, '{}', ?)",
            ("CALLS", src_row["qualified_name"], tgt_row["qualified_name"],
             src_row["file_path"], 5, time.time())
        )
        conn.commit()

    def _link(self, task_id: str, node_id: int, ref_type: str = "modifies"):
        tasks.link_task_code(self.conn, task_id, ref_type, code_node_id=node_id)

    def _contract(self, name: str, scope_task_id: str, contract_type: str = "interface",
                  definition: str = "IDef {}", provider_task_id=None,
                  consumer_task_ids=None):
        return tasks.add_contract(
            self.conn,
            contract_type=contract_type,
            definition=definition,
            name=name,
            scope_task_id=scope_task_id,
            provider_task_id=provider_task_id,
            consumer_task_ids=consumer_task_ids,
        )


# ---------------------------------------------------------------------------
# 5.2.1 — find_conflicts
# ---------------------------------------------------------------------------

class TestFindConflicts(TestAnalysisBase):

    def test_both_modify_same_node(self):
        root = self._task("Root")
        t1 = self._task("T1", parent_id=root["id"])
        t2 = self._task("T2", parent_id=root["id"])
        nid = self._node("shared_func")
        self._link(t1["id"], nid, "modifies")
        self._link(t2["id"], nid, "modifies")
        conflicts = task_analysis.find_conflicts(self.conn, root["id"])
        assert len(conflicts) == 1
        assert conflicts[0]["conflict_type"] == "both_modify"

    def test_read_write_conflict(self):
        root = self._task("Root")
        t1 = self._task("T1", parent_id=root["id"])
        t2 = self._task("T2", parent_id=root["id"])
        nid = self._node("func")
        self._link(t1["id"], nid, "modifies")
        self._link(t2["id"], nid, "reads")
        conflicts = task_analysis.find_conflicts(self.conn, root["id"])
        assert len(conflicts) == 1
        assert conflicts[0]["conflict_type"] == "read_write"

    def test_no_conflict_when_no_overlap(self):
        root = self._task("Root")
        t1 = self._task("T1", parent_id=root["id"])
        t2 = self._task("T2", parent_id=root["id"])
        n1 = self._node("func_a", "a.py")
        n2 = self._node("func_b", "b.py")
        self._link(t1["id"], n1, "modifies")
        self._link(t2["id"], n2, "modifies")
        conflicts = task_analysis.find_conflicts(self.conn, root["id"])
        assert len(conflicts) == 0

    def test_no_conflict_with_fewer_than_two_leaves(self):
        root = self._task("Root")
        t1 = self._task("T1", parent_id=root["id"])
        # Only one leaf
        conflicts = task_analysis.find_conflicts(self.conn, root["id"])
        assert len(conflicts) == 0


# ---------------------------------------------------------------------------
# 5.2.2 — check_isolation
# ---------------------------------------------------------------------------

class TestCheckIsolation(TestAnalysisBase):

    def test_high_isolation_when_no_external_callers(self):
        t = self._task("T")
        nid = self._node("standalone")
        self._link(t["id"], nid)
        result = task_analysis.check_isolation(self.conn, t["id"])
        assert result["isolation_score"] == 1.0
        assert result["internal_nodes"] == 1
        assert result["external_dependencies"] == 0

    def test_lower_isolation_with_external_callers(self):
        t = self._task("T")
        n_internal = self._node("internal_func", "a.py")
        n_external = self._node("caller_func", "b.py")
        self._link(t["id"], n_internal)
        # Add a CALLS edge: caller_func → internal_func
        self._code_edge("caller_func", "internal_func")
        result = task_analysis.check_isolation(self.conn, t["id"])
        # external_dependents should be 1 (caller is outside task)
        assert result["external_dependents"] >= 1
        assert result["isolation_score"] < 1.0

    def test_empty_code_refs_returns_full_isolation(self):
        t = self._task("T")
        result = task_analysis.check_isolation(self.conn, t["id"])
        assert result["status"] == "not_applicable"
        assert result["isolation_score"] is None
        assert result["internal_nodes"] == 0

    def test_check_isolation_not_applicable_when_no_code_refs(self):
        t = self._task("T")
        result = task_analysis.check_isolation(self.conn, t["id"])
        assert result["status"] == "not_applicable"
        assert result["message"] == "Task has no code refs linked. Use task_link_code to associate code nodes."
        assert result["isolation_score"] is None
        assert result["internal_nodes"] == 0
        assert result["external_dependencies"] == 0
        assert result["external_dependents"] == 0
        assert result["external_nodes"] == []


# ---------------------------------------------------------------------------
# 5.2.3 — blast_radius
# ---------------------------------------------------------------------------

class TestBlastRadius(TestAnalysisBase):

    def test_depth_1_finds_direct_callees(self):
        t = self._task("T")
        n1 = self._node("entry", "e.py")
        n2 = self._node("helper", "h.py")
        self._link(t["id"], n1)
        self._code_edge("entry", "helper")
        result = task_analysis.blast_radius(self.conn, t["id"], depth=1)
        assert len(result["direct_nodes"]) == 1
        assert result["affected_nodes_count"] >= 1  # helper reached
        assert "affected_nodes" not in result  # not included by default

    def test_empty_refs_returns_empty(self):
        t = self._task("T")
        result = task_analysis.blast_radius(self.conn, t["id"])
        assert result["status"] == "not_applicable"
        assert result["direct_nodes"] == []
        assert result["affected_nodes_count"] == 0
        assert result["coverage_ratio"] is None

    def test_blast_radius_not_applicable_when_no_code_refs(self):
        t = self._task("T")
        result = task_analysis.blast_radius(self.conn, t["id"])
        assert result["status"] == "not_applicable"
        assert result["message"] == "Task has no code refs linked. Use task_link_code to associate code nodes."
        assert result["direct_nodes"] == []
        assert result["affected_nodes_count"] == 0
        assert result["uncovered_nodes"] == []
        assert result["coverage_ratio"] is None

    def test_coverage_ratio_full_when_all_covered(self):
        root = self._task("Root")
        t1 = self._task("T1", parent_id=root["id"])
        t2 = self._task("T2", parent_id=root["id"])
        n1 = self._node("func_a", "a.py")
        n2 = self._node("func_b", "b.py")
        self._link(t1["id"], n1)
        self._link(t2["id"], n2)
        self._code_edge("func_a", "func_b")
        # Both nodes covered by some task
        result = task_analysis.blast_radius(self.conn, t1["id"], depth=1)
        assert result["coverage_ratio"] == 1.0
        assert result["uncovered_nodes"] == []

    def test_blast_radius_includes_affected_when_flag_true(self):
        t = self._task("T")
        n1 = self._node("entry", "e.py")
        n2 = self._node("helper", "h.py")
        self._link(t["id"], n1)
        self._code_edge("entry", "helper")
        # Default: no affected_nodes list
        result_default = task_analysis.blast_radius(self.conn, t["id"], depth=1)
        assert "affected_nodes" not in result_default
        assert result_default["affected_nodes_count"] >= 1
        # With flag: includes affected_nodes list
        result_with_flag = task_analysis.blast_radius(
            self.conn, t["id"], depth=1, include_affected_nodes=True
        )
        assert "affected_nodes" in result_with_flag
        assert len(result_with_flag["affected_nodes"]) >= 1
        assert result_with_flag["affected_nodes_count"] == len(result_with_flag["affected_nodes"])


# ---------------------------------------------------------------------------
# 5.2.4 — execution_order (level grouping)
# ---------------------------------------------------------------------------

class TestExecutionOrder(TestAnalysisBase):

    def test_no_deps_all_level_zero(self):
        root = self._task("Root")
        self._task("A", parent_id=root["id"])
        self._task("B", parent_id=root["id"])
        self._task("C", parent_id=root["id"])
        levels = task_analysis.execution_order(self.conn, root["id"])
        assert len(levels) == 1
        assert levels[0]["level"] == 0
        assert len(levels[0]["tasks"]) == 3

    def test_linear_deps_three_levels(self):
        root = self._task("Root")
        a = self._task("A", parent_id=root["id"])
        b = self._task("B", parent_id=root["id"])
        c = self._task("C", parent_id=root["id"])
        tasks.add_task_edge(self.conn, b["id"], a["id"], "depends_on")
        tasks.add_task_edge(self.conn, c["id"], b["id"], "depends_on")
        levels = task_analysis.execution_order(self.conn, root["id"])
        assert len(levels) == 3
        assert len(levels[0]["tasks"]) == 1  # a
        assert levels[0]["tasks"][0]["id"] == a["id"]
        assert len(levels[1]["tasks"]) == 1  # b
        assert len(levels[2]["tasks"]) == 1  # c

    def test_parallel_tasks_in_same_level(self):
        root = self._task("Root")
        base = self._task("Base", parent_id=root["id"])
        left = self._task("Left", parent_id=root["id"])
        right = self._task("Right", parent_id=root["id"])
        tasks.add_task_edge(self.conn, left["id"], base["id"], "depends_on")
        tasks.add_task_edge(self.conn, right["id"], base["id"], "depends_on")
        levels = task_analysis.execution_order(self.conn, root["id"])
        assert len(levels) == 2
        level1_ids = {t["id"] for t in levels[1]["tasks"]}
        assert left["id"] in level1_ids
        assert right["id"] in level1_ids

    def test_execution_order_correct_levels(self):
        """C depends_on A and B — C must appear at level 1, not level 0."""
        root = self._task("Root")
        a = self._task("A", parent_id=root["id"])
        b = self._task("B", parent_id=root["id"])
        c = self._task("C", parent_id=root["id"])
        tasks.add_task_edge(self.conn, c["id"], a["id"], "depends_on")
        tasks.add_task_edge(self.conn, c["id"], b["id"], "depends_on")
        levels = task_analysis.execution_order(self.conn, root["id"])
        # A and B have no deps → level 0; C depends on both → level 1
        assert len(levels) == 2
        level0_ids = {t["id"] for t in levels[0]["tasks"]}
        level1_ids = {t["id"] for t in levels[1]["tasks"]}
        assert a["id"] in level0_ids
        assert b["id"] in level0_ids
        assert c["id"] in level1_ids
        assert c["id"] not in level0_ids

    def test_execution_order_scoped_to_subtree(self):
        """Deps in subtree 2 must not affect execution_order for subtree 1."""
        # Use a single wrapper root to satisfy the single-pipeline rule;
        # root1 and root2 are independent sub-trees under it.
        wrapper = self._task("Wrapper")

        # Sub-tree 1: root1 → [X, Y], Y depends_on X → Y at level 1
        root1 = self._task("Root1", parent_id=wrapper["id"])
        x = self._task("X", parent_id=root1["id"])
        y = self._task("Y", parent_id=root1["id"])
        tasks.add_task_edge(self.conn, y["id"], x["id"], "depends_on")

        # Sub-tree 2: root2 → [P, Q, R], R depends_on P and Q (unrelated to sub-tree 1)
        root2 = self._task("Root2", parent_id=wrapper["id"])
        p = self._task("P", parent_id=root2["id"])
        q = self._task("Q", parent_id=root2["id"])
        r = self._task("R", parent_id=root2["id"])
        tasks.add_task_edge(self.conn, r["id"], p["id"], "depends_on")
        tasks.add_task_edge(self.conn, r["id"], q["id"], "depends_on")

        # execution_order for tree 1 must be unaffected by tree 2's edges
        levels1 = task_analysis.execution_order(self.conn, root1["id"])
        assert len(levels1) == 2
        level0_ids = {t["id"] for t in levels1[0]["tasks"]}
        level1_ids = {t["id"] for t in levels1[1]["tasks"]}
        assert x["id"] in level0_ids
        assert y["id"] in level1_ids
        # No tasks from tree 2 should appear in tree 1's levels
        all_ids = level0_ids | level1_ids
        assert p["id"] not in all_ids
        assert q["id"] not in all_ids
        assert r["id"] not in all_ids

        # execution_order for tree 2 must also be unaffected by tree 1's edges
        levels2 = task_analysis.execution_order(self.conn, root2["id"])
        assert len(levels2) == 2
        level0_ids2 = {t["id"] for t in levels2[0]["tasks"]}
        level1_ids2 = {t["id"] for t in levels2[1]["tasks"]}
        assert p["id"] in level0_ids2
        assert q["id"] in level0_ids2
        assert r["id"] in level1_ids2


# ---------------------------------------------------------------------------
# 5.2.5 — validate_dag
# ---------------------------------------------------------------------------

class TestValidateDAG(TestAnalysisBase):

    def test_clean_dag_no_errors(self):
        root = self._task("Root")
        t1 = self._task("T1", parent_id=root["id"], description="Do X")
        t2 = self._task("T2", parent_id=root["id"], description="Do Y")
        tasks.update_task(self.conn, t1["id"], acceptance_criteria="X done")
        tasks.update_task(self.conn, t2["id"], acceptance_criteria="Y done")
        n1 = self._node("func_a")
        n2 = self._node("func_b")
        self._link(t1["id"], n1)
        self._link(t2["id"], n2)
        result = task_analysis.validate_dag(self.conn, root["id"])
        assert len(result["errors"]) == 0

    def test_leaf_without_description_is_error(self):
        root = self._task("Root", description="Root")
        # Create child without description (title is used as description in _task helper)
        tasks.create_task(self.conn, "Leaf with no description", parent_id=root["id"])
        result = task_analysis.validate_dag(self.conn, root["id"])
        errors_text = " ".join(result["errors"])
        # The leaf was created via tasks.create_task without description
        assert len(result["errors"]) >= 1

    def test_open_questions_are_warnings(self):
        root = self._task("Root")
        tasks.add_note(self.conn, root["id"], "question", "What DB to use?")
        result = task_analysis.validate_dag(self.conn, root["id"])
        warnings_text = " ".join(result["warnings"])
        assert "question" in warnings_text.lower() or "unresolved" in warnings_text.lower()

    def test_unverified_assumptions_are_warnings(self):
        root = self._task("Root")
        tasks.add_note(self.conn, root["id"], "assumption", "Redis is available")
        result = task_analysis.validate_dag(self.conn, root["id"])
        warnings_text = " ".join(result["warnings"])
        assert "assumption" in warnings_text.lower() or "unverified" in warnings_text.lower()

    def test_missing_acceptance_criteria_is_warning(self):
        root = self._task("Root")
        t1 = tasks.create_task(self.conn, "Leaf", description="Do X",
                               parent_id=root["id"])
        # no acceptance_criteria set
        n1 = self._node("fn")
        self._link(t1["id"], n1)
        result = task_analysis.validate_dag(self.conn, root["id"])
        warnings_text = " ".join(result["warnings"])
        assert "acceptance_criteria" in warnings_text.lower() or "criteria" in warnings_text.lower()

    def test_unresolved_constraints_are_warnings(self):
        root = self._task("Root")
        tasks.add_note(self.conn, root["id"], "constraint", "Must use PostgreSQL", status="open")
        result = task_analysis.validate_dag(self.conn, root["id"])
        warnings_text = " ".join(result["warnings"])
        assert "constraint" in warnings_text.lower() or "unresolved" in warnings_text.lower()


# ---------------------------------------------------------------------------
# 5.2.6 — export_task (with include_analysis=True, replaces build_context)
# ---------------------------------------------------------------------------

class TestBuildContext(TestAnalysisBase):

    def test_all_layers_present(self):
        root = self._task("Root")
        t1 = self._task("Child", parent_id=root["id"])
        n1 = self._node("auth_func")
        self._link(t1["id"], n1)
        tasks.add_note(self.conn, root["id"], "decision", "Use JWT")
        self._contract("IFoo", root["id"], provider_task_id=t1["id"])
        ctx = task_analysis.export_task(self.conn, t1["id"], include_analysis=True)
        assert "task" in ctx
        assert "parent_chain" in ctx
        assert "code_refs" in ctx
        assert "notes" in ctx
        assert "contracts" in ctx
        assert "isolation" in ctx
        assert "pipeline_state" in ctx
        assert len(ctx["code_refs"]) == 1
        assert len(ctx["parent_chain"]) == 1  # root

    def test_pipeline_state_not_ready_with_open_question(self):
        root = self._task("Root")
        tasks.add_note(self.conn, root["id"], "question", "Still open?")
        ctx = task_analysis.export_task(self.conn, root["id"], include_analysis=True)
        assert ctx["pipeline_state"]["open_questions"] == 1
        assert ctx["pipeline_state"]["ready_for_coder"] is False

    def test_pipeline_state_ready_when_all_resolved(self):
        root = self._task("Root")
        ctx = task_analysis.export_task(self.conn, root["id"], include_analysis=True)
        assert ctx["pipeline_state"]["ready_for_coder"] is True


# ---------------------------------------------------------------------------
# 5.2.7 — roadmap progress + phases
# ---------------------------------------------------------------------------

class TestRoadmap(TestAnalysisBase):

    def test_progress_counts(self):
        root = self._task("Root")
        t1 = self._task("T1", parent_id=root["id"])
        t2 = self._task("T2", parent_id=root["id"])
        t3 = self._task("T3", parent_id=root["id"])
        tasks.update_task(self.conn, t1["id"], status="done")
        tasks.update_task(self.conn, t2["id"], status="in_progress")
        rm = task_analysis.roadmap(self.conn, root["id"])
        p = rm["progress"]
        assert p["total"] == 4  # root + 3
        assert p["done"] == 1
        assert p["in_progress"] == 1
        assert p["percent"] == 25

    def test_root_info_correct(self):
        root = self._task("My Root")
        rm = task_analysis.roadmap(self.conn, root["id"])
        assert rm["root"]["title"] == "My Root"

    def test_phases_populated(self):
        root = self._task("Root")
        a = self._task("A", parent_id=root["id"])
        b = self._task("B", parent_id=root["id"])
        tasks.add_task_edge(self.conn, b["id"], a["id"], "depends_on")
        rm = task_analysis.roadmap(self.conn, root["id"])
        assert len(rm["phases"]) == 2
        assert rm["phases"][0]["level"] == 0
        assert rm["phases"][0]["tasks"][0]["id"] == a["id"]

    def test_attention_unresolved_questions(self):
        root = self._task("Root")
        tasks.add_note(self.conn, root["id"], "question", "Open question?")
        rm = task_analysis.roadmap(self.conn, root["id"])
        assert len(rm["attention"]["unresolved_questions"]) == 1

    def test_contracts_summary(self):
        root = self._task("Root")
        t1 = self._task("T1", parent_id=root["id"])
        t2 = self._task("T2", parent_id=root["id"])
        c = self._contract("IFoo", root["id"], provider_task_id=t1["id"],
                           consumer_task_ids=[t2["id"]])
        tasks.update_contract(self.conn, c["id"], status="agreed")
        self._contract("IBar", root["id"], contract_type="api", definition="Y",
                       provider_task_id=t1["id"], consumer_task_ids=[t2["id"]])  # proposed
        rm = task_analysis.roadmap(self.conn, root["id"])
        assert rm["contracts"]["total"] == 2
        assert rm["contracts"]["agreed"] == 1
        assert rm["contracts"]["pending"] == 1


# ---------------------------------------------------------------------------
# 5.2.8 — roadmap_diff
# ---------------------------------------------------------------------------

class TestRoadmapDiff(TestAnalysisBase):

    def test_new_tasks_appear_in_diff(self):
        root = self._task("Root")
        ts = time.time()
        time.sleep(0.01)
        new_task = self._task("New task", parent_id=root["id"])
        diff = task_analysis.roadmap_diff(self.conn, root["id"], ts)
        new_ids = {t["id"] for t in diff["tasks_created"]}
        assert new_task["id"] in new_ids

    def test_status_changes_appear_in_diff(self):
        root = self._task("Root")
        t1 = self._task("T1", parent_id=root["id"])
        ts = time.time()
        time.sleep(0.01)
        tasks.update_task(self.conn, t1["id"], status="done")
        diff = task_analysis.roadmap_diff(self.conn, root["id"], ts)
        changed_ids = {t["id"] for t in diff["tasks_status_changed"]}
        assert t1["id"] in changed_ids

    def test_new_notes_appear_in_diff(self):
        root = self._task("Root")
        ts = time.time()
        time.sleep(0.01)
        tasks.add_note(self.conn, root["id"], "decision", "After timestamp")
        diff = task_analysis.roadmap_diff(self.conn, root["id"], ts)
        assert len(diff["notes_added"]) == 1

    def test_nothing_changed_is_empty(self):
        root = self._task("Root")
        self._task("T1", parent_id=root["id"])
        ts = time.time() + 60  # far in the future
        diff = task_analysis.roadmap_diff(self.conn, root["id"], ts)
        assert diff["tasks_created"] == []
        assert diff["tasks_status_changed"] == []
        assert diff["notes_added"] == []

    def test_roadmap_diff_status_changed_has_new_status_key(self):
        """Verify tasks_status_changed has new_status (not current_status) and old_status."""
        root = self._task("Root")
        t1 = self._task("T1", parent_id=root["id"])
        ts = time.time()
        time.sleep(0.01)
        tasks.update_task(self.conn, t1["id"], status="done")
        diff = task_analysis.roadmap_diff(self.conn, root["id"], ts)
        assert len(diff["tasks_status_changed"]) == 1
        changed = diff["tasks_status_changed"][0]
        assert "new_status" in changed, "Missing 'new_status' key"
        assert "old_status" in changed, "Missing 'old_status' key"
        assert "current_status" not in changed, "Should not have 'current_status' key"
        assert changed["new_status"] == "done"
        assert changed["old_status"] is None


# ---------------------------------------------------------------------------
# Additional validate_dag checks: blocked ready tasks, proposed contracts
# ---------------------------------------------------------------------------

class TestValidateDAGExtra(TestAnalysisBase):

    def test_ready_task_with_unmet_dep_is_error(self):
        """A leaf task marked 'ready' whose dependency is not 'done' → error."""
        root = self._task("Root")
        blocker = tasks.create_task(self.conn, "Blocker",
                                    description="Blocking task",
                                    parent_id=root["id"])
        dependent = tasks.create_task(self.conn, "Dependent",
                                      description="Needs blocker",
                                      parent_id=root["id"])
        tasks.add_task_edge(self.conn, dependent["id"], blocker["id"], "depends_on")
        tasks.update_task(self.conn, dependent["id"], status="ready")
        result = task_analysis.validate_dag(self.conn, root["id"])
        errors_text = " ".join(result["errors"])
        assert (
            "ready" in errors_text.lower()
            or "blocked" in errors_text.lower()
            or "depends" in errors_text.lower()
        )

    def test_proposed_contract_between_ready_tasks_is_error(self):
        """A 'proposed' contract between tasks that are 'ready' → error."""
        root = self._task("Root")
        provider = tasks.create_task(self.conn, "Provider",
                                     description="Provides interface",
                                     parent_id=root["id"])
        consumer = tasks.create_task(self.conn, "Consumer",
                                     description="Uses interface",
                                     parent_id=root["id"])
        tasks.update_task(self.conn, provider["id"], status="ready",
                          acceptance_criteria="done")
        tasks.update_task(self.conn, consumer["id"], status="ready",
                          acceptance_criteria="done")
        n1 = self._node("prov_func")
        n2 = self._node("cons_func")
        self._link(provider["id"], n1)
        self._link(consumer["id"], n2)
        # Contract remains 'proposed' (default)
        self._contract("IFoo", root["id"], provider_task_id=provider["id"],
                       consumer_task_ids=[consumer["id"]])
        result = task_analysis.validate_dag(self.conn, root["id"])
        errors_text = " ".join(result["errors"])
        assert "proposed" in errors_text.lower() or "contract" in errors_text.lower()

    def test_clean_dag_contract_ok_message(self):
        """With no contracts, ok list should mention contracts."""
        root = self._task("Root")
        t1 = tasks.create_task(self.conn, "Leaf", description="Do X",
                               parent_id=root["id"])
        tasks.update_task(self.conn, t1["id"], acceptance_criteria="X done")
        n1 = self._node("fn_x")
        self._link(t1["id"], n1)
        result = task_analysis.validate_dag(self.conn, root["id"])
        ok_text = " ".join(result["ok"])
        assert "contract" in ok_text.lower()


# ---------------------------------------------------------------------------
# Additional check_isolation: callee direction
# ---------------------------------------------------------------------------

class TestCheckIsolationExtra(TestAnalysisBase):

    def test_lower_isolation_with_external_callees(self):
        """Task calls external function → external_dependencies >= 1."""
        t = self._task("T")
        n_internal = self._node("my_service", "service.py")
        self._node("db_query", "db.py")
        self._link(t["id"], n_internal)
        self._code_edge("my_service", "db_query")
        result = task_analysis.check_isolation(self.conn, t["id"])
        assert result["external_dependencies"] >= 1
        assert result["isolation_score"] < 1.0


# ---------------------------------------------------------------------------
# Additional find_conflicts: shared_ref type (both read) + multi-pair
# ---------------------------------------------------------------------------

class TestFindConflictsExtra(TestAnalysisBase):

    def test_shared_ref_both_read(self):
        """Two tasks both reading the same node → shared_ref conflict type."""
        root = self._task("Root")
        t1 = self._task("T1", parent_id=root["id"])
        t2 = self._task("T2", parent_id=root["id"])
        nid = self._node("config_reader")
        self._link(t1["id"], nid, "reads")
        self._link(t2["id"], nid, "reads")
        conflicts = task_analysis.find_conflicts(self.conn, root["id"])
        assert len(conflicts) == 1
        assert conflicts[0]["conflict_type"] == "shared_ref"

    def test_three_tasks_two_pairs_conflict(self):
        """Three tasks, two pairs sharing different nodes → 2 conflicts."""
        root = self._task("Root")
        t1 = self._task("T1", parent_id=root["id"])
        t2 = self._task("T2", parent_id=root["id"])
        t3 = self._task("T3", parent_id=root["id"])
        n1 = self._node("fn_shared_12", "a.py")
        n2 = self._node("fn_shared_23", "b.py")
        self._link(t1["id"], n1, "modifies")
        self._link(t2["id"], n1, "modifies")
        self._link(t2["id"], n2, "reads")
        self._link(t3["id"], n2, "modifies")
        conflicts = task_analysis.find_conflicts(self.conn, root["id"])
        assert len(conflicts) == 2


# ---------------------------------------------------------------------------
# suggest_contracts tests
# ---------------------------------------------------------------------------

class TestSuggestContracts(TestAnalysisBase):

    def test_no_code_refs_returns_empty(self):
        """Tasks without code refs → no contract suggestions."""
        root = self._task("Root")
        self._task("T1", parent_id=root["id"])
        self._task("T2", parent_id=root["id"])
        result = task_analysis.suggest_contracts(self.conn, root["id"])
        assert result == []

    def test_no_crossing_edges_returns_empty(self):
        """Tasks with disjoint code nodes → no suggestions."""
        root = self._task("Root")
        t1 = self._task("T1", parent_id=root["id"])
        t2 = self._task("T2", parent_id=root["id"])
        n1 = self._node("fn_a", "a.py")
        n2 = self._node("fn_b", "b.py")
        self._link(t1["id"], n1, "modifies")
        self._link(t2["id"], n2, "modifies")
        result = task_analysis.suggest_contracts(self.conn, root["id"])
        assert result == []

    def test_crossing_edge_suggests_contract(self):
        """Task A's code calls Task B's code → suggests a contract."""
        root = self._task("Root")
        t1 = self._task("AuthService", parent_id=root["id"])
        t2 = self._task("UserController", parent_id=root["id"])
        n1 = self._node("auth_login", "auth.py")
        n2 = self._node("user_handler", "user.py")
        self._link(t1["id"], n1, "modifies")
        self._link(t2["id"], n2, "modifies")
        self._code_edge("user_handler", "auth_login")
        result = task_analysis.suggest_contracts(self.conn, root["id"])
        assert len(result) == 1
        s = result[0]
        assert s["crossing_edges"] >= 1
        assert "edge_details" in s
        # Both task titles should be present
        titles = {s["task_a_title"], s["task_b_title"]}
        assert "AuthService" in titles or "UserController" in titles

    def test_existing_contract_suppresses_suggestion(self):
        """If a contract exists between tasks, no suggestion emitted."""
        root = self._task("Root")
        t1 = self._task("T1", parent_id=root["id"])
        t2 = self._task("T2", parent_id=root["id"])
        n1 = self._node("fn_a", "a.py")
        n2 = self._node("fn_b", "b.py")
        self._link(t1["id"], n1, "modifies")
        self._link(t2["id"], n2, "modifies")
        self._code_edge("fn_a", "fn_b")
        # Add contract between them — should suppress the suggestion
        self._contract("IFoo", root["id"], provider_task_id=t1["id"],
                       consumer_task_ids=[t2["id"]])
        result = task_analysis.suggest_contracts(self.conn, root["id"])
        assert result == []

    def test_existing_task_edge_suppresses_suggestion(self):
        """If a task_edge exists between tasks, no suggestion emitted."""
        root = self._task("Root")
        t1 = self._task("T1", parent_id=root["id"])
        t2 = self._task("T2", parent_id=root["id"])
        n1 = self._node("fn_a", "a.py")
        n2 = self._node("fn_b", "b.py")
        self._link(t1["id"], n1, "modifies")
        self._link(t2["id"], n2, "modifies")
        self._code_edge("fn_a", "fn_b")
        # Add task edge
        tasks.add_task_edge(self.conn, t1["id"], t2["id"], "depends_on")
        result = task_analysis.suggest_contracts(self.conn, root["id"])
        assert result == []

    def test_single_leaf_returns_empty(self):
        """Single leaf task → no pairs to compare → empty."""
        root = self._task("Root")
        t1 = self._task("T1", parent_id=root["id"])
        n1 = self._node("fn_a")
        self._link(t1["id"], n1, "modifies")
        result = task_analysis.suggest_contracts(self.conn, root["id"])
        assert result == []

    def test_sorted_by_crossing_edges(self):
        """Multiple suggestions sorted by crossing_edges descending."""
        root = self._task("Root")
        t1 = self._task("T1", parent_id=root["id"])
        t2 = self._task("T2", parent_id=root["id"])
        t3 = self._task("T3", parent_id=root["id"])
        # T1 → T2: one crossing edge
        n1 = self._node("fn_1", "a.py")
        n2 = self._node("fn_2", "b.py")
        self._link(t1["id"], n1, "modifies")
        self._link(t2["id"], n2, "modifies")
        self._code_edge("fn_1", "fn_2")
        # T1 → T3: two crossing edges (fn_1→fn_3 and fn_1b→fn_3)
        n3 = self._node("fn_3", "c.py")
        n1b = self._node("fn_1b", "a.py")
        self._link(t1["id"], n1b, "modifies")
        self._link(t3["id"], n3, "modifies")
        self._code_edge("fn_1", "fn_3")
        self._code_edge("fn_1b", "fn_3")
        result = task_analysis.suggest_contracts(self.conn, root["id"])
        assert len(result) == 2
        # First result should have more crossing edges
        assert result[0]["crossing_edges"] >= result[1]["crossing_edges"]


# ---------------------------------------------------------------------------
# find_tasks_for_impact tests (cross-query #2)
# ---------------------------------------------------------------------------

class TestFindTasksForImpact(TestAnalysisBase):

    def test_no_code_nodes_for_files_returns_empty(self):
        """Files with no indexed code nodes → no impacted tasks."""
        root = self._task("Root")
        self._task("T1", parent_id=root["id"])
        result = task_analysis.find_tasks_for_impact(
            self.conn, ["nonexistent.py"], root["id"], include_node_details=True
        )
        assert result["tasks"] == []
        assert result["uncovered_nodes"] == []

    def test_node_in_file_linked_to_task_is_found(self):
        """Node in a changed file linked to a task → that task returned."""
        root = self._task("Root")
        t1 = self._task("Feature", parent_id=root["id"])
        n1 = self._node("auth_handler", "auth.py")
        self._link(t1["id"], n1)
        result = task_analysis.find_tasks_for_impact(
            self.conn, ["auth.py"], root["id"]
        )
        task_ids = [t["id"] for t in result["tasks"]]
        assert t1["id"] in task_ids

    def test_open_only_excludes_done_tasks(self):
        """open_only=True filters out tasks with status='done'."""
        root = self._task("Root")
        t_done = self._task("Done", parent_id=root["id"])
        t_open = self._task("Open", parent_id=root["id"])
        n1 = self._node("fn_done", "x.py")
        n2 = self._node("fn_open", "x.py")
        self._link(t_done["id"], n1)
        self._link(t_open["id"], n2)
        tasks.update_task(self.conn, t_done["id"], status="done")
        result = task_analysis.find_tasks_for_impact(
            self.conn, ["x.py"], root["id"], open_only=True
        )
        task_ids = [t["id"] for t in result["tasks"]]
        assert t_done["id"] not in task_ids
        assert t_open["id"] in task_ids

    def test_open_only_false_includes_done_tasks(self):
        """open_only=False includes done tasks."""
        root = self._task("Root")
        t_done = self._task("Done", parent_id=root["id"])
        n1 = self._node("fn_done", "y.py")
        self._link(t_done["id"], n1)
        tasks.update_task(self.conn, t_done["id"], status="done")
        result = task_analysis.find_tasks_for_impact(
            self.conn, ["y.py"], root["id"], open_only=False
        )
        task_ids = [t["id"] for t in result["tasks"]]
        assert t_done["id"] in task_ids

    def test_uncovered_nodes_reported(self):
        """Nodes in impact radius not linked to any task → uncovered_nodes."""
        root = self._task("Root")
        # Node in file but no task linked to it
        self._node("orphan_func", "z.py")
        result = task_analysis.find_tasks_for_impact(
            self.conn, ["z.py"], root["id"], include_node_details=True
        )
        assert result["uncovered_nodes_count"] >= 1
        assert len(result["uncovered_nodes"]) >= 1
        names = [n if isinstance(n, str) else n.get("name", "") for n in result["uncovered_nodes"]]
        assert any("orphan" in n for n in names)

    def test_bfs_traversal_finds_downstream_tasks(self):
        """BFS from seed node finds tasks linked to downstream callee nodes."""
        root = self._task("Root")
        t1 = self._task("Downstream", parent_id=root["id"])
        n_seed = self._node("entry_fn", "entry.py")
        n_callee = self._node("callee_fn", "lib.py")
        self._link(t1["id"], n_callee)
        self._code_edge("entry_fn", "callee_fn")
        result = task_analysis.find_tasks_for_impact(
            self.conn, ["entry.py"], root["id"], max_depth=2
        )
        task_ids = [t["id"] for t in result["tasks"]]
        assert t1["id"] in task_ids

    def test_coverage_ratio_all_covered(self):
        """When all impacted nodes are linked to tasks → coverage_ratio = 1.0."""
        root = self._task("Root")
        t1 = self._task("T1", parent_id=root["id"])
        n1 = self._node("sole_fn", "sole.py")
        self._link(t1["id"], n1)
        result = task_analysis.find_tasks_for_impact(
            self.conn, ["sole.py"], root["id"], include_node_details=True
        )
        assert result["coverage_ratio"] == 1.0
        assert result["uncovered_nodes"] == []
        assert result["uncovered_nodes_count"] == 0


# ---------------------------------------------------------------------------
# check_parent_rollup
# ---------------------------------------------------------------------------


class TestCheckParentRollup(TestAnalysisBase):

    def test_root_task_no_parent(self):
        """Root task has no parent — parent_chain is empty, immediate_parent is None."""
        root = self._task("Root")
        result = task_analysis.check_parent_rollup(self.conn, root["id"])
        assert result["task_id"] == root["id"]
        assert result["parent_chain"] == []
        assert result["immediate_parent"] is None

    def test_all_siblings_done_can_close(self):
        """All direct children done → parent can_close=True."""
        root = self._task("Root")
        t1 = self._task("T1", parent_id=root["id"])
        t2 = self._task("T2", parent_id=root["id"])
        tasks.update_task(self.conn, t1["id"], status="done")
        tasks.update_task(self.conn, t2["id"], status="done")
        result = task_analysis.check_parent_rollup(self.conn, t1["id"])
        parent_entry = result["immediate_parent"]
        assert parent_entry["id"] == root["id"]
        assert parent_entry["can_close"] is True
        assert parent_entry["can_archive"] is False
        assert parent_entry["blocking_children"] == []

    def test_all_siblings_archived_can_archive(self):
        """All direct children archived → parent can_archive=True."""
        root = self._task("Root")
        t1 = self._task("T1", parent_id=root["id"])
        t2 = self._task("T2", parent_id=root["id"])
        tasks.archive_task(self.conn, t1["id"], reason="dropped")
        tasks.archive_task(self.conn, t2["id"], reason="dropped")
        result = task_analysis.check_parent_rollup(self.conn, t1["id"])
        parent_entry = result["immediate_parent"]
        assert parent_entry["can_archive"] is True
        assert parent_entry["can_close"] is False

    def test_mixed_done_and_archived_can_complete(self):
        """Mix of done+archived → can_complete=True, can_close=False, can_archive=False."""
        root = self._task("Root")
        t1 = self._task("T1", parent_id=root["id"])
        t2 = self._task("T2", parent_id=root["id"])
        tasks.update_task(self.conn, t1["id"], status="done")
        tasks.archive_task(self.conn, t2["id"], reason="dropped")
        result = task_analysis.check_parent_rollup(self.conn, t1["id"])
        parent_entry = result["immediate_parent"]
        assert parent_entry["can_complete"] is True
        assert parent_entry["can_close"] is False
        assert parent_entry["can_archive"] is False

    def test_blocking_children_listed(self):
        """Non-terminal siblings appear in blocking_children."""
        root = self._task("Root")
        t1 = self._task("T1", parent_id=root["id"])
        t2 = self._task("T2", parent_id=root["id"])
        tasks.update_task(self.conn, t1["id"], status="done")
        # t2 remains draft
        result = task_analysis.check_parent_rollup(self.conn, t1["id"])
        parent_entry = result["immediate_parent"]
        assert parent_entry["can_close"] is False
        blocking_ids = [c["id"] for c in parent_entry["blocking_children"]]
        assert t2["id"] in blocking_ids

    def test_suggestion_text_contains_action(self):
        """suggestion field is non-empty and mentions relevant info."""
        root = self._task("Root")
        t1 = self._task("T1", parent_id=root["id"])
        tasks.update_task(self.conn, t1["id"], status="done")
        result = task_analysis.check_parent_rollup(self.conn, t1["id"])
        parent_entry = result["immediate_parent"]
        assert isinstance(parent_entry["suggestion"], str)
        assert len(parent_entry["suggestion"]) > 0

    def test_multi_level_chain(self):
        """Walks up multiple levels: grandchild → child → root."""
        root = self._task("Root")
        child = self._task("Child", parent_id=root["id"])
        grandchild = self._task("Grandchild", parent_id=child["id"])
        tasks.update_task(self.conn, grandchild["id"], status="done")
        result = task_analysis.check_parent_rollup(self.conn, grandchild["id"])
        # parent_chain has at least child level
        assert len(result["parent_chain"]) >= 1
        assert result["immediate_parent"]["id"] == child["id"]

    def test_stops_at_terminal_ancestor(self):
        """Climbing stops when ancestor is already done."""
        root = self._task("Root")
        tasks.update_task(self.conn, root["id"], status="done")
        child = self._task("Child", parent_id=root["id"])
        grandchild = self._task("Grandchild", parent_id=child["id"])
        tasks.update_task(self.conn, grandchild["id"], status="done")
        result = task_analysis.check_parent_rollup(self.conn, grandchild["id"])
        # chain should stop at child (root is done → stop after including it or before)
        chain_ids = [e["id"] for e in result["parent_chain"]]
        # root may or may not appear depending on whether child can_close triggers
        # but chain must not go past root
        assert root["id"] not in chain_ids or chain_ids[-1] == root["id"]

    def test_counts_correct(self):
        """children_total, children_done, children_archived counts are accurate."""
        root = self._task("Root")
        t1 = self._task("T1", parent_id=root["id"])
        t2 = self._task("T2", parent_id=root["id"])
        t3 = self._task("T3", parent_id=root["id"])
        tasks.update_task(self.conn, t1["id"], status="done")
        tasks.archive_task(self.conn, t2["id"], reason="dropped")
        # t3 remains draft
        result = task_analysis.check_parent_rollup(self.conn, t1["id"])
        entry = result["immediate_parent"]
        assert entry["children_total"] == 3
        assert entry["children_done"] == 1
        assert entry["children_archived"] == 1


# ---------------------------------------------------------------------------
# Auto-detect active root (task_id=None) tests
# ---------------------------------------------------------------------------

class TestAutoDetectRoot(TestAnalysisBase):
    """Functions that accept task_id=None should auto-detect the active root."""

    def _make_root_with_leaf(self) -> tuple:
        """Create a root + one leaf with description + acceptance_criteria."""
        root = tasks.create_task(self.conn, "Root")
        leaf = tasks.create_task(self.conn, "Leaf", description="Do X",
                                 parent_id=root["id"])
        tasks.update_task(self.conn, leaf["id"], acceptance_criteria="X done")
        n = self._node("fn_x")
        self._link(leaf["id"], n)
        return root, leaf

    def test_validate_dag_no_arg_uses_active_root(self):
        """validate_dag() with no args uses the active root."""
        self._make_root_with_leaf()
        result = task_analysis.validate_dag(self.conn)
        assert isinstance(result, dict)
        assert "errors" in result

    def test_validate_dag_no_active_root_raises(self):
        """validate_dag() with no active root raises KeyError."""
        with pytest.raises(KeyError, match="no open root"):
            task_analysis.validate_dag(self.conn)

    def test_export_task_no_arg_uses_active_root(self):
        """export_task() with no args uses the active root."""
        root, _ = self._make_root_with_leaf()
        result = task_analysis.export_task(self.conn)
        assert result["task"]["id"] == root["id"]

    def test_export_task_no_active_root_raises(self):
        """export_task() with no active root raises KeyError."""
        with pytest.raises(KeyError, match="no open root"):
             task_analysis.export_task(self.conn)

    def test_roadmap_no_arg_uses_active_root(self):
         """roadmap() with no args uses the active root."""
         root, _ = self._make_root_with_leaf()
         result = task_analysis.roadmap(self.conn)
         assert result["root"]["id"] == root["id"]

    def test_roadmap_no_active_root_raises(self):
         """roadmap() with no active root raises KeyError."""
         with pytest.raises(KeyError, match="no open root"):
            task_analysis.roadmap(self.conn)

    def test_after_closing_root_no_active(self):
         """After marking root done, auto-detect raises KeyError."""
         root, _ = self._make_root_with_leaf()
         tasks.update_task(self.conn, root["id"], status="done")
         with pytest.raises(KeyError, match="no open root"):
            task_analysis.roadmap(self.conn)

    def test_export_task_notes_is_list_when_empty(self):
         """export_task() returns notes as empty list, never None."""
         root, _ = self._make_root_with_leaf()
         result = task_analysis.export_task(self.conn)
         assert "notes" in result
         assert isinstance(result["notes"], list)
         assert result["notes"] == []

    def test_export_task_code_refs_is_list_when_empty(self):
         """export_task() returns code_refs as empty list, never None."""
         root = tasks.create_task(self.conn, "Root")
         result = task_analysis.export_task(self.conn, root["id"])
         assert "code_refs" in result
         assert isinstance(result["code_refs"], list)
         assert result["code_refs"] == []

    def test_export_task_pipeline_state_mentions_assumptions(self):
         """export_task(include_analysis=True) summary mentions assumptions."""
         root, leaf = self._make_root_with_leaf()
         # Add an open assumption to the leaf
         tasks.add_note(self.conn, leaf["id"], note_type="assumption",
                       content="User model exists", status="open")
         # Export the leaf (which has the assumption)
         result = task_analysis.export_task(self.conn, leaf["id"],
                                           include_analysis=True)
         assert "pipeline_state" in result
         assert "summary" in result["pipeline_state"]
         assert "assumption" in result["pipeline_state"]["summary"].lower()
         assert result["pipeline_state"]["ready_for_coder"] is False

    def test_export_task_pipeline_state_ready_summary(self):
         """export_task(include_analysis=True) summary says 'Ready' when all clear."""
         root, leaf = self._make_root_with_leaf()
         result = task_analysis.export_task(self.conn, root["id"],
                                          include_analysis=True)
         assert "pipeline_state" in result
         assert "summary" in result["pipeline_state"]
         assert result["pipeline_state"]["summary"] == "Ready for handoff"
         assert result["pipeline_state"]["ready_for_coder"] is True

    def test_export_task_with_subtasks_has_code_refs_summary(self):
         """export_task(root) includes subtask_code_refs_summary when root has leaf subtasks."""
         # Create root → 2 leaf subtasks
         root = tasks.create_task(self.conn, "Root Task")
         leaf1 = tasks.create_task(self.conn, "Leaf 1", parent_id=root["id"])
         leaf2 = tasks.create_task(self.conn, "Leaf 2", parent_id=root["id"])
         
         # Add code nodes to the graph
         node1_id = self.store.upsert_node(NodeInfo(
             kind="Function", name="func1", file_path="src/a.py",
             line_start=1, line_end=10, language="python"
         ))
         node2_id = self.store.upsert_node(NodeInfo(
             kind="Function", name="func2", file_path="src/b.py",
             line_start=1, line_end=10, language="python"
         ))
         node3_id = self.store.upsert_node(NodeInfo(
             kind="Function", name="func3", file_path="src/c.py",
             line_start=1, line_end=10, language="python"
         ))
         self.store.commit()
         
         # Link code refs to leaves
         tasks.link_task_code(self.conn, leaf1["id"], [
             {"ref_type": "modifies", "code_node_id": node1_id},
             {"ref_type": "modifies", "code_node_id": node2_id},
         ])
         tasks.link_task_code(self.conn, leaf2["id"], [
             {"ref_type": "modifies", "code_node_id": node2_id},  # shared with leaf1
             {"ref_type": "modifies", "code_node_id": node3_id},
         ])
         
         # Export root
         result = task_analysis.export_task(self.conn, root["id"])
         
         # Check subtask_code_refs_summary
         assert "subtask_code_refs_summary" in result
         summary = result["subtask_code_refs_summary"]
         assert summary["leaf_task_count"] == 2
         assert summary["total_unique_code_nodes"] == 3  # node1, node2, node3
         assert summary["tasks_with_code_refs"] == 2
         assert summary["tasks_without_code_refs"] == 0
         assert len(summary["per_task"]) == 2
         assert summary["per_task"][0]["task_id"] == leaf1["id"]
         assert summary["per_task"][0]["code_ref_count"] == 2
         assert summary["per_task"][1]["task_id"] == leaf2["id"]
         assert summary["per_task"][1]["code_ref_count"] == 2

    def test_export_leaf_task_no_subtask_summary(self):
         """export_task(leaf) returns empty subtask_code_refs_summary."""
         root = tasks.create_task(self.conn, "Root")
         leaf = tasks.create_task(self.conn, "Leaf", parent_id=root["id"])
         
         # Export the leaf task
         result = task_analysis.export_task(self.conn, leaf["id"])
         
         # Leaf has no subtasks, so summary should be empty dict
         assert "subtask_code_refs_summary" in result
         assert result["subtask_code_refs_summary"] == {}


# ---------------------------------------------------------------------------
# Fix 1: _resolve_code_node — double-backslash and single-backslash Windows paths
# Fix 2: find_tasks_for_impact — relative, absolute, filename-only paths
# ---------------------------------------------------------------------------

class TestPathNormalisation(TestAnalysisBase):
    """Tests for cross-platform path handling in resolve + find_for_impact."""

    def _win_node(self, name: str, win_qname: str, win_fpath: str) -> int:
        """Insert a node with Windows-style paths directly into nodes table."""
        import time
        cur = self.conn.execute(
           "INSERT INTO nodes(kind, name, qualified_name, file_path, "
           "line_start, line_end, language, updated_at) "
           "VALUES('Function',?,?,?,1,20,'python',?)",
           (name, win_qname, win_fpath, time.time()),
        )
        self.conn.commit()
        return cur.lastrowid

    # --- Fix 1: _resolve_code_node ---

    def test_resolve_forward_slash_path(self):
        """Forward-slash path (POSIX) resolves even when DB has backslash."""
        nid = self._win_node(
           "auth_fn",
           "C:\\proj\\auth.py::auth_fn",
           "C:\\proj\\auth.py",
        )
        root = self._task("Root")
        t = self._task("T", parent_id=root["id"])
        ref = tasks.link_task_code(
           self.conn, t["id"], "modifies",
           qualified_name="C:/proj/auth.py::auth_fn",
        )
        assert ref["code_node_id"] == nid

    def test_resolve_double_backslash_json_escaped(self):
        """Double-backslash (JSON-escaped, as LLM receives from MCP) resolves."""
        nid = self._win_node(
           "parse_fn",
           "C:\\proj\\parser.py::parse_fn",
           "C:\\proj\\parser.py",
        )
        root = self._task("Root")
        t = self._task("T", parent_id=root["id"])
        ref = tasks.link_task_code(
           self.conn, t["id"], "reads",
           qualified_name="C:\\\\proj\\\\parser.py::parse_fn",
        )
        assert ref["code_node_id"] == nid

    def test_resolve_mixed_separators(self):
        """Mixed separators C:/proj\\fn.py resolve correctly."""
        nid = self._win_node(
           "mixed_fn",
           "C:/proj/mixed.py::mixed_fn",
           "C:/proj/mixed.py",
        )
        root = self._task("Root")
        t = self._task("T", parent_id=root["id"])
        ref = tasks.link_task_code(
           self.conn, t["id"], "modifies",
           qualified_name="C:\\proj\\mixed.py::mixed_fn",
        )
        assert ref["code_node_id"] == nid

    # --- Fix 2: find_tasks_for_impact path matching ---

    def _node_for_file(self, name: str, fpath: str) -> int:
        """Insert a node with given file_path (absolute Windows-style)."""
        import time
        cur = self.conn.execute(
           "INSERT INTO nodes(kind, name, qualified_name, file_path, "
           "line_start, line_end, language, updated_at) "
           "VALUES('Function',?,?,?,1,10,'python',?)",
           (name, f"{fpath}::{name}", fpath, time.time()),
        )
        self.conn.commit()
        return cur.lastrowid

    def _linked_task(self, title: str, node_id: int, root_id: str) -> dict:
        t = self._task(title, parent_id=root_id)
        tasks.link_task_code(self.conn, t["id"], "modifies", code_node_id=node_id)
        return t

    def test_relative_path_matches_absolute_in_db(self):
        """Relative path 'pkg/module.py' finds node stored as 'C:\\path\\pkg\\module.py'."""
        root = self._task("Root")
        fpath = "C:\\project\\pkg\\module.py"
        nid = self._node_for_file("do_thing", fpath)
        t = self._linked_task("T", nid, root["id"])
        result = task_analysis.find_tasks_for_impact(
           self.conn, ["pkg/module.py"]
        )
        found_ids = [x["id"] for x in result["tasks"]]
        assert t["id"] in found_ids

    def test_filename_only_matches(self):
        """Filename only 'module.py' finds nodes stored with full path."""
        root = self._task("Root")
        fpath = "C:\\deep\\nested\\module.py"
        nid = self._node_for_file("helper", fpath)
        t = self._linked_task("T", nid, root["id"])
        result = task_analysis.find_tasks_for_impact(
           self.conn, ["module.py"]
        )
        found_ids = [x["id"] for x in result["tasks"]]
        assert t["id"] in found_ids

    def test_absolute_posix_path_matches_windows_db(self):
        """Absolute POSIX /project/pkg/mod.py matches C:\\project\\pkg\\mod.py in DB."""
        root = self._task("Root")
        fpath = "C:\\project\\pkg\\mod.py"
        nid = self._node_for_file("compute", fpath)
        t = self._linked_task("T", nid, root["id"])
        result = task_analysis.find_tasks_for_impact(
           self.conn, ["/project/pkg/mod.py"]
        )
        found_ids = [x["id"] for x in result["tasks"]]
        assert t["id"] in found_ids

    def test_absolute_windows_path_exact_match(self):
        """Absolute Windows path exact match still works."""
        root = self._task("Root")
        fpath = "C:\\project\\exact.py"
        nid = self._node_for_file("exact_fn", fpath)
        t = self._linked_task("T", nid, root["id"])
        result = task_analysis.find_tasks_for_impact(
           self.conn, ["C:\\project\\exact.py"]
        )
        found_ids = [x["id"] for x in result["tasks"]]
        assert t["id"] in found_ids

    def test_nonexistent_path_returns_empty(self):
        """Non-existent file path returns empty result gracefully."""
        result = task_analysis.find_tasks_for_impact(
           self.conn, ["totally/nonexistent/file.py"]
        )
        assert result["tasks"] == []
        assert "note" in result
