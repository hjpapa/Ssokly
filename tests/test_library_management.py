"""Page trash and label operations use synthetic images and temporary storage."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import patch

from PIL import Image

from services.document_library import DocumentLibrary, LibraryConflictError, LibraryReadOnlyError
from ui.capture_desk import CaptureDeskApp


class LibraryManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.library = DocumentLibrary(self.temp.name)
        self.doc = self.library.create_document('합성 문서')

    def token(self):
        return self.library.get_document(self.doc['id'])['updated_at']

    def test_delete_restore_preserves_image_edit_order_and_excludes_text_search(self):
        first = self.library.add_text_page(self.doc['id'], '앞장')
        with Image.new('RGB', (80, 60), 'white') as image:
            capture = self.library.capture_store.save(image)
        middle = self.library.add_capture(self.doc['id'], capture.id)
        self.library.save_page_text(middle['id'], '삭제검색고유문구', middle['updated_at'])
        last = self.library.add_text_page(self.doc['id'], '뒷장')
        self.library.set_page_trash(middle['id'], True, expected_updated_at=self.token())
        reopened = DocumentLibrary(self.temp.name)
        self.assertEqual(reopened.document_text(self.doc['id']), '앞장\n\n뒷장')
        self.assertEqual(reopened.list_documents('삭제검색고유문구'), [])
        self.assertEqual(len(reopened.list_documents()), 1)  # No orphan capture reappears.
        self.assertTrue(Path(capture.path).exists())
        self.assertEqual(reopened.deleted_pages()[0]['text'], '삭제검색고유문구')
        reopened.set_page_trash(middle['id'], False, expected_updated_at=self.token())
        self.assertEqual([p['id'] for p in reopened.pages(self.doc['id'])], [first['id'], middle['id'], last['id']])
        self.assertIn('삭제검색고유문구', reopened.document_text(self.doc['id']))

    def test_last_page_can_be_restored_and_trashed_page_rejects_late_edits(self):
        page = self.library.add_text_page(self.doc['id'], '원문')
        self.library.set_page_trash(page['id'], True, expected_updated_at=self.token())
        self.assertEqual(self.library.pages(self.doc['id']), [])
        self.assertEqual(self.library.get_document(self.doc['id'])['page_count'], 0)
        with self.assertRaises(LibraryReadOnlyError):
            self.library.save_page_text(page['id'], '늦은 수정', page['updated_at'])
        with self.assertRaises(LibraryReadOnlyError):
            self.library.update_page_ocr(page['id'], '늦은 OCR')
        self.library.set_page_trash(page['id'], False, expected_updated_at=self.token())
        self.assertEqual(self.library.document_text(self.doc['id']), '원문')

    def test_reorder_keeps_deleted_page_slot_for_restore(self):
        pages = [self.library.add_text_page(self.doc['id'], text) for text in ('첫째', '둘째', '셋째')]
        self.library.set_page_trash(pages[1]['id'], True, expected_updated_at=self.token())
        self.library.reorder_pages(self.doc['id'], [pages[2]['id'], pages[0]['id']])
        self.library.set_page_trash(pages[1]['id'], False, expected_updated_at=self.token())
        self.assertEqual(self.library.document_text(self.doc['id']), '셋째\n\n둘째\n\n첫째')

    def test_conflict_and_document_trash_protect_page_state(self):
        page = self.library.add_text_page(self.doc['id'], '원문')
        stale = self.token()
        self.library.rename(self.doc['id'], '새 제목')
        with self.assertRaises(LibraryConflictError):
            self.library.set_page_trash(page['id'], True, expected_updated_at=stale)
        self.library.set_page_trash(page['id'], True, expected_updated_at=self.token())
        self.library.trash(self.doc['id'])
        with self.assertRaises(LibraryReadOnlyError):
            self.library.set_page_trash(page['id'], False, expected_updated_at=self.token())
        self.library.restore(self.doc['id'])
        self.assertEqual(self.library.pages(self.doc['id']), [])
        self.library.set_page_trash(page['id'], False, expected_updated_at=self.token())

    def test_page_trash_write_failure_rolls_back(self):
        page = self.library.add_text_page(self.doc['id'], '원문')
        before = self.token()
        with closing(sqlite3.connect(self.library.path)) as db, db:
            db.execute("CREATE TRIGGER reject_trash BEFORE UPDATE ON pages BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.library.set_page_trash(page['id'], True, expected_updated_at=before)
        self.assertEqual(self.token(), before)
        self.assertEqual(self.library.document_text(self.doc['id']), '원문')

    def test_rename_label_merges_and_updates_trashed_documents_and_tokens(self):
        second = self.library.create_document('휴지통 문서')
        self.library.update_details(self.doc['id'], ['공문', '연수'], '유지할 메모')
        self.library.update_details(second['id'], ['공문'], '')
        self.library.trash(second['id'])
        before = self.token()
        self.assertEqual(self.library.rename_label('공문', '연수'), 2)
        self.assertEqual(self.library.label_counts(), {'연수': 2})
        self.assertEqual(self.library.get_document(self.doc['id'])['memo'], '유지할 메모')
        self.assertEqual(self.library.get_document(second['id'])['labels'], ['연수'])
        with self.assertRaises(LibraryConflictError):
            self.library.update_details(self.doc['id'], ['낡은 수정'], '', expected_updated_at=before)
        self.assertEqual(DocumentLibrary(self.temp.name).label_counts(), {'연수': 2})

    def test_invalid_label_and_failed_batch_leave_all_documents_unchanged(self):
        second = self.library.create_document('둘째')
        for doc in (self.doc, second):
            self.library.update_details(doc['id'], ['공문'], '')
        for name in ('', 'a,b', 'x' * 25):
            with self.assertRaises(ValueError):
                self.library.rename_label('공문', name)
        with closing(sqlite3.connect(self.library.path)) as db, db:
            db.execute("CREATE TRIGGER reject_label BEFORE UPDATE ON documents WHEN NEW.id='" + second['id'] + "' BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.library.rename_label('공문', '행사')
        self.assertEqual(self.library.label_counts(), {'공문': 2})

    def test_v3_migration_backs_up_and_preserves_pages_and_labels(self):
        self.library.add_text_page(self.doc['id'], '원문')
        self.library.update_details(self.doc['id'], ['공문'], '메모')
        with closing(sqlite3.connect(self.library.path)) as db, db:
            db.execute('ALTER TABLE pages DROP COLUMN trashed_at')
            db.execute('PRAGMA user_version=3')
        reopened = DocumentLibrary(self.temp.name)
        self.assertEqual(reopened.document_text(self.doc['id']), '원문')
        self.assertEqual(reopened.label_counts(), {'공문': 1})
        backups = list(Path(self.temp.name).glob('document_library.sqlite3.before-library-*.bak'))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 3)


class ManagementUiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.library = DocumentLibrary(self.temp.name)
        self.app = CaptureDeskApp(library=self.library)
        self.app.attributes('-alpha', 0)
        self.app.update()
        self.addCleanup(self.destroy)

    def destroy(self):
        self.app._closing = True
        for callback in self.app.tk.call('after', 'info'):
            self.app.after_cancel(callback)
        self.app.destroy()

    def widgets(self, parent):
        for child in parent.winfo_children():
            yield child
            yield from self.widgets(child)

    def dialog(self):
        return next(child for child in self.app.winfo_children() if isinstance(child, tk.Toplevel))

    def button(self, dialog, text):
        return next(w for w in self.widgets(dialog) if isinstance(w, ttk.Button) and w.cget('text') == text)

    def test_restore_dialog_restores_selected_page_and_opens_it(self):
        doc = self.library.create_document('복원 대상')
        page = self.library.add_text_page(doc['id'], '보존된 수정본')
        self.library.set_page_trash(page['id'], True, expected_updated_at=self.library.get_document(doc['id'])['updated_at'])
        self.app.show_deleted_pages()
        dialog = self.dialog()
        tree = next(w for w in self.widgets(dialog) if isinstance(w, ttk.Treeview))
        tree.selection_set(page['id'])
        self.button(dialog, '선택 페이지 복원').invoke()
        self.assertEqual(self.app.page['id'], page['id'])
        self.assertEqual(self.app.source_editor.get('1.0', 'end-1c'), '보존된 수정본')
        self.assertEqual(self.library.deleted_pages(), [])

    def test_label_manager_updates_documents_filter_and_current_editor(self):
        doc = self.library.create_document()
        self.library.update_details(doc['id'], ['공문'], '메모')
        self.app.open_document(doc['id'])
        self.app.label_filter.set('공문')
        self.app.manage_labels()
        dialog = self.dialog()
        tree = next(w for w in self.widgets(dialog) if isinstance(w, ttk.Treeview))
        tree.selection_set(tree.get_children()[0])
        with patch('ui.capture_desk.simpledialog.askstring', return_value='연수'):
            self.button(dialog, '선택 라벨 이름 변경').invoke()
        self.assertEqual(self.library.get_document(doc['id'])['labels'], ['연수'])
        self.assertEqual(self.app.labels_var.get(), '연수')
        self.assertEqual(self.app.label_filter.get(), '연수')
        self.assertEqual(self.app.document_tree.get_children(), (doc['id'],))

    def test_label_picker_can_remove_and_add_existing_labels(self):
        first = self.library.create_document()
        other = self.library.create_document()
        self.library.update_details(first['id'], ['공문'], '')
        self.library.update_details(other['id'], ['연수'], '')
        self.app.open_document(first['id'])
        self.app.choose_labels()
        dialog = self.dialog()
        listing = next(w for w in self.widgets(dialog) if isinstance(w, tk.Listbox))
        listing.selection_clear(0, 'end')
        listing.selection_set(list(listing.get(0, 'end')).index('연수'))
        self.button(dialog, '선택 적용').invoke()
        self.assertEqual(self.library.get_document(first['id'])['labels'], ['연수'])

    def test_delete_cancel_failure_and_success_only_remove_current_page(self):
        doc = self.library.create_document()
        first = self.library.add_text_page(doc['id'], '앞장')
        second = self.library.add_text_page(doc['id'], '뒷장')
        self.app.open_document(doc['id'])
        with patch('ui.capture_desk.messagebox.askyesno', return_value=False):
            self.assertFalse(self.app.delete_current_page())
        with patch.object(self.app, 'flush_edits', return_value=False):
            self.assertFalse(self.app.delete_current_page())
        self.assertEqual(len(self.library.pages(doc['id'])), 2)
        with patch('ui.capture_desk.messagebox.askyesno', return_value=True):
            self.assertTrue(self.app.delete_current_page())
        self.assertEqual(self.app.page['id'], second['id'])
        self.assertEqual(self.library.deleted_pages()[0]['id'], first['id'])
        self.assertTrue(self.app.image_view.canvas.bind('<Button-3>'))

    def test_reopening_image_document_in_narrow_window_displays_original(self):
        with Image.new('RGB', (900, 600), 'white') as image:
            page = self.app.accept_capture(image, auto_read=False)
        self.app.geometry('720x700')
        self.app.update()
        self.app._apply_layout()
        self.app.update()
        self.app.open_document(page['document_id'])
        self.app.update()
        self.assertTrue(self.app.image_view.canvas.winfo_ismapped())
        self.assertGreater(self.app.image_view.canvas.winfo_width(), 250)
        self.assertGreater(self.app.image_view.scale, 0.1)
        self.assertIsNotNone(self.app.image_view._photo)
        self.assertTrue(self.app.source_editor.winfo_ismapped())
        for panel, content in ((self.app.text_panel, self.app.source_editor),
                               (self.app.table_panel, self.app.table_view),
                               (self.app.ai_panel, self.app.output_editor)):
            self.app.editor_tabs.select(panel)
            self.app.update()
            self.assertTrue(self.app.image_view.canvas.winfo_ismapped())
            self.assertTrue(content.winfo_ismapped())

    def test_last_page_clears_workspace_and_individual_document_purge_confirms(self):
        doc = self.library.create_document('휴지통 대상')
        self.library.add_text_page(doc['id'], '원문')
        self.library.update_details(doc['id'], ['마지막 라벨'], '메모')
        self.app.open_document(doc['id'])
        self.app.label_filter.set('마지막 라벨')
        with patch('ui.capture_desk.messagebox.askyesno', return_value=True):
            self.assertTrue(self.app.delete_current_page())
        self.assertIsNone(self.app.document)
        self.assertEqual(self.app.labels_var.get(), '')
        self.assertEqual(self.app.label_filter.get(), '전체 라벨')
        self.assertEqual(self.app.document_tree.get_children(), ())
        self.app.show_trash.set(True)
        self.app.refresh_library()
        self.app.open_document(doc['id'])
        with patch('ui.capture_desk.messagebox.askyesno', return_value=False):
            self.assertFalse(self.app.purge_current_document())
        self.assertIsNotNone(self.library.get_document(doc['id']))
        with patch('ui.capture_desk.messagebox.askyesno', return_value=True):
            self.assertTrue(self.app.purge_current_document())
        self.assertIsNone(self.app.document)
        self.assertEqual(self.app.document_tree.get_children(), ())

    def test_page_purge_button_and_empty_trash_ignore_search_filter(self):
        doc = self.library.create_document('남길 문서')
        page = self.library.add_text_page(doc['id'], '삭제 쪽')
        self.library.add_text_page(doc['id'], '유지 쪽')
        self.library.set_page_trash(page['id'], True, expected_updated_at=self.library.get_document(doc['id'])['updated_at'])
        self.app.show_deleted_pages()
        dialog = self.dialog()
        tree = next(w for w in self.widgets(dialog) if isinstance(w, ttk.Treeview))
        tree.selection_set(page['id'])
        with patch('ui.capture_desk.messagebox.askyesno', return_value=True):
            self.button(dialog, '선택 페이지 영구 삭제').invoke()
        self.assertEqual(tree.get_children(), ())
        dialog.destroy()
        trash = self.library.create_document('검색에 숨겨진 휴지통')
        self.library.trash(trash['id'])
        self.app.query.set('찾을 수 없는 검색어')
        with patch('ui.capture_desk.messagebox.askyesno', return_value=True):
            self.assertTrue(self.app.empty_trash())
        self.assertIsNotNone(self.library.get_document(doc['id']))
        self.assertIsNone(self.library.get_document(trash['id']))

    def test_merge_and_split_dialogs_move_selected_pages(self):
        target = self.library.create_document('현재')
        source = self.library.create_document('가져올 자료')
        for doc, text in ((target, '현재 쪽'), (source, '옮길 쪽')):
            self.library.add_text_page(doc['id'], text)
            self.library.update_details(doc['id'], ['행사'], '분류 메모')
        self.app.open_document(target['id'])
        self.app.show_merge_dialog()
        dialog = self.dialog()
        listing = next(w for w in self.widgets(dialog) if isinstance(w, ttk.Treeview))
        listing.selection_set(source['id'])
        self.button(dialog, '선택 문서의 쪽 합치기').invoke()
        self.assertEqual(self.library.document_text(target['id']), '현재 쪽\n\n옮길 쪽')
        self.assertTrue(self.library.get_document(source['id'])['trashed'])
        self.app._load_page(1)
        moved_id = self.app.page['id']
        self.app.show_split_dialog()
        dialog = self.dialog()
        self.button(dialog, '선택한 쪽 분리').invoke()
        self.assertNotEqual(self.app.document['id'], target['id'])
        self.assertEqual(self.app.page['id'], moved_id)
        self.assertEqual(self.library.document_text(target['id']), '현재 쪽')
        self.assertEqual(self.app.document['labels'], ['행사'])
        self.assertEqual(self.app.document['memo'], '분류 메모')

    def test_cancel_organization_dialog_and_failed_save_do_not_move_pages(self):
        doc = self.library.create_document('취소')
        for text in ('첫째', '둘째'):
            self.library.add_text_page(doc['id'], text)
        self.app.open_document(doc['id'])
        with patch.object(self.app, 'flush_edits', return_value=False):
            self.app.show_split_dialog()
            self.assertFalse(any(isinstance(w, tk.Toplevel) for w in self.app.winfo_children()))
        self.app.show_split_dialog()
        self.button(self.dialog(), '취소').invoke()
        self.assertEqual(self.library.document_text(doc['id']), '첫째\n\n둘째')


if __name__ == '__main__':
    unittest.main()
