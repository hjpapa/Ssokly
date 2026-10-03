"""Original-image copy regressions; synthetic screens, mocked clipboard and network."""
from contextlib import ExitStack
import io
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from services import image_clipboard
from services.document_library import DocumentLibrary
from ui.capture_desk import CaptureDeskApp


class CaptureCopyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.library = DocumentLibrary(Path(self.temp.name))
        for target in ('socket.socket.connect', 'socket.create_connection', 'socket.getaddrinfo'):
            guard = patch(target, side_effect=AssertionError('network forbidden in image-copy tests'))
            mock = guard.start()
            self.addCleanup(guard.stop)
            self.addCleanup(mock.assert_not_called)
        for name in ('showerror', 'showwarning', 'showinfo', 'askyesno'):
            guard = patch('ui.capture_desk.messagebox.' + name, return_value=False)
            guard.start()
            self.addCleanup(guard.stop)
        self.app = CaptureDeskApp(library=self.library)
        self.app.attributes('-alpha', 0.0)
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

    @staticmethod
    def pixels(image, *, owner):
        with image.convert('RGB') as rgb:
            return image.size, rgb.tobytes(), owner

    def run_capture(self, image):
        with patch('ui.desk_jobs.capture_selected_region', return_value=image) as select:
            self.app.capture_and_copy()
            self.app.update()
        return select

    def test_capture_copy_saves_original_and_skips_ocr_in_every_capture_mode(self):
        for mode in ('보관만', '가리고 읽기', '자동 인식'):
            self.app.capture_mode.set(mode)
            image = Image.new('RGB', (37, 23), (31, 71, 113))
            image.putpixel((36, 22), (211, 101, 3))
            expected = image.size, image.tobytes(), self.app.winfo_id()
            copied = []
            with self.subTest(mode=mode), \
                    patch('ui.desk_jobs.copy_image', side_effect=lambda image, **kwargs: copied.append(self.pixels(image, **kwargs))) as copy, \
                    patch.object(self.app, '_read_image') as read, \
                    patch.object(self.app, '_choose_transfer') as choose, \
                    patch.object(self.app, '_start_job') as job:
                select = self.run_capture(image)
                select.assert_called_once_with(self.app)
                copy.assert_called_once()
                read.assert_not_called()
                choose.assert_not_called()
                job.assert_not_called()
                self.assertEqual(copied, [expected])
                self.assertEqual(self.app.page['text'], '')
                with Image.open(self.app.page['path']) as saved:
                    self.assertEqual((saved.size, saved.convert('RGB').tobytes()), expected[:2])
                self.assertIn('Ctrl+V', self.app.status.get())
                self.assertEqual(str(self.app.image_copy_button['state']), 'normal')

    def test_capture_cancellation_never_saves_or_copies(self):
        with patch.object(self.app, 'accept_capture') as accept, patch('ui.desk_jobs.copy_image') as copy:
            select = self.run_capture(None)
        select.assert_called_once_with(self.app)
        accept.assert_not_called()
        copy.assert_not_called()
        self.assertIsNone(self.app.document)

    def test_capture_failure_never_saves_or_copies(self):
        with patch('ui.desk_jobs.capture_selected_region', side_effect=OSError('synthetic capture failure')), \
                patch.object(self.app, 'accept_capture') as accept, patch('ui.desk_jobs.copy_image') as copy:
            self.app.capture_and_copy()
            self.app.update()
        accept.assert_not_called()
        copy.assert_not_called()
        self.assertIn('캡처하지 못했습니다', self.app.status.get())

    def test_clipboard_failure_preserves_saved_original_and_allows_retry(self):
        image = Image.new('RGB', (59, 43), (3, 41, 227))
        expected = image.size, image.tobytes(), self.app.winfo_id()
        self.app.capture_mode.set('자동 인식')
        with patch('ui.desk_jobs.copy_image', side_effect=OSError('synthetic clipboard busy')), \
                patch.object(self.app, '_read_image') as read:
            self.run_capture(image)
        read.assert_not_called()
        path = Path(self.app.page['path'])
        content = path.read_bytes()
        with Image.open(path) as saved:
            self.assertEqual((saved.size, saved.tobytes()), expected[:2])
        self.assertIn('복사하지 못했습니다', self.app.status.get())
        copied = []
        with patch('ui.desk_jobs.copy_image', side_effect=lambda image, **kwargs: copied.append(self.pixels(image, **kwargs))) as copy:
            self.assertTrue(self.app.copy_current_image())
            self.assertEqual(copied, [expected])
            self.assertEqual(copy.call_args.kwargs, {'owner': self.app.winfo_id()})
        self.assertEqual(path.read_bytes(), content)
        self.assertIn('Ctrl+V', self.app.status.get())

    def test_storage_failure_still_copies_capture_and_preserves_warning(self):
        image = Image.new('RGB', (47, 29), (3, 79, 229))
        expected = image.size, image.tobytes(), self.app.winfo_id()
        copied = []

        def fail_storage(*_args, **_kwargs):
            self.app.status.set('캡처 보관 실패 · 합성 저장 오류')
            return None

        with patch.object(self.app, 'accept_capture', side_effect=fail_storage) as accept, \
                patch('ui.desk_jobs.copy_image', side_effect=lambda image, **kwargs: copied.append(self.pixels(image, **kwargs))) as copy:
            self.run_capture(image)
        accept.assert_called_once_with(image, document_id=None, auto_read=False)
        copy.assert_called_once()
        self.assertEqual(copied, [expected])
        self.assertIn('Ctrl+V', self.app.status.get())
        self.assertIn('캡처 보관 실패', self.app.status.get())
        self.assertIsNone(self.app.document)

    def test_unsaved_source_failure_blocks_capture_and_preserves_edit(self):
        self.app.new_text_document()
        document_id = self.app.document['id']
        self.app.source_editor.insert('1.0', '미저장 합성 메모')
        self.app.source_editor.edit_modified(True)
        self.app.source_editor.event_generate('<<Modified>>')
        self.app.update()
        if self.app._autosave_id:
            self.app.after_cancel(self.app._autosave_id)
            self.app._autosave_id = None
        with patch.object(self.library, 'save_page_text', side_effect=OSError('synthetic save fault')), \
                patch('ui.desk_jobs.capture_selected_region') as select, \
                patch('ui.desk_jobs.copy_image') as copy:
            self.app.capture_and_copy()
            self.app.update()
        select.assert_not_called()
        copy.assert_not_called()
        self.assertEqual(self.app.document['id'], document_id)
        self.assertEqual(self.app.source_editor.get('1.0', 'end-1c'), '미저장 합성 메모')
        self.assertTrue(self.app._source_dirty)

    def test_page_image_copy_uses_selected_original_pixels_after_zoom(self):
        with Image.new('RGB', (211, 103), 'red') as first:
            first_page = self.app.accept_capture(first, auto_read=False)
        document_id = self.app.document['id']
        with Image.new('RGB', (1301, 701), (2, 71, 223)) as second:
            second.putpixel((1300, 700), (213, 111, 9))
            second_page = self.app.accept_capture(second, document_id=document_id, auto_read=False)
            second_pixels = second.tobytes()
        originals = {page['id']: Path(page['path']).read_bytes() for page in (first_page, second_page)}
        copied = []
        with patch('ui.desk_jobs.copy_image', side_effect=lambda image, **kwargs: copied.append(self.pixels(image, **kwargs))):
            self.app.page_selector.current(0)
            self.app._page_selected()
            self.assertTrue(self.app.copy_current_image())
            self.assertEqual(copied[-1][0], (211, 103))
            self.assertEqual(copied[-1][1], Image.new('RGB', (211, 103), 'red').tobytes())
            self.app.page_selector.current(1)
            self.app._page_selected()
            self.app.image_view.original_size()
            self.app.image_view.zoom_in()
            self.app.image_view.canvas.xview_moveto(0.5)
            self.app.image_view.canvas.yview_moveto(0.5)
            self.app.update()
            self.assertEqual(self.app.page['id'], second_page['id'])
            self.assertGreater(self.app.image_view.scale, 1)
            self.app.image_copy_button.invoke()
        self.assertEqual(copied[-1], ((1301, 701), second_pixels, self.app.winfo_id()))
        for page in (first_page, second_page):
            self.assertEqual(Path(page['path']).read_bytes(), originals[page['id']])

    def test_image_copy_disabled_for_empty_text_missing_and_corrupt_pages(self):
        self.assertEqual(str(self.app.image_copy_button['state']), 'disabled')
        self.app.new_text_document()
        self.assertEqual(str(self.app.image_copy_button['state']), 'disabled')
        with patch('ui.desk_jobs.copy_image') as copy:
            self.assertFalse(self.app.copy_current_image())
        copy.assert_not_called()
        for damaged in ('missing', 'corrupt'):
            with self.subTest(damaged=damaged), Image.new('RGB', (31, 19), 'green') as image:
                page = self.app.accept_capture(image, auto_read=False)
                path = Path(page['path'])
                if damaged == 'missing':
                    path.unlink()
                else:
                    path.write_bytes(b'not an image')
                self.app._load_page(0)
                self.assertEqual(str(self.app.image_copy_button['state']), 'disabled')
                with patch('ui.desk_jobs.copy_image') as copy:
                    self.assertFalse(self.app.copy_current_image())
                    self.app.image_copy_button.invoke()
                copy.assert_not_called()
                self.assertIsNone(self.app.image_view.image_size)

    def test_copy_controls_stay_visible_in_compact_window(self):
        self.app.attributes('-alpha', 0.0)
        self.app.deiconify()
        self.app.geometry('720x680')
        with Image.new('RGB', (800, 1100), 'white') as image:
            self.app.accept_capture(image, auto_read=False)
        self.app.update()
        self.assertTrue(self.app._is_compact())
        self.assertEqual(self.app.capture_copy_button.cget('text'), '캡처 후 복사')
        self.assertEqual(self.app.image_copy_button.cget('text'), '이미지 복사')
        for button in (self.app.capture_copy_button, self.app.image_copy_button):
            self.assertTrue(button.winfo_ismapped())
            self.assertGreaterEqual(button.winfo_width(), button.winfo_reqwidth())
            x = button.winfo_rootx() - self.app.winfo_rootx()
            y = button.winfo_rooty() - self.app.winfo_rooty()
            self.assertGreaterEqual(x, 0)
            self.assertGreaterEqual(y, 0)
            self.assertLessEqual(x + button.winfo_width(), self.app.winfo_width())
            self.assertLessEqual(y + button.winfo_height(), self.app.winfo_height())


