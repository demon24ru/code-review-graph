"""Algorithmic analysis layer for the Task DAG.

Pure algorithms operating over task and code graph data. No LLM logic.
All public functions accept a ``sqlite3.Connection`` and return plain dicts.

Single-pipeline discipline
--------------------------
The system enforces a strict one-root-at-a-time workflow:

1. Brainstorm phase: create ONE root task, decompose into subtasks, add
   contracts, notes, code_refs.  Do NOT start implementation until
   ``task_validate`` passes with no errors.
2. Implementation phase: work leaf-by-leaf until all subtasks are ``done``.
3. Close the root (``done`` / ``archived``) before creating a new one.

Many analysis functions accept ``task_id=None`` and will auto-detect the
active root via :func:`get_active_root` so callers rarely need to pass IDs.

Functions:
- get_active_root     — return the single open root task (or None)
- find_conflicts      — tasks that modify the same code nodes
- check_isolation     — isolation score based on external callers/callees
- blast_radius        — code graph impact of a task's code refs
- execution_order     — parallelism-aware execution levels
- validate_dag        — gate-check before handoff to coder
- export_task         — flat data structure for handoff to other workflows
- roadmap             — aggregated progress snapshot
- roadmap_diff        — what changed since a given timestamp
"""

from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from typing import Any, Optional

from .tasks import (
    _collect_ancestor_ids,
    _collect_subtree_ids,
    _row_to_dict,
    get_active_root,
    get_task,
    get_task_code_refs,
    get_task_edges,
    list_notes,
    list_contracts,
    topological_sort_tasks,
)

logger = logging.getLogger(__name__)


def _resolve_root(
    conn: sqlite3.Connection,
    task_id: Optional[str],
    fn_name: str,
) -> str:
    """Resolve *task_id* to a concrete ID.

    If *task_id* is None, returns the ID of the single open root task.
    Raises ``KeyError`` if no open root exists.
    """
    if task_id is not None:
        return task_id
    root = get_active_root(conn)
    if root is None:
        raise KeyError(
            f"{fn_name}: no open root task found. "
            "Create a root task first or pass an explicit task_id."
        )
    return root["id"]


# ---------------------------------------------------------------------------
# Internal BFS helpers over the code graph (edges table)
# ---------------------------------------------------------------------------


def _bfs_code_graph(
    conn: sqlite3.Connection,
    start_node_ids: set[int],
    direction: str,  # "outgoing" | "incoming" | "both"
    depth: int,
) -> set[int]:
    """BFS over ``nodes``/``edges`` code graph tables.

    *direction* controls edge traversal:
    - ``"outgoing"`` follows  source → target  (callee direction)
    - ``"incoming"`` follows  target → source  (caller direction)
    - ``"both"``     follows both directions

    Returns all reachable node IDs (excluding the start nodes).
    """
    visited: set[int] = set(start_node_ids)
    frontier: set[int] = set(start_node_ids)

    for _ in range(depth):
        if not frontier:
            break
        next_frontier: set[int] = set()

        frontier_list = list(frontier)
        batch_size = 450

        # Collect qualified names for frontier nodes
        qn_map: dict[int, str] = {}
        for i in range(0, len(frontier_list), batch_size):
            batch = frontier_list[i : i + batch_size]
            ph = ", ".join("?" * len(batch))
            rows = conn.execute(  # noqa: S608
                f"SELECT id, qualified_name FROM nodes WHERE id IN ({ph})", batch
            ).fetchall()
            for r in rows:
                qn_map[r["id"]] = r["qualified_name"]

        frontier_qns = list(qn_map.values())
        if not frontier_qns:
            break

        # Collect neighbor qualified names
        neighbor_qns: set[str] = set()
        for i in range(0, len(frontier_qns), batch_size):
            batch_qns = frontier_qns[i : i + batch_size]
            ph = ", ".join("?" * len(batch_qns))
            if direction in ("outgoing", "both"):
                rows = conn.execute(  # noqa: S608
                    f"SELECT target_qualified FROM edges WHERE source_qualified IN ({ph})",
                    batch_qns,
                ).fetchall()
                neighbor_qns.update(r[0] for r in rows)
            if direction in ("incoming", "both"):
                rows = conn.execute(  # noqa: S608
                    f"SELECT source_qualified FROM edges WHERE target_qualified IN ({ph})",
                    batch_qns,
                ).fetchall()
                neighbor_qns.update(r[0] for r in rows)

        if not neighbor_qns:
            break

        # Resolve qualified names back to node IDs
        neighbor_qns_list = list(neighbor_qns)
        for i in range(0, len(neighbor_qns_list), batch_size):
            batch_qns = neighbor_qns_list[i : i + batch_size]
            ph = ", ".join("?" * len(batch_qns))
            rows = conn.execute(  # noqa: S608
                f"SELECT id FROM nodes WHERE qualified_name IN ({ph})", batch_qns
            ).fetchall()
            for r in rows:
                nid = r[0]
                if nid not in visited:
                    visited.add(nid)
                    next_frontier.add(nid)

        frontier = next_frontier

    return visited - start_node_ids


def _get_code_node_ids_for_task(conn: sqlite3.Connection, task_id: str) -> set[int]:
    """Return the set of code_node_ids linked to *task_id*."""
    rows = conn.execute(
        "SELECT code_node_id FROM task_code_refs WHERE task_id = ?", (task_id,)
    ).fetchall()
    return {r[0] for r in rows}


def _get_all_covered_node_ids(
    conn: sqlite3.Connection, task_ids: list[str]
) -> dict[str, set[int]]:
    """Return mapping task_id → set of code_node_ids for a list of tasks."""
    result: dict[str, set[int]] = {tid: set() for tid in task_ids}
    if not task_ids:
        return result
    ph = ", ".join("?" * len(task_ids))
    rows = conn.execute(  # noqa: S608
        f"SELECT task_id, code_node_id FROM task_code_refs WHERE task_id IN ({ph})",
        task_ids,
    ).fetchall()
    for r in rows:
        result[r[0]].add(r[1])
    return result


# ---------------------------------------------------------------------------
# 1. find_conflicts
# ---------------------------------------------------------------------------


def find_conflicts(
    conn: sqlite3.Connection,
    root_task_id: str,
    depth: int = 0,
) -> list[dict[str, Any]]:
    """Find pairs of leaf tasks whose code_refs intersect.

    Intersection of code_node_ids indicates potential conflicts:
    - ``both_modify``  — both tasks have a *modifies/creates/deletes* ref
    - ``read_write``   — one reads while the other writes
    - ``shared_ref``   — both reference the same node (other ref types)

    When *depth* >= 1, also detects indirect conflicts: task A modifies node X,
    task B modifies node Y, and X and Y are connected via code graph edges within
    *depth* hops. Indirect conflicts have ``conflict_type: "indirect"`` and a
    ``coupling_nodes`` list of bridging node IDs.

    Returns a list of conflict records (direct conflicts first, then indirect).
    """
    get_task(conn, root_task_id)
    subtree_ids = _collect_subtree_ids(conn, root_task_id)

    # Leaf tasks: not a parent of anything in the subtree
    ph_st = ", ".join("?" * len(subtree_ids))
    parent_ids = set(
        r[0]
        for r in conn.execute(  # noqa: S608
            f"SELECT DISTINCT parent_id FROM tasks WHERE parent_id IN ({ph_st})",
            subtree_ids,
        ).fetchall()
    )
    leaf_ids = [tid for tid in subtree_ids if tid not in parent_ids]

    if len(leaf_ids) < 2:
        return []

    # Fetch code refs per leaf task, including ref_type
    ref_map: dict[str, dict[int, list[str]]] = {lid: defaultdict(list) for lid in leaf_ids}
    ph = ", ".join("?" * len(leaf_ids))
    rows = conn.execute(  # noqa: S608
        f"SELECT task_id, code_node_id, ref_type FROM task_code_refs WHERE task_id IN ({ph})",
        leaf_ids,
    ).fetchall()
    for r in rows:
        ref_map[r[0]][r[1]].append(r[2])

    _write_types = frozenset({"modifies", "creates", "deletes"})

    conflicts: list[dict[str, Any]] = []
    direct_pairs: set[tuple[str, str]] = set()
    leaf_list = list(leaf_ids)
    for i, ta in enumerate(leaf_list):
        for tb in leaf_list[i + 1 :]:
            shared = set(ref_map[ta].keys()) & set(ref_map[tb].keys())
            if not shared:
                continue

            direct_pairs.add((ta, tb))

            # Determine conflict type per shared node
            shared_nodes: list[dict[str, Any]] = []
            for nid in shared:
                types_a = set(ref_map[ta][nid])
                types_b = set(ref_map[tb][nid])
                if types_a & _write_types and types_b & _write_types:
                    conflict_type = "both_modify"
                elif (types_a & _write_types and "reads" in types_b) or (
                    "reads" in types_a and types_b & _write_types
                ):
                    conflict_type = "read_write"
                else:
                    conflict_type = "shared_ref"

                shared_nodes.append({
                    "code_node_id": nid,
                    "task_a_ref_types": sorted(types_a),
                    "task_b_ref_types": sorted(types_b),
                    "conflict_type": conflict_type,
                })

            overall_type = (
                "both_modify"
                if any(n["conflict_type"] == "both_modify" for n in shared_nodes)
                else (
                    "read_write"
                    if any(n["conflict_type"] == "read_write" for n in shared_nodes)
                    else "shared_ref"
                )
            )
            conflicts.append({
                "task_a": ta,
                "task_b": tb,
                "shared_nodes": shared_nodes,
                "conflict_type": overall_type,
            })

    # Indirect conflict detection (depth >= 1)
    if depth >= 1:
        for i, ta in enumerate(leaf_list):
            refs_a = set(ref_map[ta].keys())
            if not refs_a:
                continue
            expanded_a = refs_a | _bfs_code_graph(conn, refs_a, "both", depth)

            for tb in leaf_list[i + 1 :]:
                if (ta, tb) in direct_pairs:
                    continue

                refs_b = set(ref_map[tb].keys())
                if not refs_b:
                    continue
                expanded_b = refs_b | _bfs_code_graph(conn, refs_b, "both", depth)

                # Nodes that bridge the two tasks
                indirect_overlap = (expanded_a & refs_b) | (refs_a & expanded_b)
                if not indirect_overlap:
                    continue

                conflicts.append({
                    "task_a": ta,
                    "task_b": tb,
                    "shared_nodes": [],
                    "conflict_type": "indirect",
                    "coupling_nodes": [{"code_node_id": nid} for nid in indirect_overlap],
                })

    return conflicts


