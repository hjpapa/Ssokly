"""Synthetic OS-alias and hostile path regressions; no user data or APIs."""
from contextlib import closing, contextmanager
from dataclasses import replace
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image
from services.capture_store import CaptureStore


class CapturePathAliasTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inbox = self.root / 'logical' / 'capture_inbox'
        self.alias = self.root / 'os-private' / 'capture_inbox'
        self.store = CaptureStore(self.inbox)
        self.image = Image.new('RGB', (17, 11), (20, 60, 110))
        self.addCleanup(self.image.close)

    @contextmanager
    def virtualized(self, *, same_identity=True, target_transform=None):
        real_resolve, real_lstat = Path.resolve, Path.lstat

        def resolve(path, strict=False):
            result = real_resolve(path, strict=strict)
            if result.parent == self.inbox and result.suffix == '.png' and result.is_file():
                return self.alias / result.name
            return result

        def lstat(path):
            if same_identity and path.parent == self.alias:
                value = real_lstat(self.inbox / path.name)
                return target_transform(value) if target_transform else value
            return real_lstat(path)

        with patch('services.capture_paths._WINDOWS_ALIASES', True), patch.object(
                Path, 'resolve', autospec=True, side_effect=resolve), patch.object(
                Path, 'lstat', autospec=True, side_effect=lstat):
            yield

    def count(self):
        with closing(sqlite3.connect(self.store.db_path)) as db:
            return db.execute('SELECT COUNT(*) FROM capture_items').fetchone()[0]

    def test_save_read_search_and_restart_use_logical_path_for_verified_alias(self):
        with self.virtualized():
            record = self.store.save(self.image)
            self.assertEqual(record.path.parent, self.inbox)
            self.assertEqual(self.store.get(record.id).path, record.path)
            self.assertEqual(self.store.search()[0].id, record.id)
            loaded = self.store.load(record)
            self.addCleanup(loaded.close)
            self.assertEqual(loaded.getpixel((0, 0)), (20, 60, 110))
            self.assertEqual(CaptureStore(self.inbox).get(record.id).path, record.path)
            self.assertEqual(self.count(), 1)

    def test_promoted_png_recovers_with_verified_alias_after_metadata_failure(self):
        with self.virtualized():
            with patch.object(self.store, '_record_from_row', side_effect=sqlite3.OperationalError('synthetic')):
                with self.assertRaises(sqlite3.OperationalError):
                    self.store.save(self.image)
            self.assertEqual(self.count(), 0)
            self.assertEqual(len(list(self.inbox.glob('*.png'))), 1)
            restarted = CaptureStore(self.inbox)
            self.assertEqual(restarted.recover_orphans(), 0)
            self.assertEqual(len(restarted.search()), 1)
            self.assertEqual(self.count(), 1)

    def test_alias_record_remains_compatible_but_arbitrary_external_record_is_rejected(self):
        with self.virtualized():
            record = self.store.save(self.image)
            old_alias_record = replace(record, path=self.alias / record.path.name)
            self.assertEqual(self.store._path_for_record(old_alias_record), record.path)
            with self.assertRaises(ValueError):
                self.store._path_for_record(replace(record, path=self.root / 'external' / record.path.name))

    def test_trash_restore_and_purge_touch_only_logical_owned_entry(self):
        outside = self.root / 'unrelated.png'
        self.image.save(outside)
        original = outside.read_bytes()
        with self.virtualized():
            record = self.store.save(self.image)
            self.store.trash(record.id)
            self.store.restore(record.id)
            self.store.trash(record.id)
            self.assertEqual(self.store.purge(record.id), 1)
            self.assertFalse(record.path.exists())
            self.assertEqual(self.count(), 0)
        self.assertEqual(outside.read_bytes(), original)

    def test_resolve_outside_without_real_identity_proof_fails_closed(self):
        with self.virtualized(same_identity=False):
            with self.assertRaises(OSError):
                self.store.save(self.image)
            self.assertEqual(self.count(), 0)
            self.assertEqual(self.store.recover_orphans(), 0)
        self.assertEqual(len(list(self.inbox.glob('*.png'))), 1)

    def test_existing_outside_file_with_other_identity_is_not_an_alias(self):
        record = self.store.save(self.image)
        self.alias.mkdir(parents=True)
        self.image.save(self.alias / record.path.name)
        with self.virtualized(same_identity=False):
            with self.assertRaises(ValueError):
                self.store.get(record.id)
            self.assertEqual(self.count(), 1)

    def test_alias_with_unknown_identity_is_rejected(self):
        record = self.store.save(self.image)
        def no_identity(value):
            fields = {name: getattr(value, name) for name in dir(value) if name.startswith('st_')}
            return SimpleNamespace(**{**fields, 'st_ino': 0})
        with self.virtualized(target_transform=no_identity):
            with self.assertRaises(ValueError):
                self.store.get(record.id)

    def test_alias_target_reparse_attribute_is_rejected(self):
        record = self.store.save(self.image)
        def reparse(value):
            fields = {name: getattr(value, name) for name in dir(value) if name.startswith('st_')}
            return SimpleNamespace(**{**fields, 'st_file_attributes': 0x400})
        with self.virtualized(target_transform=reparse):
            with self.assertRaises(ValueError):
                self.store.get(record.id)

    def test_changed_alias_target_during_validation_is_rejected(self):
        record = self.store.save(self.image)
        def changed(value):
            fields = {name: getattr(value, name) for name in dir(value) if name.startswith('st_')}
            return SimpleNamespace(**{**fields, 'st_mtime_ns': value.st_mtime_ns + 1})
        with self.virtualized(target_transform=changed):
            with self.assertRaises(ValueError):
                self.store.get(record.id)

    def test_hardlink_to_external_file_is_never_loaded_recovered_or_purged(self):
        record = self.store.save(self.image)
        outside = self.root / 'hardlinked.png'
        os.link(record.path, outside)
        with self.assertRaises(ValueError):
            self.store.get(record.id)
        with self.assertRaises(ValueError):
            self.store.trash(record.id)
        self.assertTrue(outside.exists())
        self.assertTrue(record.path.exists())
        self.assertIsNone(self.store.safe_get(record.id)['record'])

    def test_symlink_and_junction_markers_are_rejected(self):
        record = self.store.save(self.image)
        for attribute in ('is_symlink', 'is_junction'):
            if not hasattr(Path, attribute):
                continue
            original = getattr(Path, attribute)
            with self.subTest(attribute=attribute), patch.object(Path, attribute, autospec=True,
                    side_effect=lambda path: path == record.path or original(path)):
                with self.assertRaises(ValueError):
                    self.store.get(record.id)

    def test_unsafe_names_and_alternate_data_streams_are_rejected(self):
        for name in ('../outside.png', '..\\outside.png', '/outside.png', 'C:outside.png',
                     'capture.png:stream', 'capture.png ', 'capture.png.', 'NUL', 'CON.png'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.store._owned_path(name)

    def test_root_replacement_during_validation_is_rejected(self):
        record = self.store.save(self.image)
        real_lstat = Path.lstat
        calls = 0
        def changed_root(path):
            nonlocal calls
            value = real_lstat(path)
            if path == self.inbox:
                calls += 1
                if calls >= 3:
                    fields = {name: getattr(value, name) for name in dir(value) if name.startswith('st_')}
                    return SimpleNamespace(**{**fields, 'st_ino': value.st_ino + 1})
            return value
        with patch.object(Path, 'lstat', autospec=True, side_effect=changed_root):
            with self.assertRaises(ValueError):
                self.store.get(record.id)

    def test_root_junction_replacement_before_resolution_is_rejected(self):
        outside = self.root / 'outside'
        outside.mkdir()
        real_resolve, real_check = Path.resolve, Path.is_junction
        with patch.object(Path, 'resolve', autospec=True,
                side_effect=lambda path, strict=False: outside if path == self.inbox else real_resolve(path, strict=strict)), patch.object(
                Path, 'is_junction', autospec=True, side_effect=lambda path: path == self.inbox or real_check(path)):
            with self.assertRaises(ValueError):
                self.store._owned_path('new.png')
        self.assertEqual(list(outside.iterdir()), [])

    def test_ancestor_junction_replacement_before_resolution_is_rejected(self):
        outside = self.root / 'outside'
        outside.mkdir()
        real_resolve, real_check = Path.resolve, Path.is_junction
        with patch.object(Path, 'resolve', autospec=True,
                side_effect=lambda path, strict=False: outside if path == self.inbox else real_resolve(path, strict=strict)), patch.object(
                Path, 'is_junction', autospec=True, side_effect=lambda path: path == self.inbox.parent or real_check(path)):
            with self.assertRaises(ValueError):
                self.store._owned_path('new.png')
        self.assertEqual(list(outside.iterdir()), [])

    def test_resolved_root_with_other_directory_identity_is_rejected(self):
        outside = self.root / 'outside'
        outside.mkdir()
        real_resolve = Path.resolve
        with patch.object(Path, 'resolve', autospec=True,
                side_effect=lambda path, strict=False: outside if path == self.inbox else real_resolve(path, strict=strict)):
            with self.assertRaises(ValueError):
                self.store._owned_path('new.png')


class CaptureSafeRowsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = CaptureStore(Path(self.temp.name) / 'capture_inbox')
        with Image.new('RGB', (8, 6), 'white') as image:
            self.healthy = self.store.save(image)
            self.bad = self.store.save(image)
        self.store.update_ocr(self.bad.id, 'synthetic raw text')
        self.store.save_verified_text(self.bad.id, '')
        self.store.link_to_task([self.bad.id, self.healthy.id], 'synthetic-task')
        with closing(sqlite3.connect(self.store.db_path)) as db, db:
            db.execute('UPDATE capture_items SET storage_name=? WHERE id=?', ('../outside.png', self.bad.id))

    def test_safe_get_preserves_empty_verified_text_but_exposes_no_path(self):
        entry = self.store.safe_get(self.bad.id)
        self.assertIsNone(entry['record'])
        self.assertTrue(entry['recovery_required'])
        self.assertEqual(entry['ocr_text'], 'synthetic raw text')
        self.assertEqual(entry['verified_text'], '')
        self.assertIsNotNone(entry['verified_at'])
        self.assertNotIn('outside', entry['warning'])
        self.assertNotIn('path', entry)
        with self.assertRaises(ValueError):
            self.store.get(self.bad.id)

    def test_safe_search_keeps_healthy_and_invalid_entries_with_link_order(self):
        entries = self.store.safe_search(task_id='synthetic-task')
        self.assertEqual([entry['id'] for entry in entries], [self.bad.id, self.healthy.id])
        self.assertIsNone(entries[0]['record'])
        self.assertEqual(entries[1]['record'].id, self.healthy.id)
        self.assertFalse(entries[1]['recovery_required'])
        with self.assertRaises(ValueError):
            self.store.search()

    def test_safe_search_keeps_search_and_trash_filters(self):
        entries = self.store.safe_search('synthetic raw')
        self.assertEqual([entry['id'] for entry in entries], [self.bad.id])
        self.assertEqual(self.store.safe_search(link_filter='unclassified', task_id='synthetic-task'), [])
        self.assertEqual(self.store.safe_search(trashed=True), [])
        self.assertIsNone(self.store.safe_get('missing'))

    def test_safe_rows_do_not_hide_database_errors(self):
        with patch.object(self.store, '_record_from_row', side_effect=sqlite3.OperationalError('synthetic DB failure')):
            with self.assertRaises(sqlite3.OperationalError):
                self.store.safe_get(self.healthy.id)
            with self.assertRaises(sqlite3.OperationalError):
                self.store.safe_search()

    def test_safe_rows_flag_missing_png_without_deleting_metadata_or_text(self):
        self.store.update_ocr(self.healthy.id, 'preserved synthetic OCR')
        self.healthy.path.unlink()
        entry = self.store.safe_get(self.healthy.id)
        self.assertIsNone(entry['record'])
        self.assertTrue(entry['recovery_required'])
        self.assertEqual(entry['ocr_text'], 'preserved synthetic OCR')
        # Strict metadata access stays compatible for repair/purge operations.
        self.assertEqual(self.store.get(self.healthy.id).id, self.healthy.id)
        self.assertEqual(len(self.store.safe_search()), 2)


if __name__ == '__main__':
    unittest.main()
