"""Tests for standards_tools.py — get_project_standards MCP tool."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_no_prompts_directory(tmp_path: Path) -> None:
    """Test with no prompts/ directory — should return empty sections list."""
    from code_review_graph.tools.standards_tools import get_project_standards_func

    # Create a minimal repo structure
    git_dir = tmp_path / ".git"
    git_dir.mkdir()

    result = get_project_standards_func(repo_root=str(tmp_path))

    assert result["status"] == "ok"
    assert result["sections"] == []
    assert "No prompts/ directory found" in result["summary"]


def test_list_sections(tmp_path: Path) -> None:
    """Test listing sections when prompts/ contains files."""
    from code_review_graph.tools.standards_tools import get_project_standards_func

    # Create repo structure
    git_dir = tmp_path / ".git"
    git_dir.mkdir()

    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()

    # Create some .md files
    (prompts_dir / "patterns.md").write_text("# Patterns\nSome patterns here")
    (prompts_dir / "stack.md").write_text("# Stack\nTech stack info")
    (prompts_dir / "conventions.md").write_text("# Conventions\nCode conventions")

    # Also create a non-.md file (should be ignored)
    (prompts_dir / "readme.txt").write_text("This should be ignored")

    result = get_project_standards_func(repo_root=str(tmp_path))

    assert result["status"] == "ok"
    assert len(result["sections"]) == 3

    section_names = {s["name"] for s in result["sections"]}
    assert section_names == {"patterns", "stack", "conventions"}

    # Check that size_bytes is present
    for section in result["sections"]:
        assert "size_bytes" in section
        assert section["size_bytes"] > 0


def test_read_specific_section(tmp_path: Path) -> None:
    """Test reading a specific section."""
    from code_review_graph.tools.standards_tools import get_project_standards_func

    # Create repo structure
    git_dir = tmp_path / ".git"
    git_dir.mkdir()

    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()

    content = "# Patterns\n\nUse snake_case for variables"
    (prompts_dir / "patterns.md").write_text(content)

    result = get_project_standards_func(section="patterns", repo_root=str(tmp_path))

    assert result["status"] == "ok"
    assert result["section"] == "patterns"
    assert result["content"] == content


def test_read_nonexistent_section(tmp_path: Path) -> None:
    """Test reading a section that doesn't exist."""
    from code_review_graph.tools.standards_tools import get_project_standards_func

    # Create repo structure
    git_dir = tmp_path / ".git"
    git_dir.mkdir()

    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()

    (prompts_dir / "patterns.md").write_text("# Patterns")

    result = get_project_standards_func(section="nonexistent", repo_root=str(tmp_path))

    assert result["status"] == "error"
    assert result.get("code") == "NOT_FOUND"


def test_invalid_section_name_path_traversal(tmp_path: Path) -> None:
    """Test that path traversal attempts are blocked."""
    from code_review_graph.tools.standards_tools import get_project_standards_func

    # Create repo structure
    git_dir = tmp_path / ".git"
    git_dir.mkdir()

    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()

    # Try path traversal
    result = get_project_standards_func(section="../etc/passwd", repo_root=str(tmp_path))

    assert result["status"] == "error"
    assert result.get("code") == "INVALID_PARAMS"


def test_invalid_section_name_with_slash(tmp_path: Path) -> None:
    """Test that section names with slashes are rejected."""
    from code_review_graph.tools.standards_tools import get_project_standards_func

    # Create repo structure
    git_dir = tmp_path / ".git"
    git_dir.mkdir()

    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()

    result = get_project_standards_func(section="subdir/patterns", repo_root=str(tmp_path))

    assert result["status"] == "error"
    assert result.get("code") == "INVALID_PARAMS"


def test_invalid_section_name_with_dot(tmp_path: Path) -> None:
    """Test that section names starting with dot are rejected."""
    from code_review_graph.tools.standards_tools import get_project_standards_func

    # Create repo structure
    git_dir = tmp_path / ".git"
    git_dir.mkdir()

    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()

    result = get_project_standards_func(section=".hidden", repo_root=str(tmp_path))

    assert result["status"] == "error"
    assert result.get("code") == "INVALID_PARAMS"


def test_auto_detect_repo_root(tmp_path: Path) -> None:
    """Test that repo_root is auto-detected when omitted."""
    from code_review_graph.tools.standards_tools import get_project_standards_func

    # Create repo structure
    git_dir = tmp_path / ".git"
    git_dir.mkdir()

    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()

    (prompts_dir / "test.md").write_text("# Test")

    # Change to the repo directory
    import os

    old_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        result = get_project_standards_func()

        assert result["status"] == "ok"
        assert len(result["sections"]) == 1
        assert result["sections"][0]["name"] == "test"
    finally:
        os.chdir(old_cwd)


def test_sections_sorted_alphabetically(tmp_path: Path) -> None:
    """Test that sections are returned in sorted order."""
    from code_review_graph.tools.standards_tools import get_project_standards_func

    # Create repo structure
    git_dir = tmp_path / ".git"
    git_dir.mkdir()

    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()

    # Create files in non-alphabetical order
    (prompts_dir / "zebra.md").write_text("z")
    (prompts_dir / "apple.md").write_text("a")
    (prompts_dir / "middle.md").write_text("m")

    result = get_project_standards_func(repo_root=str(tmp_path))

    assert result["status"] == "ok"
    names = [s["name"] for s in result["sections"]]
    assert names == ["apple", "middle", "zebra"]


def test_empty_prompts_directory(tmp_path: Path) -> None:
    """Test with empty prompts/ directory."""
    from code_review_graph.tools.standards_tools import get_project_standards_func

    # Create repo structure
    git_dir = tmp_path / ".git"
    git_dir.mkdir()

    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()

    result = get_project_standards_func(repo_root=str(tmp_path))

    assert result["status"] == "ok"
    assert result["sections"] == []
    assert "Found 0 section(s)" in result["summary"]


def test_section_with_special_characters(tmp_path: Path) -> None:
    """Test reading a section with special characters in content."""
    from code_review_graph.tools.standards_tools import get_project_standards_func

    # Create repo structure
    git_dir = tmp_path / ".git"
    git_dir.mkdir()

    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()

    content = "# Patterns\n\n```python\ndef foo():\n    pass\n```\n\nSpecial: @#$%^&*()"
    (prompts_dir / "patterns.md").write_text(content)

    result = get_project_standards_func(section="patterns", repo_root=str(tmp_path))

    assert result["status"] == "ok"
    assert result["content"] == content
