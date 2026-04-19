"""Context-aware hints system for MCP tool responses.

Tracks session state (in-memory only) and generates intelligent
next-step suggestions after each tool call.  Hints are appended as
``_hints`` to new tool responses so that Claude Code can propose
follow-up actions without the user having to discover them.

Also provides ``annotate_response()`` which injects ``confidence``,
``reason``, and ``evidence`` fields into tool responses, mirroring
the hex-graph-mcp output contract.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any

from .response import prune_response

# ---- intent categories and their characteristic tool names ----

_INTENT_TOOLS: dict[str, set[str]] = {
    "reviewing": {
        "detect_changes",
        "get_review_context",
        "get_affected_flows",
        "get_impact_radius",
    },
    "debugging": {
        "query_graph",
        "get_flow",
        "semantic_search_nodes",
    },
    "refactoring": {
        "refactor",
        "find_dead_code",
        "suggest_refactorings",
    },
    "exploring": {
        "list_communities",
        "get_architecture_overview",
        "list_flows",
        "list_graph_stats",
    },
    "brainstorming": {
        "task_create",
        "task_update",
        "task_edit",
        "task_get",
        "task_list",
        "task_search",
        "task_move",
        "task_delete",
        "task_archive",
        "task_add_edge",
        "task_remove_edge",
        "task_get_edges",
        "task_get_dag",
        "task_topological_sort",
        "task_link_code",
        "task_unlink_code",
        "task_get_code_refs",
        "task_find_by_code_node",
        "task_suggest_code_links",
        "task_find_conflicts",
        "task_check_isolation",
        "task_blast_radius",
        "task_execution_order",
        "task_validate",
"task_export",
        "task_export",
        "note_add",
        "note_update",
        "note_list",
        "note_delete",
        "contract_add",
        "contract_update",
        "contract_list",
        "task_roadmap",
        "task_roadmap_diff",
        "task_find_for_impact",
        "task_suggest_contracts",
        "task_check_rollup",
        "contract_link",
        "contract_unlink",
    },
}

# ---- workflow adjacency: for each tool, which tools are useful next ----

_WORKFLOW: dict[str, list[dict[str, str]]] = {
    # --- new tools (hex-graph-mcp inspired) ---
    "analyze_edit_region": [
        {
            "tool": "query_graph_tool",
            "suggestion": "Inspect callers/callees of an overlapping symbol",
        },
        {
            "tool": "trace_dataflow_tool",
            "suggestion": "Follow data propagation from an edited symbol",
        },
        {
            "tool": "detect_changes_tool",
            "suggestion": "Get full risk-scored change analysis for the file",
        },
    ],
    "audit_workspace": [
        {
            "tool": "refactor_tool",
            "suggestion": "Preview rename or removal of dead code symbols",
        },
        {
            "tool": "query_graph_tool",
            "suggestion": "Inspect callers of a large/dead function before removing",
        },
        {
            "tool": "detect_changes_tool",
            "suggestion": "Check if recent changes introduced the quality issues",
        },
    ],
    "trace_dataflow": [
        {
            "tool": "query_graph_tool",
            "suggestion": "Inspect callers of a reachable node in the flow",
        },
        {
            "tool": "analyze_edit_region_tool",
            "suggestion": "Evaluate impact of editing a node in the data path",
        },
        {
            "tool": "semantic_search_nodes_tool",
            "suggestion": "Find other symbols related to the source or sink",
        },
    ],
    "export_scip": [
        {
            "tool": "import_scip_tool",
            "suggestion": "Re-import the exported graph into another repository",
        },
        {
            "tool": "list_graph_stats_tool",
            "suggestion": "Verify the graph state before or after export",
        },
    ],
    "import_scip": [
        {
            "tool": "list_graph_stats_tool",
            "suggestion": "Verify the graph was updated after import",
        },
        {
            "tool": "semantic_search_nodes_tool",
            "suggestion": "Search for symbols from the imported graph",
        },
    ],
    # --- Task DAG tools (brainstorming workflow) ---
    "task_create": [
        {"tool": "task_link_code", "suggestion": "Link the new task to code graph nodes"},
        {"tool": "note_add", "suggestion": "Add design decisions or open questions"},
        {"tool": "task_add_edge", "suggestion": "Define dependencies between tasks"},
    ],
    "task_update": [
        {"tool": "task_check_rollup", "suggestion": "Check if parent can now be closed/archived"},
        {"tool": "task_validate", "suggestion": "Check if the DAG is ready for handoff"},
        {"tool": "task_export", "suggestion": "Export full context for this task (include_analysis=True for isolation+conflicts)"},
    ],
    "task_archive": [
        {"tool": "task_check_rollup", "suggestion": "Check if parent can now be archived too"},
        {"tool": "task_roadmap", "suggestion": "Review updated progress after archiving"},
    ],
    "task_check_rollup": [
        {"tool": "task_update", "suggestion": "Close parent task if all subtasks are done"},
        {"tool": "task_archive", "suggestion": "Archive parent if all subtasks are archived"},
        {"tool": "task_roadmap", "suggestion": "Review overall progress snapshot"},
    ],
    "task_edit": [
        {"tool": "task_get", "suggestion": "Verify the edited field content"},
        {"tool": "task_validate", "suggestion": "Re-validate after spec changes"},
    ],
    "task_get": [
        {"tool": "task_get_code_refs", "suggestion": "See code nodes linked to this task"},
        {"tool": "note_list", "suggestion": "Browse decisions and open questions"},
        {"tool": "task_get_edges", "suggestion": "See dependencies and related tasks"},
        {"tool": "task_export", "suggestion": "Get full aggregated context (include_analysis=True)"},
    ],
    "task_list": [
        {"tool": "task_get", "suggestion": "Inspect a specific task in detail"},
        {"tool": "task_roadmap", "suggestion": "See overall progress and attention items"},
    ],
    "task_search": [
        {"tool": "task_get", "suggestion": "Inspect a matched task"},
        {"tool": "task_find_by_code_node", "suggestion": "Find tasks by code node instead"},
    ],
    "task_delete": [
        {"tool": "task_list", "suggestion": "Verify remaining task structure"},
        {"tool": "task_validate", "suggestion": "Check DAG integrity after deletion"},
    ],
    "task_move": [
        {"tool": "task_get_dag", "suggestion": "Visualise the updated DAG"},
        {"tool": "task_validate", "suggestion": "Check DAG integrity after move"},
    ],
    "task_archive": [
        {"tool": "task_create", "suggestion": "Create a replacement task if needed"},
        {"tool": "task_roadmap", "suggestion": "See updated progress after archival"},
    ],
    "task_add_edge": [
        {"tool": "task_execution_order", "suggestion": "See parallelism-aware execution levels"},
        {"tool": "task_topological_sort", "suggestion": "Get dependency-ordered task list"},
        {"tool": "task_validate", "suggestion": "Check for cycles or blocked tasks"},
    ],
    "task_remove_edge": [
        {"tool": "task_get_edges", "suggestion": "Verify remaining edges"},
        {"tool": "task_execution_order", "suggestion": "Re-check execution grouping"},
    ],
    "task_get_edges": [
        {"tool": "task_add_edge", "suggestion": "Add a missing dependency"},
        {"tool": "task_get_dag", "suggestion": "See the full DAG structure"},
    ],
    "task_get_dag": [
        {"tool": "task_validate", "suggestion": "Run validation on the DAG"},
        {"tool": "task_find_conflicts", "suggestion": "Check for code-level conflicts"},
        {"tool": "task_suggest_contracts", "suggestion": "Find missing interface contracts"},
    ],
    "task_topological_sort": [
        {"tool": "task_execution_order", "suggestion": "See parallel execution groups"},
        {"tool": "task_export", "suggestion": "Export context for the first task"},
    ],
    "task_link_code": [
        {"tool": "task_check_isolation", "suggestion": "Check isolation score after linking"},
        {"tool": "task_find_conflicts", "suggestion": "Check for new conflicts"},
        {"tool": "task_suggest_code_links", "suggestion": "Find more code to link"},
    ],
    "task_unlink_code": [
        {"tool": "task_get_code_refs", "suggestion": "Verify remaining code refs"},
        {"tool": "task_check_isolation", "suggestion": "Re-check isolation score"},
    ],
    "task_get_code_refs": [
        {"tool": "query_graph_tool", "suggestion": "Inspect callers/callees of linked nodes"},
        {"tool": "task_check_isolation", "suggestion": "Check isolation of this task"},
    ],
    "task_find_by_code_node": [
        {"tool": "task_get", "suggestion": "Inspect the matched task"},
        {"tool": "task_find_conflicts", "suggestion": "Check if these tasks conflict"},
    ],
    "task_suggest_code_links": [
        {"tool": "task_link_code", "suggestion": "Link a suggested code node"},
        {"tool": "semantic_search_nodes_tool", "suggestion": "Search for more code symbols"},
    ],
    "task_find_conflicts": [
        {"tool": "contract_add", "suggestion": "Add a contract between conflicting tasks"},
        {"tool": "task_suggest_contracts", "suggestion": "Get full contract suggestions"},
        {"tool": "task_add_edge", "suggestion": "Add depends_on to serialise conflicting tasks"},
    ],
    "task_check_isolation": [
        {"tool": "task_blast_radius", "suggestion": "See full blast radius of this task"},
        {"tool": "task_link_code", "suggestion": "Link missing external dependencies"},
    ],
    "task_blast_radius": [
        {"tool": "task_find_for_impact", "suggestion": "Find tasks for impacted files"},
        {"tool": "task_find_conflicts", "suggestion": "Check conflicts with sibling tasks"},
    ],
    "task_execution_order": [
        {"tool": "task_export", "suggestion": "Export context for a ready task"},
        {"tool": "task_validate", "suggestion": "Validate before starting execution"},
    ],
    "task_validate": [
        {"tool": "task_export", "suggestion": "Export context for a validated task (include_analysis=True)"},
        {"tool": "task_roadmap", "suggestion": "See overall progress and next steps"},
    ],
    "task_export": [
        {"tool": "task_update", "suggestion": "Update status to in_progress after handoff"},
        {"tool": "task_roadmap", "suggestion": "Check overall brainstorm progress"},
    ],
    "note_add": [
        {"tool": "note_list", "suggestion": "Browse all notes for context"},
        {"tool": "task_validate", "suggestion": "Check if open questions block validation"},
    ],
    "note_update": [
        {"tool": "note_list", "suggestion": "Verify note was updated correctly"},
        {"tool": "task_validate", "suggestion": "Re-validate after resolving decisions"},
    ],
    "note_list": [
        {"tool": "note_add", "suggestion": "Add a new decision or question"},
        {"tool": "note_update", "suggestion": "Resolve an open question"},
    ],
    "note_delete": [
        {"tool": "note_list", "suggestion": "Verify remaining notes"},
    ],
    "contract_add": [
        {"tool": "contract_link", "suggestion": "Attach tasks to this contract as participants"},
        {"tool": "contract_list", "suggestion": "Verify the contract was recorded in the scope"},
        {"tool": "task_suggest_contracts", "suggestion": "Find other implicit code deps needing contracts"},
    ],
    "contract_link": [
        {"tool": "contract_list", "suggestion": "Verify all participants are correctly linked"},
        {"tool": "task_validate", "suggestion": "Re-validate after adding a new participant"},
    ],
    "contract_unlink": [
        {"tool": "contract_list", "suggestion": "Verify remaining participants after unlinking"},
        {"tool": "task_validate", "suggestion": "Re-validate — orphaned contracts may need reassignment"},
    ],
    "contract_update": [
        {"tool": "task_validate", "suggestion": "Re-validate the DAG after agreeing a contract"},
        {"tool": "task_roadmap", "suggestion": "Check overall contract status in roadmap"},
    ],
    "contract_list": [
        {"tool": "contract_add", "suggestion": "Add a missing contract/design entity"},
        {"tool": "contract_link", "suggestion": "Link an existing task to an orphan contract"},
        {"tool": "task_suggest_contracts", "suggestion": "Find implicit code deps needing contracts"},
        {"tool": "task_validate", "suggestion": "Validate DAG — check for unacknowledged contracts"},
    ],
    "contract_update": [
        {"tool": "contract_list", "suggestion": "Verify updated contract status"},
        {"tool": "task_validate", "suggestion": "Re-validate after contract change"},
    ],
    "contract_list": [
        {"tool": "contract_add", "suggestion": "Add a new contract"},
        {"tool": "task_suggest_contracts", "suggestion": "Find missing contracts"},
    ],
    "task_roadmap": [
        {"tool": "task_export", "suggestion": "Export full context for a ready task (include_analysis=True)"},
        {"tool": "task_validate", "suggestion": "Validate tasks in the attention list"},
        {"tool": "task_find_conflicts", "suggestion": "Check for code conflicts"},
    ],
    "task_roadmap_diff": [
        {"tool": "task_roadmap", "suggestion": "See full roadmap snapshot"},
        {"tool": "task_validate", "suggestion": "Validate if new changes are clean"},
    ],
    "task_find_for_impact": [
        {"tool": "task_get", "suggestion": "Inspect an impacted task"},
        {"tool": "task_blast_radius", "suggestion": "See blast radius of a specific task"},
        {"tool": "detect_changes_tool", "suggestion": "Get risk-scored change analysis"},
    ],
    "task_suggest_contracts": [
        {"tool": "contract_add", "suggestion": "Create a contract for the top suggestion"},
        {"tool": "task_find_conflicts", "suggestion": "Also check for direct code conflicts"},
    ],
    # --- query / search / stats tools ---
    "query_graph": [
        {
            "tool": "trace_dataflow_tool",
            "suggestion": "Trace call-graph reachability from a symbol in the results",
        },
        {
            "tool": "analyze_edit_region_tool",
            "suggestion": "Evaluate impact before editing a matched symbol",
        },
        {
            "tool": "get_flow_tool",
            "suggestion": "See the execution flow a matched symbol belongs to",
        },
    ],
    "get_impact_radius": [
        {
            "tool": "detect_changes_tool",
            "suggestion": "Get full risk-scored analysis of the same changes",
        },
        {
            "tool": "get_affected_flows_tool",
            "suggestion": "See which execution flows are impacted",
        },
        {
            "tool": "analyze_edit_region_tool",
            "suggestion": "Drill into a specific line range inside an impacted file",
        },
    ],
    "list_graph_stats": [
        {
            "tool": "list_communities_tool",
            "suggestion": "Explore how code is grouped into communities",
        },
        {
            "tool": "list_flows_tool",
            "suggestion": "Browse execution flows across the codebase",
        },
        {
            "tool": "audit_workspace_tool",
            "suggestion": "Run a full quality audit (dead code, cycles, large functions)",
        },
    ],
    "find_files_by_pattern": [
        {
            "tool": "query_graph_tool",
            "suggestion": "Inspect callers/callees of a symbol in a matched file",
        },
        {
            "tool": "analyze_edit_region_tool",
            "suggestion": "Evaluate impact before editing lines in a matched file",
        },
        {
            "tool": "semantic_search_nodes_tool",
            "suggestion": "Search for specific symbols inside matched files",
        },
    ],
    "find_large_functions": [
        {
            "tool": "audit_workspace_tool",
            "suggestion": "Run full workspace audit including dead code and cycles",
        },
        {
            "tool": "query_graph_tool",
            "suggestion": "Check callers of a large function before splitting it",
        },
        {
            "tool": "refactor_tool",
            "suggestion": "Preview rename or decomposition of a large function",
        },
    ],
    # --- existing tools ---
    "list_flows": [
        {
            "tool": "get_flow",
            "suggestion": "Drill into a specific flow for step-by-step details",
        },
        {
            "tool": "get_affected_flows",
            "suggestion": "Check which flows are affected by recent changes",
        },
        {
            "tool": "get_architecture_overview",
            "suggestion": "See the high-level architecture",
        },
    ],
    "get_flow": [
        {
            "tool": "query_graph",
            "suggestion": "Inspect callers/callees of a step in this flow",
        },
        {
            "tool": "get_affected_flows",
            "suggestion": "Check if changes affect this flow",
        },
        {
            "tool": "list_flows",
            "suggestion": "Browse other execution flows",
        },
    ],
    "get_affected_flows": [
        {
            "tool": "detect_changes",
            "suggestion": "Get risk-scored change analysis",
        },
        {
            "tool": "get_flow",
            "suggestion": "Inspect a specific affected flow",
        },
        {
            "tool": "get_review_context",
            "suggestion": "Build a full review context for the changes",
        },
    ],
    "list_communities": [
        {
            "tool": "get_community",
            "suggestion": "Inspect a specific community's members",
        },
        {
            "tool": "get_architecture_overview",
            "suggestion": "See cross-community coupling and warnings",
        },
        {
            "tool": "list_flows",
            "suggestion": "See execution flows across communities",
        },
    ],
    "get_community": [
        {
            "tool": "query_graph",
            "suggestion": "Explore callers/callees of community members",
        },
        {
            "tool": "list_communities",
            "suggestion": "Browse other communities",
        },
        {
            "tool": "get_architecture_overview",
            "suggestion": "See how this community fits the architecture",
        },
    ],
    "get_architecture_overview": [
        {
            "tool": "list_communities",
            "suggestion": "Drill into individual communities",
        },
        {
            "tool": "detect_changes",
            "suggestion": "See how recent changes affect the architecture",
        },
        {
            "tool": "list_flows",
            "suggestion": "Explore execution flows",
        },
    ],
    "detect_changes": [
        {
            "tool": "get_review_context",
            "suggestion": "Build a full review context with source snippets",
        },
        {
            "tool": "get_affected_flows",
            "suggestion": "See which execution flows are affected",
        },
        {
            "tool": "get_impact_radius",
            "suggestion": "Expand the blast radius analysis",
        },
        {
            "tool": "refactor",
            "suggestion": "Look for refactoring opportunities in changed code",
        },
    ],
    "refactor": [
        {
            "tool": "query_graph",
            "suggestion": "Verify call sites before applying a rename",
        },
        {
            "tool": "detect_changes",
            "suggestion": "Check risk of the refactored code",
        },
        {
            "tool": "semantic_search_nodes",
            "suggestion": "Find related symbols to also rename",
        },
    ],
    "semantic_search_nodes": [
        {
            "tool": "query_graph",
            "suggestion": "Inspect callers/callees of a search result",
        },
        {
            "tool": "get_flow",
            "suggestion": "See the execution flow through a matched node",
        },
        {
            "tool": "get_impact_radius",
            "suggestion": "Check the blast radius from matched nodes",
        },
    ],
}

# ---------------------------------------------------------------------------
# confidence / reason / evidence per tool  (hex-graph-mcp contract)
# ---------------------------------------------------------------------------

# confidence: how reliable is the result
#   "exact"     — deterministic graph lookup, no heuristics
#   "heuristic" — rule-based inference (dead code, cycles, large functions)
#   "inferred"  — BFS traversal, result depends on graph completeness
#   "partial"   — some data missing (e.g. no embeddings, truncated results)
_TOOL_META: dict[str, dict[str, str]] = {
    "build_or_update_graph": {
        "confidence": "exact",
        "reason": "graph_index_built",
    },
    "get_impact_radius": {
        "confidence": "inferred",
        "reason": "bfs_impact_radius",
    },
    "query_graph": {
        "confidence": "exact",
        "reason": "graph_edge_query",
    },
    "get_review_context": {
        "confidence": "inferred",
        "reason": "review_context_assembled",
    },
    "semantic_search_nodes": {
        "confidence": "inferred",
        "reason": "hybrid_fts_vector_search",
    },
    "list_graph_stats": {
        "confidence": "exact",
        "reason": "graph_aggregate_stats",
    },
    "find_files_by_pattern": {
        "confidence": "exact",
        "reason": "glob_file_match",
    },
    "find_large_functions": {
        "confidence": "exact",
        "reason": "line_count_threshold_query",
    },
    "list_flows": {
        "confidence": "inferred",
        "reason": "execution_flow_detection",
    },
    "get_flow": {
        "confidence": "inferred",
        "reason": "execution_flow_detail",
    },
    "get_affected_flows": {
        "confidence": "inferred",
        "reason": "flow_change_intersection",
    },
    "list_communities": {
        "confidence": "heuristic",
        "reason": "leiden_community_detection",
    },
    "get_community": {
        "confidence": "heuristic",
        "reason": "community_detail",
    },
    "get_architecture_overview": {
        "confidence": "heuristic",
        "reason": "community_coupling_analysis",
    },
    "detect_changes": {
        "confidence": "inferred",
        "reason": "risk_scored_change_analysis",
    },
    "analyze_edit_region": {
        "confidence": "exact",
        "reason": "edit_region_semantic_impact",
    },
    "audit_workspace": {
        "confidence": "heuristic",
        "reason": "workspace_maintenance_audit",
    },
    "trace_dataflow": {
        "confidence": "inferred",
        "reason": "call_graph_bfs_reachability",
    },
    "refactor": {
        "confidence": "heuristic",
        "reason": "refactor_analysis",
    },
    "export_scip": {
        "confidence": "exact",
        "reason": "scip_graph_export",
    },
    "import_scip": {
        "confidence": "exact",
        "reason": "scip_graph_import",
    },
    # --- Task DAG tools ---
    "task_create": {"confidence": "exact", "reason": "task_crud_insert"},
    "task_update": {"confidence": "exact", "reason": "task_crud_update"},
    "task_edit": {"confidence": "exact", "reason": "task_field_patch"},
    "task_get": {"confidence": "exact", "reason": "task_crud_read"},
    "task_list": {"confidence": "exact", "reason": "task_crud_list"},
    "task_search": {"confidence": "inferred", "reason": "task_fts_search"},
    "task_delete": {"confidence": "exact", "reason": "task_crud_delete"},
    "task_move": {"confidence": "exact", "reason": "task_hierarchy_move"},
    "task_archive": {"confidence": "exact", "reason": "task_cascade_archive"},
    "task_add_edge": {"confidence": "exact", "reason": "task_dag_edge_insert"},
    "task_remove_edge": {"confidence": "exact", "reason": "task_dag_edge_delete"},
    "task_get_edges": {"confidence": "exact", "reason": "task_dag_edge_query"},
    "task_get_dag": {"confidence": "exact", "reason": "task_dag_full_query"},
    "task_topological_sort": {"confidence": "exact", "reason": "task_kahn_sort"},
    "task_link_code": {"confidence": "exact", "reason": "task_code_ref_insert"},
    "task_unlink_code": {"confidence": "exact", "reason": "task_code_ref_delete"},
    "task_get_code_refs": {"confidence": "exact", "reason": "task_code_ref_query"},
    "task_find_by_code_node": {"confidence": "exact", "reason": "task_code_reverse_lookup"},
    "task_suggest_code_links": {"confidence": "heuristic", "reason": "task_keyword_code_search"},
    "task_find_conflicts": {"confidence": "inferred", "reason": "task_code_intersection"},
    "task_check_isolation": {"confidence": "inferred", "reason": "task_bfs_isolation_score"},
    "task_blast_radius": {"confidence": "inferred", "reason": "task_bfs_blast_radius"},
    "task_execution_order": {"confidence": "exact", "reason": "task_dag_parallel_levels"},
    "task_validate": {"confidence": "heuristic", "reason": "task_dag_gate_check"},

    "task_export": {"confidence": "exact", "reason": "task_flat_export"},
    "note_add": {"confidence": "exact", "reason": "note_crud_insert"},
    "note_update": {"confidence": "exact", "reason": "note_crud_update"},
    "note_list": {"confidence": "exact", "reason": "note_crud_list"},
    "note_delete": {"confidence": "exact", "reason": "note_crud_delete"},
    "contract_add": {"confidence": "exact", "reason": "contract_crud_insert"},
    "contract_update": {"confidence": "exact", "reason": "contract_crud_update"},
    "contract_list": {"confidence": "exact", "reason": "contract_crud_list"},
    "task_roadmap": {"confidence": "inferred", "reason": "task_progress_aggregation"},
    "task_roadmap_diff": {"confidence": "inferred", "reason": "task_temporal_diff"},
    "task_find_for_impact": {"confidence": "inferred", "reason": "task_impact_cross_query"},
    "task_suggest_contracts": {"confidence": "heuristic", "reason": "task_implicit_dep_detection"},
    "task_check_rollup": {"confidence": "exact", "reason": "task_parent_status_rollup"},
    "contract_link": {"confidence": "exact", "reason": "contract_participant_link"},
    "contract_unlink": {"confidence": "exact", "reason": "contract_participant_unlink"},
}


# Default evidence extractor: pull meaningful scalar counts from the result
def _extract_evidence(tool_name: str, result: dict) -> dict[str, Any]:
    """Pull key scalar metrics from a result dict as evidence."""
    ev: dict[str, Any] = {}
    for key in (
        "total_nodes",
        "total_edges",
        "total_impacted",
        "total_found",
        "reachable_count",
        "issues_count",
        "health_score",
        "nodes_upserted",
        "edges_upserted",
    ):
        val = result.get(key)
        if isinstance(val, (int, float)):
            ev[key] = val
    # list-length evidence
    for key in (
        "results",
        "changed_nodes",
        "impacted_nodes",
        "reachable",
        "overlapping_symbols",
        "external_callers",
        "downstream_calls",
        "dead_code",
        "large_functions",
        "cycles",
        "flows",
        "communities",
        # Task DAG evidence
        "tasks",
        "edges",
        "nodes",
        "notes",
        "contracts",
        "conflicts",
        "suggestions",
        "levels",
        "errors",
        "warnings",
        "ok",
        "uncovered_nodes",
        "archived_ids",
        "matched_nodes",
    ):
        val = result.get(key)
        if isinstance(val, list):
            ev[f"{key}_count"] = len(val)
    return ev


def annotate_response(
    tool_name: str,
    result: dict,
) -> dict:
    """Inject ``confidence``, ``reason``, and ``evidence`` into a tool result.

    Mutates *result* in-place and returns it.  Fields are only set if not
    already present, so tool functions can override them explicitly.
    """
    meta = _TOOL_META.get(tool_name, {})
    if "confidence" not in result and "confidence" in meta:
        result["confidence"] = meta["confidence"]
    if "reason" not in result and "reason" in meta:
        result["reason"] = meta["reason"]
    if "evidence" not in result:
        ev = _extract_evidence(tool_name, result)
        if ev:
            result["evidence"] = ev
    return result


# Maximum items per hints category returned to the caller.
_MAX_PER_CATEGORY = 3

# Session history caps.
_MAX_TOOLS_HISTORY = 100
_MAX_NODES_TRACKED = 1000


# ---------------------------------------------------------------------------
# SessionState
# ---------------------------------------------------------------------------


class SessionState:
    """In-memory session state for a single MCP connection."""

    def __init__(self) -> None:
        self.tools_called: deque[str] = deque(maxlen=_MAX_TOOLS_HISTORY)
        self.nodes_queried: set[str] = set()
        self.files_touched: set[str] = set()
        self.inferred_intent: str | None = None
        self.last_tool_time: float = 0.0

    def record_tool_call(self, tool_name: str) -> None:
        """Record a tool invocation (FIFO, capped at 100)."""
        self.tools_called.append(tool_name)
        self.last_tool_time = time.time()

    def record_nodes(self, node_ids: list[str]) -> None:
        """Record queried node identifiers (capped at 1000)."""
        for nid in node_ids:
            if len(self.nodes_queried) >= _MAX_NODES_TRACKED:
                break
            self.nodes_queried.add(nid)

    def record_files(self, files: list[str]) -> None:
        """Record touched file paths."""
        self.files_touched.update(files)


# ---------------------------------------------------------------------------
# Intent inference
# ---------------------------------------------------------------------------


def infer_intent(session: SessionState) -> str:
    """Classify the user's likely intent from their tool-call history.

    Returns one of: ``"reviewing"``, ``"debugging"``, ``"refactoring"``,
    ``"exploring"`` (default).
    """
    if not session.tools_called:
        return "exploring"

    # Score each intent by how many of the last N calls match its tools.
    recent = list(session.tools_called)[-10:]
    scores: dict[str, int] = {intent: 0 for intent in _INTENT_TOOLS}
    for tool in recent:
        for intent, tools in _INTENT_TOOLS.items():
            if tool in tools:
                scores[intent] += 1

    best = max(scores, key=lambda k: scores[k])
    if scores[best] == 0:
        return "exploring"
    return best


# ---------------------------------------------------------------------------
# Hints generation
# ---------------------------------------------------------------------------


def generate_hints(
    tool_name: str,
    result: dict[str, Any],
    session: SessionState,
) -> dict[str, Any]:
    """Build context-aware hints for a tool response.

    Mutates *result* inline to inject:
    - ``next_actions``: flat list of suggested follow-up tool names
    - ``warnings``: extracted warning strings
    - ``confidence``, ``reason``, ``evidence``: hex-graph-mcp output contract

    Also returns a hints object for backward compatibility.
    """
    # Update session state.
    session.record_tool_call(tool_name)
    session.inferred_intent = infer_intent(session)

    next_steps = _build_next_steps(tool_name, session)
    warnings = _extract_warnings(result)
    # Build related BEFORE tracking, so that the current result's files
    # are not yet in files_touched and can appear as suggestions.
    related = _build_related(tool_name, result, session)

    # Collect files/nodes from result for session tracking.
    _track_result(result, session)

    # Inject next_actions as flat list (hex-graph-mcp style)
    flat_actions = [step["tool"] for step in next_steps[:_MAX_PER_CATEGORY]]
    if flat_actions and "next_actions" not in result:
        result["next_actions"] = flat_actions

    if warnings and "warnings" not in result:
        result["warnings"] = warnings[:_MAX_PER_CATEGORY]

    # Inject confidence / reason / evidence
    annotate_response(tool_name, result)

    # Prune None/empty fields in-place (hex-graph-mcp pruneEmpty pattern).
    # This mutates `result` so callers get a clean dict without None noise.
    pruned = prune_response(result)
    result.clear()
    result.update(pruned)

    return {
        "next_steps": next_steps[:_MAX_PER_CATEGORY],
        "related": related[:_MAX_PER_CATEGORY],
        "warnings": warnings[:_MAX_PER_CATEGORY],
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _track_result(result: dict[str, Any], session: SessionState) -> None:
    """Extract node IDs and file paths from a tool result and record them."""
    # Files
    for key in ("changed_files", "impacted_files"):
        files = result.get(key)
        if isinstance(files, list):
            session.record_files([f for f in files if isinstance(f, str)])

    # Nodes — look in common result shapes
    node_ids: list[str] = []
    for key in ("results", "changed_nodes", "impacted_nodes"):
        items = result.get(key)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    qn = item.get("qualified_name")
                    if qn:
                        node_ids.append(qn)
    if node_ids:
        session.record_nodes(node_ids)


def _build_next_steps(tool_name: str, session: SessionState) -> list[dict[str, str]]:
    """Return next-step suggestions, filtering already-called tools."""
    called = set(session.tools_called)
    candidates = _WORKFLOW.get(tool_name, [])
    out: list[dict[str, str]] = []
    for c in candidates:
        if c["tool"] not in called:
            out.append(c)
    return out


def _extract_warnings(result: dict[str, Any]) -> list[str]:
    """Pull warning signals from a tool result."""
    warnings: list[str] = []

    # Test gaps
    test_gaps = result.get("test_gaps")
    if isinstance(test_gaps, list) and test_gaps:
        names = [g.get("name", g) if isinstance(g, dict) else str(g) for g in test_gaps[:5]]
        warnings.append(f"Test coverage gaps: {', '.join(names)}")

    # High risk score
    risk = result.get("risk_score")
    if isinstance(risk, (int, float)) and risk > 0.7:
        warnings.append(f"High risk score ({risk:.2f}) — review carefully")

    # Coupling warnings from architecture overview
    arch_warnings = result.get("warnings")
    if isinstance(arch_warnings, list):
        for w in arch_warnings[:3]:
            if isinstance(w, str):
                warnings.append(w)
            elif isinstance(w, dict) and "message" in w:
                warnings.append(w["message"])

    return warnings


def _build_related(
    tool_name: str,
    result: dict[str, Any],
    session: SessionState,
) -> list[str]:
    """Suggest related node/file identifiers from the result."""
    related: list[str] = []
    seen: set[str] = set()

    # Suggest impacted files the user hasn't touched yet
    impacted = result.get("impacted_files")
    if isinstance(impacted, list):
        for f in impacted:
            if isinstance(f, str) and f not in session.files_touched and f not in seen:
                related.append(f)
                seen.add(f)
                if len(related) >= _MAX_PER_CATEGORY:
                    break

    return related


# ---------------------------------------------------------------------------
# Module-level session singleton
# ---------------------------------------------------------------------------

_session = SessionState()


def get_session() -> SessionState:
    """Return the global in-memory session state."""
    return _session


def reset_session() -> None:
    """Reset the global session (useful for testing)."""
    global _session
    _session = SessionState()
