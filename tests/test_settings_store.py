from dataclasses import FrozenInstanceError
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from services.settings_store import WindowSettings, WindowSettingsStore


class WindowSettingsTests(unittest.TestCase):
    def test_defaults_are_immutable(self) -> None:
        settings = WindowSettings()

        self.assertEqual(
            settings,
            WindowSettings(
                compact_mode=False,
                always_on_top=False,
                opacity_percent=100,
            ),
        )
        with self.assertRaises(FrozenInstanceError):
            settings.opacity_percent = 95  # type: ignore[misc]

    def test_constructor_clamps_opacity_and_rejects_wrong_types_safely(self) -> None:
        self.assertEqual(WindowSettings(opacity_percent=80).opacity_percent, 92)
        self.assertEqual(WindowSettings(opacity_percent=110).opacity_percent, 100)
        self.assertEqual(WindowSettings(opacity_percent=95).opacity_percent, 95)
        self.assertEqual(WindowSettings(opacity_percent=True).opacity_percent, 100)
        self.assertEqual(  # type: ignore[arg-type]
            WindowSettings(opacity_percent="95").opacity_percent,
            100,
        )
        self.assertFalse(WindowSettings(compact_mode=1).compact_mode)  # type: ignore[arg-type]


class WindowSettingsStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)

    def test_missing_file_loads_defaults_without_creating_directory(self) -> None:
        settings_dir = self.root / "not-created-yet"
        store = WindowSettingsStore(app_data_dir=settings_dir)

        self.assertEqual(store.load(), WindowSettings())
        self.assertFalse(settings_dir.exists())

    def test_save_creates_directory_and_round_trips_version_one_json(self) -> None:
        settings_path = self.root / "nested" / "custom.json"
        store = WindowSettingsStore(settings_path=settings_path)
        expected = WindowSettings(True, True, 96)

        store.save(expected)

        self.assertEqual(store.load(), expected)
        self.assertEqual(
            json.loads(settings_path.read_text(encoding="utf-8")),
            {
                "version": 1,
                "compact_mode": True,
                "always_on_top": True,
                "opacity_percent": 96,
            },
        )
        self.assertEqual(list(settings_path.parent.glob(".custom.json.*.tmp")), [])

    def test_corrupt_and_unsupported_payloads_load_defaults(self) -> None:
        settings_path = self.root / "settings.json"
        store = WindowSettingsStore(settings_path=settings_path)
        settings_path.write_text("{broken", encoding="utf-8")
        self.assertEqual(store.load(), WindowSettings())

        for payload in (
            [],
            {"version": 2, "compact_mode": True},
            {"version": True, "compact_mode": True},
            {"version": "1", "compact_mode": True},
        ):
            settings_path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(store.load(), WindowSettings())

    def test_wrong_field_types_use_safe_defaults_and_integer_opacity_clamps(self) -> None:
        settings_path = self.root / "settings.json"
        store = WindowSettingsStore(settings_path=settings_path)

        settings_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "compact_mode": "yes",
                    "always_on_top": 1,
                    "opacity_percent": "95",
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(store.load(), WindowSettings())

        settings_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "compact_mode": True,
                    "always_on_top": False,
                    "opacity_percent": 20,
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(store.load(), WindowSettings(True, False, 92))

    def test_atomic_save_failure_keeps_previous_file_and_removes_temporary_file(
        self,
    ) -> None:
        settings_path = self.root / "settings.json"
        store = WindowSettingsStore(settings_path=settings_path)
        original = WindowSettings(opacity_percent=94)
        store.save(original)

        with patch("services.settings_store.os.replace", side_effect=OSError("busy")):
            with self.assertRaises(OSError):
                store.save(WindowSettings(compact_mode=True))

        self.assertEqual(store.load(), original)
        self.assertEqual(list(self.root.glob(".settings.json.*.tmp")), [])

    def test_cleanup_failure_does_not_hide_the_original_save_error(self) -> None:
        settings_path = self.root / "settings.json"
        store = WindowSettingsStore(settings_path=settings_path)

        with patch(
            "services.settings_store.os.replace",
            side_effect=OSError("replace failed"),
        ):
            with patch.object(Path, "unlink", side_effect=OSError("cleanup failed")):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    store.save(WindowSettings(compact_mode=True))

    def test_save_rejects_non_settings_value(self) -> None:
        store = WindowSettingsStore(app_data_dir=self.root)
        with self.assertRaises(TypeError):
            store.save({})  # type: ignore[arg-type]

    def test_default_path_uses_local_app_data_then_home_fallback(self) -> None:
        local_app_data = self.root / "local"
        with patch.dict(os.environ, {"LOCALAPPDATA": str(local_app_data)}):
            store = WindowSettingsStore()
        self.assertEqual(store.settings_path, local_app_data / "Ssokly" / "settings.json")

        fallback_home = self.root / "home"
        with patch.dict(os.environ, {"LOCALAPPDATA": ""}):
            with patch("services.settings_store.Path.home", return_value=fallback_home):
                fallback_store = WindowSettingsStore()
        self.assertEqual(fallback_store.settings_path, fallback_home / ".ssokly" / "settings.json")


if __name__ == "__main__":
    unittest.main()
