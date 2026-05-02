---
name: analyze-codebase
description: Deep structural analysis of codebase using communities, flows, wiki, and embedding search. Understand architecture, execution paths, and module boundaries without reading files.
argument-hint: "[module name, feature area, or question]"
---

# Analyze Codebase

Use the knowledge graph for deep structural analysis — architecture, execution flows, community clustering, and cross-file relationships.

## Step 1: Orient with Stats and Architecture

```
list_graph_stats_tool()              # files, nodes, edges, languages, last updated
get_architecture_overview_tool()     # community map + coupling warnings
get_architecture_skeleton(level="containers")   # C4 skeleton — module map in 20 lines
get_project_standards()                          # project conventions from prompts/
```

The architecture overview groups code into communities (clusters of tightly related code) and identifies cross-community coupling. High coupling between communities is a design smell.

The architecture skeleton (`.code-review-graph/architecture.c4`) provides a persistent Mermaid C4 view of the codebase — containers (from communities) and components (from nodes). Read it first for a quick module map. If it doesn't exist yet, run `code-review-graph c4` to generate it.

Project standards (`prompts/` directory) define cross-project conventions: architecture patterns, coding style, tech stack. Use `get_project_standards()` to list available sections, then `get_project_standards(section="patterns")` to read a specific one.

## Step 2: Explore Communities

```
# exclude_tests=True is the DEFAULT for communities and architecture tools.
# Without it, ~40% of communities are test files and dominate the overview.
list_communities_tool(sort_by="size")                    # production communities (default)
list_communities_tool(sort_by="cohesion")                # tightest clusters first
list_communities_tool(exclude_tests=False)               # include test communities too
get_community_tool(community_name="auth")                # details + member list
# If multiple communities share the name → status:"ambiguous" + matches:[{id,name}...]
# Use community_id= to select unambiguously

get_architecture_overview_tool()                         # production coupling map (default)
get_architecture_overview_tool(exclude_tests=False)      # show test communities in coupling too
```

Communities are detected automatically via the Leiden algorithm. Each community represents a logical module boundary — useful for understanding ownership and change impact.

## Step 3: Find Code by Keyword or Structure

```
# Semantic / keyword search — multi-word query = FTS5 OR (one round-trip)
semantic_search_nodes_tool(query="authentication", kind="Function", limit=10)
semantic_search_nodes_tool(query="GraphStore",     kind="Class")
semantic_search_nodes_tool(query="migrations",     kind="File")

# exclude_tests=True is the DEFAULT — test helpers are filtered automatically.
# Pass exclude_tests=False only when you explicitly want test nodes in results.
semantic_search_nodes_tool(query="create_task")                          # production only (default)
semantic_search_nodes_tool(query="create_task", exclude_tests=False)     # include test helpers too

# Filter by language — useful in mixed-language repos (Python + TypeScript):
semantic_search_nodes_tool(query="build_graph", language="python")       # Python only

# Bulk multi-symbol lookup in one call:
semantic_search_nodes_tool(query="create_task add_task_edge move_task archive_task")

# Filter to a specific file:
semantic_search_nodes_tool(query="create", file_path="tasks.py")

# Returns: id, name, qualified_name, file_path, line_start, line_end, params, signature
# If single-token query matches 4+ nodes with the same name, response includes
# "disambiguation_note" suggesting to add file_path= to narrow results.
```

**`line_end` is critical for large codebases.** It tells you the exact boundary of a
function without reading the file. Use it to do a targeted `Read(offset=line_start,
limit=line_end-line_start+1)` only when you actually need the body — never read
the whole file just to find where a function ends. Reading large functions bloats
input context significantly while output context stays the same.

Use `id` or `qualified_name` from results directly in `task_link_code(task_id, links=[{ref_type, code_node_id|qualified_name}])` or `contract_add`.

```
# File and pattern search
find_files_by_pattern_tool(patterns=["*router*", "src/**/*.ts", "main.*"])
find_large_functions_tool(min_lines=80, kind="Function")   # decomposition targets
find_large_functions_tool(min_lines=200, kind="File")      # oversized files
```

## Step 4: Trace Relationships

```
query_graph_tool(pattern="callers_of",   target="create_task")                    # who calls this
query_graph_tool(pattern="callers_of",   target="create_task", exclude_tests=True) # production callers only
query_graph_tool(pattern="callers_of",   target="create_task", limit=20)           # cap large result sets
query_graph_tool(pattern="callees_of",   target="export_task")                    # what this calls
query_graph_tool(pattern="callees_of",   target="export_task", kind="Function")   # only Function callees
query_graph_tool(pattern="imports_of",   target="code_review_graph/tasks.py")
query_graph_tool(pattern="importers_of", target="code_review_graph/graph.py")
query_graph_tool(pattern="children_of",  target="code_review_graph/tasks.py")  # file contents
query_graph_tool(pattern="tests_for",    target="hybrid_search")     # test coverage
query_graph_tool(pattern="inheritors_of",target="GraphStore")        # subclasses
query_graph_tool(pattern="file_summary", target="code_review_graph/main.py")
```

All results include `id` and `qualified_name` for direct use in task/contract linking.

`exclude_tests=True` (default) filters test callers/importers. Popular functions can have 100+ test callers — always use `limit=20` as a starting point and add `exclude_tests=True` to see only production call sites.

## Step 5: Understand Execution Flows

