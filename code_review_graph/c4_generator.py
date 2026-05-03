"""Generate Mermaid C4 architecture diagrams from the code knowledge graph.

Uses community detection to produce Container diagrams (one container per
community) and Component diagrams (one per community, members as components).

[AUTO] sections are machine-generated; [FEATURE] sections are preserved when
rebuilding so that human-authored design elements survive incremental updates.
"""

from __future__ import annotations

import datetime
import logging
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from .c4_parser import (
    C4Architecture,
    C4Diagram,
    C4Element,
    C4Section,
    parse_c4_file,
    write_c4_file,
)
from .communities import get_architecture_overview, get_communities
from .graph import GraphStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slugify(name: str) -> str:
    """Convert community/node name to a valid C4 identifier.

    Lowercases the name, replaces non-alphanumeric characters with underscores,
    and strips leading/trailing underscores.

    Args:
        name: Raw name string.

    Returns:
        Slug suitable for use as a C4 element identifier.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower())
    return slug.strip("_") or "node"


def _visible_targets_via_calls(
    src: str,
    calls_adjacency: dict[str, set[str]],
    private_helper_qns: set[str],
) -> set[str]:
    """BFS from *src* through CALLS edges, collapsing private-helper hops.

    Follows CALLS edges from *src*; if a reachable node is a private helper
    (name starts with ``_`` but not ``__``), continues through *its* outgoing
    edges rather than adding it to the result.  Returns only non-helper nodes.

    Args:
        src: Qualified name of the starting node.
        calls_adjacency: Map of qualified_name → set of CALLS targets, scoped
            to the current community.
        private_helper_qns: Qualified names of private-helper nodes to skip.

    Returns:
        Set of visible (non-private-helper) qualified names reachable from
        *src* via CALLS edges, including those reached through collapsed helpers.
    """
    result: set[str] = set()
    frontier = list(calls_adjacency.get(src, set()))
    seen: set[str] = {src}
    while frontier:
        cur = frontier.pop()
        if cur in seen:
            continue
        seen.add(cur)
        if cur in private_helper_qns:
            # Transparent hop — follow this helper's outgoing edges
            for nxt in calls_adjacency.get(cur, set()):
                if nxt not in seen:
                    frontier.append(nxt)
        else:
            result.add(cur)
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_c4_path(repo_root: str) -> Path:
    """Return the canonical architecture.c4 path for a repository.

    Args:
        repo_root: Absolute path to the repository root.

    Returns:
        Path to .code-review-graph/architecture.c4.
    """
    return Path(repo_root) / ".code-review-graph" / "architecture.c4"


def build_c4(store: GraphStore, repo_name: str = "") -> str:
    """Generate a complete architecture.c4 file from the code graph.

    Produces three diagram types:
    - C4Context stub (empty, for LLM/human population)
    - C4Container (one Container per community with cross-community Rels)
    - C4Component (one diagram per community with member Components)

    Args:
        store: The GraphStore instance.
        repo_name: Optional display name for the repository.

    Returns:
        Multi-diagram C4 text ready to be written to architecture.c4.
    """
    today = str(datetime.date.today())
    display_name = repo_name or "System"

    communities = get_communities(store, exclude_tests=True)
    overview = get_architecture_overview(store, exclude_tests=True)
    cross_edges = overview.get("cross_community_edges", [])

    # community_id -> community dict for quick lookups
    comm_by_id: dict[int, dict[str, Any]] = {c["id"]: c for c in communities}

    # -------------------------------------------------------------------
    # C4Context diagram — empty stub for designers
    # -------------------------------------------------------------------
    context_diagram = C4Diagram(
        title=f"{display_name} Context",
        diagram_type="C4Context",
        sections=[],
        loose_elements=[],
    )

    # -------------------------------------------------------------------
    # C4Container diagram — one Container element per community
    # -------------------------------------------------------------------
    container_elements: list[C4Element] = []
    for comm in communities:
        elem_id = _slugify(comm["name"])
        container_elements.append(
            C4Element(
                kind="Container",
                id=elem_id,
                label=comm["name"],
                technology=comm.get("dominant_language", ""),
                description=f"{comm['size']} nodes, cohesion {comm['cohesion']:.2f}",
            )
        )

    # Aggregate cross-community edges by (src_community_id, tgt_community_id)
    # to avoid duplicate Rel lines for the same pair.
    rel_counts: dict[tuple[int, int], int] = defaultdict(int)
    for ce in cross_edges:
        src_id = ce["source_community"]
        tgt_id = ce["target_community"]
        rel_counts[(src_id, tgt_id)] += 1

    rel_elements: list[C4Element] = []
    seen_pairs: set[tuple[str, str]] = set()
    for (src_id, tgt_id), count in rel_counts.items():
        src_comm = comm_by_id.get(src_id)
        tgt_comm = comm_by_id.get(tgt_id)
        if src_comm is None or tgt_comm is None:
            continue
        src_slug = _slugify(src_comm["name"])
        tgt_slug = _slugify(tgt_comm["name"])
        pair = (src_slug, tgt_slug)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        rel_elements.append(
            C4Element(
                kind="Rel",
                id=src_slug,
                label=f"{count} edges",
                technology="",
                target_id=tgt_slug,
            )
        )

    container_diagram = C4Diagram(
        title=f"{display_name} Containers",
        diagram_type="C4Container",
        sections=[
            C4Section(
                marker_type="AUTO",
                marker_id="containers",
                timestamp=today,
                elements=container_elements,
            ),
            C4Section(
                marker_type="AUTO",
                marker_id="container_rels",
                timestamp=today,
                elements=rel_elements,
            ),
        ],
    )

    # -------------------------------------------------------------------
    # C4Component diagrams — one per community
    # -------------------------------------------------------------------
    component_diagrams: list[C4Diagram] = []
    all_graph_edges = store.get_all_edges()

    for comm in communities:
        comm_slug = _slugify(comm["name"])
        comm_id = comm["id"]
        members_set: set[str] = set(comm.get("members", []))

        # Full node details for community members
        member_nodes = store.get_nodes_by_community_id(comm_id)

        # Partition members: private helpers (leading _ but not __) vs visible
        private_helper_qns: set[str] = set()
        for node in member_nodes:
            if node.name.startswith("_") and not node.name.startswith("__"):
                private_helper_qns.add(node.qualified_name)
        visible_qns: set[str] = members_set - private_helper_qns

        # Count incoming CALLS per community node for hub detection (threshold 10)
        incoming_calls_count: dict[str, int] = defaultdict(int)
        for edge in all_graph_edges:
            if (
                edge.kind == "CALLS"
                and edge.source_qualified in members_set
                and edge.target_qualified in members_set
            ):
                incoming_calls_count[edge.target_qualified] += 1

        # Build CALLS adjacency map for private-helper collapse BFS
        calls_adj: dict[str, set[str]] = defaultdict(set)
        for edge in all_graph_edges:
            if (
                edge.kind == "CALLS"
                and edge.source_qualified in members_set
                and edge.target_qualified in members_set
            ):
                calls_adj[edge.source_qualified].add(edge.target_qualified)

        component_elements: list[C4Element] = []
        qn_to_slug: dict[str, str] = {}

        for node in member_nodes:
            if node.qualified_name in private_helper_qns:
                continue  # private helpers excluded from component diagram
            # Use qualified_name for slug if reasonably short; otherwise fall
            # back to bare name to keep identifiers manageable.
            raw = node.qualified_name if len(node.qualified_name) <= 60 else node.name
            node_slug = _slugify(raw)
            qn_to_slug[node.qualified_name] = node_slug
            is_hub = incoming_calls_count[node.qualified_name] > 10
            description = f"{node.file_path}:{node.line_start}"
            if is_hub:
                description += " (hub)"
            component_elements.append(
                C4Element(
                    kind="Component",
                    id=node_slug,
                    label=node.name,
                    technology=node.kind,
                    description=description,
                )
            )

        # Internal edges: CONTAINS filtered, private helpers collapsed in CALLS
        internal_rels: list[C4Element] = []
        seen_rels: set[tuple[str, str]] = set()

        # CALLS edges: BFS collapses any private-helper intermediaries
        for src_qn in visible_qns:
            for tgt_qn in _visible_targets_via_calls(src_qn, calls_adj, private_helper_qns):
                src_slug = qn_to_slug.get(src_qn)
                tgt_slug = qn_to_slug.get(tgt_qn)
                if src_slug and tgt_slug and src_slug != tgt_slug:
                    rel_key = (src_slug, tgt_slug)
                    if rel_key not in seen_rels:
                        seen_rels.add(rel_key)
                        internal_rels.append(
                            C4Element(
                                kind="Rel",
                                id=src_slug,
                                label="calls",
                                technology="",
                                target_id=tgt_slug,
                            )
                        )

        # Non-CALLS, non-CONTAINS edges: include only if both endpoints visible
        for edge in all_graph_edges:
            if edge.kind in ("CALLS", "CONTAINS"):
                continue  # CALLS handled above; CONTAINS are structural noise
            if (
                edge.source_qualified in visible_qns
                and edge.target_qualified in visible_qns
            ):
                src_slug = qn_to_slug.get(edge.source_qualified)
                tgt_slug = qn_to_slug.get(edge.target_qualified)
                if src_slug and tgt_slug and src_slug != tgt_slug:
                    rel_key = (src_slug, tgt_slug)
                    if rel_key not in seen_rels:
                        seen_rels.add(rel_key)
                        internal_rels.append(
                            C4Element(
                                kind="Rel",
                                id=src_slug,
                                label=edge.kind.lower(),
                                technology="",
                                target_id=tgt_slug,
                            )
                        )

        section_elements = component_elements + internal_rels
        component_diagrams.append(
            C4Diagram(
                title=f"{comm['name']} Components",
                diagram_type="C4Component",
                sections=[
                    C4Section(
                        marker_type="AUTO",
                        marker_id=f"components:{comm_slug}",
                        timestamp=today,
                        elements=section_elements,
                    )
                ],
            )
        )

    arch = C4Architecture(
        diagrams=[context_diagram, container_diagram] + component_diagrams
    )
    return write_c4_file(arch)


def rebuild_c4(store: GraphStore, existing_content: str, repo_name: str = "") -> str:
    """Update [AUTO] sections while preserving [FEATURE] sections.

    Parses the existing file to extract all [FEATURE] sections, regenerates
    fresh [AUTO] content from the current graph state, then re-attaches the
    [FEATURE] sections.  Elements that have been "graduated" (their id now
    appears in an [AUTO] section) are removed from the corresponding [FEATURE]
    section.

    Args:
        store: The GraphStore instance.
        existing_content: Current text content of architecture.c4.
        repo_name: Optional display name for the repository.

    Returns:
        Merged C4 text with fresh [AUTO] and preserved [FEATURE] sections.
    """
    existing_arch = parse_c4_file(existing_content)

    # Collect [FEATURE] sections keyed by diagram title
    feature_sections: dict[str, list[C4Section]] = defaultdict(list)
    for diagram in existing_arch.diagrams:
        for section in diagram.sections:
            if section.marker_type == "FEATURE":
                feature_sections[diagram.title].append(section)

    # Generate fresh AUTO content
    fresh_content = build_c4(store, repo_name)
    fresh_arch = parse_c4_file(fresh_content)

    # Collect AUTO element ids per diagram title for graduation check
    auto_ids_by_diagram: dict[str, set[str]] = {}
    for diagram in fresh_arch.diagrams:
        ids: set[str] = set()
        for section in diagram.sections:
            if section.marker_type == "AUTO":
                for elem in section.elements:
                    ids.add(elem.id)
        auto_ids_by_diagram[diagram.title] = ids

    # Append matching FEATURE sections to each fresh diagram
    for diagram in fresh_arch.diagrams:
        auto_ids = auto_ids_by_diagram.get(diagram.title, set())
        for feat_section in feature_sections.get(diagram.title, []):
            # Graduate: remove elements whose id now exists in AUTO
            kept = [e for e in feat_section.elements if e.id not in auto_ids]
            if kept:
                diagram.sections.append(
                    C4Section(
                        marker_type=feat_section.marker_type,
                        marker_id=feat_section.marker_id,
                        timestamp=feat_section.timestamp,
                        elements=kept,
                    )
                )

    return write_c4_file(fresh_arch)


def resolve_for_render(arch: C4Architecture, diagram_title: str) -> list[dict[str, Any]]:
    """Merge [AUTO] and [FEATURE] elements for frontend rendering.

    Computes a status for each element:
    - ``"existing"``  — element appears only in AUTO sections
    - ``"new"``       — element appears only in FEATURE sections
    - ``"modified"``  — element appears in both (FEATURE values override AUTO)

    Args:
        arch: The parsed C4Architecture.
        diagram_title: Title of the diagram to resolve.

    Returns:
        List of element dicts with keys: id, label, sublabel, status, kind.
    """
    target_diagram: C4Diagram | None = None
    for diagram in arch.diagrams:
        if diagram.title == diagram_title:
            target_diagram = diagram
            break

    if target_diagram is None:
        return []

    auto_elements: dict[str, C4Element] = {}
    feature_elements: dict[str, C4Element] = {}

    _NON_NODE_KINDS = frozenset({"Rel", "UpdateElementStyle", "Container_Boundary"})

    for section in target_diagram.sections:
        if section.marker_type == "AUTO":
            for elem in section.elements:
                if elem.kind not in _NON_NODE_KINDS:
                    auto_elements[elem.id] = elem
        elif section.marker_type == "FEATURE":
            for elem in section.elements:
                if elem.kind not in _NON_NODE_KINDS:
                    feature_elements[elem.id] = elem

    result: list[dict[str, Any]] = []

    # AUTO-only → existing
    for elem_id, elem in auto_elements.items():
        if elem_id not in feature_elements:
            result.append({
                "id": elem_id,
                "label": elem.label,
                "sublabel": elem.technology,
                "status": "existing",
                "kind": elem.kind,
            })

    # FEATURE elements — modified (if in AUTO) or new (if not)
    for elem_id, feat_elem in feature_elements.items():
        status = "modified" if elem_id in auto_elements else "new"
        result.append({
            "id": elem_id,
            "label": feat_elem.label,
            "sublabel": feat_elem.technology,
            "status": status,
            "kind": feat_elem.kind,
        })

    return result
