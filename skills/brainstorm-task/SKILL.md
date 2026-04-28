---
name: brainstorm-task
description: Plan and track implementation work using the Task DAG system. Create structured task trees linked to real code, enforce single-pipeline discipline, and generate handoff context for coders.
argument-hint: "[task title or description]"
---

# Brainstorm Task

Use the Task DAG system to plan implementation work with full traceability to the code graph.

## Single-Pipeline Discipline

**One root task at a time.** Before creating a new root task, call `task_get_active_root` to check if a pipeline is already running. Only create a new root task when the current pipeline is fully done or archived.

Full pipeline — strictly in order:

```
BRAINSTORM → VALIDATE → DESIGN (root) → [PLAN → IMPLEMENT] × N leaves → CLOSE
```

Each stage answers a different question:

| Stage | Question | Scope | Output |
|---|---|---|---|
| **Brainstorm** | WHAT and WHY? | Root | DAG: tasks, notes, contracts |
| **Validate** | Are we ready? | Root | `task_validate` errors = 0 |
| **Design** | WHAT does it consist of? | Root (1×) | Design docs: architecture, behavior, contracts, testing |
| **Plan** | WHERE in files, in what order? | 1 leaf (N×) | `plan.md` — step-by-step checklist |
| **Implement** | CONCRETE CODE | 1 leaf (N×) | Files, functions, tests |
| **Close** | Mark done, propagate up | Root | `task_update(done)` |

### Why the Design and Plan stages matter

After brainstorm, the DAG has specs and acceptance criteria but no file paths or step order. The coder receiving a leaf task still asks: *"Where does this class go? What file to create? Which DI container to update?"*

- **Design** (1 session, root-level): produces `docs/feature-name/01-architecture.md`, `02-behavior.md`, `04-testing.md`, etc. Uses `task_export(root_id, include_analysis=True)` as input. Human approves. Then `task_update` each leaf to `status="ready"` with finalized spec + AC.
- **Plan** (per leaf): reads `task_export(leaf_id, include_source=True)` + design docs + real file structure (`find_files_by_pattern`, `query_graph`). Produces `plan.md` — ordered checklist of file operations, edits, test cases. No new MCP tools needed — all data is already available.

### Inputs and outputs per stage

```
# Design input (once per root)
task_export(root_id, include_analysis=True)   # full subtree spec
→ design docs: 01-architecture.md … 08-api-contract.md
→ human approval ✅
→ task_update each leaf: status="ready", spec=..., acceptance_criteria=...

# Plan input (per leaf, after design)
task_export(leaf_id, include_source=True)     # spec, AC, contracts, code_refs
find_files_by_pattern(["src/auth/*"])         # actual file structure
query_graph(pattern="children_of", target="src/auth/")
contract_list(task_id=leaf_id)
→ plan.md with step-by-step checklist (mkdir, create file, edit DI, write tests)

# Implement (per leaf)
Follow plan.md checklist.
task_update([{task_id, status: "done"}])
task_check_rollup(leaf_id)
```

Workflow phases — strictly in order:
1. **Brainstorm** — full task tree with notes, contracts, code links
2. **Validate** — `task_validate` must pass (0 errors) before design
3. **Design** — architecture + behavior docs for the root (1 session)
4. **Plan** — step-by-step file checklist for each leaf (N sessions)
5. **Implement** — code each leaf following its plan
6. **Close** — mark done, `task_check_rollup` to propagate up the tree

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

# Decompose + wire dependencies atomically — edges use 0-based task indices
task_create(parent_id=root_id, tasks=[
    {"title": "OAuth interface"},    # index 0
    {"title": "Google OAuth impl"},  # index 1
    {"title": "JWT service"},        # index 2
    {"title": "Login endpoint"},     # index 3
], edges=[
    {"from": 1, "to": 0, "type": "depends_on"},   # Google OAuth needs interface
    {"from": 3, "to": 0, "type": "depends_on"},   # Login needs interface
    {"from": 3, "to": 2, "type": "depends_on"},   # Login needs JWT
])
# → tasks + edges created atomically; "edges" key in response shows created edges
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

## Async Q&A with the Questions UI

When you have open questions or unverified assumptions that require human input, use the Questions UI instead of waiting in the chat session. This decouples LLM work from human response time.

**Note lifecycle:**
```
open  →  answered  →  resolved / rejected / deferred
         (user in UI)   (LLM validates, updates DAG)
```

