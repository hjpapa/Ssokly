"""Synthetic long-table contexts: no source documents, API or database use."""
import unittest

from services.date_evidence import source_schedule_entries, source_schedules


class TableScheduleContextTests(unittest.TestCase):
    def assert_offsets(self, source, entries):
        for entry in entries:
            self.assertEqual(source[entry['start']:entry['end']].strip(), entry['text'])
            self.assertEqual(source.count('\n', 0, entry['start']) + 1, entry['line'])

    def test_two_header_levels_keep_same_column_event_and_phase(self):
        source = ('구분\t경제 체험\t↳ 경제 체험\t과학 체험\t↳ 과학 체험\n'
                  '↳ 구분\t신청\t행사\t신청\t행사\n'
                  '1학년\t2026. 10. 1.\t2026. 10. 2.\t2026. 10. 3.\t2026. 10. 4.')
        entries = source_schedule_entries(source)
        self.assertEqual([item['context'] for item in entries], [
            '경제 체험 · 신청 · 1학년', '경제 체험 · 행사 · 1학년',
            '과학 체험 · 신청 · 1학년', '과학 체험 · 행사 · 1학년'])
        self.assert_offsets(source, entries)

    def test_three_header_levels_and_vertical_merge_markers_remain_explicit(self):
        source = ('구분\t경제 체험\t↳ 경제 체험\n'
                  '↳ 구분\t초등\t중등\n'
                  '\t신청\t행사\n'
                  '1차\t2026. 10. 1.\t2026. 10. 2.\n'
                  '↳ 1차\t2026. 10. 3.\t2026. 10. 4.')
        entries = source_schedule_entries(source)
        self.assertEqual(entries[0]['context'], '경제 체험 · 초등 · 신청 · 1차')
        self.assertEqual(entries[3]['context'], '경제 체험 · 중등 · 행사 · 1차')
        self.assert_offsets(source, entries)

    def test_reordered_repeated_header_resets_event_mapping(self):
        source = ('구분\t경제 체험\t과학 체험\n'
                  '신청\t2026. 10. 1.\t2026. 10. 2.\n'
                  '구분\t과학 체험\t경제 체험\n'
                  '행사\t2026. 10. 3.\t2026. 10. 4.')
        entries = source_schedule_entries(source)
        self.assertEqual(entries[2]['context'], '과학 체험 · 행사')
        self.assertEqual(entries[3]['context'], '경제 체험 · 행사')
        self.assert_offsets(source, entries)

    def test_reordered_named_event_header_resets_event_mapping(self):
        source = ('행사명\t경제 체험\t과학 체험\n'
                  '신청\t2026-10-01\t2026-10-02\n'
                  '행사명\t과학 체험\t경제 체험\n'
                  '행사\t2026-10-03\t2026-10-04')
        entries = source_schedule_entries(source)
        self.assertEqual(entries[2]['context'], '과학 체험 · 행사')
        self.assertEqual(entries[3]['context'], '경제 체험 · 행사')
        self.assert_offsets(source, entries)

    def test_parallel_entity_columns_do_not_reuse_first_event_label(self):
        source = ('번호\t행사명\t신청\t행사명\t신청\n'
                  '1\t경제 체험\t2026-10-01\t과학 체험\t2026-10-02')
        entries = source_schedule_entries(source)
        self.assertEqual([entry['context'] for entry in entries], [
            '신청 · 원문 2행 3열 (행사 연결 확인 필요)',
            '신청 · 원문 2행 5열 (행사 연결 확인 필요)'])
        self.assert_offsets(source, entries)

    def test_nondate_neighbor_from_other_event_is_not_a_row_label(self):
        source = '구분\t경제 체험\t과학 체험\n신청\t미실시\t2026. 10. 4.'
        entry = source_schedule_entries(source)[0]
        self.assertEqual(entry['context'], '과학 체험 · 신청')
        self.assertNotIn('미실시', entry['context'])

    def test_wide_leading_stub_columns_keep_event_and_action_names(self):
        source = '번호\t학교급\t행사명\t업무\t기한\n1\t초등\t경제 체험\t신청\t2026. 10. 4.'
        entry = source_schedule_entries(source)[0]
        self.assertEqual(entry['context'], '기한 · 1 · 초등 · 경제 체험 · 신청')
        self.assertEqual(entry['cell'], 5)

    def test_blank_parent_heading_is_not_filled_from_neighbor(self):
        source = ('구분\t경제 체험\t\n↳ 구분\t신청\t행사\n'
                  '초등\t2026. 10. 1.\t2026. 10. 2.')
        entry = source_schedule_entries(source)[1]
        self.assertNotIn('경제 체험', entry['context'])
        self.assertIn('열 제목 확인 필요', entry['context'])
        self.assertIn('원문 3행 3열', entry['context'])

    def test_bare_merge_arrow_is_unknown_not_a_copied_title(self):
        source = '구분\t경제 체험\t↳\n신청\t2026. 10. 1.\t2026. 10. 2.'
        entry = source_schedule_entries(source)[1]
        self.assertNotIn('경제 체험', entry['context'])
        self.assertIn('열 제목 확인 필요', entry['context'])

    def test_width_mismatch_does_not_reuse_shifted_column_titles(self):
        source = '구분\t경제 체험\t과학 체험\n신청\t추가 열\t2026. 10. 1.\t2026. 10. 2.'
        entries = source_schedule_entries(source)
        self.assertTrue(all('열 제목 확인 필요' in entry['context'] for entry in entries))
        self.assertTrue(all('경제 체험' not in entry['context'] and '과학 체험' not in entry['context'] for entry in entries))

    def test_nondate_body_rows_are_not_added_as_header_layers(self):
        source = ('구분\t경제 체험\t과학 체험\n'
                  '접수\t미정\t별도 안내\n'
                  '발표\t2026. 10. 1.\t2026. 10. 2.')
        entries = source_schedule_entries(source)
        self.assertEqual(entries[0]['context'], '경제 체험 · 발표')
        self.assertNotIn('미정', entries[0]['context'])

    def test_key_value_body_labels_do_not_replace_header(self):
        source = '항목\t내용\n행사명\t경제 체험\n대상\t초등학생\n신청\t2026. 10. 1.'
        self.assertEqual(source_schedule_entries(source)[0]['context'], '내용 · 신청')

    def test_audience_body_row_is_not_a_repeated_event_header(self):
        source = ('구분\t경제 체험\t과학 체험\n장소\t체험관\t과학관\n'
                  '대상\t초등학생\t중학생\n신청\t2026. 10. 15.\t2026. 10. 16.')
        entries = source_schedule_entries(source)
        self.assertEqual([entry['context'] for entry in entries], ['경제 체험 · 신청', '과학 체험 · 신청'])

    def test_unknown_heading_uses_source_position_without_claiming_event(self):
        source = '참고 내용\t경제 체험\t과학 체험\n신청\t2026. 10. 1.\t2026. 10. 2.'
        entries = source_schedule_entries(source)
        self.assertIn('원문 2행 2열', entries[0]['context'])
        self.assertIn('열 제목 확인 필요', entries[0]['context'])
        self.assertNotIn('경제 체험', entries[0]['context'])

    def test_merged_title_and_repeated_long_table_do_not_change_offsets(self):
        heading = '운영 일정\t↳ 운영 일정\t↳ 운영 일정\r\n구분\t경제 체험\t과학 체험'
        rows = [heading]
        for index in range(100):
            if index == 50:
                rows.append(heading)
            rows.append(f'{index + 1}차\t2026. 10. 1.\t2026. 10. 2.')
        source = '\r\n'.join(rows)
        entries = source_schedule_entries(source)
        self.assertEqual(len(entries), 200)
        self.assertEqual(entries[0]['context'], '운영 일정 · 경제 체험 · 1차')
        self.assertEqual(entries[-1]['context'], '운영 일정 · 과학 체험 · 100차')
        self.assertEqual(len(source_schedules(source)), 200)
        self.assert_offsets(source, entries)


if __name__ == '__main__':
    unittest.main()
