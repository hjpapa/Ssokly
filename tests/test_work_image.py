import base64
import io
import json
import queue
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image
from services import work_image as service
from services.transfer_policy import make_text_snapshot
from ui.work_image import WorkImageWindow, open_work_image


def png():
    output = io.BytesIO()
    Image.new('RGB', (100, 150), 'white').save(output, 'PNG')
    return output.getvalue()


class WorkImageTests(unittest.TestCase):
    def test_prompt_and_exact_text_match_server(self):
        text = '희망자만 10월 2일 오후 3시까지\n붙임 미제공'
        request = service.image_request(text)
        self.assertEqual(json.loads(request['prompt'][len(service.PROMPT):]), text)
        bundle = json.loads((Path(__file__).resolve().parents[1] / 'backend/prompts.json').read_text(encoding='utf-8'))
        self.assertEqual(bundle['work_image'], service.PROMPT)
        self.assertIn('없는 연도를 추가하지 않는다', service.PROMPT)
        self.assertEqual(request['model'], 'gpt-image-2.5-flare')

    def test_empty_text_and_non_png_rejected(self):
        for text in ('', '  ', None):
            with self.assertRaises(service.WorkImageError):
                service.image_request(text)
        with self.assertRaises(service.WorkImageError):
            service.validate_png(b'not png')
        self.assertEqual(service.validate_png(png()), png())

    @patch.object(service, 'server_url', return_value='https://relay.example')
    @patch.object(service.httpx, 'Client')
    def test_relay_exact_copy_no_key_access(self, client, url):
        client.return_value.__enter__.return_value.post.return_value = SimpleNamespace(status_code=200, content=png())
        with patch('dotenv.load_dotenv', side_effect=AssertionError('local key access')):
            self.assertEqual(service.generate_work_image('수정한 결과'), png())
        client.return_value.__enter__.return_value.post.assert_called_once_with(
            'https://relay.example/api/image', json={'text': '수정한 결과'})

    @patch.object(service, 'server_url', return_value='https://relay.example')
    @patch.object(service.httpx, 'Client')
    def test_relay_failure_no_retry_or_direct_fallback(self, client, url):
        client.return_value.__enter__.return_value.post.return_value = SimpleNamespace(status_code=404)
        with patch('openai.OpenAI', side_effect=AssertionError('fallback')):
            with self.assertRaisesRegex(service.WorkImageError, '배포'):
                service.generate_work_image('합성')
        self.assertEqual(client.return_value.__enter__.return_value.post.call_count, 1)

    @patch.object(service, 'server_url', return_value='')
    @patch('dotenv.load_dotenv')
    @patch('openai.OpenAI')
    def test_direct_fixed_model_and_zero_retries(self, client, dotenv, url):
        client.return_value.__enter__.return_value.images.generate.return_value = SimpleNamespace(
            data=[SimpleNamespace(b64_json=base64.b64encode(png()).decode())])
        with patch.dict('os.environ', {'OPENAI_API_KEY': 'synthetic'}):
            service.generate_work_image('합성')
        client.assert_called_once_with(timeout=300, max_retries=0)
        self.assertEqual(client.return_value.__enter__.return_value.images.generate.call_args.kwargs,
                         service.image_request('합성'))


