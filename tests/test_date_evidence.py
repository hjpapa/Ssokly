import tempfile
import unittest
import zipfile
from pathlib import Path

from services.date_evidence import date_mentions, supported_deadline, source_schedules
from services.document_service import read_hwpx_file
from services.source_review import tabular_blocks
from services.analysis_document import Action, AnalysisDocument, verify_evidence, render_document


class DateEvidenceTests(unittest.TestCase):
    def test_equivalent_format_preserves_source_time_and_qualifier(self):
        source = '참가접수 2026. 8. 28.(금) 16:00까지'
        self.assertEqual(supported_deadline('2026-08-28', source), '2026. 8. 28.(금) 16:00까지')
        self.assertIsNone(supported_deadline('2026-08-28 18:00', source))
        self.assertIsNone(supported_deadline('2026-09-28', source))

    def test_no_year_guess_or_ambiguous_column(self):
        self.assertIsNone(supported_deadline('2026-08-28', '8. 28.(금)'))
        self.assertIsNone(supported_deadline('2026-08-28', '2026. 8. 28.\t2026. 9. 30.'))
        self.assertEqual(supported_deadline('8월 28일', '8. 28.(금) 16:00까지'), '8. 28.(금) 16:00까지')

    def test_invalid_date_and_times(self):
        for value in ('2026. 2. 29.', '2026. 13. 1.', '8. 28. 25:00'):
            self.assertFalse(date_mentions(value)[0].valid)
        self.assertTrue(date_mentions('2028. 2. 29.')[0].valid)
        self.assertEqual(source_schedules('기한 ⟦불확실:2026. 8. 28.⟧'), [])

    def test_time_range_inside_cell_not_neighbor(self):
        self.assertEqual(date_mentions('2026. 9. 18.(금) / 08:40~17:00')[0].times, ((8, 40), (17, 0)))
        self.assertEqual(date_mentions('2026. 9. 18.(금)\t08:40')[0].times, ())

    def test_schedule_keeps_event_columns_and_blank_cells(self):
        source = '구분\t시 대회\t도 대회\n접수\t2026. 8. 28.(금) 16:00까지\t2026. 9. 30.(수) 18:00까지\n발표\t\t2026. 10. 12.(월) 예정'
        schedules = source_schedules(source)
        self.assertEqual(len(schedules), 3)
        self.assertEqual(schedules[0], ('시 대회 · 접수', '2026. 8. 28.(금) 16:00까지'))
        self.assertEqual(schedules[2], ('도 대회 · 발표', '2026. 10. 12.(월) 예정'))
        doc = AnalysisDocument(title='행사', summary='', actions=[], questions=[], message='')
        self.assertIn('시 대회 · 접수', render_document(doc, '업무 일정·체크리스트', source))

    def test_verification_does_not_erase_supported_date(self):
        source = '담임 참가접수 2026. 8. 28.(금) 16:00까지'
        action = Action(action='참가접수', owner='담임', deadline='2026-08-28 16:00', deliverable='', destination='', evidence=source, kind='명시된 의무')
        doc = AnalysisDocument(title='행사', summary='', actions=[action], questions=[], message='')
        result = verify_evidence(doc, source)
        self.assertEqual(result.actions[0].deadline, '2026. 8. 28.(금) 16:00까지')
        self.assertEqual(result.actions[0].kind, '명시된 의무')

    def test_hwpx_tables_no_duplicate_text_and_merged_cells(self):
        def cell(r, c, text, rs=1):
            return f'<tc><cellAddr rowAddr="{r}" colAddr="{c}"/><cellSpan rowSpan="{rs}" colSpan="1"/><subList><p><run><t>{text}</t></run></p></subList></tc>'
        xml = '<sec><p><run><t>본문</t><tbl rowCnt="3" colCnt="2"><tr>' + cell(0,0,'업무') + cell(0,1,'기한') + '</tr><tr>' + cell(1,0,'접수',2) + cell(1,1,'2026. 8. 28.') + '</tr><tr>' + cell(2,1,'2026. 9. 30.') + '</tr></tbl></run></p></sec>'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sample.hwpx'
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('Contents/section0.xml', xml)
                archive.writestr('Contents/section10.xml', '<sec><p><t>마지막</t></p></sec>')
                archive.writestr('Contents/section2.xml', '<sec><p><t>중간</t></p></sec>')
            text = read_hwpx_file(path)
        self.assertEqual(text.count('2026. 8. 28.'), 1)
        self.assertEqual(tabular_blocks(text)[0][2], ['↳ 접수', '2026. 9. 30.'])
        self.assertLess(text.index('중간'), text.index('마지막'))
