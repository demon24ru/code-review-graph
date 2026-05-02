"""Tests for the dashboard CLI command."""
from __future__ import annotations

import subprocess
import sys


def test_dashboard_command_help() -> None:
    """Test that 'code-review-graph dashboard --help' shows help."""
    result = subprocess.run(
        [sys.executable, "-m", "code_review_graph", "dashboard", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "dashboard" in result.stdout.lower()
    assert "--port" in result.stdout
    assert "--task" in result.stdout
    assert "--no-browser" in result.stdout


def test_dashboard_registered() -> None:
    """Test that the dashboard subcommand is registered in the parser."""
    # Capture output from --help
    result = subprocess.run(
        [sys.executable, "-m", "code_review_graph", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "dashboard" in result.stdout
