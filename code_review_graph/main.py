"""MCP server entry point for Code Review Graph.

Run as: code-review-graph serve
Communicates via stdio (standard MCP transport).
"""

from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP

from .prompts import (
    architecture_map_prompt,
    debug_issue_prompt,
    onboard_developer_prompt,
    pre_merge_check_prompt,
    review_changes_prompt,
)
from .tools import (
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
    semantic_search_nodes,
    trace_dataflow,
)
from .tools.task_tools import (
    contract_add_func,
    contract_link_func,
    contract_unlink_func,
    contract_list_func,
    contract_update_func,
    note_add_func,
    note_delete_func,
    note_list_func,
    note_update_func,
    task_add_edge_func,
    task_archive_func,
    task_blast_radius_func,
    task_check_isolation_func,
    task_create_func,
    task_get_active_root_func,
    task_delete_func,
    task_edit_func,
    task_execution_order_func,
    task_export_func,
    task_find_by_code_node_func,
    task_find_conflicts_func,
    task_get_dag_func,
    task_get_func,
    task_get_code_refs_func,
    task_link_code_func,
    task_list_func,
    task_move_func,
    task_remove_edge_func,
    task_roadmap_diff_func,
    task_roadmap_func,
    task_search_func,
    task_suggest_code_links_func,
    task_topological_sort_func,
    task_unlink_code_func,
    task_update_func,
    task_validate_func,
    task_find_for_impact_func,
    task_suggest_contracts_func,
    task_check_rollup_func,
)

# NOTE: Thread-safe for stdio MCP (single-threaded). If adding HTTP/SSE
# transport with concurrent requests, replace with contextvars.ContextVar.
_default_repo_root: str | None = None

mcp = FastMCP(
    "code-review-graph",
    instructions=(
        "Persistent incremental knowledge graph for token-efficient, "
        "context-aware code reviews. Parses your codebase with Tree-sitter, "
        "builds a structural graph, and provides smart impact analysis."
    ),
)


@mcp.tool()
def build_or_update_graph_tool(
    full_rebuild: bool = False,
    repo_root: Optional[str] = None,
    base: str = "HEAD~1",
) -> dict:
    """Build or incrementally update the code knowledge graph.

    Call this first to initialize the graph, or after making changes.
    By default performs an incremental update (only changed files).
    Set full_rebuild=True to re-parse every file.

    Args:
        full_rebuild: If True, re-parse all files. Default: False (incremental).
        repo_root: Repository root path. Auto-detected from current directory if omitted.
        base: Git ref to diff against for incremental updates. Default: HEAD~1.
    """
    return build_or_update_graph(full_rebuild=full_rebuild, repo_root=repo_root, base=base)


@mcp.tool()
def get_impact_radius_tool(
    changed_files: Optional[list[str]] = None,
    max_depth: int = 2,
    repo_root: Optional[str] = None,
    base: str = "HEAD~1",
) -> dict:
    """Analyze the blast radius of changed files in the codebase.

    Shows which functions, classes, and files are impacted by changes.
    Auto-detects changed files from git if not specified.

    Args:
        changed_files: List of changed file paths (relative to repo root). Auto-detected if omitted.
        max_depth: Number of hops to traverse in the dependency graph. Default: 2.
        repo_root: Repository root path. Auto-detected if omitted.
        base: Git ref for auto-detecting changes. Default: HEAD~1.
    """
    return get_impact_radius(
        changed_files=changed_files,
        max_depth=max_depth,
        repo_root=repo_root,
        base=base,
    )


@mcp.tool()
def query_graph_tool(
    pattern: str,
    target: str,
    repo_root: Optional[str] = None,
) -> dict:
    """Run a predefined graph query to explore code relationships.

    Available patterns:
    - callers_of: Find functions that call the target
    - callees_of: Find functions called by the target. Results include internal callees (in graph) plus an `_external_callees` list for stdlib/builtin calls not indexed in the graph.
    - imports_of: Find what the target imports
    - importers_of: Find files that import the target
    - children_of: Find nodes contained in a file or class
    - tests_for: Find tests for the target
    - inheritors_of: Find classes inheriting from the target
    - file_summary: Get all nodes in a file

    Args:
        pattern: Query pattern name (see above).
        target: Node name, qualified name, or file path to query.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    import os
    from pathlib import Path

    # Try to correctly find the graph.db repo base when triggered via MCP
    repo_root = repo_root or os.environ.get("CRG_REPO_ROOT") or str(Path.cwd())
    if not (Path(repo_root) / ".code-review-graph").exists():
        # Fallback hack for MCP servers running from user's appdata
        # Search parent directories from current module file, or hardcode your target project
        pass

    return query_graph(pattern=pattern, target=target, repo_root=repo_root)


@mcp.tool()
def get_review_context_tool(
    changed_files: Optional[list[str]] = None,
    max_depth: int = 2,
    include_source: bool = True,
    max_lines_per_file: int = 200,
    repo_root: Optional[str] = None,
    base: str = "HEAD~1",
) -> dict:
    """Generate a focused, token-efficient review context for code changes.

    Combines impact analysis with source snippets and review guidance.
    Use this for comprehensive code reviews.

    Args:
        changed_files: Files to review. Auto-detected from git diff if omitted.
        max_depth: Impact radius depth. Default: 2.
        include_source: Include source code snippets. Default: True.
        max_lines_per_file: Max source lines per file. Default: 200.
        repo_root: Repository root path. Auto-detected if omitted.
        base: Git ref for change detection. Default: HEAD~1.
    """
    return get_review_context(
        changed_files=changed_files,
        max_depth=max_depth,
        include_source=include_source,
        max_lines_per_file=max_lines_per_file,
        repo_root=repo_root,
        base=base,
    )


@mcp.tool()
def semantic_search_nodes_tool(
    query: str,
    kind: Optional[str] = None,
    limit: int = 20,
    repo_root: Optional[str] = None,
    model: Optional[str] = None,
    names: Optional[list] = None,
    file_path: Optional[str] = None,
) -> dict:
    """Search for code entities by name, keyword, or semantic similarity.

    Uses vector embeddings for semantic search when available (run embed_graph_tool
    first, requires sentence-transformers). Falls back to keyword matching otherwise.

    Args:
        query: Search string. Multi-word queries are automatically converted
               to FTS5 OR expressions — "create_task move_task add_note" finds
               all three in one round-trip with correct BM25 ranking.
        kind: Optional filter: File, Class, Function, Type, or Test.
        limit: Maximum results. Default: 20.
        repo_root: Repository root path. Auto-detected if omitted.
        model: Embedding model for query vectors. Must match the model used
               during embed_graph. Falls back to CRG_EMBEDDING_MODEL env var,
               then all-MiniLM-L6-v2.
        names: List of symbol names for bulk multi-symbol lookup. Equivalent
               to adding them space-separated to query.
               Example: names=["create_task", "move_task", "add_note"]
        file_path: Optional file path filter (substring). Only nodes whose
                   file_path contains this string are returned.
                   Example: "tasks.py" or "code_review_graph/tasks.py"
    """
    return semantic_search_nodes(
        query=query, kind=kind, limit=limit, repo_root=repo_root, model=model,
        names=names, file_path=file_path,
    )


@mcp.tool()
def embed_graph_tool(
    repo_root: Optional[str] = None,
    model: Optional[str] = None,
) -> dict:
    """Compute vector embeddings for all graph nodes to enable semantic search.

    Requires: pip install code-review-graph[embeddings]
    Default model: all-MiniLM-L6-v2. Override via `model` param or
    CRG_EMBEDDING_MODEL env var (any sentence-transformers compatible model).
    Changing the model re-embeds all nodes automatically.

    After running this, semantic_search_nodes_tool will use vector similarity
    instead of keyword matching for much better results.

    Args:
        repo_root: Repository root path. Auto-detected if omitted.
        model: Embedding model name (HuggingFace ID or local path).
               Falls back to CRG_EMBEDDING_MODEL env var, then all-MiniLM-L6-v2.
    """
    return embed_graph(repo_root=repo_root, model=model)


@mcp.tool()
def list_graph_stats_tool(
    repo_root: Optional[str] = None,
) -> dict:
    """Get aggregate statistics about the code knowledge graph.

    Shows total nodes, edges, languages, files, and last update time.
    Useful for checking if the graph is built and up to date.

    Args:
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return list_graph_stats(repo_root=repo_root)


