"""Capture-first documents over existing stores, without destructive migration.

Page order and new text/results live in an additive sidecar. Capture images,
raw OCR, and teacher text remain owned by CaptureStore. Existing tasks/captures
are read-only entries until explicitly adopted; startup never imports them.
"""
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from services.capture_store import CaptureConflictError, CaptureStore
from services.task_store import TaskStore


SCHEMA_VERSION = 4
_CAPTURE_WARNING = '캡처 이미지를 읽을 수 없습니다. 저장된 텍스트는 유지되며 복구가 필요합니다.'
_DOCUMENT_WARNING = '일부 캡처를 읽을 수 없습니다. 저장된 텍스트는 유지되며 복구가 필요합니다.'
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS documents (id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, trashed_at TEXT, labels_json TEXT NOT NULL DEFAULT '[]', memo TEXT NOT NULL DEFAULT '')",
    'CREATE TABLE IF NOT EXISTS pages (id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id), capture_id TEXT, text TEXT NOT NULL, source_name TEXT NOT NULL, source_path TEXT, position INTEGER NOT NULL, updated_at TEXT NOT NULL, ocr_text TEXT NOT NULL DEFAULT \'\', edited INTEGER NOT NULL DEFAULT 0, UNIQUE(document_id,capture_id))',
    'CREATE INDEX IF NOT EXISTS pages_by_document ON pages(document_id,position)',
    'CREATE TABLE IF NOT EXISTS outputs (id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id), mode TEXT NOT NULL, text TEXT NOT NULL, source_fingerprint TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)',
    'CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value_json TEXT NOT NULL)',
)


class LibraryConflictError(ValueError):
    """A newer saved value exists; retain the editor and reload/compare."""


class LibraryReadOnlyError(ValueError):
    """Legacy references must not be silently rewritten by the new library."""


def _stamp(previous=None):
    value = datetime.now(timezone.utc)
    if previous:
        value = max(value, datetime.fromisoformat(previous) + timedelta(microseconds=1))
    return value.isoformat(timespec='microseconds')


def _iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec='microseconds') if value else ''


def _safe_iso(value, fallback=''):
    """A damaged capture's date must not hide its preserved text or siblings."""
    try:
        return _iso(datetime.fromisoformat(value) if isinstance(value, str) else value)
    except (ValueError, TypeError, AttributeError, OverflowError):
        return fallback


def _text(value):
    if not isinstance(value, str):
        raise TypeError('문자열 값이 필요합니다.')
    return value


def _title(value):
    value = _text(value).strip()
    if not value:
        raise ValueError('문서 제목을 입력해 주세요.')
    return value


def _labels(value):
    if isinstance(value, str):
        value = value.split(',')
    if not isinstance(value, (list, tuple)):
        raise TypeError('라벨 목록이 필요합니다.')
    result = []
    for label in value:
        label = _text(label).strip()
        if not label:
            continue
        if len(label) > 24 or '\n' in label or ',' in label:
            raise ValueError('라벨은 24자 이내이며 쉼표와 줄바꿈을 포함할 수 없습니다.')
        if label.casefold() not in {item.casefold() for item in result}:
            result.append(label)
    if len(result) > 8:
        raise ValueError('라벨은 최대 8개까지 지정할 수 있습니다.')
    return result


