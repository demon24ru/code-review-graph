"""Schema migration framework for the code-review-graph SQLite database.

Manages incremental schema changes via versioned migration functions.
Each migration is idempotent (uses IF NOT EXISTS / column existence checks).
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Callable

logger = logging.getLogger(__name__)


def get_schema_version(conn: sqlite3.Connection) -> int:
    """Read the current schema version from the metadata table.

    Returns:
        int: The schema version (0 if metadata table doesn't exist, 1 if not set).
    """
    try:
        row = conn.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            return 1
        return int(row[0] if isinstance(row, (tuple, list)) else row["value"])
    except sqlite3.OperationalError:
        # metadata table doesn't exist
        return 0


def _set_schema_version(conn: sqlite3.Connection, version: int) -> None:
    """Set the schema version in the metadata table."""
    conn.execute(
        "INSERT OR REPLACE INTO metadata (key, value) VALUES ('schema_version', ?)",
        (str(version),),
    )


_KNOWN_TABLES = frozenset({
    "nodes", "edges", "metadata", "communities", "flows", "flow_memberships", "nodes_fts",
    "tasks", "task_edges", "task_code_refs", "notes", "contracts", "tasks_fts",
    "contract_links",
})


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Check if a column exists in a table."""
    if table not in _KNOWN_TABLES:
        raise ValueError(f"Unknown table: {table}")
    cursor = conn.execute(f"PRAGMA table_info({table})")  # noqa: S608
    columns = [row[1] if isinstance(row, tuple) else row["name"] for row in cursor]
    return column in columns


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """Check if a table exists."""
    if table not in _KNOWN_TABLES:
        raise ValueError(f"Unknown table: {table}")
    row = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type IN ('table', 'view') "
        "AND name = ?",
        (table,),
    ).fetchone()
    return row[0] > 0


# ---------------------------------------------------------------------------
# Migration functions
# ---------------------------------------------------------------------------


def _migrate_v2(conn: sqlite3.Connection) -> None:
    """v2: Add signature column to nodes table."""
    if not _has_column(conn, "nodes", "signature"):
        conn.execute("ALTER TABLE nodes ADD COLUMN signature TEXT")
        logger.info("Migration v2: added 'signature' column to nodes")


