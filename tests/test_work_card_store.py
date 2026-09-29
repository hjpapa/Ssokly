"""Synthetic/offline acceptance cases for teacher-owned facts and legacy data."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from services.work_card_store import CardConflictError, WorkCardStore, evidence_record


SOURCE = '희망 학급은 신청서를 제출한다.\n제출 기한: 2026. 10. 15.\n제출처: 교육지원청'


def proposal(**changes):
    value = {
        'action': '신청서 제출', 'owner': '', 'target': '희망 학급',
        'condition': '희망 학급만', 'obligation': '조건부',
        'deadline': '2026. 10. 15.', 'event_date': '', 'report_date': '',
        'deliverable': '신청서', 'destination': '교육지원청',
        'evidence': '희망 학급은 신청서를 제출한다.',
        'field_evidence': {'deadline': '제출 기한: 2026. 10. 15.', 'destination': '제출처: 교육지원청'},
    }
    value.update(changes)
    return value


class WorkCardStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = WorkCardStore(self.temp.name)
        self.document = self.store.ensure_document('synthetic-doc', SOURCE)
        self.card = self.store.merge_analysis('synthetic-doc', [proposal()])[0]

    def test_a07_explicit_empty_survives_reanalysis_and_restart(self):
        deleted = self.store.update_card(self.card['id'], {'deadline': '', 'owner': ''}, self.card['version'])
        merged = self.store.merge_analysis('synthetic-doc', [proposal(owner='새 AI 제안')])[0]
        self.assertEqual(merged['id'], deleted['id'])
        self.assertEqual(merged['deadline'], '')
        self.assertEqual(merged['owner'], '')
        self.assertTrue(merged['fields']['deadline']['edited'])
        self.assertEqual(merged['fields']['owner']['ai_value'], '새 AI 제안')
        self.assertEqual(WorkCardStore(self.temp.name).get_card(merged['id'])['deadline'], '')

    def test_a08_late_analysis_does_not_overwrite_teacher_and_records_ai(self):
        self.store.update_card(self.card['id'], {'deadline': '교사 확인 후 입력', 'notes': '개인 메모', 'preparation_date': '10월 12일'}, 1)
        late = self.store.merge_analysis('synthetic-doc', [proposal(deadline='늦은 AI 제안')], source_version=1)[0]
        self.assertEqual(late['deadline'], '교사 확인 후 입력')
        self.assertEqual(late['notes'], '개인 메모')
        self.assertEqual(late['preparation_date'], '10월 12일')
        self.assertEqual(late['fields']['deadline']['ai_value'], '늦은 AI 제안')

    def test_a08_old_source_response_becomes_comparison_not_current_overwrite(self):
        edited = self.store.update_card(self.card['id'], {'deadline': '교사 기한'}, 1)
        self.store.ensure_document('synthetic-doc', SOURCE + '\n검수 수정 문장')
        late = self.store.merge_analysis('synthetic-doc', [proposal()], source_version=1)[0]
        self.assertNotEqual(late['id'], edited['id'])
        self.assertTrue(late['comparison_candidate'])
        self.assertTrue(late['source_stale'])
        self.assertEqual(self.store.get_card(edited['id'])['deadline'], '교사 기한')

    def test_a09_old_artifact_remains_and_is_stale_even_if_completed(self):
        artifact = self.store.save_artifact('synthetic-doc', '교직원 안내', '예전 초안 · 사용자 문장', {self.card['id']: 1}, 1)
        self.store.update_card(self.card['id'], {'deadline': ''}, 1)
        self.assertTrue(self.store.artifact_is_stale(artifact['id']))
        self.assertEqual(self.store.list_artifacts('synthetic-doc')[0]['content'], '예전 초안 · 사용자 문장')
        current = self.store.get_card(self.card['id'])
        late = self.store.save_artifact('synthetic-doc', '늦은 응답', '옛 기한으로 생성 중이던 초안', {current['id']: 1}, 1)
        self.assertTrue(late['stale'])

    def test_confirmation_is_field_local_stales_artifact_without_content_version_change(self):
        artifact = self.store.save_artifact('synthetic-doc', '안내', '초안', {self.card['id']: 1}, 1)
        confirmed = self.store.confirm_fields(self.card['id'], ['deadline'], expected_version=1)
        self.assertTrue(confirmed['fields']['deadline']['confirmed'])
        self.assertFalse(confirmed['fields']['owner']['confirmed'])
        self.assertEqual(confirmed['version'], 1)
        self.assertTrue(self.store.artifact_is_stale(artifact))

    def test_conflict_does_not_write_any_editor_values(self):
        saved = self.store.update_card(self.card['id'], {'owner': '먼저 저장'}, 1)
        with self.assertRaises(CardConflictError):
            self.store.update_card(self.card['id'], {'owner': '늦은 저장', 'deadline': ''}, 1)
        self.assertEqual(self.store.get_card(self.card['id']), saved)

    def test_unrecognized_fields_rejected_without_partial_save(self):
        with self.assertRaises(ValueError):
            self.store.update_card(self.card['id'], {'deadline': '', 'document_id': 'other'}, 1)
        self.assertEqual(self.store.get_card(self.card['id'])['version'], 1)

    def test_exact_identity_required_no_similar_title_override_transfer(self):
        self.store.update_card(self.card['id'], {'notes': '첫 업무 메모'}, 1)
        other = self.store.merge_analysis('synthetic-doc', [proposal(action='신청서 제출 안내')])[0]
        self.assertNotEqual(other['id'], self.card['id'])
        self.assertEqual(other['notes'], '')
        self.assertTrue(other['comparison_candidate'])

    def test_duplicate_evidence_or_duplicate_proposals_never_auto_match(self):
        incoming = self.store.merge_analysis('synthetic-doc', [proposal(), proposal()])
        self.assertEqual(len(set(item['id'] for item in incoming)), 2)
        self.assertTrue(all(item['comparison_candidate'] for item in incoming))
        self.assertNotIn(self.card['id'], [item['id'] for item in incoming])
        self.store.ensure_document('repeated', SOURCE + '\n' + SOURCE)
        ambiguous = self.store.merge_analysis('repeated', [proposal()])[0]
        self.assertTrue(ambiguous['fields']['action']['evidence']['ambiguous'])
        self.assertTrue(ambiguous['comparison_candidate'])

    def test_source_version_keeps_history_and_marks_prior_evidence_stale(self):
        same = self.store.ensure_document('synthetic-doc', SOURCE)
        self.assertEqual(same['version'], 1)
        changed = self.store.ensure_document('synthetic-doc', SOURCE + '\n수정본')
        self.assertEqual(changed['version'], 2)
        current = self.store.get_card(self.card['id'])
        self.assertTrue(current['fields']['deadline']['evidence']['stale'])
        self.assertEqual(self.store.get_document('synthetic-doc', version=1)['text'], SOURCE)

    def test_a06_evidence_verified_from_actual_source_not_invented_pages(self):
        evidence = evidence_record('없는 문장', SOURCE, 'synthetic-doc', 1)
        self.assertFalse(evidence['verified'])
        self.assertEqual(evidence['locations'], [])
        evidence = evidence_record('제출 기한: 2026.\n10. 15.', SOURCE, 'synthetic-doc', 1)
        self.assertTrue(evidence['verified'])
        self.assertEqual(evidence['locations'][0]['line'], 2)
        self.assertNotIn('page', evidence['locations'][0])

    def test_r02_failed_card_save_raises_and_keeps_old_data(self):
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute("CREATE TRIGGER reject_card BEFORE UPDATE ON cards BEGIN SELECT RAISE(ABORT,'test save failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.update_card(self.card['id'], {'deadline': ''}, 1)
        recovered = WorkCardStore(self.temp.name).get_card(self.card['id'])
        self.assertEqual(recovered['deadline'], self.card['deadline'])
        self.assertEqual(recovered['version'], 1)

    def test_r02_failed_source_history_save_rolls_back_document(self):
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute("CREATE TRIGGER reject_version BEFORE INSERT ON source_versions BEGIN SELECT RAISE(ABORT,'test source failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.ensure_document('synthetic-doc', '수정본')
        self.assertEqual(self.store.get_document('synthetic-doc')['text'], SOURCE)
        self.assertEqual(self.store.get_document('synthetic-doc')['version'], 1)

    def test_quote_exists_but_unsupported_field_is_not_verified(self):
        payload = proposal(owner='원문에 없는 담당자')
        card = self.store.merge_analysis('synthetic-doc', [payload])[0]
        self.assertTrue(card['fields']['owner']['evidence']['quote_verified'])
        self.assertFalse(card['fields']['owner']['evidence']['verified'])
        self.assertIn('근거 확인 필요', card['fields']['owner']['issues'])

    def test_adopt_comparison_candidate_requires_current_version(self):
        candidate = self.store.merge_analysis('synthetic-doc', [proposal(action='다른 업무')])[0]
        self.assertTrue(candidate['comparison_candidate'])
        adopted = self.store.adopt_card(candidate['id'], candidate['version'])
        self.assertFalse(adopted['comparison_candidate'])
        self.assertGreater(adopted['version'], candidate['version'])
        with self.assertRaises(CardConflictError):
            self.store.adopt_card(candidate['id'], candidate['version'])

    def test_a09_adopting_new_card_marks_previous_artifact_stale_without_old_card_edit(self):
        artifact = self.store.save_artifact('synthetic-doc', '안내문', '기존 업무만 포함', {self.card['id']: 1}, 1)
        candidate = self.store.merge_analysis('synthetic-doc', [proposal(action='새 별도 업무')])[0]
        self.assertFalse(self.store.artifact_is_stale(artifact))
        self.store.adopt_card(candidate['id'], candidate['version'])
        self.assertEqual(self.store.get_card(self.card['id'])['version'], 1)
        self.assertTrue(self.store.artifact_is_stale(artifact))

    def test_legacy_artifact_without_card_versions_is_stale_once_cards_exist(self):
        legacy = self.store.save_artifact('synthetic-doc', '구형 텍스트', '버전 연결 전 초안', {}, 1)
        self.assertTrue(legacy['stale'])

    def test_confirmation_can_be_revoked_and_old_review_snapshot_remains_stale(self):
        confirmed = self.store.confirm_fields(self.card['id'], ['deadline'], expected_version=1)
        signatures = self.store.review_signatures('synthetic-doc')
        self.assertNotEqual(confirmed['review_signature'], self.card['review_signature'])
        artifact = self.store.save_artifact('synthetic-doc', '안내', '확인 상태 초안', {self.card['id']: 1}, 1, signatures)
        unconfirmed = self.store.set_confirmations(self.card['id'], {'deadline': False}, 1, confirmed['review_signature'])
        self.assertFalse(unconfirmed['fields']['deadline']['confirmed'])
        self.assertEqual(unconfirmed['version'], 1)
        self.assertTrue(self.store.artifact_is_stale(artifact))
        late = self.store.save_artifact('synthetic-doc', '늦은 초안', '이전 확인 상태', {self.card['id']: 1}, 1, signatures)
        self.assertTrue(late['stale'])

    def test_concurrent_confirmation_change_requires_new_comparison(self):
        self.store.confirm_fields(self.card['id'], ['deadline'], expected_version=1)
        with self.assertRaises(CardConflictError):
            self.store.set_confirmations(self.card['id'], {'deadline': False}, 1, self.card['review_signature'])
        self.assertTrue(self.store.get_card(self.card['id'])['fields']['deadline']['confirmed'])

    def test_no_op_confirmation_does_not_stale_current_review_snapshot(self):
        confirmed = self.store.confirm_fields(self.card['id'], ['deadline'], expected_version=1)
        artifact = self.store.save_artifact('synthetic-doc', '안내', '확인 후 초안', {self.card['id']: 1}, 1)
        repeated = self.store.confirm_fields(self.card['id'], ['deadline'], expected_version=1)
        self.assertEqual(repeated['review_signature'], confirmed['review_signature'])
        self.assertFalse(self.store.artifact_is_stale(artifact))

    def test_value_and_confirmation_edit_are_atomic_on_invalid_confirmation(self):
        with self.assertRaises(TypeError):
            self.store.update_card(self.card['id'], {'deadline': ''}, 1, confirmation_changes={'deadline': 'false'})
        unchanged = self.store.get_card(self.card['id'])
        self.assertEqual(unchanged['deadline'], self.card['deadline'])
        self.assertEqual(unchanged['version'], 1)

    def test_v1_migration_keeps_cards_and_artifacts_with_backup_and_unknown_review_status(self):
        artifact = self.store.save_artifact('synthetic-doc', '기존 안내', '이전 내용', {self.card['id']: 1}, 1)
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute('DROP TABLE artifact_reviews')
            db.execute('PRAGMA user_version=1')
        migrated = WorkCardStore(self.temp.name)
        self.assertEqual(migrated.get_card(self.card['id'])['deadline'], self.card['deadline'])
        self.assertEqual(migrated.list_artifacts('synthetic-doc')[0]['content'], '이전 내용')
        self.assertTrue(migrated.artifact_is_stale(artifact['id']))
        self.assertEqual(len(list(Path(self.temp.name).glob('work_cards.sqlite3.before-work-cards-*.bak'))), 1)
        with closing(sqlite3.connect(migrated.path)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 2)

    def test_v1_backup_failure_never_changes_schema_or_values(self):
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute('DROP TABLE artifact_reviews')
            db.execute('PRAGMA user_version=1')
        with patch('services.work_card_store.backup_database', side_effect=OSError('synthetic backup failure')):
            with self.assertRaises(OSError):
                WorkCardStore(self.temp.name)
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='artifact_reviews'").fetchone())
            self.assertEqual(db.execute('SELECT count(*) FROM cards').fetchone()[0], 1)

    def test_exact_source_cells_resolve_repeated_quote_without_guessing(self):
        source = '업무\t기한\n신청서 제출\t2026. 10. 15.\n신청서 제출\t2026. 10. 15.'
        self.store.ensure_document('cells', source)
        payload = {'action': '신청서 제출', 'deadline': '2026. 10. 15.', 'evidence': '신청서 제출',
                   'field_evidence': {'deadline': '2026. 10. 15.'},
                   'evidence_locations': {'action': [{'line': 2, 'cell': 1}], 'deadline': [{'line': 2, 'cell': 2}]}}
        card = self.store.merge_analysis('cells', [payload])[0]
        evidence = card['fields']['deadline']['evidence']
        self.assertFalse(evidence['ambiguous'])
        self.assertTrue(evidence['location_verified'])
        self.assertEqual(evidence['locations'][0]['cell'], 2)
        self.assertEqual(evidence['locations'][0]['line'], 2)
        again = self.store.merge_analysis('cells', [payload])[0]
        self.assertEqual(again['id'], card['id'])

    def test_fabricated_source_cell_does_not_disambiguate_valid_quote(self):
        source = '신청서 제출\n2026. 10. 15.\n2026. 10. 15.'
        record = evidence_record('2026. 10. 15.', source, 'cells', 1, [{'line': 1, 'cell': 0}])
        self.assertTrue(record['ambiguous'])
        self.assertTrue(record['location_invalid'])
        self.assertFalse(record['location_verified'])

    def test_saved_results_use_one_connection_without_public_post_commit_reads(self):
        with patch.object(self.store, 'get_card', side_effect=AssertionError('post-commit read')), \
                patch.object(self.store, '_db', wraps=self.store._db) as connections:
            updated = self.store.update_card(self.card['id'], {'deadline': ''}, 1)
            self.assertEqual(connections.call_count, 1)
            self.assertEqual(updated['deadline'], '')
            connections.reset_mock()
            merged = self.store.merge_analysis('synthetic-doc', [proposal(owner='새 AI 담당')])
            self.assertEqual(connections.call_count, 1)
            self.assertEqual(merged[0]['deadline'], '')
            connections.reset_mock()
            candidate = self.store.merge_analysis('synthetic-doc', [proposal(action='다른 업무')])[0]
            self.assertEqual(connections.call_count, 1)
            connections.reset_mock()
            adopted = self.store.adopt_card(candidate['id'], candidate['version'])
            self.assertEqual(connections.call_count, 1)
            self.assertFalse(adopted['comparison_candidate'])
        versions = {item['id']: item['version'] for item in self.store.list_cards('synthetic-doc')}
        with patch.object(self.store, 'list_artifacts', side_effect=AssertionError('post-commit read')), \
                patch.object(self.store, '_db', wraps=self.store._db) as connections:
            artifact = self.store.save_artifact('synthetic-doc', '안내', '저장된 초안', versions, 1)
            self.assertEqual(connections.call_count, 1)
        reopened = WorkCardStore(self.temp.name)
        self.assertEqual(reopened.get_card(updated['id'])['deadline'], '')
        self.assertEqual(reopened.list_artifacts('synthetic-doc'), [artifact])

    def test_update_snapshot_decode_failure_rolls_back_values_and_confirmation(self):
        with patch.object(WorkCardStore, '_decode_card', side_effect=ValueError('synthetic snapshot decode')):
            with self.assertRaisesRegex(ValueError, 'snapshot decode'):
                self.store.update_card(self.card['id'], {'deadline': ''}, 1,
                                       confirmation_changes={'deadline': True})
        self.assertEqual(WorkCardStore(self.temp.name).get_card(self.card['id']), self.card)

    def test_merge_second_snapshot_failure_rolls_back_whole_batch(self):
        decode = WorkCardStore._decode_card
        snapshots = []

        def fail_second(row, version):
            snapshots.append(row['id'])
            if len(snapshots) == 2:
                raise ValueError('synthetic second snapshot')
            return decode(row, version)

        with patch.object(WorkCardStore, '_decode_card', side_effect=fail_second):
            with self.assertRaisesRegex(ValueError, 'second snapshot'):
                self.store.merge_analysis('synthetic-doc', [proposal(owner='새 AI 담당'), proposal(action='추가 업무')])
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(WorkCardStore(self.temp.name).list_cards('synthetic-doc'), [self.card])

    def test_adopt_snapshot_failure_keeps_candidate_and_version(self):
        candidate = self.store.merge_analysis('synthetic-doc', [proposal(action='추가 업무')])[0]
        self.assertTrue(candidate['comparison_candidate'])
        with patch.object(WorkCardStore, '_decode_card', side_effect=ValueError('synthetic adopt snapshot')):
            with self.assertRaisesRegex(ValueError, 'adopt snapshot'):
                self.store.adopt_card(candidate['id'], candidate['version'])
        self.assertEqual(WorkCardStore(self.temp.name).get_card(candidate['id']), candidate)

    def test_artifact_snapshot_failure_rolls_back_artifact_and_review_rows(self):
        with patch.object(WorkCardStore, '_artifact_stale', side_effect=sqlite3.OperationalError('synthetic snapshot read')):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'snapshot read'):
                self.store.save_artifact('synthetic-doc', '안내', '실패한 초안', {self.card['id']: 1}, 1)
        self.assertEqual(WorkCardStore(self.temp.name).list_artifacts('synthetic-doc'), [])
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM artifact_reviews').fetchone()[0], 0)
        saved = self.store.save_artifact('synthetic-doc', '안내', '재시도 초안', {self.card['id']: 1}, 1)
        self.assertEqual(self.store.list_artifacts('synthetic-doc'), [saved])

    def test_new_artifact_does_not_reread_unrelated_corrupt_history(self):
        previous = self.store.save_artifact('synthetic-doc', '안내', '이전 초안', {self.card['id']: 1}, 1)
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute('UPDATE artifacts SET card_versions_json=? WHERE id=?', ('{', previous['id']))
        saved = self.store.save_artifact('synthetic-doc', '안내', '새 초안', {self.card['id']: 1}, 1)
        self.assertEqual(saved['content'], '새 초안')
        self.assertFalse(saved['stale'])
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM artifacts').fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT card_versions_json FROM artifacts WHERE id=?', (previous['id'],)).fetchone()[0], '{')
        with self.assertRaises(ValueError):
            self.store.list_artifacts('synthetic-doc')


if __name__ == '__main__':
    unittest.main()
