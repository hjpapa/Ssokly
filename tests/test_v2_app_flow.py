"""V2 acceptance flow using isolated databases, Tk, synthetic text, no API."""
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch
from PIL import Image

from services.analysis_document import Action, AnalysisDocument, FieldEvidence
from services.capture_store import CaptureStore
from services.task_store import TaskStore
from services.transfer_policy import make_text_snapshot, make_image_snapshot
from ui.app import SsoklyApp


SOURCE = '담임은 희망 학생의 신청서를 10월 2일까지 교무실에 제출한다.\n행사는 10월 8일이다.'


def sample_document():
    return AnalysisDocument(title='합성 행사 신청', summary='희망 학생의 행사 참가 신청을 취합하는 업무입니다.',
        actions=[Action(action='신청서 제출', owner='담임', target='희망 학생', condition='희망 학생', obligation='조건부',
            deadline='10월 2일', event_date='10월 8일', deliverable='신청서', destination='교무실',
            kind='명시된 의무', evidence=SOURCE.splitlines()[0],
            field_evidence=FieldEvidence(owner=SOURCE.splitlines()[0], target=SOURCE.splitlines()[0],
                condition=SOURCE.splitlines()[0], deadline=SOURCE.splitlines()[0], event_date=SOURCE.splitlines()[1],
                deliverable=SOURCE.splitlines()[0], destination=SOURCE.splitlines()[0]))], questions=[], message='')


