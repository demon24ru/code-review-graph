"""Markdown task-tree report generator.

Generates a single human- and LLM-readable markdown file that aggregates
the full task tree rooted at a given task ID.  Mirrors the ``wiki``
command but for the Task DAG rather than the code community graph.

Output structure per task (depth-first, indented by level):
    # Task Report: <root title>

    ## Summary
    Progress bar, counts, open items

    ## Task: <title>  [status]
    Description, spec, acceptance criteria

    ### Code References
    List of linked code nodes (with optional source snippets)

    ### Dependencies
    incoming / outgoing task edges

    ### Notes
    Decisions, questions, assumptions, constraints, risks

    ### Contracts
    As provider / as consumer

    ### Subtasks
    Recursive tree (H3/H4/H5…)

    ## Roadmap
    Phases, attention block
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

from .task_analysis import (
    export_task,
    export_task,
    roadmap,
    validate_dag,
)
from .tasks import (
    _collect_subtree_ids,
    get_task,
    list_tasks,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STATUS_EMOJI: dict[str, str] = {
    "draft": "📝",
    "refined": "🔍",
    "ready": "✅",
    "in_progress": "🚧",
    "done": "✔️",
    "blocked": "🚫",
    "archived": "📦",
}

_NOTE_EMOJI: dict[str, str] = {
    "decision": "🗝️",
    "question": "❓",
    "assumption": "💭",
    "constraint": "⛓️",
    "risk": "⚠️",
}


def _status_badge(status: str) -> str:
    return f"{_STATUS_EMOJI.get(status, '●')} `{status}`"


def _escape(text: str) -> str:
    """Minimal markdown escape for inline text (backtick-safe)."""
    return str(text).replace("|", "\\|")


def _heading(text: str, level: int) -> str:
    level = max(1, min(6, level))
    return f"{'#' * level} {text}"


def _progress_bar(done: int, total: int, width: int = 20) -> str:
    if total == 0:
        return f"[{'─' * width}] 0%"
    filled = round(done / total * width)
    bar = "█" * filled + "░" * (width - filled)
    pct = round(done / total * 100)
    return f"[{bar}] {pct}%"


# ---------------------------------------------------------------------------
# Per-task section renderer
# ---------------------------------------------------------------------------


def _render_task(
    conn: sqlite3.Connection,
    task_id: str,
    level: int,
    *,
    visited: Optional[set[str]] = None,
) -> list[str]:
    """Recursively render a task and its subtree as markdown lines."""
    if visited is None:
        visited = set()
    if task_id in visited:
        return [f"> ⚠️ Circular reference detected for task `{task_id}`\n"]
    visited.add(task_id)

    lines: list[str] = []

    # Context for this task
    ctx = export_task(conn, task_id, include_analysis=True)
    task = ctx["task"]
    title = _escape(task.get("title", task_id))
    status = task.get("status", "draft")

    lines.append(_heading(f"{title}  {_status_badge(status)}", level))
    lines.append("")

    # Metadata table
    lines.append("| Field | Value |")
    lines.append("|---|---|")
    lines.append(f"| ID | `{task['id']}` |")
    if task.get("description"):
        lines.append(f"| Description | {_escape(task['description'])} |")
    if task.get("acceptance_criteria"):
        lines.append(f"| Acceptance criteria | {_escape(task['acceptance_criteria'])} |")
    isolation = ctx.get("isolation", {})
    if isolation.get("isolation_score") is not None:
        score = isolation["isolation_score"]
        ext = isolation.get("external_dependencies", 0)
        lines.append(f"| Isolation | `{score:.2f}` ({ext} external deps) |")
    pipeline = ctx.get("pipeline_state", {})
    if pipeline:
        ready = "✅ Yes" if pipeline.get("ready_for_coder") else "❌ No"
        lines.append(f"| Ready for coder | {ready} |")
    lines.append("")

    # Spec (potentially long — use code block)
    if task.get("spec"):
        lines.append("**Spec:**")
        lines.append("")
        lines.append("```")
        lines.append(task["spec"])
        lines.append("```")
        lines.append("")

    # Code references — file + line range only (LLM reads on demand)
    code_refs = ctx.get("code_refs", [])
    if code_refs:
        lines.append("**Code References:**")
        lines.append("")
        for ref in code_refs:
            ref_type = ref.get("ref_type", "ref")
            name = ref.get("name", ref.get("qualified_name", "?"))
            fpath = ref.get("file_path", "")
            ls = ref.get("line_start")
            le = ref.get("line_end")
            lang = ref.get("language", "")
            loc = f"`{fpath}`" + (f" L{ls}–{le}" if ls else "")
            lang_tag = f" ({lang})" if lang else ""
            lines.append(f"- `{name}` ({ref_type}){lang_tag} — {loc}")
        lines.append("")

    # Edges / dependencies
    edges = ctx.get("edges", {})
    incoming = edges.get("incoming", [])
    outgoing = edges.get("outgoing", [])
    if incoming or outgoing:
        lines.append("**Dependencies:**")
        lines.append("")
        for e in outgoing:
            etype = e.get("type", "→")
            tid = e.get("target_task_id", "?")
            try:
                t2 = get_task(conn, tid)
                t2_title = _escape(t2.get("title", tid))
            except KeyError:
                t2_title = tid
            lines.append(f"- → `{etype}` **{t2_title}** (`{tid}`)")
        for e in incoming:
            etype = e.get("type", "←")
            tid = e.get("source_task_id", "?")
            try:
                t2 = get_task(conn, tid)
                t2_title = _escape(t2.get("title", tid))
            except KeyError:
                t2_title = tid
            lines.append(f"- ← `{etype}` **{t2_title}** (`{tid}`)")
        lines.append("")

    # Notes (decisions, questions, assumptions, constraints, risks)
    notes = ctx.get("notes", [])
    if notes:
        lines.append("**Notes:**")
        lines.append("")
        for n in notes:
            ntype = n.get("note_type", "note")
            emoji = _NOTE_EMOJI.get(ntype, "📌")
            content = _escape(n.get("content", ""))
            note_status = n.get("status", "open")
            resolution = n.get("resolution")
            suffix = f" → {_escape(resolution)}" if resolution else f" `{note_status}`"
            lines.append(f"- {emoji} **{ntype}**: {content}{suffix}")
        lines.append("")

    # Contracts
    contracts = ctx.get("contracts", {})
    as_provider = contracts.get("as_provider", [])
    as_consumer = contracts.get("as_consumer", [])
    if as_provider or as_consumer:
        lines.append("**Contracts:**")
        lines.append("")
        for c in as_provider:
            cstatus = c.get("status", "proposed")
            ctype = c.get("interface_type", "")
            lines.append(f"- 📤 Provider `{ctype}` → consumer `{c.get('consumer_task_id')}` `{cstatus}`")
        for c in as_consumer:
            cstatus = c.get("status", "proposed")
            ctype = c.get("interface_type", "")
            lines.append(f"- 📥 Consumer `{ctype}` ← provider `{c.get('provider_task_id')}` `{cstatus}`")
        lines.append("")

    # Conflicts
    conflicts = ctx.get("conflicts", [])
    if conflicts:
        lines.append("**⚠️ Code Conflicts with Sibling Tasks:**")
        lines.append("")
        for cf in conflicts:
            lines.append(
                f"- `{cf.get('sibling_task_id')}` ({cf.get('sibling_title', '?')}) — "
                f"{cf.get('shared_node_count', 0)} shared node(s)"
            )
        lines.append("")

    # Recursive subtasks
    subtasks = ctx.get("subtasks", [])
    if subtasks:
        for sub in subtasks:
            lines.extend(
                _render_task(conn, sub["id"], level + 1, visited=visited)
            )

    return lines


# ---------------------------------------------------------------------------
# Report header: summary + roadmap
# ---------------------------------------------------------------------------


def _render_header(conn: sqlite3.Connection, root_task_id: str) -> list[str]:
    """Render the report title, summary table, and roadmap section."""
    lines: list[str] = []
    root = get_task(conn, root_task_id)
    title = _escape(root.get("title", root_task_id))

    lines.append(f"# Task Report: {title}")
    lines.append("")
    lines.append(
        f"> Generated: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}  "
    )
    lines.append(f"> Root task: `{root_task_id}`")
    lines.append("")

    # Roadmap summary
    try:
        rm = roadmap(conn, root_task_id)
        progress = rm.get("progress", {})
        total = progress.get("total", 0)
        done = progress.get("done", 0)

        lines.append("## Summary")
        lines.append("")
        lines.append(_progress_bar(done, total))
        lines.append("")
        lines.append("| Status | Count |")
        lines.append("|---|---|")
        for key in ("total", "done", "in_progress", "ready", "blocked", "draft", "archived"):
            val = progress.get(key, 0)
            if val:
                emoji = _STATUS_EMOJI.get(key, "●")
                lines.append(f"| {emoji} {key} | {val} |")
        lines.append("")

        # Contracts summary
        contracts_summary = rm.get("contracts", {})
        if contracts_summary.get("total", 0) > 0:
            lines.append("**Contracts:**")
            lines.append("")
            lines.append("| | Count |")
            lines.append("|---|---|")
            lines.append(f"| Total | {contracts_summary['total']} |")
            lines.append(f"| ✅ Agreed/implemented/verified | {contracts_summary.get('agreed', 0)} |")
            lines.append(f"| ⏳ Pending (proposed) | {contracts_summary.get('pending', 0)} |")
            pending_list = contracts_summary.get("pending_list", [])
            if pending_list:
                lines.append("")
                lines.append("*Pending contracts:*")
                for c in pending_list:
                    cid = c.get("id", "?")
                    cname = c.get("name") or c.get("contract_type", "?")
                    # Support both new (provider_task_ids list) and legacy columns
                    prov_ids = c.get("provider_task_ids") or (
                        [c["provider_task_id"]] if c.get("provider_task_id") else []
                    )
                    cons_ids = c.get("consumer_task_ids") or (
                        [c["consumer_task_id"]] if c.get("consumer_task_id") else []
                    )
                    prov = ", ".join(f"`{p}`" for p in prov_ids) or "?"
                    cons = ", ".join(f"`{p}`" for p in cons_ids) or "?"
                    lines.append(f"- `{cid}` **{cname}** — {prov} → {cons}")
            lines.append("")

        # Attention block
        attention = rm.get("attention", {})
        ready_ids = attention.get("ready_to_start", [])
        open_qs = attention.get("unresolved_questions", [])
        low_iso = attention.get("low_isolation", [])
        pending_c = attention.get("pending_contracts", [])

        if ready_ids or open_qs or low_iso or pending_c:
            lines.append("### ⚡ Attention")
            lines.append("")
            if ready_ids:
                lines.append(f"**Ready to start ({len(ready_ids)}):**")
                for tid in ready_ids[:5]:
                    try:
                        t = get_task(conn, tid)
                        lines.append(f"- ✅ {_escape(t.get('title', tid))} (`{tid}`)")
                    except KeyError:
                        lines.append(f"- ✅ `{tid}`")
                lines.append("")
            if open_qs:
                lines.append(f"**Open questions ({len(open_qs)}):**")
                for n in open_qs[:5]:
                    lines.append(f"- ❓ {_escape(n.get('content', '?'))}")
                lines.append("")
            if low_iso:
                lines.append(f"**Low isolation ({len(low_iso)}):**")
                for item in low_iso[:5]:
                    lines.append(
                        f"- `{item.get('id')}` {_escape(item.get('title', '?'))} "
                        f"(score: {item.get('isolation_score', '?')})"
                    )
                lines.append("")
            if pending_c:
                lines.append(f"**Pending contracts ({len(pending_c)}):**")
                for c in pending_c[:5]:
                    lines.append(
                        f"- `{c.get('provider_task_id')}` → `{c.get('consumer_task_id')}` "
                        f"({c.get('interface_type', '?')})"
                    )
                lines.append("")

        # Validation
        try:
            val = validate_dag(conn, root_task_id)
            errors = val.get("errors", [])
            warnings_v = val.get("warnings", [])
            if errors or warnings_v:
                lines.append("### 🔍 Validation")
                lines.append("")
                for err in errors:
                    lines.append(f"- ❌ {_escape(err)}")
                for w in warnings_v:
                    lines.append(f"- ⚠️ {_escape(w)}")
                lines.append("")
        except Exception:
            pass

        # Phases
        phases = rm.get("phases", [])
        if phases:
            lines.append("## Execution Phases")
            lines.append("")
            for phase in phases:
                lines.append(f"### Phase {phase['level']}")
                lines.append("")
                for t in phase.get("tasks", []):
                    blocked = t.get("blocked_by")
                    suffix = f" ⛔ blocked by {blocked}" if blocked else ""
                    lines.append(
                        f"- {_status_badge(t.get('status', 'draft'))} "
                        f"{_escape(t.get('title', t['id']))} (`{t['id']}`){suffix}"
                    )
                lines.append("")

    except Exception as exc:
        logger.warning("Could not render roadmap: %s", exc)

    return lines


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_task_report(
    conn: sqlite3.Connection,
    root_task_id: str,
    output_dir: Path,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Generate a markdown task-tree report for *root_task_id*.

    Writes one file: ``<output_dir>/task-report.md``

    Code references are listed as ``file_path L{start}–{end}`` only —
    no source code is embedded. The LLM can read specific line ranges
    on demand, keeping the report token-efficient.

    Args:
        conn: SQLite connection with Task DAG schema.
        root_task_id: Root task ID to start the report from.
        output_dir: Directory to write the report file.
        force: Overwrite even if content has not changed.

    Returns:
        Dict with ``output_path``, ``tasks_rendered``, ``skipped`` (bool).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / "task-report.md"

    # Validate root task exists
    get_task(conn, root_task_id)
    subtree_ids = _collect_subtree_ids(conn, root_task_id)
    tasks_count = len(subtree_ids)

    # Build markdown
    all_lines: list[str] = []
    all_lines.extend(_render_header(conn, root_task_id))

    all_lines.append("---")
    all_lines.append("")
    all_lines.append("## Task Tree")
    all_lines.append("")

    all_lines.extend(_render_task(conn, root_task_id, level=3))

    content = "\n".join(all_lines) + "\n"

    # Skip if unchanged
    if out_path.exists() and not force:
        existing = out_path.read_text(encoding="utf-8")
        # Strip timestamp line before comparing
        def _strip_ts(text: str) -> str:
            return "\n".join(
                line for line in text.splitlines()
                if not line.startswith("> Generated:")
            )
        if _strip_ts(existing) == _strip_ts(content):
            return {"output_path": str(out_path), "tasks_rendered": tasks_count, "skipped": True}

    out_path.write_text(content, encoding="utf-8")
    logger.info("Task report written to %s (%d tasks)", out_path, tasks_count)

    return {"output_path": str(out_path), "tasks_rendered": tasks_count, "skipped": False}
