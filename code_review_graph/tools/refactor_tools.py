"""Tools 17, 18: refactor_func, apply_refactor_func."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..hints import generate_hints, get_session
from ..incremental import find_project_root
from ..refactor import (
    _pending_refactors,
    apply_refactor,
    find_dead_code,
    rename_preview,
    suggest_refactorings,
)
from ._common import _get_store, _validate_repo_root, graph_error

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool 17: refactor_tool  [REFACTOR]
# ---------------------------------------------------------------------------


def refactor_func(
    mode: str = "rename",
    old_name: str | None = None,
    new_name: str | None = None,
    kind: str | None = None,
    file_pattern: str | None = None,
    exclude_paths: list[str] | None = None,
    repo_root: str | None = None,
) -> dict[str, Any]:
    """Unified refactoring entry point.

    [REFACTOR] Supports three modes:
    - ``rename``: Preview renaming a symbol (requires *old_name* and
      *new_name*).
    - ``dead_code``: Find unreferenced functions/classes.
    - ``suggest``: Get community-driven refactoring suggestions.

    Args:
        mode: One of ``"rename"``, ``"dead_code"``, or ``"suggest"``.
        old_name: (rename mode) Current symbol name.
        new_name: (rename mode) Desired new name.
        kind: (dead_code mode) Optional node kind filter.
        file_pattern: (dead_code mode) Optional file path substring filter.
        exclude_paths: (dead_code mode) List of path substrings to exclude.
            Nodes in matching files are omitted from results.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        Mode-specific results dict.
    """
    valid_modes = {"rename", "dead_code", "suggest"}
    if mode not in valid_modes:
        return graph_error(
            "INVALID_PARAMS",
            f"Invalid mode '{mode}'. Must be one of: {', '.join(sorted(valid_modes))}",
        )

    store, root = _get_store(repo_root)
    try:
        if mode == "rename":
            if not old_name or not new_name:
                return graph_error(
                    "INVALID_PARAMS",
                    "rename mode requires both old_name and new_name.",
                )
            preview = rename_preview(store, old_name, new_name)
            if preview is None:
                return {
                    "status": "not_found",
                    "summary": f"No node found matching '{old_name}'.",
                }
            result = {
                "status": "ok",
                "summary": (
                    f"Rename preview: {old_name} -> {new_name}, "
                    f"{len(preview['edits'])} edit(s). "
                    f"Use apply_refactor_tool(refactor_id="
                    f"'{preview['refactor_id']}') to apply."
                ),
                **preview,
            }
            result["_hints"] = generate_hints("refactor", result, get_session())
            return result

        elif mode == "dead_code":
            dead = find_dead_code(
                store, kind=kind, file_pattern=file_pattern, exclude_paths=exclude_paths
            )
            result = {
                "status": "ok",
                "summary": f"Found {len(dead)} dead code symbol(s).",
                "dead_code": dead,
                "total": len(dead),
            }
            result["_hints"] = generate_hints("refactor", result, get_session())
            return result

        else:  # suggest
            suggestions = suggest_refactorings(store)
            result = {
                "status": "ok",
                "summary": (f"Generated {len(suggestions)} refactoring suggestion(s)."),
                "suggestions": suggestions,
                "total": len(suggestions),
            }
            result["_hints"] = generate_hints("refactor", result, get_session())
            return result

    except Exception as exc:
        return graph_error("PARSE_ERROR", str(exc))
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Tool 18: apply_refactor_tool  [REFACTOR]
# ---------------------------------------------------------------------------


def apply_refactor_func(
    refactor_id: str,
    repo_root: str | None = None,
) -> dict[str, Any]:
    """Apply a previously previewed refactoring to source files.

    [REFACTOR] Validates the refactor_id, checks expiry, ensures all edit
    paths are within the repo root, then performs exact string replacements.
    After successful application, updates the graph DB node names.

    Args:
        refactor_id: ID returned by a prior ``refactor_tool(mode="rename")``
            call.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        Status with count of applied edits and modified files.
    """
    try:
        root = _validate_repo_root(Path(repo_root)) if repo_root else find_project_root()
    except (RuntimeError, ValueError) as exc:
        return graph_error("PATH_NOT_FOUND", str(exc))

    # Grab old/new names BEFORE apply_refactor pops the preview from _pending_refactors
    preview = _pending_refactors.get(refactor_id)
    old_name = preview.get("old_name") if preview else None
    new_name = preview.get("new_name") if preview else None

    result = apply_refactor(refactor_id, root)

    # After successful apply, sync the graph DB node names
    if result.get("status") == "ok" and old_name and new_name and result.get("applied", 0) > 0:
        try:
            store, _ = _get_store(str(root))
            store._conn.execute(
                "UPDATE nodes SET name = ? WHERE name = ?",
                (new_name, old_name),
            )
            store._conn.commit()
            result["graph_updated"] = True
            logger.info(
                "apply_refactor_func: updated graph DB nodes from %r to %r",
                old_name,
                new_name,
            )
            store.close()
        except Exception as exc:
            logger.warning("apply_refactor_func: graph DB update failed: %s", exc)
            result["graph_updated"] = False
            result["graph_update_warning"] = str(exc)

    return result
