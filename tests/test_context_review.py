import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from services.analysis_document import (Action, AnalysisDocument, FieldEvidence,
    inspect_action, verify_evidence, render_document, strict_schema)
from services.analysis_review import repair_actions, ReviewResult
from services.personal_todos import checklist_items

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

    def test_bad_action_quote_produces_one_question_not_cascade(self):
        checked = verify_evidence(document([action(evidence='존재하지 않는 문장')]), SOURCE)
        self.assertEqual(len(checked.questions), 1)
        self.assertIn('업무 근거', checked.questions[0])
        self.assertTrue(all(not getattr(checked.actions[0], f) for f in ('owner','deadline','deliverable','destination')))
        self.assertEqual(checklist_items(render_document(checked, '업무 일정·체크리스트')), [])

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

    def test_external_actions_are_not_personal_checklist_items(self):
        external = action(action='참가 명단 안내', task_type='외부 기관 업무', owner='교육지원청',
                          deadline='', destination='', deliverable='', evidence=SOURCE.splitlines()[-1], field_evidence=FieldEvidence())
        result = render_document(verify_evidence(document([action(), external]), SOURCE), '업무 일정·체크리스트')
        self.assertEqual(len(checklist_items(result)), 1)
        self.assertIn('외부 기관·참고 사항', result)

    def test_reference_events_remain_visible_but_not_checkable(self):
        item = action(task_type='참고 일정')
        output = render_document(verify_evidence(document([item]), SOURCE), '업무 일정·체크리스트')
        self.assertEqual(checklist_items(output), [])
        self.assertIn('[참고 일정]', output)

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

    def test_good_analysis_never_calls_repair(self):
        client = MagicMock()
        result, status = repair_actions(client, document([action()]), SOURCE, 'gpt-5-nano', {}, lambda: None)
        self.assertEqual(status, 'not_needed')
        client.with_options.assert_not_called()

    def test_repair_replaces_only_target_and_revalidates(self):
        original = document([action(deadline='2026-10-09'), action()])
        client = client_for([{'index': 0, 'action': action().model_dump()}])
        result, status = repair_actions(client, original, SOURCE, 'gpt-5-nano', {}, lambda: None)
        self.assertEqual(status, 'completed')
        self.assertEqual(inspect_action(result.actions[0], SOURCE), [])
        self.assertEqual(original.actions[0].deadline, '2026-10-09')
        self.assertEqual(result.actions[1], original.actions[1])
        client.responses.create.assert_called_once()
        client.with_options.assert_called_once_with(timeout=30, max_retries=0)

    def test_invalid_repair_does_not_make_bad_facts_valid(self):
        original = document([action(deadline='2026-10-09')])
        client = client_for([{'index': 0, 'action': action(deadline='2026-10-10').model_dump()}])
        result, _ = repair_actions(client, original, SOURCE, 'gpt-5-nano', {}, lambda: None)
        self.assertEqual(result, original)

    def test_repair_rejects_duplicate_or_unrequested_indices(self):
        for indices in ([0, 0], [1]):
            client = client_for([{'index': i, 'action': action().model_dump()} for i in indices])
            result, status = repair_actions(client, document([action(deadline='2026-10-09')]), SOURCE, 'gpt-5-nano', {}, lambda: None)
            self.assertEqual(status, 'failed')
            self.assertEqual(result.actions[0].deadline, '2026-10-09')

    def test_repair_timeout_preserves_result_and_reports_failure(self):
        client = client_for([])
        client.responses.create.side_effect = TimeoutError()
        result, status = repair_actions(client, document([action(deadline='2026-10-09')]), SOURCE, 'gpt-5-nano', {}, lambda: None)
        self.assertEqual(status, 'failed')
        self.assertTrue(result.questions)

    def test_repair_cancellation_propagates(self):
        def cancel():
            raise RuntimeError('취소')
        with self.assertRaisesRegex(RuntimeError, '취소'):
            repair_actions(MagicMock(), document([action(deadline='2026-10-09')]), SOURCE, 'gpt-5-nano', {}, cancel)

    def test_repair_limit_is_one_call_eight_items(self):
        client = client_for([])
        repair_actions(client, document([action(deadline='2026-10-09') for _ in range(12)]), SOURCE, 'gpt-5-nano', {}, lambda: None)
        client.responses.create.assert_called_once()
        self.assertEqual(len(json.loads(client.responses.create.call_args.kwargs['input'])['items']), 8)

    def test_all_api_schema_properties_required_including_nested(self):
        def walk(node):
            if isinstance(node, dict):
                self.assertNotIn('default', node)
                if node.get('type') == 'object':
                    self.assertEqual(set(node['properties']), set(node['required']))
                    self.assertFalse(node['additionalProperties'])
                for value in node.values(): walk(value)
            elif isinstance(node, list):
                for value in node: walk(value)
        walk(strict_schema(AnalysisDocument))
        walk(strict_schema(ReviewResult))

    def test_source_coordinates_preserve_exact_date_cell(self):
        from services.source_contract import SourceRef, resolve_ref
        source = '항목\t시 대회\t도 대회\n접수\t2026. 8. 28.\t2026. 9. 30.'
        self.assertEqual(resolve_ref(SourceRef(line=2, cell=2), source), '2026. 8. 28.')
        self.assertEqual(resolve_ref(SourceRef(line=2, cell=3), source), '2026. 9. 30.')
        for line, cell in ((0,0), (9,0), (1,8), (-1,0), (1,-1)):
            self.assertEqual(resolve_ref(SourceRef(line=line, cell=cell), source), '')

    def test_review_failure_is_not_cached_and_success_is_cached(self):
        from services.ai_service import analyze_document_task
        from services.analysis_cache import AnalysisCache
        for failure in (False, True):
            client = client_for([{'index': 0, 'action': action().model_dump()}])
            client.__enter__.return_value = client
            # Reuse the valid wire shape but seed an incorrect date in the first pass.
            wire = json.loads(client.responses.create.return_value.output_text)['corrections'][0]['action']
            wire['deadline'] = '2026-10-09'
            payload = document([]).model_dump()
            payload['actions'] = [wire]
            stream = MagicMock()
            stream.__enter__.return_value = stream
            stream.get_final_response.return_value = SimpleNamespace(status='completed', output_text=json.dumps(payload))
            client.responses.stream.return_value = stream
            if failure:
                client.responses.create.side_effect = TimeoutError()
            with tempfile.TemporaryDirectory() as directory, patch('openai.OpenAI', return_value=client), patch.dict('os.environ', {'OPENAI_API_KEY': 'test-key'}):
                result = analyze_document_task(SOURCE, cache_dir=directory, raise_errors=True)
                cached = AnalysisCache(directory).get(AnalysisCache.key(SOURCE, '전체 담당자', 'gpt-5-nano'))
                self.assertEqual(cached is None, failure)
                self.assertEqual(bool(checklist_items(result)), not failure)

    def test_incomplete_review_preserves_first_pass(self):
        client = client_for([])
        client.responses.create.return_value.status = 'incomplete'
        result, status = repair_actions(client, document([action(deadline='2026-10-09')]), SOURCE, 'gpt-5-nano', {}, lambda: None)
        self.assertEqual(status, 'failed')
        self.assertEqual(result.actions[0].deadline, '2026-10-09')

    def test_cancellation_after_request_does_not_return_repaired_result(self):
        client = client_for([])
        calls = []
        def cancel():
            calls.append(1)
            if len(calls) > 1:
                raise RuntimeError('취소')
        with self.assertRaisesRegex(RuntimeError, '취소'):
            repair_actions(client, document([action(deadline='2026-10-09')]), SOURCE, 'gpt-5-nano', {}, cancel)
