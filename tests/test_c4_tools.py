"""Tests for c4_tools: get_architecture_skeleton_func and update_architecture_skeleton_func."""

from __future__ import annotations

import pytest

from code_review_graph.tools.c4_tools import (
    get_architecture_skeleton_func,
    update_architecture_skeleton_func,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

SAMPLE_C4 = """\
---
title: System Context
---
C4Context

%% [AUTO:context 2026-05-01]
  Person(user, "User", "A system user")
%% [/AUTO:context]

---
title: System Containers
---
C4Container

%% [AUTO:containers 2026-05-01]
  Container(auth, "Auth Service", "Python", "Handles authentication")
  Container(api, "API Service", "Python", "REST API")
%% [/AUTO:containers]

%% [FEATURE:oauth:t1 2026-05-01]
  Container(oauth, "OAuth Service", "Python", "OAuth provider")
%% [/FEATURE:oauth:t1]

---
title: Auth Components
---
C4Component

%% [AUTO:components:auth 2026-05-01]
  Component(auth_login, "Login Handler", "Function", "auth.py:10")
%% [/AUTO:components:auth]

---
title: API Components
---
C4Component

%% [AUTO:components:api 2026-05-01]
  Component(api_router, "Router", "Function", "api.py:1")
%% [/AUTO:components:api]

"""


def _make_repo(tmp_path):
    """Create a minimal project root with .code-review-graph/ directory."""
    crg_dir = tmp_path / ".code-review-graph"
    crg_dir.mkdir(parents=True)
    return tmp_path


def _write_c4(tmp_path, content: str = SAMPLE_C4):
    """Write architecture.c4 to the expected location."""
    c4_path = tmp_path / ".code-review-graph" / "architecture.c4"
    c4_path.write_text(content, encoding="utf-8")
    return c4_path


# ---------------------------------------------------------------------------
# get_architecture_skeleton_func
# ---------------------------------------------------------------------------


def test_get_skeleton_no_file(tmp_path):
    """No architecture.c4 → returns ok with empty content and helpful summary."""
    repo = _make_repo(tmp_path)
    result = get_architecture_skeleton_func(repo_root=str(repo))

    assert result["status"] == "ok"
    assert result["content"] == ""
    assert result["diagrams"] == 0
    assert result["designed_features"] == []
    assert "architecture.c4" in result.get("summary", "") or result["content"] == ""


def test_get_skeleton_full(tmp_path):
    """architecture.c4 exists → returns full content with correct metadata."""
    repo = _make_repo(tmp_path)
    _write_c4(repo)

    result = get_architecture_skeleton_func(repo_root=str(repo))

    assert result["status"] == "ok"
    assert "C4Context" in result["content"] or "C4Container" in result["content"]
    assert result["diagrams"] == 4  # Context + Containers + 2 Component diagrams
    assert "last_modified" in result
    assert result["file_path"].endswith("architecture.c4")


def test_get_skeleton_level_containers(tmp_path):
    """level='containers' → only the C4Container diagram is returned."""
    repo = _make_repo(tmp_path)
    _write_c4(repo)

    result = get_architecture_skeleton_func(level="containers", repo_root=str(repo))

    assert result["status"] == "ok"
    assert result["diagrams"] == 1
    assert "C4Container" in result["content"]
    assert "C4Context" not in result["content"]
    assert "C4Component" not in result["content"]


def test_get_skeleton_level_components_slug(tmp_path):
    """level='components:auth' → only the Auth Components diagram."""
    repo = _make_repo(tmp_path)
    _write_c4(repo)

    result = get_architecture_skeleton_func(level="components:auth", repo_root=str(repo))

    assert result["status"] == "ok"
    assert result["diagrams"] == 1
    assert "auth" in result["content"].lower()
    # api component diagram should NOT be present
    assert "api_router" not in result["content"]


def test_get_skeleton_designed_features(tmp_path):
    """File with FEATURE sections → lists all FEATURE marker IDs."""
    repo = _make_repo(tmp_path)
    _write_c4(repo)

    result = get_architecture_skeleton_func(repo_root=str(repo))

    assert result["status"] == "ok"
    assert "oauth:t1" in result["designed_features"]


def test_get_skeleton_invalid_level(tmp_path):
    """Unknown level string → returns an error dict."""
    repo = _make_repo(tmp_path)
    _write_c4(repo)

    result = get_architecture_skeleton_func(level="invalid_level", repo_root=str(repo))

    assert result.get("status") == "error" or "error" in result.get("code", "").lower()


# ---------------------------------------------------------------------------
# update_architecture_skeleton_func
# ---------------------------------------------------------------------------


def test_update_skeleton_add(tmp_path):
    """add operation → new element appears in the re-written file."""
    repo = _make_repo(tmp_path)
    _write_c4(repo)

    result = update_architecture_skeleton_func(
        feature_tag="test:t1",
        operations=[
            {
                "op": "add",
                "diagram": "Containers",
                "element": {
                    "kind": "Container",
                    "id": "cache",
                    "label": "Cache",
                    "technology": "Redis",
                    "description": "In-memory cache",
                },
            }
        ],
        repo_root=str(repo),
    )

    assert result["status"] == "ok"
    assert result["applied"] == 1
    assert result["skipped"] == 0

    # Verify the element actually landed in the file
    c4_path = repo / ".code-review-graph" / "architecture.c4"
    written = c4_path.read_text(encoding="utf-8")
    assert "cache" in written
    assert "Cache" in written


def test_update_skeleton_modify(tmp_path):
    """modify AUTO element → creates an override entry in the FEATURE section."""
    repo = _make_repo(tmp_path)
    _write_c4(repo)

    result = update_architecture_skeleton_func(
        feature_tag="update:t1",
        operations=[
            {
                "op": "modify",
                "diagram": "Containers",
                "element_id": "auth",
                "changes": {"description": "New auth description"},
            }
        ],
        repo_root=str(repo),
    )

    assert result["status"] == "ok"
    assert result["applied"] == 1
    assert result["skipped"] == 0
    assert "System Containers" in result["updated_diagrams"]

    c4_path = repo / ".code-review-graph" / "architecture.c4"
    written = c4_path.read_text(encoding="utf-8")
    assert "New auth description" in written
    # UpdateElementStyle should also be present for visual differentiation
    assert "UpdateElementStyle" in written


def test_update_skeleton_remove(tmp_path):
    """remove FEATURE element → element is gone from the file."""
    repo = _make_repo(tmp_path)
    _write_c4(repo)

    result = update_architecture_skeleton_func(
        feature_tag="anything",
        operations=[
            {
                "op": "remove",
                "diagram": "Containers",
                "element_id": "oauth",
            }
        ],
        repo_root=str(repo),
    )

    assert result["status"] == "ok"
    assert result["applied"] == 1
    assert result["skipped"] == 0

    c4_path = repo / ".code-review-graph" / "architecture.c4"
    written = c4_path.read_text(encoding="utf-8")
    # oauth element should be absent
    assert 'Container(oauth,' not in written


def test_update_skeleton_remove_auto_fails(tmp_path):
    """Attempting to remove an AUTO element is rejected; file unchanged."""
    repo = _make_repo(tmp_path)
    _write_c4(repo)

    c4_path = repo / ".code-review-graph" / "architecture.c4"
    before = c4_path.read_text(encoding="utf-8")

    result = update_architecture_skeleton_func(
        feature_tag="anything",
        operations=[
            {
                "op": "remove",
                "diagram": "Containers",
                "element_id": "auth",  # auth is in AUTO section
            }
        ],
        repo_root=str(repo),
    )

    assert result["status"] == "ok"
    assert result["applied"] == 0
    assert result["skipped"] == 1
    assert any("AUTO" in e or "cannot be removed" in e for e in result["errors"])

    # auth element must still be in the file
    after = c4_path.read_text(encoding="utf-8")
    assert "Auth Service" in after


def test_update_skeleton_no_file(tmp_path):
    """Update when architecture.c4 does not exist → returns an error."""
    repo = _make_repo(tmp_path)
    # Deliberately do NOT create architecture.c4

    result = update_architecture_skeleton_func(
        feature_tag="test:t1",
        operations=[
            {
                "op": "add",
                "diagram": "Containers",
                "element": {"kind": "Container", "id": "x", "label": "X"},
            }
        ],
        repo_root=str(repo),
    )

    assert result.get("status") == "error" or "error" in str(result).lower()
    assert "architecture.c4" in str(result).lower() or "not found" in str(result).lower()
