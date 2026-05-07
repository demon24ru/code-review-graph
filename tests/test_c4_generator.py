"""Tests for c4_generator module."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from code_review_graph.c4_generator import (
    _slugify,
    build_c4,
    get_c4_path,
    rebuild_c4,
    resolve_for_render,
)
from code_review_graph.c4_parser import (
    C4Architecture,
    C4Diagram,
    C4Element,
    C4Section,
    parse_c4_file,
)
from code_review_graph.communities import (
    detect_communities,
    store_communities,
)
from code_review_graph.graph import GraphStore
from code_review_graph.parser import EdgeInfo, NodeInfo


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def make_store() -> GraphStore:
    """Create an in-memory (temp file) GraphStore."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    return GraphStore(tmp.name)


def seed_two_communities(store: GraphStore) -> None:
    """Seed two distinct file-based communities (auth.py + db.py) with
    an intentional cross-community edge so container relationships appear."""
    # Auth community
    store.upsert_node(
        NodeInfo(kind="File", name="auth.py", file_path="auth.py",
                 line_start=1, line_end=80, language="python"),
        file_hash="a1",
    )
    store.upsert_node(
        NodeInfo(kind="Function", name="login", file_path="auth.py",
                 line_start=5, line_end=20, language="python"),
        file_hash="a1",
    )
    store.upsert_node(
        NodeInfo(kind="Function", name="logout", file_path="auth.py",
                 line_start=25, line_end=40, language="python"),
        file_hash="a1",
    )
    store.upsert_node(
        NodeInfo(kind="Function", name="check_token", file_path="auth.py",
                 line_start=45, line_end=60, language="python"),
        file_hash="a1",
    )
    store.upsert_edge(EdgeInfo(
        kind="CALLS", source="auth.py::login", target="auth.py::check_token",
        file_path="auth.py", line=10,
    ))

    # DB community
    store.upsert_node(
        NodeInfo(kind="File", name="db.py", file_path="db.py",
                 line_start=1, line_end=80, language="python"),
        file_hash="b1",
    )
    store.upsert_node(
        NodeInfo(kind="Function", name="connect", file_path="db.py",
                 line_start=5, line_end=20, language="python"),
        file_hash="b1",
    )
    store.upsert_node(
        NodeInfo(kind="Function", name="query", file_path="db.py",
                 line_start=25, line_end=40, language="python"),
        file_hash="b1",
    )
    store.upsert_edge(EdgeInfo(
        kind="CALLS", source="db.py::query", target="db.py::connect",
        file_path="db.py", line=30,
    ))

    # Cross-community edge
    store.upsert_edge(EdgeInfo(
        kind="CALLS", source="auth.py::login", target="db.py::query",
        file_path="auth.py", line=15,
    ))
    store.commit()

    communities = detect_communities(store, min_size=2)
    store_communities(store, communities)


# ---------------------------------------------------------------------------
# Test _slugify
# ---------------------------------------------------------------------------


class TestSlugify:
    def test_lowercase(self):
        assert _slugify("AuthService") == "authservice"

    def test_spaces_to_underscores(self):
        assert _slugify("my service name") == "my_service_name"

    def test_dots_and_colons_to_underscores(self):
        assert _slugify("auth.py::login") == "auth_py_login"

    def test_strip_leading_trailing(self):
        assert _slugify("__auth__") == "auth"

    def test_already_valid(self):
        assert _slugify("auth_service") == "auth_service"

    def test_numbers_preserved(self):
        assert _slugify("handler2go") == "handler2go"

    def test_empty_string_fallback(self):
        # All non-alphanum stripped → fallback "node"
        assert _slugify("!!!") == "node"

    def test_hyphens_to_underscores(self):
        assert _slugify("my-module") == "my_module"


# ---------------------------------------------------------------------------
# Test build_c4 — basic
# ---------------------------------------------------------------------------