# ---------------------------------------------------------------------------
# 1b. contradiction_report
# ---------------------------------------------------------------------------


def contradiction_report(
    conn: sqlite3.Connection,
    root_task_id: str,
) -> dict[str, Any]:
    """Gather compact data for LLM contradiction analysis.

    Returns a single dict with five keys:
    - ``code_conflicts``     — result of find_conflicts(depth=1)
    - ``all_decisions``      — all decision notes in the subtree
    - ``all_constraints``    — all constraint notes in the subtree
    - ``all_contracts``      — all contracts scoped to root_task_id
    - ``leaf_tasks_summary`` — compact leaf task summary with ref_types
    """
    get_task(conn, root_task_id)
    subtree_ids = _collect_subtree_ids(conn, root_task_id)

    # --- code_conflicts ---
    code_conflicts = find_conflicts(conn, root_task_id, depth=1)

    # --- leaf task detection ---
    ph_st = ", ".join("?" * len(subtree_ids))
    parent_ids_set = set(
        r[0]
        for r in conn.execute(  # noqa: S608
            f"SELECT DISTINCT parent_id FROM tasks WHERE parent_id IN ({ph_st})",
            subtree_ids,
        ).fetchall()
    )
    leaf_ids = [tid for tid in subtree_ids if tid not in parent_ids_set]

    # --- all_decisions and all_constraints ---
    ph = ", ".join("?" * len(subtree_ids))
    note_rows = conn.execute(  # noqa: S608
        f"SELECT n.task_id, n.note_type, n.content, n.resolution, n.status, t.title "
        f"FROM notes n JOIN tasks t ON t.id = n.task_id "
        f"WHERE n.task_id IN ({ph}) AND n.note_type IN ('decision', 'constraint')",
        subtree_ids,
    ).fetchall()

    all_decisions: list[dict[str, Any]] = []
    all_constraints: list[dict[str, Any]] = []
    for r in note_rows:
        if r[1] == "decision":
            all_decisions.append({
                "task_id": r[0],
                "task_title": r[5],
                "content": r[2],
                "resolution": r[3],
                "status": r[4],
            })
        else:
            all_constraints.append({
                "task_id": r[0],
                "task_title": r[5],
                "content": r[2],
                "status": r[4],
            })

    # --- all_contracts ---
    contracts_raw = list_contracts(conn, scope_task_id=root_task_id)
    all_contracts = [
        {
            "id": c["id"],
            "name": c["name"],
            "definition": c["definition"],
            "contract_type": c["contract_type"],
            "status": c["status"],
            "provider_task_ids": c.get("provider_task_ids", []),
            "consumer_task_ids": c.get("consumer_task_ids", []),
        }
        for c in contracts_raw
    ]

    # --- leaf_tasks_summary ---
    leaf_tasks_summary: list[dict[str, Any]] = []
    if leaf_ids:
        ph_leaf = ", ".join("?" * len(leaf_ids))
        task_rows = conn.execute(  # noqa: S608
            f"SELECT id, title, description FROM tasks WHERE id IN ({ph_leaf})",
            leaf_ids,
        ).fetchall()
        id_to_task = {r[0]: r for r in task_rows}

        ref_type_rows = conn.execute(  # noqa: S608
            f"SELECT task_id, ref_type FROM task_code_refs WHERE task_id IN ({ph_leaf})",
            leaf_ids,
        ).fetchall()
        ref_types_by_task: dict[str, set[str]] = {lid: set() for lid in leaf_ids}
        for r in ref_type_rows:
            ref_types_by_task[r[0]].add(r[1])

        for lid in leaf_ids:
            row = id_to_task.get(lid)
            if row is None:
                continue
            desc = row[2] or ""
            leaf_tasks_summary.append({
                "id": lid,
                "title": row[1],
                "short_description": desc[:200],
                "ref_types": sorted(ref_types_by_task[lid]),
            })

    return {
        "code_conflicts": code_conflicts,
        "all_decisions": all_decisions,
        "all_constraints": all_constraints,
        "all_contracts": all_contracts,
        "leaf_tasks_summary": leaf_tasks_summary,
    }


# ---------------------------------------------------------------------------
# 2. check_isolation
# ---------------------------------------------------------------------------


def check_isolation(
    conn: sqlite3.Connection,
    task_id: str,
) -> dict[str, Any]:
    """Score how isolated a task is by measuring external code graph dependencies.

    Algorithm:
    1. Collect code_node_ids from task's code refs (internal nodes).
    2. BFS both directions on the code graph to find callers + callees.
    3. External nodes = reachable nodes NOT in the internal set.
    4. isolation_score = internal / (internal + external), capped at [0, 1].

    Returns::

        {
            internal_nodes: int,
            external_dependencies: int,   # callees outside the task
            external_dependents: int,     # callers outside the task
            isolation_score: float,
            external_nodes: [{ id, name, qualified_name, file_path, ... }]
        }
    """
    internal_ids = _get_code_node_ids_for_task(conn, task_id)

    if not internal_ids:
        return {
            "status": "not_applicable",
            "message": "Task has no code refs linked. Use task_link_code to associate code nodes.",
            "internal_nodes": 0,
            "external_dependencies": 0,
            "external_dependents": 0,
            "isolation_score": None,
            "external_nodes": [],
        }

    callees = _bfs_code_graph(conn, internal_ids, "outgoing", depth=1)
    callers = _bfs_code_graph(conn, internal_ids, "incoming", depth=1)

    external_ids = (callees | callers) - internal_ids

    internal_count = len(internal_ids)
    external_count = len(external_ids)
    score = (
        internal_count / (internal_count + external_count)
        if (internal_count + external_count) > 0
        else 1.0
    )

    # Fetch metadata for external nodes
    external_nodes: list[dict[str, Any]] = []
    if external_ids:
        ext_list = list(external_ids)
        ph = ", ".join("?" * len(ext_list))
        rows = conn.execute(  # noqa: S608
            f"SELECT id, kind, name, qualified_name, file_path, line_start, line_end FROM nodes WHERE id IN ({ph})",
            ext_list,
        ).fetchall()
        external_nodes = [_row_to_dict(r) for r in rows]

    callee_external = len(callees - internal_ids)
    caller_external = len(callers - internal_ids)

    return {
        "status": "ok",
        "internal_nodes": internal_count,
        "external_dependencies": callee_external,
        "external_dependents": caller_external,
        "isolation_score": round(score, 4),
        "external_nodes": external_nodes,
    }


# ---------------------------------------------------------------------------
# 3. blast_radius
# ---------------------------------------------------------------------------


