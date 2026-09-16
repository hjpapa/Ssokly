"""Capture-first desktop regressions using only temporary, synthetic records."""
from pathlib import Path
import tempfile
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import patch

from PIL import Image

from services.document_library import DocumentLibrary
from ui.capture_desk import CaptureDeskApp, fingerprint


class CaptureDeskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.library = DocumentLibrary(self.directory)
        for name in ('showerror', 'showwarning', 'showinfo', 'askyesno'):
            mock = patch('ui.capture_desk.messagebox.' + name, return_value=False)
            mock.start()
            self.addCleanup(mock.stop)
        self.app = CaptureDeskApp(library=self.library)
        self.app.withdraw()
        self.addCleanup(self.destroy_app)
        self.app.update()

    def destroy_app(self):
        if self.app is not None:
            self.app._closing = True
            for callback in self.app.tk.call('after', 'info'):
                self.app.after_cancel(callback)
            self.app.destroy()
            self.app = None

    def document(self, *texts, title='합성 문서'):
        document = self.library.create_document(title)
        for index, text in enumerate(texts):
            self.library.add_text_page(document['id'], text, source_name=f'합성 {index + 1}쪽')
        return document

    def open(self, document):
        self.assertTrue(self.app.open_document(document['id']))
        self.app.update()

    def edit(self, editor, text):
        editor.delete('1.0', 'end')
        editor.insert('1.0', text)
        editor.edit_modified(True)
        editor.event_generate('<<Modified>>')
        self.app.update()

    def autosave(self):
        self.assertIsNotNone(self.app._autosave_id)
        self.app.after_cancel(self.app._autosave_id)
        self.app._autosave_id = None
        self.app._autosave()

    def test_new_text_document_and_autosave_explicit_empty_value(self):
        self.app.new_text_document()
        self.app.update()
        self.assertEqual(len(self.app.page_records), 1)
        self.assertEqual(str(self.app.source_editor['state']), 'normal')
        self.edit(self.app.source_editor, '교사 수정\t\t끝\n')
        self.assertTrue(self.app._source_dirty)
        self.autosave()
        self.assertEqual(self.library.document_text(self.app.document['id']), '교사 수정\t\t끝\n')
        self.edit(self.app.source_editor, '')
        self.assertTrue(self.app._source_dirty)
        self.autosave()
        self.assertFalse(self.app._source_dirty)
        self.assertEqual(self.library.pages(self.app.document['id'])[0]['text'], '')
        self.open(self.app.document)
        self.assertEqual(self.app.source_editor.get('1.0', 'end-1c'), '')

    def test_output_autosave_blank_survives_reopen_without_changing_source(self):
        document = self.document('합성 원문')
        output = self.library.save_output(document['id'], '요약', '이전 합성 결과', fingerprint('합성 원문'))
        self.open(document)
        self.assertEqual(self.app.output['id'], output['id'])
        self.edit(self.app.output_editor, '')
        self.assertTrue(self.app._output_dirty)
        self.autosave()
        self.open(document)
        self.assertEqual(self.app.output_editor.get('1.0', 'end-1c'), '')
        self.assertEqual(self.library.outputs(document['id'])[0]['text'], '')
        self.assertEqual(self.library.document_text(document['id']), '합성 원문')

    def test_source_save_failure_blocks_document_page_new_document_and_close(self):
        document = self.document('첫 쪽', '둘째 쪽')
        other = self.document('다른 문서')
        self.open(document)
        page_id = self.app.page['id']
        self.edit(self.app.source_editor, '미저장 입력')
        with patch.object(self.library, 'save_page_text', side_effect=OSError('synthetic write fault')):
            self.assertFalse(self.app.open_document(other['id']))
            self.assertEqual(self.app.document['id'], document['id'])
            self.app.page_selector.current(1)
            self.app._page_selected()
            self.assertEqual(self.app.page['id'], page_id)
            self.assertEqual(self.app.page_selector.current(), 0)
            with patch.object(self.library, 'create_document') as create:
                self.app.new_text_document()
                create.assert_not_called()
            self.assertFalse(self.app.close())
        self.assertEqual(self.app.source_editor.get('1.0', 'end-1c'), '미저장 입력')
        self.assertTrue(self.app._source_dirty)
        self.assertEqual(self.library.pages(document['id'])[0]['text'], '첫 쪽')

    def test_output_save_failure_blocks_page_switch_and_preserves_editor(self):
        document = self.document('첫 쪽', '둘째 쪽')
        self.library.save_output(document['id'], '안내문', '저장된 결과', fingerprint('첫 쪽\n\n둘째 쪽'))
        self.open(document)
        self.edit(self.app.output_editor, '미저장 안내')
        with patch.object(self.library, 'update_output', side_effect=OSError('synthetic output fault')):
            self.app.page_selector.current(1)
            self.app._page_selected()
        self.assertEqual(self.app.page_selector.current(), 0)
        self.assertEqual(self.app.output_editor.get('1.0', 'end-1c'), '미저장 안내')
        self.assertTrue(self.app._output_dirty)

    def test_concurrent_source_save_keeps_both_new_saved_value_and_unsaved_editor(self):
        document = self.document('최초 원문')
        self.open(document)
        token = self.app.page['updated_at']
        page_id = self.app.page['id']
        self.edit(self.app.source_editor, '현재 창의 미저장 수정')
        other = DocumentLibrary(self.directory)
        other.save_page_text(page_id, '다른 창이 저장한 수정', expected_updated_at=token)
        self.assertFalse(self.app.flush_edits())
        self.assertEqual(self.app.source_editor.get('1.0', 'end-1c'), '현재 창의 미저장 수정')
        self.assertTrue(self.app._source_dirty)
        self.assertEqual(self.library.document_text(document['id']), '다른 창이 저장한 수정')

    def test_page_switch_and_reorder_save_edits_and_keep_selected_identity(self):
        document = self.document('첫 쪽', '둘째 쪽')
        self.open(document)
        first_id = self.app.page['id']
        self.edit(self.app.source_editor, '첫 쪽 수정')
        self.app.page_selector.current(1)
        self.app._page_selected()
        self.assertEqual(self.app.source_editor.get('1.0', 'end-1c'), '둘째 쪽')
        second_id = self.app.page['id']
        self.edit(self.app.source_editor, '둘째 쪽 수정')
        self.app.move_page(-1)
        self.assertEqual(self.app.page['id'], second_id)
        self.assertEqual([page['id'] for page in self.library.pages(document['id'])], [second_id, first_id])
        self.assertEqual(self.library.document_text(document['id']), '둘째 쪽 수정\n\n첫 쪽 수정')

    def test_capture_adds_pages_and_keeps_images_without_automatic_request(self):
        with Image.new('RGB', (36, 20), 'red') as first, Image.new('RGB', (22, 40), 'blue') as second:
            first_page = self.app.accept_capture(first, auto_read=False)
            self.assertIsNotNone(first_page)
            document_id = self.app.document['id']
            second_page = self.app.accept_capture(second, document_id=document_id, auto_read=False)
        self.assertIsNotNone(second_page)
        self.assertEqual(len(self.library.pages(document_id)), 2)
        self.assertEqual(self.app.page['id'], second_page['id'])
        self.assertEqual(self.app.image_view.image_size, (22, 40))
        self.assertFalse(self.app._jobs)
        for page in self.library.pages(document_id):
            self.assertTrue(Path(page['path']).is_file())

    def test_copy_selection_page_document_and_table_keep_exact_empty_cells(self):
        document = self.document('구분\t값\t\n행사\t\t비고', '다음 쪽\t')
        self.open(document)
        self.edit(self.app.source_editor, '구분\t값\t\n↳ 행사\t\t비고')
        with patch.object(self.app, 'clipboard_clear') as clear, patch.object(self.app, 'clipboard_append') as append:
            self.app.source_editor.tag_add('sel', '2.0', '2.end')
            self.app.copy_selection()
            append.assert_called_with('↳ 행사\t\t비고')
            self.app.copy_page()
            append.assert_called_with('구분\t값\t\n↳ 행사\t\t비고')
            self.app.copy_document()
            append.assert_called_with('구분\t값\t\n↳ 행사\t\t비고\n\n다음 쪽\t')
            self.app.editor_tabs.select(self.app.table_panel)
            self.app._tab_changed()
            self.app.table_view.copy_table()
            append.assert_called_with('구분\t값\t\n↳ 행사\t\t비고')
            self.assertEqual(clear.call_count, 4)

    def test_copy_document_does_not_copy_stale_saved_text_after_failure(self):
        document = self.document('저장된 본문')
        self.open(document)
        self.edit(self.app.source_editor, '저장되지 않은 본문')
        with patch.object(self.library, 'save_page_text', side_effect=OSError('synthetic fault')):
            with patch.object(self.app, 'clipboard_clear') as clear, patch.object(self.app, 'clipboard_append') as append:
                self.app.copy_document()
                clear.assert_not_called()
                append.assert_not_called()

    def test_title_autosave_and_restart_restore_document_text_and_output(self):
        document = self.document('합성 원문')
        self.library.save_output(document['id'], '요약', '합성 결과', fingerprint('합성 원문'))
        self.open(document)
        self.app.title_var.set('고친 문서 제목')
        self.app._schedule_save()
        self.autosave()
        self.destroy_app()
        reopened = DocumentLibrary(self.directory)
        self.app = CaptureDeskApp(library=reopened)
        self.app.withdraw()
        self.open(document)
        self.assertEqual(self.app.title_var.get(), '고친 문서 제목')
        self.assertEqual(self.app.source_editor.get('1.0', 'end-1c'), '합성 원문')
        self.assertEqual(self.app.output_editor.get('1.0', 'end-1c'), '합성 결과')

    def test_legacy_task_is_readonly_and_adoption_preserves_original_database(self):
        task = self.library.task_store.create(title='이전 합성 업무', source_text='교사 통합 원문\t',
                                              analysis_text='이전 실행안')
        before = self.library.task_store.db_path.read_bytes()
        self.assertTrue(self.app.open_document(task.id))
        self.assertEqual(str(self.app.source_editor['state']), 'disabled')
        self.assertEqual(str(self.app.output_editor['state']), 'disabled')
        self.assertEqual(self.app.output_editor.get('1.0', 'end-1c'), '이전 실행안')
        self.app.source_editor.insert('end', '변경 금지')
        self.assertEqual(self.app.source_editor.get('1.0', 'end-1c'), '교사 통합 원문\t')
        self.assertEqual(self.library.task_store.db_path.read_bytes(), before)
        self.app.adopt_current()
        self.assertNotEqual(self.app.document['id'], task.id)
        self.assertFalse(self.app.document['readonly'])
        self.assertEqual(self.library.document_text(self.app.document['id']), '교사 통합 원문\t')
        self.assertEqual(self.library.outputs(self.app.document['id']), [])
        # Adoption intentionally adds a compatibility task to the same DB;
        # the pre-existing record, not the entire DB file, must stay unchanged.
        self.assertEqual(self.library.task_store.get(task.id), task)
        self.assertFalse((self.directory / 'work_cards.sqlite3').exists())
        self.assertFalse((self.directory / 'personal_todos.sqlite3').exists())

    def test_legacy_capture_adoption_keeps_teacher_empty_text_and_raw_image(self):
        with Image.new('RGB', (30, 20), 'green') as image:
            capture = self.library.capture_store.save(image)
        self.library.capture_store.update_ocr(capture.id, '합성 원래 OCR')
        capture = self.library.capture_store.save_verified_text(capture.id, '')
        before_image = capture.path.read_bytes()
        self.assertTrue(self.app.open_document('capture:' + capture.id))
        self.assertTrue(self.app.document['readonly'])
        self.app.adopt_current()
        self.assertFalse(self.app.document['readonly'])
        self.assertEqual(self.app.page['capture_id'], capture.id)
        self.assertEqual(self.app.source_editor.get('1.0', 'end-1c'), '')
        self.assertEqual(self.app.page['ocr_text'], '합성 원래 OCR')
        self.assertEqual(capture.path.read_bytes(), before_image)

    def test_empty_document_opens_without_invalid_page_selection(self):
        document = self.document()
        self.assertTrue(self.app.open_document(document['id']))
        self.assertIsNone(self.app.page)
        self.assertEqual(self.app.page_selector.current(), -1)
        self.assertEqual(str(self.app.source_editor['state']), 'disabled')

    def test_compact_and_wide_layout_switch_without_changing_document(self):
        document = self.document('합성 원문')
        self.open(document)
        # Hidden roots do not receive all native geometry notifications. Drive
        # the layout width explicitly; no screenshots or actual screen access.
        self.app.geometry('720x600')
        with patch.object(self.app, 'winfo_width', return_value=720):
            self.app._apply_layout()
        self.assertTrue(self.app._layout_compact)
        self.assertEqual(tuple(map(str, self.app.work_split.panes())), (str(self.app.editor_panel),))
        self.assertNotIn(str(self.app.library_panel), tuple(map(str, self.app.main_split.panes())))
        with patch.object(self.app, 'winfo_width', return_value=720):
            self.app.toggle_library()
            self.assertIn(str(self.app.library_panel), tuple(map(str, self.app.main_split.panes())))
            self.app.toggle_library()
            self.assertNotIn(str(self.app.library_panel), tuple(map(str, self.app.main_split.panes())))
        with patch.object(self.app, 'winfo_width', return_value=720):
            self.app.toggle_compact_view()
        self.assertEqual(tuple(map(str, self.app.work_split.panes())), (str(self.app.image_panel),))
        self.app.geometry('1280x800')
        with patch.object(self.app, 'winfo_width', return_value=1280):
            self.app._apply_layout()
        self.assertFalse(self.app._layout_compact)
        self.assertEqual(tuple(map(str, self.app.work_split.panes())),
                         (str(self.app.image_panel), str(self.app.editor_panel)))
        self.assertIn(str(self.app.library_panel), tuple(map(str, self.app.main_split.panes())))
        self.assertEqual(self.app.document['id'], document['id'])
        self.assertEqual(self.app.source_editor.get('1.0', 'end-1c'), '합성 원문')

    def test_actual_tk_geometry_keeps_primary_controls_visible_at_both_sizes(self):
        self.open(self.document('합성 원문'))
        # This is our transparent test window only, not a desktop screenshot.
        self.app.attributes('-alpha', 0.0)
        self.app.deiconify()

        def within_root(widget):
            self.assertTrue(widget.winfo_ismapped(), str(widget))
            self.assertGreater(widget.winfo_width(), 1, str(widget))
            self.assertGreater(widget.winfo_height(), 1, str(widget))
            x = widget.winfo_rootx() - self.app.winfo_rootx()
            y = widget.winfo_rooty() - self.app.winfo_rooty()
            self.assertGreaterEqual(x, 0, str(widget))
            self.assertGreaterEqual(y, 0, str(widget))
            self.assertLessEqual(x + widget.winfo_width(), self.app.winfo_width(), str(widget))
            self.assertLessEqual(y + widget.winfo_height(), self.app.winfo_height(), str(widget))

        def buttons(widget):
            for child in widget.winfo_children():
                if isinstance(child, ttk.Button):
                    yield child
                yield from buttons(child)

        for geometry, expected in (('720x600', (720, 600)), ('1280x800', (1280, 800))):
            with self.subTest(geometry=geometry):
                self.app.geometry(geometry)
                self.app.editor_tabs.select(self.app.text_panel)
                self.app.update()
                self.assertEqual((self.app.winfo_width(), self.app.winfo_height()), expected)
                for widget in (self.app.capture_button, self.app.add_button, self.app.cancel_button,
                               self.app.title_entry, self.app.view_button, self.app.read_button):
                    with self.subTest(widget=str(widget)):
                        within_root(widget)
                for button in buttons(self.app.text_panel):
                    with self.subTest(button=button['text']):
                        within_root(button)
                self.app.editor_tabs.select(self.app.ai_panel)
                self.app.update()
                for widget in (self.app.generate_button, self.app.output_editor, *buttons(self.app.ai_panel)):
                    with self.subTest(ai_widget=str(widget)):
                        within_root(widget)
                if expected[0] == 720:
                    self.app.editor_tabs.select(self.app.text_panel)
                    self.app.toggle_library()
                    self.app.update()
                    for widget in (self.app.document_tree, self.app.view_button, self.app.read_button):
                        with self.subTest(compact_library_widget=str(widget)):
                            within_root(widget)
                    self.app.toggle_library()
                    self.app.toggle_compact_view()
                    self.app.update()
                    within_root(self.app.image_view.canvas)
                    for button in buttons(self.app.image_view):
                        with self.subTest(image_button=button['text']):
                            within_root(button)
                    self.app.toggle_compact_view()
        self.app.withdraw()


if __name__ == '__main__':
    unittest.main()
