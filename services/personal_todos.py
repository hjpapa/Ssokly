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
    def __init__(self, directory, card_store=None):
        self.path = Path(directory) / "personal_todos.sqlite3"
        self.card_store = card_store
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            with closing(sqlite3.connect(self.path)) as db:
                has_links = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='todo_card_links'").fetchone()
            if not has_links:
                from services.work_card_store import backup_database
                backup_database(self.path)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS todos (id TEXT PRIMARY KEY, item TEXT NOT NULL, source TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0)")
            db.execute("CREATE TABLE IF NOT EXISTS analysis_origins (fingerprint TEXT PRIMARY KEY)")
            # Separate additive relation: old todo rows and completion flags stay intact.
            db.execute("CREATE TABLE IF NOT EXISTS todo_card_links (todo_id TEXT PRIMARY KEY, card_id TEXT NOT NULL UNIQUE)")

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

    def add_cards(self, cards, card_store=None):
        from services.work_card_store import WorkCardStore, render_card_item
        store = card_store or self.card_store
        if store is None:
            store = WorkCardStore(self.path.parent)
        count = 0
        with closing(sqlite3.connect(self.path)) as db, db:
            for supplied in cards:
                card_id = supplied if isinstance(supplied, str) else supplied['id']
                card = store.get_card(card_id)
                if card is None:
                    raise KeyError(card_id)
                if db.execute('SELECT 1 FROM todo_card_links WHERE card_id=?', (card_id,)).fetchone():
                    continue
                key = 'card:' + card_id
                document = store.get_document(card['document_id'])
                db.execute('INSERT INTO todos(id,item,source) VALUES (?,?,?)',
                           (key, render_card_item(card), document['text']))
                db.execute('INSERT INTO todo_card_links VALUES (?,?)', (key, card_id))
                count += 1
        return count

    def list(self, card_store=None):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            rows = [dict(row) for row in db.execute(
                'SELECT todos.*,todo_card_links.card_id FROM todos LEFT JOIN todo_card_links '
                'ON todos.id=todo_card_links.todo_id ORDER BY done,todos.rowid DESC')]
        if any(row['card_id'] for row in rows):
            from services.work_card_store import WorkCardStore, render_card_item
            store = card_store or self.card_store or WorkCardStore(self.path.parent)
            for row in rows:
                if row['card_id']:
                    card = store.get_card(row['card_id'])
                    row['card_missing'] = card is None
                    if card:
                        row['item'] = render_card_item(card)
                        row['card_version'] = card['version']
        return rows

    def set_done(self, ids, done):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executemany("UPDATE todos SET done=? WHERE id=?", [(int(done), key) for key in ids])

    def delete(self, ids):
        ids = list(ids)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executemany("DELETE FROM todo_card_links WHERE todo_id=?", [(key,) for key in ids])
            db.executemany("DELETE FROM todos WHERE id=?", [(key,) for key in ids])
