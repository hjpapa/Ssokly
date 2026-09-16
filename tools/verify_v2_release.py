"""Offline V2 release gate: isolated fixtures only, outbound sockets forbidden.

No --live option is provided. This checks implementation contracts, not model
interpretation accuracy or school authorization to send real documents.
"""
import argparse
import io
import json
from pathlib import Path
import socket
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SUITES = (
    'tests.test_v2_semantics', 'tests.test_date_context_regressions',
    'tests.test_card_outputs', 'tests.test_public_audience', 'tests.test_source_schedule_fallback',
    'tests.test_work_card_store', 'tests.test_work_cards_ui', 'tests.test_storage_faults',
    'tests.test_transfer_policy', 'tests.test_v2_transfer_integration',
    'tests.test_v2_app_flow',
)


def run_gate():
    attempts = []
    def deny_network(*_args, **_kwargs):
        # Record no host, request contents, or credentials.
        attempts.append(True)
        raise RuntimeError('Offline release gate prohibits network access.')

    started = time.perf_counter()
    suite = unittest.defaultTestLoader.loadTestsFromNames(SUITES)
    output = io.StringIO()
    with patch.object(socket.socket, 'connect', deny_network), \
         patch.object(socket.socket, 'connect_ex', deny_network), \
         patch.object(socket, 'create_connection', deny_network), \
         patch.object(socket, 'getaddrinfo', deny_network):
        result = unittest.TextTestRunner(stream=output, verbosity=1).run(suite)
    report = {
        'passed': result.wasSuccessful() and not result.skipped and not attempts,
        'mode': 'offline-synthetic-mock',
        'tests': result.testsRun,
        'failures': len(result.failures), 'errors': len(result.errors),
        'skipped': len(result.skipped), 'network_attempts': len(attempts),
        'seconds': round(time.perf_counter() - started, 3),
        'limits': ['not_live_model_quality', 'not_packaged_exe', 'not_manual_multi_monitor'],
    }
    if not report['passed']:
        # Only synthetic fixtures participate in this gate.
        print(output.getvalue(), file=sys.stderr)
    print(json.dumps(report, ensure_ascii=True))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    argparse.ArgumentParser(description=__doc__).parse_args()
    raise SystemExit(run_gate())