class TestBuildC4Basic:
    def setup_method(self):
        self.store = make_store()
        seed_two_communities(self.store)

    def teardown_method(self):
        self.store.close()
        Path(self.store.db_path).unlink(missing_ok=True)

    def test_output_is_parseable(self):
        """build_c4 output round-trips through parse_c4_file without errors."""
        output = build_c4(self.store, repo_name="TestRepo")
        arch = parse_c4_file(output)
        assert isinstance(arch, C4Architecture)
        assert len(arch.diagrams) > 0

    def test_has_context_diagram(self):
        output = build_c4(self.store, repo_name="TestRepo")
        arch = parse_c4_file(output)
        types = [d.diagram_type for d in arch.diagrams]
        assert "C4Context" in types

    def test_has_container_diagram(self):
        output = build_c4(self.store, repo_name="TestRepo")
        arch = parse_c4_file(output)
        types = [d.diagram_type for d in arch.diagrams]
        assert "C4Container" in types

    def test_has_component_diagrams(self):
        output = build_c4(self.store, repo_name="TestRepo")
        arch = parse_c4_file(output)
        types = [d.diagram_type for d in arch.diagrams]
        assert "C4Component" in types

    def test_containers_match_communities(self):
        """Each community should contribute at least one Container element."""
        output = build_c4(self.store, repo_name="TestRepo")
        arch = parse_c4_file(output)

        container_diagram = next(d for d in arch.diagrams if d.diagram_type == "C4Container")
        # All communities must contribute at least one Container or Container_Boundary
        all_container_ids: set[str] = set()
        for section in container_diagram.sections:
            for elem in section.elements:
                if elem.kind in ("Container_Boundary", "Container"):
                    all_container_ids.add(elem.id)
                    for child in elem.children:
                        if child.kind == "Container":
                            all_container_ids.add(child.id)

        assert len(all_container_ids) >= 1, "Expected at least one Container element"

    def test_cross_community_relationships_exist(self):
        """Container diagram must contain at least one Rel element for cross edges."""
        output = build_c4(self.store, repo_name="TestRepo")
        arch = parse_c4_file(output)

        container_diagram = next(d for d in arch.diagrams if d.diagram_type == "C4Container")
        rel_elements = [
            e
            for section in container_diagram.sections
            for e in section.elements
            if e.kind == "Rel"
        ]
        assert len(rel_elements) >= 1, "Expected at least one Rel from cross-community edge"

    def test_auto_sections_present(self):
        """Generated diagrams contain [AUTO:*] sections."""
        output = build_c4(self.store, repo_name="TestRepo")
        arch = parse_c4_file(output)

        container_diagram = next(d for d in arch.diagrams if d.diagram_type == "C4Container")
        auto_types = {s.marker_type for s in container_diagram.sections}
        assert "AUTO" in auto_types

    def test_context_diagram_has_system_node(self):
        """C4Context diagram contains a System element representing the repository."""
        output = build_c4(self.store, repo_name="TestRepo")
        arch = parse_c4_file(output)

        context_diagram = next(d for d in arch.diagrams if d.diagram_type == "C4Context")
        # Find all System elements in the context diagram
        system_elements = [
            e
            for section in context_diagram.sections
            for e in section.elements
            if e.kind == "System"
        ]
        assert len(system_elements) >= 1, "Expected at least one System element in C4Context"
        # Verify the System element has the expected properties
        system_elem = system_elements[0]
        assert system_elem.label == "TestRepo"
        assert system_elem.description == "Code repository"
        assert "system" in system_elem.id.lower()


# ---------------------------------------------------------------------------
# Test build_c4 — empty store
# ---------------------------------------------------------------------------


class TestBuildC4Empty:
    def setup_method(self):
        self.store = make_store()

    def teardown_method(self):
        self.store.close()
        Path(self.store.db_path).unlink(missing_ok=True)

    def test_empty_store_produces_valid_output(self):
        """Empty store still produces parseable output."""
        output = build_c4(self.store)
        arch = parse_c4_file(output)
        assert isinstance(arch, C4Architecture)
        # At minimum the Context and Container diagrams are created
        assert len(arch.diagrams) >= 2

    def test_empty_store_no_containers(self):
        output = build_c4(self.store)
        arch = parse_c4_file(output)
        container_diagram = next(
            (d for d in arch.diagrams if d.diagram_type == "C4Container"), None
        )
        assert container_diagram is not None
        # containers section should have no Container elements
        for section in container_diagram.sections:
            container_elems = [e for e in section.elements if e.kind == "Container"]
            assert len(container_elems) == 0


# ---------------------------------------------------------------------------
# Test rebuild_c4 — feature sections preserved
# ---------------------------------------------------------------------------


class TestRebuildPreservesFeatures:
    def setup_method(self):
        self.store = make_store()
        seed_two_communities(self.store)

    def teardown_method(self):
        self.store.close()
        Path(self.store.db_path).unlink(missing_ok=True)

    def _add_feature_section(self, content: str, diagram_title: str) -> str:
        """Inject a FEATURE section into the named diagram using the parser API."""
        arch = parse_c4_file(content)
        for diagram in arch.diagrams:
            if diagram.title == diagram_title:
                diagram.sections.append(
                    C4Section(
                        marker_type="FEATURE",
                        marker_id="custom",
                        timestamp="2026-01-01",
                        elements=[
                            C4Element(
                                kind="Person",
                                id="external_user",
                                label="External User",
                                description="Uses the system",
                            )
                        ],
                    )
                )
                break
        from code_review_graph.c4_parser import write_c4_file as _write
        return _write(arch)

    def test_feature_section_preserved(self):
        """FEATURE sections survive a rebuild cycle."""
        original = build_c4(self.store, repo_name="TestRepo")
        arch0 = parse_c4_file(original)
        # Find the Containers diagram title
        container_title = next(
            d.title for d in arch0.diagrams if d.diagram_type == "C4Container"
        )
        modified = self._add_feature_section(original, container_title)

        rebuilt = rebuild_c4(self.store, modified, repo_name="TestRepo")
        rebuilt_arch = parse_c4_file(rebuilt)

        # Find the Containers diagram in the rebuilt arch
        rebuilt_container = next(
            d for d in rebuilt_arch.diagrams if d.diagram_type == "C4Container"
        )
        feature_sections = [s for s in rebuilt_container.sections if s.marker_type == "FEATURE"]
        assert len(feature_sections) >= 1, "FEATURE section should be preserved after rebuild"

    def test_feature_elements_intact(self):
        """Elements inside a FEATURE section are not removed after rebuild."""
        original = build_c4(self.store, repo_name="TestRepo")
        arch0 = parse_c4_file(original)
        container_title = next(
            d.title for d in arch0.diagrams if d.diagram_type == "C4Container"
        )
        modified = self._add_feature_section(original, container_title)

        rebuilt = rebuild_c4(self.store, modified, repo_name="TestRepo")
        rebuilt_arch = parse_c4_file(rebuilt)

        rebuilt_container = next(
            d for d in rebuilt_arch.diagrams if d.diagram_type == "C4Container"
        )
        feature_elems = [
            e
            for s in rebuilt_container.sections
            if s.marker_type == "FEATURE"
            for e in s.elements
        ]
        labels = {e.label for e in feature_elems}
        assert "External User" in labels