class ImageClipboardTests(unittest.TestCase):
    def windows_api(self, attempts):
        kernel = SimpleNamespace(GlobalAlloc=Mock(return_value=123), GlobalLock=Mock(return_value=456),
                                 GlobalUnlock=Mock(return_value=True), GlobalFree=Mock())
        user = SimpleNamespace(OpenClipboard=Mock(side_effect=attempts), EmptyClipboard=Mock(return_value=True),
                               SetClipboardData=Mock(return_value=123), CloseClipboard=Mock(return_value=True))
        stack = ExitStack()
        self.addCleanup(stack.close)
        dll = stack.enter_context(patch('services.image_clipboard.ctypes.WinDLL', create=True,
                                       side_effect=lambda name, **_kwargs: kernel if name == 'kernel32' else user))
        memmove = stack.enter_context(patch('services.image_clipboard.ctypes.memmove'))
        sleep = stack.enter_context(patch('services.image_clipboard.time.sleep'))
        stack.enter_context(patch('services.image_clipboard.ctypes.get_last_error', create=True, return_value=5))
        stack.enter_context(patch('services.image_clipboard.ctypes.WinError', create=True,
                                 return_value=OSError('synthetic clipboard error')))
        return kernel, user, dll, memmove, sleep

    def test_dib_keeps_full_pixels_flattens_alpha_and_retries_busy_clipboard(self):
        kernel, user, _dll, memmove, sleep = self.windows_api([False, False, True])
        with Image.new('RGBA', (3, 2), (255, 0, 0, 255)) as image:
            image.putpixel((2, 1), (0, 0, 0, 0))
            image_clipboard.copy_image(image, owner=77)
        dib = memmove.call_args.args[1]
        self.assertEqual(struct.unpack_from('<ii', dib, 4), (3, 2))
        header = b'BM' + struct.pack('<IHHI', len(dib) + 14, 0, 0, 54)
        with Image.open(io.BytesIO(header + dib)) as copied:
            self.assertEqual(copied.size, (3, 2))
            self.assertEqual(copied.getpixel((0, 0)), (255, 0, 0))
            self.assertEqual(copied.getpixel((2, 1)), (255, 255, 255))
        self.assertEqual([call.args for call in user.OpenClipboard.call_args_list], [(77,), (77,), (77,)])
        self.assertEqual(sleep.call_count, 2)
        user.EmptyClipboard.assert_called_once_with()
        user.SetClipboardData.assert_called_once_with(8, 123)
        user.CloseClipboard.assert_called_once_with()
        kernel.GlobalFree.assert_not_called()

    def test_busy_clipboard_preserves_previous_contents_and_frees_memory(self):
        kernel, user, _dll, _memmove, sleep = self.windows_api([False] * 5)
        with Image.new('RGB', (7, 5), 'blue') as image, self.assertRaises(OSError):
            image_clipboard.copy_image(image, owner=77)
        self.assertEqual(user.OpenClipboard.call_count, 5)
        self.assertEqual(sleep.call_count, 4)
        user.EmptyClipboard.assert_not_called()
        user.SetClipboardData.assert_not_called()
        user.CloseClipboard.assert_not_called()
        kernel.GlobalFree.assert_called_once_with(123)

    def test_invalid_image_and_missing_owner_never_open_clipboard(self):
        with patch('services.image_clipboard.ctypes.WinDLL', create=True) as dll:
            with self.assertRaises(ValueError):
                image_clipboard.copy_image(b'not image data', owner=0)
            with self.assertRaises(OSError):
                image_clipboard.copy_image(b'not image data', owner=77)
        dll.assert_not_called()


if __name__ == '__main__':
    unittest.main()