def _migrate_v3(conn: sqlite3.Connection) -> None:
    """v3: Create flows and flow_memberships tables."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS flows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            entry_point_id INTEGER NOT NULL,
            depth INTEGER NOT NULL,
            node_count INTEGER NOT NULL,
            file_count INTEGER NOT NULL,
            criticality REAL NOT NULL DEFAULT 0.0,
            path_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS flow_memberships (
            flow_id INTEGER NOT NULL,
            node_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            PRIMARY KEY (flow_id, node_id)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_flows_criticality ON flows(criticality DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_flows_entry ON flows(entry_point_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_flow_memberships_node ON flow_memberships(node_id)"
    )
    logger.info("Migration v3: created flows and flow_memberships tables")


def _migrate_v4(conn: sqlite3.Connection) -> None:
    """v4: Create communities table, add community_id to nodes."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS communities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            level INTEGER NOT NULL DEFAULT 0,
            parent_id INTEGER,
            cohesion REAL NOT NULL DEFAULT 0.0,
            size INTEGER NOT NULL DEFAULT 0,
            dominant_language TEXT,
            description TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    if not _has_column(conn, "nodes", "community_id"):
        conn.execute("ALTER TABLE nodes ADD COLUMN community_id INTEGER")
        logger.info("Migration v4: added 'community_id' column to nodes")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_nodes_community ON nodes(community_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_communities_parent ON communities(parent_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_communities_cohesion ON communities(cohesion DESC)"
    )
    logger.info("Migration v4: created communities table")


def _migrate_v5(conn: sqlite3.Connection) -> None:
    """v5: Create FTS5 virtual table for nodes."""
    if not _table_exists(conn, "nodes_fts"):
        conn.execute("""
            CREATE VIRTUAL TABLE nodes_fts USING fts5(
                name, qualified_name, file_path, signature,
                content='nodes', content_rowid='rowid',
                tokenize='porter unicode61'
            )
        """)
        logger.info("Migration v5: created nodes_fts FTS5 virtual table")


def _migrate_v6(conn: sqlite3.Connection) -> None:
    """v6: Create Task DAG tables (tasks, task_edges, task_code_refs, notes, contracts)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            parent_id TEXT REFERENCES tasks(id),
            title TEXT NOT NULL,
            description TEXT,
            status TEXT NOT NULL DEFAULT 'draft',
            spec TEXT,
            acceptance_criteria TEXT,
            archive_reason TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_edges (
            source_task_id TEXT NOT NULL REFERENCES tasks(id),
            target_task_id TEXT NOT NULL REFERENCES tasks(id),
            type TEXT NOT NULL,
            description TEXT,
            created_at REAL NOT NULL,
            PRIMARY KEY (source_task_id, target_task_id, type)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_code_refs (
            task_id TEXT NOT NULL REFERENCES tasks(id),
            code_node_id INTEGER NOT NULL REFERENCES nodes(id),
            ref_type TEXT NOT NULL,
            description TEXT,
            created_at REAL NOT NULL,
            PRIMARY KEY (task_id, code_node_id, ref_type)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notes (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(id),
            note_type TEXT NOT NULL,
            content TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            resolution TEXT,
            rationale TEXT,
            alternatives TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS contracts (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            scope_task_id TEXT REFERENCES tasks(id),
            contract_type TEXT NOT NULL,
            definition TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'proposed',
            code_node_id INTEGER REFERENCES nodes(id),
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS contract_links (
            contract_id TEXT NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
            task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            role TEXT NOT NULL,
            PRIMARY KEY (contract_id, task_id, role)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_edges_source ON task_edges(source_task_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_edges_target ON task_edges(target_task_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_code_refs_task ON task_code_refs(task_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_code_refs_node ON task_code_refs(code_node_id)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_notes_task ON notes(task_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_notes_type ON notes(note_type)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_notes_status ON notes(status)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_contracts_scope ON contracts(scope_task_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_contract_links_contract ON contract_links(contract_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_contract_links_task ON contract_links(task_id)"
    )
    # FTS5 virtual table for full-text search across task text fields
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS tasks_fts USING fts5(
            id UNINDEXED,
            title,
            description,
            spec,
            acceptance_criteria,
            content='tasks',
            content_rowid='rowid'
        )
    """)
    # Triggers to keep tasks_fts in sync with tasks table
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS tasks_ai AFTER INSERT ON tasks BEGIN
            INSERT INTO tasks_fts(rowid, id, title, description, spec, acceptance_criteria)
            VALUES (new.rowid, new.id, new.title, new.description, new.spec, new.acceptance_criteria);
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS tasks_ad AFTER DELETE ON tasks BEGIN
            INSERT INTO tasks_fts(tasks_fts, rowid, id, title, description, spec, acceptance_criteria)
            VALUES ('delete', old.rowid, old.id, old.title, old.description, old.spec, old.acceptance_criteria);
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS tasks_au AFTER UPDATE ON tasks BEGIN
            INSERT INTO tasks_fts(tasks_fts, rowid, id, title, description, spec, acceptance_criteria)
            VALUES ('delete', old.rowid, old.id, old.title, old.description, old.spec, old.acceptance_criteria);
            INSERT INTO tasks_fts(rowid, id, title, description, spec, acceptance_criteria)
            VALUES (new.rowid, new.id, new.title, new.description, new.spec, new.acceptance_criteria);
        END
    """)
    logger.info("Migration v6: created task DAG tables (tasks, task_edges, task_code_refs, notes, contracts, tasks_fts)")


# ---------------------------------------------------------------------------
# Migration registry
# ---------------------------------------------------------------------------

def _migrate_v7(conn: sqlite3.Connection) -> None:
    """v7 — Extend contracts to first-class design entities (many-to-many).

    Changes:
    1. Ensure contracts table has name, scope_task_id, code_node_id columns
       (recreate without legacy NOT NULL provider/consumer columns if needed)
    2. Create contract_links table (many-to-many: contract ↔ task + role)
    3. Back-fill name for existing rows
    4. Add indexes
    """
    existing_cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(contracts)").fetchall()
    }

    # If contracts table still has legacy NOT NULL provider/consumer or missing
    # new columns, recreate it.
    col_constraints = {
        row[1]: row[3]
        for row in conn.execute("PRAGMA table_info(contracts)").fetchall()
    }
    legacy_not_null = (
        col_constraints.get("provider_task_id", 0)
        or col_constraints.get("consumer_task_id", 0)
    )
    needs_new_cols = not {"name", "scope_task_id", "code_node_id"}.issubset(existing_cols)

    if needs_new_cols or legacy_not_null:
        conn.execute("ALTER TABLE contracts RENAME TO _contracts_v6")
        conn.execute("""
            CREATE TABLE contracts (
                id               TEXT PRIMARY KEY,
                name             TEXT NOT NULL DEFAULT '',
                contract_type    TEXT NOT NULL,
                definition       TEXT NOT NULL,
                status           TEXT NOT NULL DEFAULT 'proposed',
                scope_task_id    TEXT REFERENCES tasks(id),
                code_node_id     INTEGER REFERENCES nodes(id),
                created_at       REAL NOT NULL,
                updated_at       REAL NOT NULL
            )
        """)
        # Copy common columns; derive name from contract_type if absent
        name_expr = (
            "contract_type || '_' || substr(id,1,8)"
            if "name" not in existing_cols
            else "COALESCE(name, '')"
        )
        scope_expr = "NULL" if "scope_task_id" not in existing_cols else "scope_task_id"
        code_expr = "NULL" if "code_node_id" not in existing_cols else "code_node_id"
        conn.execute(f"""
            INSERT INTO contracts
                (id, name, contract_type, definition, status,
                 scope_task_id, code_node_id, created_at, updated_at)
            SELECT id, {name_expr}, contract_type, definition, status,
                   {scope_expr}, {code_expr}, created_at, updated_at
            FROM _contracts_v6
        """)  # noqa: S608

        # Back-fill contract_links from legacy provider/consumer if they existed
        if "provider_task_id" in existing_cols or "consumer_task_id" in existing_cols:
            now = __import__("time").time()
            legacy_rows = conn.execute(
                "SELECT id, provider_task_id, consumer_task_id FROM _contracts_v6"
            ).fetchall()
            for contract_id, provider_id, consumer_id in legacy_rows:
                if provider_id:
                    conn.execute(
                        "INSERT OR IGNORE INTO contract_links(contract_id, task_id, role) "
                        "VALUES (?, ?, 'provider')",
                        (contract_id, provider_id),
                    )
                if consumer_id:
                    conn.execute(
                        "INSERT OR IGNORE INTO contract_links(contract_id, task_id, role) "
                        "VALUES (?, ?, 'consumer')",
                        (contract_id, consumer_id),
                    )
        conn.execute("DROP TABLE _contracts_v6")

    # Back-fill name for any unnamed rows
    conn.execute(
        "UPDATE contracts SET name = contract_type || '_' || substr(id,1,8) "
        "WHERE name = '' OR name IS NULL"
    )

    # Ensure contract_links exists (safe on fresh DB — v6 already creates it)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS contract_links (
            contract_id  TEXT NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
            task_id      TEXT NOT NULL REFERENCES tasks(id)     ON DELETE CASCADE,
            role         TEXT NOT NULL,
            PRIMARY KEY (contract_id, task_id, role)
        )
    """)

    # Indexes
    for stmt in (
        "CREATE INDEX IF NOT EXISTS idx_contract_links_contract ON contract_links(contract_id)",
        "CREATE INDEX IF NOT EXISTS idx_contract_links_task ON contract_links(task_id)",
        "CREATE INDEX IF NOT EXISTS idx_contracts_scope ON contracts(scope_task_id)",
        "CREATE INDEX IF NOT EXISTS idx_contracts_name ON contracts(name)",
        "CREATE INDEX IF NOT EXISTS idx_contracts_code_node ON contracts(code_node_id)",
    ):
        conn.execute(stmt)

    logger.info(
        "Migration v7: contracts extended (name/scope_task_id/code_node_id) + contract_links"
    )


