"""Tool: get_project_standards — read prompts/ project knowledge base."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ._common import _validate_repo_root, graph_error


def get_project_standards_func(
    section: str | None = None,
    repo_root: str | None = None,
) -> dict[str, Any]:
    """Read project standards from prompts/ directory.

    Returns content of standard files that define coding patterns,
    architecture principles, and conventions for this project.

    Args:
        section: Specific file to read (without .md extension).
                 E.g. "patterns", "stack", "conventions".
                 Omit to get index of available sections.
        repo_root: Repository root path.

    Returns:
        If section specified: { status, section, content }
        If no section: { status, sections: [{name, size_bytes}] }
    """
    try:
        if repo_root:
            root = _validate_repo_root(Path(repo_root))
        else:
            from ..incremental import find_project_root

            root = find_project_root()

        prompts_dir = root / "prompts"

        # If prompts/ doesn't exist, return empty sections list
        if not prompts_dir.exists():
            return {
                "status": "ok",
                "sections": [],
                "summary": "No prompts/ directory found",
            }

        # If section is specified, read that file
        if section:
            # Sanitize section name to prevent path traversal
            if "/" in section or "\\" in section or section.startswith("."):
                return graph_error(
                    "INVALID_PARAMS",
                    f"Invalid section name: {section}",
                )

            section_file = prompts_dir / f"{section}.md"

            if not section_file.exists():
                return graph_error(
                    "NOT_FOUND",
                    f"Section '{section}' not found in prompts/",
                )

            # Ensure the file is within prompts_dir (security check)
            try:
                section_file.resolve().relative_to(prompts_dir.resolve())
            except ValueError:
                return graph_error(
                    "INVALID_PARAMS",
                    "Section file is outside prompts/ directory",
                )

            content = section_file.read_text(encoding="utf-8")
            return {
                "status": "ok",
                "section": section,
                "content": content,
            }

        # List all .md files in prompts/
        sections = []
        for md_file in sorted(prompts_dir.glob("*.md")):
            sections.append(
                {
                    "name": md_file.stem,
                    "size_bytes": md_file.stat().st_size,
                }
            )

        return {
            "status": "ok",
            "sections": sections,
            "summary": f"Found {len(sections)} section(s) in prompts/",
        }

    except ValueError as exc:
        return graph_error("INVALID_PARAMS", str(exc))
    except Exception as exc:
        return graph_error("INTERNAL_ERROR", str(exc))