def blast_radius(
    conn: sqlite3.Connection,
    task_id: str,
    depth: int = 2,
    include_affected_nodes: bool = False,
) -> dict[str, Any]:
    """Compute the blast radius of a task through the code graph.

    Starting from the task's code_refs, performs a BFS of depth *depth*
    in both directions. Then cross-references which affected nodes are already
    covered by other tasks (via task_code_refs).

    Args:
        conn: Database connection.
        task_id: Task ID to analyze.
        depth: BFS depth (default: 2).
        include_affected_nodes: If True, include full affected_nodes and
            uncovered_nodes lists. Default False returns only scalar counts
            (affected_nodes_count, uncovered_nodes_count).

    Returns::

        {
            direct_nodes: [...],           # task's own code refs
            affected_nodes_count: int,     # count of reachable nodes (always present)
            affected_nodes: [...],         # reachable within depth (only if include_affected_nodes=True)
            uncovered_nodes: [...],        # full list (only if include_affected_nodes=True)
            uncovered_nodes_count: int,    # count only (only if include_affected_nodes=False)
            coverage_ratio: float
        }
    """
    internal_ids = _get_code_node_ids_for_task(conn, task_id)

    if not internal_ids:
        return {
            "status": "not_applicable",
            "message": "Task has no code refs linked. Use task_link_code to associate code nodes.",
            "direct_nodes": [],
            "affected_nodes_count": 0,
            "uncovered_nodes_count": 0,
            "coverage_ratio": None,
        }

    affected_ids = _bfs_code_graph(conn, internal_ids, "both", depth=depth)
    all_ids = internal_ids | affected_ids

    # Find all code_node_ids covered by any task
    covered_rows = conn.execute(
        "SELECT DISTINCT code_node_id FROM task_code_refs"
    ).fetchall()
    covered_globally: set[int] = {r[0] for r in covered_rows}

    uncovered_ids = all_ids - covered_globally

    def _fetch_nodes(ids: set[int]) -> list[dict[str, Any]]:
        if not ids:
            return []
        id_list = list(ids)
        ph = ", ".join("?" * len(id_list))
        rows = conn.execute(  # noqa: S608
            f"SELECT id, kind, name, qualified_name, file_path, line_start, line_end FROM nodes WHERE id IN ({ph})",
            id_list,
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    all_count = len(all_ids)
    coverage_ratio = (
        round((all_count - len(uncovered_ids)) / all_count, 4) if all_count > 0 else 1.0
    )

    result = {
        "status": "ok",
        "direct_nodes": _fetch_nodes(internal_ids),
        "affected_nodes_count": len(affected_ids),
        "coverage_ratio": coverage_ratio,
    }
    if include_affected_nodes:
        result["affected_nodes"] = _fetch_nodes(affected_ids)
        result["uncovered_nodes"] = _fetch_nodes(uncovered_ids)
    else:
        result["uncovered_nodes_count"] = len(uncovered_ids)
    return result


# ---------------------------------------------------------------------------
# 4. execution_order
# ---------------------------------------------------------------------------


def _compute_task_priority(
    conn: sqlite3.Connection,
    task_id: str,
) -> tuple[float, dict[str, Any]]:
    """Compute a priority score [0..1] for ranking a task within an execution level.

    Signals:
    1. isolation_score (0..1) — from check_isolation. None if no code_refs.
    2. dependents_count — tasks that depend_on this task (it is a blocker).
    3. provider_count — contracts where this task is provider.

    Formula:
    - With code_refs:    isolation*0.6 + min(dependents/5,1.0)*0.3 + min(provider/3,1.0)*0.1
    - Without code_refs: min(dependents/5,1.0)*0.7 + min(provider/3,1.0)*0.3

    Returns:
        (priority_score, ranking_signals) where ranking_signals is a dict with
        raw signal values: isolation_score, dependents_count, provider_count.
    """
    # 1. isolation_score — None if task has no code refs
    isolation_result = check_isolation(conn, task_id)
    if isolation_result.get("status") == "not_applicable":
        isolation_score: Optional[float] = None
    else:
        isolation_score = isolation_result.get("isolation_score")

    # 2. dependents_count — tasks that list task_id as a dependency (blocker count)
    dep_row = conn.execute(
        "SELECT COUNT(*) FROM task_edges WHERE type='depends_on' AND target_task_id=?",
        (task_id,),
    ).fetchone()
    dependents_count: int = dep_row[0] if dep_row else 0

    # 3. provider_count — contracts where this task is the provider
    try:
        contracts = list_contracts(conn, task_id=task_id)
        provider_count: int = sum(
            1 for c in contracts if task_id in c.get("provider_task_ids", [])
        )
    except Exception:
        provider_count = 0

    signals: dict[str, Any] = {
        "isolation_score": isolation_score,
        "dependents_count": dependents_count,
        "provider_count": provider_count,
    }

    dep_term = min(dependents_count / 5.0, 1.0)
    prov_term = min(provider_count / 3.0, 1.0)
    if isolation_score is not None:
        score = isolation_score * 0.6 + dep_term * 0.3 + prov_term * 0.1
    else:
        score = dep_term * 0.7 + prov_term * 0.3

    return round(score, 4), signals


def execution_order(
    conn: sqlite3.Connection,
    root_task_id: str,
    skip_statuses: Optional[set[str]] = None,
) -> dict[str, Any]:
    """Group leaf tasks into parallelism levels based on *depends_on* edges.

    Level 0 = no dependencies; level N = depends only on level < N tasks.

    Args:
        conn: Database connection.
        root_task_id: Root of the subtree to analyze.
        skip_statuses: Set of task statuses to exclude from levels. Defaults to
            {"done", "archived", "in_progress"}. Empty levels are skipped.

    Returns::

        {
            "levels": [
                { "level": 0, "tasks": [task_dict, ...] },
                { "level": 1, "tasks": [...] },
                ...
            ],
            "total_levels": N,
            "total_actionable": M
        }
    """
    if skip_statuses is None:
        skip_statuses = {"done", "archived", "in_progress"}

    get_task(conn, root_task_id)
    subtree_ids = set(_collect_subtree_ids(conn, root_task_id))

    # Leaf tasks only
    subtree_list = list(subtree_ids)
    ph_st = ", ".join("?" * len(subtree_list))
    parent_ids = set(
        r[0]
        for r in conn.execute(  # noqa: S608
            f"SELECT DISTINCT parent_id FROM tasks WHERE parent_id IN ({ph_st})",
            subtree_list,
        ).fetchall()
    )
    leaf_ids = [tid for tid in subtree_ids if tid not in parent_ids]

    if not leaf_ids:
        return {
            "levels": [],
            "total_levels": 0,
            "total_actionable": 0
        }

    leaf_set = set(leaf_ids)

    # Build in-degree and adjacency for depends_on within leaf set
    in_degree: dict[str, int] = {lid: 0 for lid in leaf_ids}
    successors: dict[str, list[str]] = {lid: [] for lid in leaf_ids}

    ph_dep = ", ".join("?" * len(subtree_list))
    dep_rows = conn.execute(  # noqa: S608
        f"SELECT source_task_id, target_task_id FROM task_edges "
        f"WHERE type = 'depends_on' AND source_task_id IN ({ph_dep})",
        subtree_list,
    ).fetchall()

    for r in dep_rows:
        src, tgt = r[0], r[1]
        # src depends_on tgt → tgt must be done before src
        if src in leaf_set and tgt in leaf_set:
            in_degree[src] += 1
            successors[tgt].append(src)

    # BFS level assignment
    levels: list[list[str]] = []
    current_level = [lid for lid in leaf_ids if in_degree[lid] == 0]

    while current_level:
        levels.append(current_level)
        next_level: list[str] = []
        for node in current_level:
            for succ in successors.get(node, []):
                in_degree[succ] -= 1
                if in_degree[succ] == 0:
                    next_level.append(succ)
        current_level = next_level

    # Fetch full task rows
    all_leaf_ids = [tid for level in levels for tid in level]
    ph = ", ".join("?" * len(all_leaf_ids))
    rows = conn.execute(  # noqa: S608
        f"SELECT id, title, status FROM tasks WHERE id IN ({ph})", all_leaf_ids
    ).fetchall()
    id_to_row: dict[str, dict[str, Any]] = {r["id"]: _row_to_dict(r) for r in rows}

    result: list[dict[str, Any]] = []
    total_actionable = 0
    for i, level in enumerate(levels):
        missing = [tid for tid in level if tid not in id_to_row]
        if missing:
            logger.debug("execution_order: %d task(s) missing from id_to_row at level %d: %s", len(missing), i, missing)
        filtered_tasks = [
            id_to_row[tid]
            for tid in level
            if tid in id_to_row and id_to_row[tid]["status"] not in skip_statuses
        ]
        # Add priority scoring and sort within level (highest priority first)
        for _td in filtered_tasks:
            _score, _signals = _compute_task_priority(conn, _td["id"])
            _td["priority_score"] = _score
            _td["ranking_signals"] = _signals
        filtered_tasks.sort(key=lambda t: t["priority_score"], reverse=True)
        if filtered_tasks:
            result.append({
                "level": len(result),
                "tasks": filtered_tasks,
            })
            total_actionable += len(filtered_tasks)

    return {
        "levels": result,
        "total_levels": len(result),
        "total_actionable": total_actionable
    }


# ---------------------------------------------------------------------------
# 5. validate_dag
# ---------------------------------------------------------------------------


def validate_dag(
    conn: sqlite3.Connection,
    root_task_id: Optional[str] = None,
) -> dict[str, Any]:
    """Gate-check before handing off the DAG to a coder.

    If *root_task_id* is None, the active root task is auto-detected.

    Runs a set of algorithmic checks and returns::

        {
            errors:   [str, ...],   # blocking issues
            warnings: [str, ...],   # non-blocking, worth addressing
            ok:       [str, ...],   # passed checks
        }
    """
    root_task_id = _resolve_root(conn, root_task_id, "validate_dag")
    get_task(conn, root_task_id)
    subtree_ids = _collect_subtree_ids(conn, root_task_id)

    errors: list[str] = []
    warnings: list[str] = []
    ok: list[str] = []

    # --- Fetch all tasks in subtree ---
    ph = ", ".join("?" * len(subtree_ids))
    task_rows = conn.execute(  # noqa: S608
        f"SELECT * FROM tasks WHERE id IN ({ph})", subtree_ids
    ).fetchall()
    tasks_by_id: dict[str, dict[str, Any]] = {r["id"]: _row_to_dict(r) for r in task_rows}

    parent_ids_in_subtree = set(
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT parent_id FROM tasks WHERE parent_id IS NOT NULL"
        ).fetchall()
    ) & set(subtree_ids)

    leaf_ids = [tid for tid in subtree_ids if tid not in parent_ids_in_subtree]
    leaf_tasks = [tasks_by_id[lid] for lid in leaf_ids if lid in tasks_by_id]

    # --- Check 1: Cyclic depends_on ---
    try:
        topological_sort_tasks(conn, root_task_id)
        ok.append("No circular dependencies in depends_on edges")
    except ValueError:
        errors.append("Cycle detected in depends_on edges — topological sort impossible")

    # --- Check 2: Ready tasks depend only on done tasks ---
    dep_rows = conn.execute(  # noqa: S608
        f"SELECT source_task_id, target_task_id FROM task_edges "
        f"WHERE type = 'depends_on' AND source_task_id IN ({ph})",
        subtree_ids,
    ).fetchall()
    dep_map: dict[str, list[str]] = defaultdict(list)
    for r in dep_rows:
        dep_map[r[0]].append(r[1])

    blocked_ready: list[str] = []
    for task in leaf_tasks:
        if task["status"] == "ready":
            for dep_id in dep_map.get(task["id"], []):
                dep_task = tasks_by_id.get(dep_id)
                if dep_task and dep_task["status"] != "done":
                    blocked_ready.append(
                        f"task ({task['id']}) '{task['title']}' is 'ready' but depends on "
                        f"({dep_task['id']}) '{dep_task['title']}' which is '{dep_task['status']}'"
                    )

    if blocked_ready:
        errors.extend(blocked_ready)
    else:
        ok.append("All ready tasks have their dependencies satisfied")

    # --- Check 3: Leaf tasks with no code_refs ---
    covered_tasks = set(
        r[0]
        for r in conn.execute(  # noqa: S608
            f"SELECT DISTINCT task_id FROM task_code_refs WHERE task_id IN ({ph})",
            subtree_ids,
        ).fetchall()
    )
    no_refs = [
        t for t in leaf_tasks
        if t["id"] not in covered_tasks and t["status"] not in ("archived", "done")
    ]
    if no_refs:
        for t in no_refs:
            warnings.append(
                f"Leaf task ({t['id']}) '{t['title']}' has no code refs — impact on codebase is unknown"
            )
    else:
        ok.append("All leaf tasks have at least one code ref")

    # --- Check 4: Open questions ---
    open_questions = conn.execute(  # noqa: S608
        f"SELECT count(*) FROM notes WHERE task_id IN ({ph}) AND note_type = 'question' AND status = 'open'",
        subtree_ids,
    ).fetchone()[0]
    if open_questions > 0:
        errors.append(f"{open_questions} unresolved question(s) remain — resolve before handing off")
    else:
        ok.append("No open questions")

    # --- Check 4b: Notes awaiting LLM validation ---
    answered_notes_count = conn.execute(  # noqa: S608
        f"SELECT count(*) FROM notes WHERE task_id IN ({ph}) AND status = 'answered'",
        subtree_ids,
    ).fetchone()[0]
    if answered_notes_count > 0:
        errors.append(
            f"{answered_notes_count} note(s) answered by user but not yet validated by LLM — "
            "call note_list(status='answered') to review and update to resolved/rejected"
        )
    else:
        ok.append("No notes pending LLM validation")

    # --- Check 5: Unverified assumptions ---
    open_assumptions = conn.execute(  # noqa: S608
        f"SELECT count(*) FROM notes WHERE task_id IN ({ph}) AND note_type = 'assumption' AND status = 'open'",
        subtree_ids,
    ).fetchone()[0]
    if open_assumptions > 0:
        errors.append(f"{open_assumptions} unverified assumption(s) — verify before coding")
    else:
        ok.append("No unverified assumptions")

    # --- Check 5b: Unresolved constraints ---
    open_constraints = conn.execute(  # noqa: S608
        f"SELECT count(*) FROM notes WHERE task_id IN ({ph}) AND note_type = 'constraint' AND status = 'open'",
        subtree_ids,
    ).fetchone()[0]
    if open_constraints > 0:
        errors.append(f"{open_constraints} unresolved constraint(s) — resolve before coding")
    else:
        ok.append("No unresolved constraints")

    # --- Check 5c: Unresolved risks ---
    open_risks = conn.execute(  # noqa: S608
        f"SELECT count(*) FROM notes WHERE task_id IN ({ph}) AND note_type = 'risk' AND status = 'open'",
        subtree_ids,
    ).fetchone()[0]
    if open_risks > 0:
        errors.append(f"{open_risks} unresolved risk(s) — verify before coding")
    else:
        ok.append("No unresolved risk")

    # --- Check 6: Contracts in 'proposed' status where active tasks are participants ---
    proposed_contract_ids = set(
        r[0]
        for r in conn.execute(  # noqa: S608
            f"SELECT DISTINCT cl.contract_id "
            f"FROM contract_links cl "
            f"JOIN contracts c ON c.id = cl.contract_id "
            f"WHERE cl.task_id IN ({ph}) AND c.status = 'proposed'",
            subtree_ids,
        ).fetchall()
    )

    active_proposed: list[str] = []
    for cid in proposed_contract_ids:
        # Find any active (ready/in_progress) participant
        try:
            participant_tasks = conn.execute(
                "SELECT task_id FROM contract_links WHERE contract_id = ?", (cid,)
            ).fetchall()
        except Exception:
            continue
        cname_row = conn.execute("SELECT name, contract_type FROM contracts WHERE id=?", (cid,)).fetchone()
        cname = cname_row[0] if cname_row else cid
        for (ptask_id,) in participant_tasks:
            t = tasks_by_id.get(ptask_id)
            if t and t.get("status") in ("ready", "in_progress"):
                active_proposed.append(
                    f"Contract ({cid}) '{cname}' is still 'proposed' but task "
                    f"'({t['id']}) {t['title']}' is '{t['status']}'"
                )
                break

    if active_proposed:
        errors.extend(active_proposed)
    else:
        ok.append("All active contracts are agreed or better")

    # --- Check 7: Leaf tasks missing acceptance_criteria ---
    missing_ac = [
        t for t in leaf_tasks
        if not t.get("acceptance_criteria") and t["status"] not in ("archived", "done")
    ]
    if missing_ac:
        for t in missing_ac:
            if t['status'] in ("ready", "in_progress"):
                errors.append(
                    f"Leaf task ({t['id']}) '{t['title']}' is {t['status']} "
                    f"but has no acceptance_criteria"
                )
            else:
                warnings.append(f"Leaf task ({t['id']}) '{t['title']}' has no acceptance_criteria should fill them")
    else:
        ok.append("All active leaf tasks have acceptance_criteria")

    # --- Check 8: Leaf tasks missing description ---
    missing_desc = [
        t for t in leaf_tasks
        if not t.get("description") and t["status"] not in ("archived", "done")
    ]
    if missing_desc:
        for t in missing_desc:
            errors.append(f"Leaf task ({t['id']}) '{t['title']}' has no description — coder cannot proceed")
    else:
        ok.append("All active leaf tasks have descriptions")

    # --- Check 9: Non-leaf tasks with direct code_refs or contract_links ---
    # When a parent task has been further decomposed, its code_refs and
    # contract_links should live on the leaves, not the parent.
    mixed_tasks: list[tuple[str, str, list[str]]] = []
    for tid in subtree_ids:
        if tid in parent_ids_in_subtree:  # this task has children
            has_refs = conn.execute(
                "SELECT 1 FROM task_code_refs WHERE task_id = ? LIMIT 1", (tid,)
            ).fetchone()
            has_links: Any = None
            try:
                has_links = conn.execute(
                    "SELECT 1 FROM contract_links WHERE task_id = ? LIMIT 1", (tid,)
                ).fetchone()
            except Exception:
                pass
            if has_refs or has_links:
                t = tasks_by_id.get(tid)
                name = t["title"] if t else tid
                ref_ids = [
                    str(r[0])
                    for r in conn.execute(
                        "SELECT code_node_id FROM task_code_refs WHERE task_id = ?", (tid,)
                    ).fetchall()
                ]
                mixed_tasks.append((name, tid, ref_ids))
    if mixed_tasks:
        for name, tid, ref_ids in mixed_tasks:
            refs_hint = f" (code_node_ids: {', '.join(ref_ids)})" if ref_ids else ""
            warnings.append(
                f"Parent task ({tid}) '{name}' has direct code_refs or contract links "
                f"but also has subtasks — move refs to leaf tasks{refs_hint}"
            )
    else:
        ok.append("No parent tasks with misplaced direct code_refs or contract links")

    return {"errors": errors, "warnings": warnings, "ok": ok}