class DocumentLibrary:
    def __init__(self, app_data_dir, *, capture_store=None, task_store=None):
        self.app_data_dir = Path(app_data_dir).expanduser().resolve(strict=False)
        self.app_data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.app_data_dir / 'document_library.sqlite3'
        # An explicit capture directory avoids CaptureStore's legacy auto-import.
        self.capture_store = capture_store if capture_store is not None else CaptureStore(self.app_data_dir / 'capture_inbox')
        self.task_store = task_store if task_store is not None else TaskStore(app_data_dir=self.app_data_dir)
        self._initialize()
        self._repair_deleted_only_documents()

    def _auto_trash(self, db, document_id, page_id):
        db.execute('UPDATE documents SET trashed_at=? WHERE id=?', (_stamp(), document_id))
        db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',
                   ('auto_page_trash:' + document_id, json.dumps(page_id)))
        self._touch(db, document_id)

    def _repair_deleted_only_documents(self):
        # Older versions left labelled empty documents after deleting the last page.
        with self._db(write=True) as db:
            for row in list(db.execute('SELECT id FROM documents WHERE trashed_at IS NULL '
                    'AND EXISTS (SELECT 1 FROM pages WHERE document_id=documents.id) '
                    'AND NOT EXISTS (SELECT 1 FROM pages WHERE document_id=documents.id AND trashed_at IS NULL)')):
                page = db.execute('SELECT id FROM pages WHERE document_id=? ORDER BY trashed_at DESC,id LIMIT 1', (row['id'],)).fetchone()
                self._auto_trash(db, row['id'], page['id'])

    @contextmanager
    def _db(self, write=False):
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            if write:
                db.execute('BEGIN IMMEDIATE')
            with db:
                yield db

    def _initialize(self):
        existed = self.path.exists()
        with self._db() as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version > SCHEMA_VERSION:
                raise ValueError('새 버전 문서 보관함입니다. 앱 업데이트 후 열어 주세요.')
            if version == SCHEMA_VERSION:
                self._validate_schema(db)
                return
        if existed:
            backup = self.path.with_name(self.path.name + '.before-library-' + uuid4().hex[:12] + '.bak')
            with closing(sqlite3.connect(self.path)) as source, closing(sqlite3.connect(backup)) as destination:
                source.backup(destination)
        with self._db(write=True) as db:
            for statement in _SCHEMA:
                db.execute(statement)
            columns = {row[1] for row in db.execute('PRAGMA table_info(pages)')}
            if 'ocr_text' not in columns:
                db.execute("ALTER TABLE pages ADD COLUMN ocr_text TEXT NOT NULL DEFAULT ''")
            if 'edited' not in columns:
                # Old sidecars cannot distinguish typed text from OCR. Preserve
                # every existing value instead of guessing it is replaceable.
                db.execute('ALTER TABLE pages ADD COLUMN edited INTEGER NOT NULL DEFAULT 1')
            if 'trashed_at' not in columns:
                db.execute('ALTER TABLE pages ADD COLUMN trashed_at TEXT')
            document_columns = {row[1] for row in db.execute('PRAGMA table_info(documents)')}
            if 'labels_json' not in document_columns:
                db.execute("ALTER TABLE documents ADD COLUMN labels_json TEXT NOT NULL DEFAULT '[]'")
            if 'memo' not in document_columns:
                db.execute("ALTER TABLE documents ADD COLUMN memo TEXT NOT NULL DEFAULT ''")
            self._validate_schema(db)
            db.execute('PRAGMA user_version=4')

    @staticmethod
    def _validate_schema(db):
        expected = {
            'documents': {'id', 'title', 'created_at', 'updated_at', 'trashed_at', 'labels_json', 'memo'},
            'pages': {'id', 'document_id', 'capture_id', 'text', 'source_name', 'source_path', 'position', 'updated_at', 'ocr_text', 'edited', 'trashed_at'},
            'outputs': {'id', 'document_id', 'mode', 'text', 'source_fingerprint', 'created_at', 'updated_at'},
            'settings': {'key', 'value_json'},
        }
        for table, columns in expected.items():
            actual = {row[1] for row in db.execute(f'PRAGMA table_info({table})')}
            if not columns <= actual:
                raise ValueError('문서 보관함 구조를 확인할 수 없습니다. 기존 파일은 유지했습니다.')

    def _page(self, row, capture=None):
        item = dict(row)
        if item['capture_id']:
            if capture is None:
                entry = self.capture_store.safe_get(item['capture_id'])
                if entry is None or entry['record'] is None:
                    page = self._unreadable_capture_page(entry or {
                        'id': item['capture_id'], 'ocr_text': '',
                        'verified_text': item['text'], 'verified_at': item['updated_at'],
                        'updated_at': item['updated_at'],
                    }, document_id=item['document_id'])
                    return {**page, 'id': item['id'], 'legacy': False,
                            'source_name': item['source_name'], 'source_path': None,
                            'position': item['position'], 'missing': entry is None,
                            'updated_at': page['updated_at'] or item['updated_at']}
                capture = entry['record']
            return {'id': item['id'], 'document_id': item['document_id'], 'capture_id': item['capture_id'],
                    'path': str(capture.path) if capture else None,
                    'text': capture.effective_text if capture else item['text'],
                    'ocr_text': capture.ocr_text if capture else '',
                    'updated_at': _iso(capture.updated_at) if capture else item['updated_at'],
                    'source_name': item['source_name'], 'source_path': item['source_path'],
                    'legacy': False, 'readonly': False, 'missing': capture is None,
                    'ocr_status': capture.ocr_status if capture else 'unread',
                    'review_status': capture.review_status if capture else 'unverified',
                    'edited': capture.is_verified if capture else True,
                    'warning': '', 'recovery_required': False,
                    'position': item['position']}
        return {**item, 'edited': bool(item['edited']), 'path': None, 'legacy': False, 'readonly': False,
                'missing': False, 'warning': '', 'recovery_required': False}

    def _pages(self, db, document_id, *, trashed=False):
        return [self._page(row) for row in db.execute(
            'SELECT * FROM pages WHERE document_id=? AND (trashed_at IS NOT NULL)=? ORDER BY position,id',
            (document_id, bool(trashed)))]

    def _document(self, db, row):
        pages = self._pages(db, row['id'])
        needs_recovery = any(page['recovery_required'] for page in pages)
        return {'id': row['id'], 'title': row['title'], 'page_count': len(pages),
                'labels': json.loads(row['labels_json']), 'memo': row['memo'],
                'created_at': row['created_at'],
                'updated_at': max([row['updated_at'], *(page['updated_at'] for page in pages)]),
                'legacy': False, 'readonly': False, 'trashed': row['trashed_at'] is not None,
                'warning': _DOCUMENT_WARNING if needs_recovery else '', 'recovery_required': needs_recovery,
                'thumbnail_path': next((page['path'] for page in pages if page['path']), None)}

    def _managed(self, db, document_id, expected_updated_at=None, *, allow_trashed=False):
        row = db.execute('SELECT * FROM documents WHERE id=?', (document_id,)).fetchone()
        if row is None:
            raise LibraryReadOnlyError('기존 기록은 읽기 전용입니다. 새 문서로 담은 뒤 편집해 주세요.')
        if row['trashed_at'] and not allow_trashed:
            raise LibraryReadOnlyError('휴지통 문서를 먼저 복원해 주세요.')
        if expected_updated_at is not None and self._document(db, row)['updated_at'] != expected_updated_at:
            raise LibraryConflictError('문서가 다른 작업에서 변경되었습니다. 입력을 유지하고 현재 값을 확인해 주세요.')
        return row

    def _touch(self, db, document_id):
        row = db.execute('SELECT * FROM documents WHERE id=?', (document_id,)).fetchone()
        stamp = _stamp(self._document(db, row)['updated_at'])
        db.execute('UPDATE documents SET updated_at=? WHERE id=?', (stamp, document_id))
        return stamp

    def create_document(self, title='새 문서'):
        title = _title(title)
        with self._db(write=True) as db:
            saved = self._create_document(db, title)
        return saved

    def _create_document(self, db, title):
        document_id = uuid4().hex
        stamp = _stamp()
        # If the later sidecar write fails, this compatibility task remains
        # discoverable as a legacy entry rather than deleting saved data.
        self.task_store.create(task_id=document_id, title=title, output_mode='문서', source_text='', analysis_text='')
        db.execute('INSERT INTO documents(id,title,created_at,updated_at,trashed_at) VALUES (?,?,?,?,NULL)', (document_id, title, stamp, stamp))
        return self._document(db, db.execute('SELECT * FROM documents WHERE id=?', (document_id,)).fetchone())

    def add_capture(self, document_id, capture_id):
        with self._db(write=True) as db:
            saved = self._add_capture(db, document_id, capture_id)
        return saved

    def _add_capture(self, db, document_id, capture_id):
        self._managed(db, document_id)
        existing = db.execute('SELECT * FROM pages WHERE document_id=? AND capture_id=?', (document_id, capture_id)).fetchone()
        if existing:
            if existing['trashed_at']:
                raise LibraryReadOnlyError('삭제한 페이지에서 먼저 복원해 주세요.')
            return self._page(existing)
        capture = self.capture_store.get(capture_id)
        if capture is None:
            raise KeyError('캡처를 찾을 수 없습니다.')
        if capture.trashed_at:
            raise LibraryReadOnlyError('캡처를 먼저 휴지통에서 복원해 주세요.')
        page_id, stamp = uuid4().hex, _stamp()
        position = db.execute('SELECT COALESCE(MAX(position),-1)+1 FROM pages WHERE document_id=?', (document_id,)).fetchone()[0]
        db.execute('INSERT INTO pages(id,document_id,capture_id,text,source_name,source_path,position,updated_at) VALUES (?,?,?,?,?,?,?,?)',
                   (page_id, document_id, capture_id, capture.effective_text, capture.source, str(capture.path), position, stamp))
        # Cross-store failures roll back the sidecar. The image always stays
        # in the original inbox and is still visible as a standalone entry.
        self.capture_store.link_to_task([capture_id], document_id)
        self._touch(db, document_id)
        return self._page(db.execute('SELECT * FROM pages WHERE id=?', (page_id,)).fetchone(), capture)

    def adopt_capture(self, capture_id):
        with self._db(write=True) as db:
            row = db.execute('SELECT documents.* FROM documents JOIN pages ON pages.document_id=documents.id WHERE pages.capture_id=? AND documents.trashed_at IS NULL ORDER BY documents.created_at LIMIT 1', (capture_id,)).fetchone()
            if row:
                return self._document(db, row)
            capture = self.capture_store.get(capture_id)
            if capture is None:
                raise KeyError('캡처를 찾을 수 없습니다.')
            if capture.trashed_at:
                raise LibraryReadOnlyError('캡처를 먼저 휴지통에서 복원해 주세요.')
            document = self._create_document(db, capture.ocr_preview[:60] or '캡처 문서')
            self._add_capture(db, document['id'], capture_id)
            saved = self._document(db, db.execute('SELECT * FROM documents WHERE id=?', (document['id'],)).fetchone())
        return saved

    def add_text_page(self, document_id, text, source_name='', source_path=None):
        text, source_name = _text(text), _text(source_name)
        source_path = str(source_path) if source_path is not None else None
        page_id, stamp = uuid4().hex, _stamp()
        with self._db(write=True) as db:
            self._managed(db, document_id)
            position = db.execute('SELECT COALESCE(MAX(position),-1)+1 FROM pages WHERE document_id=?', (document_id,)).fetchone()[0]
            db.execute('INSERT INTO pages(id,document_id,capture_id,text,source_name,source_path,position,updated_at,ocr_text,edited) VALUES (?,?,?,?,?,?,?,?,?,0)',
                       (page_id, document_id, None, text, source_name, source_path, position, stamp, text))
            self._touch(db, document_id)
            saved = self._page(db.execute('SELECT * FROM pages WHERE id=?', (page_id,)).fetchone())
        return saved

    def save_page_text(self, page_id, text, expected_updated_at, *, expected_document_id=None):
        text = _text(text)
        with self._db(write=True) as db:
            row = db.execute('SELECT * FROM pages WHERE id=?', (page_id,)).fetchone()
            if row is None:
                raise LibraryReadOnlyError('기존 페이지는 읽기 전용입니다. 새 문서로 담은 뒤 편집해 주세요.')
            if expected_document_id is not None and row['document_id'] != expected_document_id:
                raise LibraryConflictError('페이지가 다른 문서로 이동했습니다. 입력을 보관하고 문서를 다시 열어 주세요.')
            self._managed(db, row['document_id'])
            if row['trashed_at']:
                raise LibraryReadOnlyError('삭제한 페이지를 먼저 복원해 주세요.')
            if row['capture_id']:
                if self._page(row)['readonly']:
                    raise LibraryReadOnlyError('복구가 필요한 캡처의 저장된 텍스트는 변경할 수 없습니다.')
                # CaptureStore is the sole owner of capture text. No second
                # sidecar write can fail after the capture save has committed.
                if not isinstance(expected_updated_at, str) or not expected_updated_at:
                    raise ValueError('페이지의 저장 버전을 확인해 주세요.')
                try:
                    capture = self.capture_store.save_verified_text(row['capture_id'], text,
                        expected_updated_at=datetime.fromisoformat(expected_updated_at))
                except CaptureConflictError:
                    raise LibraryConflictError('캡처가 변경되었습니다. 입력을 유지하고 현재 값을 확인해 주세요.') from None
                saved = self._page(row, capture)
            else:
                if row['updated_at'] != expected_updated_at:
                    raise LibraryConflictError('페이지가 변경되었습니다. 입력을 유지하고 현재 값을 확인해 주세요.')
                stamp = _stamp(row['updated_at'])
                db.execute('UPDATE pages SET text=?,edited=1,updated_at=? WHERE id=?', (text, stamp, page_id))
                self._touch(db, row['document_id'])
                saved = self._page(db.execute('SELECT * FROM pages WHERE id=?', (page_id,)).fetchone())
        return saved

    def update_page_ocr(self, page_id, text, *, expected_updated_at=None):
        """Save file OCR separately; edited text (including empty) always wins."""
        text = _text(text)
        with self._db(write=True) as db:
            row = db.execute('SELECT * FROM pages WHERE id=?', (page_id,)).fetchone()
            if row is None:
                raise LibraryReadOnlyError('기존 페이지는 읽기 전용입니다.')
            if row['trashed_at']:
                raise LibraryReadOnlyError('삭제한 페이지를 먼저 복원해 주세요.')
            if row['capture_id']:
                raise ValueError('이미지 OCR 원문은 캡처 보관함에서 저장해 주세요.')
            self._managed(db, row['document_id'])
            if expected_updated_at is not None and row['updated_at'] != expected_updated_at:
                raise LibraryConflictError('페이지가 변경되어 늦게 도착한 OCR을 적용하지 않았습니다.')
            db.execute('UPDATE pages SET ocr_text=?,text=?,updated_at=? WHERE id=?',
                       (text, row['text'] if row['edited'] else text, _stamp(row['updated_at']), page_id))
            self._touch(db, row['document_id'])
            saved = self._page(db.execute('SELECT * FROM pages WHERE id=?', (page_id,)).fetchone())
        return saved

    def _capture_document(self, capture):
        return {'id': 'capture:' + capture.id, 'title': capture.ocr_preview[:60] or capture.label,
                'page_count': 1, 'created_at': _iso(capture.created_at), 'updated_at': _iso(capture.updated_at),
                'legacy': True, 'readonly': True, 'trashed': capture.trashed_at is not None,
                'warning': '', 'recovery_required': False,
                'thumbnail_path': str(capture.path)}

    @staticmethod
    def _unreadable_capture_page(entry, document_id=None):
        # Never resolve or open the untrusted path again, and never convert a
        # deliberately empty teacher revision back into raw OCR.
        verified = bool(entry.get('verified_at'))
        ocr_text = entry.get('ocr_text', '')
        verified_text = entry.get('verified_text', '')
        ocr_text = ocr_text if isinstance(ocr_text, str) else ''
        verified_text = verified_text if isinstance(verified_text, str) else ''
        return {'id': 'capture:' + entry['id'], 'document_id': document_id or 'capture:' + entry['id'],
                'capture_id': entry['id'], 'path': None,
                'text': verified_text if verified else ocr_text, 'ocr_text': ocr_text,
                'verified_text': verified_text, 'updated_at': _safe_iso(entry.get('updated_at')),
                'source_name': entry.get('source', ''), 'source_path': None,
                'legacy': True, 'readonly': True, 'missing': False,
                'ocr_status': 'unavailable', 'review_status': 'unavailable', 'edited': verified,
                'warning': _CAPTURE_WARNING, 'recovery_required': True}

    def _capture_entry_page(self, entry, document_id=None):
        return (self._legacy_capture_page(entry['record'], document_id=document_id)
                if entry['record'] is not None else self._unreadable_capture_page(entry, document_id))

    def _capture_entry_document(self, entry):
        if entry['record'] is not None:
            return self._capture_document(entry['record'])
        page = self._unreadable_capture_page(entry)
        title = next((line.strip()[:60] for line in page['text'].splitlines() if line.strip()), '복구가 필요한 캡처')
        return {'id': page['document_id'], 'title': title, 'page_count': 1,
                'created_at': _safe_iso(entry.get('created_at')), 'updated_at': page['updated_at'],
                'legacy': True, 'readonly': True, 'trashed': bool(entry.get('trashed_at')),
                'thumbnail_path': None, 'warning': _CAPTURE_WARNING, 'recovery_required': True}

    def _task_pages(self, task):
        captures = self.capture_store.safe_search(task_id=task.id)
        pages = [{'id': 'legacy-text:' + task.id, 'document_id': task.id, 'capture_id': None,
                  'path': None, 'text': task.source_text, 'ocr_text': '', 'updated_at': _iso(task.updated_at),
                  'source_name': task.source_name, 'source_path': task.source_path, 'legacy': True, 'readonly': True,
                  'warning': '', 'recovery_required': False}]
        for capture in captures:
            page = self._capture_entry_page(capture, document_id=task.id)
            page['id'] = 'legacy-capture:' + task.id + ':' + capture['id']
            page['text'] = ''  # The task's integrated teacher text is authoritative.
            pages.append(page)
        if not captures and task.capture_path:
            pages.append({'id': 'legacy-image:' + task.id, 'document_id': task.id, 'capture_id': None,
                          'path': task.capture_path, 'text': '', 'ocr_text': '', 'updated_at': _iso(task.updated_at),
                          'source_name': task.source_name, 'source_path': task.source_path, 'legacy': True, 'readonly': True,
                          'warning': '', 'recovery_required': False})
        return pages

    def _task_document(self, task):
        pages = self._task_pages(task)
        needs_recovery = any(page['recovery_required'] for page in pages)
        return {'id': task.id, 'title': task.title, 'page_count': len(pages),
                'created_at': _iso(task.created_at), 'updated_at': _iso(task.updated_at),
                'legacy': True, 'readonly': True, 'trashed': False,
                'warning': _DOCUMENT_WARNING if needs_recovery else '', 'recovery_required': needs_recovery,
                'thumbnail_path': next((page['path'] for page in pages if page['path']), None)}

    @staticmethod
    def _legacy_capture_page(capture, document_id=None):
        return {'id': 'capture:' + capture.id, 'document_id': document_id or 'capture:' + capture.id,
                'capture_id': capture.id, 'path': str(capture.path), 'text': capture.effective_text,
                'ocr_text': capture.ocr_text, 'updated_at': _iso(capture.updated_at),
                'source_name': capture.source, 'source_path': str(capture.path), 'legacy': True, 'readonly': True,
                'ocr_status': capture.ocr_status, 'review_status': capture.review_status,
                'warning': '', 'recovery_required': False}

    def get_document(self, document_id):
        with self._db() as db:
            row = db.execute('SELECT * FROM documents WHERE id=?', (document_id,)).fetchone()
            if row:
                return self._document(db, row)
        if document_id.startswith('capture:'):
            capture = self.capture_store.safe_get(document_id[len('capture:'):])
            return self._capture_entry_document(capture) if capture else None
        task = self.task_store.get(document_id)
        return self._task_document(task) if task else None

    def pages(self, document_id, *, trashed=False):
        with self._db() as db:
            if db.execute('SELECT 1 FROM documents WHERE id=?', (document_id,)).fetchone():
                return self._pages(db, document_id, trashed=trashed)
        if trashed:
            return []
        if document_id.startswith('capture:'):
            capture = self.capture_store.safe_get(document_id[len('capture:'):])
            if capture:
                return [self._capture_entry_page(capture)]
        else:
            task = self.task_store.get(document_id)
            if task:
                return self._task_pages(task)
        raise KeyError('문서를 찾을 수 없습니다.')

    def document_text(self, document_id):
        pages = self.pages(document_id)
        return '\n\n'.join(page['text'] for page in pages if page['text'] != '')

    def list_documents(self, query='', trashed=False):
        needle = _text(query).strip().casefold()
        with self._db() as db:
            all_managed = list(db.execute('SELECT * FROM documents'))
            managed_ids = {row['id'] for row in all_managed}
            assigned_captures = {row[0] for row in db.execute('SELECT capture_id FROM pages WHERE capture_id IS NOT NULL')}
            result = [self._document(db, row) for row in all_managed if bool(row['trashed_at']) == bool(trashed)]
        if not trashed:
            result.extend(self._task_document(task) for task in self.task_store.search() if task.id not in managed_ids)
        result.extend(self._capture_entry_document(capture) for capture in self.capture_store.safe_search(trashed=trashed)
                      if capture['id'] not in assigned_captures)
        if needle:
            matched = []
            for document in result:
                pages = self.pages(document['id'])
                haystack = '\n'.join([document['title'], document.get('memo', ''), *document.get('labels', []), *(page['text'] for page in pages),
                    *(page['ocr_text'] for page in pages), *(page['source_name'] for page in pages),
                    *(output['text'] for output in self.outputs(document['id']))]).casefold()
                if needle in haystack:
                    matched.append(document)
            result = matched
        return sorted(result, key=lambda item: (item['updated_at'], item['id']), reverse=True)

    def update_details(self, document_id, labels, memo, expected_updated_at=None):
        labels = _labels(labels)
        memo = _text(memo).strip()
        if len(memo) > 2000:
            raise ValueError('메모는 2000자 이내로 입력해 주세요.')
        with self._db(write=True) as db:
            self._managed(db, document_id, expected_updated_at)
            db.execute('UPDATE documents SET labels_json=?,memo=? WHERE id=?',
                       (json.dumps(labels, ensure_ascii=False), memo, document_id))
            self._touch(db, document_id)
            return self._document(db, db.execute('SELECT * FROM documents WHERE id=?', (document_id,)).fetchone())

    def rename(self, document_id, title, expected_updated_at=None):
        title = _title(title)
        with self._db(write=True) as db:
            self._managed(db, document_id, expected_updated_at)
            db.execute('UPDATE documents SET title=? WHERE id=?', (title, document_id))
            self._touch(db, document_id)
            saved = self._document(db, db.execute('SELECT * FROM documents WHERE id=?', (document_id,)).fetchone())
        return saved

    def reorder_pages(self, document_id, page_ids, expected_updated_at=None):
        page_ids = list(page_ids)
        with self._db(write=True) as db:
            self._managed(db, document_id, expected_updated_at)
            existing = {row[0] for row in db.execute('SELECT id FROM pages WHERE document_id=? AND trashed_at IS NULL', (document_id,))}
            if len(page_ids) != len(existing) or set(page_ids) != existing:
                raise ValueError('이 문서의 모든 페이지를 중복 없이 지정해 주세요.')
            positions = [row[0] for row in db.execute(
                'SELECT position FROM pages WHERE document_id=? AND trashed_at IS NULL ORDER BY position,id', (document_id,))]
            db.executemany('UPDATE pages SET position=? WHERE id=?', list(zip(positions, page_ids)))
            self._touch(db, document_id)
            saved = self._pages(db, document_id)
        return saved

    def set_page_trash(self, page_id, trashed, *, expected_updated_at):
        """Hide only this document's page; retain its image, text and order."""
        with self._db(write=True) as db:
            row = db.execute('SELECT * FROM pages WHERE id=?', (page_id,)).fetchone()
            if row is None:
                raise LibraryReadOnlyError('기존 기록은 새 문서로 가져온 뒤 편집해 주세요.')
            key = 'auto_page_trash:' + row['document_id']
            automatic = db.execute('SELECT 1 FROM settings WHERE key=?', (key,)).fetchone()
            self._managed(db, row['document_id'], expected_updated_at,
                          allow_trashed=bool(automatic) and not trashed)
            db.execute('UPDATE pages SET trashed_at=? WHERE id=?', (_stamp() if trashed else None, page_id))
            if not trashed and automatic:
                db.execute('UPDATE documents SET trashed_at=NULL WHERE id=?', (row['document_id'],))
                db.execute('DELETE FROM settings WHERE key=?', (key,))
            if trashed and not db.execute('SELECT 1 FROM pages WHERE document_id=? AND trashed_at IS NULL', (row['document_id'],)).fetchone():
                self._auto_trash(db, row['document_id'], page_id)
            self._touch(db, row['document_id'])
        return row['document_id']

    @staticmethod
    def _match_reasons(target, candidate):
        common = sorted(set(target.get('labels', [])) & set(candidate.get('labels', [])))
        reasons = ['라벨: ' + ', '.join(common)] if common else []
        if target.get('memo', '').strip() and target['memo'].strip() == candidate.get('memo', '').strip():
            reasons.append('메모 같음')
        return reasons

    def capture_pages(self):
        """Active captures with document metadata for the visual organizer."""
        result = []
        for doc in self.list_documents():
            for page in self.pages(doc['id']):
                if page.get('capture_id'):
                    result.append(dict(page, document_title=doc['title'], labels=doc.get('labels', []),
                                       memo=doc.get('memo', ''), document_version=doc['updated_at'],
                                       document_readonly=doc.get('readonly', False)))
        return result

    def _selected_active_pages(self, db, page_ids, expected_versions):
        ids = list(page_ids)
        if not ids or len(set(ids)) != len(ids):
            raise ValueError('캡처를 중복 없이 선택하세요.')
        rows = []
        for identity in ids:
            row = db.execute('SELECT * FROM pages WHERE id=? AND trashed_at IS NULL', (identity,)).fetchone()
            if row is None:
                raise LibraryReadOnlyError('삭제되었거나 기존 읽기 전용인 페이지입니다. 목록을 새로고침하세요.')
            rows.append(row)
        for identity in {row['document_id'] for row in rows}:
            if not expected_versions.get(identity):
                raise LibraryConflictError('목록을 새로고침하세요.')
            self._managed(db, identity, expected_versions[identity])
        return rows

    def rename_page(self, page_id, name, *, expected_updated_at):
        name = _title(name)
        if len(name) > 120 or '\n' in name or '\r' in name:
            raise ValueError('캡처 이름은 줄바꿈 없이 120자 이내로 입력하세요.')
        with self._db(write=True) as db:
            row = db.execute('SELECT document_id FROM pages WHERE id=?', (page_id,)).fetchone()
            if row is None:
                raise LibraryReadOnlyError('기존 기록은 새 문서로 가져온 뒤 편집하세요.')
            self._selected_active_pages(db, [page_id], {row[0]: expected_updated_at})
            db.execute('UPDATE pages SET source_name=? WHERE id=?', (name, page_id))
            self._touch(db, row[0])

    def trash_pages(self, page_ids, *, expected_versions):
        """All selected pages succeed or roll back together, across documents."""
        with self._db(write=True) as db:
            rows = self._selected_active_pages(db, page_ids, expected_versions)
            stamp = _stamp()
            for row in rows:
                db.execute('UPDATE pages SET trashed_at=? WHERE id=?', (stamp, row['id']))
            for identity in {row['document_id'] for row in rows}:
                if not db.execute('SELECT 1 FROM pages WHERE document_id=? AND trashed_at IS NULL', (identity,)).fetchone():
                    deleted = [row['id'] for row in rows if row['document_id'] == identity]
                    self._auto_trash(db, identity, deleted)
                self._touch(db, identity)

    def move_pages(self, page_ids, target_id, *, expected_versions):
        """Explicitly organize selected pages; source metadata stays with its document."""
        with self._db(write=True) as db:
            rows = self._selected_active_pages(db, page_ids, expected_versions)
            if not expected_versions.get(target_id):
                raise LibraryConflictError('이동할 문서 목록을 새로고침하세요.')
            self._managed(db, target_id, expected_versions[target_id])
            sources = {row['document_id'] for row in rows}
            if target_id in sources:
                raise ValueError('이미 대상 문서에 있는 캡처는 선택에서 제외하세요.')
            captures = {r[0] for r in db.execute('SELECT capture_id FROM pages WHERE document_id=? AND capture_id IS NOT NULL', (target_id,))}
            for row in rows:
                if row['capture_id']:
                    if row['capture_id'] in captures:
                        raise ValueError('동일한 캡처가 중복됩니다. 선택 항목과 대상 문서를 확인하세요.')
                    captures.add(row['capture_id'])
            self._prepare_page_move(sources, target_id, rows)
            for identity in sources | {target_id}:
                self._managed(db, identity, expected_versions[identity])
            position = db.execute('SELECT COALESCE(MAX(position),-1)+1 FROM pages WHERE document_id=?', (target_id,)).fetchone()[0]
            for offset, row in enumerate(rows):
                db.execute('UPDATE pages SET document_id=?,position=? WHERE id=?', (target_id, position + offset, row['id']))
            for identity in sources:
                if not db.execute('SELECT 1 FROM pages WHERE document_id=? AND trashed_at IS NULL', (identity,)).fetchone():
                    db.execute('UPDATE documents SET trashed_at=? WHERE id=?', (_stamp(), identity))
                self._touch(db, identity)
            self._touch(db, target_id)

    def related_documents(self, document_id):
        target = self.get_document(document_id)
        if not target or target.get('readonly') or target.get('trashed'):
            return []
        return [dict(doc, match_reasons=reasons) for doc in self.list_documents()
                if doc['id'] != document_id and not doc.get('readonly') and doc['page_count']
                and (reasons := self._match_reasons(target, doc))]

    def _prepare_page_move(self, source_ids, target_id, pages):
        # Carry restrictions BEFORE moving any content. Failure can only leave
        # stricter target policy/extra compatibility links, never exposed content.
        from services.desk_transfer import DeskTransfer
        from services.transfer_policy import TransferPolicyStore
        captures = list(dict.fromkeys(page['capture_id'] for page in pages if page['capture_id']))
        DeskTransfer(TransferPolicyStore(self.app_data_dir)).inherit_document_policy(source_ids, captures, target_id)
        if captures:
            self.capture_store.link_to_task(captures, target_id)

    def merge_documents(self, target_id, source_ids, *, expected_versions):
        source_ids = list(source_ids)
        if not source_ids or target_id in source_ids or len(set(source_ids)) != len(source_ids):
            raise ValueError('합칠 문서를 중복 없이 선택해 주세요.')
        if set(expected_versions) != {target_id, *source_ids} or not all(expected_versions.values()):
            raise LibraryConflictError('문서 목록을 새로 열어 저장 버전을 확인해 주세요.')
        with self._db(write=True) as db:
            target_row = self._managed(db, target_id, expected_versions[target_id])
            target = self._document(db, target_row)
            pages = []
            for source_id in source_ids:
                row = self._managed(db, source_id, expected_versions[source_id])
                doc = self._document(db, row)
                if not self._match_reasons(target, doc):
                    raise ValueError('공통 라벨 또는 같은 메모가 있는 문서만 합칠 수 있습니다.')
                incoming = list(db.execute('SELECT * FROM pages WHERE document_id=? AND trashed_at IS NULL ORDER BY position,id', (source_id,)))
                if not incoming:
                    raise ValueError('합칠 페이지가 없는 문서입니다.')
                pages.extend(incoming)
            captures = {row[0] for row in db.execute('SELECT capture_id FROM pages WHERE document_id=? AND capture_id IS NOT NULL', (target_id,))}
            for page in pages:
                if page['capture_id']:
                    if page['capture_id'] in captures:
                        raise ValueError('동일한 캡처가 중복됩니다. 중복 페이지를 먼저 확인해 주세요.')
                    captures.add(page['capture_id'])
            self._prepare_page_move(source_ids, target_id, pages)
            for identity, version in expected_versions.items():
                self._managed(db, identity, version)
            position = db.execute('SELECT COALESCE(MAX(position),-1)+1 FROM pages WHERE document_id=?', (target_id,)).fetchone()[0]
            for offset, page in enumerate(pages):
                db.execute('UPDATE pages SET document_id=?,position=? WHERE id=?', (target_id, position + offset, page['id']))
            self._touch(db, target_id)
            for source_id in source_ids:
                # Preserve source metadata, deleted pages and AI result history.
                db.execute('UPDATE documents SET trashed_at=? WHERE id=?', (_stamp(), source_id))
                self._touch(db, source_id)
            return self._document(db, db.execute('SELECT * FROM documents WHERE id=?', (target_id,)).fetchone())

    def split_pages(self, document_id, page_ids, title, *, expected_updated_at):
        title = _title(title)
        if not expected_updated_at:
            raise LibraryConflictError('문서를 다시 열어 저장 버전을 확인해 주세요.')
        selected = list(page_ids)
        if not selected or len(selected) != len(set(selected)):
            raise ValueError('분리할 페이지를 중복 없이 선택해 주세요.')
        with self._db(write=True) as db:
            source = self._managed(db, document_id, expected_updated_at)
            active = list(db.execute('SELECT * FROM pages WHERE document_id=? AND trashed_at IS NULL ORDER BY position,id', (document_id,)))
            if not set(selected) < {row['id'] for row in active}:
                raise ValueError('현재 문서에 한 쪽 이상 남도록 분리할 페이지를 선택해 주세요.')
            pages = [row for row in active if row['id'] in set(selected)]
            target = self._create_document(db, title)
            self._prepare_page_move([document_id], target['id'], pages)
            self._managed(db, document_id, expected_updated_at)
            db.execute('UPDATE documents SET labels_json=?,memo=? WHERE id=?', (source['labels_json'], source['memo'], target['id']))
            for position, page in enumerate(pages):
                db.execute('UPDATE pages SET document_id=?,position=? WHERE id=?', (target['id'], position, page['id']))
            self._touch(db, document_id)
            self._touch(db, target['id'])
            return self._document(db, db.execute('SELECT * FROM documents WHERE id=?', (target['id'],)).fetchone())

    def deleted_pages(self):
        with self._db() as db:
            return [dict(self._page(row), document_title=row['document_title'],
                         document_trashed=bool(row['document_trashed']), trashed=True)
                    for row in db.execute('SELECT pages.*, documents.title AS document_title, '
                        'documents.trashed_at AS document_trashed FROM pages JOIN documents '
                        'ON pages.document_id=documents.id WHERE pages.trashed_at IS NOT NULL '
                        'ORDER BY pages.trashed_at DESC, pages.id')]

    def label_counts(self):
        counts = {}
        with self._db() as db:
            for row in db.execute('SELECT labels_json FROM documents'):
                for label in json.loads(row[0]):
                    counts[label] = counts.get(label, 0) + 1
        return dict(sorted(counts.items(), key=lambda item: item[0].casefold()))

    def rename_label(self, old, new):
        """Rename in all managed documents atomically, merging duplicate labels."""
        values = _labels([new])
        if len(values) != 1:
            raise ValueError('새 라벨 이름을 입력해 주세요.')
        new = values[0]
        count = 0
        with self._db(write=True) as db:
            for row in list(db.execute('SELECT id,labels_json FROM documents')):
                labels = json.loads(row['labels_json'])
                if old not in labels:
                    continue
                replaced = _labels([new if label == old or label.casefold() == new.casefold() else label for label in labels])
                db.execute('UPDATE documents SET labels_json=? WHERE id=?',
                           (json.dumps(replaced, ensure_ascii=False), row['id']))
                self._touch(db, row['id'])
                count += 1
        return count

    def _set_trash(self, document_id, trashed, expected_updated_at):
        with self._db(write=True) as db:
            self._managed(db, document_id, expected_updated_at, allow_trashed=True)
            key = 'auto_page_trash:' + document_id
            marker = db.execute('SELECT value_json FROM settings WHERE key=?', (key,)).fetchone()
            if not trashed and marker:
                deleted = json.loads(marker[0])
                for page_id in deleted if isinstance(deleted, list) else [deleted]:
                    db.execute('UPDATE pages SET trashed_at=NULL WHERE id=? AND document_id=?',
                               (page_id, document_id))
            db.execute('DELETE FROM settings WHERE key=?', (key,))
            db.execute('UPDATE documents SET trashed_at=? WHERE id=?', (_stamp() if trashed else None, document_id))
            self._touch(db, document_id)
            saved = self._document(db, db.execute('SELECT * FROM documents WHERE id=?', (document_id,)).fetchone())
        return saved

    def trash(self, document_id, expected_updated_at=None):
        return self._set_trash(document_id, True, expected_updated_at)

    def restore(self, document_id, expected_updated_at=None):
        return self._set_trash(document_id, False, expected_updated_at)

    def trash_snapshot(self):
        """Capture identities and versions before an irreversible confirmation."""
        documents = {d['id']: d['updated_at'] for d in self.list_documents(trashed=True)}
        pages = {p['id']: self.get_document(p['document_id'])['updated_at']
                 for p in self.deleted_pages() if p['document_id'] not in documents}
        return {'documents': documents, 'pages': pages}

    def purge_trash(self, snapshot):
        from services.library_trash import purge_trash
        return purge_trash(self, snapshot)

    @staticmethod
    def _output(row):
        return {**dict(row), 'legacy': False, 'readonly': False}

    def save_output(self, document_id, mode, text, source_fingerprint):
        mode, text, source_fingerprint = _title(mode), _text(text), _text(source_fingerprint)
        output_id = uuid4().hex
        with self._db(write=True) as db:
            document = self._managed(db, document_id)
            stamp = _stamp(self._document(db, document)['updated_at'])
            db.execute('INSERT INTO outputs VALUES (?,?,?,?,?,?,?)', (output_id, document_id, mode, text, source_fingerprint, stamp, stamp))
            self._touch(db, document_id)
            saved = self._output(db.execute('SELECT * FROM outputs WHERE id=?', (output_id,)).fetchone())
        return saved

    def update_output(self, output_id, text, expected_updated_at):
        text = _text(text)
        with self._db(write=True) as db:
            row = db.execute('SELECT * FROM outputs WHERE id=?', (output_id,)).fetchone()
            if row is None:
                raise LibraryReadOnlyError('기존 결과는 읽기 전용입니다. 새 결과로 저장해 주세요.')
            self._managed(db, row['document_id'])
            if row['updated_at'] != expected_updated_at:
                raise LibraryConflictError('결과가 변경되었습니다. 입력을 유지하고 현재 값을 확인해 주세요.')
            db.execute('UPDATE outputs SET text=?,updated_at=? WHERE id=?', (text, _stamp(row['updated_at']), output_id))
            self._touch(db, row['document_id'])
            saved = self._output(db.execute('SELECT * FROM outputs WHERE id=?', (output_id,)).fetchone())
        return saved

    def outputs(self, document_id):
        with self._db() as db:
            if db.execute('SELECT 1 FROM documents WHERE id=?', (document_id,)).fetchone():
                return [self._output(row) for row in db.execute('SELECT * FROM outputs WHERE document_id=? ORDER BY created_at DESC,id DESC', (document_id,))]
        if document_id.startswith('capture:'):
            return []
        task = self.task_store.get(document_id)
        if task and task.analysis_text:
            return [{'id': 'legacy-output:' + task.id, 'document_id': task.id, 'mode': task.output_mode,
                     'text': task.analysis_text, 'source_fingerprint': '', 'created_at': _iso(task.created_at),
                     'updated_at': _iso(task.updated_at), 'legacy': True, 'readonly': True}]
        return []

    def get_setting(self, key, default=None):
        with self._db() as db:
            row = db.execute('SELECT value_json FROM settings WHERE key=?', (_text(key),)).fetchone()
            return json.loads(row[0]) if row else default

    def remember_initial_ocr(self, page_id, text):
        """Keep the first supplied OCR verbatim, including an intentional empty."""
        page_id, text = _text(page_id), _text(text)
        key = 'initial_ocr:' + page_id
        with self._db(write=True) as db:
            page = db.execute('SELECT document_id FROM pages WHERE id=?', (page_id,)).fetchone()
            if page is None:
                raise LibraryReadOnlyError('기존 페이지는 읽기 전용입니다. 새 문서로 담은 뒤 기록해 주세요.')
            self._managed(db, page['document_id'])
            db.execute('INSERT OR IGNORE INTO settings VALUES (?,?)', (key, json.dumps(text, ensure_ascii=False)))
            saved = json.loads(db.execute('SELECT value_json FROM settings WHERE key=?', (key,)).fetchone()[0])
            if not isinstance(saved, str):
                raise ValueError('최초 인식 기록을 확인할 수 없습니다. 기존 기록은 유지했습니다.')
        return saved

    def initial_ocr(self, page_id):
        value = self.get_setting('initial_ocr:' + _text(page_id))
        if value is not None and not isinstance(value, str):
            raise ValueError('최초 인식 기록을 확인할 수 없습니다. 기존 기록은 유지했습니다.')
        return value

    def set_setting(self, key, value):
        key = _title(key)
        serialized = json.dumps(value, ensure_ascii=False)
        with self._db(write=True) as db:
            db.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json', (key, serialized))
        return value