def _migrate_v8(conn: sqlite3.Connection) -> None:
    """v8: Relativise qualified_names — strip repo_root from absolute paths.

    Converts stored qualified_names from absolute paths (``C:\\path\\fn``) to
    POSIX-relative paths (``code_review_graph/tasks.py::fn``).

    The repo_root is inferred from the DB location stored in
    ``metadata.db_path`` (written by the build process), falling back to
    looking for a common prefix across all file_paths in nodes.  If neither
    heuristic is reliable, the migration is a no-op and a warning is logged —
    new builds will automatically write relative names going forward.

    Tables affected: ``nodes.qualified_name``, ``nodes.file_path``,
    ``edges.source_qualified``, ``edges.target_qualified``.
    """
    from pathlib import Path as _Path, PurePosixPath

    # Try to read repo_root from metadata
    row = conn.execute(
        "SELECT value FROM metadata WHERE key='repo_root'"
    ).fetchone()
    repo_root_str: str | None = row[0] if row else None

    # Fallback: infer from common ancestor of all absolute file_paths
    if repo_root_str is None:
        fp_rows = conn.execute(
            "SELECT DISTINCT file_path FROM nodes WHERE file_path LIKE '/%' "
            "OR file_path LIKE '_:/%' OR file_path LIKE '_:\\\\%' "
            "LIMIT 500"
        ).fetchall()
        abs_paths = [r[0] for r in fp_rows if r[0]]
        if abs_paths:
            try:
                from os.path import commonpath
                repo_root_str = commonpath(abs_paths)
            except ValueError:
                pass

    if not repo_root_str:
        logger.warning(
            "v8 migration: could not determine repo_root — "
            "qualified_names remain absolute. "
            "Rebuild the graph to get portable names."
        )
        return

    repo_root = _Path(repo_root_str).resolve()

    def _to_relative(abs_path: str) -> str:
        """Convert absolute path to POSIX-relative, fallback to normalised."""
        try:
            return _Path(abs_path).resolve().relative_to(repo_root).as_posix()
        except ValueError:
            return _Path(abs_path).as_posix()

    def _relativise_qname(qname: str) -> str:
        """Relativise the file-path portion of a qualified_name."""
        if "::" in qname:
            file_part, rest = qname.split("::", 1)
            return f"{_to_relative(file_part)}::{rest}"
        # File nodes — the whole thing is the path
        return _to_relative(qname)

    # --- nodes: qualified_name, file_path, and name for File nodes ---
    node_rows = conn.execute(
        "SELECT id, kind, name, qualified_name, file_path FROM nodes"
    ).fetchall()
    for nid, kind, name, qname, fpath in node_rows:
        new_qname = _relativise_qname(qname)
        new_fpath = _to_relative(fpath) if fpath else fpath
        # For File nodes the name IS the path — normalise it too
        new_name = new_fpath if kind == "File" else name
        if new_qname != qname or new_fpath != fpath or new_name != name:
            conn.execute(
                "UPDATE nodes SET name=?, qualified_name=?, file_path=?, updated_at=? WHERE id=?",
                (new_name, new_qname, new_fpath, __import__('time').time(), nid),
            )

    # --- edges: source_qualified, target_qualified, and file_path ---
    edge_rows = conn.execute(
        "SELECT id, source_qualified, target_qualified, file_path FROM edges"
    ).fetchall()
    for eid, src, tgt, fpath in edge_rows:
        new_src = _relativise_qname(src) if src else src
        new_tgt = _relativise_qname(tgt) if tgt else tgt
        new_fpath = _to_relative(fpath) if fpath else fpath
        if new_src != src or new_tgt != tgt or new_fpath != fpath:
            conn.execute(
                "UPDATE edges SET source_qualified=?, target_qualified=?, "
                "file_path=? WHERE id=?",
                (new_src, new_tgt, new_fpath, eid),
            )

    # Rebuild FTS index after bulk qualified_name changes
    try:
        conn.execute("INSERT INTO nodes_fts(nodes_fts) VALUES('rebuild')")
    except Exception:
        pass  # FTS may not exist on older DBs

    logger.info(
        "v8 migration: relativised qualified_names relative to %s", repo_root
    )


