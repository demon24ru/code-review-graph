"""Unit tests for tasks.py — Task DAG CRUD, edges, code refs, notes, contracts."""

from __future__ import annotations

import pytest
import tempfile
from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.parser import NodeInfo
from code_review_graph import tasks


class TestTaskBase:
    """Base class providing a fresh GraphStore + conn per test."""

    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.store = GraphStore(self.tmp.name)
        self.conn = self.store._conn

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _node(self, name: str = "func", path: str = "file.py") -> int:
        """Insert a code node and return its integer ID."""
        nid = self.store.upsert_node(
            NodeInfo(kind="Function", name=name, file_path=path,
                     line_start=1, line_end=10, language="python")
        )
        self.store.commit()
        return nid


# ---------------------------------------------------------------------------
# 5.1.1 — CRUD: create → get → update → list → delete
# ---------------------------------------------------------------------------

class TestTaskCRUD(TestTaskBase):

    def test_create_root_task(self):
        task = tasks.create_task(self.conn, [{"title": "My task", "description": "desc"}])["tasks"][0]
        assert task["id"]
        assert task["title"] == "My task"
        assert task["description"] == "desc"
        assert task["status"] == "draft"
        assert task["parent_id"] is None

    def test_create_subtask(self):
        root = tasks.create_task(self.conn, [{"title": "Root"}])["tasks"][0]
        child = tasks.create_task(self.conn, [{"title": "Child"}], parent_id=root["id"])["tasks"][0]
        assert child["parent_id"] == root["id"]

    def test_create_with_invalid_parent_raises(self):
        with pytest.raises(ValueError, match="does not exist"):
            tasks.create_task(self.conn, [{"title": "Bad"}], parent_id="nonexistent")

    def test_get_task(self):
        t = tasks.create_task(self.conn, [{"title": "X"}])["tasks"][0]
        fetched = tasks.get_task(self.conn, t["id"])
        assert fetched["id"] == t["id"]

    def test_get_nonexistent_raises(self):
        with pytest.raises(KeyError):
            tasks.get_task(self.conn, "no-such-id")

    def test_update_task_title(self):
        t = tasks.create_task(self.conn, [{"title": "Old"}])["tasks"][0]
        updated = tasks.update_task(self.conn, t["id"], title="New")
        assert updated["title"] == "New"

    def test_update_task_status(self):
        t = tasks.create_task(self.conn, [{"title": "T"}])["tasks"][0]
        updated = tasks.update_task(self.conn, t["id"], status="ready")
        assert updated["status"] == "ready"

    def test_update_invalid_status_raises(self):
        t = tasks.create_task(self.conn, [{"title": "T"}])["tasks"][0]
        with pytest.raises(ValueError, match="Invalid status"):
            tasks.update_task(self.conn, t["id"], status="flying")

    def test_update_no_fields_is_noop(self):
        t = tasks.create_task(self.conn, [{"title": "T"}])["tasks"][0]
        updated = tasks.update_task(self.conn, t["id"])
        assert updated["title"] == "T"

    def test_list_tasks_all(self):
        root = tasks.create_task(self.conn, [{"title": "A"}])["tasks"][0]
        tasks.create_task(self.conn, [{"title": "B"}], parent_id=root["id"])
        result = tasks.list_tasks(self.conn)
        assert len(result) == 2

    def test_list_tasks_root_only(self):
        root = tasks.create_task(self.conn, [{"title": "Root"}])["tasks"][0]
        tasks.create_task(self.conn, [{"title": "Child"}], parent_id=root["id"])
        result = tasks.list_tasks(self.conn, root_only=True)
        assert len(result) == 1
        assert result[0]["id"] == root["id"]

    def test_list_tasks_by_parent(self):
        root = tasks.create_task(self.conn, [{"title": "Root"}])["tasks"][0]
        tasks.create_task(self.conn, [{"title": "C1"}], parent_id=root["id"])
        tasks.create_task(self.conn, [{"title": "C2"}], parent_id=root["id"])
        result = tasks.list_tasks(self.conn, parent_id=root["id"])
        assert len(result) == 2

    def test_list_tasks_by_status(self):
        t = tasks.create_task(self.conn, [{"title": "T"}])["tasks"][0]
        tasks.update_task(self.conn, t["id"], status="ready")
        result = tasks.list_tasks(self.conn, status="ready")
        assert len(result) == 1

    def test_delete_task(self):
        t = tasks.create_task(self.conn, [{"title": "Del"}])["tasks"][0]
        result = tasks.delete_task(self.conn, t["id"])
        assert t["id"] in result["deleted_ids"]
        with pytest.raises(KeyError):
            tasks.get_task(self.conn, t["id"])

    def test_delete_nonexistent_raises(self):
        with pytest.raises(KeyError):
            tasks.delete_task(self.conn, "no-such-id")


# ---------------------------------------------------------------------------
# 5.1.2 + 5.1.3 — edit_task_field
# ---------------------------------------------------------------------------

class TestEditTaskField(TestTaskBase):

    def setup_method(self):
        super().setup_method()
        self.task = tasks.create_task(
            self.conn, "T",
            description="Hello World\nLine two\nLine three"
        )

    def test_search_replace_happy_path(self):
        result = tasks.edit_task_field(
            self.conn, self.task["id"], "description",
            search="World", replace="Python"
        )
        assert result["old_fragment"] == "World"
        assert result["new_fragment"] == "Python"
        t = tasks.get_task(self.conn, self.task["id"])
        assert "Python" in t["description"]
        assert "World" not in t["description"]

    def test_search_not_found_raises(self):
        with pytest.raises(ValueError, match="not found"):
            tasks.edit_task_field(
                self.conn, self.task["id"], "description",
                search="MISSING", replace="X"
            )

    def test_search_ambiguous_raises(self):
        tasks.update_task(self.conn, self.task["id"], description="ab ab ab")
        with pytest.raises(ValueError, match="appears 3 times"):
            tasks.edit_task_field(
                self.conn, self.task["id"], "description",
                search="ab", replace="cd"
            )

    def test_line_range_happy_path(self):
        result = tasks.edit_task_field(
            self.conn, self.task["id"], "description",
            line_start=2, line_end=2, content="Replaced line"
        )
        assert result["old_fragment"] == "Line two"
        assert result["new_fragment"] == "Replaced line"
        t = tasks.get_task(self.conn, self.task["id"])
        assert "Replaced line" in t["description"]

    def test_line_range_out_of_bounds_raises(self):
        with pytest.raises(ValueError, match="invalid"):
            tasks.edit_task_field(
                self.conn, self.task["id"], "description",
                line_start=10, line_end=12, content="X"
            )

    def test_invalid_field_raises(self):
        with pytest.raises(ValueError, match="not editable"):
            tasks.edit_task_field(
                self.conn, self.task["id"], "title",
                search="T", replace="S"
            )

    def test_both_modes_raises(self):
        with pytest.raises(ValueError, match="not both"):
            tasks.edit_task_field(
                self.conn, self.task["id"], "description",
                search="Hello", replace="Hi",
                line_start=1, line_end=1, content="X"
            )


# ---------------------------------------------------------------------------
# 5.1.4 — move_task (cycle detection)
# ---------------------------------------------------------------------------

class TestMoveTask(TestTaskBase):

    def test_move_to_new_parent(self):
        root = tasks.create_task(self.conn, [{"title": "Root"}])["tasks"][0]
        t1 = tasks.create_task(self.conn, [{"title": "T1"}], parent_id=root["id"])["tasks"][0]
        t2 = tasks.create_task(self.conn, [{"title": "T2"}], parent_id=root["id"])["tasks"][0]
        tasks.move_task(self.conn, [t2["id"]], new_parent_id=t1["id"])
        assert tasks.get_task(self.conn, t2["id"])["parent_id"] == t1["id"]

    def test_move_to_root(self):
        root = tasks.create_task(self.conn, [{"title": "Root"}])["tasks"][0]
        child = tasks.create_task(self.conn, [{"title": "Child"}], parent_id=root["id"])["tasks"][0]
        tasks.move_task(self.conn, [child["id"]], new_parent_id=None)
        assert tasks.get_task(self.conn, child["id"])["parent_id"] is None

    def test_move_creates_cycle_raises(self):
        root = tasks.create_task(self.conn, [{"title": "Root"}])["tasks"][0]
        child = tasks.create_task(self.conn, [{"title": "Child"}], parent_id=root["id"])["tasks"][0]
        grandchild = tasks.create_task(self.conn, [{"title": "GC"}], parent_id=child["id"])["tasks"][0]
        with pytest.raises(ValueError, match="cycle"):
            tasks.move_task(self.conn, [root["id"]], new_parent_id=grandchild["id"])


