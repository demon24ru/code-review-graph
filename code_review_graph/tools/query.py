"""Tools 2, 3, 5, 6, 9, 10: query / search / stats helpers."""

from __future__ import annotations

import logging
import fnmatch
from pathlib import Path
from typing import Any

from ..embeddings import EmbeddingStore
from ..graph import edge_to_dict, node_to_dict
from ..hints import generate_hints, get_session
from ..incremental import get_changed_files, get_db_path, get_staged_and_unstaged
from ..search import hybrid_search
from ._common import _BUILTIN_CALL_NAMES, _get_store, graph_error

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool 2: get_impact_radius
# ---------------------------------------------------------------------------

_QUERY_PATTERNS = {
    "callers_of": "Find all functions that call a given function",
    "callees_of": "Find all functions called by a given function",
    "imports_of": "Find all imports of a given file or module",
    "importers_of": "Find all files that import a given file or module",
    "children_of": "Find all nodes contained in a file or class",
    "tests_for": "Find all tests for a given function or class",
    "inheritors_of": "Find all classes that inherit from a given class",
    "file_summary": "Get a summary of all nodes in a file",
}


def get_impact_radius(
    changed_files: list[str] | None = None,
    max_depth: int = 2,
    max_results: int = 500,
    repo_root: str | None = None,
    base: str = "HEAD~1",
) -> dict[str, Any]:
    """Analyze the blast radius of changed files.

    Args:
        changed_files: Explicit list of changed file paths (relative to repo root).
                       If omitted, auto-detects from git diff.
        max_depth: How many hops to traverse in the graph (default: 2).
        max_results: Maximum impacted nodes to return (default: 500).
        repo_root: Repository root path. Auto-detected if omitted.
        base: Git ref for auto-detecting changes (default: HEAD~1).

    Returns:
        Changed nodes, impacted nodes, impacted files, connecting edges,
        plus ``truncated`` flag and ``total_impacted`` count.
    """
    store, root = _get_store(repo_root)
    try:
        if changed_files is None:
            changed_files = get_changed_files(root, base)
            if not changed_files:
                changed_files = get_staged_and_unstaged(root)

        if not changed_files:
            no_change: dict[str, Any] = {
                "status": "ok",
                "summary": "No changed files detected.",
                "changed_nodes": [],
                "impacted_nodes": [],
                "impacted_files": [],
                "truncated": False,
                "total_impacted": 0,
            }
            no_change["_hints"] = generate_hints("get_impact_radius", no_change, get_session())
            return no_change

        # Convert to absolute paths for graph lookup
        abs_files = [str(root / f) for f in changed_files]
        raw = store.get_impact_radius(abs_files, max_depth=max_depth, max_nodes=max_results)

        changed_dicts = [node_to_dict(n) for n in raw["changed_nodes"]]
        impacted_dicts = [node_to_dict(n) for n in raw["impacted_nodes"]]
        edge_dicts = [edge_to_dict(e) for e in raw["edges"]]
        truncated = raw["truncated"]
        total_impacted = raw["total_impacted"]

        summary_parts = [
            f"Blast radius for {len(changed_files)} changed file(s):",
            f"  - {len(changed_dicts)} nodes directly changed",
            f"  - {len(impacted_dicts)} nodes impacted (within {max_depth} hops)",
            f"  - {len(raw['impacted_files'])} additional files affected",
        ]
        if truncated:
            summary_parts.append(
                f"  - Results truncated: showing {len(impacted_dicts)}"
                f" of {total_impacted} impacted nodes"
            )

        result: dict[str, Any] = {
            "status": "ok",
            "summary": "\n".join(summary_parts),
            "changed_files": changed_files,
            "changed_nodes": changed_dicts,
            "impacted_nodes": impacted_dicts,
            "impacted_files": raw["impacted_files"],
            "edges": edge_dicts,
            "truncated": truncated,
            "total_impacted": total_impacted,
        }
        result["_hints"] = generate_hints("get_impact_radius", result, get_session())
        return result
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Tool 3: query_graph
# ---------------------------------------------------------------------------


