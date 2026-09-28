"""Virtual-monitor coordinates, without reading the user's screen."""
from types import SimpleNamespace
import tkinter as tk
import unittest
from unittest.mock import patch

from services.capture_service import RegionSelector


class CaptureCoordinatesTests(unittest.TestCase):
    def test_overlay_uses_absolute_negative_monitor_origin(self):
        root = tk.Tk()
        root.withdraw()
        try:
            with patch('services.capture_service._virtual_screen_bounds', return_value=(-300, -100, 600, 400)), \
                    patch.object(tk.Toplevel, 'focus_force'), patch.object(tk.Toplevel, 'grab_set'):
                selector = RegionSelector(root)
            self.assertEqual(selector.overlay.winfo_rootx(), -300)
            self.assertEqual(selector.overlay.winfo_rooty(), -100)
            selector._on_cancel(None)
        finally:
            root.destroy()

    def test_reverse_drag_across_monitor_origin_keeps_screen_coordinates(self):
        from unittest.mock import Mock
        selector = RegionSelector.__new__(RegionSelector)
        selector.start_x, selector.start_y = 120, 80
        selector.overlay = Mock()
        selector._on_release(SimpleNamespace(x_root=-250, y_root=-70))
        self.assertEqual(selector.selection, (-250, -70, 120, 80))
        selector.overlay.grab_release.assert_called_once()
        selector.overlay.destroy.assert_called_once()
