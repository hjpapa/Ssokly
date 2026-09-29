"""Default direct OCR request contracts; no network or real credentials."""
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from PIL import Image
from services import ocr_service


class LunaDefaultsTests(unittest.TestCase):
    def test_image_and_document_use_luna_none(self):
        client = Mock()
        client.responses.create.return_value = SimpleNamespace(status='completed', output_text='synthetic')
        with patch('services.ai_relay.server_url', return_value=''), \
                patch.object(ocr_service, 'load_dotenv'), \
                patch.dict('os.environ', {'OPENAI_API_KEY': 'synthetic'}), \
                patch('openai.OpenAI', return_value=client):
            for run in (
                lambda: ocr_service.extract_text_from_image(Image.new('RGB', (20, 20)), raise_errors=True),
                lambda: ocr_service.extract_text_from_file(Path('synthetic.pdf'), 'application/pdf', file_bytes=b'synthetic', raise_errors=True),
            ):
                self.assertEqual(run(), 'synthetic')
                options = client.responses.create.call_args.kwargs
                self.assertEqual(options['model'], 'gpt-6-luna')
                self.assertEqual(options['reasoning'], {'effort': 'none'})
                self.assertIs(options['store'], False)
