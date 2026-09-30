import unittest
from services.source_review import review_spans, tabular_blocks, source_table_blocks
from services.analysis_document import AnalysisDocument, render_document


class SourceReviewTests(unittest.TestCase):
    def test_pipe_ocr_preserves_empty_cells_and_dates(self):
        source = '설명\n행사 | 행사 일시 | 결과 보고 | 담당\n체험 | 10월 2일 | 10월 5일 |\n끝'
        block = source_table_blocks(source)[0]
        self.assertEqual(block.rows[1], ['체험', '10월 2일', '10월 5일', ''])
        self.assertEqual(block.source_lines, [2, 3])

    def test_markdown_borders_alignment_and_escaped_pipe(self):
        source = '설명\n| 항목 | 값 |\n| :--- | ---: |\n| 가\\|나 | |\n| 다음 | 2 |'
        block = source_table_blocks(source)[0]
        self.assertEqual(block.rows, [['항목', '값'], ['가|나', ''], ['다음', '2']])
        self.assertEqual([block.line_number(i) for i in range(3)], [2, 4, 5])

    def test_pipe_prose_and_inconsistent_columns_are_not_guessed(self):
        for source in ('선택 A | B', 'a|b\nc|d', 'A | B\nC | D | E'):
            self.assertEqual(source_table_blocks(source), [])

    def test_tsv_and_pipe_blocks_remain_separate(self):
        blocks = source_table_blocks('a\tb\nc\td\nA | B\nC | D')
        self.assertEqual([b.start_line for b in blocks], [1, 3])
        self.assertEqual(len(blocks), 2)

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

    def test_numbers_across_cells_and_rows_do_not_become_review_dates(self):
        for separator in ('\t', '\n', '\r\n'):
            with self.subTest(separator=separator):
                self.assertEqual(review_spans('13.' + separator + '2.'), [])
                text = '13.' + separator + '2월 30일'
                self.assertEqual([text[a:b] for a, b in review_spans(text)], ['2월 30일'])

    def test_summary_does_not_include_unrequested_templates(self):
        doc = AnalysisDocument(title='안내', summary='행사 참가 여부를 확인합니다.', actions=[], questions=['기한 확인'], message='전달문')
        result = render_document(doc, '원문 요약')
        self.assertIn('## 핵심 요약', result)
        self.assertIn(doc.summary, result)
        self.assertNotIn('전달문', result)
        self.assertNotIn('## 체크리스트', result)
