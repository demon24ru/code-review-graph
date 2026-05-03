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

from .graph import GraphStore
from .incremental import find_project_root, get_db_path
from .task_analysis import blast_radius, execution_order, roadmap
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
            flow_id_str = qs.get("flow_id", [None])[0]
            flow_id = int(flow_id_str) if flow_id_str else None
            self._serve_sequence(task_id=task_id, flow_name=flow_name, flow_id=flow_id)
        elif path == "/api/sequence-flows":
            self._serve_sequence_flows()
        elif path == "/api/execution-order":
            self._serve_execution_order()
        elif path == "/api/timeline":
            self._serve_timeline()
        elif path == "/api/c4/node-details":
            qs = parse_qs(parsed.query)
            node_id = qs.get("id", [""])[0]
            self._serve_c4_node_details(node_id)
        elif path == "/api/c4/task-refs":
            qs = parse_qs(parsed.query)
            node_id = qs.get("id", [""])[0]
            self._serve_c4_task_refs(node_id)
        elif path == "/api/dag/task-detail":
            qs = parse_qs(parsed.query)
            task_id = qs.get("id", [""])[0]
            self._serve_dag_task_detail(task_id)
        elif path == "/api/dataflow/sources":
            self._serve_dataflow_sources()
        elif path == "/api/dataflow":
            self._serve_dataflow()
        elif path == "/api/leaf-tasks":
            self._serve_leaf_tasks()
        elif path == "/api/blast-radius":
            qs = parse_qs(parsed.query)
            task_id = qs.get("task_id", [None])[0]
            try:
                depth = int(qs.get("depth", ["2"])[0])
            except (ValueError, TypeError):
                depth = 2
            self._serve_blast_radius(task_id=task_id, depth=depth)
        elif path == "/api/c4/zoom/containers":
            self._serve_c4_zoom_containers()
        elif path == "/api/c4/zoom/components":
            qs = parse_qs(parsed.query)
            container_id = qs.get("container_id", [""])[0]
            self._serve_c4_zoom_components(container_id)
        elif path == "/api/c4/zoom/code":
            qs = parse_qs(parsed.query)
            component = qs.get("component", [""])[0]
            self._serve_c4_zoom_code(component)
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/root-task":
            self._handle_save_root_task()
        elif path == "/api/annotations":
            self._handle_create_annotation()
        elif path.startswith("/api/contracts/") and path.endswith("/comment"):
            parts = path.split("/")
            # /api/contracts/<id>/comment → parts=['','api','contracts','<id>','comment']
            contract_id = parts[3] if len(parts) >= 5 else ""
            self._handle_contract_comment(contract_id)
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
        body = _PAGE_HTML.read_bytes()
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
    # GET /api/c4/node-details
    # ------------------------------------------------------------------

    def _serve_c4_node_details(self, node_id: str) -> None:
        """GET /api/c4/node-details?id=XXXX — node metadata + annotations."""
        if not node_id:
            self._json_response({"node": {}, "annotations": [], "annotation_count": 0})
            return

        # Collect annotations for this C4 element
        annotations: list[dict[str, Any]] = []
        if self.root_task_id is not None:
            conn = self._get_conn()
            try:
                all_notes = list_notes(conn, task_id=self.root_task_id, include_children=True)
                for note in all_notes:
                    if note.get("c4_element_id") == node_id:
                        annotations.append(dict(note))
            finally:
                conn.close()

        # Try to look up node metadata from C4 architecture file
        node_data: dict[str, Any] = {"id": node_id}
        if _C4_AVAILABLE:
            c4_path = get_c4_path(str(self.repo_root))
            if c4_path.exists():
                try:
                    arch = parse_c4_file(c4_path.read_text(encoding="utf-8"))
                    found = False
                    for diagram in arch.diagrams:
                        nodes = resolve_for_render(arch, diagram.title)
                        for n in nodes:
                            if n.get("id") == node_id:
                                node_data = n
                                found = True
                                break
                        if found:
                            break
                except Exception:
                    pass

        self._json_response({
            "node": node_data,
            "annotations": annotations,
            "annotation_count": len(annotations),
        })

    # ------------------------------------------------------------------
    # GET /api/c4/task-refs
    # ------------------------------------------------------------------

    def _serve_c4_task_refs(self, node_id: str) -> None:
        """GET /api/c4/task-refs?id=XXXX — tasks and contracts linked to a C4 element."""
        if not node_id or self.root_task_id is None:
            self._json_response({"tasks": [], "contracts": []})
            return

        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT DISTINCT task_id FROM notes WHERE c4_element_id = ? AND task_id IS NOT NULL",
                (node_id,),
            ).fetchall()
            tasks: list[dict[str, Any]] = []
            for row in rows:
                tid = row[0]
                try:
                    t = get_task(conn, tid)
                    if t is not None:
                        tasks.append({"id": tid, "title": t.get("title", tid), "status": t.get("status", "draft")})
                except Exception:
                    pass
            contracts = list_contracts(conn, scope_task_id=self.root_task_id)
        finally:
            conn.close()

        self._json_response({"tasks": tasks, "contracts": [dict(c) for c in contracts]})

    # ------------------------------------------------------------------
    # GET /api/dag/task-detail
    # ------------------------------------------------------------------

    def _serve_dag_task_detail(self, task_id: str) -> None:
        """GET /api/dag/task-detail?id=XXXX — full task detail for DAG detail panel."""
        if not task_id:
            self._json_response({"task": None, "notes": [], "edges": [], "code_refs": []})
            return
        conn = self._get_conn()
        try:
            try:
                task = get_task(conn, task_id)
            except (KeyError, Exception):
                task = None
            if task is None:
                self._json_response({"task": None, "notes": [], "edges": [], "code_refs": []})
                return
            # Notes for this task only (no parent notes)
            notes_rows = list_notes(conn, task_id=task_id, include_parent=False, include_children=False)
            notes = [dict(n) for n in notes_rows]
            # Edges involving this task — JOIN with tasks to get titles
            edge_rows = conn.execute(
                "SELECT te.source_task_id, te.target_task_id, te.type, "
                "ts.title AS source_title, tt.title AS target_title "
                "FROM task_edges te "
                "LEFT JOIN tasks ts ON ts.id = te.source_task_id "
                "LEFT JOIN tasks tt ON tt.id = te.target_task_id "
                "WHERE te.source_task_id = ? OR te.target_task_id = ?",
                (task_id, task_id),
            ).fetchall()
            edges = [
                {
                    "source": r["source_task_id"],
                    "target": r["target_task_id"],
                    "source_title": r["source_title"] or r["source_task_id"],
                    "target_title": r["target_title"] or r["target_task_id"],
                    "type": r["type"],
                    "direction": "outgoing" if r["source_task_id"] == task_id else "incoming",
                }
                for r in edge_rows
            ]
            # Code refs for this task (JOIN with nodes to get qualified_name)
            code_ref_rows = conn.execute(
                "SELECT tcr.ref_type, COALESCE(n.qualified_name, tcr.code_node_id) as qualified_name, "
                "tcr.description, n.name "
                "FROM task_code_refs tcr "
                "LEFT JOIN nodes n ON n.id = tcr.code_node_id "
                "WHERE tcr.task_id = ?",
                (task_id,),
            ).fetchall()
            code_refs = [
                {
                    "ref_type": r["ref_type"],
                    "qualified_name": str(r["qualified_name"]) if r["qualified_name"] else "",
                    "description": r["description"],
                }
                for r in code_ref_rows
            ]
        finally:
            conn.close()
        self._json_response({
            "task": dict(task),
            "notes": notes,
            "edges": edges,
            "code_refs": code_refs,
        })

    # ------------------------------------------------------------------
    # POST /api/contracts/:id/comment
    # ------------------------------------------------------------------

    def _handle_contract_comment(self, contract_id: str) -> None:
        """POST /api/contracts/:id/comment — add a note (comment) to a contract."""
        body = self._read_body()
        user_text = body.get("content", "").strip()
        if not user_text or not contract_id:
            self.send_error(400, "content and contract_id required")
            return
        conn = self._get_conn()
        try:
            # Look up contract to get scope_task_id and name
            contract_row = conn.execute(
                "SELECT scope_task_id, name FROM contracts WHERE id = ?",
                (contract_id,),
            ).fetchone()
            if contract_row is None:
                self.send_error(404, "contract not found")
                return
            task_id = contract_row["scope_task_id"] or self.root_task_id
            if not task_id:
                self.send_error(400, "no task for contract")
                return
            contract_name = contract_row["name"] or f"Contract {contract_id}"
            result = add_note(
                conn,
                task_id=task_id,
                notes=[{
                    "note_type": "question",
                    "content": contract_name,       # source title
                    "status": "answered",
                    "resolution": user_text,        # user's comment text
                    "c4_element_id": f"contract_{contract_id}",
                }],
            )
            conn.commit()
        finally:
            conn.close()
        created = result.get("notes", [{}])
        note_id = created[0].get("id") if created else None
        self._json_response({"status": "ok", "note_id": note_id})


    # ------------------------------------------------------------------
    # GET /api/leaf-tasks
    # ------------------------------------------------------------------

    def _serve_leaf_tasks(self) -> None:
        """GET /api/leaf-tasks — return leaf tasks (no children) under root."""
        if self.root_task_id is None:
            self._json_response({"tasks": []})
            return
        conn = self._get_conn()
        try:
            dag = get_task_dag(conn, self.root_task_id, compact=True)
            nodes = dag.get("nodes", [])
            all_ids = {n["id"] for n in nodes}
            parent_ids = {n["parent_id"] for n in nodes if n.get("parent_id")}
            leaf_ids = all_ids - parent_ids
            leaf_tasks = [
                {"id": n["id"], "title": n.get("title", n["id"]), "status": n.get("status", "draft")}
                for n in nodes
                if n["id"] in leaf_ids
            ]
            self._json_response({"tasks": leaf_tasks})
        except Exception as exc:
            self._json_response({"tasks": [], "error": str(exc)})
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # GET /api/blast-radius
    # ------------------------------------------------------------------

    def _serve_blast_radius(self, task_id: Optional[str] = None, depth: int = 2) -> None:
        """GET /api/blast-radius?task_id=X&depth=N — blast radius overlay data."""
        if not task_id or self.root_task_id is None:
            self._json_response({
                "status": "no_task",
                "task_id": task_id or "",
                "direct_nodes": [],
                "affected_nodes": [],
                "uncovered_nodes": [],
                "coverage_ratio": 0,
                "covered_count": 0,
                "total_affected": 0,
            })
            return
        conn = self._get_conn()
        try:
            result = blast_radius(conn, task_id, depth=depth, include_affected_nodes=True)
            if result.get("status") == "not_applicable":
                self._json_response({
                    "status": "not_applicable",
                    "task_id": task_id,
                    "direct_nodes": [],
                    "affected_nodes": [],
                    "uncovered_nodes": [],
                    "coverage_ratio": 0,
                    "covered_count": 0,
                    "total_affected": 0,
                })
                return

            direct_nodes = result.get("direct_nodes", [])
            all_affected = result.get("affected_nodes", [])
            uncovered_nodes = result.get("uncovered_nodes", [])
            coverage_ratio = float(result.get("coverage_ratio", 0.0) or 0.0)

            # BFS from direct node qualified names to compute depth per affected node
            direct_qnames: set[str] = {
                n.get("qualified_name", "") for n in direct_nodes if n.get("qualified_name")
            }
            depth_map: dict[str, int] = {qn: 0 for qn in direct_qnames}
            frontier: list[str] = list(direct_qnames)
            visited: set[str] = set(direct_qnames)

            for bfs_level in range(1, depth + 1):
                next_frontier: list[str] = []
                for qn in frontier:
                    try:
                        rows = conn.execute(
                            "SELECT target_qualified FROM edges "
                            "WHERE source_qualified = ? AND kind IN ('CALLS', 'IMPORTS_FROM')",
                            (qn,),
                        ).fetchall()
                        for row in rows:
                            target = row[0]
                            if target and target not in visited:
                                visited.add(target)
                                depth_map[target] = bfs_level
                                next_frontier.append(target)
                    except Exception:
                        pass
                frontier = next_frontier

            # Build affected_nodes list with bfs_depth (exclude direct nodes)
            direct_id_set: set[Any] = {n.get("id") for n in direct_nodes if n.get("id")}
            affected_with_depth: list[dict[str, Any]] = []
            for node in all_affected:
                qn = node.get("qualified_name", "")
                node_id = node.get("id")
                if (qn and qn in direct_qnames) or (node_id and node_id in direct_id_set):
                    continue
                bfs_d = depth_map.get(qn, depth) if qn else depth
                affected_with_depth.append({
                    "id": node_id,
                    "name": node.get("name", ""),
                    "qualified_name": qn,
                    "file_path": node.get("file_path", ""),
                    "bfs_depth": bfs_d,
                })

            total_affected = int(result.get("affected_nodes_count", len(all_affected)))
            covered_count = max(0, total_affected - len(uncovered_nodes))

            self._json_response({
                "status": "ok",
                "task_id": task_id,
                "direct_nodes": [
                    {
                        "id": n.get("id"),
                        "name": n.get("name", ""),
                        "qualified_name": n.get("qualified_name", ""),
                        "file_path": n.get("file_path", ""),
                    }
                    for n in direct_nodes
                ],
                "affected_nodes": affected_with_depth,
                "uncovered_nodes": [
                    {
                        "id": n.get("id"),
                        "name": n.get("name", ""),
                        "qualified_name": n.get("qualified_name", ""),
                    }
                    for n in uncovered_nodes
                ],
                "coverage_ratio": coverage_ratio,
                "covered_count": covered_count,
                "total_affected": total_affected,
            })
        except Exception as exc:
            self._json_response({
                "status": "error",
                "error": str(exc),
                "task_id": task_id,
                "direct_nodes": [],
                "affected_nodes": [],
                "uncovered_nodes": [],
                "coverage_ratio": 0,
                "covered_count": 0,
                "total_affected": 0,
            })
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # GET /api/c4/zoom/containers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_test_path(file_path: str, name: str = "") -> bool:
        """Return True if a node belongs to test code (should be excluded from arch views)."""
        fp = (file_path or "").replace("\\", "/").lower()
        nm = (name or "").lower()
        return (
            "/test" in fp
            or fp.startswith("test")
            or nm.startswith("test_")
            or nm == "tests"
        )

    def _serve_c4_zoom_containers(self) -> None:
        """GET /api/c4/zoom/containers — L2 level: communities as containers."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT community_id, COUNT(*) as size, MAX(language) as language, "
                "MAX(name) as sample_name "
                "FROM nodes WHERE community_id IS NOT NULL GROUP BY community_id"
            ).fetchall()
            containers = []
            for r in rows:
                # Use sample_name to detect test communities
                sample = (r["sample_name"] or "").lower()
                if "test" in sample:
                    # Check if majority of nodes are test nodes — skip entirely
                    continue
                containers.append({
                    "id": f"community_{r['community_id']}",
                    "name": f"Community {r['community_id']}",
                    "description": f"{r['size']} nodes \u2022 {r['language'] or 'Unknown'}",
                    "size": r["size"],
                    "language": r["language"] or "Unknown",
                    "community_id": r["community_id"],
                    "has_children": r["size"] > 0,
                })
            self._json_response({"containers": containers})
        except Exception:
            self._json_response({"containers": []})
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # GET /api/c4/zoom/components?container_id=42
    # ------------------------------------------------------------------

    def _serve_c4_zoom_components(self, container_id: str) -> None:
        """GET /api/c4/zoom/components — L3 level: Classes and Files within a community."""
        try:
            community_id = int(container_id)
        except (ValueError, TypeError):
            self._json_response({"container_id": f"community_{container_id}", "components": []})
            return
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT id, name, qualified_name, kind, file_path "
                "FROM nodes WHERE community_id = ? AND kind IN ('Class', 'File') "
                "ORDER BY kind, name",
                (community_id,),
            ).fetchall()
            components = [
                {
                    "id": f"qname::{r['qualified_name'] or r['name']}",
                    "name": r["name"],
                    "kind": r["kind"],
                    "file_path": r["file_path"] or "",
                    "qualified_name": r["qualified_name"] or "",
                    "node_id": r["id"],
                }
                for r in rows
                if not self._is_test_path(r["file_path"] or "", r["name"] or "")
            ]
            self._json_response({
                "container_id": f"community_{community_id}",
                "components": components,
            })
        except Exception:
            self._json_response({"container_id": f"community_{community_id}", "components": []})
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # GET /api/c4/zoom/code?component=<qualified_name>
    # ------------------------------------------------------------------

    def _serve_c4_zoom_code(self, component: str) -> None:
        """GET /api/c4/zoom/code — L4 level: Functions/Methods inside a Class or File."""
        if not component:
            self._json_response({"component": "", "nodes": []})
            return
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT id, name, qualified_name, kind, file_path, line_start, line_end, signature "
                "FROM nodes WHERE qualified_name LIKE ? AND kind IN ('Function', 'Method') "
                "ORDER BY line_start",
                (f"{component}::%",),
            ).fetchall()
            nodes = [
                {
                    "id": f"qname::{r['qualified_name'] or r['name']}",
                    "name": r["name"],
                    "kind": r["kind"],
                    "qualified_name": r["qualified_name"] or "",
                    "node_id": r["id"],
                    "line_start": r["line_start"],
                    "line_end": r["line_end"],
                    "signature": r["signature"] or "",
                }
                for r in rows
                if not self._is_test_path(r["file_path"] or "", r["name"] or "")
            ]
            self._json_response({"component": component, "nodes": nodes})
        except Exception:
            self._json_response({"component": component, "nodes": []})
        finally:
            conn.close()

    def _serve_dag(self) -> None:
        if self.root_task_id is None:
            self._json_response({"nodes": [], "edges": []})
            return
        conn = self._get_conn()
        try:
            dag = get_task_dag(conn, self.root_task_id, compact=False)
            # Count notes per task in a single query (same conn, before close)
            task_ids = [n["id"] for n in dag.get("nodes", [])]
            anno_counts: dict[str, int] = {}
            if task_ids:
                placeholders = ",".join("?" * len(task_ids))
                rows = conn.execute(
                    f"SELECT task_id, COUNT(*) as cnt FROM notes WHERE task_id IN ({placeholders}) GROUP BY task_id",
                    task_ids,
                ).fetchall()
                for row in rows:
                    anno_counts[row["task_id"]] = row["cnt"]
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
                    "annotationCount": anno_counts.get(node["id"], 0),
                }
            })
        edges = []
        for edge in dag.get("edges", []):
            src = edge.get("source_task_id") or edge.get("source_id", "")
            tgt = edge.get("target_task_id") or edge.get("target_id", "")
            etype = edge.get("edge_type") or edge.get("type", "")
            if not src or not tgt:
                continue
            if etype == "parent_child":
                continue
            edges.append({
                "data": {
                    "id": f"{src}__{tgt}__{etype}",
                    "source": src,
                    "target": tgt,
                    "label": etype,
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
            enriched = []
            for c in contracts:
                cd = dict(c)
                providers = []
                for pid in cd.get("provider_task_ids") or []:
                    try:
                        t = get_task(conn, pid)
                        providers.append({
                            "id": pid,
                            "title": t.get("title", pid),
                            "status": t.get("status", "draft"),
                        })
                    except Exception:
                        providers.append({"id": pid, "title": pid, "status": "unknown"})
                cd["providers"] = providers
                consumers = []
                for cid_val in cd.get("consumer_task_ids") or []:
                    try:
                        t = get_task(conn, cid_val)
                        consumers.append({
                            "id": cid_val,
                            "title": t.get("title", cid_val),
                            "status": t.get("status", "draft"),
                        })
                    except Exception:
                        consumers.append({"id": cid_val, "title": cid_val, "status": "unknown"})
                cd["consumers"] = consumers
                # Fetch annotations linked to this contract via c4_element_id
                anno_rows = conn.execute(
                    "SELECT id, note_type, content, status, resolution FROM notes "
                    "WHERE c4_element_id = ?",
                    (f"contract_{cd['id']}",),
                ).fetchall()
                cd["annotations"] = [dict(r) for r in anno_rows]
                enriched.append(cd)
        finally:
            conn.close()
        self._json_response({"contracts": enriched})

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
            # Enrich notes with task context (title, parent title)
            all_notes = list(open_notes) + list(answered) + list(resolved)
            task_ids = list({n["task_id"] for n in all_notes if n.get("task_id")})
            tasks_map: dict[str, Any] = {}
            if task_ids:
                ph = ",".join("?" * len(task_ids))
                task_rows = conn.execute(
                    f"SELECT id, title, description, parent_id FROM tasks WHERE id IN ({ph})",  # noqa: S608
                    task_ids,
                ).fetchall()
                tasks_map = {r["id"]: dict(r) for r in task_rows}
                parent_ids = [t["parent_id"] for t in tasks_map.values() if t.get("parent_id")]
                if parent_ids:
                    pph = ",".join("?" * len(parent_ids))
                    parent_rows = conn.execute(
                        f"SELECT id, title FROM tasks WHERE id IN ({pph})",  # noqa: S608
                        parent_ids,
                    ).fetchall()
                    parents_map = {r["id"]: r["title"] for r in parent_rows}
                    for t in tasks_map.values():
                        t["parent_title"] = parents_map.get(t.get("parent_id", ""), None)
            for n in all_notes:
                t = tasks_map.get(n.get("task_id", ""), {})
                n["_task_title"] = t.get("title", n.get("task_id", ""))
                n["_task_desc"] = t.get("description") or ""
                n["_parent_title"] = t.get("parent_title") or ""
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
        self, task_id: Optional[str] = None, flow_name: Optional[str] = None,
        flow_id: Optional[int] = None,
    ) -> None:
        if not _SEQ_AVAILABLE:
            self._json_response({"mermaid": "", "error": "sequence_generator not available"})
            return
        # Flow-based mode: use GraphStore
        if flow_id is not None or flow_name is not None:
            try:
                store = GraphStore(self.db_path)
                result = generate_sequence(store=store, flow_id=flow_id, flow_name=flow_name)
            except Exception as exc:
                self._json_response({"mermaid": "", "error": str(exc)})
                return
            finally:
                pass
            self._json_response(result)
            return
        # Contract-based mode: use conn + task_id
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
    # GET /api/sequence-flows
    # ------------------------------------------------------------------

    def _serve_sequence_flows(self) -> None:
        """GET /api/sequence-flows — list available execution flows for sequence diagram."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT id, name, criticality, depth, node_count "
                "FROM flows ORDER BY criticality DESC LIMIT 50"
            ).fetchall()
            flows = [
                {
                    "id": r["id"],
                    "name": r["name"],
                    "criticality": r["criticality"],
                    "depth": r["depth"],
                    "node_count": r["node_count"],
                }
                for r in rows
            ]
            self._json_response({"flows": flows})
        except Exception:
            self._json_response({"flows": []})
        finally:
            conn.close()

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
    # GET /api/timeline
    # ------------------------------------------------------------------

    def _serve_timeline(self) -> None:
        """GET /api/timeline — enriched execution order for Gantt rendering.

        Returns:
            {
                "levels": [{"level": int, "tasks": [{"id", "title", "status", "level", ...}]}],
                "dependencies": [{"source": task_id, "target": task_id}],
                "critical_path": [task_id, ...],  # longest dependency chain
                "parallelism": [{"level": int, "count": int}]
            }
        """
        if self.root_task_id is None:
            self._json_response({
                "levels": [],
                "dependencies": [],
                "critical_path": [],
                "parallelism": [],
            })
            return
        conn = self._get_conn()
        try:
            # Include all statuses so the Gantt shows the complete picture
            data = execution_order(conn, self.root_task_id, skip_statuses=set())
            levels = data.get("levels", [])

            # Flat list of all task IDs across all levels
            all_task_ids = [t["id"] for lvl in levels for t in lvl["tasks"]]

            # Fetch depends_on edges between tasks that appear in our levels
            dependencies: list[dict[str, str]] = []
            if all_task_ids:
                task_id_set = set(all_task_ids)
                ph = ",".join("?" * len(all_task_ids))
                dep_rows = conn.execute(  # noqa: S608
                    f"SELECT source_task_id, target_task_id FROM task_edges "
                    f"WHERE type = 'depends_on' AND source_task_id IN ({ph})",
                    all_task_ids,
                ).fetchall()
                dependencies = [
                    {"source": r["source_task_id"], "target": r["target_task_id"]}
                    for r in dep_rows
                    if r["source_task_id"] in task_id_set
                    and r["target_task_id"] in task_id_set
                ]

            # Add level field to each task and build enriched levels
            enriched_levels: list[dict[str, Any]] = []
            task_level_map: dict[str, int] = {}
            for lvl in levels:
                lvl_idx = lvl["level"]
                enriched_tasks = []
                for t in lvl["tasks"]:
                    et = dict(t)
                    et["level"] = lvl_idx
                    enriched_tasks.append(et)
                    task_level_map[t["id"]] = lvl_idx
                enriched_levels.append({"level": lvl_idx, "tasks": enriched_tasks})

            # ---------------------------------------------------------------
            # Critical path via iterative DP (process levels in order)
            # deps_of[task] = list of task IDs this task depends on
            # ---------------------------------------------------------------
            deps_of: dict[str, list[str]] = {tid: [] for tid in all_task_ids}
            for dep in dependencies:
                deps_of[dep["source"]].append(dep["target"])

            cp_len: dict[str, int] = {}
            cp_prev: dict[str, Optional[str]] = {}

            # Levels are already in topological order (0 = no deps, 1 = depends on 0, …)
            for lvl in enriched_levels:
                for t in lvl["tasks"]:
                    tid = t["id"]
                    my_deps = [d for d in deps_of.get(tid, []) if d in cp_len]
                    if not my_deps:
                        cp_len[tid] = 1
                        cp_prev[tid] = None
                    else:
                        best_dep = max(my_deps, key=lambda d: cp_len[d])
                        cp_len[tid] = cp_len[best_dep] + 1
                        cp_prev[tid] = best_dep

            # Trace back from the task with the maximum path length
            critical_path: list[str] = []
            if cp_len:
                end_task = max(cp_len, key=lambda t: cp_len[t])
                cur: Optional[str] = end_task
                while cur is not None:
                    critical_path.insert(0, cur)
                    cur = cp_prev.get(cur)

            parallelism = [
                {"level": lvl["level"], "count": len(lvl["tasks"])}
                for lvl in enriched_levels
            ]
        except Exception as exc:
            self._json_response({
                "levels": [],
                "dependencies": [],
                "critical_path": [],
                "parallelism": [],
                "error": str(exc),
            })
            return
        finally:
            conn.close()

        self._json_response({
            "levels": enriched_levels,
            "dependencies": dependencies,
            "critical_path": critical_path,
            "parallelism": parallelism,
        })

    # ------------------------------------------------------------------
    # GET /api/dataflow/sources
    # ------------------------------------------------------------------

    def _serve_dataflow_sources(self) -> None:
        """GET /api/dataflow/sources — return available source nodes."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT id, name, qualified_name, kind, file_path "
                "FROM nodes WHERE kind IN ('Function', 'Class') "
                "ORDER BY name LIMIT 100"
            ).fetchall()
            sources = [
                {
                    "id": r["id"],
                    "name": r["name"],
                    "qualified_name": r["qualified_name"],
                    "kind": r["kind"],
                    "file": r["file_path"],
                }
                for r in rows
            ]
            self._json_response({"sources": sources})
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # GET /api/dataflow
    # ------------------------------------------------------------------

    def _serve_dataflow(self) -> None:
        """GET /api/dataflow?source=QN&depth=N — BFS from source over CALLS/IMPORTS_FROM."""
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        source = qs.get("source", [None])[0]
        depth = int(qs.get("depth", ["3"])[0])
        depth = max(1, min(depth, 6))

        if not source:
            self._json_response({"nodes": [], "edges": [], "error": "source required"})
            return

        conn = self._get_conn()
        try:
            visited: set[str] = set()
            queue: list[tuple[str, int]] = [(source, 0)]
            visited.add(source)
            result_nodes: dict[str, dict[str, object]] = {}
            result_edges: list[dict[str, object]] = []

            while queue:
                current_qn, current_depth = queue.pop(0)
                if current_depth > depth:
                    continue

                row = conn.execute(
                    "SELECT id, name, qualified_name, kind, file_path "
                    "FROM nodes WHERE qualified_name = ?",
                    (current_qn,),
                ).fetchone()
                if row:
                    result_nodes[current_qn] = {
                        "id": row["qualified_name"],
                        "label": row["name"],
                        "kind": row["kind"],
                        "file": row["file_path"],
                        "depth": current_depth,
                    }

                if current_depth < depth:
                    edges = conn.execute(
                        "SELECT source_qualified, target_qualified, kind FROM edges "
                        "WHERE source_qualified = ? AND kind IN ('CALLS', 'IMPORTS_FROM')",
                        (current_qn,),
                    ).fetchall()
                    for e in edges:
                        target = e["target_qualified"]
                        result_edges.append({
                            "source": e["source_qualified"],
                            "target": target,
                            "type": e["kind"].lower(),
                        })
                        if target not in visited:
                            visited.add(target)
                            queue.append((target, current_depth + 1))

            # Filter edges: both endpoints must be in result_nodes
            # (some target_qualified values are short external names like 'max', 'abs'
            #  that aren't real graph nodes — Cytoscape refuses dangling edges)
            valid_edges = [
                e for e in result_edges
                if e["source"] in result_nodes and e["target"] in result_nodes
            ]

            self._json_response({
                "nodes": list(result_nodes.values()),
                "edges": valid_edges,
            })
        finally:
            conn.close()

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
        content = body.get("content", "")           # source title (node label / task title)
        resolution = body.get("resolution", "")     # user's comment text
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
                    "resolution": resolution or None,
                    "status": "answered",
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


_PAGE_HTML = Path(__file__).resolve().parent / "ui" / "index.html"


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
