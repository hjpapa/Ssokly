from dataclasses import dataclass
from datetime import datetime, timezone
from contextlib import contextmanager
import os
from pathlib import Path
import re
import shutil
import sqlite3
from typing import Any, Iterator, Literal, Optional, Union
from uuid import uuid4


SCHEMA_VERSION = 1
APP_DIRECTORY_NAME = "Ssokly"
DATABASE_FILENAME = "ssokly.db"
CAPTURES_DIRECTORY_NAME = "captures"
TASK_COLUMNS = (
    "id",
    "title",
    "status",
    "source_kind",
    "source_name",
    "source_path",
    "source_text",
    "analysis_text",
    "output_mode",
    "capture_path",
    "created_at",
    "updated_at",
    "completed_at",
)

TaskStatus = Literal["open", "completed"]
SourceKind = Literal["capture", "file", "manual"]

VALID_STATUSES = frozenset({"open", "completed"})
VALID_SOURCE_KINDS = frozenset({"capture", "file", "manual"})
UPDATABLE_FIELDS = frozenset(
    {
        "title",
        "source_name",
        "source_path",
        "source_text",
        "analysis_text",
        "output_mode",
    }
)

PathLike = Union[str, os.PathLike]


@dataclass(frozen=True)
class TaskRecord:
    id: str
    title: str
    status: TaskStatus
    source_kind: SourceKind
    source_name: str
    source_path: Optional[str]
    source_text: str
    analysis_text: str
    output_mode: str
    capture_path: Optional[str]
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime]


class TaskConflictError(RuntimeError):
    """Raised when another app instance updated a task after it was loaded."""


