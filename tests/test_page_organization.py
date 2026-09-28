"""Page movement keeps data, review state and outbound restrictions intact."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from services.document_library import DocumentLibrary, LibraryConflictError
from services.transfer_policy import TransferPolicyStore, ScopeExpansionRequired, make_text_snapshot


class PageOrganizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.library = DocumentLibrary(self.temp.name)
        self.policies = TransferPolicyStore(self.temp.name)

    def document(self, title, texts=(), labels=(), memo=''):
        doc = self.library.create_document(title)
        for text in texts:
            self.library.add_text_page(doc['id'], text)
        return self.library.update_details(doc['id'], list(labels), memo)

    def versions(self, *docs):
        return {doc['id']: self.library.get_document(doc['id'])['updated_at'] for doc in docs}

    def test_candidates_match_shared_label_or_nonempty_memo_only(self):
        target = self.document('현재', ['현재'], ['공문', '연수'], '같은 메모')
        label = self.document('라벨', ['a'], ['연수'])
        memo = self.document('메모', ['b'], ['별도'], '같은 메모')
        self.document('무관', ['c'])
        empty = self.document('빈 문서', [], ['연수'])
        trashed = self.document('휴지통', ['d'], ['연수'])
        self.library.trash(trashed['id'])
        self.assertEqual({doc['id'] for doc in self.library.related_documents(target['id'])}, {label['id'], memo['id']})
        self.assertEqual({doc['id'] for doc in self.library.related_documents(empty['id'])}, {target['id'], label['id']})
        plain = self.document('빈 메모', ['e'])
        self.assertEqual(self.library.related_documents(plain['id']), [])

    def test_merge_order_deleted_pages_metadata_and_results_are_preserved(self):
        target = self.document('현재', ['첫째'], ['연수'], '현재 메모')
        source = self.document('가져올 문서', ['둘째', '셋째'], ['연수'], '원래 메모')
        deleted = self.library.add_text_page(source['id'], '삭제 보존')
        self.library.set_page_trash(deleted['id'], True, expected_updated_at=self.versions(source)[source['id']])
        self.library.save_output(source['id'], '요약', '과거 결과', 'old')
        before_ids = [p['id'] for p in self.library.pages(source['id'])]
        self.library.merge_documents(target['id'], [source['id']], expected_versions=self.versions(target, source))
        reopened = DocumentLibrary(self.temp.name)
        self.assertEqual(reopened.document_text(target['id']), '첫째\n\n둘째\n\n셋째')
        self.assertEqual([p['id'] for p in reopened.pages(target['id'])][1:], before_ids)
        self.assertTrue(reopened.get_document(source['id'])['trashed'])
        self.assertEqual(reopened.get_document(source['id'])['memo'], '원래 메모')
        self.assertEqual(reopened.get_document(target['id'])['memo'], '현재 메모')
        self.assertEqual(reopened.deleted_pages()[0]['id'], deleted['id'])
        self.assertEqual(reopened.outputs(source['id'])[0]['text'], '과거 결과')

    def test_split_capture_keeps_id_image_ocr_empty_edit_and_original_ocr(self):
        source = self.document('원문', ['남길 쪽'], ['공문'], '메모')
        with Image.new('RGB', (30, 20), 'red') as image:
            capture = self.library.capture_store.save(image)
        capture = self.library.capture_store.update_ocr(capture.id, '최초 OCR')
        page = self.library.add_capture(source['id'], capture.id)
        self.library.remember_initial_ocr(page['id'], '최초 OCR')
        edited = self.library.save_page_text(page['id'], '', page['updated_at'])
        target = self.library.split_pages(source['id'], [page['id']], '분리', expected_updated_at=self.versions(source)[source['id']])
        moved = self.library.pages(target['id'])[0]
        self.assertEqual(moved['id'], page['id'])
        self.assertEqual(moved['text'], '')
        self.assertEqual(moved['ocr_text'], '최초 OCR')
        self.assertEqual(self.library.initial_ocr(moved['id']), '최초 OCR')
        self.assertTrue(Path(moved['path']).exists())
        self.assertEqual(target['labels'], ['공문'])
        self.assertEqual(target['memo'], '메모')
        self.assertEqual(self.library.document_text(source['id']), '남길 쪽')
        with self.assertRaises(LibraryConflictError):
            self.library.save_page_text(page['id'], '이전 창의 수정', edited['updated_at'], expected_document_id=source['id'])

    def test_merge_and_split_retain_document_redaction(self):
        target = self.document('현재', ['현재'], ['공문'])
        source = self.document('합칠 문서', ['가린 원문'], ['공문'])
        previous = make_text_snapshot('공개 [가림]', excluded_strings=['SYNTHETIC_SECRET']).policy
        self.policies.save('document:' + source['id'], previous)
        self.library.merge_documents(target['id'], [source['id']], expected_versions=self.versions(target, source))
        moved = self.library.pages(target['id'])[1]
        split = self.library.split_pages(target['id'], [moved['id']], '분리', expected_updated_at=self.versions(target)[target['id']])
        for identity in (target['id'], split['id']):
            policy = self.policies.get('document:' + identity)
            self.assertTrue(policy.redacted)
            with self.assertRaises(ScopeExpansionRequired):
                policy.guard_text('SYNTHETIC_SECRET', strict=False)

    def test_stale_tokens_and_wrong_selection_do_not_move_pages(self):
        target = self.document('현재', ['현재'], ['공문'])
        source = self.document('합칠 문서', ['다른 쪽'], ['공문'])
        versions = self.versions(target, source)
        self.library.rename(source['id'], '수정한 제목')
        with self.assertRaises(LibraryConflictError):
            self.library.merge_documents(target['id'], [source['id']], expected_versions=versions)
        page = self.library.pages(source['id'])[0]
        for selected in ([], [page['id']], ['없는 쪽'], [page['id'], page['id']]):
            with self.assertRaises(ValueError):
                self.library.split_pages(source['id'], selected, '분리', expected_updated_at=self.versions(source)[source['id']])
        self.assertEqual(self.library.document_text(source['id']), '다른 쪽')

    def test_policy_or_commit_failure_rolls_back_moves(self):
        target = self.document('현재', ['현재'], ['공문'])
        source = self.document('합칠 문서', ['다른 쪽', '남길 쪽'], ['공문'])
        with patch('services.desk_transfer.DeskTransfer.inherit_document_policy', side_effect=OSError('synthetic')):
            with self.assertRaises(OSError):
                self.library.merge_documents(target['id'], [source['id']], expected_versions=self.versions(target, source))
            with self.assertRaises(OSError):
                self.library.split_pages(source['id'], [self.library.pages(source['id'])[0]['id']], '실패', expected_updated_at=self.versions(source)[source['id']])
        with closing(sqlite3.connect(self.library.path)) as db, db:
            db.execute("CREATE TRIGGER reject_move BEFORE UPDATE OF document_id ON pages BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.library.merge_documents(target['id'], [source['id']], expected_versions=self.versions(target, source))
        self.assertEqual(self.library.document_text(target['id']), '현재')
        self.assertEqual(self.library.document_text(source['id']), '다른 쪽\n\n남길 쪽')
        self.assertFalse(self.library.get_document(source['id'])['trashed'])

    def test_duplicate_capture_merge_is_rejected_without_loss(self):
        target = self.document('현재', [], ['공문'])
        source = self.document('합칠 문서', [], ['공문'])
        with Image.new('RGB', (20, 10)) as image:
            capture = self.library.capture_store.save(image)
        for doc in (target, source):
            self.library.add_capture(doc['id'], capture.id)
        with self.assertRaises(ValueError):
            self.library.merge_documents(target['id'], [source['id']], expected_versions=self.versions(target, source))
        self.assertEqual(len(self.library.pages(target['id'])), 1)
        self.assertEqual(len(self.library.pages(source['id'])), 1)


if __name__ == '__main__':
    unittest.main()
