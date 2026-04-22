"""Tests for the schema migration framework."""

import sqlite3
import tempfile
from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.migrations import (
    LATEST_VERSION,
    MIGRATIONS,
    _table_exists,
    get_schema_version,
    run_migrations,
)


class TestMigrations:
    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.store = GraphStore(self.tmp.name)

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def test_fresh_db_gets_latest_version(self):
        """A newly created DB should be at the latest schema version."""
        version = get_schema_version(self.store._conn)
        assert version == LATEST_VERSION

    def test_v1_db_migrates_to_latest(self):
        """A v1 database should migrate to latest when GraphStore is opened."""
        # Close the store that was already migrated
        self.store.close()

        # Manually create a v1 database (base schema only, version=1)
        conn = sqlite3.connect(str(self.tmp.name))
        conn.execute(
            "INSERT OR REPLACE INTO metadata (key, value) VALUES ('schema_version', '1')"
        )
        conn.commit()
        # Drop migration artifacts to simulate v1
        conn.execute("DROP TABLE IF EXISTS flows")
        conn.execute("DROP TABLE IF EXISTS flow_memberships")
        conn.execute("DROP TABLE IF EXISTS communities")
        conn.execute("DROP TABLE IF EXISTS nodes_fts")
        conn.commit()
        conn.close()

        # Re-open with GraphStore — should trigger migrations
        self.store = GraphStore(self.tmp.name)
        assert get_schema_version(self.store._conn) == LATEST_VERSION

    def test_migration_is_idempotent(self):
        """Opening GraphStore twice should leave schema at latest version."""
        self.store.close()
        self.store = GraphStore(self.tmp.name)
        assert get_schema_version(self.store._conn) == LATEST_VERSION

        self.store.close()
        self.store = GraphStore(self.tmp.name)
        assert get_schema_version(self.store._conn) == LATEST_VERSION

    def test_signature_column_exists_after_migration(self):
        """The nodes table should have a 'signature' column after migration."""
        cursor = self.store._conn.execute("PRAGMA table_info(nodes)")
        columns = [row[1] if isinstance(row, tuple) else row["name"] for row in cursor]
        assert "signature" in columns

    def test_flows_table_exists_after_migration(self):
        """The flows and flow_memberships tables should exist after migration."""
        tables = _get_table_names(self.store._conn)
        assert "flows" in tables
        assert "flow_memberships" in tables

    def test_communities_table_exists_after_migration(self):
        """The communities table should exist and nodes should have community_id."""
        tables = _get_table_names(self.store._conn)
        assert "communities" in tables

        cursor = self.store._conn.execute("PRAGMA table_info(nodes)")
        columns = [row[1] if isinstance(row, tuple) else row["name"] for row in cursor]
        assert "community_id" in columns

    def test_fts5_table_exists_after_migration(self):
        """The nodes_fts FTS5 virtual table should exist after migration."""
        tables = _get_table_names(self.store._conn)
        assert "nodes_fts" in tables

    def test_get_schema_version_no_metadata_table(self):
        """get_schema_version returns 0 when metadata table doesn't exist."""
        conn = sqlite3.connect(":memory:")
        assert get_schema_version(conn) == 0
        conn.close()

    def test_get_schema_version_no_key(self):
        """get_schema_version returns 1 when metadata exists but key is missing."""
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.commit()
        assert get_schema_version(conn) == 1
        conn.close()

    def test_migrations_dict_covers_all_versions(self):
        """MIGRATIONS should have entries from 2 to LATEST_VERSION."""
        expected = set(range(2, LATEST_VERSION + 1))
        assert set(MIGRATIONS.keys()) == expected

    def test_run_migrations_on_already_current_db(self):
        """run_migrations should be a no-op on an already-current database."""
        version_before = get_schema_version(self.store._conn)
        run_migrations(self.store._conn)
        version_after = get_schema_version(self.store._conn)
        assert version_before == version_after == LATEST_VERSION


def _get_table_names(conn: sqlite3.Connection) -> set[str]:
    """Helper: return all table/view names in the database."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
    ).fetchall()
    return {row[0] if isinstance(row, (tuple, list)) else row["name"] for row in rows}


def _get_index_names(conn: sqlite3.Connection) -> set[str]:
    """Helper: return all index names in the database."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index'"
    ).fetchall()
    return {row[0] if isinstance(row, (tuple, list)) else row["name"] for row in rows}


