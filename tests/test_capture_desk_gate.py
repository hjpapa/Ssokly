"""The offline gate must fail closed, including during synthetic collection."""
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import unittest
from unittest.mock import patch

from tools import verify_capture_desk as gate


class CaptureDeskGateTests(unittest.TestCase):
    def synthetic_gate(self, body=None, *, during_collection=False):
        output, errors = io.StringIO(), io.StringIO()
        original_sockets = (socket.getaddrinfo, socket.socket.connect, socket.socket.sendto)
        environment = {name: os.getenv(name) for name in ('LOCALAPPDATA', 'APPDATA', 'OPENAI_API_KEY')}
        original_dotenv = gate.dotenv.load_dotenv

        def load(_names):
            if during_collection:
                body()
                return unittest.TestSuite([unittest.FunctionTestCase(lambda: None)])
            return unittest.TestSuite([] if body is None else [unittest.FunctionTestCase(body)])

        with patch.object(gate.unittest.defaultTestLoader, 'loadTestsFromNames', side_effect=load), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = gate.run_gate()
        report = json.loads(output.getvalue())
        self.assertEqual((socket.getaddrinfo, socket.socket.connect, socket.socket.sendto), original_sockets)
        # Never include inherited key/environment values in assertion messages.
        self.assertTrue(all(os.getenv(name) == value for name, value in environment.items()),
                        'The gate must restore inherited environment variables.')
        self.assertIs(gate.dotenv.load_dotenv, original_dotenv)
        self.assertEqual(report['count'], report['tests'])
        self.assertEqual(report['failure'], report['failures'])
        self.assertEqual(report['skips'], report['skipped'])
        return code, report

    @staticmethod
    def caught_dns():
        try:
            socket.getaddrinfo('synthetic.invalid', 443)
        except RuntimeError:
            pass

    def test_clean_fixture_uses_temporary_defaults_and_restores_environment(self):
        previous = os.getenv('LOCALAPPDATA')
        directory = []

        def body():
            self.assertNotEqual(os.getenv('LOCALAPPDATA'), previous)
            directory.append(Path(os.environ['LOCALAPPDATA']))
            self.assertTrue(directory[0].is_dir())
            self.assertFalse(gate.dotenv.load_dotenv())

        code, report = self.synthetic_gate(body)
        self.assertEqual(code, 0)
        self.assertTrue(report['passed'])
        self.assertEqual(report['network_attempts'], 0)
        self.assertFalse(directory[0].exists())

    def test_zero_tests_never_pass(self):
        code, report = self.synthetic_gate()
        self.assertEqual(code, 1)
        self.assertEqual(report['count'], 0)
        self.assertFalse(report['passed'])

    def test_skipped_test_never_passes(self):
        def skip():
            raise unittest.SkipTest('synthetic skipped case')

        code, report = self.synthetic_gate(skip)
        self.assertEqual(code, 1)
        self.assertEqual(report['skips'], 1)

    def test_assertion_failure_is_reported(self):
        def fail():
            raise AssertionError('synthetic assertion')

        code, report = self.synthetic_gate(fail)
        self.assertEqual(code, 1)
        self.assertEqual(report['failure'], 1)

    def test_test_error_is_reported(self):
        def error():
            raise ValueError('synthetic exception')

        code, report = self.synthetic_gate(error)
        self.assertEqual(code, 1)
        self.assertEqual(report['errors'], 1)

    def test_caught_tcp_connection_attempt_fails_even_if_test_returns(self):
        def attempt():
            with socket.socket() as connection:
                try:
                    connection.connect(('192.0.2.1', 443))
                except RuntimeError:
                    pass

        code, report = self.synthetic_gate(attempt)
        self.assertEqual(code, 1)
        self.assertEqual(report['network_attempts'], 1)
        self.assertEqual(report['errors'], 0)

    def test_caught_udp_send_attempt_fails_without_sending_bytes(self):
        def attempt():
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
                try:
                    connection.sendto(b'synthetic', ('192.0.2.1', 443))
                except RuntimeError:
                    pass

        code, report = self.synthetic_gate(attempt)
        self.assertEqual(code, 1)
        self.assertEqual(report['network_attempts'], 1)

    def test_dns_attempt_during_test_import_is_also_blocked(self):
        code, report = self.synthetic_gate(self.caught_dns, during_collection=True)
        self.assertEqual(code, 1)
        self.assertEqual(report['network_attempts'], 1)

    def test_uncaught_import_network_error_is_reported_and_guards_restored(self):
        def import_attempt():
            socket.getaddrinfo('synthetic.invalid', 443)

        code, report = self.synthetic_gate(import_attempt, during_collection=True)
        self.assertEqual(code, 1)
        self.assertEqual(report['network_attempts'], 1)
        self.assertEqual(report['errors'], 1)
        self.assertEqual(report['count'], 0)

    def test_test_cleanup_cannot_remove_gate_guards(self):
        def cleanup_then_attempt():
            patch.stopall()
            self.caught_dns()

        code, report = self.synthetic_gate(cleanup_then_attempt)
        self.assertEqual(code, 1)
        self.assertEqual(report['network_attempts'], 1)


if __name__ == '__main__':
    unittest.main()
