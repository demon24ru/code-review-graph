"""Output contract utilities for MCP tool responses.

Provides:
  - ``prune_empty`` / ``prune_response`` — strip None/empty fields to save tokens
  - ``graph_error``  — unified error dict with deterministic ``next_action``

Inspired by hex-graph-mcp ``output-contract.mjs`` / ``pruneEmpty()``.
No imports from the rest of the package — safe to import anywhere.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Sentinel
# ---------------------------------------------------------------------------


class _Sentinel:
    """Internal marker meaning "remove this value"."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<PRUNE>"


_SENTINEL = _Sentinel()

# ---------------------------------------------------------------------------
# prune_empty / prune_response
# ---------------------------------------------------------------------------


def prune_empty(value: Any) -> Any:
    """Recursively remove ``None``, empty lists ``[]``, and empty dicts ``{}``
    from a structure.

    Empty strings, ``False``, and ``0`` are **preserved** — only structural
    empties are stripped.

    Returns ``_SENTINEL`` (not ``None``) when the whole value should be
    removed so callers can distinguish "remove me" from the legitimate ``None``
    python value.

    Example::

        >>> prune_empty({"a": 1, "b": None, "c": [], "d": {"e": {}}})
        {"a": 1}
    """
    if isinstance(value, dict):
        pruned = {k: prune_empty(v) for k, v in value.items()}
        pruned = {k: v for k, v in pruned.items() if not isinstance(v, _Sentinel)}
        return pruned if pruned else _SENTINEL
    if isinstance(value, list):
        items = [prune_empty(i) for i in value]
        items = [i for i in items if not isinstance(i, _Sentinel)]
        return items if items else _SENTINEL
    if value is None:
        return _SENTINEL
    return value


def prune_response(response: dict[str, Any]) -> dict[str, Any]:
    """Apply ``prune_empty`` to a top-level tool response dict.

    - Internal ``_``-prefixed fields (e.g. ``_hints``) are stripped before
      pruning so they are never exposed to MCP clients.
    - Always returns a plain ``dict`` — never ``_SENTINEL``.
    """
    cleaned = {k: v for k, v in response.items() if not k.startswith("_")}
    result = prune_empty(cleaned)
    return result if isinstance(result, dict) else {"status": "ok"}


# ---------------------------------------------------------------------------
# graph_error — unified error format with deterministic next_action
# ---------------------------------------------------------------------------

_ERROR_NEXT_ACTION: dict[str, str] = {
    # Code-graph errors
    "NOT_INDEXED": "build_or_update_graph_tool",
    "NOT_FOUND": "semantic_search_nodes_tool",
    "AMBIGUOUS": "query_graph_tool",
    "DB_BUSY": "build_or_update_graph_tool",
    "DB_UNREADABLE": "build_or_update_graph_tool",
    "PATH_NOT_FOUND": "find_files_by_pattern_tool",
    "FILE_OUTSIDE_ROOT": "find_files_by_pattern_tool",
    "INVALID_PARAMS": "get_docs_section_tool",
    "SCIP_FAILED": "build_or_update_graph_tool",
    "PARSE_ERROR": "build_or_update_graph_tool",
    # Task DAG errors — route to task-specific tools
    "TASK_NOT_FOUND": "task_list",
    "TASK_INVALID_PARAMS": "task_get",
    "TASK_CYCLE": "task_get_dag",
    "TASK_HAS_CHILDREN": "task_delete",
    "TASK_PARSE_ERROR": "task_validate",
    "CODE_REF_NOT_FOUND": "task_get_code_refs",
    "SINGLE_PIPELINE_VIOLATION": "task_update",
    # Contract errors — route to contract-specific tools
    "CONTRACT_NOT_FOUND": "contract_list",
    "CONTRACT_INVALID_PARAMS": "contract_list",
    "CONTRACT_ERROR": "contract_list",
}

_ERROR_RECOVERY: dict[str, str] = {
    # Code-graph errors
    "NOT_INDEXED": "Run build_or_update_graph_tool first to index the project.",
    "NOT_FOUND": "Use semantic_search_nodes_tool or find_files_by_pattern_tool to discover the symbol.",
    "AMBIGUOUS": "Provide a fully qualified name (e.g. 'src/auth.py::save_user') to disambiguate.",
    "DB_BUSY": "Close other sessions using the same graph DB, then retry.",
    "DB_UNREADABLE": "Check that .code-review-graph/graph.db is readable, then retry.",
    "PATH_NOT_FOUND": "Verify the file path exists inside the indexed project root.",
    "FILE_OUTSIDE_ROOT": "Pass a file path inside the indexed project root.",
    "INVALID_PARAMS": "Check parameter docs via get_docs_section_tool.",
    "SCIP_FAILED": "Run build_or_update_graph_tool first, then verify the SCIP file path.",
    "PARSE_ERROR": "Verify the SCIP file is valid JSON produced by export_scip_tool.",
    # Task DAG errors
    "TASK_NOT_FOUND": "Use task_list to browse existing tasks, or task_search to find by keyword.",
    "TASK_INVALID_PARAMS": (
        "Check valid values: status in (draft|refined|ready|in_progress|done|archived), "
        "ref_type in (modifies|creates|deletes|reads|tests), "
        "edge_type in (depends_on|blocks|informs|related_to)."
    ),
    "TASK_CYCLE": "Use task_get_dag to visualise the dependency graph and identify the cycle.",
    "TASK_HAS_CHILDREN": "Pass cascade=True to delete the task and all its subtasks.",
    "TASK_PARSE_ERROR": "Verify task_id exists via task_get, then retry the operation.",
    "CODE_REF_NOT_FOUND": (
        "Use task_get_code_refs(task_id=...) to list which code nodes are linked to the task."
    ),
    "SINGLE_PIPELINE_VIOLATION": (
        "Use task_update(status='done') or task_archive() to close the blocking root task, then retry."
    ),
    # Contract errors
    "CONTRACT_NOT_FOUND": "Use contract_list(scope_task_id=...) to browse existing contracts.",
    "CONTRACT_INVALID_PARAMS": (
        "Check valid values: contract_type in (interface|api|schema|event|data_format), "
        "status in (proposed|agreed|implemented|verified|void), "
        "role in (provider|consumer)."
    ),
    "CONTRACT_ERROR": "Verify contract_id and parameters, then retry.",
}


def graph_error(
    code: str,
    message: str,
    recovery: str | None = None,
) -> dict[str, Any]:
    """Build a compact, deterministic error response.

    The ``next_action`` field maps the error code to the single most useful
    follow-up tool, so LLMs never have to guess what to do after an error.

    Args:
        code:     Short uppercase error code (e.g. ``"NOT_INDEXED"``).
        message:  Human-readable explanation.
        recovery: Optional override for the recovery hint.

    Returns:
        Dict with ``status``, ``code``, ``summary``, ``next_action``,
        and ``recovery``.  ``None``/empty fields are already pruned.
    """
    return {
        "status": "error",
        "code": code,
        "summary": message,
        "next_action": _ERROR_NEXT_ACTION.get(code, "semantic_search_nodes_tool"),
        "recovery": recovery or _ERROR_RECOVERY.get(code, "Adjust parameters and retry."),
    }