@mcp.tool()
def get_docs_section_tool(
    section_name: str,
) -> dict:
    """Get a specific section from the LLM-optimized documentation reference.

    Returns only the requested section content for minimal token usage.
    Use this before answering any user question about the plugin.

    Available sections: usage, review-delta, review-pr, commands, legal,
    watch, embeddings, languages, troubleshooting.

    Args:
        section_name: The section to retrieve (e.g. "review-delta", "usage").
    """
    return get_docs_section(section_name=section_name, repo_root=_default_repo_root)


@mcp.tool()
def find_files_by_pattern_tool(
    patterns: list[str],
    repo_root: Optional[str] = None,
    limit: int = 50,
) -> dict:
    """Find files matching path patterns and retrieve their top-level nodes.

    Useful for discovering codebase structure without reading entire files.
    Allows agents to see both WHERE the file is, and WHAT it contains conceptually.

    Args:
        patterns: List of glob patterns (e.g. ["*router*", "main.*", "src/**/*.ts"])
        repo_root: Repository root path. Auto-detected if omitted.
        limit: Maximum number of files to return (default: 50).
    """
    return find_files_by_pattern(patterns=patterns, repo_root=repo_root, limit=limit)


@mcp.tool()
def find_large_functions_tool(
    min_lines: int = 50,
    kind: Optional[str] = None,
    file_path_pattern: Optional[str] = None,
    limit: int = 50,
    repo_root: Optional[str] = None,
) -> dict:
    """Find functions, classes, or files exceeding a line-count threshold.

    Useful for decomposition audits, code quality checks, and enforcing
    size limits during code review. Results are ordered by line count.

    Args:
        min_lines: Minimum line count to flag. Default: 50.
        kind: Optional filter: Function, Class, File, or Test.
        file_path_pattern: Filter by file path substring (e.g. "components/").
        limit: Maximum results. Default: 50.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return find_large_functions(
        min_lines=min_lines,
        kind=kind,
        file_path_pattern=file_path_pattern,
        limit=limit,
        repo_root=repo_root,
    )


@mcp.tool()
def list_flows_tool(
    sort_by: str = "criticality",
    limit: int = 50,
    kind: Optional[str] = None,
    is_test: Optional[bool] = None,
    repo_root: Optional[str] = None,
) -> dict:
    """List execution flows in the codebase, sorted by criticality.

    Each flow represents a call chain starting from an entry point
    (HTTP handler, CLI command, test function, etc.). Use this to
    understand the main execution paths through the codebase.

    Args:
        sort_by: Sort column: criticality, depth, node_count, file_count, or name.
        limit: Maximum flows to return. Default: 50.
        kind: Optional filter by entry point kind (e.g. "Test", "Function").
        is_test: Filter by test status. True=only test flows, False=only
                 production flows (excludes Test-kind entry points).
                 Default None shows all.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return list_flows(
        repo_root=repo_root,
        sort_by=sort_by,
        limit=limit,
        kind=kind,
        is_test=is_test,
    )


@mcp.tool()
def get_flow_tool(
    flow_id: Optional[int] = None,
    flow_name: Optional[str] = None,
    include_source: bool = False,
    repo_root: Optional[str] = None,
) -> dict:
    """Get detailed information about a single execution flow.

    Returns the full call path with each step's function name, file, and
    line numbers. Optionally includes source code snippets for each step.

    Provide either flow_id (from list_flows_tool) or flow_name to search by name.

    Args:
        flow_id: Database ID of the flow.
        flow_name: Name to search for (partial match). Ignored if flow_id given.
        include_source: Include source code snippets for each step. Default: False.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return get_flow(
        flow_id=flow_id,
        flow_name=flow_name,
        include_source=include_source,
        repo_root=repo_root,
    )


@mcp.tool()
def get_affected_flows_tool(
    changed_files: Optional[list[str]] = None,
    base: str = "HEAD~1",
    repo_root: Optional[str] = None,
    include_steps: bool = False,
) -> dict:
    """Find execution flows affected by changed files.

    Identifies which execution flows pass through nodes in the changed files.
    Useful during code review to understand which user-facing or critical paths
    are impacted by a change. Auto-detects changed files from git if not specified.

    Args:
        changed_files: List of changed file paths (relative to repo root). Auto-detected if omitted.
        base: Git ref for auto-detecting changes. Default: HEAD~1.
        repo_root: Repository root path. Auto-detected if omitted.
        include_steps: If True, include full step arrays for each flow. Default False.
    """
    return get_affected_flows_func(
        changed_files=changed_files,
        base=base,
        repo_root=repo_root,
        include_steps=include_steps,
    )


@mcp.tool()
def list_communities_tool(
    sort_by: str = "size",
    min_size: int = 0,
    repo_root: Optional[str] = None,
) -> dict:
    """List detected code communities in the codebase.

    Each community represents a cluster of related code entities (functions,
    classes) detected via the Leiden algorithm or file-based grouping.
    Use this to understand the high-level structure of the codebase.

    Args:
        sort_by: Sort column: size, cohesion, or name.
        min_size: Minimum community size to include. Default: 0.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return list_communities_func(
        repo_root=repo_root,
        sort_by=sort_by,
        min_size=min_size,
    )


@mcp.tool()
def get_community_tool(
    community_name: Optional[str] = None,
    community_id: Optional[int] = None,
    include_members: bool = False,
    repo_root: Optional[str] = None,
) -> dict:
    """Get detailed information about a single code community.

    Returns community metadata including size, cohesion, dominant language,
    and member list. Optionally includes full node details for each member.

    Provide either community_id (from list_communities_tool) or community_name
    to search by name.

    Args:
        community_name: Name to search for (partial match). Ignored if community_id given.
        community_id: Database ID of the community.
        include_members: Include full member node details. Default: False.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return get_community_func(
        community_name=community_name,
        community_id=community_id,
        include_members=include_members,
        repo_root=repo_root,
    )


@mcp.tool()
def get_architecture_overview_tool(
    repo_root: Optional[str] = None,
) -> dict:
    """Generate an architecture overview based on community structure.

    Builds a high-level view of the codebase architecture by analyzing
    community boundaries and cross-community coupling. Returns compact
    community summaries (no member lists) and aggregated cross-community
    coupling counts instead of individual edges. Includes warnings for
    high coupling between communities.

    Args:
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return get_architecture_overview_func(repo_root=repo_root)


