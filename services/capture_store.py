from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
from typing import Iterable, Iterator, Literal, Optional, Union
from uuid import UUID, uuid4, uuid5

from PIL import Image


MAX_STORED_CAPTURES = 8
PNG_CAPTURE_COMPRESS_LEVEL = 3
APP_DIRECTORY_NAME = "Ssokly"
CAPTURE_INBOX_DIRECTORY_NAME = "capture_inbox"
METADATA_DATABASE_FILENAME = "capture_inbox.sqlite3"
METADATA_SCHEMA_VERSION = 2
LEGACY_IMPORT_NAMESPACE = UUID("4431e221-6dc9-4fc8-8efb-f61b26972d39")

OCRStatus = Literal["unread", "ready", "failed"]
LinkFilter = Literal["unclassified", "linked", "all"]
CaptureIdentifier = Union[str, Iterable[str]]


def _default_store_dir() -> Path:
    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    if local_app_data:
        root = Path(local_app_data).expanduser() / APP_DIRECTORY_NAME
    else:
        root = Path.home() / ".ssokly"
    return (root / CAPTURE_INBOX_DIRECTORY_NAME).resolve(strict=False)


def _legacy_store_dir() -> Path:
    return (
        Path(tempfile.gettempdir()) / APP_DIRECTORY_NAME / "captures"
    ).resolve(strict=False)


# Kept as public constants for callers that previously imported DEFAULT_STORE_DIR.
# CaptureStore resolves the default at construction time so tests and portable
# launches can still change LOCALAPPDATA before creating the store.
DEFAULT_STORE_DIR = _default_store_dir()
LEGACY_STORE_DIR = _legacy_store_dir()


@dataclass(frozen=True)
class CaptureRecord:
    # path and created_at stay first so existing positional construction remains
    # source compatible.
    path: Path
    created_at: datetime
    id: str = ""
    source: str = "capture"
    width: int = 0
    height: int = 0
    byte_size: int = 0
    content_sha256: str = ""
    ocr_status: OCRStatus = "unread"
    ocr_text: str = ""
    ocr_error: str = ""
    ocr_profile: str = ""
    verified_text: str = ""
    verified_at: Optional[datetime] = None
    verified_ocr_sha256: str = ""
    linked_task_ids: tuple[str, ...] = ()
    trashed_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @property
    def label(self) -> str:
        created_at = (
            self.created_at.astimezone()
            if self.created_at.tzinfo is not None
            else self.created_at
        )
        return f"{created_at:%m/%d %H:%M}  {self.source or 'capture'}"

    @property
    def ocr_preview(self) -> str:
        for line in self.effective_text.splitlines():
            preview = line.strip()
            if preview:
                return preview[:120]
        return ""

    @property
    def effective_text(self) -> str:
        """Return the teacher-verified text when one exists, otherwise raw OCR."""
        return self.verified_text if self.verified_at is not None else self.ocr_text

    @property
    def is_verified(self) -> bool:
        return self.verified_at is not None

    @property
    def review_status(self) -> str:
        if not self.is_verified:
            return "unverified"
        if self.verified_ocr_sha256 == _text_sha256(self.ocr_text):
            return "verified"
        return "ocr_updated"

    @property
    def is_linked(self) -> bool:
        return bool(self.linked_task_ids)


class CaptureStoreError(RuntimeError):
    """Base error for capture inbox operations."""


class CaptureNotFoundError(CaptureStoreError):
    """Raised when a requested capture no longer exists."""


class CaptureConflictError(CaptureStoreError):
    """Raised when another app instance changed a capture first."""


class CaptureLinkedError(CaptureStoreError):
    """Raised when a linked capture is requested for trash or purge."""


