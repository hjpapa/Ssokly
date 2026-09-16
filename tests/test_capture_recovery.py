"""Synthetic orphan recovery and v1 migration failures; no real user stores."""
from contextlib import closing
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from PIL import Image

from services.capture_store import CaptureStore, _file_metadata
from services.document_library import DocumentLibrary


class CaptureRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inbox = self.root / 'capture_inbox'
        self.store = CaptureStore(self.inbox)
        self.image = Image.new('RGBA', (12, 8), (20, 60, 110, 170))

    def orphan(self, *, name=None, image=None):
        name = name or f'20260917_010203_000001_{uuid4()}_synthetic.png'
        path = self.inbox / name
        (image or self.image).save(path, format='PNG')
        return path

    def downgrade(self):
        with closing(sqlite3.connect(self.store.db_path)) as db, db:
            for name in ('verified_text', 'verified_at', 'verified_ocr_sha256'):
                db.execute('ALTER TABLE capture_items DROP COLUMN ' + name)
            db.execute('PRAGMA user_version=1')

    def schema(self, path=None):
        with closing(sqlite3.connect(path or self.store.db_path)) as db:
            return db.execute('PRAGMA user_version').fetchone()[0], tuple(row[1] for row in db.execute('PRAGMA table_info(capture_items)'))

    def test_promoted_png_survives_metadata_failure_and_search_recovers_it(self):
        with patch.object(self.store, '_record_from_row', side_effect=sqlite3.OperationalError('synthetic metadata failure')):
            with self.assertRaises(sqlite3.OperationalError):
                self.store.save(self.image, source='synthetic_capture')
        paths = list(self.inbox.glob('*.png'))
        self.assertEqual(len(paths), 1)
        self.assertEqual(list(self.inbox.glob('*.pending')), [])
        before = paths[0].read_bytes()
        records = self.store.search()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].path.read_bytes(), before)
        self.assertEqual(records[0].content_sha256, hashlib.sha256(before).hexdigest())
        self.assertEqual(records[0].ocr_status, 'unread')
        self.assertEqual(records[0].verified_text, '')

    def test_actual_commit_failure_preserves_promoted_png_for_restart(self):
        original = self.store._record_from_row
        def fail_at_commit(db, row):
            record = original(db, row)
            db.execute('CREATE TABLE synthetic_fault (id TEXT REFERENCES capture_items(id) DEFERRABLE INITIALLY DEFERRED)')
            db.execute("INSERT INTO synthetic_fault VALUES ('missing')")
            return record
        with patch.object(self.store, '_record_from_row', side_effect=fail_at_commit):
            with self.assertRaises(sqlite3.IntegrityError):
                self.store.save(self.image)
        self.assertEqual(len(list(self.inbox.glob('*.png'))), 1)
        reopened = CaptureStore(self.inbox)
        self.assertEqual(len(reopened.search()), 1)
        with closing(sqlite3.connect(reopened.db_path)) as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='synthetic_fault'").fetchone())

    def test_database_connection_failure_still_preserves_completed_png(self):
        with patch.object(self.store, '_connect', side_effect=sqlite3.OperationalError('synthetic unavailable DB')):
            with self.assertRaises(sqlite3.OperationalError):
                self.store.save(self.image)
        self.assertEqual(len(list(self.inbox.glob('*.png'))), 1)
        self.assertEqual(len(CaptureStore(self.inbox).search()), 1)

    def test_document_library_discovers_same_session_orphan_without_restart(self):
        library = DocumentLibrary(self.root, capture_store=self.store)
        with patch.object(self.store, '_record_from_row', side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):
                self.store.save(self.image)
        documents = library.list_documents()
        self.assertEqual(len(documents), 1)
        self.assertTrue(documents[0]['id'].startswith('capture:'))
        self.assertTrue(Path(documents[0]['thumbnail_path']).exists())

    def test_constructor_and_repeated_search_recovery_are_idempotent(self):
        path = self.orphan()
        first = CaptureStore(self.inbox)
        record = first.search()[0]
        first.update_ocr(record.id, 'synthetic OCR')
        first.save_verified_text(record.id, '')
        first.trash(record.id)
        for _ in range(3):
            reopened = CaptureStore(self.inbox)
            self.assertEqual(reopened.recover_orphans(), 0)
            self.assertEqual(reopened.search(), [])
            self.assertEqual(len(reopened.search(trashed=True)), 1)
        self.assertEqual(reopened.get(record.id).ocr_text, 'synthetic OCR')
        self.assertTrue(reopened.get(record.id).is_verified)
        self.assertTrue(path.exists())

    def test_default_directory_recovers_without_importing_any_external_files(self):
        app_root = self.root / 'default-app-data'
        directory = app_root / 'Ssokly' / 'capture_inbox'
        directory.mkdir(parents=True)
        path = directory / f'20260917_010203_000001_{uuid4()}_capture.png'
        self.image.save(path, format='PNG')
        with patch.dict(os.environ, {'LOCALAPPDATA': str(app_root)}), patch(
                'services.capture_store._legacy_store_dir', return_value=self.root / 'missing-legacy'):
            default = CaptureStore()
        self.assertEqual(default.search()[0].path, path)

    def test_arbitrary_names_invalid_timestamp_pending_and_fake_png_are_ignored(self):
        self.orphan(name='user-supplied.png')
        self.orphan(name=f'20261399_010203_000001_{uuid4()}_bad.png')
        self.orphan(name=f'.20260917_010203_000001_{uuid4()}_capture.png.pending')
        broken = self.inbox / f'20260917_010203_000001_{uuid4()}_broken.png'
        broken.write_bytes(b'not an image')
        disguised = self.inbox / f'20260917_010203_000001_{uuid4()}_jpeg.png'
        self.image.convert('RGB').save(disguised, format='JPEG')
        self.assertEqual(self.store.recover_orphans(), 0)
        self.assertEqual(self.store.search(), [])
        self.assertTrue(broken.exists())
        self.assertTrue(disguised.exists())

    def test_corrupted_png_checksum_does_not_block_healthy_orphan_recovery(self):
        corrupted = self.orphan()
        payload = bytearray(corrupted.read_bytes())
        payload[payload.find(b'IDAT') + 4] ^= 1
        corrupted.write_bytes(payload)
        healthy = self.orphan()
        self.assertEqual(self.store.recover_orphans(), 1)
        self.assertEqual(self.store.search()[0].path, healthy)
        self.assertTrue(corrupted.exists())

    def test_symlink_guard_and_nested_directory_are_not_followed(self):
        suspect = self.orphan()
        nested = self.inbox / 'nested'
        nested.mkdir()
        self.image.save(nested / f'20260917_010203_000001_{uuid4()}_capture.png', format='PNG')
        real_check = Path.is_symlink
        with patch.object(Path, 'is_symlink', autospec=True, side_effect=lambda path: path == suspect or real_check(path)):
            self.assertEqual(self.store.recover_orphans(), 0)
        self.assertTrue(suspect.exists())

    def test_hardlink_to_external_file_is_not_adopted(self):
        outside = self.root / 'outside.png'
        self.image.save(outside, format='PNG')
        destination = self.inbox / f'20260917_010203_000001_{uuid4()}_capture.png'
        os.link(outside, destination)
        self.assertEqual(self.store.recover_orphans(), 0)
        self.assertEqual(outside.read_bytes(), destination.read_bytes())

    def test_file_replaced_during_validation_is_not_inserted(self):
        path = self.orphan()
        def changed(candidate):
            Image.new('RGB', (1, 1), 'white').save(candidate, format='PNG')
            return _file_metadata(candidate)
        with patch('services.capture_store._file_metadata', side_effect=changed):
            self.assertEqual(self.store.recover_orphans(), 0)
        with closing(sqlite3.connect(self.store.db_path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM capture_items').fetchone()[0], 0)
        self.assertTrue(path.exists())

    def test_collision_never_replaces_existing_id_or_metadata(self):
        existing = self.store.save(self.image)
        self.store.update_ocr(existing.id, 'retained OCR')
        duplicate = self.orphan(name=f'20260917_010203_000001_{existing.id}_collision.png',
                                image=Image.new('RGB', (1, 1), 'red'))
        self.assertEqual(self.store.recover_orphans(), 0)
        self.assertEqual(self.store.get(existing.id).path, existing.path)
        self.assertEqual(self.store.get(existing.id).ocr_text, 'retained OCR')
        self.assertTrue(duplicate.exists())

    def test_normal_save_can_finish_after_another_store_recovers_promoted_png(self):
        second = CaptureStore(self.inbox)
        original = Path.replace
        def promote(path, target):
            result = original(path, target)
            if str(path).endswith('.pending') and str(target).endswith('.png'):
                self.assertEqual(second.recover_orphans(), 1)
                second.update_ocr(second.search()[0].id, 'concurrent OCR')
            return result
        with patch.object(Path, 'replace', autospec=True, side_effect=promote):
            saved = self.store.save(self.image, source='synthetic capture source')
        self.assertEqual(saved.source, 'synthetic capture source')
        self.assertEqual(saved.ocr_text, 'concurrent OCR')
        self.assertEqual(len(self.store.search()), 1)

    def test_v1_upgrade_keeps_readable_backup_and_existing_data(self):
        capture = self.store.save(self.image)
        self.store.update_ocr(capture.id, 'existing OCR')
        self.store.link_to_task(capture.id, 'existing-task')
        self.downgrade()
        before = self.schema()
        upgraded = CaptureStore(self.inbox)
        backups = list(self.inbox.glob('capture_inbox.sqlite3.before-capture-inbox-*.bak'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(self.schema(backups[0]), before)
        self.assertEqual(self.schema()[0], 2)
        with closing(sqlite3.connect(backups[0])) as db:
            self.assertEqual(db.execute('SELECT ocr_text FROM capture_items').fetchone()[0], 'existing OCR')
            self.assertEqual(db.execute('SELECT task_id FROM capture_links').fetchone()[0], 'existing-task')
        self.assertEqual(upgraded.get(capture.id).ocr_text, 'existing OCR')
        CaptureStore(self.inbox)
        self.assertEqual(len(list(self.inbox.glob('*.bak'))), 1)

    def test_backup_failure_leaves_v1_schema_unchanged(self):
        self.store.save(self.image)
        self.downgrade()
        before = self.schema()
        with patch.object(CaptureStore, '_backup_before_upgrade', side_effect=OSError('synthetic backup failure')):
            with self.assertRaises(OSError):
                CaptureStore(self.inbox)
        self.assertEqual(self.schema(), before)

    def test_upgrade_final_failure_rolls_back_all_alters_and_keeps_backup(self):
        capture = self.store.save(self.image)
        self.downgrade()
        before = self.schema()
        class FaultConnection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if sql.strip() == 'PRAGMA user_version = 2':
                    raise sqlite3.OperationalError('synthetic migration failure')
                return super().execute(sql, parameters)
        def connect(store):
            db = sqlite3.connect(store.db_path, factory=FaultConnection)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            return db
        with patch.object(CaptureStore, '_connect', connect):
            with self.assertRaises(sqlite3.OperationalError):
                CaptureStore(self.inbox)
        self.assertEqual(self.schema(), before)
        backups = list(self.inbox.glob('*.bak'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(self.schema(backups[0]), before)
        self.assertTrue(capture.path.exists())
        self.assertIsNotNone(CaptureStore(self.inbox).get(capture.id))


if __name__ == '__main__':
    unittest.main()