# ---------------------------------------------------------------------------
# 5.1.5 — archive_task (cascade)
# ---------------------------------------------------------------------------

class TestArchiveTask(TestTaskBase):

    def test_archive_single(self):
        t = tasks.create_task(self.conn, [{"title": "T"}])["tasks"][0]
        result = tasks.archive_task(self.conn, [t["id"]], reason="Not needed", cascade=False)
        assert t["id"] in result["archived_ids"]
        assert tasks.get_task(self.conn, t["id"])["status"] == "archived"

    def test_archive_cascade(self):
        root = tasks.create_task(self.conn, [{"title": "Root"}])["tasks"][0]
        c1 = tasks.create_task(self.conn, [{"title": "C1"}], parent_id=root["id"])["tasks"][0]
        c2 = tasks.create_task(self.conn, [{"title": "C2"}], parent_id=root["id"])["tasks"][0]
        result = tasks.archive_task(self.conn, [root["id"]], reason="Pivot", cascade=True)
        assert len(result["archived_ids"]) == 3
        for tid in [root["id"], c1["id"], c2["id"]]:
            assert tasks.get_task(self.conn, tid)["status"] == "archived"

    def test_archive_preserves_data(self):
        t = tasks.create_task(self.conn, [{"title": "T"}])["tasks"][0]
        tasks.archive_task(self.conn, [t["id"]], reason="Changed direction")
        archived = tasks.get_task(self.conn, t["id"])
        assert archived["archive_reason"] == "Changed direction"

    def test_archive_requires_reason(self):
        t = tasks.create_task(self.conn, [{"title": "T"}])["tasks"][0]
        with pytest.raises(ValueError):
            tasks.archive_task(self.conn, [t["id"]], reason="")


# ---------------------------------------------------------------------------
# 5.1.6 — delete_task cascade (edges, code_refs, notes, contracts)
# ---------------------------------------------------------------------------

class TestDeleteCascade(TestTaskBase):

    def test_delete_cascade_removes_subtasks(self):
        root = tasks.create_task(self.conn, [{"title": "Root"}])["tasks"][0]
        c1 = tasks.create_task(self.conn, [{"title": "C1"}], parent_id=root["id"])["tasks"][0]
        c2 = tasks.create_task(self.conn, [{"title": "C2"}], parent_id=root["id"])["tasks"][0]
        result = tasks.delete_task(self.conn, root["id"], cascade=True)
        assert len(result["deleted_ids"]) == 3
        for tid in [root["id"], c1["id"], c2["id"]]:
            with pytest.raises(KeyError):
                tasks.get_task(self.conn, tid)

    def test_delete_cascade_removes_edges(self):
        root = tasks.create_task(self.conn, [{"title": "Root"}])["tasks"][0]
        t1 = tasks.create_task(self.conn, [{"title": "T1"}], parent_id=root["id"])["tasks"][0]
        t2 = tasks.create_task(self.conn, [{"title": "T2"}], parent_id=root["id"])["tasks"][0]
        tasks.add_task_edge(self.conn, [{"source_id": t1["id"], "target_id": t2["id"]}], edge_type="depends_on")
        tasks.delete_task(self.conn, root["id"], cascade=True)
        # edges table should be clean
        rows = self.conn.execute("SELECT * FROM task_edges").fetchall()
        assert len(rows) == 0

    def test_delete_cascade_removes_notes(self):
        root = tasks.create_task(self.conn, [{"title": "Root"}])["tasks"][0]
        tasks.add_note(self.conn, root["id"], [{"note_type": "decision", "content": "Use X"}])
        tasks.delete_task(self.conn, root["id"], cascade=True)
        rows = self.conn.execute("SELECT * FROM notes").fetchall()
        assert len(rows) == 0

    def test_delete_last_participant_removes_contract(self):
        """Deleting the LAST participant removes the contract entirely."""
        scope = tasks.create_task(self.conn, [{"title": "Scope"}])["tasks"][0]
        t1 = tasks.create_task(self.conn, [{"title": "T1"}], parent_id=scope["id"])["tasks"][0]
        tasks.add_contract(self.conn, contract_type="interface", definition="X",
                           name="IFoo", scope_task_id=scope["id"],
                           provider_task_id=t1["id"])  # only one participant
        tasks.delete_task(self.conn, t1["id"], cascade=False)
        rows = self.conn.execute("SELECT * FROM contracts").fetchall()
        assert len(rows) == 0

    def test_delete_participant_keeps_contract_for_others(self):
        """Deleting ONE of two participants keeps the contract alive."""
        scope = tasks.create_task(self.conn, [{"title": "Scope"}])["tasks"][0]
        t1 = tasks.create_task(self.conn, [{"title": "T1"}], parent_id=scope["id"])["tasks"][0]
        t2 = tasks.create_task(self.conn, [{"title": "T2"}], parent_id=scope["id"])["tasks"][0]
        tasks.add_contract(self.conn, contract_type="interface", definition="X",
                           name="IFoo", scope_task_id=scope["id"],
                           provider_task_id=t1["id"], consumer_task_ids=[t2["id"]])
        tasks.delete_task(self.conn, t1["id"], cascade=False)
        # Contract still exists (t2 is still a consumer)
        rows = self.conn.execute("SELECT * FROM contracts").fetchall()
        assert len(rows) == 1
        # t1 no longer in contract_links
        links = self.conn.execute(
            "SELECT task_id FROM contract_links WHERE task_id = ?", (t1["id"],)
        ).fetchall()
        assert len(links) == 0

    def test_delete_scope_removes_contract(self):
        """Deleting the scope task removes its contract."""
        scope = tasks.create_task(self.conn, [{"title": "Scope"}])["tasks"][0]
        t1 = tasks.create_task(self.conn, [{"title": "T1"}], parent_id=scope["id"])["tasks"][0]
        tasks.add_contract(self.conn, contract_type="schema", definition="Y",
                           name="Schema", scope_task_id=scope["id"],
                           provider_task_id=t1["id"])
        tasks.delete_task(self.conn, scope["id"], cascade=True)
        rows = self.conn.execute("SELECT * FROM contracts").fetchall()
        assert len(rows) == 0


# ---------------------------------------------------------------------------
# 5.1.7 — DAG edges: add + cycle detection for depends_on
# ---------------------------------------------------------------------------