def _migrate_v10(conn: sqlite3.Connection) -> None:
    """v10: Add UNIQUE(entry_point_id, path_json) to flows table.

    Prevents duplicate flow rows that can accumulate when incremental rebuilds
    race or when store_flows is called concurrently.  Any existing duplicate
    rows are deduplicated first (keeping the row with the lowest id per group).
    The flows and flow_memberships tables are recreated via the standard
    rename-create-copy-drop pattern because SQLite does not support ADD CONSTRAINT.
    """
    # Check whether the constraint already exists by inspecting the CREATE TABLE sql.
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='flows'"
    ).fetchone()
    existing_sql: str = (row[0] if row else "") or ""
    if "UNIQUE" in existing_sql.upper():
        logger.info("v10 migration: UNIQUE constraint already present on flows, skipping")
        return

    # De-duplicate: remove rows with a higher id when (entry_point_id, path_json) repeats.
    conn.execute("""
        DELETE FROM flow_memberships
        WHERE flow_id IN (
            SELECT id FROM flows
            WHERE id NOT IN (
                SELECT MIN(id)
                FROM flows
                GROUP BY entry_point_id, path_json
            )
        )
    """)
    conn.execute("""
        DELETE FROM flows
        WHERE id NOT IN (
            SELECT MIN(id)
            FROM flows
            GROUP BY entry_point_id, path_json
        )
    """)

    # Recreate flows with UNIQUE constraint (SQLite rename-create-copy-drop).
    conn.execute("ALTER TABLE flows RENAME TO _flows_v9")
    conn.execute("""
        CREATE TABLE flows (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            name           TEXT NOT NULL,
            entry_point_id INTEGER NOT NULL,
            depth          INTEGER NOT NULL,
            node_count     INTEGER NOT NULL,
            file_count     INTEGER NOT NULL,
            criticality    REAL NOT NULL DEFAULT 0.0,
            path_json      TEXT NOT NULL,
            created_at     TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at     TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(entry_point_id, path_json)
        )
    """)
    conn.execute("""
        INSERT INTO flows
            (id, name, entry_point_id, depth, node_count, file_count,
             criticality, path_json, created_at, updated_at)
        SELECT id, name, entry_point_id, depth, node_count, file_count,
               criticality, path_json, created_at, updated_at
        FROM _flows_v9
    """)
    conn.execute("DROP TABLE _flows_v9")

    # Recreate indexes on the new table.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_flows_criticality ON flows(criticality DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_flows_entry ON flows(entry_point_id)"
    )
    logger.info(
        "v10 migration: added UNIQUE(entry_point_id, path_json) to flows "
        "and removed duplicates"
    )


