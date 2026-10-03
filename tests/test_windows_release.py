"""Release gate ordering without executing compilers, signing or network calls."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools import release_windows as release


class WindowsReleaseTests(unittest.TestCase):
    def exercise(self, failure=None):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            tool = base / 'tool.exe'
            tool.write_bytes(b'synthetic')
            output = base / 'release'
            commands = []

            def fake_run(command):
                commands.append(command)
                if failure and failure(command):
                    raise subprocess.CalledProcessError(1, command)
                if any(Path(part).name == 'build_windows.py' for part in command):
                    (output / 'Ssokly').mkdir()
                    (output / 'Ssokly/Ssokly.exe').write_bytes(b'app')
                if '--self-test-report' in command:
                    Path(command[-1]).write_text(json.dumps({'passed': True, 'frozen': True}))
                if '/DSignedRelease=1' in command:
                    (output / 'Ssokly-Setup-0.1.0-x64.exe').write_bytes(b'installer')

            argv = ['release', '--output', str(output), '--thumbprint', 'A' * 40,
                    '--signtool', str(tool), '--iscc', str(tool)]
            with patch.object(sys, 'argv', argv), patch.object(release, 'run', side_effect=fake_run):
                if failure:
                    with self.assertRaises(subprocess.CalledProcessError):
                        release.main()
                    self.assertFalse((output / 'release-manifest.json').exists())
                else:
                    release.main()
                    manifest = json.loads((output / 'release-manifest.json').read_text())
                    self.assertTrue(manifest['signed'])
                    self.assertFalse(manifest['published'])
                    self.assertEqual(len(manifest['files']), 3)
            return commands

    def test_sign_verify_selftest_before_signed_installer(self):
        commands = self.exercise()
        self.assertEqual(commands[2][1], 'sign')
        self.assertIn('/tr', commands[2])
        self.assertEqual(commands[3][1], 'verify')
        self.assertIn('--self-test-report', commands[4])
        self.assertIn('/DSignedRelease=1', commands[5])
        self.assertIn('$f', ' '.join(commands[5]))
        self.assertEqual(commands[6][1], 'verify')

    def test_exe_signature_failure_stops_before_installer(self):
        commands = self.exercise(lambda command: command[1:2] == ['verify'])
        self.assertFalse(any('/DSignedRelease=1' in command for command in commands))

    def test_missing_certificate_stops_before_build(self):
        commands = self.exercise(lambda command: command[0] == 'powershell.exe')
        self.assertEqual(len(commands), 1)
