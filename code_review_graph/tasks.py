"""Core Task DAG logic for brainstorm-driven task management.

Provides CRUD operations, DAG edge management, code cross-references,
notes, and contracts over the SQLite task tables introduced in migration v6.

All public functions accept a ``sqlite3.Connection`` and are pure data
operations — no LLM logic, no side effects beyond the database.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
import uuid
from collections import deque
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Valid enumeration values (enforced at the Python layer)
# ---------------------------------------------------------------------------

TASK_STATUSES = frozenset({"draft", "refined", "ready", "in_progress", "done", "archived"})
TASK_EDGE_TYPES = frozenset({"depends_on", "blocks", "shares_context", "conflicts_with", "informs"})
CODE_REF_TYPES = frozenset({"modifies", "creates", "deletes", "reads", "tests"})
NOTE_TYPES = frozenset({"decision", "question", "assumption", "constraint", "risk"})
NOTE_STATUSES = frozenset({"open", "resolved", "rejected", "deferred"})
CONTRACT_TYPES = frozenset({"interface", "api", "schema", "event", "data_format"})
CONTRACT_STATUSES = frozenset({"proposed", "agreed", "implemented", "verified", "void"})

# Mapping: task status → contract link status (auto-propagated on task_update)
_TASK_TO_CONTRACT_STATUS: dict[str, str] = {
    "draft":       "proposed",
    "refined":     "proposed",
    "ready":       "acknowledged",
    "in_progress": "acknowledged",
    "done":        "implemented",
    "archived":    "void",
}

# Edge types that form a dependency ordering (cycle-check required)
_ORDERING_EDGE_TYPES = frozenset({"depends_on", "blocks"})

# Fields of tasks that support surgical text editing
_EDITABLE_TEXT_FIELDS = frozenset({"description", "spec", "acceptance_criteria"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> float:
    return time.time()


def _new_id() -> str:
    return str(uuid.uuid4())


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def _require(value: Any, name: str) -> None:
    if not value and value != 0:
        raise ValueError(f"{name} is required and cannot be empty")


def _check_enum(value: str, valid: frozenset[str], name: str) -> None:
    if value not in valid:
        raise ValueError(f"Invalid {name} '{value}'. Must be one of: {sorted(valid)}")


# ---------------------------------------------------------------------------
# Task CRUD
# ---------------------------------------------------------------------------


def get_active_root(conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
    """Return the single open root task, or *None* if no open root exists.

    A root task is one with ``parent_id IS NULL``.  An *open* root task has a
    status that is not ``done`` or ``archived``.

    This helper is the foundation of the single-pipeline discipline: at most
    one root task may be active at a time.
    """
    row = conn.execute(
        "SELECT * FROM tasks WHERE parent_id IS NULL AND status NOT IN ('done', 'archived')"
        " ORDER BY created_at LIMIT 1"
    ).fetchone()
    return _row_to_dict(row) if row else None


def create_task(
    conn: sqlite3.Connection,
    title: str,
    description: Optional[str] = None,
    parent_id: Optional[str] = None,
) -> dict[str, Any]:
    """Create a task. If *parent_id* is set, the task becomes a subtask.

    **Single-pipeline discipline**: a new *root* task (``parent_id=None``)
    can only be created when there is no other open root task.  Close or
    archive the current root before starting a new one.

    Returns the newly created task row as a dict.
    """
    _require(title, "title")
    if parent_id is not None:
        row = conn.execute("SELECT id FROM tasks WHERE id = ?", (parent_id,)).fetchone()
        if row is None:
            raise ValueError(f"Parent task '{parent_id}' does not exist")
    else:
        # Enforce single-pipeline discipline: only one open root at a time
        existing = get_active_root(conn)
        if existing is not None:
            raise ValueError(
                f"Cannot create a new root task: root task '{existing['id']}' "
                f"('{existing['title']}') is still open (status={existing['status']!r}). "
                "Close or archive the current root task before starting a new one."
            )

    task_id = _new_id()
    now = _now()
    conn.execute(
        """
        INSERT INTO tasks (id, parent_id, title, description, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'draft', ?, ?)
        """,
        (task_id, parent_id, title, description, now, now),
    )
    conn.commit()
    logger.debug("Created task %s: %r", task_id, title)
    return get_task(conn, task_id)


def get_task(conn: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    """Return a task row as a dict. Raises *KeyError* if not found."""
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise KeyError(f"Task '{task_id}' not found")
    return _row_to_dict(row)


def _build_update(
    fields: list[tuple[str, Any]],
) -> tuple[list[str], list[Any]]:
    """Build SET clause fragments and params for UPDATE queries.

    Skips fields with None values.
    Returns (set_fragments, params_without_id).
    """
    updates: list[str] = []
    params: list[Any] = []
    for col, val in fields:
        if val is not None:
            updates.append(f"{col} = ?")
            params.append(val)
    return updates, params


def update_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    title: Optional[str] = None,
    description: Optional[str] = None,
    status: Optional[str] = None,
    spec: Optional[str] = None,
    acceptance_criteria: Optional[str] = None,
) -> dict[str, Any]:
    """Update one or more fields of a task.

    Only non-None arguments are updated. *updated_at* is always refreshed.
    """
    # Verify the task exists
    get_task(conn, task_id)

    if status is not None:
        _check_enum(status, TASK_STATUSES, "status")

    updates, params = _build_update([
        ("title", title),
        ("description", description),
        ("status", status),
        ("spec", spec),
        ("acceptance_criteria", acceptance_criteria),
    ])

    if not updates:
        return get_task(conn, task_id)

    updates.append("updated_at = ?")
    params.append(_now())
    params.append(task_id)

    conn.execute(
        f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?",  # noqa: S608
        params,
    )

    # Auto-propagate status to contracts where this task is a participant
    if status is not None:
        contract_status = _TASK_TO_CONTRACT_STATUS.get(status)
        if contract_status:
            _propagate_contract_status(conn, task_id, contract_status)

    conn.commit()
    return get_task(conn, task_id)


def _propagate_contract_status(
    conn: sqlite3.Connection, task_id: str, contract_status: str
) -> None:
    """Update status of all contracts where task_id is a participant.

    Only upgrades status along the lifecycle:
      proposed → acknowledged → implemented → verified
    Archived tasks set status to 'void' regardless.
    """
    _LIFECYCLE_ORDER = ("proposed", "acknowledged", "implemented", "verified", "void")

    links = conn.execute(
        "SELECT contract_id FROM contract_links WHERE task_id = ?", (task_id,)
    ).fetchall()

    for (contract_id,) in links:
        row = conn.execute(
            "SELECT status FROM contracts WHERE id = ?", (contract_id,)
        ).fetchone()
        if row is None:
            continue
        current = row[0]
        # void is terminal — don't downgrade from void
        if current == "void" and contract_status != "void":
            continue
        # Only move forward in lifecycle (or to void)
        current_idx = _LIFECYCLE_ORDER.index(current) if current in _LIFECYCLE_ORDER else 0
        new_idx = _LIFECYCLE_ORDER.index(contract_status) if contract_status in _LIFECYCLE_ORDER else 0
        if new_idx > current_idx or contract_status == "void":
            conn.execute(
                "UPDATE contracts SET status = ?, updated_at = ? WHERE id = ?",
                (contract_status, _now(), contract_id),
            )


def edit_task_field(
    conn: sqlite3.Connection,
    task_id: str,
    field: str,
    *,
    search: Optional[str] = None,
    replace: Optional[str] = None,
    line_start: Optional[int] = None,
    line_end: Optional[int] = None,
    content: Optional[str] = None,
) -> dict[str, Any]:
    """Surgically edit a text field of a task.

    Two exclusive modes:

    **search/replace** — supply *search* and *replace*.  Raises if the
    search string is not found or appears more than once.

    **line range** — supply *line_start*, *line_end* (1-indexed, inclusive),
    and *content*.  Raises if lines are out of range.

    Returns ``{ task_id, field, old_fragment, new_fragment, line_range, total_lines }``.
    """
    if field not in _EDITABLE_TEXT_FIELDS:
        raise ValueError(
            f"Field '{field}' is not editable. Must be one of: {sorted(_EDITABLE_TEXT_FIELDS)}"
        )

    task = get_task(conn, task_id)
    current: str = task.get(field) or ""

    mode_sr = search is not None or replace is not None
    mode_lr = line_start is not None or line_end is not None or content is not None

    if mode_sr and mode_lr:
        raise ValueError("Specify either search/replace OR line_start/line_end/content, not both")
    if not mode_sr and not mode_lr:
        raise ValueError("Must specify either search/replace or line_start/line_end/content")

    if mode_sr:
        if search is None or replace is None:
            raise ValueError("Both 'search' and 'replace' are required in search/replace mode")
        count = current.count(search)
        if count == 0:
            raise ValueError(f"Search string not found in field '{field}' — nothing changed")
        if count > 1:
            raise ValueError(
                f"Search string appears {count} times in field '{field}' — ambiguous. "
                "Provide a longer search string or use line_start/line_end mode"
            )
        new_value = current.replace(search, replace, 1)
        old_fragment = search
        new_fragment = replace
        # Find which lines are affected
        pre = current[: current.index(search)]
        start_line = pre.count("\n") + 1
        end_line = start_line + search.count("\n")
        affected_range = [start_line, end_line]

    else:  # line range mode
        if line_start is None or line_end is None or content is None:
            raise ValueError(
                "line_start, line_end and content are all required in line range mode"
            )
        lines = current.split("\n")
        total = len(lines)
        if line_start < 1 or line_end < line_start or line_end > total:
            raise ValueError(
                f"Field '{field}' has {total} lines; "
                f"requested range [{line_start}, {line_end}] is invalid"
            )
        old_fragment = "\n".join(lines[line_start - 1 : line_end])
        new_lines = lines[: line_start - 1] + content.split("\n") + lines[line_end:]
        new_value = "\n".join(new_lines)
        new_fragment = content
        affected_range = [line_start, line_end]

    total_lines_after = new_value.count("\n") + 1
    conn.execute(
        f"UPDATE tasks SET {field} = ?, updated_at = ? WHERE id = ?",  # noqa: S608
        (new_value, _now(), task_id),
    )
    conn.commit()
    return {
        "task_id": task_id,
        "field": field,
        "old_fragment": old_fragment,
        "new_fragment": new_fragment,
        "line_range": affected_range,
        "total_lines": total_lines_after,
    }


def delete_task(
    conn: sqlite3.Connection,
    task_id: str,
    cascade: bool = False,
) -> dict[str, Any]:
    """Delete a task.

    If *cascade* is True, all subtasks (recursive) are deleted along with
    their edges, code refs, notes, and contracts.

    Returns ``{ deleted_ids: [...] }``.
    """
    get_task(conn, task_id)  # raises KeyError if not found

    if cascade:
        ids_to_delete = _collect_subtree_ids(conn, task_id)
    else:
        ids_to_delete = [task_id]

    for tid in ids_to_delete:
        _delete_task_data(conn, tid)

    conn.commit()
    return {"deleted_ids": ids_to_delete}


def _collect_subtree_ids(conn: sqlite3.Connection, root_id: str) -> list[str]:
    """BFS to collect the root + all descendant task IDs."""
    result: list[str] = []
    queue: deque[str] = deque([root_id])
    while queue:
        current = queue.popleft()
        result.append(current)
        children = conn.execute(
            "SELECT id FROM tasks WHERE parent_id = ?", (current,)
        ).fetchall()
        queue.extend(row[0] for row in children)
    return result


def _delete_task_data(conn: sqlite3.Connection, task_id: str) -> None:
    """Delete all data for a single task (no commit)."""
    conn.execute(
        "DELETE FROM task_edges WHERE source_task_id = ? OR target_task_id = ?",
        (task_id, task_id),
    )
    conn.execute("DELETE FROM task_code_refs WHERE task_id = ?", (task_id,))
    conn.execute("DELETE FROM notes WHERE task_id = ?", (task_id,))
    # Remove task from contract_links; then delete contracts with no remaining links
    # and whose scope_task_id is this task (fully orphaned contracts)
    try:
        # Get contracts that will lose their only remaining participant
        orphan_contracts = set(
            r[0]
            for r in conn.execute(
                "SELECT contract_id FROM contract_links WHERE task_id = ?",
                (task_id,),
            ).fetchall()
            if conn.execute(
                "SELECT COUNT(*) FROM contract_links WHERE contract_id = ?",
                (r[0],),
            ).fetchone()[0] == 1  # this task is the last participant
        )
        conn.execute("DELETE FROM contract_links WHERE task_id = ?", (task_id,))
        # Delete now-orphaned contracts
        for cid in orphan_contracts:
            conn.execute("DELETE FROM contracts WHERE id = ?", (cid,))
    except Exception:
        # Fallback: old schema (v6) — delete by legacy FK columns
        conn.execute(
            "DELETE FROM contracts WHERE provider_task_id = ? OR consumer_task_id = ?",
            (task_id, task_id),
        )
    # Also delete contracts scoped to this task (it was the scope root)
    conn.execute("DELETE FROM contracts WHERE scope_task_id = ?", (task_id,))
    conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))


def list_tasks(
    conn: sqlite3.Connection,
    parent_id: Optional[str] = None,
    status: Optional[str] = None,
    root_only: bool = False,
) -> list[dict[str, Any]]:
    """List tasks with optional filters.

    - *parent_id*: return direct children of this task.
    - *status*: filter by status string.
    - *root_only*: return only top-level tasks (parent_id IS NULL).
    """
    if root_only and parent_id is not None:
        logger.warning(
            "list_tasks: root_only=True overrides parent_id=%r; parent_id is ignored",
            parent_id,
        )
    clauses: list[str] = []
    params: list[Any] = []

    if root_only:
        clauses.append("parent_id IS NULL")
    elif parent_id is not None:
        clauses.append("parent_id = ?")
        params.append(parent_id)

    if status is not None:
        _check_enum(status, TASK_STATUSES, "status")
        clauses.append("status = ?")
        params.append(status)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM tasks {where} ORDER BY created_at",  # noqa: S608
        params,
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def move_task(
    conn: sqlite3.Connection,
    task_id: str,
    new_parent_id: Optional[str],
) -> dict[str, Any]:
    """Move a task to a new parent (or make it a root task if *new_parent_id* is None).

    Raises *ValueError* if the move would create a hierarchy cycle.
    """
    get_task(conn, task_id)  # verify exists

    if new_parent_id is not None:
        get_task(conn, new_parent_id)  # verify target exists
        # Cycle check: new_parent must not be in the subtree of task_id
        subtree_ids = set(_collect_subtree_ids(conn, task_id))
        if new_parent_id in subtree_ids:
            raise ValueError(
                f"Cannot move task '{task_id}' under '{new_parent_id}': "
                "would create a hierarchy cycle"
            )

    conn.execute(
        "UPDATE tasks SET parent_id = ?, updated_at = ? WHERE id = ?",
        (new_parent_id, _now(), task_id),
    )
    conn.commit()
    return get_task(conn, task_id)


def search_tasks(
    conn: sqlite3.Connection,
    root_task_id: str,
    query: str,
) -> list[dict[str, Any]]:
    """Full-text search within the subtree rooted at *root_task_id*.

    Searches across title, description, spec, and acceptance_criteria fields.
    Uses FTS5 (``tasks_fts``) when available for O(log n) performance;
    falls back to ``LIKE`` if the virtual table does not exist.

    Returns matching tasks ordered by creation time.
    """
    _require(query, "query")
    get_task(conn, root_task_id)  # verify root exists

    subtree_ids = _collect_subtree_ids(conn, root_task_id)
    if not subtree_ids:
        return []

    placeholders = ", ".join("?" * len(subtree_ids))

    # Check whether the FTS5 virtual table exists (it may be absent on old DBs)
    fts_exists = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='tasks_fts'"
    ).fetchone()[0] > 0

    if fts_exists:
        # FTS5 path: search the virtual table, then restrict to subtree
        # Escape FTS5 special chars: " → ""  (double-quote quoting)
        fts_query = f'"{query.replace(chr(34), chr(34) * 2)}"'
        try:
            rows = conn.execute(  # noqa: S608
                f"""
                SELECT t.* FROM tasks t
                JOIN tasks_fts fts ON fts.id = t.id
                WHERE fts.tasks_fts MATCH ?
                  AND t.id IN ({placeholders})
                ORDER BY t.created_at
                """,
                (fts_query, *subtree_ids),
            ).fetchall()
            return [_row_to_dict(r) for r in rows]
        except Exception:  # noqa: BLE001
            # FTS5 query syntax error or corrupted index — fall through to LIKE
            logger.warning("tasks_fts MATCH failed for %r, falling back to LIKE", query)

    # LIKE fallback path (old DB or FTS error)
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    rows = conn.execute(
        f"""
        SELECT * FROM tasks
        WHERE id IN ({placeholders})
          AND (
            title LIKE ? COLLATE NOCASE ESCAPE '\\'
            OR description LIKE ? COLLATE NOCASE ESCAPE '\\'
            OR spec LIKE ? COLLATE NOCASE ESCAPE '\\'
            OR acceptance_criteria LIKE ? COLLATE NOCASE ESCAPE '\\'
          )
        ORDER BY created_at
        """,  # noqa: S608
        (*subtree_ids, pattern, pattern, pattern, pattern),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def archive_task(
    conn: sqlite3.Connection,
    task_id: str,
    reason: str,
    cascade: bool = True,
) -> dict[str, Any]:
    """Archive a task (and optionally its subtree).

    Sets status to 'archived' and records the *reason*. Does not delete —
    archived tasks remain in the database for historical reference.

    Returns ``{ archived_ids: [...] }``.
    """
    _require(reason, "reason")
    get_task(conn, task_id)  # verify exists

    if cascade:
        ids_to_archive = _collect_subtree_ids(conn, task_id)
    else:
        ids_to_archive = [task_id]

    now = _now()
    for tid in ids_to_archive:
        conn.execute(
            "UPDATE tasks SET status = 'archived', archive_reason = ?, updated_at = ? WHERE id = ?",
            (reason, now, tid),
        )

    conn.commit()
    return {"archived_ids": ids_to_archive}


# ---------------------------------------------------------------------------
# DAG edge management
# ---------------------------------------------------------------------------


def add_task_edge(
    conn: sqlite3.Connection,
    source_id: str,
    target_id: str,
    edge_type: str,
    description: Optional[str] = None,
) -> dict[str, Any]:
    """Add a directed edge between two tasks.

    For *depends_on* and *blocks* edge types, performs a cycle check via DFS.
    Raises *ValueError* on invalid inputs or cycle detection.
    """
    get_task(conn, source_id)
    get_task(conn, target_id)
    _check_enum(edge_type, TASK_EDGE_TYPES, "edge_type")

    if source_id == target_id:
        raise ValueError("Cannot add a self-referencing edge")

    if edge_type in _ORDERING_EDGE_TYPES:
        _assert_no_cycle_after_edge(conn, source_id, target_id, edge_type)

    now = _now()
    conn.execute(
        """
        INSERT OR REPLACE INTO task_edges
            (source_task_id, target_task_id, type, description, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (source_id, target_id, edge_type, description, now),
    )
    conn.commit()
    return {
        "source_task_id": source_id,
        "target_task_id": target_id,
        "type": edge_type,
        "description": description,
        "created_at": now,
    }


