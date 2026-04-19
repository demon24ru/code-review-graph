# LLM-OPTIMIZED REFERENCE -- code-review-graph v2.2.0

Claude Code: Read ONLY the exact `<section>` you need. Never load the whole file.

<section name="usage">
Quick install: pip install code-review-graph
Then: code-review-graph install && code-review-graph build
First run: /code-review-graph:build-graph
After that use only delta/pr commands.
For v2 features: detect_changes_tool for risk-scored reviews, list_flows_tool for execution paths, list_communities_tool for architecture.
</section>

<section name="review-delta">
Always call get_impact_radius on changed files first.
Then get_review_context (depth=2).
Or use detect_changes_tool for risk-scored, priority-ordered review guidance.
Generate review using ONLY changed nodes + 2-hop neighbors.
Target: <800 tokens total context.
</section>

<section name="review-pr">
Fetch PR diff -> detect_changes_tool -> get_affected_flows_tool -> structured review with blast-radius table and risk scores.
Never include full files unless explicitly asked.
</section>

<section name="commands">
Code-graph MCP tools (28): build_or_update_graph_tool, get_impact_radius_tool, query_graph_tool, get_review_context_tool, semantic_search_nodes_tool, embed_graph_tool, list_graph_stats_tool, get_docs_section_tool, find_large_functions_tool, list_flows_tool, get_flow_tool, get_affected_flows_tool, list_communities_tool, get_community_tool, get_architecture_overview_tool, detect_changes_tool, refactor_tool, apply_refactor_tool, generate_wiki_tool, get_wiki_page_tool, list_repos_tool, cross_repo_search_tool, find_files_by_pattern_tool, analyze_edit_region_tool, audit_workspace_tool, trace_dataflow_tool, export_scip_tool, import_scip_tool
Task DAG MCP tools (35): task_create, task_update, task_edit, task_delete, task_get, task_list, task_move, task_search, task_archive, task_add_edge, task_remove_edge, task_get_edges, task_get_dag, task_topological_sort, task_link_code, task_unlink_code, task_get_code_refs, task_find_by_code_node, task_suggest_code_links, task_find_conflicts, task_check_isolation, task_blast_radius, task_execution_order, task_validate, task_build_context, task_export, note_add, note_update, note_list, note_delete, contract_add, contract_update, contract_list, task_roadmap, task_roadmap_diff
MCP prompts (5): review_changes, architecture_map, debug_issue, onboard_developer, pre_merge_check
Skills: build-graph, review-delta, review-pr
CLI: code-review-graph [install|init|build|update|status|watch|visualize|serve|wiki|detect-changes|register|unregister|repos|eval]
</section>

<section name="brainstorm">
Task DAG: decompose features into validated, isolated subtasks linked to code nodes.
Start: task_create(title, description) → root_id
Decompose: task_create(title, parent_id=root_id) × N subtasks
Dependencies: task_add_edge(src, tgt, "depends_on"|"blocks"|"shares_context"|"conflicts_with"|"informs")
Link code: semantic_search_nodes_tool("ClassName") → id → task_link_code(task_id, code_node_id, ref_type)
Notes: note_add(task_id, type, content) — types: decision|question|assumption|constraint|risk
Contracts: contract_add(provider_id, consumer_id, type, definition) — types: interface|api|schema|event|data_format
Analysis: task_find_conflicts → task_check_isolation → task_execution_order
Validate: task_validate(root_id) → must have errors=[] before handoff to coder
Context: task_build_context(task_id) → full spec | task_export(task_id) → flat handoff
Orient: task_roadmap(root_id) → progress + phases + attention | task_roadmap_diff(root_id, ts) → resume
Full workflow example: see docs/TASK-DAG.md
</section>

<section name="legal">
MIT license. 100% local. No telemetry. DB file: .code-review-graph/graph.db
</section>

<section name="watch">
Run: code-review-graph watch (auto-updates graph on file save via watchdog)
Or use PostToolUse (Write|Edit|Bash) hooks for automatic background updates.
</section>

<section name="embeddings">
Optional: pip install code-review-graph[embeddings]
Then call embed_graph_tool to compute vectors.
semantic_search_nodes_tool auto-uses vectors when available, falls back to keyword + FTS5.
Providers: Local (all-MiniLM-L6-v2, 384-dim), Google Gemini, MiniMax (embo-01, 1536-dim).
Configure via CRG_EMBEDDING_MODEL env var or model parameter.
</section>

<section name="languages">
Supported (18): Python, TypeScript/TSX, JavaScript, Vue, Go, Rust, Java, Scala, C#, Ruby, Kotlin, Swift, PHP, Solidity, C/C++, Dart, R, Perl
Parser: Tree-sitter via tree-sitter-language-pack
</section>

<section name="troubleshooting">
DB lock: SQLite WAL mode, auto-recovers. Only one build at a time.
Large repos: First build 30-60s. Incremental <2s. Add patterns to .code-review-graphignore.
Stale graph: Run /code-review-graph:build-graph manually.
Missing nodes: Check language support + ignore patterns. Use full_rebuild=True.
Windows/WSL: Use forward slashes in paths. Ensure uv is on PATH in WSL.
</section>

**Instruction to Claude Code (always follow):**
When user asks anything about "code-review-graph", "how to use", "commands", "review-delta", etc.:
1. Call get_docs_section_tool with the exact section name.
2. Use ONLY that content + current graph state.
3. Never include full docs or source code in your reasoning.
This guarantees 90%+ token savings.
