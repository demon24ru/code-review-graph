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

All creation calls use **list-based batch mode** — always pass a list, even for one item.

```
task_get_active_root()          # confirm pipeline is idle

# Create root task
task_create(tasks=[{"title": "My Feature", "description": "..."}])

# Decompose in one call — all subtasks sharing the same parent
task_create(parent_id=root_id, tasks=[
    {"title": "Auth module"},
    {"title": "Token service", "description": "JWT-based"},
    {"title": "Login endpoint"},
])

# Deeper nesting — same pattern
task_create(parent_id=auth_id, tasks=[
    {"title": "OAuth interface"},
    {"title": "Google OAuth impl"},
])
```

Rules:
- Only **leaf tasks** (no children) get code refs and contract links
- Parent tasks are grouping containers — no direct code refs
- 3 levels max for clarity; decompose further only if `check_isolation` score < 0.5

## Phase 2: Add Notes and Design Decisions

Add multiple notes in one call — always pass a list, even for one note.

```
# All notes for a task in one call (typical after structured brainstorm interview)
note_add(task_id=task_id, notes=[
    {"note_type": "decision",   "content": "Use JWT", "status": "resolved",
     "resolution": "JWT tokens", "rationale": "stateless"},
    {"note_type": "question",   "content": "WebSocket or polling?"},
    {"note_type": "assumption", "content": "User model already exists"},
    {"note_type": "constraint", "content": "self-hosted only"},
    {"note_type": "risk",       "content": "Token refresh race condition"},
])

# Single note
note_add(task_id=task_id, notes=[
    {"note_type": "constraint", "content": "No external SaaS dependencies"}
])
```

- Notes on root/parent tasks are visible to ALL descendants via `include_parent=True`
- Search all notes in a subtree: `note_list(task_id, include_children=True)`
- Resolve open questions before handing off to coder

## Phase 3: Link Code to Tasks

### Decomposition sweet spot

**Rule: 1 task = 1 coherent logical change, NOT 1 task = 1 code node.**

```
Too coarse: "Add OAuth"          → 50+ nodes → noise
Too fine:   "Add expires_at"     → 1 node → 50 tasks → management hell
Sweet spot: "JWT token service"  → 3-8 nodes → useful, manageable
```

Target **3–8 code nodes per leaf task**:
- 1 primary file (`creates` / `modifies`)
- 2–4 related files (`modifies`)
- 1–3 context files (`reads`)

**Code links only on leaf tasks.** Parent/mid-level tasks are grouping containers — no direct code refs (task_validate warns if violated).

**Handoff levels:**
- Designer → `task_export(mid_task_id)` — sees all leaf subtasks, contracts, notes
- Coder → `task_export(leaf_task_id)` — sees exact nodes, line ranges, acceptance criteria

### Finding nodes

```
# Single symbol
semantic_search_nodes_tool(query="create_task", kind="Function")
→ { id: 655, qualified_name: "code_review_graph/tasks.py::create_task",
    line_start: 100, line_end: 162, params: "(...)", ... }

# Multiple symbols in one call — multi-word = FTS5 OR, single round-trip
semantic_search_nodes_tool(query="create_task add_task_edge move_task archive_task add_note")

# Filter to specific file
semantic_search_nodes_tool(query="create", file_path="tasks.py")

# All functions in a file (structural, not text-based)
query_graph_tool(pattern="children_of", target="code_review_graph/tasks.py")
```

**Use `line_end` to read function bodies efficiently.**
MCP search returns `line_start` and `line_end` for every node. Use them to make
a targeted read instead of loading the whole file:
```
# GOOD: read only the function you need
Read(filePath="tasks.py", offset=line_start, limit=line_end - line_start + 1)

# BAD: reading the whole file to find where a function ends bloats input context
# significantly while output context stays the same — avoid on large codebases
Read(filePath="tasks.py")
```

**MCP does NOT replace grep for body content.** `semantic_search_nodes_tool` only
indexes declarations (name, signature, params). To search inside function bodies —
use Grep or ripgrep directly.

### Linking nodes

Always pass a list, even for one node. Both `code_node_id` (int) and `qualified_name` (str) can be mixed.
Use values directly from `semantic_search_nodes_tool` — no extra lookup needed.

```
# Single node
task_link_code(task_id=task_id, links=[
    {"ref_type": "modifies", "code_node_id": 1791}
])

# Typical leaf task (3-8 nodes in one call)
task_link_code(task_id=task_id, links=[
    {"ref_type": "modifies", "code_node_id": 1791},
    {"ref_type": "modifies", "qualified_name": "src/auth.py::TokenModel"},
    {"ref_type": "reads",    "qualified_name": "src/config.py::JWTConfig"},
    {"ref_type": "creates",  "qualified_name": "src/auth.py::TokenResponse",
     "description": "new response schema"},
])
# Returns: {task_id, success_count, error_count, total, linked[], errors[]}
# Partial failures do NOT abort — errors collected, valid items linked.
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

Always pass a list, even for one edge. A default `edge_type` applies to all items that lack their own.

```
# Single edge
task_add_edge(edges=[{"source_id": child_id, "target_id": blocker_id}],
              edge_type="depends_on")

# Pattern A — one task depends on many (all children depend on the base interface)
task_add_edge(edge_type="depends_on", edges=[
    {"source_id": google_oauth_id, "target_id": oauth_interface_id},
    {"source_id": github_oauth_id, "target_id": oauth_interface_id},
    {"source_id": login_endpoint_id, "target_id": oauth_interface_id},
])

# Pattern B — mixed edge types in one call (per-item edge_type overrides default)
task_add_edge(edge_type="depends_on", edges=[
    {"source_id": t5_id, "target_id": t2_id},
    {"source_id": t6_id, "target_id": t7_id, "edge_type": "shares_context"},
])
```

Edge types: `depends_on` | `blocks` | `shares_context` | `conflicts_with` | `informs`
Cycle detection is automatic — `depends_on`/`blocks` edges cannot form cycles (checked atomically).

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
task_archive(task_ids=[root_task_id], reason="Completed successfully")
```

**Selective archiving** — when changing approach mid-brainstorm (archive only what's no longer needed):
```
task_archive(reason="Switching to in-app only", task_ids=["t2", "t3", "t6"])
```

**Restructuring** — move a group of tasks to a new parent in one call:
```
task_move(new_parent_id=auth_group_id, task_ids=["t2", "t3", "t4"])
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

- **Sweet spot**: 3–8 code nodes per leaf task — not 1, not 50
- **Batch API**: all 6 bulk ops use list-based batch — always pass a list, even for one item
  - `task_create(tasks=[...], parent_id=...)` — decompose
  - `task_add_edge(edges=[...], edge_type=...)` — add dependencies
  - `task_link_code(task_id=.., links=[...])` — link code nodes
  - `task_move(task_ids=[...], new_parent_id=...)` — restructure tree
  - `task_archive(task_ids=[...], reason=...)` — selective archiving
  - `note_add(task_id=.., notes=[...])` — add brainstorm notes
- **Handoff**: designer gets `task_export(mid_task_id)`, coder gets `task_export(leaf_task_id, include_analysis=True)`
- Always link leaf tasks to code before `task_validate` — unlisted code refs are a warning
- Use `note_list(include_children=True)` to search decisions across the whole brainstorm
- `task_suggest_code_links(task_id)` auto-suggests nodes from task title/description keywords
- `task_search(root_task_id, query="auth")` finds tasks by title/description text
- `task_get_dag(root_task_id)` returns the full tree with all edges in one call
- Contract `status` auto-upgrades when provider task status changes (draft→proposed→acknowledged→implemented)