- `open` — question or assumption added, no response yet
- `answered` — user wrote a response in the web UI (LLM not yet involved)
- `resolved` — LLM validated and accepted; DAG may have been updated
- `rejected` — LLM or user determined the note was invalid/incorrect
- `deferred` — postponed, not blocking current work

### Starting the Questions UI

```bash
# Auto-detect active root task
code-review-graph questions

# Specify task and port
code-review-graph questions --task t1 --port 6234 --no-browser
```

The server opens `http://localhost:6234` with three sections:
- **Awaiting LLM Validation** — notes the user has answered but LLM hasn't processed yet
- **Open Questions** — questions/assumptions waiting for user response
- **Resolved/Rejected** (collapsed) — history

Answers are persisted to SQLite instantly. The session can close — answers survive.

### LLM workflow when resuming a session

At the start of every session, call `task_roadmap()`. The `attention.answered_notes` field lists notes that need processing:

```
task_roadmap()
→ attention.answered_notes: [
    { id: "n3", note_type: "question",
      content: "WebSocket or polling?",
      resolution: "Let's use SSE — simpler than WS, more reliable than polling",
      task_id: "t1" },
    { id: "n7", note_type: "assumption",
      content: "User model has email field",
      resolution: "No! Users have phone only, no email",
      task_id: "t1" }
  ]
```

For each answered note, you MUST:
1. Read the resolution carefully
2. Assess impact on the DAG (does this change invalidate existing tasks? create new questions?)
3. Act accordingly (see table below)
4. Mark the note: `note_update(note_id, status="resolved"/"rejected", rationale="...")`

**Decision table:**

| Answer type | Example | LLM action |
|---|---|---|
| Simple choice | "Use JWT" | `note_update(resolved)` + add decision note |
| Design clarification | "SSE not WebSocket" | `note_update(resolved)` + possibly rename/edit tasks |
| Rejected assumption | "No email field" | `note_update(rejected)` + `task_search` for affected tasks + `task_archive` dead paths + add new questions |
| Direction change | "No templates, hardcode" | `note_update(resolved)` + `task_archive(subtree)` + simplify dependencies |
| Scope expansion | "Also needs offline PWA" | `note_update(resolved)` + `task_create` new subtasks |

`task_validate()` will warn if there are unprocessed `answered` notes. Process all answered notes before marking work ready for implementation.

```
note_list(task_id=root_id, status="answered", include_children=True)
# → process each, then mark resolved/rejected
note_update(note_id, status="resolved", resolution="Accepted: SSE", rationale="...")
note_update(note_id, status="rejected", resolution="User has no email", rationale="Invalidates t2, t8")
```

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
- Designer → `task_export(mid_task_id)` — sees all leaf subtasks, contracts, notes, plus `subtask_code_refs_summary` (rollup: how many unique code nodes all leaves collectively touch, which leaves have no code refs yet)
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
# contract_link returns changed:True (new) or changed:False + reason (already linked)
# contract_unlink returns changed:True (removed) or changed:False + reason (wasn't linked)
```

Find all contracts in a brainstorm:
```
contract_list(scope_task_id=root_id)           # all including orphans
contract_list(task_id=leaf_id)                 # contracts for specific task
contract_list(name="OAuthToken")               # find by name
```

## Phase 5: Add DAG Edges

**When to use inline edges vs task_add_edge:**
- **Inline `edges=` in `task_create`** — use at decomposition time (60% of cases). Atomic: tasks + edges in one call.
- **`task_add_edge`** — use post-factum when tasks already exist ("turns out t7 depends on t4").
- **`task_remove_edge`** — use when a dependency changes ("t5 no longer needs t3 after design change").
- **Viewing edges** — use `task_export(task_id)` which returns the `edges` field; `task_get_dag` for the full subtree.

```
# Post-factum edge (tasks already exist) — always a list, even for one edge
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
Duplicate edges are idempotent — adding the same edge twice returns `already_exists: true` (no overwrite).
`blocks` edges are cross-checked against `depends_on` to prevent mutual-wait deadlocks.

## Phase 6: Run Analysis

```
task_find_conflicts(root_task_id)            # tasks sharing same code nodes (direct, depth=0)
task_find_conflicts(root_task_id, depth=1)   # + indirect: tasks connected via code graph edges
task_contradiction_report(root_task_id)      # compile full report for LLM semantic analysis
task_check_isolation(task_id)         # isolation_score < 0.5 → too coupled, split
task_blast_radius(task_id, depth=2)   # code impact radius (affected_nodes_count + uncovered_nodes)
task_execution_order(root_task_id)    # parallelism-aware order (levels)
task_suggest_contracts(root_task_id)  # hidden code dependencies without contracts
task_find_for_impact(file_paths)      # open tasks in blast radius of changed files
```

