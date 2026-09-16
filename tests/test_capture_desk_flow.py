"""Capture desk integration with temporary stores and zero live API requests."""
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image

from services.document_library import DocumentLibrary
from services.transfer_policy import ScopeExpansionRequired, TransferPolicy, TransferSnapshot, make_image_snapshot, make_text_snapshot
from ui.capture_desk import CaptureDeskApp, fingerprint


class CaptureDeskFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.library = DocumentLibrary(self.directory)
        self.app = CaptureDeskApp(library=self.library)
        self.app.withdraw()
        self.addCleanup(self.destroy_app)
        self.image = Image.new('RGB', (30, 20), (40, 80, 120))
        self.operations = {}
        self.start = patch.object(self.app, '_start_job', side_effect=self.start_job).start()
        self.addCleanup(patch.stopall)
        self.choose = patch.object(self.app, '_choose_transfer', return_value=None).start()
        self.ocr = patch('services.ocr_service.extract_text_from_image', return_value='합성 OCR 결과').start()
        self.file_ocr = patch('services.ocr_service.extract_text_from_file', return_value='합성 파일 OCR').start()
        self.ai = patch('services.text_actions.generate_text_action', return_value='합성 AI 결과').start()
        for name in ('showerror', 'showwarning', 'showinfo', 'askyesno'):
            patch('ui.capture_desk.messagebox.' + name, return_value=False).start()

    def destroy_app(self):
        if self.app is not None:
            self.app._closing = True
            for identifier in self.app.tk.call('after', 'info'):
                self.app.after_cancel(identifier)
            self.app.destroy()
            self.app = None

    def start_job(self, kind, operation, metadata):
        identifier = 'synthetic-' + str(len(self.operations))
        self.operations[identifier] = operation
        self.app._jobs[identifier] = {**metadata, 'kind': kind, 'cancel': threading.Event()}
        return identifier

    def latest(self):
        identifier = next(reversed(self.operations))
        return identifier, self.app._jobs[identifier]

    def finish(self, value=None, *, success=True):
        identifier, metadata = self.latest()
        if value is None:
            value = self.operations[identifier](metadata['cancel'])
        self.app._results.put((identifier, success, value))
        self.app._poll_results()

    def capture(self, *, mode='보관만', consent=False):
        self.app.capture_mode.set(mode)
        self.library.set_setting('automatic_ocr_consent', consent)
        page = self.app.accept_capture(self.image)
        self.assertIsNotNone(page)
        return page

    def text_document(self, source='합성 원문 10월 2일까지 신청'):
        doc = self.library.create_document('합성 텍스트')
        self.library.add_text_page(doc['id'], source, source_name='합성 원문')
        self.app.open_document(doc['id'])
        return doc

    def edit_source(self, text):
        self.app.source_editor.delete('1.0', 'end')
        self.app.source_editor.insert('1.0', text)
        self.app.source_editor.edit_modified(True)
        self.app._source_modified()

    def prepare_ai(self):
        self.choose.return_value = make_text_snapshot(self.library.document_text(self.app.document['id']))
        self.app.generate()
        self.assertTrue(self.operations)

    def test_storage_only_capture_makes_no_choice_ocr_or_analysis(self):
        page = self.capture(consent=True)
        self.assertTrue(Path(page['path']).exists())
        self.choose.assert_not_called()
        self.start.assert_not_called()
        self.ocr.assert_not_called()
        self.ai.assert_not_called()

    def test_no_consent_automatic_setting_does_not_send_without_choice(self):
        page = self.capture(mode='자동 인식', consent=False)
        self.choose.assert_called_once()
        self.start.assert_not_called()
        self.ocr.assert_not_called()
        self.assertTrue(Path(page['path']).exists())

    def test_approved_new_automatic_capture_performs_one_ocr_without_analysis(self):
        page = self.capture(mode='자동 인식', consent=True)
        self.choose.assert_not_called()
        self.assertEqual(self.latest()[1]['kind'], 'ocr')
        self.finish()
        self.ocr.assert_called_once()
        self.ai.assert_not_called()
        self.assertEqual(self.library.capture_store.get(page['capture_id']).ocr_text, '합성 OCR 결과')
        self.assertEqual(self.app.source_editor.get('1.0', 'end-1c'), '합성 OCR 결과')

    def test_explicit_file_choice_dispatches_only_selected_immutable_bytes(self):
        path = self.directory / 'synthetic.pdf'
        selected = TransferSnapshot('file', TransferPolicy('synthetic-file', 'file'),
                                    file_bytes=b'%PDF-safe-selected-copy', file_name='safe.pdf')
        self.choose.return_value = selected
        self.app.import_file(path)
        self.choose.assert_called_once()
        self.assertEqual(self.choose.call_args.kwargs['kind'], 'file')
        self.finish()
        options = self.file_ocr.call_args.kwargs
        self.assertEqual(options['file_bytes'], b'%PDF-safe-selected-copy')
        self.assertEqual(options['filename'], 'safe.pdf')
        self.assertFalse(path.exists())
        self.ocr.assert_not_called()
        self.ai.assert_not_called()

    def test_file_to_redacted_text_never_calls_file_or_image_api(self):
        self.choose.return_value = make_text_snapshot('공개 전송 사본', excluded_strings=['SYNTHETIC_SECRET'])
        self.app.import_file(self.directory / 'synthetic.pdf')
        self.finish()
        self.file_ocr.assert_not_called()
        self.ocr.assert_not_called()
        self.assertEqual(self.app.source_editor.get('1.0', 'end-1c'), '공개 전송 사본')

    def test_file_choice_cancel_does_not_dispatch(self):
        self.app.import_file(self.directory / 'synthetic.pdf')
        self.start.assert_not_called()
        self.file_ocr.assert_not_called()

    def test_masked_capture_dispatch_contains_opaque_pixels_only(self):
        self.choose.return_value = make_image_snapshot(self.image, rectangles=[(2, 3, 15, 12)])
        self.capture(mode='가리고 읽기')
        self.finish()
        transmitted = self.ocr.call_args.args[0]
        self.assertEqual(transmitted.getpixel((3, 4)), (0, 0, 0))
        self.assertEqual(transmitted.getpixel((0, 0)), (40, 80, 120))
        self.file_ocr.assert_not_called()

    def test_old_document_only_mask_follows_adopted_capture(self):
        capture = self.library.capture_store.save(self.image)
        old = self.library.task_store.create(title='합성 기존 업무', source_text='원래 원문')
        self.library.capture_store.link_to_task([capture.id], old.id)
        policy = make_text_snapshot('safe [가림]', excluded_strings=['SYNTHETIC_SECRET']).policy
        self.app._transfer().policy_store.save('document:' + old.id, policy)
        doc = self.library.adopt_capture(capture.id)
        self.app.open_document(doc['id'])
        self.library.set_setting('automatic_ocr_consent', True)
        self.app.capture_mode.set('자동 인식')
        self.app._read_image(self.app.page, automatic=True)
        self.choose.assert_called_once()
        self.assertTrue(self.choose.call_args.kwargs['previous'].redacted)
        self.start.assert_not_called()

    def test_task_adoption_inherits_document_and_capture_masks_before_ai(self):
        capture = self.library.capture_store.save(self.image)
        old = self.library.task_store.create(title='합성 기존 업무', source_text='PUBLIC DOCUMENT_SECRET CAPTURE_SECRET')
        self.library.capture_store.link_to_task([capture.id], old.id)
        store = self.app._transfer().policy_store
        store.save('document:' + old.id, make_text_snapshot('safe [가림]', excluded_strings=['DOCUMENT_SECRET']).policy)
        store.save('capture:' + capture.id, make_text_snapshot('safe [가림]', excluded_strings=['CAPTURE_SECRET']).policy)
        self.app.open_document(old.id)
        self.app.adopt_current()
        self.assertNotEqual(self.app.document['id'], old.id)
        self.assertEqual(self.library.document_text(self.app.document['id']), old.source_text)
        inherited = store.get('document:' + self.app.document['id'])
        self.assertTrue(inherited.redacted)
        for secret in ('DOCUMENT_SECRET', 'CAPTURE_SECRET'):
            with self.assertRaises(ScopeExpansionRequired):
                inherited.guard_text(secret, strict=False)
        self.app.generate()
        self.assertTrue(self.choose.call_args.kwargs['previous'].redacted)
        self.start.assert_not_called()

    def test_adoption_policy_save_failure_never_creates_unprotected_text_copy(self):
        old = self.library.task_store.create(title='합성 기존 업무', source_text='SYNTHETIC_SECRET')
        store = self.app._transfer().policy_store
        store.save('document:' + old.id, make_text_snapshot('safe', excluded_strings=['SYNTHETIC_SECRET']).policy)
        self.app.open_document(old.id)
        with patch.object(store, 'save', side_effect=OSError('synthetic storage failure')):
            self.app.adopt_current()
        self.assertEqual(self.app.document['id'], old.id)
        for doc in self.library.list_documents():
            if not doc.get('legacy'):
                self.assertEqual(self.library.document_text(doc['id']), '')
        self.start.assert_not_called()

    def test_cancelled_ocr_response_does_not_change_capture_or_editor(self):
        page = self.capture(mode='자동 인식', consent=True)
        self.app.cancel_jobs()
        self.finish('늦은 OCR')
        self.assertEqual(self.library.capture_store.get(page['capture_id']).ocr_text, '')
        self.assertEqual(self.app.source_editor.get('1.0', 'end-1c'), '')

    def test_late_ocr_keeps_unsaved_source_and_can_save_after_completion(self):
        page = self.capture(mode='자동 인식', consent=True)
        self.edit_source('교사 수정본')
        self.finish('늦은 OCR')
        self.assertEqual(self.app.source_editor.get('1.0', 'end-1c'), '교사 수정본')
        self.assertTrue(self.app.flush_edits())
        self.assertEqual(self.library.capture_store.get(page['capture_id']).effective_text, '교사 수정본')

    def test_reread_keeps_first_transcript_separate_from_latest_and_edited_text(self):
        page = self.capture(mode='자동 인식', consent=True)
        self.finish('처음 인식한 합성 원문')
        self.edit_source('교사 수정본')
        self.assertTrue(self.app.flush_edits())
        self.choose.return_value = make_image_snapshot(self.image)
        self.app.read_current_page()
        self.finish('다시 인식한 합성 원문')
        self.assertEqual(self.library.initial_ocr(page['id']), '처음 인식한 합성 원문')
        self.assertEqual(self.app.page['ocr_text'], '다시 인식한 합성 원문')
        self.assertEqual(self.app.source_editor.get('1.0', 'end-1c'), '교사 수정본')
        with patch.object(self.app, '_readonly_view') as view:
            self.app.show_original_text()
        shown = view.call_args.args[1]
        self.assertIn('처음 인식한 합성 원문', shown)
        self.assertIn('다시 인식한 합성 원문', shown)
        self.assertEqual(self.library.capture_store.get(page['capture_id']).effective_text, '교사 수정본')

    def test_failed_ocr_keeps_successful_ocr_and_unsaved_editor_can_save(self):
        page = self.capture()
        self.library.capture_store.update_ocr(page['capture_id'], '이전 성공 OCR')
        self.app.open_document(page['document_id'])
        self.choose.return_value = make_image_snapshot(self.image)
        self.app.read_current_page()
        self.edit_source('실패 중 교사 수정본')
        self.finish('합성 연결 오류', success=False)
        self.assertEqual(self.library.capture_store.get(page['capture_id']).ocr_text, '이전 성공 OCR')
        self.assertEqual(self.app.source_editor.get('1.0', 'end-1c'), '실패 중 교사 수정본')
        self.assertTrue(self.app.flush_edits())

    def test_late_ai_result_is_history_not_overwrite_of_edited_source(self):
        doc = self.text_document()
        self.prepare_ai()
        self.edit_source('교사가 바꾼 원문')
        self.finish()
        self.assertEqual(self.app.source_editor.get('1.0', 'end-1c'), '교사가 바꾼 원문')
        self.assertEqual(self.app.output_editor.get('1.0', 'end-1c'), '')
        self.assertEqual(len(self.library.outputs(doc['id'])), 1)

    def test_late_ai_result_does_not_overwrite_edited_previous_output(self):
        doc = self.text_document()
        old = self.library.save_output(doc['id'], '요약', '이전 결과', fingerprint(self.library.document_text(doc['id'])))
        self.app._load_output(old)
        self.prepare_ai()
        self.app.output_editor.delete('1.0', 'end')
        self.app.output_editor.insert('1.0', '교사 결과 수정본')
        self.app.output_editor.edit_modified(True)
        self.app._output_modified()
        self.finish()
        self.assertEqual(self.app.output_editor.get('1.0', 'end-1c'), '교사 결과 수정본')
        self.assertEqual(len(self.library.outputs(doc['id'])), 2)
        self.assertTrue(self.app.flush_edits())

    def test_cancelled_ai_result_is_not_saved(self):
        doc = self.text_document()
        self.prepare_ai()
        self.app.cancel_jobs()
        self.finish('취소 후 응답')
        self.assertEqual(self.library.outputs(doc['id']), [])

    def test_scope_change_after_request_rejects_ocr_completion(self):
        page = self.capture(mode='자동 인식', consent=True)
        newer = make_image_snapshot(self.image, rectangles=[(0, 0, 15, 20)])
        self.app._transfer().policy_store.save('capture:' + page['capture_id'], newer.policy)
        self.finish('옛 전송 범위 OCR')
        self.assertEqual(self.library.capture_store.get(page['capture_id']).ocr_text, '')
        self.assertEqual(self.app.source_editor.get('1.0', 'end-1c'), '')

    def test_scope_change_after_request_rejects_ai_completion(self):
        doc = self.text_document()
        self.prepare_ai()
        newer = make_text_snapshot('더 좁은 사본', excluded_strings=['합성 원문']).policy
        self.app._transfer().policy_store.save('document:' + doc['id'], newer)
        self.finish('옛 전송 범위 결과')
        self.assertEqual(self.library.outputs(doc['id']), [])

    def test_scope_change_while_snapshot_tokens_are_collected_blocks_dispatch(self):
        doc = self.text_document()
        self.choose.return_value = make_text_snapshot(self.library.document_text(doc['id']))
        original = self.app._scope_tokens
        def raced(*args, **kwargs):
            newer = make_text_snapshot('더 좁은 사본', excluded_strings=['합성 원문']).policy
            self.app._transfer().policy_store.save('document:' + doc['id'], newer)
            return original(*args, **kwargs)
        self.start.side_effect = lambda *args: CaptureDeskApp._start_job(self.app, *args)
        with patch.object(self.app, '_scope_tokens', side_effect=raced), patch('ui.capture_desk.threading.Thread') as worker:
            self.app.generate()
        worker.assert_not_called()
        self.assertEqual(self.app._jobs, {})
        self.ai.assert_not_called()

    def test_policy_change_while_worker_is_queued_prevents_api_request(self):
        doc = self.text_document()
        self.choose.return_value = make_text_snapshot(self.library.document_text(doc['id']))
        self.start.side_effect = lambda *args: CaptureDeskApp._start_job(self.app, *args)
        with patch('ui.capture_desk.threading.Thread') as worker:
            self.app.generate()
        worker.assert_called_once()
        newer = make_text_snapshot('더 좁은 사본', excluded_strings=['합성 원문']).policy
        self.app._transfer().policy_store.save('document:' + doc['id'], newer)
        worker.call_args.kwargs['target']()
        self.ai.assert_not_called()
        self.app._poll_results()
        self.assertEqual(self.library.outputs(doc['id']), [])

    def test_cancel_queued_worker_releases_job_and_allows_retry(self):
        doc = self.text_document()
        self.choose.return_value = make_text_snapshot(self.library.document_text(doc['id']))
        self.start.side_effect = lambda *args: CaptureDeskApp._start_job(self.app, *args)
        with patch('ui.capture_desk.threading.Thread') as worker:
            self.app.generate()
        self.app.cancel_jobs()
        worker.call_args.kwargs['target']()
        self.app._poll_results()
        self.ai.assert_not_called()
        self.assertEqual(self.app._jobs, {})
        with patch('ui.capture_desk.threading.Thread') as retry:
            self.app.generate()
        retry.assert_called_once()

    def test_worker_start_failure_does_not_leave_busy_job(self):
        doc = self.text_document()
        self.choose.return_value = make_text_snapshot(self.library.document_text(doc['id']))
        self.start.side_effect = lambda *args: CaptureDeskApp._start_job(self.app, *args)
        with patch('ui.capture_desk.threading.Thread') as worker:
            worker.return_value.start.side_effect = RuntimeError('synthetic thread failure')
            self.app.generate()
        self.assertEqual(self.app._jobs, {})
        self.ai.assert_not_called()


if __name__ == '__main__':
    unittest.main()
