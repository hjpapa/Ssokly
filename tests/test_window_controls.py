from __future__ import annotations

from pathlib import Path
import tempfile
import tkinter as tk
import unittest

from services.capture_store import CaptureStore
from services.settings_store import WindowSettingsStore
from services.task_store import TaskStore
from ui.app import SsoklyApp


class WindowControlsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        self.root = Path(self.temp_directory.name)
        self.task_store = TaskStore(
            db_path=self.root / "ssokly.db",
            app_data_dir=self.root / "app-data",
        )
        self.capture_store = CaptureStore(
            directory=self.root / "temporary-captures"
        )
        self.settings_path = self.root / "window-settings.json"
        self.settings_store = WindowSettingsStore(
            settings_path=self.settings_path,
            app_data_dir=self.root / "app-data",
        )
        self.app = None

        try:
            self.app = self._make_app()
        except tk.TclError as exc:
            self.skipTest(f"Tk is not available in this environment: {exc}")

        self.addCleanup(self._destroy_current_app)
        self.app.update_idletasks()

    def _make_app(self) -> SsoklyApp:
        return SsoklyApp(
            task_store=self.task_store,
            capture_store=self.capture_store,
            settings_store=self.settings_store,
        )

    def _destroy_app(self, app: SsoklyApp | None) -> None:
        if app is None:
            return
        try:
            app._closing = True
            app._cancel_autosave()
            if app._task_search_after_id is not None:
                app.after_cancel(app._task_search_after_id)
                app._task_search_after_id = None
            if app._operation_progress_after_id is not None:
                app.after_cancel(app._operation_progress_after_id)
                app._operation_progress_after_id = None
            if app._worker_poll_after_id is not None:
                app.after_cancel(app._worker_poll_after_id)
                app._worker_poll_after_id = None
            app.destroy()
        except tk.TclError:
            pass

    def _destroy_current_app(self) -> None:
        self._destroy_app(self.app)
        self.app = None

    def _prepare_saved_dirty_workspace(self) -> tuple[str, dict[str, object]]:
        self.app._start_new_workspace(source_kind="manual", source_name="직접 입력")
        self.app._replace_ocr_text("보존할 업무 원문", track_change=True)
        self.app._replace_result_text("보존할 업무 실행안", track_change=True)
        self.app.task_title_var.set("창 모드 전환 업무")
        self.assertTrue(self.app.save_current_task(show_success=False))
        task_id = self.app.current_task_id
        self.assertIsNotNone(task_id)

        self.app._replace_result_text("전환 중에도 보존할 수정 실행안", track_change=True)
        self.app._cancel_autosave()
        snapshot = {
            "task_id": task_id,
            "title": self.app.task_title_var.get(),
            "source": self.app.ocr_text.get("1.0", "end-1c"),
            "result": self.app.result_text.get("1.0", "end-1c"),
            "dirty": self.app.current_dirty,
            "context_id": self.app.current_context_id,
        }
        return task_id, snapshot

    def _assert_workspace_matches(self, snapshot: dict[str, object]) -> None:
        self.assertEqual(self.app.current_task_id, snapshot["task_id"])
        self.assertEqual(self.app.task_title_var.get(), snapshot["title"])
        self.assertEqual(
            self.app.ocr_text.get("1.0", "end-1c"), snapshot["source"]
        )
        self.assertEqual(
            self.app.result_text.get("1.0", "end-1c"), snapshot["result"]
        )
        self.assertEqual(self.app.current_dirty, snapshot["dirty"])
        self.assertEqual(self.app.current_context_id, snapshot["context_id"])

    def test_compact_roundtrip_preserves_workspace_and_restores_window(self) -> None:
        _, workspace = self._prepare_saved_dirty_workspace()
        self.app.geometry("1280x820+40+30")
        self.app.update_idletasks()
        normal_geometry = self.app.geometry()
        normal_minsize = self.app.minsize()
        collapsible_widgets = [
            self.app.sidebar,
            self.app.header_title_block,
            self.app.creator_footer,
            self.app.task_detail_actions,
            self.app.result_title_block,
            *self.app.partial_copy_buttons,
        ]
        for widget in collapsible_widgets:
            self.assertNotEqual(widget.winfo_manager(), "")

        self.app.set_compact_mode(True)
        self.app.update_idletasks()

        self.assertTrue(self.app.compact_mode)
        self.assertEqual(self.app.minsize(), (620, 560))
        for widget in collapsible_widgets:
            self.assertEqual(widget.winfo_manager(), "")
        self.assertNotEqual(self.app.window_controls.winfo_manager(), "")
        self.assertNotEqual(self.app.header_button_bar.winfo_manager(), "")
        self.assertNotEqual(self.app.task_identity_row.winfo_manager(), "")
        self._assert_workspace_matches(workspace)

        self.app.set_compact_mode(False)
        self.app.update_idletasks()

        self.assertFalse(self.app.compact_mode)
        self.assertEqual(self.app.minsize(), normal_minsize)
        self.assertEqual(self.app.geometry(), normal_geometry)
        for widget in collapsible_widgets:
            self.assertNotEqual(widget.winfo_manager(), "")
        self._assert_workspace_matches(workspace)

    def test_compact_mode_keeps_native_frame_and_visible_exit_control(self) -> None:
        self.app.set_compact_mode(True)
        self.app.update_idletasks()

        self.assertFalse(bool(self.app.overrideredirect()))
        self.assertNotEqual(self.app.compact_button.winfo_manager(), "")
        self.assertTrue(self.app.compact_button.instate(["!disabled"]))

        self.app.compact_button.invoke()
        self.app.update_idletasks()
        self.assertFalse(self.app.compact_mode)

    def test_opacity_is_clamped_and_always_on_top_toggles(self) -> None:
        self.app.set_opacity_percent(40)
        self.app.update_idletasks()
        self.assertEqual(self.app.opacity_percent, 92)
        self.assertAlmostEqual(float(self.app.attributes("-alpha")), 0.92, places=2)

        self.app.set_opacity_percent(140)
        self.app.update_idletasks()
        self.assertEqual(self.app.opacity_percent, 100)
        self.assertAlmostEqual(float(self.app.attributes("-alpha")), 1.0, places=2)

        self.app.set_always_on_top(True)
        self.app.update_idletasks()
        self.assertTrue(self.app.always_on_top)
        self.assertTrue(bool(self.app.attributes("-topmost")))

        self.app.set_always_on_top(False)
        self.app.update_idletasks()
        self.assertFalse(self.app.always_on_top)
        self.assertFalse(bool(self.app.attributes("-topmost")))

    def test_window_controls_do_not_invalidate_active_operation(self) -> None:
        self.app._start_new_workspace(source_kind="manual", source_name="직접 입력")
        self.app._replace_ocr_text("처리 중인 원문", track_change=True)
        self.app._replace_result_text("기존 실행안", track_change=True)
        self.app._cancel_autosave()
        operation_id = "window-control-operation"
        operation_context = self.app.current_context_id
        operation_revisions = (
            self.app._source_revision,
            self.app._result_revision,
        )
        self.app._active_operation_id = operation_id
        self.app._active_operation_context = operation_context
        self.app._active_operation_kind = "analysis"
        self.app._active_operation_revisions = operation_revisions

        self.app.set_compact_mode(True)
        self.app.set_opacity_percent(96)
        self.app.set_always_on_top(True)
        self.app.set_compact_mode(False)

        self.assertEqual(self.app._active_operation_id, operation_id)
        self.assertEqual(self.app._active_operation_context, operation_context)
        self.assertEqual(self.app._active_operation_kind, "analysis")
        self.assertEqual(
            self.app._active_operation_revisions, operation_revisions
        )
        self.assertEqual(
            self.app.ocr_text.get("1.0", "end-1c"), "처리 중인 원문"
        )
        self.assertEqual(
            self.app.result_text.get("1.0", "end-1c"), "기존 실행안"
        )

    def test_window_settings_are_persisted_and_reloaded(self) -> None:
        self.app.set_compact_mode(True)
        self.app.set_always_on_top(True)
        self.app.set_opacity_percent(95)
        self.app._flush_window_settings()

        saved = self.settings_store.load()
        self.assertTrue(saved.compact_mode)
        self.assertTrue(saved.always_on_top)
        self.assertEqual(saved.opacity_percent, 95)

        old_app = self.app
        self.app = None
        self._destroy_app(old_app)
        self.settings_store = WindowSettingsStore(
            settings_path=self.settings_path,
            app_data_dir=self.root / "app-data",
        )
        try:
            self.app = self._make_app()
        except tk.TclError as exc:
            self.skipTest(f"Tk is not available in this environment: {exc}")
        self.app.update_idletasks()

        self.assertTrue(self.app.compact_mode)
        self.assertTrue(self.app.always_on_top)
        self.assertEqual(self.app.opacity_percent, 95)
        self.assertEqual(self.app.minsize(), (620, 560))
        self.assertEqual(self.app.sidebar.winfo_manager(), "")
        self.assertAlmostEqual(float(self.app.attributes("-alpha")), 0.95, places=2)
        self.assertTrue(bool(self.app.attributes("-topmost")))


if __name__ == "__main__":
    unittest.main()
