import unittest
from services.source_review import review_spans, tabular_blocks
from services.analysis_document import AnalysisDocument, render_document


class SourceReviewTests(unittest.TestCase):
    def test_only_uncertain_or_invalid_dates_are_red(self):
        text = '안내 10월 2일 14시 30분 25,000원 30명 010-1234-5678 ⟦불확실:교무실⟧'
        spans = [text[a:b] for a,b in review_spans(text)]
        self.assertEqual(spans, ['⟦불확실:교무실⟧'])
        self.assertTrue(review_spans('2026. 2. 30.'))

    def test_tables_preserve_empty_cells_and_multiple_blocks(self):
        text = '제목\n대상\t기한\t담당\n3학년\t\t담임\n본문\n항목\t값\n예산\t1,000원'
        blocks = tabular_blocks(text)
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0][1], ['3학년', '', '담임'])
        self.assertEqual(tabular_blocks('표 없는 문장\n한 줄\t탭'), [])

    def test_summary_does_not_include_unrequested_templates(self):
        doc = AnalysisDocument(title='안내', summary='행사 참가 여부를 확인합니다.', actions=[], questions=['기한 확인'], message='전달문')
        result = render_document(doc, '원문 요약')
        self.assertIn('## 핵심 요약', result)
        self.assertIn(doc.summary, result)
        self.assertNotIn('전달문', result)
        self.assertNotIn('## 체크리스트', result)
