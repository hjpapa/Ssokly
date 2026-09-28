"""Capture location preferences and lossless, non-destructive relocation."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile

from services.app_paths import default_app_data_dir, desktop_directory
from services.capture_store import METADATA_DATABASE_FILENAME


class CaptureLocation:
    def __init__(self, app_data_dir=None):
        self.app_data_dir = Path(app_data_dir) if app_data_dir is not None else default_app_data_dir()
        self.path = self.app_data_dir / 'capture-location.json'

    def resolve(self):
        if self.path.exists():
            try:
                payload = json.loads(self.path.read_text(encoding='utf-8'))
                value = payload['directory']
                if payload['version'] != 1 or not isinstance(value, str) or not Path(value).is_absolute():
                    raise ValueError()
                directory = Path(value)
            except (ValueError, KeyError, TypeError) as error:
                raise ValueError('캡처 저장 위치 설정을 읽을 수 없습니다. capture-location.json을 확인하세요.') from error
            if not directory.is_dir():
                raise OSError('설정한 캡처 폴더가 없습니다. 저장 장치 연결 또는 폴더 위치를 확인하세요.')
            return directory.resolve()
        legacy = self.app_data_dir / 'capture_inbox'
        if legacy.exists():
            return legacy.resolve()
        directory = desktop_directory() / 'ssokly'
        # Never mix a new library with a pre-existing, unrelated capture store.
        if directory.exists() and any(directory.iterdir()):
            raise FileExistsError('바탕화면 ssokly 폴더가 비어 있지 않습니다. 기존 자료를 별도로 보관한 뒤 다시 실행하세요.')
        directory.mkdir(parents=True, exist_ok=True)
        self.save(directory)
        return directory.resolve()

    def save(self, directory):
        self.app_data_dir.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.app_data_dir,
                                             prefix='.capture-location-', suffix='.tmp', delete=False) as stream:
                temporary = Path(stream.name)
                json.dump({'version': 1, 'directory': str(Path(directory).resolve())}, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def relocate(self, store, target):
        """Copy committed captures and metadata; publish the pointer last.

        Original files remain as a recovery copy. The UI exits after success,
        so no further writes are made to the old store by that app instance.
        """
        target = Path(target).expanduser()
        if target.is_symlink():
            raise ValueError('바로가기 대신 실제 빈 폴더를 선택하세요.')
        target = target.resolve()
        source = store.directory.resolve()
        if target == source:
            return False
        if source in target.parents or target in source.parents:
            raise ValueError('현재 캡처 폴더의 상위·하위 폴더는 선택할 수 없습니다.')
        if target.exists() and (not target.is_dir() or any(target.iterdir())):
            raise ValueError('자료를 덮어쓰지 않도록 비어 있는 폴더를 선택하세요.')
        target.parent.mkdir(parents=True, exist_ok=True)
        # This temporary tree is owned by this operation, never the selected folder.
        with tempfile.TemporaryDirectory(prefix='.ssokly-copy-', dir=target.parent) as temp:
            stage = Path(temp) / 'captures'
            stage.mkdir()
            with closing(sqlite3.connect(store.db_path, timeout=5)) as lock:
                lock.execute('BEGIN IMMEDIATE')
                try:
                    rows = lock.execute('SELECT storage_name,content_sha256 FROM capture_items').fetchall()
                    for name, expected_hash in rows:
                        original = store._owned_path(name)
                        copied = stage / name
                        shutil.copyfile(original, copied)
                        digest = hashlib.sha256()
                        with copied.open('rb') as stream:
                            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                                digest.update(chunk)
                        if expected_hash and digest.hexdigest() != expected_hash:
                            raise ValueError('캡처 파일 검증에 실패했습니다. 기존 폴더는 유지됩니다.')
                    # A separate reader backs up the committed database while
                    # BEGIN IMMEDIATE prevents another writer changing it.
                    with closing(sqlite3.connect(store.db_path)) as reader, \
                            closing(sqlite3.connect(stage / METADATA_DATABASE_FILENAME)) as backup:
                        reader.backup(backup)
                        if backup.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                            raise ValueError('캡처 정보 검증에 실패했습니다.')
                    if target.exists():
                        target.rmdir()  # Only an empty directory can be removed.
                    stage.rename(target)
                finally:
                    lock.rollback()
        # Publish only after database handles and temporary files are closed.
        self.save(target)
        return True
