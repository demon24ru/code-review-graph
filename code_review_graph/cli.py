"""CLI entry point for code-review-graph.

Usage:
    code-review-graph install
    code-review-graph init
    code-review-graph build [--base BASE]
    code-review-graph update [--base BASE]
    code-review-graph watch
    code-review-graph status
    code-review-graph serve
    code-review-graph visualize
    code-review-graph wiki
    code-review-graph c4 [--rebuild]
    code-review-graph detect-changes [--base BASE] [--brief]
    code-review-graph register <path> [--alias name]
    code-review-graph unregister <path_or_alias>
    code-review-graph repos
    code-review-graph dashboard [--port PORT] [--task ID] [--no-browser]
"""

from __future__ import annotations

import sys

# Python version check — must come before any other imports
if sys.version_info < (3, 10):
    print("code-review-graph requires Python 3.10 or higher.")
    print(f"  You are running Python {sys.version}")
    print()
    print("Install Python 3.10+: https://www.python.org/downloads/")
    sys.exit(1)

import argparse
import json
import logging
import os
from importlib.metadata import version as pkg_version
from pathlib import Path


def _get_version() -> str:
    """Get the installed package version."""
    try:
        return pkg_version("code-review-graph")
    except Exception:
        return "dev"


def _supports_color() -> bool:
    """Check if the terminal likely supports ANSI colors."""
    if os.environ.get("NO_COLOR"):
        return False
    if not hasattr(sys.stdout, "isatty"):
        return False
    return sys.stdout.isatty()


def _print_banner() -> None:
    """Print the startup banner with graph art and available commands."""
    color = _supports_color()
    version = _get_version()

    # ANSI escape codes
    c = "\033[36m" if color else ""   # cyan — graph art
    y = "\033[33m" if color else ""   # yellow — center node
    b = "\033[1m" if color else ""    # bold
    d = "\033[2m" if color else ""    # dim
    g = "\033[32m" if color else ""   # green — commands
    r = "\033[0m" if color else ""    # reset

    print(f"""
{c}  ●──●──●{r}
{c}  │╲ │ ╱│{r}       {b}code-review-graph{r}  {d}v{version}{r}
{c}  ●──{y}◆{c}──●{r}
{c}  │╱ │ ╲│{r}       {d}Structural knowledge graph for{r}
{c}  ●──●──●{r}       {d}smarter code reviews{r}

  {b}Commands:{r}
    {g}install{r}     Set up MCP server for AI coding platforms
    {g}init{r}        Alias for install
    {g}build{r}       Full graph build {d}(parse all files){r}
    {g}update{r}      Incremental update {d}(changed files only){r}
    {g}watch{r}       Auto-update on file changes
    {g}status{r}      Show graph statistics
    {g}visualize{r}   Generate interactive HTML graph
    {g}wiki{r}        Generate markdown wiki from communities
    {g}c4{r}          Generate C4 architecture diagram
    {g}detect-changes{r} Analyze change impact {d}(risk-scored review){r}
    {g}task-report{r}  Generate markdown report of the full task tree
    {g}register{r}    Register a repository in the multi-repo registry
    {g}unregister{r}  Remove a repository from the registry
    {g}repos{r}       List registered repositories
    {g}questions{r}   Open web UI for answering brainstorm questions
    {g}dashboard{r}   Launch interactive dashboard
    {g}eval{r}        Run evaluation benchmarks
    {g}serve{r}       Start MCP server

  {d}Run{r} {b}code-review-graph <command> --help{r} {d}for details{r}
""")