@mcp.tool()
def detect_changes_tool(
    base: str = "HEAD~1",
    changed_files: Optional[list[str]] = None,
    include_source: bool = False,
    max_depth: int = 2,
    repo_root: Optional[str] = None,
) -> dict:
    """Detect changes and produce risk-scored, priority-ordered review guidance.

    Primary tool for code review. Maps git diffs to affected functions,
    flows, communities, and test coverage gaps. Returns risk scores and
    prioritized review items. Replaces get_review_context for change-aware reviews.

    Args:
        base: Git ref to diff against. Default: HEAD~1.
        changed_files: List of changed file paths (relative to repo root). Auto-detected if omitted.
        include_source: Include source code snippets for changed functions. Default: False.
        max_depth: Impact radius depth for BFS traversal. Default: 2.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return detect_changes_func(
        base=base,
        changed_files=changed_files,
        include_source=include_source,
        max_depth=max_depth,
        repo_root=repo_root,
    )


@mcp.tool()
def refactor_tool(
    mode: str = "rename",
    old_name: Optional[str] = None,
    new_name: Optional[str] = None,
    kind: Optional[str] = None,
    file_pattern: Optional[str] = None,
    exclude_paths: Optional[list] = None,
    repo_root: Optional[str] = None,
) -> dict:
    """Graph-powered refactoring operations.

    Unified entry point for rename previews, dead code detection, and
    refactoring suggestions.

    Modes:
    - rename: Preview renaming a symbol. Returns an edit list and a refactor_id
      to pass to apply_refactor_tool. Requires old_name and new_name.
    - dead_code: Find unreferenced functions/classes (no callers, tests, or
      importers, and not entry points).
    - suggest: Get community-driven refactoring suggestions (move misplaced
      functions, remove dead code).

    Args:
        mode: Operation mode: "rename", "dead_code", or "suggest".
        old_name: (rename) Current symbol name to rename.
        new_name: (rename) Desired new name for the symbol.
        kind: (dead_code) Optional filter: Function or Class.
        file_pattern: (dead_code) Filter by file path substring.
        exclude_paths: List of path substrings to exclude (e.g. ['vscode', 'test']).
            Nodes in matching files are omitted from results.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return refactor_func(
        mode=mode,
        old_name=old_name,
        new_name=new_name,
        kind=kind,
        file_pattern=file_pattern,
        exclude_paths=exclude_paths,
        repo_root=repo_root,
    )


@mcp.tool()
def apply_refactor_tool(
    refactor_id: str,
    repo_root: Optional[str] = None,
) -> dict:
    """Apply a previously previewed refactoring to source files.

    Takes a refactor_id from a prior refactor_tool(mode="rename") call and
    applies the exact string replacements to the target files. Previews
    expire after 10 minutes.

    Security: All edit paths are validated to be within the repo root.
    Only exact string replacements are performed (no regex, no eval).

    Args:
        refactor_id: The refactor ID from refactor_tool's response.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return apply_refactor_func(
        refactor_id=refactor_id,
        repo_root=repo_root,
    )


@mcp.tool()
def generate_wiki_tool(
    repo_root: Optional[str] = None,
    force: bool = False,
) -> dict:
    """Generate a markdown wiki from the code community structure.

    Creates a wiki page for each detected community and an index page.
    Pages are written to .code-review-graph/wiki/ inside the repository.
    Only regenerates pages whose content has changed unless force=True.

    Args:
        repo_root: Repository root path. Auto-detected if omitted.
        force: If True, regenerate all pages even if content unchanged. Default: False.
    """
    return generate_wiki_func(repo_root=repo_root, force=force)


@mcp.tool()
def get_wiki_page_tool(
    community_name: str,
    repo_root: Optional[str] = None,
) -> dict:
    """Retrieve a specific wiki page by community name.

    Returns the markdown content of the wiki page for the given community.
    The wiki must have been generated first via generate_wiki_tool.

    Args:
        community_name: Community name to look up.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return get_wiki_page_func(community_name=community_name, repo_root=repo_root)


@mcp.tool()
def list_repos_tool() -> dict:
    """List all registered repositories in the multi-repo registry.

    Returns the list of repos registered at ~/.code-review-graph/registry.json.
    Use the CLI 'register' command to add repos.
    """
    return list_repos_func()


@mcp.tool()
def cross_repo_search_tool(
    query: str,
    kind: Optional[str] = None,
    limit: int = 20,
) -> dict:
    """Search for code entities across all registered repositories.

    Runs hybrid search on each registered repo's graph database and merges
    the results by score. Register repos first with the CLI 'register' command.

    Args:
        query: Search string to match against node names.
        kind: Optional filter: File, Class, Function, Type, or Test.
        limit: Maximum results per repo. Default: 20.
    """
    return cross_repo_search_func(query=query, kind=kind, limit=limit)


@mcp.tool()
def analyze_edit_region_tool(
    file_path: str,
    line_start: int,
    line_end: int,
    repo_root: Optional[str] = None,
) -> dict:
    """Analyze the blast radius of a specific line range within a file.

    Unlike get_impact_radius_tool (whole-file), this tool focuses on the exact
    lines being edited and reports which graph nodes overlap the region, which
    external callers are affected, and which downstream functions are called.

    Args:
        file_path: Path to the file being edited (relative or absolute).
        line_start: First line of the edit region (1-indexed).
        line_end: Last line of the edit region (1-indexed, inclusive).
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return analyze_edit_region(
        file_path=file_path,
        line_start=line_start,
        line_end=line_end,
        repo_root=repo_root,
    )


@mcp.tool()
def audit_workspace_tool(
    include_dead_code: bool = True,
    include_large_functions: bool = True,
    include_cycles: bool = True,
    min_lines: int = 50,
    file_pattern: Optional[str] = None,
    exclude_paths: Optional[list] = None,
    repo_root: Optional[str] = None,
) -> dict:
    """Consolidated workspace audit: dead code, large functions, and dependency cycles.

    Runs multiple quality checks in a single call and returns a health score.
    Equivalent to running refactor_tool(dead_code) + find_large_functions_tool +
    cycle detection in one shot. Use before merging a PR.

    Args:
        include_dead_code: Detect unreferenced functions/classes. Default: True.
        include_large_functions: Find oversized functions. Default: True.
        include_cycles: Detect import/call cycles. Default: True.
        min_lines: Minimum lines to flag a function as large. Default: 50.
        file_pattern: Filter dead code / large functions by file path substring.
        exclude_paths: List of path substrings to exclude (e.g. ['vscode', 'test']).
            Nodes in matching files are omitted from results.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return audit_workspace(
        include_dead_code=include_dead_code,
        include_large_functions=include_large_functions,
        include_cycles=include_cycles,
        min_lines=min_lines,
        file_pattern=file_pattern,
        exclude_paths=exclude_paths,
        repo_root=repo_root,
    )