def _assert_no_cycle_after_edge(
    conn: sqlite3.Connection,
    source_id: str,
    target_id: str,
    edge_type: str,
) -> None:
    """DFS reachability: if *target_id* can already reach *source_id* via the
    same edge type, adding source→target would create a cycle."""
    visited: set[str] = set()
    stack = [target_id]
    while stack:
        node = stack.pop()
        if node == source_id:
            raise ValueError(
                f"Adding edge {source_id} --{edge_type}--> {target_id} "
                "would create a cycle in the task dependency graph"
            )
        if node in visited:
            continue
        visited.add(node)
        neighbours = conn.execute(
            "SELECT target_task_id FROM task_edges WHERE source_task_id = ? AND type = ?",
            (node, edge_type),
        ).fetchall()
        stack.extend(r[0] for r in neighbours)


def remove_task_edge(
    conn: sqlite3.Connection,
    source_id: str,
    target_id: str,
    edge_type: str,
) -> dict[str, Any]:
    """Remove an edge between two tasks. Raises *KeyError* if not found."""
    _check_enum(edge_type, TASK_EDGE_TYPES, "edge_type")
    row = conn.execute(
        "SELECT * FROM task_edges WHERE source_task_id = ? AND target_task_id = ? AND type = ?",
        (source_id, target_id, edge_type),
    ).fetchone()
    if row is None:
        raise KeyError(
            f"Edge {source_id} --{edge_type}--> {target_id} not found"
        )
    conn.execute(
        "DELETE FROM task_edges WHERE source_task_id = ? AND target_task_id = ? AND type = ?",
        (source_id, target_id, edge_type),
    )
    conn.commit()
    return _row_to_dict(row)


