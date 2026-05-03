"""Tests for dashboard_ui.py API endpoints and handlers."""

from __future__ import annotations

import json
import tempfile
import threading
from http.client import HTTPConnection
from pathlib import Path
from typing import Any

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph import tasks
from code_review_graph.dashboard_ui import DashboardHandler
from http.server import HTTPServer


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


class _Handler(DashboardHandler):
    """Subclass with suppressed logging."""

    def log_message(self, *args: Any) -> None:
        pass


def _make_db() -> tuple[GraphStore, str]:
    """Create a temp DB and return (store, path)."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    store = GraphStore(tmp.name)
    return store, tmp.name


def _setup_handler(db_path: str, root_task_id: str, root_title: str) -> None:
    """Patch DashboardHandler class attributes for tests."""
    _Handler.db_path = Path(db_path)
    _Handler.repo_root = Path(db_path).parent
    _Handler.root_task_id = root_task_id
    _Handler.root_title = root_title


def _start_server(db_path: str, root_task_id: str, root_title: str = "Test Task") -> tuple[HTTPServer, int]:
    """Start a test server on a random port and return (server, port)."""
    _setup_handler(db_path, root_task_id, root_title)
    server = HTTPServer(("localhost", 0), _Handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, port


def _get(port: int, path: str) -> tuple[int, dict[str, Any]]:
    conn = HTTPConnection("localhost", port, timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = json.loads(resp.read())
    conn.close()
    return resp.status, body


def _post(port: int, path: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload).encode()
    conn = HTTPConnection("localhost", port, timeout=5)
    conn.request("POST", path, body=body, headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    result = json.loads(resp.read())
    conn.close()
    return resp.status, result


def _get_raw(port: int, path: str) -> tuple[int, bytes, str]:
    """Return (status, raw_body, content_type)."""
    conn = HTTPConnection("localhost", port, timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read()
    ct = resp.getheader("Content-Type", "")
    conn.close()
    return resp.status, body, ct


# ---------------------------------------------------------------------------
# Base class with DB + task setup
# ---------------------------------------------------------------------------


class DashboardTestBase:
    def setup_method(self) -> None:
        self.store, self.db_path = _make_db()
        self.conn = self.store._conn
        # Create a root task
        result = tasks.create_task(self.conn, [{"title": "Dashboard Test Root"}])
        self.root_task_id = result["tasks"][0]["id"]
        self.server, self.port = _start_server(self.db_path, self.root_task_id)

    def teardown_method(self) -> None:
        self.server.shutdown()
        self.store.close()
        Path(self.db_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 1. test_api_annotations_empty — no notes → empty annotations
# ---------------------------------------------------------------------------


class TestAnnotationsEmpty(DashboardTestBase):
    def test_api_annotations_empty(self) -> None:
        status, body = _get(self.port, "/api/annotations")
        assert status == 200
        assert "annotations" in body
        assert body["annotations"] == {}


# ---------------------------------------------------------------------------
# 2. test_api_annotations_grouped — notes with c4_element_id → grouped
# ---------------------------------------------------------------------------


class TestAnnotationsGrouped(DashboardTestBase):
    def test_api_annotations_grouped(self) -> None:
        # Create notes — one with c4_element_id, one without
        tasks.add_note(
            self.conn,
            task_id=self.root_task_id,
            notes=[
                {"note_type": "question", "content": "Why so slow?", "c4_element_id": "comm_5"},
                {"note_type": "question", "content": "Retry policy?", "c4_element_id": "comm_5"},
                {"note_type": "assumption", "content": "No c4 id", "c4_element_id": None},
            ],
        )
        self.conn.commit()

        status, body = _get(self.port, "/api/annotations")
        assert status == 200
        ann = body["annotations"]
        assert "comm_5" in ann
        assert len(ann["comm_5"]) == 2
        contents = {n["content"] for n in ann["comm_5"]}
        assert contents == {"Why so slow?", "Retry policy?"}
        # Note without c4_element_id should NOT appear
        assert len(ann) == 1


# ---------------------------------------------------------------------------
# 3. test_api_notes_by_status — notes grouped open/answered/resolved
# ---------------------------------------------------------------------------


class TestNotesByStatus(DashboardTestBase):
    def test_api_notes_by_status(self) -> None:
        tasks.add_note(
            self.conn,
            task_id=self.root_task_id,
            notes=[
                {"note_type": "question", "content": "Open Q", "status": "open"},
                {"note_type": "question", "content": "Answered Q", "status": "answered",
                 "resolution": "42"},
                {"note_type": "assumption", "content": "Resolved A", "status": "resolved"},
            ],
        )
        self.conn.commit()

        status, body = _get(self.port, "/api/notes")
        assert status == 200
        assert len(body["open"]) == 1
        assert body["open"][0]["content"] == "Open Q"
        assert len(body["answered"]) == 1
        assert body["answered"][0]["content"] == "Answered Q"
        assert len(body["resolved"]) == 1
        assert body["resolved"][0]["content"] == "Resolved A"
        assert "task_title" in body


# ---------------------------------------------------------------------------
# 4. test_api_post_annotation — POST creates note with c4_element_id
# ---------------------------------------------------------------------------


class TestPostAnnotation(DashboardTestBase):
    def test_api_post_annotation(self) -> None:
        payload = {
            "c4_element_id": "node_123",
            "content": "This service is too coupled",
            "task_id": self.root_task_id,
        }
        status, body = _post(self.port, "/api/annotations", payload)
        assert status == 200
        assert body["status"] == "ok"
        assert body.get("note_id")  # a note ID was returned

        # Verify persisted
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM notes WHERE c4_element_id = 'node_123'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["content"] == "This service is too coupled"


# ---------------------------------------------------------------------------
# 5. test_api_post_answer — POST updates note status to answered
# ---------------------------------------------------------------------------


class TestPostAnswer(DashboardTestBase):
    def test_api_post_answer(self) -> None:
        result = tasks.add_note(
            self.conn,
            task_id=self.root_task_id,
            notes=[{"note_type": "question", "content": "Redis or Postgres?", "status": "open"}],
        )
        self.conn.commit()
        note_id = result["notes"][0]["id"]

        status, body = _post(self.port, f"/api/notes/{note_id}/answer", {"resolution": "Redis"})
        assert status == 200
        assert body["ok"] is True

        # Verify status updated
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT status, resolution FROM notes WHERE id = ?", (note_id,)).fetchone()
        conn.close()
        assert row["status"] == "answered"
        assert row["resolution"] == "Redis"


# ---------------------------------------------------------------------------
# 6. test_api_roadmap — returns roadmap data
# ---------------------------------------------------------------------------


class TestRoadmap(DashboardTestBase):
    def test_api_roadmap(self) -> None:
        # Add a subtask so roadmap has something to compute
        tasks.create_task(
            self.conn,
            [{"title": "Child Task", "description": "do stuff"}],
            parent_id=self.root_task_id,
        )
        self.conn.commit()

        status, body = _get(self.port, "/api/roadmap")
        assert status == 200
        # Basic structure checks — roadmap returns attention/contracts/notes_summary
        assert isinstance(body, dict)
        assert "attention" in body or "error" in body


# ---------------------------------------------------------------------------
# 7. test_serve_page — GET / returns HTML with correct content-type
# ---------------------------------------------------------------------------


class TestServePage(DashboardTestBase):
    def test_serve_page_content_type(self) -> None:
        status, body, ct = _get_raw(self.port, "/")
        assert status == 200
        assert "text/html" in ct

    def test_serve_page_has_required_content(self) -> None:
        status, body, _ = _get_raw(self.port, "/")
        assert status == 200
        text = body.decode("utf-8")
        assert "Code Review Graph Dashboard" in text
        assert "cytoscape" in text.lower()
        assert "vue" in text.lower()


# ---------------------------------------------------------------------------
# 8. Additional: test_api_contracts_empty & test_api_dag_structure
# ---------------------------------------------------------------------------


class TestContractsEmpty(DashboardTestBase):
    def test_api_contracts_empty(self) -> None:
        status, body = _get(self.port, "/api/contracts")
        assert status == 200
        assert "contracts" in body
        assert body["contracts"] == []


class TestDagStructure(DashboardTestBase):
    def test_api_dag_returns_nodes_and_edges(self) -> None:
        status, body = _get(self.port, "/api/dag")
        assert status == 200
        assert "nodes" in body
        assert "edges" in body
        # Root task should appear as a node
        node_ids = [n["data"]["id"] for n in body["nodes"]]
        assert self.root_task_id in node_ids


class TestExecutionOrder(DashboardTestBase):
    def test_api_execution_order(self) -> None:
        status, body = _get(self.port, "/api/execution-order")
        assert status == 200
        # May have 'levels' or 'error' if no leaf tasks
        assert isinstance(body, dict)


# ---------------------------------------------------------------------------
# 9. No-root-task tests (new)
# ---------------------------------------------------------------------------


def _start_server_no_root(db_path: str) -> tuple[HTTPServer, int]:
    """Start a server with root_task_id=None (no active root task)."""
    _Handler.db_path = Path(db_path)
    _Handler.repo_root = Path(db_path).parent
    _Handler.root_task_id = None  # type: ignore[assignment]
    _Handler.root_title = ""
    server = HTTPServer(("localhost", 0), _Handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, port


class TestNoRootTask:
    """Tests for dashboard behaviour when no root task exists."""

    def setup_method(self) -> None:
        self.store, self.db_path = _make_db()
        self.conn = self.store._conn
        # Do NOT create a root task
        self.server, self.port = _start_server_no_root(self.db_path)

    def teardown_method(self) -> None:
        self.server.shutdown()
        self.store.close()
        Path(self.db_path).unlink(missing_ok=True)

    def test_start_dashboard_no_root_task(self) -> None:
        """Server starts and serves HTML even with no root task."""
        status, body, ct = _get_raw(self.port, "/")
        assert status == 200
        assert "text/html" in ct

    def test_api_root_task_empty(self) -> None:
        """GET /api/root-task returns {task: null} when no root task exists."""
        status, body = _get(self.port, "/api/root-task")
        assert status == 200
        assert body["task"] is None

    def test_api_root_task_create(self) -> None:
        """POST /api/root-task creates a task and returns it."""
        status, body = _post(self.port, "/api/root-task", {
            "title": "New Root",
            "description": "A description",
        })
        assert status == 200
        assert body["task"]["title"] == "New Root"
        assert body["task"]["description"] == "A description"
        assert body["task"]["status"] == "draft"
        task_id = body["task"]["id"]
        assert task_id

        # Subsequent GET should return the created task
        status2, body2 = _get(self.port, "/api/root-task")
        assert status2 == 200
        assert body2["task"]["id"] == task_id

    def test_api_root_task_create_missing_title(self) -> None:
        """POST /api/root-task with no title returns error."""
        status, body = _post(self.port, "/api/root-task", {"description": "no title"})
        assert status == 200
        assert "error" in body

    def test_api_dag_no_root(self) -> None:
        """GET /api/dag returns empty nodes/edges when no root task."""
        status, body = _get(self.port, "/api/dag")
        assert status == 200
        assert body["nodes"] == []
        assert body["edges"] == []

    def test_api_contracts_no_root(self) -> None:
        """GET /api/contracts returns empty list when no root task."""
        status, body = _get(self.port, "/api/contracts")
        assert status == 200
        assert body["contracts"] == []

    def test_api_notes_no_root(self) -> None:
        """GET /api/notes returns empty structure when no root task."""
        status, body = _get(self.port, "/api/notes")
        assert status == 200
        assert body["open"] == []
        assert body["answered"] == []
        assert body["resolved"] == []

    def test_api_roadmap_no_root(self) -> None:
        """GET /api/roadmap returns empty data when no root task."""
        status, body = _get(self.port, "/api/roadmap")
        assert status == 200
        assert isinstance(body, dict)

    def test_api_execution_order_no_root(self) -> None:
        """GET /api/execution-order returns empty when no root task."""
        status, body = _get(self.port, "/api/execution-order")
        assert status == 200
        assert isinstance(body, dict)


class TestRootTaskExisting(DashboardTestBase):
    """Tests for /api/root-task when root task already exists."""

    def test_api_root_task_existing(self) -> None:
        """GET /api/root-task returns task data when one exists."""
        status, body = _get(self.port, "/api/root-task")
        assert status == 200
        assert body["task"] is not None
        assert body["task"]["id"] == self.root_task_id
        assert body["task"]["title"] == "Dashboard Test Root"
        assert "status" in body["task"]

    def test_api_root_task_update(self) -> None:
        """POST /api/root-task updates existing task title and description."""
        status, body = _post(self.port, "/api/root-task", {
            "title": "Updated Title",
            "description": "Updated description",
        })
        assert status == 200
        assert body["task"]["title"] == "Updated Title"
        assert body["task"]["id"] == self.root_task_id

        # Verify persisted
        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(self.db_path)
        conn.row_factory = _sqlite3.Row
        row = conn.execute("SELECT title FROM tasks WHERE id = ?", (self.root_task_id,)).fetchone()
        conn.close()
        assert row["title"] == "Updated Title"


# ---------------------------------------------------------------------------
# 10. test_c4_node_details_endpoint — new endpoint for node details
# ---------------------------------------------------------------------------


class TestC4NodeDetails(DashboardTestBase):
    def test_c4_node_details_empty_id(self) -> None:
        """GET /api/c4/node-details with empty id returns empty data."""
        status, body = _get(self.port, "/api/c4/node-details?id=")
        assert status == 200
        assert "node" in body
        assert "annotations" in body
        assert "annotation_count" in body
        assert body["annotation_count"] == 0
        assert body["annotations"] == []

    def test_c4_node_details_missing_id(self) -> None:
        """GET /api/c4/node-details with no query param returns empty data."""
        status, body = _get(self.port, "/api/c4/node-details")
        assert status == 200
        assert body["annotation_count"] == 0

    def test_c4_node_details_with_annotations(self) -> None:
        """GET /api/c4/node-details returns annotations matching element_id."""
        tasks.add_note(
            self.conn,
            task_id=self.root_task_id,
            notes=[
                {"note_type": "question", "content": "Why slow?", "c4_element_id": "node_abc"},
                {"note_type": "assumption", "content": "Other node", "c4_element_id": "node_xyz"},
                {"note_type": "question", "content": "Second note", "c4_element_id": "node_abc"},
            ],
        )
        self.conn.commit()

        status, body = _get(self.port, "/api/c4/node-details?id=node_abc")
        assert status == 200
        assert body["annotation_count"] == 2
        assert len(body["annotations"]) == 2
        contents = {n["content"] for n in body["annotations"]}
        assert "Why slow?" in contents
        assert "Second note" in contents

    def test_c4_node_details_no_match(self) -> None:
        """GET /api/c4/node-details for nonexistent element returns empty."""
        status, body = _get(self.port, "/api/c4/node-details?id=nonexistent_id_xyz")
        assert status == 200
        assert body["annotation_count"] == 0
        assert body["annotations"] == []

    def test_c4_node_details_node_field_present(self) -> None:
        """GET /api/c4/node-details always includes a 'node' field with the id."""
        status, body = _get(self.port, "/api/c4/node-details?id=some_node")
        assert status == 200
        assert "node" in body
        # node should have the id if no C4 file found
        assert body["node"].get("id") == "some_node" or "id" in body["node"]


# ---------------------------------------------------------------------------
# 11. test_breadcrumb_state_management — HTML contains breadcrumb elements
# ---------------------------------------------------------------------------


class TestBreadcrumbAndDetailPanel(DashboardTestBase):
    def test_breadcrumb_html_elements_present(self) -> None:
        """Dashboard HTML contains breadcrumb class and navigateTo function."""
        status, body, _ = _get_raw(self.port, "/")
        assert status == 200
        text = body.decode("utf-8")
        assert "breadcrumb" in text
        assert "navigateTo" in text

    def test_detail_panel_html_present(self) -> None:
        """Dashboard HTML contains detail-panel class and selectedNode ref."""
        status, body, _ = _get_raw(self.port, "/")
        assert status == 200
        text = body.decode("utf-8")
        assert "detail-panel" in text
        assert "selectedNode" in text

    def test_legend_html_present(self) -> None:
        """Dashboard HTML contains C4 Zoom Navigator elements."""
        status, body, _ = _get_raw(self.port, "/")
        assert status == 200
        text = body.decode("utf-8")
        assert "legend" in text
        # C4 Zoom Navigator replaced old Existing/Modified/New legend
        assert "zoomLevel" in text
        assert "zoomBreadcrumb" in text

    def test_submit_annotation_function_present(self) -> None:
        """Dashboard HTML contains submitAnnotation function."""
        status, body, _ = _get_raw(self.port, "/")
        assert status == 200
        text = body.decode("utf-8")
        assert "submitAnnotation" in text
        assert "newAnnotation" in text

    def test_node_badge_class_function_present(self) -> None:
        """Dashboard HTML contains nodeBadgeClass function."""
        status, body, _ = _get_raw(self.port, "/")
        assert status == 200
        text = body.decode("utf-8")
        assert "nodeBadgeClass" in text

    def test_arch_layout_css_present(self) -> None:
        """Dashboard HTML contains arch-layout CSS class."""
        status, body, _ = _get_raw(self.port, "/")
        assert status == 200
        text = body.decode("utf-8")
        assert "arch-layout" in text
        assert "arch-graph-wrap" in text


# ---------------------------------------------------------------------------
# New tests: annotation popup system
# ---------------------------------------------------------------------------


class TestAnnotationPopupHtmlElements:
    """Verify the _PAGE_HTML contains all annotation popup UI elements."""

    def test_annotation_popup_html_elements(self) -> None:
        from code_review_graph.dashboard_ui import _PAGE_HTML

        html = _PAGE_HTML.read_text(encoding="utf-8")

        # Panel div
        assert 'id="annotation-panel"' in html
        # CSS classes
        assert "anno-header" in html
        assert "anno-item" in html
        assert "anno-new" in html
        assert "anno-empty" in html
        assert "anno-resolution" in html
        # Colored borders by type
        assert "anno-item.question" in html
        assert "anno-item.decision" in html
        assert "anno-item.risk" in html
        # JS interaction functions
        assert "showAnnotationPanel" in html
        assert "closeAnnotationPanel" in html
        assert "submitAnnotation" in html
        # Cytoscape right-click binding
        assert "cxttap" in html
        # Double-tap binding for mobile
        assert "dbltap" in html


class TestPostAnnotationCreatesNote(DashboardTestBase):
    """POST /api/annotations creates a note, GET /api/annotations returns it."""

    def test_post_annotation_creates_note(self) -> None:
        payload = {
            "c4_element_id": "comm_api",
            "content": "This component needs caching",
        }
        status, body = _post(self.port, "/api/annotations", payload)
        assert status == 200
        assert body["status"] == "ok"
        assert body.get("note_id")

        # Verify via GET annotations endpoint
        status2, body2 = _get(self.port, "/api/annotations")
        assert status2 == 200
        assert "comm_api" in body2["annotations"]
        notes_list = body2["annotations"]["comm_api"]
        assert any(n["content"] == "This component needs caching" for n in notes_list)


class TestGetAnnotationsGroupsByElement(DashboardTestBase):
    """GET /api/annotations groups notes by c4_element_id, excludes notes without one."""

    def test_get_annotations_groups_by_element(self) -> None:
        tasks.add_note(
            self.conn,
            task_id=self.root_task_id,
            notes=[
                {"note_type": "risk", "content": "Single point of failure", "c4_element_id": "db_node"},
                {"note_type": "decision", "content": "Use connection pool", "c4_element_id": "db_node"},
                {"note_type": "question", "content": "Unrelated question"},  # no c4_element_id
            ],
        )
        self.conn.commit()

        status, body = _get(self.port, "/api/annotations")
        assert status == 200
        ann = body["annotations"]
        assert "db_node" in ann
        assert len(ann["db_node"]) == 2
        note_types = {n["note_type"] for n in ann["db_node"]}
        assert "risk" in note_types
        assert "decision" in note_types
        # Note without c4_element_id must not appear
        assert len(ann) == 1


# ---------------------------------------------------------------------------
# 10. test_contracts_endpoint_enriched — /api/contracts returns providers/consumers
# ---------------------------------------------------------------------------


class TestContractsEnriched(DashboardTestBase):
    def test_contracts_endpoint_enriched(self) -> None:
        """GET /api/contracts returns providers and consumers arrays with task info."""
        # Create provider and consumer tasks
        provider_result = tasks.create_task(
            self.conn,
            [{"title": "Provider Task", "description": "provides something"}],
            parent_id=self.root_task_id,
        )
        consumer_result = tasks.create_task(
            self.conn,
            [{"title": "Consumer Task", "description": "consumes something"}],
            parent_id=self.root_task_id,
        )
        self.conn.commit()
        provider_id = provider_result["tasks"][0]["id"]
        consumer_id = consumer_result["tasks"][0]["id"]

        # Create a contract linked to provider and consumer
        contract_result = tasks.add_contract(
            self.conn,
            name="TestInterface",
            contract_type="interface",
            definition="{ foo: str }",
            scope_task_id=self.root_task_id,
            provider_task_id=provider_id,
            consumer_task_ids=[consumer_id],
        )
        self.conn.commit()

        status, body = _get(self.port, "/api/contracts")
        assert status == 200
        contracts_list = body["contracts"]
        assert len(contracts_list) == 1
        c = contracts_list[0]

        # providers array
        assert "providers" in c
        assert isinstance(c["providers"], list)
        assert len(c["providers"]) == 1
        assert c["providers"][0]["title"] == "Provider Task"
        assert c["providers"][0]["id"] == provider_id
        assert "status" in c["providers"][0]

        # consumers array
        assert "consumers" in c
        assert isinstance(c["consumers"], list)
        assert len(c["consumers"]) == 1
        assert c["consumers"][0]["title"] == "Consumer Task"
        assert c["consumers"][0]["id"] == consumer_id
        assert "status" in c["consumers"][0]


# ---------------------------------------------------------------------------
# 11. test_contracts_graph_html — _PAGE_HTML contains cy-contracts div
# ---------------------------------------------------------------------------


class TestContractsGraphHtml(DashboardTestBase):
    def test_contracts_graph_html(self) -> None:
        """_PAGE_HTML should contain the cy-contracts div for the dependency graph."""
        from code_review_graph.dashboard_ui import _PAGE_HTML
        html = _PAGE_HTML.read_text(encoding="utf-8")
        assert 'id="cy-contracts"' in html
        assert "buildCyContracts" in html


# ---------------------------------------------------------------------------
# 10. Data Flow tab tests
# ---------------------------------------------------------------------------


class TestDataflowEndpoints(DashboardTestBase):
    """Tests for /api/dataflow/sources and /api/dataflow endpoints."""

    def test_dataflow_sources_endpoint(self) -> None:
        """GET /api/dataflow/sources returns valid JSON with a sources array."""
        status, body = _get(self.port, "/api/dataflow/sources")
        assert status == 200
        assert "sources" in body
        assert isinstance(body["sources"], list)

    def test_dataflow_sources_returns_code_nodes(self) -> None:
        """Nodes inserted into the graph appear in /api/dataflow/sources."""
        import sqlite3
        import time

        # Insert a Function node directly into the nodes table
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO nodes (kind, name, qualified_name, file_path, updated_at)"
            " VALUES ('Function', 'my_func', 'mymod::my_func', 'mymod.py', ?)",
            (time.time(),),
        )
        conn.commit()
        conn.close()

        status, body = _get(self.port, "/api/dataflow/sources")
        assert status == 200
        qns = [s["qualified_name"] for s in body["sources"]]
        assert "mymod::my_func" in qns

    def test_dataflow_endpoint_missing_source(self) -> None:
        """GET /api/dataflow without source param returns error key."""
        status, body = _get(self.port, "/api/dataflow")
        assert status == 200
        assert "error" in body
        assert body["nodes"] == []
        assert body["edges"] == []

    def test_dataflow_endpoint_with_source(self) -> None:
        """BFS from a source returns reachable nodes and edges."""
        import sqlite3
        import time
        import urllib.parse

        ts = time.time()
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        # Insert two Function nodes
        conn.execute(
            "INSERT INTO nodes (kind, name, qualified_name, file_path, updated_at)"
            " VALUES ('Function', 'entry_fn', 'mod::entry_fn', 'mod.py', ?)",
            (ts,),
        )
        conn.execute(
            "INSERT INTO nodes (kind, name, qualified_name, file_path, updated_at)"
            " VALUES ('Function', 'helper_fn', 'mod::helper_fn', 'mod.py', ?)",
            (ts,),
        )
        # Insert a CALLS edge
        conn.execute(
            "INSERT INTO edges (kind, source_qualified, target_qualified, file_path, updated_at)"
            " VALUES ('CALLS', 'mod::entry_fn', 'mod::helper_fn', 'mod.py', ?)",
            (ts,),
        )
        conn.commit()
        conn.close()

        path = "/api/dataflow?source=" + urllib.parse.quote("mod::entry_fn") + "&depth=2"
        status, body = _get(self.port, path)
        assert status == 200
        assert "nodes" in body
        assert "edges" in body
        node_ids = [n["id"] for n in body["nodes"]]
        assert "mod::entry_fn" in node_ids
        assert "mod::helper_fn" in node_ids
        assert len(body["edges"]) >= 1
        edge = body["edges"][0]
        assert edge["source"] == "mod::entry_fn"
        assert edge["target"] == "mod::helper_fn"
        assert edge["type"] == "calls"

    def test_dataflow_endpoint_unknown_source(self) -> None:
        """BFS with source that doesn't exist returns empty nodes/edges."""
        import urllib.parse

        path = "/api/dataflow?source=" + urllib.parse.quote("nonexistent::fn") + "&depth=3"
        status, body = _get(self.port, path)
        assert status == 200
        assert body["nodes"] == []
        assert body["edges"] == []

