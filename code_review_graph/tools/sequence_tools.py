"""MCP tool wrapper for sequence diagram generation (Tool: generate_sequence)."""

from __future__ import annotations

from typing import Any

from ..sequence_generator import generate_sequence
from ._common import _get_store, graph_error


def generate_sequence_func(
    task_id: str | None = None,
    flow_id: int | None = None,
    flow_name: str | None = None,
    repo_root: str | None = None,
) -> dict[str, Any]:
    """Auto-generate a Mermaid sequence diagram from flows or task contracts.

    Dispatches to the appropriate mode based on which arguments are provided:

    - **Flow mode** (``flow_id`` or ``flow_name``): Reads the stored execution
      flow from the code graph and converts its call chain into a Mermaid
      ``sequenceDiagram``.  Participants are grouped by source file.

    - **Contract mode** (``task_id``): Uses task contracts and execution order
      from the Task DAG to build a sequence diagram.  Each contract becomes an
      arrow from provider to consumer.  Gaps (missing error cases, etc.) are
      reported in the ``gaps`` field.

    Args:
        task_id: Root task ID for contract-based generation.
        flow_id: Integer flow ID for flow-based generation.
        flow_name: Partial flow name for flow-based generation (ignored if
            flow_id is given).
        repo_root: Repository root path.  Auto-detected if omitted.

    Returns:
        Dict with ``mermaid`` (string), ``source`` ("flow" or "contracts"),
        and mode-specific metadata fields.
    """
    store, _ = _get_store(repo_root)
    try:
        if task_id is not None:
            return generate_sequence(
                store=store,
                conn=store._conn,
                task_id=task_id,
            )
        if flow_id is not None or flow_name is not None:
            return generate_sequence(
                store=store,
                flow_id=flow_id,
                flow_name=flow_name,
            )
        return graph_error(
            "INVALID_PARAMS",
            "Provide task_id (contract mode) or flow_id/flow_name (flow mode).",
        )
    except Exception as exc:
        return graph_error("PARSE_ERROR", str(exc))
    finally:
        store.close()
