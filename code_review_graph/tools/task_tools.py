"""MCP tool wrappers for the Task DAG system (tools 29-63).

Thin adapters over ``tasks.py`` (CRUD) and ``task_analysis.py`` (algorithms).
Each function opens the graph store, runs the operation, closes the store,
and returns a structured dict with ``status``, ``summary``, and data fields.

Groups:
  CRUD (9):           task_create, task_update, task_edit, task_delete,
                      task_get, task_list, task_move, task_search, task_archive
  DAG Edges (5):      task_add_edge, task_remove_edge, task_get_edges,
                      task_get_dag, task_topological_sort
  Code Links (5):     task_link_code, task_unlink_code, task_get_code_refs,
                      task_find_by_code_node, task_suggest_code_links
  Analysis (4):       task_find_conflicts, task_check_isolation,
                      task_blast_radius, task_execution_order
  Validation (3):     task_validate, task_build_context, task_export
  Notes (4):          note_add, note_update, note_list, note_delete
  Contracts (3):      contract_add, contract_update, contract_list
  Roadmap (2):        task_roadmap, task_roadmap_diff
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .. import task_analysis, tasks
from ._common import _get_store, graph_error
from ..response import prune_response

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
      other     → TASK_PARSE_ERROR → next: task_validate
    """
    store, _ = _get_store(repo_root)
    try:
        return fn(store._conn, *args, **kwargs)
    except KeyError as exc:
        return graph_error("TASK_NOT_FOUND", str(exc))
    except ValueError as exc:
        msg = str(exc)
        code = "TASK_CYCLE" if "cycle" in msg.lower() else "TASK_INVALID_PARAMS"
        return graph_error(code, msg)
    except Exception as exc:
        return graph_error("TASK_PARSE_ERROR", str(exc))
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
        return _ok("No active root task — pipeline is idle.", active_root=None)
    return _run(repo_root, _fn)


# ===========================================================================
# Group 1: CRUD (9 tools)
# ===========================================================================