class TestDataflowTabHtml(DashboardTestBase):
    """Verify the _PAGE_HTML contains Data Flow tab elements."""

    def test_dataflow_tab_html(self) -> None:
        """GET / returns HTML that includes dataflow tab markup."""
        from code_review_graph.dashboard_ui import _PAGE_HTML

        html = _PAGE_HTML.read_text(encoding="utf-8")
        assert "dataflow" in html
        assert "Data Flow" in html
        assert "cy-dataflow" in html
        assert "dfSource" in html
        assert "dfSources" in html
        assert "/api/dataflow/sources" in html
        assert "/api/dataflow" in html


# ---------------------------------------------------------------------------
# 12. test_c4_task_refs endpoint
# ---------------------------------------------------------------------------


class TestC4TaskRefs(DashboardTestBase):
    """Tests for GET /api/c4/task-refs?id=XXXX endpoint."""

    def test_c4_task_refs_empty(self) -> None:
        """Returns empty tasks/contracts when no notes have this c4_element_id."""
        status, body = _get(self.port, "/api/c4/task-refs?id=nonexistent_node")
        assert status == 200
        assert "tasks" in body
        assert "contracts" in body
        assert body["tasks"] == []
        # contracts may be non-empty (all root-scope contracts returned)
        assert isinstance(body["contracts"], list)

    def test_c4_task_refs_returns_tasks_from_notes(self) -> None:
        """Tasks referenced in notes with c4_element_id=X appear in /api/c4/task-refs?id=X."""
        child = tasks.create_task(
            self.conn,
            [{"title": "Auth Service Task"}],
            parent_id=self.root_task_id,
        )
        self.conn.commit()
        child_id = child["tasks"][0]["id"]

        tasks.add_note(
            self.conn,
            task_id=child_id,
            notes=[{"note_type": "question", "content": "Slow?", "c4_element_id": "auth_service"}],
        )
        self.conn.commit()

        status, body = _get(self.port, "/api/c4/task-refs?id=auth_service")
        assert status == 200
        task_ids = [t["id"] for t in body["tasks"]]
        assert child_id in task_ids
        # Verify title and status present
        matching = [t for t in body["tasks"] if t["id"] == child_id]
        assert matching[0]["title"] == "Auth Service Task"
        assert "status" in matching[0]

    def test_c4_task_refs_includes_contracts(self) -> None:
        """Contracts in scope always appear in /api/c4/task-refs response."""
        provider_result = tasks.create_task(
            self.conn,
            [{"title": "Contract Provider"}],
            parent_id=self.root_task_id,
        )
        self.conn.commit()
        provider_id = provider_result["tasks"][0]["id"]

        tasks.add_contract(
            self.conn,
            name="MyInterface",
            contract_type="interface",
            definition="{ x: str }",
            scope_task_id=self.root_task_id,
            provider_task_id=provider_id,
            consumer_task_ids=[],
        )
        self.conn.commit()

        status, body = _get(self.port, "/api/c4/task-refs?id=some_node")
        assert status == 200
        assert "contracts" in body
        assert any(c["name"] == "MyInterface" for c in body["contracts"])

    def test_c4_task_refs_no_root(self) -> None:
        """Returns empty data when no root task set."""
        # Use a server with no root task
        store2, db_path2 = _make_db()
        conn2 = store2._conn
        tasks.create_task(conn2, [{"title": "orphan"}])
        conn2.commit()
        server2, port2 = _start_server_no_root(db_path2)
        try:
            status, body = _get(port2, "/api/c4/task-refs?id=node_xyz")
            assert status == 200
            assert body["tasks"] == []
            assert body["contracts"] == []
        finally:
            server2.shutdown()
            store2.close()
            import os
            try:
                os.unlink(db_path2)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# 13. test_notes_tab_html — answer input + filter buttons present
