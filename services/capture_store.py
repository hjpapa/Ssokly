from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
import tempfile
from typing import Iterable, Optional

from PIL import Image


MAX_STORED_CAPTURES = 8
DEFAULT_STORE_DIR = Path(tempfile.gettempdir()) / "Ssokly" / "captures"
PNG_CAPTURE_COMPRESS_LEVEL = 3


@dataclass(frozen=True)
class CaptureRecord:
    path: Path
    created_at: datetime

    @property
    def label(self) -> str:
        parts = self.path.stem.split("_", 3)
        source = parts[3] if len(parts) == 4 else "capture"
        return f"{self.created_at:%m/%d %H:%M}  {source}"


class CaptureStore:
    def __init__(
        self,
        directory: Optional[Path] = None,
        max_items: int = MAX_STORED_CAPTURES,
    ) -> None:
        self.directory = directory or DEFAULT_STORE_DIR
        self.max_items = max(5, max_items)
        self.directory.mkdir(parents=True, exist_ok=True)

    def save(self, image: Image.Image, source: str = "capture") -> CaptureRecord:
        created_at = datetime.now()
        safe_source = re.sub(r"[^0-9A-Za-z가-힣_-]+", "_", source).strip("_")
        safe_source = safe_source[:32] or "capture"
        filename = f"{created_at:%Y%m%d_%H%M%S_%f}_{safe_source}.png"
        path = self.directory / filename

        prepared_image = (
            image if image.mode in ("RGB", "L") else image.convert("RGB")
        )
        prepared_image.save(
            path,
            format="PNG",
            compress_level=PNG_CAPTURE_COMPRESS_LEVEL,
        )

        self._prune()
        return CaptureRecord(path=path, created_at=created_at)

    def list_recent(self) -> list[CaptureRecord]:
        records = [self._to_record(path) for path in self.directory.glob("*.png")]
        return sorted(records, key=lambda record: record.created_at, reverse=True)

    def load(self, record: CaptureRecord) -> Image.Image:
        with Image.open(record.path) as image:
            return image.copy()

    def delete(self, record: CaptureRecord) -> None:
        record.path.unlink(missing_ok=True)

    def _prune(self) -> None:
        for record in self.list_recent()[self.max_items :]:
            record.path.unlink(missing_ok=True)

    @staticmethod
    def _to_record(path: Path) -> CaptureRecord:
        created_at = datetime.fromtimestamp(path.stat().st_mtime)
        return CaptureRecord(path=path, created_at=created_at)


def labels_for(records: Iterable[CaptureRecord]) -> list[str]:
    return [record.label for record in records]
