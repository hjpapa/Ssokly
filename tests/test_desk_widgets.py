"""Capture desk widgets: synthetic images/text/tempfiles, no real clipboard/API."""
import os
from pathlib import Path
import tempfile
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from ui.desk_widgets import InlineTableView, ThumbnailCache, ZoomImageView


class DeskWidgetTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.geometry('420x360')
        self.addCleanup(self.root.destroy)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def layout(self, widget, width=400, height=300):
        # Explicit placement computes geometry while the root stays withdrawn.
        widget.place(x=0, y=0, width=width, height=height)
        self.root.update()

    def image_file(self, name='synthetic.png', size=(600, 400), color='red'):
        path = self.directory / name
        with Image.new('RGB', size, color) as image:
            image.save(path)
        return path

    def test_image_copy_fit_zoom_original_and_reset(self):
        view = ZoomImageView(self.root)
        self.layout(view)
        original = Image.new('RGB', (800, 600), 'red')
        self.addCleanup(original.close)
        before = original.tobytes()
        view.set_image(original)
        self.assertTrue(view.fit_mode)
        self.assertLess(view.scale, 1)
        fit_scale = view.scale
        view.zoom_in()
        self.assertFalse(view.fit_mode)
        self.assertGreater(view.scale, fit_scale)
        view.zoom_out()
        self.assertAlmostEqual(view.scale, fit_scale)
        view.original_size()
        self.assertEqual(view.scale, 1)
        view._image.putpixel((0, 0), (0, 0, 255))
        self.assertEqual(original.tobytes(), before)
        view.set_image(None)
        self.assertIsNone(view.image_size)
        self.assertIsNone(view._photo)
        self.assertEqual(view.canvas.itemcget(view._image_item, 'image'), '')
        view.fit()
        view.zoom_in()

    def test_resize_only_refits_in_fit_mode(self):
        view = ZoomImageView(self.root)
        self.layout(view, 220, 220)
        image = Image.new('RGB', (1000, 600), 'white')
        self.addCleanup(image.close)
        view.set_image(image)
        initial = view.scale
        self.layout(view, 400, 300)
        # Withdrawn windows do not deliver mapped-child Configure events; drive
        # their bound handler without exposing a real desktop window.
        self.assertTrue(view.canvas.bind('<Configure>'))
        view._on_resize(None)
        self.root.update()
        self.assertGreater(view.scale, initial)
        view.original_size()
        self.layout(view, 240, 220)
        view._on_resize(None)
        self.root.update()
        self.assertEqual(view.scale, 1)
        self.assertFalse(view.fit_mode)

    def test_large_zoom_renders_only_viewport_and_both_axes_pan(self):
        view = ZoomImageView(self.root)
        self.layout(view, 300, 220)
        image = Image.new('RGB', (1600, 1200), 'blue')
        self.addCleanup(image.close)
        view.set_image(image)
        view._set_scale(4)
        self.assertLessEqual(view._photo.width(), view.canvas.winfo_width() + 8)
        self.assertLessEqual(view._photo.height(), view.canvas.winfo_height() + 8)
        self.assertLess(view.canvas.xview()[1] - view.canvas.xview()[0], 1)
        self.assertLess(view.canvas.yview()[1] - view.canvas.yview()[0], 1)
        view._xview('moveto', 0.2)
        view._yview('moveto', 0.2)
        before = view.canvas.xview(), view.canvas.yview()
        view._pan_start(SimpleNamespace(x=100, y=100))
        view._pan_move(SimpleNamespace(x=50, y=50))
        self.assertGreater(view.canvas.xview()[0], before[0][0])
        self.assertGreater(view.canvas.yview()[0], before[1][0])

    def test_load_path_closes_file_and_failure_clears_previous_image(self):
        view = ZoomImageView(self.root)
        self.layout(view)
        path = self.image_file()
        self.assertTrue(view.load_path(path))
        self.assertEqual(view.image_size, (600, 400))
        renamed = path.with_name('renamed.png')
        path.rename(renamed)  # Windows would fail if Pillow retained its file handle.
        self.assertFalse(view.load_path(path))
        self.assertIsNone(view.image_size)
        self.assertIn('열 수 없습니다', view.status.get())
        self.assertTrue(view.load_path(None))

    def test_destroy_cancels_pending_resize_and_releases_image(self):
        view = ZoomImageView(self.root)
        image = Image.new('RGB', (20, 20))
        self.addCleanup(image.close)
        view.set_image(image)
        view._on_resize(None)
        pending = view._pending_render
        self.assertIsNotNone(pending)
        view.destroy()
        self.assertIsNone(view._image)
        self.assertIsNone(view._photo)
        self.assertNotIn(pending, self.root.tk.call('after', 'info'))
        self.root.update_idletasks()

    def test_thumbnail_reuses_same_key_and_invalidates_changed_file(self):
        cache = ThumbnailCache(self.root)
        path = self.image_file()
        first = cache.get(path, (80, 60))
        self.assertIs(first, cache.get(str(path), (80, 60)))
        self.assertEqual((first.width(), first.height()), (80, 53))
        self.assertIsNot(first, cache.get(path, (40, 40)))
        previous = path.stat().st_mtime_ns
        self.image_file(color='blue')
        os.utime(path, ns=(previous + 1_000_000_000, previous + 1_000_000_000))
        self.assertIsNot(first, cache.get(path, (80, 60)))
        self.assertEqual(len(cache), 1)

    def test_thumbnail_lru_bound_missing_file_and_clear(self):
        cache = ThumbnailCache(self.root, max_items=2)
        paths = [self.image_file(f'synthetic-{i}.png', (20, 10)) for i in range(3)]
        first = cache.get(paths[0])
        second = cache.get(paths[1])
        self.assertIs(first, cache.get(paths[0]))
        cache.get(paths[2])
        self.assertEqual(len(cache), 2)
        self.assertIsNot(second, cache.get(paths[1]))
        paths[1].unlink()
        self.assertIsNone(cache.get(paths[1]))
        self.assertIsNone(cache.get(None))
        self.assertEqual(len(cache), 1)
        self.assertEqual(ThumbnailCache(self.root, max_items=999).max_items, 128)
        with self.assertRaises(ValueError):
            cache.get(paths[0], (0, 20))
        cache.clear()
        self.assertEqual(len(cache), 0)

    def test_inline_table_source_lines_empty_merged_and_read_only(self):
        copied = Mock()
        view = InlineTableView(self.root, on_copy=copied)
        self.layout(view)
        source = ('안내\r\n구분\t신청\t행사\t비고\r\n'
                  '↳ 합성 행사\t10월 2일\t2월 30일\t\r\n'
                  '다른 표\r\n항목\t날짜\r\n개최\t10월 8일')
        view.set_text(source)
        self.assertEqual([block.start_line for block in view.blocks], [2, 5])
        self.assertEqual(view.tree.selection(), ('0',))
        self.assertEqual(view.tree.item('1', 'text'), '3행 · 확인')
        view.tree.selection_set('1')
        view.show_row()
        self.assertEqual(str(view.detail['state']), 'disabled')
        self.assertIn('(빈 셀)', view.detail.get('1.0', 'end-1c'))
        self.assertIn('↳ 합성 행사', view.detail.get('1.0', 'end-1c'))
        for tag, expected in (('source_date', ['10월 2일']), ('source_review', ['2월 30일'])):
            spans = view.detail.tag_ranges(tag)
            self.assertEqual([view.detail.get(spans[i], spans[i + 1])
                              for i in range(0, len(spans), 2)], expected)
        with patch.object(view, 'clipboard_clear') as clear, patch.object(view, 'clipboard_append') as append:
            view.copy_row()
            copied.assert_called_with('↳ 합성 행사\t10월 2일\t2월 30일\t')
            view.select_table(1)
            view.copy_table()
            copied.assert_called_with('항목\t날짜\n개최\t10월 8일')
            clear.assert_not_called()
            append.assert_not_called()
        self.assertEqual(view.source_text, source)

    def test_inline_ragged_long_row_copies_exact_values_at_narrow_width(self):
        copied = Mock()
        view = InlineTableView(self.root, on_copy=copied)
        self.layout(view, 240, 300)
        long_cell = '합성 긴 안내 ' * 200
        view.set_text('구분\t설명\t빈 셀\n행사\t' + long_cell)
        self.root.update_idletasks()
        self.assertLessEqual(view.winfo_reqwidth(), 240)
        self.assertLessEqual(view.tree.winfo_width(), 240)
        self.assertGreater(view.tree.xview()[1] - view.tree.xview()[0], 0)
        self.assertLess(view.tree.xview()[1] - view.tree.xview()[0], 1)
        view.tree.selection_set('1')
        view.show_row()
        self.assertIn(long_cell, view.detail.get('1.0', 'end-1c'))
        view.copy_row()
        copied.assert_called_once_with('행사\t' + long_cell)
        self.assertEqual(view.blocks[0].rows[1], ['행사', long_cell])

    def test_inline_empty_reset_single_marked_table_and_no_copy_callback(self):
        view = InlineTableView(self.root)
        self.layout(view)
        self.assertEqual(view.selected_table, -1)
        self.assertTrue(view.copy_table_button.instate(['disabled']))
        view.set_text('[표 · ↳는 병합 셀에서 이어지는 값]\n행사\t10월 8일\t\n[/표]')
        self.assertEqual(len(view.blocks), 1)
        self.assertEqual(view.blocks[0].rows, [['행사', '10월 8일', '']])
        self.assertTrue(view.copy_table_button.instate(['disabled']))
        view.copy_row()
        view.copy_table()
        view.set_text('일반 본문입니다')
        self.assertEqual(view.tree.get_children(), ())
        self.assertEqual(view.selected_table, -1)
        self.assertNotIn('10월 8일', view.detail.get('1.0', 'end-1c'))


if __name__ == '__main__':
    unittest.main()