class TestDAGEdges(TestTaskBase):

    def setup_method(self):
        super().setup_method()
        self.root = tasks.create_task(self.conn, [{"title": "Root"}])["tasks"][0]
        self.t1 = tasks.create_task(self.conn, [{"title": "T1"}], parent_id=self.root["id"])["tasks"][0]
        self.t2 = tasks.create_task(self.conn, [{"title": "T2"}], parent_id=self.root["id"])["tasks"][0]
        self.t3 = tasks.create_task(self.conn, [{"title": "T3"}], parent_id=self.root["id"])["tasks"][0]

    def test_add_edge(self):
        e = tasks.add_task_edge(self.conn, [{"source_id": self.t1["id"], "target_id": self.t2["id"]}], edge_type="depends_on")["edges"][0]
        assert e["type"] == "depends_on"

    def test_add_edge_invalid_type(self):
        with pytest.raises(ValueError, match="Invalid edge_type"):
            tasks.add_task_edge(self.conn, [{"source_id": self.t1["id"], "target_id": self.t2["id"]}], edge_type="unknown")

    def test_add_self_loop_raises(self):
        with pytest.raises(ValueError, match="self-referencing"):
            tasks.add_task_edge(self.conn, [{"source_id": self.t1["id"], "target_id": self.t1["id"]}], edge_type="depends_on")

    def test_cycle_detection_depends_on(self):
        tasks.add_task_edge(self.conn, [{"source_id": self.t1["id"], "target_id": self.t2["id"]}], edge_type="depends_on")
        tasks.add_task_edge(self.conn, [{"source_id": self.t2["id"], "target_id": self.t3["id"]}], edge_type="depends_on")
        with pytest.raises(ValueError, match="cycle"):
            tasks.add_task_edge(self.conn, [{"source_id": self.t3["id"], "target_id": self.t1["id"]}], edge_type="depends_on")

    def test_intra_batch_cycle_detected(self):
        # A→B and B→A in the same batch call must raise, even though neither
        # edge exists in the DB yet when the check runs.
        with pytest.raises(ValueError, match="cycle"):
            tasks.add_task_edge(
                self.conn,
                [
                    {"source_id": self.t1["id"], "target_id": self.t2["id"]},
                    {"source_id": self.t2["id"], "target_id": self.t1["id"]},
                ],
                edge_type="depends_on",
            )
        # Neither edge should have been inserted (atomic)
        edges = tasks.get_task_edges(self.conn, self.t1["id"])
        assert len(edges) == 0

    def test_no_cycle_check_for_informs(self):
        # informs edges don't require cycle check
        tasks.add_task_edge(self.conn, [{"source_id": self.t1["id"], "target_id": self.t2["id"]}], edge_type="informs")
        tasks.add_task_edge(self.conn, [{"source_id": self.t2["id"], "target_id": self.t1["id"]}], edge_type="informs")
        edges = tasks.get_task_edges(self.conn, self.t1["id"])
        assert len(edges) == 2

    def test_remove_edge(self):
        tasks.add_task_edge(self.conn, [{"source_id": self.t1["id"], "target_id": self.t2["id"]}], edge_type="depends_on")
        tasks.remove_task_edge(self.conn, self.t1["id"], self.t2["id"], "depends_on")
        edges = tasks.get_task_edges(self.conn, self.t1["id"], direction="outgoing")
        assert len(edges) == 0

    def test_remove_nonexistent_raises(self):
        with pytest.raises(KeyError):
            tasks.remove_task_edge(self.conn, self.t1["id"], self.t2["id"], "depends_on")

    def test_get_edges_direction(self):
        tasks.add_task_edge(self.conn, [{"source_id": self.t1["id"], "target_id": self.t2["id"]}], edge_type="depends_on")
        outgoing = tasks.get_task_edges(self.conn, self.t1["id"], direction="outgoing")
        incoming = tasks.get_task_edges(self.conn, self.t2["id"], direction="incoming")
        both = tasks.get_task_edges(self.conn, self.t1["id"], direction="both")
        assert len(outgoing) == 1
        assert len(incoming) == 1
        assert len(both) == 1

    def test_get_edges_invalid_direction(self):
        with pytest.raises(ValueError, match="direction"):
            tasks.get_task_edges(self.conn, self.t1["id"], direction="sideways")

    def test_get_task_dag(self):
        tasks.add_task_edge(self.conn, [{"source_id": self.t1["id"], "target_id": self.t2["id"]}], edge_type="depends_on")
        dag = tasks.get_task_dag(self.conn, self.root["id"])
        assert len(dag["nodes"]) == 4  # root + 3 children
        node_ids = {n["id"] for n in dag["nodes"]}
        assert self.root["id"] in node_ids


# ---------------------------------------------------------------------------
# 5.1.7b — task_create with inline edges
# ---------------------------------------------------------------------------

class TestCreateTaskWithInlineEdges(TestTaskBase):
    """Tests for the edges= parameter of create_task."""

    def setup_method(self):
        super().setup_method()
        self.root = tasks.create_task(self.conn, [{"title": "Root"}])["tasks"][0]

    def test_inline_edges_creates_tasks_and_edges(self):
        result = tasks.create_task(
            self.conn,
            parent_id=self.root["id"],
            tasks=[
                {"title": "Interface"},    # 0
                {"title": "Impl"},         # 1
            ],
            edges=[{"from": 1, "to": 0, "type": "depends_on"}],
        )
        assert len(result["tasks"]) == 2
        assert len(result["edges"]) == 1
        e = result["edges"][0]
        assert e["source_id"] == result["tasks"][1]["id"]
        assert e["target_id"] == result["tasks"][0]["id"]
        assert e["edge_type"] == "depends_on"

    def test_inline_edges_default_type_is_depends_on(self):
        result = tasks.create_task(
            self.conn,
            parent_id=self.root["id"],
            tasks=[{"title": "A"}, {"title": "B"}],
            edges=[{"from": 1, "to": 0}],  # no "type"
        )
        assert result["edges"][0]["edge_type"] == "depends_on"

    def test_inline_edges_non_ordering_type(self):
        result = tasks.create_task(
            self.conn,
            parent_id=self.root["id"],
            tasks=[{"title": "A"}, {"title": "B"}],
            edges=[{"from": 0, "to": 1, "type": "shares_context"}],
        )
        assert result["edges"][0]["edge_type"] == "shares_context"

    def test_inline_edges_persisted_in_db(self):
        """Edges created inline must be queryable afterwards."""
        result = tasks.create_task(
            self.conn,
            parent_id=self.root["id"],
            tasks=[{"title": "A"}, {"title": "B"}],
            edges=[{"from": 1, "to": 0, "type": "depends_on"}],
        )
        t0, t1 = result["tasks"][0]["id"], result["tasks"][1]["id"]
        outgoing = tasks.get_task_edges(self.conn, t1, direction="outgoing")
        assert len(outgoing) == 1
        assert outgoing[0]["target_task_id"] == t0

    def test_inline_edges_no_edges_key_when_none(self):
        """When edges= is omitted, result must not contain 'edges' key."""
        result = tasks.create_task(
            self.conn,
            parent_id=self.root["id"],
            tasks=[{"title": "Solo"}],
        )
        assert "edges" not in result

    def test_inline_edges_cycle_raises_atomically(self):
        """A→B + B→A inline must raise and leave no tasks in DB."""
        before = self.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        with pytest.raises(ValueError, match="cycle"):
            tasks.create_task(
                self.conn,
                parent_id=self.root["id"],
                tasks=[{"title": "A"}, {"title": "B"}],
                edges=[
                    {"from": 0, "to": 1, "type": "depends_on"},
                    {"from": 1, "to": 0, "type": "depends_on"},
                ],
            )
        after = self.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        assert after == before, "Tasks must not be committed when cycle is detected"

    def test_inline_edges_out_of_range_raises(self):
        with pytest.raises(ValueError, match="out of range"):
            tasks.create_task(
                self.conn,
                parent_id=self.root["id"],
                tasks=[{"title": "A"}, {"title": "B"}],
                edges=[{"from": 0, "to": 5}],
            )

    def test_inline_edges_self_reference_raises(self):
        with pytest.raises(ValueError, match="self-referencing"):
            tasks.create_task(
                self.conn,
                parent_id=self.root["id"],
                tasks=[{"title": "A"}, {"title": "B"}],
                edges=[{"from": 0, "to": 0}],
            )

    def test_inline_edges_invalid_type_raises(self):
        with pytest.raises(ValueError, match="Invalid edge type"):
            tasks.create_task(
                self.conn,
                parent_id=self.root["id"],
                tasks=[{"title": "A"}, {"title": "B"}],
                edges=[{"from": 0, "to": 1, "type": "not_a_valid_type"}],
            )

    def test_inline_edges_diamond_dependency(self):
        """A→C and B→C and A→B — valid DAG, no cycle."""
        result = tasks.create_task(
            self.conn,
            parent_id=self.root["id"],
            tasks=[
                {"title": "A"},   # 0
                {"title": "B"},   # 1
                {"title": "C"},   # 2
            ],
            edges=[
                {"from": 0, "to": 2, "type": "depends_on"},
                {"from": 1, "to": 2, "type": "depends_on"},
                {"from": 0, "to": 1, "type": "depends_on"},
            ],
        )
        assert len(result["tasks"]) == 3
        assert len(result["edges"]) == 3


# ---------------------------------------------------------------------------
# 5.1.8 — topological_sort: linear, diamond, cycle
# ---------------------------------------------------------------------------

