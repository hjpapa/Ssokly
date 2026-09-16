"""Small local pointers, separate from immutable source/artifact history."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3


def text_fingerprint(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


class WorkspaceStateStore:
    def __init__(self, directory):
        self.path = Path(directory) / 'workspace_state.sqlite3'
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS workspace_state (id TEXT PRIMARY KEY, value TEXT NOT NULL)')

    def get(self, document_id):
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute('SELECT value FROM workspace_state WHERE id=?', (document_id,)).fetchone()
            return json.loads(row[0]) if row else {}

    def update(self, document_id, **changes):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT value FROM workspace_state WHERE id=?', (document_id,)).fetchone()
            state = json.loads(row[0]) if row else {}
            state.update(changes)
            db.execute('INSERT OR REPLACE INTO workspace_state VALUES (?,?)',
                       (document_id, json.dumps(state, ensure_ascii=False)))
