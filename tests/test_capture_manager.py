"""Synthetic fixtures for image organization, without user data or API calls."""
import sqlite3
import tempfile
import tkinter as tk
from pathlib import Path
from tkinter import ttk
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from PIL import Image

from services.document_library import DocumentLibrary, LibraryConflictError
from services.transfer_policy import TransferPolicyStore, ScopeExpansionRequired, make_text_snapshot
from ui.capture_desk import CaptureDeskApp
from ui.capture_manager import CaptureManager


class CaptureFixtures:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.library = DocumentLibrary(self.temp.name)
        self.source = self.library.create_document('Source')
        self.target = self.library.create_document('Target')
        self.pages = []
        for color in ('white', 'blue'):
            with Image.new('RGB', (60, 40), color) as image:
                capture = self.library.capture_store.save(image)
            self.pages.append(self.library.add_capture(self.source['id'], capture.id))

    def versions(self):
        return {d['id']: self.library.get_document(d['id'])['updated_at'] for d in (self.source, self.target)}


class CaptureOrganizationTests(CaptureFixtures, unittest.TestCase):
    def test_batch_trash_restore_keeps_original_images_and_labels(self):
        self.library.update_details(self.source['id'], ['tag'], 'memo')
        self.library.trash_pages([p['id'] for p in self.pages], expected_versions=self.versions())
        self.assertEqual(self.library.capture_pages(), [])
        self.assertTrue(self.library.get_document(self.source['id'])['trashed'])
        self.library.restore(self.source['id'])
        self.assertEqual(len(self.library.capture_pages()), 2)
        self.assertTrue(all(Path(p['path']).exists() for p in self.pages))
        self.assertEqual(self.library.get_document(self.source['id'])['labels'], ['tag'])

    def test_move_preserves_ids_text_initial_ocr_and_source_results(self):
        page = self.pages[0]
        policies = TransferPolicyStore(self.temp.name)
        policies.save('document:' + self.source['id'], make_text_snapshot('[hidden]', excluded_strings=['SYNTHETIC_SECRET']).policy)
        self.library.remember_initial_ocr(page['id'], 'OCR')
        self.library.save_page_text(page['id'], 'edited', page['updated_at'])
        self.library.save_output(self.source['id'], 'summary', 'result', 'hash')
        self.library.move_pages([p['id'] for p in reversed(self.pages)], self.target['id'], expected_versions=self.versions())
        self.assertEqual([p['id'] for p in self.library.pages(self.target['id'])], [p['id'] for p in reversed(self.pages)])
        self.assertEqual(self.library.initial_ocr(page['id']), 'OCR')
        self.assertEqual(self.library.pages(self.target['id'])[1]['text'], 'edited')
        self.assertTrue(self.library.get_document(self.source['id'])['trashed'])
        self.assertEqual(self.library.outputs(self.source['id'])[0]['text'], 'result')
        with self.assertRaises(ScopeExpansionRequired):
            policies.get('document:' + self.target['id']).guard_text('SYNTHETIC_SECRET', strict=False)

    def test_stale_batch_and_duplicate_capture_fail_without_partial_move(self):
        old = self.versions()
        self.library.rename(self.source['id'], 'Changed')
        with self.assertRaises(LibraryConflictError):
            self.library.trash_pages([p['id'] for p in self.pages], expected_versions=old)
        self.library.add_capture(self.target['id'], self.pages[1]['capture_id'])
        with self.assertRaises(ValueError):
            self.library.move_pages([p['id'] for p in self.pages], self.target['id'], expected_versions=self.versions())
        self.assertEqual(len(self.library.pages(self.source['id'])), 2)

    def test_batch_database_failure_rolls_back_every_page(self):
        with sqlite3.connect(self.library.path) as db:
            db.execute("CREATE TRIGGER fail_second BEFORE UPDATE ON pages WHEN NEW.id='" + self.pages[1]['id'] + "' BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.library.trash_pages([p['id'] for p in self.pages], expected_versions=self.versions())
        self.assertEqual(len(self.library.pages(self.source['id'])), 2)
        self.assertFalse(self.library.get_document(self.source['id'])['trashed'])

    def test_rename_is_document_local_and_survives_reopen(self):
        page = self.pages[0]
        sibling = self.library.add_capture(self.target['id'], page['capture_id'])
        self.library.rename_page(page['id'], 'Custom name', expected_updated_at=self.versions()[self.source['id']])
        reopened = DocumentLibrary(self.temp.name)
        self.assertEqual(reopened.pages(self.source['id'])[0]['source_name'], 'Custom name')
        self.assertEqual(reopened.pages(self.target['id'])[0]['source_name'], sibling['source_name'])
        self.assertTrue(Path(page['path']).exists())

    def test_multi_document_batch_validates_all_versions_before_deleting(self):
        other = self.library.add_capture(self.target['id'], self.pages[0]['capture_id'])
        versions = self.versions()
        self.library.rename(self.target['id'], 'New target')
        ids = [self.pages[0]['id'], other['id']]
        with self.assertRaises(LibraryConflictError):
            self.library.trash_pages(ids, expected_versions=versions)
        self.assertEqual(len(self.library.deleted_pages()), 0)
        self.library.trash_pages(ids, expected_versions=self.versions())
        self.assertEqual(len(self.library.deleted_pages()), 2)
        self.assertFalse(self.library.get_document(self.source['id'])['trashed'])
        self.assertTrue(self.library.get_document(self.target['id'])['trashed'])


class CaptureManagerUiTests(CaptureFixtures, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.app = CaptureDeskApp(library=self.library)
        self.old_scaling = self.app.tk.call('tk', 'scaling')
        self.app.attributes('-alpha', 0)
        self.app.open_document(self.source['id'])
        self.manager = CaptureManager(self.app)
        self.manager.attributes('-alpha', 0)
        self.app.update()
        self.addCleanup(self.destroy)

    def destroy(self):
        self.app._closing = True
        # Let preview widgets cancel their own callbacks before root cleanup.
        if self.manager.winfo_exists():
            self.manager.destroy()
        for callback in self.app.tk.call('after', 'info'):
            self.app.after_cancel(callback)
        self.app.tk.call('tk', 'scaling', self.old_scaling)
        self.app.destroy()

    def widgets(self, parent):
        for child in parent.winfo_children():
            yield child
            yield from self.widgets(child)

    def button(self, parent, text):
        return next(w for w in self.widgets(parent) if isinstance(w, ttk.Button) and w.cget('text') == text)

    def test_preview_search_filter_rename_and_bulk_delete(self):
        self.manager.tree.selection_set(self.pages[0]['id'])
        self.app.update()
        self.assertEqual(self.manager.preview.image_size, (60, 40))
        with patch('ui.capture_manager.simpledialog.askstring', return_value='Named capture'):
            self.manager.rename()
        self.manager.query.set('Named capture')
        self.manager.render()
        self.assertEqual(self.manager.tree.get_children(), (self.pages[0]['id'],))
        self.manager.query.set('')
        self.manager.render()
        self.manager.tree.selection_set(self.manager.tree.get_children())
        with patch('ui.capture_manager.messagebox.askyesno', return_value=False):
            self.manager.trash()
        self.assertEqual(len(self.library.capture_pages()), 2)
        with patch('ui.capture_manager.messagebox.askyesno', return_value=True):
            self.manager.trash()
        self.assertIsNone(self.app.document)
        self.assertEqual(self.manager.tree.get_children(), ())

    def test_metadata_dialog_and_move_button(self):
        self.manager.tree.selection_set(self.pages[0]['id'])
        self.manager.edit_details()
        dialog = next(w for w in self.manager.winfo_children() if isinstance(w, tk.Toplevel))
        entries = [w for w in self.widgets(dialog) if isinstance(w, ttk.Entry)]
        entries[0].insert(0, 'Label')
        entries[1].insert(0, 'Memo')
        self.button(dialog, '저장').invoke()
        self.assertEqual(self.library.get_document(self.source['id'])['labels'], ['Label'])
        self.assertEqual(self.app.memo_var.get(), 'Memo')
        self.manager.query.set('Memo')
        self.manager.label.set('Label')
        self.manager.render()
        self.assertEqual(len(self.manager.tree.get_children()), 2)
        self.manager.tree.selection_set(self.pages[0]['id'])
        self.manager.move()
        dialog = next(w for w in self.manager.winfo_children() if isinstance(w, tk.Toplevel))
        tree = next(w for w in self.widgets(dialog) if isinstance(w, ttk.Treeview))
        tree.selection_set(self.target['id'])
        self.button(dialog, '선택 문서로 이동').invoke()
        self.assertEqual(self.library.pages(self.target['id'])[0]['id'], self.pages[0]['id'])
        self.assertEqual(len(self.library.pages(self.source['id'])), 1)

    def test_open_selected_page_and_narrow_window_controls(self):
        self.manager.geometry('660x480')
        self.app.update()
        self.assertTrue(self.manager.tree.winfo_ismapped())
        self.assertGreater(self.manager.preview.canvas.winfo_height(), 60)
        self.manager.tree.selection_set(self.pages[1]['id'])
        self.manager.open_selected()
        self.assertEqual(self.app.page['id'], self.pages[1]['id'])

    def test_high_dpi_narrow_manager_keeps_action_buttons_visible(self):
        self.manager.destroy()
        self.app.tk.call('tk', 'scaling', 2.67)
        self.manager = CaptureManager(self.app)
        self.manager.attributes('-alpha', 0)
        self.manager.geometry('720x680')
        self.manager.toggle_filters()
        self.manager.multi.set(True)
        self.manager.toggle_multi()
        self.app.update()
        for text in ('검색', '새로고침', '표시된 항목 전체 선택', '전체 보기', '이동', '삭제'):
            button = self.button(self.manager, text)
            self.assertTrue(button.winfo_ismapped(), text)
            self.assertLessEqual(button.winfo_rootx() + button.winfo_width(), self.manager.winfo_rootx() + self.manager.winfo_width(), text)

    def test_simple_default_and_click_to_select_multiple(self):
        self.assertFalse(self.manager.filters.winfo_ismapped())
        self.assertFalse(self.manager.select_all.winfo_ismapped())
        self.assertFalse(self.manager.more_button.winfo_ismapped())
        self.assertTrue(self.manager.trash_button.instate(['disabled']))
        self.manager.multi.set(True)
        self.manager.toggle_multi()
        self.app.update()
        for page in self.pages:
            x, y, width, height = self.manager.tree.bbox(page['id'])
            self.manager.select_clicked(SimpleNamespace(y=y + 5, state=0))
        self.app.update()
        self.assertEqual(set(self.manager.tree.selection()), {p['id'] for p in self.pages})
        self.assertTrue(self.manager.edit_button.instate(['disabled']))
        self.assertFalse(self.manager.trash_button.instate(['disabled']))
        self.manager.multi.set(False)
        self.manager.toggle_multi()
        self.app.update()
        self.assertEqual(len(self.manager.tree.selection()), 1)
        self.assertFalse(self.manager.edit_button.instate(['disabled']))
        self.manager.toggle_filters()
        self.manager.sort.set('이름순')
        self.manager.render()
        self.manager.toggle_filters()
        self.assertIn('적용 중', self.manager.filter_button.cget('text'))
        self.manager.reset_filters()
        self.assertNotIn('적용 중', self.manager.filter_button.cget('text'))


if __name__ == '__main__':
    unittest.main()
