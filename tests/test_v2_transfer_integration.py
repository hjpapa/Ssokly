"""P03-P05 synthetic boundary checks, with no keys/files/network from users."""
import base64
from io import BytesIO
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image

from services.ocr_service import extract_text_from_file
from services.transfer_policy import make_file_snapshot


SECRET = "SYNTHETIC_PRIVATE_TOKEN_63891"


class V2TransferBoundaryTests(unittest.TestCase):
    def test_p04_file_bytes_override_does_not_reopen_mutated_original(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.pdf"
            path.write_bytes(b"%PDF selected immutable copy")
            snapshot = make_file_snapshot(path)
            path.write_bytes(SECRET.encode())
            client = MagicMock()
            client.responses.create.return_value = SimpleNamespace(status="completed", output_text="선택한 문서의 전사 결과")
            with patch("services.ocr_service._load_openai_settings", return_value=("synthetic-only", "gpt-5-nano")), patch("openai.OpenAI", return_value=client), patch.object(Path, "read_bytes", side_effect=AssertionError("Original file reopened")), patch.object(Path, "stat", side_effect=AssertionError("Original file restatted")):
                extract_text_from_file(path, "application/pdf", file_bytes=snapshot.file_bytes, filename=snapshot.file_name, raise_errors=True)
            request = client.responses.create.call_args.kwargs
            attachment = request["input"][0]["content"][1]
            self.assertEqual(attachment["filename"], "synthetic.pdf")
            self.assertEqual(base64.b64decode(attachment["file_data"].split(",", 1)[1]), snapshot.file_bytes)
            self.assertNotIn(SECRET.encode(), base64.b64decode(attachment["file_data"].split(",", 1)[1]))


if __name__ == "__main__":
    unittest.main()
