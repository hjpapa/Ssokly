"""Synthetic follow-up regressions: date surfaces and source cell provenance."""
import tempfile
import unittest

from services.analysis_cache import AnalysisCache
from services.analysis_document import Action, EvidenceLocation, FieldEvidence, action_card_data, strict_schema
from services.date_evidence import date_mentions, date_status, source_schedule_entries, supported_deadline
from services.source_contract import SourceAction, SourceDocument, resolve_action
from services.source_review import review_spans


def source_action(**changes):
    data = dict(action='경제 체험 신청', owner='', deadline='2026. 10. 15.', deliverable='', destination='',
                evidence={'line': 2, 'cell': 1}, kind='명시된 의무', obligation='필수',
                field_evidence={'owner': {'line':0,'cell':0}, 'deadline': {'line':2,'cell':2},
                                'deliverable': {'line':0,'cell':0}, 'destination': {'line':0,'cell':0}})
    data.update(changes)
    return SourceAction(**data)


class DateContextRegressionTests(unittest.TestCase):
    def test_short_range_end_is_preserved_and_reviewed_not_inferred(self):
        raw = '2026. 10. 15.(목) ~ 16.(금)'
        self.assertEqual(supported_deadline(raw, raw), raw)
        self.assertEqual(date_mentions(raw)[0].text, raw)
        self.assertTrue(date_mentions(raw)[0].partial_range)
        self.assertIn('축약된 기간 끝', '\n'.join(date_status(raw)['issues']))
        self.assertNotIn('2026. 10. 16.', supported_deadline(raw, raw))

    def test_explicit_afternoon_time_is_preserved_and_compared(self):
        raw = '2026. 10. 15.(목) 오후 2시까지'
        self.assertEqual(supported_deadline(raw, raw), raw)
        self.assertEqual(supported_deadline('2026-10-15 14:00', raw), raw)
        self.assertIsNone(supported_deadline('2026-10-15 02:00', raw))
        self.assertEqual(date_mentions(raw)[0].times, ((14, 0),))
        self.assertTrue(date_status(raw)['time_specified'])

    def test_midnight_noon_and_invalid_meridiem_are_not_conflated(self):
        self.assertEqual(date_mentions('2026. 10. 15. 오전 12시')[0].times, ((0,0),))
        self.assertEqual(date_mentions('2026. 10. 15. 오후 12시')[0].times, ((12,0),))
        self.assertFalse(date_mentions('2026. 10. 15. 오후 13시')[0].valid)

    def test_partially_parsed_literal_time_keeps_surface_and_review_reason(self):
        raw = '2026. 10. 15.(목) 오후 2시 반까지'
        self.assertEqual(supported_deadline(raw, '제출: ' + raw), raw)
        self.assertIn('일부 해석', '\n'.join(date_status(raw)['issues']))

    def test_half_hour_suffix_survives_normalized_proposal_and_source_fallback(self):
        raw = '2026. 10. 15.(목) 오후 2시 반까지'
        self.assertIsNone(supported_deadline('2026-10-15 14:00', raw))
        self.assertEqual(supported_deadline('2026-10-15', raw), raw)
        self.assertEqual(source_schedule_entries(raw)[0]['text'], raw)
        self.assertTrue(date_status(source_schedule_entries(raw)[0]['text'])['issues'])
        self.assertFalse(date_status(raw)['time_specified'])
        self.assertEqual([raw[start:end].strip() for start,end in review_spans(raw)], [raw])

    def test_ampm_prefix_and_suffix_are_explicit_not_dropped(self):
        for expression in ('PM 2:00', '2:00 PM', '2:00 p.m.', '오후 2:00'):
            raw = '2026. 10. 15. ' + expression
            with self.subTest(expression=expression):
                self.assertEqual(supported_deadline('2026-10-15 14:00',raw), raw)
                self.assertIsNone(supported_deadline('2026-10-15 02:00',raw))
                self.assertEqual(source_schedule_entries(raw)[0]['text'], raw)
                self.assertEqual(date_status(raw)['issues'], [])
                self.assertEqual(review_spans(raw), [])
        self.assertEqual(date_mentions('2026. 10. 15. 12:00 AM')[0].times, ((0,0),))
        self.assertEqual(date_mentions('2026. 10. 15. 12:00 PM')[0].times, ((12,0),))
        self.assertFalse(date_mentions('2026. 10. 15. PM 2:00 AM')[0].valid)

    def test_shared_meridiem_range_preserves_literal_without_clock_inference(self):
        raw = '2026. 10. 15. 오후 2시~4시'
        self.assertEqual(supported_deadline(raw,raw), raw)
        for proposed in ('2026-10-15 14:00~04:00','2026-10-15 14:00~16:00'):
            self.assertIsNone(supported_deadline(proposed,raw))
        self.assertIn('오전·오후 일부 생략', '\n'.join(date_status(raw)['issues']))
        self.assertFalse(date_status(raw)['time_specified'])
        self.assertEqual(source_schedule_entries(raw)[0]['text'], raw)
        self.assertTrue(review_spans(raw))

    def test_explicit_two_sided_meridiem_and_24h_ranges_stay_supported(self):
        for raw in ('2026. 10. 15. 오전 9시~오후 1시', '2026. 10. 15. 09:00~13:00',
                    '2026. 10. 15. 9:00 AM~1:00 PM'):
            with self.subTest(raw=raw):
                self.assertEqual(supported_deadline('2026-10-15 09:00~13:00',raw),raw)
                self.assertEqual(date_status(raw)['issues'], [])
                self.assertTrue(date_status(raw)['time_specified'])

    def test_partial_range_with_half_hour_suffix_keeps_full_end_expression(self):
        raw = '2026. 10. 15.(목) ~ 16.(금) 오후 2시 반까지'
        self.assertEqual(supported_deadline('2026-10-15',raw), raw)
        self.assertEqual(source_schedule_entries(raw)[0]['text'], raw)
        self.assertIn('축약된 기간 끝', '\n'.join(date_status(raw)['issues']))
        self.assertIn('일부 해석', '\n'.join(date_status(raw)['issues']))

    def test_approximate_time_is_not_rewritten_as_exact_time(self):
        raw = '2026. 10. 15. 오후 2시경'
        self.assertEqual(supported_deadline(raw,raw),raw)
        self.assertIsNone(supported_deadline('2026-10-15 14:00',raw))
        self.assertEqual(source_schedule_entries(raw)[0]['text'],raw)
        self.assertIn('대략적인 시각', '\n'.join(date_status(raw)['issues']))

    def test_unparsed_adjacent_time_units_and_ranges_are_not_truncated(self):
        for tail in ('오후 2시 반~4시 반', '오후 2시경까지', '오후 2시 30분 30초까지'):
            raw = '2026. 10. 15. ' + tail
            with self.subTest(tail=tail):
                self.assertEqual(supported_deadline(raw,raw),raw)
                self.assertEqual(supported_deadline('2026-10-15',raw),raw)
                self.assertEqual(source_schedule_entries(raw)[0]['text'],raw)
                self.assertTrue(date_status(raw)['issues'])
                self.assertTrue(review_spans(raw))

    def test_partial_time_proposal_is_not_a_verified_card_fact(self):
        raw = '2026. 10. 15. 오후 2시 반까지'
        source = '모든 학교는 신청서를 제출한다.\n기한: ' + raw
        action = Action(action='신청서 제출',owner='',deadline='2026-10-15 14:00',deliverable='',destination='',
            evidence=source.splitlines()[0],kind='명시된 의무',field_evidence=FieldEvidence(deadline=source.splitlines()[1]))
        payload = action_card_data(action,source)
        self.assertFalse(payload['fields']['deadline']['verified'])
        self.assertIn('근거 확인 필요', payload['field_issues']['deadline'])
        self.assertEqual(source_schedule_entries(source)[0]['text'],raw)

    def test_ordinary_following_words_are_not_time_suffixes(self):
        for tail in ('반별 안내', '경기도 안내'):
            raw = '2026. 10. 15. ' + tail
            self.assertEqual(date_mentions(raw)[0].text, '2026. 10. 15.')
            self.assertEqual(date_status(raw)['issues'], [])

    def test_table_dates_keep_exact_spans_even_with_crlf_and_merged_markers(self):
        source = '행사\t경제 체험\t과학 체험\r\n신청\t2026. 10. 15.\t↳ 2026. 10. 15.\r\n'
        entries = source_schedule_entries(source)
        self.assertEqual(len(entries), 2)
        self.assertEqual([(v['line'],v['cell']) for v in entries], [(2,2),(2,3)])
        self.assertNotEqual(entries[0]['start'], entries[1]['start'])
        for item in entries:
            self.assertEqual(source[item['start']:item['end']].strip(), item['text'])
        self.assertIn('경제 체험', entries[0]['context'])
        self.assertIn('과학 체험', entries[1]['context'])

    def test_selected_same_date_cell_survives_resolution_and_card_payload(self):
        source = '행사\t경제 체험\t과학 체험\n신청\t2026. 10. 15.\t2026. 10. 15.'
        action = resolve_action(source_action(), source)
        data = action_card_data(action, source)
        self.assertEqual(data['evidence_locations']['deadline'], [{'line':2,'cell':2}])
        self.assertEqual(data['fields']['deadline']['locations'], [{'line':2,'cell':2}])
        self.assertNotIn('동일 근거가 여러 위치에 있음', data['fields']['deadline']['issues'])

    def test_unlocated_duplicate_quotes_remain_ambiguous(self):
        source = '모든 학교는 신청서를 제출한다.\n기한: 2026. 10. 15.\n기한: 2026. 10. 15.'
        action = Action(action='신청', owner='', deadline='2026. 10. 15.', deliverable='', destination='',
                        evidence=source.splitlines()[0], kind='명시된 의무',
                        field_evidence=FieldEvidence(deadline='기한: 2026. 10. 15.'))
        data = action_card_data(action, source)
        self.assertEqual(len(data['fields']['deadline']['locations']), 2)
        self.assertIn('동일 근거가 여러 위치에 있음', data['fields']['deadline']['issues'])

    def test_wire_does_not_request_local_metadata_or_trust_supplied_coordinates(self):
        schema = strict_schema(SourceDocument)
        self.assertNotIn('evidence_locations', schema['$defs']['SourceAction']['properties'])
        source = '행사\t경제 체험\t과학 체험\n신청\t2026. 10. 15.\t2026. 10. 16.'
        wire = source_action(evidence_locations={'deadline':[{'line':2,'cell':3}]})
        action = resolve_action(wire, source)
        self.assertEqual(action.model_dump()['evidence_locations']['deadline'], [{'line':2,'cell':2}])

    def test_invalid_selected_coordinate_does_not_override_actual_quote_locations(self):
        source = '행사\t경제 체험\t과학 체험\n신청\t2026. 10. 15.\t2026. 10. 16.'
        action = resolve_action(source_action(), source)
        action.evidence_locations = {'deadline':[EvidenceLocation(line=99,cell=8)]}
        data = action_card_data(action, source)
        self.assertEqual(data['fields']['deadline']['locations'], [{'line':2,'cell':2}])
        self.assertIn('선택한 원문 위치 확인 필요', data['fields']['deadline']['issues'])

    def test_local_reference_metadata_survives_analysis_cache_roundtrip(self):
        source = '행사\t경제 체험\n신청\t2026. 10. 15.'
        from services.analysis_document import AnalysisDocument
        document = AnalysisDocument(title='합성 행사',summary='',actions=[resolve_action(source_action(),source)],questions=[],message='')
        with tempfile.TemporaryDirectory() as directory:
            cache = AnalysisCache(directory)
            key = cache.key(source, '전체 담당자', 'gpt-5-nano')
            cache.put(key, document)
            actual = cache.get(key)
        self.assertEqual(actual.actions[0].model_dump()['evidence_locations']['deadline'], [{'line':2,'cell':2}])


if __name__ == '__main__':
    unittest.main()