@mcp.tool()
def trace_dataflow_tool(
    source: str,
    sink: Optional[str] = None,
    max_depth: int = 6,
    repo_root: Optional[str] = None,
) -> dict:
    """Trace data propagation from a source symbol to a sink (or all reachable nodes).

    Performs a forward BFS from source over CALLS and IMPORTS_FROM edges.
    If sink is provided, finds the shortest path and reports whether the data
    can reach the sink. Without a sink, returns all reachable symbols.

    Use to answer: "Can user-supplied data from parse_request reach execute_query?"

    Args:
        source: Qualified name or plain name of the source symbol.
        sink: Optional qualified name or plain name of the target symbol.
              If omitted, returns all symbols reachable from source.
        max_depth: Maximum BFS hops. Default: 6.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return trace_dataflow(
        source=source,
        sink=sink,
        max_depth=max_depth,
        repo_root=repo_root,
    )


@mcp.tool()
def export_scip_tool(
    output_path: Optional[str] = None,
    file_pattern: Optional[str] = None,
    repo_root: Optional[str] = None,
) -> dict:
    """Export the code knowledge graph to a SCIP-compatible JSON document.

    Serialises all nodes and edges to the SCIP open standard (JSON subset)
    so the graph can be consumed by external tools or re-imported elsewhere.

    Args:
        output_path: Destination file path. Defaults to
            .code-review-graph/export.scip.json inside the repo root.
        file_pattern: Restrict export to nodes whose file path contains this
            substring. Omit to export the entire graph.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return export_scip_func(
        output_path=output_path,
        file_pattern=file_pattern,
        repo_root=repo_root,
    )


@mcp.tool()
def import_scip_tool(
    scip_path: str,
    repo_root: Optional[str] = None,
) -> dict:
    """Import a SCIP JSON document into the code knowledge graph.

    Reads a .scip.json file (produced by export_scip_tool or compatible tools)
    and upserts all symbols and relationships into the graph store.
    Existing nodes with matching qualified names are updated in place.

    Args:
        scip_path: Path to the .scip.json file (absolute or relative to repo_root).
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return import_scip_func(scip_path=scip_path, repo_root=repo_root)


@mcp.prompt()
def review_changes(base: str = "HEAD~1") -> list[dict]:
    """Pre-commit review workflow using detect_changes, affected_flows, and test gaps.

    Produces a structured code review with risk levels and actionable findings.

    Args:
        base: Git ref to diff against. Default: HEAD~1.
    """
    return review_changes_prompt(base=base)


@mcp.prompt()
def architecture_map() -> list[dict]:
    """Architecture documentation using communities, flows, and Mermaid diagrams.

    Generates a comprehensive architecture map with module summaries and coupling warnings.
    """
    return architecture_map_prompt()


@mcp.prompt()
def debug_issue(description: str = "") -> list[dict]:
    """Guided debugging using search, flow tracing, and recent changes.

    Systematic debugging workflow that traces execution paths and identifies root causes.

    Args:
        description: Description of the issue to debug.
    """
    return debug_issue_prompt(description=description)


@mcp.prompt()
def onboard_developer() -> list[dict]:
    """New developer orientation using stats, architecture, and critical flows.

    Creates an onboarding guide covering codebase structure, key modules, and patterns.
    """
    return onboard_developer_prompt()


@mcp.prompt()
def pre_merge_check(base: str = "HEAD~1") -> list[dict]:
    """PR readiness check with risk scoring, test gaps, and dead code detection.

    Produces a merge readiness report with risk assessment and recommendations.

    Args:
        base: Git ref to diff against. Default: HEAD~1.
    """
    return pre_merge_check_prompt(base=base)


# ===========================================================================
# Task DAG tools (29-63)
# ===========================================================================

# --- Active root (1) ---

@mcp.tool()
def task_get_active_root(
    repo_root: Optional[str] = None,
) -> dict:
    """Return the single open root task (or null if pipeline is idle).

    [BRAINSTORM] Single-pipeline discipline: at most one root task may be
    open (status not done/archived) at a time.  Use this to:
    - Check if a pipeline is running before calling task_roadmap
    - Verify nothing is open before creating a new root task
    - Orient quickly at the start of a session

    Returns ``{active_root: {id, title, status, ...}}`` or
    ``{active_root: null}`` when idle.
    """
    return task_get_active_root_func(repo_root=repo_root)


# --- CRUD (9) ---

@mcp.tool()
def task_create(
    tasks: list,
    parent_id: Optional[str] = None,
    edges: Optional[list] = None,
    repo_root: Optional[str] = None,
) -> dict:
    """Create one or more tasks under a common parent.

    [BRAINSTORM] Single-pipeline discipline: at most one root task may be
    open (status not done/archived) at a time.  Use this to:
    - Create the root task for a new brainstorm pipeline
    - Decompose a task into subtasks (all sharing the same parent_id)

    Always pass a list — even for a single task:
        task_create(tasks=[{"title": "Root task"}])
        task_create(parent_id="t1", tasks=[
            {"title": "OAuth interface"},
            {"title": "Google OAuth", "description": "impl Google provider"},
            {"title": "JWT service"},
        ])

    With inline edges — atomic decomposition + dependency wiring in one call:
        task_create(parent_id="t1", tasks=[
            {"title": "OAuth interface"},   # index 0
            {"title": "Google OAuth"},      # index 1
            {"title": "JWT service"},       # index 2
            {"title": "Login endpoint"},    # index 3
        ], edges=[
            {"from": 1, "to": 0, "type": "depends_on"},
            {"from": 3, "to": 0, "type": "depends_on"},
            {"from": 3, "to": 2, "type": "depends_on"},
        ])

    Args:
        tasks: List of task dicts — each with title (required), description (optional).
        parent_id: Shared parent task ID. Omit to create root task(s).
        edges: Optional inline edge list — each item: {from, to, type?, description?}.
               ``from`` and ``to`` are 0-based indices into ``tasks``.
               ``type`` defaults to ``"depends_on"``. Cycle detection runs atomically.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return task_create_func(tasks_list=tasks, parent_id=parent_id, edges_list=edges, repo_root=repo_root)


