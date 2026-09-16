"""Capture-first persistence using only synthetic images and temporary stores."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from services.document_library import DocumentLibrary, LibraryConflictError, LibraryReadOnlyError


class DocumentLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.library = DocumentLibrary(self.temp.name)

    def capture(self, text='합성 OCR'):
        capture = self.library.capture_store.save(Image.new('RGB', (8, 6), 'white'))
        return self.library.capture_store.update_ocr(capture.id, text)

    def reopen(self):
        return DocumentLibrary(self.temp.name)

    def test_create_empty_document_reuses_task_identity_without_cards(self):
        document = self.library.create_document()
        self.assertFalse(document['legacy'])
        self.assertFalse(document['readonly'])
        self.assertEqual(document['page_count'], 0)
        self.assertEqual(self.library.pages(document['id']), [])
        self.assertEqual(self.library.task_store.get(document['id']).title, '새 문서')
        self.assertEqual(self.reopen().get_document(document['id']), document)
        self.assertFalse((Path(self.temp.name) / 'work_cards.sqlite3').exists())

    def test_raw_capture_stays_discoverable_without_auto_adoption(self):
        capture = self.capture()
        task_count = len(self.library.task_store.search())
        reopened = self.reopen()
        entry = reopened.list_documents()[0]
        self.assertEqual(entry['id'], 'capture:' + capture.id)
        self.assertTrue(entry['readonly'])
        self.assertEqual(reopened.pages(entry['id'])[0]['text'], '합성 OCR')
        self.assertEqual(len(reopened.task_store.search()), task_count)
        with self.assertRaises(LibraryReadOnlyError):
            reopened.save_page_text(entry['id'], '변경', reopened.pages(entry['id'])[0]['updated_at'])

    def test_adopt_capture_is_explicit_idempotent_and_links_existing_image(self):
        capture = self.capture()
        document = self.library.adopt_capture(capture.id)
        again = self.library.adopt_capture(capture.id)
        self.assertEqual(document['id'], again['id'])
        page = self.library.pages(document['id'])[0]
        self.assertEqual(page['capture_id'], capture.id)
        self.assertEqual(page['path'], str(capture.path))
        self.assertEqual(self.library.capture_store.captures_for_task(document['id'])[0].id, capture.id)
        self.assertEqual([item['id'] for item in self.library.list_documents()], [document['id']])
        self.assertEqual(len(list(self.library.task_store.captures_dir.iterdir())), 0)
        self.assertEqual(self.library.add_capture(document['id'], capture.id)['id'], page['id'])

    def test_capture_edit_preserves_raw_ocr_and_intentional_empty_across_restart(self):
        capture = self.capture('원 OCR')
        document = self.library.adopt_capture(capture.id)
        page = self.library.pages(document['id'])[0]
        saved = self.library.save_page_text(page['id'], '', page['updated_at'])
        self.assertEqual(saved['text'], '')
        self.assertEqual(saved['ocr_text'], '원 OCR')
        self.assertEqual(self.reopen().document_text(document['id']), '')
        self.library.capture_store.update_ocr(capture.id, '새 OCR')
        current = self.library.pages(document['id'])[0]
        self.assertEqual(current['text'], '')
        self.assertEqual(current['ocr_text'], '새 OCR')
        self.assertEqual(current['review_status'], 'ocr_updated')

    def test_capture_conflict_preserves_newer_teacher_text(self):
        capture = self.capture()
        document = self.library.adopt_capture(capture.id)
        page = self.library.pages(document['id'])[0]
        self.library.save_page_text(page['id'], '최신 수정', page['updated_at'])
        with self.assertRaises(LibraryConflictError):
            self.library.save_page_text(page['id'], '오래된 수정', page['updated_at'])
        self.assertEqual(self.reopen().document_text(document['id']), '최신 수정')

    def test_text_pages_reorder_and_conflict_preserve_current_order_and_text(self):
        document = self.library.create_document('페이지 문서')
        first = self.library.add_text_page(document['id'], '  첫째\n', '합성 파일', '/synthetic/source.txt')
        second = self.library.add_text_page(document['id'], '둘째')
        token = self.library.get_document(document['id'])['updated_at']
        self.library.reorder_pages(document['id'], [second['id'], first['id']], token)
        self.assertEqual(self.reopen().document_text(document['id']), '둘째\n\n  첫째\n')
        with self.assertRaises(LibraryConflictError):
            self.library.reorder_pages(document['id'], [first['id'], second['id']], token)
        with self.assertRaises(ValueError):
            self.library.reorder_pages(document['id'], [second['id'], second['id']])
        saved = self.library.save_page_text(first['id'], '교사 편집', first['updated_at'])
        with self.assertRaises(LibraryConflictError):
            self.library.save_page_text(first['id'], '낡은 편집', first['updated_at'])
        self.assertEqual(saved['source_path'], '/synthetic/source.txt')
        self.assertEqual(self.library.document_text(document['id']), '둘째\n\n교사 편집')

    def test_search_uses_live_capture_text_text_pages_and_outputs(self):
        capture = self.capture('초기')
        document = self.library.adopt_capture(capture.id)
        page = self.library.pages(document['id'])[0]
        self.library.save_page_text(page['id'], '수정검색어', page['updated_at'])
        self.library.add_text_page(document['id'], '텍스트검색어')
        self.library.save_output(document['id'], '요약', '결과검색어', 'synthetic-hash')
        for query in ('수정검색어', '텍스트검색어', '결과검색어'):
            self.assertEqual([item['id'] for item in self.library.list_documents(query)], [document['id']])
        self.assertEqual(self.library.list_documents('없는검색어'), [])

    def test_labels_and_memo_are_searchable_persisted_and_conflict_safe(self):
        document = self.library.create_document('자료')
        saved = self.library.update_details(document['id'], '행사, 제출, 행사', '교무실에서 확인', document['updated_at'])
        self.assertEqual(saved['labels'], ['행사', '제출'])
        self.assertEqual(saved['memo'], '교무실에서 확인')
        for query in ('행사', '제출', '교무실'):
            self.assertEqual([item['id'] for item in self.reopen().list_documents(query)], [document['id']])
        with self.assertRaises(LibraryConflictError):
            self.library.update_details(document['id'], ['변경'], '덮어쓰기', document['updated_at'])
        self.assertEqual(self.reopen().get_document(document['id']), saved)
        with self.assertRaises(ValueError):
            self.library.update_details(document['id'], ['쉼표,포함'], '')

    def test_v2_library_upgrade_adds_empty_details_and_keeps_document(self):
        document = self.library.create_document('기존 자료')
        with closing(sqlite3.connect(self.library.path)) as db, db:
            db.execute('ALTER TABLE documents DROP COLUMN labels_json')
            db.execute('ALTER TABLE documents DROP COLUMN memo')
            db.execute('PRAGMA user_version=2')
        reopened = self.reopen()
        self.assertEqual(reopened.get_document(document['id'])['labels'], [])
        self.assertEqual(reopened.get_document(document['id'])['memo'], '')
        self.assertEqual(len(list(Path(self.temp.name).glob('document_library.sqlite3.before-library-*.bak'))), 1)

    def test_legacy_task_keeps_integrated_text_results_and_image_references_read_only(self):
        capture = self.capture('페이지 OCR')
        task = self.library.task_store.create(title='기존 업무', source_text='교사가 편집한 전체 원문', analysis_text='기존 결과')
        self.library.capture_store.link_to_task([capture.id], task.id)
        before_task, before_capture = self.library.task_store.get(task.id), self.library.capture_store.get(capture.id)
        reopened = self.reopen()
        entries = {entry['id']: entry for entry in reopened.list_documents()}
        self.assertTrue(entries[task.id]['readonly'])
        self.assertIn('capture:' + capture.id, entries)  # Linked old captures do not disappear.
        self.assertEqual(reopened.document_text(task.id), '교사가 편집한 전체 원문')
        pages = reopened.pages(task.id)
        self.assertEqual(pages[1]['path'], str(capture.path))
        self.assertEqual(pages[1]['text'], '')
        self.assertEqual(reopened.outputs(task.id)[0]['text'], '기존 결과')
        for operation in (lambda: reopened.rename(task.id, '변경'), lambda: reopened.trash(task.id),
                          lambda: reopened.save_output(task.id, '요약', '변경', 'hash')):
            with self.assertRaises(LibraryReadOnlyError):
                operation()
        self.assertEqual(reopened.task_store.get(task.id), before_task)
        self.assertEqual(reopened.capture_store.get(capture.id), before_capture)

    def test_legacy_task_only_capture_copy_is_still_accessible(self):
        capture = self.capture()
        task = self.library.task_store.create(title='구버전 이미지', source_kind='capture', capture_path=capture.path)
        pages = self.library.pages(task.id)
        self.assertEqual(pages[1]['path'], task.capture_path)
        self.assertTrue(pages[1]['readonly'])

    def test_rename_cas_and_non_destructive_document_trash_restore(self):
        capture = self.capture()
        document = self.library.adopt_capture(capture.id)
        renamed = self.library.rename(document['id'], '새 제목', document['updated_at'])
        with self.assertRaises(LibraryConflictError):
            self.library.rename(document['id'], '오래된 제목', document['updated_at'])
        trashed = self.library.trash(document['id'], renamed['updated_at'])
        self.assertEqual(self.library.list_documents(), [])
        self.assertEqual(self.library.list_documents(trashed=True), [trashed])
        self.assertTrue(capture.path.exists())
        self.assertIsNone(self.library.capture_store.get(capture.id).trashed_at)
        self.assertIsNotNone(self.library.task_store.get(document['id']))
        with self.assertRaises(LibraryReadOnlyError):
            self.library.add_text_page(document['id'], '금지')
        restored = self.library.restore(document['id'], trashed['updated_at'])
        self.assertFalse(restored['trashed'])
        self.assertEqual(restored['title'], '새 제목')

    def test_output_history_edit_cas_and_settings_survive_restart(self):
        document = self.library.create_document()
        first = self.library.save_output(document['id'], '요약', '첫 결과', 'source-one')
        second = self.library.save_output(document['id'], '안내', '둘째 결과', 'source-two')
        edited = self.library.update_output(first['id'], '', first['updated_at'])
        with self.assertRaises(LibraryConflictError):
            self.library.update_output(first['id'], '낡은 변경', first['updated_at'])
        self.library.set_setting('automatic_ocr', False)
        self.library.set_setting('window', {'compact': True})
        reopened = self.reopen()
        self.assertEqual(reopened.outputs(document['id']), [second, edited])
        self.assertFalse(reopened.get_setting('automatic_ocr', True))
        self.assertEqual(reopened.get_setting('window'), {'compact': True})
        self.assertEqual(reopened.get_setting('missing', 'fallback'), 'fallback')

    def test_sidecar_write_failure_rolls_back_text_and_output(self):
        document = self.library.create_document()
        page = self.library.add_text_page(document['id'], '보존 원문')
        with closing(sqlite3.connect(self.library.path)) as db, db:
            db.execute("CREATE TRIGGER reject_touch BEFORE UPDATE ON documents BEGIN SELECT RAISE(ABORT,'synthetic write failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.library.save_page_text(page['id'], '실패 수정', page['updated_at'])
        with self.assertRaises(sqlite3.IntegrityError):
            self.library.save_output(document['id'], '요약', '실패 결과', 'hash')
        reopened = self.reopen()
        self.assertEqual(reopened.document_text(document['id']), '보존 원문')
        self.assertEqual(reopened.outputs(document['id']), [])

    def test_return_snapshot_failure_rolls_back_sidecar_changes(self):
        document = self.library.create_document()
        with patch.object(self.library, '_document', side_effect=ValueError('synthetic snapshot')):
            with self.assertRaises(ValueError):
                self.library.rename(document['id'], '저장하면 안 되는 제목')
        self.assertEqual(self.reopen().get_document(document['id'])['title'], document['title'])

    def test_capture_link_failure_keeps_original_discoverable_and_rolls_back_page(self):
        capture = self.capture()
        document = self.library.create_document()
        with patch.object(self.library.capture_store, 'link_to_task', side_effect=OSError('synthetic link failure')):
            with self.assertRaises(OSError):
                self.library.add_capture(document['id'], capture.id)
        self.assertEqual(self.reopen().pages(document['id']), [])
        self.assertIn('capture:' + capture.id, {item['id'] for item in self.library.list_documents()})
        self.assertTrue(capture.path.exists())

    def test_failure_after_link_keeps_raw_capture_discoverable_without_partial_page(self):
        capture = self.capture()
        document = self.library.create_document()
        with patch.object(self.library, '_touch', side_effect=OSError('synthetic metadata failure')):
            with self.assertRaises(OSError):
                self.library.add_capture(document['id'], capture.id)
        self.assertEqual(self.reopen().pages(document['id']), [])
        self.assertIn('capture:' + capture.id, {item['id'] for item in self.library.list_documents()})
        self.assertIn(document['id'], self.library.capture_store.get(capture.id).linked_task_ids)

    def test_failed_document_sidecar_insert_exposes_compatibility_task_as_legacy(self):
        with closing(sqlite3.connect(self.library.path)) as db, db:
            db.execute("CREATE TRIGGER reject_document BEFORE INSERT ON documents BEGIN SELECT RAISE(ABORT,'synthetic create failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.library.create_document('복구 가능한 기록')
        entries = self.reopen().list_documents()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['title'], '복구 가능한 기록')
        self.assertTrue(entries[0]['legacy'])

    def test_failed_task_creation_does_not_leave_managed_document(self):
        with patch.object(self.library.task_store, 'create', side_effect=OSError('synthetic task failure')):
            with self.assertRaises(OSError):
                self.library.create_document()
        self.assertEqual(self.library.list_documents(), [])

    def test_failed_adoption_rolls_back_sidecar_document_and_preserves_capture(self):
        capture = self.capture()
        with patch.object(self.library.capture_store, 'link_to_task', side_effect=OSError('synthetic adoption failure')):
            with self.assertRaises(OSError):
                self.library.adopt_capture(capture.id)
        with closing(sqlite3.connect(self.library.path)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM documents').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM pages').fetchone()[0], 0)
        self.assertIn('capture:' + capture.id, {item['id'] for item in self.reopen().list_documents()})

    def test_schema_upgrade_backups_sidecar_and_preserves_existing_values(self):
        document = self.library.create_document('보존 문서')
        self.library.add_text_page(document['id'], '보존 원문')
        with closing(sqlite3.connect(self.library.path)) as db, db:
            db.execute('PRAGMA user_version=0')
        reopened = self.reopen()
        self.assertEqual(reopened.document_text(document['id']), '보존 원문')
        backups = list(Path(self.temp.name).glob('document_library.sqlite3.before-library-*.bak'))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 0)

    def test_future_sidecar_schema_is_rejected_without_file_changes(self):
        with closing(sqlite3.connect(self.library.path)) as db, db:
            db.execute('PRAGMA user_version=99')
        before = self.library.path.read_bytes()
        with self.assertRaises(ValueError):
            self.reopen()
        self.assertEqual(self.library.path.read_bytes(), before)

    def test_file_ocr_placeholder_updates_raw_and_effective_text(self):
        document = self.library.create_document()
        page = self.library.add_text_page(document['id'], '', 'synthetic.pdf', '/synthetic/synthetic.pdf')
        saved = self.library.update_page_ocr(page['id'], '첨부 OCR', expected_updated_at=page['updated_at'])
        self.assertEqual(saved['text'], '첨부 OCR')
        self.assertEqual(saved['ocr_text'], '첨부 OCR')
        self.assertFalse(saved['edited'])
        self.assertEqual(self.reopen().document_text(document['id']), '첨부 OCR')

    def test_file_reread_preserves_explicit_empty_teacher_text_and_raw_is_accessible(self):
        document = self.library.create_document()
        page = self.library.add_text_page(document['id'], '최초 추출 원문')
        edited = self.library.save_page_text(page['id'], '', page['updated_at'])
        self.assertEqual(edited['ocr_text'], '최초 추출 원문')
        reread = self.library.update_page_ocr(page['id'], '새 OCR', expected_updated_at=edited['updated_at'])
        self.assertEqual(reread['text'], '')
        self.assertEqual(reread['ocr_text'], '새 OCR')
        self.assertTrue(reread['edited'])
        self.assertEqual(self.reopen().pages(document['id'])[0], reread)

    def test_late_file_ocr_conflict_does_not_overwrite_raw_or_teacher_edit(self):
        document = self.library.create_document()
        page = self.library.add_text_page(document['id'], '최초 원문')
        edited = self.library.save_page_text(page['id'], '교사 수정', page['updated_at'])
        with self.assertRaises(LibraryConflictError):
            self.library.update_page_ocr(page['id'], '늦은 OCR', expected_updated_at=page['updated_at'])
        self.assertEqual(self.reopen().pages(document['id'])[0], edited)

    def test_v1_text_values_are_preserved_as_edited_without_fabricating_raw_ocr(self):
        document = self.library.create_document()
        page = self.library.add_text_page(document['id'], '이전 버전 편집값')
        with closing(sqlite3.connect(self.library.path)) as db, db:
            db.execute('ALTER TABLE pages DROP COLUMN ocr_text')
            db.execute('ALTER TABLE pages DROP COLUMN edited')
            db.execute('PRAGMA user_version=1')
        reopened = self.reopen()
        migrated = reopened.pages(document['id'])[0]
        self.assertEqual(migrated['text'], '이전 버전 편집값')
        self.assertEqual(migrated['ocr_text'], '')
        self.assertTrue(migrated['edited'])
        reread = reopened.update_page_ocr(page['id'], '새 OCR')
        self.assertEqual(reread['text'], '이전 버전 편집값')
        self.assertEqual(reread['ocr_text'], '새 OCR')
        self.assertEqual(len(list(Path(self.temp.name).glob('document_library.sqlite3.before-library-*.bak'))), 1)

    def test_commit_failure_raises_and_rolls_back_text_and_document_token(self):
        document = self.library.create_document()
        page = self.library.add_text_page(document['id'], '원래 텍스트')
        document = self.library.get_document(document['id'])
        with closing(sqlite3.connect(self.library.path)) as db, db:
            db.execute('CREATE TABLE commit_guard (document_id TEXT REFERENCES documents(id) DEFERRABLE INITIALLY DEFERRED)')
            db.execute("CREATE TRIGGER reject_commit AFTER UPDATE ON pages BEGIN INSERT INTO commit_guard VALUES ('missing'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.library.save_page_text(page['id'], '커밋 실패', page['updated_at'])
        reopened = self.reopen()
        self.assertEqual(reopened.pages(document['id'])[0], page)
        self.assertEqual(reopened.get_document(document['id']), document)

    def test_schema_final_failure_rolls_back_migration_and_keeps_backup(self):
        document = self.library.create_document()
        self.library.add_text_page(document['id'], '보존 값')
        with closing(sqlite3.connect(self.library.path)) as db, db:
            db.execute('ALTER TABLE pages DROP COLUMN ocr_text')
            db.execute('ALTER TABLE pages DROP COLUMN edited')
            db.execute('PRAGMA user_version=1')
        original_connect = sqlite3.connect

        class FailFinalVersion(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                if sql.strip().upper() == 'PRAGMA USER_VERSION=3':
                    raise sqlite3.OperationalError('synthetic final migration failure')
                return super().execute(sql, *args, **kwargs)

        def connect(*args, **kwargs):
            return original_connect(*args, factory=FailFinalVersion, **kwargs)

        with patch('services.document_library.sqlite3.connect', side_effect=connect):
            with self.assertRaises(sqlite3.OperationalError):
                DocumentLibrary(self.temp.name, capture_store=self.library.capture_store, task_store=self.library.task_store)
        with closing(sqlite3.connect(self.library.path)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)
            self.assertNotIn('ocr_text', {row[1] for row in db.execute('PRAGMA table_info(pages)')})
        self.assertEqual(len(list(Path(self.temp.name).glob('document_library.sqlite3.before-library-*.bak'))), 1)
        self.assertEqual(self.reopen().document_text(document['id']), '보존 값')

    def test_failed_settings_update_preserves_previous_preference(self):
        self.library.set_setting('automatic_ocr', False)
        with closing(sqlite3.connect(self.library.path)) as db, db:
            db.execute("CREATE TRIGGER reject_setting BEFORE UPDATE ON settings BEGIN SELECT RAISE(ABORT,'synthetic settings failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.library.set_setting('automatic_ocr', True)
        self.assertFalse(self.reopen().get_setting('automatic_ocr', True))

    def test_incompatible_current_sidecar_schema_is_rejected_without_overwrite(self):
        with closing(sqlite3.connect(self.library.path)) as db, db:
            db.execute('ALTER TABLE outputs DROP COLUMN source_fingerprint')
        before = self.library.path.read_bytes()
        with self.assertRaises(ValueError):
            self.reopen()
        self.assertEqual(self.library.path.read_bytes(), before)

    def test_initial_ocr_is_write_once_across_reread_and_restart(self):
        capture = self.capture('최초 OCR\t\n')
        document = self.library.adopt_capture(capture.id)
        page = self.library.pages(document['id'])[0]
        self.assertIsNone(self.library.initial_ocr(page['id']))
        self.assertEqual(self.library.remember_initial_ocr(page['id'], page['ocr_text']), '최초 OCR\t\n')
        self.library.capture_store.update_ocr(capture.id, '최근 OCR')
        self.assertEqual(self.library.remember_initial_ocr(page['id'], '최근 OCR'), '최초 OCR\t\n')
        reopened = self.reopen()
        self.assertEqual(reopened.initial_ocr(page['id']), '최초 OCR\t\n')
        self.assertEqual(reopened.pages(document['id'])[0]['ocr_text'], '최근 OCR')

    def test_initial_ocr_explicit_empty_is_not_replaced(self):
        document = self.library.create_document()
        page = self.library.add_text_page(document['id'], '')
        self.assertEqual(self.library.remember_initial_ocr(page['id'], ''), '')
        self.assertEqual(self.library.remember_initial_ocr(page['id'], '나중 값'), '')
        self.assertEqual(self.reopen().initial_ocr(page['id']), '')

    def test_initial_ocr_snapshot_failure_rolls_back_and_allows_retry(self):
        document = self.library.create_document()
        page = self.library.add_text_page(document['id'], '원문 유지')
        with patch('services.document_library.json.loads', side_effect=ValueError('synthetic decode fault')):
            with self.assertRaises(ValueError):
                self.library.remember_initial_ocr(page['id'], '최초 인식')
        reopened = self.reopen()
        self.assertIsNone(reopened.initial_ocr(page['id']))
        self.assertEqual(reopened.document_text(document['id']), '원문 유지')
        self.assertEqual(reopened.remember_initial_ocr(page['id'], '재시도 인식'), '재시도 인식')

    def test_initial_ocr_cannot_write_legacy_or_unknown_pages(self):
        capture = self.capture()
        task = self.library.task_store.create(title='기존 업무', source_text='기존 원문')
        for page_id in ('capture:' + capture.id, 'legacy-text:' + task.id, 'missing-page'):
            with self.subTest(page_id=page_id):
                with self.assertRaises(LibraryReadOnlyError):
                    self.library.remember_initial_ocr(page_id, '기록 금지')
                self.assertIsNone(self.library.initial_ocr(page_id))
        self.assertEqual(self.library.capture_store.get(capture.id), capture)
        self.assertEqual(self.library.task_store.get(task.id), task)


if __name__ == '__main__':
    unittest.main()
