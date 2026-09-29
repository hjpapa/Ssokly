import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from services.analysis_document import Action, AnalysisDocument, FieldEvidence, inspect_action, verify_evidence, render_document

SOURCE = '학교는 과학행사 참가 신청서를 제출한다.\n과학행사 신청 기한: 2026. 10. 2.(금) 16:00까지\n과학행사 제출처: 교육지원청\n교육지원청은 참가 명단을 안내한다.'

def action(**kwargs):
    data = dict(action='과학행사 신청서 제출', owner='학교', deadline='2026-10-02',
                deliverable='신청서', destination='교육지원청', evidence=SOURCE.splitlines()[0],
                kind='명시된 의무', field_evidence=FieldEvidence(
                    deadline=SOURCE.splitlines()[1], destination=SOURCE.splitlines()[2]))
    data.update(kwargs)
    return Action(**data)

def document(actions):
    return AnalysisDocument(title='과학행사', summary='신청 안내', actions=actions, questions=[], message='')

def client_for(corrections):
    for correction in corrections:
        data = correction['action']
        def ref(quote):
            return {'line': next((i for i, line in enumerate(SOURCE.splitlines(), 1) if quote and quote in line), 0), 'cell': 0}
        primary = data['evidence']
        data['evidence'] = ref(primary)
        data['field_evidence'] = {key: ref(value or primary) for key, value in data['field_evidence'].items()}
    client = MagicMock()
    client.with_options.return_value = client
    client.responses.create.return_value = SimpleNamespace(status='completed', output_text=json.dumps({'corrections': corrections}, ensure_ascii=False))
    return client

class ContextReviewTests(unittest.TestCase):
    def test_fields_can_use_separate_source_passages(self):
        checked = verify_evidence(document([action()]), SOURCE)
        self.assertEqual(checked.questions, [])
        self.assertEqual(checked.actions[0].deadline, '2026. 10. 2.(금) 16:00까지')
        self.assertEqual(checked.actions[0].destination, '교육지원청')

    def test_field_errors_are_grouped_without_losing_good_fields(self):
        checked = verify_evidence(document([action(deadline='2026-10-09', destination='다른 기관')]), SOURCE)
        self.assertEqual(len(checked.questions), 1)
        self.assertIn('기한·제출처', checked.questions[0])
        self.assertEqual(checked.actions[0].owner, '학교')

    def test_absent_optional_fields_do_not_create_warning_placeholders(self):
        item = action(deadline='', destination='', deliverable='', field_evidence=FieldEvidence())
        checked = verify_evidence(document([item]), SOURCE)
        output = render_document(checked, '업무 일정·체크리스트')
        self.assertEqual(checked.questions, [])
        self.assertNotIn('제출처:', output)
        self.assertNotIn('기한 확인 필요', output)

    def test_notice_recipient_is_not_a_submission_destination(self):
        text = '담임은 학생에게 수칙을 안내한다.'
        item = action(action='수칙 안내', owner='담임', deadline='', deliverable='수칙', destination='학생', evidence=text,
                      requires_submission=False, field_evidence=FieldEvidence())
        checked = verify_evidence(document([item]), text)
        self.assertEqual(checked.questions, [])
        self.assertEqual(checked.actions[0].destination, '')
        self.assertNotIn('제출처:', render_document(checked, '업무 일정·체크리스트'))

    def test_unrelated_value_elsewhere_is_not_accepted_without_field_quote(self):
        item = action(destination='다른 기관', field_evidence=FieldEvidence(deadline=SOURCE.splitlines()[1]))
        self.assertIn('제출처', inspect_action(item, SOURCE + '\n다른 행사의 제출처: 다른 기관'))

    def test_fake_quote_and_cross_cell_join_rejected(self):
        item = action(field_evidence=FieldEvidence(deadline='2026. 10. 9.', destination='교육지원청'))
        self.assertIn('기한', inspect_action(item, SOURCE))
        item.evidence = '학교는과학행사참가신청서를제출한다.'
        self.assertIn('업무 근거', inspect_action(item, '학교는\t과학행사 참가 신청서를 제출한다.'))
