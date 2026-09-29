import json
import base64
import hashlib
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from PIL import Image
from services import ai_relay
from services.ocr_service import extract_text_from_image, extract_text_from_file
from services.text_actions import generate_text_action, TextActionCancelled, TextActionError


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict('os.environ', {'SSOKLY_API_URL': 'https://example.test',
                                           'OPENAI_API_KEY': 'synthetic-never-send'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_request_has_no_credentials_or_redirects_and_no_retries(self):
        with patch('services.ai_relay.httpx.Client') as factory:
            client = factory.return_value.__enter__.return_value
            client.post.return_value = Mock(status_code=200)
            client.post.return_value.json.return_value = {'status': 'completed', 'text': '합성 결과'}
            self.assertEqual(ai_relay.request_relay({'operation': 'text', 'text': '합성'}), '합성 결과')
            self.assertFalse(factory.call_args.kwargs['follow_redirects'])
            self.assertEqual(client.post.call_count, 1)
            self.assertNotIn('synthetic-never-send', str(client.post.call_args))
            self.assertEqual(client.post.call_args.kwargs['headers'], {'Content-Type': 'application/json'})

    def test_ocr_does_not_load_key_or_call_sdk(self):
        with patch('services.ai_relay.request_relay', return_value='합성 OCR') as request, \
                patch('services.ocr_service._load_openai_settings') as settings, patch('openai.OpenAI') as sdk:
            self.assertEqual(extract_text_from_image(Image.new('RGB', (20, 20)), raise_errors=True), '합성 OCR')
            self.assertTrue(request.call_args.args[0]['image'].startswith('data:image/png;base64,'))
            settings.assert_not_called()
            sdk.assert_not_called()

    def test_text_does_not_load_key_and_cancelled_result_is_not_applied(self):
        cancel = threading.Event()
        def answer(_):
            cancel.set()
            return '합성 결과'
        with patch('services.ai_relay.request_relay', side_effect=answer), \
                patch('services.text_actions.load_dotenv') as settings, patch('openai.OpenAI') as sdk:
            preview = Mock()
            with self.assertRaises(TextActionCancelled):
                generate_text_action('합성', cancel_event=cancel, on_preview=preview)
            preview.assert_not_called()
            settings.assert_not_called()
            sdk.assert_not_called()

    def test_failure_never_falls_back_or_leaks_server_error(self):
        with patch('services.ai_relay.httpx.Client') as factory, patch('openai.OpenAI') as sdk:
            client = factory.return_value.__enter__.return_value
            client.post.side_effect = RuntimeError('synthetic-secret-response')
            with self.assertRaises(TextActionError) as error:
                generate_text_action('합성')
            self.assertNotIn('synthetic-secret', str(error.exception))
            sdk.assert_not_called()
            self.assertEqual(client.post.call_count, 1)

    def test_unsafe_addresses_are_rejected_but_large_payload_is_sent(self):
        for value in ('http://example.test', 'https://name:password@example.test', 'https://example.test/?token=x'):
            with patch.dict('os.environ', {'SSOKLY_API_URL': value}), self.assertRaises(ai_relay.RelayError):
                ai_relay.server_url()
        with patch('services.ai_relay.httpx.Client') as factory:
            client = factory.return_value.__enter__.return_value
            client.post.return_value = Mock(status_code=200)
            client.post.return_value.json.return_value = {'status': 'completed', 'text': '합성 결과'}
            self.assertEqual(ai_relay.request_relay({'text': 'x' * 4_000_001}), '합성 결과')
            self.assertGreater(len(client.post.call_args.kwargs['content']), 4_000_000)
            self.assertEqual(factory.call_args.kwargs['timeout'], 300)

    def test_incomplete_and_http_failures_are_not_results(self):
        with patch('services.ai_relay.httpx.Client') as factory:
            response = factory.return_value.__enter__.return_value.post.return_value
            for status in (302, 400, 413, 429, 502, 503):
                response.status_code = status
                with self.assertRaises(ai_relay.RelayError):
                    ai_relay.request_relay({})
            response.status_code = 200
            response.json.return_value = {'status': 'incomplete', 'text': 'partial'}
            with self.assertRaises(ai_relay.RelayError):
                ai_relay.request_relay({})

    def test_legacy_file_path_does_not_load_local_key(self):
        with patch('services.ocr_service._load_openai_settings') as settings:
            with self.assertRaises(ai_relay.RelayError):
                extract_text_from_file(Path('synthetic.pdf'), 'application/pdf', raise_errors=True)
            settings.assert_not_called()

    def test_server_prompts_match_desktop(self):
        from tools.export_relay_prompts import bundle, ROOT
        self.assertEqual(json.loads((ROOT / 'backend/prompts.json').read_text(encoding='utf-8')), bundle())

    def large_payload(self):
        data = b'\x89PNG\r\n\x1a\n' + b'x' * 3_100_000
        return data, {'operation': 'ocr', 'image': 'data:image/png;base64,' + base64.b64encode(data).decode('ascii'), 'detail': 'high'}

    def test_large_image_bypasses_function_body_without_changing_bytes(self):
        data, payload = self.large_payload()
        with patch('services.ai_relay.httpx.Client') as factory:
            client = factory.return_value.__enter__.return_value
            grant = Mock(status_code=200)
            grant.json.return_value = {'upload_url': 'https://vercel.com/api/blob/?signed=synthetic', 'receipt': 'synthetic-receipt'}
            result = Mock(status_code=200)
            result.json.return_value = {'status': 'completed', 'text': 'large image result'}
            client.post.side_effect = [grant, result, Mock(status_code=200)]
            client.put.return_value = Mock(status_code=200)
            self.assertEqual(ai_relay.request_relay(payload), 'large image result')
            create, analyze, cleanup = client.post.call_args_list
            self.assertEqual(create.kwargs['json']['sha256'], hashlib.sha256(data).hexdigest())
            self.assertEqual(client.put.call_args.kwargs['content'], data)
            self.assertEqual(client.put.call_args.kwargs['headers'], {'Content-Type': 'image/png'})
            self.assertEqual(analyze.kwargs['json'], {'operation': 'ocr_blob', 'receipt': 'synthetic-receipt', 'detail': 'high'})
            self.assertEqual(cleanup.kwargs['json']['action'], 'delete')
            self.assertNotIn('synthetic-never-send', str(client.post.call_args_list))

    def test_failed_upload_still_cleans_up_and_never_calls_ai(self):
        _, payload = self.large_payload()
        with patch('services.ai_relay.httpx.Client') as factory:
            client = factory.return_value.__enter__.return_value
            grant = Mock(status_code=200)
            grant.json.return_value = {'upload_url': 'https://vercel.com/api/blob/?signed=synthetic', 'receipt': 'synthetic-receipt'}
            client.post.side_effect = [grant, Mock(status_code=200)]
            client.put.side_effect = RuntimeError('secret-upload-url')
            with self.assertRaises(ai_relay.RelayError) as error:
                ai_relay.request_relay(payload)
            self.assertNotIn('secret-upload-url', str(error.exception))
            self.assertTrue(all(call.args[0].endswith('/api/upload') for call in client.post.call_args_list))
            self.assertEqual(client.post.call_args_list[-1].kwargs['json']['action'], 'delete')

    def test_foreign_upload_target_is_rejected_without_transmitting_image(self):
        _, payload = self.large_payload()
        with patch('services.ai_relay.httpx.Client') as factory:
            client = factory.return_value.__enter__.return_value
            grant = Mock(status_code=200)
            grant.json.return_value = {'upload_url': 'https://example.test/private', 'receipt': 'synthetic'}
            client.post.side_effect = [grant, Mock(status_code=200)]
            with self.assertRaises(ai_relay.RelayError):
                ai_relay.request_relay(payload)
            client.put.assert_not_called()

    def test_cleanup_failure_preserves_successful_result(self):
        _, payload = self.large_payload()
        with patch('services.ai_relay.httpx.Client') as factory:
            client = factory.return_value.__enter__.return_value
            grant = Mock(status_code=200)
            grant.json.return_value = {'upload_url': 'https://vercel.com/api/blob/?signed=synthetic', 'receipt': 'synthetic'}
            result = Mock(status_code=200)
            result.json.return_value = {'status': 'completed', 'text': 'completed', 'cleanup_pending': True}
            client.post.side_effect = [grant, result, RuntimeError('cleanup unavailable')]
            client.put.return_value = Mock(status_code=200)
            self.assertEqual(ai_relay.request_relay(payload), 'completed')

    def test_submission_excludes_secrets_and_user_data(self):
        from tools.build_submission import build
        import zipfile
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'submission.zip'
            build('https://example.test', output)
            with zipfile.ZipFile(output) as archive:
                names = archive.namelist()
                self.assertIn('Ssokly/main.py', names)
                self.assertNotIn('Ssokly/.env', names)
                self.assertFalse(any('/.git/' in name or '/.venv/' in name or '/backend/api/' in name for name in names))
                self.assertEqual(json.loads(archive.read('Ssokly/ai-server.json'))['url'], 'https://example.test')
