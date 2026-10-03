"""Help navigation/lifecycle and readable small/high-DPI window regressions."""
import tkinter as tk
from tkinter import font as tkfont
import unittest

from tests.tk_support import destroy_root
from ui.desk_theme import apply_theme
from ui.user_manual import MANUAL_SECTIONS, show_user_manual


class UserManualTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.original_scaling = self.root.tk.call('tk', 'scaling')
        self.root.geometry('720x680')
        apply_theme(self.root)
        self.root.update()

    def tearDown(self):
        try:
            self.root.tk.call('tk', 'scaling', self.original_scaling)
        finally:
            destroy_root(self.root)

    def test_reopen_reuses_window_and_close_allows_a_fresh_one(self):
        first = show_user_manual(self.root)
        self.root.update()
        first.show_section(8)
        first.window.withdraw()
        self.assertIs(show_user_manual(self.root), first)
        self.root.update()
        self.assertTrue(first.window.winfo_viewable())
        self.assertEqual(first.section_picker.current(), 8)
        self.assertIsNone(first.window.grab_current())
        first.close()
        self.assertIsNone(self.root._user_manual_window)
        second = show_user_manual(self.root)
        self.assertIsNot(second, first)
        second.window.destroy()  # Native/root destruction also clears the owner.
        self.assertIsNone(self.root._user_manual_window)

    def test_navigation_scrolls_to_real_sections_and_keeps_help_readonly(self):
        manual = show_user_manual(self.root)
        self.root.update()
        original = manual.text.get('1.0', 'end-1c')
        manual.text.insert('1.0', 'must not change help')
        self.assertEqual(manual.text.get('1.0', 'end-1c'), original)
        for index in (0, 4, 8, len(MANUAL_SECTIONS) - 1):
            with self.subTest(section=index):
                manual.show_section(index)
                self.root.update()
                title = MANUAL_SECTIONS[index][0]
                self.assertIn(title, manual.text.get(f'section_{index}', f'section_{index} lineend'))
                self.assertIsNotNone(manual.text.bbox(f'section_{index}'))
                self.assertEqual(manual.section_picker.current(), index)
        manual.move_section(1)
        self.assertEqual(manual.section_picker.current(), len(MANUAL_SECTIONS) - 1)
        manual.show_section(0)
        manual.scroll(1)
        self.assertGreater(manual.text.yview()[0], 0)
        manual.scroll_to(1)
        self.assertAlmostEqual(manual.text.yview()[1], 1)
        manual.scroll_to(0)
        self.assertAlmostEqual(manual.text.yview()[0], 0)

    def test_keyboard_navigation_and_escape(self):
        manual = show_user_manual(self.root)
        self.root.update()
        manual.window.event_generate('<Alt-Right>')
        self.root.update()
        self.assertEqual(manual.section_picker.current(), 1)
        manual.window.event_generate('<Alt-Left>')
        self.root.update()
        self.assertEqual(manual.section_picker.current(), 0)
        manual.window.event_generate('<Escape>')
        self.root.update()
        self.assertFalse(manual.window.winfo_exists())

    def test_controls_and_scrolling_fit_small_window_at_supported_scales(self):
        for scaling in (100 / 75, 2.0, 4.0):
            self.root.tk.call('tk', 'scaling', scaling)
            apply_theme(self.root)
            manual = show_user_manual(self.root)
            self.root.update()
            with self.subTest(scaling=scaling, size='initial'):
                self.assertLessEqual(manual.window.winfo_width(), self.root.winfo_screenwidth())
                self.assertLessEqual(manual.window.winfo_height(), self.root.winfo_screenheight() - 50)
            width, height = manual.window.minsize()
            manual.window.geometry(f'{width}x{height}')
            self.root.update()
            with self.subTest(scaling=scaling, size='minimum'):
                for widget in (manual.section_picker, manual.text, manual.scrollbar, manual.close_button):
                    self.assertTrue(widget.winfo_viewable())
                    self.assertGreater(widget.winfo_height(), 10)
                    self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(),
                                         manual.window.winfo_rooty() + manual.window.winfo_height())
                    self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(),
                                         manual.window.winfo_rootx() + manual.window.winfo_width())
                self.assertGreaterEqual(manual.close_button.winfo_width(), manual.close_button.winfo_reqwidth())
                self.assertGreaterEqual(manual.text.winfo_height(), tkfont.Font(root=self.root, font=manual.text.cget('font')).metrics('linespace') * 3)
                manual.show_section(len(MANUAL_SECTIONS) - 1)
                self.root.update()
                self.assertIsNotNone(manual.text.bbox(f'section_{len(MANUAL_SECTIONS) - 1}'))
            manual.close()


if __name__ == '__main__':
    unittest.main()
