"""Tools 13, 14, 15: community listing, detail, architecture overview."""

from __future__ import annotations

from typing import Any

from ..communities import get_architecture_overview, get_communities
from ..graph import node_to_dict
from ..hints import generate_hints, get_session
from ._common import _get_store, graph_error


def _is_test_community(c: dict) -> bool:
    """Return True if community name suggests it is test-dominated."""
    name = (c.get("name") or "").lower()
    return (
        name.startswith("test")
        or "/test" in name
        or "\\test" in name
    )


# ---------------------------------------------------------------------------
# Tool 13: list_communities  [EXPLORE]
# ---------------------------------------------------------------------------


def list_communities_func(
    repo_root: str | None = None,
    sort_by: str = "size",
    min_size: int = 0,
    exclude_tests: bool = False,
) -> dict[str, Any]:
    """List detected code communities in the codebase.

    [EXPLORE] Retrieves stored communities from the knowledge graph.
    Each community represents a cluster of related code entities
    (functions, classes) detected via the Leiden algorithm or
    file-based grouping.

    Args:
        repo_root: Repository root path. Auto-detected if omitted.
        sort_by: Sort column: size, cohesion, or name.
        min_size: Minimum community size to include (default: 0).
        exclude_tests: If True, exclude test-dominated communities. Default: False.

    Returns:
        List of communities with size and cohesion scores.
    """
    store, root = _get_store(repo_root)
    try:
        communities = get_communities(store, sort_by=sort_by, min_size=min_size)
        # Strip 'members' field from each community in the list
        communities_out = [
            {k: v for k, v in c.items() if k != "members"} for c in communities
        ]
        if exclude_tests:
            communities_out = [
                c for c in communities_out if not _is_test_community(c)
            ]
        result: dict[str, object] = {
            "status": "ok",
            "summary": f"Found {len(communities_out)} communities",
            "communities": communities_out,
        }
        result["_hints"] = generate_hints("list_communities", result, get_session())
        return result
    except Exception as exc:
        return graph_error("PARSE_ERROR", str(exc))
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Tool 14: get_community  [EXPLORE]
# ---------------------------------------------------------------------------


def get_community_func(
    community_name: str | None = None,
    community_id: int | None = None,
    include_members: bool = False,
    repo_root: str | None = None,
) -> dict[str, Any]:
    """Get details of a single code community.

    [EXPLORE] Retrieves a community by its database ID or by name match.
    Optionally includes the full list of member nodes.

    Args:
        community_name: Name to search for (partial match). Ignored if
                        community_id given.
        community_id: Database ID of the community.
        include_members: If True, include full member node details.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        Community details, or not_found status.
    """
    store, root = _get_store(repo_root)
    try:
        community: dict | None = None
        all_communities = get_communities(store)

        if community_id is not None:
            for c in all_communities:
                if c.get("id") == community_id:
                    community = c
                    break
        elif community_name is not None:
            matches = [c for c in all_communities if community_name.lower() in c["name"].lower()]
            if len(matches) == 1:
                community = matches[0]
            elif len(matches) > 1:
                return {
                    "status": "ambiguous",
                    "summary": (
                        f"Multiple communities match '{community_name}'. "
                        f"Use community_id to select one."
                    ),
                    "matches": [{"id": c["id"], "name": c["name"]} for c in matches],
                }

        if community is None:
            return {
                "status": "not_found",
                "summary": ("No community found matching the given criteria."),
                "_hints": {"next_actions": ["list_communities_tool"]},
            }

        if include_members:
            cid = community.get("id")
            if cid is not None:
                member_nodes = store.get_nodes_by_community_id(cid)
                members = [node_to_dict(n) for n in member_nodes]
                community["member_details"] = members

        # Build a clean community dict — exclude 'members' unless members were requested
        community_out = {k: v for k, v in community.items() if k != "members"}
        if not include_members:
            # Also exclude member_details if it somehow snuck in
            community_out.pop("member_details", None)

        result = {
            "status": "ok",
            "summary": (
                f"Community '{community['name']}': "
                f"{community['size']} nodes, "
                f"cohesion {community['cohesion']:.4f}"
            ),
            "community": community_out,
            "member_count": len(community.get("members", [])),
        }
        result["_hints"] = generate_hints("get_community", result, get_session())
        if include_members:
            result["_hints"]["next_steps"] = [
                {
                    "tool": "query_graph_tool",
                    "suggestion": "Explore callers/callees of community members with callers_of pattern",
                },
                {
                    "tool": "trace_dataflow_tool",
                    "suggestion": "Trace data flow through community members",
                },
                {
                    "tool": "get_flow_tool",
                    "suggestion": "See execution flows involving community members",
                },
            ]
        return result
    except Exception as exc:
        return graph_error("PARSE_ERROR", str(exc))
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Tool 15: get_architecture_overview  [EXPLORE]
# ---------------------------------------------------------------------------


