"""Real dialog/button flows, synthetic text and offline model responses."""
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ui.capture_desk import CaptureDeskApp
from ui.transfer_dialog import TransferDialog
from tests.tk_support import destroy_root


class MaskAndContextTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.app = CaptureDeskApp(app_data_dir=self.directory.name)
        self.addCleanup(destroy_root, self.app)
        self.app.geometry('720x680')
        self.document = self.app.library.create_document('합성 공문')
        self.app.library.add_text_page(self.document['id'], '대상 1명\n마감 오후 세 시')
        self.app.open_document(self.document['id'])
        self.app.update()

    def test_preselected_text_is_masked_when_entering_mask_mode(self):
        dialog = TransferDialog(self.app, kind='text', text='가상 이름 신청 안내')
        dialog.editor.tag_add('sel', '1.0', '1.5')
        dialog.mask_button.invoke()
        self.assertEqual(dialog.editor.get('1.0', 'end-1c'), '[가림] 신청 안내')
        dialog.finish_button.invoke()
        self.assertIsNotNone(dialog.result)
        self.assertNotIn('가상 이름', dialog.result.text)

    def test_mask_mode_explains_steps_and_requires_a_changed_copy(self):
        dialog = TransferDialog(self.app, kind='text', text='가상 이름 신청 안내')
        dialog.mask_button.invoke()
        self.assertIn('disabled', dialog.finish_button.state())
        self.assertIn('선택한 텍스트 가리기', dialog.note.get())
        dialog.editor.tag_add('sel', '1.0', '1.5')
        dialog.mask_selection_button.invoke()
        self.assertNotIn('disabled', dialog.finish_button.state())
        dialog.editor.delete('1.0', 'end')
        dialog.editor.insert('1.0', dialog.original_text)
        self.app.update()
        self.assertIn('disabled', dialog.finish_button.state())
        dialog.cancel()

    def test_finish_button_is_visible_before_and_after_masking_at_150_percent(self):
        self.app.tk.call('tk', 'scaling', 2.0)
        for kind in ('text', 'image'):
            from PIL import Image
            dialog = TransferDialog(self.app, kind=kind, text='희망 학생만 신청',
                                    image=Image.new('RGB', (800, 1100), 'white') if kind == 'image' else None)
            dialog.window.geometry('860x700')
            for masking in (False, True):
                if masking:
                    dialog.start_masking()
                self.app.update()
                button = dialog.finish_button
                with self.subTest(kind=kind, masking=masking):
                    self.assertTrue(button.winfo_ismapped())
                    self.assertGreaterEqual(button.winfo_height(), button.winfo_reqheight())
                    self.assertLessEqual(button.winfo_rooty() + button.winfo_height(),
                                         dialog.window.winfo_rooty() + dialog.window.winfo_height())
            dialog.cancel()

    def test_file_text_to_image_mask_restores_finish_action(self):
        from PIL import Image
        dialog = TransferDialog(self.app, kind='file', path='synthetic.pdf')
        dialog.start_masking()
        self.assertIn('disabled', dialog.finish_button.state())
        dialog.original_image = Image.new('RGB', (30, 30), 'white')
        dialog.rectangles = [(1, 1, 10, 10)]
        dialog._show_image()
        self.assertNotIn('disabled', dialog.finish_button.state())
        dialog.finish_button.invoke()
        self.assertEqual(dialog.result.kind, 'image')
        self.assertEqual(dialog.result.as_image().getpixel((2, 2)), (0, 0, 0))

    def test_masked_digit_reaches_model_only_as_approved_copy(self):
        errors = []
        def choose(**kwargs):
            dialog = TransferDialog(self.app, **kwargs)
            dialog.mask_button.invoke()
            dialog.editor.tag_add('sel', '1.3', '1.4')
            dialog.mask_selection()
            with patch('ui.transfer_dialog.messagebox.showerror', side_effect=lambda *a, **kw: errors.append(a)):
                dialog.finish_button.invoke()
            result = dialog.result
            if dialog.window.winfo_exists():
                dialog.cancel()
            return result
        with patch.object(self.app, '_choose_transfer', side_effect=choose), \
                patch('services.text_actions.generate_text_action', return_value='합성 정리 결과') as model:
            self.app.generate_button.invoke()
            deadline = time.monotonic() + 3
            while self.app._jobs and time.monotonic() < deadline:
                self.app.update()
                time.sleep(.01)
        self.assertEqual(errors, [])
        model.assert_called_once()
        self.assertNotIn('1', model.call_args.args[0])
        self.assertIn('[가림]', model.call_args.args[0])
        self.assertEqual(self.app.output_editor.get('1.0', 'end-1c'), '합성 정리 결과')
        self.assertEqual(self.app.library.document_text(self.document['id']), '대상 1명\n마감 오후 세 시')

    def test_repeated_removed_text_still_blocks_transmission(self):
        dialog = TransferDialog(self.app, kind='text', text='가상비밀 / 가상비밀')
        dialog.mask_button.invoke()
        dialog.editor.tag_add('sel', '1.0', '1.4')
        dialog.mask_selection_button.invoke()
        with patch('ui.transfer_dialog.messagebox.showerror') as error:
            dialog.finish_button.invoke()
        self.assertIsNone(dialog.result)
        self.assertIn('반복된 내용', error.call_args.args[1])
        dialog.cancel()

    def test_trash_context_menu_keeps_library_open_after_selection_events(self):
        self.app.library.trash(self.document['id'])
        other = self.app.library.create_document('다른 문서')
        self.app.open_document(other['id'])
        self.app.show_trash.set(True)
        self.app.refresh_library()
        self.app.update()
        self.app._compact_library = True
        self.app._apply_layout()
        self.app.update()
        before = self.app.library_panel.winfo_width()
        event = SimpleNamespace(y=10, x_root=100, y_root=100)
        with patch.object(self.app.document_tree, 'identify_row', return_value=self.document['id']), \
                patch('ui.desk_library.tk.Menu'):
            self.app._document_context_menu(event)
        self.app.update()
        self.assertTrue(self.app._compact_library)
        self.assertTrue(self.app.library_panel.winfo_ismapped())
        self.assertEqual(self.app.library_panel.winfo_width(), before)
        self.assertEqual(self.app.document['id'], self.document['id'])
        with patch('ui.desk_library.messagebox.askyesno', return_value=True):
            self.assertTrue(self.app.purge_current_document())
        self.app.update()
        self.assertTrue(self.app._compact_library)
        self.assertTrue(self.app.library_panel.winfo_ismapped())

    def test_empty_trash_keeps_library_with_an_unrelated_open_document(self):
        other = self.app.library.create_document('삭제할 합성 문서')
        self.app.library.trash(other['id'])
        self.app._compact_library = True
        self.app._apply_layout()
        with patch('ui.desk_library.messagebox.askyesno', return_value=True):
            self.assertTrue(self.app.empty_trash())
        self.app.update()
        self.assertTrue(self.app._compact_library)
        self.assertTrue(self.app.library_panel.winfo_ismapped())
        self.assertEqual(self.app.document['id'], self.document['id'])
