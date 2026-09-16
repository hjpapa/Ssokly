import tempfile
import unittest
from services.personal_todos import PersonalTodoStore, checklist_items
from services.analysis_document import Action, AnalysisDocument, render_document


class PersonalTodoTests(unittest.TestCase):
    def test_analysis_origin_persists_but_rejects_edits_and_legacy_results(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PersonalTodoStore(directory)
            self.assertFalse(store.matches_analysis("결과", "원문"))
            store.remember_analysis("결과", "원문")
            reopened = PersonalTodoStore(directory)
            self.assertTrue(reopened.matches_analysis("결과", "원문"))
            self.assertFalse(reopened.matches_analysis("결과", "수정 원문"))
            self.assertFalse(reopened.matches_analysis("수정 결과", "원문"))

    def test_all_owners_and_unknown_owner_are_present(self):
        doc = AnalysisDocument(title="학교 업무", summary="담당별 업무", actions=[
            Action(action="명단 제출", owner="담임", deadline="10월 5일", deliverable="명단", destination="연구부", evidence="", kind="명시된 의무"),
            Action(action="비용 처리", owner="행정실", deadline="", deliverable="", destination="", evidence="", kind="명시된 의무"),
            Action(action="담당 확인", owner="", deadline="", deliverable="", destination="", evidence="", kind="명시된 의무")], questions=[], message="")
        items = checklist_items(render_document(doc, "업무 일정·체크리스트"))
        self.assertEqual(len(items), 3)
        self.assertIn("담당: 담임", items[0])
        self.assertIn("10월 5일", items[0])
        self.assertIn("행정실", items[1])
        self.assertIn("담당 확인 필요", items[2])

    def test_parser_ignores_completed_and_other_sections(self):
        text = "## 체크리스트\n- [ ] 첫 업무\n  담당: 담임\n- [x] 완료 업무\n  기한: 어제\n## 전달 문구\n- [ ] 가짜 항목"
        self.assertEqual(checklist_items(text), ["첫 업무\n담당: 담임"])

    def test_selection_persists_and_duplicates_do_not_reset_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PersonalTodoStore(directory)
            self.assertEqual(store.add(["첫 업무", "둘째 업무"], "원문"), 2)
            row = next(row for row in store.list() if row['item'] == '첫 업무')
            store.set_done([row['id']], True)
            self.assertEqual(store.add(["첫 업무"], "원문"), 0)
            reopened = PersonalTodoStore(directory)
            self.assertTrue(next(saved for saved in reopened.list() if saved['id'] == row['id'])['done'])
            reopened.set_done([row['id']], False)
            self.assertFalse(any(row['done'] for row in reopened.list()))
            reopened.delete([row['id']])
            self.assertEqual(len(reopened.list()), 1)
            self.assertEqual(reopened.list()[0]['source'], "원문")
