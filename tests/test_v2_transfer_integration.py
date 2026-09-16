"""P03-P05 synthetic boundary checks, with no keys/files/network from users."""
import base64
from io import BytesIO
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image

from services.ai_service import analyze_document_task
from services.diagram_service import generate_workflow_image
from services.ocr_service import extract_text_from_file
from services.source_contract import SourceAction, SourceDocument, SourceRef, FieldRefs
from services.transfer_policy import ScopeExpansionRequired, make_file_snapshot, make_image_snapshot, make_text_snapshot


SECRET = "SYNTHETIC_PRIVATE_TOKEN_63891"
ORIGINAL = "학교는 참가 신청서를 제출한다.\n기한: 2026-10-15\n제출처: 교육지원청\n학생 이름: " + SECRET
SAFE = ORIGINAL.replace(SECRET, "[가림1]")


def wire_action(*, bad=False):
    return SourceAction(action="참가 신청서 제출", owner="학교", deadline="2026-10-16" if bad else "2026-10-15",
                        deliverable="신청서", destination="교육지원청", kind="명시된 의무", obligation="필수",
                        evidence=SourceRef(line=1, cell=0), field_evidence=FieldRefs(
                            owner=SourceRef(line=1, cell=0), deadline=SourceRef(line=2, cell=0),
                            deliverable=SourceRef(line=1, cell=0), destination=SourceRef(line=3, cell=0)))


def client_with_document(*, bad=False, parent=False):
    client, stream = MagicMock(), MagicMock()
    client.__enter__.return_value = client
    client.with_options.return_value = client
    stream.__enter__.return_value = stream
    if parent:
        payload = json.dumps({"title": "참가 안내", "message": "참가 희망 여부를 확인해 주세요.", "questions": []})
    else:
        payload = SourceDocument(title="참가 신청", summary="신청 안내", actions=[wire_action(bad=bad)], questions=[], message="신청서를 제출해 주세요.").model_dump_json()
    stream.__iter__.return_value = iter([SimpleNamespace(type="response.output_text.delta", delta=payload)])
    stream.get_final_response.return_value = SimpleNamespace(status="completed", output_text=payload)
    client.responses.stream.return_value = stream
    client.responses.create.return_value = SimpleNamespace(status="completed", output_text=json.dumps({"corrections": [{"index": 0, "action": wire_action().model_dump()}]}))
    return client


