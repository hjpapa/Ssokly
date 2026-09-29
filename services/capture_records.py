"""Capture record model, store errors and pure helper functions."""
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Literal, Optional, Union

from PIL import Image


OCRStatus = Literal["unread", "ready", "failed"]
LinkFilter = Literal["unclassified", "linked", "all"]
CaptureIdentifier = Union[str, Iterable[str]]


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


def _next_datetime_text(previous_text: str) -> str:
    """Return a timestamp strictly newer than the previous concurrency token."""
    now = _utc_now()
    previous = _datetime_from_text(previous_text)
    if now <= previous:
        now = previous + timedelta(microseconds=1)
    return _datetime_to_text(now)


def _validated_datetime_text(value: datetime) -> str:
    if not isinstance(value, datetime):
        raise TypeError("expected_updated_at must be a datetime")
    return _datetime_to_text(value)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
