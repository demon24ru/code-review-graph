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
        """Each community should appear as a Container element."""
        from code_review_graph.communities import get_communities

        communities = get_communities(self.store, exclude_tests=True)
        output = build_c4(self.store, repo_name="TestRepo")
        arch = parse_c4_file(output)

        container_diagram = next(d for d in arch.diagrams if d.diagram_type == "C4Container")
        container_labels = set()
        for section in container_diagram.sections:
            for elem in section.elements:
                if elem.kind == "Container":
                    container_labels.add(elem.label)

        for comm in communities:
            assert comm["name"] in container_labels, (
                f"Community '{comm['name']}' missing from Container diagram"
            )

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
                    if elem.kind == "Container":
                        auto_id = elem.id
                        break
            if auto_id:
                break
        assert auto_id is not None, "Need at least one Container in AUTO section"

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
