---
name: trace-impact
description: Trace data flow, analyze edit regions before coding, and search across multiple registered repositories. Use before editing to understand blast radius at line-level precision.
argument-hint: "[function name, file:line range, or repo query]"
---

# Trace Impact

Precision impact analysis — from line-level edit regions to multi-repository cross-search. Use before making changes to understand exactly what you'll affect.

## Line-Level Edit Region Analysis

**Use this before editing any function** to see which graph nodes overlap the target lines and who calls into the region:

```
analyze_edit_region_tool(
    file_path="code_review_graph/tasks.py",
    line_start=100,
    line_end=140
)
# Returns:
#   overlapping_nodes — functions/classes in this line range
#   external_callers  — who calls into this region from outside
#   downstream        — what this region calls
```

This is more precise than `get_impact_radius_tool` (which operates at file level) — it shows blast radius for a specific line range rather than a whole file.

**Typical workflow:**
```
# Before editing lines 100-140 of tasks.py:
analyze_edit_region_tool("code_review_graph/tasks.py", 100, 140)
→ see 3 overlapping functions, 12 external callers
→ decide: is this change safe? who needs to know?
```

## Dataflow Tracing

Trace how data propagates from a source to a sink:

```
# Can user input from parse_request reach execute_query?
trace_dataflow_tool(
    source="parse_request",
    sink="execute_query",
    max_depth=6
)
# Returns: path[] if reachable, or "not reachable"

# All symbols reachable from a function (no sink = full reachability)
trace_dataflow_tool(source="hybrid_search")
# Returns: all functions/classes reachable via CALLS + IMPORTS_FROM edges
```

**Use cases:**
- Security: can untrusted input reach a dangerous sink (SQL, shell, file write)?
- Debugging: does data from function A ever reach function B?
- Architecture: what is the full downstream blast of changing function X?

## Get Impact Radius (File-Level)

```
get_impact_radius_tool(changed_files=["code_review_graph/tasks.py"])
get_impact_radius_tool(base="HEAD~1")     # auto-detect from git diff
get_impact_radius_tool(max_depth=3)       # deeper traversal
```

Returns: affected files, affected functions, impacted communities.

## Multi-Repository Cross-Search

First, register repositories:
```bash
code-review-graph register /path/to/repo-a
code-review-graph register /path/to/repo-b
code-review-graph repos    # list registered repos
```

Then search across all registered repos simultaneously:
```
list_repos_tool()                              # see all registered repos + metadata
cross_repo_search_tool(query="AuthService")    # search across all repos
cross_repo_search_tool(query="create_user", kind="Function", limit=10)
```

Results include `repo_path` and `repo_id` so you can distinguish same-named functions from different projects. Useful for:
- Finding shared patterns across microservices
- Identifying where an interface is implemented in a monorepo
- Cross-team API compatibility checks

## Affected Flows (Execution Path Impact)

```
get_affected_flows_tool(changed_files=["auth.py"])   # filename, relative, or absolute
get_affected_flows_tool(base="HEAD~1")               # auto-detect from git
```

Returns: which execution flows (entry point → end) pass through the changed code. Sorted by criticality — highest-impact flows first.

## Combining Tools for Pre-Edit Safety Check

```
# Full pre-edit workflow for code_review_graph/tasks.py lines 200-250:

# 1. Line-level precision
analyze_edit_region_tool("code_review_graph/tasks.py", 200, 250)

# 2. File-level blast radius
get_impact_radius_tool(changed_files=["code_review_graph/tasks.py"])

# 3. Data flow from changed function
trace_dataflow_tool(source="update_task", max_depth=4)

# 4. Execution flows that pass through
get_affected_flows_tool(changed_files=["code_review_graph/tasks.py"])

# 5. Open tasks that cover this area
task_find_for_impact(file_paths=["code_review_graph/tasks.py"])
```

## Tips

- `analyze_edit_region_tool` uses 1-indexed line numbers matching your editor
- `trace_dataflow_tool` traverses CALLS and IMPORTS_FROM edges — not data types
- For `cross_repo_search_tool` to work, repos must first be registered via CLI: `code-review-graph register <path>`
- `task_find_for_impact` bridges code graph and task DAG — shows open tasks for the same blast radius
- Use `trace_dataflow_tool(source=X, sink=Y)` for security reviews to check injection paths
