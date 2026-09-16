"""Local personal selections, independent of regenerated analysis and source tasks."""
from contextlib import closing
import hashlib
from pathlib import Path
import re
import sqlite3


def checklist_items(markdown):
    """Read only unchecked items in the checklist section, including their details."""
    from services.workflow_service import extract_section
    section = extract_section(markdown, "checklist")
    items = []
    current = None
    for line in section.splitlines():
        match = re.match(r"^- \[([ xX])\] (.+)$", line)
        if match:
            current = [match[2]] if match[1] == " " else None
            if current is not None:
                items.append(current)
        elif current is not None and line.startswith("  "):
            current.append(line.strip())
        else:
            current = None
    return list(dict.fromkeys("\n".join(item) for item in items))


class PersonalTodoStore:
    def __init__(self, directory):
        self.path = Path(directory) / "personal_todos.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS todos (id TEXT PRIMARY KEY, item TEXT NOT NULL, source TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0)")
            db.execute("CREATE TABLE IF NOT EXISTS analysis_origins (fingerprint TEXT PRIMARY KEY)")

    @staticmethod
    def _fingerprint(result, source):
        return hashlib.sha256((source.strip() + '\0' + result.strip()).encode()).hexdigest()

    def remember_analysis(self, result, source):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT OR IGNORE INTO analysis_origins VALUES (?)", (self._fingerprint(result, source),))

    def matches_analysis(self, result, source):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute("SELECT 1 FROM analysis_origins WHERE fingerprint=?", (self._fingerprint(result, source),)).fetchone() is not None

    def add(self, items, source):
        count = 0
        with closing(sqlite3.connect(self.path)) as db, db:
            for item in items:
                if not item.strip():
                    continue
                key = hashlib.sha256((source + '\0' + item).encode()).hexdigest()
                count += db.execute("INSERT OR IGNORE INTO todos(id,item,source) VALUES(?,?,?)", (key, item, source)).rowcount
        return count

    def list(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM todos ORDER BY done, rowid DESC")]

    def set_done(self, ids, done):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executemany("UPDATE todos SET done=? WHERE id=?", [(int(done), key) for key in ids])

    def delete(self, ids):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executemany("DELETE FROM todos WHERE id=?", [(key,) for key in ids])