# ---------------------------------------------------------------------------


class TestNotesTabHtmlEnhancements:
    """Verify index.html contains Notes tab answer inputs and filter buttons."""

    def test_notes_tab_has_answer_button(self) -> None:
        from code_review_graph.dashboard_ui import _PAGE_HTML
        html = _PAGE_HTML.read_text(encoding="utf-8")
        # Answer button present
        assert "answerNote" in html
        # Submit answer text
        assert "Submit answer" in html

    def test_notes_tab_has_filter_buttons(self) -> None:
        from code_review_graph.dashboard_ui import _PAGE_HTML
        html = _PAGE_HTML.read_text(encoding="utf-8")
        # Type filter buttons
        assert "notesFilter" in html
        assert "filteredNotes" in html
        # Filter option labels
        assert "Questions" in html
        assert "Decisions" in html
        assert "Constraints" in html

    def test_notes_tab_has_answer_textarea(self) -> None:
        from code_review_graph.dashboard_ui import _PAGE_HTML
        html = _PAGE_HTML.read_text(encoding="utf-8")
        # noteAnswers bound textarea
        assert "noteAnswers" in html


# ---------------------------------------------------------------------------
# 14. test_arch_detail_panel_html — task-refs and contracts sections present
# ---------------------------------------------------------------------------


