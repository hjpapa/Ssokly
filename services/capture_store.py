"""Capture image storage and metadata. Record model/helpers live in capture_records,
schema creation/upgrade in capture_schema; both are re-exported here for existing callers."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import tempfile
from typing import Iterator, Optional
from uuid import UUID, uuid4, uuid5

from PIL import Image

from services.app_paths import default_app_data_dir
from services.capture_paths import owned_capture_path
from services.capture_records import (  # noqa: F401 - OCRStatus is re-exported
    CaptureConflictError, CaptureIdentifier, CaptureLinkedError, CaptureNotFoundError, CaptureRecord,
    CaptureStoreError, LinkFilter, OCRStatus,
)
# Helpers are imported by name so tests can patch services.capture_store._file_metadata.
from services.capture_records import (
    _capture_ids, _datetime_from_text, _datetime_to_text, _escape_like, _file_metadata, _legacy_source,
    _next_datetime_text, _normalized_source, _required_identifier, _required_string,
    _safe_filename_component, _text_sha256, _utc_now, _validated_datetime_text, _verified_image_size,
)
from services.capture_schema import METADATA_SCHEMA_VERSION, CaptureSchemaMixin  # noqa: F401 - re-exported
from services.diagnostics import log_failure


MAX_STORED_CAPTURES = 8
PNG_CAPTURE_COMPRESS_LEVEL = 3
APP_DIRECTORY_NAME = "Ssokly"
CAPTURE_INBOX_DIRECTORY_NAME = "capture_inbox"
METADATA_DATABASE_FILENAME = "capture_inbox.sqlite3"
LEGACY_IMPORT_NAMESPACE = UUID("4431e221-6dc9-4fc8-8efb-f61b26972d39")
_UUID_FILENAME = r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}'
_CAPTURE_FILENAME = re.compile(r'(?P<stamp>\d{8}_\d{6}_\d{6})_(?P<id>' + _UUID_FILENAME + r')_(?P<source>[0-9A-Za-z가-힣_-]{1,32})\.png')
_LEGACY_FILENAME = re.compile(r'legacy_(?P<id>' + _UUID_FILENAME + r')\.png')


def _default_store_dir() -> Path:
    return default_app_data_dir() / CAPTURE_INBOX_DIRECTORY_NAME


def _legacy_store_dir() -> Path:
    return (
        Path(tempfile.gettempdir()) / APP_DIRECTORY_NAME / "captures"
    ).resolve(strict=False)


# Kept as public constants for callers that previously imported DEFAULT_STORE_DIR.
# CaptureStore resolves the default at construction time so tests and portable
# launches can still change LOCALAPPDATA before creating the store.
DEFAULT_STORE_DIR = _default_store_dir()
LEGACY_STORE_DIR = _legacy_store_dir()


class CaptureStore(CaptureSchemaMixin):
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
        self.recover_orphans()
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
        try:
            prepared_image.save(
                temporary_path,
                format="PNG",
                compress_level=PNG_CAPTURE_COMPRESS_LEVEL,
            )
            width, height = _verified_image_size(temporary_path)
            byte_size, content_sha256 = _file_metadata(temporary_path)
            temporary_path.replace(path)

            now_text = _datetime_to_text(created_at)
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO capture_items (
                        id, storage_name, source, width, height, byte_size,
                        content_sha256, ocr_status, ocr_text, ocr_error,
                        ocr_profile, created_at, updated_at, trashed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'unread', '', '', '', ?, ?, NULL)
                    ON CONFLICT(storage_name) DO NOTHING
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
                if (row is None or row['storage_name'] != filename
                        or row['content_sha256'] != content_sha256):
                    raise CaptureStoreError('capture metadata does not match the saved image')
                # Another process may recover the promoted PNG before this
                # insert. Retain its OCR/review/link state, only restoring the
                # original display source which the filename may abbreviate.
                if row['source'] != normalized_source:
                    connection.execute('UPDATE capture_items SET source=? WHERE id=?',
                                       (normalized_source, capture_id))
                    row = self._select_capture_row(connection, capture_id)
                record = self._record_from_row(connection, row)
        finally:
            # Once promoted, the PNG is the recoverable original even when the
            # metadata transaction/commit fails. Never delete it as rollback.
            temporary_path.unlink(missing_ok=True)

        return record

    def recover_orphans(self) -> int:
        """Recover verified app-named PNGs in this inbox, without following links.

        Existing rows (including trashed captures) are never modified. Failed
        files remain in place for a later retry; arbitrary PNG names, pending
        writes, subdirectories and files outside this directory are not imports.
        """
        try:
            with self._connection() as connection:
                known = {row[0] for row in connection.execute('SELECT storage_name FROM capture_items')}
            candidates = list(self.directory.iterdir())
        except (OSError, sqlite3.Error) as error:
            log_failure('capture_store.recover_orphans', error)
            return 0
        recovered = 0
        for candidate in candidates:
            if candidate.name in known:
                continue
            match = _CAPTURE_FILENAME.fullmatch(candidate.name)
            legacy = _LEGACY_FILENAME.fullmatch(candidate.name) if match is None else None
            if match is None and legacy is None:
                continue
            try:
                if candidate.is_symlink() or (hasattr(candidate, 'is_junction') and candidate.is_junction()):
                    continue
                before = candidate.lstat()
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                    continue
                owned = self._owned_path(candidate.name)
                if owned != candidate or owned.parent != self.directory.resolve(strict=True):
                    continue
                capture_id = str(UUID((match or legacy)['id']))
                created_at = (datetime.strptime(match['stamp'], '%Y%m%d_%H%M%S_%f').replace(tzinfo=timezone.utc)
                              if match else datetime.fromtimestamp(before.st_mtime, tz=timezone.utc))
                source = match['source'] if match else '복구된 캡처'
                with Image.open(owned) as image:
                    if image.format != 'PNG':
                        continue
                    width, height = image.size
                    if width <= 0 or height <= 0 or (Image.MAX_IMAGE_PIXELS and width * height > Image.MAX_IMAGE_PIXELS):
                        continue
                    image.verify()
                with Image.open(owned) as image:
                    image.load()
                byte_size, digest = _file_metadata(owned)
                after = candidate.lstat()
                identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
                if (identity(before) != identity(after) or not stat.S_ISREG(after.st_mode)
                        or after.st_nlink != 1 or candidate.is_symlink() or self._owned_path(candidate.name) != owned):
                    continue
                with self._connection() as connection:
                    connection.execute('BEGIN IMMEDIATE')
                    # Recheck under the writer lock: a normal save/recovery may
                    # have committed after the initial directory snapshot.
                    cursor = connection.execute(
                        """INSERT OR IGNORE INTO capture_items
                        (id,storage_name,source,width,height,byte_size,content_sha256,
                         ocr_status,ocr_text,ocr_error,ocr_profile,created_at,updated_at,trashed_at)
                        VALUES (?,?,?,?,?,?,?,'unread','','','',?,?,NULL)""",
                        (capture_id, owned.name, source, width, height, byte_size, digest,
                         _datetime_to_text(created_at), _datetime_to_text(_utc_now())))
                    inserted = cursor.rowcount
                recovered += inserted
            except (OSError, sqlite3.Error, ValueError, RuntimeError, SyntaxError, EOFError, Image.DecompressionBombError) as error:
                log_failure('capture_store.recover_orphans', error)
                # Recovery must not hide healthy entries because one orphan is
                # damaged/locked. No source path or content is logged here.
                continue
        return recovered

    def list_recent(self) -> list[CaptureRecord]:
        return self.search(link_filter="all", trashed=False)

    def get(self, capture_id: str) -> Optional[CaptureRecord]:
        normalized_id = _required_identifier(capture_id, "capture_id")
        with self._connection() as connection:
            row = self._select_capture_row(connection, normalized_id)
            return self._record_from_row(connection, row) if row is not None else None

    def safe_get(self, capture_id: str) -> Optional[dict]:
        """Read text/identity even when one capture path needs local recovery."""
        normalized_id = _required_identifier(capture_id, "capture_id")
        with self._connection() as connection:
            row = self._select_capture_row(connection, normalized_id)
            return self._safe_record_from_row(connection, row) if row is not None else None

    def search(
        self,
        query: str = "",
        link_filter: LinkFilter = "all",
        *,
        trashed: bool = False,
        limit: Optional[int] = None,
    ) -> list[CaptureRecord]:
        return self._search(query, link_filter, trashed=trashed, limit=limit)

    def safe_search(
        self,
        query: str = "",
        link_filter: LinkFilter = "all",
        *,
        trashed: bool = False,
        limit: Optional[int] = None,
        task_id: Optional[str] = None,
    ) -> list[dict]:
        """Isolate per-row path failures; unsafe rows expose no usable path.

        SQLite and whole-store failures still raise. This is not a relaxed
        path API and never bypasses the strict read/write entry points.
        """
        return self._search(query, link_filter, trashed=trashed, limit=limit,
                            task_id=task_id, safe=True)

    def _search(self, query, link_filter, *, trashed, limit, task_id=None, safe=False):
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
        if task_id is not None:
            task_id = _required_identifier(task_id, "task_id")

        # Per-task views must not rescan every PNG once for each legacy task.
        # Whole-inbox search (and construction) performs recovery once.
        if task_id is None:
            self.recover_orphans()

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

        if task_id is not None:
            clauses.append('EXISTS (SELECT 1 FROM capture_links l WHERE l.capture_id=c.id AND l.task_id=?)')
            parameters.append(task_id)

        sql = (
            "SELECT c.* FROM capture_items c WHERE "
            + " AND ".join(clauses)
        )
        if task_id is not None:
            sql += ' ORDER BY (SELECT l.linked_at FROM capture_links l WHERE l.capture_id=c.id AND l.task_id=?), c.id'
            parameters.append(task_id)
        else:
            sql += ' ORDER BY c.created_at DESC, c.id DESC'
        if limit is not None:
            sql += " LIMIT ?"
            parameters.append(limit)
        with self._connection() as connection:
            rows = connection.execute(sql, parameters).fetchall()
            reader = self._safe_record_from_row if safe else self._record_from_row
            return [reader(connection, row) for row in rows]

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
        with self._connection() as connection:
            existing = self._select_capture_row(connection, normalized_id)
            if existing is None:
                raise CaptureNotFoundError(
                    f"capture does not exist: {normalized_id}"
                )
            now_text = _next_datetime_text(existing["updated_at"])
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
        with self._connection() as connection:
            existing = self._select_capture_row(connection, normalized_id)
            if existing is None:
                raise CaptureNotFoundError(
                    f"capture does not exist: {normalized_id}"
                )
            now_text = _next_datetime_text(existing["updated_at"])
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
            now_text = _next_datetime_text(row["updated_at"])
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
        with self._connection() as connection:
            row = self._select_capture_row(connection, normalized_id)
            if row is None:
                raise CaptureNotFoundError(
                    f"capture does not exist: {normalized_id}"
                )
            now_text = _next_datetime_text(row["updated_at"])
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
        except Exception as error:
            log_failure('capture_store.purge', error)
            self._restore_staged_files(staged_files)
            raise

        # The metadata deletion is committed. A failed unlink leaves only a
        # hidden app-owned orphan, never an external source file.
        for _original, staged in staged_files:
            try:
                staged.unlink(missing_ok=True)
            except OSError as error:
                log_failure('capture_store.purge', error)
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
        except OSError as error:
            log_failure('capture_store._import_legacy_captures', error)
            return
        for legacy_path in legacy_paths:
            try:
                self._import_legacy_capture(legacy_path)
            except (OSError, sqlite3.Error, ValueError, RuntimeError) as error:
                log_failure('capture_store._import_legacy_captures', error)
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
        except Exception as error:
            log_failure('capture_store._import_legacy_capture', error)
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
        path = record.path.expanduser().absolute()
        owned_path = self._owned_path(path.name)
        # Public records now retain the logical app-owned path. Older records
        # can contain the OS-resolved alias, but only the exact validated alias
        # of this entry is accepted; arbitrary equivalent paths are not.
        if path != owned_path and path != owned_path.resolve(strict=False):
            raise ValueError("capture path is outside the app-owned inbox")
        return owned_path

    def _owned_path(self, storage_name: str) -> Path:
        return owned_capture_path(self.directory, storage_name)

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
                "ORDER BY task_id",
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

    def _safe_record_from_row(self, connection: sqlite3.Connection, row: sqlite3.Row) -> dict:
        try:
            record = self._record_from_row(connection, row)
            if not record.path.is_file():
                record = None
        except (ValueError, OSError) as error:
            log_failure('capture_store._safe_record_from_row', error)
            record = None
        text_fields = ('id', 'source', 'ocr_text', 'verified_text', 'created_at', 'updated_at')
        result = {name: row[name] if isinstance(row[name], str) else '' for name in text_fields}
        result.update({name: row[name] if isinstance(row[name], str) else None
                       for name in ('verified_at', 'trashed_at')})
        result.update(record=record, recovery_required=record is None,
                      warning='원본 이미지 경로를 확인하지 못했습니다. 저장된 텍스트는 보존됩니다.' if record is None else '')
        return result

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