class TestTopologicalSort(TestTaskBase):

    def test_linear_chain(self):
        root = tasks.create_task(self.conn, [{"title": "Root"}])["tasks"][0]
        a = tasks.create_task(self.conn, [{"title": "A"}], parent_id=root["id"])["tasks"][0]
        b = tasks.create_task(self.conn, [{"title": "B"}], parent_id=root["id"])["tasks"][0]
        c = tasks.create_task(self.conn, [{"title": "C"}], parent_id=root["id"])["tasks"][0]
        # c depends_on b depends_on a
        tasks.add_task_edge(self.conn, [{"source_id": b["id"], "target_id": a["id"]}], edge_type="depends_on")
        tasks.add_task_edge(self.conn, [{"source_id": c["id"], "target_id": b["id"]}], edge_type="depends_on")
        sorted_tasks = tasks.topological_sort_tasks(self.conn, root["id"])
        ids = [t["id"] for t in sorted_tasks]
        assert ids.index(a["id"]) < ids.index(b["id"])
        assert ids.index(b["id"]) < ids.index(c["id"])

    def test_diamond_dependency(self):
        root = tasks.create_task(self.conn, [{"title": "Root"}])["tasks"][0]
        base = tasks.create_task(self.conn, [{"title": "Base"}], parent_id=root["id"])["tasks"][0]
        left = tasks.create_task(self.conn, [{"title": "Left"}], parent_id=root["id"])["tasks"][0]
        right = tasks.create_task(self.conn, [{"title": "Right"}], parent_id=root["id"])["tasks"][0]
        top = tasks.create_task(self.conn, [{"title": "Top"}], parent_id=root["id"])["tasks"][0]
        tasks.add_task_edge(self.conn, [{"source_id": left["id"], "target_id": base["id"]}], edge_type="depends_on")
        tasks.add_task_edge(self.conn, [{"source_id": right["id"], "target_id": base["id"]}], edge_type="depends_on")
        tasks.add_task_edge(self.conn, [{"source_id": top["id"], "target_id": left["id"]}], edge_type="depends_on")
        tasks.add_task_edge(self.conn, [{"source_id": top["id"], "target_id": right["id"]}], edge_type="depends_on")
        sorted_tasks = tasks.topological_sort_tasks(self.conn, root["id"])
        ids = [t["id"] for t in sorted_tasks]
        assert ids.index(base["id"]) < ids.index(left["id"])
        assert ids.index(base["id"]) < ids.index(right["id"])
        assert ids.index(left["id"]) < ids.index(top["id"])
        assert ids.index(right["id"]) < ids.index(top["id"])

    def test_cycle_raises(self):
        root = tasks.create_task(self.conn, [{"title": "Root"}])["tasks"][0]
        a = tasks.create_task(self.conn, [{"title": "A"}], parent_id=root["id"])["tasks"][0]
        b = tasks.create_task(self.conn, [{"title": "B"}], parent_id=root["id"])["tasks"][0]
        # Force a cycle directly in DB to bypass add_task_edge cycle check
        import time
        self.conn.execute(
            "INSERT INTO task_edges (source_task_id, target_task_id, type, created_at) VALUES (?,?,?,?)",
            (a["id"], b["id"], "depends_on", time.time())
        )
        self.conn.execute(
            "INSERT INTO task_edges (source_task_id, target_task_id, type, created_at) VALUES (?,?,?,?)",
            (b["id"], a["id"], "depends_on", time.time())
        )
        self.conn.commit()
        with pytest.raises(ValueError, match="[Cc]ycle"):
            tasks.topological_sort_tasks(self.conn, root["id"])


# ---------------------------------------------------------------------------
# 5.1.9 — Code refs: link + unlink + suggest
# ---------------------------------------------------------------------------

class TestCodeRefs(TestTaskBase):

    def test_link_code(self):
        t = tasks.create_task(self.conn, [{"title": "T"}])["tasks"][0]
        nid = self._node("auth")
        result = tasks.link_task_code(self.conn, t["id"], [{"ref_type": "modifies", "code_node_id": nid}])
        assert result["task_id"] == t["id"]
        assert result["success_count"] == 1
        assert result["linked"][0]["code_node_id"] == nid

    def test_link_nonexistent_node_raises(self):
        t = tasks.create_task(self.conn, [{"title": "T"}])["tasks"][0]
        result = tasks.link_task_code(self.conn, t["id"], [{"ref_type": "modifies", "code_node_id": 99999}])
        assert result["error_count"] == 1
        assert "does not exist" in result["errors"][0]["error"]

    def test_unlink_code(self):
        t = tasks.create_task(self.conn, [{"title": "T"}])["tasks"][0]
        nid = self._node("func")
        tasks.link_task_code(self.conn, t["id"], [{"ref_type": "modifies", "code_node_id": nid}])
        tasks.unlink_task_code(self.conn, t["id"], nid)
        refs = tasks.get_task_code_refs(self.conn, t["id"])
        assert len(refs) == 0

    def test_unlink_nonexistent_raises(self):
        t = tasks.create_task(self.conn, [{"title": "T"}])["tasks"][0]
        nid = self._node("func")
        with pytest.raises(KeyError):
            tasks.unlink_task_code(self.conn, t["id"], nid)

    def test_get_code_refs_includes_node_metadata(self):
        t = tasks.create_task(self.conn, [{"title": "T"}])["tasks"][0]
        nid = self._node("my_function", "src/app.py")
        tasks.link_task_code(self.conn, t["id"], [{"ref_type": "reads", "code_node_id": nid}])
        refs = tasks.get_task_code_refs(self.conn, t["id"])
        assert len(refs) == 1
        assert refs[0]["name"] == "my_function"
        assert refs[0]["file_path"] == "src/app.py"

    def test_find_tasks_by_code_node(self):
        t1 = tasks.create_task(self.conn, [{"title": "T1"}])["tasks"][0]
        t2 = tasks.create_task(self.conn, [{"title": "T2"}], parent_id=t1["id"])["tasks"][0]
        nid = self._node("shared_func")
        tasks.link_task_code(self.conn, t1["id"], [{"ref_type": "modifies", "code_node_id": nid}])
        tasks.link_task_code(self.conn, t2["id"], [{"ref_type": "reads", "code_node_id": nid}])
        result = tasks.find_tasks_by_code_node(self.conn, nid)
        assert len(result) == 2

    def test_suggest_code_links_returns_candidates(self):
        # Node name must partially match extracted keyword (LIKE %keyword%)
        # "authentication" keyword matches "authentication_handler"
        self._node("authentication_handler", "auth.py")
        self._node("token_generator", "jwt.py")
        t = tasks.create_task(self.conn, [{"title": "Implement authentication flow", "description": "Build authentication handler"}])["tasks"][0]
        suggestions = tasks.suggest_code_links(self.conn, t["id"])
        names = [s["name"] for s in suggestions]
        assert any("authentication" in n.lower() for n in names)


# ---------------------------------------------------------------------------
# 5.1.10 — Notes: CRUD + include_parent chain
# ---------------------------------------------------------------------------

