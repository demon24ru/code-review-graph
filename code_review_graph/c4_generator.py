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
    - C4Container (one Container_Boundary per community with file Container children;
      cross-community Rels at the top level)
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

    # -------------------------------------------------------------------
    # C4Container diagram
    #
    # Grouping strategy:
    #   - A file may appear in many small communities (Leiden artefact).
    #     We assign each file to its *dominant* community — the one that
    #     contributes the most nodes to that file.
    #   - Communities that end up with 2+ distinct files are rendered as
    #     Container_Boundary(community) { Container(file) … }
    #   - Communities that end up with exactly 1 file are collapsed: the
    #     file is emitted as a flat Container (no wrapping boundary).
    #   - Communities with no file data fall back to a flat Container node.
    # -------------------------------------------------------------------

    # Step 1: collect every (file_path, comm_id) → node count
    file_comm_count: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    comm_info: dict[int, Any] = {c["id"]: c for c in communities}

    for comm in communities:
        for n in store.get_nodes_by_community_id(comm["id"], exclude_tests=True):
            if n.file_path:
                file_comm_count[n.file_path][comm["id"]] += 1

    # Step 2: assign each file to its dominant community
    file_dominant_comm: dict[str, int] = {}
    for file_path, comm_counts in file_comm_count.items():
        file_dominant_comm[file_path] = max(comm_counts, key=lambda k: comm_counts[k])

    # Step 3: group files by dominant community
    comm_files: dict[int, list[str]] = defaultdict(list)
    for file_path, comm_id in file_dominant_comm.items():
        comm_files[comm_id].append(file_path)

    # Step 4: collect per-file metadata (prefer File-kind node)
    file_meta: dict[str, dict[str, Any]] = {}
    for comm in communities:
        for n in store.get_nodes_by_community_id(comm["id"], exclude_tests=True):
            fp = n.file_path
            if not fp:
                continue
            if fp not in file_meta:
                file_meta[fp] = {"label": Path(fp).name, "tech": n.language or "", "desc": ""}
            if n.kind == "File":
                n_lines = (n.line_end or 1) - (n.line_start or 1) + 1
                file_meta[fp] = {
                    "label": n.name,
                    "tech": n.language or "",
                    "desc": f"{n_lines} lines",
                }

    def _make_file_container(fp: str) -> C4Element:
        meta = file_meta.get(fp, {"label": Path(fp).name, "tech": "", "desc": ""})
        if not meta["desc"]:
            meta["desc"] = f"{len(file_comm_count[fp])} communities"
        return C4Element(
            kind="Container",
            id=_slugify(fp),
            label=meta["label"],
            technology=meta["tech"],
            description=meta["desc"],
        )

    container_elements: list[C4Element] = []
    processed_comm_ids: set[int] = set()
    # Maps community_id → the actual id used for its top-level Container/Boundary
    # element.  Single-file communities use _slugify(file_path) while multi-file
    # and no-file communities use _slugify(comm_name).  Rel elements must use
    # the same id so they reference known elements.
    comm_id_to_container_slug: dict[int, str] = {}

    for comm in communities:
        comm_id = comm["id"]
        files = sorted(comm_files.get(comm_id, []))

        if not files:
            # No file data — emit a flat Container for the community itself
            slug = _slugify(comm["name"])
            comm_id_to_container_slug[comm_id] = slug
            container_elements.append(
                C4Element(
                    kind="Container",
                    id=slug,
                    label=comm["name"],
                    technology=comm.get("dominant_language", ""),
                    description=f"{comm['size']} nodes",
                )
            )
            processed_comm_ids.add(comm_id)
        elif len(files) == 1:
            # Single file → flat Container, no boundary wrapper
            fp = files[0]
            slug = _slugify(fp)
            comm_id_to_container_slug[comm_id] = slug
            container_elements.append(_make_file_container(fp))
            processed_comm_ids.add(comm_id)
        else:
            # Multiple files → Container_Boundary wrapping file Containers
            slug = _slugify(comm["name"])
            comm_id_to_container_slug[comm_id] = slug
            boundary = C4Element(
                kind="Container_Boundary",
                id=slug,
                label=comm["name"],
                children=[_make_file_container(fp) for fp in files],
            )
            container_elements.append(boundary)
            processed_comm_ids.add(comm_id)

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
        src_slug = comm_id_to_container_slug.get(src_id)
        tgt_slug = comm_id_to_container_slug.get(tgt_id)
        # Skip if either community has no container element (e.g. filtered out)
        if src_slug is None or tgt_slug is None:
            continue
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
    # C4Component diagrams — one per Container group
    #
    # Each group corresponds to one top-level element in Container diagram:
    #   - multi-file community → one Component diagram for the whole community
    #   - single-file community → one Component diagram for that file only
    # -------------------------------------------------------------------
    component_diagrams: list[C4Diagram] = []
    all_graph_edges = store.get_all_edges()

    # Build a helper that generates a Component diagram for a given set of nodes
    def _build_component_diagram(
        title: str,
        marker_id: str,
        member_nodes: list[Any],
    ) -> C4Diagram:
        members_set: set[str] = {n.qualified_name for n in member_nodes}

        private_helper_qns: set[str] = {
            n.qualified_name
            for n in member_nodes
            if n.name.startswith("_") and not n.name.startswith("__")
        }
        visible_qns: set[str] = members_set - private_helper_qns

        incoming_calls_count: dict[str, int] = defaultdict(int)
        calls_adj: dict[str, set[str]] = defaultdict(set)
        contains_map: dict[str, list[str]] = defaultdict(list)

        for edge in all_graph_edges:
            src, tgt = edge.source_qualified, edge.target_qualified
            if edge.kind == "CALLS" and src in members_set and tgt in members_set:
                incoming_calls_count[tgt] += 1
                calls_adj[src].add(tgt)
            elif edge.kind == "CONTAINS" and src in members_set and tgt in members_set:
                contains_map[src].append(tgt)

        qn_to_slug: dict[str, str] = {}
        for node in member_nodes:
            if node.qualified_name in private_helper_qns:
                continue
            raw = node.qualified_name if len(node.qualified_name) <= 60 else node.name
            qn_to_slug[node.qualified_name] = _slugify(raw)

        qn_to_node = {n.qualified_name: n for n in member_nodes}
        placed_qns: set[str] = set()
        component_elements: list[C4Element] = []

        for file_node in member_nodes:
            if file_node.kind != "File" or file_node.qualified_name in private_helper_qns:
                continue
            fqn = file_node.qualified_name
            placed_qns.add(fqn)
            file_children = [
                c for c in contains_map.get(fqn, []) if c not in private_helper_qns
            ]
            if not file_children:
                is_hub = incoming_calls_count[fqn] > 10
                desc = f"{file_node.file_path}:{file_node.line_start}"
                if is_hub:
                    desc += " (hub)"
                component_elements.append(C4Element(
                    kind="Component",
                    id=qn_to_slug.get(fqn, _slugify(file_node.name)),
                    label=file_node.name,
                    technology=file_node.kind,
                    description=desc,
                ))
                continue
            file_boundary_children: list[C4Element] = []
            for child_qn in file_children:
                placed_qns.add(child_qn)
                child_node = qn_to_node.get(child_qn)
                if child_node is None:
                    continue
                class_children = [
                    c for c in contains_map.get(child_qn, []) if c not in private_helper_qns
                ]
                if child_node.kind == "Class" and class_children:
                    method_elems: list[C4Element] = []
                    for method_qn in class_children:
                        placed_qns.add(method_qn)
                        method_node = qn_to_node.get(method_qn)
                        if method_node is None:
                            continue
                        is_hub = incoming_calls_count[method_qn] > 10
                        desc = f"{method_node.file_path}:{method_node.line_start}"
                        if is_hub:
                            desc += " (hub)"
                        method_elems.append(C4Element(
                            kind="Component",
                            id=qn_to_slug.get(method_qn, _slugify(method_node.name)),
                            label=method_node.name,
                            technology=method_node.kind,
                            description=desc,
                        ))
                    file_boundary_children.append(C4Element(
                        kind="Component_Boundary",
                        id=qn_to_slug.get(child_qn, _slugify(child_node.name)),
                        label=child_node.name,
                        children=method_elems,
                    ))
                else:
                    is_hub = incoming_calls_count[child_qn] > 10
                    desc = f"{child_node.file_path}:{child_node.line_start}"
                    if is_hub:
                        desc += " (hub)"
                    file_boundary_children.append(C4Element(
                        kind="Component",
                        id=qn_to_slug.get(child_qn, _slugify(child_node.name)),
                        label=child_node.name,
                        technology=child_node.kind,
                        description=desc,
                    ))
            component_elements.append(C4Element(
                kind="Component_Boundary",
                id=qn_to_slug.get(fqn, _slugify(file_node.name)),
                label=file_node.name,
                children=file_boundary_children,
            ))

        # Orphan nodes not reachable as children of any File
        for node in member_nodes:
            if node.qualified_name in private_helper_qns or node.qualified_name in placed_qns:
                continue
            if node.qualified_name not in qn_to_slug:
                continue
            is_hub = incoming_calls_count[node.qualified_name] > 10
            desc = f"{node.file_path}:{node.line_start}"
            if is_hub:
                desc += " (hub)"
            component_elements.append(C4Element(
                kind="Component",
                id=qn_to_slug[node.qualified_name],
                label=node.name,
                technology=node.kind,
                description=desc,
            ))

        # Internal CALLS edges
        internal_rels: list[C4Element] = []
        seen_rels: set[tuple[str, str]] = set()
        for src_qn in visible_qns:
            for tgt_qn in _visible_targets_via_calls(src_qn, calls_adj, private_helper_qns):
                src_slug = qn_to_slug.get(src_qn)
                tgt_slug = qn_to_slug.get(tgt_qn)
                if src_slug and tgt_slug and src_slug != tgt_slug:
                    rel_key = (src_slug, tgt_slug)
                    if rel_key not in seen_rels:
                        seen_rels.add(rel_key)
                        internal_rels.append(C4Element(
                            kind="Rel", id=src_slug, label="calls",
                            technology="", target_id=tgt_slug,
                        ))

        for edge in all_graph_edges:
            if edge.kind in ("CALLS", "CONTAINS"):
                continue
            if edge.source_qualified in visible_qns and edge.target_qualified in visible_qns:
                src_slug = qn_to_slug.get(edge.source_qualified)
                tgt_slug = qn_to_slug.get(edge.target_qualified)
                if src_slug and tgt_slug and src_slug != tgt_slug:
                    rel_key = (src_slug, tgt_slug)
                    if rel_key not in seen_rels:
                        seen_rels.add(rel_key)
                        internal_rels.append(C4Element(
                            kind="Rel", id=src_slug, label=edge.kind.lower(),
                            technology="", target_id=tgt_slug,
                        ))

        return C4Diagram(
            title=title,
            diagram_type="C4Component",
            sections=[C4Section(
                marker_type="AUTO",
                marker_id=f"components:{marker_id}",
                timestamp=today,
                elements=component_elements + internal_rels,
            )],
        )

    # Generate one Component diagram per Container group
    for comm in communities:
        comm_id = comm["id"]
        files = sorted(comm_files.get(comm_id, []))
        if not files:
            # No-file community: pass all community member nodes
            member_nodes = store.get_nodes_by_community_id(comm_id, exclude_tests=True)
            if member_nodes:
                component_diagrams.append(_build_component_diagram(
                    title=f"{comm['name']} Components",
                    marker_id=_slugify(comm["name"]),
                    member_nodes=member_nodes,
                ))
        elif len(files) == 1:
            # Single-file: diagram keyed by file name
            fp = files[0]
            all_nodes_for_file = store.get_nodes_by_community_id(comm_id, exclude_tests=True)
            file_nodes = [n for n in all_nodes_for_file if n.file_path == fp]
            if file_nodes:
                component_diagrams.append(_build_component_diagram(
                    title=f"{Path(fp).stem} Components",
                    marker_id=_slugify(fp),
                    member_nodes=file_nodes,
                ))
        else:
            # Multi-file community: all nodes together
            member_nodes = store.get_nodes_by_community_id(comm_id, exclude_tests=True)
            if member_nodes:
                component_diagrams.append(_build_component_diagram(
                    title=f"{comm['name']} Components",
                    marker_id=_slugify(comm["name"]),
                    member_nodes=member_nodes,
                ))

    arch = C4Architecture(
        diagrams=[container_diagram, ] + component_diagrams
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

    # Rel and UpdateElementStyle are not renderable graph nodes.
    # Boundary kinds (Container_Boundary, Component_Boundary) ARE nodes —
    # they act as compound parents in Cytoscape and must be included.
    _NON_NODE_KINDS = frozenset({"Rel", "UpdateElementStyle"})

    def _collect(elem: C4Element, target: dict[str, C4Element]) -> None:
        if elem.kind not in _NON_NODE_KINDS:
            target[elem.id] = elem
        for child in elem.children:
            _collect(child, target)

    for section in target_diagram.sections:
        if section.marker_type == "AUTO":
            for elem in section.elements:
                _collect(elem, auto_elements)
        elif section.marker_type == "FEATURE":
            for elem in section.elements:
                _collect(elem, feature_elements)

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
