import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from services.analysis_document import Action, AnalysisDocument, render_document, verify_evidence, partial_actions
from services.analysis_cache import AnalysisCache
from services.ai_service import analyze_document_task

SOURCE = "담임은 9월 30일까지 계획서를 교무실에 제출한다."

def sample():
    return AnalysisDocument(title="계획서 제출", summary="9월 30일까지 제출하세요.",
        actions=[Action(action="계획서 제출", owner="담임", deadline="9월 30일", deliverable="계획서", destination="교무실", evidence=SOURCE, kind="명시된 의무")], questions=[], message="9월 30일까지 계획서를 제출해 주세요.")

class AnalysisPipelineTests(unittest.TestCase):
    def test_real_quote_does_not_validate_invented_fields(self):
        doc = sample()
        doc.actions[0].deadline = "10월 9일"
        doc.actions[0].destination = "없는 부서"
        checked = verify_evidence(doc, SOURCE)
        self.assertEqual(checked.actions[0].deadline, "")
        self.assertEqual(checked.actions[0].destination, "")
        self.assertEqual(checked.actions[0].kind, "확인 필요")
        self.assertEqual(checked.actions[0].owner, "담임")
        self.assertEqual(verify_evidence(sample(), SOURCE).actions[0].kind, "명시된 의무")

    def test_parent_privacy_filter_covers_all_fields_and_preserves_parent_request(self):
        from services.analysis_document import ParentDraft, review_parent_draft
        draft = ParentDraft(title="문의 010-1234-5678", message="학부모는 신청서를 담임에게 제출해 주세요.\n담임은 신청서를 교무실에 전달합니다.\n문의 010 1234 5678", questions=["person@example.com 확인"])
        result = review_parent_draft(draft)
        self.assertNotIn("교무실", result.message)
        self.assertIn("학부모는 신청서를 담임에게 제출", result.message)
        self.assertNotIn("1234", result.model_dump_json())
        self.assertNotIn("person@example.com", result.model_dump_json())

    def test_parent_draft_removes_internal_reporting_sentence(self):
        from services.analysis_document import ParentDraft, review_parent_draft
        draft = ParentDraft(title="행사", message="학부모는 10월 2일까지 담임에게 알려주세요.\n담임은 신청 명단을 10월 5일까지 연구부에 제출합니다.\n행사는 도서관에서 진행합니다.", questions=[])
        checked = review_parent_draft(draft)
        self.assertNotIn("연구부", checked.message)
        self.assertIn("10월 2일", checked.message)
        self.assertIn("도서관", checked.message)
        self.assertTrue(checked.questions)

    def test_school_templates_and_copy_exclude_internal_facts(self):
        from services.workflow_service import extract_section
        doc = sample()
        doc.message = "학부모님 안녕하세요. 9월 30일까지 신청해 주세요."
        for mode in ("학부모 메신저", "가정통신문 초안"):
            output = render_document(doc, mode)
            self.assertNotIn("교무실", output)
            self.assertIn(doc.message, extract_section(output, "message"))
            self.assertNotIn("발송 전 확인", extract_section(output, "message"))
        self.assertIn("- [ ]", render_document(doc, "내 업무 일정·할 일"))

    def test_parent_cache_is_separate_from_internal_and_other_parent_format(self):
        self.assertEqual(len({AnalysisCache.key(SOURCE, "담임", "gpt-5-nano", mode)
                             for mode in ("internal", "학부모 메신저", "가정통신문 초안")}), 3)

    def test_empty_parent_message_requires_review(self):
        doc = sample()
        doc.message = ""
        self.assertIn("안내 대상과 공개할 내용을 확인", render_document(doc, "가정통신문 초안"))

    def test_templates_only_include_selected_sections(self):
        output = render_document(sample(), "일정 확인표")
        self.assertIn("## 일정 메모", output)
        self.assertNotIn("## 전달 문구", output)
        self.assertNotIn("해당 없음", output)
        self.assertIn(SOURCE, output)

    def test_fabricated_quote_is_not_presented_as_source(self):
        doc = sample()
        doc.actions[0].evidence = "10월 1일까지 제출"
        checked = verify_evidence(doc, SOURCE)
        self.assertEqual(checked.actions[0].kind, "확인 필요")
        self.assertEqual(checked.actions[0].evidence, "")
        self.assertTrue(checked.questions)

    def test_partial_json_shows_only_complete_actions(self):
        payload = sample().model_dump_json()
        stop = payload.index('"questions"')
        self.assertEqual(len(partial_actions(payload[:stop])), 1)
        self.assertEqual(partial_actions(payload[:payload.index('"evidence"')]), [])

    def test_cache_invalidates_on_source_role_and_model(self):
        keys = {AnalysisCache.key(SOURCE, "담임", "model"), AnalysisCache.key(SOURCE+"수정", "담임", "model"), AnalysisCache.key(SOURCE, "부장", "model"), AnalysisCache.key(SOURCE, "담임", "other")}
        self.assertEqual(len(keys), 4)

    def test_stream_and_cache_reuse_without_second_api_call(self):
        payload = sample().model_dump_json()
        stream = MagicMock()
        stream.__enter__.return_value = stream
        stream.__iter__.return_value = iter([SimpleNamespace(type="response.output_text.delta", delta=payload)])
        stream.get_final_response.return_value = SimpleNamespace(status="completed", output_text=payload)
        client = MagicMock()
        client.__enter__.return_value = client
        client.responses.stream.return_value = stream
        previews, metrics = [], []
        with tempfile.TemporaryDirectory() as directory, patch("openai.OpenAI", return_value=client), patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}):
            first = analyze_document_task(SOURCE, role="담임", model="gpt-5.6-luna", on_preview=previews.append, cache_dir=directory, raise_errors=True)
            second = analyze_document_task(SOURCE, "일정 확인표", role="행정실", model="gpt-5.6-luna", cache_dir=directory, on_metrics=metrics.append, raise_errors=True)
            self.assertIn("## 체크리스트", first)
            self.assertNotIn("## 업무 순서", second)
            self.assertTrue(previews)
            self.assertTrue(metrics[0]["cached"])
            client.responses.stream.assert_called_once()
            self.assertNotIn("담당 역할:", client.responses.stream.call_args.kwargs['input'])

    def test_incomplete_stream_never_cached(self):
        stream = MagicMock()
        stream.__enter__.return_value = stream
        stream.get_final_response.return_value = SimpleNamespace(status="incomplete")
        client = MagicMock()
        client.__enter__.return_value = client
        client.responses.stream.return_value = stream
        with tempfile.TemporaryDirectory() as directory, patch("openai.OpenAI", return_value=client), patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}):
            with self.assertRaisesRegex(RuntimeError, "완성되지"):
                analyze_document_task(SOURCE, model="gpt-5.6-luna", cache_dir=directory, raise_errors=True)
            self.assertIsNone(AnalysisCache(directory).get(AnalysisCache.key(SOURCE, "전체 담당자", "gpt-5.6-luna")))

    def test_cancelled_analysis_does_not_call_api(self):
        event = threading.Event()
        event.set()
        with patch("openai.OpenAI") as client, self.assertRaisesRegex(RuntimeError, "취소"):
            analyze_document_task(SOURCE, cancel_event=event, raise_errors=True)
        client.assert_not_called()
