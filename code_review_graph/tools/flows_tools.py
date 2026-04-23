"""Tools 10, 11: list_flows, get_flow."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..flows import get_flow_by_id, get_flows
from ..hints import generate_hints, get_session
from ._common import _get_store, graph_error

# ---------------------------------------------------------------------------
# Tool 10: list_flows  [EXPLORE]
# ---------------------------------------------------------------------------


def list_flows(
    repo_root: str | None = None,
    sort_by: str = "criticality",
    limit: int = 50,
    kind: str | None = None,
    is_test: bool | None = None,
) -> dict[str, Any]:
    """List execution flows in the codebase, sorted by criticality.

    [EXPLORE] Retrieves stored execution flows from the knowledge graph.
    Each flow represents a call chain starting from an entry point
    (e.g. HTTP handler, CLI command, test function).

    Args:
        repo_root: Repository root path. Auto-detected if omitted.
        sort_by: Sort column: criticality, depth, node_count, file_count,
                 or name.
        limit: Maximum flows to return (default: 50).
        kind: Optional filter by entry point kind (e.g. "Test", "Function").
        is_test: Filter by test status. True=only test flows, False=only
                 production flows (excludes Test-kind entry points).
                 Default None shows all.

    Returns:
        List of flows with criticality scores and total_count.
    """
    store, root = _get_store(repo_root)
    try:
        # Fetch more to account for post-fetch filtering
        fetch_limit = limit
        if kind or is_test is not None:
            fetch_limit = limit * 10
        flows = get_flows(store, sort_by=sort_by, limit=fetch_limit)
        total_before_filter = len(flows)

        if kind:
            filtered = []
            for f in flows:
                ep_id = f.get("entry_point_id")
                if ep_id is not None:
                    node_kind = store.get_node_kind_by_id(ep_id)
                    if node_kind == kind:
                        filtered.append(f)
            flows = filtered

        if is_test is not None:
            test_flows = []
            for f in flows:
                ep_id = f.get("entry_point_id")
                if ep_id is not None:
                    node_kind = store.get_node_kind_by_id(ep_id)
                    flow_is_test = (node_kind == "Test")
                    if flow_is_test == is_test:
                        test_flows.append(f)
                elif is_test is False:
                    # No entry_point → treat as non-test
                    test_flows.append(f)
            flows = test_flows

        flows = flows[:limit]

        result: dict[str, object] = {
            "status": "ok",
            "summary": f"Found {len(flows)} execution flow(s)"
            + (
                f" (filtered from {total_before_filter})"
                if total_before_filter > len(flows)
                else ""
            ),
            "flows": flows,
            "total_count": len(flows),
        }
        result["_hints"] = generate_hints("list_flows", result, get_session())
        return result
    except Exception as exc:
        return graph_error("PARSE_ERROR", str(exc))
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Tool 11: get_flow  [EXPLORE]
# ---------------------------------------------------------------------------


def get_flow(
    flow_id: int | None = None,
    flow_name: str | None = None,
    include_source: bool = False,
    repo_root: str | None = None,
) -> dict[str, Any]:
    """Get details of a single execution flow.

    [EXPLORE] Retrieves full path details for a flow, including each step's
    function name, file, and line numbers.  Optionally includes source
    snippets for every step in the path.

    Args:
        flow_id: Database ID of the flow (from list_flows).
        flow_name: Name to search for (partial match). Ignored if flow_id
                   given.
        include_source: If True, include source code snippets for each step.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        Flow details with steps, or not_found status.
    """
    store, root = _get_store(repo_root)
    try:
        flow: dict | None = None

        if flow_id is not None:
            flow = get_flow_by_id(store, flow_id)
        elif flow_name is not None:
            # Search flows by name match
            all_flows = get_flows(store, sort_by="criticality", limit=500)
            for f in all_flows:
                if flow_name.lower() in f["name"].lower():
                    flow = get_flow_by_id(store, f["id"])
                    break

        if flow is None:
            return {
                "status": "not_found",
                "summary": "No flow found matching the given criteria.",
            }

        # Optionally include source snippets for each step
        if include_source and "steps" in flow:
            for step in flow["steps"]:
                fp = Path(step["file"]) if step.get("file") else None
                if fp is not None and not fp.is_absolute():
                    fp = root / fp
                file_path = fp
                if file_path and file_path.is_file():
                    try:
                        lines = file_path.read_text(errors="replace").splitlines()
                        start = max(0, (step.get("line_start") or 1) - 1)
                        end = min(
                            len(lines),
                            step.get("line_end") or len(lines),
                        )
                        step["source"] = "\n".join(
                            f"{i + 1}: {lines[i]}" for i in range(start, end)
                        )
                    except (OSError, UnicodeDecodeError):
                        step["source"] = "(could not read file)"

        result = {
            "status": "ok",
            "summary": (
                f"Flow '{flow['name']}': {flow['node_count']} nodes, "
                f"depth {flow['depth']}, "
                f"criticality {flow['criticality']:.4f}"
            ),
            "flow": flow,
        }
        result["_hints"] = generate_hints("get_flow", result, get_session())
        return result
    except Exception as exc:
        return graph_error("PARSE_ERROR", str(exc))
    finally:
        store.close()