def query_graph(
    pattern: str,
    target: str,
    repo_root: str | None = None,
) -> dict[str, Any]:
    """Run a predefined graph query.

    Args:
        pattern: Query pattern. One of: callers_of, callees_of, imports_of,
                 importers_of, children_of, tests_for, inheritors_of, file_summary.
        target: The node name, qualified name, or file path to query about.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        Matching nodes and edges for the query.
    """
    store, root = _get_store(repo_root)
    try:
        if pattern not in _QUERY_PATTERNS:
            return graph_error(
                "INVALID_PARAMS",
                f"Unknown pattern '{pattern}'. Available: {list(_QUERY_PATTERNS.keys())}",
            )

        results: list[dict] = []
        edges_out: list[dict] = []

        # For callers_of, skip common builtins early (bare names only)
        # "Who calls .map()?" returns hundreds of useless hits.
        # Qualified names (e.g. "utils.py::map") bypass this filter.
        if pattern == "callers_of" and target in _BUILTIN_CALL_NAMES and "::" not in target:
            return {
                "status": "ok",
                "pattern": pattern,
                "target": target,
                "description": _QUERY_PATTERNS[pattern],
                "summary": (f"'{target}' is a common builtin — callers_of skipped to avoid noise."),
                "results": [],
                "edges": [],
            }

        # Resolve target - try as-is, then as absolute path, then search
        node = store.get_node(target)
        if not node:
            abs_target = str(root / target)
            node = store.get_node(abs_target)

        # Track whether the resolution was ambiguous so we can attach a note to the result.
        ambiguous_meta: dict | None = None

        if not node:
            # Search by name
            candidates = store.search_nodes(target, limit=10)

            # Prefer exact name matches over partial ones.
            exact_match_candidates = [c for c in candidates if c.name == target]
            pool = exact_match_candidates if exact_match_candidates else candidates

            if len(pool) == 1:
                # Unambiguous — resolve silently.
                node = pool[0]
                target = node.qualified_name
            elif len(pool) > 1:
                # Multiple candidates: pick the best one and carry on, but signal
                # the ambiguity so the caller can verify.
                # Priority: non-test nodes first, then highest BM25 score (pool is
                # already score-sorted by store.search_nodes).
                non_test = [c for c in pool if not c.is_test]
                best = (non_test if non_test else pool)[0]
                node = best
                target = node.qualified_name
                ambiguous_meta = {
                    "resolved_as": node.qualified_name,
                    "note": (
                        f"Multiple nodes named '{node.name}' exist. "
                        "Used the best match (non-test, highest score). "
                        "Pass the exact qualified_name to override."
                    ),
                    "alternatives": [
                        c.qualified_name for c in pool if c.qualified_name != node.qualified_name
                    ],
                }

        if not node and pattern != "file_summary":
            return {
                "status": "not_found",
                "summary": f"No node found matching '{target}'. If you are looking for a file, try using find_files_by_pattern_tool.",
                "next_actions": ["find_files_by_pattern_tool", "semantic_search_nodes_tool"],
            }

        qn = node.qualified_name if node else target

        if pattern == "callers_of":
            for e in store.get_edges_by_target(qn):
                if e.kind == "CALLS":
                    caller = store.get_node(e.source_qualified)
                    if caller:
                        results.append(node_to_dict(caller))
                    edges_out.append(edge_to_dict(e))
            # Fallback: CALLS edges store unqualified target names
            # (e.g. "generateTestCode") while qn is fully qualified
            # (e.g. "file.ts::generateTestCode"). Search by plain name too.
            if not results and node:
                for e in store.search_edges_by_target_name(node.name):
                    caller = store.get_node(e.source_qualified)
                    if caller:
                        results.append(node_to_dict(caller))
                    edges_out.append(edge_to_dict(e))

        elif pattern == "callees_of":
            for e in store.get_edges_by_source(qn):
                if e.kind == "CALLS":
                    callee = store.get_node(e.target_qualified)
                    if callee:
                        results.append(node_to_dict(callee))
                    edges_out.append(edge_to_dict(e))

        elif pattern == "imports_of":
            for e in store.get_edges_by_source(qn):
                if e.kind == "IMPORTS_FROM":
                    results.append({"import_target": e.target_qualified})
                    edges_out.append(edge_to_dict(e))

        elif pattern == "importers_of":
            # Find edges where target matches this file.
            # Use resolve() to canonicalize the path, matching how
            # _resolve_module_to_file stores edge targets.
            abs_target = str((root / target).resolve()) if node is None else node.file_path
            for e in store.get_edges_by_target(abs_target):
                if e.kind == "IMPORTS_FROM":
                    results.append(
                        {
                            "importer": e.source_qualified,
                            "file": e.file_path,
                        }
                    )
                    edges_out.append(edge_to_dict(e))

        elif pattern == "children_of":
            for e in store.get_edges_by_source(qn):
                if e.kind == "CONTAINS":
                    child = store.get_node(e.target_qualified)
                    if child:
                        results.append(node_to_dict(child))

        elif pattern == "tests_for":
            for e in store.get_edges_by_target(qn):
                if e.kind == "TESTED_BY":
                    test = store.get_node(e.source_qualified)
                    if test:
                        results.append(node_to_dict(test))
            # Also search by naming convention
            name = node.name if node else target
            test_nodes = store.search_nodes(f"test_{name}", limit=10)
            test_nodes += store.search_nodes(f"Test{name}", limit=10)
            seen = {r.get("qualified_name") for r in results}
            for t in test_nodes:
                if t.qualified_name not in seen and t.is_test:
                    results.append(node_to_dict(t))

        elif pattern == "inheritors_of":
            seen_sources: set[str] = set()
            for e in store.get_edges_by_target(qn):
                if e.kind in ("INHERITS", "IMPLEMENTS"):
                    child = store.get_node(e.source_qualified)
                    if child and e.source_qualified not in seen_sources:
                        results.append(node_to_dict(child))
                        seen_sources.add(e.source_qualified)
                    edges_out.append(edge_to_dict(e))
            # Fallback: INHERITS edges store bare target names (e.g. "BaseService")
            # rather than fully qualified names (e.g. "file.py::BaseService").
            # Search by bare name too so callers get results even when the
            # qualified-name lookup returns nothing.
            if node:
                for e in store.get_edges_by_target(node.name):
                    if e.kind in ("INHERITS", "IMPLEMENTS") and e.source_qualified not in seen_sources:
                        child = store.get_node(e.source_qualified)
                        if child:
                            results.append(node_to_dict(child))
                            seen_sources.add(e.source_qualified)
                        edges_out.append(edge_to_dict(e))

        elif pattern == "file_summary":
            abs_path = str(root / target)
            file_nodes = store.get_nodes_by_file(abs_path)
            for n in file_nodes:
                results.append(node_to_dict(n))

        status = "ambiguous" if ambiguous_meta else "ok"
        result = {
            "status": status,
            "pattern": pattern,
            "target": target,
            "description": _QUERY_PATTERNS[pattern],
            "summary": (f"Found {len(results)} result(s) for {pattern}('{target}')"),
            "results": results,
            "edges": edges_out,
        }
        if ambiguous_meta:
            result["resolved_as"] = ambiguous_meta["resolved_as"]
            result["disambiguation_note"] = ambiguous_meta["note"]
            result["alternatives"] = ambiguous_meta["alternatives"]
        result["_hints"] = generate_hints("query_graph", result, get_session())
        return result
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Tool 5: semantic_search_nodes
# ---------------------------------------------------------------------------


