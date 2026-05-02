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
from typing import Any, Optional, Union

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Valid enumeration values (enforced at the Python layer)
# ---------------------------------------------------------------------------

TASK_STATUSES = frozenset({"draft", "refined", "ready", "in_progress", "done", "archived"})
TASK_EDGE_TYPES = frozenset({"depends_on", "blocks", "shares_context", "conflicts_with", "informs"})
CODE_REF_TYPES = frozenset({"modifies", "creates", "deletes", "reads", "tests"})
NOTE_TYPES = frozenset({"decision", "question", "assumption", "constraint", "risk"})
NOTE_STATUSES = frozenset({"open", "answered", "resolved", "rejected", "deferred"})
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
    # Use time.time_ns() for higher precision to ensure unique timestamps in batch operations
    return time.time_ns() / 1e9


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
    tasks: list[dict[str, Any]],
    parent_id: Optional[str] = None,
    edges: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Create one or more tasks sharing a common parent.

    **Batch mode** — always a list, even for a single task:

        create_task(conn, tasks=[{"title": "OAuth interface"}], parent_id="t1")
        create_task(conn, tasks=[
            {"title": "OAuth interface"},
            {"title": "Google OAuth", "description": "impl Google provider"},
            {"title": "JWT service"},
        ], parent_id="t1")

    Each item must have ``title`` (required) and may include ``description``.

    **Inline edges** — pass ``edges`` to create relationships between the new
    tasks in the same atomic transaction.  Use zero-based indices into ``tasks``
    instead of task IDs (which are not known yet at call time):

        create_task(conn, parent_id="t1", tasks=[
            {"title": "OAuth interface"},   # index 0
            {"title": "Google OAuth"},      # index 1
            {"title": "JWT service"},       # index 2
            {"title": "Login endpoint"},    # index 3
        ], edges=[
            {"from": 1, "to": 0, "type": "depends_on"},
            {"from": 3, "to": 0, "type": "depends_on"},
            {"from": 3, "to": 2, "type": "depends_on"},
        ])

    Edge items: ``from`` and ``to`` are required (0-based task indices).
    ``type`` is optional (default ``"depends_on"``).
    ``description`` is optional.

    **Single-pipeline discipline**: a new *root* task (``parent_id=None``)
    can only be created when there is no other open root task.  Close or
    archive the current root before starting a new one.  This check is
    performed once, before any task is inserted.

    Returns ``{"tasks": [...], "edges": [...]}``.  ``edges`` is omitted when
    no inline edges were requested.
    """
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("tasks must be a non-empty list of dicts")

    # Validate all items first — before any business-rule checks —
    # so the caller gets a clear "title is required" error rather than a
    # potentially confusing single-pipeline error.
    for item in tasks:
        if not isinstance(item, dict):
            raise ValueError("Each task item must be a dict with at least 'title'")
        _require(item.get("title"), "title")

    # Validate inline edge specs early (index bounds, required fields).
    n = len(tasks)
    edge_specs: list[tuple[int, int, str, Optional[str]]] = []
    if edges:
        for i, espec in enumerate(edges):
            if not isinstance(espec, dict):
                raise ValueError(f"edges[{i}] must be a dict with 'from' and 'to'")
            from_idx = espec.get("from")
            to_idx = espec.get("to")
            if from_idx is None or to_idx is None:
                raise ValueError(f"edges[{i}] must have 'from' and 'to' (0-based task indices)")
            if not isinstance(from_idx, int) or not isinstance(to_idx, int):
                raise ValueError(f"edges[{i}] 'from' and 'to' must be integers")
            if not (0 <= from_idx < n) or not (0 <= to_idx < n):
                raise ValueError(
                    f"edges[{i}] indices out of range: from={from_idx}, to={to_idx}, "
                    f"tasks has {n} items (0-{n - 1})"
                )
            if from_idx == to_idx:
                raise ValueError(f"edges[{i}] self-referencing edge (from={from_idx})")
            etype: str = espec.get("type") or "depends_on"
            _check_enum(etype, TASK_EDGE_TYPES, "edge type")
            edge_specs.append((from_idx, to_idx, etype, espec.get("description")))

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

    created_ids: list[str] = []
    for i, item in enumerate(tasks):
        title = item.get("title")
        description = item.get("description")
        task_id = _new_id()
        now = _now()
        # Add a tiny sleep to ensure unique timestamps for batch operations
        if i < len(tasks) - 1:
            time.sleep(0.000001)  # 1 microsecond
        conn.execute(
            """
            INSERT INTO tasks (id, parent_id, title, description, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'draft', ?, ?)
            """,
            (task_id, parent_id, title, description, now, now),
        )
        created_ids.append(task_id)
        logger.debug("Created task %s: %r", task_id, title)

    # Insert inline edges if provided.  Cycle checks run before any insert.
    created_edges: list[dict[str, Any]] = []
    if edge_specs:
        # Track ordering edges seen so far (type-aware) for intra-batch cycle detection.
        # Same pattern as add_task_edge: only previously-resolved edges are passed as pending
        # so each new edge is validated against already-accepted same-batch edges.
        pending_ordering_local: list[tuple[str, str, str]] = []  # (src, tgt, type)
        try:
            for fi, ti, etype, desc in edge_specs:
                src_id = created_ids[fi]
                tgt_id = created_ids[ti]
                if etype in ("depends_on", "blocks"):
                    same_type_pending = [(s, t) for s, t, et in pending_ordering_local if et == etype]
                    _assert_no_cycle_after_edge(conn, src_id, tgt_id, etype, pending=same_type_pending)
                    # Cross-type deadlock: same logic as add_task_edge.
                    if etype == "blocks":
                        depends_on_pending = [(s, t) for s, t, et in pending_ordering_local if et == "depends_on"]
                        _assert_no_cycle_after_edge(conn, tgt_id, src_id, "depends_on", pending=depends_on_pending)
                    pending_ordering_local.append((src_id, tgt_id, etype))
                conn.execute(
                    """
                    INSERT OR IGNORE INTO task_edges
                        (source_task_id, target_task_id, type, description, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (src_id, tgt_id, etype, desc, _now()),
                )
                created_edges.append(
                    {
                        "source_task_id": src_id,
                        "target_task_id": tgt_id,
                        "type": etype,
                        "description": desc,
                    }
                )
                logger.debug("Created inline edge %s → %s (%s)", src_id, tgt_id, etype)
        except Exception:
            conn.rollback()
            raise

    conn.commit()
    result: dict[str, Any] = {"tasks": [get_task(conn, tid) for tid in created_ids]}
    if created_edges:
        result["edges"] = created_edges
    return result


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


