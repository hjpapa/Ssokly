"""Desk recovery regressions: synthetic temporary DBs and Tk widgets only."""
from pathlib import Path
import tempfile
import threading
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from services.document_library import DocumentLibrary
from ui.capture_desk import CaptureDeskApp, fingerprint


class CaptureDeskRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.library = DocumentLibrary(self.directory)
        for name in ('showerror', 'showwarning', 'showinfo', 'askyesno'):
            patched = patch('ui.capture_desk.messagebox.' + name, return_value=False)
            patched.start()
            self.addCleanup(patched.stop)
        self.app = CaptureDeskApp(library=self.library)
        self.app.withdraw()
        self.addCleanup(self.destroy_app)
        self.app.update()

    def destroy_app(self):
        self.app._closing = True
        for callback in self.app.tk.call('after', 'info'):
            self.app.after_cancel(callback)
        self.app.destroy()

    def document(self):
        document = self.library.create_document('저장 제목')
        page = self.library.add_text_page(document['id'], '저장 원문')
        output = self.library.save_output(document['id'], '요약', '저장 결과', fingerprint('저장 원문'))
        self.assertTrue(self.app.open_document(document['id']))
        self.app.update()
        return document, page, output

    def edit(self, editor, value):
        editor.delete('1.0', 'end')
        editor.insert('1.0', value)
        editor.edit_modified(True)
        editor.event_generate('<<Modified>>')
        self.app.update()
        # Exercise explicit recovery, not a timing-dependent autosave race.
        if self.app._autosave_id:
            self.app.after_cancel(self.app._autosave_id)
            self.app._autosave_id = None

    def edit_both(self):
        self.edit(self.app.source_editor, '미저장 원문')
        self.edit(self.app.output_editor, '미저장 결과')
        self.app.title_var.set('미저장 제목')

    def assert_unsaved_retained(self):
        self.assertEqual(self.app.source_editor.get('1.0', 'end-1c'), '미저장 원문')
        self.assertEqual(self.app.output_editor.get('1.0', 'end-1c'), '미저장 결과')
        self.assertEqual(self.app.title_var.get(), '미저장 제목')
        self.assertTrue(self.app._source_dirty)
        self.assertTrue(self.app._output_dirty)

    @staticmethod
    def descendants(widget):
        for child in widget.winfo_children():
            yield child
            yield from CaptureDeskRecoveryTests.descendants(child)

    def test_raw_capture_in_trash_can_restore_but_legacy_task_cannot_be_deleted(self):
        with Image.new('RGB', (20, 12), 'white') as image:
            capture = self.library.capture_store.save(image)
        self.library.capture_store.trash(capture.id)
        document_id = 'capture:' + capture.id
        self.app.show_trash.set(True)
        self.app.refresh_library()
        self.assertTrue(self.app.document_tree.exists(document_id))
        self.assertTrue(self.app.open_document(document_id))
        self.assertTrue(self.app.document['readonly'])
        self.app.toggle_trash()
        self.assertIsNone(self.library.capture_store.get(capture.id).trashed_at)
        self.assertTrue(capture.path.is_file())
        self.assertIn(document_id, {item['id'] for item in self.library.list_documents()})
        task = self.library.task_store.create(title='기존 합성 업무', source_text='기존 원문')
        self.assertTrue(self.app.open_document(task.id))
        with patch.object(self.library, 'trash') as trash:
            self.app.toggle_trash()
            trash.assert_not_called()
        self.assertEqual(self.library.task_store.get(task.id), task)

    def test_trashed_managed_document_is_readonly_until_restored(self):
        document, page, output = self.document()
        self.library.trash(document['id'])
        self.assertTrue(self.app.open_document(document['id']))
        self.assertEqual(str(self.app.source_editor['state']), 'disabled')
        self.assertEqual(str(self.app.output_editor['state']), 'disabled')
        self.assertEqual(str(self.app.title_entry['state']), 'readonly')
        self.app.source_editor.insert('end', '변경 금지')
        self.app.output_editor.insert('end', '변경 금지')
        self.assertFalse(self.app._source_dirty)
        self.assertFalse(self.app._output_dirty)
        self.app.toggle_trash()
        self.assertFalse(self.library.get_document(document['id'])['trashed'])
        self.assertEqual(self.library.pages(document['id'])[0]['text'], page['text'])
        self.assertEqual(self.library.outputs(document['id'])[0]['text'], output['text'])

    def test_history_selection_refreshes_snapshot_after_saving_current_edits(self):
        document, _page, output = self.document()
        self.edit(self.app.output_editor, '방금 편집한 최신 결과')
        window = self.app.show_output_history()
        self.assertIsNotNone(window)
        listing = next(widget for widget in self.descendants(window) if isinstance(widget, tk.Listbox))
        listing.selection_set(0)
        button = next(widget for widget in self.descendants(window) if isinstance(widget, ttk.Button) and widget['text'] == '열기')
        button.invoke()
        self.app.update()
        current = self.library.outputs(document['id'])[0]
        self.assertEqual(current['id'], output['id'])
        self.assertEqual(current['text'], '방금 편집한 최신 결과')
        self.assertEqual(self.app.output_editor.get('1.0', 'end-1c'), current['text'])
        self.assertEqual(self.app.output['updated_at'], current['updated_at'])
        self.edit(self.app.output_editor, '계속 편집한 결과')
        self.assertTrue(self.app.flush_edits())
        self.assertEqual(self.library.outputs(document['id'])[0]['text'], '계속 편집한 결과')

    def test_reload_saved_approval_discards_only_local_drafts_and_loads_latest_values(self):
        document, page, output = self.document()
        other = DocumentLibrary(self.directory)
        other.save_page_text(page['id'], '다른 창 최신 원문', page['updated_at'])
        other.update_output(output['id'], '다른 창 최신 결과', output['updated_at'])
        other.rename(document['id'], '다른 창 최신 제목')
        self.edit_both()
        with patch('ui.capture_desk.messagebox.askyesno', return_value=True) as confirm:
            self.app.reload_saved()
            confirm.assert_called_once()
        self.assertEqual(self.app.source_editor.get('1.0', 'end-1c'), '다른 창 최신 원문')
        self.assertEqual(self.app.output_editor.get('1.0', 'end-1c'), '다른 창 최신 결과')
        self.assertEqual(self.app.title_var.get(), '다른 창 최신 제목')
        self.assertFalse(self.app._source_dirty)
        self.assertFalse(self.app._output_dirty)
        self.assertEqual(other.document_text(document['id']), '다른 창 최신 원문')
        self.assertEqual(other.outputs(document['id'])[0]['text'], '다른 창 최신 결과')

    def test_reload_saved_rejection_preserves_unsaved_inputs_and_database(self):
        document, page, output = self.document()
        self.edit_both()
        with patch('ui.capture_desk.messagebox.askyesno', return_value=False) as confirm:
            self.app.reload_saved()
            confirm.assert_called_once()
        self.assert_unsaved_retained()
        self.assertEqual(self.library.pages(document['id'])[0], page)
        self.assertEqual(self.library.outputs(document['id'])[0], output)
        self.assertEqual(self.library.get_document(document['id'])['title'], '저장 제목')

    def test_reload_read_failure_preserves_drafts_flags_and_saved_tokens(self):
        document, page, output = self.document()
        self.edit_both()
        for method in ('get_document', 'pages', 'outputs', 'get_setting'):
            with self.subTest(method=method):
                with patch('ui.capture_desk.messagebox.askyesno', return_value=True), \
                        patch.object(self.library, method, side_effect=OSError('synthetic read fault')):
                    self.app.reload_saved()
                self.assert_unsaved_retained()
                self.assertEqual(self.app.page['id'], page['id'])
                self.assertEqual(self.app.page['updated_at'], page['updated_at'])
                self.assertEqual(self.app.output['id'], output['id'])
                self.assertEqual(self.app.output['updated_at'], output['updated_at'])
        self.assertEqual(self.library.document_text(document['id']), '저장 원문')
        self.assertEqual(self.library.outputs(document['id'])[0]['text'], '저장 결과')

    def test_external_title_change_is_preserved_when_current_window_did_not_edit_title(self):
        document, _page, _output = self.document()
        self.edit(self.app.source_editor, '현재 창 원문 수정')
        other = DocumentLibrary(self.directory)
        other.rename(document['id'], '다른 창 최신 제목')
        self.assertEqual(self.app.title_var.get(), '저장 제목')
        self.assertTrue(self.app.flush_edits())
        self.assertEqual(self.app.title_var.get(), '다른 창 최신 제목')
        self.assertEqual(self.app.document['title'], '다른 창 최신 제목')
        self.assertEqual(other.get_document(document['id'])['title'], '다른 창 최신 제목')
        self.assertEqual(other.document_text(document['id']), '현재 창 원문 수정')
        self.assertTrue(self.app.flush_edits())
        self.assertEqual(other.get_document(document['id'])['title'], '다른 창 최신 제목')

    def test_concurrent_title_edits_raise_conflict_without_saving_other_local_drafts(self):
        document, page, output = self.document()
        self.edit_both()
        other = DocumentLibrary(self.directory)
        other.rename(document['id'], '다른 창이 먼저 저장한 제목')
        self.assertFalse(self.app.flush_edits())
        self.assert_unsaved_retained()
        self.assertEqual(other.get_document(document['id'])['title'], '다른 창이 먼저 저장한 제목')
        self.assertEqual(other.pages(document['id'])[0], page)
        self.assertEqual(other.outputs(document['id'])[0], output)
        self.assertEqual(self.app.document['title'], '저장 제목')

    def test_other_page_background_ocr_is_loaded_fresh_on_page_selection(self):
        document, first, _output = self.document()
        with Image.new('RGB', (24, 16), 'white') as image:
            capture = self.library.capture_store.save(image)
        self.library.capture_store.update_ocr(capture.id, '둘째 쪽 이전 OCR')
        second = self.library.add_capture(document['id'], capture.id)
        self.assertTrue(self.app.open_document(document['id']))
        self.assertEqual(self.app.page['id'], first['id'])
        identifier = 'synthetic-background-ocr'
        self.app._jobs[identifier] = {'kind': 'ocr', 'cancel': threading.Event(),
            'document_id': document['id'], 'page_id': second['id'], 'capture_id': capture.id,
            'snapshot': None, 'scopes': {}}
        self.app._results.put((identifier, True, '둘째 쪽 최신 OCR'))
        with patch.object(self.app, '_scopes_match', return_value=True), \
                patch.object(self.app, '_transfer', return_value=Mock()):
            self.app._poll_results()
        self.assertNotIn(identifier, self.app._jobs)
        self.assertEqual(self.library.pages(document['id'])[1]['text'], '둘째 쪽 최신 OCR')
        self.assertEqual(self.app.page_records[1]['text'], '둘째 쪽 이전 OCR')
        self.assertEqual(self.app.source_editor.get('1.0', 'end-1c'), first['text'])
        self.app.page_selector.current(1)
        self.app._page_selected()
        self.assertEqual(self.app.page['id'], second['id'])
        self.assertEqual(self.app.source_editor.get('1.0', 'end-1c'), '둘째 쪽 최신 OCR')
        self.assertEqual(self.app.page['updated_at'], self.library.pages(document['id'])[1]['updated_at'])
        self.edit(self.app.source_editor, '둘째 쪽 교사 수정')
        self.assertTrue(self.app.flush_edits())
        self.assertEqual(self.library.pages(document['id'])[1]['text'], '둘째 쪽 교사 수정')


if __name__ == '__main__':
    unittest.main()
