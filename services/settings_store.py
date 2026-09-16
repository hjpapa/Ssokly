from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Optional, Union


SETTINGS_VERSION = 1
APP_DIRECTORY_NAME = "Ssokly"
SETTINGS_FILENAME = "settings.json"
MIN_OPACITY_PERCENT = 92
MAX_OPACITY_PERCENT = 100

PathLike = Union[str, os.PathLike]


@dataclass(frozen=True)
class WindowSettings:
    compact_mode: bool = False
    always_on_top: bool = False
    opacity_percent: int = MAX_OPACITY_PERCENT
    role: str = "담당 미지정"
    ocr_engine: str = "OpenAI 정밀 OCR"
    analysis_model: str = "gpt-5-nano"
    ocr_model: str = "gpt-5-nano"

    def __post_init__(self) -> None:
        for field, default in (("role", "담당 미지정"), ("ocr_engine", "OpenAI 정밀 OCR"), ("analysis_model", "gpt-5-nano"), ("ocr_model", "gpt-5-nano")):
            value = getattr(self, field)
            object.__setattr__(self, field, value.strip()[:120] if isinstance(value, str) and value.strip() else default)
        object.__setattr__(
            self,
            "compact_mode",
            _safe_bool(self.compact_mode, default=False),
        )
        object.__setattr__(
            self,
            "always_on_top",
            _safe_bool(self.always_on_top, default=False),
        )
        object.__setattr__(
            self,
            "opacity_percent",
            _safe_opacity_percent(self.opacity_percent),
        )


class WindowSettingsStore:
    """JSON-backed storage for Ssokly window preferences."""

    def __init__(
        self,
        settings_path: Optional[PathLike] = None,
        app_data_dir: Optional[PathLike] = None,
    ) -> None:
        if app_data_dir is not None:
            resolved_app_data_dir = Path(app_data_dir).expanduser()
        elif settings_path is not None:
            resolved_app_data_dir = Path(settings_path).expanduser().parent
        else:
            resolved_app_data_dir = _default_app_data_dir()

        self.app_data_dir = resolved_app_data_dir.resolve(strict=False)
        self.settings_path = (
            Path(settings_path).expanduser().resolve(strict=False)
            if settings_path is not None
            else self.app_data_dir / SETTINGS_FILENAME
        )

    def load(self) -> WindowSettings:
        try:
            with self.settings_path.open("r", encoding="utf-8") as settings_file:
                payload = json.load(settings_file)
        except (OSError, UnicodeError, json.JSONDecodeError):
            return WindowSettings()

        if not isinstance(payload, dict):
            return WindowSettings()
        version = payload.get("version")
        if not isinstance(version, int) or isinstance(version, bool):
            return WindowSettings()
        if version != SETTINGS_VERSION:
            return WindowSettings()

        return WindowSettings(
            compact_mode=_safe_bool(payload.get("compact_mode"), default=False),
            always_on_top=_safe_bool(payload.get("always_on_top"), default=False),
            opacity_percent=_safe_opacity_percent(payload.get("opacity_percent")),
            role=payload.get("role", "담당 미지정"),
            ocr_engine="OpenAI 정밀 OCR",
            analysis_model="gpt-5-nano",
            ocr_model="gpt-5-nano",
        )

    def save(self, settings: WindowSettings) -> None:
        if not isinstance(settings, WindowSettings):
            raise TypeError("settings must be a WindowSettings instance")

        payload = {
            "version": SETTINGS_VERSION,
            "compact_mode": settings.compact_mode,
            "always_on_top": settings.always_on_top,
            "opacity_percent": settings.opacity_percent,
            "role": settings.role,
            "ocr_engine": settings.ocr_engine,
            "analysis_model": settings.analysis_model,
            "ocr_model": settings.ocr_model,
        }
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)

        temporary_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.settings_path.parent,
                prefix=f".{self.settings_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                json.dump(payload, temporary_file, ensure_ascii=False, indent=2)
                temporary_file.write("\n")
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, self.settings_path)
        except Exception:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise


def _default_app_data_dir() -> Path:
    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    if local_app_data:
        return (Path(local_app_data).expanduser() / APP_DIRECTORY_NAME).resolve(
            strict=False
        )
    return (Path.home() / ".ssokly").resolve(strict=False)


def _safe_bool(value: Any, *, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _safe_opacity_percent(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        return MAX_OPACITY_PERCENT
    return min(MAX_OPACITY_PERCENT, max(MIN_OPACITY_PERCENT, value))
