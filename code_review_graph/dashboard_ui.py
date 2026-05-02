"""Dashboard UI — multi-tab local web server for code-review-graph.

Serves a single-page app with tabs for:
  Architecture (C4 diagrams), Task DAG, Contracts,
  Notes & Questions, Roadmap, Timeline, Sequence Diagrams.

All data is read from SQLite. No external Python dependencies beyond stdlib.

Lifecycle: start_dashboard() blocks until Ctrl+C.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from .incremental import find_project_root, get_db_path
from .task_analysis import execution_order, roadmap
from .tasks import (
    add_note,
    create_task,
    get_active_root,
    get_task,
    get_task_dag,
    list_contracts,
    list_notes,
    update_note,
    update_tasks,
)

try:
    from .c4_generator import get_c4_path, resolve_for_render
    from .c4_parser import parse_c4_file

    _C4_AVAILABLE = True
except ImportError:
    _C4_AVAILABLE = False

try:
    from .sequence_generator import generate_sequence

    _SEQ_AVAILABLE = True
except ImportError:
    _SEQ_AVAILABLE = False


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class DashboardHandler(BaseHTTPRequestHandler):
    db_path: Path
    repo_root: Path
    root_task_id: Optional[str]
    root_title: str

    def log_message(self, *args: Any) -> None:  # type: ignore[override]
        pass  # suppress request logs

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            self._serve_page()
        elif path == "/api/root-task":
            self._serve_root_task()
        elif path == "/api/c4":
            self._serve_c4()
        elif path == "/api/dag":
            self._serve_dag()
        elif path == "/api/annotations":
            self._serve_annotations()
        elif path == "/api/contracts":
            self._serve_contracts()
        elif path == "/api/roadmap":
            self._serve_roadmap()
        elif path == "/api/notes":
            self._serve_notes()
        elif path == "/api/sequence":
            qs = parse_qs(parsed.query)
            task_id = qs.get("task_id", [None])[0]
            flow_name = qs.get("flow_name", [None])[0]
            self._serve_sequence(task_id=task_id, flow_name=flow_name)
        elif path == "/api/execution-order":
            self._serve_execution_order()
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/root-task":
            self._handle_save_root_task()
        elif path == "/api/annotations":
            self._handle_create_annotation()
        elif path.startswith("/api/notes/") and path.endswith("/answer"):
            parts = path.split("/")
            # /api/notes/<id>/answer  → parts = ['', 'api', 'notes', '<id>', 'answer']
            note_id = parts[3] if len(parts) >= 5 else ""
            self._handle_answer(note_id)
        else:
            self.send_error(404)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        return json.loads(raw) if raw else {}

    def _json_response(self, data: object) -> None:
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_page(self) -> None:
        body = _PAGE_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------------
    # GET /api/c4
    # ------------------------------------------------------------------

    def _serve_c4(self) -> None:
        if not _C4_AVAILABLE:
            self._json_response({"levels": {}, "error": "c4 modules not available"})
            return
        c4_path = get_c4_path(str(self.repo_root))
        if not c4_path.exists():
            self._json_response({"levels": {}})
            return
        try:
            arch = parse_c4_file(c4_path.read_text(encoding="utf-8"))
            levels: dict[str, Any] = {}
            for diagram in arch.diagrams:
                nodes = resolve_for_render(arch, diagram.title)
                edges: list[dict[str, Any]] = []
                for section in diagram.sections:
                    for elem in section.elements:
                        if elem.kind == "Rel":
                            edges.append({
                                "source": elem.id,
                                "target": elem.target_id,
                                "label": elem.label,
                            })
                for elem in diagram.loose_elements:
                    if elem.kind == "Rel":
                        edges.append({
                            "source": elem.id,
                            "target": elem.target_id,
                            "label": elem.label,
                        })
                slug = diagram.title.replace(" ", "_")
                levels[slug] = {"title": diagram.title, "nodes": nodes, "edges": edges}
            self._json_response({"levels": levels})
        except Exception as exc:
            self._json_response({"levels": {}, "error": str(exc)})

    # ------------------------------------------------------------------
    # GET /api/dag
    # ------------------------------------------------------------------

    def _serve_dag(self) -> None:
        if self.root_task_id is None:
            self._json_response({"nodes": [], "edges": []})
            return
        conn = self._get_conn()
        try:
            dag = get_task_dag(conn, self.root_task_id, compact=False)
        except Exception as exc:
            self._json_response({"nodes": [], "edges": [], "error": str(exc)})
            return
        finally:
            conn.close()
        _status_colors = {
            "done": "#28a745",
            "in_progress": "#17a2b8",
            "ready": "#ffc107",
            "blocked": "#6c757d",
            "draft": "#e9ecef",
            "refined": "#adb5bd",
            "archived": "#6c757d",
        }
        nodes = []
        for node in dag.get("nodes", []):
            status = node.get("status", "draft")
            nodes.append({
                "data": {
                    "id": node["id"],
                    "label": node.get("title", node["id"]),
                    "status": status,
                    "color": _status_colors.get(status, "#e9ecef"),
                    "depth": node.get("depth", 0),
                    "parent": node.get("parent_id"),
                }
            })
        edges = []
        for edge in dag.get("edges", []):
            edges.append({
                "data": {
                    "id": f"{edge['source_id']}__{edge['target_id']}__{edge['edge_type']}",
                    "source": edge["source_id"],
                    "target": edge["target_id"],
                    "label": edge.get("edge_type", ""),
                }
            })
        self._json_response({"nodes": nodes, "edges": edges})

    # ------------------------------------------------------------------
    # GET /api/annotations
    # ------------------------------------------------------------------

    def _serve_annotations(self) -> None:
        if self.root_task_id is None:
            self._json_response({"annotations": {}})
            return
        conn = self._get_conn()
        try:
            all_notes = list_notes(conn, task_id=self.root_task_id, include_children=True)
        finally:
            conn.close()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for note in all_notes:
            c4_id = note.get("c4_element_id")
            if c4_id:
                grouped.setdefault(c4_id, []).append(dict(note))
        self._json_response({"annotations": grouped})

    # ------------------------------------------------------------------
    # GET /api/contracts
    # ------------------------------------------------------------------

    def _serve_contracts(self) -> None:
        if self.root_task_id is None:
            self._json_response({"contracts": []})
            return
        conn = self._get_conn()
        try:
            contracts = list_contracts(conn, scope_task_id=self.root_task_id)
        finally:
            conn.close()
        self._json_response({"contracts": [dict(c) for c in contracts]})

    # ------------------------------------------------------------------
    # GET /api/roadmap
    # ------------------------------------------------------------------

    def _serve_roadmap(self) -> None:
        if self.root_task_id is None:
            self._json_response({"done": 0, "total": 0, "percent": 0, "attention": {}})
            return
        conn = self._get_conn()
        try:
            data = roadmap(conn, self.root_task_id)
        except Exception as exc:
            self._json_response({"error": str(exc)})
            return
        finally:
            conn.close()
        self._json_response(data)

    # ------------------------------------------------------------------
    # GET /api/notes
    # ------------------------------------------------------------------

    def _serve_notes(self) -> None:
        if self.root_task_id is None:
            self._json_response({"open": [], "answered": [], "resolved": [], "task_title": ""})
            return
        conn = self._get_conn()
        try:
            open_notes = list_notes(
                conn,
                task_id=self.root_task_id,
                status="open",
                include_children=True,
            )
            answered = list_notes(
                conn,
                task_id=self.root_task_id,
                status="answered",
                include_children=True,
            )
            resolved = list_notes(
                conn,
                task_id=self.root_task_id,
                status="resolved",
                include_children=True,
            )
        finally:
            conn.close()
        self._json_response({
            "open": [dict(n) for n in open_notes],
            "answered": [dict(n) for n in answered],
            "resolved": [dict(n) for n in resolved],
            "task_title": self.root_title,
        })

    # ------------------------------------------------------------------
    # GET /api/sequence
    # ------------------------------------------------------------------

    def _serve_sequence(
        self, task_id: Optional[str] = None, flow_name: Optional[str] = None
    ) -> None:
        if not _SEQ_AVAILABLE:
            self._json_response({"mermaid": "", "error": "sequence_generator not available"})
            return
        effective_task_id = task_id or self.root_task_id
        if effective_task_id is None:
            self._json_response({"mermaid": ""})
            return
        conn = self._get_conn()
        try:
            result = generate_sequence(conn=conn, task_id=effective_task_id)
        except Exception as exc:
            self._json_response({"mermaid": "", "error": str(exc)})
            return
        finally:
            conn.close()
        self._json_response(result)

    # ------------------------------------------------------------------
    # GET /api/execution-order
    # ------------------------------------------------------------------

    def _serve_execution_order(self) -> None:
        if self.root_task_id is None:
            self._json_response({"levels": []})
            return
        conn = self._get_conn()
        try:
            data = execution_order(conn, self.root_task_id)
        except Exception as exc:
            self._json_response({"error": str(exc)})
            return
        finally:
            conn.close()
        self._json_response(data)

    # ------------------------------------------------------------------
    # GET /api/root-task
    # ------------------------------------------------------------------

    def _serve_root_task(self) -> None:
        """GET /api/root-task — return current root task or null."""
        if self.root_task_id is None:
            self._json_response({"task": None})
            return
        conn = self._get_conn()
        try:
            task = get_task(conn, self.root_task_id)
            if task is None:
                self._json_response({"task": None})
                return
            self._json_response({
                "task": {
                    "id": task["id"],
                    "title": task["title"],
                    "description": task.get("description", "") or "",
                    "status": task["status"],
                }
            })
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # POST /api/root-task
    # ------------------------------------------------------------------

    def _handle_save_root_task(self) -> None:
        """POST /api/root-task — create or update root task."""
        body = self._read_body()
        title = body.get("title", "").strip()
        description = body.get("description", "").strip()

        if not title:
            self._json_response({"error": "Title is required"})
            return

        conn = self._get_conn()
        try:
            if self.root_task_id is None:
                # Create new root task
                result = create_task(conn, [{"title": title, "description": description}])
                new_id = result["tasks"][0]["id"]
                # Update class-level state so subsequent requests see the new task
                type(self).root_task_id = new_id
                type(self).root_title = title
                self._json_response({
                    "task": {
                        "id": new_id,
                        "title": title,
                        "description": description,
                        "status": "draft",
                    }
                })
            else:
                # Update existing root task
                update_tasks(conn, [{"task_id": self.root_task_id, "title": title, "description": description}])
                type(self).root_title = title
                self._json_response({
                    "task": {
                        "id": self.root_task_id,
                        "title": title,
                        "description": description,
                    }
                })
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # POST /api/annotations
    # ------------------------------------------------------------------

    def _handle_create_annotation(self) -> None:
        body = self._read_body()
        c4_element_id = body.get("c4_element_id", "")
        content = body.get("content", "")
        task_id = body.get("task_id") or self.root_task_id
        if not content:
            self.send_error(400, "content required")
            return
        conn = self._get_conn()
        try:
            result = add_note(
                conn,
                task_id=task_id,
                notes=[{
                    "note_type": "question",
                    "content": content,
                    "status": "open",
                    "c4_element_id": c4_element_id or None,
                }],
            )
            conn.commit()
        finally:
            conn.close()
        created = result.get("notes", [{}])
        note_id = created[0].get("id") if created else None
        self._json_response({"status": "ok", "note_id": note_id})

    # ------------------------------------------------------------------
    # POST /api/notes/:id/answer
    # ------------------------------------------------------------------

    def _handle_answer(self, note_id: str) -> None:
        body = self._read_body()
        resolution = body.get("resolution", "")
        conn = self._get_conn()
        try:
            update_note(conn, note_id=note_id, status="answered", resolution=resolution)
            conn.commit()
        finally:
            conn.close()
        self._json_response({"ok": True})


# ---------------------------------------------------------------------------
# Inline HTML page
# ---------------------------------------------------------------------------


_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Code Review Graph Dashboard</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/cytoscape/3.28.1/cytoscape.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/dagre/0.8.5/dagre.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/cytoscape-dagre@2.5.0/cytoscape-dagre.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/vue@3/dist/vue.global.prod.js"></script>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<style>
:root {
  --blue: #438DD5; --green: #28a745; --yellow: #ffc107; --gray: #6c757d;
  --red: #ef4444; --teal: #17a2b8; --bg: #f8f9fa; --border: #dee2e6;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: system-ui, sans-serif; background: var(--bg); color: #212529; }
#app { display: flex; flex-direction: column; height: 100vh; }
header {
  background: #1a1a2e; color: #e0e0e0; padding: 10px 20px;
  display: flex; align-items: center; gap: 16px; flex-shrink: 0;
}
header h1 { font-size: 1rem; font-weight: 600; color: #7eb3e8; }
header .subtitle { font-size: 0.8rem; color: #888; }
.tabs { display: flex; background: #fff; border-bottom: 2px solid var(--border); flex-shrink: 0; }
.tab-btn {
  padding: 10px 18px; border: none; background: none; cursor: pointer;
  font-size: 0.875rem; color: var(--gray); border-bottom: 2px solid transparent;
  margin-bottom: -2px; font-weight: 500;
}
.tab-btn:hover { color: var(--blue); }
.tab-btn.active { color: var(--blue); border-bottom-color: var(--blue); }
.tab-btn.disabled { color: #ccc; cursor: not-allowed; pointer-events: none; }
.tab-content { flex: 1; overflow: auto; padding: 16px 20px; }
.graph-container { width: 100%; height: calc(100vh - 180px); background: #fff;
  border: 1px solid var(--border); border-radius: 6px; }
.card { background: #fff; border: 1px solid var(--border); border-radius: 6px;
  padding: 14px 16px; margin-bottom: 10px; }
.card-title { font-weight: 600; margin-bottom: 6px; font-size: 0.95rem; }
.badge { display: inline-block; border-radius: 20px; padding: 2px 8px;
  font-size: 0.75rem; font-weight: 600; }
.badge-green { background: #d4edda; color: #155724; }
.badge-yellow { background: #fff3cd; color: #856404; }
.badge-blue { background: #cce5ff; color: #004085; }
.badge-gray { background: #e9ecef; color: #495057; }
.badge-red { background: #f8d7da; color: #721c24; }
.progress-bar { background: var(--border); border-radius: 4px; height: 8px; overflow: hidden; }
.progress-fill { height: 100%; background: var(--green); border-radius: 4px; transition: width 0.3s; }
.note-card { border-left: 4px solid var(--blue); }
.note-card.answered { border-left-color: var(--green); }
.note-card.resolved { border-left-color: var(--gray); opacity: 0.7; }
.section-hdr { font-size: 0.8rem; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.05em; color: var(--gray); margin: 16px 0 8px; }
.empty { color: var(--gray); font-size: 0.875rem; padding: 12px 0; }
.level-row { display: flex; gap: 8px; margin-bottom: 8px; flex-wrap: wrap; }
.level-task { background: #fff; border: 1px solid var(--border); border-radius: 4px;
  padding: 6px 10px; font-size: 0.8rem; max-width: 200px; }
.msg { color: var(--gray); padding: 40px; text-align: center; }
.select-row { display: flex; gap: 8px; margin-bottom: 12px; align-items: center; }
select { padding: 5px 10px; border: 1px solid var(--border); border-radius: 4px;
  font-size: 0.875rem; }
.mermaid-wrap { background: #fff; border: 1px solid var(--border); border-radius: 6px;
  padding: 16px; min-height: 200px; overflow: auto; }
.contract-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 12px; }
.contract-type { font-size: 0.75rem; color: var(--gray); margin-bottom: 4px; }
.contract-def { font-size: 0.8rem; font-family: monospace; background: #f8f9fa;
  border: 1px solid var(--border); border-radius: 4px; padding: 8px; white-space: pre-wrap;
  word-break: break-all; margin-top: 6px; }
.c4-level-select { margin-bottom: 12px; }
</style>
</head>
<body>
<div id="app">
  <header>
    <div>
      <h1>&#x1F4CA; Code Review Graph Dashboard</h1>
      <div class="subtitle" v-if="rootTitle">{{ rootTitle }}</div>
    </div>
  </header>
  <div class="tabs">
    <button v-for="t in tabs" :key="t.id"
      class="tab-btn" :class="{active: activeTab === t.id, disabled: !rootTask && t.id !== 'root'}"
      :disabled="!rootTask && t.id !== 'root'"
      @click="switchTab(t.id)">{{ t.label }}</button>
  </div>
  <div class="tab-content">
    <!-- Root Task Tab -->
    <div v-if="activeTab === 'root'">
      <div class="card" style="max-width:600px">
        <div class="card-title">Root Task</div>
        <div v-if="rootTask">
          <p style="font-size:0.85rem;color:var(--gray);margin-bottom:12px">
            ID: {{ rootTask.id }} &bull; Status: {{ rootTask.status }}
          </p>
        </div>
        <div style="margin-top:12px">
          <label style="display:block;font-size:0.85rem;font-weight:600;margin-bottom:4px">Title</label>
          <input v-model="rootTaskForm.title"
            style="width:100%;padding:8px 10px;border:1px solid var(--border);border-radius:4px;font-size:0.9rem"
            placeholder="Feature title...">
        </div>
        <div style="margin-top:12px">
          <label style="display:block;font-size:0.85rem;font-weight:600;margin-bottom:4px">Description</label>
          <textarea v-model="rootTaskForm.description" rows="6"
            style="width:100%;padding:8px 10px;border:1px solid var(--border);border-radius:4px;font-size:0.9rem;resize:vertical"
            placeholder="Describe the feature / objective..."></textarea>
        </div>
        <div style="margin-top:16px">
          <button @click="saveRootTask" :disabled="rootTaskSaving || !rootTaskForm.title.trim()"
            style="padding:8px 20px;background:var(--blue);color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:0.9rem">
            {{ rootTask ? 'Update Task' : 'Create Root Task' }}
          </button>
          <span v-if="rootTaskSaving" style="margin-left:8px;color:var(--gray);font-size:0.85rem">Saving...</span>
        </div>
      </div>
    </div>

    <!-- Architecture Tab -->
    <div v-if="activeTab === 'arch'">
      <div class="c4-level-select" v-if="Object.keys(c4Data.levels || {}).length">
        <select v-model="c4Level">
          <option v-for="(_, slug) in c4Data.levels" :key="slug" :value="slug">
            {{ c4Data.levels[slug].title || slug }}
          </option>
        </select>
      </div>
      <div id="cy-arch" class="graph-container" v-if="c4Level"></div>
      <div class="msg" v-else-if="!c4Loading">No C4 architecture data. Run code-review-graph build first.</div>
      <div class="msg" v-else>Loading C4 architecture&hellip;</div>
    </div>

    <!-- Task DAG Tab -->
    <div v-if="activeTab === 'dag'">
      <div id="cy-dag" class="graph-container"></div>
    </div>

    <!-- Contracts Tab -->
    <div v-if="activeTab === 'contracts'">
      <div v-if="contracts.length === 0" class="msg">No contracts defined yet.</div>
      <div class="contract-grid" v-else>
        <div class="card" v-for="c in contracts" :key="c.id">
          <div class="contract-type">{{ c.contract_type }}</div>
          <div class="card-title">{{ c.name }}</div>
          <span class="badge" :class="contractBadgeClass(c.status)">{{ c.status }}</span>
          <div class="contract-def" v-if="c.definition">{{ c.definition }}</div>
        </div>
      </div>
    </div>

    <!-- Notes Tab -->
    <div v-if="activeTab === 'notes'">
      <div class="section-hdr" v-if="notes.open && notes.open.length">
        &#x2753; Open ({{ notes.open.length }})
      </div>
      <div v-for="n in (notes.open || [])" :key="n.id" class="card note-card">
        <div class="card-title">{{ n.content }}</div>
        <div style="font-size:0.8rem;color:var(--gray)">{{ n.note_type }} &bull; {{ n.task_id }}</div>
      </div>
      <div v-if="!(notes.open && notes.open.length)" class="empty">No open notes &#x2714;</div>

      <div class="section-hdr" v-if="notes.answered && notes.answered.length">
        &#x23F3; Answered ({{ notes.answered.length }})
      </div>
      <div v-for="n in (notes.answered || [])" :key="n.id" class="card note-card answered">
        <div class="card-title">{{ n.content }}</div>
        <div style="font-size:0.875rem;font-style:italic;color:#374151;margin-top:4px">
          {{ n.resolution }}
        </div>
      </div>

      <div class="section-hdr" v-if="notes.resolved && notes.resolved.length">
        &#x2705; Resolved ({{ notes.resolved.length }})
      </div>
      <div v-for="n in (notes.resolved || [])" :key="n.id" class="card note-card resolved">
        <div class="card-title">{{ n.content }}</div>
      </div>
    </div>

    <!-- Roadmap Tab -->
    <div v-if="activeTab === 'roadmap'">
      <div v-if="!roadmapData" class="msg">Loading&hellip;</div>
      <div v-else>
        <div class="card" style="margin-bottom:16px">
          <div class="card-title">Progress</div>
          <div style="margin:10px 0 4px;font-size:0.875rem">
            {{ roadmapData.done || 0 }} / {{ roadmapData.total || 0 }} tasks done
            ({{ roadmapData.percent || 0 }}%)
          </div>
          <div class="progress-bar">
            <div class="progress-fill" :style="{width: (roadmapData.percent || 0) + '%'}"></div>
          </div>
        </div>
        <div v-if="roadmapData.attention && roadmapData.attention.ready_to_start && roadmapData.attention.ready_to_start.length">
          <div class="section-hdr">&#x25B6; Ready to Start</div>
          <div class="card" v-for="t in roadmapData.attention.ready_to_start" :key="t.id">
            <div class="card-title">{{ t.title }}</div>
            <span class="badge badge-green">{{ t.status }}</span>
          </div>
        </div>
        <div v-if="roadmapData.attention && roadmapData.attention.unresolved_questions && roadmapData.attention.unresolved_questions.length">
          <div class="section-hdr">&#x2753; Open Questions</div>
          <div class="card" v-for="n in roadmapData.attention.unresolved_questions" :key="n.id">
            {{ n.content }}
          </div>
        </div>
      </div>
    </div>

    <!-- Timeline Tab -->
    <div v-if="activeTab === 'timeline'">
      <div v-if="!execOrder" class="msg">Loading&hellip;</div>
      <div v-else>
        <div v-for="(level, idx) in (execOrder.levels || [])" :key="idx"
             style="margin-bottom:12px">
          <div class="section-hdr">Level {{ idx + 1 }} (parallel)</div>
          <div class="level-row">
            <div class="level-task card" v-for="t in level.tasks" :key="t.id">
              <div style="font-weight:600;font-size:0.8rem">{{ t.title }}</div>
              <span class="badge" :class="statusBadgeClass(t.status)">{{ t.status }}</span>
            </div>
          </div>
        </div>
        <div v-if="!(execOrder.levels && execOrder.levels.length)" class="empty">
          No execution levels found.
        </div>
      </div>
    </div>

    <!-- Sequence Tab -->
    <div v-if="activeTab === 'sequence'">
      <div class="select-row">
        <span style="font-size:0.875rem">Task sequence diagram</span>
      </div>
      <div class="mermaid-wrap" ref="mermaidDiv">
        <div class="msg" v-if="!seqDiagram">Loading sequence diagram&hellip;</div>
        <div v-else v-html="seqHtml"></div>
      </div>
    </div>
  </div>
</div>

<script>
const { createApp, ref, reactive, watch, onMounted, nextTick } = Vue;

if (typeof cytoscapeDagre !== 'undefined' && typeof cytoscape !== 'undefined') {
  cytoscape.use(cytoscapeDagre);
}

createApp({
  setup() {
    const tabs = [
      { id: 'root',      label: '📋 Root Task' },
      { id: 'arch',      label: 'Architecture' },
      { id: 'dag',       label: 'Task DAG' },
      { id: 'contracts', label: 'Contracts' },
      { id: 'notes',     label: 'Notes' },
      { id: 'roadmap',   label: 'Roadmap' },
      { id: 'timeline',  label: 'Timeline' },
      { id: 'sequence',  label: 'Sequence' },
    ];
    const activeTab = ref('root');
    const rootTitle = ref('');
    const rootTask = ref(null);
    const rootTaskForm = reactive({ title: '', description: '' });
    const rootTaskSaving = ref(false);
    const c4Data = reactive({ levels: {} });
    const c4Level = ref('');
    const c4Loading = ref(false);
    const dagData = reactive({ nodes: [], edges: [] });
    const contracts = ref([]);
    const notes = reactive({ open: [], answered: [], resolved: [] });
    const roadmapData = ref(null);
    const execOrder = ref(null);
    const seqDiagram = ref('');
    const seqHtml = ref('');

    let cyArch = null;
    let cyDag = null;

    async function api(path) {
      const r = await fetch(path);
      return r.json();
    }

    async function loadRootTask() {
      const data = await api('/api/root-task');
      rootTask.value = data.task;
      if (data.task) {
        rootTaskForm.title = data.task.title || '';
        rootTaskForm.description = data.task.description || '';
        rootTitle.value = data.task.title || '';
      }
    }

    async function saveRootTask() {
      rootTaskSaving.value = true;
      try {
        const r = await fetch('/api/root-task', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            title: rootTaskForm.title,
            description: rootTaskForm.description
          })
        });
        const data = await r.json();
        if (data.task) {
          rootTask.value = data.task;
          rootTitle.value = data.task.title;
          // After creating, switch to roadmap tab
          if (activeTab.value === 'root') {
            switchTab('roadmap');
          }
        }
      } finally {
        rootTaskSaving.value = false;
      }
    }

    async function loadRoadmap() {
      const data = await api('/api/roadmap');
      rootTitle.value = data.root_title || '';
      roadmapData.value = data;
    }

    async function loadNotes() {
      const data = await api('/api/notes');
      rootTitle.value = data.task_title || rootTitle.value;
      notes.open = data.open || [];
      notes.answered = data.answered || [];
      notes.resolved = data.resolved || [];
    }

    async function loadContracts() {
      const data = await api('/api/contracts');
      contracts.value = data.contracts || [];
    }

    async function loadC4() {
      c4Loading.value = true;
      const data = await api('/api/c4');
      Object.assign(c4Data, data);
      const keys = Object.keys(data.levels || {});
      if (keys.length && !c4Level.value) c4Level.value = keys[0];
      c4Loading.value = false;
    }

    async function loadDag() {
      const data = await api('/api/dag');
      dagData.nodes = data.nodes || [];
      dagData.edges = data.edges || [];
    }

    async function loadExecOrder() {
      const data = await api('/api/execution-order');
      execOrder.value = data;
    }

    async function loadSequence() {
      const data = await api('/api/sequence');
      seqDiagram.value = data.mermaid || '';
      if (data.mermaid) {
        try {
          const { svg } = await mermaid.render('mermaid-seq', data.mermaid);
          seqHtml.value = svg;
        } catch(e) {
          const s = String(data.mermaid || '');
          seqHtml.value = '<pre>' + s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;') + '</pre>';
        }
      }
    }

    function buildCyArch(levelSlug) {
      const level = c4Data.levels[levelSlug];
      if (!level) return;
      nextTick(() => {
        const el = document.getElementById('cy-arch');
        if (!el) return;
        if (cyArch) { cyArch.destroy(); cyArch = null; }
        const STATUS_BG = { existing: '#438DD5', modified: '#ffc107', 'new': '#28a745' };
        const elements = [];
        (level.nodes || []).forEach(n => {
          elements.push({
            data: { id: n.id, label: n.label + (n.sublabel ? ' / ' + n.sublabel : ''),
                    color: STATUS_BG[n.status] || '#888' }
          });
        });
        (level.edges || []).forEach(e => {
          elements.push({
            data: {
              id: 'e_' + e.source + '_' + e.target,
              source: e.source, target: e.target, label: e.label || ''
            }
          });
        });
        cyArch = cytoscape({
          container: el, elements,
          style: [
            { selector: 'node', style: {
              'background-color': 'data(color)', label: 'data(label)',
              'text-valign': 'center', 'text-halign': 'center', color: '#fff',
              'font-size': '11px', width: 100, height: 60, shape: 'rectangle',
              'text-wrap': 'wrap', 'text-max-width': '90px'
            }},
            { selector: 'edge', style: {
              width: 1.5, 'line-color': '#999', 'target-arrow-color': '#999',
              'target-arrow-shape': 'triangle', 'curve-style': 'bezier',
              label: 'data(label)', 'font-size': '9px', 'text-rotation': 'autorotate'
            }},
          ],
          layout: { name: typeof cytoscapeDagre !== 'undefined' ? 'dagre' : 'breadthfirst',
                    rankDir: 'TB', padding: 30 }
        });
      });
    }

    function buildCyDag() {
      nextTick(() => {
        const el = document.getElementById('cy-dag');
        if (!el) return;
        if (cyDag) { cyDag.destroy(); cyDag = null; }
        const elements = [...dagData.nodes, ...dagData.edges];
        cyDag = cytoscape({
          container: el, elements,
          style: [
            { selector: 'node', style: {
              'background-color': 'data(color)', label: 'data(label)',
              'text-valign': 'center', 'text-halign': 'center', color: '#212529',
              'font-size': '11px', width: 120, height: 50, shape: 'roundrectangle',
              'text-wrap': 'wrap', 'text-max-width': '110px'
            }},
            { selector: 'edge', style: {
              width: 1.5, 'line-color': '#adb5bd', 'target-arrow-color': '#adb5bd',
              'target-arrow-shape': 'triangle', 'curve-style': 'bezier',
              label: 'data(label)', 'font-size': '9px', 'text-rotation': 'autorotate'
            }},
          ],
          layout: { name: typeof cytoscapeDagre !== 'undefined' ? 'dagre' : 'breadthfirst',
                    rankDir: 'TB', padding: 30 }
        });
      });
    }

    function switchTab(id) {
      activeTab.value = id;
      if (id === 'dag') loadDag().then(buildCyDag);
      if (id === 'timeline') loadExecOrder();
      if (id === 'sequence') loadSequence();
    }

    watch(c4Level, (slug) => {
      if (slug && activeTab.value === 'arch') buildCyArch(slug);
    });

    watch(activeTab, (id) => {
      if (id === 'arch') loadC4().then(() => buildCyArch(c4Level.value));
    });

    function contractBadgeClass(status) {
      const map = { proposed: 'badge-yellow', agreed: 'badge-blue',
        implemented: 'badge-green', verified: 'badge-green', void: 'badge-gray' };
      return map[status] || 'badge-gray';
    }

    function statusBadgeClass(status) {
      const map = { done: 'badge-green', in_progress: 'badge-blue', ready: 'badge-yellow',
        draft: 'badge-gray', blocked: 'badge-gray', archived: 'badge-gray' };
      return map[status] || 'badge-gray';
    }

    onMounted(async () => {
      mermaid.initialize({ startOnLoad: false, theme: 'neutral', securityLevel: 'loose' });
      await loadRootTask();
      if (rootTask.value) {
        // Root task exists — load data and show roadmap
        activeTab.value = 'roadmap';
        await Promise.all([loadRoadmap(), loadNotes(), loadContracts()]);
      } else {
        // No root task — show root task creation form
        activeTab.value = 'root';
      }
    });

    return {
      tabs, activeTab, rootTitle, rootTask, rootTaskForm, rootTaskSaving,
      c4Data, c4Level, c4Loading,
      contracts, notes, roadmapData, execOrder, seqDiagram, seqHtml,
      switchTab, saveRootTask, contractBadgeClass, statusBadgeClass,
    };
  }
}).mount('#app');
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Launcher
# ---------------------------------------------------------------------------


def start_dashboard(
    port: int = 6235,
    root_task_id: Optional[str] = None,
    repo_root: Optional[Path] = None,
    open_browser: bool = True,
) -> None:
    """Start the Dashboard UI web server.

    Blocks until Ctrl+C.
    """
    if repo_root is None:
        repo_root = find_project_root()
    repo_root = Path(repo_root)
    db_path = get_db_path(repo_root)

    resolved_task_id: Optional[str] = None
    root_title: str = ""

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        if root_task_id is not None:
            task = get_task(conn, root_task_id)
            if task is None:
                raise ValueError(f"Task '{root_task_id}' not found.")
            resolved_task_id = root_task_id
            root_title = task["title"]
        else:
            active = get_active_root(conn)
            if active is not None:
                resolved_task_id = active["id"]
                root_title = active["title"]
            # else: no root task — dashboard starts in "create task" mode
    finally:
        conn.close()

    # Patch class-level attributes (single-threaded server)
    DashboardHandler.db_path = db_path
    DashboardHandler.repo_root = repo_root
    DashboardHandler.root_task_id = resolved_task_id  # can be None
    DashboardHandler.root_title = root_title

    url = f"http://localhost:{port}"
    print(f"Dashboard: {url}")
    if resolved_task_id:
        print(f"Task: '{root_title}' ({resolved_task_id})")
    else:
        print("No active root task. Create one in the dashboard.")
    print("Ctrl+C to stop.")

    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=[url]).start()

    server = HTTPServer(("localhost", port), DashboardHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
