from __future__ import annotations

"""Tests for code_review_graph.c4_parser module."""

import pytest

from code_review_graph.c4_parser import (
    C4Architecture,
    C4Diagram,
    C4Element,
    C4Section,
    _parse_element,
    _write_element,
    parse_c4_file,
    write_c4_file,
)


# ─────────────────────────────────────────────────────────────────────────────
# Sample fixtures
# ─────────────────────────────────────────────────────────────────────────────

SAMPLE_C4 = """\
---
title: System Context
---
C4Context

Person(customer, "Customer", "A user of the system")
System(webapp, "Web App", "Handles requests")
System_Ext(email, "Email Service", "Sends emails")
Rel(customer, webapp, "Uses", "HTTPS")

%% [AUTO:context 2026-05-01]
  Container(api, "API", "Python", "REST endpoints")
  ContainerDb(db, "Database", "PostgreSQL", "Stores data")
%% [/AUTO:context]

%% [FEATURE:oauth:t1 2026-05-01]
  Component(auth, "Auth Module", "JWT", "Handles auth")
%% [/FEATURE:oauth:t1]

---
title: Container View
---
C4Container

%% [AUTO:containers 2026-05-01]
  Container(frontend, "Frontend", "React", "SPA")
  Container(backend, "Backend", "FastAPI", "API server")
%% [/AUTO:containers]
"""


# ─────────────────────────────────────────────────────────────────────────────
# _parse_element tests
# ─────────────────────────────────────────────────────────────────────────────


class TestParseElement:
    def test_container_full(self):
        e = _parse_element('Container(api, "API", "Python", "REST")', "")
        assert e is not None
        assert e.kind == "Container"
        assert e.id == "api"
        assert e.label == "API"
        assert e.technology == "Python"
        assert e.description == "REST"

    def test_container_no_desc(self):
        e = _parse_element('Container(api, "API", "Python")', "")
        assert e is not None
        assert e.technology == "Python"
        assert e.description == ""

    def test_container_label_only(self):
        e = _parse_element('Container(api, "API")', "")
        assert e is not None
        assert e.label == "API"
        assert e.technology == ""

    def test_component(self):
        e = _parse_element('Component(auth, "Auth Module", "JWT", "Handles auth")', "")
        assert e is not None
        assert e.kind == "Component"
        assert e.id == "auth"
        assert e.technology == "JWT"

    def test_person(self):
        e = _parse_element('Person(customer, "Customer", "A user")', "")
        assert e is not None
        assert e.kind == "Person"
        assert e.label == "Customer"
        assert e.description == "A user"

    def test_person_no_desc(self):
        e = _parse_element('Person(customer, "Customer")', "")
        assert e is not None
        assert e.description == ""

    def test_system(self):
        e = _parse_element('System(webapp, "Web App", "Handles")', "")
        assert e is not None
        assert e.kind == "System"
        assert e.id == "webapp"

    def test_system_ext(self):
        e = _parse_element('System_Ext(email, "Email", "Sends")', "")
        assert e is not None
        assert e.kind == "System_Ext"

    def test_containerdb(self):
        e = _parse_element('ContainerDb(db, "Database", "PostgreSQL", "Stores")', "")
        assert e is not None
        assert e.kind == "ContainerDb"
        assert e.technology == "PostgreSQL"

    def test_rel_full(self):
        e = _parse_element('Rel(customer, webapp, "Uses", "HTTPS")', "")
        assert e is not None
        assert e.kind == "Rel"
        assert e.id == "customer"
        assert e.target_id == "webapp"
        assert e.label == "Uses"
        assert e.technology == "HTTPS"

    def test_rel_no_tech(self):
        e = _parse_element('Rel(a, b, "calls")', "")
        assert e is not None
        assert e.technology == ""

    def test_container_boundary(self):
        e = _parse_element('Container_Boundary(group, "My Group")', "")
        assert e is not None
        assert e.kind == "Container_Boundary"
        assert e.id == "group"
        assert e.label == "My Group"

    def test_update_element_style(self):
        e = _parse_element('UpdateElementStyle(api, $bgColor="red", $fontColor="white")', "")
        assert e is not None
        assert e.kind == "UpdateElementStyle"
        assert e.id == "api"
        assert e.style_props == {"bgColor": "red", "fontColor": "white"}

    def test_update_element_style_no_props(self):
        e = _parse_element("UpdateElementStyle(api)", "")
        assert e is not None
        assert e.style_props == {}

    def test_unknown_kind_returns_none(self):
        assert _parse_element("UnknownKind(foo, bar)", "") is None

    def test_no_parens_returns_none(self):
        assert _parse_element("just a comment text", "") is None

    def test_raw_line_preserved(self):
        raw = '  Container(api, "API", "Python", "REST")'
        e = _parse_element(raw.strip(), raw)
        assert e is not None
        assert e.raw_line == raw


