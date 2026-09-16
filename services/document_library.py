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


SCHEMA_VERSION = 3
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
            document_columns = {row[1] for row in db.execute('PRAGMA table_info(documents)')}
            if 'labels_json' not in document_columns:
                db.execute("ALTER TABLE documents ADD COLUMN labels_json TEXT NOT NULL DEFAULT '[]'")
            if 'memo' not in document_columns:
                db.execute("ALTER TABLE documents ADD COLUMN memo TEXT NOT NULL DEFAULT ''")
            self._validate_schema(db)
            db.execute('PRAGMA user_version=3')

    @staticmethod
    def _validate_schema(db):
        expected = {
            'documents': {'id', 'title', 'created_at', 'updated_at', 'trashed_at', 'labels_json', 'memo'},
            'pages': {'id', 'document_id', 'capture_id', 'text', 'source_name', 'source_path', 'position', 'updated_at', 'ocr_text', 'edited'},
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

    def _pages(self, db, document_id):
        return [self._page(row) for row in db.execute('SELECT * FROM pages WHERE document_id=? ORDER BY position,id', (document_id,))]

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

    def save_page_text(self, page_id, text, expected_updated_at):
        text = _text(text)
        with self._db(write=True) as db:
            row = db.execute('SELECT * FROM pages WHERE id=?', (page_id,)).fetchone()
            if row is None:
                raise LibraryReadOnlyError('기존 페이지는 읽기 전용입니다. 새 문서로 담은 뒤 편집해 주세요.')
            self._managed(db, row['document_id'])
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

    def pages(self, document_id):
        with self._db() as db:
            if db.execute('SELECT 1 FROM documents WHERE id=?', (document_id,)).fetchone():
                return self._pages(db, document_id)
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
            existing = {row[0] for row in db.execute('SELECT id FROM pages WHERE document_id=?', (document_id,))}
            if len(page_ids) != len(existing) or set(page_ids) != existing:
                raise ValueError('이 문서의 모든 페이지를 중복 없이 지정해 주세요.')
            db.executemany('UPDATE pages SET position=? WHERE id=?', [(index, page_id) for index, page_id in enumerate(page_ids)])
            self._touch(db, document_id)
            saved = self._pages(db, document_id)
        return saved

    def _set_trash(self, document_id, trashed, expected_updated_at):
        with self._db(write=True) as db:
            self._managed(db, document_id, expected_updated_at, allow_trashed=True)
            db.execute('UPDATE documents SET trashed_at=? WHERE id=?', (_stamp() if trashed else None, document_id))
            self._touch(db, document_id)
            saved = self._document(db, db.execute('SELECT * FROM documents WHERE id=?', (document_id,)).fetchone())
        return saved

    def trash(self, document_id, expected_updated_at=None):
        return self._set_trash(document_id, True, expected_updated_at)

    def restore(self, document_id, expected_updated_at=None):
        return self._set_trash(document_id, False, expected_updated_at)

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
