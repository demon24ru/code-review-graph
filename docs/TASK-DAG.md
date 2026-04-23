# Task DAG — Brainstorm-Driven Task Planning

**code-review-graph** includes a Task DAG layer for structuring, decomposing,
and managing development tasks directly inside the code knowledge graph.

Tasks are stored in the same SQLite database as the code graph, enabling
cross-layer queries: which tasks touch which code, which tasks conflict,
what is the blast radius of a task's planned changes.

---

## Concepts

### Task hierarchy
Tasks form a tree (parent → children). The root task represents the
top-level feature or goal. Subtasks are the decomposed units of work.

### DAG edges
Beyond the parent-child hierarchy, tasks can have directed relationships:
- **depends_on** — source cannot start until target is done
- **blocks** — source blocks target from starting
- **shares_context** — both tasks share architectural decisions
- **conflicts_with** — tasks need coordination (potential merge conflict)
- **informs** — source decision influences target approach

Together, hierarchy + edges form an annotated DAG.

### Code refs
Each task can be linked to code graph nodes (functions, classes, files):
- **modifies** — task changes this node
- **creates** — task introduces this node
- **deletes** — task removes this node
- **reads** — task consumes but does not change this node
- **tests** — task adds tests for this node

Code refs enable: conflict detection, isolation scoring, blast radius analysis.

### Notes
A unified journal for all brainstorm context:
- **decision** — a resolved design choice
- **question** — an open question (blocks readiness for coder)
- **assumption** — something assumed true; should be verified
- **constraint** — a hard limit from context or requirements
- **risk** — a potential problem to monitor

### Contracts
Explicit interface agreements between tasks:
`interface | api | schema | event | data_format`

Lifecycle: `proposed → agreed → implemented → verified`

---

## Tool Reference

### CRUD tools

| Tool | Description |
|------|-------------|
| `task_create` | Create a task. Optionally supply `parent_id` for subtasks. |
| `task_update` | Update title, description, status, spec, acceptance_criteria. |
| `task_edit` | Surgically edit a text field: search/replace or line-range mode. |
| `task_get` | Get a task by ID. |
| `task_list` | List tasks with filters: parent_id, status, root_only. |
| `task_move` | Move a task to a new parent (cycle detection enforced). |
| `task_search` | Keyword search within a subtree (title + description + spec). |
| `task_delete` | Delete a task. Without `cascade=True`, raises error if task has children (prevents orphaned subtasks). |
| `task_archive` | Archive (soft-delete) with a recorded reason. Preserves history. |

**Valid statuses:** `draft` → `refined` → `ready` → `in_progress` → `done` → `archived`

### DAG edge tools

| Tool | Description |
|------|-------------|
| `task_add_edge` | Add a directed edge. Cycle check enforced for depends_on/blocks. |
| `task_remove_edge` | Remove an edge by source + target + type. |
| `task_get_dag` | Full DAG for a subtree: nodes + task_edges + hierarchy edges. |
| `task_topological_sort` | Topological order of leaf tasks by depends_on. |

### Code link tools

| Tool | Description |
|------|-------------|
| `task_link_code` | Associate a task with a code node by ID and ref_type. |
| `task_unlink_code` | Remove all associations between a task and a code node. |
| `task_get_code_refs` | Get code nodes linked to a task (with metadata). |
| `task_find_by_code_node` | Find open tasks referencing a code node. `open_only=True` by default (excludes done/archived). |
| `task_suggest_code_links` | Keyword-based suggestions from title+description. Does NOT auto-link. |

Use `semantic_search_nodes_tool` to find `code_node_id` values from descriptions.

### Analysis tools

| Tool | Description |
|------|-------------|
| `task_find_conflicts` | Leaf tasks with overlapping code refs. Types: both_modify, read_write, shared_ref. |
| `task_check_isolation` | isolation_score = internal / (internal + external). Score <0.5 → consider splitting. |
| `task_blast_radius` | BFS from task's code refs into code graph (configurable depth). |
| `task_execution_order` | Group leaf tasks by parallelism level based on depends_on. |