def semantic_search_nodes(
    query: str,
    kind: str | None = None,
    limit: int = 20,
    repo_root: str | None = None,
    context_files: list[str] | None = None,
    model: str | None = None,
    names: list[str] | None = None,
    file_path: str | None = None,
) -> dict[str, Any]:
    """Search for nodes by name, keyword, or semantic similarity.

    Uses hybrid search (FTS5 BM25 + vector embeddings merged via Reciprocal
    Rank Fusion) as the primary search path, with graceful fallback to
    keyword matching.

    Args:
        query: Search string. Multi-word queries are converted to FTS5 OR
            expressions automatically — ``"create_task move_task"`` finds
            nodes matching either token in one round-trip.
        kind: Optional filter by node kind (File, Class, Function, Type, Test).
        limit: Maximum results to return (default: 20).
        repo_root: Repository root path. Auto-detected if omitted.
        context_files: Optional list of file paths. Nodes in these files
            receive a relevance boost.
        names: Optional list of symbol names for bulk multi-symbol lookup.
            Equivalent to adding them space-separated to ``query``.
            Example: ``names=["create_task", "move_task", "add_note"]``.
        file_path: Optional file path filter. Only nodes whose ``file_path``
            contains this string are returned (e.g. ``"tasks.py"``).

    Returns:
        Ranked list of matching nodes.
    """
    store, root = _get_store(repo_root)
    try:
        results = hybrid_search(
            store,
            query,
            kind=kind,
            limit=limit,
            context_files=context_files,
            model=model,
            names=names,
            file_path=file_path,
        )

        search_mode = "hybrid"
        if not results:
            search_mode = "keyword"

        display_query = query
        if names:
            display_query = ", ".join(names) + (f" + {query}" if query and query.strip() else "")

        # Disambiguation signal: single-token query with many exact-name matches
        # Threshold of 3 is a design choice: >3 exact matches suggests ambiguity
        disambiguation_note: str | None = None
        query_tokens = query.strip().split() if query else []
        if len(query_tokens) == 1 and not names:
            exact_matches = [r for r in results if r.get("name") == query.strip()]
            if len(exact_matches) > 3:
                non_test = [r for r in exact_matches if not r.get("is_test")]
                disambiguation_note = (
                    f"Query '{query.strip()}' matched {len(exact_matches)} nodes with the exact same name. "
                    f"Use file_path='.../filename.py' to narrow results"
                    + (f" ({len(non_test)} non-test, {len(exact_matches) - len(non_test)} test)" if non_test else "")
                    + "."
                )

        result: dict[str, object] = {
            "status": "ok",
            "query": display_query,
            "search_mode": search_mode,
            "summary": f"Found {len(results)} node(s) matching '{display_query}'"
            + (f" (kind={kind})" if kind else "")
            + (f" (file_path contains '{file_path}')" if file_path else ""),
            "results": results,
        }
        if disambiguation_note:
            result["disambiguation_note"] = disambiguation_note
        result["_hints"] = generate_hints("semantic_search_nodes", result, get_session())
        return result
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Tool 6: list_graph_stats
# ---------------------------------------------------------------------------