class V2AppFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = TaskStore(app_data_dir=self.root)
        self.captures = CaptureStore(directory=self.root / 'captures-inbox')
        self.app = SsoklyApp(task_store=self.store, capture_store=self.captures)
        self.app.withdraw()
        self.addCleanup(self.destroy_app)
        for name in ('showerror', 'showwarning', 'showinfo'):
            mock = patch('ui.app.messagebox.' + name)
            mock.start()
            self.addCleanup(mock.stop)

    def destroy_app(self):
        if self.app is not None:
            self.app._closing = True
            for key in self.app.tk.call('after', 'info'):
                self.app.after_cancel(key)
            self.app.destroy()
            self.app = None

    def prepare_request(self, text=SOURCE):
        self.app._replace_ocr_text(text, track_change=True)
        snapshot = make_text_snapshot(text)
        with patch('ui.app.choose_transfer', return_value=snapshot), patch.object(self.app, '_start_worker_operation') as start:
            self.app.analyze_text(force=True)
        self.assertTrue(start.called)
        metadata = start.call_args.args[2]
        metadata['document'] = sample_document()
        return metadata

    def finish_request(self, metadata):
        self.app._active_operation_id = 'synthetic-request'
        self.app._active_operation_context = self.app.current_context_id
        self.app._active_operation_kind = 'analysis'
        self.app._active_operation_revisions = metadata.get('test_revisions',
            (self.app._source_revision, self.app._result_revision))
        self.app._worker_results.put(('synthetic-request', self.app.current_context_id, 'analysis', True,
            '합성 응답', metadata))
        self.app._drain_worker_results()

    def test_A01_A07_A09_save_reopen_current_todo_and_draft(self):
        self.finish_request(self.prepare_request())
        self.assertEqual(str(self.app.notebook.select()), str(self.app.cards_tab))
        card = self.app.work_cards.list_cards(self.app.document_id)[0]
        self.app._add_card_todo(card)
        self.app.generate_card_draft('교직원 메신저')
        old_artifact = self.app._current_artifact
        panel = self.app.card_panel
        panel.variables['deadline'].set('10월 5일')
        self.assertTrue(panel.save_selected())
        self.assertTrue(self.app.work_cards.artifact_is_stale(old_artifact))
        self.assertIn('10월 2일', old_artifact['content'])
        self.app.generate_card_draft('교직원 메신저')
        self.assertIn('10월 5일', self.app.result_text.get('1.0', 'end-1c'))
        self.assertIn('10월 5일', self.app.personal_todos.list()[0]['item'])
        panel.variables['deadline'].set('')
        self.assertTrue(panel.save_selected())
        self.assertTrue(self.app.save_current_task(show_success=False))
        saved = self.store.get(self.app.current_task_id)
        doc_id = self.app.document_id
        self.destroy_app()
        self.app = SsoklyApp(task_store=self.store, capture_store=self.captures)
        self.app.withdraw()
        self.app._load_task_record(saved)
        self.assertEqual(self.app.document_id, doc_id)
        self.assertEqual(self.app.work_cards.list_cards(doc_id)[0]['deadline'], '')
        self.app.generate_card_draft('교직원 메신저')
        self.assertNotIn('10월 2일', self.app.result_text.get('1.0', 'end-1c'))
        self.assertNotIn('10월 5일', self.app.personal_todos.list()[0]['item'])

    def test_A08_late_response_does_not_overwrite_edited_card_or_draft(self):
        self.finish_request(self.prepare_request())
        metadata = self.prepare_request()
        metadata['test_revisions'] = (self.app._source_revision, self.app._result_revision)
        panel = self.app.card_panel
        panel.variables['deadline'].set('10월 6일')
        self.assertTrue(panel.save_selected())
        before = self.app.result_text.get('1.0', 'end-1c')
        self.finish_request(metadata)
        self.assertEqual(self.app.work_cards.list_cards(self.app.document_id)[0]['deadline'], '10월 6일')
        self.assertEqual(self.app.result_text.get('1.0', 'end-1c'), before)
        late = self.app.work_cards.list_artifacts(self.app.document_id)[0]
        self.assertIn('지연', late['kind'])
        self.assertTrue(late['stale'])

    def test_P01_no_repeat_dialog_for_ordinary_business_contact(self):
        text = '업무 문의 교무실 043-000-0000'
        with patch('ui.app.choose_transfer', return_value=make_text_snapshot(text)) as choose:
            self.assertIsNotNone(self.app._select_transfer(kind='text', text=text))
            self.assertIsNotNone(self.app._select_transfer(kind='text', text=text))
        self.assertEqual(choose.call_count, 1)

    def test_P04_redacted_text_reused_after_reopen_and_restoration_requires_choice(self):
        raw = '합성 비밀: SYNTHETIC-SECRET\n학교 업무'
        safe = '합성 비밀: [가림]\n학교 업무'
        self.app._replace_ocr_text(raw, track_change=True)
        with patch('ui.app.choose_transfer', return_value=make_text_snapshot(safe, original_text=raw)):
            self.app.choose_text_transfer()
        self.assertTrue(self.app.save_current_task(show_success=False))
        saved = self.store.get(self.app.current_task_id)
        self.app._load_task_record(saved)
        with patch('ui.app.choose_transfer') as choose:
            snapshot = self.app._select_transfer(kind='text', text=raw)
            choose.assert_not_called()
        self.assertEqual(snapshot.text, safe)
        with patch('ui.app.choose_transfer', return_value=None) as choose:
            self.assertIsNone(self.app._select_transfer(kind='text', text=raw + '\n새 값'))
            choose.assert_called_once()

    def test_P05_cancel_capture_selection_never_starts_request(self):
        with patch('ui.app.choose_transfer', return_value=None), patch.object(self.app, '_start_worker_operation') as start:
            self.app._run_ocr(Image.new('RGB', (20, 20)))
            start.assert_not_called()

    def test_R02_imported_source_and_teacher_revision_are_separate(self):
        self.app._finish_ocr('합성 OCR 원문')
        original = self.app.work_cards.get_document(self.app.document_id)
        self.assertEqual(original['source_kind'], 'OCR 원본')
        self.app._replace_ocr_text('교사 검수 수정본', track_change=True)
        corrected = self.app._sync_work_source()
        self.assertEqual(corrected['source_kind'], '검수본')
        self.assertEqual(self.app.work_cards.get_document(self.app.document_id, original['version'])['text'], '합성 OCR 원문')

    def test_P03_ui_ocr_passes_only_masked_pixels(self):
        image = Image.new('RGB', (40, 40), 'white')
        snapshot = make_image_snapshot(image, rectangles=[(5, 5, 15, 15)])
        with patch('ui.app.choose_transfer', return_value=snapshot), patch.object(self.app, '_start_worker_operation') as start:
            self.app._run_ocr(image)
        with patch('ui.app.extract_text_from_image', return_value='safe') as send:
            start.call_args.args[1]()
        pixels = send.call_args.args[0]
        self.assertEqual(pixels.getpixel((10, 10)), (0, 0, 0))
        self.assertEqual(pixels.getpixel((30, 30)), (255, 255, 255))
        self.assertEqual(image.getpixel((10, 10)), (255, 255, 255))

    def test_P04_file_mask_to_text_never_calls_original_file_reader(self):
        snapshot = make_text_snapshot('safe file excerpt', original_text='SYNTHETIC-SECRET file excerpt')
        with patch('ui.app.choose_transfer', return_value=snapshot), patch.object(self.app, '_start_worker_operation') as start:
            self.app._run_document_extraction(self.root / 'not-opened.pdf')
        with patch('ui.app.extract_text_from_file') as file_reader, patch('ui.app.extract_text_from_image') as image_reader:
            self.assertEqual(start.call_args.args[1](), snapshot.text)
            file_reader.assert_not_called()
            image_reader.assert_not_called()

    def test_P05_late_ocr_cannot_loosen_a_newer_policy(self):
        old = make_text_snapshot('old raw text')
        current = make_text_snapshot('safe', excluded_strings=['old raw text'])
        self.app.transfer_policies.save('document:' + self.app.document_id, current.policy)
        from services.transfer_policy import ScopeExpansionRequired
        with self.assertRaises(ScopeExpansionRequired):
            self.app._remember_transfer_output({'transfer_snapshot': old,
                'document_policy_scope': old.policy.scope_id}, 'old raw text')
        self.assertEqual(self.app._transfer_policy().scope_id, current.policy.scope_id)

    def test_P05_new_capture_mask_overrides_older_document_grant(self):
        capture_id = 'synthetic-capture'
        self.app.current_capture_ids = [capture_id]
        self.app.transfer_policies.save('document:' + self.app.document_id, make_text_snapshot('SECRET old text').policy)
        masked = make_image_snapshot(Image.new('RGB', (20, 20)), rectangles=[(0, 0, 10, 10)])
        self.app.transfer_policies.save('capture:' + capture_id, masked.policy.with_safe_text('safe OCR'))
        with patch('ui.app.choose_transfer', return_value=None) as choose:
            self.assertIsNone(self.app._select_transfer(kind='text', text='SECRET old text'))
            choose.assert_called_once()

    def test_R02_artifact_save_failure_preserves_visible_manual_edits(self):
        self.finish_request(self.prepare_request())
        self.app._replace_result_text('교사가 직접 고친 안내문', track_change=True)
        with patch.object(self.app.work_cards, 'save_artifact', side_effect=OSError('synthetic full disk')):
            self.app.generate_card_draft('교직원 메신저')
        self.assertEqual(self.app.result_text.get('1.0', 'end-1c'), '교사가 직접 고친 안내문')

    def test_R03_source_summary_and_generation_do_not_call_ai_again(self):
        self.finish_request(self.prepare_request())
        with patch('ui.app.analyze_document_task') as analyze, patch('ui.app.choose_transfer') as choose:
            self.app.analyze_text('원문 요약')
            self.assertIn('희망 학생의 행사 참가 신청', self.app.result_text.get('1.0', 'end-1c'))
            self.app.analyze_text('가정통신문 초안')
            analyze.assert_not_called()
            choose.assert_not_called()


if __name__ == '__main__':
    unittest.main()