def _migrate_v9(conn: sqlite3.Connection) -> None:
    """v9: Drop legacy provider_task_id / consumer_task_id columns from contracts.

    These columns were kept as nullable stubs in v7 for backward compatibility
    during the migration to many-to-many contract_links.  All code now reads
    participants exclusively from contract_links, so the legacy columns are safe
    to remove.

    SQLite does not support DROP COLUMN before 3.35.0, so we recreate the table
    via the standard rename-create-copy-drop pattern which works on all versions.
    """
    # Check if legacy columns still exist
    cols = {row[1] for row in conn.execute("PRAGMA table_info(contracts)").fetchall()}
    if "provider_task_id" not in cols and "consumer_task_id" not in cols:
        logger.info("v9 migration: legacy columns already absent, nothing to do")
        return

    conn.executescript("""
        PRAGMA foreign_keys = OFF;

        ALTER TABLE contracts RENAME TO _contracts_v8;

        CREATE TABLE contracts (
            id              TEXT PRIMARY KEY,
            name            TEXT NOT NULL DEFAULT '',
            scope_task_id   TEXT REFERENCES tasks(id),
            contract_type   TEXT NOT NULL,
            definition      TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'proposed',
            code_node_id    INTEGER REFERENCES nodes(id),
            created_at      REAL NOT NULL,
            updated_at      REAL NOT NULL
        );

        INSERT INTO contracts
            (id, name, scope_task_id, contract_type, definition,
             status, code_node_id, created_at, updated_at)
        SELECT
            id,
            COALESCE(name, ''),
            scope_task_id,
            contract_type,
            definition,
            status,
            code_node_id,
            created_at,
            updated_at
        FROM _contracts_v8;

        DROP TABLE _contracts_v8;

        CREATE INDEX IF NOT EXISTS idx_contracts_scope
            ON contracts(scope_task_id);

        PRAGMA foreign_keys = ON;
    """)
    logger.info("v9 migration: dropped legacy provider_task_id/consumer_task_id from contracts")


MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    2: _migrate_v2,
    3: _migrate_v3,
    4: _migrate_v4,
    5: _migrate_v5,
    6: _migrate_v6,
    7: _migrate_v7,
    8: _migrate_v8,
    9: _migrate_v9,
    10: _migrate_v10,
}

LATEST_VERSION = max(MIGRATIONS.keys())


def run_migrations(conn: sqlite3.Connection) -> None:
    """Run all pending migrations in order.

    Each migration runs in its own transaction. The schema_version metadata
    entry is updated after each successful migration.
    """
    current = get_schema_version(conn)
    if current >= LATEST_VERSION:
        return

    logger.info("Schema version %d -> %d: running migrations", current, LATEST_VERSION)

    for version in sorted(MIGRATIONS.keys()):
        if version <= current:
            continue
        logger.info("Running migration v%d", version)
        try:
            MIGRATIONS[version](conn)
            _set_schema_version(conn, version)
            conn.commit()
        except Exception:
            conn.rollback()
            logger.error("Migration v%d failed, rolling back", version, exc_info=True)
            raise

    logger.info("Migrations complete, now at schema version %d", LATEST_VERSION)