def list_graph_stats(repo_root: str | None = None) -> dict[str, Any]:
    """Get aggregate statistics about the knowledge graph.

    Args:
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        Total nodes, edges, breakdown by kind, languages, and last update time.
    """
    store, root = _get_store(repo_root)
    try:
        stats = store.get_stats()

        summary_parts = [
            f"Graph statistics for {root.name}:",
            f"  Files: {stats.files_count}",
            f"  Total nodes: {stats.total_nodes}",
            f"  Total edges: {stats.total_edges}",
            f"  Languages: {', '.join(stats.languages) if stats.languages else 'none'}",
            f"  Last updated: {stats.last_updated or 'never'}",
            "",
            "Nodes by kind:",
        ]
        for kind, count in sorted(stats.nodes_by_kind.items()):
            summary_parts.append(f"  {kind}: {count}")
        summary_parts.append("")
        summary_parts.append("Edges by kind:")
        for kind, count in sorted(stats.edges_by_kind.items()):
            summary_parts.append(f"  {kind}: {count}")

        # Add embedding info if available
        emb_store = EmbeddingStore(get_db_path(root))
        try:
            emb_count = emb_store.count()
            summary_parts.append("")
            summary_parts.append(f"Embeddings: {emb_count} nodes embedded")
            if not emb_store.available:
                summary_parts.append("  (install sentence-transformers for semantic search)")
        finally:
            emb_store.close()

        result: dict[str, Any] = {
            "status": "ok",
            "summary": "\n".join(summary_parts),
            "total_nodes": stats.total_nodes,
            "total_edges": stats.total_edges,
            "nodes_by_kind": stats.nodes_by_kind,
            "edges_by_kind": stats.edges_by_kind,
            "languages": stats.languages,
            "files_count": stats.files_count,
            "last_updated": stats.last_updated,
            "embeddings_count": emb_count,
        }
        result["_hints"] = generate_hints("list_graph_stats", result, get_session())
        return result
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Tool 9: find_files_by_pattern
# ---------------------------------------------------------------------------


import fnmatch