def get_task_edges(
    conn: sqlite3.Connection,
    task_id: str,
    direction: str = "both",
) -> list[dict[str, Any]]:
    """Return edges for *task_id*.

    *direction* is one of ``"incoming"``, ``"outgoing"``, or ``"both"`` (default).
    """
    get_task(conn, task_id)
    if direction not in ("incoming", "outgoing", "both"):
        raise ValueError("direction must be 'incoming', 'outgoing', or 'both'")

    rows: list[sqlite3.Row] = []
    if direction in ("outgoing", "both"):
        rows += conn.execute(
            "SELECT * FROM task_edges WHERE source_task_id = ?", (task_id,)
        ).fetchall()
    if direction in ("incoming", "both"):
        rows += conn.execute(
            "SELECT * FROM task_edges WHERE target_task_id = ?", (task_id,)
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_task_dag(
    conn: sqlite3.Connection,
    root_task_id: str,
) -> dict[str, Any]:
    """Return the full DAG rooted at *root_task_id*.

    Collects all tasks in the subtree plus all task_edges between them.
    Returns ``{ nodes: [...], edges: [...] }`` suitable for visualisation.
    """
    get_task(conn, root_task_id)
    subtree_ids = _collect_subtree_ids(conn, root_task_id)

    placeholders = ", ".join("?" * len(subtree_ids))
    nodes = conn.execute(
        f"SELECT * FROM tasks WHERE id IN ({placeholders})",  # noqa: S608
        subtree_ids,
    ).fetchall()

    edges = conn.execute(
        f"""
        SELECT * FROM task_edges
        WHERE source_task_id IN ({placeholders})
           OR target_task_id IN ({placeholders})
        """,  # noqa: S608
        (*subtree_ids, *subtree_ids),
    ).fetchall()

    # Include parent→child hierarchy edges
    hierarchy_edges = [
        {
            "source_task_id": row["parent_id"],
            "target_task_id": row["id"],
            "type": "parent_child",
            "description": None,
        }
        for row in nodes
        if row["parent_id"] is not None
    ]

    return {
        "nodes": [_row_to_dict(r) for r in nodes],
        "edges": [_row_to_dict(r) for r in edges] + hierarchy_edges,
    }


def topological_sort_tasks(
    conn: sqlite3.Connection,
    root_task_id: str,
) -> list[dict[str, Any]]:
    """Topological sort of leaf tasks (no children) using Kahn's algorithm.

    Only *depends_on* edges are considered for ordering.
    Raises *ValueError* if a dependency cycle is detected.

    Returns tasks in topological order (dependencies first).
    """
    get_task(conn, root_task_id)
    subtree_ids = set(_collect_subtree_ids(conn, root_task_id))

    # Find leaf tasks (no children)
    all_parents = set(
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT parent_id FROM tasks WHERE parent_id IS NOT NULL"
        ).fetchall()
    )
    leaf_ids = [tid for tid in subtree_ids if tid not in all_parents]

    if not leaf_ids:
        # No leaves: return all tasks in the subtree ordered by creation time
        placeholders = ", ".join("?" * len(subtree_ids))
        rows = conn.execute(
            f"SELECT * FROM tasks WHERE id IN ({placeholders}) ORDER BY created_at",  # noqa: S608
            list(subtree_ids),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # Build in-degree map and adjacency list for leaf tasks
    leaf_set = set(leaf_ids)
    in_degree: dict[str, int] = {lid: 0 for lid in leaf_ids}
    adjacency: dict[str, list[str]] = {lid: [] for lid in leaf_ids}

    dep_edges = conn.execute(
        """
        SELECT source_task_id, target_task_id FROM task_edges
        WHERE type = 'depends_on'
        """,
    ).fetchall()

    for row in dep_edges:
        src, tgt = row[0], row[1]
        if src in leaf_set and tgt in leaf_set:
            adjacency[tgt].append(src)  # tgt must come before src
            in_degree[src] = in_degree.get(src, 0) + 1

    # Kahn's algorithm
    queue = [lid for lid in leaf_ids if in_degree[lid] == 0]
    sorted_ids: list[str] = []
    while queue:
        node = queue.pop(0)
        sorted_ids.append(node)
        for neighbour in adjacency.get(node, []):
            in_degree[neighbour] -= 1
            if in_degree[neighbour] == 0:
                queue.append(neighbour)

    if len(sorted_ids) != len(leaf_ids):
        raise ValueError(
            "Cycle detected in task dependency graph — topological sort is impossible"
        )

    # Fetch full task rows in sorted order
    id_to_row: dict[str, dict[str, Any]] = {}
    placeholders = ", ".join("?" * len(sorted_ids))
    rows = conn.execute(
        f"SELECT * FROM tasks WHERE id IN ({placeholders})",  # noqa: S608
        sorted_ids,
    ).fetchall()
    for r in rows:
        id_to_row[r["id"]] = _row_to_dict(r)

    return [id_to_row[tid] for tid in sorted_ids if tid in id_to_row]


# ---------------------------------------------------------------------------
# Code cross-references
# ---------------------------------------------------------------------------


def _resolve_code_node(
    conn: sqlite3.Connection,
    code_node_id: Optional[int],
    qualified_name: Optional[str],
    *,
    caller: str = "operation",
) -> int:
    """Resolve *code_node_id* or *qualified_name* to a numeric node id.

    Exactly one of the two must be provided.  Path separators in
    *qualified_name* are normalised (``\\\\`` → ``/``) before matching so
    that Windows-style qualified names from graph search results work on any
    platform.

    Raises:
        ValueError: if both or neither are provided, or if the qualified name
            is not found in the ``nodes`` table.
    """
    if code_node_id is not None and qualified_name is not None:
        raise ValueError(f"{caller}: provide either code_node_id or qualified_name, not both")
    if code_node_id is None and qualified_name is None:
        raise ValueError(f"{caller}: either code_node_id or qualified_name is required")

    if qualified_name is not None:
        # Normalise separators on both sides so that Windows absolute paths
        # ("C:\\path\\fn"), POSIX absolute paths ("/path/fn"), and relative
        # paths ("pkg/mod.py::fn") all resolve consistently regardless of
        # how the graph was built.  Since v8 migration stores relative POSIX
        # paths, most lookups will hit the first branch directly.
        qn_norm = qualified_name.replace("\\\\", "/").replace("\\", "/")

        # 1. Exact match after normalisation (covers relative and absolute POSIX)
        row = conn.execute(
            "SELECT id FROM nodes WHERE replace(replace(qualified_name, '\\\\', '/'), '\\', '/') = ?",
            (qn_norm,),
        ).fetchone()
        if row is not None:
            return int(row[0])

        # 2. Suffix match: "pkg/mod.py::fn" matches "proj/pkg/mod.py::fn"
        #    Useful when caller passes a relative path that is a suffix of the
        #    stored absolute path (pre-v8 DBs or cross-platform mismatches).
        suffix = qn_norm.lstrip("/")
        rows = conn.execute(
            "SELECT id FROM nodes "
            "WHERE replace(replace(qualified_name, '\\\\', '/'), '\\', '/') LIKE ?",
            (f"%/{suffix}",),
        ).fetchall()
        if len(rows) == 1:
            return int(rows[0][0])
        if len(rows) > 1:
            qnames = [
                conn.execute("SELECT qualified_name FROM nodes WHERE id=?", (r[0],))
                .fetchone()[0]
                for r in rows
            ]
            raise ValueError(
                f"{caller}: ambiguous suffix '{qualified_name}' — "
                f"{len(rows)} nodes matched. Use an exact qualified_name: {qnames}"
            )

        # 3. Short-name fallback: "fn" matches any node with name="fn"
        name_part = qn_norm.rsplit("::", 1)[-1] if "::" in qn_norm else qn_norm
        name_rows = conn.execute(
            "SELECT id FROM nodes WHERE name = ?", (name_part,)
        ).fetchall()
        if len(name_rows) == 0:
            raise ValueError(
                f"{caller}: no code node found with qualified_name '{qualified_name}'. "
                "Use semantic_search_nodes_tool to find the correct name."
            )
        if len(name_rows) > 1:
            qnames = [
                conn.execute("SELECT qualified_name FROM nodes WHERE id=?", (r[0],))
                .fetchone()[0]
                for r in name_rows
            ]
            raise ValueError(
                f"{caller}: ambiguous — {len(name_rows)} nodes found with name "
                f"'{name_part}'. Use one of these qualified_names: {qnames}"
            )
        return int(name_rows[0][0])

    # code_node_id path
    row = conn.execute("SELECT id FROM nodes WHERE id = ?", (code_node_id,)).fetchone()
    if row is None:
        raise ValueError(f"Code node '{code_node_id}' does not exist in the graph")
    return int(row[0])


def link_task_code(
    conn: sqlite3.Connection,
    task_id: str,
    ref_type: str,
    code_node_id: Optional[int] = None,
    qualified_name: Optional[str] = None,
    description: Optional[str] = None,
    batch: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Link a task to one or many code graph nodes.

    **Single mode** (existing behaviour):
        Exactly one of *code_node_id* or *qualified_name* must be supplied.

    **Batch mode** (new):
        Supply *batch* — a list of dicts, each with at least ``ref_type`` and
        one of ``code_node_id`` / ``qualified_name``.  Top-level
        ``code_node_id``, ``qualified_name``, and ``ref_type`` are ignored
        when *batch* is provided.

        Each batch item:
            {
                "ref_type": "modifies",          # required
                "code_node_id": 101,             # either this …
                "qualified_name": "src/fn",      # … or this
                "description": "optional note",  # optional
            }

    Returns (batch mode):
        {
            "task_id": "…",
            "linked": [{"code_node_id": …, "ref_type": …, "qualified_name": …}, …],
            "errors": [{"item": …, "error": "…"}, …],
            "total": N,
            "success_count": N,
            "error_count": N,
        }

    Using *qualified_name* lets the LLM pass the value directly from
    ``semantic_search_nodes_tool`` or ``task_export`` code_refs without a
    separate ID-lookup step.  Path separators are normalised (``\\`` → ``/``).
    """
    get_task(conn, task_id)
    now = _now()

    # ── Batch mode ────────────────────────────────────────────────────────────
    if batch is not None:
        if not isinstance(batch, list):
            raise ValueError("batch must be a list of dicts")
        linked: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []

        for item in batch:
            if not isinstance(item, dict):
                errors.append({"item": item, "error": "each batch item must be a dict"})
                continue
            item_ref_type = item.get("ref_type")
            item_node_id: Optional[int] = item.get("code_node_id")
            item_qname: Optional[str] = item.get("qualified_name")
            item_desc: Optional[str] = item.get("description")
            try:
                _check_enum(item_ref_type, CODE_REF_TYPES, "ref_type")
                resolved_id = _resolve_code_node(
                    conn, item_node_id, item_qname, caller="link_task_code[batch]"
                )
                conn.execute(
                    """
                    INSERT OR REPLACE INTO task_code_refs
                        (task_id, code_node_id, ref_type, description, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (task_id, resolved_id, item_ref_type, item_desc, now),
                )
                linked.append({
                    "code_node_id": resolved_id,
                    "ref_type": item_ref_type,
                    "qualified_name": item_qname,
                    "description": item_desc,
                })
            except (KeyError, ValueError) as exc:
                errors.append({"item": item, "error": str(exc)})

        conn.commit()
        return {
            "task_id": task_id,
            "linked": linked,
            "errors": errors,
            "total": len(batch),
            "success_count": len(linked),
            "error_count": len(errors),
        }

    # ── Single mode ───────────────────────────────────────────────────────────
    _check_enum(ref_type, CODE_REF_TYPES, "ref_type")
    code_node_id = _resolve_code_node(conn, code_node_id, qualified_name, caller="link_task_code")

    conn.execute(
        """
        INSERT OR REPLACE INTO task_code_refs
            (task_id, code_node_id, ref_type, description, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (task_id, code_node_id, ref_type, description, now),
    )
    conn.commit()
    return {
        "task_id": task_id,
        "code_node_id": code_node_id,
        "ref_type": ref_type,
        "description": description,
        "created_at": now,
    }


def unlink_task_code(
    conn: sqlite3.Connection,
    task_id: str,
    code_node_id: int,
) -> dict[str, Any]:
    """Remove all ref_type associations between a task and a code node."""
    get_task(conn, task_id)
    rows = conn.execute(
        "SELECT * FROM task_code_refs WHERE task_id = ? AND code_node_id = ?",
        (task_id, code_node_id),
    ).fetchall()
    if not rows:
        raise KeyError(
            f"No code ref found between task '{task_id}' and node '{code_node_id}'"
        )
    conn.execute(
        "DELETE FROM task_code_refs WHERE task_id = ? AND code_node_id = ?",
        (task_id, code_node_id),
    )
    conn.commit()
    return {"task_id": task_id, "code_node_id": code_node_id, "removed": len(rows)}


def _read_source_snippet(
    file_path: str,
    line_start: Optional[int],
    line_end: Optional[int],
    *,
    max_lines: int = 50,
    repo_root: Optional[str] = None,
) -> Optional[str]:
    """Read source lines from disk and return as a string (capped at max_lines).

    Tries *file_path* as-is first; if not found and *repo_root* is supplied,
    tries joining them.  Returns ``None`` if the file cannot be read or the
    line numbers are unavailable.
    """
    if not file_path or line_start is None:
        return None

    from pathlib import Path as _Path

    candidates = [_Path(file_path)]
    if repo_root:
        candidates.append(_Path(repo_root) / file_path)

    for candidate in candidates:
        try:
            lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
            start = max(0, line_start - 1)
            end = line_end if line_end else start + max_lines
            end = min(end, start + max_lines, len(lines))
            return "\n".join(lines[start:end])
        except OSError:
            continue
    return None


def get_task_code_refs(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    include_source: bool = False,
    repo_root: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Return all code nodes linked to a task, enriched with node metadata.

    Args:
        task_id: Task to query.
        include_source: If True, attach ``source_snippet`` (up to 50 lines)
            read from disk for each node.
        repo_root: Base path for resolving relative ``file_path`` values.
    """
    get_task(conn, task_id)
    rows = conn.execute(
        """
        SELECT tcr.task_id, tcr.code_node_id, tcr.ref_type, tcr.description,
               tcr.created_at,
               n.kind, n.name, n.qualified_name, n.file_path,
               n.line_start, n.line_end, n.language
        FROM task_code_refs tcr
        JOIN nodes n ON n.id = tcr.code_node_id
        WHERE tcr.task_id = ?
        ORDER BY tcr.created_at
        """,
        (task_id,),
    ).fetchall()
    result = []
    for r in rows:
        d = _row_to_dict(r)
        if include_source:
            d["source_snippet"] = _read_source_snippet(
                d.get("file_path", ""),
                d.get("line_start"),
                d.get("line_end"),
                repo_root=repo_root,
            )
        result.append(d)
    return result


def find_tasks_by_code_node(
    conn: sqlite3.Connection,
    code_node_id: int,
) -> list[dict[str, Any]]:
    """Return all tasks that reference *code_node_id*."""
    rows = conn.execute(
        """
        SELECT t.*, tcr.ref_type, tcr.description as ref_description
        FROM task_code_refs tcr
        JOIN tasks t ON t.id = tcr.task_id
        WHERE tcr.code_node_id = ?
        ORDER BY t.created_at
        """,
        (code_node_id,),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def suggest_code_links(
    conn: sqlite3.Connection,
    task_id: str,
) -> list[dict[str, Any]]:
    """Suggest code nodes to link based on keyword extraction from the task.

    Extracts significant words from title and description, then runs a
    keyword search against the nodes table (name + qualified_name).
    Returns candidate nodes — does NOT create any links automatically.
    """
    task = get_task(conn, task_id)
    text = " ".join(filter(None, [task.get("title"), task.get("description")]))
    keywords = _extract_keywords(text)
    if not keywords:
        return []

    results: dict[int, dict[str, Any]] = {}
    for kw in keywords:
        pattern = f"%{kw}%"
        rows = conn.execute(
            """
            SELECT id, kind, name, qualified_name, file_path, line_start, line_end, language
            FROM nodes
            WHERE name LIKE ? COLLATE NOCASE
               OR qualified_name LIKE ? COLLATE NOCASE
            LIMIT 10
            """,
            (pattern, pattern),
        ).fetchall()
        for row in rows:
            if row["id"] not in results:
                results[row["id"]] = _row_to_dict(row)

    return list(results.values())


def _extract_keywords(text: str) -> list[str]:
    """Extract meaningful words (≥4 chars) from task text for code search."""
    words = re.findall(r"[A-Za-z][A-Za-z0-9_]*", text)
    stop_words = frozenset({
        "this", "that", "with", "from", "have", "will", "need", "should",
        "must", "task", "todo", "implement", "create", "update", "delete",
        "add", "remove", "make", "build", "test", "check", "get", "set",
    })
    seen: set[str] = set()
    result: list[str] = []
    for w in words:
        lower = w.lower()
        if len(w) >= 4 and lower not in stop_words and lower not in seen:
            seen.add(lower)
            result.append(w)
    return result[:10]  # limit to 10 keywords


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------


def add_note(
    conn: sqlite3.Connection,
    task_id: str,
    note_type: str,
    content: str,
    status: str = "open",
    resolution: Optional[str] = None,
    rationale: Optional[str] = None,
    alternatives: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Add a brainstorm note (decision / question / assumption / constraint / risk)."""
    get_task(conn, task_id)
    _require(content, "content")
    _check_enum(note_type, NOTE_TYPES, "note_type")
    _check_enum(status, NOTE_STATUSES, "status")

    note_id = _new_id()
    now = _now()
    alternatives_json = json.dumps(alternatives) if alternatives is not None else None

    conn.execute(
        """
        INSERT INTO notes
            (id, task_id, note_type, content, status, resolution, rationale, alternatives,
             created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (note_id, task_id, note_type, content, status, resolution, rationale,
         alternatives_json, now, now),
    )
    conn.commit()
    return _get_note(conn, note_id)


def _get_note(conn: sqlite3.Connection, note_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    if row is None:
        raise KeyError(f"Note '{note_id}' not found")
    d = _row_to_dict(row)
    if d.get("alternatives"):
        try:
            d["alternatives"] = json.loads(d["alternatives"])
        except (json.JSONDecodeError, TypeError):
            pass
    return d


def update_note(
    conn: sqlite3.Connection,
    note_id: str,
    *,
    status: Optional[str] = None,
    resolution: Optional[str] = None,
    rationale: Optional[str] = None,
    content: Optional[str] = None,
) -> dict[str, Any]:
    """Update fields of an existing note."""
    _get_note(conn, note_id)  # verify exists

    if status is not None:
        _check_enum(status, NOTE_STATUSES, "status")

    updates, params = _build_update([
        ("status", status),
        ("resolution", resolution),
        ("rationale", rationale),
        ("content", content),
    ])

    if not updates:
        return _get_note(conn, note_id)

    updates.append("updated_at = ?")
    params.append(_now())
    params.append(note_id)

    conn.execute(
        f"UPDATE notes SET {', '.join(updates)} WHERE id = ?",  # noqa: S608
        params,
    )
    conn.commit()
    return _get_note(conn, note_id)


def list_notes(
    conn: sqlite3.Connection,
    task_id: str,
    note_type: Optional[str] = None,
    status: Optional[str] = None,
    include_parent: bool = True,
    include_children: bool = False,
) -> list[dict[str, Any]]:
    """List notes for a task.

    If *include_parent* is True, notes from all ancestor tasks (up to root)
    are included, annotated with their originating task_id.

    If *include_children* is True, notes from all descendant tasks are also
    included. Useful for searching notes across an entire brainstorm subtree.
    """
    get_task(conn, task_id)

    if note_type is not None:
        _check_enum(note_type, NOTE_TYPES, "note_type")
    if status is not None:
        _check_enum(status, NOTE_STATUSES, "status")

    task_ids: list[str] = [task_id]
    if include_parent:
        task_ids = _collect_ancestor_ids(conn, task_id)
    if include_children:
        # BFS subtree (excludes task_id itself — already in task_ids)
        subtree = _collect_subtree_ids(conn, task_id)
        for tid in subtree:
            if tid not in task_ids:
                task_ids.append(tid)

    placeholders = ", ".join("?" * len(task_ids))
    extra_clauses = []
    extra_params: list[Any] = []
    if note_type is not None:
        extra_clauses.append("note_type = ?")
        extra_params.append(note_type)
    if status is not None:
        extra_clauses.append("status = ?")
        extra_params.append(status)

    where_extra = (f" AND {' AND '.join(extra_clauses)}" if extra_clauses else "")
    rows = conn.execute(
        f"SELECT * FROM notes WHERE task_id IN ({placeholders}){where_extra} ORDER BY created_at",  # noqa: S608
        (*task_ids, *extra_params),
    ).fetchall()

    result = []
    for row in rows:
        d = _row_to_dict(row)
        if d.get("alternatives"):
            try:
                d["alternatives"] = json.loads(d["alternatives"])
            except (json.JSONDecodeError, TypeError):
                pass
        result.append(d)
    return result


def delete_note(conn: sqlite3.Connection, note_id: str) -> dict[str, Any]:
    """Delete a note by ID."""
    note = _get_note(conn, note_id)
    conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
    conn.commit()
    return note


def _collect_ancestor_ids(conn: sqlite3.Connection, task_id: str) -> list[str]:
    """Collect task_id and all ancestor IDs from child to root."""
    ids = [task_id]
    current = task_id
    while True:
        row = conn.execute(
            "SELECT parent_id FROM tasks WHERE id = ?", (current,)
        ).fetchone()
        if row is None or row[0] is None:
            break
        ids.append(row[0])
        current = row[0]
    return ids


# ---------------------------------------------------------------------------
# Contracts — first-class design entities
# ---------------------------------------------------------------------------


def add_contract(
    conn: sqlite3.Connection,
    contract_type: str,
    definition: str,
    name: str,
    scope_task_id: str,
    *,
    provider_task_id: Optional[str] = None,
    consumer_task_ids: Optional[list[str]] = None,
    code_node_id: Optional[int] = None,
    qualified_name: Optional[str] = None,
) -> dict[str, Any]:
    """Create a contract / design entity scoped to a brainstorm subtree.

    A contract can represent a new data structure (OAuthToken), a shared
    interface (IUserRepo), an API endpoint spec, or a schema change on an
    *existing* code node.

    To link to an existing code node provide EITHER *code_node_id* (integer
    ``id`` from ``semantic_search_nodes_tool``) OR *qualified_name* (string
    ``qualified_name`` field from the same results).  Omit both when the
    contract describes a *new* entity that does not yet exist in the code graph.

    Participants are optional at creation time — attach them later with
    :func:`link_contract`.

    Args:
        contract_type: One of CONTRACT_TYPES (schema / interface / api / event).
        definition: Human-readable spec (TypeScript-like, JSON Schema, etc.).
        name: Short identifier, e.g. "OAuthToken", "UserRepo.save()".
        scope_task_id: Root task that owns this brainstorm scope.
        provider_task_id: Task that will *implement* this contract (optional).
        consumer_task_ids: Tasks that will *use* this contract (optional list).
        code_node_id: Integer node id from ``semantic_search_nodes_tool``.
        qualified_name: Qualified name string from search results or code_refs.
    """
    get_task(conn, scope_task_id)
    _require(definition, "definition")
    _require(name, "name")
    _check_enum(contract_type, CONTRACT_TYPES, "contract_type")

    # Resolve optional code node reference
    if code_node_id is not None or qualified_name is not None:
        code_node_id = _resolve_code_node(
            conn, code_node_id, qualified_name, caller="add_contract"
        )

    # Validate optional task refs
    if provider_task_id:
        get_task(conn, provider_task_id)
    for cid in (consumer_task_ids or []):
        get_task(conn, cid)

    contract_id = _new_id()
    now = _now()
    # After v7 migration, provider_task_id and consumer_task_id allow NULL.
    # We use contract_links for participants; legacy columns kept for back-compat.
    conn.execute(
        """
        INSERT INTO contracts
            (id, name, contract_type, definition, status,
             scope_task_id, code_node_id,
             created_at, updated_at)
        VALUES (?, ?, ?, ?, 'proposed', ?, ?, ?, ?)
        """,
        (contract_id, name, contract_type, definition,
         scope_task_id, code_node_id, now, now),
    )

    # Insert contract_links rows
    if provider_task_id:
        conn.execute(
            "INSERT OR IGNORE INTO contract_links(contract_id, task_id, role, linked_at) "
            "VALUES (?, ?, 'provider', ?)",
            (contract_id, provider_task_id, now),
        )
    for cid in (consumer_task_ids or []):
        conn.execute(
            "INSERT OR IGNORE INTO contract_links(contract_id, task_id, role, linked_at) "
            "VALUES (?, ?, 'consumer', ?)",
            (contract_id, cid, now),
        )

    conn.commit()
    return _get_contract(conn, contract_id)


def link_contract(
    conn: sqlite3.Connection,
    contract_id: str,
    task_id: str,
    role: str,
) -> dict[str, Any]:
    """Attach a task to an existing contract as provider or consumer.

    Safe to call multiple times (INSERT OR IGNORE). Useful when task
    decomposition happens *after* the contract was created.

    Args:
        contract_id: Contract to link to.
        task_id: Task to attach.
        role: ``"provider"`` or ``"consumer"``.
    """
    _get_contract(conn, contract_id)
    get_task(conn, task_id)
    if role not in ("provider", "consumer"):
        raise ValueError(f"role must be 'provider' or 'consumer', got {role!r}")

    conn.execute(
        "INSERT OR IGNORE INTO contract_links(contract_id, task_id, role, linked_at) "
        "VALUES (?, ?, ?, ?)",
        (contract_id, task_id, role, _now()),
    )
    conn.commit()
    return _get_contract(conn, contract_id)


def unlink_contract(
    conn: sqlite3.Connection,
    contract_id: str,
    task_id: str,
) -> dict[str, Any]:
    """Remove all links (any role) between a contract and a task."""
    _get_contract(conn, contract_id)
    conn.execute(
        "DELETE FROM contract_links WHERE contract_id = ? AND task_id = ?",
        (contract_id, task_id),
    )
    conn.commit()
    return _get_contract(conn, contract_id)


def _get_contract(conn: sqlite3.Connection, contract_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM contracts WHERE id = ?", (contract_id,)).fetchone()
    if row is None:
        raise KeyError(f"Contract '{contract_id}' not found")
    d = _row_to_dict(row)
    # Enrich with contract_links (participants)
    link_rows = conn.execute(
        "SELECT task_id, role FROM contract_links WHERE contract_id = ? ORDER BY role, linked_at",
        (contract_id,),
    ).fetchall()
    providers = [r[0] for r in link_rows if r[1] == "provider"]
    consumers = [r[0] for r in link_rows if r[1] == "consumer"]
    d["provider_task_ids"] = providers
    d["consumer_task_ids"] = consumers
    return d


def update_contract(
    conn: sqlite3.Connection,
    contract_id: str,
    *,
    name: Optional[str] = None,
    definition: Optional[str] = None,
    status: Optional[str] = None,
    code_node_id: Optional[int] = None,
    qualified_name: Optional[str] = None,
) -> dict[str, Any]:
    """Update a contract's name, definition, status, or code_node reference.

    To update the linked code node provide EITHER *code_node_id* (integer)
    OR *qualified_name* (string from ``semantic_search_nodes_tool`` results).
    """
    _get_contract(conn, contract_id)  # verify exists

    if status is not None:
        _check_enum(status, CONTRACT_STATUSES, "status")

    # Resolve code node if either identifier was supplied
    if code_node_id is not None or qualified_name is not None:
        code_node_id = _resolve_code_node(
            conn, code_node_id, qualified_name, caller="update_contract"
        )

    updates, params = _build_update([
        ("name", name),
        ("definition", definition),
        ("status", status),
        ("code_node_id", code_node_id),
    ])

    if not updates:
        return _get_contract(conn, contract_id)

    updates.append("updated_at = ?")
    params.append(_now())
    params.append(contract_id)

    conn.execute(
        f"UPDATE contracts SET {', '.join(updates)} WHERE id = ?",  # noqa: S608
        params,
    )
    conn.commit()
    return _get_contract(conn, contract_id)


def list_contracts(
    conn: sqlite3.Connection,
    *,
    scope_task_id: Optional[str] = None,
    task_id: Optional[str] = None,
    name: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Return contracts matching the given filters.

    Filters can be combined:
    - ``scope_task_id`` — all contracts in a brainstorm subtree (incl. orphans).
    - ``task_id`` — contracts where this task participates (any role).
    - ``name`` — exact or partial name match (LIKE, case-insensitive).

    At least one filter must be provided.
    """
    if scope_task_id is None and task_id is None and name is None:
        raise ValueError("list_contracts requires at least one filter")

    clauses: list[str] = []
    params: list[Any] = []

    if task_id is not None:
        get_task(conn, task_id)
        clauses.append(
            "c.id IN (SELECT contract_id FROM contract_links WHERE task_id = ?)"
        )
        params.append(task_id)

    if scope_task_id is not None:
        get_task(conn, scope_task_id)
        # Collect all task IDs in the subtree for scope matching
        subtree = _collect_subtree_ids(conn, scope_task_id)
        ph = ", ".join("?" * len(subtree))
        clauses.append(f"c.scope_task_id IN ({ph})")  # noqa: S608
        params.extend(subtree)

    if name is not None:
        escaped = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append("c.name LIKE ? ESCAPE '\\'")
        params.append(f"%{escaped}%")

    where = " AND ".join(clauses)
    rows = conn.execute(
        f"SELECT c.* FROM contracts c WHERE {where} ORDER BY c.created_at",  # noqa: S608
        params,
    ).fetchall()

    result = []
    for row in rows:
        d = _row_to_dict(row)
        link_rows = conn.execute(
            "SELECT task_id, role FROM contract_links WHERE contract_id = ? ORDER BY role, linked_at",
            (d["id"],),
        ).fetchall()
        d["provider_task_ids"] = [r[0] for r in link_rows if r[1] == "provider"]
        d["consumer_task_ids"] = [r[0] for r in link_rows if r[1] == "consumer"]
        result.append(d)
    return result