# ─────────────────────────────────────────────────────────────────────────────
# _write_element tests
# ─────────────────────────────────────────────────────────────────────────────


class TestWriteElement:
    def test_container_full(self):
        e = C4Element(kind="Container", id="api", label="API", technology="Python",
                      description="REST")
        line = _write_element(e)
        assert line == 'Container(api, "API", "Python", "REST")'

    def test_container_no_desc(self):
        e = C4Element(kind="Container", id="api", label="API", technology="Python")
        line = _write_element(e)
        assert line == 'Container(api, "API", "Python")'

    def test_container_label_only(self):
        e = C4Element(kind="Container", id="api", label="API")
        line = _write_element(e)
        assert line == 'Container(api, "API")'

    def test_person(self):
        e = C4Element(kind="Person", id="u", label="User", description="Customer")
        assert _write_element(e) == 'Person(u, "User", "Customer")'

    def test_person_no_desc(self):
        e = C4Element(kind="Person", id="u", label="User")
        assert _write_element(e) == 'Person(u, "User")'

    def test_system_ext(self):
        e = C4Element(kind="System_Ext", id="ext", label="External")
        assert _write_element(e) == 'System_Ext(ext, "External")'

    def test_rel(self):
        e = C4Element(kind="Rel", id="a", label="calls", target_id="b", technology="HTTP")
        assert _write_element(e) == 'Rel(a, b, "calls", "HTTP")'

    def test_rel_no_tech(self):
        e = C4Element(kind="Rel", id="a", label="calls", target_id="b")
        assert _write_element(e) == 'Rel(a, b, "calls")'

    def test_container_boundary(self):
        e = C4Element(kind="Container_Boundary", id="grp", label="Group")
        line = _write_element(e)
        assert line == 'Container_Boundary(grp, "Group") {'

    def test_update_element_style(self):
        e = C4Element(kind="UpdateElementStyle", id="api", label="",
                      style_props={"bgColor": "red"})
        assert _write_element(e) == 'UpdateElementStyle(api, $bgColor="red")'

    def test_update_element_style_empty(self):
        e = C4Element(kind="UpdateElementStyle", id="api", label="")
        assert _write_element(e) == "UpdateElementStyle(api)"


# ─────────────────────────────────────────────────────────────────────────────
# parse_c4_file tests
# ─────────────────────────────────────────────────────────────────────────────


