"""Thin stdin→stdout dispatcher for OpenCode custom tool wrapper.

Usage (from .opencode/tools/crg.ts via Bun.$):
    echo '<json>' | python code_review_graph/mcp_tool_runner.py

Input  (stdin): {"tool": "<tool_name>", "args": {<kwargs>}}
Output (stdout): JSON string with tool result

This module is intentionally small — it is the hot-reload boundary.
All real logic lives in main.py / tools.py / task_tools.py.
Editing THOSE files takes effect on the NEXT tool call with no restart.

To add a new tool:
    1. Implement it anywhere in code_review_graph/
    2. Register it in TOOL_REGISTRY below
    3. Add a matching thin export in .opencode/tools/crg.ts
       (requires opencode session reload — but only for the TS change)
"""

from __future__ import annotations

import json
import sys
import os
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Repo root auto-detection: walk up from this file until we find pyproject.toml
# or the .code-review-graph directory.
# ---------------------------------------------------------------------------


def _find_repo_root() -> str:
    here = Path(__file__).resolve().parent.parent  # code_review_graph/../
    for candidate in [here, *here.parents]:
        if (candidate / "pyproject.toml").exists() or (candidate / ".code-review-graph").exists():
            return str(candidate)
    return str(here)


_DEFAULT_REPO_ROOT = _find_repo_root()


def _inject_repo_root(args: dict[str, Any]) -> dict[str, Any]:
    """Fill in repo_root from environment or auto-detection if caller omitted it."""
    if "repo_root" not in args or args.get("repo_root") is None:
        args["repo_root"] = os.environ.get("CRG_REPO_ROOT") or _DEFAULT_REPO_ROOT
    return args


# ---------------------------------------------------------------------------
# Lazy tool imports — loaded only when actually called so startup is fast.
# ---------------------------------------------------------------------------