def _handle_init(args: argparse.Namespace) -> None:
    """Set up MCP config for detected AI coding platforms."""
    from .incremental import find_repo_root
    from .skills import install_platform_configs

    repo_root = Path(args.repo) if args.repo else find_repo_root()
    if not repo_root:
        repo_root = Path.cwd()

    dry_run = getattr(args, "dry_run", False)
    target = getattr(args, "platform", "all") or "all"
    if target == "claude-code":
        target = "claude"

    print("Installing MCP server config...")
    configured = install_platform_configs(repo_root, target=target, dry_run=dry_run)

    if not configured:
        print("No platforms detected.")
    else:
        print(f"\nConfigured {len(configured)} platform(s): {', '.join(configured)}")

    if dry_run:
        print("\n[dry-run] No files were modified.")
        return

    # Skills and hooks are installed by default so Claude actually uses the
    # graph tools proactively.  Use --no-skills / --no-hooks to opt out.
    skip_skills = getattr(args, "no_skills", False)
    skip_hooks = getattr(args, "no_hooks", False)
    # Legacy: --skills/--hooks/--all still accepted (no-op, everything is default)

    from .skills import (
        generate_skills,
        inject_claude_md,
        inject_platform_instructions,
        install_hooks,
    )

    if not skip_skills:
        skills_dir = generate_skills(repo_root)
        print(f"Generated skills in {skills_dir}")
        inject_claude_md(repo_root)
        updated = inject_platform_instructions(repo_root)
        if updated:
            print(f"Injected graph instructions into: {', '.join(updated)}")

    if not skip_hooks:
        install_hooks(repo_root)
        print(f"Installed hooks in {repo_root / '.claude' / 'settings.json'}")

    print()
    print("Next steps:")
    print("  1. code-review-graph build    # build the knowledge graph")
    print("  2. Restart your AI coding tool to pick up the new config")


def _cmd_c4(args: argparse.Namespace) -> None:
    """Generate or rebuild architecture.c4 from the code graph."""
    from .c4_generator import build_c4, get_c4_path, rebuild_c4
    from .graph import GraphStore
    from .incremental import find_project_root, get_db_path

    root = Path(args.repo) if args.repo else find_project_root()
    if not root:
        root = Path.cwd()

    db = get_db_path(root)
    if not db.exists():
        print("No graph database found. Run 'code-review-graph build' first.")
        sys.exit(1)

    store = GraphStore(str(db))
    try:
        c4_path = get_c4_path(str(root))
        repo_name = root.name  # use directory name as repo name

        if args.rebuild and c4_path.exists():
            existing = c4_path.read_text(encoding="utf-8")
            content = rebuild_c4(store, existing, repo_name)
            action = "Rebuilt"
        else:
            content = build_c4(store, repo_name)
            action = "Generated"

        # Ensure parent directory exists
        c4_path.parent.mkdir(parents=True, exist_ok=True)
        c4_path.write_text(content, encoding="utf-8")

        # Count diagrams for user feedback
        line_count = len(content.strip().split("\n"))
        print(f"{action} architecture.c4 ({line_count} lines)")
        print(f"  Path: {c4_path}")
    finally:
        store.close()


def _cmd_dashboard(args: argparse.Namespace) -> None:
    """Launch the dashboard web UI."""
    from .dashboard_ui import start_dashboard
    from .incremental import find_project_root, get_db_path

    root = Path(args.repo) if args.repo else find_project_root()
    if not root:
        root = Path.cwd()

    db = get_db_path(root)
    if not db.exists():
        print("No graph database found. Run 'code-review-graph build' first.")
        sys.exit(1)

    # Auto-detect active root task if not specified
    root_task_id = args.task
    if not root_task_id:
        import sqlite3
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        try:
            from .tasks import get_active_root
            active = get_active_root(conn)
            if active:
                root_task_id = active["id"]
        finally:
            conn.close()

    start_dashboard(
        port=args.port,
        root_task_id=root_task_id,
        repo_root=str(root),
        open_browser=not args.no_browser,
    )


def _activate_windows_ansi():
    """Включает поддержку ANSI-последовательностей в консоли Windows."""
    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        # -11 — это стандартный вывод (STD_OUTPUT_HANDLE)
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            # 0x0004 — это ENABLE_VIRTUAL_TERMINAL_PROCESSING
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)