```
list_flows_tool(is_test=False, language="python", sort_by="depth", limit=20)
# ↑ RECOMMENDED for /design-feature: surfaces real user-facing entry points (deep call chains)
# sort_by="criticality" ranks by number of callers — helper functions outrank MCP handlers.
# sort_by="depth" ranks by call chain length — actual entry points (CLI, MCP) have deepest paths.
list_flows_tool(sort_by="criticality", limit=20)            # most called functions (often helpers)
list_flows_tool(kind="Test")                                # test entry points only
# Without is_test=False, test functions dominate by criticality score.
# Without language="python", TypeScript VS Code extension flows may appear.

get_flow_tool(flow_name="handle_request")          # full call path with line numbers
get_flow_tool(flow_id=42, include_source=True)     # with source snippets
```

Flows show complete call chains from entry points (HTTP handlers, CLI commands, tests) through the codebase. Use criticality score to prioritize review effort.

## Step 6: Generate and Browse Wiki

```
generate_wiki_tool()           # creates .code-review-graph/wiki/ markdown pages
get_wiki_page_tool(community_name="search")  # get specific community wiki page
```

Wiki pages describe each community: its purpose, key functions, dependencies, and architecture notes. Useful for onboarding and documentation.

## Step 7: Embed for Semantic Search (Optional)

```
embed_graph_tool()    # generate vector embeddings (requires sentence-transformers)
```

After embedding, `semantic_search_nodes_tool` uses vector similarity instead of keyword matching — much better results for fuzzy queries like "authentication flow" or "database connection".

## Patterns for Common Questions

**"What's the high-level architecture?"**
```
get_architecture_skeleton(level="containers")        # C4 containers view
→ get_architecture_skeleton(level="components:auth")  # drill into specific module
→ get_architecture_overview_tool()                    # coupling between communities
```

**"How does X work?"**
```
semantic_search_nodes_tool(query="X")
→ get_flow_tool(flow_name="X_entrypoint")
→ query_graph_tool(pattern="callees_of", target="X_main_function")
```

**"What does module Y do?"**
```
query_graph_tool(pattern="children_of", target="path/to/module.py")
→ get_community_tool(community_name="Y")
→ get_wiki_page_tool(community_name="Y")
```

**"Where are the largest/most complex functions?"**
```
find_large_functions_tool(min_lines=50, kind="Function")
audit_workspace_tool(limit=20)    # dead code + large functions + cycles; start with limit=20
# Full audit without limit can return 100KB+ — always use limit= for initial exploration
```

**"Is there circular dependency between A and B?"**
```
audit_workspace_tool(include_cycles=True, include_dead_code=False, include_large_functions=False)
```

**"What's the overall health of this codebase?"**
```
audit_workspace_tool()    # comprehensive: dead code, large functions, import cycles
→ get_architecture_overview_tool()   # coupling between communities
```

## Tool Selection: MCP vs Direct Tools

**Do NOT use MCP for everything.** Use the right tool for each job:

| Need | Best tool | Why |
|---|---|---|
| Find function declarations + signatures | `semantic_search_nodes_tool` | Returns params, line_end, id in one call |
| Find 3+ symbols at once | `semantic_search_nodes_tool(query="a b c")` | Single FTS5 OR query |
| Find all callers/usages of a symbol | `query_graph_tool(callers_of)` | Graph edge traversal |
| Search inside function bodies | **Grep / ripgrep** | MCP only indexes declarations, not bodies |
| Find a specific pattern/string in code | **Grep** | MCP cannot search body content |
| Find all places variable X is used | **LSP find_references** | Semantic, not text-based |
| Find where a value flows | `trace_dataflow_tool` | Graph traversal |
| Read a specific function body | `Read(offset=line_start, limit=line_end-line_start+1)` | Use line_end from MCP search first |

**MCP semantic_search ≈ `grep "^def "` + auto-read of signature lines.**
It does NOT replace grep for searching inside function bodies.

## Tips

- `list_graph_stats_tool` shows `last_updated` — if stale, run `build_or_update_graph_tool()`
- `query_graph_tool` results include `id` + `qualified_name` — use directly in task linking
- `find_files_by_pattern_tool` supports glob patterns: `**/*.py`, `!tests/**`
- Communities are automatically named by dominant file paths — search by partial name
- For new codebases, run `embed_graph_tool` once for much better semantic search quality
- Always use `line_end` from search results to scope `Read` calls — never read whole files to find function boundaries
- `list_communities_tool` and `get_architecture_overview_tool` default to `exclude_tests=True` — test communities are hidden unless you pass `exclude_tests=False`
- `semantic_search_nodes_tool` defaults to `exclude_tests=True` — test helpers are filtered automatically
- `query_graph_tool` with `exclude_tests=True` filters BOTH results AND edges — test caller edges are suppressed, not just test nodes in results
- `list_flows_tool(sort_by="criticality")` surfaces helper functions (many callers) not user-facing entry points — use `sort_by="depth"` for actual CLI/MCP entry points
- `get_architecture_skeleton()` is the fastest orientation tool — 20 lines of C4 DSL vs hundreds of nodes from `get_architecture_overview_tool`
- `get_project_standards()` without arguments lists available sections; with `section="patterns"` returns specific content
- The C4 skeleton has `[AUTO]` sections (from code graph) and `[FEATURE]` sections (from LLM design) — `designed_features` field in the response lists active design work
