"""Synthetic table review tests; no app data, credentials or network."""
import tkinter as tk
import unittest
from tests.tk_support import destroy_root
from unittest.mock import Mock, patch

from services.source_review import source_table_blocks, tabular_blocks


class SourceTableDetectionTests(unittest.TestCase):
    def test_source_line_numbers_empty_cells_and_multiple_blocks(self):
        source = '설명\r\n항목\t기한\t\r\n접수\t\t비고\r\n구분\r\n행사\t날짜\r\n개최\t10월 8일'
        blocks = source_table_blocks(source)
        self.assertEqual([block.start_line for block in blocks], [2, 5])
        self.assertEqual(blocks[0].rows, [['항목', '기한', ''], ['접수', '', '비고']])
        self.assertEqual(tabular_blocks(source), [block.rows for block in blocks])

    def test_explicit_hwpx_marker_allows_single_row_without_guessing(self):
        source = '[표 · ↳는 병합 셀에서 이어지는 값]\n신청\t10월 2일\t\n[/표]'
        self.assertEqual(source_table_blocks(source)[0].rows, [['신청', '10월 2일', '']])
        self.assertEqual(source_table_blocks('설명\n신청\t10월 2일'), [])
        self.assertEqual(source_table_blocks('[표 · ↳는 병합 셀에서 이어지는 값]\n설명\n신청\t10월 2일'), [])


class SourceTablesWindowTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(destroy_root, self.root)
        self.source = ('설명\n항목\t신청\t행사\t비고\n'
                       '합성 행사\t10월 2일\t2월 30일\t\n'
                       '다른 표\n구분\t일자\n↳ 이어진 값\t10월 8일')
        self.blocks = source_table_blocks(self.source)
        self.on_copy = Mock()
        self.root.update_idletasks()


if __name__ == '__main__':
    unittest.main()