class TestArchDetailPanelHtml:
    """Verify index.html Architecture detail panel has task-refs and contracts sections."""

    def test_arch_detail_panel_task_refs_section(self) -> None:
        from code_review_graph.dashboard_ui import _PAGE_HTML
        html = _PAGE_HTML.read_text(encoding="utf-8")
        # nodeTaskRefs present in HTML
        assert "nodeTaskRefs" in html
        # API endpoint called
        assert "/api/c4/task-refs" in html

    def test_arch_detail_panel_contracts_section(self) -> None:
        from code_review_graph.dashboard_ui import _PAGE_HTML
        html = _PAGE_HTML.read_text(encoding="utf-8")
        # Contracts section present in detail panel
        assert "nodeTaskRefs.contracts" in html
        assert "contractBadgeClass" in html

    def test_annotation_escaping_uses_data_nodeid(self) -> None:
        """Verify the annotation panel uses data-nodeid (not broken onclick)."""
        from code_review_graph.dashboard_ui import _PAGE_HTML
        html = _PAGE_HTML.read_text(encoding="utf-8")
        # data-nodeid approach used
        assert "data-nodeid" in html
        # anno-submit-btn class for event delegation
        assert "anno-submit-btn" in html
        # No broken inline onclick with escaped quotes
        assert "window._crg_submitAnnotation(\\" not in html


# ---------------------------------------------------------------------------
# NEW: TestRoadmapTabHtml — roadmapData.progress, phases, notes_summary in HTML
# ---------------------------------------------------------------------------


class TestRoadmapTabHtml:
    """Verify index.html Roadmap tab uses correct field paths."""

    def test_roadmap_uses_progress_field(self) -> None:
        from code_review_graph.dashboard_ui import _PAGE_HTML
        html = _PAGE_HTML.read_text(encoding="utf-8")
        # Uses roadmapData.progress (not roadmapData.done directly)
        assert "roadmapData.progress" in html

    def test_roadmap_has_phases_section(self) -> None:
        from code_review_graph.dashboard_ui import _PAGE_HTML
        html = _PAGE_HTML.read_text(encoding="utf-8")
        # Renders phases array
        assert "roadmapData.phases" in html
        assert "phase.tasks" in html

    def test_roadmap_has_notes_summary(self) -> None:
        from code_review_graph.dashboard_ui import _PAGE_HTML
        html = _PAGE_HTML.read_text(encoding="utf-8")
        # notes_summary section present
        assert "notes_summary" in html
        assert "open_questions" in html

    def test_roadmap_has_contracts_summary(self) -> None:
        from code_review_graph.dashboard_ui import _PAGE_HTML
        html = _PAGE_HTML.read_text(encoding="utf-8")
        assert "roadmapData.contracts" in html
        assert "contracts.agreed" in html or "roadmapData.contracts.agreed" in html

    def test_roadmap_attention_uses_flexible_fields(self) -> None:
        from code_review_graph.dashboard_ui import _PAGE_HTML
        html = _PAGE_HTML.read_text(encoding="utf-8")
        # Supports both t.title and t.task_title for ready_to_start items
        assert "t.title || t.task_title" in html or "task_title" in html


# ---------------------------------------------------------------------------
# NEW: TestDagDetailPanel — selectedDagTask and /api/dag/task-detail in HTML
# ---------------------------------------------------------------------------


class TestDagDetailPanel:
    """Verify index.html Task DAG tab has a detail panel."""

    def test_dag_task_detail_api_in_html(self) -> None:
        from code_review_graph.dashboard_ui import _PAGE_HTML
        html = _PAGE_HTML.read_text(encoding="utf-8")
        assert "/api/dag/task-detail" in html

    def test_selected_dag_task_in_html(self) -> None:
        from code_review_graph.dashboard_ui import _PAGE_HTML
        html = _PAGE_HTML.read_text(encoding="utf-8")
        assert "selectedDagTask" in html

    def test_dag_filter_buttons_in_html(self) -> None:
        from code_review_graph.dashboard_ui import _PAGE_HTML
        html = _PAGE_HTML.read_text(encoding="utf-8")
        assert "dagFilter" in html

    def test_dag_submit_annotation_in_html(self) -> None:
        from code_review_graph.dashboard_ui import _PAGE_HTML
        html = _PAGE_HTML.read_text(encoding="utf-8")
        assert "submitDagAnnotation" in html
        assert "newDagAnnotation" in html

    def test_dag_detail_shows_code_refs(self) -> None:
        from code_review_graph.dashboard_ui import _PAGE_HTML
        html = _PAGE_HTML.read_text(encoding="utf-8")
        assert "code_refs" in html

    def test_dag_detail_shows_edges(self) -> None:
        from code_review_graph.dashboard_ui import _PAGE_HTML
        html = _PAGE_HTML.read_text(encoding="utf-8")
        # Both incoming and outgoing edge display
        assert "direction" in html or "outgoing" in html


# ---------------------------------------------------------------------------
# NEW: TestContractsTabFilters — contractsFilter and submitContractComment in HTML
# ---------------------------------------------------------------------------


class TestContractsTabFilters:
    """Verify index.html Contracts tab has filter buttons and comment box."""

    def test_contracts_filter_in_html(self) -> None:
        from code_review_graph.dashboard_ui import _PAGE_HTML
        html = _PAGE_HTML.read_text(encoding="utf-8")
        assert "contractsFilter" in html

    def test_contracts_submit_comment_in_html(self) -> None:
        from code_review_graph.dashboard_ui import _PAGE_HTML
        html = _PAGE_HTML.read_text(encoding="utf-8")
        assert "submitContractComment" in html

    def test_contracts_type_filter_options(self) -> None:
        from code_review_graph.dashboard_ui import _PAGE_HTML
        html = _PAGE_HTML.read_text(encoding="utf-8")
        # Type filter options present
        assert "Schemas" in html
        assert "Interfaces" in html
        assert "APIs" in html

    def test_contracts_status_filter_options(self) -> None:
        from code_review_graph.dashboard_ui import _PAGE_HTML
        html = _PAGE_HTML.read_text(encoding="utf-8")
        # Status filter options present
        assert "Proposed" in html
        assert "Agreed" in html

    def test_contracts_comment_textarea_in_html(self) -> None:
        from code_review_graph.dashboard_ui import _PAGE_HTML
        html = _PAGE_HTML.read_text(encoding="utf-8")
        assert "contractComments" in html
        assert "Pin comment" in html

    def test_filtered_contracts_function_in_html(self) -> None:
        from code_review_graph.dashboard_ui import _PAGE_HTML
        html = _PAGE_HTML.read_text(encoding="utf-8")
        assert "filteredContracts" in html


