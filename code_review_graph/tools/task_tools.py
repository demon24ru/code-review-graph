"""MCP tool wrappers for the Task DAG system (tools 29-63).

Thin adapters over ``tasks.py`` (CRUD) and ``task_analysis.py`` (algorithms).
Each function opens the graph store, runs the operation, closes the store,
and returns a structured dict with ``status``, ``summary``, and data fields.

Groups:
  CRUD (9):           task_create, task_update, task_edit, task_delete,
                      task_get, task_list, task_move, task_search, task_archive
  DAG Edges (4):      task_add_edge, task_remove_edge,
                      task_get_dag, task_topological_sort
  Code Links (5):     task_link_code, task_unlink_code, task_get_code_refs,
                      task_find_by_code_node, task_suggest_code_links
   Analysis (5):       task_find_conflicts, task_contradiction_report,
                      task_blast_radius, task_execution_order
  Validation (3):     task_validate, task_build_context, task_export
   Notes (4):          note_add, note_update, note_list, note_delete
  Contracts (4):      contract_add, contract_update, contract_list, contract_delete
  Roadmap (2):        task_roadmap, task_roadmap_diff
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from .. import task_analysis, tasks
from ..response import prune_response
from ._common import _get_store, graph_error

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _ok(summary: str, **kwargs: Any) -> dict[str, Any]:
    """Build a standard success response, pruning empty fields."""
    return prune_response({"status": "ok", "summary": summary, **kwargs})


def _run(repo_root: Optional[str], fn, *args, **kwargs) -> dict[str, Any]:
    """Open the store, run ``fn(conn, *args, **kwargs)``, close the store.

    Uses task-specific error codes so LLM ``next_action`` routing is
    meaningful for task operations:
      KeyError  → TASK_NOT_FOUND  → next: task_list
      ValueError → TASK_INVALID_PARAMS → next: task_get
      cycle ValueError → TASK_CYCLE → next: task_get_dag
      single-pipeline ValueError → SINGLE_PIPELINE_VIOLATION → next: task_update/task_archive
      other     → TASK_PARSE_ERROR → next: task_validate
    """
    store, _ = _get_store(repo_root)
    try:
        return fn(store._conn, *args, **kwargs)
    except KeyError as exc:
        return graph_error("TASK_NOT_FOUND", str(exc))
    except ValueError as exc:
        msg = str(exc)
        if "Cannot create a new root task" in msg:
            # Extract blocking task ID for machine-readable response
            m = re.search(r"root task '([^']+)'", msg)
            blocking_id = m.group(1) if m else None
            err = graph_error("SINGLE_PIPELINE_VIOLATION", msg)
            if blocking_id:
                err["blocking_task_id"] = blocking_id
            return prune_response(err)
        elif "has children" in msg.lower():
            code = "TASK_HAS_CHILDREN"
        elif "cycle" in msg.lower():
            code = "TASK_CYCLE"
        else:
            code = "TASK_INVALID_PARAMS"
        return graph_error(code, msg)
    except Exception as exc:
        return graph_error("TASK_PARSE_ERROR", str(exc))
    finally:
        store.close()


def _run_contract(repo_root: Optional[str], fn, *args, **kwargs) -> dict[str, Any]:
    """Like _run() but uses contract-specific error codes for LLM routing.

    Uses contract-specific error codes so LLM ``next_action`` routing is
    meaningful for contract operations:
      KeyError  → CONTRACT_NOT_FOUND  → next: contract_list
      ValueError → CONTRACT_INVALID_PARAMS → next: contract_list
      other     → CONTRACT_ERROR → next: contract_list
    """
    store, _ = _get_store(repo_root)
    try:
        return fn(store._conn, *args, **kwargs)
    except KeyError as exc:
        return graph_error("CONTRACT_NOT_FOUND", str(exc))
    except ValueError as exc:
        return graph_error("CONTRACT_INVALID_PARAMS", str(exc))
    except Exception as exc:
        return graph_error("CONTRACT_ERROR", str(exc))
    finally:
        store.close()


# ===========================================================================
# Group 0: Active root helper (1 tool)
# ===========================================================================


def task_get_active_root_func(
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Return the single open root task, if any.

    [BRAINSTORM] Single-pipeline discipline: at most one root task can be
    open (not done/archived) at a time. Use this to check whether a pipeline
    is active before calling task_create, task_roadmap, task_export, etc.

    Returns:
        ``{active_root: {id, title, status, ...}}`` if an open root exists,
        or ``{active_root: null}`` when the pipeline is idle.
    """
    def _fn(conn):
        root = tasks.get_active_root(conn)
        if root:
            return _ok(
                f"Active root task: '{root['title']}' (status={root['status']!r})",
                active_root=root,
            )
        # P-05: active_root=None must not be pruned — add after _ok() to bypass prune_empty.
        result = _ok("No active root task — pipeline is idle.")
        result["active_root"] = None
        return result
    return _run(repo_root, _fn)


# ===========================================================================
# Group 1: CRUD (9 tools)
# ===========================================================================