class TestNotes(TestTaskBase):

    def test_add_note(self):
        t = tasks.create_task(self.conn, [{"title": "T"}])["tasks"][0]
        note = tasks.add_note(self.conn, t["id"], [{"note_type": "decision", "content": "Use JWT", "status": "resolved", "resolution": "stateless"}])["notes"][0]
        assert note["note_type"] == "decision"
        assert note["status"] == "resolved"
        assert note["resolution"] == "stateless"

    def test_add_note_invalid_type(self):
        t = tasks.create_task(self.conn, [{"title": "T"}])["tasks"][0]
        with pytest.raises(ValueError, match="Invalid note_type"):
            tasks.add_note(self.conn, t["id"], [{"note_type": "memo", "content": "X"}])

    def test_add_note_invalid_status(self):
        t = tasks.create_task(self.conn, [{"title": "T"}])["tasks"][0]
        with pytest.raises(ValueError, match="Invalid status"):
            tasks.add_note(self.conn, t["id"], [{"note_type": "question", "content": "X", "status": "pending"}])

    def test_update_note(self):
        t = tasks.create_task(self.conn, [{"title": "T"}])["tasks"][0]
        note = tasks.add_note(self.conn, t["id"], [{"note_type": "question", "content": "WebSocket?"}])["notes"][0]
        updated = tasks.update_note(self.conn, note["id"],
                                    status="resolved", resolution="Use polling")
        assert updated["status"] == "resolved"
        assert updated["resolution"] == "Use polling"

    def test_delete_note(self):
        t = tasks.create_task(self.conn, [{"title": "T"}])["tasks"][0]
        note = tasks.add_note(self.conn, t["id"], [{"note_type": "constraint", "content": "No external deps"}])["notes"][0]
        tasks.delete_note(self.conn, note["id"])
        rows = self.conn.execute("SELECT * FROM notes WHERE id=?", (note["id"],)).fetchall()
        assert len(rows) == 0

    def test_list_notes_own_task(self):
        t = tasks.create_task(self.conn, [{"title": "T"}])["tasks"][0]
        tasks.add_note(self.conn, t["id"], [{"note_type": "decision", "content": "D1"}])
        tasks.add_note(self.conn, t["id"], [{"note_type": "question", "content": "Q1"}])
        notes = tasks.list_notes(self.conn, t["id"], include_parent=False)
        assert len(notes) == 2

    def test_list_notes_include_parent_chain(self):
        root = tasks.create_task(self.conn, [{"title": "Root"}])["tasks"][0]
        child = tasks.create_task(self.conn, [{"title": "Child"}], parent_id=root["id"])["tasks"][0]
        grandchild = tasks.create_task(self.conn, [{"title": "GC"}], parent_id=child["id"])["tasks"][0]
        tasks.add_note(self.conn, root["id"], [{"note_type": "decision", "content": "Root decision"}])
        tasks.add_note(self.conn, child["id"], [{"note_type": "constraint", "content": "Child constraint"}])
        tasks.add_note(self.conn, grandchild["id"], [{"note_type": "question", "content": "GC question"}])
        # grandchild should see all 3 notes
        notes = tasks.list_notes(self.conn, grandchild["id"], include_parent=True)
        assert len(notes) == 3

    def test_list_notes_filter_by_type(self):
        t = tasks.create_task(self.conn, [{"title": "T"}])["tasks"][0]
        tasks.add_note(self.conn, t["id"], [{"note_type": "decision", "content": "D"}])
        tasks.add_note(self.conn, t["id"], [{"note_type": "question", "content": "Q"}])
        questions = tasks.list_notes(self.conn, t["id"],
                                     note_type="question", include_parent=False)
        assert len(questions) == 1
        assert questions[0]["note_type"] == "question"

    def test_note_alternatives_roundtrip(self):
        t = tasks.create_task(self.conn, [{"title": "T"}])["tasks"][0]
        alts = ["Option A", "Option B"]
        tasks.add_note(self.conn, t["id"], [{"note_type": "decision", "content": "Use X", "alternatives": alts}])
        fetched = tasks.list_notes(self.conn, t["id"], include_parent=False)
        assert fetched[0]["alternatives"] == alts


# ---------------------------------------------------------------------------
# 5.1.11 — Contracts: CRUD + list (provider + consumer)
# ---------------------------------------------------------------------------

class TestContracts(TestTaskBase):

    def setup_method(self):
        super().setup_method()
        self.root = tasks.create_task(self.conn, [{"title": "Scope"}])["tasks"][0]
        self.t1 = tasks.create_task(self.conn, [{"title": "Provider"}], parent_id=self.root["id"])["tasks"][0]
        self.t2 = tasks.create_task(self.conn, [{"title": "Consumer"}], parent_id=self.root["id"])["tasks"][0]

    def _contract(self, name="IAuth", contract_type="interface", definition="IAuth { }",
                  provider_task_id=None, consumer_task_ids=None):
        return tasks.add_contract(
            self.conn,
            contract_type=contract_type,
            definition=definition,
            name=name,
            scope_task_id=self.root["id"],
            provider_task_id=provider_task_id or self.t1["id"],
            consumer_task_ids=consumer_task_ids or [self.t2["id"]],
        )

    def test_add_contract(self):
        c = self._contract("IAuth", "interface", "IAuth { login(): Token }")
        assert c["contract_type"] == "interface"
        assert c["status"] == "proposed"
        assert self.t1["id"] in c["provider_task_ids"]
        assert self.t2["id"] in c["consumer_task_ids"]

    def test_add_contract_invalid_type(self):
        with pytest.raises(ValueError, match="Invalid contract_type"):
            tasks.add_contract(self.conn, contract_type="memo", definition="X",
                               name="Bad", scope_task_id=self.root["id"])

    def test_update_contract_status(self):
        c = self._contract("PostAuth", "api", "POST /auth")
        updated = tasks.update_contract(self.conn, c["id"], status="agreed")
        assert updated["status"] == "agreed"

    def test_update_invalid_status_raises(self):
        c = self._contract("UserSchema", "schema", "UserSchema {}")
        with pytest.raises(ValueError, match="Invalid status"):
            tasks.update_contract(self.conn, c["id"], status="pending")

    def test_list_contracts_by_task_id_returns_both_roles(self):
        self._contract("IFoo", "interface", "X")
        t3 = tasks.create_task(self.conn, [{"title": "T3"}], parent_id=self.root["id"])["tasks"][0]
        tasks.add_contract(self.conn, contract_type="api", definition="Y",
                           name="IBar", scope_task_id=self.root["id"],
                           provider_task_id=t3["id"],
                           consumer_task_ids=[self.t1["id"]])
        # t1 is provider of first, consumer of second
        contracts = tasks.list_contracts(self.conn, task_id=self.t1["id"])
        assert len(contracts) == 2

    def test_list_contracts_by_scope(self):
        self._contract("IFoo", "interface", "X")
        self._contract("IBar", "schema", "Y")
        contracts = tasks.list_contracts(self.conn, scope_task_id=self.root["id"])
        assert len(contracts) == 2

    def test_list_contracts_empty(self):
        result = tasks.list_contracts(self.conn, task_id=self.t1["id"])
        assert result == []

    def test_list_contracts_no_filter_raises(self):
        with pytest.raises(ValueError, match="at least one filter"):
            tasks.list_contracts(self.conn)

    def test_contract_link_unlink(self):
        c = tasks.add_contract(self.conn, contract_type="schema", definition="D",
                               name="NewSchema", scope_task_id=self.root["id"])
        assert c["provider_task_ids"] == []
        # link
        tasks.link_contract(self.conn, c["id"], self.t1["id"], "provider")
        tasks.link_contract(self.conn, c["id"], self.t2["id"], "consumer")
        c2 = tasks.update_contract(self.conn, c["id"])  # noop just re-fetch
        updated = tasks.link_contract(self.conn, c["id"], self.t1["id"], "provider")  # idempotent
        assert self.t1["id"] in updated["provider_task_ids"]
        assert self.t2["id"] in updated["consumer_task_ids"]
        # unlink
        tasks.unlink_contract(self.conn, c["id"], self.t1["id"])
        after = tasks.list_contracts(self.conn, scope_task_id=self.root["id"])
        found = next(x for x in after if x["id"] == c["id"])
        assert self.t1["id"] not in found["provider_task_ids"]
        assert self.t2["id"] in found["consumer_task_ids"]

    def test_contract_status_auto_propagates_on_task_update(self):
        c = self._contract()
        assert c["status"] == "proposed"
        tasks.update_task(self.conn, self.t1["id"], status="done")
        c_after = tasks.list_contracts(self.conn, task_id=self.t1["id"])
        assert c_after[0]["status"] == "implemented"


# ---------------------------------------------------------------------------
# Contracts: code_node_id / qualified_name resolution
# ---------------------------------------------------------------------------

