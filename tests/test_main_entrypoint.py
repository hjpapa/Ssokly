"""Default entrypoint contract without opening user data or desktop windows."""
import ast
from pathlib import Path
import unittest
from unittest.mock import patch


class MainEntrypointTests(unittest.TestCase):
    def test_default_main_runs_capture_desk_only(self):
        import main
        from ui.capture_desk import CaptureDeskApp
        self.assertIs(main.CaptureDeskApp, CaptureDeskApp)
        with patch.object(main, '_enable_windows_dpi_awareness') as dpi, patch.object(main, 'CaptureDeskApp') as app:
            main.main()
        dpi.assert_called_once_with()
        app.assert_called_once_with()
        app.return_value.mainloop.assert_called_once_with()

    def test_startup_does_not_import_old_monolithic_ui(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'main.py').read_text(encoding='utf-8'))
        imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        self.assertIn('ui.capture_desk', imports)
        self.assertNotIn('ui.app', imports)

    def test_unavailable_storage_is_reported_without_silent_new_library(self):
        import main
        with patch.object(main, '_enable_windows_dpi_awareness'), \
                patch.object(main, 'CaptureDeskApp', side_effect=OSError('합성 저장 장치 오류')) as app, \
                patch('tkinter.Tk') as root, patch('tkinter.messagebox.showerror') as show:
            main.main()
        app.assert_called_once_with()
        show.assert_called_once()
        root.return_value.destroy.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