class TestParsec4File:
    def test_two_diagrams(self):
        arch = parse_c4_file(SAMPLE_C4)
        assert len(arch.diagrams) == 2

    def test_diagram_titles(self):
        arch = parse_c4_file(SAMPLE_C4)
        assert arch.diagrams[0].title == "System Context"
        assert arch.diagrams[1].title == "Container View"

    def test_diagram_types(self):
        arch = parse_c4_file(SAMPLE_C4)
        assert arch.diagrams[0].diagram_type == "C4Context"
        assert arch.diagrams[1].diagram_type == "C4Container"

    def test_loose_elements(self):
        arch = parse_c4_file(SAMPLE_C4)
        loose = arch.diagrams[0].loose_elements
        kinds = [e.kind for e in loose]
        assert "Person" in kinds
        assert "System" in kinds
        assert "System_Ext" in kinds
        assert "Rel" in kinds

    def test_loose_element_details(self):
        arch = parse_c4_file(SAMPLE_C4)
        person = next(e for e in arch.diagrams[0].loose_elements if e.kind == "Person")
        assert person.id == "customer"
        assert person.label == "Customer"

    def test_sections_count(self):
        arch = parse_c4_file(SAMPLE_C4)
        assert len(arch.diagrams[0].sections) == 2

    def test_auto_section(self):
        arch = parse_c4_file(SAMPLE_C4)
        auto_sec = arch.diagrams[0].sections[0]
        assert auto_sec.marker_type == "AUTO"
        assert auto_sec.marker_id == "context"
        assert auto_sec.timestamp == "2026-05-01"
        assert len(auto_sec.elements) == 2

    def test_feature_section(self):
        arch = parse_c4_file(SAMPLE_C4)
        feat_sec = arch.diagrams[0].sections[1]
        assert feat_sec.marker_type == "FEATURE"
        assert feat_sec.marker_id == "oauth:t1"
        assert len(feat_sec.elements) == 1

    def test_second_diagram_section(self):
        arch = parse_c4_file(SAMPLE_C4)
        sec = arch.diagrams[1].sections[0]
        assert sec.marker_id == "containers"
        assert len(sec.elements) == 2

    def test_empty_content(self):
        arch = parse_c4_file("")
        assert arch.diagrams == []

    def test_no_diagrams(self):
        arch = parse_c4_file("# just a comment\n")
        assert arch.diagrams == []

    def test_diagram_with_only_loose_elements(self):
        content = "---\ntitle: Simple\n---\nC4Context\nPerson(u, \"User\")\n"
        arch = parse_c4_file(content)
        assert len(arch.diagrams) == 1
        assert len(arch.diagrams[0].loose_elements) == 1
        assert arch.diagrams[0].sections == []

    def test_diagram_with_only_sections_no_loose(self):
        content = (
            "---\ntitle: Only Sections\n---\nC4Container\n"
            "%% [AUTO:x 2026-01-01]\n"
            '  Container(a, "A")\n'
            "%% [/AUTO:x]\n"
        )
        arch = parse_c4_file(content)
        assert arch.diagrams[0].loose_elements == []
        assert len(arch.diagrams[0].sections) == 1

    def test_comment_lines_skipped(self):
        content = (
            "---\ntitle: T\n---\nC4Context\n"
            "%% This is just a comment\n"
            'Person(u, "User")\n'
        )
        arch = parse_c4_file(content)
        assert len(arch.diagrams[0].loose_elements) == 1

    def test_closing_brace_skipped(self):
        content = (
            "---\ntitle: T\n---\nC4Container\n"
            'Container_Boundary(g, "Group")\n'
            '  Container(a, "A")\n'
            "}\n"
        )
        arch = parse_c4_file(content)
        assert len(arch.diagrams[0].loose_elements) == 2  # boundary + container


# ─────────────────────────────────────────────────────────────────────────────
# write_c4_file tests
# ─────────────────────────────────────────────────────────────────────────────