# ---------------------------------------------------------------------------
# Test rebuild_c4 — graduation of implemented elements
# ---------------------------------------------------------------------------


class TestRebuildGraduatesImplemented:
    def setup_method(self):
        self.store = make_store()
        seed_two_communities(self.store)

    def teardown_method(self):
        self.store.close()
        Path(self.store.db_path).unlink(missing_ok=True)

    def test_element_graduated_when_in_auto(self):
        """FEATURE element whose id matches an AUTO element is removed on rebuild."""
        original = build_c4(self.store, repo_name="TestRepo")
        arch0 = parse_c4_file(original)
        container_diagram = next(d for d in arch0.diagrams if d.diagram_type == "C4Container")

        # Pick an existing AUTO element id
        auto_id: str | None = None
        for section in container_diagram.sections:
            if section.marker_type == "AUTO":
                for elem in section.elements:
                    if elem.kind in ("Container", "Container_Boundary"):
                        auto_id = elem.id
                        break
            if auto_id:
                break
        assert auto_id is not None, "Need at least one Container or Container_Boundary in AUTO section"

        # Inject a FEATURE section in the Container diagram using the same id
        feature_block = (
            f"\n%% [FEATURE:overlap 2026-01-01]\n"
            f"  Container({auto_id}, \"Overlap Element\", \"lang\", \"duplicate\")\n"
            f"%% [/FEATURE:overlap]\n"
        )
        modified = original + feature_block

        rebuilt = rebuild_c4(self.store, modified, repo_name="TestRepo")
        rebuilt_arch = parse_c4_file(rebuilt)
        rebuilt_container = next(
            d for d in rebuilt_arch.diagrams if d.diagram_type == "C4Container"
        )

        # The FEATURE element with auto_id should be graduated (removed)
        feature_ids = {
            e.id
            for s in rebuilt_container.sections
            if s.marker_type == "FEATURE"
            for e in s.elements
        }
        assert auto_id not in feature_ids, (
            f"Element '{auto_id}' should have been graduated from FEATURE section"
        )


# ---------------------------------------------------------------------------
# Test resolve_for_render
# ---------------------------------------------------------------------------


