"""MCP tool wrappers for reading and editing the architecture.c4 skeleton file."""

from __future__ import annotations

import dataclasses
import datetime
from pathlib import Path
from typing import Any

from ..c4_generator import get_c4_path
from ..c4_parser import (
    C4Architecture,
    C4Diagram,
    C4Element,
    C4Section,
    parse_c4_file,
    write_c4_file,
)
from ..incremental import find_project_root
from ._common import _validate_repo_root, graph_error

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_root(repo_root: str | None) -> Path:
    """Resolve the repository root to an absolute Path."""
    if repo_root:
        return _validate_repo_root(Path(repo_root))
    return find_project_root()


def _get_or_create_feature_section(
    diagram: C4Diagram, feature_tag: str, today: str
) -> C4Section:
    """Return existing FEATURE section with *feature_tag*, or append a new one."""
    for section in diagram.sections:
        if section.marker_type == "FEATURE" and section.marker_id == feature_tag:
            return section
    new_section = C4Section(
        marker_type="FEATURE",
        marker_id=feature_tag,
        timestamp=today,
    )
    diagram.sections.append(new_section)
    return new_section


def _dict_to_c4element(element_dict: dict[str, Any]) -> C4Element | None:
    """Convert a plain dict to a C4Element. Returns None if required fields missing."""
    kind = element_dict.get("kind", "")
    elem_id = element_dict.get("id", "")
    label = element_dict.get("label", "")
    if not kind or not elem_id:
        return None
    return C4Element(
        kind=kind,
        id=elem_id,
        label=label,
        technology=element_dict.get("technology", ""),
        description=element_dict.get("description", ""),
        target_id=element_dict.get("target_id", ""),
        style_props=element_dict.get("style_props", {}),
    )


def _find_in_elements(elements: list[C4Element], element_id: str) -> C4Element | None:
    """Recursively search elements and their children for element_id."""
    for elem in elements:
        if elem.id == element_id:
            return elem
        found = _find_in_elements(elem.children, element_id)
        if found is not None:
            return found
    return None


def _find_element_recursive(diagram: C4Diagram, element_id: str) -> C4Element | None:
    """Find element by id in any section of diagram, recursively through children."""
    for section in diagram.sections:
        found = _find_in_elements(section.elements, element_id)
        if found is not None:
            return found
    return None


def _remove_from_children(
    elements: list[C4Element], element_id: str
) -> tuple[list[C4Element], bool]:
    """Remove element_id from elements and their children recursively.

    Returns:
        Tuple of (new_elements_list, found_flag).
    """
    new_list: list[C4Element] = []
    found = False
    for elem in elements:
        if elem.id == element_id:
            found = True
            continue
        child_list, child_found = _remove_from_children(elem.children, element_id)
        if child_found:
            found = True
            elem.children = child_list
        new_list.append(elem)
    return new_list, found


# ---------------------------------------------------------------------------
# Tool 1: get_architecture_skeleton
# ---------------------------------------------------------------------------


def get_architecture_skeleton_func(
    level: str | None = None,
    repo_root: str | None = None,
) -> dict[str, Any]:
    """Get the persistent C4 architecture skeleton.

    Returns contents of .code-review-graph/architecture.c4.
    Primary orientation tool for any LLM agent.

    Args:
        level: Optional filter:
            - None → full file content
            - "container" → C4Container diagram only
            - "component:MODULE_SLUG" → specific module's components (case-insensitive)
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        Dict with status, content, file_path, last_modified, diagrams, designed_features.
    """
    try:
        root = _resolve_root(repo_root)
    except (ValueError, FileNotFoundError) as exc:
        return graph_error("BAD_REPO_ROOT", str(exc))

    c4_path = get_c4_path(str(root))

    if not c4_path.exists():
        return {
            "status": "ok",
            "content": "",
            "file_path": str(c4_path),
            "summary": (
                "No architecture.c4 found. Run 'code-review-graph c4' to generate."
            ),
            "diagrams": 0,
            "designed_features": [],
        }

    content = c4_path.read_text(encoding="utf-8")
    arch = parse_c4_file(content)

    # Collect all FEATURE tag names across every diagram
    designed_features: list[str] = []
    for diag in arch.diagrams:
        for section in diag.sections:
            if section.marker_type == "FEATURE" and section.marker_id not in designed_features:
                designed_features.append(section.marker_id)

    mtime = datetime.datetime.fromtimestamp(c4_path.stat().st_mtime).isoformat()

    # Apply level filter
    if level is not None:
        level_lower = level.lower()
        if level_lower == "container":
            filtered = [d for d in arch.diagrams if d.diagram_type == "C4Container"]
        elif level_lower.startswith("component:"):
            slug = level[len("component:"):].lower()
            filtered = [
                d
                for d in arch.diagrams
                if d.diagram_type == "C4Component" and slug in d.title.lower()
            ]
        else:
            return graph_error(
                "INVALID_LEVEL",
                f"Unknown level filter '{level}'. "
                "Use context, containers, components, or components:SLUG.",
            )

        filtered_arch = C4Architecture(diagrams=filtered)
        filtered_content = write_c4_file(filtered_arch) if filtered else ""
        return {
            "status": "ok",
            "content": filtered_content,
            "file_path": str(c4_path),
            "last_modified": mtime,
            "diagrams": len(filtered),
            "designed_features": designed_features,
        }

    return {
        "status": "ok",
        "content": content,
        "file_path": str(c4_path),
        "last_modified": mtime,
        "diagrams": len(arch.diagrams),
        "designed_features": designed_features,
    }


