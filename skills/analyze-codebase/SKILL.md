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
```

The architecture overview groups code into communities (clusters of tightly related code) and identifies cross-community coupling. High coupling between communities is a design smell.

## Step 2: Explore Communities

```
list_communities_tool(sort_by="size")        # all communities sorted by size
list_communities_tool(sort_by="cohesion")    # tightest clusters first
get_community_tool(community_name="auth")    # details + member list
# If multiple communities share the name → status:"ambiguous" + matches:[{id,name}...]
# Use community_id= to select unambiguously
```

Communities are detected automatically via the Leiden algorithm. Each community represents a logical module boundary — useful for understanding ownership and change impact.

## Step 3: Find Code by Keyword or Structure

```
# Semantic / keyword search — multi-word query = FTS5 OR (one round-trip)
semantic_search_nodes_tool(query="authentication", kind="Function", limit=10)
semantic_search_nodes_tool(query="GraphStore",     kind="Class")
semantic_search_nodes_tool(query="migrations",     kind="File")

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
query_graph_tool(pattern="callers_of",   target="create_task")       # who calls this
query_graph_tool(pattern="callees_of",   target="export_task")       # what this calls
query_graph_tool(pattern="imports_of",   target="code_review_graph/tasks.py")
query_graph_tool(pattern="importers_of", target="code_review_graph/graph.py")
query_graph_tool(pattern="children_of",  target="code_review_graph/tasks.py")  # file contents
query_graph_tool(pattern="tests_for",    target="hybrid_search")     # test coverage
query_graph_tool(pattern="inheritors_of",target="GraphStore")        # subclasses
query_graph_tool(pattern="file_summary", target="code_review_graph/main.py")
```

All results include `id` and `qualified_name` for direct use in task/contract linking.

## Step 5: Understand Execution Flows

```
list_flows_tool(sort_by="criticality", limit=20)   # most critical entry points first
list_flows_tool(sort_by="depth")                   # deepest call chains
list_flows_tool(kind="Test")                       # test entry points only

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
audit_workspace_tool()    # dead code + large functions + cycles in one call
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
