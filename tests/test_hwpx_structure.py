"""Synthetic HWPX extraction contracts, without real documents or API calls."""
from pathlib import Path
import tempfile
import unittest
from xml.sax.saxutils import escape, quoteattr
import zipfile

from services.document_service import read_hwpx_file
from services.source_review import tabular_blocks


def paragraph(text):
    return '<p><run><t>' + escape(text) + '</t></run></p>'


def cell(row, col, text='', *, rows=1, cols=1, body=None):
    return (f'<tc><cellAddr rowAddr={quoteattr(str(row))} colAddr={quoteattr(str(col))}/>'
            f'<cellSpan rowSpan={quoteattr(str(rows))} colSpan={quoteattr(str(cols))}/>'
            '<subList>' + (paragraph(text) if body is None else body) + '</subList></tc>')


def table(rows, cols, contents):
    return f'<tbl rowCnt={quoteattr(str(rows))} colCnt={quoteattr(str(cols))}>' + contents + '</tbl>'


class HwpxStructureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'synthetic.hwpx'

    def read(self, body='', *, sections=None):
        sections = sections if sections is not None else {'Contents/section0.xml': '<sec>' + body + '</sec>'}
        with zipfile.ZipFile(self.path, 'w') as archive:
            for name, xml in sections.items():
                archive.writestr(name, xml)
        return read_hwpx_file(self.path)

    def test_long_table_preserves_empty_edge_and_middle_cells(self):
        values = [['', '업무', '기한', ''], ['', '첫 안내', '', '2026. 10. 1.']]
        values += [[str(i), f'합성 업무 {i}', '', f'2026. 11. {i % 28 + 1}.'] for i in range(120)]
        body = ''.join('<tr>' + ''.join(cell(r, c, value) for c, value in enumerate(row)) + '</tr>'
                       for r, row in enumerate(values))
        text = self.read(table(len(values), 4, body))
        self.assertEqual(tabular_blocks(text), [values])
        self.assertEqual(text.count('합성 업무 119'), 1)

    def test_row_and_column_merges_preserve_continuations_and_empty_merged_cells(self):
        body = ('<tr>' + cell(0, 0, '공통', rows=2, cols=2) + cell(0, 2, '') + '</tr>'
                '<tr>' + cell(1, 2, '첫 기한') + '</tr>'
                '<tr>' + cell(2, 0, '', cols=2) + cell(2, 2, '둘째 기한') + '</tr>')
        text = self.read(table(3, 3, body))
        self.assertEqual(tabular_blocks(text), [[['공통', '↳ 공통', ''],
                                                ['↳ 공통', '↳ 공통', '첫 기한'],
                                                ['', '', '둘째 기한']]])

    def test_single_column_layout_stays_paragraphs_with_merge_marker(self):
        body = '<tr>' + cell(0, 0, '배치 문단', rows=2) + '</tr><tr></tr><tr>' + cell(2, 0, '') + '</tr>'
        text = self.read(paragraph('앞') + table(3, 1, body) + paragraph('뒤'))
        self.assertEqual(text, '앞\n배치 문단\n↳ 배치 문단\n뒤')
        self.assertEqual(tabular_blocks(text), [])

    def test_text_node_controls_and_tails_preserve_boundaries(self):
        body = '<p><run><t>신청<lineBreak/>기한<tab/>오후<span>두 시</span>까지</t></run></p>'
        self.assertEqual(self.read(body), '신청\n기한\t오후두 시까지')

    def test_cell_controls_and_paragraphs_cannot_shift_table_columns(self):
        body = ('<p><run><t>신청<lineBreak/>기한<tab/>오후</t>'
                '<lineBreak/><t>두 시</t><tab/><t>까지</t></run></p>' + paragraph('별도 문단'))
        text = self.read(table(1, 2, '<tr>' + cell(0, 0, body=body) + cell(0, 1, '옆 셀') + '</tr>'))
        self.assertEqual(text.splitlines()[1].split('\t'), ['신청 / 기한 | 오후 / 두 시 | 까지 / 별도 문단', '옆 셀'])

    def test_nested_table_flattens_inside_parent_cell_without_fake_table_markers(self):
        inner = table(2, 2, '<tr>' + cell(0, 0, '내부 제목') + cell(0, 1, '내부 기한') + '</tr>'
                      '<tr>' + cell(1, 0, '') + cell(1, 1, '10월 3일') + '</tr>')
        body = '<p><run><t>앞 문장</t>' + inner + '<t>뒤 문장</t></run></p>'
        text = self.read(table(1, 2, '<tr>' + cell(0, 0, body=body) + cell(0, 1, '바깥 기한') + '</tr>'))
        self.assertEqual(text.count('[표 ·'), 1)
        self.assertEqual(text.count('[/표]'), 1)
        self.assertEqual(text.splitlines()[1].split('\t'), ['앞 문장 / 내부 제목 | 내부 기한 /  | 10월 3일 / 뒤 문장', '바깥 기한'])

    def test_namespace_and_numeric_section_order_are_preserved(self):
        sections = {'Contents/section10.xml': '<sec xmlns="urn:synthetic:hwpx">' + paragraph('셋째') + '</sec>',
                    'Contents/section2.xml': '<sec>' + paragraph('둘째') + '</sec>',
                    'Contents/section0.xml': '<sec>' + paragraph('첫째') + '</sec>',
                    'Contents/not-a-section.xml': '<ignore/>'}
        self.assertEqual(self.read(sections=sections), '첫째\n둘째\n셋째')

    def test_missing_span_stays_one_cell_and_addresses_define_column_order(self):
        right = cell(0, 1, '오른쪽').replace('<cellSpan rowSpan="1" colSpan="1"/>', '')
        left = cell(0, 0, '왼쪽').replace('<cellSpan rowSpan="1" colSpan="1"/>', '')
        text = self.read(table(1, 2, '<tr>' + right + left + '</tr>'))
        self.assertEqual(text.splitlines()[1], '왼쪽\t오른쪽')

    def test_repeated_cell_metadata_cannot_silently_discard_text_or_coordinates(self):
        original = cell(0, 0, '원래 셀')
        repeated = ('<cellAddr rowAddr="0" colAddr="1"/>',
                    '<cellSpan rowSpan="1" colSpan="2"/>',
                    '<subList>' + paragraph('다른 셀') + '</subList>')
        for metadata in repeated:
            with self.subTest(metadata=metadata), self.assertRaisesRegex(ValueError, 'HWPX.*셀'):
                self.read(table(1, 2, '<tr>' + original.replace('</tc>', metadata + '</tc>') + '</tr>'))

    def test_missing_table_dimensions_are_not_guessed(self):
        for attrs in ('rowCnt="1"', 'colCnt="2"', ''):
            with self.subTest(attrs=attrs), self.assertRaisesRegex(ValueError, 'HWPX.*표'):
                self.read('<tbl ' + attrs + '><tr>' + cell(0, 0, '추측 금지') + '</tr></tbl>')

    def test_malformed_section_names_fail_with_safe_message(self):
        for name in ('Contents/sectionSYNTHETIC_PRIVATE.xml', 'Contents/section1-extra.xml', 'Contents/section1/nested.xml'):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, 'HWPX.*섹션') as error:
                    self.read(sections={name: '<sec>' + paragraph('비공개 합성 본문') + '</sec>'})
                self.assertNotIn('SYNTHETIC_PRIVATE', str(error.exception))
                self.assertNotIn('비공개 합성 본문', str(error.exception))

    def test_duplicate_numeric_section_identifiers_do_not_duplicate_body(self):
        with self.assertRaisesRegex(ValueError, 'HWPX.*섹션'):
            self.read(sections={'Contents/section0.xml': '<sec/>', 'Contents/section00.xml': '<sec/>'})

    def test_invalid_address_or_span_never_wraps_or_silently_truncates(self):
        cases = [cell(-1, 0, '비공개 합성 본문'), cell(0, -1), cell(2, 0), cell(0, 2),
                 cell(0, 0, rows=0), cell(0, 0, cols=-1), cell(0, 0, rows=3), cell(0, 0, cols=3),
                 cell('SYNTHETIC_PRIVATE', 0), cell(0, '1.5'),
                 cell(0, 0).replace('rowAddr="0"', ''),
                 cell(0, 0).replace('<cellAddr rowAddr="0" colAddr="0"/>', ''),
                 cell(0, 0).replace('rowSpan="1"', '')]
        for body in cases:
            with self.subTest(body=body):
                with self.assertRaisesRegex(ValueError, 'HWPX.*셀') as error:
                    self.read(table(2, 2, '<tr>' + body + '</tr>'))
                self.assertNotIn('SYNTHETIC_PRIVATE', str(error.exception))
                self.assertNotIn('비공개 합성 본문', str(error.exception))

    def test_overlapping_cells_including_empty_merges_do_not_overwrite_data(self):
        for first in (cell(0, 0, '보존할 값', cols=2), cell(0, 0, '', cols=2)):
            with self.subTest(first=first), self.assertRaisesRegex(ValueError, 'HWPX.*셀'):
                self.read(table(1, 2, '<tr>' + first + cell(0, 1, '겹친 값') + '</tr>'))

    def test_invalid_table_dimensions_fail_with_safe_message(self):
        for rows, cols in ((0, 2), (-1, 2), (100001, 1), (2, 'SYNTHETIC_PRIVATE'), ('1.5', 2)):
            with self.subTest(rows=rows, cols=cols):
                with self.assertRaisesRegex(ValueError, 'HWPX.*표') as error:
                    self.read(table(rows, cols, '<tr/>'))
                self.assertNotIn('SYNTHETIC_PRIVATE', str(error.exception))


if __name__ == '__main__':
    unittest.main()