# ---------------------------------------------------------------------------
# 6. build_context
# ---------------------------------------------------------------------------


def _find_sibling_conflicts(
    conn: sqlite3.Connection,
    task_id: str,
    parent_id: Optional[str],
) -> list[dict[str, Any]]:
    """Find conflicts between *task_id* and its siblings under the same parent."""
    if parent_id is None:
        return []

    sibling_rows = conn.execute(
        "SELECT id FROM tasks WHERE parent_id = ? AND id != ?", (parent_id, task_id)
    ).fetchall()
    sibling_ids = [r[0] for r in sibling_rows]

    if not sibling_ids:
        return []

    task_refs = _get_code_node_ids_for_task(conn, task_id)
    if not task_refs:
        return []

    conflicts: list[dict[str, Any]] = []
    for sib_id in sibling_ids:
        sib_refs = _get_code_node_ids_for_task(conn, sib_id)
        shared = task_refs & sib_refs
        if shared:
            sib_task = get_task(conn, sib_id)
            conflicts.append({
                "sibling_task_id": sib_id,
                "sibling_title": sib_task.get("title"),
                "shared_node_count": len(shared),
                "shared_node_ids": list(shared),
            })

    return conflicts


def generate_mermaid_dag(
    tasks: list[dict[str, Any]],
    dependencies: list[tuple[str, str]],
) -> str:
    """Generate a Mermaid TD diagram for a task hierarchy with dependency edges.

    Args:
        tasks: List of dicts with keys: id, title, parent_id, status.
        dependencies: List of (source_id, target_id) tuples for depends_on edges.

    Returns:
        A Mermaid ``graph TD`` string with subgraphs for nodes that have
        children and plain node declarations for leaves.  Double-quotes in
        titles are escaped as ``&quot;``.  Subgraph blocks are indented with
        2 extra spaces relative to their parent.
    """
    lines: list[str] = ["graph TD"]

    if not tasks:
        return "\n".join(lines)

    def _escape(text: str) -> str:
        return (text or "").replace('"', "&quot;")

    task_ids: set[str] = {t["id"] for t in tasks}
    task_map: dict[str, dict[str, Any]] = {t["id"]: t for t in tasks}

    # Build parent → children mapping and identify roots
    children_map: dict[str, list[str]] = {t["id"]: [] for t in tasks}
    roots: list[str] = []
    for t in tasks:
        pid = t.get("parent_id")
        if pid and pid in task_ids:
            children_map[pid].append(t["id"])
        else:
            roots.append(t["id"])

    def _render(task_id: str, indent: int) -> None:
        t = task_map[task_id]
        prefix = "  " * indent
        title = _escape(t.get("title") or "")
        children = children_map.get(task_id, [])
        if children:
            lines.append(f'{prefix}subgraph cluster_{task_id} ["{title}"]')
            for child_id in children:
                _render(child_id, indent + 1)
            lines.append(f"{prefix}end")
        else:
            lines.append(f'{prefix}{task_id}["{title}"]')

    # Identify which task ids are rendered as subgraphs (have children)
    parent_ids: set[str] = {tid for tid, children in children_map.items() if children}

    def _mermaid_ref(task_id: str) -> str:
        """Return the Mermaid node reference for a task id.

        Tasks with children are rendered as ``subgraph cluster_<id>`` so their
        reference in edge declarations must also use the ``cluster_`` prefix.
        Leaf tasks are plain nodes referenced by their bare id.
        """
        return f"cluster_{task_id}" if task_id in parent_ids else task_id

    for root_id in roots:
        _render(root_id, 1)

    if dependencies:
        lines.append("  %% Dependencies")
        for src, tgt in dependencies:
            lines.append(f"  {_mermaid_ref(src)} --> {_mermaid_ref(tgt)}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 7. export_task
# ---------------------------------------------------------------------------


def export_task(
    conn: sqlite3.Connection,
    task_id: Optional[str] = None,
    *,
    include_analysis: bool = False,
    include_source: bool = False,
    repo_root: Optional[str] = None,
) -> dict[str, Any]:
    """Flat data export of a task for handoff to other workflows.

    The single entry point for task context — replaces the old
    ``build_context`` function.  By default returns a lightweight snapshot;
    use flags to add expensive computed fields.

    If *task_id* is None, the active root task is auto-detected via
    :func:`get_active_root`.

    Args:
        task_id: Task to export. Defaults to the active root task.
        include_analysis: If True, append ``isolation``, ``conflicts``, and
            ``pipeline_state``.  Equivalent to the old ``build_context`` output.
        include_source: If True, attach ``source_snippet`` (line range) to
            each code ref so the LLM knows where to look without reading files.
        repo_root: Base path for resolving relative file paths.

    Returns a dict with:
    - ``task``, ``parent_chain``, ``subtasks`` (with edges)
    - ``edges``: ``{incoming, outgoing}``
    - ``related_tasks``: task edges as flat list with direction
    - ``code_refs``: linked code nodes (+ line range if include_source)
    - ``notes``: all notes from task + ancestors
    - ``contracts``: ``{as_provider, as_consumer}`` with participant lists
    - ``isolation``: score + external_deps  (only if include_analysis)
    - ``conflicts``: sibling code conflicts  (only if include_analysis)
    - ``pipeline_state``: open items summary (only if include_analysis)
    - ``open_items``: always present — counts of open questions/assumptions/contracts
    """
    task_id = _resolve_root(conn, task_id, "export_task")
    task = get_task(conn, task_id)

    # Parent chain
    ancestor_ids = _collect_ancestor_ids(conn, task_id)
    parent_chain = []
    for aid in ancestor_ids[1:]:
        try:
            p = get_task(conn, aid)
            parent_chain.append({"id": p["id"], "title": p["title"],
                                  "description": p.get("description")})
        except KeyError:
            pass

    # Subtasks with their edges
    subtask_rows = conn.execute(
        "SELECT * FROM tasks WHERE parent_id = ? ORDER BY created_at", (task_id,)
    ).fetchall()
    subtasks = []
    for r in subtask_rows:
        sub = _row_to_dict(r)
        sub["edges"] = get_task_edges(conn, sub["id"], "both")
        subtasks.append(sub)

    # Subtask code refs rollup — collect from all leaf descendants
    subtask_code_refs_summary: dict[str, Any] = {}
    if subtasks:
        subtree_ids = _collect_subtree_ids(conn, task_id)
        leaf_ids_in_subtree = []
        if len(subtree_ids) > 1:  # has descendants
            all_parents = {
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT parent_id FROM tasks WHERE parent_id IS NOT NULL AND parent_id IN ({})".format(
                        ", ".join("?" * len(subtree_ids))
                    ),
                    subtree_ids,
                ).fetchall()
            }
            leaf_ids_in_subtree = [tid for tid in subtree_ids if tid not in all_parents and tid != task_id]

        if leaf_ids_in_subtree:
            # Count unique code nodes across all leaf tasks
            all_leaf_node_ids: set[int] = set()
            leaf_refs_by_task: list[dict[str, Any]] = []
            for leaf_id in leaf_ids_in_subtree:
                try:
                    leaf_task = get_task(conn, leaf_id)
                    refs = get_task_code_refs(conn, leaf_id)
                    if refs:
                        leaf_refs_by_task.append({
                            "task_id": leaf_id,
                            "task_title": leaf_task.get("title"),
                            "code_ref_count": len(refs),
                        })
                        all_leaf_node_ids.update(r.get("code_node_id") for r in refs if r.get("code_node_id"))
                except (KeyError, Exception):
                    pass
            subtask_code_refs_summary = {
                "leaf_task_count": len(leaf_ids_in_subtree),
                "total_unique_code_nodes": len(all_leaf_node_ids),
                "tasks_with_code_refs": len(leaf_refs_by_task),
                "tasks_without_code_refs": len(leaf_ids_in_subtree) - len(leaf_refs_by_task),
                "per_task": leaf_refs_by_task,
            }

    # Edges: both incoming and outgoing
    edges_raw = get_task_edges(conn, task_id, "both")
    edges = {
        "incoming": [e for e in edges_raw if e.get("target_task_id") == task_id],
        "outgoing": [e for e in edges_raw if e.get("source_task_id") == task_id],
    }

    # related_tasks: flat list for easy scanning
    related: list[dict[str, Any]] = []
    for e in edges_raw:
        direction = "outgoing" if e.get("source_task_id") == task_id else "incoming"
        other_id = e.get("target_task_id") if direction == "outgoing" else e.get("source_task_id")
        try:
            other = get_task(conn, other_id)
            related.append({
                "id": other_id,
                "title": other.get("title"),
                "status": other.get("status", "draft"),
                "edge_type": e.get("type"),
                "direction": direction,
            })
        except (KeyError, TypeError):
            related.append({"id": other_id, "status": "draft", "edge_type": e.get("type"), "direction": direction})

    # Code refs
    code_refs = get_task_code_refs(conn, task_id,
                                   include_source=include_source, repo_root=repo_root) or []

    # Notes from task + ancestor chain
    all_notes = list_notes(conn, task_id, include_parent=True) or []

    # Contracts — use new list_contracts(task_id=...) for participant-based lookup
    try:
        contracts_raw = list_contracts(conn, task_id=task_id)
    except Exception:
        contracts_raw = []
    contracts_as_provider = [
        c for c in contracts_raw if task_id in c.get("provider_task_ids", [])
    ]
    contracts_as_consumer = [
        c for c in contracts_raw if task_id in c.get("consumer_task_ids", [])
    ]

    # Open items summary (always included)
    open_questions = sum(
        1 for n in all_notes if n.get("note_type") == "question" and n.get("status") == "open"
    )
    unresolved_constraints = sum(
        1 for n in all_notes if n.get("note_type") == "constraint" and n.get("status") == "open"
    )
    unverified_assumptions = sum(
        1 for n in all_notes if n.get("note_type") == "assumption" and n.get("status") == "open"
    )
    unverified_risk = sum(
        1 for n in all_notes if n.get("note_type") == "risk" and n.get("status") == "open"
    )
    pending_contracts = sum(1 for c in contracts_raw if c.get("status") == "proposed")

    # Mermaid DAG diagram — always generated for the full subtree
    _mermaid_subtree_ids = list(_collect_subtree_ids(conn, task_id))
    if _mermaid_subtree_ids:
        _ph_m = ", ".join("?" * len(_mermaid_subtree_ids))
        _mermaid_task_rows = conn.execute(  # noqa: S608
            f"SELECT id, title, parent_id, status FROM tasks WHERE id IN ({_ph_m})",
            _mermaid_subtree_ids,
        ).fetchall()
        _mermaid_tasks_list: list[dict[str, Any]] = [
            _row_to_dict(r) for r in _mermaid_task_rows
        ]
        _mermaid_dep_rows = conn.execute(  # noqa: S608
            f"SELECT source_task_id, target_task_id FROM task_edges "
            f"WHERE type='depends_on' AND source_task_id IN ({_ph_m})",
            _mermaid_subtree_ids,
        ).fetchall()
        _mermaid_subtree_set = set(_mermaid_subtree_ids)
        _mermaid_deps: list[tuple[str, str]] = [
            (r[0], r[1]) for r in _mermaid_dep_rows if r[1] in _mermaid_subtree_set
        ]
    else:
        _mermaid_tasks_list = []
        _mermaid_deps = []
    mermaid_diagram = generate_mermaid_dag(_mermaid_tasks_list, _mermaid_deps)

    result: dict[str, Any] = {
        "task": task,
        "parent_chain": parent_chain,
        "subtasks": subtasks,
        "edges": edges,
        "related_tasks": related,
        "code_refs": code_refs,
        "notes": all_notes,
        "contracts": {
            "as_provider": contracts_as_provider,
            "as_consumer": contracts_as_consumer,
        },
        "open_items": {
            "unresolved_questions": open_questions,
            "unresolved_constraints": unresolved_constraints,
            "unverified_assumptions": unverified_assumptions,
            "unverified_risk": unverified_risk,
            "pending_contracts": pending_contracts,
        },
        "subtask_code_refs_summary": subtask_code_refs_summary,
        "mermaid_diagram": mermaid_diagram,
    }

    if include_analysis:
        # Isolation score
        isolation_raw = check_isolation(conn, task_id)
        result["isolation"] = {
            "score": isolation_raw.get("isolation_score"),
            "external_deps": isolation_raw.get("external_dependencies"),
        }
        # Conflicts with sibling tasks
        result["conflicts"] = _find_sibling_conflicts(conn, task_id, task.get("parent_id"))
        # Pipeline state
        is_ready = (
            open_questions == 0
            and unverified_assumptions == 0
            and unresolved_constraints == 0
            and pending_contracts == 0
        )
        if is_ready:
            summary = "Ready for handoff"
        else:
            reasons = []
            if open_questions:
                reasons.append(f"{open_questions} open question(s)")
            if unverified_assumptions:
                reasons.append(f"{unverified_assumptions} unverified assumption(s)")
            if unresolved_constraints:
                reasons.append(f"{unresolved_constraints} unresolved constraint(s)")
            if pending_contracts:
                reasons.append(f"{pending_contracts} pending contract(s)")
            summary = f"NOT ready — {', '.join(reasons)}"

        result["pipeline_state"] = {
            "open_questions": open_questions,
            "open_assumptions": unverified_assumptions,
            "open_constraints": unresolved_constraints,
            "pending_contracts": pending_contracts,
            "ready_for_coder": is_ready,
            "summary": summary,
        }

    return result


