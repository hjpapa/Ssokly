from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest import mock

from PIL import Image

from services.capture_store import CaptureStore
from services.task_store import TaskStore
from ui.app import SsoklyApp
from tests.transfer_fixtures import approve_synthetic_transfer


class AppTaskLifecycleTestCase(unittest.TestCase):
    def test_source_highlighting_table_view_and_edits_preserve_text(self):
        text = '😀 안내\n대상\t기한\n담임\t10월 2일\n⟦판독불가⟧'
        self.app._replace_ocr_text(text, track_change=True)
        ranges = self.app.ocr_text.tag_ranges('source_review')
        highlighted = [self.app.ocr_text.get(ranges[i], ranges[i+1]) for i in range(0,len(ranges),2)]
        self.assertNotIn('10월 2일', highlighted)
        dates = self.app.ocr_text.tag_ranges('source_date')
        self.assertEqual(self.app.ocr_text.get(dates[0], dates[1]), '10월 2일')
        self.assertIn('⟦판독불가⟧', highlighted)
        self.assertEqual(self.app.ocr_text.get('1.0','end-1c'), text)
        self.app.show_source_tables()
        dialog = next(w for w in self.app.winfo_children() if isinstance(w, tk.Toplevel))
        self.assertIn('인식된 표', dialog.title())
        dialog.destroy()
        self.app._replace_ocr_text('수정 완료')
        self.assertFalse(self.app.ocr_text.tag_ranges('source_review'))

    def test_changed_source_blocks_personal_todo_dialog(self):
        self.app._replace_ocr_text("기한 10월 2일")
        self.app._finish_analysis("## 체크리스트\n- [ ] 10월 2일까지 제출", "업무 일정·체크리스트")
        self.app._replace_ocr_text("기한 10월 9일", track_change=True)
        with mock.patch("ui.app.messagebox.showinfo") as notice, mock.patch.object(self.app, "_todo_window") as window:
            self.app.choose_personal_todos()
        notice.assert_called_once()
        window.assert_not_called()
        self.assertEqual(self.app.personal_todos.list(), [])

    def test_choose_personal_todo_and_complete_after_reopen(self):
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        self.assertFalse(hasattr(self.app, 'role_var'))
        self.app._replace_ocr_text("담임은 명단을 제출한다.")
        self.app._finish_analysis("## 체크리스트\n- [ ] 명단 제출\n  담당: 담임 · 기한: 10월 5일\n- [ ] 비용 처리\n  담당: 행정실", "업무 일정·체크리스트")
        self.app.choose_personal_todos()
        dialog = next(w for w in self.app.winfo_children() if isinstance(w, tk.Toplevel))
        widgets = list(descendants(dialog))
        box = next(w for w in widgets if isinstance(w, tk.Listbox))
        box.selection_set(0)
        next(w for w in widgets if 'text' in w.keys() and w.cget('text') == '선택한 항목 담기').invoke()
        rows = self.app.personal_todos.list()
        self.assertEqual(len(rows), 1)
        self.assertIn('담임', rows[0]['item'])
        self.app.show_personal_todos()
        dialog = next(w for w in self.app.winfo_children() if isinstance(w, tk.Toplevel))
        widgets = list(descendants(dialog))
        box = next(w for w in widgets if isinstance(w, tk.Listbox))
        box.selection_set(0)
        next(w for w in widgets if 'text' in w.keys() and w.cget('text') == '완료').invoke()
        self.assertTrue(self.app.personal_todos.list()[0]['done'])
        dialog.destroy()

    def test_stream_preview_never_changes_saved_editor_and_drops_stale_updates(self):
        import threading
        self.app._replace_result_text("기존 실행안")
        event = threading.Event()
        self.app._active_operation_cancel_event = event
        revisions = (self.app._source_revision, self.app._result_revision)
        self.app._worker_results.put((event, self.app.current_context_id, "analysis_preview", True, "생성 중", {"revisions": revisions}))
        self.app._drain_worker_results()
        self.assertEqual(self.app.result_text.get("1.0", "end-1c"), "기존 실행안")
        self.assertEqual(self.app.stream_preview.get("1.0", "end-1c"), "생성 중")
        self.app._replace_result_text("사용자가 편집", track_change=True)
        self.app._worker_results.put((event, self.app.current_context_id, "analysis_preview", True, "오래된 조각", {"revisions": revisions}))
        self.app._drain_worker_results()
        self.assertEqual(self.app.stream_preview.get("1.0", "end-1c"), "생성 중")
        self.assertEqual(self.app.result_text.get("1.0", "end-1c"), "사용자가 편집")

    def test_capture_and_reread_use_nano_after_explicit_transfer_approval(self):
        from PIL import Image
        image = Image.new("RGB", (100, 100))
        with mock.patch.object(self.app, "_start_worker_operation") as start:
            self.app._run_ocr(image)
        work = start.call_args.args[1]
        with mock.patch("ui.app.extract_local_text") as local, mock.patch("ui.app.extract_text_from_image", return_value="nano 원문") as cloud:
            self.assertEqual(work(), "nano 원문")
            local.assert_not_called()
            self.assertEqual(cloud.call_args.kwargs["model_override"], "gpt-5-nano")
        self.assertEqual(self.app.analysis_model_var.get(), "gpt-5-nano")
        self.assertEqual([button.cget("text") for button in self.app.template_buttons],
                         ["업무 일정·체크리스트", "교직원 메신저", "학부모 메신저", "가정통신문 초안"])
        with mock.patch.object(self.app, "_start_worker_operation") as start:
            self.app._run_ocr(image, apply_mode="review")
        with mock.patch("ui.app.extract_text_from_image", return_value="정밀 원문") as cloud:
            self.assertEqual(start.call_args.args[1](), "정밀 원문")
            self.assertEqual(cloud.call_args.kwargs["model_override"], "gpt-5-nano")

    def setUp(self) -> None:
        transfer = mock.patch('ui.app.choose_transfer', side_effect=approve_synthetic_transfer)
        transfer.start()
        self.addCleanup(transfer.stop)
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        self.root = Path(self.temp_directory.name)
        self.store = TaskStore(
            db_path=self.root / "ssokly.db",
            app_data_dir=self.root / "app-data",
        )
        try:
            self.app = SsoklyApp(
                task_store=self.store,
                capture_store=CaptureStore(directory=self.root / "temporary-captures"),
            )
        except tk.TclError as exc:
            self.skipTest(f"Tk is not available in this environment: {exc}")
        self.addCleanup(self._destroy_app)
        self.app.update_idletasks()

    def _destroy_app(self) -> None:
        app = getattr(self, "app", None)
        if app is None:
            return
        try:
            app._closing = True
            app._cancel_autosave()
            if app._worker_poll_after_id is not None:
                app.after_cancel(app._worker_poll_after_id)
                app._worker_poll_after_id = None
            app.destroy()
        except tk.TclError:
            pass

    def test_save_autosave_and_reopen_restore_without_ai_calls(self) -> None:
        self.app._start_new_workspace(source_kind="manual", source_name="직접 입력")
        self.app._replace_ocr_text("출장 신청 원문", track_change=True)

        self.assertTrue(self.app.save_current_task(show_success=False))
        task_id = self.app.current_task_id
        self.assertIsNotNone(task_id)

        self.app._replace_result_text("출장 신청 실행안", track_change=True)
        self.assertIsNotNone(self.app._autosave_after_id)
        self.app._cancel_autosave()
        self.app._autosave_if_current(task_id, self.app.current_context_id)

        saved = self.store.get(task_id)
        self.assertIsNotNone(saved)
        self.assertEqual(saved.source_text, "출장 신청 원문")
        self.assertEqual(saved.analysis_text, "출장 신청 실행안")

        self.app._start_new_workspace(source_kind="manual", source_name="직접 입력")
        self.app._refresh_task_library(selected_id=task_id)
        self.app.task_tree.selection_set(task_id)
        with mock.patch("ui.app.extract_text_from_image") as image_ocr:
            with mock.patch("ui.app.extract_text_from_file") as file_ocr:
                with mock.patch("ui.app.analyze_document_task") as analysis:
                    self.app.open_selected_task()

        image_ocr.assert_not_called()
        file_ocr.assert_not_called()
        analysis.assert_not_called()
        self.assertEqual(self.app.ocr_text.get("1.0", "end-1c"), "출장 신청 원문")
        self.assertEqual(self.app.result_text.get("1.0", "end-1c"), "출장 신청 실행안")

    def test_unsaved_leave_prompt_can_cancel_discard_or_save(self) -> None:
        self.app._start_new_workspace(source_kind="manual", source_name="직접 입력")
        self.app._replace_ocr_text("저장 전 원문", track_change=True)

        with mock.patch("ui.app.messagebox.askyesnocancel", return_value=None):
            self.assertFalse(self.app._prepare_to_leave_current("다른 업무"))
        self.assertEqual(self.app.ocr_text.get("1.0", "end-1c"), "저장 전 원문")
        self.assertIsNone(self.app.current_task_id)

        with mock.patch("ui.app.messagebox.askyesnocancel", return_value=False):
            self.assertTrue(self.app._prepare_to_leave_current("다른 업무"))
        self.assertIsNone(self.app.current_task_id)

        with mock.patch("ui.app.messagebox.askyesnocancel", return_value=True):
            self.assertTrue(self.app._prepare_to_leave_current("다른 업무"))
        self.assertIsNotNone(self.app.current_task_id)

    def test_save_failure_preserves_editor_content_and_dirty_state(self) -> None:
        self.app._start_new_workspace(source_kind="manual", source_name="직접 입력")
        self.app._replace_ocr_text("보존해야 할 원문", track_change=True)
        self.assertTrue(self.app.save_current_task(show_success=False))
        self.app._replace_result_text("보존해야 할 실행안", track_change=True)

        with mock.patch.object(self.store, "update", side_effect=OSError("disk error")):
            self.assertFalse(self.app.save_current_task(show_success=False))

        self.assertTrue(self.app.current_dirty)
        self.assertTrue(self.app._autosave_failed)
        self.assertEqual(self.app.ocr_text.get("1.0", "end-1c"), "보존해야 할 원문")
        self.assertEqual(self.app.result_text.get("1.0", "end-1c"), "보존해야 할 실행안")

    def test_saved_task_can_persist_intentionally_empty_editors(self) -> None:
        self.app._start_new_workspace(source_kind="manual", source_name="직접 입력")
        self.app._replace_ocr_text("지울 원문", track_change=True)
        self.app._replace_result_text("지울 실행안", track_change=True)
        self.assertTrue(self.app.save_current_task(show_success=False))
        task_id = self.app.current_task_id

        self.app._replace_ocr_text("", track_change=True)
        self.app._replace_result_text("", track_change=True)
        self.assertTrue(self.app.save_current_task(show_success=False))

        saved = self.store.get(task_id)
        self.assertEqual(saved.source_text, "")
        self.assertEqual(saved.analysis_text, "")
        self.assertFalse(self.app.current_dirty)

    def test_stale_app_save_preserves_newer_database_content(self) -> None:
        self.app._start_new_workspace(source_kind="manual", source_name="직접 입력")
        self.app._replace_ocr_text("공유 원문", track_change=True)
        self.app._replace_result_text("초기 실행안", track_change=True)
        self.assertTrue(self.app.save_current_task(show_success=False))
        task_id = self.app.current_task_id

        other_store = TaskStore(
            db_path=self.store.db_path,
            app_data_dir=self.store.app_data_dir,
        )
        other_store.update(
            task_id,
            expected_updated_at=self.app.current_task_updated_at,
            analysis_text="다른 창의 최신 실행안",
        )
        self.app._replace_result_text("현재 창의 오래된 실행안", track_change=True)

        self.assertFalse(self.app.save_current_task(show_success=False))
        self.assertTrue(self.app.current_dirty)
        self.assertEqual(
            self.store.get(task_id).analysis_text,
            "다른 창의 최신 실행안",
        )

    def test_stale_worker_result_cannot_overwrite_new_workspace(self) -> None:
        self.app._start_new_workspace(source_kind="manual", source_name="직접 입력")
        old_context = self.app.current_context_id
        self.app._active_operation_id = "old-operation"
        self.app._active_operation_context = old_context
        self.app._active_operation_kind = "ocr"
        self.app._worker_results.put(
            ("old-operation", old_context, "ocr", True, "늦게 도착한 원문", {})
        )

        self.app.current_context_id = "new-context"
        self.app._replace_ocr_text("현재 원문")
        self.app._drain_worker_results()

        self.assertEqual(self.app.ocr_text.get("1.0", "end-1c"), "현재 원문")

    def test_processing_indicator_keeps_safe_controls_available(self) -> None:
        self.app._start_new_workspace(source_kind="manual", source_name="직접 입력")
        self.app._replace_ocr_text("분석할 원문", track_change=True)
        operation_id = "visible-analysis"
        context_id = self.app.current_context_id
        self.app._active_operation_id = operation_id
        self.app._active_operation_context = context_id
        self.app._active_operation_kind = "analysis"
        self.app._active_operation_revisions = (
            self.app._source_revision,
            self.app._result_revision,
        )
        self.app._show_operation_progress(operation_id, "analysis")
        self.app._render_workspace_state()

        self.assertNotEqual(self.app.processing_frame.winfo_manager(), "")
        self.assertIn("업무 분석", self.app.processing_var.get())
        self.assertEqual(str(self.app.capture_button.cget("state")), "disabled")
        self.assertEqual(str(self.app.task_title_entry.cget("state")), "normal")
        self.assertEqual(str(self.app.task_search_entry.cget("state")), "normal")
        self.assertEqual(str(self.app.ocr_text.cget("state")), "normal")
        self.assertEqual(str(self.app.result_text.cget("state")), "normal")
        self.assertTrue(self.app.save_task_button.instate(["!disabled"]))
        self.assertTrue(self.app.copy_button.instate(["!disabled"]))

        self.app._worker_results.put(
            (
                operation_id,
                context_id,
                "analysis",
                True,
                "완료된 실행안",
                {"output_mode": "통합 실행안"},
            )
        )
        self.app._drain_worker_results()

        self.assertEqual(self.app.processing_frame.winfo_manager(), "")
        self.assertEqual(self.app.result_text.get("1.0", "end-1c"), "완료된 실행안")

    def test_edit_during_processing_protects_user_content(self) -> None:
        self.app._start_new_workspace(source_kind="manual", source_name="직접 입력")
        self.app._replace_ocr_text("분석 시작 원문", track_change=True)
        self.app._replace_result_text("기존 실행안", track_change=True)
        operation_id = "edited-analysis"
        context_id = self.app.current_context_id
        self.app._active_operation_id = operation_id
        self.app._active_operation_context = context_id
        self.app._active_operation_kind = "analysis"
        self.app._active_operation_revisions = (
            self.app._source_revision,
            self.app._result_revision,
        )
        self.app._show_operation_progress(operation_id, "analysis")

        self.app._replace_ocr_text("처리 중 사용자가 고친 원문", track_change=True)
        self.app._worker_results.put(
            (
                operation_id,
                context_id,
                "analysis",
                True,
                "늦게 도착한 실행안",
                {"output_mode": "통합 실행안"},
            )
        )

        with mock.patch("ui.app.messagebox.showinfo") as show_info:
            self.app._drain_worker_results()

        show_info.assert_called_once()
        self.assertEqual(
            self.app.ocr_text.get("1.0", "end-1c"),
            "처리 중 사용자가 고친 원문",
        )
        self.assertEqual(self.app.result_text.get("1.0", "end-1c"), "기존 실행안")

    def test_processing_blocks_tree_shortcut_task_switch(self) -> None:
        self.app._active_operation_id = "active-operation"
        with mock.patch.object(self.app, "_prepare_to_leave_current") as leave_guard:
            with mock.patch("ui.app.messagebox.showinfo") as show_info:
                self.app.open_selected_task()

        show_info.assert_called_once()
        leave_guard.assert_not_called()

    def test_local_text_read_is_dispatched_to_worker(self) -> None:
        path = self.root / "large-notice.txt"
        with mock.patch("ui.app.read_text_file", return_value="비동기 원문") as reader:
            with mock.patch.object(self.app, "_start_worker_operation") as start:
                self.app._load_text_attachment(path)

        reader.assert_not_called()
        kind, work, metadata = start.call_args.args
        self.assertEqual(kind, "document")
        self.assertEqual(metadata["filename"], path.name)
        self.assertEqual(work(), "비동기 원문")
        reader.assert_called_once_with(path)

    def test_task_search_refresh_is_debounced(self) -> None:
        with mock.patch.object(self.app, "after", return_value="search-timer") as after:
            self.app._on_task_search_changed()

        self.assertEqual(after.call_args.args[0], 200)
        self.assertEqual(self.app._task_search_after_id, "search-timer")
        self.app._task_search_after_id = None

    def test_capture_selector_starts_without_fixed_delay(self) -> None:
        with mock.patch.object(self.app, "withdraw"):
            with mock.patch.object(self.app, "after_idle") as after_idle:
                self.app.capture_area()

        after_idle.assert_called_once_with(self.app._capture_after_hide)

    def test_failed_ai_operation_preserves_last_good_saved_content(self) -> None:
        self.app._start_new_workspace(source_kind="manual", source_name="직접 입력")
        self.app._replace_ocr_text("정상 원문", track_change=True)
        self.app._replace_result_text("정상 실행안", track_change=True)
        self.assertTrue(self.app.save_current_task(show_success=False))
        task_id = self.app.current_task_id

        operation_id = "failed-analysis"
        context_id = self.app.current_context_id
        self.app._active_operation_id = operation_id
        self.app._active_operation_context = context_id
        self.app._active_operation_kind = "analysis"
        self.app._worker_results.put(
            (operation_id, context_id, "analysis", False, "network error", {})
        )

        with mock.patch("ui.app.messagebox.showerror") as show_error:
            self.app._drain_worker_results()

        show_error.assert_called_once()
        self.assertEqual(self.app.ocr_text.get("1.0", "end-1c"), "정상 원문")
        self.assertEqual(self.app.result_text.get("1.0", "end-1c"), "정상 실행안")
        saved = self.store.get(task_id)
        self.assertEqual(saved.source_text, "정상 원문")
        self.assertEqual(saved.analysis_text, "정상 실행안")

    def test_leave_confirmation_discards_result_completed_during_prompt(self) -> None:
        self.app._start_new_workspace(source_kind="manual", source_name="직접 입력")
        self.app._replace_ocr_text("정상 원문", track_change=True)
        self.app._replace_result_text("기존 실행안", track_change=True)
        self.assertTrue(self.app.save_current_task(show_success=False))
        task_id = self.app.current_task_id

        operation_id = "analysis-during-prompt"
        context_id = self.app.current_context_id
        self.app._active_operation_id = operation_id
        self.app._active_operation_context = context_id
        self.app._active_operation_kind = "analysis"
        self.app._worker_results.put(
            (
                operation_id,
                context_id,
                "analysis",
                True,
                "확인창 중 완료된 새 실행안",
                {"output_mode": "통합 실행안"},
            )
        )

        def confirm_leave(*_args, **_kwargs) -> bool:
            self.app._drain_worker_results()
            self.assertEqual(len(self.app._held_worker_results), 1)
            return True

        with mock.patch("ui.app.messagebox.askyesno", side_effect=confirm_leave):
            self.assertTrue(self.app._prepare_to_leave_current("다른 업무"))

        self.assertIsNone(self.app._active_operation_id)
        self.assertEqual(self.app.result_text.get("1.0", "end-1c"), "기존 실행안")
        self.assertEqual(self.store.get(task_id).analysis_text, "기존 실행안")

    def test_new_capture_does_not_save_or_prune_before_leave_guard(self) -> None:
        image = Image.new("RGB", (20, 20), "white")
        with mock.patch("ui.app.capture_selected_region", return_value=image):
            with mock.patch.object(
                self.app,
                "_prepare_to_leave_current",
                return_value=False,
            ) as leave_guard:
                with mock.patch.object(self.app.capture_store, "save") as save_capture:
                    self.app._capture_after_hide()

        leave_guard.assert_called_once_with("새 캡처")
        save_capture.assert_not_called()


if __name__ == "__main__":
    unittest.main()