def _get_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """Helper: return column names for a table."""
    cursor = conn.execute(f"PRAGMA table_info({table})")  # noqa: S608
    return [row[1] if isinstance(row, tuple) else row["name"] for row in cursor]


class TestMigrationV6:
    """Tests specifically for the v6 Task DAG migration."""

    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.store = GraphStore(self.tmp.name)
        self.conn = self.store._conn

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    # --- Table existence ---

    def test_v6_tasks_table_exists(self):
        """tasks table must exist after v6 migration."""
        assert _table_exists(self.conn, "tasks")

    def test_v6_task_edges_table_exists(self):
        """task_edges table must exist after v6 migration."""
        assert _table_exists(self.conn, "task_edges")

    def test_v6_task_code_refs_table_exists(self):
        """task_code_refs table must exist after v6 migration."""
        assert _table_exists(self.conn, "task_code_refs")

    def test_v6_notes_table_exists(self):
        """notes table must exist after v6 migration."""
        assert _table_exists(self.conn, "notes")

    def test_v6_contracts_table_exists(self):
        """contracts table must exist after v6 migration."""
        assert _table_exists(self.conn, "contracts")

    # --- Column correctness ---

    def test_v6_tasks_columns(self):
        """tasks table must have all required columns."""
        cols = _get_columns(self.conn, "tasks")
        required = {
            "id", "parent_id", "title", "description", "status",
            "spec", "acceptance_criteria", "archive_reason",
            "created_at", "updated_at",
        }
        assert required.issubset(set(cols)), f"Missing columns: {required - set(cols)}"

    def test_v6_task_edges_columns(self):
        """task_edges table must have all required columns."""
        cols = _get_columns(self.conn, "task_edges")
        required = {"source_task_id", "target_task_id", "type", "description", "created_at"}
        assert required.issubset(set(cols)), f"Missing columns: {required - set(cols)}"

    def test_v6_task_code_refs_columns(self):
        """task_code_refs table must have all required columns."""
        cols = _get_columns(self.conn, "task_code_refs")
        required = {"task_id", "code_node_id", "ref_type", "description", "created_at"}
        assert required.issubset(set(cols)), f"Missing columns: {required - set(cols)}"

    def test_v6_notes_columns(self):
        """notes table must have all required columns."""
        cols = _get_columns(self.conn, "notes")
        required = {
            "id", "task_id", "note_type", "content", "status",
            "resolution", "rationale", "alternatives",
            "created_at", "updated_at",
        }
        assert required.issubset(set(cols)), f"Missing columns: {required - set(cols)}"

    def test_v6_contracts_columns(self):
        """contracts table must have v9 schema (no legacy provider/consumer columns)."""
        cols = set(_get_columns(self.conn, "contracts"))
        required = {
            "id", "name", "scope_task_id", "contract_type",
            "definition", "status", "code_node_id",
            "created_at", "updated_at",
        }
        assert required.issubset(cols), f"Missing columns: {required - cols}"
        # Legacy columns must be gone
        assert "provider_task_id" not in cols, "Legacy provider_task_id still present"
        assert "consumer_task_id" not in cols, "Legacy consumer_task_id still present"

    # --- Index existence ---

    def test_v6_indexes_exist(self):
        """All 11 indexes for Task DAG tables must exist after v6 migration."""
        idx = _get_index_names(self.conn)
        expected = {
            "idx_tasks_parent",
            "idx_tasks_status",
            "idx_task_edges_source",
            "idx_task_edges_target",
            "idx_task_code_refs_task",
            "idx_task_code_refs_node",
            "idx_notes_task",
            "idx_notes_type",
            "idx_notes_status",
            "idx_contracts_scope",
            "idx_contract_links_contract",
            "idx_contract_links_task",
        }
        missing = expected - idx
        assert not missing, f"Missing indexes after v6+v9: {missing}"

    # --- Idempotency of v6 specifically ---

    def test_v6_idempotent_reopen(self):
        """Closing and re-opening DB after v6 leaves schema at LATEST_VERSION."""
        self.store.close()
        self.store = GraphStore(self.tmp.name)
        self.conn = self.store._conn
        assert get_schema_version(self.conn) == LATEST_VERSION
        assert _table_exists(self.conn, "tasks")
        assert _table_exists(self.conn, "contracts")

    def test_v6_run_migrations_noop(self):
        """run_migrations on a v6 DB is a no-op and leaves tables intact."""
        run_migrations(self.conn)
        assert get_schema_version(self.conn) == LATEST_VERSION
        assert _table_exists(self.conn, "tasks")

    # --- Basic INSERT round-trip (smoke test) ---

    def test_v6_tasks_insert_and_select(self):
        """tasks table accepts INSERT and SELECT after migration."""
        import time
        now = time.time()
        self.conn.execute(
            "INSERT INTO tasks (id, title, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("t1", "Test task", "draft", now, now),
        )
        self.conn.commit()
        row = self.conn.execute("SELECT id, title FROM tasks WHERE id = ?", ("t1",)).fetchone()
        assert row is not None
        assert (row[0] if isinstance(row, tuple) else row["id"]) == "t1"

    def test_v6_notes_insert_and_select(self):
        """notes table accepts INSERT after tasks row exists."""
        import time
        now = time.time()
        self.conn.execute(
            "INSERT INTO tasks (id, title, status, created_at, updated_at) VALUES (?,?,?,?,?)",
            ("t2", "Parent task", "draft", now, now),
        )
        self.conn.execute(
            "INSERT INTO notes (id, task_id, note_type, content, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("n1", "t2", "question", "Open question?", "open", now, now),
        )
        self.conn.commit()
        row = self.conn.execute("SELECT content FROM notes WHERE id = ?", ("n1",)).fetchone()
        assert row is not None


