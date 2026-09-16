"""DPI/sash and unreadable-page regressions; synthetic records and no network."""
from pathlib import Path
from contextlib import closing
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from services.document_library import DocumentLibrary
from ui.capture_desk import CaptureDeskApp


class CaptureDeskLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.library = DocumentLibrary(Path(self.temp.name))
        for target in ('socket.socket.connect', 'socket.getaddrinfo'):
            blocked = patch(target, side_effect=AssertionError('network forbidden'))
            blocked.start()
            self.addCleanup(blocked.stop)
        self.app = None
        self.addCleanup(self.destroy_app)

    def create_app(self, scaling=1.67):
        self.destroy_app()
        build = CaptureDeskApp._build_ui

        def scaled_build(app):
            app._test_old_scaling = app.tk.call('tk', 'scaling')
            app.tk.call('tk', 'scaling', scaling)
            build(app)

        with patch.object(CaptureDeskApp, '_build_ui', scaled_build):
            self.app = CaptureDeskApp(library=self.library)
        self.app.attributes('-alpha', 0.0)
        self.app.update()
        return self.app

    def destroy_app(self):
        if self.app is not None:
            self.app._closing = True
            for callback in self.app.tk.call('after', 'info'):
                self.app.after_cancel(callback)
            self.app.tk.call('tk', 'scaling', self.app._test_old_scaling)
            self.app.destroy()
            self.app = None

    def assert_visible(self, widget, *, min_width=2):
        self.assertTrue(widget.winfo_ismapped(), str(widget))
        self.assertGreaterEqual(widget.winfo_width(), min_width, str(widget))
        self.assertGreater(widget.winfo_height(), 1, str(widget))
        x = widget.winfo_rootx() - self.app.winfo_rootx()
        y = widget.winfo_rooty() - self.app.winfo_rooty()
        self.assertGreaterEqual(x, 0, str(widget))
        self.assertGreaterEqual(y, 0, str(widget))
        self.assertLessEqual(x + widget.winfo_width(), self.app.winfo_width(), str(widget))
        self.assertLessEqual(y + widget.winfo_height(), self.app.winfo_height(), str(widget))

    def test_capture_preview_has_usable_width_at_dpi_and_window_boundaries(self):
        for scaling in (1.33, 1.67, 2.0, 2.67):
            app = self.create_app(scaling)
            with Image.new('RGB', (900, 600), 'white') as image:
                page = app.accept_capture(image, auto_read=False)
            self.assertIsNotNone(page)
            for width in (1050, 1280, 720, 1280):
                with self.subTest(scaling=scaling, width=width):
                    app.geometry(f'{width}x800')
                    app.update()
                    for widget in (app.capture_button, app.add_button, app.library_button):
                        self.assert_visible(widget)
                    if app._layout_compact and not app._compact_image:
                        app.toggle_compact_view()
                        app.update()
                    self.assert_visible(app.image_view.canvas, min_width=250)
                    self.assertEqual(app.image_view.image_size, (900, 600))
                    if not app._layout_compact:
                        _lmin, _lwidth, image_min, editor_min = app._layout_metrics()
                        self.assertGreaterEqual(app.image_panel.winfo_width(), image_min)
                        self.assertGreaterEqual(app.editor_panel.winfo_width(), editor_min)
                    else:
                        app.toggle_compact_view()
                        app.update()
                    self.assert_visible(app.source_editor, min_width=250)

    def test_sashes_are_bounded_and_single_library_click_restores_zero_width(self):
        app = self.create_app(1.33)
        self.assertFalse(app._layout_compact)
        app.main_split.sashpos(0, 0)
        app.toggle_library()
        app.update()
        self.assertIn(str(app.library_panel), tuple(map(str, app.main_split.panes())))
        self.assert_visible(app.document_tree, min_width=150)
        app.work_split.sashpos(0, 0)
        app._schedule_layout()
        app.update()
        self.assert_visible(app.image_view.canvas, min_width=250)
        app.main_split.sashpos(0, app.main_split.winfo_width())
        app.work_split.sashpos(0, app.work_split.winfo_width())
        app._schedule_layout()
        app.update()
        self.assert_visible(app.image_view.canvas, min_width=250)
        self.assert_visible(app.source_editor, min_width=250)

    def test_narrow_high_dpi_library_uses_full_area_and_selection_restores_editor(self):
        app = self.create_app(2.67)
        doc = self.library.create_document('합성 고배율 문서')
        self.library.add_text_page(doc['id'], '합성 수정 내용')
        app.refresh_library()
        app.geometry('720x800')
        app.update()
        app.toggle_library()
        app.update()
        self.assertEqual(tuple(map(str, app.main_split.panes())), (str(app.library_panel),))
        self.assert_visible(app.document_tree, min_width=500)
        self.assertTrue(app.open_document(doc['id']))
        app.update()
        self.assertFalse(app._compact_library)
        self.assert_visible(app.source_editor, min_width=500)
        self.assertEqual(app.source_editor.get('1.0', 'end-1c'), '합성 수정 내용')

    def test_new_capture_in_compact_window_shows_original_immediately(self):
        app = self.create_app(2.0)
        app.geometry('720x800')
        app.update()
        self.assertFalse(app._compact_image)
        with Image.new('RGB', (650, 450), 'white') as image:
            page = app.accept_capture(image, auto_read=False)
        app.update()
        self.assertIsNotNone(page)
        self.assertTrue(app._compact_image)
        self.assertFalse(app._compact_library)
        self.assert_visible(app.image_view.canvas, min_width=500)
        self.assertEqual(app.image_view.image_size, (650, 450))
        self.assertEqual(str(app.view_button['text']), '텍스트 보기')
        app.toggle_compact_view()
        app.update()
        self.assert_visible(app.source_editor, min_width=500)

    def test_high_dpi_notice_controls_wrap_without_hiding_generate_button(self):
        app = self.create_app(2.67)
        app.geometry('720x800')
        app.ai_mode.set('안내문')
        app._ai_mode_changed()
        app.editor_tabs.select(app.ai_panel)
        app.update()
        for widget in (app._ai_mode_picker, app.audience_picker, app.generate_button, app.output_editor):
            self.assert_visible(widget)

    def test_window_roundtrip_preserves_dirty_editor_and_library_toggle(self):
        app = self.create_app(1.67)
        app.new_text_document()
        app.source_editor.insert('1.0', '미저장 합성 입력')
        app.source_editor.edit_modified(True)
        app._source_modified()
        app.after_cancel(app._autosave_id)
        app._autosave_id = None
        for width in (720, 1280, 1050, 1280):
            app.geometry(f'{width}x800')
            app.update()
            app.toggle_library()
            app.update()
            app.toggle_library()
            app.update()
            self.assertEqual(app.source_editor.get('1.0', 'end-1c'), '미저장 합성 입력')
            self.assertTrue(app._source_dirty)
        self.assertTrue(app.flush_edits())
        self.assertEqual(self.library.document_text(app.document['id']), '미저장 합성 입력')

    def test_recovery_page_warning_persists_and_reading_and_save_are_blocked(self):
        app = self.create_app()
        app.new_text_document()
        doc_id = app.document['id']
        page = {**app.page, 'readonly': True, 'recovery_required': True,
                'warning': '합성 복구 안내: 저장된 텍스트는 유지됩니다.', 'text': '유지할 합성 본문'}
        with patch.object(self.library, 'pages', return_value=[page]):
            self.assertTrue(app.open_document(doc_id))
        app.update()
        self.assertEqual(str(app.source_editor['state']), 'disabled')
        self.assertEqual(str(app.read_button['state']), 'disabled')
        self.assert_visible(app.recovery_label)
        self.assertEqual(app.recovery_warning.get(), page['warning'])
        with patch.object(app, '_transfer') as transfer, patch.object(self.library, 'save_page_text') as save:
            app.read_current_page()
            app._read_image(page, automatic=True)
            app._read_file(page)
            transfer.assert_not_called()
            app._source_dirty = True
            self.assertFalse(app.flush_edits(show_error=False))
            save.assert_not_called()
        self.assertEqual(app.source_editor.get('1.0', 'end-1c'), '유지할 합성 본문')
        self.assertEqual(app.recovery_warning.get(), page['warning'])

    def test_actual_unreadable_page_keeps_other_pages_and_persistent_warning_accessible(self):
        app = self.create_app()
        with Image.new('RGB', (120, 80), 'white') as image:
            capture = self.library.capture_store.save(image)
        self.library.capture_store.update_ocr(capture.id, '보존할 합성 OCR')
        doc = self.library.create_document('합성 일부 복구 필요 문서')
        self.library.add_capture(doc['id'], capture.id)
        healthy = self.library.add_text_page(doc['id'], '정상 페이지 본문')
        with closing(self.library.capture_store._connect()) as connection:
            with connection:
                connection.execute('UPDATE capture_items SET storage_name=? WHERE id=?', ('../synthetic.png', capture.id))
        app.refresh_library()
        self.assertTrue(app.open_document(doc['id']))
        app.update()
        self.assertTrue(app.page['recovery_required'])
        self.assertFalse(app.document['readonly'])
        self.assertEqual(app.source_editor.get('1.0', 'end-1c'), '보존할 합성 OCR')
        self.assertEqual(str(app.source_editor['state']), 'disabled')
        self.assertIn('복구 필요', app.document_tree.item(doc['id'], 'text'))
        self.assert_visible(app.recovery_label)
        with patch.object(app, 'clipboard_clear'), patch.object(app, 'clipboard_append') as copy:
            app.copy_page()
            copy.assert_called_once_with('보존할 합성 OCR')
        self.assert_visible(app.recovery_label)
        app.page_selector.current(1)
        app._page_selected()
        app.update()
        self.assertEqual(app.page['id'], healthy['id'])
        self.assertEqual(str(app.source_editor['state']), 'normal')
        self.assertEqual(str(app.read_button['state']), 'normal')
        self.assert_visible(app.recovery_label)


if __name__ == '__main__':
    unittest.main()
