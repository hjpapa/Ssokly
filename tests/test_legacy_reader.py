"""Read-only legacy rendering against isolated synthetic SQLite fixtures."""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from services.legacy_reader import read_legacy_records
from services.work_card_store import CARD_FIELDS


class LegacyReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def database(self, name, statements):
        path = self.directory / name
        with closing(sqlite3.connect(path)) as db, db:
            for sql, parameters in statements:
                db.execute(sql, parameters)
        return path

    def test_absent_legacy_data_does_not_create_or_migrate_databases(self):
        text = read_legacy_records(self.directory)
        self.assertIn('저장된 기존 기록이 없습니다', text)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_tasks_cards_artifacts_and_linked_todos_preserve_teacher_current_values(self):
        fields = {name: {'value': '', 'edited': True, 'confirmed': False, 'evidence': {}} for name in CARD_FIELDS}
        fields['action']['value'] = '교사가 수정한 행동'
        fields['deadline']['value'] = ''  # An intentional deletion must stay absent.
        paths = [self.database('ssokly.db', [
            ('CREATE TABLE tasks(title TEXT,status TEXT,source_text TEXT,analysis_text TEXT)', ()),
            ('INSERT INTO tasks VALUES(?,?,?,?)', ('합성 업무', 'open', '합성 원문', '이전 실행안')),
        ]), self.database('work_cards.sqlite3', [
            ('CREATE TABLE cards(id TEXT,fields_json TEXT)', ()),
            ('INSERT INTO cards VALUES(?,?)', ('card-one', json.dumps(fields, ensure_ascii=False))),
            ('CREATE TABLE artifacts(kind TEXT,created_at TEXT,content TEXT)', ()),
            ('INSERT INTO artifacts VALUES(?,?,?)', ('요약', '2026-09-17', '직접 수정한 이전 초안')),
        ]), self.database('personal_todos.sqlite3', [
            ('CREATE TABLE todos(id INTEGER,item TEXT,done INTEGER)', ()),
            ('CREATE TABLE todo_card_links(todo_id INTEGER,card_id TEXT)', ()),
            ('INSERT INTO todos VALUES(?,?,?)', (1, '오래된 행동 · 10월 1일', 1)),
            ('INSERT INTO todos VALUES(?,?,?)', (2, '독립적인 이전 할 일', 0)),
            ('INSERT INTO todo_card_links VALUES(?,?)', (1, 'card-one')),
        ])]
        snapshots = {path: path.read_bytes() for path in paths}
        text = read_legacy_records(self.directory)
        self.assertIn('합성 원문', text)
        self.assertIn('이전 실행안', text)
        self.assertIn('직접 수정한 이전 초안', text)
        self.assertIn('교사가 수정한 행동', text)
        self.assertIn('독립적인 이전 할 일', text)
        self.assertIn('기존 내 할 일 · 완료', text)
        self.assertNotIn('오래된 행동', text)
        self.assertNotIn('10월 1일', text)
        self.assertEqual({path: path.read_bytes() for path in paths}, snapshots)
        self.assertEqual(set(self.directory.iterdir()), set(paths))

    def test_missing_tables_are_skipped_without_schema_changes(self):
        path = self.database('work_cards.sqlite3', [('CREATE TABLE historical_table(value TEXT)', ())])
        before = path.read_bytes()
        self.assertIn('저장된 기존 기록이 없습니다', read_legacy_records(self.directory))
        self.assertEqual(path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
