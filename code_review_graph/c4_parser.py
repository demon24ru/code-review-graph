"""Parse and serialize Mermaid C4 DSL diagrams.

This is a pure data module with no dependencies on the rest of the codebase.
It converts C4 architecture text (Mermaid C4 DSL) into structured Python
dataclasses and back, with round-trip fidelity.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Dataclasses
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class C4Element:
    """One C4 element: Person, System, Container, Component, Rel, etc."""

    kind: str
    """Element kind: Container | Component | Person | System | System_Ext |
    Rel | ContainerDb | UpdateElementStyle | Container_Boundary."""
    id: str
    """First argument — the identifier."""
    label: str
    """Second argument — human-readable label."""
    technology: str = ""
    description: str = ""
    target_id: str = ""
    """For Rel elements: the target identifier."""
    style_props: dict = field(default_factory=dict)
    """For UpdateElementStyle: dollar-prefixed key→value pairs."""
    raw_line: str = ""
    """Original source line for exact round-trip."""


@dataclass
class C4Section:
    """A block between %% [AUTO:X] … %% [/AUTO:X] or FEATURE markers."""

    marker_type: str
    """AUTO or FEATURE."""
    marker_id: str
    """E.g. containers | oauth:t1 | components:auth."""
    timestamp: str
    """E.g. 2026-05-01."""
    elements: list[C4Element] = field(default_factory=list)


@dataclass
class C4Diagram:
    """One diagram (C4Context / C4Container / C4Component)."""

    title: str
    diagram_type: str
    """C4Context | C4Container | C4Component."""
    sections: list[C4Section] = field(default_factory=list)
    loose_elements: list[C4Element] = field(default_factory=list)


@dataclass
class C4Architecture:
    """Full architecture file — collection of diagrams."""

    diagrams: list[C4Diagram] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Regex patterns
# ─────────────────────────────────────────────────────────────────────────────

# Marker patterns: %% [AUTO:id timestamp] or %% [/AUTO:id]
_MARKER_OPEN_RE = re.compile(
    r"%%\s+\[(AUTO|FEATURE):([^\]\s]+)(?:\s+([^\]]*))?\]"
)
_MARKER_CLOSE_RE = re.compile(r"%%\s+\[/(AUTO|FEATURE):([^\]]+)\]")

# Quoted string in C4 argument lists
_QUOTED_RE = re.compile(r'"([^"]*)"')

# UpdateElementStyle prop: $key="value"
_STYLE_PROP_RE = re.compile(r'\$(\w+)="([^"]*)"')


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────


def _extract_args(argstr: str) -> list[str]:
    """Extract positional arguments from C4 argument string.

    Handles:
    - Bare identifiers (no quotes)
    - Quoted strings

    Args:
        argstr: Raw arguments portion inside parentheses.

    Returns:
        List of string argument values, unquoted.
    """
    args: list[str] = []
    i = 0
    s = argstr.strip()
    while i < len(s):
        if s[i] == '"':
            # Quoted string
            end = s.find('"', i + 1)
            if end == -1:
                args.append(s[i + 1 :])
                break
            args.append(s[i + 1 : end])
            i = end + 1
            # skip comma + whitespace
            while i < len(s) and s[i] in ", ":
                i += 1
        elif s[i] == "$":
            # Style prop — stop here; handled separately
            break
        else:
            # Bare identifier up to comma or end
            end = s.find(",", i)
            if end == -1:
                args.append(s[i:].strip())
                break
            args.append(s[i:end].strip())
            i = end + 1
            while i < len(s) and s[i] == " ":
                i += 1
    return args


def _parse_element(stripped: str, raw_line: str) -> Optional[C4Element]:
    """Parse a single C4 DSL line into a C4Element.

    Args:
        stripped: Line with leading/trailing whitespace removed.
        raw_line: Original unmodified line (stored for round-trip).

    Returns:
        C4Element if the line is recognized, None otherwise.
    """
    # Match KEYWORD(args)
    m = re.match(r"^(\w+)\((.+)\)\s*$", stripped, re.DOTALL)
    if not m:
        return None

    kind = m.group(1)
    inner = m.group(2)

    # UpdateElementStyle — parse dollar props separately
    if kind == "UpdateElementStyle":
        # First arg is the id (bare), rest are $key="value"
        comma_pos = inner.find(",")
        if comma_pos == -1:
            elem_id = inner.strip()
            style_props: dict[str, str] = {}
        else:
            elem_id = inner[:comma_pos].strip()
            prop_str = inner[comma_pos + 1 :]
            style_props = dict(_STYLE_PROP_RE.findall(prop_str))
        return C4Element(
            kind=kind,
            id=elem_id,
            label="",
            style_props=style_props,
            raw_line=raw_line,
        )

    args = _extract_args(inner)

    if kind in ("Container", "ContainerDb"):
        # Container(id, "label", "tech", "desc")
        elem_id = args[0] if len(args) > 0 else ""
        label = args[1] if len(args) > 1 else ""
        technology = args[2] if len(args) > 2 else ""
        description = args[3] if len(args) > 3 else ""
        return C4Element(
            kind=kind,
            id=elem_id,
            label=label,
            technology=technology,
            description=description,
            raw_line=raw_line,
        )

    if kind == "Component":
        # Component(id, "label", "tech", "desc")
        elem_id = args[0] if len(args) > 0 else ""
        label = args[1] if len(args) > 1 else ""
        technology = args[2] if len(args) > 2 else ""
        description = args[3] if len(args) > 3 else ""
        return C4Element(
            kind=kind,
            id=elem_id,
            label=label,
            technology=technology,
            description=description,
            raw_line=raw_line,
        )

    if kind in ("Person", "System", "System_Ext"):
        # Person(id, "label", "desc")
        elem_id = args[0] if len(args) > 0 else ""
        label = args[1] if len(args) > 1 else ""
        description = args[2] if len(args) > 2 else ""
        return C4Element(
            kind=kind,
            id=elem_id,
            label=label,
            description=description,
            raw_line=raw_line,
        )

    if kind == "Container_Boundary":
        # Container_Boundary(id, "label")
        elem_id = args[0] if len(args) > 0 else ""
        label = args[1] if len(args) > 1 else ""
        return C4Element(kind=kind, id=elem_id, label=label, raw_line=raw_line)

    if kind == "Rel":
        # Rel(source, target, "label", "tech")
        source_id = args[0] if len(args) > 0 else ""
        target_id = args[1] if len(args) > 1 else ""
        label = args[2] if len(args) > 2 else ""
        technology = args[3] if len(args) > 3 else ""
        return C4Element(
            kind=kind,
            id=source_id,
            label=label,
            technology=technology,
            target_id=target_id,
            raw_line=raw_line,
        )

    logger.debug("Unrecognized C4 element kind: %s", kind)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Diagram parser
# ─────────────────────────────────────────────────────────────────────────────


def _parse_diagram(title: str, body: str) -> C4Diagram:
    """Parse a single diagram block.

    Args:
        title: Diagram title string.
        body: Multi-line body text of the diagram.

    Returns:
        Populated C4Diagram instance.
    """
    diagram_type = "C4Context"
    sections: list[C4Section] = []
    loose_elements: list[C4Element] = []

    current_section: Optional[C4Section] = None

    lines = body.splitlines()
    for raw_line in lines:
        stripped = raw_line.strip()

        if not stripped:
            continue

        # Detect diagram type declaration
        if stripped in ("C4Context", "C4Container", "C4Component"):
            diagram_type = stripped
            continue

        # Opening section marker
        open_m = _MARKER_OPEN_RE.match(stripped)
        if open_m:
            marker_type = open_m.group(1)
            marker_id = open_m.group(2)
            timestamp = (open_m.group(3) or "").strip()
            current_section = C4Section(
                marker_type=marker_type,
                marker_id=marker_id,
                timestamp=timestamp,
            )
            continue

        # Closing section marker
        close_m = _MARKER_CLOSE_RE.match(stripped)
        if close_m:
            if current_section is not None:
                sections.append(current_section)
                current_section = None
            continue

        # Skip bare comment lines (not markers)
        if stripped.startswith("%%"):
            continue

        # Skip closing braces from Container_Boundary
        if stripped == "}":
            continue

        # Skip title lines inside body (already captured)
        if stripped.startswith("title"):
            continue

        # Try parsing as an element
        elem = _parse_element(stripped, raw_line)
        if elem is not None:
            if current_section is not None:
                current_section.elements.append(elem)
            else:
                loose_elements.append(elem)
        else:
            logger.debug("Skipping unrecognized line: %r", stripped)

    # Unclosed section — still collect it
    if current_section is not None:
        logger.warning("Section %r was not closed", current_section.marker_id)
        sections.append(current_section)

    return C4Diagram(
        title=title,
        diagram_type=diagram_type,
        sections=sections,
        loose_elements=loose_elements,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Public parse API
# ─────────────────────────────────────────────────────────────────────────────


def parse_c4_file(content: str) -> C4Architecture:
    """Parse a full architecture C4 file with multiple diagrams.

    Diagrams are separated by ``---`` lines. Each diagram begins with a
    ``title: ...`` metadata line.

    Args:
        content: Full text content of the architecture file.

    Returns:
        C4Architecture with all parsed diagrams.
    """
    arch = C4Architecture()

    # Split on --- separators (front-matter style)
    blocks = re.split(r"^---\s*$", content, flags=re.MULTILINE)

    # Blocks alternate: [preamble, frontmatter, body, frontmatter, body, ...]
    # Front-matter blocks contain 'title:' lines; body blocks contain diagram DSL
    i = 0
    while i < len(blocks):
        block = blocks[i]
        # Try to find a title in this block
        title_m = re.search(r"^title\s*:\s*(.+)$", block, re.MULTILINE)
        if title_m:
            title = title_m.group(1).strip()
            # Body is the next block
            body = blocks[i + 1] if i + 1 < len(blocks) else ""
            diagram = _parse_diagram(title, body)
            arch.diagrams.append(diagram)
            i += 2
        else:
            i += 1

    return arch


# ─────────────────────────────────────────────────────────────────────────────
# Serialization
# ─────────────────────────────────────────────────────────────────────────────


def _quote(s: str) -> str:
    """Wrap string in double quotes.

    Args:
        s: Value to quote.

    Returns:
        Quoted string.
    """
    return f'"{s}"'


def _write_element(elem: C4Element) -> str:
    """Serialize one C4Element to its Mermaid DSL line.

    Args:
        elem: The element to serialize.

    Returns:
        Single-line Mermaid C4 DSL string (no trailing newline).
    """
    k = elem.kind

    if k == "UpdateElementStyle":
        props = ", ".join(f'${key}="{val}"' for key, val in elem.style_props.items())
        if props:
            return f"UpdateElementStyle({elem.id}, {props})"
        return f"UpdateElementStyle({elem.id})"

    if k in ("Container", "ContainerDb", "Component"):
        parts = [elem.id, _quote(elem.label)]
        if elem.technology or elem.description:
            parts.append(_quote(elem.technology))
        if elem.description:
            parts.append(_quote(elem.description))
        return f"{k}({', '.join(parts)})"

    if k in ("Person", "System", "System_Ext"):
        parts = [elem.id, _quote(elem.label)]
        if elem.description:
            parts.append(_quote(elem.description))
        return f"{k}({', '.join(parts)})"

    if k == "Container_Boundary":
        return f"Container_Boundary({elem.id}, {_quote(elem.label)}) {{"

    if k == "Rel":
        parts = [elem.id, elem.target_id, _quote(elem.label)]
        if elem.technology:
            parts.append(_quote(elem.technology))
        return f"Rel({', '.join(parts)})"

    # Fallback: best-effort
    return f"{k}({elem.id}, {_quote(elem.label)})"


def write_c4_file(arch: C4Architecture) -> str:
    """Serialize a C4Architecture back to Mermaid C4 text.

    Args:
        arch: The architecture to serialize.

    Returns:
        Multi-line string with ``---`` separators between diagrams.
    """
    parts: list[str] = []

    for diagram in arch.diagrams:
        # Front-matter block
        parts.append("---")
        parts.append(f"title: {diagram.title}")
        parts.append("---")

        # Diagram type declaration
        parts.append(diagram.diagram_type)
        parts.append("")

        # Loose elements (before any section)
        for elem in diagram.loose_elements:
            parts.append(_write_element(elem))

        if diagram.loose_elements and diagram.sections:
            parts.append("")

        # Sections
        for section in diagram.sections:
            ts = f" {section.timestamp}" if section.timestamp else ""
            parts.append(f"%% [{section.marker_type}:{section.marker_id}{ts}]")
            for elem in section.elements:
                parts.append(f"  {_write_element(elem)}")
            parts.append(f"%% [/{section.marker_type}:{section.marker_id}]")
            parts.append("")

        parts.append("")

    return "\n".join(parts)
