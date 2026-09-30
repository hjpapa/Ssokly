"""Build a keyless Windows folder distribution; never copies workspace data."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
from importlib import metadata

ROOT = Path(__file__).resolve().parents[1]


def main():
    subprocess.run([
        sys.executable, '-m', 'PyInstaller', '--noconfirm', '--windowed',
        '--onedir', '--name', 'Ssokly', '--paths', str(ROOT),
        '--distpath', str(ROOT / 'dist'),
        '--workpath', str(ROOT / '.local-results/pyinstaller'),
        '--specpath', str(ROOT / '.local-results'),
        '--collect-all', 'pypdfium2_raw',
        str(ROOT / 'packaging/windows_entry.py'),
    ], cwd=ROOT, check=True)
    destination = ROOT / 'dist/Ssokly'
    config = json.loads((ROOT / 'ai-server.json').read_text(encoding='utf-8'))
    if config != {'url': 'https://ssokly-ai-relay.vercel.app'}:
        raise ValueError('Unexpected release relay configuration')
    (destination / 'ai-server.json').write_text(json.dumps(config, indent=2), encoding='utf-8')
    shutil.copyfile(ROOT / 'packaging/START-HERE.txt', destination / 'START-HERE.txt')
    notices = destination / 'THIRD-PARTY-LICENSES'
    notices.mkdir(exist_ok=True)
    for distribution in metadata.distributions():
        for item in distribution.files or ():
            if any(word in Path(item).name.lower() for word in ('license', 'copying', 'notice')):
                source = Path(distribution.locate_file(item))
                if source.is_file():
                    target = notices / distribution.metadata['Name'] / str(item).replace('../', '')
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
    python_license = Path(sys.base_prefix) / 'LICENSE.txt'
    if python_license.exists():
        shutil.copyfile(python_license, notices / 'PYTHON-LICENSE.txt')
    # Explicitly exclude private inputs. PyInstaller only collects imported code
    # and dependency resources; no --add-data of the project directory is used.
    for path in destination.rglob('*'):
        if path.is_file() and (path.name.startswith('.env') or path.suffix in ('.sqlite3', '.db', '.log')):
            raise ValueError('Unexpected private-data file in distribution')
    print(destination / 'Ssokly.exe')


if __name__ == '__main__':
    main()
