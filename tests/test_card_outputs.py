"""Current-card drafting acceptance checks; local synthetic data and no API."""
import tempfile
import unittest

from services.analysis_document import Action, FieldEvidence, action_card_data
from services.card_outputs import current_value, render_current_cards
from services.personal_todos import PersonalTodoStore
from services.work_card_store import WorkCardStore
from services.personal_todos import checklist_items


class CardOutputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = WorkCardStore(self.temp.name)
        self.todos = PersonalTodoStore(self.temp.name, self.store)

    def create(self, document_id='synthetic', **changes):
        source = '\n'.join([
            '희망 학급만 프로그램 신청서를 제출한다.', '담당: 담임', '대상: 학생·학부모',
            '조건: 참가를 희망하는 학급만', '신청 마감: 2026. 10. 15.', '행사일: 2026. 10. 20.',
            '보고일: 2026. 10. 23.', '제출물: 신청서', '제출처: 교육지원청',
        ])
        data = dict(action='프로그램 신청서 제출', owner='담임', target='학생·학부모',
                    condition='참가를 희망하는 학급만', obligation='조건부', deadline='2026. 10. 15.',
                    event_date='2026. 10. 20.', report_date='2026. 10. 23.', deliverable='신청서',
                    destination='교육지원청', evidence=source.splitlines()[0], kind='명시된 의무',
                    field_evidence=FieldEvidence(owner='담당: 담임', target='대상: 학생·학부모',
                        condition='조건: 참가를 희망하는 학급만', deadline='신청 마감: 2026. 10. 15.',
                        event_date='행사일: 2026. 10. 20.', report_date='보고일: 2026. 10. 23.',
                        deliverable='제출물: 신청서', destination='제출처: 교육지원청'))
        data.update(changes)
        action = Action(**data)
        self.store.ensure_document(document_id, source)
        return self.store.merge_analysis(document_id, [action_card_data(action, source)])[0]

    def test_a01_teacher_date_matches_linked_todo_and_new_draft(self):
        card = self.create()
        self.todos.add_cards([card])
        edited = self.store.update_card(card['id'], {'deadline': '2026. 10. 16.'}, card['version'])
        reopened = WorkCardStore(self.temp.name).get_card(card['id'])
        output = render_current_cards([reopened], '교직원 메신저')
        todo = self.todos.list()[0]['item']
        self.assertIn('제출 기한: 2026. 10. 16.', output)
        self.assertIn('제출 기한: 2026. 10. 16.', todo)
        self.assertNotIn('2026. 10. 15.', output)
        self.assertEqual(current_value(edited, 'deadline'), '2026. 10. 16.')

    def test_a07_explicit_deleted_deadline_is_not_reintroduced_into_output(self):
        card = self.create()
        self.store.update_card(card['id'], {'deadline': ''}, card['version'])
        proposal = self.store.get_card(card['id'])['ai_proposal']
        merged = self.store.merge_analysis(card['document_id'], [proposal])[0]
        output = render_current_cards([merged], '업무 일정·체크리스트')
        self.assertNotIn('2026. 10. 15.', output)
        self.assertNotIn('제출 기한:', output)
        self.assertIn('행사일: 2026. 10. 20.', output)

    def test_a09_historical_artifact_preserved_new_output_has_new_version(self):
        card = self.create()
        content = render_current_cards([card], '교직원 메신저')
        old = self.store.save_artifact(card['document_id'], '교직원 메신저', content, {card['id']: card['version']}, 1)
        edited = self.store.update_card(card['id'], {'deadline': '2026. 10. 16.'}, card['version'])
        new = self.store.save_artifact(card['document_id'], '교직원 메신저',
            render_current_cards([edited], '교직원 메신저'), {edited['id']: edited['version']}, 1)
        self.assertTrue(self.store.artifact_is_stale(old))
        self.assertFalse(self.store.artifact_is_stale(new))
        history = self.store.list_artifacts(card['document_id'])
        self.assertEqual(len(history), 2)
        self.assertIn('2026. 10. 15.', history[1]['content'])
        self.assertIn('2026. 10. 16.', history[0]['content'])

    def test_parent_draft_excludes_internal_report_deadline_and_personal_notes(self):
        card = self.create()
        card = self.store.update_card(card['id'], {'notes': '개인 메모 합성 문자열', 'preparation_date': '2026. 10. 12.'}, card['version'])
        for mode in ('학부모 메신저', '가정통신문 초안'):
            with self.subTest(mode=mode):
                output = render_current_cards([card], mode)
                self.assertIn('2026. 10. 20.', output)
                self.assertNotIn('2026. 10. 23.', output)
                self.assertNotIn('2026. 10. 15.', output)
                self.assertNotIn('교육지원청', output)
                self.assertNotIn('개인 메모 합성 문자열', output)
                self.assertNotIn('2026. 10. 12.', output)

    def test_a02_a04_internal_output_retains_condition_and_separate_date_roles(self):
        output = render_current_cards([self.create()], '업무 일정·체크리스트')
        self.assertIn('[조건부]', output)
        self.assertIn('참가를 희망하는 학급만', output)
        self.assertIn('제출 기한: 2026. 10. 15.', output)
        self.assertIn('행사일: 2026. 10. 20.', output)
        self.assertIn('보고일: 2026. 10. 23.', output)
        self.assertNotIn('18:00', output)
        self.assertNotIn('23:59', output)

    def test_parent_event_keeps_participation_condition_even_with_teacher_owner(self):
        output = render_current_cards([self.create()], '학부모 메신저')
        self.assertIn('참가를 희망하는 학급만', output)

    def test_unsupported_fact_shows_placeholder_and_candidate_not_auto_included(self):
        card = self.create()
        card['fields']['deadline']['evidence']['verified'] = False
        card['fields']['deadline']['issues'] = ['근거 확인 필요']
        output = render_current_cards([card], '교직원 메신저')
        self.assertNotIn('2026. 10. 15.', output)
        self.assertIn('제출 기한: [확인 필요]', output)
        card['comparison_candidate'] = True
        output = render_current_cards([card], '교직원 메신저')
        self.assertNotIn('프로그램 신청서 제출', output)

    def test_public_reply_retains_supported_consent_form_and_reply_destination(self):
        card = self.create()
        card = self.store.update_card(card['id'], {
            'action': '참가 동의서를 회신해 주세요', 'owner': '학부모',
            'deliverable': '참가 동의서', 'destination': '담임 교사',
        }, card['version'])
        output = render_current_cards([card], '학부모 메신저')
        self.assertIn('준비·회신 자료: 참가 동의서', output)
        self.assertIn('회신처: 담임 교사', output)
        self.assertIn('신청 기한: 2026. 10. 15.', output)
        card = self.store.update_card(card['id'], {'destination': '내부 결재 담당 행정실'}, card['version'])
        self.assertNotIn('내부 결재 담당 행정실', render_current_cards([card], '학부모 메신저'))

    def test_external_reference_proposed_and_pending_are_not_school_checkboxes(self):
        base = self.create()
        import copy
        cards = [base]
        for index, (task_type, obligation, title) in enumerate([
            ('외부 기관 업무', '필수', '교육청 명단 발표'),
            ('참고 일정', '안내', '행사 일정 참고'),
            ('학교 업무', '추가 제안', '개인 준비 제안'),
            ('학교 업무', '판단 유보', '붙임 확인 대기'),
        ]):
            item = copy.deepcopy(base)
            item['id'] = 'synthetic-' + str(index)
            item['ai_proposal']['task_type'] = task_type
            item['fields']['action'].update(value=title, edited=True)
            item['fields']['obligation'].update(value=obligation, edited=True)
            cards.append(item)
        output = render_current_cards(cards, '업무 일정·체크리스트')
        items = checklist_items(output)
        self.assertEqual(len(items), 1)
        self.assertIn('프로그램 신청서 제출', items[0])
        self.assertIn('## 참고 일정·외부 기관 안내', output)
        self.assertIn('교육청 명단 발표', output)
        self.assertIn('## 추가 제안 · 공문상 의무 아님', output)
        self.assertIn('개인 준비 제안', output)
        self.assertIn('## 판단 유보 · 확인 후 적용', output)
        self.assertIn('붙임 확인 대기', output)

    def _date_case(self, raw, *, action_text='신청서 제출'):
        source = '모든 학교는 신청서를 제출한다.\n신청 기한: ' + raw
        action = Action(action=action_text, owner='', deadline=raw, deliverable='', destination='',
            evidence=source.splitlines()[0], kind='명시된 의무',
            field_evidence=FieldEvidence(deadline=source.splitlines()[1]))
        self.store.ensure_document('date-case', source)
        return self.store.merge_analysis('date-case', [action_card_data(action, source)])[0]

    def test_missing_year_keeps_original_date_and_action_checkbox_with_warning(self):
        card = self._date_case('10월 2일까지')
        output = render_current_cards([card], '업무 일정·체크리스트')
        self.assertIn('10월 2일까지 [확인 필요: 연도 미지정]', output)
        self.assertNotIn('2026', output)
        self.assertEqual(len(checklist_items(output)), 1)

    def test_relative_deadline_keeps_expression_without_invented_base_or_blocking_action(self):
        card = self._date_case('행사 3일 전')
        output = render_current_cards([card], '업무 일정·체크리스트')
        self.assertIn('행사 3일 전 [확인 필요: 상대 기한: 기준일 확인 필요]', output)
        self.assertEqual(len(checklist_items(output)), 1)

    def test_weekday_conflict_preserves_surface_but_is_not_ready_checkbox(self):
        card = self._date_case('2026. 10. 17.(금)')
        output = render_current_cards([card], '업무 일정·체크리스트')
        self.assertIn('2026. 10. 17.(금)', output)
        self.assertIn('원문 금요일 / 달력 토요일', output)
        self.assertIn('## 판단 유보 · 확인 후 적용', output)
        self.assertEqual(checklist_items(output), [])

    def test_ai_action_date_does_not_restore_teacher_cleared_or_changed_deadline(self):
        card = self._date_case('2026. 10. 15.', action_text='2026. 10. 15.까지 신청서 제출')
        for value in ('2026. 10. 16.', ''):
            with self.subTest(current_deadline=value):
                card = self.store.update_card(card['id'], {'deadline': value}, card['version'])
                output = render_current_cards([card], '업무 일정·체크리스트')
                self.assertNotIn('2026. 10. 15.', output)
                self.assertIn('최신 날짜 항목 참조', output)
                if value:
                    self.assertIn(value, output)
        self.assertIn('2026. 10. 15.', self.store.get_card(card['id'])['fields']['action']['ai_value'])

    def test_teacher_owned_action_is_not_silently_rewritten(self):
        card = self._date_case('2026. 10. 15.')
        teacher_text = '2026. 10. 16. 설명회 후 직접 안내'
        card = self.store.update_card(card['id'], {'action': teacher_text}, card['version'])
        self.assertEqual(current_value(card, 'action'), teacher_text)


if __name__ == '__main__':
    unittest.main()
