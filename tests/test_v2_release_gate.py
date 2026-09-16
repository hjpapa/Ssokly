"""The release gate itself must fail closed, without contacting a host."""
import contextlib
import io
import json
import socket
import unittest
from unittest.mock import patch

from tools.verify_v2_release import run_gate


class ReleaseGateTests(unittest.TestCase):
    def run_synthetic_gate(self, body):
        suite = unittest.TestSuite([unittest.FunctionTestCase(body)])
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch('tools.verify_v2_release.unittest.defaultTestLoader.loadTestsFromNames', return_value=suite), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = run_gate()
        return code, json.loads(stdout.getvalue())

    def test_clean_offline_test_passes(self):
        code, report = self.run_synthetic_gate(lambda: None)
        self.assertEqual(code, 0)
        self.assertTrue(report['passed'])
        self.assertEqual(report['network_attempts'], 0)

    def test_even_caught_network_attempt_fails_gate(self):
        def attempt():
            try:
                socket.getaddrinfo('synthetic.invalid', 443)
            except RuntimeError:
                pass
        original = socket.getaddrinfo
        code, report = self.run_synthetic_gate(attempt)
        self.assertEqual(code, 1)
        self.assertEqual(report['network_attempts'], 1)
        self.assertIs(socket.getaddrinfo, original)

    def test_skipped_test_is_not_a_release_pass(self):
        def skip():
            raise unittest.SkipTest('synthetic blocked UI')
        code, report = self.run_synthetic_gate(skip)
        self.assertEqual(code, 1)
        self.assertEqual(report['skipped'], 1)


if __name__ == '__main__':
    unittest.main()
