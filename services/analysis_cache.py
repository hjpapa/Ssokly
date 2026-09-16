from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
from services.analysis_document import AnalysisDocument

class AnalysisCache:
    def __init__(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "analysis_cache.sqlite3"
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS analyses (key TEXT PRIMARY KEY, payload TEXT NOT NULL)")

    @staticmethod
    def key(source, role, model, variant="internal"):
        return hashlib.sha256(json.dumps(["school-date-qualifiers-v10", source, role, model, variant], ensure_ascii=False).encode()).hexdigest()

    def get(self, key):
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute("SELECT payload FROM analyses WHERE key = ?", (key,)).fetchone()
        if row:
            try:
                return AnalysisDocument.model_validate_json(row[0])
            except ValueError:
                return None
        return None

    def put(self, key, document):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT OR REPLACE INTO analyses VALUES (?, ?)", (key, document.model_dump_json()))
