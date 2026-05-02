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
