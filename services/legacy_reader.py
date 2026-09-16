"""Read old records without initializing, migrating or writing their databases."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3


def _rows(path, table):
    if not path.exists():
        return []
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            return []
        # Table names are static internal constants, never input from documents.
        return [dict(row) for row in db.execute(f'SELECT * FROM {table}')]


def read_legacy_records(directory):
    from services.card_outputs import current_value
    from services.work_card_store import FIELD_LABELS
    directory = Path(directory)
    lines = ['기존 기록 · 읽기 전용', '이 화면에서 기존 DB를 변경하지 않습니다.', '']
    tasks = _rows(directory / 'ssokly.db', 'tasks')
    managed_ids = {row['id'] for row in _rows(directory / 'document_library.sqlite3', 'documents')}
    for row in tasks:
        if row.get('id') in managed_ids:
            continue
        lines.extend([f"■ {row['title']} [{row['status']}]", row['source_text'], '', row['analysis_text'], ''])
    cards = _rows(directory / 'work_cards.sqlite3', 'cards')
    indexed = {}
    for row in cards:
        card = {**row, 'fields': json.loads(row['fields_json'])}
        indexed[card['id']] = card
        lines.append('■ 기존 업무 카드 · 교사 현재값')
        for field, label in FIELD_LABELS.items():
            value = current_value(card, field)
            if value:
                lines.append(f'{label}: {value}')
        lines.append('')
    artifacts = _rows(directory / 'work_cards.sqlite3', 'artifacts')
    for row in artifacts:
        lines.extend([f"■ 이전 초안 · {row['kind']} · {row['created_at']}", row['content'], ''])
    links = {row['todo_id']: row['card_id'] for row in _rows(directory / 'personal_todos.sqlite3', 'todo_card_links')}
    for row in _rows(directory / 'personal_todos.sqlite3', 'todos'):
        card = indexed.get(links.get(row['id']))
        item = row['item']
        if card:
            # Linked todos historically display the card's current edited values.
            item = '\n'.join(f'{label}: {current_value(card, field)}' for field, label in FIELD_LABELS.items()
                             if current_value(card, field))
        lines.extend([f"■ 기존 내 할 일 · {'완료' if row['done'] else '미완료'}", item, ''])
    return '\n'.join(lines) if len(lines) > 3 else '\n'.join(lines + ['저장된 기존 기록이 없습니다.'])
