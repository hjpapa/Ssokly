"""SQLite schema creation, upgrade backup and validation for CaptureStore."""
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4


METADATA_SCHEMA_VERSION = 2


class CaptureSchemaMixin:
    def _initialize_schema(self) -> None:
        with self._connection() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > METADATA_SCHEMA_VERSION:
                raise RuntimeError(
                    f"capture metadata version {version} is newer than supported "
                    f"version {METADATA_SCHEMA_VERSION}"
                )
            if version == 0:
                existing = connection.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'capture_items'"
                ).fetchone()
                if existing is not None:
                    raise RuntimeError(
                        "capture metadata has an unversioned incompatible schema"
                    )
                connection.executescript(
                    """
                    CREATE TABLE capture_items (
                        id TEXT PRIMARY KEY,
                        storage_name TEXT NOT NULL UNIQUE,
                        source TEXT NOT NULL,
                        width INTEGER NOT NULL CHECK (width > 0),
                        height INTEGER NOT NULL CHECK (height > 0),
                        byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
                        content_sha256 TEXT NOT NULL,
                        ocr_status TEXT NOT NULL DEFAULT 'unread'
                            CHECK (ocr_status IN ('unread', 'ready', 'failed')),
                        ocr_text TEXT NOT NULL DEFAULT '',
                        ocr_error TEXT NOT NULL DEFAULT '',
                        ocr_profile TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        trashed_at TEXT,
                        verified_text TEXT NOT NULL DEFAULT '',
                        verified_at TEXT,
                        verified_ocr_sha256 TEXT NOT NULL DEFAULT ''
                    );

                    CREATE TABLE capture_links (
                        capture_id TEXT NOT NULL
                            REFERENCES capture_items(id) ON DELETE CASCADE,
                        task_id TEXT NOT NULL,
                        linked_at TEXT NOT NULL,
                        PRIMARY KEY (capture_id, task_id)
                    );

                    CREATE TABLE legacy_imports (
                        legacy_key TEXT PRIMARY KEY,
                        capture_id TEXT NOT NULL
                            REFERENCES capture_items(id) ON DELETE CASCADE
                    );

                    CREATE INDEX idx_capture_items_active_created
                    ON capture_items (trashed_at, created_at DESC);

                    CREATE INDEX idx_capture_links_task
                    ON capture_links (task_id, capture_id);

                    PRAGMA user_version = 2;
                    """
                )
            elif version == 1:
                connection.execute('BEGIN IMMEDIATE')
                # Another process can finish an upgrade before this writer lock.
                current_version = connection.execute('PRAGMA user_version').fetchone()[0]
                if current_version == METADATA_SCHEMA_VERSION:
                    self._validate_schema(connection)
                    return
                if current_version != 1:
                    raise RuntimeError('capture metadata version changed before upgrade')
                self._validate_schema(connection, version=1)
                self._backup_before_upgrade()
                connection.execute(
                    "ALTER TABLE capture_items "
                    "ADD COLUMN verified_text TEXT NOT NULL DEFAULT ''"
                )
                connection.execute(
                    "ALTER TABLE capture_items ADD COLUMN verified_at TEXT"
                )
                connection.execute(
                    "ALTER TABLE capture_items "
                    "ADD COLUMN verified_ocr_sha256 TEXT NOT NULL DEFAULT ''"
                )
                connection.execute("PRAGMA user_version = 2")
                self._validate_schema(connection)
            else:
                self._validate_schema(connection)

    def _backup_before_upgrade(self) -> Path:
        """SQLite-consistent backup, completed before any v1 ALTER statement."""
        backup = self.db_path.with_name(self.db_path.name + '.before-capture-inbox-' + uuid4().hex[:12] + '.bak')
        pending = backup.with_suffix(backup.suffix + '.pending')
        # A separate read connection works while the upgrade connection holds
        # BEGIN IMMEDIATE; backing up that write connection itself can block.
        with closing(sqlite3.connect(self.db_path)) as source, closing(sqlite3.connect(pending)) as destination:
            source.backup(destination)
        pending.replace(backup)
        return backup

    @staticmethod
    def _validate_schema(
        connection: sqlite3.Connection,
        *,
        version: int = METADATA_SCHEMA_VERSION,
    ) -> None:
        capture_columns = (
            "id",
            "storage_name",
            "source",
            "width",
            "height",
            "byte_size",
            "content_sha256",
            "ocr_status",
            "ocr_text",
            "ocr_error",
            "ocr_profile",
            "created_at",
            "updated_at",
            "trashed_at",
        )
        if version >= 2:
            capture_columns += (
                "verified_text",
                "verified_at",
                "verified_ocr_sha256",
            )
        expected = {
            "capture_items": capture_columns,
            "capture_links": ("capture_id", "task_id", "linked_at"),
            "legacy_imports": ("legacy_key", "capture_id"),
        }
        for table, expected_columns in expected.items():
            actual_columns = tuple(
                row["name"]
                for row in connection.execute(f"PRAGMA table_info({table})")
            )
            if actual_columns != expected_columns:
                raise RuntimeError(
                    f"capture metadata has an incompatible {table} table"
                )
