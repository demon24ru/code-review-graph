---
name: refactor-code
description: Safe, graph-powered refactoring with dead code detection, rename preview, workspace audit, and SCIP export. Always preview before applying.
argument-hint: "[rename target, dead code scan, or audit]"
---

# Refactor Code

Use the knowledge graph to plan and execute refactoring safely — with full blast-radius awareness before making any changes.

## Workspace Audit (Start Here)

Run a comprehensive health check before any refactoring:

```
audit_workspace_tool()
# Returns: health_score, dead_code[], large_functions[], import_cycles[]
# Equivalent to running dead_code + find_large_functions + cycle detection together
```

**Interpret results:**
- `dead_code` — functions/classes with no callers, importers, or tests (safe candidates for removal)
- `large_functions` — functions > 50 lines (decomposition candidates)
- `cycles` — circular imports/calls (architecture issues to fix)

Filter by area:
```
audit_workspace_tool(file_pattern="src/auth/", min_lines=100)
audit_workspace_tool(include_dead_code=True, include_large_functions=False, include_cycles=False)
audit_workspace_tool(exclude_paths=["vscode", "generated/"])  # exclude paths from dead code + large functions
```

## Find Dead Code

```
refactor_tool(mode="dead_code")                    # all unreferenced code
refactor_tool(mode="dead_code", kind="Function")   # only functions
refactor_tool(mode="dead_code", file_pattern="code_review_graph/")
refactor_tool(mode="dead_code", exclude_paths=["vscode/", "generated/"])  # exclude paths
```

Dead code = no callers + no tests + no importers + not an entry point.
Before deleting: verify with `query_graph_tool(pattern="callers_of", target=name)` to confirm 0 callers.

## Get Refactoring Suggestions

```
refactor_tool(mode="suggest")
# Returns: misplaced functions (wrong community), dead code, large decomposition targets
```

Suggestions are community-driven: if a function is used predominantly by community B but lives in community A, it's a candidate for relocation.

## Safe Rename (Preview → Apply)

**Always preview first:**
```
refactor_tool(mode="rename", old_name="create_task", new_name="make_task")
# Returns: refactor_id, edits[] (high-confidence), possible_misses[] (docstrings/comments — review manually)
# Preview expires in 10 minutes
```

**Review the edit list carefully**, then apply:
```
apply_refactor_tool(refactor_id="ref_abc123")
# Applies exact string replacements to all affected files
```

After applying:
```
build_or_update_graph_tool()                   # update graph to reflect rename
detect_changes_tool(summary_only=True)         # quick impact check (counts only)
detect_changes_tool()                          # full impact if needed
```

## Find Large Functions (Decomposition Targets)

```
find_large_functions_tool(min_lines=80, kind="Function")
find_large_functions_tool(min_lines=200, kind="File")
find_large_functions_tool(min_lines=50, file_path_pattern="controllers/")
```

For each large function, trace its responsibilities:
```
query_graph_tool(pattern="callees_of", target="large_function_name")
# Group callees by domain → natural decomposition boundaries
```

## SCIP Export/Import (Cross-Tool Interop)

Export the entire code graph in SCIP format for use with external tools:
```
export_scip_tool()                              # exports to .code-review-graph/export.scip.json
export_scip_tool(file_pattern="src/auth/")      # partial export
export_scip_tool(output_path="custom/path.json")
```

Import a SCIP file (e.g., from another tool or CI pipeline):
```
import_scip_tool(scip_path=".code-review-graph/export.scip.json")
# Upserts all symbols and relationships into the graph store
```

## Safety Checklist Before Any Refactor

1. `audit_workspace_tool()` — understand current health
2. `get_impact_radius_tool(summary_only=True)` — gauge blast radius (counts); omit `summary_only` for full details
3. `query_graph_tool(pattern="tests_for", target=name)` — test coverage
4. `refactor_tool(mode="rename", ...)` — preview all changes
5. Review edit list — confirm no unintended replacements
6. `apply_refactor_tool(refactor_id)` — apply
7. `build_or_update_graph_tool()` — refresh graph
8. `detect_changes_tool()` — verify final impact

## Tips

- Never skip the preview step — `refactor_tool` is safe, `apply_refactor_tool` is irreversible
- Dead code scan can have false positives for dynamic dispatch / plugin systems — verify manually
- Large functions with high caller counts are high-risk to decompose — check `callers_of` first
- SCIP export is useful for sharing graph state between CI runs or team members
- `refactor_tool(mode="suggest")` is a great starting point for a new codebase audit