class TestWriteC4File:
    def test_single_diagram_structure(self):
        arch = C4Architecture(
            diagrams=[
                C4Diagram(
                    title="Test",
                    diagram_type="C4Context",
                    loose_elements=[
                        C4Element(kind="Person", id="u", label="User")
                    ],
                )
            ]
        )
        out = write_c4_file(arch)
        assert "title: Test" in out
        assert "C4Context" in out
        assert 'Person(u, "User")' in out

    def test_section_markers_written(self):
        arch = C4Architecture(
            diagrams=[
                C4Diagram(
                    title="T",
                    diagram_type="C4Container",
                    sections=[
                        C4Section(
                            marker_type="AUTO",
                            marker_id="containers",
                            timestamp="2026-05-01",
                            elements=[C4Element(kind="Container", id="a", label="A")],
                        )
                    ],
                )
            ]
        )
        out = write_c4_file(arch)
        assert "%% [AUTO:containers 2026-05-01]" in out
        assert "%% [/AUTO:containers]" in out

    def test_empty_architecture(self):
        out = write_c4_file(C4Architecture())
        assert out.strip() == ""

    def test_separators_present(self):
        arch = C4Architecture(
            diagrams=[
                C4Diagram(title="A", diagram_type="C4Context"),
                C4Diagram(title="B", diagram_type="C4Container"),
            ]
        )
        out = write_c4_file(arch)
        # Should have multiple --- separators
        assert out.count("---") >= 4

    def test_section_no_timestamp(self):
        arch = C4Architecture(
            diagrams=[
                C4Diagram(
                    title="T",
                    diagram_type="C4Context",
                    sections=[
                        C4Section(
                            marker_type="FEATURE",
                            marker_id="feat1",
                            timestamp="",
                            elements=[],
                        )
                    ],
                )
            ]
        )
        out = write_c4_file(arch)
        assert "%% [FEATURE:feat1]" in out


# ─────────────────────────────────────────────────────────────────────────────
# Round-trip tests
# ─────────────────────────────────────────────────────────────────────────────


class TestRoundTrip:
    def _canonical(self, arch: C4Architecture) -> list[dict]:
        """Convert arch to a list of comparable dicts (ignoring raw_line)."""
        result = []
        for diag in arch.diagrams:
            for elem in diag.loose_elements:
                result.append({
                    "diag": diag.title,
                    "section": None,
                    "kind": elem.kind,
                    "id": elem.id,
                    "label": elem.label,
                    "tech": elem.technology,
                    "desc": elem.description,
                    "target": elem.target_id,
                    "props": elem.style_props,
                })
            for sec in diag.sections:
                for elem in sec.elements:
                    result.append({
                        "diag": diag.title,
                        "section": f"{sec.marker_type}:{sec.marker_id}",
                        "kind": elem.kind,
                        "id": elem.id,
                        "label": elem.label,
                        "tech": elem.technology,
                        "desc": elem.description,
                        "target": elem.target_id,
                        "props": elem.style_props,
                    })
        return result

    def test_roundtrip_full_sample(self):
        arch1 = parse_c4_file(SAMPLE_C4)
        serialized = write_c4_file(arch1)
        arch2 = parse_c4_file(serialized)
        assert self._canonical(arch1) == self._canonical(arch2)

    def test_roundtrip_titles(self):
        arch1 = parse_c4_file(SAMPLE_C4)
        arch2 = parse_c4_file(write_c4_file(arch1))
        for d1, d2 in zip(arch1.diagrams, arch2.diagrams):
            assert d1.title == d2.title

    def test_roundtrip_diagram_types(self):
        arch1 = parse_c4_file(SAMPLE_C4)
        arch2 = parse_c4_file(write_c4_file(arch1))
        for d1, d2 in zip(arch1.diagrams, arch2.diagrams):
            assert d1.diagram_type == d2.diagram_type

    def test_roundtrip_sections(self):
        arch1 = parse_c4_file(SAMPLE_C4)
        arch2 = parse_c4_file(write_c4_file(arch1))
        for d1, d2 in zip(arch1.diagrams, arch2.diagrams):
            assert len(d1.sections) == len(d2.sections)
            for s1, s2 in zip(d1.sections, d2.sections):
                assert s1.marker_type == s2.marker_type
                assert s1.marker_id == s2.marker_id
                assert len(s1.elements) == len(s2.elements)

    def test_double_roundtrip(self):
        """parse → write → parse → write should produce equal output."""
        arch1 = parse_c4_file(SAMPLE_C4)
        out1 = write_c4_file(arch1)
        arch2 = parse_c4_file(out1)
        out2 = write_c4_file(arch2)
        assert out1 == out2

    def test_roundtrip_rel_element(self):
        content = (
            "---\ntitle: T\n---\nC4Context\n"
            'Rel(customer, webapp, "Uses", "HTTPS")\n'
        )
        arch1 = parse_c4_file(content)
        arch2 = parse_c4_file(write_c4_file(arch1))
        r1 = arch1.diagrams[0].loose_elements[0]
        r2 = arch2.diagrams[0].loose_elements[0]
        assert r1.id == r2.id
        assert r1.target_id == r2.target_id
        assert r1.label == r2.label
        assert r1.technology == r2.technology

    def test_roundtrip_style_props(self):
        content = (
            "---\ntitle: T\n---\nC4Context\n"
            'UpdateElementStyle(api, $bgColor="red", $fontColor="white")\n'
        )
        arch1 = parse_c4_file(content)
        arch2 = parse_c4_file(write_c4_file(arch1))
        e1 = arch1.diagrams[0].loose_elements[0]
        e2 = arch2.diagrams[0].loose_elements[0]
        assert e1.style_props == e2.style_props