# ---------------------------------------------------------------------------
# 8. roadmap
# ---------------------------------------------------------------------------


def roadmap(
    conn: sqlite3.Connection,
    root_task_id: Optional[str] = None,
    include_archived: bool = False,
) -> dict[str, Any]:
    """Aggregated progress snapshot for the root task and its entire subtree.

    If *root_task_id* is None, the active root task is auto-detected.

    Args:
        root_task_id: Root task ID. Auto-detected if omitted.
        include_archived: If True, include archived tasks in *phases* and add
            an ``archived`` section listing them. Default False — archived
            tasks are excluded from phases to keep the roadmap readable.

    Returns a structured roadmap useful for LLM orientation::

        {
            root: { id, title, status },
            progress: { total, done, in_progress, ready, blocked, draft, archived, percent },
            phases: [ { level, tasks: [{ id, title, status, blocked_by }] } ],
            archived: [ { id, title, archive_reason } ],   # only when include_archived=True
            contracts: { total, agreed, pending, pending_list },
            notes_count: int,
            attention: {
                ready_to_start: [task_id],
                unresolved_questions: [note],
                unverified_assumptions: [note],
                low_isolation: [{ id, title, isolation_score }],
                pending_contracts: [contract]
            }
        }
    """
    root_task_id = _resolve_root(conn, root_task_id, "roadmap")
    root = get_task(conn, root_task_id)
    subtree_ids = _collect_subtree_ids(conn, root_task_id)

    ph = ", ".join("?" * len(subtree_ids))
    task_rows = conn.execute(  # noqa: S608
        f"SELECT * FROM tasks WHERE id IN ({ph})", subtree_ids
    ).fetchall()
    all_tasks = [_row_to_dict(r) for r in task_rows]

    # Progress counts
    status_counts: dict[str, int] = defaultdict(int)
    for t in all_tasks:
        status_counts[t["status"]] += 1

    total = len(all_tasks)
    done = status_counts.get("done", 0)
    percent = round(done / total * 100) if total > 0 else 0

    # Determine blocked tasks: tasks that depend on non-done tasks
    dep_rows = conn.execute(  # noqa: S608
        f"SELECT source_task_id, target_task_id FROM task_edges "
        f"WHERE type = 'depends_on' AND source_task_id IN ({ph})",
        subtree_ids,
    ).fetchall()
    task_deps: dict[str, list[str]] = defaultdict(list)
    for r in dep_rows:
        task_deps[r[0]].append(r[1])

    tasks_by_id = {t["id"]: t for t in all_tasks}
    blocked_by: dict[str, list[str]] = {}
    for t in all_tasks:
        if t["status"] in ("draft", "refined", "ready"):
            unmet_deps = [
                dep_id
                for dep_id in task_deps.get(t["id"], [])
                if tasks_by_id.get(dep_id, {}).get("status") != "done"
            ]
            if unmet_deps:
                blocked_by[t["id"]] = unmet_deps

    blocked_count = len(blocked_by)

    # Collect archived tasks separately (always, regardless of include_archived)
    archived_tasks = [
        {"id": t["id"], "title": t["title"], "archive_reason": t.get("archive_reason")}
        for t in all_tasks
        if t["status"] == "archived"
    ]
    archived_ids = {t["id"] for t in archived_tasks}

    # Build phases via execution_order — exclude archived tasks unless requested
    try:
        _exec_order = execution_order(conn, root_task_id)
        phases_raw = _exec_order["levels"]
        phases = []
        for phase in phases_raw:
            phase_tasks = []
            for t in phase["tasks"]:
                if not include_archived and t["id"] in archived_ids:
                    continue
                entry = {
                    "id": t["id"],
                    "title": t["title"],
                    "status": t["status"],
                }
                if t["id"] in blocked_by:
                    entry["blocked_by"] = [
                        tasks_by_id.get(uid, {}).get("title", uid)
                        for uid in blocked_by[t["id"]]
                    ]
                phase_tasks.append(entry)
            # Drop empty phases (all tasks were archived)
            if phase_tasks:
                phases.append({"level": phase["level"], "tasks": phase_tasks})
    except ValueError:
        phases = []

    # Contracts summary — by scope_task_id; also include contracts
    # linked to subtree tasks directly (in case scope was not set)
    contract_rows = conn.execute(  # noqa: S608
        f"SELECT DISTINCT c.* FROM contracts c "
        f"WHERE c.scope_task_id IN ({ph})",
        subtree_ids,
    ).fetchall()
    if not contract_rows:
        contract_rows = conn.execute(  # noqa: S608
            f"SELECT DISTINCT c.* FROM contracts c "
            f"JOIN contract_links cl ON cl.contract_id = c.id "
            f"WHERE cl.task_id IN ({ph})",
            subtree_ids,
        ).fetchall()
    contracts_list = [_row_to_dict(r) for r in contract_rows]
    pending_contracts = [c for c in contracts_list if c["status"] == "proposed"]

    # Notes count and breakdown by type+status
    notes_count = conn.execute(  # noqa: S608
        f"SELECT count(*) FROM notes WHERE task_id IN ({ph})", subtree_ids
    ).fetchone()[0]
    open_questions_count = conn.execute(  # noqa: S608
        f"SELECT count(*) FROM notes WHERE task_id IN ({ph}) AND note_type = 'question' AND status = 'open'",
        subtree_ids,
    ).fetchone()[0]
    open_assumptions_count = conn.execute(  # noqa: S608
        f"SELECT count(*) FROM notes WHERE task_id IN ({ph}) AND note_type = 'assumption' AND status = 'open'",
        subtree_ids,
    ).fetchone()[0]
    open_constraints_count = conn.execute(  # noqa: S608
        f"SELECT count(*) FROM notes WHERE task_id IN ({ph}) AND note_type = 'constraint' AND status = 'open'",
        subtree_ids,
    ).fetchone()[0]

    open_risk_count = conn.execute(  # noqa: S608
        f"SELECT count(*) FROM notes WHERE task_id IN ({ph}) AND note_type = 'risk' AND status = 'open'",
        subtree_ids,
    ).fetchone()[0]

    # Attention block
    ready_to_start = [
        t["id"]
        for t in all_tasks
        if t["status"] == "ready" and t["id"] not in blocked_by
    ]

    unresolved_q = conn.execute(  # noqa: S608
        f"SELECT * FROM notes WHERE task_id IN ({ph}) AND note_type = 'question' AND status = 'open'",
        subtree_ids,
    ).fetchall()

    unresolved_a = conn.execute(  # noqa: S608
        f"SELECT * FROM notes WHERE task_id IN ({ph}) AND note_type = 'assumption' AND status = 'open'",
        subtree_ids,
    ).fetchall()

    unresolved_c = conn.execute(  # noqa: S608
        f"SELECT * FROM notes WHERE task_id IN ({ph}) AND note_type = 'constraint' AND status = 'open'",
        subtree_ids,
    ).fetchall()

    unresolved_r = conn.execute(  # noqa: S608
        f"SELECT * FROM notes WHERE task_id IN ({ph}) AND note_type = 'risk' AND status = 'open'",
        subtree_ids,
    ).fetchall()

    answered_notes_rows = conn.execute(  # noqa: S608
        f"SELECT * FROM notes WHERE task_id IN ({ph}) AND status = 'answered'",
        subtree_ids,
    ).fetchall()

    # Low isolation: check leaf tasks with code refs
    parent_ids_set = set(
        r[0]
        for r in conn.execute(  # noqa: S608
            f"SELECT DISTINCT parent_id FROM tasks WHERE parent_id IN ({ph})",
            subtree_ids,
        ).fetchall()
    )
    leaf_ids_with_refs = set(
        r[0]
        for r in conn.execute(  # noqa: S608
            f"SELECT DISTINCT task_id FROM task_code_refs WHERE task_id IN ({ph})",
            subtree_ids,
        ).fetchall()
        if r[0] not in parent_ids_set
    )

    low_isolation: list[dict[str, Any]] = []
    for lid in list(leaf_ids_with_refs)[:10]:  # cap at 10 to avoid heavy computation
        iso = check_isolation(conn, lid)
        if iso.get("isolation_score") is not None and iso["isolation_score"] < 0.5:
            t = tasks_by_id.get(lid, {})
            low_isolation.append({
                "id": lid,
                "title": t.get("title"),
                "isolation_score": iso["isolation_score"],
            })

    result: dict[str, Any] = {
        "root": {"id": root["id"], "title": root["title"], "status": root["status"]},
        "progress": {
            "total": total,
            "done": done,
            "in_progress": status_counts.get("in_progress", 0),
            "ready": status_counts.get("ready", 0),
            "blocked": blocked_count,
            "draft": status_counts.get("draft", 0),
            "archived": status_counts.get("archived", 0),
            "percent": percent,
        },
        "phases": phases,
        "contracts": {
            "total": len(contracts_list),
            "agreed": sum(1 for c in contracts_list if c["status"] in ("agreed", "implemented", "verified")),
            "pending": len(pending_contracts),
            "pending_list": pending_contracts,
        },
        "notes_count": notes_count,
        "notes_summary": {
            "total": notes_count,
            "open_questions": open_questions_count,
            "open_assumptions": open_assumptions_count,
            "open_constraints": open_constraints_count,
            "open_risk": open_risk_count,
        },
        "attention": {
            "ready_to_start": ready_to_start,
            "answered_notes": [_row_to_dict(r) for r in answered_notes_rows],
            "unresolved_questions": [_row_to_dict(r) for r in unresolved_q],
            "unverified_assumptions": [_row_to_dict(r) for r in unresolved_a],
            "unverified_constraints": [_row_to_dict(r) for r in unresolved_c],
            "unverified_risk": [_row_to_dict(r) for r in unresolved_r],
            "low_isolation": low_isolation,
            "pending_contracts": pending_contracts,
        },
    }
    if include_archived:
        result["archived"] = archived_tasks
    return result


