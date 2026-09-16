"""Offline unlinked-date recall without restoring teachers' changed deadlines."""
import copy
import tempfile
import unittest

from services.analysis_document import Action, FieldEvidence, action_card_data
from services.card_outputs import render_current_cards, unlinked_source_schedules
from services.personal_todos import checklist_items
from services.source_contract import SourceAction, resolve_action
from services.work_card_store import WorkCardStore


class SourceScheduleFallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = WorkCardStore(self.temp.name)

    def save(self, action, source):
        self.store.ensure_document('synthetic', source)
        return self.store.merge_analysis('synthetic', [action_card_data(action, source)])[0]

    def plain_card(self, *, in_action=False):
        text = '모든 학교는 2026. 10. 15.까지 신청서를 제출한다.' if in_action else '모든 학교는 신청서를 제출한다.'
        source = text + '\n신청 마감: 2026. 10. 15.\n보고일: 2026. 10. 23.'
        action = Action(action='신청서 제출', owner='', deadline='2026. 10. 15.', deliverable='', destination='',
            evidence=text, kind='명시된 의무', obligation='필수',
            field_evidence=FieldEvidence(deadline=source.splitlines()[1]))
        return self.save(action,source), source

    def test_missing_ai_date_remains_reference_not_checkbox(self):
        card, source = self.plain_card()
        output = render_current_cards([card], '업무 일정·체크리스트', source=source)
        self.assertIn('## 원문 미연결 참고 일정 · 확인 후 업무에 반영', output)
        section = output.split('## 원문 미연결 참고 일정')[1]
        self.assertIn('2026. 10. 23.', section)
        self.assertNotIn('2026. 10. 15.', section)
        self.assertEqual(len(checklist_items(output)), 1)
        self.assertNotIn('2026. 10. 23.', checklist_items(output)[0])

    def test_changed_and_explicitly_cleared_deadlines_do_not_reappear(self):
        card, source = self.plain_card()
        for value in ('2026. 10. 16.', ''):
            card = self.store.update_card(card['id'], {'deadline': value}, card['version'])
            output = render_current_cards([card], '업무 일정·체크리스트', source=source)
            self.assertNotIn('2026. 10. 15.', output)
            self.assertIn('2026. 10. 23.', output)

    def test_action_quote_dates_are_covered_after_separate_field_change(self):
        card, source = self.plain_card(in_action=True)
        card = self.store.update_card(card['id'], {'deadline': ''}, card['version'])
        entries = unlinked_source_schedules([card], source)
        self.assertEqual([entry['text'] for entry in entries], ['2026. 10. 23.'])

    def test_equal_dates_in_different_event_cells_are_not_globally_hidden(self):
        source = '항목\t경제 체험\t과학 체험\n신청\t2026. 10. 15.\t2026. 10. 15.'
        action = SourceAction(action='경제 체험 신청', owner='', deadline='2026. 10. 15.', deliverable='', destination='',
            evidence={'line':2,'cell':1}, kind='명시된 의무', obligation='필수',
            field_evidence={'owner':{'line':0,'cell':0},'deadline':{'line':2,'cell':2},
                            'deliverable':{'line':0,'cell':0},'destination':{'line':0,'cell':0}})
        card = self.save(resolve_action(action,source),source)
        card = self.store.update_card(card['id'], {'deadline': ''}, card['version'])
        entries = unlinked_source_schedules([card], source)
        self.assertEqual(len(entries), 1)
        self.assertEqual((entries[0]['line'], entries[0]['cell']), (2,3))
        self.assertIn('과학 체험', entries[0]['context'])

    def test_broad_action_quote_does_not_cover_other_event_columns(self):
        source = '항목\t경제 체험\t과학 체험\n신청\t2026. 10. 15.\t2026. 10. 16.'
        action = Action(action='경제 체험 신청', owner='', deadline='', deliverable='', destination='',
                        evidence=source.splitlines()[1], kind='명시된 의무', obligation='필수')
        card = self.save(action, source)
        self.assertEqual(len(unlinked_source_schedules([card], source)), 2)

    def test_unverified_stale_or_wrong_spans_never_hide_dates(self):
        card, source = self.plain_card()
        for change in ({'verified':False}, {'stale':True}, {'ambiguous':True},
                       {'locations':[{'start':0,'end':4,'line':1}]}):
            changed = copy.deepcopy(card)
            changed['fields']['deadline']['evidence'].update(change)
            self.assertEqual(len(unlinked_source_schedules([changed],source)), 2)

    def test_only_schedule_modes_show_reference_fallback(self):
        card, source = self.plain_card()
        for mode in ('교직원 메신저','학부모 메신저','가정통신문 초안','원문 요약'):
            output = render_current_cards([card], mode, source=source)
            self.assertNotIn('원문 미연결 참고 일정', output)
            self.assertNotIn('2026. 10. 23.', output)

    def test_no_cards_still_show_source_dates_without_creating_tasks(self):
        output = render_current_cards([], '업무 일정·체크리스트', source='설명회 2026. 10. 20.')
        self.assertIn('2026. 10. 20.', output)
        self.assertIn('확정된 업무나 내 할 일이 아닙니다', output)
        self.assertEqual(checklist_items(output), [])

    def test_comparison_candidates_do_not_hide_unassigned_dates(self):
        card, source = self.plain_card()
        card['comparison_candidate'] = True
        self.assertEqual(len(unlinked_source_schedules([card],source)), 2)

    def test_original_text_is_not_used_when_safe_source_has_no_date(self):
        card, _ = self.plain_card()
        safe = '가린 전송 사본: 일정 [가림]'
        self.assertEqual(unlinked_source_schedules([card],safe), [])
        output = render_current_cards([], '업무 일정·체크리스트', source=safe)
        self.assertNotIn('2026.', output)


if __name__ == '__main__':
    unittest.main()
