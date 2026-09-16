from __future__ import annotations

from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest import mock

from PIL import Image

from services.capture_store import CaptureRecord, CaptureStore
from services.task_store import TaskStore
from ui.app import SsoklyApp


class CaptureInboxUiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        self.root = Path(self.temp_directory.name)
        self.task_store = TaskStore(
            db_path=self.root / "ssokly.db",
            app_data_dir=self.root / "app-data",
        )
        self.capture_store = CaptureStore(directory=self.root / "capture-inbox")
        self.app: SsoklyApp | None = None

        try:
            self.app = SsoklyApp(
                task_store=self.task_store,
                capture_store=self.capture_store,
            )
        except tk.TclError as exc:
            self.skipTest(f"Tk is not available in this environment: {exc}")

        self.addCleanup(self._destroy_app)
        self.app.update_idletasks()

    def _destroy_app(self) -> None:
        app = self.app
        self.app = None
        if app is None:
            return
        try:
            app._closing = True
            app._cancel_autosave()
            app._destroy_capture_review_window()
            app._close_recent_window()
            if app._worker_poll_after_id is not None:
                app.after_cancel(app._worker_poll_after_id)
                app._worker_poll_after_id = None
            app.destroy()
        except tk.TclError:
            pass

    def _create_read_capture(
        self,
        number: int,
        text: str,
        *,
        source: str = "capture",
    ) -> CaptureRecord:
        image = Image.new(
            "RGB",
            (80 + number, 50 + number),
            (20 * number % 255, 40 * number % 255, 60 * number % 255),
        )
        record = self.capture_store.save(image, source=f"{source}_{number}")
        return self.capture_store.update_ocr(
            record.id,
            text,
            profile="test-exact-text",
        )

    def _open_inbox_and_select(self, *capture_ids: str) -> None:
        assert self.app is not None
        self.app.show_recent_captures()
        self.app._set_capture_inbox_filter("all")
        self.app.update_idletasks()
        self.app.capture_inbox_tree.selection_set(*capture_ids)
        self.app.capture_inbox_tree.focus(capture_ids[-1])
        self.app.update_idletasks()

    def _linked_capture_ids(self) -> set[str]:
        return {
            record.id
            for record in self.capture_store.search(link_filter="linked")
        }

    def test_recent_button_opens_extended_multi_select_capture_inbox(self) -> None:
        assert self.app is not None
        self._create_read_capture(1, "첫 번째 공문")

        self.app.show_recent_captures()
        self.app.update_idletasks()

        self.assertIsNotNone(self.app.recent_window)
        self.assertTrue(self.app.recent_window.winfo_exists())
        self.assertIn("캡처 보관함", self.app.recent_window.title())
        self.assertEqual(
            str(self.app.capture_inbox_tree.cget("selectmode")),
            "extended",
        )
        self.assertIsInstance(self.app.capture_inbox_filter_var, tk.StringVar)
        for method_name in (
            "_set_capture_inbox_filter",
            "_selected_captures",
            "bundle_selected_captures",
            "add_selected_captures_to_current_task",
            "reread_selected_captures",
            "trash_selected_captures",
        ):
            self.assertTrue(callable(getattr(self.app, method_name)))

    def test_capture_row_shows_thumbnail_and_ocr_first_line(self) -> None:
        assert self.app is not None
        record = self._create_read_capture(
            1,
            "학부모 상담주간 안내\n둘째 줄은 전체 OCR 미리보기에서 확인",
        )

        self.app.show_recent_captures()
        self.app._set_capture_inbox_filter("all")
        self.app.update()

        item = self.app.capture_inbox_tree.item(record.id)
        row_text = " ".join(
            [str(item.get("text", "")), *(str(value) for value in item["values"])]
        )
        self.assertIn("학부모 상담주간 안내", row_text)
        self.assertTrue(item["image"], "capture rows must keep a visible thumbnail")

    def test_single_capture_review_saves_exact_local_text_without_ocr_call(self) -> None:
        assert self.app is not None
        record = self._create_read_capture(21, "김민수  010-1234-567B\n상담일 9월 2일")
        exact_review = "\n김민수  010-1234-5678\n상담일\t9월 2일  \n"
        self._open_inbox_and_select(record.id)

        with mock.patch("ui.app.extract_text_from_image") as image_ocr:
            self.app.review_selected_capture()
            self.assertIsNotNone(self.app.capture_review_window)
            self.assertEqual(
                self.app.capture_review_raw_text.get("1.0", "end-1c"),
                record.ocr_text,
            )
            self.app.capture_review_text.delete("1.0", tk.END)
            self.app.capture_review_text.insert("1.0", exact_review)
            self.app.capture_review_dirty = True
            self.assertTrue(self.app._save_capture_review())

        image_ocr.assert_not_called()
        saved = self.capture_store.get(record.id)
        self.assertTrue(saved.is_verified)
        self.assertEqual(saved.ocr_text, record.ocr_text)
        self.assertEqual(saved.verified_text, exact_review)
        self.assertEqual(saved.effective_text, exact_review)
        self.assertIsNone(self.app.capture_review_window)

    def test_verified_text_safely_updates_current_capture_before_analysis(self) -> None:
        assert self.app is not None
        record = self._create_read_capture(29, "담당: 김민수 010-1234-567B")
        corrected = "담당: 김민수 010-1234-5678"
        self.app._start_new_workspace(
            source_kind="capture",
            source_name="현재 캡처",
            capture_path=record.path,
            capture_ids=[record.id],
        )
        self.app._replace_ocr_text(record.ocr_text, track_change=True)
        self._open_inbox_and_select(record.id)
        self.app.review_selected_capture()
        self.app.capture_review_text.delete("1.0", tk.END)
        self.app.capture_review_text.insert("1.0", corrected)
        self.app.capture_review_dirty = True

        self.assertTrue(self.app._save_capture_review())

        self.assertEqual(self.app.ocr_text.get("1.0", "end-1c"), corrected)
        with mock.patch.object(self.app, "_start_worker_operation") as start:
            self.app.analyze_text()
        kind, work, _metadata = start.call_args.args
        self.assertEqual(kind, "analysis")
        with mock.patch("ui.app.analyze_document_task", return_value="실행안") as analyze:
            self.assertEqual(work(), "실행안")
        analyze.assert_called_once_with(
            corrected,
            self.app.current_output_mode,
            raise_errors=True,
            model="gpt-5-nano", on_preview=mock.ANY,
            cancel_event=mock.ANY, cache_dir=self.task_store.app_data_dir, on_metrics=mock.ANY,
        )

    def test_custom_current_edits_open_comparison_instead_of_auto_replace(self) -> None:
        assert self.app is not None
        record = self._create_read_capture(30, "AI OCR 원문")
        teacher_workspace = "교사가 업무 원문에서 이미 편집한 내용"
        verified = "캡처별 교사 검수 최종본"
        self.app._start_new_workspace(
            source_kind="capture",
            source_name="편집 보호 캡처",
            capture_path=record.path,
            capture_ids=[record.id],
        )
        self.app._replace_ocr_text(teacher_workspace, track_change=True)
        self._open_inbox_and_select(record.id)
        self.app.review_selected_capture()
        self.app.capture_review_text.delete("1.0", tk.END)
        self.app.capture_review_text.insert("1.0", verified)
        self.app.capture_review_dirty = True

        with mock.patch.object(self.app, "_show_ocr_comparison") as compare:
            self.assertTrue(self.app._save_capture_review())
            self.app.update()

        self.assertEqual(
            self.app.ocr_text.get("1.0", "end-1c"),
            teacher_workspace,
        )
        compare.assert_called_once()
        self.assertEqual(compare.call_args.args[0], verified)
        self.assertEqual(compare.call_args.kwargs["title"], "검수본 적용 비교")

    def test_verified_capture_updates_an_unchanged_multi_capture_bundle(self) -> None:
        assert self.app is not None
        first = self._create_read_capture(32, "첫 장 AI OCR")
        second = self._create_read_capture(33, "둘째 장 AI OCR 9월 2B일")
        corrected = "둘째 장 교사 검수본 9월 28일"
        records = [first, second]
        self.app._start_new_workspace(
            source_kind="capture",
            source_name="캡처 2개",
            capture_path=first.path,
            capture_ids=[first.id, second.id],
        )
        self.app._replace_ocr_text(
            self.app._capture_bundle_text(records),
            track_change=True,
        )
        self._open_inbox_and_select(second.id)
        self.app.review_selected_capture()
        self.app.capture_review_text.delete("1.0", tk.END)
        self.app.capture_review_text.insert("1.0", corrected)
        self.app.capture_review_dirty = True

        with mock.patch.object(self.app, "_show_ocr_comparison") as compare:
            self.assertTrue(self.app._save_capture_review())
            self.app.update()

        compare.assert_not_called()
        source = self.app.ocr_text.get("1.0", "end-1c")
        self.assertIn(first.ocr_text, source)
        self.assertIn(corrected, source)
        self.assertNotIn(second.ocr_text, source)
        self.assertLess(source.index(first.ocr_text), source.index(corrected))

    def test_concurrent_capture_change_keeps_review_editor_and_rejects_save(self) -> None:
        assert self.app is not None
        record = self._create_read_capture(31, "검수 창이 본 AI OCR")
        self._open_inbox_and_select(record.id)
        self.app.review_selected_capture()
        review_text = "검수 창에서 작성 중인 최종본"
        self.app.capture_review_text.delete("1.0", tk.END)
        self.app.capture_review_text.insert("1.0", review_text)
        self.app.capture_review_dirty = True
        self.capture_store.update_ocr(record.id, "다른 창의 최신 AI OCR")

        with mock.patch("ui.app.messagebox.showerror") as show_error:
            self.assertFalse(self.app._save_capture_review())

        show_error.assert_called_once()
        self.assertTrue(self.app.capture_review_window.winfo_exists())
        self.assertEqual(
            self.app.capture_review_text.get("1.0", "end-1c"),
            review_text,
        )
        self.assertTrue(self.app.capture_review_dirty)
        self.assertFalse(self.capture_store.get(record.id).is_verified)

    def test_review_button_requires_one_active_capture(self) -> None:
        assert self.app is not None
        first = self._create_read_capture(22, "첫 캡처")
        second = self._create_read_capture(23, "둘째 캡처")
        self._open_inbox_and_select(first.id)
        self.app._render_capture_inbox_actions()
        self.assertEqual(str(self.app.review_capture_button.cget("state")), "normal")

        self.app.capture_inbox_tree.selection_set(first.id, second.id)
        self.app._render_capture_inbox_actions()
        self.assertEqual(str(self.app.review_capture_button.cget("state")), "disabled")

        self.capture_store.trash(first.id)
        self.app._set_capture_inbox_filter("trash")
        self.app.capture_inbox_tree.selection_set(first.id)
        self.app._render_capture_inbox_actions()
        self.assertEqual(str(self.app.review_capture_button.winfo_manager()), "")

    def test_review_save_failure_keeps_window_and_teacher_text(self) -> None:
        assert self.app is not None
        record = self._create_read_capture(24, "저장 전 AI OCR")
        self._open_inbox_and_select(record.id)
        self.app.review_selected_capture()
        exact_review = "저장 실패에도 보존할 교사 검수본\n끝 공백  "
        self.app.capture_review_text.delete("1.0", tk.END)
        self.app.capture_review_text.insert("1.0", exact_review)
        self.app.capture_review_dirty = True

        with mock.patch.object(
            self.capture_store,
            "save_verified_text",
            side_effect=OSError("database locked"),
        ):
            with mock.patch("ui.app.messagebox.showerror") as show_error:
                self.assertFalse(self.app._save_capture_review())

        show_error.assert_called_once()
        self.assertTrue(self.app.capture_review_window.winfo_exists())
        self.assertEqual(
            self.app.capture_review_text.get("1.0", "end-1c"),
            exact_review,
        )
        self.assertTrue(self.app.capture_review_dirty)
        self.assertFalse(self.capture_store.get(record.id).is_verified)

    def test_dirty_review_close_can_cancel_or_discard(self) -> None:
        assert self.app is not None
        record = self._create_read_capture(25, "닫기 확인 OCR")
        self._open_inbox_and_select(record.id)
        self.app.review_selected_capture()
        self.app.capture_review_text.insert(tk.END, "\n교사 수정")
        self.app.capture_review_dirty = True

        with mock.patch("ui.app.messagebox.askyesnocancel", return_value=None):
            self.assertFalse(self.app._close_capture_review_window())
        self.assertTrue(self.app.capture_review_window.winfo_exists())

        with mock.patch("ui.app.messagebox.askyesnocancel", return_value=False):
            self.assertTrue(self.app._close_capture_review_window())
        self.assertIsNone(self.app.capture_review_window)
        self.assertFalse(self.capture_store.get(record.id).is_verified)

    def test_app_close_stops_when_dirty_capture_review_is_cancelled(self) -> None:
        assert self.app is not None
        record = self._create_read_capture(27, "앱 종료 보호 OCR")
        self._open_inbox_and_select(record.id)
        self.app.review_selected_capture()
        self.app.capture_review_text.insert(tk.END, "\n종료 전 교사 수정")
        self.app.capture_review_dirty = True

        with mock.patch("ui.app.messagebox.askyesnocancel", return_value=None):
            with mock.patch.object(self.app, "_prepare_to_leave_current") as prepare:
                self.app._on_close()

        prepare.assert_not_called()
        self.assertFalse(self.app._closing)
        self.assertTrue(self.app.winfo_exists())
        self.assertTrue(self.app.capture_review_window.winfo_exists())

    def test_missing_original_blocks_review_completion(self) -> None:
        assert self.app is not None
        record = self._create_read_capture(28, "원본이 필요한 OCR")
        self._open_inbox_and_select(record.id)
        record.path.unlink()

        with mock.patch("ui.app.messagebox.showerror") as show_error:
            self.app.review_selected_capture()

        show_error.assert_called_once()
        self.assertIsNone(self.app.capture_review_window)
        self.assertFalse(self.capture_store.get(record.id).is_verified)

    def test_verified_text_drives_preview_search_and_new_bundle(self) -> None:
        assert self.app is not None
        record = self._create_read_capture(26, "이전 AI OCR 전화 010-9999-000O")
        verified = "교사 확정 원문 전화 010-9999-0000"
        self.capture_store.save_verified_text(record.id, verified)
        self._open_inbox_and_select(record.id)

        item = self.app.capture_inbox_tree.item(record.id)
        self.assertIn("교사 확정 원문", str(item.get("text", "")))
        self.assertEqual(item["values"][1], "검수 완료")
        preview = self.app.capture_preview_text.get("1.0", "end-1c")
        self.assertIn("[교사 검수 최종본]", preview)
        self.assertIn(verified, preview)
        self.assertIn(record.ocr_text, preview)

        self.app.capture_search_var.set("010-9999-0000")
        self.app._cancel_capture_search_refresh()
        self.app._apply_capture_search()
        self.assertEqual([item.id for item in self.app.capture_records], [record.id])

        self.app.capture_inbox_tree.selection_set(record.id)
        self.app.bundle_selected_captures()
        source = self.app.ocr_text.get("1.0", "end-1c")
        self.assertIn(verified, source)
        self.assertNotIn(record.ocr_text, source)

    def test_filters_show_unclassified_linked_and_all_counts(self) -> None:
        assert self.app is not None
        first = self._create_read_capture(1, "미분류 가정통신문")
        second = self._create_read_capture(2, "연결된 체험학습 공문")
        third = self._create_read_capture(3, "미분류 행사 안내")
        task = self.task_store.create(title="체험학습", source_kind="manual")
        self.capture_store.link_to_task(second.id, task.id)

        self.app.show_recent_captures()

        expected_by_filter = {
            "unclassified": {first.id, third.id},
            "linked": {second.id},
            "all": {first.id, second.id, third.id},
        }
        for filter_value, expected_ids in expected_by_filter.items():
            with self.subTest(filter=filter_value):
                self.app._set_capture_inbox_filter(filter_value)
                self.app.update_idletasks()
                self.assertEqual(self.app.capture_inbox_filter_var.get(), filter_value)
                self.assertEqual(
                    {record.id for record in self.app.capture_records},
                    expected_ids,
                )
                self.assertEqual(
                    set(self.app.capture_inbox_tree.get_children()),
                    expected_ids,
                )

    def test_capture_search_finds_older_text_within_selected_filter(self) -> None:
        assert self.app is not None
        matching = self._create_read_capture(15, "학부모 상담 신청 안내")
        self._create_read_capture(16, "교내 체육대회 일정")
        self.app.show_recent_captures()
        self.app._set_capture_inbox_filter("unclassified")

        self.app.capture_search_var.set("상담 신청")
        self.app._cancel_capture_search_refresh()
        self.app._apply_capture_search()

        self.assertEqual(
            [record.id for record in self.app.capture_records],
            [matching.id],
        )
        self.assertEqual(
            set(self.app.capture_inbox_tree.get_children()),
            {matching.id},
        )

    def test_bundle_uses_oldest_first_boundaries_and_links_only_after_save(self) -> None:
        assert self.app is not None
        older = self._create_read_capture(1, "첫 장의 정확한 OCR 원문")
        newer = self._create_read_capture(2, "둘째 장의 정확한 OCR 원문")
        self.assertLessEqual(older.created_at, newer.created_at)
        self._open_inbox_and_select(newer.id, older.id)

        selected = self.app._selected_captures()
        self.assertEqual([record.id for record in selected], [older.id, newer.id])

        with mock.patch.object(self.app, "_run_ocr") as run_workspace_ocr:
            with mock.patch("ui.app.extract_text_from_image") as image_ocr:
                self.app.bundle_selected_captures()

        run_workspace_ocr.assert_not_called()
        image_ocr.assert_not_called()
        self.assertIsNone(self.app.current_task_id)
        self.assertEqual(self._linked_capture_ids(), set())

        source = self.app.ocr_text.get("1.0", "end-1c")
        first_position = source.index(older.ocr_text)
        second_position = source.index(newer.ocr_text)
        self.assertLess(first_position, second_position)
        self.assertRegex(source[:first_position], r"\[?캡처\s*1(?:[^\]\n]*)\]?")
        self.assertRegex(
            source[first_position + len(older.ocr_text) : second_position],
            r"\[?캡처\s*2(?:[^\]\n]*)\]?",
        )

        self.assertTrue(self.app.save_current_task(show_success=False))
        task_id = self.app.current_task_id
        self.assertIsNotNone(task_id)
        self.assertEqual(self._linked_capture_ids(), {older.id, newer.id})
        for record in self.capture_store.search(link_filter="linked"):
            self.assertIn(task_id, record.linked_task_ids)

    def test_add_to_current_task_preserves_edits_and_persists_append(self) -> None:
        assert self.app is not None
        self.app._start_new_workspace(source_kind="manual", source_name="직접 입력")
        self.app._replace_ocr_text("원래 저장된 교사 메모", track_change=True)
        self.assertTrue(self.app.save_current_task(show_success=False))
        task_id = self.app.current_task_id
        self.assertIsNotNone(task_id)

        self.app._replace_ocr_text("교사가 저장 후 직접 고친 최신 메모", track_change=True)
        self.app._cancel_autosave()
        older = self._create_read_capture(3, "추가할 첫 캡처 원문")
        newer = self._create_read_capture(4, "추가할 둘째 캡처 원문")
        self._open_inbox_and_select(newer.id, older.id)

        self.app.add_selected_captures_to_current_task()

        self.assertEqual(self.app.current_task_id, task_id)
        source = self.app.ocr_text.get("1.0", "end-1c")
        self.assertIn("교사가 저장 후 직접 고친 최신 메모", source)
        self.assertNotIn("원래 저장된 교사 메모", source)
        self.assertLess(source.index(older.ocr_text), source.index(newer.ocr_text))
        saved = self.task_store.get(task_id)
        self.assertIsNotNone(saved)
        self.assertEqual(saved.source_text, source)
        self.assertEqual(self._linked_capture_ids(), {older.id, newer.id})

    def test_linked_capture_cannot_be_trashed_from_inbox(self) -> None:
        assert self.app is not None
        record = self._create_read_capture(1, "보존할 연결 캡처")
        task = self.task_store.create(title="연결된 업무", source_kind="manual")
        self.capture_store.link_to_task(record.id, task.id)
        self._open_inbox_and_select(record.id)

        with mock.patch.object(
            self.capture_store,
            "trash",
            wraps=self.capture_store.trash,
        ) as trash:
            with mock.patch("ui.app.messagebox.showinfo") as show_info:
                self.app.trash_selected_captures()

        trash.assert_not_called()
        show_info.assert_called()
        visible_ids = {
            item.id for item in self.capture_store.search(link_filter="all")
        }
        self.assertIn(record.id, visible_ids)
        self.assertTrue(record.path.exists())

    def test_unclassified_cleanup_is_soft_trash(self) -> None:
        assert self.app is not None
        record = self._create_read_capture(1, "정리할 미분류 캡처")
        self._open_inbox_and_select(record.id)

        with mock.patch("ui.app.messagebox.askyesno", return_value=True):
            self.app.trash_selected_captures()

        active_ids = {
            item.id for item in self.capture_store.search(link_filter="all")
        }
        trashed_records = self.capture_store.search(link_filter="all", trashed=True)
        trashed_ids = {item.id for item in trashed_records}
        self.assertNotIn(record.id, active_ids)
        self.assertIn(record.id, trashed_ids)
        trashed_record = next(item for item in trashed_records if item.id == record.id)
        self.assertTrue(trashed_record.path.exists())

    def test_late_ocr_is_saved_to_capture_but_never_overwrites_teacher_edit(self) -> None:
        assert self.app is not None
        record = self.capture_store.save(Image.new("RGB", (120, 80), "white"))
        self.app._start_new_workspace(
            source_kind="capture",
            source_name="검수 캡처",
            capture_path=record.path,
            capture_ids=[record.id],
        )
        self.app._replace_ocr_text("OCR 시작 당시 원문", track_change=True)
        operation_id = "late-capture-ocr"
        context_id = self.app.current_context_id
        self.app._active_operation_id = operation_id
        self.app._active_operation_context = context_id
        self.app._active_operation_kind = "ocr"
        self.app._active_operation_revisions = (
            self.app._source_revision,
            self.app._result_revision,
        )

        self.app._replace_ocr_text("교사가 처리 중 고친 최신 원문", track_change=True)
        self.app._worker_results.put(
            (
                operation_id,
                context_id,
                "ocr",
                True,
                "뒤늦게 도착한 정밀 OCR 전문",
                {
                    "capture_id": record.id,
                    "apply_mode": "replace",
                    "ocr_profile": "high-exact-v1",
                },
            )
        )

        with mock.patch("ui.app.messagebox.showinfo") as show_info:
            self.app._drain_worker_results()

        show_info.assert_called_once()
        self.assertEqual(
            self.app.ocr_text.get("1.0", "end-1c"),
            "교사가 처리 중 고친 최신 원문",
        )
        stored = self.capture_store.get(record.id)
        self.assertEqual(stored.ocr_text, "뒤늦게 도착한 정밀 OCR 전문")
        self.assertEqual(stored.ocr_profile, "high-exact-v1")

    def test_failed_reread_keeps_last_successful_capture_text(self) -> None:
        assert self.app is not None
        record = self._create_read_capture(5, "직전 성공 OCR 원문")
        self.app._start_new_workspace(
            source_kind="capture",
            source_name="재인식 캡처",
            capture_path=record.path,
            capture_ids=[record.id],
        )
        self.app._replace_ocr_text("교사가 검수한 원문", track_change=True)
        operation_id = "failed-capture-ocr"
        context_id = self.app.current_context_id
        self.app._active_operation_id = operation_id
        self.app._active_operation_context = context_id
        self.app._active_operation_kind = "ocr"
        self.app._active_operation_revisions = (
            self.app._source_revision,
            self.app._result_revision,
        )
        self.app._worker_results.put(
            (
                operation_id,
                context_id,
                "ocr",
                False,
                "네트워크 연결 실패",
                {
                    "capture_id": record.id,
                    "apply_mode": "review",
                    "ocr_profile": "high-exact-v1",
                },
            )
        )

        with mock.patch("ui.app.messagebox.showerror"):
            self.app._drain_worker_results()

        stored = self.capture_store.get(record.id)
        self.assertEqual(stored.ocr_status, "failed")
        self.assertEqual(stored.ocr_text, "직전 성공 OCR 원문")
        self.assertEqual(
            self.app.ocr_text.get("1.0", "end-1c"),
            "교사가 검수한 원문",
        )

    def test_uncertainty_markers_require_confirmation_before_analysis(self) -> None:
        assert self.app is not None
        self.app._start_new_workspace(source_kind="manual", source_name="직접 입력")
        self.app._replace_ocr_text(
            "제출일: ⟦불확실:9월 2일⟧\n첨부: ⟦판독불가⟧",
            track_change=True,
        )

        with mock.patch("ui.app.messagebox.askyesno", return_value=False) as confirm:
            with mock.patch.object(self.app, "_start_worker_operation") as start:
                self.app.analyze_text()

        confirm.assert_called_once()
        start.assert_not_called()
        self.assertEqual(self.app.notebook.select(), str(self.app.source_tab))

    def test_capture_link_failure_keeps_save_pending_until_retry_succeeds(self) -> None:
        assert self.app is not None
        record = self._create_read_capture(6, "연결 재시도 원문")
        self.app._start_new_workspace(
            source_kind="capture",
            source_name="연결 재시도",
            capture_path=record.path,
            capture_ids=[record.id],
        )
        self.app._replace_ocr_text(record.ocr_text, track_change=True)

        with mock.patch.object(
            self.capture_store,
            "link_to_task",
            side_effect=OSError("metadata locked"),
        ):
            self.assertFalse(self.app.save_current_task(show_success=False))

        task_id = self.app.current_task_id
        self.assertIsNotNone(task_id)
        self.assertTrue(self.app.current_dirty)
        self.assertTrue(self.app._autosave_failed)
        self.assertTrue(self.app.save_task_button.instate(["!disabled"]))
        self.assertEqual(self._linked_capture_ids(), set())

        self.assertTrue(self.app.save_current_task(show_success=False))
        self.assertFalse(self.app.current_dirty)
        self.assertEqual(self._linked_capture_ids(), {record.id})

    def test_add_preserves_exact_existing_suffix_and_skips_duplicate_capture(self) -> None:
        assert self.app is not None
        exact_existing = "교사 표 메모\n1열\t2열  \n\n"
        self.app._start_new_workspace(source_kind="manual", source_name="직접 입력")
        self.app._replace_ocr_text(exact_existing, track_change=True)
        record = self._create_read_capture(7, "추가 캡처 원문")
        self._open_inbox_and_select(record.id)

        self.app.add_selected_captures_to_current_task()

        combined = self.app.ocr_text.get("1.0", "end-1c")
        self.assertTrue(combined.startswith(exact_existing + "\n\n"))
        self.assertEqual(combined.count(record.ocr_text), 1)

        self._open_inbox_and_select(record.id)
        with mock.patch("ui.app.messagebox.showinfo") as show_info:
            self.app.add_selected_captures_to_current_task()

        show_info.assert_called_once()
        self.assertEqual(self.app.ocr_text.get("1.0", "end-1c"), combined)

    def test_trash_view_only_enables_restore(self) -> None:
        assert self.app is not None
        record = self._create_read_capture(8, "휴지통 동작 원문")
        self.capture_store.trash(record.id)
        self.app.show_recent_captures()
        self.app._set_capture_inbox_filter("trash")
        self.app.capture_inbox_tree.selection_set(record.id)
        self.app.update_idletasks()
        self.app._render_capture_inbox_actions()

        self.assertEqual(str(self.app.reopen_button.winfo_manager()), "")
        self.assertEqual(str(self.app.add_capture_button.winfo_manager()), "")
        self.assertEqual(str(self.app.reread_capture_button.winfo_manager()), "")
        self.assertEqual(str(self.app.restore_capture_button.cget("state")), "normal")
        self.assertEqual(str(self.app.delete_capture_button.winfo_manager()), "")

        with mock.patch.object(self.app, "_run_capture_batch_ocr") as run_batch:
            with mock.patch("ui.app.messagebox.showinfo") as show_info:
                self.app.reread_selected_captures()
        run_batch.assert_not_called()
        show_info.assert_called_once()

    def test_delete_task_unlinks_capture_but_keeps_inbox_original(self) -> None:
        assert self.app is not None
        record = self._create_read_capture(9, "삭제 업무의 보존 원문")
        self.app._start_new_workspace(
            source_kind="capture",
            source_name="삭제 테스트",
            capture_path=record.path,
            capture_ids=[record.id],
        )
        self.app._replace_ocr_text(record.ocr_text, track_change=True)
        self.assertTrue(self.app.save_current_task(show_success=False))
        task_id = self.app.current_task_id
        self.app._refresh_task_library(selected_id=task_id)
        self.app.task_tree.selection_set(task_id)

        with mock.patch("ui.app.messagebox.askyesno", return_value=True):
            self.app.delete_selected_task()

        self.assertIsNone(self.task_store.get(task_id))
        self.assertTrue(record.path.exists())
        self.assertIn(
            record.id,
            {
                item.id
                for item in self.capture_store.search(link_filter="unclassified")
            },
        )

    def test_batch_ocr_reads_each_original_at_high_and_preserves_failed_text(self) -> None:
        assert self.app is not None
        first = self._create_read_capture(10, "첫 장의 직전 OCR")
        second = self._create_read_capture(11, "둘째 장의 직전 OCR")
        self.app._start_new_workspace(source_kind="manual", source_name="직접 입력")
        self.app._replace_ocr_text("교사가 검수 중인 업무 원문", track_change=True)

        with mock.patch.object(self.app, "_start_worker_operation") as start:
            with mock.patch(
                "ui.app.extract_text_from_image",
                side_effect=["첫 장 새 정밀 OCR", RuntimeError("둘째 장 판독 실패")],
            ) as extract:
                with mock.patch("ui.app.messagebox.askyesno", return_value=True):
                    self.app._run_capture_batch_ocr([first, second])
                kind, work, metadata = start.call_args.args
                results = work()

        self.assertEqual(kind, "capture_batch")
        self.assertEqual(metadata["apply_mode"], "metadata_only")
        self.assertEqual(extract.call_count, 2)
        for call in extract.call_args_list:
            self.assertEqual(call.kwargs["detail"], "high")
            self.assertTrue(call.kwargs["raise_errors"])

        with mock.patch("ui.app.messagebox.showinfo"):
            self.app._finish_capture_batch_ocr(
                results,
                profile=metadata["ocr_profile"],
                apply_mode=metadata["apply_mode"],
            )

        self.assertEqual(self.capture_store.get(first.id).ocr_text, "첫 장 새 정밀 OCR")
        failed = self.capture_store.get(second.id)
        self.assertEqual(failed.ocr_status, "failed")
        self.assertEqual(failed.ocr_text, "둘째 장의 직전 OCR")
        self.assertEqual(
            self.app.ocr_text.get("1.0", "end-1c"),
            "교사가 검수 중인 업무 원문",
        )

    def test_discarded_batch_callback_cannot_overwrite_newer_capture_ocr(self) -> None:
        assert self.app is not None
        record = self._create_read_capture(12, "더 최신인 OCR 원문")
        old_context = self.app.current_context_id
        self.app._active_operation_id = "new-operation"
        self.app._active_operation_context = "new-context"
        self.app._active_operation_kind = "capture_batch"
        self.app._worker_results.put(
            (
                "discarded-operation",
                old_context,
                "capture_batch",
                True,
                [
                    {
                        "capture_id": record.id,
                        "succeeded": True,
                        "text": "늦게 도착한 오래된 OCR",
                    }
                ],
                {"ocr_profile": "old", "apply_mode": "metadata_only"},
            )
        )

        self.app._drain_worker_results()

        self.assertEqual(
            self.capture_store.get(record.id).ocr_text,
            "더 최신인 OCR 원문",
        )

    def test_batch_cancel_event_stops_before_starting_the_next_api_call(self) -> None:
        assert self.app is not None
        first = self._create_read_capture(13, "첫 장")
        second = self._create_read_capture(14, "둘째 장")
        with mock.patch.object(self.app, "_start_worker_operation") as start:
            with mock.patch("ui.app.messagebox.askyesno", return_value=True):
                self.app._run_capture_batch_ocr([first, second])

        _kind, work, _metadata = start.call_args.args
        cancel_event = start.call_args.kwargs["cancel_event"]

        def finish_first_then_cancel(*_args, **_kwargs) -> str:
            cancel_event.set()
            return "첫 장 OCR"

        with mock.patch(
            "ui.app.extract_text_from_image",
            side_effect=finish_first_then_cancel,
        ) as extract:
            results = work()

        self.assertEqual(extract.call_count, 1)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["capture_id"], first.id)


if __name__ == "__main__":
    unittest.main()
