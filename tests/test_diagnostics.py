"""Only operation names and exception classes may reach local diagnostics."""
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from services.diagnostics import LOGGER, configure_local_logging, log_failure
from services.text_actions import generate_text_action, TextActionError


class DiagnosticsTests(unittest.TestCase):
    def test_file_contains_type_but_no_exception_text_or_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(configure_local_logging(directory))
            log_failure('synthetic.save', OSError('SECRET body key path student'))
            text = (Path(directory) / 'logs' / 'diagnostics.log').read_text(encoding='utf-8')
            self.assertIn('synthetic.save: OSError', text)
            self.assertNotIn('SECRET', text)
            self.assertNotIn('Traceback', text)

    def test_log_setup_and_write_failure_do_not_interrupt_app(self):
        with patch.object(Path, 'mkdir', side_effect=PermissionError('SECRET')):
            self.assertFalse(configure_local_logging('unused'))
        with tempfile.TemporaryDirectory() as directory:
            configure_local_logging(directory)
            with patch('logging.FileHandler._open', side_effect=OSError('SECRET')):
                log_failure('synthetic.save', RuntimeError('SECRET'))

    def test_direct_api_failure_logs_type_without_request_contents(self):
        with patch('services.text_actions.load_dotenv'), patch.dict('os.environ', {'OPENAI_API_KEY': 'synthetic'}), \
                patch('openai.OpenAI', side_effect=RuntimeError('SECRET request')), \
                self.assertLogs(LOGGER, level='WARNING') as captured:
            with self.assertRaises(TextActionError):
                generate_text_action('SECRET body')
        self.assertIn('text_actions.generate_text_action: RuntimeError', captured.output[0])
        self.assertNotIn('SECRET', ''.join(captured.output))