def task_create_func(
    title: str,
    description: Optional[str] = None,
    parent_id: Optional[str] = None,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Create a new task.

    [BRAINSTORM] Creates a task. If *parent_id* is given the task becomes a
    subtask of the specified parent.

    Args:
        title: Task title (required).
        description: Optional free-text description.
        parent_id: Parent task ID. Omit to create a root task.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        The newly created task dict.
    """
    def _fn(conn, title, description, parent_id):
        task = tasks.create_task(conn, title, description=description, parent_id=parent_id)
        return _ok(f"Created task '{task['title']}' ({task['id'][:8]})", task=task)
    return _run(repo_root, _fn, title, description, parent_id)


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
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Delete a task (and optionally its entire subtree).

    [BRAINSTORM] Permanently removes the task and all associated edges, code
    refs, notes, and contracts. Set *cascade=True* to also delete all subtasks
    recursively. To preserve history, use task_archive instead.

    Args:
        task_id: Task to delete.
        cascade: If True, delete all subtasks recursively.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        List of deleted task IDs.
    """
    def _fn(conn, task_id, cascade):
        result = tasks.delete_task(conn, task_id, cascade=cascade)
        count = len(result["deleted_ids"])
        return _ok(f"Deleted {count} task(s)", **result)
    return _run(repo_root, _fn, task_id, cascade)


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
    task_id: str,
    new_parent_id: Optional[str] = None,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Move a task to a new parent (or promote it to root).

    [BRAINSTORM] Fails if the move would create a hierarchy cycle.

    Args:
        task_id: Task to move.
        new_parent_id: New parent task ID. Pass null/None to make it a root task.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        Updated task dict.
    """
    def _fn(conn, task_id, new_parent_id):
        task = tasks.move_task(conn, task_id, new_parent_id=new_parent_id)
        parent_label = f"under '{new_parent_id[:8]}'" if new_parent_id else "to root"
        return _ok(f"Moved task '{task['title']}' {parent_label}", task=task)
    return _run(repo_root, _fn, task_id, new_parent_id)


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
        results = tasks.search_tasks(conn, root_task_id, query)
        return _ok(f"Found {len(results)} task(s) matching '{query}'", tasks=results)
    return _run(repo_root, _fn, root_task_id, query)


def task_archive_func(
    task_id: str,
    reason: str,
    cascade: bool = True,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Archive a task (and optionally its subtree).

    [BRAINSTORM] Sets status to 'archived' and records the reason. Unlike
    task_delete, the data is preserved for historical reference.
    Use this when the direction has changed fundamentally.

    Args:
        task_id: Task to archive.
        reason: Why this task is being archived (required).
        cascade: If True (default), archive all subtasks too.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        List of archived task IDs.
    """
    def _fn(conn, task_id, reason, cascade):
        result = tasks.archive_task(conn, task_id, reason=reason, cascade=cascade)
        count = len(result["archived_ids"])
        return _ok(f"Archived {count} task(s): {reason}", **result)
    return _run(repo_root, _fn, task_id, reason, cascade)


# ===========================================================================
# Group 2: DAG Edges (5 tools)
# ===========================================================================


def task_add_edge_func(
    source_id: str,
    target_id: str,
    edge_type: str,
    description: Optional[str] = None,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Add a directed edge between two tasks.

    [BRAINSTORM] Valid edge types:
    - **depends_on** — source cannot start until target is done
    - **blocks** — source blocks target from starting
    - **shares_context** — both tasks share architectural decisions
    - **conflicts_with** — tasks may conflict and need coordination
    - **informs** — source decision influences target approach

    Cycle detection is enforced for depends_on and blocks.

    Args:
        source_id: Source task ID.
        target_id: Target task ID.
        edge_type: Type of relationship (see above).
        description: Optional human-readable description of the relationship.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        The created edge dict.
    """
    def _fn(conn, source_id, target_id, edge_type, description):
        edge = tasks.add_task_edge(conn, source_id, target_id, edge_type, description=description)
        return _ok(f"Added {source_id[:8]} --{edge_type}--> {target_id[:8]}", edge=edge)
    return _run(repo_root, _fn, source_id, target_id, edge_type, description)


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
        edge = tasks.remove_task_edge(conn, source_id, target_id, edge_type)
        return _ok(f"Removed {source_id[:8]} --{edge_type}--> {target_id[:8]}", edge=edge)
    return _run(repo_root, _fn, source_id, target_id, edge_type)


def task_get_edges_func(
    task_id: str,
    direction: str = "both",
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Get all edges for a task.

    [BRAINSTORM] Returns the task's dependency and relationship edges.

    Args:
        task_id: Task ID.
        direction: "incoming", "outgoing", or "both" (default).
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        List of edge dicts.
    """
    def _fn(conn, task_id, direction):
        edge_list = tasks.get_task_edges(conn, task_id, direction=direction)
        return _ok(f"Found {len(edge_list)} edge(s)", edges=edge_list)
    return _run(repo_root, _fn, task_id, direction)


def task_get_dag_func(
    root_task_id: str,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Get the full DAG rooted at a task.

    [BRAINSTORM] Returns all tasks in the subtree plus all edges between them
    (both task_edges and parent→child hierarchy edges). Suitable for
    visualisation.

    Args:
        root_task_id: Root of the DAG to retrieve.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        Dict with ``nodes`` (tasks) and ``edges`` (all relationships).
    """
    def _fn(conn, root_task_id):
        dag = tasks.get_task_dag(conn, root_task_id)
        n_nodes = len(dag["nodes"])
        n_edges = len(dag["edges"])
        return _ok(f"DAG: {n_nodes} tasks, {n_edges} edges", **dag)
    return _run(repo_root, _fn, root_task_id)


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
    ref_type: str = "modifies",
    code_node_id: Optional[int] = None,
    qualified_name: Optional[str] = None,
    description: Optional[str] = None,
    batch: Optional[list] = None,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Link a task to one or many code graph nodes.

    [BRAINSTORM] Associates a task with code entities (functions, classes, files).

    **Single mode** — link one node:
        task_link_code(task_id, ref_type="modifies", code_node_id=1786)
        task_link_code(task_id, ref_type="modifies",
                       qualified_name="src/auth.py::login")

    **Batch mode** — link many nodes in one call:
        task_link_code(task_id, batch=[
            {"ref_type": "modifies", "code_node_id": 101},
            {"ref_type": "modifies", "code_node_id": 102},
            {"ref_type": "reads",    "qualified_name": "src/auth.py::TokenService"},
            {"ref_type": "creates",  "qualified_name": "src/models.py::OAuthToken",
             "description": "new model"},
        ])
        When batch is provided, top-level ref_type/code_node_id/qualified_name are ignored.
        Failed items are collected in ``errors`` — does not abort the whole batch.

    Both ``id`` and ``qualified_name`` are returned by semantic_search_nodes_tool,
    so no extra lookup step is needed.

    ref_type values: modifies | creates | deletes | reads | tests

    Args:
        task_id: Task ID.
        ref_type: Default ref type for single mode (modifies|creates|deletes|reads|tests).
        code_node_id: Integer node ID from semantic_search_nodes_tool results.
        qualified_name: Qualified name string from search results or task_export code_refs.
        description: Optional description of the relationship (single mode).
        batch: List of {ref_type, code_node_id|qualified_name, description?} dicts.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    def _fn(conn, task_id, ref_type, code_node_id, qualified_name, description, batch):
        ref = tasks.link_task_code(
            conn, task_id, ref_type,
            code_node_id=code_node_id,
            qualified_name=qualified_name,
            description=description,
            batch=batch,
        )
        if batch is not None:
            sc = ref["success_count"]
            ec = ref["error_count"]
            msg = f"Batch linked {sc}/{ref['total']} nodes to task {task_id[:8]}"
            if ec:
                msg += f" ({ec} errors)"
            return _ok(msg, ref=ref)
        node_ref = ref.get("code_node_id", code_node_id or qualified_name)
        return _ok(f"Linked task {task_id[:8]} --{ref_type}--> node {node_ref}", ref=ref)
    return _run(repo_root, _fn, task_id, ref_type, code_node_id, qualified_name,
                description, batch)


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
    def _fn(conn, task_id, code_node_id):
        result = tasks.unlink_task_code(conn, task_id, code_node_id)
        return _ok(
            f"Unlinked {result['removed']} ref(s) between task {task_id[:8]} and node {code_node_id}",
            **result,
        )
    return _run(repo_root, _fn, task_id, code_node_id)


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
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Find all tasks that reference a given code node.

    [BRAINSTORM] Useful for understanding which tasks are touching a
    specific function or class.

    Args:
        code_node_id: Integer ID of the code node.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        List of task dicts (with ref_type appended).
    """
    def _fn(conn, code_node_id):
        result = tasks.find_tasks_by_code_node(conn, code_node_id)
        return _ok(f"Found {len(result)} task(s) referencing node {code_node_id}", tasks=result)
    return _run(repo_root, _fn, code_node_id)


def task_suggest_code_links_func(
    task_id: str,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Suggest code nodes to link to a task based on keyword extraction.

    [BRAINSTORM] Extracts keywords from the task's title and description,
    then searches the code graph. Returns candidates — does NOT create any
    links automatically.

    Args:
        task_id: Task ID to find code suggestions for.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        List of candidate code node dicts.
    """
    def _fn(conn, task_id):
        suggestions = tasks.suggest_code_links(conn, task_id)
        return _ok(f"Found {len(suggestions)} code node suggestion(s)", suggestions=suggestions)
    return _run(repo_root, _fn, task_id)


# ===========================================================================
# Group 4: Analysis (4 tools)
# ===========================================================================


def task_find_conflicts_func(
    root_task_id: str,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Find conflicting leaf tasks whose code refs intersect.

    [BRAINSTORM] Algorithmically detects tasks that touch the same code nodes.
    Conflict types: both_modify, read_write, shared_ref.
    Use this after decomposing tasks to catch coordination issues early.

    Args:
        root_task_id: Root of the subtree to analyze.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        List of conflict records with task pairs and shared nodes.
    """
    def _fn(conn, root_task_id):
        conflicts = task_analysis.find_conflicts(conn, root_task_id)
        return _ok(f"Found {len(conflicts)} conflict(s)", conflicts=conflicts)
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
            f"{result['external_dependencies']} external deps",
            **result,
        )
    return _run(repo_root, _fn, task_id)


def task_blast_radius_func(
    task_id: str,
    depth: int = 2,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Compute the blast radius of a task through the code graph.

    [BRAINSTORM] Traverses the code graph BFS from the task's code refs up to
    *depth* hops. Shows which affected nodes are not covered by any task
    (potential risk zones).

    Args:
        task_id: Task ID to analyze.
        depth: BFS depth (default: 2).
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        direct_nodes, affected_nodes, uncovered_nodes, coverage_ratio.
    """
    def _fn(conn, task_id, depth):
        result = task_analysis.blast_radius(conn, task_id, depth=depth)
        return _ok(
            f"Blast radius: {len(result['direct_nodes'])} direct, "
            f"{len(result['affected_nodes'])} affected, "
            f"{len(result['uncovered_nodes'])} uncovered "
            f"(coverage {result['coverage_ratio']:.0%})",
            **result,
        )
    return _run(repo_root, _fn, task_id, depth)


def task_execution_order_func(
    root_task_id: str,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Compute parallelism-aware execution order for leaf tasks.

    [BRAINSTORM] Groups leaf tasks into execution levels based on depends_on
    edges. Level 0 tasks have no dependencies and can start immediately.
    Tasks in the same level can run in parallel.

    Args:
        root_task_id: Root of the subtree to analyze.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        List of levels, each with tasks that can run in parallel.
    """
    def _fn(conn, root_task_id):
        levels = task_analysis.execution_order(conn, root_task_id)
        total_leaves = sum(len(lvl["tasks"]) for lvl in levels)
        return _ok(
            f"Execution plan: {len(levels)} level(s), {total_leaves} leaf task(s)",
            levels=levels,
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
    note_type: str,
    content: str,
    status: str = "open",
    resolution: Optional[str] = None,
    rationale: Optional[str] = None,
    alternatives: Optional[list[str]] = None,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Add a brainstorm note to a task.

    [BRAINSTORM] Records any piece of context from the brainstorm session.
    Note types:
    - **decision** — a resolved design choice (set status='resolved')
    - **question** — an open question to answer before coding
    - **assumption** — something assumed true that needs verification
    - **constraint** — a hard constraint from the user/context
    - **risk** — a potential problem to monitor

    Args:
        task_id: Task to attach the note to.
        note_type: Type (decision|question|assumption|constraint|risk).
        content: The note text (required).
        status: open|resolved|rejected|deferred (default: open).
        resolution: The answer or decision (for resolved notes).
        rationale: Why this decision was made.
        alternatives: List of considered alternatives that were rejected.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        The created note dict.
    """
    def _fn(conn, task_id, note_type, content, status, resolution, rationale, alternatives):
        note = tasks.add_note(
            conn, task_id, note_type, content,
            status=status, resolution=resolution,
            rationale=rationale, alternatives=alternatives,
        )
        return _ok(f"Added {note_type} note to task {task_id[:8]}", note=note)
    return _run(repo_root, _fn, task_id, note_type, content, status, resolution, rationale, alternatives)


def note_update_func(
    note_id: str,
    status: Optional[str] = None,
    resolution: Optional[str] = None,
    rationale: Optional[str] = None,
    content: Optional[str] = None,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Update an existing note.

    [BRAINSTORM] Use to resolve an open question, update a decision's
    rationale, or change a note's status.

    Args:
        note_id: Note ID to update.
        status: New status (open|resolved|rejected|deferred).
        resolution: Answer or decision text.
        rationale: Reasoning behind the decision.
        content: Updated note text.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        The updated note dict.
    """
    def _fn(conn, note_id, status, resolution, rationale, content):
        note = tasks.update_note(
            conn, note_id,
            status=status, resolution=resolution,
            rationale=rationale, content=content,
        )
        return _ok(f"Updated note {note_id[:8]} (status: {note['status']})", note=note)
    return _run(repo_root, _fn, note_id, status, resolution, rationale, content)


def note_list_func(
    task_id: str,
    note_type: Optional[str] = None,
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
        status: Filter by status (open|resolved|rejected|deferred).
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
    return _run(repo_root, _fn, name, contract_type, definition, scope_task_id,
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
    return _run(repo_root, _fn, contract_id, task_id, role)


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
    return _run(repo_root, _fn, contract_id, task_id)


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
    return _run(repo_root, _fn, contract_id, name, definition, status, code_node_id, qualified_name)


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
    return _run(repo_root, _fn, scope_task_id, task_id, name)


# ===========================================================================
# Group 8: Roadmap (2 tools)
# ===========================================================================


def task_roadmap_func(
    root_task_id: Optional[str] = None,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Get an aggregated progress snapshot for the entire task tree.

    [BRAINSTORM] The primary orientation tool — call this at the start of
    every session to understand where the brainstorm stands.
    If *root_task_id* is None, the active root task is auto-detected.

    Returns:
    - **progress**: total/done/in_progress/ready/blocked/draft counts + percent
    - **phases**: execution levels with task statuses and blocked_by info
    - **contracts**: summary of contract statuses
    - **attention**: what needs action right now (ready_to_start, open questions,
      unverified assumptions, low-isolation tasks, pending contracts)

    Args:
        root_task_id: Root task ID. Auto-detected if omitted.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        Full roadmap snapshot.
    """
    def _fn(conn, root_task_id):
        rm = task_analysis.roadmap(conn, root_task_id)
        p = rm["progress"]
        return _ok(
            f"Roadmap: {p['total']} tasks, {p['done']} done ({p['percent']}%), "
            f"{p['blocked']} blocked",
            **rm,
        )
    return _run(repo_root, _fn, root_task_id)


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
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        Dict with:
          ``impacted_nodes``  — code nodes in the blast radius
          ``tasks``           — matching open tasks with ``matched_nodes`` field
          ``uncovered_nodes`` — impacted nodes with no task refs
          ``coverage_ratio``  — fraction of impacted nodes covered by tasks
    """
    def _fn(conn, file_paths, root_task_id, max_depth, open_only):
        result = task_analysis.find_tasks_for_impact(
            conn,
            file_paths,
            root_task_id=root_task_id,
            max_depth=max_depth,
            open_only=open_only,
        )
        n_tasks = len(result["tasks"])
        n_nodes = len(result["impacted_nodes"])
        coverage = result["coverage_ratio"]
        return _ok(
            f"Impact radius: {n_nodes} node(s), {n_tasks} open task(s), "
            f"coverage {coverage:.0%}",
            **result,
        )
    return _run(repo_root, _fn, file_paths, root_task_id, max_depth, open_only)


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
    root_task_id: str,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Suggest contracts between tasks with implicit code-level dependencies.

    [BRAINSTORM] Detects pairs of leaf tasks whose code refs are connected
    via code graph edges (calls/imports) but have no explicit task_edge or
    contract between them. These are *hidden dependencies* that need
    interface contracts for safe parallel development.

    Args:
        root_task_id: Root of the subtree to analyze.
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