class TaskStore:
    """SQLite-backed storage for saved Ssokly tasks."""

    def __init__(
        self,
        db_path: Optional[PathLike] = None,
        app_data_dir: Optional[PathLike] = None,
    ) -> None:
        if app_data_dir is not None:
            resolved_app_data_dir = Path(app_data_dir).expanduser()
        elif db_path is not None:
            resolved_app_data_dir = Path(db_path).expanduser().parent
        else:
            resolved_app_data_dir = _default_app_data_dir()

        self.app_data_dir = resolved_app_data_dir.resolve(strict=False)
        self.db_path = (
            Path(db_path).expanduser().resolve(strict=False)
            if db_path is not None
            else self.app_data_dir / DATABASE_FILENAME
        )
        self.captures_dir = self.app_data_dir / CAPTURES_DIRECTORY_NAME

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.captures_dir.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()

    def create(
        self,
        *,
        title: str,
        status: TaskStatus = "open",
        source_kind: SourceKind = "manual",
        source_name: str = "",
        source_path: Optional[PathLike] = None,
        source_text: str = "",
        analysis_text: str = "",
        output_mode: str = "통합 실행안",
        capture_path: Optional[PathLike] = None,
    ) -> TaskRecord:
        normalized_title = _required_text(title, "title")
        normalized_status = _validated_status(status)
        normalized_source_kind = _validated_source_kind(source_kind)
        normalized_output_mode = _required_text(output_mode, "output_mode")
        normalized_source_path = _optional_path_text(source_path)

        if capture_path is not None and normalized_source_kind != "capture":
            raise ValueError("capture_path can only be used with source_kind='capture'")

        task_id = str(uuid4())
        now = _utc_now()
        now_text = _datetime_to_text(now)
        completed_at_text = now_text if normalized_status == "completed" else None
        owned_capture_path: Optional[Path] = None

        try:
            if capture_path is not None:
                owned_capture_path = self._copy_capture(task_id, capture_path)

            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO tasks (
                        id,
                        title,
                        status,
                        source_kind,
                        source_name,
                        source_path,
                        source_text,
                        analysis_text,
                        output_mode,
                        capture_path,
                        created_at,
                        updated_at,
                        completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        normalized_title,
                        normalized_status,
                        normalized_source_kind,
                        _text(source_name, "source_name"),
                        normalized_source_path,
                        _text(source_text, "source_text"),
                        _text(analysis_text, "analysis_text"),
                        normalized_output_mode,
                        owned_capture_path.name if owned_capture_path else None,
                        now_text,
                        now_text,
                        completed_at_text,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM tasks WHERE id = ?",
                    (task_id,),
                ).fetchone()
        except Exception:
            if owned_capture_path is not None:
                owned_capture_path.unlink(missing_ok=True)
            raise

        return self._record_from_row(row)

    def get(self, task_id: str) -> Optional[TaskRecord]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        return self._record_from_row(row) if row is not None else None

    def search(
        self,
        query: str = "",
        status: Optional[TaskStatus] = None,
    ) -> list[TaskRecord]:
        if not isinstance(query, str):
            raise TypeError("query must be a string")
        if status is not None:
            _validated_status(status)

        clauses: list[str] = []
        parameters: list[str] = []
        normalized_query = query.strip()
        if normalized_query:
            pattern = f"%{_escape_like(normalized_query)}%"
            clauses.append(
                "(" + " OR ".join(
                    f"{column} LIKE ? ESCAPE '\\' COLLATE NOCASE"
                    for column in (
                        "title",
                        "source_name",
                        "source_text",
                        "analysis_text",
                    )
                ) + ")"
            )
            parameters.extend([pattern] * 4)

        if status is not None:
            clauses.append("status = ?")
            parameters.append(status)

        where_clause = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            "SELECT * FROM tasks"
            f"{where_clause}"
            " ORDER BY updated_at DESC, created_at DESC, id DESC"
        )
        with self._connection() as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return [self._record_from_row(row) for row in rows]

    def update(
        self,
        task_id: str,
        *,
        expected_updated_at: Optional[datetime] = None,
        **changes: Any,
    ) -> Optional[TaskRecord]:
        unknown_fields = set(changes) - UPDATABLE_FIELDS
        if unknown_fields:
            fields = ", ".join(sorted(unknown_fields))
            raise ValueError(f"fields cannot be updated directly: {fields}")

        if not changes:
            return self.get(task_id)

        normalized_changes: dict[str, Optional[str]] = {}
        for field, value in changes.items():
            if field == "title":
                normalized_changes[field] = _required_text(value, field)
            elif field == "output_mode":
                normalized_changes[field] = _required_text(value, field)
            elif field == "source_path":
                normalized_changes[field] = _optional_path_text(value)
            else:
                normalized_changes[field] = _text(value, field)

        normalized_changes["updated_at"] = _datetime_to_text(_utc_now())
        assignments = ", ".join(f"{field} = ?" for field in normalized_changes)
        where_clause = "id = ?"
        parameters = list(normalized_changes.values()) + [task_id]
        if expected_updated_at is not None:
            where_clause += " AND updated_at = ?"
            parameters.append(_validated_datetime_text(expected_updated_at))

        with self._connection() as connection:
            cursor = connection.execute(
                f"UPDATE tasks SET {assignments} WHERE {where_clause}",
                parameters,
            )
            if cursor.rowcount == 0:
                exists = connection.execute(
                    "SELECT 1 FROM tasks WHERE id = ?",
                    (task_id,),
                ).fetchone()
                if exists is None:
                    return None
                if expected_updated_at is not None:
                    raise TaskConflictError(
                        "다른 Ssokly 창에서 이 업무가 수정되었습니다. "
                        "현재 내용을 복사한 뒤 업무를 다시 열어 주세요."
                    )
                return None
            row = connection.execute(
                "SELECT * FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        return self._record_from_row(row)

    def set_status(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        expected_updated_at: Optional[datetime] = None,
    ) -> Optional[TaskRecord]:
        normalized_status = _validated_status(status)
        expected_text = (
            _validated_datetime_text(expected_updated_at)
            if expected_updated_at is not None
            else None
        )

        with self._connection() as connection:
            existing = connection.execute(
                "SELECT * FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if existing is None:
                return None
            if expected_text is not None and existing["updated_at"] != expected_text:
                raise TaskConflictError(
                    "다른 Ssokly 창에서 이 업무가 수정되었습니다. "
                    "업무를 다시 연 뒤 상태를 변경해 주세요."
                )
            if existing["status"] == normalized_status:
                return self._record_from_row(existing)

            now_text = _datetime_to_text(_utc_now())
            completed_at_text = now_text if normalized_status == "completed" else None
            where_clause = "id = ?"
            parameters: list[Optional[str]] = [
                normalized_status,
                now_text,
                completed_at_text,
                task_id,
            ]
            if expected_text is not None:
                where_clause += " AND updated_at = ?"
                parameters.append(expected_text)
            cursor = connection.execute(
                "UPDATE tasks "
                "SET status = ?, updated_at = ?, completed_at = ? "
                f"WHERE {where_clause}",
                parameters,
            )
            if cursor.rowcount == 0:
                raise TaskConflictError(
                    "다른 Ssokly 창에서 이 업무가 수정되었습니다. "
                    "업무를 다시 연 뒤 상태를 변경해 주세요."
                )
            row = connection.execute(
                "SELECT * FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        return self._record_from_row(row)

    def delete(self, task_id: str) -> bool:
        original_capture: Optional[Path] = None
        staged_capture: Optional[Path] = None
        try:
            with self._connection() as connection:
                row = connection.execute(
                    "SELECT capture_path FROM tasks WHERE id = ?",
                    (task_id,),
                ).fetchone()
                if row is None:
                    return False

                capture_path = row["capture_path"]
                if capture_path:
                    original_capture = self._capture_path_from_storage(capture_path)
                    if original_capture is not None and original_capture.exists():
                        staged_capture = original_capture.with_name(
                            f".{original_capture.name}.{uuid4().hex}.deleting"
                        )
                        original_capture.replace(staged_capture)

                cursor = connection.execute(
                    "DELETE FROM tasks WHERE id = ?",
                    (task_id,),
                )
                if cursor.rowcount == 0:
                    raise RuntimeError("task disappeared during deletion")
        except Exception:
            if (
                staged_capture is not None
                and original_capture is not None
                and staged_capture.exists()
            ):
                staged_capture.replace(original_capture)
            raise

        if staged_capture is not None:
            try:
                staged_capture.unlink(missing_ok=True)
            except OSError:
                # The database deletion is already committed. Leaving a hidden
                # orphan is safer than claiming that the task still exists.
                pass
        return True

    def _initialize_schema(self) -> None:
        with self._connection() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"task database schema version {version} is newer than supported "
                    f"version {SCHEMA_VERSION}"
                )
            if version == 0:
                existing_table = connection.execute(
                    """
                    SELECT 1
                    FROM sqlite_master
                    WHERE type = 'table' AND name = 'tasks'
                    """
                ).fetchone()
                if existing_table is not None:
                    self._validate_tasks_schema(connection)
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS tasks (
                        id TEXT PRIMARY KEY,
                        title TEXT NOT NULL,
                        status TEXT NOT NULL CHECK (status IN ('open', 'completed')),
                        source_kind TEXT NOT NULL
                            CHECK (source_kind IN ('capture', 'file', 'manual')),
                        source_name TEXT NOT NULL,
                        source_path TEXT,
                        source_text TEXT NOT NULL,
                        analysis_text TEXT NOT NULL,
                        output_mode TEXT NOT NULL,
                        capture_path TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        completed_at TEXT
                    );

                    CREATE INDEX IF NOT EXISTS idx_tasks_status_updated_at
                    ON tasks (status, updated_at DESC);

                    PRAGMA user_version = 1;
                    """
                )
            elif version == SCHEMA_VERSION:
                self._validate_tasks_schema(connection)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path), timeout=5.0)
        connection.row_factory = sqlite3.Row
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

    def _copy_capture(self, task_id: str, source_path: PathLike) -> Path:
        source = Path(source_path).expanduser().resolve(strict=True)
        if not source.is_file():
            raise ValueError("capture_path must point to a file")

        suffix = re.sub(r"[^0-9A-Za-z.]", "", source.suffix)[:16] or ".png"
        destination = (self.captures_dir / f"{task_id}{suffix.lower()}").resolve(
            strict=False
        )
        shutil.copy2(source, destination)
        return destination

    def _owned_capture_path(self, capture_path: Path) -> Optional[Path]:
        root = self.captures_dir.resolve(strict=False)
        candidate = capture_path.expanduser().resolve(strict=False)
        try:
            candidate.relative_to(root)
        except ValueError:
            return None
        return candidate

    def _capture_path_from_storage(self, value: str) -> Optional[Path]:
        filename = Path(value).name
        if not filename or filename in {".", ".."}:
            return None
        return self._owned_capture_path(self.captures_dir / filename)

    def _record_from_row(self, row: sqlite3.Row) -> TaskRecord:
        completed_at = row["completed_at"]
        stored_capture_path = row["capture_path"]
        owned_capture_path = (
            self._capture_path_from_storage(stored_capture_path)
            if stored_capture_path
            else None
        )
        return TaskRecord(
            id=row["id"],
            title=row["title"],
            status=row["status"],
            source_kind=row["source_kind"],
            source_name=row["source_name"],
            source_path=row["source_path"],
            source_text=row["source_text"],
            analysis_text=row["analysis_text"],
            output_mode=row["output_mode"],
            capture_path=str(owned_capture_path) if owned_capture_path else None,
            created_at=_datetime_from_text(row["created_at"]),
            updated_at=_datetime_from_text(row["updated_at"]),
            completed_at=(
                _datetime_from_text(completed_at) if completed_at else None
            ),
        )

    @staticmethod
    def _validate_tasks_schema(connection: sqlite3.Connection) -> None:
        table = connection.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = 'tasks'
            """
        ).fetchone()
        if table is None:
            raise RuntimeError("task database is missing the tasks table")
        columns = tuple(
            row["name"] for row in connection.execute("PRAGMA table_info(tasks)")
        )
        if columns != TASK_COLUMNS:
            raise RuntimeError(
                "task database has an incompatible tasks table and was not upgraded"
            )


def _default_app_data_dir() -> Path:
    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    if local_app_data:
        return (Path(local_app_data).expanduser() / APP_DIRECTORY_NAME).resolve(
            strict=False
        )
    return (Path.home() / ".ssokly").resolve(strict=False)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _datetime_to_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _datetime_from_text(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _validated_status(value: str) -> TaskStatus:
    if value not in VALID_STATUSES:
        raise ValueError("status must be 'open' or 'completed'")
    return value  # type: ignore[return-value]


def _validated_source_kind(value: str) -> SourceKind:
    if value not in VALID_SOURCE_KINDS:
        raise ValueError("source_kind must be 'capture', 'file', or 'manual'")
    return value  # type: ignore[return-value]


def _required_text(value: Any, field: str) -> str:
    normalized = _text(value, field).strip()
    if not normalized:
        raise ValueError(f"{field} must not be empty")
    return normalized


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    return value


def _optional_path_text(value: Optional[PathLike]) -> Optional[str]:
    if value is None:
        return None
    return str(Path(value).expanduser().resolve(strict=False))


def _validated_datetime_text(value: datetime) -> str:
    if not isinstance(value, datetime):
        raise TypeError("expected_updated_at must be a datetime")
    return _datetime_to_text(value)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
