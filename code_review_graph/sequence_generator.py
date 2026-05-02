"""Mermaid sequence diagram generator.

Generates sequence diagrams from:
 - Existing execution flows in the code graph (Mode A)
 - Task contracts and execution order from brainstorm tasks (Mode B)
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from typing import Any

from .flows import get_flow_by_id, get_flows
from .graph import GraphStore
from .task_analysis import execution_order
from .tasks import get_task, list_contracts, list_notes

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _file_participant_id(file_path: str) -> str:
    """Convert a file path to a Mermaid-safe participant ID.

    Uses the basename without extension, with non-alphanumeric chars replaced
    by underscores.
    """
    if not file_path:
        return "unknown"
    basename = os.path.basename(file_path)
    name = os.path.splitext(basename)[0] or "unknown"
    return re.sub(r"[^a-zA-Z0-9_]", "_", name)


def _task_safe_id(task_id: str) -> str:
    """Convert a task UUID to a Mermaid-safe participant ID."""
    # UUIDs contain hyphens which are not valid in Mermaid participant IDs
    return "t_" + re.sub(r"[^a-zA-Z0-9]", "_", task_id)


def _task_short_title(conn: sqlite3.Connection, task_id: str) -> str:
    """Get a short display title for a task, falling back to truncated ID."""
    try:
        task = get_task(conn, task_id)
        title = task.get("title", task_id)
        return title[:25] if len(title) > 25 else title
    except Exception:
        return task_id[:8]


# ---------------------------------------------------------------------------
# Mode A: From existing execution flow
# ---------------------------------------------------------------------------


def sequence_from_flow(
    store: GraphStore,
    flow_id: int | None = None,
    flow_name: str | None = None,
) -> dict[str, Any]:
    """Generate Mermaid sequence diagram from an existing execution flow.

    Uses get_flow_by_id() to get the call chain, then converts to Mermaid.

    Returns:
        {
            mermaid: str,         # Complete Mermaid sequence diagram text
            source: "flow",
            participant_count: int,
            step_count: int,
        }
    """
    flow: dict[str, Any] | None = None

    if flow_id is not None:
        flow = get_flow_by_id(store, flow_id)
    elif flow_name is not None:
        all_flows = get_flows(store, limit=500)
        for f in all_flows:
            if flow_name.lower() in f["name"].lower():
                flow = get_flow_by_id(store, f["id"])
                break

    if flow is None:
        return {
            "status": "not_found",
            "summary": "No flow found matching the given criteria.",
            "mermaid": "",
            "source": "flow",
            "participant_count": 0,
            "step_count": 0,
        }

    steps: list[dict[str, Any]] = flow.get("steps", [])

    if not steps:
        mermaid = "sequenceDiagram\n    Note over System: No steps found"
        return {
            "status": "ok",
            "mermaid": mermaid,
            "source": "flow",
            "participant_count": 0,
            "step_count": 0,
        }

    # Build participant mapping: file_path -> safe_id
    # Files that appear with the same basename get a numeric suffix.
    file_to_id: dict[str, str] = {}
    base_id_counts: dict[str, int] = {}

    for step in steps:
        fp = step.get("file") or ""
        if fp not in file_to_id:
            base_id = _file_participant_id(fp)
            if base_id in base_id_counts:
                base_id_counts[base_id] += 1
                file_to_id[fp] = f"{base_id}_{base_id_counts[base_id]}"
            else:
                base_id_counts[base_id] = 0
                file_to_id[fp] = base_id

    # Collect participants in order of first appearance
    seen_fps: list[str] = []
    for step in steps:
        fp = step.get("file") or ""
        if fp not in seen_fps:
            seen_fps.append(fp)

    lines: list[str] = ["sequenceDiagram"]

    for fp in seen_fps:
        p_id = file_to_id[fp]
        display = os.path.basename(fp) if fp else "unknown"
        lines.append(f"    participant {p_id} as {display}")

    # Add arrows for consecutive step pairs
    for i in range(len(steps) - 1):
        src_fp = steps[i].get("file") or ""
        tgt_fp = steps[i + 1].get("file") or ""
        src_id = file_to_id[src_fp]
        tgt_id = file_to_id[tgt_fp]
        func_name = steps[i + 1].get("name") or "call"
        lines.append(f"    {src_id}->>{tgt_id}: {func_name}()")

    return {
        "status": "ok",
        "mermaid": "\n".join(lines),
        "source": "flow",
        "participant_count": len(seen_fps),
        "step_count": len(steps),
    }


# ---------------------------------------------------------------------------
# Mode B: From task contracts
# ---------------------------------------------------------------------------


def sequence_from_contracts(
    conn: sqlite3.Connection,
    task_id: str,
) -> dict[str, Any]:
    """Generate Mermaid sequence from task contracts and execution order.

    Returns:
        {
            mermaid: str,
            source: "contracts",
            participant_count: int,
            call_count: int,
            gaps: list[str],    # What's missing for a complete sequence
        }
    """
    contracts = list_contracts(conn, scope_task_id=task_id)

    # Get leaf tasks in execution order for context
    try:
        order_result = execution_order(conn, task_id)
        ordered_tasks: list[dict[str, Any]] = []
        for lvl in order_result.get("levels", []):
            ordered_tasks.extend(lvl.get("tasks", []))
    except Exception:
        ordered_tasks = []

    # Build participant map from task IDs referenced in contracts
    task_id_to_safe: dict[str, str] = {}
    task_id_to_title: dict[str, str] = {}

    for contract in contracts:
        for tid in contract.get("provider_task_ids", []):
            if tid not in task_id_to_safe:
                task_id_to_safe[tid] = _task_safe_id(tid)
                task_id_to_title[tid] = _task_short_title(conn, tid)
        for tid in contract.get("consumer_task_ids", []):
            if tid not in task_id_to_safe:
                task_id_to_safe[tid] = _task_safe_id(tid)
                task_id_to_title[tid] = _task_short_title(conn, tid)

    # Fallback: if no contract participants found, use all leaf tasks
    if not task_id_to_safe and ordered_tasks:
        for task in ordered_tasks:
            tid = task["id"]
            title = task.get("title", tid)
            task_id_to_safe[tid] = _task_safe_id(tid)
            task_id_to_title[tid] = title[:25] if len(title) > 25 else title

    lines: list[str] = ["sequenceDiagram"]

    # Declare participants in order from contracts
    seen: list[str] = []
    for contract in contracts:
        for tid in contract.get("provider_task_ids", []):
            if tid not in seen and tid in task_id_to_safe:
                p_id = task_id_to_safe[tid]
                display = task_id_to_title[tid]
                lines.append(f"    participant {p_id} as {display}")
                seen.append(tid)
        for tid in contract.get("consumer_task_ids", []):
            if tid not in seen and tid in task_id_to_safe:
                p_id = task_id_to_safe[tid]
                display = task_id_to_title[tid]
                lines.append(f"    participant {p_id} as {display}")
                seen.append(tid)

    # Fallback participants from leaf tasks
    if not seen and ordered_tasks:
        for task in ordered_tasks:
            tid = task["id"]
            if tid in task_id_to_safe and tid not in seen:
                p_id = task_id_to_safe[tid]
                display = task_id_to_title[tid]
                lines.append(f"    participant {p_id} as {display}")
                seen.append(tid)

    if not seen:
        lines.append("    Note over System: No participants found")

    # Add arrows from contract relationships: provider ->> consumer: contract_name
    call_count = 0
    for contract in contracts:
        providers = contract.get("provider_task_ids", [])
        consumers = contract.get("consumer_task_ids", [])
        label = contract.get("name", "call")
        for provider_id in providers:
            for consumer_id in consumers:
                if provider_id in task_id_to_safe and consumer_id in task_id_to_safe:
                    p_id = task_id_to_safe[provider_id]
                    c_id = task_id_to_safe[consumer_id]
                    lines.append(f"    {p_id}->>{c_id}: {label}")
                    call_count += 1

    if not contracts:
        lines.append("    Note over System: No contracts defined")

    mermaid = "\n".join(lines)

    # Detect gaps
    gaps: list[str] = []
    if not contracts:
        gaps.append("No contracts defined — full sequence unknown")

    try:
        risk_notes = list_notes(
            conn, task_id, note_type="risk", include_parent=False, include_children=True
        )
        if not risk_notes:
            gaps.append("No error cases defined")
    except Exception:
        gaps.append("No error cases defined")

    return {
        "status": "ok",
        "mermaid": mermaid,
        "source": "contracts",
        "participant_count": len(seen),
        "call_count": call_count,
        "gaps": gaps,
    }


# ---------------------------------------------------------------------------
# Top-level dispatcher
# ---------------------------------------------------------------------------


def generate_sequence(
    store: GraphStore | None = None,
    conn: sqlite3.Connection | None = None,
    task_id: str | None = None,
    flow_id: int | None = None,
    flow_name: str | None = None,
) -> dict[str, Any]:
    """Auto-generate Mermaid sequence diagram.

    Dispatches to sequence_from_flow or sequence_from_contracts based on args:
    - If task_id + conn are provided → sequence_from_contracts
    - If flow_id or flow_name + store are provided → sequence_from_flow
    """
    if task_id is not None and conn is not None:
        return sequence_from_contracts(conn, task_id)
    if (flow_id is not None or flow_name is not None) and store is not None:
        return sequence_from_flow(store, flow_id=flow_id, flow_name=flow_name)
    return {
        "status": "error",
        "summary": (
            "Provide either (task_id + conn) for contract-based mode, "
            "or (flow_id/flow_name + store) for flow-based mode."
        ),
        "mermaid": "",
    }
