"""Synthetic table review tests; no app data, credentials or network."""
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from services.source_review import highlight_source, source_table_blocks, tabular_blocks
from ui.source_tables import SourceTablesWindow


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
        self.addCleanup(self.root.destroy)
        self.source = ('설명\n항목\t신청\t행사\t비고\n'
                       '합성 행사\t10월 2일\t2월 30일\t\n'
                       '다른 표\n구분\t일자\n↳ 이어진 값\t10월 8일')
        self.blocks = source_table_blocks(self.source)
        self.on_copy = Mock()
        self.window = SourceTablesWindow(self.root, self.blocks, self.on_copy)
        self.root.update_idletasks()

    def test_initial_selection_line_number_and_read_only_detail(self):
        self.assertEqual(self.window.trees[0].selection(), ('0',))
        self.assertIn('원문 2행', self.window.details[0].get('1.0', 'end-1c'))
        self.assertEqual(str(self.window.details[0]['state']), 'disabled')
        self.assertEqual(self.window.trees[0].item('1', 'text'), '3행 · 확인')
        self.assertEqual(self.window.trees[0].item('1', 'tags'), '')

    def test_only_uncertain_span_is_red_and_valid_neighbor_stays_valid(self):
        self.window.trees[0].selection_set('1')
        self.window.show_row(0)
        detail = self.window.details[0]
        ranges = detail.tag_ranges('source_review')
        self.assertEqual([detail.get(ranges[i], ranges[i + 1]) for i in range(0, len(ranges), 2)], ['2월 30일'])
        self.assertIn('(빈 셀)', detail.get('1.0', 'end-1c'))
        valid_ranges = detail.tag_ranges('source_date')
        self.assertEqual([detail.get(valid_ranges[i], valid_ranges[i + 1]) for i in range(0, len(valid_ranges), 2)], ['10월 2일'])

    def test_copy_preserves_selected_tab_empty_cells_and_merged_markers(self):
        with patch.object(self.window, 'clipboard_clear'), patch.object(self.window, 'clipboard_append') as copied:
            self.window.trees[0].selection_set('1')
            self.window.copy_row()
            copied.assert_called_with('합성 행사\t10월 2일\t2월 30일\t')
            self.window.notebook.select(1)
            self.window.copy_table()
            copied.assert_called_with('구분\t일자\n↳ 이어진 값\t10월 8일')
        self.assertEqual(tabular_blocks(self.source), [block.rows for block in self.blocks])

    def test_long_cell_and_ragged_row_are_not_truncated_in_detail_or_copy(self):
        long_cell = '긴 안내 내용 ' * 180
        source = '구분\t비고\t추가\n신청\t' + long_cell
        window = SourceTablesWindow(self.root, source_table_blocks(source), self.on_copy)
        window.trees[0].selection_set('1')
        window.show_row(0)
        self.assertIn(long_cell, window.details[0].get('1.0', 'end-1c'))
        self.assertLessEqual(window.trees[0].column('1', 'width'), 620)
        with patch.object(window, 'clipboard_clear'), patch.object(window, 'clipboard_append') as copied:
            window.copy_row()
            copied.assert_called_once_with('신청\t' + long_cell)

    def test_source_highlighting_never_combines_numbers_across_table_cells(self):
        source = '항목\t값\nA\t10.\n2.\tB\n행사\t13.\t2.\n실제 날짜\t10월 8일\t2월 30일'
        editor = tk.Text(self.root)
        editor.insert('1.0', source)
        highlight_source(editor)
        for tag, expected in (('source_date', ['10월 8일']), ('source_review', ['2월 30일'])):
            ranges = editor.tag_ranges(tag)
            self.assertEqual([editor.get(ranges[i], ranges[i + 1]) for i in range(0, len(ranges), 2)], expected)
        self.assertEqual(editor.get('1.0', 'end-1c'), source)
        window = SourceTablesWindow(self.root, source_table_blocks(source), self.on_copy)
        self.assertEqual(window.trees[0].item('3', 'text'), '4행')


if __name__ == '__main__':
    unittest.main()