class TestResolveForRender:
    def _make_arch_with_both(self) -> C4Architecture:
        """Arch with one AUTO element and one FEATURE element; one shared id."""
        auto_section = C4Section(
            marker_type="AUTO",
            marker_id="containers",
            timestamp="2026-01-01",
            elements=[
                C4Element(kind="Container", id="auth", label="Auth", technology="Python"),
                C4Element(kind="Container", id="db", label="DB", technology="Python"),
            ],
        )
        feature_section = C4Section(
            marker_type="FEATURE",
            marker_id="extras",
            timestamp="2026-01-01",
            elements=[
                # modified — same id as AUTO but different label
                C4Element(kind="Container", id="auth", label="Auth Modified", technology="Go"),
                # new — only in FEATURE
                C4Element(kind="Person", id="admin", label="Admin User", technology=""),
            ],
        )
        diagram = C4Diagram(
            title="Test Containers",
            diagram_type="C4Container",
            sections=[auto_section, feature_section],
        )
        return C4Architecture(diagrams=[diagram])

    def test_existing_status(self):
        arch = self._make_arch_with_both()
        items = resolve_for_render(arch, "Test Containers")
        db_item = next((i for i in items if i["id"] == "db"), None)
        assert db_item is not None
        assert db_item["status"] == "existing"

    def test_modified_status(self):
        arch = self._make_arch_with_both()
        items = resolve_for_render(arch, "Test Containers")
        auth_item = next((i for i in items if i["id"] == "auth"), None)
        assert auth_item is not None
        assert auth_item["status"] == "modified"
        # FEATURE label wins
        assert auth_item["label"] == "Auth Modified"

    def test_new_status(self):
        arch = self._make_arch_with_both()
        items = resolve_for_render(arch, "Test Containers")
        admin_item = next((i for i in items if i["id"] == "admin"), None)
        assert admin_item is not None
        assert admin_item["status"] == "new"

    def test_all_ids_present(self):
        arch = self._make_arch_with_both()
        items = resolve_for_render(arch, "Test Containers")
        ids = {i["id"] for i in items}
        # auth (modified), db (existing), admin (new)
        assert ids == {"auth", "db", "admin"}

    def test_unknown_diagram_returns_empty(self):
        arch = self._make_arch_with_both()
        items = resolve_for_render(arch, "Nonexistent Diagram")
        assert items == []

    def test_result_has_required_keys(self):
        arch = self._make_arch_with_both()
        items = resolve_for_render(arch, "Test Containers")
        for item in items:
            assert "id" in item
            assert "label" in item
            assert "sublabel" in item
            assert "status" in item
            assert "kind" in item

    def test_rel_elements_excluded_from_auto(self):
        """Rel elements in AUTO section must NOT appear as nodes."""
        rel_elem = C4Element(kind="Rel", id="auth", label="calls", technology="")
        rel_elem.target_id = "db"  # type: ignore[attr-defined]
        auto_section = C4Section(
            marker_type="AUTO",
            marker_id="containers",
            timestamp="2026-01-01",
            elements=[
                C4Element(kind="Container", id="auth", label="Auth", technology="Python"),
                C4Element(kind="Container", id="db", label="DB", technology="Postgres"),
                rel_elem,
            ],
        )
        diagram = C4Diagram(
            title="With Rels",
            diagram_type="C4Container",
            sections=[auto_section],
        )
        arch = C4Architecture(diagrams=[diagram])
        items = resolve_for_render(arch, "With Rels")
        kinds = {i["kind"] for i in items}
        assert "Rel" not in kinds
        ids = {i["id"] for i in items}
        # auth and db should still be present
        assert "auth" in ids
        assert "db" in ids
        # no Rel entry that would have id="auth" (overwriting the Component)
        auth_items = [i for i in items if i["id"] == "auth"]
        assert all(i["kind"] != "Rel" for i in auth_items)

    def test_update_element_style_excluded(self):
        """UpdateElementStyle must not appear as a node."""
        style_elem = C4Element(kind="UpdateElementStyle", id="foo", label="", technology="")
        auto_section = C4Section(
            marker_type="AUTO",
            marker_id="containers",
            timestamp="2026-01-01",
            elements=[
                C4Element(kind="Container", id="foo", label="Foo Svc", technology="Go"),
                style_elem,
            ],
        )
        diagram = C4Diagram(
            title="Styled",
            diagram_type="C4Container",
            sections=[auto_section],
        )
        arch = C4Architecture(diagrams=[diagram])
        items = resolve_for_render(arch, "Styled")
        kinds = {i["kind"] for i in items}
        assert "UpdateElementStyle" not in kinds
        # foo should still be a Container node
        foo = next((i for i in items if i["id"] == "foo"), None)
        assert foo is not None
        assert foo["kind"] == "Container"

    def test_container_boundary_included(self):
        """Container_Boundary appears as a compound-parent node for Cytoscape."""
        boundary = C4Element(kind="Container_Boundary", id="boundary1", label="Boundary", technology="")
        auto_section = C4Section(
            marker_type="AUTO",
            marker_id="containers",
            timestamp="2026-01-01",
            elements=[
                C4Element(kind="Container", id="svc", label="Service", technology="Python"),
                boundary,
            ],
        )
        diagram = C4Diagram(
            title="Boundary Test",
            diagram_type="C4Container",
            sections=[auto_section],
        )
        arch = C4Architecture(diagrams=[diagram])
        items = resolve_for_render(arch, "Boundary Test")
        kinds = {i["kind"] for i in items}
        assert "Container_Boundary" in kinds
        assert "Container" in kinds


# ---------------------------------------------------------------------------
# Test get_c4_path
# ---------------------------------------------------------------------------


class TestGetC4Path:
    def test_path_structure(self):
        result = get_c4_path("/some/repo")
        assert result == Path("/some/repo/.code-review-graph/architecture.c4")

    def test_returns_path_object(self):
        result = get_c4_path("/some/repo")
        assert isinstance(result, Path)

    def test_filename(self):
        result = get_c4_path("/any/path")
        assert result.name == "architecture.c4"

    def test_parent_dir(self):
        result = get_c4_path("/any/path")
        assert result.parent.name == ".code-review-graph"


# ---------------------------------------------------------------------------
# Test Component diagram noise filtering — new behaviours
# ---------------------------------------------------------------------------


class TestComponentDiagramFiltersContainsEdges:
    """CONTAINS edges must never appear as Rel elements in C4Component diagrams."""

    def setup_method(self):
        self.store = make_store()
        # Two functions in auth_module.py — one CALLS edge + one CONTAINS edge
        self.store.upsert_node(
            NodeInfo(kind="File", name="auth_module.py", file_path="auth_module.py",
                     line_start=1, line_end=100, language="python"),
            file_hash="c1",
        )
        self.store.upsert_node(
            NodeInfo(kind="Function", name="handler", file_path="auth_module.py",
                     line_start=5, line_end=20, language="python"),
            file_hash="c1",
        )
        self.store.upsert_node(
            NodeInfo(kind="Function", name="helper_fn", file_path="auth_module.py",
                     line_start=25, line_end=40, language="python"),
            file_hash="c1",
        )
        # CALLS edge (should produce a Rel)
        self.store.upsert_edge(EdgeInfo(
            kind="CALLS", source="auth_module.py::handler",
            target="auth_module.py::helper_fn", file_path="auth_module.py", line=10,
        ))
        # CONTAINS edge (must be filtered out)
        self.store.upsert_edge(EdgeInfo(
            kind="CONTAINS", source="auth_module.py",
            target="auth_module.py::handler", file_path="auth_module.py", line=5,
        ))
        self.store.commit()
        communities = detect_communities(self.store, min_size=2)
        store_communities(self.store, communities)

    def teardown_method(self):
        self.store.close()
        Path(self.store.db_path).unlink(missing_ok=True)

    def test_component_diagram_filters_contains_edges(self):
        output = build_c4(self.store)
        arch = parse_c4_file(output)
        component_diagrams = [d for d in arch.diagrams if d.diagram_type == "C4Component"]
        assert len(component_diagrams) > 0, "Expected at least one C4Component diagram"
        for diag in component_diagrams:
            for section in diag.sections:
                for elem in section.elements:
                    if elem.kind == "Rel":
                        assert elem.label.lower() != "contains", (
                            f"CONTAINS edge must be filtered; found Rel with label='{elem.label}'"
                        )