# ---------------------------------------------------------------------------
# 9. check_parent_rollup
# ---------------------------------------------------------------------------

#: Statuses considered "terminal" for rollup purposes.
_TERMINAL_STATUSES: frozenset[str] = frozenset({"done", "archived"})
#: Statuses considered "successfully closed" (not just abandoned).
_DONE_STATUSES: frozenset[str] = frozenset({"done"})
#: Statuses considered "abandoned".
_ARCHIVED_STATUSES: frozenset[str] = frozenset({"archived"})


def check_parent_rollup(
    conn: sqlite3.Connection,
    task_id: str,
) -> dict[str, Any]:
    """Check whether a task's parent (and ancestors) can change status.

    Walks up the parent chain from *task_id* and for each ancestor reports:
    - whether all direct children are in terminal status (done / archived)
    - a suggested action with the reasoning

    Does **not** modify any data — purely analytical.

    Returns::

        {
            "task_id": "...",
            "parent_chain": [
                {
                    "id": "...",
                    "title": "...",
                    "current_status": "in_progress",
                    "children_total": 3,
                    "children_done": 2,
                    "children_archived": 1,
                    "blocking_children": [
                        { "id": "...", "title": "...", "status": "ready" }
                    ],
                    "can_close": false,   # all children done (none archived)
                    "can_archive": false, # all children archived
                    "can_complete": false,# all children terminal (done|archived)
                    "suggestion": "2/3 children done, 1 blocking. Cannot close yet."
                },
                ...   # up to root
            ],
            "immediate_parent": { ... }   # first entry, None if task is root
        }
    """
    task = get_task(conn, task_id)
    parent_id = task.get("parent_id")

    chain: list[dict[str, Any]] = []
    current_id = parent_id

    while current_id:
        try:
            parent = get_task(conn, current_id)
        except KeyError:
            break

        # Direct children of this parent
        child_rows = conn.execute(
            "SELECT id, title, status FROM tasks WHERE parent_id = ?",
            (current_id,),
        ).fetchall()
        children = [{"id": r[0], "title": r[1], "status": r[2]} for r in child_rows]

        if not children:
            break

        total = len(children)
        done_count = sum(1 for c in children if c["status"] == "done")
        archived_count = sum(1 for c in children if c["status"] == "archived")
        terminal_count = done_count + archived_count
        blocking = [c for c in children if c["status"] not in _TERMINAL_STATUSES]

        can_close = done_count == total          # all children done (clean success)
        can_archive = archived_count == total     # all children abandoned
        can_complete = terminal_count == total    # all terminal (mixed done+archived)

        # Build suggestion text
        current_status = parent.get("status", "draft")
        if can_close and current_status != "done":
            suggestion = (
                f"All {total} subtask(s) are done. "
                f"Consider closing parent ({parent['id']}) '{parent['title']}' → status='done'."
            )
        elif can_archive and current_status != "archived":
            suggestion = (
                f"All {total} subtask(s) are archived. "
                f"Consider archiving parent ({parent['id']}) '{parent['title']}' → status='archived'."
            )
        elif can_complete and current_status not in _TERMINAL_STATUSES:
            suggestion = (
                f"{done_count} done, {archived_count} archived out of {total}. "
                f"All subtasks are resolved. Consider closing or archiving "
                f"({parent['id']}) '{parent['title']}' depending on outcome."
            )
        elif blocking:
            blocking_summary = ", ".join(
                f"({c['id']}) '{c['title']}' ({c['status']})" for c in blocking[:3]
            )
            if len(blocking) > 3:
                blocking_summary += f" (+{len(blocking) - 3} more)"
            suggestion = (
                f"{terminal_count}/{total} subtasks resolved. "
                f"Blocking: {blocking_summary}."
            )
        else:
            suggestion = f"Parent ({parent['id']}) '{parent['title']}' status '{current_status}' — no action needed."

        # Determine suggested_action
        if can_archive and not can_close:
            suggested_action: str | None = "archive"
        elif can_complete:
            suggested_action = "complete"
        else:
            suggested_action = None

        entry: dict[str, Any] = {
            "id": current_id,
            "title": parent.get("title"),
            "current_status": current_status,
            "children_total": total,
            "children_done": done_count,
            "children_archived": archived_count,
            "blocking_children": blocking,
            "can_close": can_close,
            "can_archive": can_archive,
            "can_complete": can_complete,
            "suggested_action": suggested_action,
            "suggestion": suggestion,
        }
        chain.append(entry)

        # Stop climbing if this parent is already terminal
        if current_status in _TERMINAL_STATUSES:
            break

        current_id = parent.get("parent_id")

    return {
        "task_id": task_id,
        "parent_chain": chain,
        "immediate_parent": chain[0] if chain else None,
    }