### Validation and context tools

| Tool | Description |
|------|-------------|
| `task_validate` | Gate-check before handoff: 8 algorithmic checks → errors + warnings. |
| `task_build_context` | Full aggregated context for LLM: task + chain + code + notes + contracts + isolation. |
| `task_export` | Flat handoff structure for enrich-task / design-feature / coder workflows. |

### Note tools

| Tool | Description |
|------|-------------|
| `note_add` | Add a brainstorm note. Types: decision\|question\|assumption\|constraint\|risk. |
| `note_update` | Update status, resolution, rationale, or content. |
| `note_list` | List notes. `include_parent=True` includes ancestor task notes. |
| `note_delete` | Delete a note. |

### Contract tools

| Tool | Description |
|------|-------------|
| `contract_add` | Record an interface contract between provider and consumer tasks. |
| `contract_update` | Update definition or advance status (proposed→agreed→implemented→verified). Backward transitions return a `warning` field. |
| `contract_list` | All contracts where task is provider OR consumer. |

### Roadmap tools

| Tool | Description |
|------|-------------|
| `task_roadmap` | Progress snapshot: counts, phases, contracts, attention block. |
| `task_roadmap_diff` | What changed since a Unix timestamp. `tasks_status_changed` items have `old_status` (null — not stored) + `new_status`. |

---

## Validate: 8 checks

`task_validate` runs these checks before handing off to a coder:

| Check | Severity |
|-------|----------|
| No circular depends_on | error |
| Ready tasks have satisfied deps | error |
| Leaf tasks have descriptions | error |
| Contracts between active tasks are not just "proposed" | error |
| Leaf tasks have code_refs | warning |
| No open questions (note_type=question, status=open) | warning |
| No unverified assumptions | warning |
| Leaf tasks have acceptance_criteria | warning |

A task with `errors=[]` is safe to hand to a coder. Warnings are advisory.

---

## Example Brainstorm Workflow

```
User: "Add OAuth authorization via Google and GitHub"

1. task_create(title="OAuth Authorization", description="...")
   → root_id

2. note_add(root_id, "question", "JWT or sessions?")
3. User answers: "JWT"
4. note_update(note_id, status="resolved", resolution="JWT — stateless, scalable")

5. task_create("OAuth Provider Interface", parent_id=root_id)   → t_iface
6. task_create("Google OAuth Implementation", parent_id=root_id) → t_google
7. task_create("JWT Token Service", parent_id=root_id)           → t_jwt
8. task_create("Update Login Endpoint", parent_id=root_id)       → t_login

9. task_add_edge(t_google, t_iface, "depends_on")
10. task_add_edge(t_login, t_iface, "depends_on")
11. task_add_edge(t_login, t_jwt, "depends_on")

12. # Find relevant code nodes
    semantic_search_nodes_tool("AuthController")  → node_id=42
    task_link_code(t_login, 42, "modifies")
    task_link_code(t_iface, 38, "creates")        # OAuthProvider class (new)
    task_link_code(t_google, 38, "reads")

13. task_find_conflicts(root_id)
    → t_iface creates node 38, t_google reads → shared_ref (ok)
    → t_iface creates node 38, t_login reads  → shared_ref (ok)

14. contract_add(t_iface, t_login, "interface",
        "interface OAuthProvider { authenticate(code: str): Token }")
15. contract_update(contract_id, status="agreed")

16. task_check_isolation(t_login)
    → isolation_score: 0.72 (good)

17. task_execution_order(root_id)
    → Level 0: t_iface, t_jwt   (no deps)
    → Level 1: t_google, t_login (depend on level 0)

18. task_validate(root_id)
    → errors: 0, warnings: 2 (no acceptance_criteria)

19. task_update(t_iface, acceptance_criteria="OAuthProvider interface defined")
    task_update(t_jwt, acceptance_criteria="Token generated and validated")
    task_update(t_google, acceptance_criteria="Google OAuth flow working")
    task_update(t_login, acceptance_criteria="Login with Google/GitHub works")

20. task_validate(root_id)
    → errors: 0, warnings: 0, ready for coder: True

21. task_build_context(t_iface)  → full spec for coder

22. task_roadmap(root_id)
    → progress: 5 tasks, 0 done (0%)
    → attention: []  (all clear)
```