def main() -> None:
    _activate_windows_ansi()
    """Main CLI entry point."""
    ap = argparse.ArgumentParser(
        prog="code-review-graph",
        description="Persistent incremental knowledge graph for code reviews",
    )
    ap.add_argument(
        "-v", "--version", action="store_true", help="Show version and exit"
    )
    sub = ap.add_subparsers(dest="command")

    # install (primary) + init (alias)
    install_cmd = sub.add_parser(
        "install", help="Register MCP server with AI coding platforms"
    )
    install_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")
    install_cmd.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be done without writing files",
    )
    install_cmd.add_argument(
        "--no-skills", action="store_true",
        help="Skip generating Claude Code skill files",
    )
    install_cmd.add_argument(
        "--no-hooks", action="store_true",
        help="Skip installing Claude Code hooks",
    )
    # Legacy flags (kept for backwards compat, now no-ops since all is default)
    install_cmd.add_argument("--skills", action="store_true", help=argparse.SUPPRESS)
    install_cmd.add_argument("--hooks", action="store_true", help=argparse.SUPPRESS)
    install_cmd.add_argument("--all", action="store_true", dest="install_all",
                             help=argparse.SUPPRESS)
    install_cmd.add_argument(
        "--platform",
        choices=[
            "claude", "claude-code", "cursor", "windsurf", "zed",
            "continue", "opencode", "antigravity", "all",
        ],
        default="all",
        help="Target platform for MCP config (default: all detected)",
    )

    init_cmd = sub.add_parser(
        "init", help="Alias for install"
    )
    init_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")
    init_cmd.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be done without writing files",
    )
    init_cmd.add_argument(
        "--no-skills", action="store_true",
        help="Skip generating Claude Code skill files",
    )
    init_cmd.add_argument(
        "--no-hooks", action="store_true",
        help="Skip installing Claude Code hooks",
    )
    init_cmd.add_argument("--skills", action="store_true", help=argparse.SUPPRESS)
    init_cmd.add_argument("--hooks", action="store_true", help=argparse.SUPPRESS)
    init_cmd.add_argument("--all", action="store_true", dest="install_all",
                             help=argparse.SUPPRESS)
    init_cmd.add_argument(
        "--platform",
        choices=[
            "claude", "claude-code", "cursor", "windsurf", "zed",
            "continue", "opencode", "antigravity", "all",
        ],
        default="all",
        help="Target platform for MCP config (default: all detected)",
    )

    # build
    build_cmd = sub.add_parser("build", help="Full graph build (re-parse all files)")
    build_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")

    # update
    update_cmd = sub.add_parser("update", help="Incremental update (only changed files)")
    update_cmd.add_argument("--base", default="HEAD~1", help="Git diff base (default: HEAD~1)")
    update_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")

    # watch
    watch_cmd = sub.add_parser("watch", help="Watch for changes and auto-update")
    watch_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")

    # status
    status_cmd = sub.add_parser("status", help="Show graph statistics")
    status_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")

    # visualize
    vis_cmd = sub.add_parser("visualize", help="Generate interactive HTML graph visualization")
    vis_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")
    vis_cmd.add_argument(
        "--serve", action="store_true",
        help="Start a local HTTP server to view the visualization (localhost:8765)",
    )

    # wiki
    wiki_cmd = sub.add_parser("wiki", help="Generate markdown wiki from community structure")
    wiki_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")
    wiki_cmd.add_argument(
        "--force", action="store_true",
        help="Regenerate all pages even if content unchanged",
    )

    # c4
    c4_cmd = sub.add_parser("c4", help="Generate C4 architecture diagram")
    c4_cmd.add_argument(
        "--rebuild", action="store_true",
        help="Update [AUTO] sections, preserve [FEATURE]",
    )
    c4_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")
    c4_cmd.set_defaults(func=_cmd_c4)

    # register
    register_cmd = sub.add_parser(
        "register", help="Register a repository in the multi-repo registry"
    )
    register_cmd.add_argument("path", help="Path to the repository root")
    register_cmd.add_argument("--alias", default=None, help="Short alias for the repository")

    # unregister
    unregister_cmd = sub.add_parser(
        "unregister", help="Remove a repository from the multi-repo registry"
    )
    unregister_cmd.add_argument("path_or_alias", help="Repository path or alias to remove")

    # repos
    sub.add_parser("repos", help="List registered repositories")

    # questions
    questions_cmd = sub.add_parser(
        "questions",
        help="Open web UI for answering brainstorm questions",
    )
    questions_cmd.add_argument(
        "--task", default=None, metavar="ID",
        help="Root task ID (auto-detected from active task if omitted)",
    )
    questions_cmd.add_argument(
        "--port", type=int, default=6234,
        help="Local port (default: 6234)",
    )
    questions_cmd.add_argument(
        "--no-browser", action="store_true",
        help="Don't open browser automatically",
    )
    questions_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")

    # dashboard
    dashboard_cmd = sub.add_parser(
        "dashboard",
        help="Launch interactive dashboard",
    )
    dashboard_cmd.add_argument(
        "--port", type=int, default=6235,
        help="Local port (default: 6235)",
    )
    dashboard_cmd.add_argument(
        "--task", default=None, metavar="ID",
        help="Root task ID (auto-detected from active task if omitted)",
    )
    dashboard_cmd.add_argument(
        "--no-browser", action="store_true",
        help="Don't open browser automatically",
    )
    dashboard_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")
    dashboard_cmd.set_defaults(func=_cmd_dashboard)

    # eval
    eval_cmd = sub.add_parser("eval", help="Run evaluation benchmarks")
    eval_cmd.add_argument(
        "--benchmark", default=None,
        help="Comma-separated benchmarks to run (token_efficiency, impact_accuracy, "
             "flow_completeness, search_quality, build_performance)",
    )
    eval_cmd.add_argument("--repo", default=None, help="Comma-separated repo config names")
    eval_cmd.add_argument("--all", action="store_true", dest="run_all", help="Run all benchmarks")
    eval_cmd.add_argument("--report", action="store_true", help="Generate report from results")
    eval_cmd.add_argument("--output-dir", default=None, help="Output directory for results")

    # detect-changes
    detect_cmd = sub.add_parser("detect-changes", help="Analyze change impact")
    detect_cmd.add_argument(
        "--base", default="HEAD~1", help="Git diff base (default: HEAD~1)"
    )
    detect_cmd.add_argument(
        "--brief", action="store_true", help="Show brief summary only"
    )
    detect_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")

    # task-report
    task_report_cmd = sub.add_parser(
        "task-report",
        help="Generate a human- and LLM-readable markdown report of the full task tree",
    )
    task_report_cmd.add_argument(
        "root_task_id",
        nargs="?",
        default=None,
        help="Root task ID (optional — auto-detected from the active root task if omitted)",
    )
    task_report_cmd.add_argument(
        "--repo", default=None, help="Repository root (auto-detected)"
    )
    task_report_cmd.add_argument(
        "--output-dir", default=None,
        help="Output directory (default: <repo>/.code-review-graph/tasks/)",
    )
    task_report_cmd.add_argument(
        "--force", action="store_true",
        help="Regenerate even if content is unchanged",
    )

    # serve
    serve_cmd = sub.add_parser("serve", help="Start MCP server (stdio transport)")
    serve_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")

    args = ap.parse_args()

    if args.version:
        print(f"code-review-graph {_get_version()}")
        return

    if not args.command:
        _print_banner()
        return

    if args.command == "serve":
        from .main import main as serve_main
        serve_main(repo_root=args.repo)
        return

    if args.command == "eval":
        from .eval.reporter import generate_full_report, generate_readme_tables
        from .eval.runner import run_eval

        if getattr(args, "report", False):
            output_dir = Path(
                getattr(args, "output_dir", None) or "evaluate/results"
            )
            report = generate_full_report(output_dir)
            report_path = Path("evaluate/reports/summary.md")
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(report, encoding="utf-8")
            print(f"Report written to {report_path}")

            tables = generate_readme_tables(output_dir)
            print("\n--- README Tables (copy-paste) ---\n")
            print(tables)
        else:
            repos = (
                [r.strip() for r in args.repo.split(",")]
                if getattr(args, "repo", None)
                else None
            )
            benchmarks = (
                [b.strip() for b in args.benchmark.split(",")]
                if getattr(args, "benchmark", None)
                else None
            )

            if not repos and not benchmarks and not getattr(args, "run_all", False):
                print("Specify --all, --repo, or --benchmark. See --help.")
                return

            results = run_eval(
                repos=repos,
                benchmarks=benchmarks,
                output_dir=getattr(args, "output_dir", None),
            )
            print(f"\nCompleted {len(results)} benchmark(s).")
            print("Run 'code-review-graph eval --report' to generate tables.")
        return

    if args.command in ("init", "install"):
        _handle_init(args)
        return

    if args.command in ("register", "unregister", "repos", "questions", "dashboard"):
        logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
        from .registry import Registry

        registry = Registry()
        if args.command == "register":
            try:
                entry = registry.register(args.path, alias=args.alias)
                alias_info = f" (alias: {entry['alias']})" if entry.get("alias") else ""
                print(f"Registered: {entry['path']}{alias_info}")
            except ValueError as exc:
                logging.error(str(exc))
                sys.exit(1)
        elif args.command == "unregister":
            if registry.unregister(args.path_or_alias):
                print(f"Unregistered: {args.path_or_alias}")
            else:
                print(f"Not found: {args.path_or_alias}")
                sys.exit(1)
        elif args.command == "repos":
            repos = registry.list_repos()
            if not repos:
                print("No repositories registered.")
                print("Use: code-review-graph register <path> [--alias name]")
            else:
                for entry in repos:
                    alias = entry.get("alias", "")
                    alias_str = f"  ({alias})" if alias else ""
                    print(f"  {entry['path']}{alias_str}")
        elif args.command == "questions":
            from .incremental import find_project_root
            from .questions_ui import serve as serve_questions
            repo_root = Path(args.repo) if args.repo else find_project_root()
            if not repo_root:
                print("Error: Could not detect repository root.")
                sys.exit(1)
            try:
                serve_questions(
                    root_task_id=args.task,
                    repo_root=repo_root,
                    port=args.port,
                    open_browser=not args.no_browser,
                )
            except ValueError as exc:
                print(f"Error: {exc}")
                sys.exit(1)
            return
        elif args.command == "dashboard":
            _cmd_dashboard(args)
            return
        return

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    from .graph import GraphStore
    from .incremental import (
        find_project_root,
        find_repo_root,
        full_build,
        get_db_path,
        incremental_update,
        watch,
    )

    if args.command in ("update", "detect-changes"):
        # update and detect-changes require git for diffing
        repo_root = Path(args.repo) if args.repo else find_repo_root()
        if not repo_root:
            logging.error(
                "Not in a git repository. '%s' requires git for diffing.",
                args.command,
            )
            logging.error("Use 'build' for a full parse, or run 'git init' first.")
            sys.exit(1)
    else:
        repo_root = Path(args.repo) if args.repo else find_project_root()

    db_path = get_db_path(repo_root)
    store = GraphStore(db_path, repo_root=repo_root)

    try:
        if args.command == "build":
            # Pre-warm igraph/matplotlib so community detection doesn't add
            # cold-import latency to the reported elapsed time.
            try:
                import code_review_graph.communities  # noqa: F401
            except Exception:
                pass
            result = full_build(repo_root, store)
            elapsed = result.get("elapsed_sec", 0)
            nps = result.get("nodes_per_sec", 0)
            print(
                f"Full build: {result['files_parsed']} files, "
                f"{result['total_nodes']} nodes, {result['total_edges']} edges "
                f"[{elapsed:.1f}s, {nps:.0f} nodes/sec]"
            )
            if result["errors"]:
                print(f"Errors: {len(result['errors'])}")

        elif args.command == "update":
            result = incremental_update(repo_root, store, base=args.base)
            print(
                f"Incremental: {result['files_updated']} files updated, "
                f"{result['total_nodes']} nodes, {result['total_edges']} edges"
            )

        elif args.command == "status":
            stats = store.get_stats()
            print(f"Nodes: {stats.total_nodes}")
            print(f"Edges: {stats.total_edges}")
            print(f"Files: {stats.files_count}")
            print(f"Languages: {', '.join(stats.languages)}")
            print(f"Last updated: {stats.last_updated or 'never'}")
            # Show branch info and warn if stale
            stored_branch = store.get_metadata("git_branch")
            stored_sha = store.get_metadata("git_head_sha")
            if stored_branch:
                print(f"Built on branch: {stored_branch}")
            if stored_sha:
                print(f"Built at commit: {stored_sha[:12]}")
            from .incremental import _git_branch_info
            current_branch, current_sha = _git_branch_info(repo_root)
            if stored_branch and current_branch and stored_branch != current_branch:
                print(
                    f"WARNING: Graph was built on '{stored_branch}' "
                    f"but you are now on '{current_branch}'. "
                    f"Run 'code-review-graph build' to rebuild."
                )

        elif args.command == "watch":
            watch(repo_root, store)

        elif args.command == "visualize":
            from .visualization import generate_html
            html_path = repo_root / ".code-review-graph" / "graph.html"
            generate_html(store, html_path)
            print(f"Visualization: {html_path}")
            if getattr(args, "serve", False):
                import functools
                import http.server

                serve_dir = html_path.parent
                port = 8765
                handler = functools.partial(
                    http.server.SimpleHTTPRequestHandler,
                    directory=str(serve_dir),
                )
                print(f"Serving at http://localhost:{port}/graph.html")
                print("Press Ctrl+C to stop.")
                with http.server.HTTPServer(("localhost", port), handler) as httpd:
                    try:
                        httpd.serve_forever()
                    except KeyboardInterrupt:
                        print("\nServer stopped.")
            else:
                print("Open in browser to explore your codebase graph.")

        elif args.command == "wiki":
            from .wiki import generate_wiki
            wiki_dir = repo_root / ".code-review-graph" / "wiki"
            result = generate_wiki(store, wiki_dir, force=args.force)
            total = result["pages_generated"] + result["pages_updated"] + result["pages_unchanged"]
            print(
                f"Wiki: {result['pages_generated']} new, "
                f"{result['pages_updated']} updated, "
                f"{result['pages_unchanged']} unchanged "
                f"({total} total pages)"
            )
            print(f"Output: {wiki_dir}")

        elif args.command == "c4":
            _cmd_c4(args)

        elif args.command == "task-report":
            from .task_report import generate_task_report
            from .tasks import get_active_root
            output_dir = (
                Path(args.output_dir)
                if getattr(args, "output_dir", None)
                else repo_root / ".code-review-graph" / "tasks"
            )
            root_task_id = getattr(args, "root_task_id", None)
            if root_task_id is None:
                active = get_active_root(store._conn)
                if active is None:
                    print("No open root task found. Create a root task first.")
                    return 1
                root_task_id = active["id"]
                print(f"Auto-detected root task: '{active['title']}' ({root_task_id})")
            result = generate_task_report(
                store._conn,
                root_task_id,
                output_dir,
                force=getattr(args, "force", False),
            )
            if result.get("skipped"):
                print(f"Task report unchanged: {result['output_path']}")
            else:
                print(
                    f"Task report: {result['tasks_rendered']} tasks → "
                    f"{result['output_path']}"
                )

        elif args.command == "detect-changes":
            from .changes import analyze_changes
            from .incremental import get_changed_files, get_staged_and_unstaged

            base = args.base
            changed = get_changed_files(repo_root, base)
            if not changed:
                changed = get_staged_and_unstaged(repo_root)

            if not changed:
                print("No changes detected.")
            else:
                result = analyze_changes(
                    store,
                    changed,
                    repo_root=str(repo_root),
                    base=base,
                )
                if args.brief:
                    print(result.get("summary", "No summary available."))
                else:
                    print(json.dumps(result, indent=2, default=str))

    finally:
        store.close()
