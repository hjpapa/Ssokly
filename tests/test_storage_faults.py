"""Synthetic local fault injection; no user databases, network, or API calls."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from services.transfer_policy import ScopeExpansionRequired, TransferPolicyStore, make_text_snapshot
from services.work_card_store import WorkCardStore
from services.workspace_state import WorkspaceStateStore


class StorageFaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = WorkCardStore(self.temp.name)
        self.store.ensure_document('synthetic-doc', '신청서를 제출한다.')
        self.card = self.store.merge_analysis('synthetic-doc', [{
            'action': '신청서 제출', 'evidence': '신청서를 제출한다.',
        }])[0]

    def _deferred_failure(self, table):
        # The violation is deliberately deferred until COMMIT, after all reads
        # and writes within the transaction have succeeded.
        self.assertIn(table, {'cards', 'artifacts'})
        event = 'UPDATE' if table == 'cards' else 'INSERT'
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute('CREATE TABLE commit_guard (document_id TEXT REFERENCES documents(id) DEFERRABLE INITIALLY DEFERRED)')
            db.execute(f"CREATE TRIGGER reject_commit AFTER {event} ON {table} BEGIN INSERT INTO commit_guard VALUES ('missing-document'); END")
        original_connect = sqlite3.connect

        def connect(*args, **kwargs):
            db = original_connect(*args, **kwargs)
            db.execute('PRAGMA foreign_keys=ON')
            return db

        return connect

    def test_card_commit_failure_raises_after_snapshot_and_restores_on_restart(self):
        connect = self._deferred_failure('cards')
        with patch('services.work_card_store.sqlite3.connect', side_effect=connect), \
                patch.object(WorkCardStore, '_card_snapshot', wraps=WorkCardStore._card_snapshot) as snapshot:
            with self.assertRaises(sqlite3.IntegrityError):
                self.store.update_card(self.card['id'], {'deadline': '교사 기한'}, 1)
            snapshot.assert_called_once()
        self.assertEqual(WorkCardStore(self.temp.name).get_card(self.card['id']), self.card)
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM commit_guard').fetchone()[0], 0)

    def test_artifact_commit_failure_rolls_back_both_tables_after_snapshot(self):
        connect = self._deferred_failure('artifacts')
        with patch('services.work_card_store.sqlite3.connect', side_effect=connect), \
                patch.object(WorkCardStore, '_artifact_snapshot', wraps=WorkCardStore._artifact_snapshot) as snapshot:
            with self.assertRaises(sqlite3.IntegrityError):
                self.store.save_artifact('synthetic-doc', '안내', '합성 초안', {self.card['id']: 1}, 1)
            snapshot.assert_called_once()
        self.assertEqual(WorkCardStore(self.temp.name).list_artifacts('synthetic-doc'), [])
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM artifact_reviews').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM commit_guard').fetchone()[0], 0)

    def test_failed_artifact_review_insert_does_not_leave_orphan_draft(self):
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute("CREATE TRIGGER reject_review BEFORE INSERT ON artifact_reviews BEGIN SELECT RAISE(ABORT,'synthetic review write'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'review write'):
            self.store.save_artifact('synthetic-doc', '안내', '합성 초안', {self.card['id']: 1}, 1)
        self.assertEqual(WorkCardStore(self.temp.name).list_artifacts('synthetic-doc'), [])
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM artifact_reviews').fetchone()[0], 0)

    def test_workspace_failed_replacement_preserves_previous_pointer_on_restart(self):
        state = WorkspaceStateStore(self.temp.name)
        state.update('synthetic-doc', artifact_id='old-draft', source_version=1)
        with closing(sqlite3.connect(state.path)) as db, db:
            db.execute("CREATE TRIGGER reject_pointer BEFORE INSERT ON workspace_state BEGIN SELECT RAISE(ABORT,'synthetic pointer write'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'pointer write'):
            state.update('synthetic-doc', artifact_id='new-draft', source_version=2)
        self.assertEqual(WorkspaceStateStore(self.temp.name).get('synthetic-doc'),
                         {'artifact_id': 'old-draft', 'source_version': 1})

    def test_transfer_failed_replacement_preserves_guarded_scope_on_restart(self):
        policies = TransferPolicyStore(self.temp.name)
        previous = make_text_snapshot('일반 안내 [가림1]', excluded_strings=['합성제외항목']).policy
        policies.save('synthetic-doc', previous, expected_scope_id=None)
        with closing(sqlite3.connect(policies.path)) as db, db:
            db.execute("CREATE TRIGGER reject_policy BEFORE UPDATE ON transfer_policies BEGIN SELECT RAISE(ABORT,'synthetic policy write'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'policy write'):
            policies.save('synthetic-doc', make_text_snapshot('새 합성 범위').policy,
                          expected_scope_id=previous.scope_id)
        restored = TransferPolicyStore(self.temp.name).get('synthetic-doc')
        self.assertEqual(restored, previous)
        self.assertEqual(restored.guard_text('일반 안내'), '일반 안내')
        with self.assertRaises(ScopeExpansionRequired):
            restored.guard_text('합성제외항목')
        with closing(sqlite3.connect(policies.path)) as db:
            self.assertNotIn('합성제외항목', db.execute('SELECT policy_json FROM transfer_policies').fetchone()[0])

    def test_schema_upgrade_final_failure_rolls_back_ddl_and_preserves_backup(self):
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute('DROP TABLE artifact_reviews')
            db.execute('PRAGMA user_version=1')
        original_connect = sqlite3.connect

        class FailFinalSchemaVersion(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                if sql.strip().replace(' ', '').upper() == 'PRAGMAUSER_VERSION=2':
                    raise sqlite3.OperationalError('synthetic final migration write')
                return super().execute(sql, *args, **kwargs)

        def connect(*args, **kwargs):
            return original_connect(*args, factory=FailFinalSchemaVersion, **kwargs)

        with patch('services.work_card_store.sqlite3.connect', side_effect=connect):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'final migration write'):
                WorkCardStore(self.temp.name)
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='artifact_reviews'").fetchone())
        backups = list(Path(self.temp.name).glob('work_cards.sqlite3.before-work-cards-*.bak'))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT id FROM cards').fetchone()[0], self.card['id'])
        self.assertEqual(WorkCardStore(self.temp.name).get_card(self.card['id']), self.card)
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 2)

    def test_future_schema_rejection_leaves_database_bytes_unchanged(self):
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute('PRAGMA user_version=100')
        before = self.store.path.read_bytes()
        with self.assertRaises(ValueError):
            WorkCardStore(self.temp.name)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(list(Path(self.temp.name).glob('*.bak')), [])


if __name__ == '__main__':
    unittest.main()