class TestMigrationV9:
    """v9 migration: legacy provider_task_id/consumer_task_id removed from contracts."""

    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.store = GraphStore(self.tmp.name)
        self.conn = self.store._conn

    def teardown_method(self):
        self.store.close()
        try:
            Path(self.tmp.name).unlink()
        except PermissionError:
            pass

    def test_legacy_columns_absent_after_migration(self):
        """After v9, contracts must NOT have provider_task_id or consumer_task_id."""
        cols = set(_get_columns(self.conn, "contracts"))
        assert "provider_task_id" not in cols
        assert "consumer_task_id" not in cols

    def test_required_columns_present(self):
        """All v9 columns must be present."""
        cols = set(_get_columns(self.conn, "contracts"))
        for col in ("id", "name", "scope_task_id", "contract_type",
                    "definition", "status", "code_node_id",
                    "created_at", "updated_at"):
            assert col in cols, f"Missing column: {col}"

    def test_contract_links_table_exists(self):
        """contract_links many-to-many table must exist."""
        assert _table_exists(self.conn, "contract_links")
        cols = set(_get_columns(self.conn, "contract_links"))
        assert {"contract_id", "task_id", "role"}.issubset(cols)

    def test_idempotent_rerun(self):
        """Running v9 migration again on an already-migrated DB is a no-op."""
        from code_review_graph.migrations import _migrate_v9
        _migrate_v9(self.conn)  # should not raise
        cols = set(_get_columns(self.conn, "contracts"))
        assert "provider_task_id" not in cols

    def test_data_preserved_after_migration(self):
        """Existing contract data must survive the table recreation."""
        import time
        now = time.time()
        # Insert prerequisite tasks
        for tid, title in [("t1", "Provider"), ("t2", "Consumer")]:
            self.conn.execute(
                "INSERT INTO tasks (id, title, status, created_at, updated_at) "
                "VALUES (?,?,?,?,?)", (tid, title, "draft", now, now)
            )
        # Insert contract with new schema
        self.conn.execute(
            "INSERT INTO contracts (id, name, contract_type, definition, "
            "status, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            ("c1", "IFoo", "interface", "{ bar(): void }", "proposed", now, now),
        )
        self.conn.execute(
            "INSERT INTO contract_links (contract_id, task_id, role) VALUES (?,?,?)",
            ("c1", "t1", "provider"),
        )
        self.conn.commit()

        # Re-run migration (idempotent)
        from code_review_graph.migrations import _migrate_v9
        _migrate_v9(self.conn)

        row = self.conn.execute(
            "SELECT name, definition FROM contracts WHERE id = ?", ("c1",)
        ).fetchone()
        assert row is not None
        name = row[0] if isinstance(row, tuple) else row["name"]
        assert name == "IFoo"
