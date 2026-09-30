"""Create a source submission from tracked files only, never from the working folder wholesale."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import zipfile
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


def build(url, output):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ('', '/')):
        raise ValueError('Use an HTTPS server origin without credentials.')
    paths = subprocess.check_output(['git', 'ls-files'], cwd=ROOT, text=True).splitlines()
    entries = {}
    for name in paths:
        path = Path(name)
        if (any(part.startswith('.') for part in path.parts)
                or (path.parts[0] in ('backend', 'tests', 'tools', 'docs') and name != 'backend/README.md')
                or path.suffix.lower() not in ('.py', '.md', '.txt', '.json', '.png', '.ico', '.svg')):
            continue
        source = ROOT / path
        if source.is_symlink():
            raise ValueError('Submission refuses symlinks.')
        data = source.read_bytes()
        if re.search(rb'(?<![A-Za-z0-9_])sk-(?:proj-)?[A-Za-z0-9_-]{20,}', data):
            raise ValueError('Possible secret found; submission stopped.')
        entries[name] = data
    entries['ai-server.json'] = json.dumps({'url': url.rstrip('/')}, indent=2).encode()
    entries['START-HERE.txt'] = ('Ssokly source submission\nInstall Python with Tk, then run:\n'
                               'python -m pip install -r requirements.txt\npython main.py\n'
                               'AI uses the configured AI server. No API key or login is needed.\n').encode()
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr('Ssokly/' + name, data)
    return len(entries)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', required=True)
    parser.add_argument('--output', default=str(ROOT / 'dist' / 'Ssokly-submission.zip'))
    args = parser.parse_args()
    print('Submission files:', build(args.url, args.output))