# ─────────────────────────────────────────────────────────────────────────────
# Edge case tests
# ─────────────────────────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_empty_diagram(self):
        content = "---\ntitle: Empty\n---\nC4Context\n"
        arch = parse_c4_file(content)
        assert arch.diagrams[0].sections == []
        assert arch.diagrams[0].loose_elements == []

    def test_multiple_feature_sections(self):
        content = (
            "---\ntitle: T\n---\nC4Context\n"
            "%% [FEATURE:feat1 2026-01-01]\n"
            '  Component(a, "A")\n'
            "%% [/FEATURE:feat1]\n"
            "%% [FEATURE:feat2 2026-01-02]\n"
            '  Component(b, "B")\n'
            "%% [/FEATURE:feat2]\n"
        )
        arch = parse_c4_file(content)
        assert len(arch.diagrams[0].sections) == 2
        assert arch.diagrams[0].sections[0].marker_id == "feat1"
        assert arch.diagrams[0].sections[1].marker_id == "feat2"

    def test_unrecognized_lines_ignored(self):
        content = (
            "---\ntitle: T\n---\nC4Context\n"
            "this is garbage\n"
            'Person(u, "User")\n'
        )
        arch = parse_c4_file(content)
        assert len(arch.diagrams[0].loose_elements) == 1

    def test_element_missing_optional_fields(self):
        e = _parse_element('Container(x, "X")', "")
        assert e is not None
        assert e.technology == ""
        assert e.description == ""

    def test_write_containerdb(self):
        e = C4Element(
            kind="ContainerDb",
            id="db",
            label="Database",
            technology="PostgreSQL",
            description="Stores data",
        )
        line = _write_element(e)
        assert line == 'ContainerDb(db, "Database", "PostgreSQL", "Stores data")'

    def test_nested_container_boundary_flat_parsed(self):
        """Container_Boundary + nested containers → flat list of loose_elements."""
        content = (
            "---\ntitle: T\n---\nC4Container\n"
            'Container_Boundary(g, "Group")\n'
            '  Container(a, "A", "Python", "desc")\n'
            '  Container(b, "B", "Go", "desc2")\n'
            "}\n"
        )
        arch = parse_c4_file(content)
        kinds = [e.kind for e in arch.diagrams[0].loose_elements]
        assert "Container_Boundary" in kinds
        assert kinds.count("Container") == 2

    def test_parse_then_write_preserves_section_elements(self):
        content = (
            "---\ntitle: T\n---\nC4Context\n"
            "%% [AUTO:x 2026-05-01]\n"
            '  Container(api, "API", "Python", "REST")\n'
            "%% [/AUTO:x]\n"
        )
        arch = parse_c4_file(content)
        sec = arch.diagrams[0].sections[0]
        assert sec.elements[0].id == "api"
        assert sec.elements[0].technology == "Python"


