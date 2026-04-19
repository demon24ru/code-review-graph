---
name: brainstorm-task
description: Plan and track implementation work using the Task DAG system. Create structured task trees linked to real code, enforce single-pipeline discipline, and generate handoff context for coders.
argument-hint: "[task title or description]"
---

# Brainstorm Task

Use the Task DAG system to plan implementation work with full traceability to the code graph.

## Single-Pipeline Discipline

**One root task at a time.** Before creating a new root task, call `task_get_active_root` to check if a pipeline is already running. Only create a new root task when the current pipeline is fully done or archived.

Workflow phases — strictly in order:
1. **Brainstorm** — full task tree with notes, contracts, code links
2. **Validate** — `task_validate` must pass (0 errors) before implementation
3. **Implement** — task by task, leaf-first, following `task_execution_order`
4. **Close** — mark done, `task_check_rollup` to propagate up the tree

## Phase 1: Create the Task Tree

```
task_get_active_root()          # confirm pipeline is idle
task_create(title, description) # root task — blocks until done/archived
task_create(title, parent_id)   # L1 feature groups
task_create(title, parent_id)   # L2 specific changes
task_create(title, parent_id)   # L3 leaf tasks (actually implementable)
```

Rules:
- Only **leaf tasks** (no children) get code refs and contract links
- Parent tasks are grouping containers — no direct code refs
- 3 levels max for clarity; decompose further only if `check_isolation` score < 0.5

## Phase 2: Add Notes and Design Decisions

```
note_add(task_id, note_type="decision",   content="...", status="resolved", resolution="...")
note_add(task_id, note_type="question",   content="...")   # open question
note_add(task_id, note_type="assumption", content="...")   # to verify
note_add(task_id, note_type="constraint", content="...")   # hard limit
note_add(task_id, note_type="risk",       content="...")   # known risk
```

- Notes on root/parent tasks are visible to ALL descendants via `include_parent=True`
- Search all notes in a subtree: `note_list(task_id, include_children=True)`
- Resolve open questions before handing off to coder

## Phase 3: Link Code to Tasks

**Find nodes** — two approaches:
```
# By keyword search (returns id + qualified_name)
semantic_search_nodes_tool(query="create_task", kind="Function")
→ { id: 1791, qualified_name: "code_review_graph/tasks.py::create_task", ... }

# By file structure
query_graph_tool(pattern="children_of", target="code_review_graph/tasks.py")
```

**Link to task** — use either id or qualified_name:
```
task_link_code(task_id, ref_type="modifies",  code_node_id=1791)
task_link_code(task_id, ref_type="modifies",  qualified_name="code_review_graph/tasks.py::create_task")
task_link_code(task_id, ref_type="reads",     qualified_name="...")
task_link_code(task_id, ref_type="creates",   qualified_name="...")
task_link_code(task_id, ref_type="deletes",   qualified_name="...")
task_link_code(task_id, ref_type="tests",     qualified_name="...")
```

**ref_type guide:**
| ref_type | When to use |
|----------|-------------|
| `modifies` | Changing existing function/class |
| `creates` | Adding new function/class |
| `reads` | Reading/querying only |
| `deletes` | Removing code |
| `tests` | Adding/updating tests |

## Phase 4: Define Contracts (Design Entities)

Contracts capture data structures, interfaces, and APIs — even before they exist in code.

```
# New design entity (doesn't exist yet)
contract_add(
    name="OAuthToken",
    contract_type="schema",           # schema | interface | api | event | data_format
    definition="{ access_token: str, refresh_token: str, expires_at: datetime }",
    scope_task_id=root_id,
    provider_task_id=t3_id,           # who creates this
    consumer_task_ids=[t5_id, t6_id]  # who uses it
)

# Modifying existing code (link to real code node)
contract_add(
    name="User_extended",
    contract_type="schema",
    definition="Add oauth_provider: str, oauth_id: str",
    scope_task_id=root_id,
    qualified_name="code_review_graph/graph.py::GraphStore"  # existing code
)

# Add participants after decomposition
contract_link(contract_id, task_id, role="consumer")
contract_unlink(contract_id, task_id)
```

Find all contracts in a brainstorm:
```
contract_list(scope_task_id=root_id)           # all including orphans
contract_list(task_id=leaf_id)                 # contracts for specific task
contract_list(name="OAuthToken")               # find by name
```

## Phase 5: Add DAG Edges

```
task_add_edge(source_id, target_id, edge_type="depends_on")   # sequential dependency
task_add_edge(source_id, target_id, edge_type="blocks")        # blocking relationship
task_add_edge(source_id, target_id, edge_type="shares_context") # related work
```

Cycle detection is automatic — depends_on/blocks edges cannot form cycles.

## Phase 6: Run Analysis

```
task_find_conflicts(root_task_id)     # tasks sharing same code nodes
task_check_isolation(task_id)         # isolation_score < 0.5 → too coupled, split
task_blast_radius(task_id, depth=2)   # code impact radius
task_execution_order(root_task_id)    # parallelism-aware order (levels)
task_suggest_contracts(root_task_id)  # hidden code dependencies without contracts
task_find_for_impact(file_paths)      # open tasks in blast radius of changed files
```

**Interpret `check_isolation`:**
- score > 0.7 → well isolated, safe to implement independently
- score 0.4–0.7 → moderate coupling, coordinate with related tasks
- score < 0.4 → highly coupled, consider splitting or adding contracts

## Phase 7: Validate Before Implementation

```
task_validate()   # auto-detects active root, runs 9 checks
```

Required: **0 errors** before handing off to coder. Warnings are advisory.

Common errors to fix:
- Missing `acceptance_criteria` on leaf tasks → add via `task_update`
- Open questions → resolve via `note_update(note_id, status="resolved", resolution="...")`
- Proposed contracts between ready tasks → `contract_update(id, status="agreed")`
- Leaf tasks with no code refs → `task_link_code(...)` or justify in notes

## Phase 8: Generate Handoff Context

```
task_export()                              # full context for active root
task_export(task_id, include_analysis=True) # + isolation, conflicts, pipeline_state
task_roadmap()                             # progress snapshot + attention block
```

CLI equivalent (generates markdown file for humans + LLM):
```bash
code-review-graph task-report
```

## Phase 9: Implementation Tracking

```
task_update(task_id, status="in_progress")
task_update(task_id, status="done")
task_check_rollup(task_id)    # check if parent can be closed
```

After all leaves are done:
```
task_validate()    # confirm 0 errors still
task_archive(root_task_id, reason="Completed successfully")
```

## Cross-Layer Queries

```
# "Which tasks touch AuthService?"
task_find_by_code_node(code_node_id)

# "What open tasks are in blast radius of my changes?"
task_find_for_impact(file_paths=["src/auth.py", "src/user.py"])

# "Which task pairs need contracts?"
task_suggest_contracts(root_task_id)

# "Is this task safe to implement alone?"
task_check_isolation(task_id)

# "What code will this task touch transitively?"
task_blast_radius(task_id, depth=2)

# "In what order should I implement leaves?"
task_execution_order(root_task_id)   # returns parallel levels
```

## Tips

- Always link leaf tasks to code before `task_validate` — unlisted code refs are a warning
- Use `note_list(include_children=True)` to search decisions across the whole brainstorm
- `task_suggest_code_links(task_id)` auto-suggests nodes from task title/description keywords
- `task_search(root_task_id, query="auth")` finds tasks by title/description text
- `task_get_dag(root_task_id)` returns the full tree with all edges in one call
- Contract `status` auto-upgrades when provider task status changes (draft→proposed→acknowledged→implemented)