@mcp.tool()
def task_update(
    task_id: str,
    title: Optional[str] = None,
    description: Optional[str] = None,
    status: Optional[str] = None,
    spec: Optional[str] = None,
    acceptance_criteria: Optional[str] = None,
    repo_root: Optional[str] = None,
) -> dict:
    """Update fields of an existing task.

    [BRAINSTORM] Only supplied (non-None) fields are changed.
    Valid status: draft | refined | ready | in_progress | done | archived.

    Args:
        task_id: Task ID to update.
        title: New title.
        description: New description.
        status: New status.
        spec: Full coder specification.
        acceptance_criteria: Verification criteria.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return task_update_func(task_id=task_id, title=title, description=description,
                            status=status, spec=spec,
                            acceptance_criteria=acceptance_criteria, repo_root=repo_root)


@mcp.tool()
def task_edit(
    task_id: str,
    field: str,
    search: Optional[str] = None,
    replace: Optional[str] = None,
    line_start: Optional[int] = None,
    line_end: Optional[int] = None,
    content: Optional[str] = None,
    repo_root: Optional[str] = None,
) -> dict:
    """Surgically edit a text field of a task without rewriting the whole field.

    [BRAINSTORM] Two modes:
    - search/replace: supply search + replace (fails if ambiguous).
    - line range: supply line_start + line_end + content.
    Editable fields: description | spec | acceptance_criteria.

    Args:
        task_id: Task to edit.
        field: Field to edit (description|spec|acceptance_criteria).
        search: String to find (search/replace mode).
        replace: Replacement (search/replace mode).
        line_start: First line (line range mode, 1-indexed).
        line_end: Last line (line range mode, inclusive).
        content: New content (line range mode).
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return task_edit_func(task_id=task_id, field=field, search=search, replace=replace,
                          line_start=line_start, line_end=line_end,
                          content=content, repo_root=repo_root)


@mcp.tool()
def task_delete(
    task_id: str,
    cascade: bool = False,
    repo_root: Optional[str] = None,
) -> dict:
    """Delete a task permanently.

    [BRAINSTORM] Set cascade=True to also delete all subtasks. To preserve
    history, use task_archive instead.

    Args:
        task_id: Task to delete.
        cascade: If True, delete all subtasks recursively.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return task_delete_func(task_id=task_id, cascade=cascade, repo_root=repo_root)


@mcp.tool()
def task_get(
    task_id: str,
    repo_root: Optional[str] = None,
) -> dict:
    """Get a task by ID.

    [BRAINSTORM] Returns all fields of the task row.

    Args:
        task_id: Task ID to retrieve.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return task_get_func(task_id=task_id, repo_root=repo_root)


@mcp.tool()
def task_list(
    parent_id: Optional[str] = None,
    status: Optional[str] = None,
    root_only: bool = False,
    repo_root: Optional[str] = None,
) -> dict:
    """List tasks with optional filters.

    [BRAINSTORM] Returns matching tasks sorted by creation time.

    Args:
        parent_id: Return only direct children of this task.
        status: Filter by status.
        root_only: If True, return only top-level tasks.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return task_list_func(parent_id=parent_id, status=status,
                          root_only=root_only, repo_root=repo_root)


@mcp.tool()
def task_move(
    task_ids: list,
    new_parent_id: Optional[str] = None,
    repo_root: Optional[str] = None,
) -> dict:
    """Move one or more tasks to a new parent (or promote them to root).

    [BRAINSTORM] Fails if the move would create a hierarchy cycle
    (checked atomically across all tasks before applying any changes).

    Always pass a list — even for a single task:
        task_move(task_ids=["t5"], new_parent_id="t11")

    Batch restructuring (common during brainstorm refinement):
        task_move(new_parent_id="t11", task_ids=["t2", "t3", "t4"])

    Promote to root:
        task_move(task_ids=["t5"])

    Args:
        task_ids: List of task IDs to move.
        new_parent_id: Shared new parent task ID. Pass None to make them root tasks.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return task_move_func(task_ids=task_ids, new_parent_id=new_parent_id, repo_root=repo_root)


@mcp.tool()
def task_search(
    root_task_id: str,
    query: str,
    repo_root: Optional[str] = None,
) -> dict:
    """Keyword search within a task subtree.

    [BRAINSTORM] Searches title, description, and spec (case-insensitive).

    Args:
        root_task_id: Root of the subtree to search.
        query: Search string.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return task_search_func(root_task_id=root_task_id, query=query, repo_root=repo_root)


@mcp.tool()
def task_archive(
    task_ids: list,
    reason: str,
    cascade: bool = True,
    repo_root: Optional[str] = None,
) -> dict:
    """Archive one or more tasks (preserves history, unlike task_delete).

    [BRAINSTORM] Sets status to 'archived' with a recorded reason.
    Use when the direction changes fundamentally.

    Always pass a list — even for a single task:
        task_archive(task_ids=["t5"], reason="No longer needed")

    Selective archiving (common when changing approach mid-brainstorm):
        task_archive(reason="Switching to in-app only",
                     task_ids=["t2", "t3", "t6"])

    *cascade* applies to each task individually (default: True).

    Args:
        task_ids: List of task IDs to archive.
        reason: Why these tasks are being archived (required).
        cascade: If True (default), archive all subtasks of each task too.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return task_archive_func(task_ids=task_ids, reason=reason, cascade=cascade, repo_root=repo_root)


# --- DAG Edges (5) ---

@mcp.tool()
def task_add_edge(
    edges: list,
    edge_type: Optional[str] = None,
    repo_root: Optional[str] = None,
) -> dict:
    """Add one or more directed edges between tasks.

    [BRAINSTORM] Edge types: depends_on | blocks | shares_context |
    conflicts_with | informs. Cycle detection enforced for depends_on/blocks
    (checked atomically before any insert).

    Always pass a list — even for a single edge:
        task_add_edge(edges=[{"source_id": "t8", "target_id": "t5"}],
                      edge_type="depends_on")

    Pattern A — one source, many targets:
        task_add_edge(edge_type="depends_on", edges=[
            {"source_id": "t8", "target_id": "t5"},
            {"source_id": "t8", "target_id": "t6"},
        ])

    Pattern B — many sources, one target:
        task_add_edge(edge_type="depends_on", edges=[
            {"source_id": "t5", "target_id": "t2"},
            {"source_id": "t6", "target_id": "t2"},
        ])

    Pattern C — mixed (per-item edge_type overrides default):
        task_add_edge(edge_type="depends_on", edges=[
            {"source_id": "t5", "target_id": "t2"},
            {"source_id": "t6", "target_id": "t7", "edge_type": "shares_context"},
        ])

    Args:
        edges: List of edge dicts — each with source_id, target_id, and optional
               edge_type (overrides top-level default) and description.
        edge_type: Default edge type for items lacking their own edge_type.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return task_add_edge_func(edges=edges, edge_type=edge_type, repo_root=repo_root)


@mcp.tool()
def task_remove_edge(
    source_id: str,
    target_id: str,
    edge_type: str,
    repo_root: Optional[str] = None,
) -> dict:
    """Remove an edge between two tasks.

    [BRAINSTORM] Raises NOT_FOUND if the edge does not exist.

    Args:
        source_id: Source task ID.
        target_id: Target task ID.
        edge_type: Type of edge to remove.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return task_remove_edge_func(source_id=source_id, target_id=target_id,
                                 edge_type=edge_type, repo_root=repo_root)