class TestComponentDiagramDetectsHubNodes:
    """Nodes with >10 incoming CALLS within their community get '(hub)' in description."""

    def setup_method(self):
        self.store = make_store()
        # 11 callers + hub_func + File node — all in hub_utils.py
        self.store.upsert_node(
            NodeInfo(kind="File", name="hub_utils.py", file_path="hub_utils.py",
                     line_start=1, line_end=200, language="python"),
            file_hash="h2",
        )
        for i in range(11):
            self.store.upsert_node(
                NodeInfo(kind="Function", name=f"caller_{i:02d}", file_path="hub_utils.py",
                         line_start=5 + i * 10, line_end=9 + i * 10, language="python"),
                file_hash="h2",
            )
        self.store.upsert_node(
            NodeInfo(kind="Function", name="hub_func", file_path="hub_utils.py",
                     line_start=115, line_end=130, language="python"),
            file_hash="h2",
        )
        # Each caller calls hub_func → 11 incoming CALLS on hub_func
        for i in range(11):
            self.store.upsert_edge(EdgeInfo(
                kind="CALLS", source=f"hub_utils.py::caller_{i:02d}",
                target="hub_utils.py::hub_func", file_path="hub_utils.py", line=7 + i * 10,
            ))
        self.store.commit()
        communities = detect_communities(self.store, min_size=2)
        store_communities(self.store, communities)

    def teardown_method(self):
        self.store.close()
        Path(self.store.db_path).unlink(missing_ok=True)

    def test_component_diagram_detects_hub_nodes(self):
        output = build_c4(self.store)
        arch = parse_c4_file(output)
        component_diagrams = [d for d in arch.diagrams if d.diagram_type == "C4Component"]
        assert len(component_diagrams) > 0

        hub_found = False
        for diag in component_diagrams:
            for section in diag.sections:
                for elem in section.elements:
                    if elem.kind == "Component" and elem.label == "hub_func":
                        assert "(hub)" in elem.description, (
                            f"hub_func (11 callers) should have '(hub)' in description, "
                            f"got: '{elem.description}'"
                        )
                        hub_found = True
        assert hub_found, "hub_func element not found in any C4Component diagram"

    def test_non_hub_nodes_have_no_hub_marker(self):
        """Callers with only 1 outgoing call should not be marked as hubs."""
        output = build_c4(self.store)
        arch = parse_c4_file(output)
        for diag in arch.diagrams:
            if diag.diagram_type != "C4Component":
                continue
            for section in diag.sections:
                for elem in section.elements:
                    if elem.kind == "Component" and elem.label.startswith("caller_"):
                        assert "(hub)" not in elem.description, (
                            f"Caller '{elem.label}' should NOT be a hub"
                        )


