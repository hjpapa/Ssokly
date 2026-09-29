"""Permanent trash deletion with shared-reference checks and staged owned files."""
from contextlib import closing
import sqlite3
from uuid import uuid4

from services.document_library import LibraryConflictError, LibraryReadOnlyError
from services.diagnostics import log_failure


def purge_trash(library, snapshot):
    documents = dict(snapshot.get('documents', {}))
    selected_pages = dict(snapshot.get('pages', {}))
    staged_files = []
    removed_pages = set()
    capture_ids = set()
    managed_ids = set()
    try:
        with closing(sqlite3.connect(library.path, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('ATTACH DATABASE ? AS captures', (str(library.capture_store.db_path),))
            db.execute('ATTACH DATABASE ? AS tasks_store', (str(library.task_store.db_path),))
            # Multi-file atomic commits require rollback journals, as used by these stores.
            for schema in ('main', 'captures', 'tasks_store'):
                if db.execute('PRAGMA ' + schema + '.journal_mode').fetchone()[0].lower() not in ('delete', 'truncate', 'persist'):
                    raise ValueError('저장소 저널 설정을 확인한 후 다시 시도해 주세요.')
            with db:
                db.execute('BEGIN IMMEDIATE')
                all_managed = {r[0] for r in db.execute('SELECT id FROM documents')}
                for identity, version in documents.items():
                    if not version:
                        raise LibraryConflictError('휴지통을 새로고침하세요.')
                    if identity.startswith('capture:'):
                        capture_id = identity.split(':', 1)[1]
                        doc = library.get_document(identity)
                        if not doc or not doc['trashed'] or doc['updated_at'] != version:
                            raise LibraryConflictError('캡처 상태가 바뀌었습니다. 휴지통을 새로고침하세요.')
                        if db.execute('SELECT 1 FROM pages WHERE capture_id=?', (capture_id,)).fetchone() or db.execute('SELECT 1 FROM captures.capture_links WHERE capture_id=?', (capture_id,)).fetchone():
                            raise LibraryReadOnlyError('다른 문서에서 사용하는 캡처입니다.')
                        capture_ids.add(capture_id)
                        continue
                    row = library._managed(db, identity, version, allow_trashed=True)
                    if not row['trashed_at']:
                        raise LibraryReadOnlyError('휴지통의 문서만 영구 삭제할 수 있습니다.')
                    managed_ids.add(identity)
                    for page in db.execute('SELECT id,capture_id FROM pages WHERE document_id=?', (identity,)):
                        removed_pages.add(page['id'])
                        if page['capture_id']:
                            capture_ids.add(page['capture_id'])
                touched = set()
                for identity, version in selected_pages.items():
                    row = db.execute('SELECT * FROM pages WHERE id=?', (identity,)).fetchone()
                    if not row or not row['trashed_at'] or not version:
                        raise LibraryReadOnlyError('휴지통의 페이지만 영구 삭제할 수 있습니다.')
                    library._managed(db, row['document_id'], version, allow_trashed=True)
                    removed_pages.add(identity)
                    touched.add(row['document_id'])
                    if row['capture_id']:
                        capture_ids.add(row['capture_id'])
                for identity in removed_pages:
                    db.execute('DELETE FROM settings WHERE key=?', ('initial_ocr:' + identity,))
                    db.execute('DELETE FROM pages WHERE id=?', (identity,))
                for identity in managed_ids:
                    capture_ids.update(r[0] for r in db.execute('SELECT capture_id FROM captures.capture_links WHERE task_id=?', (identity,)))
                    db.execute('DELETE FROM captures.capture_links WHERE task_id=?', (identity,))
                    db.execute('DELETE FROM outputs WHERE document_id=?', (identity,))
                    db.execute('DELETE FROM settings WHERE key IN (?,?)', ('active_output:' + identity, 'auto_page_trash:' + identity))
                    db.execute('DELETE FROM documents WHERE id=?', (identity,))
                    db.execute('DELETE FROM tasks_store.tasks WHERE id=?', (identity,))
                for identity in touched - managed_ids:
                    library._touch(db, identity)
                for capture_id in capture_ids:
                    # Old move links are conservative compatibility references. Remove
                    # only links to managed documents with no remaining page reference.
                    for link in list(db.execute('SELECT task_id FROM captures.capture_links WHERE capture_id=?', (capture_id,))):
                        if link[0] in all_managed and not db.execute('SELECT 1 FROM pages WHERE document_id=? AND capture_id=?', (link[0], capture_id)).fetchone():
                            db.execute('DELETE FROM captures.capture_links WHERE capture_id=? AND task_id=?', (capture_id, link[0]))
                    if db.execute('SELECT 1 FROM pages WHERE capture_id=?', (capture_id,)).fetchone() or db.execute('SELECT 1 FROM captures.capture_links WHERE capture_id=?', (capture_id,)).fetchone():
                        continue
                    row = db.execute('SELECT storage_name FROM captures.capture_items WHERE id=?', (capture_id,)).fetchone()
                    if row:
                        original = library.capture_store._owned_path(row[0])
                        if original.exists():
                            staged = library.capture_store._owned_path('.' + original.name + '.' + uuid4().hex + '.deleting')
                            original.replace(staged)
                            staged_files.append((original, staged))
                        db.execute('DELETE FROM captures.capture_items WHERE id=?', (capture_id,))
    except Exception as error:
        log_failure('library_trash.purge_trash', error)
        library.capture_store._restore_staged_files(staged_files)
        raise
    pending_files = 0
    for original, staged in staged_files:
        try:
            staged.unlink(missing_ok=True)
        except OSError as error:
            log_failure('library_trash.purge_trash', error)
            pending_files += 1
    return {'documents': len(documents), 'pages': len(removed_pages), 'pending_files': pending_files}
