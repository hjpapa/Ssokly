"""Storage changes use synthetic images and isolated desktops only."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from services.capture_location import CaptureLocation
from services.capture_store import CaptureStore
from services.document_library import DocumentLibrary


class CaptureLocationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.location = CaptureLocation(self.root / 'appdata')
        self.desktop = self.root / 'redirected-desktop'
        mocked = patch('services.capture_location.desktop_directory', return_value=self.desktop)
        mocked.start()
        self.addCleanup(mocked.stop)

    def source(self):
        store = CaptureStore(self.location.app_data_dir / 'capture_inbox')
        with Image.new('RGB', (60, 90), 'teal') as image:
            self.record = store.save(image)
        return store

    def test_first_launch_uses_shell_desktop_and_keeps_saved_location(self):
        directory = self.location.resolve()
        self.assertEqual(directory, self.desktop / 'ssokly')
        self.assertTrue(directory.is_dir())
        with patch('services.capture_location.desktop_directory', side_effect=AssertionError('saved preference required')):
            self.assertEqual(CaptureLocation(self.location.app_data_dir).resolve(), directory)

    def test_existing_installation_keeps_legacy_store(self):
        store = self.source()
        self.assertEqual(self.location.resolve(), store.directory)
        self.assertFalse(self.desktop.exists())

    def test_unavailable_or_invalid_preference_never_creates_empty_fallback(self):
        self.location.save(self.root / 'disconnected')
        with self.assertRaises(OSError):
            self.location.resolve()
        self.location.path.write_text('{broken', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.location.resolve()
        self.assertFalse(self.desktop.exists())

    def test_nonempty_desktop_folder_is_not_mixed_with_other_files(self):
        destination = self.desktop / 'ssokly'
        destination.mkdir(parents=True)
        marker = destination / 'unrelated.txt'
        marker.write_text('keep', encoding='utf-8')
        with self.assertRaises(FileExistsError):
            self.location.resolve()
        self.assertEqual(marker.read_text(encoding='utf-8'), 'keep')
        self.assertFalse(self.location.path.exists())

    def test_relocation_preserves_document_links_text_labels_trash_and_source(self):
        store = self.source()
        library = DocumentLibrary(self.location.app_data_dir, capture_store=store)
        doc = library.create_document('합성 문서')
        page = library.add_capture(doc['id'], self.record.id)
        library.remember_initial_ocr(page['id'], '최초 OCR')
        library.save_page_text(page['id'], '수정한 본문', page['updated_at'])
        library.update_details(doc['id'], ['합성 라벨'], '메모')
        library.set_page_trash(page['id'], True, expected_updated_at=library.get_document(doc['id'])['updated_at'])
        target = self.root / 'chosen-folder'
        target.mkdir()
        self.assertTrue(self.location.relocate(store, target))
        self.assertTrue(self.record.path.exists())
        reopened = DocumentLibrary(self.location.app_data_dir, capture_store=CaptureStore(self.location.resolve()))
        self.assertEqual(reopened.get_document(doc['id'])['labels'], ['합성 라벨'])
        self.assertEqual(reopened.get_document(doc['id'])['memo'], '메모')
        self.assertEqual(reopened.initial_ocr(page['id']), '최초 OCR')
        self.assertEqual(reopened.deleted_pages()[0]['text'], '수정한 본문')
        reopened.restore(doc['id'])
        restored = reopened.pages(doc['id'])[0]
        self.assertEqual(Path(restored['path']).parent, target)
        with Image.open(restored['path']) as image:
            self.assertEqual(image.size, (60, 90))

    def test_nonempty_nested_and_same_locations(self):
        store = self.source()
        self.assertFalse(self.location.relocate(store, store.directory))
        occupied = self.root / 'occupied'
        occupied.mkdir()
        (occupied / 'keep.txt').write_text('keep', encoding='utf-8')
        for path in (occupied, store.directory / 'child', store.directory.parent):
            with self.subTest(path=path.name), self.assertRaises(ValueError):
                self.location.relocate(store, path)
        self.assertTrue(self.record.path.exists())

    def test_copy_failure_keeps_old_preference_and_removes_only_temporary_copy(self):
        store = self.source()
        self.location.save(store.directory)
        target = self.root / 'target'
        with patch('services.capture_location.shutil.copyfile', side_effect=OSError('synthetic disk full')):
            with self.assertRaises(OSError):
                self.location.relocate(store, target)
        self.assertEqual(self.location.resolve(), store.directory)
        self.assertFalse(target.exists())
        self.assertEqual(list(self.root.glob('.ssokly-copy-*')), [])

    def test_settings_failure_preserves_original_and_verified_copy(self):
        store = self.source()
        self.location.save(store.directory)
        target = self.root / 'target'
        with patch.object(self.location, 'save', side_effect=OSError('synthetic permission failure')):
            with self.assertRaises(OSError):
                self.location.relocate(store, target)
        self.assertEqual(self.location.resolve(), store.directory)
        self.assertEqual((target / self.record.path.name).read_bytes(), self.record.path.read_bytes())

    def test_damaged_capture_does_not_publish_new_location(self):
        store = self.source()
        self.record.path.write_bytes(b'damaged')
        with self.assertRaises(ValueError):
            self.location.relocate(store, self.root / 'target')
        self.assertFalse(self.location.path.exists())

    def test_failed_atomic_settings_write_keeps_previous_value(self):
        self.location.save(self.root)
        original = self.location.path.read_bytes()
        with patch('services.capture_location.os.replace', side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):
                self.location.save(self.root / 'new')
        self.assertEqual(self.location.path.read_bytes(), original)
        self.assertEqual(list(self.location.app_data_dir.glob('*.tmp')), [])


class CaptureLocationUiTests(unittest.TestCase):
    def test_default_app_bootstraps_desktop_and_reopens_saved_capture(self):
        from ui.capture_desk import CaptureDeskApp
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch('ui.capture_desk.default_app_data_dir', return_value=root / 'appdata'), \
                    patch('services.capture_location.desktop_directory', return_value=root / 'desktop'):
                app = CaptureDeskApp()
                app.attributes('-alpha', 0)
                try:
                    self.assertEqual(app.library.capture_store.directory, root / 'desktop' / 'ssokly')
                    with Image.new('RGB', (90, 60), 'white') as image:
                        page = app.accept_capture(image, auto_read=False)
                    self.assertIsNotNone(page)
                finally:
                    app.close()
                reopened = CaptureDeskApp()
                reopened.attributes('-alpha', 0)
                try:
                    self.assertTrue(reopened.open_document(page['document_id']))
                    self.assertTrue(Path(reopened.page['path']).is_file())
                finally:
                    reopened.close()

    def test_change_flushes_edits_closes_and_reopens_with_same_documents(self):
        from ui.capture_desk import CaptureDeskApp
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            location = CaptureLocation(root / 'appdata')
            library = DocumentLibrary(location.app_data_dir)
            app = CaptureDeskApp(library=library)
            app.attributes('-alpha', 0)
            app._capture_location = location
            try:
                with Image.new('RGB', (90, 60), 'white') as image:
                    page = app.accept_capture(image, auto_read=False)
                app.source_editor.insert('1.0', '종료 시 저장')
                app.source_editor.edit_modified(True)
                app._source_modified()
                app.labels_var.set('폴더 변경')
                app.memo_var.set('유지할 메모')
                target = root / 'new-folder'
                with patch('ui.capture_desk.filedialog.askdirectory', return_value=str(target)), \
                        patch('ui.capture_desk.messagebox.askyesno', return_value=True):
                    self.assertTrue(app.change_capture_location())
                self.assertTrue(app._closing)
                reopened = DocumentLibrary(location.app_data_dir, capture_store=CaptureStore(location.resolve()))
                self.assertEqual(reopened.pages(page['document_id'])[0]['text'], '종료 시 저장')
                self.assertEqual(reopened.get_document(page['document_id'])['labels'], ['폴더 변경'])
                self.assertEqual(reopened.get_document(page['document_id'])['memo'], '유지할 메모')
            finally:
                if not app._closing:
                    app.close()