# ---------------------------------------------------------------------------
# NEW: TestDagTaskDetailEndpoint — GET /api/dag/task-detail returns correct fields
# ---------------------------------------------------------------------------


class TestDagTaskDetailEndpoint(DashboardTestBase):
    """Tests for GET /api/dag/task-detail?id=X endpoint."""

    def test_dag_task_detail_returns_task_fields(self) -> None:
        """GET /api/dag/task-detail returns task, notes, edges, code_refs."""
        status, body = _get(self.port, f"/api/dag/task-detail?id={self.root_task_id}")
        assert status == 200
        assert "task" in body
        assert body["task"]["id"] == self.root_task_id
        assert body["task"]["title"] == "Dashboard Test Root"
        assert "notes" in body
        assert "edges" in body
        assert "code_refs" in body

    def test_dag_task_detail_notes_included(self) -> None:
        """Notes for a task are included in task-detail response."""
        tasks.add_note(
            self.conn,
            task_id=self.root_task_id,
            notes=[{"note_type": "question", "content": "Detail question?", "status": "open"}],
        )
        self.conn.commit()

        status, body = _get(self.port, f"/api/dag/task-detail?id={self.root_task_id}")
        assert status == 200
        note_contents = [n["content"] for n in body.get("notes", [])]
        assert "Detail question?" in note_contents

    def test_dag_task_detail_invalid_id(self) -> None:
        """GET /api/dag/task-detail with unknown id returns empty or error."""
        status, body = _get(self.port, "/api/dag/task-detail?id=nonexistent_task_id_xyz")
        assert status == 200
        # Either returns empty task or an error key — should not crash
        assert isinstance(body, dict)

    def test_dag_task_detail_missing_id(self) -> None:
        """GET /api/dag/task-detail with no id param returns error or empty."""
        status, body = _get(self.port, "/api/dag/task-detail")
        assert status == 200
        assert isinstance(body, dict)


# ---------------------------------------------------------------------------
# BugFix tests
# ---------------------------------------------------------------------------


class TestBuildCyDagInReturnBlock:
    """buildCyDag must be exported so DAG filter buttons don't crash."""

    def test_buildcydag_in_vue_return_block(self) -> None:
        from code_review_graph.dashboard_ui import _PAGE_HTML
        html = _PAGE_HTML.read_text(encoding="utf-8")
        # The return block must export buildCyDag so template can call it
        assert "buildCyDag" in html
        # Specifically must be in the return { ... } section (use 1500 chars to cover full block)
        ret_idx = html.rfind("return {")
        assert ret_idx > 0
        ret_block = html[ret_idx : ret_idx + 1500]
        assert "buildCyDag" in ret_block