@mcp.tool()
def task_get_dag(
    root_task_id: str,
    compact: bool = False,
    repo_root: Optional[str] = None,
) -> dict:
    """Get the full DAG rooted at a task.

    [BRAINSTORM] Returns all tasks in the subtree plus all edges between them.

    Args:
        root_task_id: Root of the DAG to retrieve.
        compact: If True, return only {id, title, status, depth, parent_id} per node.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return task_get_dag_func(root_task_id=root_task_id, compact=compact, repo_root=repo_root)


@mcp.tool()
def task_topological_sort(
    root_task_id: str,
    repo_root: Optional[str] = None,
) -> dict:
    """Topologically sort leaf tasks by their depends_on relationships.

    [BRAINSTORM] Returns leaf tasks in dependency order (dependencies first).

    Args:
        root_task_id: Root of the subtree to sort.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return task_topological_sort_func(root_task_id=root_task_id, repo_root=repo_root)


# --- Code Links (5) ---

@mcp.tool()
def task_link_code(
    task_id: str,
    links: list,
    repo_root: Optional[str] = None,
) -> dict:
    """Link a task to one or many code graph nodes.

    [BRAINSTORM] Associates a task with code entities (functions, classes, files).
    Always pass a list — even for a single node.

    Single node:
        task_link_code(task_id="t5", links=[
            {"ref_type": "modifies", "code_node_id": 1786}
        ])

    Multiple nodes (typical leaf task — 3-8 code refs is normal):
        task_link_code(task_id="t5", links=[
            {"ref_type": "modifies", "code_node_id": 101},
            {"ref_type": "modifies", "code_node_id": 102},
            {"ref_type": "reads",    "qualified_name": "src/auth.py::TokenService"},
            {"ref_type": "creates",  "qualified_name": "src/models.py::OAuthToken",
             "description": "new model class"},
        ])

    Both ``code_node_id`` (int) and ``qualified_name`` (string) are returned by
    semantic_search_nodes_tool — no extra lookup needed.
    Failed items are collected in errors and do not abort the whole batch.

    ref_type values: modifies | creates | deletes | reads | tests

    Args:
        task_id: Task ID.
        links: List of dicts — each with ref_type and one of code_node_id /
               qualified_name. Optional per-item: description.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return task_link_code_func(task_id=task_id, links=links, repo_root=repo_root)


@mcp.tool()
def task_unlink_code(
    task_id: str,
    code_node_id: int,
    repo_root: Optional[str] = None,
) -> dict:
    """Remove all code refs between a task and a code node.

    [BRAINSTORM] Removes the association regardless of ref_type.

    Args:
        task_id: Task ID.
        code_node_id: Integer ID of the code node.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return task_unlink_code_func(task_id=task_id, code_node_id=code_node_id, repo_root=repo_root)


@mcp.tool()
def task_get_code_refs(
    task_id: str,
    repo_root: Optional[str] = None,
) -> dict:
    """Get all code nodes linked to a task.

    [BRAINSTORM] Returns code nodes with metadata (name, file, lines).

    Args:
        task_id: Task ID.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return task_get_code_refs_func(task_id=task_id, repo_root=repo_root)


@mcp.tool()
def task_find_by_code_node(
    code_node_id: int,
    open_only: bool = True,
    repo_root: Optional[str] = None,
) -> dict:
    """Find all tasks that reference a given code node.

    [BRAINSTORM] Useful for understanding which tasks touch a specific function.

    Args:
        code_node_id: Integer ID of the code node.
        open_only: If True (default), exclude archived and done tasks.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return task_find_by_code_node_func(code_node_id=code_node_id, open_only=open_only, repo_root=repo_root)


@mcp.tool()
def task_suggest_code_links(
    task_id: str,
    repo_root: Optional[str] = None,
    limit: int = 20,
) -> dict:
    """Suggest code nodes to link to a task based on keyword extraction.

    [BRAINSTORM] Extracts keywords from task title and description, searches
    the code graph, and returns ranked candidates. Already-linked nodes are
    excluded. Results scored by keyword match count. Does NOT create links —
    returns candidates for review.

    Args:
        task_id: Task ID.
        repo_root: Repository root path. Auto-detected if omitted.
        limit: Maximum number of results to return (default 20).
    """
    return task_suggest_code_links_func(task_id=task_id, repo_root=repo_root, limit=limit)


# --- Analysis (4) ---

@mcp.tool()
def task_find_conflicts(
    root_task_id: str,
    repo_root: Optional[str] = None,
) -> dict:
    """Find conflicting leaf tasks whose code refs intersect.

    [BRAINSTORM] Algorithmically detects tasks that touch the same code nodes.
    Run this after decomposing tasks to catch coordination issues early.

    Args:
        root_task_id: Root of the subtree to analyze.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return task_find_conflicts_func(root_task_id=root_task_id, repo_root=repo_root)


@mcp.tool()
def task_check_isolation(
    task_id: str,
    repo_root: Optional[str] = None,
) -> dict:
    """Check how isolated a leaf task is from the rest of the codebase.

    [BRAINSTORM] Score = internal / (internal + external). Low score (<0.5)
    suggests the task may be too coupled and should be split.

    Args:
        task_id: Task ID to check.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return task_check_isolation_func(task_id=task_id, repo_root=repo_root)


@mcp.tool()
def task_blast_radius(
    task_id: str,
    depth: int = 2,
    include_affected_nodes: bool = False,
    repo_root: Optional[str] = None,
) -> dict:
    """Compute the code graph blast radius of a task.

    [BRAINSTORM] Shows direct nodes, affected nodes (up to depth hops),
    uncovered nodes (risk zones), and coverage ratio.

    Args:
        task_id: Task ID to analyze.
        depth: BFS depth (default: 2).
        include_affected_nodes: If True, include full affected_nodes list.
            Default False returns only affected_nodes_count (scalar).
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return task_blast_radius_func(
        task_id=task_id,
        depth=depth,
        include_affected_nodes=include_affected_nodes,
        repo_root=repo_root,
    )


@mcp.tool()
def task_execution_order(
    root_task_id: str,
    repo_root: Optional[str] = None,
) -> dict:
    """Compute parallelism-aware execution order for leaf tasks.

    [BRAINSTORM] Groups leaf tasks into levels. Tasks in the same level
    can run in parallel. Based on depends_on edges.

    Args:
        root_task_id: Root of the subtree to analyze.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return task_execution_order_func(root_task_id=root_task_id, repo_root=repo_root)


# --- Validation & Context (3) ---

