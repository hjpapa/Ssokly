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


if __name__ == '__main__':
    unittest.main()
