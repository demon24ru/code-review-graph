"""Questions UI — lightweight local web server for answering brainstorm notes.

Opens http://localhost:PORT with a single-page UI where users can review and
answer open questions/assumptions, with changes persisted to SQLite instantly.

Lifecycle:
  open → answered   (user writes answer in UI; LLM not yet involved)
  answered → resolved / rejected / deferred  (LLM validates in next session)
"""

from __future__ import annotations

import json
import sqlite3
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional

from .incremental import find_project_root, get_db_path
from .tasks import get_active_root, get_task, list_notes, update_note


class QuestionsHandler(BaseHTTPRequestHandler):
    db_path: Path
    root_task_id: str
    root_title: str

    def log_message(self, *args):  # type: ignore[override]
        pass  # suppress request logs

    def do_GET(self) -> None:
        if self.path == "/":
            self._serve_page()
        elif self.path == "/api/notes":
            self._serve_notes()
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        if self.path.startswith("/api/notes/"):
            self._handle_answer()
        else:
            self.send_error(404)

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _serve_notes(self) -> None:
        conn = self._get_conn()
        try:
            answered = list_notes(
                conn, task_id=self.root_task_id, status="answered", include_children=True
            )
            open_q = list_notes(
                conn,
                task_id=self.root_task_id,
                status="open",
                note_type="question",
                include_children=True,
            )
            open_a = list_notes(
                conn,
                task_id=self.root_task_id,
                status="open",
                note_type="assumption",
                include_children=True,
            )
            open_notes = open_q + open_a
            resolved = list_notes(
                conn, task_id=self.root_task_id, status="resolved", include_children=True
            )
            rejected = list_notes(
                conn, task_id=self.root_task_id, status="rejected", include_children=True
            )

            # Enrich notes with task metadata
            all_notes = answered + open_notes + resolved + rejected
            task_ids = list({n["task_id"] for n in all_notes})
            if task_ids:
                ph = ",".join("?" * len(task_ids))
                rows = conn.execute(
                    f"SELECT id, title, description, parent_id FROM tasks WHERE id IN ({ph})",
                    task_ids,
                ).fetchall()
                tasks_map = {r["id"]: dict(r) for r in rows}
                parent_ids = [t["parent_id"] for t in tasks_map.values() if t.get("parent_id")]
                if parent_ids:
                    pph = ",".join("?" * len(parent_ids))
                    parent_rows = conn.execute(
                        f"SELECT id, title FROM tasks WHERE id IN ({pph})", parent_ids
                    ).fetchall()
                    parents_map = {r["id"]: r["title"] for r in parent_rows}
                else:
                    parents_map = {}
                for t in tasks_map.values():
                    t["parent_title"] = parents_map.get(t.get("parent_id", ""), None)
            else:
                tasks_map = {}

            for n in all_notes:
                t = tasks_map.get(n["task_id"], {})
                n["_task_title"] = t.get("title", n["task_id"])
                n["_task_desc"] = t.get("description") or ""
                n["_parent_title"] = t.get("parent_title") or ""

            payload = {
                "task_title": self.root_title,
                "answered": answered,
                "open": open_notes,
                "resolved": resolved + rejected,
            }
        finally:
            conn.close()
        self._json_response(payload)

    def _handle_answer(self) -> None:
        note_id = self.path.split("/")[-1]
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        conn = self._get_conn()
        try:
            update_note(
                conn,
                note_id=note_id,
                status="answered",
                resolution=body.get("resolution", ""),
                rationale=body.get("rationale"),
            )
            conn.commit()
        finally:
            conn.close()
        self._json_response({"ok": True})

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