class V2TransferBoundaryTests(unittest.TestCase):
    def assert_no_secret_or_attachment(self, kwargs):
        serialized = json.dumps(kwargs, ensure_ascii=False)
        self.assertNotIn(SECRET, serialized)
        self.assertNotIn("input_file", serialized)
        self.assertNotIn("file_data", serialized)
        self.assertNotIn("input_image", serialized)
        self.assertIn("[가림1]", serialized)

    def test_p04_initial_analysis_stream_and_callback_only_use_safe_copy(self):
        snapshot = make_text_snapshot(SAFE, original_text=ORIGINAL)
        client = client_with_document()
        document_callback = MagicMock()
        with patch("openai.OpenAI", return_value=client), patch("services.ai_service.load_dotenv"), patch.dict("os.environ", {"OPENAI_API_KEY": "synthetic-only"}):
            result = analyze_document_task(snapshot.text, raise_errors=True, on_document=document_callback)
        request = client.responses.stream.call_args.kwargs
        self.assert_no_secret_or_attachment(request)
        self.assertEqual(json.loads(request["input"])["source_lines"][-1]["text"], "학생 이름: [가림1]")
        self.assertNotIn(SECRET, result)
        document_callback.assert_called_once()
        self.assertNotIn(SECRET, document_callback.call_args.args[0].model_dump_json())

    def test_p04_one_repair_request_and_reanalysis_keep_same_safe_scope(self):
        snapshot = make_text_snapshot(SAFE, original_text=ORIGINAL)
        client = client_with_document(bad=True)
        with patch("openai.OpenAI", return_value=client), patch("services.ai_service.load_dotenv"), patch.dict("os.environ", {"OPENAI_API_KEY": "synthetic-only"}):
            for _ in range(2):
                analyze_document_task(snapshot.guard_text(snapshot.text), raise_errors=True)
        self.assertEqual(client.responses.stream.call_count, 2)
        self.assertEqual(client.responses.create.call_count, 2)
        for call in client.responses.stream.call_args_list + client.responses.create.call_args_list:
            self.assert_no_secret_or_attachment(call.kwargs)
        client.with_options.assert_called_with(timeout=30, max_retries=0)

    def test_p04_parent_draft_has_no_original_file_or_excluded_string(self):
        snapshot = make_text_snapshot(SAFE, original_text=ORIGINAL)
        for mode in ("학부모 메신저", "가정통신문 초안"):
            with self.subTest(mode=mode):
                client = client_with_document(parent=True)
                with patch("openai.OpenAI", return_value=client), patch("services.ai_service.load_dotenv"), patch.dict("os.environ", {"OPENAI_API_KEY": "synthetic-only"}):
                    analyze_document_task(snapshot.guard_text(snapshot.text), mode, raise_errors=True)
                self.assert_no_secret_or_attachment(client.responses.stream.call_args.kwargs)

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

    def test_p04_diagram_payload_contains_only_approved_safe_text(self):
        snapshot = make_text_snapshot(SAFE, original_text=ORIGINAL)
        output = BytesIO()
        Image.new("RGB", (20, 20), "white").save(output, "PNG")
        client = MagicMock()
        client.__enter__.return_value = client
        client.images.generate.return_value = SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(output.getvalue()).decode())])
        with patch("openai.OpenAI", return_value=client), patch("services.diagram_service.load_dotenv"):
            generate_workflow_image(snapshot.guard_text(snapshot.text))
        request = client.images.generate.call_args.kwargs
        self.assert_no_secret_or_attachment(request)
        self.assertTrue(request["prompt"].endswith(SAFE))

    def test_p05_scope_expansion_rejected_before_analysis_parent_and_diagram(self):
        snapshot = make_text_snapshot(SAFE, original_text=ORIGINAL)
        actions = [lambda value: analyze_document_task(value, raise_errors=True),
                   lambda value: analyze_document_task(value, "학부모 메신저", raise_errors=True),
                   generate_workflow_image]
        for action in actions:
            with self.subTest(action=action), patch("openai.OpenAI") as client:
                with self.assertRaises(ScopeExpansionRequired):
                    action(snapshot.guard_text(SAFE + "\n복원 정보 " + SECRET))
                client.assert_not_called()

    def test_p05_cache_never_reuses_unmasked_input_for_masked_scope(self):
        from services.analysis_cache import AnalysisCache
        snapshot = make_text_snapshot(SAFE, original_text=ORIGINAL)
        self.assertNotEqual(AnalysisCache.key(snapshot.text, "전체 담당자", "gpt-5-nano"),
                            AnalysisCache.key(ORIGINAL, "전체 담당자", "gpt-5-nano"))

    def test_p05_recombined_image_ocr_letters_do_not_reach_any_api(self):
        snapshot = make_image_snapshot(Image.new("RGB", (10, 10), "white"), rectangles=[(1, 1, 5, 5)])
        snapshot = snapshot.with_safe_text("SCHOOL EDUCATION CREATIVE RESEARCH EVALUATION TEACHER")
        for send in (lambda value: analyze_document_task(value, raise_errors=True), generate_workflow_image):
            with self.subTest(send=send), patch("openai.OpenAI") as client:
                with self.assertRaises(ScopeExpansionRequired):
                    send(snapshot.guard_text("S E C R E T"))
                client.assert_not_called()

    def test_p04_one_id_redacted_does_not_hide_other_id_or_send_original(self):
        raw = "학교는 참가 신청서를 제출한다.\n학생 A001, 학생 A002"
        safe = "학교는 참가 신청서를 제출한다.\n학생 [가림1], 학생 A002"
        snapshot = make_text_snapshot(safe, original_text=raw, excluded_strings=["A001"])
        client = client_with_document(parent=True)
        with patch("openai.OpenAI", return_value=client), patch("services.ai_service.load_dotenv"), patch.dict("os.environ", {"OPENAI_API_KEY": "synthetic-only"}):
            analyze_document_task(snapshot.guard_text(snapshot.text), "학부모 메신저", raise_errors=True)
        payload = client.responses.stream.call_args.kwargs["input"]
        self.assertNotIn("A001", payload)
        self.assertIn("A002", payload)
        self.assertIn("[가림1]", payload)


if __name__ == "__main__":
    unittest.main()
