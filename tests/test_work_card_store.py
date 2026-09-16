"""Synthetic/offline acceptance cases for teacher-owned facts and legacy data."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from services.personal_todos import PersonalTodoStore
from services.work_card_store import CardConflictError, WorkCardStore, evidence_record, render_card_item


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

    def test_a01_edit_reopen_linked_todo_and_new_artifact_use_current_value(self):
        todos = PersonalTodoStore(self.temp.name, self.store)
        todos.add_cards([self.card])
        edited = self.store.update_card(self.card['id'], {'deadline': '2026. 10. 16.', 'owner': '담당 교사'}, self.card['version'])
        reopened = WorkCardStore(self.temp.name)
        current = reopened.get_card(edited['id'])
        self.assertEqual(current['deadline'], '2026. 10. 16.')
        self.assertEqual(current['fields']['deadline']['ai_value'], '2026. 10. 15.')
        self.assertIn('2026. 10. 15.', current['fields']['deadline']['evidence']['quote'])
        self.assertIn('2026. 10. 16.', PersonalTodoStore(self.temp.name).list()[0]['item'])
        content = render_card_item(current)
        artifact = reopened.save_artifact('synthetic-doc', '안내문', content, {current['id']: current['version']}, 1)
        self.assertIn('2026. 10. 16.', artifact['content'])
        self.assertFalse(artifact['stale'])
        self.assertEqual(reopened.get_document('synthetic-doc')['text'], SOURCE)

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

    def test_a10_linked_add_deduplicates_preserves_done_and_card_version(self):
        todos = PersonalTodoStore(self.temp.name, self.store)
        self.assertEqual(todos.add_cards([self.card, self.card['id']]), 1)
        row = todos.list()[0]
        todos.set_done([row['id']], True)
        self.assertEqual(todos.add_cards([self.card]), 0)
        self.assertEqual(todos.list()[0]['done'], 1)
        self.assertEqual(self.store.get_card(self.card['id'])['version'], 1)
        self.store.update_card(self.card['id'], {'deadline': '새 기한'}, 1)
        self.assertIn('새 기한', todos.list()[0]['item'])
        self.assertEqual(todos.list()[0]['done'], 1)

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

    def test_r02_additive_legacy_migration_backup_keeps_done_and_analysis_origin(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'personal_todos.sqlite3'
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('CREATE TABLE todos (id TEXT PRIMARY KEY,item TEXT NOT NULL,source TEXT NOT NULL,done INTEGER NOT NULL DEFAULT 0)')
                db.execute("INSERT INTO todos VALUES ('legacy','기존 업무\n메모','기존 원문',1)")
                db.execute('CREATE TABLE analysis_origins (fingerprint TEXT PRIMARY KEY)')
                db.execute("INSERT INTO analysis_origins VALUES ('legacy-origin')")
            todos = PersonalTodoStore(directory)
            row = todos.list()[0]
            self.assertEqual((row['id'], row['item'], row['source'], row['done']), ('legacy', '기존 업무\n메모', '기존 원문', 1))
            self.assertIsNone(row['card_id'])
            backups = list(Path(directory).glob('personal_todos.sqlite3.before-work-cards-*.bak'))
            self.assertEqual(len(backups), 1)
            with closing(sqlite3.connect(backups[0])) as db:
                self.assertEqual(db.execute('SELECT done FROM todos').fetchone()[0], 1)
                self.assertEqual(db.execute('SELECT fingerprint FROM analysis_origins').fetchone()[0], 'legacy-origin')
            PersonalTodoStore(directory)
            self.assertEqual(len(list(Path(directory).glob('*.bak'))), 1)

    def test_r02_backup_failure_aborts_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'personal_todos.sqlite3'
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('CREATE TABLE todos (id TEXT PRIMARY KEY,item TEXT NOT NULL,source TEXT NOT NULL,done INTEGER NOT NULL DEFAULT 0)')
            with patch('services.work_card_store.backup_database', side_effect=OSError('synthetic backup failure')):
                with self.assertRaises(OSError):
                    PersonalTodoStore(directory)
            with closing(sqlite3.connect(path)) as db:
                self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='todo_card_links'").fetchone())

    def test_r02_failed_link_batch_has_no_partial_addition(self):
        todos = PersonalTodoStore(self.temp.name, self.store)
        with self.assertRaises(KeyError):
            todos.add_cards([self.card, 'missing'])
        self.assertEqual(todos.list(), [])

    def test_deleting_link_does_not_delete_card_and_can_add_again(self):
        todos = PersonalTodoStore(self.temp.name, self.store)
        todos.add_cards([self.card])
        todos.delete([todos.list()[0]['id']])
        self.assertEqual(todos.list(), [])
        self.assertIsNotNone(self.store.get_card(self.card['id']))
        self.assertEqual(todos.add_cards([self.card]), 1)

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

    def test_todo_uses_current_action_dates_and_shows_stale_source_status(self):
        payload = proposal(action='2026. 10. 15.까지 신청서 제출')
        card = self.store.merge_analysis('synthetic-doc', [payload])[0]
        card = self.store.adopt_card(card['id'], card['version'])
        todos = PersonalTodoStore(self.temp.name, self.store)
        todos.add_cards([card])
        todos.set_done([todos.list()[0]['id']], True)
        self.store.update_card(card['id'], {'deadline': '2026. 10. 16.'}, card['version'])
        row = todos.list()[0]
        self.assertNotIn('2026. 10. 15.', row['item'])
        self.assertIn('2026. 10. 16.', row['item'])
        self.assertTrue(row['done'])
        self.store.ensure_document('synthetic-doc', SOURCE + '\n수정 공문')
        row = todos.list()[0]
        self.assertTrue(row['source_stale'])
        self.assertIn('원문 변경 · 이전 근거 재확인 필요', row['item'])

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