# ─────────────────────────────────────────────────────────────────────────────
# Nested boundary tests
# ─────────────────────────────────────────────────────────────────────────────


class TestNestedBoundaries:
    def test_parse_container_boundary_with_children(self):
        content = (
            "---\ntitle: T\n---\nC4Container\n"
            'Container_Boundary(sys, "System") {\n'
            '    Container(api, "API", "Python", "REST")\n'
            '    ContainerDb(db, "DB", "SQLite", "stores")\n'
            "}\n"
        )
        arch = parse_c4_file(content)
        loose = arch.diagrams[0].loose_elements
        assert len(loose) == 1
        assert loose[0].kind == "Container_Boundary"
        assert loose[0].id == "sys"
        assert len(loose[0].children) == 2
        assert loose[0].children[0].kind == "Container"
        assert loose[0].children[0].id == "api"
        assert loose[0].children[1].kind == "ContainerDb"

    def test_parse_component_boundary_with_children(self):
        content = (
            "---\ntitle: T\n---\nC4Component\n"
            'Component_Boundary(mod, "Module") {\n'
            '    Component(svc, "Service", "Python", "handles logic")\n'
            "}\n"
        )
        arch = parse_c4_file(content)
        loose = arch.diagrams[0].loose_elements
        assert len(loose) == 1
        assert loose[0].kind == "Component_Boundary"
        assert loose[0].id == "mod"
        assert len(loose[0].children) == 1
        assert loose[0].children[0].kind == "Component"
        assert loose[0].children[0].id == "svc"

    def test_roundtrip_container_boundary_with_children(self):
        content = (
            "---\ntitle: T\n---\nC4Container\n"
            'Container_Boundary(sys, "System") {\n'
            '  Container(api, "API", "Python", "REST")\n'
            "}\n"
        )
        arch1 = parse_c4_file(content)
        arch2 = parse_c4_file(write_c4_file(arch1))
        loose1 = arch1.diagrams[0].loose_elements
        loose2 = arch2.diagrams[0].loose_elements
        assert len(loose1) == len(loose2) == 1
        assert loose1[0].kind == loose2[0].kind == "Container_Boundary"
        assert len(loose1[0].children) == len(loose2[0].children) == 1
        assert loose1[0].children[0].id == loose2[0].children[0].id

    def test_write_empty_boundary_round_trips(self):
        content = (
            "---\ntitle: T\n---\nC4Container\n"
            'Container_Boundary(b, "Boundary") {\n'
            "}\n"
        )
        arch = parse_c4_file(content)
        loose = arch.diagrams[0].loose_elements
        assert len(loose) == 1
        assert loose[0].kind == "Container_Boundary"
        assert loose[0].children == []
        out = write_c4_file(arch)
        assert "Container_Boundary" in out
        assert "{" in out
        arch2 = parse_c4_file(out)
        assert len(arch2.diagrams[0].loose_elements) == 1
        assert arch2.diagrams[0].loose_elements[0].kind == "Container_Boundary"
        assert arch2.diagrams[0].loose_elements[0].children == []

    def test_deeply_nested_boundaries(self):
        content = (
            "---\ntitle: T\n---\nC4Container\n"
            'Container_Boundary(outer, "Outer") {\n'
            '  Component_Boundary(inner, "Inner") {\n'
            '    Component(c, "C", "Python", "desc")\n'
            "  }\n"
            "}\n"
        )
        arch = parse_c4_file(content)
        loose = arch.diagrams[0].loose_elements
        assert len(loose) == 1
        outer = loose[0]
        assert outer.kind == "Container_Boundary"
        assert len(outer.children) == 1
        inner = outer.children[0]
        assert inner.kind == "Component_Boundary"
        assert len(inner.children) == 1
        assert inner.children[0].kind == "Component"
        assert inner.children[0].id == "c"
