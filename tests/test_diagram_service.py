import base64
from io import BytesIO
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from PIL import Image
from services.diagram_service import generate_workflow_image

class DiagramTests(unittest.TestCase):
    def test_valid_image_response_and_explicit_model(self):
        buffer = BytesIO()
        Image.new("RGB", (40, 30), "white").save(buffer, "PNG")
        client = MagicMock()
        client.__enter__.return_value = client
        client.images.generate.return_value = SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(buffer.getvalue()).decode())])
        with patch("openai.OpenAI", return_value=client), patch.dict("os.environ", {"OPENAI_IMAGE_MODEL": "gpt-image-2.5-flare"}):
            data = generate_workflow_image("9월 30일 계획서 제출")
        self.assertEqual(Image.open(BytesIO(data)).size, (40, 30))
        self.assertEqual(client.images.generate.call_args.kwargs["model"], "gpt-image-2.5-flare")

    def test_empty_input_makes_no_request(self):
        with patch("openai.OpenAI") as client, self.assertRaises(ValueError):
            generate_workflow_image("  ")
        client.assert_not_called()