def _build_registry() -> dict[str, Any]:
    # Import here so this module can be imported without pulling in the whole
    # package at startup time (keeps the Bun.$ subprocess launch fast).
    from code_review_graph.tools import (
        analyze_edit_region,
        apply_refactor_func,
        audit_workspace,
        build_or_update_graph,
        cross_repo_search_func,
        detect_changes_func,
        embed_graph,
        export_scip_func,
        find_files_by_pattern,
        find_large_functions,
        generate_wiki_func,
        get_affected_flows_func,
        get_architecture_overview_func,
        get_community_func,
        get_docs_section,
        get_flow,
        get_impact_radius,
        get_review_context,
        get_wiki_page_func,
        import_scip_func,
        list_communities_func,
        list_flows,
        list_graph_stats,
        list_repos_func,
        query_graph,
        refactor_func,
        register_repo_func,
        semantic_search_nodes,
        trace_dataflow,
        unregister_repo_func,
    )
    from code_review_graph.tools.task_tools import (
        contract_add_func,
        contract_link_func,
        contract_list_func,
        contract_unlink_func,
        contract_update_func,
        note_add_func,
        note_delete_func,
        note_list_func,
        note_update_func,
        task_add_edge_func,
        task_archive_func,
        task_blast_radius_func,
        task_check_isolation_func,
        task_check_rollup_func,
        task_create_func,
        task_delete_func,
        task_edit_func,
        task_execution_order_func,
        task_export_func,
        task_find_by_code_node_func,
        task_find_conflicts_func,
        task_find_for_impact_func,
        task_get_active_root_func,
        task_get_code_refs_func,
        task_get_dag_func,
        task_get_func,
        task_link_code_func,
        task_list_func,
        task_move_func,
        task_remove_edge_func,
        task_roadmap_diff_func,
        task_roadmap_func,
        task_search_func,
        task_suggest_code_links_func,
        task_suggest_contracts_func,
        task_topological_sort_func,
        task_unlink_code_func,
        task_update_func,
        task_validate_func,
    )

    return {
        # ---- code graph tools ----
        "build_or_update_graph_tool": build_or_update_graph,
        "get_impact_radius_tool": get_impact_radius,
        "query_graph_tool": query_graph,
        "get_review_context_tool": get_review_context,
        "semantic_search_nodes_tool": semantic_search_nodes,
        "embed_graph_tool": embed_graph,
        "list_graph_stats_tool": list_graph_stats,
        "get_docs_section_tool": get_docs_section,
        "find_files_by_pattern_tool": find_files_by_pattern,
        "find_large_functions_tool": find_large_functions,
        "list_flows_tool": list_flows,
        "get_flow_tool": get_flow,
        "get_affected_flows_tool": get_affected_flows_func,
        "list_communities_tool": list_communities_func,
        "get_community_tool": get_community_func,
        "get_architecture_overview_tool": get_architecture_overview_func,
        "detect_changes_tool": detect_changes_func,
        "refactor_tool": refactor_func,
        "apply_refactor_tool": apply_refactor_func,
        "generate_wiki_tool": generate_wiki_func,
        "get_wiki_page_tool": get_wiki_page_func,
        "list_repos_tool": list_repos_func,
        "cross_repo_search_tool": cross_repo_search_func,
        "register_repo_tool": register_repo_func,
        "unregister_repo_tool": unregister_repo_func,
        "analyze_edit_region_tool": analyze_edit_region,
        "audit_workspace_tool": audit_workspace,
        "trace_dataflow_tool": trace_dataflow,
        "export_scip_tool": export_scip_func,
        "import_scip_tool": import_scip_func,
        # ---- task dag tools ----
        "task_get_active_root": task_get_active_root_func,
        "task_create": lambda **kw: task_create_func(
            tasks_list=kw.pop("tasks", []),
            edges_list=kw.pop("edges", None),
            **kw,
        ),
        "task_update": task_update_func,
        "task_edit": task_edit_func,
        "task_delete": task_delete_func,
        "task_get": task_get_func,
        "task_list": task_list_func,
        "task_move": task_move_func,
        "task_search": task_search_func,
        "task_archive": task_archive_func,
        "task_add_edge": task_add_edge_func,
        "task_remove_edge": task_remove_edge_func,
        "task_get_dag": task_get_dag_func,
        "task_topological_sort": task_topological_sort_func,
        "task_link_code": task_link_code_func,
        "task_unlink_code": task_unlink_code_func,
        "task_get_code_refs": task_get_code_refs_func,
        "task_find_by_code_node": task_find_by_code_node_func,
        "task_suggest_code_links": task_suggest_code_links_func,
        "task_find_conflicts": task_find_conflicts_func,
        "task_check_isolation": task_check_isolation_func,
        "task_blast_radius": task_blast_radius_func,
        "task_execution_order": task_execution_order_func,
        "task_validate": task_validate_func,
        "task_export": task_export_func,
        "note_add": note_add_func,
        "note_update": note_update_func,
        "note_list": note_list_func,
        "note_delete": note_delete_func,
        "contract_add": contract_add_func,
        "contract_update": contract_update_func,
        "contract_list": contract_list_func,
        "contract_link": contract_link_func,
        "contract_unlink": contract_unlink_func,
        "task_roadmap": task_roadmap_func,
        "task_roadmap_diff": task_roadmap_diff_func,
        "task_find_for_impact": task_find_for_impact_func,
        "task_suggest_contracts": task_suggest_contracts_func,
        "task_check_rollup": task_check_rollup_func,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    raw = sys.stdin.read().strip()
    if not raw:
        print(json.dumps({"error": "empty stdin"}))
        return

    try:
        request = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(json.dumps({"error": f"invalid JSON: {exc}"}))
        return

    tool_name: str = request.get("tool", "")
    args: dict[str, Any] = request.get("args", {})

    registry = _build_registry()

    if tool_name not in registry:
        available = sorted(registry)
        print(json.dumps({"error": f"unknown tool '{tool_name}'", "available": available}))
        return

    args = _inject_repo_root(args)

    try:
        result = registry[tool_name](**args)
        print(json.dumps(result, default=str))
    except TypeError as exc:
        # Surface argument mismatch clearly
        print(json.dumps({"error": f"argument error calling '{tool_name}': {exc}"}))
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))


if __name__ == "__main__":
    main()