# ---------------------------------------------------------------------------
# 10. roadmap_diff
# ---------------------------------------------------------------------------


def roadmap_diff(
    conn: sqlite3.Connection,
    root_task_id: str,
    since_timestamp: float,
) -> dict[str, Any]:
    """Return what changed in the subtree since *since_timestamp* (Unix time).

    Useful for restoring context after a pause or switching agents.

    Returns::

        {
            tasks_created: [...],
            tasks_status_changed: [{ id, title, old_status, new_status }],
            notes_added: [...],
            contracts_changed: [...],
            new_conflicts: [...]
        }
    """
    get_task(conn, root_task_id)
    subtree_ids = _collect_subtree_ids(conn, root_task_id)
    ph = ", ".join("?" * len(subtree_ids))

    # Tasks created after since_timestamp
    new_tasks = conn.execute(  # noqa: S608
        f"SELECT * FROM tasks WHERE id IN ({ph}) AND created_at > ?",
        (*subtree_ids, since_timestamp),
    ).fetchall()

    # Tasks whose status changed after since_timestamp (updated_at > since but created_at <= since)
    status_changed_rows = conn.execute(  # noqa: S608
        f"""
        SELECT * FROM tasks
        WHERE id IN ({ph})
          AND updated_at > ?
          AND created_at <= ?
        """,
        (*subtree_ids, since_timestamp, since_timestamp),
    ).fetchall()
    # old_status is not recoverable without a history table; reported as null
    tasks_status_changed = [
        {
            "id": r["id"],
            "title": r["title"],
            "old_status": None,
            "new_status": r["status"],
            "updated_at": r["updated_at"],
        }
        for r in status_changed_rows
    ]

    # Notes added after since_timestamp
    new_notes = conn.execute(  # noqa: S608
        f"SELECT * FROM notes WHERE task_id IN ({ph}) AND created_at > ?",
        (*subtree_ids, since_timestamp),
    ).fetchall()

    # Contracts updated after since_timestamp
    changed_contracts = conn.execute(  # noqa: S608
        f"""
        SELECT DISTINCT c.* FROM contracts c
        LEFT JOIN contract_links cl ON cl.contract_id = c.id
        WHERE (c.scope_task_id IN ({ph}) OR cl.task_id IN ({ph}))
          AND c.updated_at > ?
        """,
        (*subtree_ids, *subtree_ids, since_timestamp),
    ).fetchall()

    # New conflicts (tasks created after since with overlapping code refs)
    new_task_ids = {r["id"] for r in new_tasks}
    new_conflicts: list[dict[str, Any]] = []
    if new_task_ids:
        for new_id in new_task_ids:
            new_refs = _get_code_node_ids_for_task(conn, new_id)
            if not new_refs:
                continue
            # Check against all other subtree tasks
            for existing_id in subtree_ids:
                if existing_id == new_id or existing_id in new_task_ids:
                    continue
                existing_refs = _get_code_node_ids_for_task(conn, existing_id)
                shared = new_refs & existing_refs
                if shared:
                    try:
                        existing_task = get_task(conn, existing_id)
                        new_task_data = get_task(conn, new_id)
                        new_conflicts.append({
                            "new_task": new_task_data["title"],
                            "existing_task": existing_task["title"],
                            "shared_node_count": len(shared),
                        })
                    except KeyError:
                        pass

    return {
        "tasks_created": [_row_to_dict(r) for r in new_tasks],
        "tasks_status_changed": tasks_status_changed,
        "notes_added": [_row_to_dict(r) for r in new_notes],
        "contracts_changed": [_row_to_dict(r) for r in changed_contracts],
        "new_conflicts": new_conflicts,
    }


# ---------------------------------------------------------------------------
# Cross-query #2: blast-radius of changed files → open tasks
# ---------------------------------------------------------------------------


def find_tasks_for_impact(
    conn: sqlite3.Connection,
    file_paths: list[str],
    root_task_id: Optional[str] = None,
    *,
    max_depth: int = 2,
    open_only: bool = True,
    include_node_details: bool = False,
) -> dict[str, Any]:
    """Find tasks whose code refs fall within the blast radius of changed files.

    This answers: *"Are there open tasks for nodes in the impact radius of
    these changed files?"* — useful before merging a branch.

    Algorithm:
    1. Resolve each *file_path* to node IDs in the code graph.
    2. BFS outgoing + incoming up to *max_depth* to collect the impact set.
    3. Join with ``task_code_refs`` to find tasks that reference those nodes.
    4. Optionally filter to *open_only* statuses (draft|refined|ready|in_progress).
    5. Optionally restrict to the subtree of *root_task_id*.

    Args:
        conn: SQLite connection.
        file_paths: File paths — relative (``code_review_graph/tasks.py``),
            absolute (``/project/code_review_graph/tasks.py`` or
            ``C:\\project\\code_review_graph\\tasks.py``), or filename-only
            (``tasks.py``).  All forms are normalised and matched against the
            code graph using the same strategy as ``get_impact_radius_tool``.
        root_task_id: If given, restrict results to tasks in this subtree.
        max_depth: BFS hops into the code graph. Default 2.
        open_only: If True (default) only return non-done/archived tasks.

    Returns:
        Dict with keys:
          ``impacted_nodes_count`` — total number of nodes in the blast radius
          ``impacted_nodes``       — list of {id, name, file_path, kind} dicts
                                     (only when ``include_node_details=True``)
          ``tasks``                — list of matching task dicts enriched with
                                     ``matched_nodes`` (which nodes triggered the match)
          ``uncovered_nodes_count``— number of impact nodes with no task refs
          ``uncovered_nodes``      — node detail dicts (only when ``include_node_details=True``)
          ``coverage_ratio``       — float 0..1 (impacted nodes covered by tasks)
    """
    if not file_paths:
        return {
            "impacted_nodes_count": 0,
            "impacted_nodes": [],
            "tasks": [],
            "uncovered_nodes_count": 0,
            "uncovered_nodes": [],
            "coverage_ratio": 1.0,
        }

    # Step 1: resolve file paths to node IDs.
    #
    # Since v8 migration, file_path in nodes is stored as a POSIX-relative
    # path (e.g. "code_review_graph/tasks.py").  We support three input forms:
    #
    #   a) Relative POSIX  "code_review_graph/tasks.py"  → direct match (v8 DB)
    #   b) Absolute POSIX  "/project/code_review_graph/tasks.py" → suffix match
    #   c) Absolute Windows "C:\project\...\tasks.py"  → normalise + suffix
    #   d) Filename only   "tasks.py"  → suffix match "%/tasks.py"
    #
    # All forms are normalised to forward slashes.  The suffix strip removes
    # the leading "/" from absolute POSIX paths before the LIKE pattern so
    # "/project/pkg/mod.py" searches for "%/project/pkg/mod.py" (not
    # "%//project/pkg/mod.py").
    seed_ids: set[int] = set()
    for fp in file_paths:
        fp_norm = fp.replace("\\\\", "/").replace("\\", "/")
        fp_suffix = fp_norm.lstrip("/")  # strip leading slash for suffix LIKE
        rows = conn.execute(
            """
            SELECT id FROM nodes WHERE
                file_path = ?
                OR replace(replace(file_path, '\\\\', '/'), '\\', '/') = ?
                OR replace(replace(file_path, '\\\\', '/'), '\\', '/') LIKE ?
            """,
            (fp, fp_norm, f"%/{fp_suffix}"),
        ).fetchall()
        seed_ids.update(r[0] if isinstance(r, tuple) else r["id"] for r in rows)

    if not seed_ids:
        return {
            "impacted_nodes_count": 0,
            "impacted_nodes": [],
            "tasks": [],
            "uncovered_nodes_count": 0,
            "uncovered_nodes": [],
            "coverage_ratio": 1.0,
            "note": "No code nodes found for the provided file paths.",
        }

    # Step 2: BFS over code graph
    all_impacted = seed_ids | _bfs_code_graph(conn, seed_ids, "both", max_depth)

    # Fetch node metadata for the impacted set
    ph = ", ".join("?" * len(all_impacted))
    node_rows = conn.execute(  # noqa: S608
        f"SELECT id, name, qualified_name, file_path, kind FROM nodes WHERE id IN ({ph})",
        list(all_impacted),
    ).fetchall()
    node_meta: dict[int, dict[str, Any]] = {}
    for r in node_rows:
        nid = r[0] if isinstance(r, tuple) else r["id"]
        node_meta[nid] = {
            "id": nid,
            "name": r[1] if isinstance(r, tuple) else r["name"],
            "qualified_name": r[2] if isinstance(r, tuple) else r["qualified_name"],
            "file_path": r[3] if isinstance(r, tuple) else r["file_path"],
            "kind": r[4] if isinstance(r, tuple) else r["kind"],
        }

    # Step 3: Join with task_code_refs
    ref_rows = conn.execute(  # noqa: S608
        f"SELECT task_id, code_node_id, ref_type FROM task_code_refs "
        f"WHERE code_node_id IN ({ph})",
        list(all_impacted),
    ).fetchall()

    # Group matched nodes per task
    task_matched: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in ref_rows:
        tid = r[0] if isinstance(r, tuple) else r["task_id"]
        nid = r[1] if isinstance(r, tuple) else r["code_node_id"]
        rtype = r[2] if isinstance(r, tuple) else r["ref_type"]
        task_matched[tid].append({
            "node_id": nid,
            "ref_type": rtype,
            **{k: v for k, v in node_meta.get(nid, {}).items() if k != "id"},
        })

    # Nodes covered by at least one task
    covered_node_ids: set[int] = set()
    for nid_meta_list in task_matched.values():
        for nm in nid_meta_list:
            covered_node_ids.add(nm["node_id"])

    # Step 4: Fetch task rows and filter
    _open_statuses = frozenset({"draft", "refined", "ready", "in_progress"})
    matching_task_ids = list(task_matched.keys())
    tasks_out: list[dict[str, Any]] = []

    if matching_task_ids:
        tp = ", ".join("?" * len(matching_task_ids))
        task_rows = conn.execute(  # noqa: S608
            f"SELECT * FROM tasks WHERE id IN ({tp})",
            matching_task_ids,
        ).fetchall()

        subtree_ids: Optional[set[str]] = None
        if root_task_id:
            from .tasks import _collect_subtree_ids as _csi
            subtree_ids = set(_csi(conn, root_task_id))

        for r in task_rows:
            d = _row_to_dict(r)
            if subtree_ids is not None and d["id"] not in subtree_ids:
                continue
            if open_only and d.get("status") not in _open_statuses:
                continue
            d["matched_nodes"] = task_matched[d["id"]]
            tasks_out.append(d)

    uncovered_ids = sorted(all_impacted - covered_node_ids)
    coverage = (
        len(covered_node_ids) / len(all_impacted) if all_impacted else 1.0
    )

    result: dict[str, Any] = {
        "impacted_nodes_count": len(node_meta),
        "tasks": tasks_out,
        "uncovered_nodes_count": len(uncovered_ids),
        "coverage_ratio": round(coverage, 3),
    }
    if include_node_details:
        result["impacted_nodes"] = list(node_meta.values())
        result["uncovered_nodes"] = [
            node_meta[nid] for nid in uncovered_ids if nid in node_meta
        ]
    return result