def task_create_func(
    tasks_list: list[dict],
    parent_id: Optional[str] = None,
    edges_list: Optional[list[dict]] = None,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Create one or more tasks sharing a common parent.

    [BRAINSTORM] Always pass a list — even for a single task.

    Single task:
        task_create(tasks=[{"title": "Root task"}])

    Multiple tasks (batch decomposition):
        task_create(parent_id="t1", tasks=[
            {"title": "OAuth interface"},
            {"title": "Google OAuth", "description": "impl Google provider"},
            {"title": "JWT service"},
        ])

    With inline edges (atomic decomposition + wiring in one call):
        task_create(parent_id="t1", tasks=[
            {"title": "OAuth interface"},   # index 0
            {"title": "Google OAuth"},      # index 1
            {"title": "JWT service"},       # index 2
            {"title": "Login endpoint"},    # index 3
        ], edges=[
            {"from": 1, "to": 0, "type": "depends_on"},
            {"from": 3, "to": 0, "type": "depends_on"},
            {"from": 3, "to": 2, "type": "depends_on"},
        ])

    Each item requires ``title`` and may include ``description``.
    Edge items use 0-based indices into ``tasks``; ``type`` defaults to
    ``"depends_on"``; ``description`` is optional.

    **Single-pipeline discipline**: creating a root task (no parent_id) is
    blocked while another open root task exists.

    Args:
        tasks: List of task dicts — each with title (required), description (optional).
        parent_id: Shared parent task ID. Omit to create root task(s).
        edges: Optional list of inline edge dicts — {from, to, type?, description?}.
               ``from`` and ``to`` are 0-based indices into ``tasks``.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        ``{"tasks": [...]}`` — list of created task dicts.
        ``{"tasks": [...], "edges": [...]}`` — when inline edges were provided.
    """
    def _fn(conn, tasks_list, parent_id, edges_list):
        result = tasks.create_task(conn, tasks_list, parent_id=parent_id, edges=edges_list)
        created = result["tasks"]
        edge_count = len(result.get("edges", []))
        if len(created) == 1:
            msg = f"Created task '{created[0]['title']}' ({created[0]['id'][:8]})"
        else:
            msg = (
                f"Created {len(created)} task(s) under parent "
                f"{parent_id[:8] if parent_id else 'root'}"
            )
            if edge_count:
                msg += f" with {edge_count} inline edge(s)"
        return _ok(msg, **result)
    return _run(repo_root, _fn, tasks_list, parent_id, edges_list)


def task_update_func(
    task_id: str,
    title: Optional[str] = None,
    description: Optional[str] = None,
    status: Optional[str] = None,
    spec: Optional[str] = None,
    acceptance_criteria: Optional[str] = None,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Update fields of an existing task.

    [BRAINSTORM] Updates one or more fields. Only supplied (non-None) fields
    are changed. Valid status values: draft, refined, ready, in_progress, done,
    archived.

    Args:
        task_id: ID of the task to update.
        title: New title.
        description: New description.
        status: New status.
        spec: Full specification for the coder.
        acceptance_criteria: How to verify completion.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        The updated task dict.
    """
    def _fn(conn, task_id, title, description, status, spec, ac):
        task = tasks.update_task(
            conn, task_id,
            title=title, description=description, status=status,
            spec=spec, acceptance_criteria=ac,
        )
        return _ok(f"Updated task '{task['title']}'", task=task)
    return _run(repo_root, _fn, task_id, title, description, status, spec, acceptance_criteria)


def task_edit_func(
    task_id: str,
    field: str,
    search: Optional[str] = None,
    replace: Optional[str] = None,
    line_start: Optional[int] = None,
    line_end: Optional[int] = None,
    content: Optional[str] = None,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Surgically edit a text field of a task.

    [BRAINSTORM] Two modes:
    - **search/replace**: supply *search* and *replace*. Fails if the search
      string is not found or appears more than once (prevents silent drift).
    - **line range**: supply *line_start*, *line_end* (1-indexed, inclusive),
      and *content*. Replaces those lines with new content.

    Editable fields: description, spec, acceptance_criteria.

    Args:
        task_id: Task to edit.
        field: Field name (description | spec | acceptance_criteria).
        search: String to find (search/replace mode).
        replace: Replacement string (search/replace mode).
        line_start: First line to replace (line range mode, 1-indexed).
        line_end: Last line to replace (line range mode, inclusive).
        content: New content for the replaced lines (line range mode).
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        Edit result with old_fragment, new_fragment, line_range, total_lines.
    """
    def _fn(conn, task_id, field, search, replace, line_start, line_end, content):
        result = tasks.edit_task_field(
            conn, task_id, field,
            search=search, replace=replace,
            line_start=line_start, line_end=line_end, content=content,
        )
        return _ok(
            f"Edited '{field}' of task {task_id[:8]} "
            f"({result['total_lines']} lines total)",
            **result,
        )
    return _run(repo_root, _fn, task_id, field, search, replace, line_start, line_end, content)


def task_delete_func(
    task_id: str,
    cascade: bool = False,
    dry_run: bool = False,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Delete a task (and optionally its entire subtree).

    [BRAINSTORM] Permanently removes the task and all associated edges, code
    refs, notes, and contracts. Set *cascade=True* to also delete all subtasks
    recursively. To preserve history, use task_archive instead.

    Set *dry_run=True* to preview what would be deleted without performing
    the operation.

    Args:
        task_id: Task to delete.
        cascade: If True, delete all subtasks recursively.
        dry_run: If True, preview without deleting.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        List of deleted task IDs, or preview if dry_run=True.
    """
    def _fn(conn, task_id, cascade, dry_run):
        result = tasks.delete_task(conn, task_id, cascade=cascade, dry_run=dry_run)
        if dry_run:
            count = result["count"]
            return _ok(f"Preview: would delete {count} task(s)", **result)
        else:
            count = len(result["deleted_ids"])
            return _ok(f"Deleted {count} task(s)", **result)
    return _run(repo_root, _fn, task_id, cascade, dry_run)


def task_get_func(
    task_id: str,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Get a task by ID.

    [BRAINSTORM] Returns all fields of the task row.

    Args:
        task_id: Task ID to retrieve.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        Full task dict.
    """
    def _fn(conn, task_id):
        task = tasks.get_task(conn, task_id)
        return _ok(f"Task '{task['title']}' (status: {task['status']})", task=task)
    return _run(repo_root, _fn, task_id)


def task_list_func(
    parent_id: Optional[str] = None,
    status: Optional[str] = None,
    root_only: bool = False,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """List tasks with optional filters.

    [BRAINSTORM] Returns a filtered list of tasks.

    Args:
        parent_id: Return only direct children of this task.
        status: Filter by status (draft|refined|ready|in_progress|done|archived).
        root_only: If True, return only top-level (root) tasks.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        List of matching task dicts.
    """
    def _fn(conn, parent_id, status, root_only):
        task_list = tasks.list_tasks(conn, parent_id=parent_id, status=status, root_only=root_only)
        return _ok(f"Found {len(task_list)} task(s)", tasks=task_list)
    return _run(repo_root, _fn, parent_id, status, root_only)


def task_move_func(
    task_ids: list[str],
    new_parent_id: Optional[str] = None,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Move one or more tasks to a shared new parent (or promote them to root).

    [BRAINSTORM] Always pass a list — even for a single task.
    Fails if any move would create a hierarchy cycle (checked atomically).

    Single move:
        task_move(task_ids=["t5"], new_parent_id="t11")

    Batch restructuring (common during brainstorm refinement):
        task_move(new_parent_id="t11", task_ids=["t2", "t3", "t4"])

    Promote to root:
        task_move(task_ids=["t5"])   # new_parent_id defaults to None

    Args:
        task_ids: List of task IDs to move.
        new_parent_id: Shared new parent task ID. Omit to promote to root.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        ``{"tasks": [...]}`` — list of updated task dicts.
    """
    def _fn(conn, task_ids, new_parent_id):
        result = tasks.move_task(conn, task_ids, new_parent_id=new_parent_id)
        moved = result["tasks"]
        parent_label = f"under '{new_parent_id[:8]}'" if new_parent_id else "to root"
        if len(moved) == 1:
            msg = f"Moved task '{moved[0]['title']}' {parent_label}"
        else:
            msg = f"Moved {len(moved)} task(s) {parent_label}"
        return _ok(msg, **result)
    return _run(repo_root, _fn, task_ids, new_parent_id)


def task_search_func(
    root_task_id: str,
    query: str,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Keyword search within a task subtree.

    [BRAINSTORM] Searches title, description, and spec fields
    (case-insensitive) within the entire subtree rooted at *root_task_id*.

    Args:
        root_task_id: Root of the subtree to search within.
        query: Search string.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        List of matching task dicts.
    """
    def _fn(conn, root_task_id, query):
        resolved = task_analysis._resolve_root(conn, root_task_id, "task_search")
        results = tasks.search_tasks(conn, resolved, query)
        return _ok(f"Found {len(results)} task(s) matching '{query}'", tasks=results)
    return _run(repo_root, _fn, root_task_id, query)


def task_archive_func(
    task_ids: list[str],
    reason: str,
    cascade: bool = True,
    dry_run: bool = False,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Archive one or more tasks (and optionally their subtrees).

    [BRAINSTORM] Always pass a list — even for a single task.
    Sets status to 'archived' and records the reason. Unlike task_delete,
    the data is preserved for historical reference.

    Single archive:
        task_archive(task_ids=["t5"], reason="No longer needed")

    Selective archiving (common when changing approach mid-brainstorm):
        task_archive(reason="Switching to in-app only", task_ids=["t2", "t3", "t6"])

    Set *dry_run=True* to preview what would be archived without performing
    the operation.

    *cascade* applies to each task individually (default: True).

    Args:
        task_ids: List of task IDs to archive.
        reason: Why these tasks are being archived (required).
        cascade: If True (default), archive all subtasks of each task too.
        dry_run: If True, preview without archiving.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        ``{"archived_ids": [...]}`` — all archived task IDs (including cascaded),
        or preview if dry_run=True.
    """
    def _fn(conn, task_ids, reason, cascade, dry_run):
        result = tasks.archive_task(conn, task_ids, reason=reason, cascade=cascade, dry_run=dry_run)
        if dry_run:
            count = result["count"]
            return _ok(f"Preview: would archive {count} task(s)", reason=reason, **result)
        else:
            count = len(result["archived_ids"])
            return _ok(f"Archived {count} task(s)", reason=reason, **result)
    return _run(repo_root, _fn, task_ids, reason, cascade, dry_run)


# ===========================================================================
# Group 2: DAG Edges (5 tools)
# ===========================================================================


def task_add_edge_func(
    edges: list[dict],
    edge_type: Optional[str] = None,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Add one or more directed edges between tasks.

    [BRAINSTORM] Always pass a list — even for a single edge.

    Valid edge types: depends_on | blocks | shares_context | conflicts_with | informs
    - **depends_on** — source cannot start until target is done
    - **blocks** — source blocks target from starting
    - **shares_context** — both tasks share architectural decisions
    - **conflicts_with** — tasks may conflict and need coordination
    - **informs** — source decision influences target approach

    Cycle detection is enforced for depends_on and blocks (checked atomically).

    Single edge:
        task_add_edge(edges=[{"source_id": "t8", "target_id": "t5"}],
                      edge_type="depends_on")

    Pattern A — one source, many targets:
        task_add_edge(edge_type="depends_on", edges=[
            {"source_id": "t8", "target_id": "t5"},
            {"source_id": "t8", "target_id": "t6"},
            {"source_id": "t8", "target_id": "t7"},
        ])

    Pattern B — many sources, one target:
        task_add_edge(edge_type="depends_on", edges=[
            {"source_id": "t5", "target_id": "t2"},
            {"source_id": "t6", "target_id": "t2"},
        ])

    Pattern C — mixed (per-item edge_type overrides default):
        task_add_edge(edge_type="depends_on", edges=[
            {"source_id": "t5", "target_id": "t2"},
            {"source_id": "t6", "target_id": "t7", "edge_type": "shares_context"},
        ])

    Args:
        edges: List of edge dicts — each with source_id, target_id, and optional
               edge_type (overrides top-level default) and description.
        edge_type: Default edge type for all items lacking their own edge_type.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        ``{"edges": [...]}`` — list of created edge dicts.
    """
    def _fn(conn, edges, edge_type):
        result = tasks.add_task_edge(conn, edges, edge_type=edge_type)
        created = result["edges"]
        if len(created) == 1:
            e = created[0]
            msg = f"Added {e['source_task_id'][:8]} --{e['type']}--> {e['target_task_id'][:8]}"
        else:
            msg = f"Added {len(created)} edge(s)"
        return _ok(msg, **result)
    return _run(repo_root, _fn, edges, edge_type)


def task_remove_edge_func(
    source_id: str,
    target_id: str,
    edge_type: str,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Remove an edge between two tasks.

    [BRAINSTORM] Raises NOT_FOUND if the edge does not exist.

    Args:
        source_id: Source task ID.
        target_id: Target task ID.
        edge_type: Type of edge to remove.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        The removed edge dict.
    """
    def _fn(conn, source_id, target_id, edge_type):
        try:
            edge = tasks.remove_task_edge(conn, source_id, target_id, edge_type)
        except KeyError as exc:
            msg = exc.args[0] if exc.args else str(exc)
            return graph_error("EDGE_NOT_FOUND", msg)
        return _ok(f"Removed {source_id[:8]} --{edge_type}--> {target_id[:8]}", edge=edge)
    return _run(repo_root, _fn, source_id, target_id, edge_type)


def task_get_dag_func(
    root_task_id: str,
    compact: bool = False,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Get the full DAG rooted at a task.

    [BRAINSTORM] Returns all tasks in the subtree plus all edges between them
    (both task_edges and parent→child hierarchy edges). Suitable for
    visualisation.

    Args:
        root_task_id: Root of the DAG to retrieve.
        compact: If True, return only {id, title, status, depth, parent_id} per node.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        Dict with ``nodes`` (tasks) and ``edges`` (all relationships).
    """
    def _fn(conn, root_task_id, compact):
        dag = tasks.get_task_dag(conn, root_task_id, compact=compact)
        n_nodes = len(dag["nodes"])
        n_edges = len(dag["edges"])
        return _ok(f"DAG: {n_nodes} tasks, {n_edges} edges", **dag)
    return _run(repo_root, _fn, root_task_id, compact)


def task_topological_sort_func(
    root_task_id: str,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Topologically sort leaf tasks by their depends_on relationships.

    [BRAINSTORM] Returns leaf tasks in dependency order (dependencies first).
    Raises INVALID_PARAMS if a cycle is detected.

    Args:
        root_task_id: Root of the subtree to sort.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        Ordered list of leaf task dicts.
    """
    def _fn(conn, root_task_id):
        sorted_tasks = tasks.topological_sort_tasks(conn, root_task_id)
        return _ok(f"Topological order: {len(sorted_tasks)} leaf task(s)", tasks=sorted_tasks)
    return _run(repo_root, _fn, root_task_id)


# ===========================================================================
# Group 3: Code Links (5 tools)
# ===========================================================================


def task_link_code_func(
    task_id: str,
    links: list[dict],
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Link a task to one or many code graph nodes.

    [BRAINSTORM] Always pass a list — even for a single node.
    Associates a task with code entities (functions, classes, files).

    Single node:
        task_link_code(task_id="t5", links=[
            {"ref_type": "modifies", "code_node_id": 1786}
        ])

    Multiple nodes (typical leaf task — 3-8 code refs is normal):
        task_link_code(task_id="t5", links=[
            {"ref_type": "modifies", "code_node_id": 101},
            {"ref_type": "modifies", "code_node_id": 102},
            {"ref_type": "reads",    "qualified_name": "src/auth.py::TokenService"},
            {"ref_type": "creates",  "qualified_name": "src/models.py::OAuthToken",
             "description": "new model class"},
        ])

    Both ``code_node_id`` (int) and ``qualified_name`` (string) are returned by
    semantic_search_nodes_tool — no extra lookup needed.

    ref_type values: modifies | creates | deletes | reads | tests

    Failed items are collected in ``errors`` — does not abort the whole batch.

    Args:
        task_id: Task ID.
        links: List of dicts — each with ref_type and one of code_node_id /
               qualified_name. Optional per-item: description.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        ``{"task_id": ..., "linked": [...], "errors": [...], "total": N,
           "success_count": N, "error_count": N}``
    """
    def _fn(conn, task_id, links):
        ref = tasks.link_task_code(conn, task_id, links)
        sc = ref["success_count"]
        ec = ref["error_count"]
        msg = f"Linked {sc}/{ref['total']} node(s) to task {task_id[:8]}"
        if ec:
            msg += f" ({ec} error(s))"
        return _ok(msg, **ref)
    return _run(repo_root, _fn, task_id, links)


def task_unlink_code_func(
    task_id: str,
    code_node_id: int,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Remove all code refs between a task and a code node.

    [BRAINSTORM] Removes the association regardless of ref_type.

    Args:
        task_id: Task ID.
        code_node_id: Integer ID of the code node.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        Number of refs removed.
    """
    store, _ = _get_store(repo_root)
    try:
        result = tasks.unlink_task_code(store._conn, task_id, code_node_id)
        return _ok(
            f"Unlinked {result['removed']} ref(s) between task {task_id[:8]} and node {code_node_id}",
            **result,
        )
    except KeyError as exc:
        msg = str(exc)
        if "code ref" in msg.lower():
            return graph_error(
                "CODE_REF_NOT_FOUND",
                msg,
                recovery=f"Use task_get_code_refs(task_id='{task_id}') to list linked code nodes.",
            )
        return graph_error("TASK_NOT_FOUND", msg)
    except ValueError as exc:
        return graph_error("TASK_INVALID_PARAMS", str(exc))
    except Exception as exc:
        return graph_error("TASK_PARSE_ERROR", str(exc))
    finally:
        store.close()


def task_get_code_refs_func(
    task_id: str,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Get all code nodes linked to a task.

    [BRAINSTORM] Returns code nodes with their metadata (name, file, lines,
    language) and ref_type.

    Args:
        task_id: Task ID.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        List of code ref dicts with node metadata.
    """
    def _fn(conn, task_id):
        refs = tasks.get_task_code_refs(conn, task_id)
        return _ok(f"Found {len(refs)} code ref(s) for task {task_id[:8]}", code_refs=refs)
    return _run(repo_root, _fn, task_id)


def task_find_by_code_node_func(
    code_node_id: int,
    open_only: bool = True,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Find all tasks that reference a given code node.

    [BRAINSTORM] Useful for understanding which tasks are touching a
    specific function or class.

    Args:
        code_node_id: Integer ID of the code node.
        open_only: If True (default), exclude archived and done tasks.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        List of task dicts (with ref_type appended).
    """
    def _fn(conn, code_node_id, open_only):
        task_list = tasks.find_tasks_by_code_node(conn, code_node_id)
        if open_only:
            task_list = [t for t in task_list if t.get("status") not in ("done", "archived")]
        return _ok(f"Found {len(task_list)} task(s) referencing node {code_node_id}", tasks=task_list)
    return _run(repo_root, _fn, code_node_id, open_only)


def task_suggest_code_links_func(
    task_id: str,
    repo_root: Optional[str] = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Suggest code nodes to link to a task based on keyword extraction.

    [BRAINSTORM] Extracts keywords from the task's title and description,
    then searches the code graph. Already-linked nodes are excluded.
    Results scored by keyword match count. Returns candidates — does NOT
    create any links automatically.

    Args:
        task_id: Task ID to find code suggestions for.
        repo_root: Repository root path. Auto-detected if omitted.
        limit: Maximum number of results to return (default 20).

    Returns:
        List of candidate code node dicts, each with 'match_score' field.
    """
    def _fn(conn, task_id):
        suggestions = tasks.suggest_code_links(conn, task_id, limit=limit)
        return _ok(f"Found {len(suggestions)} code node suggestion(s)", suggestions=suggestions)
    return _run(repo_root, _fn, task_id)


# ===========================================================================
# Group 4: Analysis (4 tools)
# ===========================================================================


def task_find_conflicts_func(
    root_task_id: str,
    depth: int = 0,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Find conflicting leaf tasks whose code refs intersect.

    [BRAINSTORM] Algorithmically detects tasks that touch the same code nodes.
    Conflict types: both_modify, read_write, shared_ref (direct), indirect (via code graph).
    Use this after decomposing tasks to catch coordination issues early.

    Args:
        root_task_id: Root of the subtree to analyze.
        depth: Code graph BFS hops for indirect conflict detection.
            0 = direct code ref overlap only (default).
            1+ = also detect tasks connected through code graph edges.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        List of conflict records with task pairs and shared nodes.
    """
    def _fn(conn, root_task_id):
        conflicts = task_analysis.find_conflicts(conn, root_task_id, depth=depth)
        return _ok(f"Found {len(conflicts)} conflict(s)", conflicts=conflicts)
    return _run(repo_root, _fn, root_task_id)


def task_contradiction_report_func(
    root_task_id: str,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Gather compact contradiction analysis data for LLM review.

    [BRAINSTORM] Collects code conflicts (direct + indirect), all decisions,
    constraints, contracts, and leaf task summaries into one compact report.
    Pass this to an LLM to detect semantic contradictions that cannot be
    found algorithmically.

    Args:
        root_task_id: Root of the subtree to analyze.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        Dict with keys: code_conflicts, all_decisions, all_constraints,
        all_contracts, leaf_tasks_summary.
    """
    def _fn(conn, root_task_id):
        report = task_analysis.contradiction_report(conn, root_task_id)
        n_conflicts = len(report["code_conflicts"])
        n_decisions = len(report["all_decisions"])
        n_constraints = len(report["all_constraints"])
        return _ok(
            f"Report: {n_conflicts} code conflict(s), "
            f"{n_decisions} decision(s), {n_constraints} constraint(s)",
            **report,
        )
    return _run(repo_root, _fn, root_task_id)


def task_check_isolation_func(
    task_id: str,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Check how isolated a leaf task is from the rest of the codebase.

    [BRAINSTORM] Measures external callers and callees (depth=1) relative to
    the task's own code refs. Low isolation_score (<0.5) suggests the task
    may be too coupled and should be split further.

    Score = internal_nodes / (internal_nodes + external_nodes).
    Score of 1.0 = fully isolated; 0.0 = no internal nodes.

    Args:
        task_id: Task ID to check.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        Isolation metrics including score and external nodes.
    """
    def _fn(conn, task_id):
        result = task_analysis.check_isolation(conn, task_id)
        score = result["isolation_score"]
        quality = "good" if score >= 0.7 else ("marginal" if score >= 0.4 else "poor")
        return _ok(
            f"Isolation score: {score:.2f} ({quality}) — "
            f"{result['internal_nodes']} internal, "
            f"{result['external_dependencies']} deps (callees), "
            f"{result['external_dependents']} dependents (callers)",
            **result,
        )
    return _run(repo_root, _fn, task_id)


def task_blast_radius_func(
    task_id: str,
    depth: int = 2,
    include_affected_nodes: bool = False,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Compute the blast radius of a task through the code graph.

    [BRAINSTORM] Traverses the code graph BFS from the task's code refs up to
    *depth* hops. Shows which affected nodes are not covered by any task
    (potential risk zones).

    Args:
        task_id: Task ID to analyze.
        depth: BFS depth (default: 2).
        include_affected_nodes: If True, include full affected_nodes list.
            Default False returns only affected_nodes_count (scalar).
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        direct_nodes, affected_nodes_count, affected_nodes (if include_affected_nodes=True),
        uncovered_nodes, coverage_ratio.
    """
    def _fn(conn, task_id, depth, include_affected_nodes):
        result = task_analysis.blast_radius(
            conn, task_id, depth=depth, include_affected_nodes=include_affected_nodes
        )
        uncovered_count = result.get(
            "uncovered_nodes_count", len(result.get("uncovered_nodes", []))
        )
        coverage = result["coverage_ratio"]
        coverage_str = f"{coverage:.0%}" if coverage is not None else "N/A"
        return _ok(
            f"Blast radius: {len(result['direct_nodes'])} direct, "
            f"{result['affected_nodes_count']} affected, "
            f"{uncovered_count} uncovered "
            f"(coverage {coverage_str})",
            **result,
        )
    return _run(repo_root, _fn, task_id, depth, include_affected_nodes)


def task_execution_order_func(
    root_task_id: Optional[str] = None,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Compute parallelism-aware execution order for leaf tasks.

    [BRAINSTORM] Groups leaf tasks into execution levels based on depends_on
    edges. Level 0 tasks have no dependencies and can start immediately.
    Tasks in the same level can run in parallel.

    Args:
        root_task_id: Root of the subtree to analyze. Auto-detected if omitted.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        Execution plan with levels, total_levels, total_actionable, and note.
    """
    def _fn(conn, root_task_id):
        resolved = task_analysis._resolve_root(conn, root_task_id, "task_execution_order")
        result = task_analysis.execution_order(conn, resolved)
        return _ok(
            f"Execution plan: {result['total_levels']} level(s), {result['total_actionable']} actionable task(s)",
            **result,
        )
    return _run(repo_root, _fn, root_task_id)


# ===========================================================================
# Group 5: Validation & Context (3 tools)
# ===========================================================================


def task_validate_func(
    root_task_id: Optional[str] = None,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Validate the task DAG before handing it off to a coder.

    [BRAINSTORM] Runs 9 algorithmic checks. If *root_task_id* is None,
    the active root task is auto-detected.

    Args:
        root_task_id: Root of the DAG to validate. Auto-detected if omitted.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        errors (blocking), warnings (advisory), ok (passed checks).
    """
    def _fn(conn, root_task_id):
        result = task_analysis.validate_dag(conn, root_task_id)
        n_err = len(result["errors"])
        n_warn = len(result["warnings"])
        n_ok = len(result["ok"])
        ready = n_err == 0
        return _ok(
            f"Validation: {n_err} error(s), {n_warn} warning(s), {n_ok} check(s) passed "
            f"— {'ready for coder' if ready else 'NOT ready'}",
            ready_for_coder=ready,
            **result,
        )
    return _run(repo_root, _fn, root_task_id)


def task_export_func(
    task_id: Optional[str] = None,
    include_analysis: bool = False,
    include_source: bool = False,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Export task context — the primary context-building entry point.

    [BRAINSTORM] Returns all task data in a single dict ready for LLM
    consumption or workflow handoff.  Replaces the old ``task_build_context``.
    If *task_id* is None, the active root task is auto-detected.

    Args:
        task_id: Task ID to export. Auto-detected from active root if omitted.
        include_analysis: If True, also compute isolation score, sibling
            conflicts, and pipeline_state.ready_for_coder.  Costs an extra
            BFS traversal — set False for quick snapshots.
        include_source: If True, attach line-range info to code_refs so
            the LLM knows exactly where to read without loading full files.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        Flat dict with task, parent_chain, subtasks (with edges), edges,
        related_tasks, code_refs, notes, contracts, open_items.
        With include_analysis=True: also isolation, conflicts, pipeline_state.
    """
    def _fn(conn, task_id):
        data = task_analysis.export_task(
            conn, task_id,
            include_analysis=include_analysis,
            include_source=include_source,
        )
        oi = data["open_items"]
        ps = data.get("pipeline_state")
        if ps:
            ready_msg = "ready for coder" if ps["ready_for_coder"] else "NOT ready"
            msg = (
                f"Context for '{data['task']['title']}' — {ready_msg} "
                f"({oi['unresolved_questions']} open questions, "
                f"{oi['pending_contracts']} pending contracts)"
            )
        else:
            msg = (
                f"Exported task '{data['task']['title']}' — "
                f"{oi['unresolved_questions']} open question(s), "
                f"{oi['pending_contracts']} pending contract(s)"
            )
        return _ok(msg, **data)
    return _run(repo_root, _fn, task_id)


def task_build_context_func(
    task_id: str,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """[Alias] Build full task context with analysis (isolation + conflicts).

    Equivalent to task_export(include_analysis=True).
    Kept for backward compatibility — prefer task_export directly.
    """
    return task_export_func(task_id=task_id, include_analysis=True, repo_root=repo_root)


# ===========================================================================
# Group 6: Notes (4 tools)
# ===========================================================================


def note_add_func(
    task_id: str,
    notes: list[dict],
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Add one or more brainstorm notes to a task.

    [BRAINSTORM] Always pass a list — even for a single note.
    Records context from the brainstorm session.

    Note types:
    - **decision** — a resolved design choice (set status='resolved')
    - **question** — an open question to answer before coding
    - **assumption** — something assumed true that needs verification
    - **constraint** — a hard constraint from the user/context
    - **risk** — a potential problem to monitor

    Single note:
        note_add(task_id="t1", notes=[
            {"note_type": "constraint", "content": "self-hosted only"}
        ])

    Multiple notes (typical after structured brainstorm interview):
        note_add(task_id="t1", notes=[
            {"note_type": "decision", "content": "Use JWT tokens",
             "status": "resolved", "resolution": "JWT", "rationale": "stateless"},
            {"note_type": "constraint", "content": "self-hosted only"},
            {"note_type": "question", "content": "WebSocket or polling?"},
            {"note_type": "assumption", "content": "User model already exists"},
        ])

    Each item requires ``note_type`` and ``content``.
    Optional per-item: ``status`` (default "open"), ``resolution``,
    ``rationale``, ``alternatives``.

    Args:
        task_id: Task to attach the notes to.
        notes: List of note dicts.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        ``{"notes": [...]}`` — list of created note dicts.
    """
    def _fn(conn, task_id, notes):
        result = tasks.add_note(conn, task_id, notes)
        created = result["notes"]
        if len(created) == 1:
            msg = f"Added {created[0]['note_type']} note to task {task_id[:8]}"
        else:
            msg = f"Added {len(created)} note(s) to task {task_id[:8]}"
        return _ok(msg, **result)
    return _run(repo_root, _fn, task_id, notes)


def note_update_func(
    updates: list[dict],
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Update one or more notes.

    [BRAINSTORM] Batch-only API: always pass a list, even for a single note.
    Use to resolve open questions, update decisions' rationale, or change note status.

    Args:
        updates: List of dicts, each with ``note_id`` (required) plus any of:
            ``status``, ``resolution``, ``content``, ``rationale``.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        ``{"notes": [updated_note_objects], "errors": [...]}``
    """
    def _fn(conn, updates):
        result = tasks.update_notes(conn, updates)
        n_ok = len(result["notes"])
        n_err = len(result["errors"])
        return _ok(
            f"Updated {n_ok} note(s)" + (f", {n_err} error(s)" if n_err else ""),
            **result,
        )
    return _run(repo_root, _fn, updates)


def note_list_func(
    task_id: str,
    note_type=None,
    status: Optional[str] = None,
    include_parent: bool = True,
    include_children: bool = False,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """List notes for a task.

    [BRAINSTORM] Returns notes attached to the task. With include_parent=True
    (default), also includes notes from all ancestor tasks up to root — giving
    the full decision history for context.
    With include_children=True, searches notes across the entire subtree —
    useful for finding notes added to any child task.

    Args:
        task_id: Task ID.
        note_type: Filter by type (decision|question|assumption|constraint|risk).
        status: Filter by status (open|answered|resolved|rejected|deferred).
        include_parent: Include notes from ancestor tasks (default: True).
        include_children: Include notes from all descendant tasks.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        List of note dicts, each with task_id indicating where it was added.
    """
    def _fn(conn, task_id, note_type, status, include_parent, include_children):
        note_list = tasks.list_notes(
            conn, task_id, note_type=note_type, status=status,
            include_parent=include_parent, include_children=include_children,
        )
        return _ok(f"Found {len(note_list)} note(s)", notes=note_list)
    return _run(repo_root, _fn, task_id, note_type, status, include_parent, include_children)


def note_delete_func(
    note_id: str,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Delete a note by ID.

    [BRAINSTORM] Permanently removes the note.

    Args:
        note_id: Note ID to delete.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        The deleted note dict.
    """
    def _fn(conn, note_id):
        note = tasks.delete_note(conn, note_id)
        return _ok(f"Deleted note {note_id[:8]}", note=note)
    return _run(repo_root, _fn, note_id)


# ===========================================================================
# Group 7: Contracts (5 tools)
# ===========================================================================


def contract_add_func(
    name: str,
    contract_type: str,
    definition: str,
    scope_task_id: str,
    provider_task_id: Optional[str] = None,
    consumer_task_ids: Optional[list[str]] = None,
    code_node_id: Optional[int] = None,
    qualified_name: Optional[str] = None,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Create a contract / design entity scoped to a brainstorm subtree.

    [BRAINSTORM] A contract can represent a data structure that doesn't exist
    yet (OAuthToken), a shared interface (IUserRepo), an API endpoint spec,
    or a schema change on an *existing* code node.

    Participants are optional at creation time — attach them later with
    contract_link when task decomposition happens.

    Contract types:
    - **interface** — TypeScript/Python interface or abstract class
    - **api** — REST/RPC endpoint contract
    - **schema** — data structure or database schema
    - **event** — event name and payload structure
    - **data_format** — file format or data serialisation contract

    Args:
        name: Short identifier, e.g. "OAuthToken", "UserRepo.save()".
        contract_type: Type (interface|api|schema|event|data_format).
        definition: Human-readable spec (TypeScript-like, JSON Schema, etc.).
        scope_task_id: Root task that owns this brainstorm scope. Determines
            visibility in contract_list and roadmap.
        provider_task_id: Task that will implement this contract (optional).
        consumer_task_ids: Tasks that will use this contract (optional).
        code_node_id: Existing code node this contract *modifies* (optional).
            None means this is a new entity not yet in the code graph.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        The created contract dict with provider_task_ids and consumer_task_ids.
    """
    def _fn(conn, name, contract_type, definition, scope_task_id,
            provider_task_id, consumer_task_ids, code_node_id, qualified_name):
        contract = tasks.add_contract(
            conn,
            contract_type=contract_type,
            definition=definition,
            name=name,
            scope_task_id=scope_task_id,
            provider_task_id=provider_task_id,
            consumer_task_ids=consumer_task_ids,
            code_node_id=code_node_id,
            qualified_name=qualified_name,
        )
        n_participants = len(contract.get("provider_task_ids", [])) + len(contract.get("consumer_task_ids", []))
        return _ok(
            f"Created {contract_type} contract '{name}' "
            f"({'linked to code node' if (code_node_id or qualified_name) else 'new entity'}, "
            f"{n_participants} participant(s))",
            contract=contract,
        )
    return _run_contract(repo_root, _fn, name, contract_type, definition, scope_task_id,
                         provider_task_id, consumer_task_ids, code_node_id, qualified_name)


def contract_link_func(
    contract_id: str,
    task_id: str,
    role: str,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Attach a task to a contract as provider or consumer.

    [BRAINSTORM] Use after task decomposition when new subtasks need to
    participate in an existing contract.

    Args:
        contract_id: Contract to link to.
        task_id: Task to attach.
        role: 'provider' or 'consumer'.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    def _fn(conn, contract_id, task_id, role):
        contract = tasks.link_contract(conn, contract_id, task_id, role)
        return _ok(
            f"Linked task {task_id[:8]} as {role} to contract {contract_id[:8]}",
            contract=contract,
        )
    return _run_contract(repo_root, _fn, contract_id, task_id, role)


def contract_unlink_func(
    contract_id: str,
    task_id: str,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Remove all links between a task and a contract.

    [BRAINSTORM] Use when a task no longer owns or uses a design entity.

    Args:
        contract_id: Contract to unlink from.
        task_id: Task to detach.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    def _fn(conn, contract_id, task_id):
        contract = tasks.unlink_contract(conn, contract_id, task_id)
        return _ok(
            f"Unlinked task {task_id[:8]} from contract {contract_id[:8]}",
            contract=contract,
        )
    return _run_contract(repo_root, _fn, contract_id, task_id)


def contract_update_func(
    contract_id: str,
    name: Optional[str] = None,
    definition: Optional[str] = None,
    status: Optional[str] = None,
    code_node_id: Optional[int] = None,
    qualified_name: Optional[str] = None,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Update a contract's name, definition, status, or code_node reference.

    [BRAINSTORM] Use to progress a contract through its lifecycle:
    proposed -> agreed -> implemented -> verified.
    Status is also auto-propagated when participant task statuses change.

    To link to an existing code node provide EITHER *code_node_id* (integer
    ``id`` from ``semantic_search_nodes_tool``) OR *qualified_name* (string
    ``qualified_name`` field from the same results).

    Args:
        contract_id: Contract ID to update.
        name: Updated contract name.
        definition: Updated contract definition.
        status: New status (proposed|agreed|implemented|verified|void).
        code_node_id: Integer node id from semantic_search_nodes_tool results.
        qualified_name: Qualified name string from search results or code_refs.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        The updated contract dict.
    """
    def _fn(conn, contract_id, name, definition, status, code_node_id, qualified_name):
        contract = tasks.update_contract(
            conn, contract_id, name=name, definition=definition,
            status=status, code_node_id=code_node_id, qualified_name=qualified_name,
        )
        return _ok(
            f"Updated contract {contract_id[:8]} (status: {contract['status']})",
            contract=contract,
        )
    return _run_contract(repo_root, _fn, contract_id, name, definition, status, code_node_id, qualified_name)


def contract_list_func(
    scope_task_id: Optional[str] = None,
    task_id: Optional[str] = None,
    name: Optional[str] = None,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """List contracts matching given filters.

    [BRAINSTORM] Filters can be combined:
    - scope_task_id — all contracts in a brainstorm subtree (incl. orphans
      not yet linked to any task).
    - task_id — contracts where this task participates (any role).
    - name — partial name match (case-insensitive).

    At least one filter must be provided.

    Args:
        scope_task_id: Root of the brainstorm scope to list all contracts.
        task_id: Task participating in the contracts.
        name: Partial name to search for.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        List of contract dicts with provider_task_ids and consumer_task_ids.
    """
    def _fn(conn, scope_task_id, task_id, name):
        contract_list = tasks.list_contracts(
            conn, scope_task_id=scope_task_id, task_id=task_id, name=name
        )
        return _ok(
            f"Found {len(contract_list)} contract(s)",
            contracts=contract_list,
        )
    return _run_contract(repo_root, _fn, scope_task_id, task_id, name)


def contract_delete_func(
    contract_id: str,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Delete a contract and all its participant links.

    [BRAINSTORM] Permanently removes the contract from the brainstorm scope.

    Args:
        contract_id: Contract ID to delete.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        ``{"status": "ok", "deleted_contract_id": ..., "name": ...}``
    """
    def _fn(conn, contract_id):
        result = tasks.delete_contract(conn, contract_id)
        return _ok(f"Deleted contract {contract_id[:8]} ('{result['name']}')", **result)
    return _run_contract(repo_root, _fn, contract_id)


# ===========================================================================
# Group 8: Roadmap (2 tools)
# ===========================================================================


def task_roadmap_func(
    root_task_id: Optional[str] = None,
    include_archived: bool = False,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Get an aggregated progress snapshot for the entire task tree.

    [BRAINSTORM] The primary orientation tool — call this at the start of
    every session to understand where the brainstorm stands.
    If *root_task_id* is None, the active root task is auto-detected.

    Returns:
    - **progress**: total/done/in_progress/ready/blocked/draft counts + percent
    - **phases**: execution levels with task statuses and blocked_by info
      (archived tasks hidden by default; use include_archived=True to show them)
    - **archived**: list of archived tasks (only when include_archived=True)
    - **contracts**: summary of contract statuses
    - **attention**: what needs action right now (ready_to_start, open questions,
      unverified assumptions, low-isolation tasks, pending contracts)

    Args:
        root_task_id: Root task ID. Auto-detected if omitted.
        include_archived: If True, include archived tasks in phases and add
            an ``archived`` section. Default False.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        Full roadmap snapshot.
    """
    def _fn(conn, root_task_id, include_archived):
        rm = task_analysis.roadmap(conn, root_task_id, include_archived=include_archived)
        p = rm["progress"]
        archived_hint = (
            f", {p['archived']} archived (hidden — use include_archived=True to show)"
            if p["archived"] and not include_archived
            else ""
        )
        return _ok(
            f"Roadmap: {p['total']} tasks, {p['done']} done ({p['percent']}%), "
            f"{p['blocked']} blocked{archived_hint}",
            **rm,
        )
    return _run(repo_root, _fn, root_task_id, include_archived)


def task_roadmap_diff_func(
    root_task_id: str,
    since_timestamp: float,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Get what changed in the task tree since a given timestamp.

    [BRAINSTORM] Use at the start of a resumed session to quickly understand
    what happened since you last worked on this brainstorm.

    Args:
        root_task_id: Root task ID.
        since_timestamp: Unix timestamp (float). Changes after this time are returned.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        tasks_created, tasks_status_changed, notes_added, contracts_changed,
        new_conflicts since the given timestamp.
    """
    def _fn(conn, root_task_id, since_timestamp):
        diff = task_analysis.roadmap_diff(conn, root_task_id, since_timestamp)
        n_new = len(diff["tasks_created"])
        n_changed = len(diff["tasks_status_changed"])
        n_notes = len(diff["notes_added"])
        return _ok(
            f"Since timestamp: {n_new} new task(s), "
            f"{n_changed} status change(s), {n_notes} new note(s)",
            **diff,
        )
    return _run(repo_root, _fn, root_task_id, since_timestamp)


# ===========================================================================
# Cross-query #2: impact radius → open tasks
# ===========================================================================


def task_find_for_impact_func(
    file_paths: list[str],
    root_task_id: Optional[str] = None,
    max_depth: int = 2,
    open_only: bool = True,
    include_node_details: bool = False,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Find tasks whose code refs fall within the blast radius of changed files.

    [BRAINSTORM] Cross-query that bridges the code graph and the task DAG:
    given a list of changed files, expand their blast radius in the code graph,
    then return all tasks that reference nodes within that radius.

    Use this to answer: *"Are there open tasks for code I'm about to change?"*
    before merging a branch or landing a refactor.

    Args:
        file_paths: Repository-relative paths to changed files (list of strings).
        root_task_id: If given, restrict results to tasks in this subtree.
        max_depth: BFS hops into the code graph. Default 2.
        open_only: If True (default), only return non-done/archived tasks.
        include_node_details: If True, include full ``impacted_nodes`` and
            ``uncovered_nodes`` lists. Default False — only counts are returned
            to keep the response compact.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        Dict with:
          ``impacted_nodes_count``  — total nodes in blast radius
          ``impacted_nodes``        — node detail list (only if include_node_details=True)
          ``tasks``                 — matching open tasks with ``matched_nodes`` field
          ``uncovered_nodes_count`` — nodes with no task refs
          ``uncovered_nodes``       — node detail list (only if include_node_details=True)
          ``coverage_ratio``        — fraction of impacted nodes covered by tasks
    """
    def _fn(conn, file_paths, root_task_id, max_depth, open_only, include_node_details):
        result = task_analysis.find_tasks_for_impact(
            conn,
            file_paths,
            root_task_id=root_task_id,
            max_depth=max_depth,
            open_only=open_only,
            include_node_details=include_node_details,
        )
        n_tasks = len(result["tasks"])
        n_nodes = result["impacted_nodes_count"]
        coverage = result["coverage_ratio"]
        return _ok(
            f"Impact radius: {n_nodes} node(s), {n_tasks} open task(s), "
            f"coverage {coverage:.0%}",
            **result,
        )
    return _run(repo_root, _fn, file_paths, root_task_id, max_depth, open_only, include_node_details)


def task_check_rollup_func(
    task_id: str,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Check whether a task's parent (and ancestors) can change status.

    [BRAINSTORM] After completing or archiving a task, call this to find out
    if the parent task can now be closed or archived as well. Walks the full
    ancestor chain and reports readiness at each level.

    Does NOT modify any data — purely analytical. The LLM/user decides
    whether to act on the suggestions.

    Args:
        task_id: The task that was just updated (completed/archived).
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        parent_chain with can_close/can_archive/can_complete flags and
        suggestion text per ancestor. immediate_parent is the first entry.
    """
    def _fn(conn, task_id):
        result = task_analysis.check_parent_rollup(conn, task_id)
        chain = result.get("parent_chain", [])
        actionable = [e for e in chain if e.get("can_close") or e.get("can_archive") or e.get("can_complete")]
        if actionable:
            msg = f"{len(actionable)} ancestor(s) ready to close/archive"
        elif chain:
            msg = "No ancestors ready to close yet"
        else:
            msg = "Task has no parent — nothing to roll up"
        return _ok(msg, **result)
    return _run(repo_root, _fn, task_id)


def task_suggest_contracts_func(
    root_task_id: Optional[str] = None,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Suggest contracts between tasks with implicit code-level dependencies.

    [BRAINSTORM] Detects pairs of leaf tasks whose code refs are connected
    via code graph edges (calls/imports) but have no explicit task_edge or
    contract between them. These are *hidden dependencies* that need
    interface contracts for safe parallel development.

    Args:
        root_task_id: Root of the subtree to analyze. Auto-detected if omitted (uses active root task).
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        List of suggestions, each with task pair info, crossing edge count,
        and sample edge details. Sorted by crossing_edges (strongest first).
    """
    def _fn(conn, root_task_id):
        suggestions = task_analysis.suggest_contracts(conn, root_task_id)
        if not suggestions:
            return _ok("No contract suggestions — all code dependencies are covered", suggestions=[])
        return _ok(
            f"{len(suggestions)} contract suggestion(s) found",
            suggestions=suggestions,
        )
    return _run(repo_root, _fn, root_task_id)
