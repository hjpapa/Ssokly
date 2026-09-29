"""Fresh-process regression for stale Tcl theme events and application timers."""
from pathlib import Path
import subprocess
import sys
import unittest


class TkLifecycleTests(unittest.TestCase):
    def test_repeated_create_destroy_has_no_tcl_background_errors(self):
        script = '''
import tempfile
import tkinter as tk
from tkinter import ttk
from ui.capture_desk import CaptureDeskApp
from tests.tk_support import destroy_root

called = []
for _ in range(3):
    root = tk.Tk()
    root.withdraw()
    ttk.Style(root).theme_use('clam')
    root.after_idle(lambda: called.append(True))
    root.after(0, lambda: called.append(True))
    destroy_root(root)
    with tempfile.TemporaryDirectory() as directory:
        app = CaptureDeskApp(app_data_dir=directory)
        app.destroy()
root = tk.Tk()
root.update()
destroy_root(root)
assert not called, called
'''
        result = subprocess.run([sys.executable, '-c', script],
                                cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, '')