class TestContractCodeNode(TestTaskBase):
    """Tests for add_contract / update_contract with qualified_name."""

    def setup_method(self):
        super().setup_method()
        self.root = tasks.create_task(self.conn, [{"title": "Root"}])["tasks"][0]

    def _insert_node(self, name: str, qname: str) -> int:
        import time
        cur = self.conn.execute(
            """INSERT INTO nodes (kind, name, qualified_name, file_path,
               line_start, line_end, language, updated_at)
               VALUES ('Function', ?, ?, 'fake.py', 1, 10, 'python', ?)""",
            (name, qname, time.time()),
        )
        self.conn.commit()
        return cur.lastrowid

    def test_add_contract_with_code_node_id(self):
        """Classic path: link contract via integer code_node_id."""
        nid = self._insert_node("auth_fn", "src/auth.py::auth_fn")
        c = tasks.add_contract(self.conn, "schema", "{ token: str }",
                               "AuthToken", self.root["id"],
                               code_node_id=nid)
        assert c["code_node_id"] == nid

    def test_add_contract_with_qualified_name(self):
        """New path: link contract via qualified_name string."""
        nid = self._insert_node("user_repo", "src/repo.py::user_repo")
        c = tasks.add_contract(self.conn, "interface", "IUserRepo { find(): User }",
                               "IUserRepo", self.root["id"],
                               qualified_name="src/repo.py::user_repo")
        assert c["code_node_id"] == nid

    def test_add_contract_qualified_name_windows_sep(self):
        """Windows backslash in qualified_name is normalised."""
        nid = self._insert_node("parse_fn", "src/parser.py::parse_fn")
        c = tasks.add_contract(self.conn, "schema", "ParseResult {}",
                               "ParseResult", self.root["id"],
                               qualified_name=r"src\parser.py::parse_fn")
        assert c["code_node_id"] == nid

    def test_add_contract_qualified_name_not_found(self):
        """Non-existent qualified_name raises ValueError."""
        with pytest.raises(ValueError, match="no code node found"):
            tasks.add_contract(self.conn, "schema", "X", "Ghost",
                               self.root["id"],
                               qualified_name="nonexistent.py::ghost")

    def test_add_contract_both_raises(self):
        """Providing both code_node_id and qualified_name raises ValueError."""
        nid = self._insert_node("fn", "f.py::fn")
        with pytest.raises(ValueError, match="not both"):
            tasks.add_contract(self.conn, "schema", "X", "Both",
                               self.root["id"],
                               code_node_id=nid,
                               qualified_name="f.py::fn")

    def test_update_contract_with_qualified_name(self):
        """update_contract accepts qualified_name to set code_node_id."""
        nid = self._insert_node("svc", "core/svc.py::svc")
        c = tasks.add_contract(self.conn, "schema", "Svc {}", "Svc",
                               self.root["id"])
        assert c.get("code_node_id") is None
        updated = tasks.update_contract(self.conn, c["id"],
                                        qualified_name="core/svc.py::svc")
        assert updated["code_node_id"] == nid

    def test_update_contract_qname_not_found(self):
        """Non-existent qualified_name in update_contract raises ValueError."""
        c = tasks.add_contract(self.conn, "schema", "X", "Y", self.root["id"])
        with pytest.raises(ValueError, match="no code node found"):
            tasks.update_contract(self.conn, c["id"],
                                  qualified_name="no.py::ghost")


# ---------------------------------------------------------------------------
# Misc: search_tasks
# ---------------------------------------------------------------------------

class TestSearchTasks(TestTaskBase):

    def test_search_finds_by_title(self):
        root = tasks.create_task(self.conn, [{"title": "OAuth integration"}])["tasks"][0]
        tasks.create_task(self.conn, [{"title": "JWT tokens"}], parent_id=root["id"])
        tasks.create_task(self.conn, [{"title": "Unrelated task"}], parent_id=root["id"])
        results = tasks.search_tasks(self.conn, root["id"], "OAuth")
        ids = [r["id"] for r in results]
        assert root["id"] in ids

    def test_search_is_case_insensitive(self):
        root = tasks.create_task(self.conn, [{"title": "Root", "description": "Authentication flow"}])["tasks"][0]
        results = tasks.search_tasks(self.conn, root["id"], "authentication")
        assert len(results) == 1

    def test_search_empty_query_raises(self):
        root = tasks.create_task(self.conn, [{"title": "Root"}])["tasks"][0]
        with pytest.raises(ValueError):
            tasks.search_tasks(self.conn, root["id"], "")

    def test_search_limited_to_subtree(self):
        root = tasks.create_task(self.conn, [{"title": "Root"}])["tasks"][0]
        other = tasks.create_task(self.conn, [{"title": "Other branch"}], parent_id=root["id"])["tasks"][0]
        tasks.create_task(self.conn, [{"title": "hidden"}], parent_id=other["id"])
        # search from root should find hidden
        results_root = tasks.search_tasks(self.conn, root["id"], "hidden")
        assert len(results_root) == 1
        # search from a sibling subtree should NOT find it
        sibling = tasks.create_task(self.conn, [{"title": "Sibling"}], parent_id=root["id"])["tasks"][0]
        results_sibling = tasks.search_tasks(self.conn, sibling["id"], "hidden")
        assert len(results_sibling) == 0


# ---------------------------------------------------------------------------
# Edge cases: wildcard search, list_tasks conflict, delete non-cascade
# ---------------------------------------------------------------------------

class TestEdgeCases(TestTaskBase):

    def test_search_tasks_with_percent_wildcard(self):
        """% in query must be treated as literal, not SQL wildcard."""
        root = tasks.create_task(self.conn, [{"title": "Root"}])["tasks"][0]
        tasks.create_task(self.conn, [{"title": "100% complete"}], parent_id=root["id"])
        tasks.create_task(self.conn, [{"title": "50 percent done"}], parent_id=root["id"])
        results = tasks.search_tasks(self.conn, root["id"], "100%")
        titles = [r["title"] for r in results]
        assert "100% complete" in titles
        assert "50 percent done" not in titles

    def test_search_tasks_with_underscore_wildcard(self):
        """_ in query must be treated as literal, not SQL wildcard."""
        root = tasks.create_task(self.conn, [{"title": "Root"}])["tasks"][0]
        tasks.create_task(self.conn, [{"title": "func_a helper"}], parent_id=root["id"])
        tasks.create_task(self.conn, [{"title": "funcXa helper"}], parent_id=root["id"])
        results = tasks.search_tasks(self.conn, root["id"], "func_a")
        titles = [r["title"] for r in results]
        assert "func_a helper" in titles
        assert "funcXa helper" not in titles

    def test_list_tasks_root_only_ignores_parent_id(self):
        """root_only=True should override parent_id."""
        root = tasks.create_task(self.conn, [{"title": "Root"}])["tasks"][0]
        child = tasks.create_task(self.conn, [{"title": "Child"}], parent_id=root["id"])["tasks"][0]
        result = tasks.list_tasks(self.conn, root_only=True, parent_id=child["id"])
        assert len(result) == 1
        assert result[0]["id"] == root["id"]

    def test_delete_no_cascade_leaves_children(self):
        """delete_task(cascade=False) should not delete children."""
        root = tasks.create_task(self.conn, [{"title": "Root"}])["tasks"][0]
        child = tasks.create_task(self.conn, [{"title": "Child"}], parent_id=root["id"])["tasks"][0]
        tasks.delete_task(self.conn, root["id"], cascade=False)
        with pytest.raises(KeyError):
            tasks.get_task(self.conn, root["id"])
        # Child remains (parent_id points to deleted task — SQLite no FK enforcement by default)
        remaining = tasks.get_task(self.conn, child["id"])
        assert remaining["id"] == child["id"]

    def test_edit_task_field_on_empty_field(self):
        """search/replace on empty field should raise ValueError."""
        t = tasks.create_task(self.conn, [{"title": "T"}])["tasks"][0]  # description is None
        with pytest.raises(ValueError, match="not found"):
            tasks.edit_task_field(self.conn, t["id"], "description",
                                  search="anything", replace="x")

    def test_update_task_nonexistent_raises(self):
        """update_task on nonexistent ID raises KeyError."""
        with pytest.raises(KeyError):
            tasks.update_task(self.conn, "no-such-id", title="X")

    def test_move_task_nonexistent_target_raises(self):
        """move_task to nonexistent new_parent_id raises KeyError."""
        t = tasks.create_task(self.conn, [{"title": "T"}])["tasks"][0]
        with pytest.raises(KeyError):
            tasks.move_task(self.conn, [t["id"]], new_parent_id="no-such-parent")


