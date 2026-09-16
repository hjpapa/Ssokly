"""Offline acceptance flows; temporary data, mock AI, no real clipboard."""
from pathlib import Path
import tempfile
import threading
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import patch

from PIL import Image

from services.document_library import DocumentLibrary
from services.transfer_policy import make_image_snapshot, make_text_snapshot
from ui.capture_desk import CaptureDeskApp, fingerprint


class CaptureAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        for target in ('socket.socket.connect', 'socket.create_connection', 'socket.getaddrinfo'):
            guard = patch(target, side_effect=AssertionError('network forbidden in synthetic acceptance'))
            mock = guard.start()
            self.addCleanup(guard.stop)
            self.addCleanup(mock.assert_not_called)
        for name in ('showerror', 'showwarning', 'showinfo', 'askyesno'):
            guard = patch('ui.capture_desk.messagebox.' + name, return_value=False)
            guard.start()
            self.addCleanup(guard.stop)
        self.library = DocumentLibrary(self.directory)
        self.app = CaptureDeskApp(library=self.library)
        self.app.withdraw()
        self.addCleanup(self.destroy_app)
        self.app.update()
        self.serial = 0

    def destroy_app(self):
        if self.app is None:
            return
        self.app._closing = True
        for callback in self.app.tk.call('after', 'info'):
            self.app.after_cancel(callback)
        self.app.destroy()
        self.app = None

    def edit(self, editor, text):
        editor.delete('1.0', 'end')
        editor.insert('1.0', text)
        editor.edit_modified(True)
        editor.event_generate('<<Modified>>')
        self.app.update()
        if self.app._autosave_id:
            self.app.after_cancel(self.app._autosave_id)
            self.app._autosave_id = None

    def page(self, index):
        self.app.page_selector.current(index)
        self.app._page_selected()
        self.app.update()

    @staticmethod
    def choose(**kwargs):
        if kwargs['kind'] == 'image':
            return make_image_snapshot(kwargs['image'])
        return make_text_snapshot(kwargs['text'])

    def queue_mock_job(self, kind, operation, metadata):
        # Invoke the patched service synchronously, retain the production result
        # queue/persistence path, and let the test decide when to deliver it.
        self.serial += 1
        identifier = 'synthetic-job-' + str(self.serial)
        cancel = threading.Event()
        self.app._jobs[identifier] = {**metadata, 'kind': kind, 'cancel': cancel}
        self.app._results.put((identifier, True, operation(cancel)))
        return identifier

    @staticmethod
    def buttons(widget):
        for child in widget.winfo_children():
            if isinstance(child, ttk.Button):
                yield child
            yield from CaptureAcceptanceTests.buttons(child)

    def test_capture_bundle_ocr_edit_reorder_restart_search_copy_and_full_ai_input(self):
        pages = []
        for color in ('red', 'green', 'blue'):
            with Image.new('RGB', (28, 18), color) as image:
                pages.append(self.app.accept_capture(image, document_id=self.app.document['id'] if self.app.document else None,
                                                     auto_read=False))
        document_id = self.app.document['id']
        images = {page['path']: Path(page['path']).read_bytes() for page in pages}
        raw = ['첫째 OCR\t값\t', '둘째 OCR 삭제 예정', '셋째 OCR 유지']
        with patch.object(self.app, '_choose_transfer', side_effect=self.choose), \
                patch.object(self.app, '_start_job', side_effect=self.queue_mock_job), \
                patch('services.ocr_service.extract_text_from_image', side_effect=raw) as ocr:
            for index in range(3):
                self.page(index)
                self.app.read_current_page()
                self.app._poll_results()
            self.assertEqual(ocr.call_count, 3)
        revised = '구분\t값\t\n첫째\t교사 변경\t'
        self.page(0)
        self.edit(self.app.source_editor, revised)
        self.assertTrue(self.app.flush_edits())
        self.page(1)
        self.edit(self.app.source_editor, '')
        self.assertTrue(self.app.flush_edits())
        self.page(2)
        self.app.move_page(-1)
        self.app.move_page(-1)
        expected = raw[2] + '\n\n' + revised
        self.assertEqual(self.library.document_text(document_id), expected)
        self.destroy_app()
        self.library = DocumentLibrary(self.directory)
        self.app = CaptureDeskApp(library=self.library)
        self.app.withdraw()
        self.app.query.set('교사 변경')
        self.app.refresh_library()
        self.assertTrue(self.app.document_tree.exists(document_id))
        self.app.document_tree.selection_set(document_id)
        self.app._document_selected()
        self.app.update()
        restored = self.library.pages(document_id)
        self.assertEqual([page['id'] for page in restored], [pages[2]['id'], pages[0]['id'], pages[1]['id']])
        self.assertEqual([page['text'] for page in restored], [raw[2], revised, ''])
        self.assertEqual(restored[2]['ocr_text'], raw[1])
        with patch.object(self.app, 'clipboard_clear'), patch.object(self.app, 'clipboard_append') as copied:
            self.app.source_editor.tag_add('sel', '1.0', '1.2')
            self.app.copy_selection()
            copied.assert_called_with('셋째')
            self.app.copy_page()
            copied.assert_called_with(raw[2])
            self.app.copy_document()
            copied.assert_called_with(expected)
        with patch.object(self.app, '_choose_transfer', side_effect=self.choose), \
                patch.object(self.app, '_start_job', side_effect=self.queue_mock_job), \
                patch('services.text_actions.generate_text_action', return_value='합성 전체 요약') as generate:
            self.app.generate()
            generate.assert_called_once()
            self.assertEqual(generate.call_args.args[0], expected)
            self.app._poll_results()
        self.assertEqual(self.library.outputs(document_id)[0]['text'], '합성 전체 요약')
        self.assertEqual(self.library.document_text(document_id), expected)
        for path, content in images.items():
            self.assertEqual(Path(path).read_bytes(), content)
        self.assertFalse((self.directory / 'work_cards.sqlite3').exists())

    def test_selected_ai_input_and_late_result_preserve_manual_output(self):
        document = self.library.create_document('선택 정리 합성')
        text = '앞부분 선택부분 뒷부분'
        self.library.add_text_page(document['id'], text)
        self.library.add_text_page(document['id'], '다른 페이지의 내용')
        original = self.library.save_output(document['id'], '요약', '기존 결과', fingerprint(self.library.document_text(document['id'])))
        self.assertTrue(self.app.open_document(document['id']))
        self.app.update()
        start = text.index('선택부분')
        self.app.source_editor.tag_add('sel', f'1.{start}', f'1.{start + len("선택부분")}')
        self.app.selection_only.set(True)
        self.app.after_cancel(self.app._poll_id)
        self.app._poll_id = None  # Deliver the queued result only after the edit.
        with patch.object(self.app, '_choose_transfer', side_effect=self.choose), \
                patch.object(self.app, '_start_job', side_effect=self.queue_mock_job), \
                patch('services.text_actions.generate_text_action', return_value='늦게 온 선택 요약') as generate:
            self.app.generate()
            generate.assert_called_once()
            self.assertEqual(generate.call_args.args[0], '선택부분')
            self.edit(self.app.output_editor, '교사가 작성한 결과')
            self.assertTrue(self.app.flush_edits())
            self.app._poll_results()
        self.assertEqual(self.app.output['id'], original['id'])
        self.assertEqual(self.app.output_editor.get('1.0', 'end-1c'), '교사가 작성한 결과')
        results = self.library.outputs(document['id'])
        self.assertEqual(results[0]['text'], '늦게 온 선택 요약')
        self.assertIn('선택 부분', results[0]['mode'])
        self.assertEqual(next(item['text'] for item in results if item['id'] == original['id']), '교사가 작성한 결과')
        self.assertEqual(self.library.document_text(document['id']), text + '\n\n다른 페이지의 내용')

    def test_long_table_large_image_and_minimum_window_keep_edit_copy_controls_accessible(self):
        with Image.new('RGB', (3200, 2400), 'white') as image:
            page = self.app.accept_capture(image, auto_read=False)
        document_id = self.app.document['id']
        text = '구분\t기한\t\t메모\n' + '\n'.join(f'합성 행사 {i:03d}\t기한 미정\t\t확인 메모' for i in range(350))
        self.library.capture_store.update_ocr(page['capture_id'], text)
        self.assertTrue(self.app.open_document(document_id))
        self.app.attributes('-alpha', 0.0)
        self.app.deiconify()
        self.app.geometry('720x560')
        self.app.editor_tabs.select(self.app.text_panel)
        self.app.update()
        self.assertEqual((self.app.winfo_width(), self.app.winfo_height()), (720, 560))

        def inside(widget):
            self.assertTrue(widget.winfo_ismapped(), str(widget))
            self.assertGreater(widget.winfo_width(), 1)
            self.assertGreater(widget.winfo_height(), 1)
            x = widget.winfo_rootx() - self.app.winfo_rootx()
            y = widget.winfo_rooty() - self.app.winfo_rooty()
            self.assertGreaterEqual(x, 0)
            self.assertGreaterEqual(y, 0)
            self.assertLessEqual(x + widget.winfo_width(), self.app.winfo_width())
            self.assertLessEqual(y + widget.winfo_height(), self.app.winfo_height())

        for widget in (self.app.capture_button, self.app.add_button, self.app.title_entry,
                       self.app.view_button, self.app.read_button, self.app.source_editor,
                       *self.buttons(self.app.text_panel)):
            inside(widget)
        revised = text + '\n마지막 교사 메모\t\t\t'
        self.edit(self.app.source_editor, revised)
        self.app.source_editor.see('end')
        self.assertTrue(self.app.flush_edits())
        with patch.object(self.app, 'clipboard_clear'), patch.object(self.app, 'clipboard_append') as copied:
            self.app.copy_page()
            copied.assert_called_with(revised)
            self.app.copy_document()
            copied.assert_called_with(revised)
            self.app.editor_tabs.select(self.app.table_panel)
            self.app._tab_changed()
            self.app.table_view.copy_table()
            copied.assert_called_with(revised)
        self.app.editor_tabs.select(self.app.text_panel)
        self.app.toggle_compact_view()
        self.app.update()
        self.assertEqual(self.app.image_view.image_size, (3200, 2400))
        self.app.image_view.original_size()
        self.app.update()
        inside(self.app.image_view.canvas)
        for button in self.buttons(self.app.image_view):
            inside(button)
        self.app.toggle_compact_view()
        self.app.editor_tabs.select(self.app.ai_panel)
        self.app.update()
        for widget in (self.app.generate_button, self.app.output_editor, *self.buttons(self.app.ai_panel)):
            inside(widget)
        self.assertEqual(self.library.document_text(document_id), revised)
        self.assertTrue(Path(page['path']).is_file())


if __name__ == '__main__':
    unittest.main()