class WorkImageUITests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.dialog = WorkImageWindow(self.root, '수정된 결과\n10월 2일 오후 3시까지')

    def tearDown(self):
        self.root.update_idletasks()
        if self.dialog.window.winfo_exists():
            self.dialog.close()
        self.root.update_idletasks()
        self.root.destroy()

    @patch('ui.work_image.choose_transfer', return_value=None)
    @patch('ui.work_image.generate_work_image')
    def test_cancel_never_calls_model(self, generate, choose):
        self.dialog.generate()
        generate.assert_not_called()
        self.assertFalse(self.dialog.busy)

    def test_success_preserves_transmitted_text_failure_keeps_previous(self):
        self.dialog.started = __import__('time').monotonic()
        self.dialog.results.put((png(), '가린 사본', None))
        self.dialog.poll()
        self.assertEqual(self.dialog.text, '가린 사본')
        self.assertEqual(self.dialog.source.get('1.0', 'end-1c'), '가린 사본')
        self.dialog.results.put((None, None, '실패'))
        self.dialog.poll()
        self.assertEqual(self.dialog.data, png())
        self.assertEqual(self.dialog.text, '가린 사본')

    @patch('ui.work_image.WorkImageWindow')
    def test_entry_uses_current_editor_not_stored_output(self, window):
        app = SimpleNamespace(_jobs={}, output_editor=Mock(), output={'text': '옛 결과'})
        app.output_editor.get.return_value = '사용자가 수정한 결과'
        open_work_image(app)
        window.assert_called_once_with(app, '사용자가 수정한 결과')

    def test_close_cancels_poll_callback(self):
        self.dialog.started = __import__('time').monotonic()
        self.dialog.poll()
        identifier = self.dialog.after_id
        self.dialog.close()
        self.assertNotIn(identifier, self.root.tk.call('after', 'info'))

    def test_controls_fit_minimum_at_150_percent(self):
        self.root.tk.call('tk', 'scaling', 2.0)
        self.dialog.window.geometry('620x540')
        self.root.update()
        for button in (self.dialog.generate_button, self.dialog.save_button, self.dialog.copy_button):
            self.assertGreaterEqual(button.winfo_width(), button.winfo_reqwidth())
        self.assertGreaterEqual(self.dialog.view.canvas.winfo_height(), 100)

    @patch('ui.work_image.choose_transfer')
    @patch('ui.work_image.generate_work_image', return_value=png())
    @patch('ui.work_image.threading.Thread')
    def test_generation_uses_only_approved_copy(self, thread, generate, choose):
        choose.return_value = make_text_snapshot('가린 사본')
        thread.side_effect = lambda **kwargs: SimpleNamespace(start=kwargs['target'])
        self.dialog.generate()
        generate.assert_called_once_with('가린 사본')
        self.assertEqual(self.dialog.text, '가린 사본')

    def test_save_writes_exact_png_and_cancel_does_nothing(self):
        self.dialog.data = png()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'result.png'
            with patch('ui.work_image.filedialog.asksaveasfilename', return_value=str(target)):
                self.dialog.save()
            self.assertEqual(target.read_bytes(), png())
        with patch('ui.work_image.filedialog.asksaveasfilename', return_value=''), patch.object(Path, 'write_bytes') as write:
            self.dialog.save()
            write.assert_not_called()

    def test_image_copy_transfers_current_result_only(self):
        self.dialog.data = png()
        with patch('ui.work_image.copy_image') as copy:
            self.dialog.copy()
            copy.assert_called_once_with(png(), owner=self.dialog.window.winfo_id())

    def test_image_window_minimum_reserves_preview_at_all_scales(self):
        from ui.desk_theme import apply_theme
        for percent in (100, 125, 150, 175, 200, 225, 250, 300):
            self.dialog.close()
            self.root.tk.call('tk', 'scaling', percent / 75)
            apply_theme(self.root)
            self.dialog = WorkImageWindow(self.root, '희망자만 신청합니다.')
            self.dialog.window.geometry('620x540')
            self.root.update()
            with self.subTest(percent=percent):
                self.assertGreaterEqual(self.dialog.view.canvas.winfo_height(), 100)
                for button in (self.dialog.generate_button, self.dialog.save_button, self.dialog.copy_button):
                    self.assertGreaterEqual(button.winfo_width(), button.winfo_reqwidth())
                    self.assertLessEqual(button.winfo_rootx() + button.winfo_width(),
                                         self.dialog.window.winfo_rootx() + self.dialog.window.winfo_width())