# ---------------------------------------------------------------------------
# Single-pipeline discipline tests
# ---------------------------------------------------------------------------

class TestSinglePipelineDiscipline(TestTaskBase):
    """Tests for the one-root-at-a-time enforcement."""

    def test_get_active_root_returns_none_when_empty(self):
        """No tasks → get_active_root returns None."""
        assert tasks.get_active_root(self.conn) is None

    def test_get_active_root_returns_open_root(self):
        """Create a root task → get_active_root returns it."""
        root = tasks.create_task(self.conn, [{"title": "Pipeline A"}])["tasks"][0]
        result = tasks.get_active_root(self.conn)
        assert result is not None
        assert result["id"] == root["id"]

    def test_create_second_root_blocked_while_first_open(self):
        """Creating a second root task raises ValueError."""
        tasks.create_task(self.conn, [{"title": "Pipeline A"}])
        with pytest.raises(ValueError, match="open"):
            tasks.create_task(self.conn, [{"title": "Pipeline B"}])

    def test_subtasks_always_allowed_regardless_of_root(self):
        """Subtasks can be created even while a root is open."""
        root = tasks.create_task(self.conn, [{"title": "Pipeline A"}])["tasks"][0]
        child = tasks.create_task(self.conn, [{"title": "Subtask"}], parent_id=root["id"])["tasks"][0]
        assert child["parent_id"] == root["id"]

    def test_can_create_new_root_after_done(self):
        """After marking root done, a new root can be created."""
        root = tasks.create_task(self.conn, [{"title": "Pipeline A"}])["tasks"][0]
        tasks.update_task(self.conn, root["id"], status="done")
        root2 = tasks.create_task(self.conn, [{"title": "Pipeline B"}])["tasks"][0]
        assert root2["title"] == "Pipeline B"

    def test_can_create_new_root_after_archived(self):
        """After archiving root, a new root can be created."""
        root = tasks.create_task(self.conn, [{"title": "Pipeline A"}])["tasks"][0]
        tasks.archive_task(self.conn, [root["id"]], reason="cancelled")
        root2 = tasks.create_task(self.conn, [{"title": "Pipeline B"}])["tasks"][0]
        assert root2["title"] == "Pipeline B"

    def test_get_active_root_ignores_done_root(self):
        """Done root does not appear as active root."""
        root = tasks.create_task(self.conn, [{"title": "Pipeline A"}])["tasks"][0]
        tasks.update_task(self.conn, root["id"], status="done")
        assert tasks.get_active_root(self.conn) is None

    def test_get_active_root_ignores_archived_root(self):
        """Archived root does not appear as active root."""
        root = tasks.create_task(self.conn, [{"title": "Pipeline A"}])["tasks"][0]
        tasks.archive_task(self.conn, [root["id"]], reason="cancelled")
        assert tasks.get_active_root(self.conn) is None

    def test_draft_root_is_active(self):
        """draft status = open = active root."""
        root = tasks.create_task(self.conn, [{"title": "Pipeline A"}])["tasks"][0]
        assert tasks.get_active_root(self.conn)["id"] == root["id"]

    def test_in_progress_root_is_active(self):
        """in_progress status = open = active root."""
        root = tasks.create_task(self.conn, [{"title": "Pipeline A"}])["tasks"][0]
        tasks.update_task(self.conn, root["id"], status="in_progress")
        assert tasks.get_active_root(self.conn)["id"] == root["id"]

    def test_error_message_includes_existing_title(self):
        """Error message names the blocking task."""
        root = tasks.create_task(self.conn, [{"title": "My Feature"}])["tasks"][0]
        with pytest.raises(ValueError, match="My Feature"):
            tasks.create_task(self.conn, [{"title": "Another Feature"}])


# ---------------------------------------------------------------------------
# task_link_code: qualified_name as alternative to code_node_id
# ---------------------------------------------------------------------------

class TestLinkCodeQualifiedName(TestTaskBase):
    """Tests for link_task_code with qualified_name parameter."""

    def _insert_node(self, name: str, qname: str) -> int:
        """Insert a fake node into nodes table and return its id."""
        import time
        cur = self.conn.execute(
            """INSERT INTO nodes (kind, name, qualified_name, file_path,
               line_start, line_end, language, updated_at)
               VALUES ('Function', ?, ?, 'fake.py', 1, 10, 'python', ?)""",
            (name, qname, time.time()),
        )
        self.conn.commit()
        return cur.lastrowid

    def test_link_by_code_node_id(self):
        """Classic path: link via integer code_node_id."""
        root = tasks.create_task(self.conn, [{"title": "Root"}])["tasks"][0]
        t = tasks.create_task(self.conn, [{"title": "T"}], parent_id=root["id"])["tasks"][0]
        nid = self._insert_node("my_func", "src/mod.py::my_func")
        result = tasks.link_task_code(self.conn, t["id"], [{"ref_type": "modifies", "code_node_id": nid}])
        assert result["linked"][0]["code_node_id"] == nid

    def test_link_by_qualified_name(self):
        """New path: link via qualified_name string."""
        root = tasks.create_task(self.conn, [{"title": "Root"}])["tasks"][0]
        t = tasks.create_task(self.conn, [{"title": "T"}], parent_id=root["id"])["tasks"][0]
        nid = self._insert_node("auth_func", "src/auth.py::auth_func")
        result = tasks.link_task_code(
            self.conn, t["id"],
            [{"ref_type": "reads", "qualified_name": "src/auth.py::auth_func"}],
        )
        assert result["linked"][0]["code_node_id"] == nid
        assert result["linked"][0]["ref_type"] == "reads"

    def test_link_by_qualified_name_windows_separator(self):
        """Windows backslash in qualified_name is normalised."""
        root = tasks.create_task(self.conn, [{"title": "Root"}])["tasks"][0]
        t = tasks.create_task(self.conn, [{"title": "T"}], parent_id=root["id"])["tasks"][0]
        nid = self._insert_node("parse_fn", "src/parser.py::parse_fn")
        result = tasks.link_task_code(
            self.conn, t["id"],
            [{"ref_type": "modifies", "qualified_name": r"src\parser.py::parse_fn"}],
        )
        assert result["linked"][0]["code_node_id"] == nid

    def test_link_by_qualified_name_not_found_raises(self):
        """Non-existent qualified_name goes to errors (batch mode — no raise)."""
        root = tasks.create_task(self.conn, [{"title": "Root"}])["tasks"][0]
        t = tasks.create_task(self.conn, [{"title": "T"}], parent_id=root["id"])["tasks"][0]
        result = tasks.link_task_code(
            self.conn, t["id"],
            [{"ref_type": "modifies", "qualified_name": "nonexistent.py::ghost_func"}],
        )
        assert result["error_count"] == 1
        assert "no code node found" in result["errors"][0]["error"]

    def test_link_both_raises(self):
        """Providing both code_node_id and qualified_name goes to errors."""
        root = tasks.create_task(self.conn, [{"title": "Root"}])["tasks"][0]
        t = tasks.create_task(self.conn, [{"title": "T"}], parent_id=root["id"])["tasks"][0]
        nid = self._insert_node("fn", "f.py::fn")
        result = tasks.link_task_code(
            self.conn, t["id"],
            [{"ref_type": "modifies", "code_node_id": nid, "qualified_name": "f.py::fn"}],
        )
        assert result["error_count"] == 1
        assert "not both" in result["errors"][0]["error"]

    def test_link_neither_raises(self):
        """Providing neither code_node_id nor qualified_name goes to errors."""
        root = tasks.create_task(self.conn, [{"title": "Root"}])["tasks"][0]
        t = tasks.create_task(self.conn, [{"title": "T"}], parent_id=root["id"])["tasks"][0]
        result = tasks.link_task_code(
            self.conn, t["id"],
            [{"ref_type": "modifies"}],
        )
        assert result["error_count"] == 1
        assert "required" in result["errors"][0]["error"]

    def test_link_by_qualified_name_double_backslash(self):
        """Double-backslash Windows path (JSON-escaped) resolves correctly."""
        root = tasks.create_task(self.conn, [{"title": "Root"}])["tasks"][0]
        t = tasks.create_task(self.conn, [{"title": "T"}], parent_id=root["id"])["tasks"][0]
        import time
        cur = self.conn.execute(
            "INSERT INTO nodes(kind, name, qualified_name, file_path, "
            "line_start, line_end, language, updated_at) "
            "VALUES('Function','win_fn','C:\\\\proj\\\\src.py::win_fn',"
            "'C:\\\\proj\\\\src.py',1,10,'python',?)",
            (time.time(),),
        )
        self.conn.commit()
        nid = cur.lastrowid
        # Pass double-backslash qualified_name (as LLM receives from JSON)
        result = tasks.link_task_code(
            self.conn, t["id"],
            [{"ref_type": "modifies", "qualified_name": "C:\\\\proj\\\\src.py::win_fn"}],
        )
        assert result["linked"][0]["code_node_id"] == nid

    def test_link_by_single_backslash_path(self):
        """Single-backslash Windows path resolves correctly."""
        root = tasks.create_task(self.conn, [{"title": "Root"}])["tasks"][0]
        t = tasks.create_task(self.conn, [{"title": "T"}], parent_id=root["id"])["tasks"][0]
        import time
        cur = self.conn.execute(
            "INSERT INTO nodes(kind, name, qualified_name, file_path, "
            "line_start, line_end, language, updated_at) "
            "VALUES('Function','win2_fn','C:\\proj\\src2.py::win2_fn',"
            "'C:\\proj\\src2.py',1,10,'python',?)",
            (time.time(),),
        )
        self.conn.commit()
        nid = cur.lastrowid
        tasks.link_task_code(
            self.conn, t["id"],
            [{"ref_type": "reads", "qualified_name": "C:\\proj\\src2.py::win2_fn"}],
        )


