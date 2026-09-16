"""V2 A02-A06: synthetic local contracts, no live API or real documents."""
import unittest
from pydantic import ValidationError
from services.analysis_document import (Action, AnalysisDocument, FieldEvidence, action_card_data,
                                         assess_conditions, scope_notice, strict_schema, verify_evidence)
from services.date_evidence import date_mentions, date_status, source_schedules, supported_deadline
from services.source_contract import FieldRefs, SourceAction, SourceDocument, SourceRef, resolve_action
from services.source_review import review_spans, highlight_source
from unittest.mock import MagicMock


def action(**changes):
    data = dict(action='프로그램 신청서 제출', owner='', deadline='', deliverable='', destination='',
                evidence='모든 학교는 프로그램 신청서를 제출한다.', kind='명시된 의무')
    data.update(changes)
    return Action(**data)


class V2SemanticsTests(unittest.TestCase):
    def test_a02_date_roles_are_independent_and_do_not_invent_time(self):
        lines = ['모든 학교는 프로그램 신청서를 제출한다.', '신청 마감: 2026. 10. 15.',
                 '행사일: 2026. 10. 20.', '보고일: 2026. 10. 23.']
        item = action(deadline='2026-10-15', event_date='2026-10-20', report_date='2026-10-23',
                      field_evidence=FieldEvidence(deadline=lines[1], event_date=lines[2], report_date=lines[3]))
        data = action_card_data(item, '\n'.join(lines))
        for field, expected in [('deadline','2026. 10. 15.'),('event_date','2026. 10. 20.'),('report_date','2026. 10. 23.')]:
            self.assertEqual(data[field], expected)
            self.assertTrue(data['fields'][field]['verified'])
            self.assertFalse(data['date_states'][field]['time_specified'])
            self.assertNotIn('23:59', data[field])
        self.assertEqual(data['obligation'], '필수')

    def test_a03_missing_year_and_relative_date_are_preserved(self):
        self.assertIsNone(supported_deadline('2026-10-15', '10. 15.까지'))
        self.assertEqual(supported_deadline('10월 15일', '10. 15.까지'), '10. 15.까지')
        for raw in ('행사 3일 전', '공문 접수 후 5일 이내'):
            self.assertEqual(supported_deadline(raw, '제출 기한: ' + raw), raw)
            self.assertTrue(date_status(raw)['relative'])
        self.assertIn('연도 미지정', date_status('10. 15.까지')['issues'])

    def test_a03_weekday_conflict_keeps_both_values_for_review(self):
        raw = '2026. 10. 17.(금)'
        date = date_mentions(raw)[0]
        self.assertTrue(date.valid)
        self.assertFalse(date.weekday_matches)
        self.assertEqual(date.calendar_weekday, '토')
        item = action(deadline=raw, field_evidence=FieldEvidence(deadline=raw))
        card = action_card_data(item, item.evidence + '\n' + raw)
        self.assertEqual(card['deadline'], raw)
        self.assertIn('원문 금요일 / 달력 토요일', '\n'.join(card['issues']))

    def test_a03_unknown_year_does_not_claim_weekday_conflict(self):
        self.assertIsNone(date_mentions('10. 17.(금)')[0].weekday_matches)
        self.assertNotIn('날짜·요일 불일치', '\n'.join(date_status('10. 17.(금)')['issues']))

    def test_a03_weekday_conflict_is_red_without_mutating_source(self):
        raw = '행사 2026. 10. 17.(금) / 정상 2026. 10. 17.(토) / 연도 미상 10. 17.(금)'
        spans = review_spans(raw)
        self.assertEqual([raw[a:b].strip() for a,b in spans], ['2026. 10. 17.(금)'])
        widget = MagicMock()
        widget.get.return_value = raw
        highlight_source(widget)
        widget.delete.assert_not_called()
        widget.insert.assert_not_called()
        tags = [call.args[0] for call in widget.tag_add.call_args_list]
        self.assertEqual(tags.count('source_date'), 2)
        self.assertEqual(tags.count('source_review'), 1)

    def test_a03_table_event_and_cell_ref_remain_distinct(self):
        source = '항목\t경제 프로그램\t과학 프로그램\n신청\t2026. 10. 15.\t2026. 10. 16.\n행사\t2026. 10. 20.\t2026. 10. 21.'
        refs = FieldRefs(owner=SourceRef(line=0,cell=0), deadline=SourceRef(line=2,cell=2),
                         event_date=SourceRef(line=3,cell=2), deliverable=SourceRef(line=0,cell=0),
                         destination=SourceRef(line=0,cell=0))
        wire = SourceAction(**(action(deadline='2026. 10. 15.', event_date='2026. 10. 20.').model_dump()
                              | {'evidence': {'line':2,'cell':1}, 'field_evidence':refs.model_dump()}))
        result = resolve_action(wire, source)
        self.assertEqual(result.field_evidence.deadline, '2026. 10. 15.')
        self.assertEqual(result.field_evidence.event_date, '2026. 10. 20.')
        self.assertIn(('과학 프로그램 · 신청', '2026. 10. 16.'), source_schedules(source))

    def test_a03_conflicting_dates_do_not_select_a_favorite(self):
        quote = '신청 기한: 본문 2026. 10. 15. / 붙임 2026. 10. 16.'
        item = action(deadline='2026-10-15', field_evidence=FieldEvidence(deadline=quote))
        card = action_card_data(item, item.evidence + '\n' + quote)
        self.assertEqual(card['deadline'], '2026-10-15')
        self.assertFalse(card['fields']['deadline']['verified'])
        self.assertIn('충돌 확인 필요', '\n'.join(card['issues']))

    def test_a04_optional_omission_and_none_reply_are_not_equivalent(self):
        cases = [('희망 학교만 신청서를 제출한다.', '조건부'),
                 ('해당 사항 없으면 제출 생략', '조건부'),
                 ("해당 사항 없으면 '없음'으로 회신", '필수'),
                 ('모든 학교 제출', '필수'), ('참고하시기 바랍니다.', '안내')]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(assess_conditions(text)['obligation'], expected)
        reply = action_card_data(action(evidence=cases[2][0]), cases[2][0])
        self.assertEqual(reply['obligation'], '필수')
        self.assertIn('회신', '\n'.join(reply['issues']))

    def test_a04_unrelated_condition_does_not_apply_to_this_action(self):
        item = action(obligation='필수')
        card = action_card_data(item, item.evidence + '\n희망 학교만 문화행사에 참여한다.')
        self.assertEqual(card['obligation'], '필수')

    def test_a05_missing_attachment_is_not_no_task(self):
        quote = '세부 사항은 붙임 참조'
        card = action_card_data(action(evidence=quote, obligation='필수'), quote)
        self.assertEqual(card['obligation'], '판단 유보')
        self.assertIn('붙임 확인 필요', '\n'.join(card['issues']))
        self.assertTrue(card['action'])
        self.assertIn('가져온 범위에서는', scope_notice(partial=True))
        self.assertNotIn('업무가 없습니다', scope_notice(partial=True))

    def test_a05_loaded_attachment_is_not_automatically_missing(self):
        self.assertEqual(assess_conditions('세부 사항은 붙임 참조', attachment_available=True)['issues'], [])

    def test_a06_fabricated_quote_is_not_verified_and_value_is_retained(self):
        card = action_card_data(action(deadline='2026-10-15', evidence='없는 문장'), '프로그램 안내')
        self.assertFalse(card['fields']['deadline']['verified'])
        self.assertEqual(card['fields']['deadline']['locations'], [])
        self.assertEqual(card['deadline'], '2026-10-15')
        self.assertEqual(card['obligation'], '판단 유보')

    def test_a06_fabricated_page_number_not_accepted_by_contract(self):
        with self.assertRaises(ValidationError):
            SourceRef(line=1, cell=0, page=99)
        self.assertEqual(SourceRef(line=0, cell=0).model_dump(), {'line':0,'cell':0})

    def test_a06_duplicate_quote_is_not_presented_as_unique_location(self):
        item = action(deadline='10. 15.', field_evidence=FieldEvidence(deadline='10. 15.'))
        card = action_card_data(item, item.evidence + '\n10. 15.\n10. 15.')
        self.assertEqual(len(card['fields']['deadline']['locations']), 2)
        self.assertIn('여러 위치', '\n'.join(card['field_issues']['deadline']))

    def test_schema_requires_new_wire_fields_but_old_records_still_load(self):
        old = action()
        self.assertEqual(old.target, '')
        self.assertEqual(old.obligation, '판단 유보')
        schema = strict_schema(SourceDocument)
        definition = schema['$defs']['SourceAction']
        for field in ('target','condition','obligation','deadline','event_date','report_date'):
            self.assertIn(field, definition['required'])

    def test_legacy_verifier_uses_each_date_quote_and_card_keeps_unknown_values(self):
        quote = '모든 학교는 프로그램 신청서를 제출한다.'
        item = action(deadline='2026-10-15', event_date='2026-10-20', field_evidence=FieldEvidence(event_date='행사 2026. 10. 20.'))
        doc = AnalysisDocument(title='안내',summary='',actions=[item],questions=[],message='')
        source = quote + '\n행사 2026. 10. 20.'
        checked = verify_evidence(doc, source)
        self.assertEqual(checked.actions[0].event_date, '2026. 10. 20.')
        self.assertEqual(checked.actions[0].deadline, '')
        self.assertEqual(action_card_data(item, source)['deadline'], '2026-10-15')


if __name__ == '__main__':
    unittest.main()
