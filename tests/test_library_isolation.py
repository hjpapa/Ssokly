"""One unreadable capture must not hide healthy documents or their text."""
from contextlib import closing
from pathlib import Path
import socket
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from services.document_library import DocumentLibrary, LibraryReadOnlyError


class LibraryIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='ssokly-isolation-')
        self.addCleanup(self.temporary.cleanup)
        self.network = patch.object(socket.socket, 'connect', side_effect=AssertionError('No network'))
        self.dns = patch.object(socket, 'getaddrinfo', side_effect=AssertionError('No network'))
        self.network.start()
        self.dns.start()
        self.addCleanup(self.network.stop)
        self.addCleanup(self.dns.stop)
        self.library = DocumentLibrary(self.temporary.name)
        self.store = self.library.capture_store

    def capture(self, ocr='인식 원문', verified=None):
        capture = self.store.save(Image.new('RGB', (12, 8), 'white'))
        capture = self.store.update_ocr(capture.id, ocr)
        if verified is not None:
            capture = self.store.save_verified_text(capture.id, verified)
        return capture

    def damage(self, capture, column='storage_name', value='../outside-private-name.png'):
        self.assertIn(column, {'storage_name', 'created_at', 'updated_at'})
        with closing(sqlite3.connect(self.store.db_path)) as db, db:
            db.execute(f'UPDATE capture_items SET {column}=? WHERE id=?', (value, capture.id))

    def capture_rows(self):
        with closing(sqlite3.connect(self.store.db_path)) as db:
            return list(db.execute('SELECT * FROM capture_items ORDER BY id'))

    def assert_recovery(self, item):
        self.assertTrue(item['recovery_required'])
        self.assertIn('복구', item['warning'])
        self.assertNotIn('outside-private-name', item['warning'])
        self.assertNotIn(self.temporary.name, item['warning'])

    def test_linked_legacy_capture_isolated_without_hiding_task_text_or_healthy_entries(self):
        capture = self.capture(verified='보존할 검수본')
        task = self.library.task_store.create(title='기존 문서', source_text='통합 원문 검색어', analysis_text='기존 결과')
        self.store.link_to_task([capture.id], task.id)
        healthy = self.library.create_document('정상 문서')
        self.library.add_text_page(healthy['id'], '정상 검색어')
        raw_healthy = self.capture('독립 정상 캡처')
        png = capture.path.read_bytes()
        self.damage(capture)
        before = self.capture_rows()

        documents = {item['id']: item for item in self.library.list_documents()}
        self.assertEqual(set(documents), {task.id, healthy['id'], 'capture:' + capture.id, 'capture:' + raw_healthy.id})
        self.assert_recovery(documents[task.id])
        self.assert_recovery(documents['capture:' + capture.id])
        self.assertFalse(documents[healthy['id']]['recovery_required'])
        pages = self.library.pages(task.id)
        self.assertEqual(len(pages), 2)
        self.assertEqual(pages[0]['text'], '통합 원문 검색어')
        self.assertIsNone(pages[1]['path'])
        self.assertTrue(all(page['readonly'] for page in pages))
        self.assertEqual(self.library.document_text(task.id), '통합 원문 검색어')
        self.assertEqual([item['id'] for item in self.library.list_documents('통합 원문 검색어')], [task.id])
        self.assertEqual([item['id'] for item in self.library.list_documents('정상 검색어')], [healthy['id']])
        self.assertEqual(self.library.task_store.get(task.id), task)
        self.assertEqual(self.capture_rows(), before)
        self.assertEqual(capture.path.read_bytes(), png)

    def test_standalone_unreadable_capture_preserves_explicit_empty_and_raw_ocr(self):
        capture = self.capture('원 인식 검색어', verified='')
        self.damage(capture)
        before = self.capture_rows()
        document_id = 'capture:' + capture.id
        document = self.library.get_document(document_id)
        self.assert_recovery(document)
        self.assertTrue(document['readonly'])
        self.assertIsNone(document['thumbnail_path'])
        page = self.library.pages(document_id)[0]
        self.assert_recovery(page)
        self.assertTrue(page['readonly'])
        self.assertEqual(page['text'], '')
        self.assertEqual(page['ocr_text'], '원 인식 검색어')
        self.assertIsNone(page['path'])
        self.assertIsNone(page['source_path'])
        self.assertEqual([item['id'] for item in self.library.list_documents('원 인식 검색어')], [document_id])
        reopened = DocumentLibrary(self.temporary.name)
        self.assertEqual(reopened.pages(document_id)[0]['text'], '')
        self.assertEqual(self.capture_rows(), before)

    def test_managed_bad_page_read_only_but_sibling_edits_outputs_and_search_work(self):
        capture = self.capture(verified='교사가 보존한 내용')
        document = self.library.create_document('혼합 문서')
        affected = self.library.add_capture(document['id'], capture.id)
        sibling = self.library.add_text_page(document['id'], '정상 페이지')
        self.damage(capture)
        before = self.capture_rows()
        current = self.library.get_document(document['id'])
        self.assert_recovery(current)
        self.assertFalse(current['readonly'])
        pages = self.library.pages(document['id'])
        self.assert_recovery(pages[0])
        self.assertTrue(pages[0]['readonly'])
        self.assertFalse(pages[1]['readonly'])
        with self.assertRaises(LibraryReadOnlyError):
            self.library.save_page_text(affected['id'], '덮어쓰기 금지', affected['updated_at'])
        self.assertEqual(self.capture_rows(), before)
        self.library.save_page_text(sibling['id'], '정상 수정 검색어', sibling['updated_at'])
        self.library.save_output(document['id'], '요약', '결과 검색어', 'synthetic-fingerprint')
        self.assertEqual(self.library.document_text(document['id']), '교사가 보존한 내용\n\n정상 수정 검색어')
        for needle in ('교사가 보존한 내용', '정상 수정 검색어', '결과 검색어'):
            self.assertEqual([item['id'] for item in self.library.list_documents(needle)], [document['id']])
        self.assertEqual(len(self.library.list_documents()), 1)

    def test_external_path_is_not_opened_or_used_as_a_thumbnail(self):
        capture = self.capture(verified='안전한 검수본')
        self.damage(capture)
        external = Path(self.temporary.name) / 'outside-private-name.png'
        Image.new('RGB', (4, 3), 'red').save(external)
        opened = []
        original_open = Image.open

        def guarded_open(path, *args, **kwargs):
            if isinstance(path, (str, Path)):
                opened.append(Path(path).absolute())
                self.assertNotEqual(Path(path).absolute(), external.absolute())
            return original_open(path, *args, **kwargs)

        with patch.object(Image, 'open', side_effect=guarded_open):
            entries = self.library.list_documents()
            pages = self.library.pages('capture:' + capture.id)
        self.assertIsNone(entries[0]['thumbnail_path'])
        self.assertIsNone(pages[0]['path'])
        self.assertNotIn(external.absolute(), opened)

    def test_bad_timestamp_stays_visible_without_breaking_sort_or_search(self):
        capture = self.capture('보존할 원문 검색')
        healthy = self.library.create_document('정상')
        self.damage(capture, 'updated_at', 'not-a-date')
        documents = {item['id']: item for item in self.library.list_documents()}
        self.assertEqual(set(documents), {healthy['id'], 'capture:' + capture.id})
        self.assert_recovery(documents['capture:' + capture.id])
        self.assertEqual(documents['capture:' + capture.id]['updated_at'], '')
        self.assertEqual([item['id'] for item in self.library.list_documents('보존할 원문 검색')], ['capture:' + capture.id])

    def test_missing_capture_record_keeps_page_slot_and_stored_snapshot_read_only(self):
        capture = self.capture('처음 담은 원문')
        document = self.library.create_document('원문 유지')
        original = self.library.add_capture(document['id'], capture.id)
        with patch.object(self.store, 'safe_get', return_value=None):
            current = self.library.get_document(document['id'])
            self.assertEqual(current['page_count'], 1)
            self.assert_recovery(current)
            page = self.library.pages(document['id'])[0]
            self.assertEqual(page['id'], original['id'])
            self.assertEqual(page['text'], '처음 담은 원문')
            self.assertTrue(page['missing'])
            self.assertTrue(page['readonly'])
            self.assertIsNone(page['path'])
            with self.assertRaises(LibraryReadOnlyError):
                self.library.save_page_text(page['id'], '', page['updated_at'])
        self.assertEqual(self.store.get(capture.id).ocr_text, '처음 담은 원문')

    def test_missing_png_keeps_current_teacher_text_but_marks_page_for_recovery(self):
        capture = self.capture('인식 원문', verified='남아 있는 교사 수정본')
        document = self.library.adopt_capture(capture.id)
        before = self.capture_rows()
        # Only this test's synthetic PNG is removed; the metadata is untouched.
        capture.path.unlink()
        current = self.library.get_document(document['id'])
        self.assert_recovery(current)
        page = self.library.pages(document['id'])[0]
        self.assert_recovery(page)
        self.assertTrue(page['readonly'])
        self.assertIsNone(page['path'])
        self.assertEqual(page['text'], '남아 있는 교사 수정본')
        self.assertEqual(page['ocr_text'], '인식 원문')
        self.assertEqual(self.capture_rows(), before)

    def test_trashed_unreadable_capture_retains_trash_state_and_warning(self):
        capture = self.capture('휴지통 원문')
        self.store.trash(capture.id)
        self.damage(capture)
        self.assertEqual(self.library.list_documents(), [])
        entries = self.library.list_documents(trashed=True)
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0]['trashed'])
        self.assert_recovery(entries[0])
        self.assertEqual(self.library.pages(entries[0]['id'])[0]['text'], '휴지통 원문')

    def test_storage_failure_is_not_disguised_as_successful_empty_library(self):
        self.library.create_document('정상 문서')
        with patch.object(self.store, 'safe_search', side_effect=sqlite3.OperationalError('synthetic unavailable')):
            with self.assertRaises(sqlite3.OperationalError):
                self.library.list_documents()

    def test_new_capture_can_be_saved_and_opened_while_old_capture_needs_recovery(self):
        old = self.capture('과거 캡처')
        self.damage(old)
        fresh = self.capture('새 캡처')
        document = self.library.create_document('새 정상 문서')
        self.library.add_capture(document['id'], fresh.id)
        entries = {item['id']: item for item in self.library.list_documents()}
        self.assertEqual(set(entries), {'capture:' + old.id, document['id']})
        self.assert_recovery(entries['capture:' + old.id])
        self.assertFalse(entries[document['id']]['recovery_required'])
        self.assertEqual(self.library.document_text(document['id']), '새 캡처')
        self.assertEqual(Path(self.library.pages(document['id'])[0]['path']).read_bytes(), fresh.path.read_bytes())

    def test_repaired_metadata_removes_warning_without_reimport_or_text_overwrite(self):
        capture = self.capture('원문', verified='검수본')
        document = self.library.adopt_capture(capture.id)
        self.damage(capture)
        self.assert_recovery(self.library.get_document(document['id']))
        self.damage(capture, value=capture.path.name)
        restored = self.library.get_document(document['id'])
        self.assertFalse(restored['recovery_required'])
        self.assertEqual(restored['warning'], '')
        page = self.library.pages(document['id'])[0]
        self.assertFalse(page['readonly'])
        self.assertEqual(page['text'], '검수본')
        self.library.save_page_text(page['id'], '복구 후 수정', page['updated_at'])
        self.assertEqual(self.library.document_text(document['id']), '복구 후 수정')


if __name__ == '__main__':
    unittest.main()