class TestContractCommentBugFix(DashboardTestBase):
    """Contract comment must create note with question/answered, linked to contract's scope task."""

    def test_contract_comment_correct_type_and_status(self) -> None:
        # Create a contract linked to the root task (add_contract returns flat dict)
        contract = tasks.add_contract(
            self.conn,
            scope_task_id=self.root_task_id,
            name="OAuthToken",
            contract_type="schema",
            definition="{ access_token: str }",
        )
        self.conn.commit()
        contract_id = contract["id"]

        status, body = _post(self.port, f"/api/contracts/{contract_id}/comment", {
            "content": "This looks good"
        })
        assert status == 200
        assert body.get("status") == "ok"
        note_id = body.get("note_id")
        assert note_id

        # Verify note in DB has correct type/status
        note_rows = tasks.list_notes(self.conn, task_id=self.root_task_id, include_children=False)
        note = next((n for n in note_rows if n["id"] == note_id), None)
        assert note is not None
        assert note["note_type"] == "question"     # not "decision"
        assert note["status"] == "answered"         # not "open"
        assert note["resolution"] == "This looks good"
        assert note["content"] == "OAuthToken"      # contract name as source title
        assert note.get("c4_element_id") == f"contract_{contract_id}"

    def test_contract_comment_uses_contract_scope_task(self) -> None:
        """Note must be linked to the contract's scope_task_id, not root_task."""
        # Create a sub-task and a contract scoped to it
        sub = tasks.create_task(self.conn, [{"title": "Sub Task"}], parent_id=self.root_task_id)
        sub_id = sub["tasks"][0]["id"]
        contract = tasks.add_contract(
            self.conn,
            scope_task_id=sub_id,
            name="SubContract",
            contract_type="interface",
            definition="interface X {}",
        )
        self.conn.commit()
        contract_id = contract["id"]

        status, body = _post(self.port, f"/api/contracts/{contract_id}/comment", {
            "content": "Interface comment"
        })
        assert status == 200
        note_id = body.get("note_id")
        # Note must be on sub_id, not root_task_id
        note_rows = tasks.list_notes(self.conn, task_id=sub_id, include_children=False)
        note_ids = [n["id"] for n in note_rows]
        assert note_id in note_ids

    def test_contract_comment_unknown_contract_returns_404(self) -> None:
        # Use raw HTTP to handle non-JSON 404 response body
        conn = HTTPConnection("localhost", self.port, timeout=5)
        body_bytes = json.dumps({"content": "test"}).encode()
        conn.request(
            "POST", "/api/contracts/nonexistent_xyz/comment",
            body=body_bytes,
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        status = resp.status
        conn.close()
        assert status == 404


class TestAnnotationModelBugFix(DashboardTestBase):
    """Annotation POST must accept resolution separately from content (source title)."""

    def test_annotation_creates_with_resolution(self) -> None:
        status, body = _post(self.port, "/api/annotations", {
            "task_id": self.root_task_id,
            "content": "Auth Module",          # source title
            "resolution": "Needs review",      # user text
        })
        assert status == 200
        note_id = body.get("note_id")
        assert note_id
        note_rows = tasks.list_notes(self.conn, task_id=self.root_task_id, include_children=False)
        note = next((n for n in note_rows if n["id"] == note_id), None)
        assert note is not None
        assert note["content"] == "Auth Module"
        assert note["resolution"] == "Needs review"
        assert note["status"] == "answered"
        assert note["note_type"] == "question"

    def test_annotation_without_resolution_still_works(self) -> None:
        """Old callers that only send content still work (backward compat)."""
        status, body = _post(self.port, "/api/annotations", {
            "task_id": self.root_task_id,
            "content": "Some node",
        })
        assert status == 200
        assert body.get("note_id")


class TestArchAnnotationsHtml:
    """Architecture tab annotations must show type icons and resolution with edit button."""

    def test_arch_annotations_show_resolution(self) -> None:
        from code_review_graph.dashboard_ui import _PAGE_HTML
        html = _PAGE_HTML.read_text(encoding="utf-8")
        # nodeAnnotations loop must reference resolution
        assert "a.resolution" in html

    def test_arch_annotations_show_edit_button(self) -> None:
        from code_review_graph.dashboard_ui import _PAGE_HTML
        html = _PAGE_HTML.read_text(encoding="utf-8")
        # Edit button must reference startNoteEdit for arch annotations
        assert "startNoteEdit(a.id" in html

    def test_arch_annotations_show_type_icons(self) -> None:
        from code_review_graph.dashboard_ui import _PAGE_HTML
        html = _PAGE_HTML.read_text(encoding="utf-8")
        # Must use note_type to choose icon for arch annotations
        assert "a.note_type" in html


class TestDagNotesHtml:
    """DAG detail panel notes must show resolution and edit button."""

    def test_dag_notes_show_resolution(self) -> None:
        from code_review_graph.dashboard_ui import _PAGE_HTML
        html = _PAGE_HTML.read_text(encoding="utf-8")
        assert "n.resolution" in html

    def test_dag_notes_show_edit_button(self) -> None:
        from code_review_graph.dashboard_ui import _PAGE_HTML
        html = _PAGE_HTML.read_text(encoding="utf-8")
        assert "startNoteEdit(n.id" in html


class TestQuestionsUiRemoved:
    """questions_ui.py must be deleted — it is no longer used."""

    def test_questions_ui_file_deleted(self) -> None:
        import os
        assert not os.path.exists(
            "code_review_graph/questions_ui.py"
        ), "questions_ui.py should have been deleted"

    def test_questions_ui_not_imported_in_dashboard(self) -> None:
        content = Path("code_review_graph/dashboard_ui.py").read_text(encoding="utf-8")
        assert "import questions_ui" not in content
        assert "from code_review_graph.questions_ui" not in content


# ---------------------------------------------------------------------------
# New feature tests — annotation counts, edge titles, contract annotations
# ---------------------------------------------------------------------------


class TestDagAnnotationCount(DashboardTestBase):
    """GET /api/dag returns annotationCount in each node's data."""

    def test_nodes_have_annotation_count_zero(self) -> None:
        status, body = _get(self.port, "/api/dag")
        assert status == 200
        nodes = body.get("nodes", [])
        assert len(nodes) > 0
        for n in nodes:
            assert "annotationCount" in n["data"]
            assert n["data"]["annotationCount"] == 0

    def test_nodes_annotation_count_reflects_notes(self) -> None:
        # Add a note to the root task
        tasks.add_note(
            self.conn,
            task_id=self.root_task_id,
            notes=[{"note_type": "question", "content": "Why?", "status": "open"}],
        )
        self.conn.commit()
        status, body = _get(self.port, "/api/dag")
        assert status == 200
        root_node = next((n for n in body["nodes"] if n["data"]["id"] == self.root_task_id), None)
        assert root_node is not None
        assert root_node["data"]["annotationCount"] == 1


class TestDagEdgeTitles(DashboardTestBase):
    """GET /api/dag/task-detail returns source_title/target_title in edges."""

    def test_edges_include_titles(self) -> None:
        # Create two tasks with a depends_on edge
        child_res = tasks.create_task(
            self.conn, [{"title": "Child Task Alpha"}], parent_id=self.root_task_id
        )
        child_id = child_res["tasks"][0]["id"]
        tasks.add_task_edge(
            self.conn,
            edges=[{"source_id": child_id, "target_id": self.root_task_id}],
            edge_type="depends_on",
        )
        self.conn.commit()

        status, body = _get(self.port, f"/api/dag/task-detail?id={child_id}")
        assert status == 200
        edges = body.get("edges", [])
        assert len(edges) > 0
        for e in edges:
            assert "source_title" in e
            assert "target_title" in e
        # The outgoing depends_on edge target should show root task title
        outgoing = next((e for e in edges if e["direction"] == "outgoing"), None)
        assert outgoing is not None
        assert outgoing["target_title"] == "Dashboard Test Root"


class TestContractAnnotations(DashboardTestBase):
    """GET /api/contracts returns annotations array per contract."""

    def test_contracts_have_empty_annotations(self) -> None:
        # Create a contract with no annotations
        tasks.add_contract(
            self.conn,
            scope_task_id=self.root_task_id,
            name="TestContract",
            contract_type="schema",
            definition="{}",
        )
        self.conn.commit()
        status, body = _get(self.port, "/api/contracts")
        assert status == 200
        contracts = body.get("contracts", [])
        assert len(contracts) > 0
        for c in contracts:
            assert "annotations" in c
            assert isinstance(c["annotations"], list)

    def test_contracts_annotations_populated(self) -> None:
        # Create a contract and link a note to it
        contract = tasks.add_contract(
            self.conn,
            scope_task_id=self.root_task_id,
            name="AnnotatedContract",
            contract_type="api",
            definition="GET /foo",
        )
        self.conn.commit()
        contract_id = contract["id"]
        tasks.add_note(
            self.conn,
            task_id=self.root_task_id,
            notes=[{
                "note_type": "question",
                "content": "AnnotatedContract",
                "status": "answered",
                "resolution": "Looks good",
                "c4_element_id": f"contract_{contract_id}",
            }],
        )
        self.conn.commit()

        status, body = _get(self.port, "/api/contracts")
        assert status == 200
        contracts = body.get("contracts", [])
        target = next((c for c in contracts if c["id"] == contract_id), None)
        assert target is not None
        assert len(target["annotations"]) == 1
        ann = target["annotations"][0]
        assert ann["content"] == "AnnotatedContract"
        assert ann["resolution"] == "Looks good"


class TestDagFilterHtml:
    """buildCyDag must implement status filtering."""

    def test_filter_logic_in_html(self) -> None:
        from code_review_graph.dashboard_ui import _PAGE_HTML
        html = _PAGE_HTML.read_text(encoding="utf-8")
        # Filter logic keywords must be present
        assert "visibleNodeIds" in html
        assert "dagFilter.value" in html
        assert "filteredNodes" in html
        assert "filteredEdges" in html


# ---------------------------------------------------------------------------
# /api/timeline endpoint tests
# ---------------------------------------------------------------------------


class TestTimelineEndpointShape(DashboardTestBase):
    """GET /api/timeline returns the required top-level keys."""

    def test_timeline_response_has_required_keys(self) -> None:
        status, body = _get(self.port, "/api/timeline")
        assert status == 200
        assert "levels" in body
        assert "dependencies" in body
        assert "critical_path" in body
        assert "parallelism" in body

    def test_timeline_levels_and_parallelism_are_lists(self) -> None:
        status, body = _get(self.port, "/api/timeline")
        assert status == 200
        assert isinstance(body["levels"], list)
        assert isinstance(body["dependencies"], list)
        assert isinstance(body["critical_path"], list)
        assert isinstance(body["parallelism"], list)


class TestTimelineEndpointWithEdges(DashboardTestBase):
    """Dependency edges appear in /api/timeline dependencies array."""

    def test_depends_on_edges_in_dependencies(self) -> None:
        # Create two leaf tasks: B depends_on A
        t1 = tasks.create_task(self.conn, [{"title": "Task A"}], parent_id=self.root_task_id)
        t2 = tasks.create_task(self.conn, [{"title": "Task B"}], parent_id=self.root_task_id)
        self.conn.commit()
        t1_id = t1["tasks"][0]["id"]
        t2_id = t2["tasks"][0]["id"]

        tasks.add_task_edge(
            self.conn,
            edges=[{"source_id": t2_id, "target_id": t1_id}],
            edge_type="depends_on",
        )
        self.conn.commit()

        status, body = _get(self.port, "/api/timeline")
        assert status == 200
        deps = body["dependencies"]
        assert len(deps) >= 1
        # t2 depends on t1
        assert any(d["source"] == t2_id and d["target"] == t1_id for d in deps)

    def test_tasks_appear_in_levels_with_level_field(self) -> None:
        t1 = tasks.create_task(self.conn, [{"title": "Leaf Task"}], parent_id=self.root_task_id)
        self.conn.commit()

        status, body = _get(self.port, "/api/timeline")
        assert status == 200
        levels = body["levels"]
        assert len(levels) >= 1
        # Each task in levels must have a level field
        for lvl in levels:
            for t in lvl["tasks"]:
                assert "level" in t
                assert t["level"] == lvl["level"]

    def test_parallelism_matches_level_task_counts(self) -> None:
        tasks.create_task(self.conn, [{"title": "P1"}], parent_id=self.root_task_id)
        tasks.create_task(self.conn, [{"title": "P2"}], parent_id=self.root_task_id)
        self.conn.commit()

        status, body = _get(self.port, "/api/timeline")
        assert status == 200
        levels = body["levels"]
        parallelism = body["parallelism"]
        # Parallelism must have one entry per level with matching count
        for level_data in levels:
            par = next((p for p in parallelism if p["level"] == level_data["level"]), None)
            assert par is not None
            assert par["count"] == len(level_data["tasks"])


class TestTimelineCriticalPath(DashboardTestBase):
    """Critical path is computed correctly for linear and branching chains."""

    def test_linear_chain_all_on_critical_path(self) -> None:
        """For A→B→C, all three should appear in critical_path."""
        tA = tasks.create_task(self.conn, [{"title": "Task A"}], parent_id=self.root_task_id)
        tB = tasks.create_task(self.conn, [{"title": "Task B"}], parent_id=self.root_task_id)
        tC = tasks.create_task(self.conn, [{"title": "Task C"}], parent_id=self.root_task_id)
        self.conn.commit()
        a_id = tA["tasks"][0]["id"]
        b_id = tB["tasks"][0]["id"]
        c_id = tC["tasks"][0]["id"]

        # B depends_on A, C depends_on B  →  chain A→B→C
        tasks.add_task_edge(
            self.conn,
            edges=[{"source_id": b_id, "target_id": a_id}],
            edge_type="depends_on",
        )
        tasks.add_task_edge(
            self.conn,
            edges=[{"source_id": c_id, "target_id": b_id}],
            edge_type="depends_on",
        )
        self.conn.commit()

        status, body = _get(self.port, "/api/timeline")
        assert status == 200
        cp = body["critical_path"]
        assert a_id in cp
        assert b_id in cp
        assert c_id in cp
        # Order must be preserved (A before B before C)
        assert cp.index(a_id) < cp.index(b_id) < cp.index(c_id)

    def test_critical_path_is_longest_chain(self) -> None:
        """Critical path follows the longest dependency chain."""
        # Two branches from A: A→B (len 2) vs A→B→C (len 3)
        tA = tasks.create_task(self.conn, [{"title": "Base"}], parent_id=self.root_task_id)
        tB = tasks.create_task(self.conn, [{"title": "Mid"}], parent_id=self.root_task_id)
        tC = tasks.create_task(self.conn, [{"title": "End"}], parent_id=self.root_task_id)
        tD = tasks.create_task(self.conn, [{"title": "Short"}], parent_id=self.root_task_id)
        self.conn.commit()
        a_id = tA["tasks"][0]["id"]
        b_id = tB["tasks"][0]["id"]
        c_id = tC["tasks"][0]["id"]
        d_id = tD["tasks"][0]["id"]

        # C depends_on B, B depends_on A → chain A→B→C (depth 3)
        # D depends_on A → chain A→D (depth 2)
        tasks.add_task_edge(
            self.conn,
            edges=[
                {"source_id": b_id, "target_id": a_id},
                {"source_id": c_id, "target_id": b_id},
                {"source_id": d_id, "target_id": a_id},
            ],
            edge_type="depends_on",
        )
        self.conn.commit()

        status, body = _get(self.port, "/api/timeline")
        assert status == 200
        cp = body["critical_path"]
        # The 3-long chain must be chosen
        assert a_id in cp
        assert b_id in cp
        assert c_id in cp
        # c_id must be at the end of the critical path
        assert cp[-1] == c_id


class TestTimelineNoRootTask:
    """GET /api/timeline returns empty collections when no root task."""

    def setup_method(self) -> None:
        self.store, self.db_path = _make_db()
        self.conn = self.store._conn
        self.server, self.port = _start_server_no_root(self.db_path)

    def teardown_method(self) -> None:
        self.server.shutdown()
        self.store.close()
        Path(self.db_path).unlink(missing_ok=True)

    def test_timeline_empty_no_root(self) -> None:
        status, body = _get(self.port, "/api/timeline")
        assert status == 200
        assert body["levels"] == []
        assert body["dependencies"] == []
        assert body["critical_path"] == []
        assert body["parallelism"] == []


class TestTimelineGanttHtml:
    """index.html contains Gantt markup and Vue bindings for the Timeline tab."""

    def test_gantt_css_classes_present(self) -> None:
        from code_review_graph.dashboard_ui import _PAGE_HTML
        html = _PAGE_HTML.read_text(encoding="utf-8")
        assert "gantt-wrap" in html
        assert "gantt-table" in html
        assert "gantt-bar" in html
        assert "gantt-bar-done" in html
        assert "gantt-critical" in html

    def test_timeline_uses_timeline_data_ref(self) -> None:
        from code_review_graph.dashboard_ui import _PAGE_HTML
        html = _PAGE_HTML.read_text(encoding="utf-8")
        assert "timelineData" in html

    def test_timeline_calls_api_timeline(self) -> None:
        from code_review_graph.dashboard_ui import _PAGE_HTML
        html = _PAGE_HTML.read_text(encoding="utf-8")
        assert "/api/timeline" in html

    def test_critical_path_display_present(self) -> None:
        from code_review_graph.dashboard_ui import _PAGE_HTML
        html = _PAGE_HTML.read_text(encoding="utf-8")
        assert "critical_path" in html
        assert "getTaskTitle" in html

    def test_parallelism_display_present(self) -> None:
        from code_review_graph.dashboard_ui import _PAGE_HTML
        html = _PAGE_HTML.read_text(encoding="utf-8")
        assert "parallelism" in html
        assert "gantt-par-badge" in html

    def test_status_icon_function_present(self) -> None:
        from code_review_graph.dashboard_ui import _PAGE_HTML
        html = _PAGE_HTML.read_text(encoding="utf-8")
        assert "statusIcon" in html

    def test_gantt_legend_present(self) -> None:
        from code_review_graph.dashboard_ui import _PAGE_HTML
        html = _PAGE_HTML.read_text(encoding="utf-8")
        assert "gantt-legend" in html


# ---------------------------------------------------------------------------
# Blast Radius feature tests
# ---------------------------------------------------------------------------


class TestLeafTasksEndpoint(DashboardTestBase):
    """Tests for GET /api/leaf-tasks endpoint."""

    def test_leaf_tasks_returns_correct_shape(self) -> None:
        """GET /api/leaf-tasks returns a dict with 'tasks' list."""
        status, body = _get(self.port, "/api/leaf-tasks")
        assert status == 200
        assert "tasks" in body
        assert isinstance(body["tasks"], list)

    def test_leaf_tasks_correct_leaf_identification(self) -> None:
        """Leaf tasks are those with no children; root with children is not a leaf."""
        child1_res = tasks.create_task(
            self.conn, [{"title": "Leaf One"}], parent_id=self.root_task_id
        )
        child2_res = tasks.create_task(
            self.conn, [{"title": "Leaf Two"}], parent_id=self.root_task_id
        )
        self.conn.commit()
        child1_id = child1_res["tasks"][0]["id"]
        child2_id = child2_res["tasks"][0]["id"]

        status, body = _get(self.port, "/api/leaf-tasks")
        assert status == 200
        task_ids = [t["id"] for t in body["tasks"]]
        # Leaf tasks are the children (no children of their own)
        assert child1_id in task_ids
        assert child2_id in task_ids
        # Root has children, so it should NOT be a leaf
        assert self.root_task_id not in task_ids

    def test_leaf_tasks_title_and_status_present(self) -> None:
        """Each returned leaf task has 'id', 'title', 'status' fields."""
        tasks.create_task(
            self.conn, [{"title": "Leaf With Data"}], parent_id=self.root_task_id
        )
        self.conn.commit()

        status, body = _get(self.port, "/api/leaf-tasks")
        assert status == 200
        assert len(body["tasks"]) > 0
        for t in body["tasks"]:
            assert "id" in t
            assert "title" in t
            assert "status" in t


class TestLeafTasksNoRoot:
    """GET /api/leaf-tasks returns empty list when no root task."""

    def setup_method(self) -> None:
        self.store, self.db_path = _make_db()
        self.conn = self.store._conn
        self.server, self.port = _start_server_no_root(self.db_path)

    def teardown_method(self) -> None:
        self.server.shutdown()
        self.store.close()
        Path(self.db_path).unlink(missing_ok=True)

    def test_leaf_tasks_empty_on_no_root(self) -> None:
        status, body = _get(self.port, "/api/leaf-tasks")
        assert status == 200
        assert body["tasks"] == []


class TestBlastRadiusEndpoint(DashboardTestBase):
    """Tests for GET /api/blast-radius endpoint."""

    def test_blast_radius_no_task_id_returns_no_task(self) -> None:
        """GET /api/blast-radius without task_id returns no_task status."""
        status, body = _get(self.port, "/api/blast-radius")
        assert status == 200
        assert body["status"] == "no_task"
        assert body["direct_nodes"] == []
        assert body["affected_nodes"] == []
        assert body["uncovered_nodes"] == []
        assert body["coverage_ratio"] == 0

    def test_blast_radius_returns_proper_shape(self) -> None:
        """GET /api/blast-radius returns all required keys."""
        import urllib.parse

        child_res = tasks.create_task(
            self.conn, [{"title": "BR Test Task"}], parent_id=self.root_task_id
        )
        self.conn.commit()
        task_id = child_res["tasks"][0]["id"]

        path = "/api/blast-radius?task_id=" + urllib.parse.quote(task_id) + "&depth=2"
        status, body = _get(self.port, path)
        assert status == 200
        assert isinstance(body, dict)
        # All required fields must be present
        for key in ("status", "direct_nodes", "affected_nodes", "uncovered_nodes",
                    "coverage_ratio", "covered_count", "total_affected"):
            assert key in body, f"Missing key: {key}"
        # Arrays must be lists
        assert isinstance(body["direct_nodes"], list)
        assert isinstance(body["affected_nodes"], list)
        assert isinstance(body["uncovered_nodes"], list)
        # Status is a valid value
        assert body["status"] in ("ok", "not_applicable", "no_task", "error")

    def test_blast_radius_task_without_code_refs_returns_not_applicable(self) -> None:
        """A task with no code refs returns status 'not_applicable'."""
        import urllib.parse

        child_res = tasks.create_task(
            self.conn, [{"title": "No Code Refs Task"}], parent_id=self.root_task_id
        )
        self.conn.commit()
        task_id = child_res["tasks"][0]["id"]

        path = "/api/blast-radius?task_id=" + urllib.parse.quote(task_id) + "&depth=2"
        status, body = _get(self.port, path)
        assert status == 200
        # No code refs → not_applicable
        assert body["status"] in ("not_applicable", "ok")
        # Either way, shape should be correct
        assert "direct_nodes" in body
        assert "affected_nodes" in body

    def test_blast_radius_invalid_task_id_returns_gracefully(self) -> None:
        """GET /api/blast-radius with unknown task_id does not crash."""
        import urllib.parse

        path = "/api/blast-radius?task_id=" + urllib.parse.quote("nonexistent_task_id_xyz")
        status, body = _get(self.port, path)
        assert status == 200
        assert isinstance(body, dict)
        # Must not return 500 — should return a valid status
        assert "status" in body


class TestBlastRadiusNoRoot:
    """GET /api/blast-radius returns no_task when no root task set."""

    def setup_method(self) -> None:
        self.store, self.db_path = _make_db()
        self.conn = self.store._conn
        self.server, self.port = _start_server_no_root(self.db_path)

    def teardown_method(self) -> None:
        self.server.shutdown()
        self.store.close()
        Path(self.db_path).unlink(missing_ok=True)

    def test_blast_radius_no_root_returns_no_task(self) -> None:
        import urllib.parse
        path = "/api/blast-radius?task_id=" + urllib.parse.quote("any_task") + "&depth=2"
        status, body = _get(self.port, path)
        assert status == 200
        assert body["status"] == "no_task"
        assert body["direct_nodes"] == []


class TestBlastRadiusHtml:
    """Verify index.html contains blast radius UI elements."""

    def test_blast_radius_controls_present(self) -> None:
        from code_review_graph.dashboard_ui import _PAGE_HTML
        html = _PAGE_HTML.read_text(encoding="utf-8")
        assert "blastTask" in html
        assert "blastDepth" in html
        assert "blastResult" in html
        assert "leafTasks" in html

    def test_blast_radius_functions_present(self) -> None:
        from code_review_graph.dashboard_ui import _PAGE_HTML
        html = _PAGE_HTML.read_text(encoding="utf-8")
        assert "applyBlastOverlay" in html
        assert "clearBlastOverlay" in html
        assert "loadLeafTasks" in html

    def test_blast_radius_api_endpoints_present(self) -> None:
        from code_review_graph.dashboard_ui import _PAGE_HTML
        html = _PAGE_HTML.read_text(encoding="utf-8")
        assert "/api/blast-radius" in html
        assert "/api/leaf-tasks" in html

    def test_blast_radius_color_values_present(self) -> None:
        """The overlay uses specific hex colors for direct/depth1/depth2."""
        from code_review_graph.dashboard_ui import _PAGE_HTML
        html = _PAGE_HTML.read_text(encoding="utf-8")
        assert "#dc3545" in html  # direct = red
        assert "#fd7e14" in html  # depth 1 = orange
        assert "#ffc107" in html  # depth 2 = yellow

    def test_blast_coverage_display_present(self) -> None:
        """Coverage percentage and uncovered nodes are shown in HTML."""
        from code_review_graph.dashboard_ui import _PAGE_HTML
        html = _PAGE_HTML.read_text(encoding="utf-8")
        assert "coverage_ratio" in html
        assert "covered_count" in html
        assert "uncovered_nodes" in html


# ---------------------------------------------------------------------------
# 12. test_api_sequence_flows — /api/sequence-flows returns flows list
# ---------------------------------------------------------------------------


class TestSequenceFlows(DashboardTestBase):
    def test_api_sequence_flows_empty(self) -> None:
        """No flows in DB → empty list, not an error."""
        status, body = _get(self.port, "/api/sequence-flows")
        assert status == 200
        assert "flows" in body
        assert isinstance(body["flows"], list)

    def test_api_sequence_flows_structure(self) -> None:
        """When flows exist, each entry has expected keys."""
        # Insert a minimal flow row directly
        conn = self.store._conn
        conn.execute(
            "INSERT INTO flows (name, entry_point_id, depth, node_count, file_count, criticality, path_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("test_flow", 1, 3, 5, 2, 0.8, "[]"),
        )
        conn.commit()
        status, body = _get(self.port, "/api/sequence-flows")
        assert status == 200
        flows = body["flows"]
        assert len(flows) >= 1
        first = flows[0]
        assert "id" in first
        assert "name" in first
        assert "criticality" in first
        assert "depth" in first
        assert "node_count" in first


# ---------------------------------------------------------------------------
# 13. test_sequence_html — Sequence tab has mode selector in index.html
# ---------------------------------------------------------------------------


class TestSequenceHtml:
    def test_sequence_mode_selector_present(self) -> None:
        from code_review_graph.dashboard_ui import _PAGE_HTML
        html = _PAGE_HTML.read_text(encoding="utf-8")
        assert "seqMode" in html
        assert "Task Contracts" in html
        assert "Code Flow" in html

    def test_sequence_flow_dropdown_present(self) -> None:
        from code_review_graph.dashboard_ui import _PAGE_HTML
        html = _PAGE_HTML.read_text(encoding="utf-8")
        assert "seqFlows" in html
        assert "seqFlowId" in html
        assert "loadSequenceFlow" in html

    def test_sequence_flows_api_present(self) -> None:
        from code_review_graph.dashboard_ui import _PAGE_HTML
        html = _PAGE_HTML.read_text(encoding="utf-8")
        assert "/api/sequence-flows" in html


# ---------------------------------------------------------------------------
# NEW: C4 Zoom Navigator tests
# ---------------------------------------------------------------------------


class TestC4ZoomContainers(DashboardTestBase):
    """Tests for GET /api/c4/zoom/containers."""

    def test_c4_zoom_containers_returns_list(self) -> None:
        """Returns containers list (may be empty when no community_id nodes)."""
        status, body = _get(self.port, "/api/c4/zoom/containers")
        assert status == 200
        assert "containers" in body
        assert isinstance(body["containers"], list)

    def test_c4_zoom_containers_with_nodes(self) -> None:
        """Nodes with community_id appear as containers."""
        import sqlite3
        import time

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        ts = time.time()
        conn.execute(
            "INSERT INTO nodes (kind, name, qualified_name, file_path, language, community_id, updated_at)"
            " VALUES ('Function', 'fn_a', 'mod::fn_a', 'mod.py', 'Python', 7, ?)",
            (ts,),
        )
        conn.execute(
            "INSERT INTO nodes (kind, name, qualified_name, file_path, language, community_id, updated_at)"
            " VALUES ('Function', 'fn_b', 'mod::fn_b', 'mod.py', 'Python', 7, ?)",
            (ts,),
        )
        conn.commit()
        conn.close()

        status, body = _get(self.port, "/api/c4/zoom/containers")
        assert status == 200
        containers = body["containers"]
        ids = [c["id"] for c in containers]
        assert "community_7" in ids
        comm = next(c for c in containers if c["id"] == "community_7")
        assert comm["size"] == 2
        assert comm["language"] == "Python"
        assert comm["community_id"] == 7
        assert comm["has_children"] is True
        assert "nodes" in comm["description"]


class TestC4ZoomComponents(DashboardTestBase):
    """Tests for GET /api/c4/zoom/components?container_id=N."""

    def test_c4_zoom_components_returns_filtered_by_kind(self) -> None:
        """Only Class and File nodes are returned, not Function/Method nodes."""
        import sqlite3
        import time
        import urllib.parse

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        ts = time.time()
        # Insert nodes of different kinds
        conn.execute(
            "INSERT INTO nodes (kind, name, qualified_name, file_path, community_id, updated_at)"
            " VALUES ('Class', 'MyClass', 'mod.py::MyClass', 'mod.py', 5, ?)",
            (ts,),
        )
        conn.execute(
            "INSERT INTO nodes (kind, name, qualified_name, file_path, community_id, updated_at)"
            " VALUES ('File', 'mod.py', 'mod.py', 'mod.py', 5, ?)",
            (ts,),
        )
        conn.execute(
            "INSERT INTO nodes (kind, name, qualified_name, file_path, community_id, updated_at)"
            " VALUES ('Function', 'my_func', 'mod.py::my_func', 'mod.py', 5, ?)",
            (ts,),
        )
        conn.commit()
        conn.close()

        path = "/api/c4/zoom/components?container_id=5"
        status, body = _get(self.port, path)
        assert status == 200
        assert "components" in body
        assert body["container_id"] == "community_5"
        kinds = {c["kind"] for c in body["components"]}
        assert kinds <= {"Class", "File"}
        assert "Function" not in kinds
        names = {c["name"] for c in body["components"]}
        assert "MyClass" in names
        assert "my_func" not in names
        # Each component has required fields
        for comp in body["components"]:
            assert "id" in comp
            assert "name" in comp
            assert "kind" in comp
            assert "file_path" in comp
            assert "qualified_name" in comp
            assert "node_id" in comp

    def test_c4_zoom_components_invalid_container_id(self) -> None:
        """Non-integer container_id returns empty components, no crash."""
        status, body = _get(self.port, "/api/c4/zoom/components?container_id=bad")
        assert status == 200
        assert "components" in body
        assert body["components"] == []

    def test_c4_zoom_components_missing_container_id(self) -> None:
        """Missing container_id returns empty components."""
        status, body = _get(self.port, "/api/c4/zoom/components")
        assert status == 200
        assert "components" in body
        assert body["components"] == []


class TestC4ZoomCode(DashboardTestBase):
    """Tests for GET /api/c4/zoom/code?component=<qualified_name>."""

    def test_c4_zoom_code_returns_functions_in_component(self) -> None:
        """Functions and Methods inside a Class are returned."""
        import sqlite3
        import time
        import urllib.parse

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        ts = time.time()
        # Insert a Class and its methods
        conn.execute(
            "INSERT INTO nodes (kind, name, qualified_name, file_path, line_start, line_end, updated_at)"
            " VALUES ('Class', 'Parser', 'parser.py::Parser', 'parser.py', 1, 100, ?)",
            (ts,),
        )
        conn.execute(
            "INSERT INTO nodes (kind, name, qualified_name, file_path, line_start, line_end, signature, updated_at)"
            " VALUES ('Method', 'parse', 'parser.py::Parser::parse', 'parser.py', 10, 40, 'def parse(self)', ?)",
            (ts,),
        )
        conn.execute(
            "INSERT INTO nodes (kind, name, qualified_name, file_path, line_start, line_end, updated_at)"
            " VALUES ('Method', 'reset', 'parser.py::Parser::reset', 'parser.py', 42, 50, ?)",
            (ts,),
        )
        # A Function outside the class should not appear
        conn.execute(
            "INSERT INTO nodes (kind, name, qualified_name, file_path, line_start, line_end, updated_at)"
            " VALUES ('Function', 'helper', 'parser.py::helper', 'parser.py', 200, 210, ?)",
            (ts,),
        )
        conn.commit()
        conn.close()

        path = "/api/c4/zoom/code?component=" + urllib.parse.quote("parser.py::Parser")
        status, body = _get(self.port, path)
        assert status == 200
        assert "nodes" in body
        assert body["component"] == "parser.py::Parser"
        node_names = {n["name"] for n in body["nodes"]}
        assert "parse" in node_names
        assert "reset" in node_names
        assert "helper" not in node_names
        # Each node has expected fields
        for n in body["nodes"]:
            assert "id" in n
            assert "name" in n
            assert "kind" in n
            assert "qualified_name" in n
            assert "node_id" in n
            assert "line_start" in n
            assert "line_end" in n
            assert "signature" in n
        # Results ordered by line_start
        starts = [n["line_start"] for n in body["nodes"] if n["line_start"] is not None]
        assert starts == sorted(starts)

    def test_c4_zoom_code_empty_component(self) -> None:
        """Empty component param returns empty nodes list."""
        status, body = _get(self.port, "/api/c4/zoom/code")
        assert status == 200
        assert body["nodes"] == []

    def test_c4_zoom_code_nonexistent_component(self) -> None:
        """Non-matching component returns empty nodes."""
        import urllib.parse
        path = "/api/c4/zoom/code?component=" + urllib.parse.quote("nonexistent::Class")
        status, body = _get(self.port, path)
        assert status == 200
        assert body["nodes"] == []
        assert body["component"] == "nonexistent::Class"