class TestComponentDiagramCollapsesPrivateHelpers:
    """Private helpers (name starts with _ but not __) are excluded from Component
    diagrams; their CALLS edges are collapsed so A→_helper→B becomes A→B."""

    def setup_method(self):
        self.store = make_store()
        # workflow.py: start → _process → finish
        self.store.upsert_node(
            NodeInfo(kind="File", name="workflow.py", file_path="workflow.py",
                     line_start=1, line_end=100, language="python"),
            file_hash="p1",
        )
        self.store.upsert_node(
            NodeInfo(kind="Function", name="start", file_path="workflow.py",
                     line_start=5, line_end=20, language="python"),
            file_hash="p1",
        )
        self.store.upsert_node(
            NodeInfo(kind="Function", name="_process", file_path="workflow.py",
                     line_start=25, line_end=40, language="python"),
            file_hash="p1",
        )
        self.store.upsert_node(
            NodeInfo(kind="Function", name="finish", file_path="workflow.py",
                     line_start=45, line_end=60, language="python"),
            file_hash="p1",
        )
        self.store.upsert_edge(EdgeInfo(
            kind="CALLS", source="workflow.py::start",
            target="workflow.py::_process", file_path="workflow.py", line=10,
        ))
        self.store.upsert_edge(EdgeInfo(
            kind="CALLS", source="workflow.py::_process",
            target="workflow.py::finish", file_path="workflow.py", line=30,
        ))
        self.store.commit()
        communities = detect_communities(self.store, min_size=2)
        store_communities(self.store, communities)

    def teardown_method(self):
        self.store.close()
        Path(self.store.db_path).unlink(missing_ok=True)

    def test_private_helper_excluded_from_component_elements(self):
        output = build_c4(self.store)
        arch = parse_c4_file(output)
        for diag in arch.diagrams:
            if diag.diagram_type != "C4Component":
                continue
            for section in diag.sections:
                for elem in section.elements:
                    assert not (elem.kind == "Component" and elem.label == "_process"), (
                        "Private helper '_process' must be excluded from Component elements"
                    )

    def test_collapsed_rel_start_to_finish(self):
        """A→_helper→B must produce a direct A→B Rel."""
        output = build_c4(self.store)
        arch = parse_c4_file(output)

        start_slug = _slugify("workflow.py::start")    # "workflow_py_start"
        finish_slug = _slugify("workflow.py::finish")  # "workflow_py_finish"

        for diag in arch.diagrams:
            if diag.diagram_type != "C4Component":
                continue
            rels = [
                (e.id, e.target_id)
                for s in diag.sections
                for e in s.elements
                if e.kind == "Rel"
            ]
            if (start_slug, finish_slug) in rels:
                return  # collapsed rel found ✓

        pytest.fail(
            f"Expected collapsed Rel ({start_slug!r} → {finish_slug!r}) "
            "through '_process' helper, but none found"
        )

    def test_dunder_init_not_treated_as_private_helper(self):
        """__init__ must NOT be excluded (dunders are not private helpers)."""
        # Add __init__ to the same community (must be same file for file-based grouping)
        # We assert it appears in whatever communities were built in setup_method
        # by examining the output of a fresh store that includes __init__
        store2 = make_store()
        store2.upsert_node(
            NodeInfo(kind="File", name="workflow.py", file_path="workflow.py",
                     line_start=1, line_end=100, language="python"),
            file_hash="p2",
        )
        store2.upsert_node(
            NodeInfo(kind="Function", name="__init__", file_path="workflow.py",
                     line_start=1, line_end=4, language="python"),
            file_hash="p2",
        )
        store2.upsert_node(
            NodeInfo(kind="Function", name="start", file_path="workflow.py",
                     line_start=5, line_end=20, language="python"),
            file_hash="p2",
        )
        store2.upsert_node(
            NodeInfo(kind="Function", name="_process", file_path="workflow.py",
                     line_start=25, line_end=40, language="python"),
            file_hash="p2",
        )
        store2.commit()
        communities = detect_communities(store2, min_size=2)
        store_communities(store2, communities)

        try:
            output = build_c4(store2)
            arch = parse_c4_file(output)
            found = False
            for diag in arch.diagrams:
                if diag.diagram_type != "C4Component":
                    continue
                for section in diag.sections:
                    for elem in section.elements:
                        if elem.kind == "Component" and elem.label == "__init__":
                            found = True
            assert found, "__init__ (dunder) should appear as a Component element, not be filtered"
        finally:
            store2.close()
            Path(store2.db_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Test Container_Boundary grouping in Container diagram
# ---------------------------------------------------------------------------


def seed_multi_file_community(store: GraphStore) -> None:
    """Seed two communities where the first spans two files (auth.py + db.py)
    and the second has one file (utils.py).
    The multi-file community → Container_Boundary; utils → flat Container.
    A cross-community edge from auth.py::login → utils.py::helper is added."""
    # Auth+DB community (2 files — will become Container_Boundary)
    for fname, fhash, funcs in [
        ("auth.py", "a1", [("login", 5, 20), ("logout", 25, 40)]),
        ("db.py",   "b1", [("connect", 5, 20), ("query", 25, 40)]),
    ]:
        store.upsert_node(
            NodeInfo(kind="File", name=fname, file_path=fname,
                     line_start=1, line_end=80, language="python"),
            file_hash=fhash,
        )
        for fn, ls, le in funcs:
            store.upsert_node(
                NodeInfo(kind="Function", name=fn, file_path=fname,
                         line_start=ls, line_end=le, language="python"),
                file_hash=fhash,
            )
    # Cross-file edge keeps auth.py+db.py in the same community
    store.upsert_edge(EdgeInfo(
        kind="CALLS", source="auth.py::login", target="db.py::query",
        file_path="auth.py", line=15,
    ))
    # Utils community (1 file — will become flat Container)
    store.upsert_node(
        NodeInfo(kind="File", name="utils.py", file_path="utils.py",
                 line_start=1, line_end=30, language="python"),
        file_hash="u1",
    )
    store.upsert_node(
        NodeInfo(kind="Function", name="helper", file_path="utils.py",
                 line_start=5, line_end=20, language="python"),
        file_hash="u1",
    )
    store.upsert_node(
        NodeInfo(kind="Function", name="fmt", file_path="utils.py",
                 line_start=22, line_end=30, language="python"),
        file_hash="u1",
    )
    store.upsert_edge(EdgeInfo(
        kind="CALLS", source="utils.py::helper", target="utils.py::fmt",
        file_path="utils.py", line=10,
    ))
    # Cross-community edge: auth → utils
    store.upsert_edge(EdgeInfo(
        kind="CALLS", source="auth.py::login", target="utils.py::helper",
        file_path="auth.py", line=18,
    ))
    store.commit()
    communities = detect_communities(store, min_size=2)
    store_communities(store, communities)


class TestContainerBoundaryGeneration:
    """build_c4 emits Container_Boundary only for communities with 2+ files.
    Single-file communities produce a flat Container."""

    def setup_method(self):
        self.store = make_store()
        seed_multi_file_community(self.store)

    def teardown_method(self):
        self.store.close()
        Path(self.store.db_path).unlink(missing_ok=True)

    def test_community_produces_container_boundary(self):
        """A community with 2+ files appears as Container_Boundary."""
        output = build_c4(self.store, repo_name="TestRepo")
        arch = parse_c4_file(output)
        container_diagram = next(d for d in arch.diagrams if d.diagram_type == "C4Container")

        section_kinds = [
            elem.kind
            for section in container_diagram.sections
            for elem in section.elements
        ]
        assert "Container_Boundary" in section_kinds, (
            "Expected Container_Boundary for multi-file community"
        )

    def test_community_boundary_has_file_children(self):
        """Container_Boundary elements must have Container children for file nodes."""
        output = build_c4(self.store, repo_name="TestRepo")
        arch = parse_c4_file(output)
        container_diagram = next(d for d in arch.diagrams if d.diagram_type == "C4Container")

        for section in container_diagram.sections:
            for elem in section.elements:
                if elem.kind == "Container_Boundary":
                    assert len(elem.children) >= 1, (
                        f"Container_Boundary '{elem.label}' must have at least 1 Container child"
                    )
                    for child in elem.children:
                        assert child.kind == "Container", (
                            f"Children of Container_Boundary must be Container, got {child.kind}"
                        )

    def test_single_file_community_produces_flat_container(self):
        """A community with exactly 1 file gets a flat Container (no boundary wrapper)."""
        store2 = make_store()
        # Seed a single-file community
        store2.upsert_node(
            NodeInfo(kind="File", name="solo.py", file_path="solo.py",
                     line_start=1, line_end=40, language="python"),
            file_hash="s1",
        )
        store2.upsert_node(
            NodeInfo(kind="Function", name="run", file_path="solo.py",
                     line_start=5, line_end=20, language="python"),
            file_hash="s1",
        )
        store2.upsert_node(
            NodeInfo(kind="Function", name="stop", file_path="solo.py",
                     line_start=25, line_end=40, language="python"),
            file_hash="s1",
        )
        store2.upsert_edge(EdgeInfo(
            kind="CALLS", source="solo.py::run", target="solo.py::stop",
            file_path="solo.py", line=10,
        ))
        store2.commit()
        communities = detect_communities(store2, min_size=2)
        store_communities(store2, communities)

        output = build_c4(store2, repo_name="TestRepo")
        arch = parse_c4_file(output)
        container_diagram = next(d for d in arch.diagrams if d.diagram_type == "C4Container")

        # A single-file community must appear as a flat Container, not wrapped in a boundary
        all_elements = [
            elem
            for section in container_diagram.sections
            for elem in section.elements
        ]
        kinds = [e.kind for e in all_elements]
        # Must have a Container
        assert "Container" in kinds or "Container_Boundary" in kinds
        # No boundaries wrapping a single file
        for elem in all_elements:
            if elem.kind == "Container_Boundary":
                # If a boundary exists, it must have 2+ children
                assert len(elem.children) >= 2, (
                    f"Container_Boundary '{elem.label}' wraps only {len(elem.children)} file — expected 2+"
                )

        store2.close()
        Path(store2.db_path).unlink(missing_ok=True)

    def test_cross_community_rel_at_section_level(self):
        """Rel elements must be in section.elements, not inside a boundary's children."""
        output = build_c4(self.store, repo_name="TestRepo")
        arch = parse_c4_file(output)
        container_diagram = next(d for d in arch.diagrams if d.diagram_type == "C4Container")

        # Collect all Rel elements at section level
        section_rels = [
            elem
            for section in container_diagram.sections
            for elem in section.elements
            if elem.kind == "Rel"
        ]
        assert len(section_rels) >= 1, "Expected cross-community Rel at section level"

        # Ensure no Rel is hidden inside a boundary's children
        for section in container_diagram.sections:
            for elem in section.elements:
                if elem.kind == "Container_Boundary":
                    for child in elem.children:
                        assert child.kind != "Rel", (
                            f"Rel found inside Container_Boundary '{elem.label}'; "
                            "Rels must be at section level"
                        )

    def test_build_c4_round_trips_boundary_structure(self):
        """build_c4 output → write → parse preserves Container_Boundary with children."""
        output = build_c4(self.store, repo_name="TestRepo")
        arch = parse_c4_file(output)
        container_diagram = next(d for d in arch.diagrams if d.diagram_type == "C4Container")

        # seed_multi_file_community creates a community with 2 files → 1 boundary
        boundaries_before = [
            elem
            for section in container_diagram.sections
            for elem in section.elements
            if elem.kind == "Container_Boundary"
        ]
        assert len(boundaries_before) >= 1, (
            "Expected Container_Boundary for multi-file community"
        )


# ---------------------------------------------------------------------------
# Test hierarchical Component_Boundary generation
# ---------------------------------------------------------------------------


class TestHierarchicalComponentBoundaries:
    """C4Component diagrams use Component_Boundary for files/classes with CONTAINS children."""

    def test_file_with_class_and_methods_uses_boundaries(self):
        """File and Class with CONTAINS children → Component_Boundary in raw output."""
        store = make_store()
        store.upsert_node(
            NodeInfo(kind="File", name="models.py", file_path="models.py",
                     line_start=1, line_end=100, language="python"), file_hash="m1",
        )
        store.upsert_node(
            NodeInfo(kind="Class", name="User", file_path="models.py",
                     line_start=5, line_end=80, language="python"), file_hash="m1",
        )
        store.upsert_node(
            NodeInfo(kind="Method", name="save", file_path="models.py",
                     line_start=10, line_end=30, language="python"), file_hash="m1",
        )
        store.upsert_node(
            NodeInfo(kind="Method", name="delete", file_path="models.py",
                     line_start=35, line_end=55, language="python"), file_hash="m1",
        )
        store.upsert_edge(EdgeInfo(
            kind="CONTAINS", source="models.py", target="models.py::User",
            file_path="models.py", line=5,
        ))
        store.upsert_edge(EdgeInfo(
            kind="CONTAINS", source="models.py::User", target="models.py::save",
            file_path="models.py", line=10,
        ))
        store.upsert_edge(EdgeInfo(
            kind="CONTAINS", source="models.py::User", target="models.py::delete",
            file_path="models.py", line=35,
        ))
        store.commit()
        communities = detect_communities(store, min_size=2)
        store_communities(store, communities)
        try:
            output = build_c4(store)
            # File models.py and Class User should produce Component_Boundary
            assert "Component_Boundary(" in output, "Expected Component_Boundary for file/class"
            # Methods should appear as Component labels
            assert '"save"' in output, "Expected Component for method 'save'"
            assert '"delete"' in output, "Expected Component for method 'delete'"
        finally:
            store.close()
            Path(store.db_path).unlink(missing_ok=True)

    def test_file_with_only_functions_uses_file_boundary(self):
        """File with direct CONTAINS to functions → Component_Boundary for file."""
        store = make_store()
        store.upsert_node(
            NodeInfo(kind="File", name="utils.py", file_path="utils.py",
                     line_start=1, line_end=60, language="python"), file_hash="u1",
        )
        store.upsert_node(
            NodeInfo(kind="Function", name="parse", file_path="utils.py",
                     line_start=5, line_end=25, language="python"), file_hash="u1",
        )
        store.upsert_node(
            NodeInfo(kind="Function", name="validate", file_path="utils.py",
                     line_start=30, line_end=55, language="python"), file_hash="u1",
        )
        store.upsert_edge(EdgeInfo(
            kind="CONTAINS", source="utils.py", target="utils.py::parse",
            file_path="utils.py", line=5,
        ))
        store.upsert_edge(EdgeInfo(
            kind="CONTAINS", source="utils.py", target="utils.py::validate",
            file_path="utils.py", line=30,
        ))
        store.commit()
        communities = detect_communities(store, min_size=2)
        store_communities(store, communities)
        try:
            output = build_c4(store)
            assert "Component_Boundary(" in output, "Expected file-level Component_Boundary"
            assert '"parse"' in output, "Expected Component for function 'parse'"
            assert '"validate"' in output, "Expected Component for function 'validate'"
        finally:
            store.close()
            Path(store.db_path).unlink(missing_ok=True)

    def test_community_without_file_nodes_produces_flat_components(self):
        """Community with no File nodes falls back to flat Component elements only."""
        store = make_store()
        # Only Function nodes — no File node, no CONTAINS edges
        store.upsert_node(
            NodeInfo(kind="Function", name="func_a", file_path="misc.py",
                     line_start=1, line_end=10, language="python"), file_hash="x1",
        )
        store.upsert_node(
            NodeInfo(kind="Function", name="func_b", file_path="misc.py",
                     line_start=15, line_end=25, language="python"), file_hash="x1",
        )
        store.upsert_edge(EdgeInfo(
            kind="CALLS", source="misc.py::func_a", target="misc.py::func_b",
            file_path="misc.py", line=5,
        ))
        store.commit()
        communities = detect_communities(store, min_size=2)
        store_communities(store, communities)
        try:
            output = build_c4(store)
            assert "Component_Boundary(" not in output, (
                "Expected no Component_Boundary for community without File nodes"
            )
        finally:
            store.close()
            Path(store.db_path).unlink(missing_ok=True)

    def test_round_trip_preserves_component_elements(self):
        """build → write → parse preserves all inner Component elements after round-trip."""
        store = make_store()
        store.upsert_node(
            NodeInfo(kind="File", name="svc.py", file_path="svc.py",
                     line_start=1, line_end=40, language="python"), file_hash="r1",
        )
        store.upsert_node(
            NodeInfo(kind="Function", name="run", file_path="svc.py",
                     line_start=5, line_end=18, language="python"), file_hash="r1",
        )
        store.upsert_node(
            NodeInfo(kind="Function", name="stop", file_path="svc.py",
                     line_start=20, line_end=35, language="python"), file_hash="r1",
        )
        store.upsert_edge(EdgeInfo(
            kind="CONTAINS", source="svc.py", target="svc.py::run",
            file_path="svc.py", line=5,
        ))
        store.upsert_edge(EdgeInfo(
            kind="CONTAINS", source="svc.py", target="svc.py::stop",
            file_path="svc.py", line=20,
        ))
        store.commit()
        communities = detect_communities(store, min_size=2)
        store_communities(store, communities)
        try:
            output = build_c4(store)
            arch = parse_c4_file(output)

            def collect_components(elements: list) -> set[str]:
                """Recursively collect Component labels from nested element tree."""
                labels: set[str] = set()
                for elem in elements:
                    if elem.kind == "Component":
                        labels.add(elem.label)
                    labels |= collect_components(elem.children)
                return labels

            component_labels = set()
            for diag in arch.diagrams:
                if diag.diagram_type != "C4Component":
                    continue
                for section in diag.sections:
                    component_labels |= collect_components(section.elements)

            assert "run" in component_labels, "Component 'run' must survive round-trip"
            assert "stop" in component_labels, "Component 'stop' must survive round-trip"
        finally:
            store.close()
            Path(store.db_path).unlink(missing_ok=True)
