"""Tools 21-24: list_repos, cross_repo_search, register_repo, unregister_repo."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..graph import GraphStore
from ..response import graph_error
from ..search import hybrid_search

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool 21: list_repos  [REGISTRY]
# ---------------------------------------------------------------------------


def list_repos_func() -> dict[str, Any]:
    """List all registered repositories.

    [REGISTRY] Returns the list of repositories registered in the global
    multi-repo registry at ``~/.code-review-graph/registry.json``.

    Returns:
        List of registered repos with paths and aliases.
    """
    from ..registry import Registry

    try:
        registry = Registry()
        repos = registry.list_repos()
        return {
            "status": "ok",
            "summary": f"{len(repos)} registered repository(ies)",
            "repos": repos,
        }
    except Exception as exc:
        return graph_error("PARSE_ERROR", str(exc))


# ---------------------------------------------------------------------------
# Tool 22: cross_repo_search  [REGISTRY]
# ---------------------------------------------------------------------------


def cross_repo_search_func(
    query: str,
    kind: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Search across all registered repositories.

    [REGISTRY] Runs hybrid_search on each registered repo's graph database
    and merges the results.

    Args:
        query: Search query string.
        kind: Optional node kind filter (e.g. "Function", "Class").
        limit: Maximum results per repo (default: 20).

    Returns:
        Combined search results from all registered repos.
    """
    from ..registry import Registry

    try:
        registry = Registry()
        repos = registry.list_repos()
        if not repos:
            return {
                "status": "ok",
                "summary": ("No repositories registered. Use 'register' to add repos."),
                "results": [],
            }

        all_results: list[dict[str, Any]] = []
        searched_repos: list[str] = []

        for repo_entry in repos:
            repo_path = Path(repo_entry["path"])
            db_path = repo_path / ".code-review-graph" / "graph.db"
            if not db_path.exists():
                continue

            try:
                store = GraphStore(str(db_path))
                try:
                    results = hybrid_search(store, query, kind=kind, limit=limit)
                    alias = repo_entry.get("alias", repo_path.name)
                    for r in results:
                        r["repo"] = alias
                        r["repo_path"] = str(repo_path)
                    all_results.extend(results)
                    searched_repos.append(alias)
                finally:
                    store.close()
            except Exception as exc:
                logger.warning("Search failed for %s: %s", repo_path, exc)

        # Sort all results by score descending
        all_results.sort(key=lambda r: r.get("score", 0), reverse=True)

        return {
            "status": "ok",
            "summary": (
                f"Found {len(all_results)} result(s) across "
                f"{len(searched_repos)} repo(s) for '{query}'"
            ),
            "results": all_results[:limit],
            "repos_searched": searched_repos,
        }
    except Exception as exc:
        return graph_error("PARSE_ERROR", str(exc))


# ---------------------------------------------------------------------------
# Tool 23: register_repo  [REGISTRY]
# ---------------------------------------------------------------------------


def register_repo_func(
    path: str,
    alias: str | None = None,
) -> dict[str, Any]:
    """Register a repository in the multi-repo registry.

    [REGISTRY] Adds a repository path to the global registry at
    ``~/.code-review-graph/registry.json`` so it can be searched via
    ``cross_repo_search_tool``.

    The repository must already have a built graph (run
    ``code-review-graph build`` in that repo first).

    Args:
        path: Absolute path to the repository root.
        alias: Optional short name for the repo (defaults to directory name).

    Returns:
        Registered repo entry with path and alias.
    """
    from ..registry import Registry

    try:
        registry = Registry()
        entry = registry.register(path, alias=alias)
        alias = entry.get("alias", Path(entry["path"]).name)
        return {
            "status": "ok",
            "summary": f"Registered '{alias}' at {entry['path']}",
            "repo": entry,
            "next_actions": ["list_repos_tool", "cross_repo_search_tool"],
        }
    except Exception as exc:
        return graph_error("REGISTRY_ERROR", str(exc))


# ---------------------------------------------------------------------------
# Tool 24: unregister_repo  [REGISTRY]
# ---------------------------------------------------------------------------


def unregister_repo_func(
    path_or_alias: str,
) -> dict[str, Any]:
    """Unregister a repository from the multi-repo registry.

    [REGISTRY] Removes a repository from the global registry at
    ``~/.code-review-graph/registry.json``. The repository files are
    NOT deleted — only the registry entry is removed.

    Args:
        path_or_alias: Repository path or alias to remove.

    Returns:
        Success/failure status.
    """
    from ..registry import Registry

    try:
        registry = Registry()
        removed = registry.unregister(path_or_alias)
        if removed:
            return {
                "status": "ok",
                "summary": f"Unregistered '{path_or_alias}'",
                "removed": True,
            }
        else:
            return graph_error(
                "REGISTRY_NOT_FOUND",
                f"No registered repo matching '{path_or_alias}'",
            )
    except Exception as exc:
        return graph_error("REGISTRY_ERROR", str(exc))