@mcp.tool()
def task_validate(
    root_task_id: Optional[str] = None,
    repo_root: Optional[str] = None,
) -> dict:
    """Validate the task DAG before handing it off to a coder.

    [BRAINSTORM] Gate-check: runs 9 algorithmic checks covering cycles,
    dependencies, code refs, open questions, assumptions, contracts,
    acceptance criteria, descriptions, and leaf-vs-parent attachment.

    If *root_task_id* is omitted, the active root task is auto-detected.

    Single-pipeline rule: the brainstorm phase must be fully complete
    (all errors resolved) before starting implementation.

    Args:
        root_task_id: Root of the DAG to validate. Auto-detected if omitted.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return task_validate_func(root_task_id=root_task_id, repo_root=repo_root)


@mcp.tool()
def task_export(
    task_id: Optional[str] = None,
    include_analysis: bool = False,
    include_source: bool = False,
    repo_root: Optional[str] = None,
) -> dict:
    """Export full task context — primary entry point for LLM consumption.

    [BRAINSTORM] Single call that assembles all layers of a task into one dict.
    Replaces the old ``task_build_context`` tool (removed).

    If *task_id* is omitted, the active root task is auto-detected — since
    the single-pipeline discipline guarantees at most one open root at a time.

    Use this to hand off a task to a coder, review progress, or let LLM
    reason about what a task requires.

    Returns:
        task: {id, title, description, spec, acceptance_criteria, status, ...}
        parent_chain: [{id, title, description}] — from root down to parent
        subtasks: [{...task fields, edges: [...]}] — direct children
        edges: {incoming: [...], outgoing: [...]} — DAG edges
        related_tasks: [{id, title, edge_type, direction}] — flat edge list
        code_refs: [{node_id, name, file, line_start, line_end, ref_type,
                     qualified_name, kind, language}]  — linked code nodes
        notes: [{id, note_type, content, status, resolution, ...}]
                — notes from this task AND all ancestors
        contracts: {
            as_provider: [{id, name, contract_type, definition, status,
                           provider_task_ids, consumer_task_ids, code_node_id}],
            as_consumer: [...]
        }
        open_items: {unresolved_questions, unverified_assumptions,
                     pending_contracts}  — always present
        isolation: {isolation_score, external_dependencies, ...}
                    — only if include_analysis=True
        conflicts: [{task_a_id, task_b_id, shared_nodes, conflict_type}]
                    — only if include_analysis=True
        pipeline_state: {ready_for_coder, open_questions, open_assumptions,
                         pending_contracts}
                    — only if include_analysis=True

    Args:
        task_id: Task ID to export.
        include_analysis: Add isolation, sibling conflicts, pipeline_state.
            Equivalent to the old task_build_context behaviour. Default False.
        include_source: Attach line-range info to code_refs so LLM knows
            exactly where to read without loading full files. Default False.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return task_export_func(task_id=task_id, include_analysis=include_analysis,
                            include_source=include_source, repo_root=repo_root)


# --- Notes (4) ---

@mcp.tool()
def note_add(
    task_id: str,
    notes: list,
    repo_root: Optional[str] = None,
) -> dict:
    """Add one or more brainstorm notes to a task.

    [BRAINSTORM] Types: decision | question | assumption | constraint | risk.
    Always pass a list — even for a single note.

    Single note:
        note_add(task_id="t1", notes=[
            {"note_type": "constraint", "content": "self-hosted only"}
        ])

    Multiple notes (typical after structured brainstorm interview):
        note_add(task_id="t1", notes=[
            {"note_type": "decision", "content": "Use JWT",
             "status": "resolved", "resolution": "JWT tokens",
             "rationale": "stateless"},
            {"note_type": "constraint", "content": "self-hosted only"},
            {"note_type": "question", "content": "WebSocket or polling?"},
            {"note_type": "assumption", "content": "User model already exists"},
        ])

    Each item requires note_type and content.
    Optional per-item: status (default "open"), resolution, rationale, alternatives.

    Args:
        task_id: Task to attach the notes to.
        notes: List of note dicts.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return note_add_func(task_id=task_id, notes=notes, repo_root=repo_root)


@mcp.tool()
def note_update(
    note_id: str,
    status: Optional[str] = None,
    resolution: Optional[str] = None,
    rationale: Optional[str] = None,
    content: Optional[str] = None,
    repo_root: Optional[str] = None,
) -> dict:
    """Update an existing note.

    [BRAINSTORM] Use to resolve open questions or update rationale.

    Args:
        note_id: Note ID to update.
        status: New status (open|resolved|rejected|deferred).
        resolution: Answer or decision text.
        rationale: Reasoning.
        content: Updated note text.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return note_update_func(note_id=note_id, status=status, resolution=resolution,
                            rationale=rationale, content=content, repo_root=repo_root)


@mcp.tool()
def note_list(
    task_id: str,
    note_type: Optional[str] = None,
    status: Optional[str] = None,
    include_parent: bool = True,
    include_children: bool = False,
    repo_root: Optional[str] = None,
) -> dict:
    """List notes for a task.

    [BRAINSTORM] With include_parent=True (default), includes notes from all
    ancestor tasks — the full decision history for this task.
    With include_children=True, includes notes from all descendants —
    useful for searching notes across an entire brainstorm subtree.

    Args:
        task_id: Task ID.
        note_type: Filter by type (decision|question|assumption|constraint|risk).
        status: Filter by status.
        include_parent: Include ancestor notes (default: True).
        include_children: Include descendant notes — search entire subtree.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return note_list_func(task_id=task_id, note_type=note_type, status=status,
                          include_parent=include_parent, include_children=include_children,
                          repo_root=repo_root)


@mcp.tool()
def note_delete(
    note_id: str,
    repo_root: Optional[str] = None,
) -> dict:
    """Delete a note by ID.

    [BRAINSTORM] Permanently removes the note.

    Args:
        note_id: Note ID to delete.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return note_delete_func(note_id=note_id, repo_root=repo_root)


# --- Contracts (5) ---

@mcp.tool()
def contract_add(
    name: str,
    contract_type: str,
    definition: str,
    scope_task_id: str,
    provider_task_id: Optional[str] = None,
    consumer_task_ids: Optional[list] = None,
    code_node_id: Optional[int] = None,
    qualified_name: Optional[str] = None,
    repo_root: Optional[str] = None,
) -> dict:
    """Create a contract / design entity scoped to a brainstorm subtree.

    [BRAINSTORM] Represents a data structure (OAuthToken), interface
    (IUserRepo), API spec, or schema change on existing code. Participants
    are optional at creation — attach them later with contract_link.

    Types: interface | api | schema | event | data_format

    Args:
        name: Short identifier, e.g. "OAuthToken", "UserRepo.save()".
        contract_type: Contract type.
        definition: Human-readable spec (TypeScript-like, JSON Schema, etc.).
        scope_task_id: Root task that owns this brainstorm scope.
        provider_task_id: Task that will implement this contract (optional).
        consumer_task_ids: Tasks that will use this contract (optional).
        code_node_id: Integer node id from semantic_search_nodes_tool results.
        qualified_name: Qualified name string from search results or code_refs.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return contract_add_func(name=name, contract_type=contract_type,
                             definition=definition, scope_task_id=scope_task_id,
                             provider_task_id=provider_task_id,
                             consumer_task_ids=consumer_task_ids,
                             code_node_id=code_node_id,
                             qualified_name=qualified_name,
                             repo_root=repo_root)


@mcp.tool()
def contract_link(
    contract_id: str,
    task_id: str,
    role: str,
    repo_root: Optional[str] = None,
) -> dict:
    """Attach a task to a contract as provider or consumer.

    [BRAINSTORM] Use after task decomposition when new subtasks need to
    participate in an existing design entity/contract.

    Args:
        contract_id: Contract to link to.
        task_id: Task to attach.
        role: 'provider' or 'consumer'.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return contract_link_func(contract_id=contract_id, task_id=task_id,
                              role=role, repo_root=repo_root)