def update_tasks(
    conn: sqlite3.Connection,
    updates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Update multiple tasks in a single transaction.

    Batch-only API: always pass a list, even for a single task.

    Each item in *updates* must have ``task_id`` plus any of:
    ``title``, ``description``, ``status``, ``spec``, ``acceptance_criteria``.

    Args:
        updates: List of dicts, each with ``task_id`` (required) and optional
            ``title``, ``description``, ``status``, ``spec``,
            ``acceptance_criteria``.

    Returns:
        ``{"tasks": [updated_task_objects], "errors": [{task_id, error}, ...]}``
    """
    updated_tasks: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for item in updates:
        task_id = item.get("task_id")
        if not task_id:
            errors.append({"task_id": "", "error": "missing task_id"})
            continue
        try:
            task = update_task(
                conn, task_id,
                title=item.get("title"),
                description=item.get("description"),
                status=item.get("status"),
                spec=item.get("spec"),
                acceptance_criteria=item.get("acceptance_criteria"),
            )
            updated_tasks.append(task)
        except (KeyError, ValueError) as exc:
            errors.append({"task_id": task_id, "error": str(exc)})

    return {"tasks": updated_tasks, "errors": errors}


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
    dry_run: bool = False,
) -> dict[str, Any]:
    """Delete a task.

    If *cascade* is True, all subtasks (recursive) are deleted along with
    their edges, code refs, notes, and contracts.

    If *dry_run* is True, returns what WOULD be deleted without performing
    the operation.

    Returns ``{ deleted_ids: [...], deleted_tasks: [{id, title}, ...] }``
    or ``{ dry_run: True, would_delete: [...], count: ... }`` if dry_run=True.
    """
    get_task(conn, task_id)  # raises KeyError if not found

    if not cascade:
        child = conn.execute(
            "SELECT id FROM tasks WHERE parent_id = ? LIMIT 1", (task_id,)
        ).fetchone()
        if child:
            raise ValueError(
                f"Task '{task_id}' has children. Use cascade=True to delete recursively."
            )

    if cascade:
        ids_to_delete = _collect_subtree_ids(conn, task_id)
    else:
        ids_to_delete = [task_id]

    # Collect titles BEFORE deletion for confirmation in response
    if ids_to_delete:
        ph = ", ".join("?" * len(ids_to_delete))
        title_rows = conn.execute(  # noqa: S608
            f"SELECT id, title FROM tasks WHERE id IN ({ph})", ids_to_delete
        ).fetchall()
        deleted_titles = {r["id"]: r["title"] for r in title_rows}
    else:
        deleted_titles = {}

    # Preview mode: return what WOULD be deleted without doing it
    if dry_run:
        return {
            "dry_run": True,
            "would_delete": [
                {"id": tid, "title": deleted_titles.get(tid, "<unknown>")}
                for tid in ids_to_delete
            ],
            "count": len(ids_to_delete),
        }

    for tid in ids_to_delete:
        _delete_task_data(conn, tid)

    conn.commit()
    return {
        "deleted_ids": ids_to_delete,
        "deleted_tasks": [
            {"id": tid, "title": deleted_titles.get(tid, "<unknown>")}
            for tid in ids_to_delete
        ],
    }


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
    for cid in orphan_contracts:
        conn.execute("DELETE FROM contracts WHERE id = ?", (cid,))
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
    task_ids: list[str],
    new_parent_id: Optional[str] = None,
) -> dict[str, Any]:
    """Move one or more tasks to a shared new parent (or promote them to root).

    **Batch mode** — always a list, even for a single task:

        move_task(conn, task_ids=["t2", "t3", "t4"], new_parent_id="t11")
        move_task(conn, task_ids=["t5"], new_parent_id=None)   # promote to root

    Raises *ValueError* if any move would create a hierarchy cycle.
    All cycle checks are performed before any update is applied (atomic).

    Returns ``{"tasks": [...]}``.
    """
    if not isinstance(task_ids, list) or not task_ids:
        raise ValueError("task_ids must be a non-empty list of task IDs")

    for tid in task_ids:
        get_task(conn, tid)  # verify all tasks exist first

    if new_parent_id is not None:
        get_task(conn, new_parent_id)  # verify target exists
        # Cycle check: new_parent must not be in the subtree of any moved task
        for tid in task_ids:
            subtree_ids = set(_collect_subtree_ids(conn, tid))
            if new_parent_id in subtree_ids:
                # Use titles in error message for human readability
                try:
                    tid_title = get_task(conn, tid).get("title", tid)
                    parent_title = get_task(conn, new_parent_id).get("title", new_parent_id)
                except KeyError:
                    tid_title, parent_title = tid, new_parent_id
                raise ValueError(
                    f"Cannot move task '{tid_title}' under '{parent_title}': "
                    "would create a hierarchy cycle"
                )

    now = _now()
    for tid in task_ids:
        conn.execute(
            "UPDATE tasks SET parent_id = ?, updated_at = ? WHERE id = ?",
            (new_parent_id, now, tid),
        )
    conn.commit()
    return {"tasks": [get_task(conn, tid) for tid in task_ids]}


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
    task_ids: list[str],
    reason: str,
    cascade: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Archive one or more tasks (and optionally their subtrees).

    **Batch mode** — always a list, even for a single task:

        archive_task(conn, task_ids=["t2", "t3", "t6"], reason="Switching to in-app only")
        archive_task(conn, task_ids=["t2"], reason="No longer needed")

    Sets status to 'archived' and records the *reason*. Does not delete —
    archived tasks remain in the database for historical reference.
    *cascade* applies to each task individually.

    If *dry_run* is True, returns what WOULD be archived without performing
    the operation.

    **Idempotent**: Re-archiving an already-archived task does NOT overwrite
    the original archive_reason. Already-archived tasks are returned separately.

    Returns ``{"archived": [...], "already_archived": [...], "archived_ids": [...]}``
    or ``{ dry_run: True, reason: ..., would_archive: [...], count: ... }`` if dry_run=True.
    """
    if not isinstance(task_ids, list) or not task_ids:
        raise ValueError("task_ids must be a non-empty list of task IDs")
    _require(reason, "reason")

    for tid in task_ids:
        get_task(conn, tid)  # verify all exist first

    ids_to_archive: list[str] = []
    for tid in task_ids:
        if cascade:
            ids_to_archive.extend(_collect_subtree_ids(conn, tid))
        else:
            ids_to_archive.append(tid)

    # Deduplicate while preserving order (subtrees may overlap)
    seen: set[str] = set()
    unique_ids: list[str] = []
    for tid in ids_to_archive:
        if tid not in seen:
            seen.add(tid)
            unique_ids.append(tid)

    # Collect titles for preview
    ph = ", ".join("?" * len(unique_ids))
    rows = conn.execute(  # noqa: S608
        f"SELECT id, title FROM tasks WHERE id IN ({ph})", unique_ids
    ).fetchall()
    would_archive = [{"id": r["id"], "title": r["title"]} for r in rows]

    # Preview mode: return what WOULD be archived without doing it
    if dry_run:
        return {
            "dry_run": True,
            "reason": reason,
            "would_archive": would_archive,
            "count": len(would_archive),
        }

    now = _now()
    
    # Query current status of each task to detect already-archived ones
    to_archive: list[str] = []
    already_archived: list[str] = []
    
    for tid in unique_ids:
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone()
        if row and row[0] == "archived":
            already_archived.append(tid)
        else:
            to_archive.append(tid)
    
    # Only update tasks that are not yet archived
    for tid in to_archive:
        conn.execute(
            "UPDATE tasks SET status = 'archived', archive_reason = ?, updated_at = ? WHERE id = ?",
            (reason, now, tid),
        )

    conn.commit()
    return {
        "archived": [get_task(conn, tid) for tid in to_archive],
        "already_archived": [get_task(conn, tid) for tid in already_archived],
        "archived_ids": to_archive,  # kept for backward compat: only newly archived
    }


# ---------------------------------------------------------------------------
# DAG edge management
# ---------------------------------------------------------------------------


def add_task_edge(
    conn: sqlite3.Connection,
    edges: list[dict[str, Any]],
    edge_type: Optional[str] = None,
) -> dict[str, Any]:
    """Add one or more directed edges between tasks.

    **Batch mode** — always a list, even for a single edge:

        # One source → many targets (Pattern A):
        add_task_edge(conn, edge_type="depends_on", edges=[
            {"source_id": "t8", "target_id": "t5"},
            {"source_id": "t8", "target_id": "t6"},
            {"source_id": "t8", "target_id": "t7"},
        ])

        # Many sources → one target (Pattern B):
        add_task_edge(conn, edge_type="depends_on", edges=[
            {"source_id": "t5", "target_id": "t2"},
            {"source_id": "t6", "target_id": "t2"},
        ])

        # Mixed (Pattern C) — per-item edge_type overrides default:
        add_task_edge(conn, edge_type="depends_on", edges=[
            {"source_id": "t5", "target_id": "t2"},
            {"source_id": "t6", "target_id": "t7", "edge_type": "shares_context"},
        ])

    Each item must have ``source_id`` and ``target_id``.
    ``edge_type`` per item overrides the top-level default.
    ``description`` per item is optional.

    For *depends_on* and *blocks* edge types, performs a cycle check via DFS.
    All cycle checks run before any insert (atomic).

    Returns ``{"edges": [...]}``.
    """
    if not isinstance(edges, list) or not edges:
        raise ValueError("edges must be a non-empty list of dicts")

    # Validate all items and pre-resolve edge types before touching the DB
    resolved: list[tuple[str, str, str, Optional[str]]] = []
    for item in edges:
        if not isinstance(item, dict):
            raise ValueError("Each edge item must be a dict with source_id and target_id")
        source_id: str = item.get("source_id", "")
        target_id: str = item.get("target_id", "")
        item_edge_type: str = item.get("edge_type") or edge_type or ""
        description: Optional[str] = item.get("description")
        if not source_id or not target_id:
            raise ValueError("Each edge item must have 'source_id' and 'target_id'")
        get_task(conn, source_id)
        get_task(conn, target_id)
        _check_enum(item_edge_type, TASK_EDGE_TYPES, "edge_type")
        if source_id == target_id:
            raise ValueError(f"Cannot add a self-referencing edge for task '{source_id}'")
        resolved.append((source_id, target_id, item_edge_type, description))

    # Cycle checks for ordering edges (before any insert).
    # Pass all previously-checked ordering edges of the same type as *pending*
    # so that intra-batch cycles (e.g. A→B + B→A in one call) are detected
    # even though nothing has been inserted into the DB yet.
    pending_ordering: list[tuple[str, str, str]] = []  # (src, tgt, type)
    for source_id, target_id, item_edge_type, _ in resolved:
        if item_edge_type in _ORDERING_EDGE_TYPES:
            same_type_pending = [(s, t) for s, t, et in pending_ordering if et == item_edge_type]
            _assert_no_cycle_after_edge(
                conn, source_id, target_id, item_edge_type, pending=same_type_pending
            )
            # Cross-type deadlock: blocks(A→B) + depends_on(A→B) = mutual wait (A waits for B
            # via depends_on, B waits for A via blocks).  Detect by checking if target can reach
            # source via depends_on — equivalent to "would adding depends_on(target→source)
            # create a depends_on cycle?".
            if item_edge_type == "blocks":
                depends_on_pending = [(s, t) for s, t, et in pending_ordering if et == "depends_on"]
                _assert_no_cycle_after_edge(
                    conn, target_id, source_id, "depends_on", pending=depends_on_pending
                )
            pending_ordering.append((source_id, target_id, item_edge_type))

    now = _now()
    created: list[dict[str, Any]] = []
    for source_id, target_id, item_edge_type, description in resolved:
        existing = conn.execute(
            "SELECT source_task_id, target_task_id, type, description, created_at "
            "FROM task_edges WHERE source_task_id = ? AND target_task_id = ? AND type = ?",
            (source_id, target_id, item_edge_type),
        ).fetchone()
        if existing:
            created.append({
                "source_task_id": source_id,
                "target_task_id": target_id,
                "type": item_edge_type,
                "description": existing["description"],
                "created_at": existing["created_at"],
                "already_exists": True,
            })
            continue
        conn.execute(
            """
            INSERT INTO task_edges
                (source_task_id, target_task_id, type, description, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (source_id, target_id, item_edge_type, description, now),
        )
        created.append({
            "source_task_id": source_id,
            "target_task_id": target_id,
            "type": item_edge_type,
            "description": description,
            "created_at": now,
            "already_exists": False,
        })

    conn.commit()
    return {"edges": created}


def _assert_no_cycle_after_edge(
    conn: sqlite3.Connection,
    source_id: str,
    target_id: str,
    edge_type: str,
    pending: Optional[list[tuple[str, str]]] = None,
) -> None:
    """DFS reachability: if *target_id* can already reach *source_id* via the
    same edge type, adding source→target would create a cycle.

    *pending* is a list of (src, tgt) tuples already resolved in the current
    batch but not yet inserted into the DB.  They are treated as virtual edges
    so that intra-batch cycles (e.g. A→B + B→A in one call) are detected.
    """
    # Build a small in-memory adjacency map from pending edges of the same type
    pending_adj: dict[str, list[str]] = {}
    for psrc, ptgt in (pending or []):
        pending_adj.setdefault(psrc, []).append(ptgt)

    visited: set[str] = set()
    stack = [target_id]
    while stack:
        node = stack.pop()
        if node == source_id:
            # Try to get human-readable titles, fall back to IDs
            try:
                src_title = get_task(conn, source_id).get("title", source_id)
                tgt_title = get_task(conn, target_id).get("title", target_id)
            except (KeyError, Exception):
                src_title, tgt_title = source_id, target_id
            raise ValueError(
                f"Adding edge '{src_title}' --{edge_type}--> '{tgt_title}' "
                "would create a cycle in the task dependency graph"
            )
        if node in visited:
            continue
        visited.add(node)
        db_neighbours = conn.execute(
            "SELECT target_task_id FROM task_edges WHERE source_task_id = ? AND type = ?",
            (node, edge_type),
        ).fetchall()
        stack.extend(r[0] for r in db_neighbours)
        stack.extend(pending_adj.get(node, []))


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
    compact: bool = False,
) -> dict[str, Any]:
    """Return the full DAG rooted at *root_task_id*.

    Collects all tasks in the subtree plus all task_edges between them.
    Returns ``{ nodes: [...], edges: [...] }`` suitable for visualisation.

    Args:
        conn: Database connection.
        root_task_id: Root task ID.
        compact: If True, return only {id, title, status, depth, parent_id} per node.
                 If False (default), return full task objects with depth field added.
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

    # Compute depth of each task from root using BFS
    depth_map: dict[str, int] = {root_task_id: 0}
    queue = deque([root_task_id])
    while queue:
        current = queue.popleft()
        children = conn.execute(
            "SELECT id FROM tasks WHERE parent_id = ?", (current,)
        ).fetchall()
        for child in children:
            cid = child["id"]
            if cid not in depth_map:
                depth_map[cid] = depth_map[current] + 1
                queue.append(cid)

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

    # Build node list based on compact mode
    if compact:
        def _compact_node(row_dict: dict[str, Any], depth: int) -> dict[str, Any]:
            return {
                "id": row_dict["id"],
                "title": row_dict["title"],
                "status": row_dict["status"],
                "depth": depth,
                "parent_id": row_dict.get("parent_id"),
            }
        node_list = [
            _compact_node(_row_to_dict(r), depth_map.get(r["id"], 0))
            for r in nodes
        ]
    else:
        node_list = []
        for r in nodes:
            d = _row_to_dict(r)
            d["depth"] = depth_map.get(r["id"], 0)
            node_list.append(d)

    # Sort nodes: depth first (BFS order), then created_at for stable tie-breaking
    node_list.sort(key=lambda n: (n.get("depth", 0), n.get("created_at", "") or ""))

    return {
        "nodes": node_list,
        "edges": [_row_to_dict(r) for r in edges] + hierarchy_edges,
    }


def topological_sort_tasks(
    conn: sqlite3.Connection,
    root_task_id: str,
) -> list[dict[str, Any]]:
    """Topological sort of leaf tasks (no children) using Kahn's algorithm.

    Only *depends_on* edges are considered for ordering.
    Raises *ValueError* if a dependency cycle is detected.

    Returns tasks in topological order (dependencies first). Each task
    includes a ``topo_order`` field (1-based index in sort order).

    Note: prefer ``execution_order`` / ``task_execution_order`` for parallel
    scheduling — it groups tasks into executable levels, which is strictly more
    useful than a flat ordered list.
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

    result = []
    for i, tid in enumerate(sorted_ids, 1):
        if tid in id_to_row:
            task = id_to_row[tid].copy()
            task["topo_order"] = i  # 1-based position in topological order
            result.append(task)
    return result


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
    links: list[dict[str, Any]],
) -> dict[str, Any]:
    """Link a task to one or many code graph nodes.

    **Batch mode** — always a list, even for a single node:

        link_task_code(conn, task_id, links=[
            {"ref_type": "modifies", "code_node_id": 101},
        ])
        link_task_code(conn, task_id, links=[
            {"ref_type": "modifies", "code_node_id": 101},
            {"ref_type": "modifies", "code_node_id": 102},
            {"ref_type": "reads",    "qualified_name": "src/auth.py::TokenService"},
            {"ref_type": "creates",  "qualified_name": "src/models.py::OAuthToken",
             "description": "new model"},
        ])

    Each item requires ``ref_type`` and one of ``code_node_id`` / ``qualified_name``.
    Failed items are collected in ``errors`` — does not abort the whole batch.

    Using *qualified_name* lets the LLM pass the value directly from
    ``semantic_search_nodes_tool`` or ``task_export`` code_refs without a
    separate ID-lookup step.  Path separators are normalised (``\\`` → ``/``).

    ref_type values: modifies | creates | deletes | reads | tests

    Returns:
        {
            "task_id": "…",
            "linked": [{"code_node_id": …, "ref_type": …, "qualified_name": …}, …],
            "errors": [{"item": …, "error": "…"}, …],
            "total": N,
            "success_count": N,
            "error_count": N,
        }
    """
    get_task(conn, task_id)
    if not isinstance(links, list):
        raise ValueError("links must be a list of dicts")
    now = _now()

    linked: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for item in links:
        if not isinstance(item, dict):
            errors.append({"item": item, "error": "each link item must be a dict"})
            continue
        item_ref_type = item.get("ref_type")
        item_node_id: Optional[int] = item.get("code_node_id")
        item_qname: Optional[str] = item.get("qualified_name")
        item_desc: Optional[str] = item.get("description")
        try:
            _check_enum(item_ref_type, CODE_REF_TYPES, "ref_type")
            resolved_id = _resolve_code_node(
                conn, item_node_id, item_qname, caller="link_task_code"
            )
            # Check if (task_id, code_node_id) already exists regardless of ref_type
            existing = conn.execute(
                "SELECT ref_type FROM task_code_refs WHERE task_id = ? AND code_node_id = ?",
                (task_id, resolved_id),
            ).fetchone()
            if existing is not None:
                errors.append({
                    "item": item,
                    "error": f"Node {resolved_id} is already linked to this task "
                             f"(ref_type='{existing[0]}')",
                    "already_linked": True,
                })
                continue
            conn.execute(
                """
                INSERT INTO task_code_refs
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
        "total": len(links),
        "success_count": len(linked),
        "error_count": len(errors),
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
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Suggest code nodes to link based on keyword extraction from the task.

    Extracts significant words from title and description, then runs a
    keyword search against the nodes table (name + qualified_name).
    Results are scored by number of matching keywords and ranked descending.
    Already-linked nodes are excluded.
    Returns candidate nodes — does NOT create any links automatically.

    Args:
        conn: Database connection.
        task_id: Task ID to find suggestions for.
        limit: Maximum number of results to return (default 20).

    Returns:
        List of candidate code node dicts, each with a 'match_score' field.
    """
    task = get_task(conn, task_id)
    text = " ".join(filter(None, [task.get("title"), task.get("description")]))
    keywords = _extract_keywords(text)
    if not keywords:
        return []

    # Get already-linked node IDs to exclude them
    existing_rows = conn.execute(
        "SELECT code_node_id FROM task_code_refs WHERE task_id = ?", (task_id,)
    ).fetchall()
    already_linked: set[int] = {r["code_node_id"] for r in existing_rows}

    # Score nodes by how many keywords they match
    scores: dict[int, int] = {}
    node_data: dict[int, dict[str, Any]] = {}

    for kw in keywords:
        pattern = f"%{kw}%"
        rows = conn.execute(
            """
            SELECT id, kind, name, qualified_name, file_path, line_start, line_end, language, is_test
            FROM nodes
            WHERE (name LIKE ? COLLATE NOCASE
               OR qualified_name LIKE ? COLLATE NOCASE)
              AND (is_test = 0 OR is_test IS NULL)
            LIMIT 20
            """,
            (pattern, pattern),
        ).fetchall()
        for row in rows:
            node_id = row["id"]
            if node_id in already_linked:
                continue
            scores[node_id] = scores.get(node_id, 0) + 1
            if node_id not in node_data:
                node_data[node_id] = _row_to_dict(row)

    # Sort by score descending, take top `limit`
    sorted_ids = sorted(scores, key=lambda nid: scores[nid], reverse=True)
    result = []
    for nid in sorted_ids[:limit]:
        entry = node_data[nid].copy()
        entry["match_score"] = scores[nid]
        result.append(entry)
    return result


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
    notes: list[dict[str, Any]],
) -> dict[str, Any]:
    """Add one or more brainstorm notes to a task.

    **Batch mode** — always a list, even for a single note:

        add_note(conn, task_id, notes=[
            {"note_type": "decision", "content": "Use JWT", "status": "resolved",
             "resolution": "JWT tokens", "rationale": "stateless"},
        ])
        add_note(conn, task_id, notes=[
            {"note_type": "decision", "content": "Use JWT",
             "status": "resolved", "resolution": "JWT tokens"},
            {"note_type": "constraint", "content": "self-hosted only"},
            {"note_type": "question", "content": "WebSocket or polling?"},
            {"note_type": "assumption", "content": "User model already exists"},
        ])

    Each item requires ``note_type`` and ``content``.
    Optional per-item fields: ``status`` (default "open"), ``resolution``,
    ``rationale``, ``alternatives``, ``c4_element_id``.

    Note types: decision | question | assumption | constraint | risk

    Returns ``{"notes": [...]}``.
    """
    get_task(conn, task_id)
    if not isinstance(notes, list) or not notes:
        raise ValueError("notes must be a non-empty list of dicts")
    now = _now()

    created_ids: list[str] = []
    for item in notes:
        if not isinstance(item, dict):
            raise ValueError("Each note item must be a dict with at least 'note_type' and 'content'")
        note_type = item.get("note_type", "")
        content = item.get("content", "")
        status = item.get("status", "open")
        resolution = item.get("resolution")
        rationale = item.get("rationale")
        alternatives = item.get("alternatives")
        c4_element_id = item.get("c4_element_id")
        _require(content, "content")
        _check_enum(note_type, NOTE_TYPES, "note_type")
        _check_enum(status, NOTE_STATUSES, "status")
        note_id = _new_id()
        alternatives_json = json.dumps(alternatives) if alternatives is not None else None
        conn.execute(
            """
            INSERT INTO notes
                (id, task_id, note_type, content, status, resolution, rationale, alternatives,
                 c4_element_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (note_id, task_id, note_type, content, status, resolution, rationale,
             alternatives_json, c4_element_id, now, now),
        )
        created_ids.append(note_id)

    conn.commit()
    return {"notes": [_get_note(conn, nid) for nid in created_ids]}


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


def update_notes(
    conn: sqlite3.Connection,
    updates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Update multiple notes in a single transaction.

    Batch-only API: always pass a list, even for a single note.

    Each item in *updates* must have ``note_id`` plus any of:
    ``status``, ``resolution``, ``content``, ``rationale``.

    Args:
        updates: List of dicts, each with ``note_id`` (required) and optional
            ``status``, ``resolution``, ``content``, ``rationale``.

    Returns:
        ``{"notes": [updated_note_objects], "errors": [{note_id, error}, ...]}``
    """
    updated_notes: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for item in updates:
        note_id = item.get("note_id")
        if not note_id:
            errors.append({"note_id": "", "error": "missing note_id"})
            continue
        try:
            note = update_note(
                conn, note_id,
                status=item.get("status"),
                resolution=item.get("resolution"),
                content=item.get("content"),
                rationale=item.get("rationale"),
            )
            updated_notes.append(note)
        except (KeyError, ValueError) as exc:
            errors.append({"note_id": note_id, "error": str(exc)})

    # update_note commits individually; no extra commit needed
    return {"notes": updated_notes, "errors": errors}


def list_notes(
    conn: sqlite3.Connection,
    task_id: str,
    note_type: Optional[Union[str, list[str]]] = None,
    status: Optional[str] = None,
    include_parent: bool = True,
    include_children: bool = False,
) -> list[dict[str, Any]]:
    """List notes for a task.

    If *include_parent* is True, notes from all ancestor tasks (up to root)
    are included, annotated with their originating task_id.

    If *include_children* is True, notes from all descendant tasks are also
    included. Useful for searching notes across an entire brainstorm subtree.

    *note_type* may be a single string or a list of strings for multi-type
    filtering.
    """
    get_task(conn, task_id)

    if note_type is not None:
        if isinstance(note_type, list):
            for nt in note_type:
                _check_enum(nt, NOTE_TYPES, "note_type")
        else:
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
        if isinstance(note_type, list):
            type_placeholders = ", ".join("?" * len(note_type))
            extra_clauses.append(f"note_type IN ({type_placeholders})")  # noqa: S608
            extra_params.extend(note_type)
        else:
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
    # Participants are stored in contract_links (many-to-many), not in contracts table.
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
            "INSERT OR IGNORE INTO contract_links(contract_id, task_id, role) "
            "VALUES (?, ?, 'provider')",
            (contract_id, provider_task_id),
        )
    for cid in (consumer_task_ids or []):
        conn.execute(
            "INSERT OR IGNORE INTO contract_links(contract_id, task_id, role) "
            "VALUES (?, ?, 'consumer')",
            (contract_id, cid),
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

    Safe to call multiple times. Returns changed=False if link already exists.

    Args:
        contract_id: Contract to link to.
        task_id: Task to attach.
        role: ``"provider"`` or ``"consumer"``.
    """
    _get_contract(conn, contract_id)
    get_task(conn, task_id)
    if role not in ("provider", "consumer"):
        raise ValueError(f"role must be 'provider' or 'consumer', got {role!r}")

    # Check if link already exists
    existing = conn.execute(
        "SELECT 1 FROM contract_links WHERE contract_id = ? AND task_id = ? AND role = ?",
        (contract_id, task_id, role),
    ).fetchone()

    if existing:
        contract = _get_contract(conn, contract_id)
        contract["changed"] = False
        contract["reason"] = f"Task '{task_id}' is already linked as '{role}'"
        return contract

    conn.execute(
        "INSERT INTO contract_links(contract_id, task_id, role) "
        "VALUES (?, ?, ?)",
        (contract_id, task_id, role),
    )
    conn.commit()
    contract = _get_contract(conn, contract_id)
    contract["changed"] = True
    return contract


def unlink_contract(
    conn: sqlite3.Connection,
    contract_id: str,
    task_id: str,
) -> dict[str, Any]:
    """Remove all links (any role) between a contract and a task.
    
    Returns changed=False if the link did not exist.
    """
    _get_contract(conn, contract_id)
    cursor = conn.execute(
        "DELETE FROM contract_links WHERE contract_id = ? AND task_id = ?",
        (contract_id, task_id),
    )
    conn.commit()
    contract = _get_contract(conn, contract_id)
    contract["changed"] = cursor.rowcount > 0
    if not contract["changed"]:
        contract["reason"] = f"Task '{task_id}' was not linked to contract '{contract_id}'"
    return contract


def _get_contract(conn: sqlite3.Connection, contract_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM contracts WHERE id = ?", (contract_id,)).fetchone()
    if row is None:
        raise KeyError(f"Contract '{contract_id}' not found")
    d = _row_to_dict(row)
    # Enrich with contract_links (participants)
    link_rows = conn.execute(
        "SELECT task_id, role FROM contract_links WHERE contract_id = ? ORDER BY role, task_id",
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
    
    If status is updated to a backward lifecycle state, a warning is added.
    """
    _get_contract(conn, contract_id)  # verify exists

    if status is not None:
        _check_enum(status, CONTRACT_STATUSES, "status")

    # Capture old status before update (for lifecycle validation)
    old_row = conn.execute("SELECT status FROM contracts WHERE id = ?", (contract_id,)).fetchone()
    old_status = old_row["status"] if old_row else None

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
    result = _get_contract(conn, contract_id)

    # Check for backward lifecycle transition
    if status is not None and status != "void":
        STATUS_ORDER = {"proposed": 0, "agreed": 1, "implemented": 2, "verified": 3, "void": 99}
        old_order = STATUS_ORDER.get(old_status, -1)
        new_order = STATUS_ORDER.get(status, -1)
        if 0 <= new_order < old_order:
            result["warning"] = (
                f"Backward lifecycle transition: {old_status} → {status}. "
                f"Expected order: proposed → agreed → implemented → verified."
            )

    return result


def delete_contract(conn: sqlite3.Connection, contract_id: str) -> dict[str, Any]:
    """Delete a contract and all its participant links.

    Args:
        contract_id: Contract ID to delete.

    Returns:
        ``{"status": "ok", "deleted_contract_id": ..., "name": ...}``

    Raises:
        KeyError: if the contract does not exist.
    """
    row = conn.execute("SELECT id, name FROM contracts WHERE id = ?", (contract_id,)).fetchone()
    if not row:
        raise KeyError(f"Contract '{contract_id}' not found")
    conn.execute("DELETE FROM contract_links WHERE contract_id = ?", (contract_id,))
    conn.execute("DELETE FROM contracts WHERE id = ?", (contract_id,))
    conn.commit()
    return {"status": "ok", "deleted_contract_id": contract_id, "name": row["name"]}


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
            "SELECT task_id, role FROM contract_links WHERE contract_id = ? ORDER BY role, task_id",
            (d["id"],),
        ).fetchall()
        d["provider_task_ids"] = [r[0] for r in link_rows if r[1] == "provider"]
        d["consumer_task_ids"] = [r[0] for r in link_rows if r[1] == "consumer"]
        result.append(d)
    return result
