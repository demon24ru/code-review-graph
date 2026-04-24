/**
 * code-review-graph — OpenCode custom tool wrapper
 *
 * THIN STABLE WRAPPER. Logic lives in code_review_graph/ (Python).
 * Editing Python files takes effect immediately — no session restart needed.
 * Only changes to THIS file require a session restart.
 *
 * Naming: crg_<function_name_from_main.py>
 *
 * Adding a new tool:
 *   1. Implement in code_review_graph/          ← no restart
 *   2. Register in mcp_tool_runner.py           ← no restart
 *   3. Add export here                          ← restart needed (once)
 */

import { tool } from "@opencode-ai/plugin"
import path from "path"

// ---------------------------------------------------------------------------
// Helper
// ---------------------------------------------------------------------------

async function callPython(
  toolName: string,
  args: Record<string, unknown>,
  repoRoot: string,
): Promise<string> {
  const runner = path.join(repoRoot, "code_review_graph", "mcp_tool_runner.py")
  const payload = JSON.stringify({ tool: toolName, args })
  const result = await Bun.$`python ${runner}`.stdin(payload).text()
  return result.trim()
}

// ---------------------------------------------------------------------------
// Code-graph tools (30)
// ---------------------------------------------------------------------------

export const crg_build_or_update_graph_tool = tool({
  description: "Build or incrementally update the code knowledge graph. Call this first to initialize the graph, or after making changes. By default performs an incremental update (only changed files). Set full_rebuild=true to re-parse every file.",
  args: {
    full_rebuild: tool.schema.boolean().default(false).describe("If true, re-parse all files. Default: false (incremental)."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
    base: tool.schema.string().default("HEAD~1").describe("Git ref to diff against for incremental updates."),
  },
  async execute(args, context) {
    return callPython("build_or_update_graph_tool", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_get_impact_radius_tool = tool({
  description: "Analyze the blast radius of changed files in the codebase. Shows which functions, classes, and files are impacted by changes. Auto-detects changed files from git if not specified.",
  args: {
    changed_files: tool.schema.array(tool.schema.string()).optional().describe("List of changed file paths (relative to repo root). Auto-detected if omitted."),
    max_depth: tool.schema.number().default(2).describe("Number of hops to traverse in the dependency graph."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
    base: tool.schema.string().default("HEAD~1").describe("Git ref for auto-detecting changes."),
    summary_only: tool.schema.boolean().default(true).describe("If true, return only counts and summary (no full node/edge arrays). Keeps response under 1KB."),
  },
  async execute(args, context) {
    return callPython("get_impact_radius_tool", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_query_graph_tool = tool({
  description: "Run a predefined graph query to explore code relationships. Available patterns: callers_of, callees_of, imports_of, importers_of, children_of, tests_for, inheritors_of, file_summary.",
  args: {
    pattern: tool.schema.string().describe("Query pattern name: callers_of | callees_of | imports_of | importers_of | children_of | tests_for | inheritors_of | file_summary"),
    target: tool.schema.string().describe("Node name, qualified name, or file path to query."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("query_graph_tool", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_get_review_context_tool = tool({
  description: "Generate a focused, token-efficient review context for code changes. Combines impact analysis with source snippets and review guidance. Use this for comprehensive code reviews.",
  args: {
    changed_files: tool.schema.array(tool.schema.string()).optional().describe("Files to review. Auto-detected from git diff if omitted."),
    max_depth: tool.schema.number().default(2).describe("Impact radius depth."),
    include_source: tool.schema.boolean().default(true).describe("Include source code snippets."),
    max_lines_per_file: tool.schema.number().default(200).describe("Max source lines per file."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
    base: tool.schema.string().default("HEAD~1").describe("Git ref for change detection."),
    summary_only: tool.schema.boolean().default(false).describe("If true, return only counts and summary (no full node/edge arrays). Keeps response under 1KB."),
  },
  async execute(args, context) {
    return callPython("get_review_context_tool", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_semantic_search_nodes_tool = tool({
  description: "Search for code entities by name, keyword, or semantic similarity. Uses vector embeddings for semantic search when available (run embed_graph_tool first). Falls back to keyword matching otherwise. Multi-word queries are automatically converted to FTS5 OR expressions.",
  args: {
    query: tool.schema.string().describe("Search string. Multi-word queries find all in one round-trip."),
    kind: tool.schema.string().optional().describe("Optional filter: File, Class, Function, or Test."),
    limit: tool.schema.number().default(20).describe("Maximum results."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
    model: tool.schema.string().optional().describe("Embedding model for query vectors. Falls back to CRG_EMBEDDING_MODEL env var, then all-MiniLM-L6-v2."),
    names: tool.schema.array(tool.schema.string()).optional().describe("List of symbol names for bulk multi-symbol lookup. Example: [\"create_task\", \"move_task\"]"),
    file_path: tool.schema.string().optional().describe("Optional file path filter (substring). Only nodes whose file_path contains this string are returned."),
  },
  async execute(args, context) {
    return callPython("semantic_search_nodes_tool", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_embed_graph_tool = tool({
  description: "Compute vector embeddings for all graph nodes to enable semantic search. Requires: pip install code-review-graph[embeddings]. Default model: all-MiniLM-L6-v2. After running this, semantic_search_nodes_tool will use vector similarity instead of keyword matching.",
  args: {
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
    model: tool.schema.string().optional().describe("Embedding model name (HuggingFace ID or local path)."),
  },
  async execute(args, context) {
    return callPython("embed_graph_tool", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_list_graph_stats_tool = tool({
  description: "Get aggregate statistics about the code knowledge graph. Shows total nodes, edges, languages, files, and last update time. Useful for checking if the graph is built and up to date.",
  args: {
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("list_graph_stats_tool", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_get_docs_section_tool = tool({
  description: "Get a specific section from the LLM-optimized documentation reference. Returns only the requested section content for minimal token usage. Use this before answering any user question about the plugin. Available sections: usage, review-delta, review-pr, commands, legal, watch, embeddings, languages, troubleshooting.",
  args: {
    section_name: tool.schema.string().describe("The section to retrieve (e.g. \"review-delta\", \"usage\")."),
  },
  async execute(args, context) {
    return callPython("get_docs_section_tool", args, context.worktree)
  },
})

export const crg_find_files_by_pattern_tool = tool({
  description: "Find files matching path patterns and retrieve their top-level nodes. Useful for discovering codebase structure without reading entire files.",
  args: {
    patterns: tool.schema.array(tool.schema.string()).describe("List of glob patterns (e.g. [\"*router*\", \"main.*\", \"src/**/*.ts\"])"),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
    limit: tool.schema.number().default(50).describe("Maximum number of files to return."),
  },
  async execute(args, context) {
    return callPython("find_files_by_pattern_tool", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_find_large_functions_tool = tool({
  description: "Find functions, classes, or files exceeding a line-count threshold. Useful for decomposition audits, code quality checks, and enforcing size limits during code review. Results are ordered by line count.",
  args: {
    min_lines: tool.schema.number().default(50).describe("Minimum line count to flag."),
    kind: tool.schema.string().optional().describe("Optional filter: Function, Class, or File."),
    file_path_pattern: tool.schema.string().optional().describe("Filter by file path substring (e.g. \"components/\")."),
    limit: tool.schema.number().default(50).describe("Maximum results."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("find_large_functions_tool", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_list_flows_tool = tool({
  description: "List execution flows in the codebase, sorted by criticality. Each flow represents a call chain starting from an entry point (HTTP handler, CLI command, test function, etc.).",
  args: {
    sort_by: tool.schema.string().default("criticality").describe("Sort column: criticality, depth, node_count, file_count, or name."),
    limit: tool.schema.number().default(50).describe("Maximum flows to return."),
    kind: tool.schema.string().optional().describe("Optional filter by entry point kind (e.g. \"Test\", \"Function\")."),
    is_test: tool.schema.boolean().optional().describe("Filter by test status. true=only test flows, false=only production flows. Default null shows all."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("list_flows_tool", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_get_flow_tool = tool({
  description: "Get detailed information about a single execution flow. Returns the full call path with each step's function name, file, and line numbers. Provide either flow_id (from list_flows_tool) or flow_name to search by name.",
  args: {
    flow_id: tool.schema.number().optional().describe("Database ID of the flow."),
    flow_name: tool.schema.string().optional().describe("Name to search for (partial match). Ignored if flow_id given."),
    include_source: tool.schema.boolean().default(false).describe("Include source code snippets for each step."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("get_flow_tool", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_get_affected_flows_tool = tool({
  description: "Find execution flows affected by changed files. Identifies which execution flows pass through nodes in the changed files. Useful during code review to understand which user-facing or critical paths are impacted.",
  args: {
    changed_files: tool.schema.array(tool.schema.string()).optional().describe("List of changed file paths. Auto-detected if omitted."),
    base: tool.schema.string().default("HEAD~1").describe("Git ref for auto-detecting changes."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
    include_steps: tool.schema.boolean().default(false).describe("If true, include full step arrays for each flow."),
  },
  async execute(args, context) {
    return callPython("get_affected_flows_tool", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_list_communities_tool = tool({
  description: "List detected code communities in the codebase. Each community represents a cluster of related code entities detected via the Leiden algorithm or file-based grouping.",
  args: {
    sort_by: tool.schema.string().default("size").describe("Sort column: size, cohesion, or name."),
    min_size: tool.schema.number().default(0).describe("Minimum community size to include."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("list_communities_tool", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_get_community_tool = tool({
  description: "Get detailed information about a single code community. Returns community metadata including size, cohesion, dominant language, and member list. Provide either community_id (from list_communities_tool) or community_name to search by name.",
  args: {
    community_name: tool.schema.string().optional().describe("Name to search for (partial match). Ignored if community_id given."),
    community_id: tool.schema.number().optional().describe("Database ID of the community."),
    include_members: tool.schema.boolean().default(false).describe("Include full member node details."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("get_community_tool", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_get_architecture_overview_tool = tool({
  description: "Generate an architecture overview based on community structure. Builds a high-level view of the codebase architecture by analyzing community boundaries and cross-community coupling. Returns compact community summaries and aggregated cross-community coupling counts.",
  args: {
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("get_architecture_overview_tool", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_detect_changes_tool = tool({
  description: "Detect changes and produce risk-scored, priority-ordered review guidance. Primary tool for code review. Maps git diffs to affected functions, flows, communities, and test coverage gaps.",
  args: {
    base: tool.schema.string().default("HEAD~1").describe("Git ref to diff against."),
    changed_files: tool.schema.array(tool.schema.string()).optional().describe("List of changed file paths. Auto-detected if omitted."),
    include_source: tool.schema.boolean().default(false).describe("Include source code snippets for changed functions."),
    max_depth: tool.schema.number().default(2).describe("Impact radius depth for BFS traversal."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
    summary_only: tool.schema.boolean().default(true).describe("If true, return only counts and summary (no full node/edge arrays). Keeps response under 1KB."),
  },
  async execute(args, context) {
    return callPython("detect_changes_tool", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_refactor_tool = tool({
  description: "Graph-powered refactoring operations. Unified entry point for rename previews, dead code detection, and refactoring suggestions. Modes: rename (preview renaming a symbol), dead_code (find unreferenced functions/classes), suggest (community-driven suggestions).",
  args: {
    mode: tool.schema.string().default("rename").describe("Operation mode: \"rename\", \"dead_code\", or \"suggest\"."),
    old_name: tool.schema.string().optional().describe("(rename) Current symbol name to rename."),
    new_name: tool.schema.string().optional().describe("(rename) Desired new name for the symbol."),
    kind: tool.schema.string().optional().describe("(dead_code) Optional filter: Function or Class."),
    file_pattern: tool.schema.string().optional().describe("(dead_code) Filter by file path substring."),
    exclude_paths: tool.schema.array(tool.schema.string()).optional().describe("List of path substrings to exclude (e.g. ['vscode', 'test'])."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("refactor_tool", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_apply_refactor_tool = tool({
  description: "Apply a previously previewed refactoring to source files. Takes a refactor_id from a prior refactor_tool(mode=\"rename\") call and applies the exact string replacements. Previews expire after 10 minutes.",
  args: {
    refactor_id: tool.schema.string().describe("The refactor ID from refactor_tool's response."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("apply_refactor_tool", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_generate_wiki_tool = tool({
  description: "Generate a markdown wiki from the code community structure. Creates a wiki page for each detected community and an index page. Pages are written to .code-review-graph/wiki/ inside the repository.",
  args: {
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
    force: tool.schema.boolean().default(false).describe("If true, regenerate all pages even if content unchanged."),
  },
  async execute(args, context) {
    return callPython("generate_wiki_tool", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_get_wiki_page_tool = tool({
  description: "Retrieve a specific wiki page by community name. Returns the markdown content of the wiki page for the given community. The wiki must have been generated first via generate_wiki_tool.",
  args: {
    community_name: tool.schema.string().describe("Community name to look up."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("get_wiki_page_tool", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_list_repos_tool = tool({
  description: "List all registered repositories in the multi-repo registry. Returns the list of repos registered at ~/.code-review-graph/registry.json. Use the CLI 'register' command to add repos.",
  args: {},
  async execute(_args, context) {
    return callPython("list_repos_tool", {}, context.worktree)
  },
})

export const crg_cross_repo_search_tool = tool({
  description: "Search for code entities across all registered repositories. Runs hybrid search on each registered repo's graph database and merges the results by score. Register repos first with the CLI 'register' command.",
  args: {
    query: tool.schema.string().describe("Search string to match against node names."),
    kind: tool.schema.string().optional().describe("Optional filter: File, Class, Function, or Test."),
    limit: tool.schema.number().default(20).describe("Maximum results per repo."),
  },
  async execute(args, context) {
    return callPython("cross_repo_search_tool", args, context.worktree)
  },
})

export const crg_register_repo_tool = tool({
  description: "Register a repository in the multi-repo registry. Adds a repository to the global registry so it can be searched via cross_repo_search_tool. The repository must have a built graph first.",
  args: {
    path: tool.schema.string().describe("Absolute path to the repository root."),
    alias: tool.schema.string().optional().describe("Optional short name (defaults to directory name)."),
  },
  async execute(args, context) {
    return callPython("register_repo_tool", args, context.worktree)
  },
})

export const crg_unregister_repo_tool = tool({
  description: "Remove a repository from the multi-repo registry. Removes the registry entry only — does NOT delete any files. Use list_repos_tool to see registered repos.",
  args: {
    path_or_alias: tool.schema.string().describe("Repository path or alias to remove."),
  },
  async execute(args, context) {
    return callPython("unregister_repo_tool", args, context.worktree)
  },
})

export const crg_analyze_edit_region_tool = tool({
  description: "Analyze the blast radius of a specific line range within a file. Unlike get_impact_radius_tool (whole-file), this tool focuses on the exact lines being edited and reports which graph nodes overlap the region.",
  args: {
    file_path: tool.schema.string().describe("Path to the file being edited (relative or absolute)."),
    line_start: tool.schema.number().describe("First line of the edit region (1-indexed)."),
    line_end: tool.schema.number().describe("Last line of the edit region (1-indexed, inclusive)."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
    summary_only: tool.schema.boolean().default(false).describe("If true, return only counts and summary (no full node/edge arrays). Keeps response under 1KB."),
  },
  async execute(args, context) {
    return callPython("analyze_edit_region_tool", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_audit_workspace_tool = tool({
  description: "Consolidated workspace audit: dead code, large functions, and dependency cycles. Runs multiple quality checks in a single call and returns a health score. Equivalent to running refactor_tool(dead_code) + find_large_functions_tool + cycle detection in one shot. Use before merging a PR.",
  args: {
    include_dead_code: tool.schema.boolean().default(true).describe("Detect unreferenced functions/classes."),
    include_large_functions: tool.schema.boolean().default(true).describe("Find oversized functions."),
    include_cycles: tool.schema.boolean().default(true).describe("Detect import/call cycles."),
    min_lines: tool.schema.number().default(50).describe("Minimum lines to flag a function as large."),
    file_pattern: tool.schema.string().optional().describe("Filter dead code / large functions by file path substring."),
    exclude_paths: tool.schema.array(tool.schema.string()).optional().describe("List of path substrings to exclude (e.g. ['vscode', 'test'])."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("audit_workspace_tool", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_trace_dataflow_tool = tool({
  description: "Trace data propagation from a source symbol to a sink (or all reachable nodes). Performs a forward BFS from source over CALLS and IMPORTS_FROM edges. Use to answer: \"Can user-supplied data from parse_request reach execute_query?\"",
  args: {
    source: tool.schema.string().describe("Qualified name or plain name of the source symbol."),
    sink: tool.schema.string().optional().describe("Optional qualified name or plain name of the target symbol. If omitted, returns all symbols reachable from source."),
    max_depth: tool.schema.number().default(6).describe("Maximum BFS hops."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("trace_dataflow_tool", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_export_scip_tool = tool({
  description: "Export the code knowledge graph to a SCIP-compatible JSON document. Serialises all nodes and edges to the SCIP open standard (JSON subset) so the graph can be consumed by external tools or re-imported elsewhere.",
  args: {
    output_path: tool.schema.string().optional().describe("Destination file path. Defaults to .code-review-graph/export.scip.json inside the repo root."),
    file_pattern: tool.schema.string().optional().describe("Restrict export to nodes whose file path contains this substring. Omit to export the entire graph."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("export_scip_tool", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_import_scip_tool = tool({
  description: "Import a SCIP JSON document into the code knowledge graph. Reads a .scip.json file (produced by export_scip_tool or compatible tools) and upserts all symbols and relationships into the graph store.",
  args: {
    scip_path: tool.schema.string().describe("Path to the .scip.json file (absolute or relative to repo_root)."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("import_scip_tool", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

// ---------------------------------------------------------------------------
// Task DAG tools (39)
// ---------------------------------------------------------------------------

export const crg_task_get_active_root = tool({
  description: "Return the single open root task (or null if pipeline is idle). [BRAINSTORM] Single-pipeline discipline: at most one root task may be open at a time. Use this to check if a pipeline is running before calling task_roadmap, or to orient quickly at the start of a session.",
  args: {
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("task_get_active_root", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_task_create = tool({
  description: "Create one or more tasks under a common parent. [BRAINSTORM] Always pass a list — even for a single task. Supports inline edges for atomic decomposition + dependency wiring in one call.",
  args: {
    tasks: tool.schema.array(tool.schema.object({ title: tool.schema.string(), description: tool.schema.string().optional() })).describe("List of task dicts — each with title (required), description (optional)."),
    parent_id: tool.schema.string().optional().describe("Shared parent task ID. Omit to create root task(s)."),
    edges: tool.schema.array(tool.schema.object({ from: tool.schema.number(), to: tool.schema.number(), type: tool.schema.string().optional() })).optional().describe("Inline edge list. from/to are 0-based indices into tasks. type defaults to \"depends_on\"."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("task_create", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_task_update = tool({
  description: "Update fields of an existing task. [BRAINSTORM] Only supplied (non-null) fields are changed. Valid status: draft | refined | ready | in_progress | done | archived.",
  args: {
    task_id: tool.schema.string().describe("Task ID to update."),
    title: tool.schema.string().optional().describe("New title."),
    description: tool.schema.string().optional().describe("New description."),
    status: tool.schema.string().optional().describe("New status: draft | refined | ready | in_progress | done | archived."),
    spec: tool.schema.string().optional().describe("Full coder specification."),
    acceptance_criteria: tool.schema.string().optional().describe("Verification criteria."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("task_update", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_task_edit = tool({
  description: "Surgically edit a text field of a task without rewriting the whole field. [BRAINSTORM] Two modes: search/replace (supply search + replace), or line range (supply line_start + line_end + content). Editable fields: description | spec | acceptance_criteria.",
  args: {
    task_id: tool.schema.string().describe("Task to edit."),
    field: tool.schema.string().describe("Field to edit: description | spec | acceptance_criteria."),
    search: tool.schema.string().optional().describe("String to find (search/replace mode)."),
    replace: tool.schema.string().optional().describe("Replacement (search/replace mode)."),
    line_start: tool.schema.number().optional().describe("First line (line range mode, 1-indexed)."),
    line_end: tool.schema.number().optional().describe("Last line (line range mode, inclusive)."),
    content: tool.schema.string().optional().describe("New content (line range mode)."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("task_edit", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_task_delete = tool({
  description: "Delete a task permanently. [BRAINSTORM] Set cascade=true to also delete all subtasks. To preserve history, use task_archive instead.",
  args: {
    task_id: tool.schema.string().describe("Task to delete."),
    cascade: tool.schema.boolean().default(false).describe("If true, delete all subtasks recursively."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("task_delete", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_task_get = tool({
  description: "Get a task by ID. [BRAINSTORM] Returns all fields of the task row.",
  args: {
    task_id: tool.schema.string().describe("Task ID to retrieve."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("task_get", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_task_list = tool({
  description: "List tasks with optional filters. [BRAINSTORM] Returns matching tasks sorted by creation time.",
  args: {
    parent_id: tool.schema.string().optional().describe("Return only direct children of this task."),
    status: tool.schema.string().optional().describe("Filter by status."),
    root_only: tool.schema.boolean().default(false).describe("If true, return only top-level tasks."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("task_list", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_task_move = tool({
  description: "Move one or more tasks to a new parent (or promote them to root). [BRAINSTORM] Fails if the move would create a hierarchy cycle. Always pass a list — even for a single task.",
  args: {
    task_ids: tool.schema.array(tool.schema.string()).describe("List of task IDs to move."),
    new_parent_id: tool.schema.string().optional().describe("Shared new parent task ID. Pass null to make them root tasks."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("task_move", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_task_search = tool({
  description: "Keyword search within a task subtree. [BRAINSTORM] Searches title, description, and spec (case-insensitive).",
  args: {
    root_task_id: tool.schema.string().describe("Root of the subtree to search."),
    query: tool.schema.string().describe("Search string."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("task_search", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_task_archive = tool({
  description: "Archive one or more tasks (preserves history, unlike task_delete). [BRAINSTORM] Sets status to 'archived' with a recorded reason. Always pass a list — even for a single task.",
  args: {
    task_ids: tool.schema.array(tool.schema.string()).describe("List of task IDs to archive."),
    reason: tool.schema.string().describe("Why these tasks are being archived (required)."),
    cascade: tool.schema.boolean().default(true).describe("If true (default), archive all subtasks of each task too."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("task_archive", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_task_add_edge = tool({
  description: "Add one or more directed edges between tasks. [BRAINSTORM] Edge types: depends_on | blocks | shares_context | conflicts_with | informs. Cycle detection enforced for depends_on/blocks. Always pass a list — even for a single edge.",
  args: {
    edges: tool.schema.array(tool.schema.object({ source_id: tool.schema.string(), target_id: tool.schema.string(), edge_type: tool.schema.string().optional(), description: tool.schema.string().optional() })).describe("List of edge dicts — each with source_id, target_id, and optional edge_type and description."),
    edge_type: tool.schema.string().optional().describe("Default edge type for items lacking their own edge_type."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("task_add_edge", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_task_remove_edge = tool({
  description: "Remove an edge between two tasks. [BRAINSTORM] Raises NOT_FOUND if the edge does not exist.",
  args: {
    source_id: tool.schema.string().describe("Source task ID."),
    target_id: tool.schema.string().describe("Target task ID."),
    edge_type: tool.schema.string().describe("Type of edge to remove."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("task_remove_edge", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_task_get_dag = tool({
  description: "Get the full DAG rooted at a task. [BRAINSTORM] Returns all tasks in the subtree plus all edges between them.",
  args: {
    root_task_id: tool.schema.string().describe("Root of the DAG to retrieve."),
    compact: tool.schema.boolean().default(false).describe("If true, return only {id, title, status, depth, parent_id} per node."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("task_get_dag", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_task_topological_sort = tool({
  description: "Topologically sort leaf tasks by their depends_on relationships. [BRAINSTORM] Returns leaf tasks in dependency order (dependencies first).",
  args: {
    root_task_id: tool.schema.string().describe("Root of the subtree to sort."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("task_topological_sort", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_task_link_code = tool({
  description: "Link a task to one or many code graph nodes. [BRAINSTORM] Associates a task with code entities (functions, classes, files). Always pass a list — even for a single node. ref_type values: modifies | creates | deletes | reads | tests.",
  args: {
    task_id: tool.schema.string().describe("Task ID."),
    links: tool.schema.array(tool.schema.object({ ref_type: tool.schema.string(), code_node_id: tool.schema.number().optional(), qualified_name: tool.schema.string().optional(), description: tool.schema.string().optional() })).describe("List of dicts — each with ref_type and one of code_node_id / qualified_name."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("task_link_code", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_task_unlink_code = tool({
  description: "Remove all code refs between a task and a code node. [BRAINSTORM] Removes the association regardless of ref_type.",
  args: {
    task_id: tool.schema.string().describe("Task ID."),
    code_node_id: tool.schema.number().describe("Integer ID of the code node."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("task_unlink_code", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_task_get_code_refs = tool({
  description: "Get all code nodes linked to a task. [BRAINSTORM] Returns code nodes with metadata (name, file, lines).",
  args: {
    task_id: tool.schema.string().describe("Task ID."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("task_get_code_refs", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_task_find_by_code_node = tool({
  description: "Find all tasks that reference a given code node. [BRAINSTORM] Useful for understanding which tasks touch a specific function.",
  args: {
    code_node_id: tool.schema.number().describe("Integer ID of the code node."),
    open_only: tool.schema.boolean().default(true).describe("If true (default), exclude archived and done tasks."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("task_find_by_code_node", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_task_suggest_code_links = tool({
  description: "Suggest code nodes to link to a task based on keyword extraction. [BRAINSTORM] Extracts keywords from task title and description, searches the code graph, and returns ranked candidates. Already-linked nodes are excluded. Does NOT create links — returns candidates for review.",
  args: {
    task_id: tool.schema.string().describe("Task ID."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
    limit: tool.schema.number().default(20).describe("Maximum number of results to return."),
  },
  async execute(args, context) {
    return callPython("task_suggest_code_links", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_task_find_conflicts = tool({
  description: "Find conflicting leaf tasks whose code refs intersect. [BRAINSTORM] Algorithmically detects tasks that touch the same code nodes. Run this after decomposing tasks to catch coordination issues early.",
  args: {
    root_task_id: tool.schema.string().describe("Root of the subtree to analyze."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("task_find_conflicts", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_task_check_isolation = tool({
  description: "Check how isolated a leaf task is from the rest of the codebase. [BRAINSTORM] Score = internal / (internal + external). Low score (<0.5) suggests the task may be too coupled and should be split.",
  args: {
    task_id: tool.schema.string().describe("Task ID to check."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("task_check_isolation", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_task_blast_radius = tool({
  description: "Compute the code graph blast radius of a task. [BRAINSTORM] Shows direct nodes, affected nodes (up to depth hops), uncovered nodes (risk zones), and coverage ratio.",
  args: {
    task_id: tool.schema.string().describe("Task ID to analyze."),
    depth: tool.schema.number().default(2).describe("BFS depth."),
    include_affected_nodes: tool.schema.boolean().default(false).describe("If true, include full affected_nodes list. Default false returns only affected_nodes_count (scalar)."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("task_blast_radius", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_task_execution_order = tool({
  description: "Compute parallelism-aware execution order for leaf tasks. [BRAINSTORM] Groups leaf tasks into levels. Tasks in the same level can run in parallel. Based on depends_on edges.",
  args: {
    root_task_id: tool.schema.string().describe("Root of the subtree to analyze."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("task_execution_order", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_task_validate = tool({
  description: "Validate the task DAG before handing it off to a coder. [BRAINSTORM] Gate-check: runs 9 algorithmic checks covering cycles, dependencies, code refs, open questions, assumptions, contracts, acceptance criteria, descriptions, and leaf-vs-parent attachment. If root_task_id is omitted, the active root task is auto-detected.",
  args: {
    root_task_id: tool.schema.string().optional().describe("Root of the DAG to validate. Auto-detected if omitted."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("task_validate", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_task_export = tool({
  description: "Export full task context — primary entry point for LLM consumption. [BRAINSTORM] Single call that assembles all layers of a task into one dict. If task_id is omitted, the active root task is auto-detected. Use this to hand off a task to a coder, review progress, or let LLM reason about what a task requires.",
  args: {
    task_id: tool.schema.string().optional().describe("Task ID to export. Auto-detects active root if omitted."),
    include_analysis: tool.schema.boolean().default(false).describe("Add isolation, sibling conflicts, pipeline_state."),
    include_source: tool.schema.boolean().default(false).describe("Attach line-range info to code_refs so LLM knows exactly where to read."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("task_export", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_note_add = tool({
  description: "Add one or more brainstorm notes to a task. [BRAINSTORM] Types: decision | question | assumption | constraint | risk. Always pass a list — even for a single note.",
  args: {
    task_id: tool.schema.string().describe("Task to attach the notes to."),
    notes: tool.schema.array(tool.schema.object({ note_type: tool.schema.string(), content: tool.schema.string(), status: tool.schema.string().optional(), resolution: tool.schema.string().optional(), rationale: tool.schema.string().optional(), alternatives: tool.schema.string().optional() })).describe("List of note dicts. Each requires note_type and content."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("note_add", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_note_update = tool({
  description: "Update an existing note. [BRAINSTORM] Use to resolve open questions or update rationale.",
  args: {
    note_id: tool.schema.string().describe("Note ID to update."),
    status: tool.schema.string().optional().describe("New status: open | resolved | rejected | deferred."),
    resolution: tool.schema.string().optional().describe("Answer or decision text."),
    rationale: tool.schema.string().optional().describe("Reasoning."),
    content: tool.schema.string().optional().describe("Updated note text."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("note_update", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_note_list = tool({
  description: "List notes for a task. [BRAINSTORM] With include_parent=true (default), includes notes from all ancestor tasks — the full decision history for this task. With include_children=true, includes notes from all descendants.",
  args: {
    task_id: tool.schema.string().describe("Task ID."),
    note_type: tool.schema.string().optional().describe("Filter by type: decision | question | assumption | constraint | risk."),
    status: tool.schema.string().optional().describe("Filter by status."),
    include_parent: tool.schema.boolean().default(true).describe("Include ancestor notes."),
    include_children: tool.schema.boolean().default(false).describe("Include descendant notes — search entire subtree."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("note_list", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_note_delete = tool({
  description: "Delete a note by ID. [BRAINSTORM] Permanently removes the note.",
  args: {
    note_id: tool.schema.string().describe("Note ID to delete."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("note_delete", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_contract_add = tool({
  description: "Create a contract / design entity scoped to a brainstorm subtree. [BRAINSTORM] Represents a data structure (OAuthToken), interface (IUserRepo), API spec, or schema change. Types: interface | api | schema | event | data_format.",
  args: {
    name: tool.schema.string().describe("Short identifier, e.g. \"OAuthToken\", \"UserRepo.save()\"."),
    contract_type: tool.schema.string().describe("Contract type: interface | api | schema | event | data_format."),
    definition: tool.schema.string().describe("Human-readable spec (TypeScript-like, JSON Schema, etc.)."),
    scope_task_id: tool.schema.string().describe("Root task that owns this brainstorm scope."),
    provider_task_id: tool.schema.string().optional().describe("Task that will implement this contract (optional)."),
    consumer_task_ids: tool.schema.array(tool.schema.string()).optional().describe("Tasks that will use this contract (optional)."),
    code_node_id: tool.schema.number().optional().describe("Integer node id from semantic_search_nodes_tool results."),
    qualified_name: tool.schema.string().optional().describe("Qualified name string from search results or code_refs."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("contract_add", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_contract_link = tool({
  description: "Attach a task to a contract as provider or consumer. [BRAINSTORM] Use after task decomposition when new subtasks need to participate in an existing design entity/contract.",
  args: {
    contract_id: tool.schema.string().describe("Contract to link to."),
    task_id: tool.schema.string().describe("Task to attach."),
    role: tool.schema.string().describe("'provider' or 'consumer'."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("contract_link", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_contract_unlink = tool({
  description: "Remove all links between a task and a contract. [BRAINSTORM] Use when a task no longer owns or uses a design entity.",
  args: {
    contract_id: tool.schema.string().describe("Contract to unlink from."),
    task_id: tool.schema.string().describe("Task to detach."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("contract_unlink", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_contract_update = tool({
  description: "Update a contract's name, definition, status, or code_node reference. [BRAINSTORM] Lifecycle: proposed -> agreed -> implemented -> verified. Provide EITHER code_node_id OR qualified_name to link to an existing code node.",
  args: {
    contract_id: tool.schema.string().describe("Contract ID to update."),
    name: tool.schema.string().optional().describe("Updated name."),
    definition: tool.schema.string().optional().describe("Updated contract definition."),
    status: tool.schema.string().optional().describe("New status: proposed | agreed | implemented | verified | void."),
    code_node_id: tool.schema.number().optional().describe("Integer node id from semantic_search_nodes_tool results."),
    qualified_name: tool.schema.string().optional().describe("Qualified name string from search results or code_refs."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("contract_update", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_contract_list = tool({
  description: "List contracts matching given filters. [BRAINSTORM] Filters can be combined: scope_task_id (all contracts in a subtree), task_id (contracts where this task participates), name (partial name match).",
  args: {
    scope_task_id: tool.schema.string().optional().describe("Root of the brainstorm scope."),
    task_id: tool.schema.string().optional().describe("Task participating in the contracts."),
    name: tool.schema.string().optional().describe("Partial name to search for."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("contract_list", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_task_roadmap = tool({
  description: "Get an aggregated progress snapshot for the entire task tree. [BRAINSTORM] The primary orientation tool — call this at the start of every session. Returns progress counts, execution phases, contract statuses, and an attention block showing what needs action right now. Auto-detects active root if root_task_id is omitted.",
  args: {
    root_task_id: tool.schema.string().optional().describe("Root task ID. Auto-detected if omitted."),
    include_archived: tool.schema.boolean().default(false).describe("If true, include archived tasks in phases and add an archived section."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("task_roadmap", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_task_roadmap_diff = tool({
  description: "Get what changed in the task tree since a given Unix timestamp. [BRAINSTORM] Use at the start of a resumed session to quickly understand what happened since you last worked on this brainstorm.",
  args: {
    root_task_id: tool.schema.string().describe("Root task ID."),
    since_timestamp: tool.schema.number().describe("Unix timestamp (float). Changes after this are returned."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("task_roadmap_diff", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_task_find_for_impact = tool({
  description: "Find open tasks whose code refs overlap the blast radius of changed files. [BRAINSTORM] Cross-query bridging code graph and task DAG. Use before merging: \"Are there open tasks for code I'm about to change?\"",
  args: {
    file_paths: tool.schema.array(tool.schema.string()).describe("Changed file paths — relative, absolute, or filename-only."),
    root_task_id: tool.schema.string().optional().describe("Restrict results to this task subtree. Omit for all tasks."),
    max_depth: tool.schema.number().default(2).describe("BFS hops into code graph."),
    open_only: tool.schema.boolean().default(true).describe("If true (default), only return non-done/archived tasks."),
    include_node_details: tool.schema.boolean().default(false).describe("If true, include full node metadata. Default false returns only counts."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("task_find_for_impact", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_task_suggest_contracts = tool({
  description: "Suggest contracts between tasks with implicit code-level dependencies. [BRAINSTORM] Detects pairs of leaf tasks whose code refs are connected via code graph edges (calls/imports) but have no explicit task_edge or contract between them.",
  args: {
    root_task_id: tool.schema.string().optional().describe("Root of the subtree to analyze. Auto-detected if omitted (uses active root task)."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("task_suggest_contracts", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})

export const crg_task_check_rollup = tool({
  description: "Check whether a task's parent (and ancestors) can change status. [BRAINSTORM] After completing or archiving a task, call this to find out if the parent task can now be closed or archived as well. Does NOT modify any data — purely analytical.",
  args: {
    task_id: tool.schema.string().describe("The task that was just updated (completed/archived)."),
    repo_root: tool.schema.string().optional().describe("Repository root path. Auto-detected if omitted."),
  },
  async execute(args, context) {
    return callPython("task_check_rollup", { ...args, repo_root: args.repo_root ?? context.worktree }, context.worktree)
  },
})