def get_architecture_overview_func(
    repo_root: str | None = None,
    exclude_tests: bool = False,
) -> dict[str, Any]:
    """Generate an architecture overview based on community structure.

    [EXPLORE] Builds a high-level view of the codebase architecture by
    analyzing community boundaries and cross-community coupling.
    Returns compact community summaries (no member lists) and aggregated
    cross-community coupling counts instead of individual edges.
    Includes warnings for high coupling between communities.

    Args:
        repo_root: Repository root path. Auto-detected if omitted.
        exclude_tests: If True, exclude test-dominated communities from the
                       overview. Default: False.

    Returns:
        Architecture overview with compact communities, aggregated
        cross-community coupling, and warnings.
    """
    store, root = _get_store(repo_root)
    try:
        overview = get_architecture_overview(store)

        # Strip member lists from communities — keep only summary fields
        communities_compact = []
        for c in overview.get("communities", []):
            communities_compact.append({
                "id": c.get("id"),
                "name": c.get("name"),
                "size": c.get("size"),
                "cohesion": c.get("cohesion"),
                "dominant_language": c.get("dominant_language"),
                "member_count": len(c.get("members", c.get("member_qns", []))),
            })

        if exclude_tests:
            communities_compact = [c for c in communities_compact if not _is_test_community(c)]

        # Compress cross-community edges into pair counts (de-duplicated)
        cross_pairs: dict[str, int] = {}
        included_ids = {c.get("id") for c in communities_compact}
        comm_name_by_id = {c.get("id"): c.get("name") for c in communities_compact}
        for e in overview.get("cross_community_edges", []):
            if exclude_tests and (
                e.get("source_community") not in included_ids
                or e.get("target_community") not in included_ids
            ):
                continue
            src_id = e.get("source_community")
            tgt_id = e.get("target_community")
            src_name = comm_name_by_id.get(src_id, f"community-{src_id}")
            tgt_name = comm_name_by_id.get(tgt_id, f"community-{tgt_id}")
            # Use sorted pair key to deduplicate direction
            pair_key = " <-> ".join(sorted([src_name, tgt_name]))
            cross_pairs[pair_key] = cross_pairs.get(pair_key, 0) + 1

        # Sort by count descending
        cross_coupling = [
            {"communities": k, "edge_count": v}
            for k, v in sorted(cross_pairs.items(), key=lambda x: -x[1])
        ]

        n_communities = len(communities_compact)
        n_cross_pairs = len(cross_coupling)
        n_warnings = len(overview["warnings"])
        result = {
            "status": "ok",
            "summary": (
                f"Architecture: {n_communities} communities, "
                f"{n_cross_pairs} cross-community pairs, "
                f"{n_warnings} warning(s)"
            ),
            "communities": communities_compact,
            "cross_community_coupling": cross_coupling,
            "warnings": overview["warnings"],
            "total_communities": n_communities,
            "total_cross_pairs": n_cross_pairs,
        }
        result["_hints"] = generate_hints("get_architecture_overview", result, get_session())
        return result
    except Exception as exc:
        return graph_error("PARSE_ERROR", str(exc))
    finally:
        store.close()