@mcp.tool()
def contract_unlink(
    contract_id: str,
    task_id: str,
    repo_root: Optional[str] = None,
) -> dict:
    """Remove all links between a task and a contract.

    [BRAINSTORM] Use when a task no longer owns or uses a design entity.

    Args:
        contract_id: Contract to unlink from.
        task_id: Task to detach.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return contract_unlink_func(contract_id=contract_id, task_id=task_id,
                                repo_root=repo_root)


@mcp.tool()
def contract_update(
    contract_id: str,
    name: Optional[str] = None,
    definition: Optional[str] = None,
    status: Optional[str] = None,
    code_node_id: Optional[int] = None,
    qualified_name: Optional[str] = None,
    repo_root: Optional[str] = None,
) -> dict:
    """Update a contract's name, definition, status, or code_node reference.

    [BRAINSTORM] Lifecycle: proposed -> agreed -> implemented -> verified.
    Status is also auto-propagated when participant task statuses change.

    To link to an existing code node provide EITHER *code_node_id* (integer
    ``id`` from ``semantic_search_nodes_tool``) OR *qualified_name* (string
    ``qualified_name`` field from the same results).

    Args:
        contract_id: Contract ID to update.
        name: Updated name.
        definition: Updated contract definition.
        status: New status (proposed|agreed|implemented|verified|void).
        code_node_id: Integer node id from semantic_search_nodes_tool results.
        qualified_name: Qualified name string from search results or code_refs.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return contract_update_func(contract_id=contract_id, name=name,
                                definition=definition, status=status,
                                code_node_id=code_node_id,
                                qualified_name=qualified_name,
                                repo_root=repo_root)


@mcp.tool()
def contract_list(
    scope_task_id: Optional[str] = None,
    task_id: Optional[str] = None,
    name: Optional[str] = None,
    repo_root: Optional[str] = None,
) -> dict:
    """List contracts matching given filters.

    [BRAINSTORM] Filters can be combined:
    - scope_task_id — all contracts in a brainstorm subtree (including orphans
      not yet linked to any task).
    - task_id — contracts where this task participates (any role).
    - name — partial name match (case-insensitive).

    Args:
        scope_task_id: Root of the brainstorm scope.
        task_id: Task participating in the contracts.
        name: Partial name to search for.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return contract_list_func(scope_task_id=scope_task_id, task_id=task_id,
                              name=name, repo_root=repo_root)


# --- Roadmap (2) ---

@mcp.tool()
def task_roadmap(
    root_task_id: Optional[str] = None,
    include_archived: bool = False,
    repo_root: Optional[str] = None,
) -> dict:
    """Get an aggregated progress snapshot for the entire task tree.

    [BRAINSTORM] The primary orientation tool — call this at the start of
    every session. Returns progress counts, execution phases, contract
    statuses, and an attention block showing what needs action right now.

    If *root_task_id* is omitted, the active root task is auto-detected.
    This is the typical usage: since the single-pipeline discipline ensures
    there is at most one open root task at a time.

    Args:
        root_task_id: Root task ID. Auto-detected if omitted.
        include_archived: If True, include archived tasks in phases and add
            an ``archived`` section. Default False — archived tasks are
            hidden from phases to keep the roadmap readable.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return task_roadmap_func(root_task_id=root_task_id,
                             include_archived=include_archived,
                             repo_root=repo_root)


@mcp.tool()
def task_roadmap_diff(
    root_task_id: str,
    since_timestamp: float,
    repo_root: Optional[str] = None,
) -> dict:
    """Get what changed in the task tree since a given Unix timestamp.

    [BRAINSTORM] Use at the start of a resumed session to quickly understand
    what happened since you last worked on this brainstorm.

    Args:
        root_task_id: Root task ID.
        since_timestamp: Unix timestamp (float). Changes after this are returned.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return task_roadmap_diff_func(root_task_id=root_task_id,
                                  since_timestamp=since_timestamp, repo_root=repo_root)


@mcp.tool()
def task_find_for_impact(
    file_paths: list[str],
    root_task_id: Optional[str] = None,
    max_depth: int = 2,
    open_only: bool = True,
    include_node_details: bool = False,
    repo_root: Optional[str] = None,
) -> dict:
    """Find open tasks whose code refs overlap the blast radius of changed files.

    [BRAINSTORM] Cross-query bridging code graph and task DAG. Given a list of
    changed file paths, expands their impact radius in the code graph (BFS),
    then returns all tasks that reference nodes within that radius.

    Use before merging: "Are there open tasks for code I'm about to change?"

    Args:
        file_paths: Changed file paths — relative (``code_review_graph/tasks.py``),
            absolute (``C:\\path\\tasks.py``), or filename-only (``tasks.py``).
            All forms are normalised and matched the same way as
            ``get_impact_radius_tool`` (converts to absolute via repo_root,
            then falls back to suffix/tail matching).
        root_task_id: Restrict results to this task subtree. Omit for all tasks.
        max_depth: BFS hops into code graph. Default 2.
        open_only: If True (default), only return non-done/archived tasks.
        include_node_details: If True, include full node metadata in
            ``impacted_nodes`` and ``uncovered_nodes``. Default False —
            only counts are returned to keep the response compact.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return task_find_for_impact_func(file_paths=file_paths, root_task_id=root_task_id,
                                     max_depth=max_depth, open_only=open_only,
                                     include_node_details=include_node_details,
                                     repo_root=repo_root)


@mcp.tool()
def task_suggest_contracts(
    root_task_id: str,
    repo_root: Optional[str] = None,
) -> dict:
    """Suggest contracts between tasks with implicit code-level dependencies.

    [BRAINSTORM] Detects pairs of leaf tasks whose code refs are connected
    via code graph edges (calls/imports) but have no explicit task_edge or
    contract between them. These hidden dependencies need interface contracts.

    Args:
        root_task_id: Root of the subtree to analyze.
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return task_suggest_contracts_func(root_task_id=root_task_id, repo_root=repo_root)


@mcp.tool()
def task_check_rollup(
    task_id: str,
    repo_root: Optional[str] = None,
) -> dict:
    """Check whether a task's parent (and ancestors) can change status.

    [BRAINSTORM] After completing or archiving a task, call this to find out
    if the parent task can now be closed or archived as well. Walks the full
    ancestor chain and reports readiness at each level.

    Does NOT modify any data — purely analytical. The LLM/user decides
    whether to act on the suggestions.

    Args:
        task_id: The task that was just updated (completed/archived).
        repo_root: Repository root path. Auto-detected if omitted.
    """
    return task_check_rollup_func(task_id=task_id, repo_root=repo_root)


def main(repo_root: str | None = None) -> None:
    """Run the MCP server via stdio."""
    global _default_repo_root
    _default_repo_root = repo_root
    # Pre-warm heavy optional dependencies (igraph, matplotlib) in a background
    # thread so the first build/update call is not delayed by cold imports.
    import threading

    def _preload() -> None:
        try:
            import code_review_graph.communities  # noqa: F401
        except Exception:
            pass

    threading.Thread(target=_preload, daemon=True).start()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