**Interpret `check_isolation`:**
- score > 0.7 → well isolated, safe to implement independently
- score 0.4–0.7 → moderate coupling, coordinate with related tasks
- score < 0.4 → highly coupled, consider splitting or adding contracts
- `status: "not_applicable"` → task has no code refs yet (link code first)

Summary includes both `external_dependencies` (callees this task calls) and `external_dependents` (callers of this task).

**`task_blast_radius` response:**
- `affected_nodes_count` — scalar count of BFS-reachable nodes (always present)
- `uncovered_nodes` — actionable list: nodes in blast radius not covered by any task
- `include_affected_nodes=True` — opt-in to get full `affected_nodes` list (can be large)
- `status: "not_applicable"` → task has no code refs linked yet

## Rule of 3 Ps: Problems / Gaps / Contradictions

Brainstorming should resolve three categories of risk before implementation:

```
P1 — Problems      → task_validate + task_blast_radius + task_check_isolation
P2 — Gaps          → task_blast_radius.uncovered_nodes + task_find_for_impact
P3 — Contradictions → task_find_conflicts(depth=1) + task_contradiction_report
```

### P1: Problems
Algorithmic checks that catch structural issues:
- `task_validate()` — gate-check: 9 algorithmic checks (missing code refs, open questions, unsatisfied deps)
- `task_blast_radius(task_id)` — code graph impact; uncovered_nodes = code this task touches but no subtask explains
- `task_check_isolation(task_id)` — score < 0.5 → task too coupled, consider splitting

### P2: Gaps
Cross-layer coverage queries:
- `task_blast_radius(task_id).uncovered_nodes` — nodes in blast radius not covered by any task
- `task_find_for_impact(file_paths)` — open tasks in blast radius of changed files
- `task_suggest_contracts(root_task_id)` — hidden code dependencies needing interface contracts

### P3: Contradictions
Three-level detection:

**Level 1 — Direct code overlap** (algorithmic):
```
task_find_conflicts(root_task_id, depth=0)
```
→ tasks that modify the same code nodes: `both_modify`, `read_write`, `shared_ref`

**Level 2 — Indirect code coupling** (algorithmic):
```
task_find_conflicts(root_task_id, depth=1)
```
→ tasks whose nodes are connected via code graph edges (calls/imports). Returned as `conflict_type: "indirect"` with `coupling_nodes`.

**Level 3 — Semantic contradictions** (LLM-driven):
```
task_contradiction_report(root_task_id)
```
Returns a compact report:
```
{
  code_conflicts:      [...],   # depth=1 conflicts — algorithmic
  all_decisions:       [...],   # every decision note in the subtree
  all_constraints:     [...],   # every constraint note
  all_contracts:       [...],   # all interface contracts
  leaf_tasks_summary:  [...]    # compact: title + first 200 chars + ref_types
}
```
Pass this to the LLM as a single prompt: *"Find semantic contradictions between these decisions, constraints, and contracts."* The LLM looks for things like `"All APIs synchronous"` + `"Realtime WebSocket notifications"` — architectural conflicts across different branches of the task tree.

Record found contradictions as:
```
task_add_edge(edge_type="conflicts_with", edges=[{"source_id": t5_id, "target_id": t8_id}])
note_add(task_id=root_id, notes=[{"note_type": "risk", "content": "Sync-only API conflicts with WebSocket requirement"}])
```

## Phase 7: Validate Before Implementation

```
task_validate()   # auto-detects active root, runs 9 checks
```

Required: **0 errors** before handing off to coder. Warnings are advisory.

Common errors to fix:
- Missing `acceptance_criteria` on leaf tasks → add via `task_update(updates=[{task_id, acceptance_criteria: "..."}])`
- Open questions → resolve via `note_update(note_id, status="resolved", resolution="...")`
- Proposed contracts between ready tasks → `contract_update(id, status="agreed")`
  # Backward transition (e.g. implemented→proposed) returns a "warning" field in response
- Leaf tasks with no code refs → `task_link_code(...)` or justify in notes

## Phase 8: Generate Handoff Context

```
task_export()                              # full context for active root
task_export(task_id, include_analysis=True) # + isolation, conflicts, pipeline_state
task_roadmap()                             # progress snapshot + attention block
```

Every `task_export` response includes a `mermaid_diagram` field — a `graph TD` Mermaid diagram
of the **full subtree** with subgraphs for parent tasks and `depends_on` edges between leaves.

