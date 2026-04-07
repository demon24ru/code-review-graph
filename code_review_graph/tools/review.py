"""Tools 4, 12, 16, 17, 18, 19: review context, affected flows, detect changes,
analyze_edit_region, audit_workspace, trace_dataflow.

New tools (17-19) are inspired by hex-graph-mcp and provide line-level blast
radius, unified workspace audit, and source-to-sink data-flow tracing.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..changes import analyze_changes, parse_git_diff_ranges
from ..flows import get_affected_flows as _get_affected_flows
from ..graph import GraphNode, edge_to_dict, node_to_dict
from ..hints import generate_hints, get_session
from ..incremental import get_changed_files, get_staged_and_unstaged
from ._common import _get_store, graph_error

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool 4: get_review_context
# ---------------------------------------------------------------------------


def get_review_context(
    changed_files: list[str] | None = None,
    max_depth: int = 2,
    include_source: bool = True,
    max_lines_per_file: int = 200,
    repo_root: str | None = None,
    base: str = "HEAD~1",
) -> dict[str, Any]:
    """Generate a focused review context from changed files.

    Builds a token-optimized subgraph + source snippets for code review.

    Args:
        changed_files: Files to review (auto-detected from git diff if omitted).
        max_depth: Impact radius depth (default: 2).
        include_source: Whether to include source code snippets (default: True).
        max_lines_per_file: Max source lines per file in output (default: 200).
        repo_root: Repository root path. Auto-detected if omitted.
        base: Git ref for change detection (default: HEAD~1).

    Returns:
        Structured review context with subgraph, source snippets, and
        review guidance.
    """
    store, root = _get_store(repo_root)
    try:
        # Get impact radius first
        if changed_files is None:
            changed_files = get_changed_files(root, base)
            if not changed_files:
                changed_files = get_staged_and_unstaged(root)

        if not changed_files:
            return {
                "status": "ok",
                "summary": "No changes detected. Nothing to review.",
                "context": {},
            }

        abs_files = [str(root / f) for f in changed_files]
        impact = store.get_impact_radius(abs_files, max_depth=max_depth)

        # Build review context
        context: dict[str, Any] = {
            "changed_files": changed_files,
            "impacted_files": impact["impacted_files"],
            "graph": {
                "changed_nodes": [node_to_dict(n) for n in impact["changed_nodes"]],
                "impacted_nodes": [node_to_dict(n) for n in impact["impacted_nodes"]],
                "edges": [edge_to_dict(e) for e in impact["edges"]],
            },
        }

        # Add source snippets for changed files
        if include_source:
            snippets = {}
            for rel_path in changed_files:
                full_path = root / rel_path
                if full_path.is_file():
                    try:
                        lines = full_path.read_text(errors="replace").splitlines()
                        if len(lines) > max_lines_per_file:
                            # Include only the relevant functions/classes
                            relevant_lines = _extract_relevant_lines(
                                lines,
                                impact["changed_nodes"],
                                str(full_path),
                            )
                            snippets[rel_path] = relevant_lines
                        else:
                            snippets[rel_path] = "\n".join(
                                f"{i + 1}: {line}" for i, line in enumerate(lines)
                            )
                    except (OSError, UnicodeDecodeError):
                        snippets[rel_path] = "(could not read file)"
            context["source_snippets"] = snippets

        # Generate review guidance
        guidance = _generate_review_guidance(impact, changed_files)
        context["review_guidance"] = guidance

        summary_parts = [
            f"Review context for {len(changed_files)} changed file(s):",
            f"  - {len(impact['changed_nodes'])} directly changed nodes",
            f"  - {len(impact['impacted_nodes'])} impacted nodes"
            f" in {len(impact['impacted_files'])} files",
            "",
            "Review guidance:",
            guidance,
        ]

        return {
            "status": "ok",
            "summary": "\n".join(summary_parts),
            "context": context,
        }
    finally:
        store.close()


def _extract_relevant_lines(lines: list[str], nodes: list, file_path: str) -> str:
    """Extract only the lines relevant to changed nodes."""
    ranges = []
    for n in nodes:
        if n.file_path == file_path:
            start = max(0, n.line_start - 3)  # 2 lines context before
            end = min(len(lines), n.line_end + 2)  # 1 line context after
            ranges.append((start, end))

    if not ranges:
        # Show first N lines as fallback
        return "\n".join(f"{i + 1}: {line}" for i, line in enumerate(lines[:50]))

    # Merge overlapping ranges
    ranges.sort()
    merged = [ranges[0]]
    for start, end in ranges[1:]:
        if start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    parts: list[str] = []
    for start, end in merged:
        if parts:
            parts.append("...")
        for i in range(start, end):
            parts.append(f"{i + 1}: {lines[i]}")

    return "\n".join(parts)


def _generate_review_guidance(impact: dict, changed_files: list[str]) -> str:
    """Generate review guidance based on the impact analysis."""
    guidance_parts = []

    # Check for test coverage
    changed_funcs = [n for n in impact["changed_nodes"] if n.kind == "Function"]
    test_edges = [e for e in impact["edges"] if e.kind == "TESTED_BY"]
    tested_funcs = {e.source_qualified for e in test_edges}

    untested = [f for f in changed_funcs if f.qualified_name not in tested_funcs and not f.is_test]
    if untested:
        guidance_parts.append(
            f"- {len(untested)} changed function(s) lack test coverage: "
            + ", ".join(n.name for n in untested[:5])
        )

    # Check for wide blast radius
    if len(impact["impacted_nodes"]) > 20:
        guidance_parts.append(
            f"- Wide blast radius: {len(impact['impacted_nodes'])} "
            "nodes impacted. "
            "Review callers and dependents carefully."
        )

    # Check for inheritance changes
    inheritance_edges = [e for e in impact["edges"] if e.kind in ("INHERITS", "IMPLEMENTS")]
    if inheritance_edges:
        guidance_parts.append(
            f"- {len(inheritance_edges)} inheritance/implementation "
            "relationship(s) affected. "
            "Check for Liskov substitution violations."
        )

    # Check for cross-file impact
    impacted_file_count = len(impact["impacted_files"])
    if impacted_file_count > 3:
        guidance_parts.append(
            f"- Changes impact {impacted_file_count} other files."
            " Consider splitting into smaller PRs."
        )

    if not guidance_parts:
        guidance_parts.append("- Changes appear well-contained with minimal blast radius.")

    return "\n".join(guidance_parts)


# ---------------------------------------------------------------------------
# Tool 12: get_affected_flows  [REVIEW]
# ---------------------------------------------------------------------------


def get_affected_flows_func(
    changed_files: list[str] | None = None,
    base: str = "HEAD~1",
    repo_root: str | None = None,
) -> dict[str, Any]:
    """Find execution flows affected by changed files.

    [REVIEW] Identifies which execution flows pass through nodes in the
    changed files.  Useful during code review to understand which user-facing
    or critical paths are affected by a change.

    Args:
        changed_files: List of changed file paths (relative to repo root).
                       Auto-detected from git diff if omitted.
        base: Git ref for auto-detecting changes (default: HEAD~1).
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        Affected flows sorted by criticality, with step details.
    """
    store, root = _get_store(repo_root)
    try:
        if changed_files is None:
            changed_files = get_changed_files(root, base)
            if not changed_files:
                changed_files = get_staged_and_unstaged(root)

        if not changed_files:
            return {
                "status": "ok",
                "summary": "No changed files detected.",
                "affected_flows": [],
                "total": 0,
            }

        # Convert to absolute paths for graph lookup
        abs_files = [str(root / f) for f in changed_files]
        result = _get_affected_flows(store, abs_files)

        total = result["total"]
        out = {
            "status": "ok",
            "summary": (f"{total} flow(s) affected by changes in {len(changed_files)} file(s)"),
            "changed_files": changed_files,
            "affected_flows": result["affected_flows"],
            "total": total,
        }
        out["_hints"] = generate_hints("get_affected_flows", out, get_session())
        return out
    except Exception as exc:
        return graph_error("PARSE_ERROR", str(exc))
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Tool 16: detect_changes  [REVIEW]
# ---------------------------------------------------------------------------


def detect_changes_func(
    base: str = "HEAD~1",
    changed_files: list[str] | None = None,
    include_source: bool = False,
    max_depth: int = 2,
    repo_root: str | None = None,
) -> dict[str, Any]:
    """Detect changes and produce risk-scored review guidance.

    [REVIEW] Primary tool for code review.  Maps git diffs to affected
    functions, flows, communities, and test coverage gaps.  Returns
    priority-ordered review guidance with risk scores.

    Args:
        base: Git ref to diff against (default: HEAD~1).
        changed_files: Explicit list of changed file paths (relative to repo
            root).  Auto-detected from git diff if omitted.
        include_source: If True, include source code snippets for changed
            functions.  Default: False.
        max_depth: Impact radius depth for BFS traversal.  Default: 2.
        repo_root: Repository root path.  Auto-detected if omitted.

    Returns:
        Risk-scored analysis with changed functions, affected flows,
        test gaps, and review priorities.
    """
    store, root = _get_store(repo_root)
    try:
        # Detect changed files if not provided.
        if changed_files is None:
            changed_files = get_changed_files(root, base)
            if not changed_files:
                changed_files = get_staged_and_unstaged(root)

        if not changed_files:
            return {
                "status": "ok",
                "summary": "No changed files detected.",
                "risk_score": 0.0,
                "changed_functions": [],
                "affected_flows": [],
                "test_gaps": [],
                "review_priorities": [],
            }

        # Convert to absolute paths for graph lookup.
        abs_files = [str(root / f) for f in changed_files]

        # Parse diff ranges for line-level mapping.
        diff_ranges = parse_git_diff_ranges(str(root), base)
        # Remap to absolute paths so they match graph file_paths.
        abs_ranges: dict[str, list[tuple[int, int]]] = {}
        for rel_path, ranges in diff_ranges.items():
            abs_path = str(root / rel_path)
            abs_ranges[abs_path] = ranges

        analysis = analyze_changes(
            store,
            changed_files=abs_files,
            changed_ranges=abs_ranges if abs_ranges else None,
            repo_root=str(root),
            base=base,
        )

        # Optionally include source snippets for changed functions.
        if include_source:
            for func in analysis.get("changed_functions", []):
                fp = func.get("file_path")
                ls = func.get("line_start")
                le = func.get("line_end")
                if fp and ls and le:
                    file_path = Path(fp)
                    if file_path.is_file():
                        try:
                            lines = file_path.read_text(errors="replace").splitlines()
                            start = max(0, ls - 1)
                            end = min(len(lines), le)
                            func["source"] = "\n".join(
                                f"{i + 1}: {lines[i]}" for i in range(start, end)
                            )
                        except (OSError, UnicodeDecodeError):
                            func["source"] = "(could not read file)"

        result = {
            "status": "ok",
            "changed_files": changed_files,
            **analysis,
        }
        result["_hints"] = generate_hints("detect_changes", result, get_session())
        return result
    except Exception as exc:
        return graph_error("PARSE_ERROR", str(exc))
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Tool 17: analyze_edit_region  [REVIEW]
# ---------------------------------------------------------------------------


def analyze_edit_region(
    file_path: str,
    line_start: int,
    line_end: int,
    repo_root: str | None = None,
) -> dict[str, Any]:
    """Analyze the blast radius of edits within a specific line range of a file.

    Unlike ``get_impact_radius`` (which operates on whole files), this tool
    focuses on the *exact lines* being changed so that agents get a tight,
    low-noise view of what will be affected.

    Args:
        file_path: Path to the file being edited (absolute or relative to
            ``repo_root``).
        line_start: First line of the edited region (1-indexed, inclusive).
        line_end: Last line of the edited region (1-indexed, inclusive).
        repo_root: Repository root path.  Auto-detected if omitted.

    Returns:
        Dict with:
          - ``overlapping_symbols``: functions/classes whose body overlaps the
            range.
          - ``external_callers``: nodes from *other* files that call any
            overlapping symbol.
          - ``downstream_calls``: nodes that the overlapping symbols call.
          - ``test_coverage``: test nodes that reference any overlapping symbol.
          - ``summary``: human-readable summary string.
          - ``next_actions``: suggested follow-up tools.
    """
    store, root = _get_store(repo_root)
    try:
        # Resolve to absolute path
        fp = Path(file_path)
        if not fp.is_absolute():
            fp = root / fp
        abs_path = str(fp)

        # Get all nodes in the file
        all_nodes = store.get_nodes_by_file(abs_path)
        if not all_nodes:
            return {
                "status": "not_found",
                "summary": (
                    f"No graph nodes found for '{file_path}'. Run build_or_update_graph_tool first."
                ),
                "next_actions": ["build_or_update_graph_tool", "find_files_by_pattern_tool"],
            }

        # Filter nodes that overlap [line_start, line_end]
        overlapping: list[Any] = []
        for node in all_nodes:
            if node.kind == "File":
                continue
            ls = node.line_start or 0
            le = node.line_end or 0
            if ls <= line_end and le >= line_start:
                overlapping.append(node)

        if not overlapping:
            return {
                "status": "ok",
                "summary": (
                    f"No function/class symbols overlap lines {line_start}-{line_end} "
                    f"in '{file_path}'."
                ),
                "overlapping_symbols": [],
                "external_callers": [],
                "downstream_calls": [],
                "test_coverage": [],
                "next_actions": ["query_graph_tool", "semantic_search_nodes_tool"],
            }

        from ..graph import node_to_dict

        overlapping_dicts = [node_to_dict(n) for n in overlapping]
        overlapping_qns = {n.qualified_name for n in overlapping}

        external_callers: list[dict[str, Any]] = []
        downstream_calls: list[dict[str, Any]] = []
        test_coverage: list[dict[str, Any]] = []
        seen_callers: set[str] = set()
        seen_downstream: set[str] = set()
        seen_tests: set[str] = set()

        for node in overlapping:
            # Incoming edges — look for callers from OTHER files
            incoming = store.get_edges_by_target(node.qualified_name)
            for edge in incoming:
                if edge.kind == "CALLS" and edge.source_qualified not in seen_callers:
                    caller = store.get_node(edge.source_qualified)
                    if caller and caller.file_path != abs_path:
                        external_callers.append(
                            {
                                **node_to_dict(caller),
                                "call_line": edge.line,
                                "call_file": edge.file_path,
                            }
                        )
                        seen_callers.add(edge.source_qualified)
                if edge.kind == "TESTED_BY" and edge.source_qualified not in seen_tests:
                    test_node = store.get_node(edge.source_qualified)
                    if test_node:
                        test_coverage.append(node_to_dict(test_node))
                        seen_tests.add(edge.source_qualified)

            # Outgoing edges — downstream calls
            outgoing = store.get_edges_by_source(node.qualified_name)
            for edge in outgoing:
                if edge.kind == "CALLS" and edge.target_qualified not in seen_downstream:
                    callee = store.get_node(edge.target_qualified)
                    if callee:
                        downstream_calls.append(node_to_dict(callee))
                        seen_downstream.add(edge.target_qualified)

        summary_parts = [
            f"Edit region {file_path}:{line_start}-{line_end} overlaps "
            f"{len(overlapping)} symbol(s).",
            f"  External callers: {len(external_callers)}",
            f"  Downstream calls: {len(downstream_calls)}",
            f"  Test coverage:    {len(test_coverage)}",
        ]
        if not test_coverage:
            summary_parts.append(
                "  WARNING: No tests cover the edited symbols. "
                "Consider adding tests before merging."
            )

        result: dict[str, Any] = {
            "status": "ok",
            "summary": "\n".join(summary_parts),
            "file_path": file_path,
            "line_start": line_start,
            "line_end": line_end,
            # Aggregate impact counts (mirrors hex-graph-mcp impact_summary)
            "impact_summary": {
                "edited_symbol_count": len(overlapping),
                "external_callers": len(external_callers),
                "downstream_calls": len(downstream_calls),
                "test_coverage": len(test_coverage),
            },
            "overlapping_symbols": overlapping_dicts,
            "external_callers": external_callers,
            "downstream_calls": downstream_calls,
            "test_coverage": test_coverage,
        }
        result["_hints"] = generate_hints("analyze_edit_region", result, get_session())
        return result
    except Exception as exc:
        logger.exception("analyze_edit_region failed")
        return graph_error("PARSE_ERROR", str(exc))
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Tarjan SCC cycle detection helper
# ---------------------------------------------------------------------------


def _tarjan_cycles(
    store: Any,
    file_pattern: str | None = None,
    max_cycles: int = 20,
) -> list[list[str]]:
    """Find import/call cycles using Tarjan's SCC algorithm.

    O(V+E) — always terminates, unlike ``nx.simple_cycles`` (Johnson) which
    is O((V+E)(C+1)) and can hang on dense graphs.

    Operates at the **symbol** level on IMPORTS_FROM and CALLS edges,
    optionally filtered by ``file_pattern``.

    Returns a list of representative cycles (each is a list of qualified
    names forming a loop), sorted shortest first, capped at ``max_cycles``.
    """
    from collections import defaultdict

    # Build adjacency list from relevant edges
    adj: dict[str, list[str]] = defaultdict(list)
    for row in store._conn.execute(
        "SELECT source_qualified, target_qualified FROM edges "
        "WHERE kind IN ('IMPORTS_FROM', 'CALLS')"
    ).fetchall():
        src, tgt = row["source_qualified"], row["target_qualified"]
        if not src or not tgt or src == tgt:
            continue
        if file_pattern:
            # Keep only edges whose source node is in a matching file
            node = store.get_node(src)
            if not node or file_pattern not in (node.file_path or ""):
                continue
        adj[src].append(tgt)
        # Ensure target appears as a vertex even if it has no outgoing edges
        if tgt not in adj:
            adj[tgt] = []

    # --- Tarjan SCC (iterative to avoid Python recursion limit) ---
    index_counter = [0]
    stack: list[str] = []
    on_stack: set[str] = set()
    index: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    sccs: list[list[str]] = []

    def strongconnect(start: str) -> None:
        # Iterative Tarjan using an explicit work-stack
        work = [(start, iter(adj.get(start, [])))]
        index[start] = lowlink[start] = index_counter[0]
        index_counter[0] += 1
        stack.append(start)
        on_stack.add(start)

        while work:
            node, neighbors = work[-1]
            try:
                w = next(neighbors)
                if w not in index:
                    work.append((w, iter(adj.get(w, []))))
                    index[w] = lowlink[w] = index_counter[0]
                    index_counter[0] += 1
                    stack.append(w)
                    on_stack.add(w)
                elif w in on_stack:
                    lowlink[node] = min(lowlink[node], index[w])
            except StopIteration:
                work.pop()
                if work:
                    parent = work[-1][0]
                    lowlink[parent] = min(lowlink[parent], lowlink[node])
                if lowlink[node] == index[node]:
                    scc: list[str] = []
                    while True:
                        w = stack.pop()
                        on_stack.discard(w)
                        scc.append(w)
                        if w == node:
                            break
                    if len(scc) >= 2:
                        sccs.append(scc)

    for v in list(adj.keys()):
        if v not in index:
            strongconnect(v)

    # Extract one representative cycle per SCC (BFS shortest path back to start)
    from collections import deque

    result_cycles: list[list[str]] = []
    for scc in sccs:
        scc_set = set(scc)
        start = scc[0]
        # BFS within SCC to find shortest cycle through start
        q: deque[tuple[str, list[str]]] = deque([(start, [start])])
        visited_bfs: set[str] = {start}
        found: list[str] = list(scc) + [scc[0]]  # fallback
        while q:
            cur, path = q.popleft()
            for nb in adj.get(cur, []):
                if nb not in scc_set:
                    continue
                if nb == start and len(path) >= 2:
                    found = path + [start]
                    q.clear()
                    break
                if nb not in visited_bfs:
                    visited_bfs.add(nb)
                    q.append((nb, path + [nb]))
        result_cycles.append(found)
        if len(result_cycles) >= max_cycles:
            break

    result_cycles.sort(key=len)
    return result_cycles[:max_cycles]


# ---------------------------------------------------------------------------
# Tool 18: audit_workspace  [REVIEW]
# ---------------------------------------------------------------------------


def audit_workspace(
    include_dead_code: bool = True,
    include_large_functions: bool = True,
    include_cycles: bool = True,
    min_lines: int = 50,
    file_pattern: str | None = None,
    repo_root: str | None = None,
) -> dict[str, Any]:
    """Consolidated workspace quality audit: dead code, cycles, and large functions.

    Runs all three quality checks in one call, giving a single pre-merge
    health report suitable for CI gating or agent decision-making.

    Args:
        include_dead_code: Detect unreferenced functions/classes.  Default: True.
        include_large_functions: Find functions/classes over *min_lines*.  Default: True.
        include_cycles: Detect import/call cycles in the dependency graph.
            Default: True.
        min_lines: Line threshold for ``include_large_functions``.  Default: 50.
        file_pattern: Restrict all checks to file paths containing this
            substring.  Default: None (entire repo).
        repo_root: Repository root path.  Auto-detected if omitted.

    Returns:
        Dict with ``dead_code``, ``large_functions``, ``cycles``, a scalar
        ``health_score`` (0-100), and ``next_actions``.
    """
    store, _root = _get_store(repo_root)
    try:
        from ..refactor import find_dead_code

        dead_code: list[dict[str, Any]] = []
        large_functions: list[dict[str, Any]] = []
        cycles: list[list[str]] = []

        if include_dead_code:
            dead_code = find_dead_code(store, file_pattern=file_pattern)

        if include_large_functions:
            nodes = store.get_nodes_by_size(
                min_lines=min_lines,
                kind=None,
                file_path_pattern=file_pattern,
                limit=100,
            )
            large_functions = [
                {
                    "name": n.name,
                    "qualified_name": n.qualified_name,
                    "kind": n.kind,
                    "file": n.file_path,
                    "line_start": n.line_start,
                    "line_end": n.line_end,
                    "line_count": (n.line_end or 0) - (n.line_start or 0) + 1,
                }
                for n in nodes
            ]

        if include_cycles:
            try:
                cycles = _tarjan_cycles(store, file_pattern=file_pattern)
            except Exception as exc:
                logger.warning("audit_workspace: cycle detection failed: %s", exc)

        # Compute a simple health score: 100 - penalties
        dead_penalty = min(len(dead_code) * 2, 30)
        large_penalty = min(len(large_functions) * 1, 20)
        cycle_penalty = min(len(cycles) * 5, 50)
        health_score = max(0, 100 - dead_penalty - large_penalty - cycle_penalty)

        issues_count = len(dead_code) + len(large_functions) + len(cycles)
        summary_lines = [
            f"Workspace audit complete.  Health score: {health_score}/100",
            f"  Dead code symbols:    {len(dead_code)}",
            f"  Oversized functions:  {len(large_functions)} (>= {min_lines} lines)",
            f"  Dependency cycles:    {len(cycles)}",
        ]
        if health_score < 60:
            summary_lines.append(
                "  ACTION REQUIRED: Multiple quality issues detected. "
                "Review dead_code, large_functions, and cycles fields."
            )
        elif health_score < 80:
            summary_lines.append("  WARN: Some quality issues detected.")
        else:
            summary_lines.append("  OK: Codebase is in good health.")

        result: dict[str, Any] = {
            "status": "ok",
            "summary": "\n".join(summary_lines),
            "health_score": health_score,
            "issues_count": issues_count,
            "dead_code": dead_code,
            "large_functions": large_functions,
            "cycles": cycles,
        }
        result["_hints"] = generate_hints("audit_workspace", result, get_session())
        return result
    except Exception as exc:
        logger.exception("audit_workspace failed")
        return graph_error("PARSE_ERROR", str(exc))
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Tool 19: trace_dataflow  [REVIEW]
# ---------------------------------------------------------------------------


def trace_dataflow(
    source: str,
    sink: str | None = None,
    max_depth: int = 6,
    repo_root: str | None = None,
) -> dict[str, Any]:
    """Trace call-graph reachability from a source symbol to a sink.

    Performs a forward BFS along CALLS edges starting from *source*.  If
    *sink* is provided, it finds all shortest call-paths from source to sink
    and reports whether source can reach sink.  Without a sink it returns
    the full reachable set up to *max_depth* hops.

    NOTE: This is **call-graph reachability**, not variable-level taint
    analysis.  It answers: "Can execution flow from ``parse_request`` reach
    ``execute_query``?"  It does NOT track how a specific variable/parameter
    propagates through assignments — for that level of precision a dedicated
    AST dataflow pass would be needed.

    Args:
        source: Qualified name or bare function name of the source symbol.
        sink: Qualified name or bare function name of the target (optional).
            When omitted, returns the full reachable node set.
        max_depth: Maximum number of CALLS hops to follow.  Default: 6.
        repo_root: Repository root path.  Auto-detected if omitted.

    Returns:
        Dict with:
          - ``source_node``: resolved source node.
          - ``sink_node``: resolved sink node (or ``null``).
          - ``reachable``: list of nodes reachable from source within depth,
            each annotated with ``depth`` and ``path``.
          - ``paths``: call-paths from source to sink (when sink given).
          - ``reaches_sink``: bool — whether source can reach sink.
          - ``reachable_count``: total reachable node count.
          - ``summary``: human-readable result.
          - ``next_actions``: suggested follow-up tools.
    """
    store, _root = _get_store(repo_root)
    try:
        from ..graph import node_to_dict

        # --- Resolve source node ---
        source_candidates = store.search_nodes(source, limit=5)
        if not source_candidates:
            return {
                "status": "not_found",
                "summary": f"Source node '{source}' not found in graph.",
                "next_actions": ["semantic_search_nodes_tool", "query_graph_tool"],
            }
        # Prefer exact name match
        source_node = next(
            (n for n in source_candidates if n.name == source or n.qualified_name == source),
            source_candidates[0],
        )

        # --- Resolve sink node (optional) ---
        sink_node = None
        if sink:
            sink_candidates = store.search_nodes(sink, limit=5)
            if sink_candidates:
                sink_node = next(
                    (n for n in sink_candidates if n.name == sink or n.qualified_name == sink),
                    sink_candidates[0],
                )

        # --- Forward BFS along CALLS edges ---
        # Build a lightweight adjacency list from CALLS edges only
        visited: dict[str, int] = {}  # qualified_name -> depth first seen
        frontier: list[tuple[str, list[str]]] = [
            (source_node.qualified_name, [source_node.qualified_name])
        ]
        reachable_qns: dict[str, list[str]] = {}  # qn -> first path to reach it
        paths_to_sink: list[list[str]] = []
        sink_qn = sink_node.qualified_name if sink_node else None

        depth = 0
        while frontier and depth < max_depth:
            next_frontier: list[tuple[str, list[str]]] = []
            for qn, path in frontier:
                if qn in visited:
                    continue
                visited[qn] = depth
                if qn != source_node.qualified_name:
                    reachable_qns[qn] = path

                if sink_qn and qn == sink_qn:
                    paths_to_sink.append(path)
                    continue  # don't expand further from sink

                # Follow outgoing CALLS edges
                outgoing = store.get_edges_by_source(qn)
                for edge in outgoing:
                    if edge.kind == "CALLS" and edge.target_qualified not in visited:
                        next_frontier.append(
                            (edge.target_qualified, path + [edge.target_qualified])
                        )
            frontier = next_frontier
            depth += 1

        # Materialise reachable nodes
        reachable_nodes: list[dict[str, Any]] = []
        for qn in reachable_qns:
            node = store.get_node(qn)
            if node:
                reachable_nodes.append(
                    {
                        **node_to_dict(node),
                        "depth": visited.get(qn, -1),
                        "path": reachable_qns[qn],
                    }
                )

        reaches_sink = len(paths_to_sink) > 0 if sink_qn else None

        # Build summary
        sink_name = sink_node.name if sink_node else sink
        if sink_qn:
            if reaches_sink:
                summary = (
                    f"Data from '{source_node.name}' CAN reach '{sink_name}' "
                    f"via {len(paths_to_sink)} path(s) within {depth} hop(s)."
                )
            else:
                summary = (
                    f"Data from '{source_node.name}' CANNOT reach '{sink_name}' "
                    f"within {max_depth} hop(s). "
                    f"{len(reachable_nodes)} node(s) reachable from source."
                )
        else:
            summary = (
                f"From '{source_node.name}', {len(reachable_nodes)} node(s) are "
                f"reachable within {max_depth} CALLS hop(s)."
            )

        result: dict[str, Any] = {
            "status": "ok",
            "summary": summary,
            "source_node": node_to_dict(source_node),
            "sink_node": node_to_dict(sink_node) if sink_node else None,
            "reaches_sink": reaches_sink,
            "reachable_count": len(reachable_nodes),
            "reachable": reachable_nodes,
            "paths": paths_to_sink,
        }
        result["_hints"] = generate_hints("trace_dataflow", result, get_session())
        return result
    except Exception as exc:
        logger.exception("trace_dataflow failed")
        return graph_error("PARSE_ERROR", str(exc))
    finally:
        store.close()
