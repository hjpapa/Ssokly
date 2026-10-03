"""Build, sign and verify a Windows release in a fresh staging directory.

Requires a trusted code-signing certificate with private-key access in the
current user's Windows certificate store. Never uploads or publishes files.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def file_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def run(command):
    subprocess.run(command, cwd=ROOT, check=True)


def signing_command(tool, thumbprint, timestamp):
    return [str(tool), 'sign', '/sha1', thumbprint, '/s', 'My',
            '/fd', 'SHA256', '/tr', timestamp, '/td', 'SHA256', '/d', 'Ssokly']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', default='0.1.0')
    parser.add_argument('--output', type=Path, required=True,
                        help='New, nonexistent release directory; existing releases are preserved.')
    parser.add_argument('--signtool', type=Path)
    parser.add_argument('--iscc', type=Path)
    parser.add_argument('--thumbprint', help='Certificate thumbprint in CurrentUser/My; no PFX password.')
    parser.add_argument('--timestamp', default='http://timestamp.digicert.com')
    parser.add_argument('--plan', action='store_true', help='Validate options and show stages without building/signing.')
    args = parser.parse_args()
    if not re.fullmatch(r'\d+\.\d+\.\d+', args.version):
        parser.error('Version must be major.minor.patch')
    output = args.output.resolve()
    if output.exists():
        parser.error('Output already exists; choose a fresh directory')
    stages = ['build folder', 'sign EXE with SHA256 and RFC3161 timestamp',
              'verify EXE', 'offline packaged self-test',
              'compile installer and sign uninstaller + installer',
              'verify installer', 'archive signed portable folder', 'write SHA256 manifest']
    if args.plan:
        print(json.dumps({'version': args.version, 'stages': stages}, indent=2))
        return
    thumbprint = re.sub(r'\s', '', args.thumbprint or '')
    if not re.fullmatch(r'[0-9a-fA-F]{40}', thumbprint):
        parser.error('A 40-digit signing certificate thumbprint is required')
    for label, tool in [('signtool', args.signtool), ('iscc', args.iscc)]:
        if tool is None or not tool.is_file():
            parser.error(f'--{label} must point to the installed tool')
    if not re.fullmatch(r'https?://[A-Za-z0-9./:_-]+', args.timestamp):
        parser.error('Invalid timestamp URL')
    sign = signing_command(args.signtool.resolve(), thumbprint, args.timestamp)
    # Inno expands $q to a quote and $f to the filename, including its quotes.
    # Paths containing $ cannot safely be represented in that expansion syntax.
    if '$' in str(args.signtool.resolve()):
        parser.error('SignTool path cannot contain $')
    inno_sign = subprocess.list2cmdline(sign).replace('"', '$q') + ' $f'
    # Fail before a costly build when the certificate or private key is missing.
    run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
         "$c = Get-ChildItem -LiteralPath 'Cert:\\CurrentUser\\My' -CodeSigningCert "
         "-ErrorAction Stop | Where-Object Thumbprint -EQ '" + thumbprint +
         "'; if ($null -eq $c -or -not $c.HasPrivateKey -or $c.NotAfter -le (Get-Date) "
         "-or $c.NotBefore -gt (Get-Date)) "
         "{ throw 'Valid code-signing certificate and private key required' }"])
    output.mkdir(parents=True)
    run([sys.executable, str(ROOT / 'tools/build_windows.py'), '--dist-dir', str(output)])
    executable = output / 'Ssokly/Ssokly.exe'
    run(sign + [str(executable)])
    run([str(args.signtool), 'verify', '/pa', '/all', '/v', str(executable)])
    report = output / 'self-test.json'
    run([str(executable), '--self-test-report', str(report)])
    result = json.loads(report.read_text(encoding='utf-8'))
    if result.get('passed') is not True or result.get('frozen') is not True:
        raise RuntimeError('Packaged self-test failed; release stopped')
    run([str(args.iscc), f'/DAppSource={output / "Ssokly"}',
         f'/DReleaseOutput={output}', f'/DAppVersion={args.version}',
         '/DSignedRelease=1', f'/Sssokly={inno_sign}', str(ROOT / 'packaging/Ssokly.iss')])
    installer = output / f'Ssokly-Setup-{args.version}-x64.exe'
    run([str(args.signtool), 'verify', '/pa', '/all', '/v', str(installer)])
    archive = output / f'Ssokly-{args.version}-x64-portable.zip'
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted((output / 'Ssokly').rglob('*')):
            if path.is_file():
                bundle.write(path, arcname=str(path.relative_to(output)))
    manifest = {'version': args.version, 'signed': True, 'published': False, 'files': []}
    for path in [executable, installer, archive]:
        manifest['files'].append({'file': str(path.relative_to(output)), 'bytes': path.stat().st_size,
                                 'sha256': file_hash(path)})
    (output / 'release-manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print('Signed and verified release is ready. Publishing is a separate action.')


if __name__ == '__main__':
    main()
