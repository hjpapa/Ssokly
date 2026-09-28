"""Synthetic trash lifecycle and irreversible deletion regression coverage."""
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from services.document_library import DocumentLibrary, LibraryConflictError, LibraryReadOnlyError


class TrashTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.library = DocumentLibrary(self.temp.name)
        self.doc = self.library.create_document('Synthetic')

    def token(self, doc=None):
        return self.library.get_document((doc or self.doc)['id'])['updated_at']

    def image_page(self, doc=None):
        with Image.new('RGB', (20, 20), 'white') as image:
            capture = self.library.capture_store.save(image)
        return capture, self.library.add_capture((doc or self.doc)['id'], capture.id)

    def trash_page(self, page):
        self.library.set_page_trash(page['id'], True, expected_updated_at=self.library.get_document(page['document_id'])['updated_at'])

    def test_last_page_moves_labels_and_document_restore_brings_last_page_back(self):
        first = self.library.add_text_page(self.doc['id'], 'first')
        last = self.library.add_text_page(self.doc['id'], 'last')
        self.library.update_details(self.doc['id'], ['Label'], 'Memo')
        self.trash_page(first)
        self.assertEqual(len(self.library.list_documents()), 1)
        self.trash_page(last)
        self.assertEqual(self.library.list_documents(), [])
        self.assertEqual(self.library.list_documents(trashed=True)[0]['labels'], ['Label'])
        self.library.restore(self.doc['id'], self.token())
        self.assertEqual(self.library.document_text(self.doc['id']), 'last')
        self.assertEqual(len(self.library.deleted_pages()), 1)

    def test_reopen_repairs_old_leftover_but_keeps_intentionally_empty_document(self):
        page = self.library.add_text_page(self.doc['id'], 'old')
        empty = self.library.create_document('Empty')
        with sqlite3.connect(self.library.path) as db:
            db.execute("UPDATE pages SET trashed_at='2026-01-01' WHERE id=?", (page['id'],))
        reopened = DocumentLibrary(self.temp.name)
        self.assertEqual([d['id'] for d in reopened.list_documents()], [empty['id']])
        reopened.set_page_trash(page['id'], False, expected_updated_at=self.token())
        self.assertFalse(reopened.get_document(self.doc['id'])['trashed'])

    def test_document_purge_removes_compatibility_outputs_settings_and_owned_image(self):
        capture, page = self.image_page()
        self.library.remember_initial_ocr(page['id'], 'original')
        self.library.save_output(self.doc['id'], 'mode', 'result', 'fingerprint')
        self.library.set_setting('active_output:' + self.doc['id'], 'result')
        self.library.set_setting('unrelated', 'keep')
        self.library.update_details(self.doc['id'], ['Label'], 'Memo')
        self.trash_page(page)
        self.library.purge_trash(self.library.trash_snapshot())
        reopened = DocumentLibrary(self.temp.name)
        self.assertEqual(reopened.list_documents(), [])
        self.assertEqual(reopened.list_documents(trashed=True), [])
        self.assertEqual(reopened.label_counts(), {})
        self.assertIsNone(reopened.task_store.get(self.doc['id']))
        self.assertIsNone(reopened.initial_ocr(page['id']))
        self.assertEqual(reopened.outputs(self.doc['id']), [])
        self.assertEqual(reopened.get_setting('unrelated'), 'keep')
        self.assertFalse(Path(capture.path).exists())

    def test_shared_capture_survives_active_and_trashed_other_document(self):
        for other_trashed in (False, True):
            with self.subTest(other_trashed=other_trashed):
                source = self.library.create_document('Source')
                capture, page = self.image_page(source)
                other = self.library.create_document('Other')
                self.library.add_capture(other['id'], capture.id)
                if other_trashed:
                    self.library.trash(other['id'])
                self.library.trash(source['id'])
                self.library.purge_trash({'documents': {source['id']: self.token(source)}, 'pages': {}})
                self.assertTrue(Path(capture.path).exists())
                self.assertEqual(self.library.pages(other['id'])[0]['capture_id'], capture.id)

    def test_page_purge_keeps_active_sibling_and_external_source(self):
        capture, page = self.image_page()
        external = Path(self.temp.name) / 'external.txt'
        external.write_text('preserve')
        sibling = self.library.add_text_page(self.doc['id'], 'sibling')
        with sqlite3.connect(self.library.path) as db:
            db.execute('UPDATE pages SET source_path=? WHERE id=?', (str(external), page['id']))
        self.trash_page(page)
        self.library.purge_trash(self.library.trash_snapshot())
        self.assertEqual(self.library.pages(self.doc['id'])[0]['id'], sibling['id'])
        self.assertEqual(external.read_text(), 'preserve')
        self.assertFalse(Path(capture.path).exists())

    def test_active_or_restored_items_cannot_be_purged(self):
        page = self.library.add_text_page(self.doc['id'], 'keep')
        with self.assertRaises(LibraryReadOnlyError):
            self.library.purge_trash({'documents': {self.doc['id']: self.token()}, 'pages': {}})
        self.trash_page(page)
        snapshot = self.library.trash_snapshot()
        self.library.restore(self.doc['id'])
        with self.assertRaises(LibraryConflictError):
            self.library.purge_trash(snapshot)
        self.assertEqual(self.library.document_text(self.doc['id']), 'keep')

    def test_empty_trash_snapshot_preserves_new_trash(self):
        self.library.trash(self.doc['id'])
        snapshot = self.library.trash_snapshot()
        later = self.library.create_document('Later')
        self.library.trash(later['id'])
        self.library.purge_trash(snapshot)
        self.assertEqual([d['id'] for d in self.library.list_documents(trashed=True)], [later['id']])

    def test_database_failure_restores_staged_file_and_all_metadata(self):
        capture, page = self.image_page()
        self.trash_page(page)
        with sqlite3.connect(self.library.capture_store.db_path) as db:
            db.execute("CREATE TRIGGER fail_purge BEFORE DELETE ON capture_items BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.library.purge_trash(self.library.trash_snapshot())
        self.assertTrue(Path(capture.path).exists())
        self.assertIsNotNone(self.library.task_store.get(self.doc['id']))
        self.assertEqual(len(self.library.deleted_pages()), 1)

    def test_legacy_reference_protects_shared_image(self):
        capture, page = self.image_page()
        self.library.capture_store.link_to_task([capture.id], 'legacy-reference')
        self.trash_page(page)
        self.library.purge_trash(self.library.trash_snapshot())
        self.assertTrue(Path(capture.path).exists())

    def test_unlink_failure_is_reported_without_resurrection(self):
        capture, page = self.image_page()
        self.trash_page(page)
        original_unlink = Path.unlink
        def fail_staged(path, *args, **kwargs):
            if path.name.endswith('.deleting'):
                raise PermissionError('synthetic')
            return original_unlink(path, *args, **kwargs)
        with patch.object(Path, 'unlink', fail_staged):
            result = self.library.purge_trash(self.library.trash_snapshot())
        self.assertEqual(result['pending_files'], 1)
        self.assertEqual(DocumentLibrary(self.temp.name).list_documents(), [])

    def test_purge_merged_source_keeps_moved_capture(self):
        capture, page = self.image_page()
        target = self.library.create_document('Target')
        for doc in (self.doc, target):
            self.library.update_details(doc['id'], ['Same'], '')
        self.library.merge_documents(target['id'], [self.doc['id']], expected_versions={
            target['id']: self.token(target), self.doc['id']: self.token()})
        self.library.purge_trash(self.library.trash_snapshot())
        self.assertTrue(Path(capture.path).exists())
        self.assertEqual(self.library.pages(target['id'])[0]['id'], page['id'])

    def test_raw_capture_trash_can_be_emptied(self):
        with Image.new('RGB', (20, 20), 'white') as image:
            capture = self.library.capture_store.save(image)
        self.library.capture_store.trash([capture.id])
        self.library.purge_trash(self.library.trash_snapshot())
        self.assertFalse(Path(capture.path).exists())

    def test_unsafe_storage_path_rejects_batch_without_deleting_external_file(self):
        capture, page = self.image_page()
        external = Path(self.temp.name) / 'external.png'
        external.write_bytes(b'keep')
        self.trash_page(page)
        with sqlite3.connect(self.library.capture_store.db_path) as db:
            db.execute('UPDATE capture_items SET storage_name=? WHERE id=?', ('../external.png', capture.id))
        with self.assertRaises(Exception):
            self.library.purge_trash(self.library.trash_snapshot())
        self.assertEqual(external.read_bytes(), b'keep')
        self.assertTrue(self.library.get_document(self.doc['id'])['trashed'])
        self.assertTrue(Path(capture.path).exists())
