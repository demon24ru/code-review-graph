"""Tests for sequence_generator.py — Mermaid sequence diagram generation."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from code_review_graph.flows import store_flows
from code_review_graph.graph import GraphStore
from code_review_graph.parser import EdgeInfo, NodeInfo
from code_review_graph.sequence_generator import (
    generate_sequence,
    sequence_from_contracts,
    sequence_from_flow,
)
from code_review_graph import tasks


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


class FlowTestBase:
    """Base class: fresh GraphStore per test."""

    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.store = GraphStore(self.tmp.name)

    def teardown_method(self):
        self.store.close()
        try:
            self.tmp.close()
        except Exception:
            pass
        Path(self.tmp.name).unlink(missing_ok=True)

    def _add_func(
        self,
        name: str,
        path: str = "app.py",
        is_test: bool = False,
    ) -> int:
        node = NodeInfo(
            kind="Test" if is_test else "Function",
            name=name,
            file_path=path,
            line_start=1,
            line_end=10,
            language="python",
            is_test=is_test,
        )
        nid = self.store.upsert_node(node, file_hash="abc")
        self.store.commit()
        return nid

    def _add_call(self, source_qn: str, target_qn: str, path: str = "app.py") -> None:
        edge = EdgeInfo(kind="CALLS", source=source_qn, target=target_qn, file_path=path, line=5)
        self.store.upsert_edge(edge)
        self.store.commit()

    def _store_flow(
        self, name: str, entry_point_id: int, node_ids: list[int]
    ) -> int:
        """Persist a flow and return its DB id."""
        flow_dict = {
            "name": name,
            "entry_point_id": entry_point_id,
            "depth": len(node_ids) - 1,
            "node_count": len(node_ids),
            "file_count": 1,
            "criticality": 0.5,
            "path": node_ids,
        }
        store_flows(self.store, [flow_dict])
        # Retrieve the id assigned by store_flows
        row = self.store._conn.execute(
            "SELECT id FROM flows WHERE name = ? ORDER BY id DESC LIMIT 1", (name,)
        ).fetchone()
        return row["id"]


class TaskTestBase:
    """Base class: fresh GraphStore + conn per test."""

    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.store = GraphStore(self.tmp.name)
        self.conn = self.store._conn

    def teardown_method(self):
        self.store.close()
        try:
            self.tmp.close()
        except Exception:
            pass
        Path(self.tmp.name).unlink(missing_ok=True)

    def _create_root(self, title: str = "Root task") -> dict:
        return tasks.create_task(self.conn, [{"title": title}])["tasks"][0]

    def _create_child(self, parent_id: str, title: str) -> dict:
        return tasks.create_task(self.conn, [{"title": title}], parent_id=parent_id)["tasks"][0]


# ---------------------------------------------------------------------------
# Mode A: From execution flow
# ---------------------------------------------------------------------------


class TestSequenceFromFlow(FlowTestBase):

    def test_sequence_from_flow_basic(self):
        """Three steps across three different files → three participants, two arrows."""
        nid_a = self._add_func("entry_fn", path="routes.py")
        nid_b = self._add_func("process_fn", path="service.py")
        nid_c = self._add_func("save_fn", path="db.py")

        flow_id = self._store_flow("entry_fn", nid_a, [nid_a, nid_b, nid_c])

        result = sequence_from_flow(self.store, flow_id=flow_id)

        assert result["status"] == "ok"
        assert result["source"] == "flow"
        assert result["participant_count"] == 3
        assert result["step_count"] == 3

        mermaid = result["mermaid"]
        assert mermaid.startswith("sequenceDiagram")
        assert "participant routes as routes.py" in mermaid
        assert "participant service as service.py" in mermaid
        assert "participant db as db.py" in mermaid
        assert "routes->>service:" in mermaid
        assert "service->>db:" in mermaid

    def test_sequence_from_flow_same_file(self):
        """Two steps in the same file → single participant, self-call arrow."""
        nid_a = self._add_func("fn_a", path="utils.py")
        nid_b = self._add_func("fn_b", path="utils.py")

        flow_id = self._store_flow("fn_a", nid_a, [nid_a, nid_b])

        result = sequence_from_flow(self.store, flow_id=flow_id)

        assert result["status"] == "ok"
        assert result["participant_count"] == 1  # same file = same participant
        mermaid = result["mermaid"]
        assert "participant utils as utils.py" in mermaid
        # Self-call: utils->>utils
        assert "utils->>utils:" in mermaid

    def test_sequence_from_flow_by_name(self):
        """Look up flow by name (partial match)."""
        nid_a = self._add_func("handle_req", path="api.py")
        nid_b = self._add_func("validate", path="validators.py")

        self._store_flow("handle_req", nid_a, [nid_a, nid_b])

        result = sequence_from_flow(self.store, flow_name="handle")

        assert result["status"] == "ok"
        assert "participant api as api.py" in result["mermaid"]

    def test_sequence_from_flow_not_found(self):
        """Non-existent flow ID → not_found status."""
        result = sequence_from_flow(self.store, flow_id=9999)

        assert result["status"] == "not_found"
        assert result["mermaid"] == ""
        assert result["participant_count"] == 0

    def test_sequence_from_flow_not_found_by_name(self):
        """Non-existent flow name → not_found status."""
        result = sequence_from_flow(self.store, flow_name="does_not_exist")

        assert result["status"] == "not_found"
        assert result["mermaid"] == ""

    def test_sequence_from_flow_mermaid_structure(self):
        """Generated Mermaid is valid: starts with sequenceDiagram, has ->> arrows."""
        nid_a = self._add_func("start", path="a.py")
        nid_b = self._add_func("middle", path="b.py")
        nid_c = self._add_func("end_fn", path="c.py")

        flow_id = self._store_flow("start", nid_a, [nid_a, nid_b, nid_c])
        result = sequence_from_flow(self.store, flow_id=flow_id)

        mermaid = result["mermaid"]
        lines = mermaid.splitlines()
        assert lines[0] == "sequenceDiagram"
        # Arrows use ->>
        arrow_lines = [ln for ln in lines if "->>" in ln]
        assert len(arrow_lines) == 2  # 3 steps → 2 arrows

    def test_sequence_from_flow_duplicate_basename(self):
        """Two files with the same basename get unique participant IDs."""
        nid_a = self._add_func("fn_a", path="pkg1/utils.py")
        nid_b = self._add_func("fn_b", path="pkg2/utils.py")

        flow_id = self._store_flow("fn_a", nid_a, [nid_a, nid_b])
        result = sequence_from_flow(self.store, flow_id=flow_id)

        assert result["status"] == "ok"
        assert result["participant_count"] == 2
        mermaid = result["mermaid"]
        # Both participants declared (utils and utils_1)
        assert "participant utils as" in mermaid
        assert "participant utils_1 as" in mermaid


# ---------------------------------------------------------------------------
# Mode B: From task contracts
# ---------------------------------------------------------------------------


class TestSequenceFromContracts(TaskTestBase):

    def test_sequence_from_contracts_basic(self):
        """Task with one contract: provider ->> consumer with contract name as label."""
        root = self._create_root("Feature X")
        provider = self._create_child(root["id"], "Provider Task")
        consumer = self._create_child(root["id"], "Consumer Task")

        tasks.add_contract(
            self.conn,
            contract_type="api",
            definition="GET /users -> [User]",
            name="GetUsers",
            scope_task_id=root["id"],
            provider_task_id=provider["id"],
            consumer_task_ids=[consumer["id"]],
        )

        result = sequence_from_contracts(self.conn, root["id"])

        assert result["status"] == "ok"
        assert result["source"] == "contracts"
        assert result["participant_count"] == 2
        assert result["call_count"] == 1

        mermaid = result["mermaid"]
        assert mermaid.startswith("sequenceDiagram")
        # Provider and consumer are declared
        assert "Provider Task" in mermaid
        assert "Consumer Task" in mermaid
        # Arrow from provider to consumer with contract name
        assert "GetUsers" in mermaid
        assert "->>" in mermaid

    def test_sequence_from_contracts_with_gaps(self):
        """No risk notes → 'No error cases defined' appears in gaps."""
        root = self._create_root("Feature Y")
        t1 = self._create_child(root["id"], "Task A")
        t2 = self._create_child(root["id"], "Task B")

        tasks.add_contract(
            self.conn,
            contract_type="schema",
            definition="{ id: str, value: int }",
            name="DataRecord",
            scope_task_id=root["id"],
            provider_task_id=t1["id"],
            consumer_task_ids=[t2["id"]],
        )

        result = sequence_from_contracts(self.conn, root["id"])

        assert result["status"] == "ok"
        gaps = result["gaps"]
        assert any("error cases" in g.lower() for g in gaps)

    def test_sequence_from_contracts_with_risk_note(self):
        """When a risk note exists, 'No error cases defined' is NOT in gaps."""
        root = self._create_root("Feature Z")
        t1 = self._create_child(root["id"], "Task A")
        t2 = self._create_child(root["id"], "Task B")

        tasks.add_contract(
            self.conn,
            contract_type="api",
            definition="POST /items",
            name="CreateItem",
            scope_task_id=root["id"],
            provider_task_id=t1["id"],
            consumer_task_ids=[t2["id"]],
        )

        # Add a risk note (batch API: notes=[{...}])
        tasks.add_note(
            self.conn,
            t1["id"],
            notes=[{"note_type": "risk", "content": "Database may be unavailable"}],
        )

        result = sequence_from_contracts(self.conn, root["id"])
        gaps = result["gaps"]
        assert not any("No error cases defined" in g for g in gaps)

    def test_sequence_from_contracts_empty(self):
        """No contracts → minimal output with gap warning."""
        root = self._create_root("Empty Feature")

        result = sequence_from_contracts(self.conn, root["id"])

        assert result["status"] == "ok"
        assert result["source"] == "contracts"
        assert result["call_count"] == 0

        mermaid = result["mermaid"]
        assert mermaid.startswith("sequenceDiagram")

        gaps = result["gaps"]
        assert any("No contracts" in g for g in gaps)

    def test_sequence_from_contracts_multiple_contracts(self):
        """Multiple contracts produce multiple arrows."""
        root = self._create_root("Multi-contract")
        t1 = self._create_child(root["id"], "Auth Service")
        t2 = self._create_child(root["id"], "API Gateway")
        t3 = self._create_child(root["id"], "DB Layer")

        tasks.add_contract(
            self.conn,
            contract_type="api",
            definition="POST /login",
            name="LoginAPI",
            scope_task_id=root["id"],
            provider_task_id=t1["id"],
            consumer_task_ids=[t2["id"]],
        )
        tasks.add_contract(
            self.conn,
            contract_type="schema",
            definition="{ user_id: str }",
            name="UserRecord",
            scope_task_id=root["id"],
            provider_task_id=t1["id"],
            consumer_task_ids=[t3["id"]],
        )

        result = sequence_from_contracts(self.conn, root["id"])

        assert result["call_count"] == 2
        mermaid = result["mermaid"]
        assert "LoginAPI" in mermaid
        assert "UserRecord" in mermaid


# ---------------------------------------------------------------------------
# Top-level dispatcher
# ---------------------------------------------------------------------------


class TestGenerateSequenceDispatch(FlowTestBase):

    def setup_method(self):
        super().setup_method()
        # Also set up a task conn from the same store
        self.conn = self.store._conn

    def test_dispatch_to_flow_mode_by_id(self):
        """generate_sequence dispatches to flow mode when flow_id + store given."""
        nid_a = self._add_func("main_fn", path="main.py")
        nid_b = self._add_func("helper", path="helpers.py")
        flow_id = self._store_flow("main_fn", nid_a, [nid_a, nid_b])

        result = generate_sequence(store=self.store, flow_id=flow_id)

        assert result["status"] == "ok"
        assert result["source"] == "flow"

    def test_dispatch_to_flow_mode_by_name(self):
        """generate_sequence dispatches to flow mode when flow_name + store given."""
        nid_a = self._add_func("run_job", path="runner.py")
        nid_b = self._add_func("execute", path="executor.py")
        self._store_flow("run_job", nid_a, [nid_a, nid_b])

        result = generate_sequence(store=self.store, flow_name="run_job")

        assert result["status"] == "ok"
        assert result["source"] == "flow"

    def test_dispatch_to_contract_mode(self):
        """generate_sequence dispatches to contract mode when task_id + conn given."""
        root = tasks.create_task(self.conn, [{"title": "Root"}])["tasks"][0]
        t1 = tasks.create_task(self.conn, [{"title": "T1"}], parent_id=root["id"])["tasks"][0]
        t2 = tasks.create_task(self.conn, [{"title": "T2"}], parent_id=root["id"])["tasks"][0]

        tasks.add_contract(
            self.conn,
            contract_type="api",
            definition="spec",
            name="SomeAPI",
            scope_task_id=root["id"],
            provider_task_id=t1["id"],
            consumer_task_ids=[t2["id"]],
        )

        result = generate_sequence(store=self.store, conn=self.conn, task_id=root["id"])

        assert result["status"] == "ok"
        assert result["source"] == "contracts"

    def test_dispatch_no_args_returns_error(self):
        """generate_sequence with no useful args returns error status."""
        result = generate_sequence()

        assert result["status"] == "error"
        assert "mermaid" in result

    def test_dispatch_task_id_without_conn(self):
        """task_id without conn falls through to error (no conn to dispatch with)."""
        result = generate_sequence(task_id="some-id")

        assert result["status"] == "error"

    def test_dispatch_flow_id_without_store(self):
        """flow_id without store falls through to error."""
        result = generate_sequence(flow_id=1)

        assert result["status"] == "error"
