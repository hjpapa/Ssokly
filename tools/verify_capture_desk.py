"""Offline capture-desk gate: synthetic fixtures, temporary stores, no live API.

No --live option. Socket/DNS guards cover test collection and execution. This
is an in-process regression check, not an OS sandbox or a model-quality test.
"""
import argparse
from contextlib import ExitStack, redirect_stdout
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SUITES = (
    'tests.test_main_entrypoint',
    'tests.test_document_library',
    'tests.test_desk_widgets',
    'tests.test_text_actions',
    'tests.test_desk_transfer',
    'tests.test_capture_desk',
    'tests.test_capture_desk_flow',
    'tests.test_capture_acceptance',
    'tests.test_desk_mask_restore',
    'tests.test_capture_desk_recovery',
    'tests.test_capture_recovery',
    'tests.test_legacy_reader',
    'tests.test_capture_desk_gate',
)

LIMITS = (
    'synthetic_fixtures_only',
    'in_process_socket_guard_not_os_sandbox',
    'not_live_model_quality',
    'not_native_capture_or_clipboard',
    'not_packaged_exe',
    'not_manual_multi_monitor',
)


def run_gate():
    attempts = []

    def deny_network(*_args, **_kwargs):
        # Count attempts without retaining hosts, request bodies or credentials.
        attempts.append(True)
        raise RuntimeError('Offline capture-desk gate prohibits network access.')

    started = time.perf_counter()
    output = io.StringIO()
    with tempfile.TemporaryDirectory(prefix='ssokly-capture-gate-') as directory, ExitStack() as guards:
        # These contexts are not registered with patch.start(), so a test's
        # patch.stopall() cleanup cannot accidentally remove the gate guards.
        for owner, names in (
            (socket.socket, ('connect', 'connect_ex', 'send', 'sendall', 'sendto', 'sendmsg')),
            (socket, ('create_connection', 'getaddrinfo', 'gethostbyname',
                      'gethostbyname_ex', 'gethostbyaddr', 'getnameinfo')),
        ):
            for name in names:
                if hasattr(owner, name):
                    guards.enter_context(patch.object(owner, name, deny_network))
        guards.enter_context(patch.dict(os.environ, {
            'LOCALAPPDATA': directory, 'APPDATA': directory,
            'OPENAI_API_KEY': 'synthetic-offline-gate-key',
        }))
        # Prevent project .env reads as well as the default user-data fallback.
        guards.enter_context(patch.object(dotenv, 'load_dotenv', return_value=False))
        with redirect_stdout(output):
            try:
                suite = unittest.defaultTestLoader.loadTestsFromNames(SUITES)
            except Exception:
                # Some non-ImportError exceptions escape unittest collection.
                # Still return a machine-readable failure and restore guards.
                result = unittest.TestResult()
                result.addError(unittest.FunctionTestCase(lambda: None), sys.exc_info())
                output.write(result.errors[0][1])
            else:
                result = unittest.TextTestRunner(stream=output, verbosity=1).run(suite)

    passed = result.wasSuccessful() and result.testsRun > 0 and not result.skipped and not attempts
    report = {
        'passed': passed,
        'mode': 'offline-synthetic-mock-capture-desk',
        'count': result.testsRun,
        'tests': result.testsRun,
        'failure': len(result.failures),
        'failures': len(result.failures),
        'errors': len(result.errors),
        'skips': len(result.skipped),
        'skipped': len(result.skipped),
        'network_attempts': len(attempts),
        'seconds': round(time.perf_counter() - started, 3),
        'limits': list(LIMITS),
    }
    if not passed:
        # Participating suites contain synthetic fixtures only.
        print(output.getvalue(), file=sys.stderr)
    print(json.dumps(report, ensure_ascii=True))
    return 0 if passed else 1


if __name__ == '__main__':
    argparse.ArgumentParser(description=__doc__).parse_args()
    raise SystemExit(run_gate())
