import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from services.analysis_document import Action, AnalysisDocument, render_document, verify_evidence, partial_actions

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