def find_files_by_pattern(
    patterns: list[str],
    repo_root: str | None = None,
) -> dict[str, Any]:
    """Find files matching path patterns and retrieve their top-level nodes (classes/functions).

    Useful for discovering the codebase structure without reading entire files.
    Allows agents to see both WHERE the file is, and WHAT it contains conceptually.

    Args:
        patterns: List of glob patterns (e.g. "*router*", "main.*", "src/**/*.ts")
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        List of matching files, along with their primary constituent node names.
    """
    store, root = _get_store(repo_root)
    try:
        all_files = store.get_all_files()
        matched_abs_files: set[str] = set()

        for f in all_files:
            try:
                rel_path = str(Path(f).relative_to(root))
            except ValueError:
                rel_path = f

            # Normalise to forward slashes for consistent matching
            rel_posix = Path(rel_path).as_posix()

            for pat in patterns:
                pat_norm = pat.replace("\\", "/")

                # --- Strategy 1: pathlib.PurePath.match() handles ** correctly ---
                # e.g.  "**/response.py"  "src/**/*.ts"  "*.py"
                if Path(rel_posix).match(pat_norm):
                    matched_abs_files.add(f)
                    break

                # --- Strategy 2: fnmatch on the full relative path ---
                # e.g.  "code_review_graph/*.py"
                if fnmatch.fnmatch(rel_posix, pat_norm):
                    matched_abs_files.add(f)
                    break

                # --- Strategy 3: implicit substring wrap for bare names ---
                # e.g.  "router"  →  "*router*"   (no wildcards at all)
                if "*" not in pat_norm and "?" not in pat_norm:
                    if fnmatch.fnmatch(rel_posix, f"*{pat_norm}*"):
                        matched_abs_files.add(f)
                        break

        results = []
        for abs_file in matched_abs_files:
            try:
                rel_path = str(Path(abs_file).relative_to(root))
            except ValueError:
                rel_path = abs_file

            file_nodes = store.get_nodes_by_file(abs_file)

            # Summarize content: classes and functions, ignore imports/variables/etc.
            # to keep context window footprint low.
            content_summary = []
            for n in file_nodes:
                if n.kind in ("Class", "Function", "Test"):
                    content_summary.append({"name": n.name, "kind": n.kind, "line": n.line_start})

            results.append(
                {"file": rel_path, "node_summary": sorted(content_summary, key=lambda x: x["line"])}
            )

        # Sort for deterministic output
        results.sort(key=lambda x: x["file"])

        out: dict[str, Any] = {
            "status": "ok",
            "summary": f"Found {len(results)} file(s) matching patterns: {patterns}",
            "patterns_used": patterns,
            "results": results,
        }
        out["_hints"] = generate_hints("find_files_by_pattern", out, get_session())
        return out
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Tool 10: find_large_functions
# ---------------------------------------------------------------------------


def find_large_functions(
    min_lines: int = 50,
    kind: str | None = None,
    file_path_pattern: str | None = None,
    limit: int = 50,
    repo_root: str | None = None,
) -> dict[str, Any]:
    """Find functions, classes, or files exceeding a line-count threshold.

    Useful for identifying decomposition targets, code-quality audits,
    and enforcing size limits during code review.

    Args:
        min_lines: Minimum line count to flag (default: 50).
        kind: Filter by node kind: Function, Class, File, or Test.
        file_path_pattern: Filter by file path substring (e.g. "components/").
        limit: Maximum results (default: 50).
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        Oversized nodes with line counts, ordered largest first.
    """
    store, root = _get_store(repo_root)
    try:
        nodes = store.get_nodes_by_size(
            min_lines=min_lines,
            kind=kind,
            file_path_pattern=file_path_pattern,
            limit=limit,
        )

        results = []
        for n in nodes:
            d = node_to_dict(n)
            d["line_count"] = (n.line_end - n.line_start + 1) if n.line_start and n.line_end else 0
            # Make file_path relative for readability
            try:
                d["relative_path"] = str(Path(n.file_path).relative_to(root))
            except ValueError:
                d["relative_path"] = n.file_path
            results.append(d)

        summary_parts = [
            f"Found {len(results)} node(s) with >= {min_lines} lines"
            + (f" (kind={kind})" if kind else "")
            + (f" matching '{file_path_pattern}'" if file_path_pattern else "")
            + ":",
        ]
        for r in results[:10]:
            summary_parts.append(
                f"  {r['line_count']:>4} lines | {r['kind']:>8} | "
                f"{r['name']} ({r['relative_path']}:{r['line_start']})"
            )
        if len(results) > 10:
            summary_parts.append(f"  ... and {len(results) - 10} more")

        result: dict[str, Any] = {
            "status": "ok",
            "summary": "\n".join(summary_parts),
            "total_found": len(results),
            "min_lines": min_lines,
            "results": results,
        }
        result["_hints"] = generate_hints("find_large_functions", result, get_session())
        return result
    finally:
        store.close()