---

## System Prompt for Brainstorm Sessions

Use this system prompt to guide an LLM brainstorm agent:

```
You are a brainstorm assistant. Your job is to help the user decompose a
development task into well-specified, isolated subtasks ready for coding.

WORKFLOW:
1. Create a root task with task_create.
2. Ask the user clarifying questions. Record each answer with note_add
   (type=decision if resolved, type=question with status=open if deferred).
3. Decompose into subtasks with task_create (parent_id=root).
4. Add dependency edges with task_add_edge.
5. Link code nodes with task_link_code (use semantic_search_nodes_tool to find IDs).
6. Run task_find_conflicts. Add contracts with contract_add for any conflicts.
7. Run task_check_isolation on each leaf. If score < 0.5, consider splitting.
8. Run task_validate. Fix all errors. Address warnings where possible.
9. Call task_roadmap at any time to show the user where we stand.
10. When validate returns errors=[], use task_export to produce the handoff.

RULES:
- Never start coding. Your output is a validated task DAG + context.
- Every open question (note status=open, type=question) blocks readiness.
- Every assumption (type=assumption, status=open) should be verified.
- Check conflicts after every new code link.
- Use task_roadmap to answer "where are we?" questions instantly.
- Resume sessions with: task_roadmap(root_id) + task_roadmap_diff(root_id, last_ts).
```

---

## Roadmap Output Guide

`task_roadmap` returns a structured snapshot:

```python
{
  "root": { "id": "...", "title": "OAuth Authorization", "status": "draft" },

  "progress": {
    "total": 5, "done": 0, "in_progress": 1,
    "ready": 2, "blocked": 1, "draft": 1, "percent": 0
  },

  "phases": [
    { "level": 0, "tasks": [
        { "id": "...", "title": "OAuth Provider Interface", "status": "done" },
        { "id": "...", "title": "JWT Token Service", "status": "done" }
    ]},
    { "level": 1, "tasks": [
        { "id": "...", "title": "Google OAuth Implementation", "status": "ready" },
        { "id": "...", "title": "Update Login Endpoint",
          "status": "blocked", "blocked_by": ["t_iface_id", "t_jwt_id"] }
    ]}
  ],

  "contracts": { "total": 1, "agreed": 1, "pending": 0 },
  "notes_count": 4,

  "attention": {
    "ready_to_start": ["t_google_id"],
    "unresolved_questions": [],
    "unverified_assumptions": [],
    "low_isolation": [],
    "pending_contracts": []
  }
}
```

---

## Data Model

```sql
tasks            id, parent_id, title, description, status,
                 spec, acceptance_criteria, archive_reason,
                 created_at, updated_at

task_edges       source_task_id, target_task_id, type, description, created_at

task_code_refs   task_id, code_node_id (→ nodes.id), ref_type,
                 description, created_at

notes            id, task_id, note_type, content, status,
                 resolution, rationale, alternatives (JSON), created_at, updated_at

contracts        id, name, scope_task_id (→ tasks.id),
                 contract_type, definition, status,
                 code_node_id (→ nodes.id), created_at, updated_at

contract_links   contract_id (→ contracts.id), task_id (→ tasks.id),
                 role ('provider'|'consumer')
                 PRIMARY KEY (contract_id, task_id, role)
```

All tables live in `.code-review-graph/graph.db` alongside the code graph.
Migrations v6–v9 create and update them automatically on first open.