_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Brainstorm Questions</title>
<style>
  :root { --blue: #3b82f6; --amber: #f59e0b; --green: #22c55e; --red: #ef4444; --gray: #6b7280; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: system-ui, sans-serif; max-width: 800px; margin: 0 auto; padding: 16px 20px; color: #111; }
  h1 { font-size: 1.25rem; margin-bottom: 4px; }
  .subtitle { color: var(--gray); font-size: 0.875rem; margin-bottom: 20px; }

  .section-header { font-size: 0.9rem; font-weight: 600; color: var(--gray);
                    text-transform: uppercase; letter-spacing: 0.05em; margin: 20px 0 8px; }

  .card { border: 1px solid #e5e7eb; border-radius: 8px; padding: 14px 16px; margin-bottom: 10px; }
  .card.question { border-left: 4px solid var(--blue); }
  .card.assumption { border-left: 4px solid var(--amber); }
  .card.answered-card { border-left: 4px solid var(--green); background: #f0fdf4; }

  .card-content { font-size: 0.95rem; font-weight: 500; margin-bottom: 8px; }

  .task-ctx-details { margin-bottom: 10px; }
  .task-ctx-details summary { cursor: pointer; list-style: none; }
  .task-ctx-details summary::-webkit-details-marker { display: none; }
  .task-ctx { font-size: 0.8rem; color: var(--gray); }
  .task-ctx:hover { color: #374151; }
  .task-desc { font-size: 0.8rem; color: #374151; padding: 6px 0 4px 0; line-height: 1.4; }
  .task-parent { font-size: 0.75rem; color: var(--gray); padding-bottom: 4px; }

  .alts { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }
  .alt-btn { padding: 4px 12px; border-radius: 20px; border: 1px solid var(--blue);
             color: var(--blue); background: white; cursor: pointer; font-size: 0.875rem; }
  .alt-btn:hover { background: #eff6ff; }
  .alt-btn.selected { background: var(--blue); color: white; border-color: var(--blue); }

  textarea { width: 100%; min-height: 64px; border: 1px solid #d1d5db; border-radius: 6px;
             padding: 8px; font-size: 0.875rem; resize: vertical; font-family: inherit; }
  textarea:focus { outline: none; border-color: var(--blue); box-shadow: 0 0 0 2px #bfdbfe; }

  .actions { display: flex; gap: 8px; margin-top: 8px; }
  .btn { padding: 6px 16px; border-radius: 6px; border: none; cursor: pointer; font-size: 0.875rem; font-weight: 500; }
  .btn-primary { background: var(--blue); color: white; }
  .btn-primary:hover { background: #2563eb; }
  .btn-confirm { background: var(--green); color: white; }
  .btn-reject { background: white; color: var(--red); border: 1px solid var(--red); }
  .btn-edit { background: white; color: var(--gray); border: 1px solid #d1d5db; font-size: 0.8rem; }

  .answered-resolution { font-style: italic; color: #374151; margin-bottom: 6px; font-size: 0.875rem; }

  details.resolved-section > summary { cursor: pointer; font-size: 0.9rem; font-weight: 600;
    color: var(--gray); text-transform: uppercase; letter-spacing: 0.05em; padding: 4px 0; }
  .resolved-card { opacity: 0.7; border-left: 4px solid var(--gray); }
  .resolved-resolution { font-size: 0.8rem; color: var(--gray); margin-top: 4px; }

  .empty { color: var(--gray); font-size: 0.875rem; padding: 12px 0; }
  .badge { display: inline-block; border-radius: 20px; padding: 2px 10px; font-size: 0.75rem;
           font-weight: 600; margin-left: 6px; }
  .badge-blue { background: #dbeafe; color: #1d4ed8; }
  .badge-green { background: #dcfce7; color: #166534; }
  .badge-amber { background: #fef9c3; color: #92400e; }
  .pulse { animation: pulse 2s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.6} }
</style>
</head>
<body>
<div id="app"><p style="padding:40px;color:#6b7280">Loading...</p></div>
<script>
  let data = null;

  // Capture UI state before re-render so auto-refresh doesn't lose user input
  function captureState() {
    const state = {
      textareas: {},      // id → value
      altSelected: {},    // alts container id → [selected data-alt values]
      detailsOpen: {},    // details element id → open bool
      editOpen: {},       // note id → bool (edit panel visible)
      resolvedOpen: false,
    };
    // Textareas
    document.querySelectorAll('textarea[id]').forEach(el => {
      if (el.value) state.textareas[el.id] = el.value;
    });
    // Selected alt buttons
    document.querySelectorAll('.alts[id]').forEach(container => {
      const sel = Array.from(container.querySelectorAll('.alt-btn.selected'))
                       .map(b => b.dataset.alt);
      if (sel.length) state.altSelected[container.id] = sel;
    });
    // Open <details> elements with an id (task-ctx-details)
    document.querySelectorAll('details[id]').forEach(el => {
      state.detailsOpen[el.id] = el.open;
    });
    // Resolved/Rejected section (the outer <details> has no id, use a sentinel)
    const resolvedDetails = document.querySelector('details.resolved-section');
    if (resolvedDetails) state.resolvedOpen = resolvedDetails.open;
    // Edit panels
    document.querySelectorAll('[id^="aedit-"]').forEach(el => {
      if (el.style.display !== 'none') {
        const noteId = el.id.replace('aedit-', '');
        state.editOpen[noteId] = true;
      }
    });
    return state;
  }

  function restoreState(state) {
    // Textareas
    Object.entries(state.textareas).forEach(([id, val]) => {
      const el = document.getElementById(id);
      if (el && !el.value) el.value = val;
    });
    // Alt buttons
    Object.entries(state.altSelected).forEach(([cid, alts]) => {
      const container = document.getElementById(cid);
      if (!container) return;
      container.querySelectorAll('.alt-btn').forEach(btn => {
        if (alts.includes(btn.dataset.alt)) btn.classList.add('selected');
      });
    });
    // Open details
    Object.entries(state.detailsOpen).forEach(([id, open]) => {
      const el = document.getElementById(id);
      if (el && open) el.open = true;
    });
    // Resolved section
    if (state.resolvedOpen) {
      const el = document.querySelector('details.resolved-section');
      if (el) el.open = true;
    }
    // Edit panels
    Object.keys(state.editOpen).forEach(noteId => {
      startEdit(noteId);
    });
  }

  async function load() {
    const r = await fetch('/api/notes');
    const fresh = await r.json();
    // Skip re-render if data hash unchanged (avoids flicker when nothing changed)
    const freshHash = JSON.stringify(fresh);
    if (data && JSON.stringify(data) === freshHash) return;
    const state = captureState();
    data = fresh;
    render();
    restoreState(state);
  }

  async function answer(noteId, resolution, status) {
    await fetch('/api/notes/' + noteId, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({resolution, status: status || 'answered'})
    });
    data = null; // force re-render
    load();
  }

  function esc(s) {
    return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  function taskCtx(n) {
    const title = esc(n._task_title || n.task_id);
    const desc = esc(n._task_desc || '');
    const parent = esc(n._parent_title || '');
    const inner = (desc ? '<div class="task-desc">' + desc + '</div>' : '')
      + (parent ? '<div class="task-parent">&#x2191; ' + parent + '</div>' : '');
    if (!inner) return '<div class="task-ctx">Task: ' + title + '</div>';
    const did = 'tctx-' + (n.id || '').replace(/-/g,'');
    return '<details id="' + did + '" class="task-ctx-details"><summary class="task-ctx">&#x25B8; Task: ' + title + '</summary>' + inner + '</details>';
  }

  function renderNote(n) {
    const isA = n.note_type === 'assumption';
    const icon = isA ? '🔍' : '❓';
    const nid = n.id.replace(/-/g, '');
    const alts = (n.alternatives || '').split(',').map(s => s.trim()).filter(Boolean);

    const altBtns = alts.length
      ? '<div class="alts" id="alts-' + nid + '">'
        + alts.map(a =>
            '<button class="alt-btn" data-alt="' + esc(a) + '" onclick="toggleAlt(this, \\'' + nid + '\\')">'
            + esc(a) + '</button>'
          ).join('')
        + '</div>'
      : '';

    const tid = 'ta-' + nid;

    if (isA) {
      return '<div class="card assumption">'
        + '<div class="card-content">' + icon + ' ' + esc(n.content) + '</div>'
        + taskCtx(n)
        + '<textarea id="' + tid + '" placeholder="Add a comment (optional)..." style="min-height:48px;margin-bottom:8px"></textarea>'
        + '<div class="actions">'
        + '<button class="btn btn-confirm" onclick="submitWithAlts(\\'' + n.id + '\\', \\'' + nid + '\\', \\'' + tid + '\\', \\'Confirmed\\')">&#x2705; Confirmed</button>'
        + '<button class="btn btn-reject" onclick="submitWithAlts(\\'' + n.id + '\\', \\'' + nid + '\\', \\'' + tid + '\\', \\'Not correct\\')">&#x274C; Not correct</button>'
        + '</div></div>';
    }

    return '<div class="card question">'
      + '<div class="card-content">' + icon + ' ' + esc(n.content) + '</div>'
      + taskCtx(n)
      + altBtns
      + '<textarea id="' + tid + '" placeholder="Your answer or additional comments..."></textarea>'
      + '<div class="actions">'
      + '<button class="btn btn-primary" onclick="submitWithAlts(\\'' + n.id + '\\', \\'' + nid + '\\', \\'' + tid + '\\', \\'\\')">Answer &#x2192;</button>'
      + '</div></div>';
  }

  function renderAnswered(n) {
    const icon = n.note_type === 'assumption' ? '🔍' : '❓';
    const eid = 'edit-' + n.id;
    return '<div class="card answered-card" id="acard-' + n.id + '">'
      + '<div class="card-content">' + icon + ' ' + esc(n.content) + '</div>'
      + taskCtx(n)
      + '<div class="answered-resolution" id="ares-' + n.id + '">&#x1F4AC; ' + esc(n.resolution || '') + '</div>'
      + '<div id="aedit-' + n.id + '" style="display:none">'
      +   '<textarea id="' + eid + '" style="width:100%;min-height:60px;margin-top:6px">' + esc(n.resolution || '') + '</textarea>'
      +   '<div class="actions" style="margin-top:6px">'
      +     '<button class="btn btn-primary" onclick="saveEdit(\\'' + n.id + '\\', \\'' + eid + '\\')">Save</button>'
      +     '<button class="btn btn-edit" onclick="cancelEdit(\\'' + n.id + '\\')">Cancel</button>'
      +   '</div>'
      + '</div>'
      + '<div class="actions"><button class="btn btn-edit" id="ebtn-' + n.id + '" onclick="startEdit(\\'' + n.id + '\\')">&#x270F;&#xFE0F; Edit</button></div>'
      + '</div>';
  }

  function renderResolved(n) {
    return '<div class="card resolved-card">'
      + '<div class="card-content">' + esc(n.content) + '</div>'
      + (n.resolution ? '<div class="resolved-resolution">&#x2192; ' + esc(n.resolution) + '</div>' : '')
      + '</div>';
  }

  function render() {
    const {task_title, answered, open, resolved} = data;
    const app = document.getElementById('app');

    let html = '<h1>&#x1F535; ' + esc(task_title) + '</h1>';
    html += '<div class="subtitle">Brainstorm Q&amp;A &#x2014; answers persist to SQLite; LLM validates on next session</div>';

    if (answered.length > 0) {
      html += '<div class="section-header pulse">&#x23F3; Awaiting LLM Validation <span class="badge badge-green">' + answered.length + '</span></div>';
      html += answered.map(renderAnswered).join('');
    }

    if (open.length > 0) {
      html += '<div class="section-header">&#x2753; Open Questions <span class="badge badge-blue">' + open.length + '</span></div>';
      html += open.map(renderNote).join('');
    } else {
      html += '<div class="section-header">&#x2753; Open Questions</div><div class="empty">No open questions &#x2014; all answered &#x2705;</div>';
    }

    if (resolved.length > 0) {
      html += '<details class="resolved-section"><summary>&#x2705; Resolved / Rejected <span class="badge badge-amber">' + resolved.length + '</span></summary>';
      html += resolved.map(renderResolved).join('');
      html += '</details>';
    }

    app.innerHTML = html;
  }

  function toggleAlt(btn, nid) {
    btn.classList.toggle('selected');
  }

  function submitWithAlts(noteId, nid, textareaId, prefix) {
    const altsContainer = document.getElementById('alts-' + nid);
    const selected = altsContainer
      ? Array.from(altsContainer.querySelectorAll('.alt-btn.selected')).map(b => b.dataset.alt)
      : [];
    const freeText = document.getElementById(textareaId)
      ? document.getElementById(textareaId).value.trim()
      : '';

    let parts = [];
    if (prefix) parts.push(prefix);
    if (selected.length) parts.push(selected.join(', '));
    if (freeText) parts.push(freeText);
    const resolution = parts.join(' \u2014 ');

    if (!resolution.trim()) { alert('Please select an option or write an answer.'); return; }
    answer(noteId, resolution);
  }

  function startEdit(noteId) {
    document.getElementById('ares-' + noteId).style.display = 'none';
    document.getElementById('aedit-' + noteId).style.display = 'block';
    document.getElementById('ebtn-' + noteId).style.display = 'none';
  }

  function cancelEdit(noteId) {
    document.getElementById('ares-' + noteId).style.display = '';
    document.getElementById('aedit-' + noteId).style.display = 'none';
    document.getElementById('ebtn-' + noteId).style.display = '';
  }

  async function saveEdit(noteId, textareaId) {
    const val = document.getElementById(textareaId).value.trim();
    if (!val) return;
    await fetch('/api/notes/' + noteId, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({resolution: val})
    });
    cancelEdit(noteId);
    data = null; // force re-render on next load
    load();
  }

  load();
  setInterval(load, 5000);
</script>
</body>
</html>"""


def serve(
    root_task_id: Optional[str] = None,
    repo_root: Optional[Path] = None,
    port: int = 6234,
    open_browser: bool = True,
) -> None:
    """Start the Questions UI web server.

    Blocks until Ctrl+C.
    """
    if repo_root is None:
        repo_root = find_project_root()
    db_path = get_db_path(repo_root)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        if root_task_id is None:
            active = get_active_root(conn)
            if active is None:
                raise ValueError("No active root task. Create a task first, or pass --task ID.")
            resolved_task_id: str = active["id"]
            root_title: str = active["title"]
        else:
            task = get_task(conn, root_task_id)
            if task is None:
                raise ValueError(f"Task '{root_task_id}' not found.")
            resolved_task_id = root_task_id
            root_title = task["title"]
    finally:
        conn.close()

    # Patch class attributes (single-threaded server, so safe)
    QuestionsHandler.db_path = db_path
    QuestionsHandler.root_task_id = resolved_task_id
    QuestionsHandler.root_title = root_title

    url = f"http://localhost:{port}"
    print(f"Questions UI: {url}")
    print(f"Brainstorm: '{root_title}' ({root_task_id})")
    print("Ctrl+C to stop. Answers saved automatically to SQLite.")
    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=[url]).start()

    server = HTTPServer(("localhost", port), QuestionsHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