class CaptureStore:
    """Persistent, lossless capture inbox with SQLite sidecar metadata.

    The legacy save/list_recent/load/delete surface is intentionally retained.
    ``delete`` now performs a recoverable soft delete; ``purge`` is the only
    operation that removes an app-owned image file.
    """

    def __init__(
        self,
        directory: Optional[Path] = None,
        max_items: int = MAX_STORED_CAPTURES,
    ) -> None:
        self._uses_default_directory = directory is None
        self.directory = (
            _default_store_dir()
            if directory is None
            else Path(directory).expanduser().resolve(strict=False)
        )
        # Retained for the current UI counter. It no longer causes pruning.
        self.max_items = max(5, max_items)
        self.db_path = self.directory / METADATA_DATABASE_FILENAME
        self.directory.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()
        if self._uses_default_directory:
            self._import_legacy_captures()

    def save(self, image: Image.Image, source: str = "capture") -> CaptureRecord:
        if not isinstance(image, Image.Image):
            raise TypeError("image must be a PIL Image")

        created_at = _utc_now()
        capture_id = str(uuid4())
        normalized_source = _normalized_source(source)
        safe_source = _safe_filename_component(normalized_source)
        filename = (
            f"{created_at:%Y%m%d_%H%M%S_%f}_{capture_id}_{safe_source}.png"
        )
        path = self._owned_path(filename)
        temporary_path = self._owned_path(f".{filename}.{uuid4().hex}.pending")

        prepared_image = (
            image
            if image.mode in {"1", "L", "LA", "P", "RGB", "RGBA", "I", "I;16"}
            else image.convert("RGB")
        )
        file_promoted = False
        try:
            prepared_image.save(
                temporary_path,
                format="PNG",
                compress_level=PNG_CAPTURE_COMPRESS_LEVEL,
            )
            width, height = _verified_image_size(temporary_path)
            byte_size, content_sha256 = _file_metadata(temporary_path)
            temporary_path.replace(path)
            file_promoted = True

            now_text = _datetime_to_text(created_at)
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO capture_items (
                        id, storage_name, source, width, height, byte_size,
                        content_sha256, ocr_status, ocr_text, ocr_error,
                        ocr_profile, created_at, updated_at, trashed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'unread', '', '', '', ?, ?, NULL)
                    """,
                    (
                        capture_id,
                        path.name,
                        normalized_source,
                        width,
                        height,
                        byte_size,
                        content_sha256,
                        now_text,
                        now_text,
                    ),
                )
                row = self._select_capture_row(connection, capture_id)
                record = self._record_from_row(connection, row)
        except Exception:
            if file_promoted:
                path.unlink(missing_ok=True)
            raise
        finally:
            temporary_path.unlink(missing_ok=True)

        return record

    def list_recent(self) -> list[CaptureRecord]:
        return self.search(link_filter="all", trashed=False)

    def get(self, capture_id: str) -> Optional[CaptureRecord]:
        normalized_id = _required_identifier(capture_id, "capture_id")
        with self._connection() as connection:
            row = self._select_capture_row(connection, normalized_id)
            return self._record_from_row(connection, row) if row is not None else None

    def search(
        self,
        query: str = "",
        link_filter: LinkFilter = "all",
        *,
        trashed: bool = False,
        limit: Optional[int] = None,
    ) -> list[CaptureRecord]:
        if not isinstance(query, str):
            raise TypeError("query must be a string")
        if link_filter not in {"unclassified", "linked", "all"}:
            raise ValueError(
                "link_filter must be 'unclassified', 'linked', or 'all'"
            )
        if not isinstance(trashed, bool):
            raise TypeError("trashed must be a bool")
        if limit is not None and (
            not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0
        ):
            raise ValueError("limit must be a positive integer or None")

        clauses = ["c.trashed_at IS NOT NULL" if trashed else "c.trashed_at IS NULL"]
        parameters: list[object] = []
        normalized_query = query.strip()
        if normalized_query:
            pattern = f"%{_escape_like(normalized_query)}%"
            clauses.append(
                "(c.source LIKE ? ESCAPE '\\' COLLATE NOCASE "
                "OR c.storage_name LIKE ? ESCAPE '\\' COLLATE NOCASE "
                "OR c.ocr_text LIKE ? ESCAPE '\\' COLLATE NOCASE "
                "OR c.verified_text LIKE ? ESCAPE '\\' COLLATE NOCASE)"
            )
            parameters.extend([pattern, pattern, pattern, pattern])

        if link_filter == "unclassified":
            clauses.append(
                "NOT EXISTS (SELECT 1 FROM capture_links l "
                "WHERE l.capture_id = c.id)"
            )
        elif link_filter == "linked":
            clauses.append(
                "EXISTS (SELECT 1 FROM capture_links l "
                "WHERE l.capture_id = c.id)"
            )

        sql = (
            "SELECT c.* FROM capture_items c WHERE "
            + " AND ".join(clauses)
            + " ORDER BY c.created_at DESC, c.id DESC"
        )
        if limit is not None:
            sql += " LIMIT ?"
            parameters.append(limit)
        with self._connection() as connection:
            rows = connection.execute(sql, parameters).fetchall()
            return [self._record_from_row(connection, row) for row in rows]

    def count(
        self,
        link_filter: LinkFilter = "all",
        *,
        trashed: bool = False,
    ) -> int:
        if link_filter not in {"unclassified", "linked", "all"}:
            raise ValueError(
                "link_filter must be 'unclassified', 'linked', or 'all'"
            )
        if not isinstance(trashed, bool):
            raise TypeError("trashed must be a bool")
        clauses = ["c.trashed_at IS NOT NULL" if trashed else "c.trashed_at IS NULL"]
        if link_filter == "unclassified":
            clauses.append(
                "NOT EXISTS (SELECT 1 FROM capture_links l WHERE l.capture_id = c.id)"
            )
        elif link_filter == "linked":
            clauses.append(
                "EXISTS (SELECT 1 FROM capture_links l WHERE l.capture_id = c.id)"
            )
        with self._connection() as connection:
            value = connection.execute(
                "SELECT COUNT(*) FROM capture_items c WHERE " + " AND ".join(clauses)
            ).fetchone()[0]
        return int(value)

    def load(self, record: CaptureRecord) -> Image.Image:
        path = self._path_for_record(record)
        with Image.open(path) as image:
            return image.copy()

    def delete(self, record: CaptureRecord) -> None:
        capture_id = self._capture_id_for(record)
        self.trash(capture_id)

    def update_ocr(
        self,
        capture_id: str,
        text: str,
        *,
        profile: str = "",
    ) -> CaptureRecord:
        normalized_id = _required_identifier(capture_id, "capture_id")
        normalized_text = _required_string(text, "text")
        normalized_profile = _required_string(profile, "profile")
        now_text = _datetime_to_text(_utc_now())
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE capture_items
                SET ocr_status = 'ready', ocr_text = ?, ocr_error = '',
                    ocr_profile = ?, updated_at = ?
                WHERE id = ?
                """,
                (normalized_text, normalized_profile, now_text, normalized_id),
            )
            if cursor.rowcount == 0:
                raise CaptureNotFoundError(
                    f"capture does not exist: {normalized_id}"
                )
            row = self._select_capture_row(connection, normalized_id)
            return self._record_from_row(connection, row)

    def set_ocr_failure(
        self,
        capture_id: str,
        error: str,
        *,
        profile: str = "",
    ) -> CaptureRecord:
        normalized_id = _required_identifier(capture_id, "capture_id")
        normalized_error = _required_string(error, "error")
        normalized_profile = _required_string(profile, "profile")
        now_text = _datetime_to_text(_utc_now())
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE capture_items
                SET ocr_status = 'failed', ocr_error = ?, ocr_profile = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (normalized_error, normalized_profile, now_text, normalized_id),
            )
            if cursor.rowcount == 0:
                raise CaptureNotFoundError(
                    f"capture does not exist: {normalized_id}"
                )
            row = self._select_capture_row(connection, normalized_id)
            return self._record_from_row(connection, row)

    def save_verified_text(
        self,
        capture_id: str,
        text: str,
        *,
        expected_updated_at: Optional[datetime] = None,
    ) -> CaptureRecord:
        """Persist the teacher-reviewed transcription without altering raw OCR."""
        normalized_id = _required_identifier(capture_id, "capture_id")
        verified_text = _required_string(text, "text")
        expected_text = (
            _validated_datetime_text(expected_updated_at)
            if expected_updated_at is not None
            else None
        )
        verified_at = _utc_now()
        now_text = _datetime_to_text(verified_at)
        with self._connection() as connection:
            row = self._select_capture_row(connection, normalized_id)
            if row is None:
                raise CaptureNotFoundError(
                    f"capture does not exist: {normalized_id}"
                )
            if expected_text is not None and row["updated_at"] != expected_text:
                raise CaptureConflictError(
                    "다른 Ssokly 창에서 이 캡처가 수정되었습니다. "
                    "검수 중인 내용을 복사한 뒤 캡처를 다시 열어 주세요."
                )
            where_clause = "id = ?"
            parameters: list[object] = [
                verified_text,
                now_text,
                _text_sha256(row["ocr_text"]),
                now_text,
                normalized_id,
            ]
            if expected_text is not None:
                where_clause += " AND updated_at = ?"
                parameters.append(expected_text)
            cursor = connection.execute(
                f"""
                UPDATE capture_items
                SET verified_text = ?, verified_at = ?,
                    verified_ocr_sha256 = ?, updated_at = ?
                WHERE {where_clause}
                """,
                parameters,
            )
            if cursor.rowcount == 0:
                if expected_text is not None:
                    raise CaptureConflictError(
                        "다른 Ssokly 창에서 이 캡처가 수정되었습니다. "
                        "검수 중인 내용을 복사한 뒤 캡처를 다시 열어 주세요."
                    )
                raise CaptureNotFoundError(f"capture does not exist: {normalized_id}")
            row = self._select_capture_row(connection, normalized_id)
            return self._record_from_row(connection, row)

    def clear_verified_text(
        self,
        capture_id: str,
        *,
        expected_updated_at: Optional[datetime] = None,
    ) -> CaptureRecord:
        """Return a capture to its raw OCR text while keeping that OCR intact."""
        normalized_id = _required_identifier(capture_id, "capture_id")
        expected_text = (
            _validated_datetime_text(expected_updated_at)
            if expected_updated_at is not None
            else None
        )
        now_text = _datetime_to_text(_utc_now())
        with self._connection() as connection:
            where_clause = "id = ?"
            parameters: list[object] = [now_text, normalized_id]
            if expected_text is not None:
                where_clause += " AND updated_at = ?"
                parameters.append(expected_text)
            cursor = connection.execute(
                f"""
                UPDATE capture_items
                SET verified_text = '', verified_at = NULL,
                    verified_ocr_sha256 = '', updated_at = ?
                WHERE {where_clause}
                """,
                parameters,
            )
            if cursor.rowcount == 0:
                exists = self._select_capture_row(connection, normalized_id)
                if exists is not None and expected_text is not None:
                    raise CaptureConflictError(
                        "다른 Ssokly 창에서 이 캡처가 수정되었습니다. "
                        "캡처를 다시 연 뒤 검수본을 해제해 주세요."
                    )
                raise CaptureNotFoundError(
                    f"capture does not exist: {normalized_id}"
                )
            row = self._select_capture_row(connection, normalized_id)
            return self._record_from_row(connection, row)

    def link_to_task(
        self,
        capture_ids: CaptureIdentifier,
        task_id: str,
    ) -> list[CaptureRecord]:
        normalized_ids = _capture_ids(capture_ids)
        normalized_task_id = _required_identifier(task_id, "task_id")
        with self._connection() as connection:
            rows = self._required_capture_rows(connection, normalized_ids)
            trashed_ids = [row["id"] for row in rows if row["trashed_at"]]
            if trashed_ids:
                raise CaptureStoreError(
                    "trashed captures cannot be linked: " + ", ".join(trashed_ids)
                )
            latest_link = connection.execute(
                "SELECT MAX(linked_at) FROM capture_links WHERE task_id = ?",
                (normalized_task_id,),
            ).fetchone()[0]
            next_linked_at = _utc_now()
            if latest_link:
                next_linked_at = max(
                    next_linked_at,
                    _datetime_from_text(latest_link) + timedelta(microseconds=1),
                )
            for capture_id in normalized_ids:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO capture_links (
                        capture_id, task_id, linked_at
                    ) VALUES (?, ?, ?)
                    """,
                    (
                        capture_id,
                        normalized_task_id,
                        _datetime_to_text(next_linked_at),
                    ),
                )
                if cursor.rowcount:
                    next_linked_at += timedelta(microseconds=1)
            return self._records_in_id_order(connection, normalized_ids)

    def captures_for_task(self, task_id: str) -> list[CaptureRecord]:
        normalized_task_id = _required_identifier(task_id, "task_id")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT c.*
                FROM capture_links l
                JOIN capture_items c ON c.id = l.capture_id
                WHERE l.task_id = ? AND c.trashed_at IS NULL
                ORDER BY l.linked_at, l.capture_id
                """,
                (normalized_task_id,),
            ).fetchall()
            return [self._record_from_row(connection, row) for row in rows]

    def unlink_task(
        self,
        capture_ids: CaptureIdentifier,
        task_id: str,
    ) -> list[CaptureRecord]:
        normalized_ids = _capture_ids(capture_ids)
        normalized_task_id = _required_identifier(task_id, "task_id")
        with self._connection() as connection:
            self._required_capture_rows(connection, normalized_ids)
            placeholders = ", ".join("?" for _ in normalized_ids)
            connection.execute(
                f"DELETE FROM capture_links WHERE task_id = ? "
                f"AND capture_id IN ({placeholders})",
                [normalized_task_id, *normalized_ids],
            )
            return self._records_in_id_order(connection, normalized_ids)

    def trash(self, capture_ids: CaptureIdentifier) -> list[CaptureRecord]:
        normalized_ids = _capture_ids(capture_ids)
        now_text = _datetime_to_text(_utc_now())
        with self._connection() as connection:
            self._required_capture_rows(connection, normalized_ids)
            linked_ids = self._linked_ids(connection, normalized_ids)
            if linked_ids:
                raise CaptureLinkedError(
                    "linked captures must be unlinked before trashing: "
                    + ", ".join(linked_ids)
                )
            placeholders = ", ".join("?" for _ in normalized_ids)
            connection.execute(
                f"UPDATE capture_items SET trashed_at = COALESCE(trashed_at, ?), "
                f"updated_at = ? WHERE id IN ({placeholders})",
                [now_text, now_text, *normalized_ids],
            )
            return self._records_in_id_order(connection, normalized_ids)

    def restore(self, capture_ids: CaptureIdentifier) -> list[CaptureRecord]:
        normalized_ids = _capture_ids(capture_ids)
        now_text = _datetime_to_text(_utc_now())
        with self._connection() as connection:
            self._required_capture_rows(connection, normalized_ids)
            placeholders = ", ".join("?" for _ in normalized_ids)
            connection.execute(
                f"UPDATE capture_items SET trashed_at = NULL, updated_at = ? "
                f"WHERE id IN ({placeholders})",
                [now_text, *normalized_ids],
            )
            return self._records_in_id_order(connection, normalized_ids)

    def purge(self, capture_ids: CaptureIdentifier) -> int:
        normalized_ids = _capture_ids(capture_ids)
        staged_files: list[tuple[Path, Path]] = []
        try:
            with self._connection() as connection:
                rows = self._required_capture_rows(connection, normalized_ids)
                linked_ids = self._linked_ids(connection, normalized_ids)
                if linked_ids:
                    raise CaptureLinkedError(
                        "linked captures cannot be purged: "
                        + ", ".join(linked_ids)
                    )
                active_ids = [row["id"] for row in rows if not row["trashed_at"]]
                if active_ids:
                    raise CaptureStoreError(
                        "captures must be trashed before purging: "
                        + ", ".join(active_ids)
                    )

                for row in rows:
                    original = self._owned_path(row["storage_name"])
                    if not original.exists():
                        continue
                    staged = self._owned_path(
                        f".{original.name}.{uuid4().hex}.deleting"
                    )
                    original.replace(staged)
                    staged_files.append((original, staged))

                placeholders = ", ".join("?" for _ in normalized_ids)
                cursor = connection.execute(
                    f"DELETE FROM capture_items WHERE id IN ({placeholders})",
                    normalized_ids,
                )
                deleted_count = cursor.rowcount
        except Exception:
            self._restore_staged_files(staged_files)
            raise

        # The metadata deletion is committed. A failed unlink leaves only a
        # hidden app-owned orphan, never an external source file.
        for _original, staged in staged_files:
            try:
                staged.unlink(missing_ok=True)
            except OSError:
                pass
        return deleted_count

    def storage_bytes(self, *, trashed: Optional[bool] = None) -> int:
        if trashed is not None and not isinstance(trashed, bool):
            raise TypeError("trashed must be a bool or None")
        if trashed is None:
            where_clause = ""
        elif trashed:
            where_clause = " WHERE trashed_at IS NOT NULL"
        else:
            where_clause = " WHERE trashed_at IS NULL"
        with self._connection() as connection:
            value = connection.execute(
                "SELECT COALESCE(SUM(byte_size), 0) FROM capture_items"
                + where_clause
            ).fetchone()[0]
        return int(value)

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
                self._validate_schema(connection, version=1)
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

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path), timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _import_legacy_captures(self) -> None:
        legacy_directory = _legacy_store_dir()
        if legacy_directory == self.directory or not legacy_directory.is_dir():
            return
        try:
            legacy_paths = sorted(
                legacy_directory.glob("*.png"),
                key=lambda path: path.stat().st_mtime_ns,
            )
        except OSError:
            return
        for legacy_path in legacy_paths:
            try:
                self._import_legacy_capture(legacy_path)
            except (OSError, sqlite3.Error, ValueError, RuntimeError):
                # One damaged or locked old temporary file must not stop the app
                # or prevent other captures from being recovered.
                continue

    def _import_legacy_capture(self, legacy_path: Path) -> None:
        resolved_legacy = legacy_path.expanduser().resolve(strict=True)
        stat = resolved_legacy.stat()
        legacy_key = f"{resolved_legacy}|{stat.st_size}|{stat.st_mtime_ns}"
        with self._connection() as connection:
            imported = connection.execute(
                "SELECT capture_id FROM legacy_imports WHERE legacy_key = ?",
                (legacy_key,),
            ).fetchone()
            if imported is not None:
                return

        capture_id = str(uuid5(LEGACY_IMPORT_NAMESPACE, legacy_key))
        filename = f"legacy_{capture_id}.png"
        destination = self._owned_path(filename)
        temporary_path = self._owned_path(
            f".{filename}.{uuid4().hex}.importing"
        )
        destination_created = False
        try:
            if not destination.exists():
                shutil.copy2(resolved_legacy, temporary_path)
                width, height = _verified_image_size(temporary_path)
                byte_size, content_sha256 = _file_metadata(temporary_path)
                temporary_path.replace(destination)
                destination_created = True
            else:
                width, height = _verified_image_size(destination)
                byte_size, content_sha256 = _file_metadata(destination)

            created_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            now_text = _datetime_to_text(_utc_now())
            source = _legacy_source(resolved_legacy)
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO capture_items (
                        id, storage_name, source, width, height, byte_size,
                        content_sha256, ocr_status, ocr_text, ocr_error,
                        ocr_profile, created_at, updated_at, trashed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'unread', '', '', '', ?, ?, NULL)
                    """,
                    (
                        capture_id,
                        destination.name,
                        source,
                        width,
                        height,
                        byte_size,
                        content_sha256,
                        _datetime_to_text(created_at),
                        now_text,
                    ),
                )
                row = self._select_capture_row(connection, capture_id)
                if row is None:
                    raise RuntimeError("legacy capture metadata could not be created")
                connection.execute(
                    "INSERT OR IGNORE INTO legacy_imports (legacy_key, capture_id) "
                    "VALUES (?, ?)",
                    (legacy_key, capture_id),
                )
        except Exception:
            if destination_created:
                destination.unlink(missing_ok=True)
            raise
        finally:
            temporary_path.unlink(missing_ok=True)

    def _capture_id_for(self, record: CaptureRecord) -> str:
        if record.id:
            return record.id
        path = self._path_for_record(record)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT id FROM capture_items WHERE storage_name = ?",
                (path.name,),
            ).fetchone()
        if row is None:
            raise CaptureNotFoundError(f"capture does not exist: {path.name}")
        return row["id"]

    def _path_for_record(self, record: CaptureRecord) -> Path:
        if not isinstance(record, CaptureRecord):
            raise TypeError("record must be a CaptureRecord")
        path = record.path.expanduser().resolve(strict=False)
        owned_path = self._owned_path(path.name)
        if path != owned_path:
            raise ValueError("capture path is outside the app-owned inbox")
        return owned_path

    def _owned_path(self, storage_name: str) -> Path:
        if not isinstance(storage_name, str) or not storage_name:
            raise ValueError("storage_name must be a non-empty filename")
        if Path(storage_name).name != storage_name or storage_name in {".", ".."}:
            raise ValueError("storage_name must not contain a directory")
        root = self.directory.resolve(strict=False)
        candidate = (root / storage_name).resolve(strict=False)
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError("capture path is outside the app-owned inbox") from exc
        return candidate

    def _select_capture_row(
        self,
        connection: sqlite3.Connection,
        capture_id: str,
    ) -> Optional[sqlite3.Row]:
        return connection.execute(
            "SELECT * FROM capture_items WHERE id = ?",
            (capture_id,),
        ).fetchone()

    def _record_from_row(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> CaptureRecord:
        linked_task_ids = tuple(
            link["task_id"]
            for link in connection.execute(
                "SELECT task_id FROM capture_links WHERE capture_id = ? "
                "ORDER BY linked_at, task_id",
                (row["id"],),
            ).fetchall()
        )
        return CaptureRecord(
            id=row["id"],
            path=self._owned_path(row["storage_name"]),
            created_at=_datetime_from_text(row["created_at"]),
            source=row["source"],
            width=row["width"],
            height=row["height"],
            byte_size=row["byte_size"],
            content_sha256=row["content_sha256"],
            ocr_status=row["ocr_status"],
            ocr_text=row["ocr_text"],
            ocr_error=row["ocr_error"],
            ocr_profile=row["ocr_profile"],
            verified_text=row["verified_text"],
            verified_at=(
                _datetime_from_text(row["verified_at"])
                if row["verified_at"]
                else None
            ),
            verified_ocr_sha256=row["verified_ocr_sha256"],
            linked_task_ids=linked_task_ids,
            trashed_at=(
                _datetime_from_text(row["trashed_at"])
                if row["trashed_at"]
                else None
            ),
            updated_at=_datetime_from_text(row["updated_at"]),
        )

    def _required_capture_rows(
        self,
        connection: sqlite3.Connection,
        capture_ids: list[str],
    ) -> list[sqlite3.Row]:
        placeholders = ", ".join("?" for _ in capture_ids)
        rows = connection.execute(
            f"SELECT * FROM capture_items WHERE id IN ({placeholders})",
            capture_ids,
        ).fetchall()
        rows_by_id = {row["id"]: row for row in rows}
        missing = [capture_id for capture_id in capture_ids if capture_id not in rows_by_id]
        if missing:
            raise CaptureNotFoundError(
                "captures do not exist: " + ", ".join(missing)
            )
        return [rows_by_id[capture_id] for capture_id in capture_ids]

    def _records_in_id_order(
        self,
        connection: sqlite3.Connection,
        capture_ids: list[str],
    ) -> list[CaptureRecord]:
        rows = self._required_capture_rows(connection, capture_ids)
        return [self._record_from_row(connection, row) for row in rows]

    @staticmethod
    def _linked_ids(
        connection: sqlite3.Connection,
        capture_ids: list[str],
    ) -> list[str]:
        placeholders = ", ".join("?" for _ in capture_ids)
        return [
            row["capture_id"]
            for row in connection.execute(
                f"SELECT DISTINCT capture_id FROM capture_links "
                f"WHERE capture_id IN ({placeholders}) ORDER BY capture_id",
                capture_ids,
            ).fetchall()
        ]

    @staticmethod
    def _restore_staged_files(staged_files: list[tuple[Path, Path]]) -> None:
        restore_error: Optional[OSError] = None
        for original, staged in reversed(staged_files):
            if not staged.exists():
                continue
            try:
                staged.replace(original)
            except OSError as exc:
                restore_error = restore_error or exc
        if restore_error is not None:
            raise restore_error

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


def labels_for(records: Iterable[CaptureRecord]) -> list[str]:
    return [record.label for record in records]


def _capture_ids(value: CaptureIdentifier) -> list[str]:
    values = [value] if isinstance(value, str) else list(value)
    if not values:
        raise ValueError("at least one capture_id is required")
    normalized: list[str] = []
    seen: set[str] = set()
    for item in values:
        capture_id = _required_identifier(item, "capture_id")
        if capture_id not in seen:
            normalized.append(capture_id)
            seen.add(capture_id)
    return normalized


def _required_identifier(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} must not be empty")
    return normalized


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    return value


def _normalized_source(value: object) -> str:
    source = _required_string(value, "source").strip()
    return source or "capture"


def _safe_filename_component(source: str) -> str:
    safe_source = re.sub(r"[^0-9A-Za-z가-힣_-]+", "_", source).strip("_")
    return safe_source[:32] or "capture"


def _legacy_source(path: Path) -> str:
    parts = path.stem.split("_", 3)
    return parts[3] if len(parts) == 4 and parts[3] else "capture"


def _verified_image_size(path: Path) -> tuple[int, int]:
    with Image.open(path) as image:
        image.verify()
    with Image.open(path) as image:
        width, height = image.size
    if width <= 0 or height <= 0:
        raise ValueError("capture image has invalid dimensions")
    return width, height


def _file_metadata(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    byte_size = 0
    with path.open("rb") as source:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            byte_size += len(chunk)
            digest.update(chunk)
    return byte_size, digest.hexdigest()


def _text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _datetime_to_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _datetime_from_text(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _validated_datetime_text(value: datetime) -> str:
    if not isinstance(value, datetime):
        raise TypeError("expected_updated_at must be a datetime")
    return _datetime_to_text(value)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