# ---------------------------------------------------------------------------
# Tool 2: update_architecture_skeleton
# ---------------------------------------------------------------------------

_MODIFIABLE_FIELDS = frozenset({"label", "technology", "description", "target_id", "style_props"})


def update_architecture_skeleton_func(
    feature_tag: str,
    operations: list[dict[str, Any]],
    repo_root: str | None = None,
) -> dict[str, Any]:
    """Structured editing of architecture.c4 — add/modify/remove elements.

    LLM describes WHAT to change; the tool handles HOW in valid Mermaid C4.
    Only [FEATURE] sections are writable. [AUTO] sections are read-only.

    Operations:
        add:    {"op": "add",    "diagram": "container", "element": {kind, id, label, ...}}
        modify: {"op": "modify", "diagram": "container", "element_id": "auth",
                 "changes": {"description": "new desc"}}
        remove: {"op": "remove", "diagram": "container", "element_id": "oauth"}
        add:    {"op": "add",    "diagram": "component:MODULE_SLUG", "element": {kind, id, label, ...}}
        modify: {"op": "modify", "diagram": "component:MODULE_SLUG", "element_id": "auth",
                 "changes": {"description": "new desc"}}
        remove: {"op": "remove", "diagram": "component:MODULE_SLUG", "element_id": "oauth"}

    Args:
        feature_tag: Feature identifier, e.g. "oauth:t1".
        operations: List of structured operations.
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        Dict with status, applied, skipped, errors, updated_diagrams.
    """
    try:
        root = _resolve_root(repo_root)
    except (ValueError, FileNotFoundError) as exc:
        return graph_error("BAD_REPO_ROOT", str(exc))

    c4_path = get_c4_path(str(root))

    if not c4_path.exists():
        return graph_error(
            "NOT_FOUND",
            "architecture.c4 not found. Run 'code-review-graph c4' first.",
        )

    content = c4_path.read_text(encoding="utf-8")
    arch = parse_c4_file(content)

    applied = 0
    skipped = 0
    errors: list[str] = []
    updated_diagrams: set[str] = set()
    today = str(datetime.date.today())

    for op in operations:
        op_type = op.get("op", "")
        diagram_name = op.get("diagram", "")

        # Find target diagram by partial case-insensitive match
        target_diagram: C4Diagram | None = None
        level_lower = diagram_name.lower()
        for d in arch.diagrams:
            if level_lower == "container" and d.diagram_type == "C4Container":
                target_diagram = d
                break
            elif level_lower.startswith("component:") and d.diagram_type == "C4Component":
                slug = level_lower[len("component:"):].lower()
                if slug == d.title.lower():
                    target_diagram = d
                    break

        if target_diagram is None:
            errors.append(f"Diagram '{diagram_name}' not found")
            skipped += 1
            continue

        # ── add ──────────────────────────────────────────────────────────────
        if op_type == "add":
            element_dict = op.get("element", {})
            if not element_dict:
                errors.append("add operation missing 'element' field")
                skipped += 1
                continue

            elem = _dict_to_c4element(element_dict)
            if elem is None:
                errors.append(
                    f"Invalid element dict (must have 'kind' and 'id'): {element_dict}"
                )
                skipped += 1
                continue

            feature_section = _get_or_create_feature_section(
                target_diagram, feature_tag, today
            )
            feature_section.elements.append(elem)
            updated_diagrams.add(target_diagram.title)
            applied += 1

        # ── modify ───────────────────────────────────────────────────────────
        elif op_type == "modify":
            element_id = op.get("element_id", "")
            changes = op.get("changes", {})

            if not element_id:
                errors.append("modify operation missing 'element_id' field")
                skipped += 1
                continue

            # Find original element in any section of the target diagram (recursively)
            original = _find_element_recursive(target_diagram, element_id)

            if original is None:
                errors.append(
                    f"Element '{element_id}' not found in diagram '{diagram_name}'"
                )
                skipped += 1
                continue

            # Create modified copy in FEATURE section
            valid_changes = {k: v for k, v in changes.items() if k in _MODIFIABLE_FIELDS}
            modified = dataclasses.replace(original, **valid_changes, raw_line="")

            feature_section = _get_or_create_feature_section(
                target_diagram, feature_tag, today
            )
            feature_section.elements.append(modified)

            # Add UpdateElementStyle for visual differentiation
            style_elem = C4Element(
                kind="UpdateElementStyle",
                id=element_id,
                label="",
                style_props={"borderColor": "#ff6600", "fontColor": "#ff6600"},
            )
            feature_section.elements.append(style_elem)

            updated_diagrams.add(target_diagram.title)
            applied += 1

        # ── remove ───────────────────────────────────────────────────────────
        elif op_type == "remove":
            element_id = op.get("element_id", "")
            if not element_id:
                errors.append("remove operation missing 'element_id' field")
                skipped += 1
                continue

            # Check if element lives in an AUTO section — those are read-only
            in_auto = any(
                _find_in_elements(section.elements, element_id) is not None
                for section in target_diagram.sections
                if section.marker_type == "AUTO"
            )
            if in_auto:
                errors.append(
                    f"Element '{element_id}' is in an AUTO section and cannot be removed"
                )
                skipped += 1
                continue

            # Remove from FEATURE sections (top-level and children recursively)
            removed = False
            for section in target_diagram.sections:
                if section.marker_type == "FEATURE":
                    new_elements, section_found = _remove_from_children(
                        section.elements, element_id
                    )
                    if section_found:
                        removed = True
                        section.elements = new_elements

            if removed:
                updated_diagrams.add(target_diagram.title)
                applied += 1
            else:
                errors.append(
                    f"Element '{element_id}' not found in any FEATURE section"
                )
                skipped += 1

        # ── add_child ─────────────────────────────────────────────────────────
        elif op_type == "add_child":
            parent_id = op.get("parent_id", "")
            element_dict = op.get("element", {})

            if not parent_id:
                errors.append("add_child operation missing 'parent_id' field")
                skipped += 1
                continue

            if not element_dict:
                errors.append("add_child operation missing 'element' field")
                skipped += 1
                continue

            child_elem = _dict_to_c4element(element_dict)
            if child_elem is None:
                errors.append(
                    f"Invalid element dict (must have 'kind' and 'id'): {element_dict}"
                )
                skipped += 1
                continue

            # Cannot add children to elements in AUTO sections
            in_auto = any(
                _find_in_elements(section.elements, parent_id) is not None
                for section in target_diagram.sections
                if section.marker_type == "AUTO"
            )
            if in_auto:
                errors.append(f"Cannot add child to AUTO element '{parent_id}'")
                skipped += 1
                continue

            parent_elem = _find_element_recursive(target_diagram, parent_id)
            if parent_elem is None:
                errors.append(
                    f"Parent element '{parent_id}' not found in diagram '{diagram_name}'"
                )
                skipped += 1
                continue

            parent_elem.children.append(child_elem)
            updated_diagrams.add(target_diagram.title)
            applied += 1

        else:
            errors.append(f"Unknown operation type: '{op_type}'")
            skipped += 1

    # Write back regardless of partial failures (applied > 0 may still be useful)
    new_content = write_c4_file(arch)
    c4_path.write_text(new_content, encoding="utf-8")

    return {
        "status": "ok",
        "applied": applied,
        "skipped": skipped,
        "errors": errors,
        "updated_diagrams": sorted(updated_diagrams),
    }