# ---------------------------------------------------------------------------
# Cross-query #4: suggest contracts between tasks with implicit code deps
# ---------------------------------------------------------------------------


def suggest_contracts(
    conn: sqlite3.Connection,
    root_task_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Suggest contracts between leaf tasks that have implicit code dependencies.

    A contract is suggested when:
    1. Code nodes of task A call/import code nodes of task B (via ``edges``).
    2. No ``task_edge`` and no ``contract`` already exists between A and B.

    This answers: *"Between which tasks should we define interface contracts?"*

    If *root_task_id* is None, auto-detects the active root task.

    Algorithm:
        - Collect leaf tasks in the subtree.
        - Build a mapping: code_node_id → task_id for each leaf.
        - Scan ``edges`` that cross task boundaries (source in A, target in B).
        - Exclude pairs that already have a task_edge or contract.
        - Return suggestions sorted by number of crossing edges (strongest first).

    Returns:
        List of suggestion dicts with keys:
          ``task_a_id``, ``task_a_title``,
          ``task_b_id``, ``task_b_title``,
          ``crossing_edges`` (count of code edges A→B + B→A),
          ``edge_details`` (list of {source_node, target_node, edge_type}).
    """
    root_task_id = _resolve_root(conn, root_task_id, "suggest_contracts")
    get_task(conn, root_task_id)
    subtree_ids = _collect_subtree_ids(conn, root_task_id)

    # Leaf tasks only
    ph_st = ", ".join("?" * len(subtree_ids))
    parent_ids = set(
        r[0]
        for r in conn.execute(  # noqa: S608
            f"SELECT DISTINCT parent_id FROM tasks WHERE parent_id IN ({ph_st})",
            subtree_ids,
        ).fetchall()
    )
    leaf_ids = [tid for tid in subtree_ids if tid not in parent_ids]

    if len(leaf_ids) < 2:
        return []

    # Map: code_node_id → task_id (for leaf tasks)
    node_to_task: dict[int, str] = {}
    ph = ", ".join("?" * len(leaf_ids))
    ref_rows = conn.execute(  # noqa: S608
        f"SELECT task_id, code_node_id FROM task_code_refs WHERE task_id IN ({ph})",
        leaf_ids,
    ).fetchall()
    for r in ref_rows:
        node_to_task[r[1]] = r[0]

    all_node_ids = list(node_to_task.keys())
    if not all_node_ids:
        return []

    # Resolve node ids → qualified names for edge matching
    np = ", ".join("?" * len(all_node_ids))
    qn_rows = conn.execute(  # noqa: S608
        f"SELECT id, qualified_name, name FROM nodes WHERE id IN ({np})",
        all_node_ids,
    ).fetchall()
    id_to_info: dict[int, dict[str, Any]] = {}
    qn_to_id: dict[str, int] = {}
    for r in qn_rows:
        nid = r[0] if isinstance(r, tuple) else r["id"]
        qn = r[1] if isinstance(r, tuple) else r["qualified_name"]
        name = r[2] if isinstance(r, tuple) else r["name"]
        id_to_info[nid] = {"qualified_name": qn, "name": name}
        qn_to_id[qn] = nid

    # Find code edges that cross task boundaries
    crossing: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)

    batch_size = 450
    for i in range(0, len(all_node_ids), batch_size):
        batch = all_node_ids[i : i + batch_size]
        bp = ", ".join("?" * len(batch))

        # Outgoing edges from our nodes
        edge_rows = conn.execute(  # noqa: S608
            f"SELECT source_qualified, target_qualified, kind "
            f"FROM edges WHERE source_qualified IN ("
            f"  SELECT qualified_name FROM nodes WHERE id IN ({bp})"
            f")",
            batch,
        ).fetchall()

        for er in edge_rows:
            src_qn = er[0] if isinstance(er, tuple) else er["source_qualified"]
            tgt_qn = er[1] if isinstance(er, tuple) else er["target_qualified"]
            etype = er[2] if isinstance(er, tuple) else er["kind"]

            src_id = qn_to_id.get(src_qn)
            tgt_id = qn_to_id.get(tgt_qn)
            if src_id is None or tgt_id is None:
                continue

            task_a = node_to_task.get(src_id)
            task_b = node_to_task.get(tgt_id)
            if task_a is None or task_b is None or task_a == task_b:
                continue

            pair = (min(task_a, task_b), max(task_a, task_b))
            crossing[pair].append({
                "source_node": id_to_info.get(src_id, {}).get("name", src_qn),
                "target_node": id_to_info.get(tgt_id, {}).get("name", tgt_qn),
                "edge_type": etype,
            })

    if not crossing:
        return []

    # Exclude pairs that already have a task_edge or contract
    existing_pairs: set[tuple[str, str]] = set()

    edge_rows = conn.execute(  # noqa: S608
        f"SELECT source_task_id, target_task_id FROM task_edges "
        f"WHERE source_task_id IN ({ph}) AND target_task_id IN ({ph})",
        leaf_ids + leaf_ids,
    ).fetchall()
    for r in edge_rows:
        a, b = r[0], r[1]
        existing_pairs.add((min(a, b), max(a, b)))

    # Contracts — find pairs of leaf tasks sharing the same contract
    cl_rows = conn.execute(  # noqa: S608
        f"SELECT cl1.task_id, cl2.task_id "
        f"FROM contract_links cl1 "
        f"JOIN contract_links cl2 "
        f"  ON cl1.contract_id = cl2.contract_id AND cl1.task_id != cl2.task_id "
        f"WHERE cl1.task_id IN ({ph})",
        leaf_ids,
    ).fetchall()
    for r in cl_rows:
        a, b = r[0], r[1]
        existing_pairs.add((min(a, b), max(a, b)))

    # Fetch task titles
    task_rows = conn.execute(  # noqa: S608
        f"SELECT id, title FROM tasks WHERE id IN ({ph})",
        leaf_ids,
    ).fetchall()
    titles: dict[str, str] = {}
    for r in task_rows:
        titles[r[0] if isinstance(r, tuple) else r["id"]] = (
            r[1] if isinstance(r, tuple) else r["title"]
        )

    # Build suggestions
    suggestions: list[dict[str, Any]] = []
    for pair, edges in crossing.items():
        if pair in existing_pairs:
            continue
        suggestions.append({
            "task_a_id": pair[0],
            "task_a_title": titles.get(pair[0], "?"),
            "task_b_id": pair[1],
            "task_b_title": titles.get(pair[1], "?"),
            "crossing_edges": len(edges),
            "edge_details": edges[:10],  # cap details for token efficiency
        })

    suggestions.sort(key=lambda s: s["crossing_edges"], reverse=True)
    return suggestions