# ---------------------------------------------------------------------------
# Batch link_task_code
# ---------------------------------------------------------------------------

class TestBatchLinkCode(TestTaskBase):
    """Tests for batch mode of link_task_code."""

    def test_batch_by_code_node_id(self):
        """Batch with code_node_id integers links all nodes."""
        t = tasks.create_task(self.conn, [{"title": "T"}])["tasks"][0]
        n1 = self._node("fn_a", "src/a.py")
        n2 = self._node("fn_b", "src/b.py")
        n3 = self._node("fn_c", "src/c.py")

        result = tasks.link_task_code(
            self.conn, t["id"],
            [
                {"ref_type": "modifies", "code_node_id": n1},
                {"ref_type": "reads",    "code_node_id": n2},
                {"ref_type": "creates",  "code_node_id": n3},
            ],
        )
        assert result["success_count"] == 3
        assert result["error_count"] == 0
        assert result["total"] == 3
        refs = tasks.get_task_code_refs(self.conn, t["id"])
        assert len(refs) == 3
        ref_types = {r["ref_type"] for r in refs}
        assert ref_types == {"modifies", "reads", "creates"}

    def test_batch_by_qualified_name(self):
        """Batch with qualified_name strings links all nodes."""
        t = tasks.create_task(self.conn, [{"title": "T"}])["tasks"][0]
        self._node("svc_a", "src/svc.py")
        self._node("svc_b", "src/svc.py")

        result = tasks.link_task_code(
            self.conn, t["id"],
            [
                {"ref_type": "modifies", "qualified_name": "src/svc.py::svc_a"},
                {"ref_type": "reads",    "qualified_name": "src/svc.py::svc_b"},
            ],
        )
        assert result["success_count"] == 2
        assert result["error_count"] == 0
        refs = tasks.get_task_code_refs(self.conn, t["id"])
        assert len(refs) == 2

    def test_batch_mixed_id_and_qualified_name(self):
        """Batch can mix code_node_id and qualified_name."""
        t = tasks.create_task(self.conn, [{"title": "T"}])["tasks"][0]
        n1 = self._node("fn_x", "src/x.py")
        self._node("fn_y", "src/y.py")

        result = tasks.link_task_code(
            self.conn, t["id"],
            [
                {"ref_type": "modifies", "code_node_id": n1},
                {"ref_type": "reads",    "qualified_name": "src/y.py::fn_y"},
            ],
        )
        assert result["success_count"] == 2
        assert result["error_count"] == 0

    def test_batch_with_description(self):
        """Each batch item can have its own description."""
        t = tasks.create_task(self.conn, [{"title": "T"}])["tasks"][0]
        n1 = self._node("fn_d", "src/d.py")
        n2 = self._node("fn_e", "src/e.py")

        result = tasks.link_task_code(
            self.conn, t["id"],
            [
                {"ref_type": "modifies", "code_node_id": n1, "description": "core change"},
                {"ref_type": "reads",    "code_node_id": n2, "description": "side effect"},
            ],
        )
        assert result["success_count"] == 2
        linked = result["linked"]
        descs = {item["description"] for item in linked}
        assert "core change" in descs
        assert "side effect" in descs

    def test_batch_partial_failure_continues(self):
        """Invalid items are collected in errors; valid items are still linked."""
        t = tasks.create_task(self.conn, [{"title": "T"}])["tasks"][0]
        n1 = self._node("fn_ok", "src/ok.py")

        result = tasks.link_task_code(
            self.conn, t["id"],
            [
                {"ref_type": "modifies",  "code_node_id": n1},          # valid
                {"ref_type": "modifies",  "code_node_id": 99999},        # nonexistent node
                {"ref_type": "INVALID",   "code_node_id": n1},           # bad ref_type
                {"ref_type": "reads",     "qualified_name": "no::such"}, # not found
            ],
        )
        assert result["success_count"] == 1
        assert result["error_count"] == 3
        assert result["total"] == 4
        # The valid item was still linked
        refs = tasks.get_task_code_refs(self.conn, t["id"])
        assert len(refs) == 1

    def test_batch_errors_contain_item_and_message(self):
        """Error entries expose the original item and a readable message."""
        t = tasks.create_task(self.conn, [{"title": "T"}])["tasks"][0]

        result = tasks.link_task_code(
            self.conn, t["id"],
            [{"ref_type": "modifies", "code_node_id": 99999}],
        )
        assert len(result["errors"]) == 1
        err = result["errors"][0]
        assert "item" in err
        assert "error" in err
        assert isinstance(err["error"], str)

    def test_batch_empty_list(self):
        """Empty list returns success with zero counts."""
        t = tasks.create_task(self.conn, [{"title": "T"}])["tasks"][0]
        result = tasks.link_task_code(self.conn, t["id"], [])
        assert result["success_count"] == 0
        assert result["error_count"] == 0
        assert result["total"] == 0

    def test_batch_non_list_raises(self):
        """Passing a non-list raises ValueError."""
        t = tasks.create_task(self.conn, [{"title": "T"}])["tasks"][0]
        with pytest.raises(ValueError, match="links must be a list"):
            tasks.link_task_code(self.conn, t["id"], "not-a-list")

    def test_batch_idempotent_insert_or_replace(self):
        """Linking the same (task, node, ref_type) twice replaces, not duplicates.
        Different ref_types for the same node produce separate rows (by design)."""
        t = tasks.create_task(self.conn, [{"title": "T"}])["tasks"][0]
        n1 = self._node("fn_idem", "src/idem.py")

        # First batch: modifies + reads → 2 rows (different ref_type)
        tasks.link_task_code(
            self.conn, t["id"],
            [
                {"ref_type": "modifies", "code_node_id": n1},
                {"ref_type": "reads",    "code_node_id": n1},
            ],
        )
        refs = tasks.get_task_code_refs(self.conn, t["id"])
        assert len(refs) == 2  # modifies + reads

        # Second batch: link same ref_type again → INSERT OR REPLACE, still 2 rows
        tasks.link_task_code(
            self.conn, t["id"],
            [{"ref_type": "modifies", "code_node_id": n1}],  # duplicate → replace
        )
        refs2 = tasks.get_task_code_refs(self.conn, t["id"])
        assert len(refs2) == 2  # no new row added, existing replaced
        node_ids = {r["code_node_id"] for r in refs2}
        assert node_ids == {n1}  # same node, two ref_types