### Mermaid diagram for the Designer

The `mermaid_diagram` from `task_export(root_id)` gives the designer the complete task skeleton
at a glance — all mid-level groups and their leaf tasks, with dependency arrows showing what
must be done before what. Use it to:

- **Understand scope instantly** — one look shows all nested levels without scrolling through task lists
- **Spot deep nesting** — if a subtree has 3+ levels of nesting and many leaves, that branch is a
  candidate for a dedicated sub-agent design session rather than a single monolithic design pass
- **Distribute design work across sub-agents** — when the tree is large, split by top-level mid-task:

```
# Full tree too large for one design session? Use mermaid to identify natural splits:
task_export(root_id)
→ mermaid_diagram shows:
    subgraph cluster_t2 [JWT Token Service]      ← 8 leaves
    subgraph cluster_t3 [Google OAuth Flow]      ← 6 leaves
    subgraph cluster_t4 [Login Endpoint]         ← 4 leaves

# Hand each cluster to a separate designer sub-agent:
task_export(t2_id, include_analysis=True)   # → designer agent A (JWT)
task_export(t3_id, include_analysis=True)   # → designer agent B (Google OAuth)
task_export(t4_id, include_analysis=True)   # → designer agent C (Login)
```

Rule of thumb: if a single mid-level subtree has **more than 8 leaves**, spawn a dedicated
design sub-agent for it. The `mermaid_diagram` makes this split decision visual and obvious.

CLI equivalent (generates markdown file for humans + LLM):
```bash
code-review-graph task-report
```

## Phase 9: Implementation Tracking

```
task_update(updates=[{"task_id": task_id, "status": "in_progress"}])
task_update(updates=[{"task_id": task_id, "status": "done"}])
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

**Preview before destructive operations** — `dry_run=True` returns what WOULD be affected without executing:
```
task_delete(task_id, cascade=True, dry_run=True)   # → {would_delete: [{id, title}], count: N}
task_archive(task_ids=[...], reason="...", dry_run=True)  # → {would_archive: [{id, title}], count: N}
```

**Restructuring** — move a group of tasks to a new parent in one call:
```
task_move(new_parent_id=auth_group_id, task_ids=["t2", "t3", "t4"])
```

## Cross-Layer Queries

```
# "Which tasks touch AuthService?" (open tasks only by default)
task_find_by_code_node(code_node_id)                  # open_only=True by default
task_find_by_code_node(code_node_id, open_only=False) # include done/archived

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
- **`mermaid_diagram`**: every `task_export` returns this field — use it to instantly see the full subtree skeleton (subgraphs = parent tasks, arrows = `depends_on` edges). For deep trees (3+ levels, many leaves), use it to identify subtree boundaries and distribute design/plan work across sub-agents — one sub-agent per top-level cluster
- **`priority_score` in execution levels**: `task_execution_order` now returns each task enriched with `priority_score` (0..1) and `ranking_signals` (`isolation_score`, `dependents_count`, `provider_count`). Tasks within each level are sorted by score descending — the most foundational (many dependents) and most isolated (safe to implement alone) tasks come first. Start with the highest-score tasks in each level
- Always link leaf tasks to code before `task_validate` — unlisted code refs are a warning
- Use `note_list(include_children=True)` to search decisions across the whole brainstorm
- `task_suggest_code_links(task_id)` — scored by keyword match count, already-linked nodes excluded, `limit=20` default
- `task_search(root_task_id, query="auth")` finds tasks by title/description text
- `task_get_dag(root_task_id)` returns the full tree with all edges; use `compact=True` for `{id, title, status, depth, parent_id}` nodes (faster for large trees)
- `task_delete(task_id)` response includes `deleted_tasks: [{id, title}]` — confirm what was deleted
- Pipeline error on `task_create` includes `blocking_task_id` for direct navigation to the blocking root
- **Questions UI**: use `code-review-graph questions` to let users answer open questions asynchronously — survives session restarts; answers stored in SQLite
- **`answered` notes**: after user answers via UI, `task_roadmap()` shows them in `attention.answered_notes`; process each with note_update before `task_validate` passes cleanly
- Contract `status` auto-upgrades when provider task status changes (draft→proposed→acknowledged→implemented)
- **Rule of 3 Ps**: after decomposition, verify P1 (task_validate + blast_radius), P2 (uncovered_nodes), P3 (task_find_conflicts depth=1 + task_contradiction_report)
- **Semantic contradictions**: task_contradiction_report gives LLM everything to find conflicts in decisions/constraints — one call, compact output
