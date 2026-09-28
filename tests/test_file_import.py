"""All advertised file extensions, with synthetic files and no API access."""
from contextlib import closing
from datetime import datetime
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from PIL import Image
from services.document_library import DocumentLibrary
from services.document_service import SUPPORTED_FILE_EXTENSIONS, IMAGE_EXTENSIONS, read_text_file
from services.file_import import import_local_document, prepare_pdf
from services.office_reader import read_office
from tools.file_fixtures import create_supported_fixtures


class FileImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = tempfile.TemporaryDirectory()
        cls.files = create_supported_fixtures(cls.fixtures.name)

    @classmethod
    def tearDownClass(cls):
        cls.fixtures.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.library = DocumentLibrary(self.root / 'store')

    def test_pdf_keeps_real_page_images_local_text_and_scan_for_opt_in_ocr(self):
        doc, unread = import_local_document(self.library, self.files['.pdf'])
        pages = self.library.pages(doc['id'])
        self.assertEqual((len(pages), unread), (2, 1))
        self.assertIn('독서교실', pages[0]['text'])
        self.assertIn('2026. 10. 2. 16:00', pages[0]['text'])
        self.assertIn('2026. 10. 16. 14:00', pages[0]['text'])
        self.assertEqual(pages[1]['text'], '')
        for page in pages:
            with Image.open(page['path']) as image:
                self.assertLessEqual(max(image.size), 2400)
                self.assertGreater(min(image.size), 1000)

    def test_hwpx_table_and_embedded_image_are_both_available(self):
        doc, unread = import_local_document(self.library, self.files['.hwpx'])
        pages = self.library.pages(doc['id'])
        self.assertEqual((len(pages), unread), (2, 1))
        self.assertIn('독서교실\t2026. 10. 2. 16:00\t2026. 10. 16. 14:00', pages[0]['text'])
        self.assertIsNone(pages[0]['path'])
        self.assertTrue(Path(pages[1]['path']).is_file())

    def test_pages_and_edits_survive_reopen_without_pdf_source(self):
        source = self.root / 'copy.pdf'
        source.write_bytes(self.files['.pdf'].read_bytes())
        doc, _ = import_local_document(self.library, source)
        page = self.library.pages(doc['id'])[0]
        self.library.save_page_text(page['id'], '교사 수정본', page['updated_at'])
        source.unlink()
        reopened = DocumentLibrary(self.library.app_data_dir)
        pages = reopened.pages(doc['id'])
        self.assertEqual(pages[0]['text'], '교사 수정본')
        self.assertTrue(all(Path(page['path']).is_file() for page in pages))

    def test_invalid_and_encrypted_pdf_do_not_create_documents(self):
        from reportlab.pdfgen.canvas import Canvas
        damaged = self.root / 'damaged.pdf'
        damaged.write_bytes(b'%PDF-broken')
        encrypted = self.root / 'encrypted.pdf'
        canvas = Canvas(str(encrypted), encrypt='synthetic-password')
        canvas.drawString(20, 20, 'SYNTHETIC')
        canvas.save()
        for path in (damaged, encrypted):
            with self.subTest(path=path.name), self.assertRaises(ValueError):
                import_local_document(self.library, path)
        self.assertEqual(self.library.list_documents(), [])

    def test_large_page_count_rejected_before_render(self):
        from reportlab.pdfgen.canvas import Canvas
        path = self.root / 'long.pdf'
        canvas = Canvas(str(path))
        for _ in range(101):
            canvas.drawString(20, 20, 'SYNTHETIC')
            canvas.showPage()
        canvas.save()
        with self.assertRaises(ValueError):
            import_local_document(self.library, path)
        self.assertEqual(self.library.list_documents(), [])

    def test_text_utf8_cp949_and_utf16(self):
        for encoding in ('utf-8-sig', 'cp949', 'utf-16'):
            path = self.root / (encoding + '.txt')
            path.write_text('한글\t2026. 10. 2.', encoding=encoding)
            self.assertEqual(read_text_file(path), '한글\t2026. 10. 2.')

    def test_xlsx_dates_percent_formula_and_blank_cells(self):
        from openpyxl import Workbook
        path = self.root / 'cells.xlsx'
        book = Workbook()
        sheet = book.active
        sheet.append([datetime(2026, 10, 2, 16), None, 0.25, '=1+1'])
        sheet['C1'].number_format = '0%'
        hidden = book.create_sheet('hidden')
        hidden.sheet_state = 'hidden'
        hidden.append(['SYNTHETIC_HIDDEN'])
        book.save(path)
        book.close()
        entries = read_office(path)
        self.assertEqual(len(entries), 1)
        self.assertIn('2026-10-02 16:00:00\t\t25%\t[계산 결과 없음: =1+1]', entries[0]['text'])

    def test_failed_page_link_does_not_publish_partial_managed_document(self):
        original = self.library._add_capture
        count = 0
        def fail_second(*args):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError('synthetic disk failure')
            return original(*args)
        with patch.object(self.library, '_add_capture', side_effect=fail_second), self.assertRaises(OSError):
            import_local_document(self.library, self.files['.pdf'])
        with closing(sqlite3.connect(self.library.path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM documents').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM pages').fetchone()[0], 0)
        self.assertTrue(list(self.library.capture_store.directory.glob('*.png')))

    def test_all_advertised_extensions_open_through_file_dialog_without_api(self):
        from ui.capture_desk import CaptureDeskApp
        self.assertEqual(set(self.files), SUPPORTED_FILE_EXTENSIONS)
        app = CaptureDeskApp(library=self.library)
        app.attributes('-alpha', 0)
        try:
            with patch.object(app, '_start_job') as jobs, patch.object(app, '_choose_transfer') as transfer:
                for extension, path in self.files.items():
                    with self.subTest(extension=extension), patch('ui.capture_desk.filedialog.askopenfilename', return_value=str(path)):
                        previous = app.document['id'] if app.document else None
                        app.open_file()
                        self.assertIsNotNone(app.document)
                        self.assertNotEqual(app.document['id'], previous)
                        self.assertGreater(len(app.page_records), 0)
                        if extension not in IMAGE_EXTENSIONS:
                            self.assertIn('합성', self.library.document_text(app.document['id']))
                        if extension in IMAGE_EXTENSIONS or extension == '.pdf':
                            app.update()
                            self.assertIsNotNone(app.image_view._photo)
                jobs.assert_not_called()
                transfer.assert_not_called()
        finally:
            app.close()

    def test_old_binary_formats_are_not_advertised_and_explain_conversion(self):
        from ui.capture_desk import CaptureDeskApp
        app = CaptureDeskApp(library=self.library)
        app.attributes('-alpha', 0)
        try:
            for suffix in ('.hwp', '.doc', '.ppt', '.xls'):
                self.assertNotIn(suffix, SUPPORTED_FILE_EXTENSIONS)
                app.import_file(self.root / ('old' + suffix))
                self.assertIn('직접 읽지 않습니다', app.status.get())
            self.assertEqual(self.library.list_documents(), [])
        finally:
            app.close()
