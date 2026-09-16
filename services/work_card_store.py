"""Versioned, local work facts. Teacher values never become source quotations."""
from contextlib import contextmanager, closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid


CARD_FIELDS = (
    'action', 'owner', 'target', 'condition', 'obligation', 'deadline',
    'event_date', 'report_date', 'deliverable', 'destination',
    'preparation_date', 'notes',
)
FIELD_LABELS = dict(zip(CARD_FIELDS, (
    '해야 할 일', '담당', '대상', '업무 조건', '업무 구분', '제출 기한',
    '행사일', '보고일', '제출물', '제출처', '개인 준비일', '메모',
)))


class CardConflictError(ValueError):
    """The caller edited an obsolete card snapshot; keep its unsaved editor open."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def backup_database(path):
    """SQLite's backup API includes committed WAL data; failure aborts migration."""
    path = Path(path)
    backup = path.with_name(path.name + '.before-work-cards-' + uuid.uuid4().hex[:12] + '.bak')
    with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(backup)) as dest:
        source.backup(dest)
    return backup


def _normalized_with_offsets(text):
    chars, offsets = [], []
    for index, char in enumerate(text):
        if not char.isspace():
            chars.append(char)
            offsets.append(index)
    return ''.join(chars), offsets


def evidence_record(quote, text, document_id, source_version, selected_locations=None):
    """Verify a quotation locally; derive locations instead of trusting AI pages."""
    quote = str(quote or '')
    normalized, offsets = _normalized_with_offsets(text)
    needle = ''.join(quote.split())
    matches = []
    if needle and not re.search(r'⟦|\[확인 필요\]', quote):
        start = 0
        while (pos := normalized.find(needle, start)) >= 0:
            left, right = offsets[pos], offsets[pos + len(needle) - 1] + 1
            matches.append({'start': left, 'end': right, 'line': text.count('\n', 0, left) + 1})
            start = pos + 1
    selected, invalid = [], False
    if selected_locations:
        lines = text.splitlines(keepends=True)
        for location in selected_locations:
            line, cell = location.get('line'), location.get('cell')
            if not isinstance(line, int) or not isinstance(cell, int) or line < 1 or line > len(lines) or cell < 0:
                invalid = True
                continue
            raw = lines[line - 1].rstrip('\r\n')
            left = sum(len(item) for item in lines[:line - 1])
            if cell:
                cells = raw.split('\t')
                if cell > len(cells):
                    invalid = True
                    continue
                left += sum(len(item) + 1 for item in cells[:cell - 1])
                raw = cells[cell - 1]
            right = left + len(raw)
            local = [dict(match, cell=cell) for match in matches if left <= match['start'] and match['end'] <= right]
            if not local:
                invalid = True
            selected.extend(local)
        if selected and not invalid:
            matches = sorted([dict(items) for items in {tuple(sorted(item.items())) for item in selected}],
                             key=lambda item: (item['start'], item['end'], item.get('cell', 0)))
    return {
        'quote': quote, 'document_id': document_id, 'source_version': source_version,
        'verified': bool(matches), 'ambiguous': len(matches) > 1,
        'locations': matches, 'stale': False, 'location_verified': bool(selected) and not invalid,
        'location_invalid': invalid,
    }


def render_card_item(card):
    from services.card_outputs import current_value
    values = {name: current_value(card, name) for name in CARD_FIELDS}
    details = [f'{FIELD_LABELS[name]}: {values[name]}' for name in CARD_FIELDS
               if name != 'action' and values[name]]
    if card.get('source_stale'):
        details.append('원문 변경 · 이전 근거 재확인 필요')
    return '\n'.join([values['action'] or '업무 내용 미지정', *details])


def _review_signature(fields):
    return hashlib.sha256(_json({name: bool(field['confirmed']) for name, field in fields.items()}).encode()).hexdigest()


class WorkCardStore:
    """Additive database alongside legacy stores; all writes are transactional."""

    def __init__(self, directory):
        self.path = Path(directory) / 'work_cards.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            with closing(sqlite3.connect(self.path)) as db:
                version = db.execute('PRAGMA user_version').fetchone()[0]
                if version > 2:
                    raise ValueError('새 버전 업무 데이터입니다. 앱 업데이트 후 열어 주세요.')
                if version < 2:
                    backup_database(self.path)
        with self._db(write=True) as db:
            schema = '''
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY, version INTEGER NOT NULL,
                    text TEXT NOT NULL, source_kind TEXT NOT NULL, source_ref TEXT NOT NULL,
                    scope TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS source_versions (
                    document_id TEXT NOT NULL, version INTEGER NOT NULL,
                    text TEXT NOT NULL, source_kind TEXT NOT NULL, source_ref TEXT NOT NULL,
                    scope TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(document_id, version));
                CREATE TABLE IF NOT EXISTS cards (
                    id TEXT PRIMARY KEY, document_id TEXT NOT NULL,
                    version INTEGER NOT NULL, source_version INTEGER NOT NULL,
                    identity TEXT NOT NULL, fields_json TEXT NOT NULL,
                    proposal_json TEXT NOT NULL, comparison_candidate INTEGER NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS cards_by_document ON cards(document_id);
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY, document_id TEXT NOT NULL, kind TEXT NOT NULL,
                    content TEXT NOT NULL, card_versions_json TEXT NOT NULL,
                    source_version INTEGER NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS artifact_reviews (
                    artifact_id TEXT PRIMARY KEY, signatures_json TEXT NOT NULL);
                PRAGMA user_version=2;
            '''
            for statement in schema.split(';'):
                if statement.strip():
                    db.execute(statement)

    @contextmanager
    def _db(self, write=False):
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            if write:
                db.execute('BEGIN IMMEDIATE')
            with db:
                yield db

    def ensure_document(self, document_id, text, source_kind='검수본', source_ref='', scope='일부'):
        if not document_id:
            raise ValueError('문서 ID가 필요합니다.')
        values = (str(text), str(source_kind), str(source_ref), str(scope))
        with self._db(write=True) as db:
            row = db.execute('SELECT * FROM documents WHERE id=?', (document_id,)).fetchone()
            if row and values == tuple(row[name] for name in ('text', 'source_kind', 'source_ref', 'scope')):
                return dict(row)
            version = row['version'] + 1 if row else 1
            now = _now()
            db.execute('INSERT OR REPLACE INTO documents VALUES (?,?,?,?,?,?,?)',
                       (document_id, version, *values, now))
            db.execute('INSERT INTO source_versions VALUES (?,?,?,?,?,?,?)',
                       (document_id, version, *values, now))
            return dict(db.execute('SELECT * FROM documents WHERE id=?', (document_id,)).fetchone())

    def get_document(self, document_id, version=None):
        with self._db() as db:
            if version is None:
                row = db.execute('SELECT * FROM documents WHERE id=?', (document_id,)).fetchone()
            else:
                row = db.execute('SELECT * FROM source_versions WHERE document_id=? AND version=?',
                                 (document_id, version)).fetchone()
            return dict(row) if row else None

    @staticmethod
    def _decode_card(row, current_source_version):
        if row is None:
            return None
        card = dict(row)
        card['fields'] = json.loads(card.pop('fields_json'))
        card['ai_proposal'] = json.loads(card.pop('proposal_json'))
        card['review_signature'] = _review_signature(card['fields'])
        card['comparison_candidate'] = bool(card['comparison_candidate'])
        card['source_stale'] = current_source_version != card['source_version']
        for name, field in card['fields'].items():
            field['evidence']['stale'] = current_source_version != field['evidence']['source_version']
            card[name] = field['value']
        return card

    def list_cards(self, document_id):
        with self._db() as db:
            document = db.execute('SELECT version FROM documents WHERE id=?', (document_id,)).fetchone()
            if document is None:
                return []
            return [self._decode_card(row, document['version']) for row in db.execute(
                'SELECT * FROM cards WHERE document_id=? ORDER BY rowid', (document_id,))]

    def get_card(self, card_id):
        with self._db() as db:
            row = db.execute('SELECT cards.*, documents.version AS current_source_version FROM cards '
                             'JOIN documents ON documents.id=cards.document_id WHERE cards.id=?',
                             (card_id,)).fetchone()
            card = self._decode_card(row, row['current_source_version']) if row else None
            if card:
                card.pop('current_source_version', None)
            return card

    @staticmethod
    def _proposal_fields(action, source, document_id, source_version):
        quotes = action.get('field_evidence') or {}
        evidence = action.get('evidence') or ''
        checked_fields = action.get('fields') or {}
        fields = {}
        for name in CARD_FIELDS:
            value = str(action.get(name) or '')
            checked = checked_fields.get(name) or {}
            quote = checked.get('quote') or (evidence if name == 'action' else quotes.get(name, '') or evidence)
            if isinstance(quote, dict):
                quote = quote.get('quote', '')
            locations = (action.get('evidence_locations') or {}).get(name)
            record = evidence_record(quote, source, document_id, source_version, locations)
            record['quote_verified'] = record['verified']
            issues = list(checked.get('issues') or (action.get('field_issues') or {}).get(name) or [])
            if record['location_invalid']:
                issues.append('선택한 원문 위치 확인 필요')
            if record['location_verified'] and not record['ambiguous']:
                issues = [issue for issue in issues if issue != '동일 근거가 여러 위치에 있음']
            if 'verified' in checked:
                record['verified'] = record['verified'] and bool(checked['verified'])
            elif name in ('deadline', 'event_date', 'report_date'):
                from services.date_evidence import supported_deadline
                record['verified'] = record['verified'] and bool(value) and supported_deadline(value, quote) is not None
            elif name not in ('action', 'obligation'):
                record['verified'] = record['verified'] and bool(value) and ''.join(value.split()) in ''.join(quote.split())
            if value and not record['verified'] and name not in ('preparation_date', 'notes') and '근거 확인 필요' not in issues:
                issues.append('근거 확인 필요')
            fields[name] = {
                'ai_value': value, 'value': value, 'edited': False, 'confirmed': False,
                'evidence': record, 'issues': issues,
            }
        return fields

    @staticmethod
    def _identity(fields, action):
        evidence = fields['action']['evidence']
        if not evidence['verified'] or evidence['ambiguous']:
            return ''
        # No fuzzy title matching. Different field evidence denotes another candidate.
        exact = {'action': action.get('action', ''),
                 'quotes': {key: value['evidence']['quote'] for key, value in fields.items()},
                 'task_type': action.get('task_type', '학교 업무')}
        if any(field['evidence'].get('location_verified') for field in fields.values()):
            exact['locations'] = {name: [{'line': item['line'], 'cell': item.get('cell', 0)} for item in field['evidence']['locations']]
                                  for name, field in fields.items() if field['evidence'].get('location_verified')}
        return hashlib.sha256(_json(exact).encode()).hexdigest()

    def merge_analysis(self, document_id, actions, source_version=None):
        """A late source response remains a candidate; teacher edits always win."""
        result_ids = []
        with self._db(write=True) as db:
            document = db.execute('SELECT * FROM documents WHERE id=?', (document_id,)).fetchone()
            if document is None:
                raise KeyError(document_id)
            source_version = document['version'] if source_version is None else source_version
            source = db.execute('SELECT text FROM source_versions WHERE document_id=? AND version=?',
                                (document_id, source_version)).fetchone()
            if source is None:
                raise ValueError('분석에 사용한 원문 버전을 찾을 수 없습니다.')
            incoming = []
            for proposal in actions:
                action = proposal.model_dump() if hasattr(proposal, 'model_dump') else dict(proposal)
                fields = self._proposal_fields(action, source['text'], document_id, source_version)
                incoming.append((action, fields, self._identity(fields, action)))
            existing = list(db.execute('SELECT * FROM cards WHERE document_id=?', (document_id,)))
            for action, fields, identity in incoming:
                matches = [row for row in existing if identity and row['identity'] == identity
                           and not row['comparison_candidate']]
                unambiguous = identity and sum(entry[2] == identity for entry in incoming) == 1
                match = matches[0] if len(matches) == 1 and unambiguous and source_version == document['version'] else None
                now = _now()
                if match:
                    old = json.loads(match['fields_json'])
                    for name, field in fields.items():
                        previous = old[name]
                        if previous['edited']:
                            field['value'] = previous['value']
                            field['edited'] = True
                            field['confirmed'] = previous['confirmed']
                        elif previous['value'] == field['value'] and previous['evidence'] == field['evidence']:
                            field['confirmed'] = previous['confirmed']
                    changed = fields != old or source_version != match['source_version']
                    db.execute('UPDATE cards SET version=?,source_version=?,fields_json=?,proposal_json=?,updated_at=? WHERE id=?',
                               (match['version'] + int(changed), source_version, _json(fields), _json(action), now, match['id']))
                    result_ids.append(match['id'])
                else:
                    key = uuid.uuid4().hex
                    candidate = bool(existing) or not unambiguous or source_version != document['version']
                    db.execute('INSERT INTO cards VALUES (?,?,?,?,?,?,?,?,?,?)',
                               (key, document_id, 1, source_version, identity, _json(fields), _json(action), int(candidate), now, now))
                    result_ids.append(key)
        return [self.get_card(key) for key in result_ids]

    def update_card(self, card_id, changes, expected_version, *, confirmation_changes=None, expected_review_signature=None):
        confirmation_changes = confirmation_changes or {}
        unknown = set(changes) - set(CARD_FIELDS)
        unknown |= set(confirmation_changes) - set(CARD_FIELDS)
        if unknown:
            raise ValueError('수정할 수 없는 업무 필드입니다: ' + ', '.join(sorted(unknown)))
        with self._db(write=True) as db:
            row = db.execute('SELECT * FROM cards WHERE id=?', (card_id,)).fetchone()
            if row is None:
                raise KeyError(card_id)
            if row['version'] != expected_version:
                raise CardConflictError('다른 창에서 업무가 변경되었습니다. 편집값을 보관하고 현재 업무와 비교하세요.')
            fields = json.loads(row['fields_json'])
            if expected_review_signature is not None and _review_signature(fields) != expected_review_signature:
                raise CardConflictError('확인 상태가 변경되었습니다. 현재 값과 비교한 뒤 다시 저장하세요.')
            before_content = {name: {key: value for key, value in field.items() if key != 'confirmed'} for name, field in fields.items()}
            for name, value in changes.items():
                if not isinstance(value, str):
                    raise TypeError('업무 수정값은 문자열이어야 합니다.')
                fields[name].update(value=value, edited=True, confirmed=False)
            for name, confirmed in confirmation_changes.items():
                if not isinstance(confirmed, bool):
                    raise TypeError('확인 상태는 참/거짓이어야 합니다.')
                fields[name]['confirmed'] = confirmed
            if fields != json.loads(row['fields_json']):
                after_content = {name: {key: value for key, value in field.items() if key != 'confirmed'} for name, field in fields.items()}
                db.execute('UPDATE cards SET fields_json=?,version=version+?,updated_at=? WHERE id=?',
                           (_json(fields), int(before_content != after_content), _now(), card_id))
        return self.get_card(card_id)

    def set_confirmations(self, card_id, changes, expected_version=None, expected_review_signature=None):
        if expected_version is None:
            card = self.get_card(card_id)
            if card is None:
                raise KeyError(card_id)
            expected_version = card['version']
        return self.update_card(card_id, {}, expected_version, confirmation_changes=changes,
                                expected_review_signature=expected_review_signature)

    def confirm_fields(self, card_id, names, expected_version=None, expected_review_signature=None):
        return self.set_confirmations(card_id, {name: True for name in names}, expected_version, expected_review_signature)

    def review_signatures(self, document_id):
        with self._db() as db:
            return self._review_signatures(db, document_id)

    @staticmethod
    def _review_signatures(db, document_id):
        return {row['id']: _review_signature(json.loads(row['fields_json'])) for row in db.execute(
            'SELECT id,fields_json FROM cards WHERE document_id=? AND comparison_candidate=0', (document_id,))}

    def adopt_card(self, card_id, expected_version):
        """Explicitly accept a comparison as a separate task; never copy old edits."""
        with self._db(write=True) as db:
            row = db.execute('SELECT * FROM cards WHERE id=?', (card_id,)).fetchone()
            if row is None:
                raise KeyError(card_id)
            if row['version'] != expected_version:
                raise CardConflictError('업무가 변경되어 비교 후보를 채택하지 않았습니다.')
            if row['comparison_candidate']:
                db.execute('UPDATE cards SET comparison_candidate=0,version=version+1,updated_at=? WHERE id=?', (_now(), card_id))
        return self.get_card(card_id)

    def save_artifact(self, document_id, kind, content, card_versions, source_version, review_signatures=None):
        key = uuid.uuid4().hex
        with self._db(write=True) as db:
            if db.execute('SELECT 1 FROM source_versions WHERE document_id=? AND version=?',
                          (document_id, source_version)).fetchone() is None:
                raise ValueError('결과물의 원문 버전이 없습니다.')
            for card_id, version in card_versions.items():
                row = db.execute('SELECT version FROM cards WHERE id=? AND document_id=?', (card_id, document_id)).fetchone()
                if not row or not isinstance(version, int) or version < 1 or version > row['version']:
                    raise ValueError('결과물의 업무 버전이 올바르지 않습니다.')
            db.execute('INSERT INTO artifacts VALUES (?,?,?,?,?,?,?)',
                       (key, document_id, str(kind), str(content), _json(card_versions), source_version, _now()))
            signatures = self._review_signatures(db, document_id) if review_signatures is None else review_signatures
            db.execute('INSERT INTO artifact_reviews VALUES (?,?)', (key, _json(signatures)))
        return next(item for item in self.list_artifacts(document_id) if item['id'] == key)

    def list_artifacts(self, document_id):
        with self._db() as db:
            result = []
            for row in db.execute('SELECT * FROM artifacts WHERE document_id=? ORDER BY rowid DESC', (document_id,)):
                item = dict(row)
                item['card_versions'] = json.loads(item.pop('card_versions_json'))
                review = db.execute('SELECT signatures_json FROM artifact_reviews WHERE artifact_id=?', (item['id'],)).fetchone()
                item['review_signatures'] = json.loads(review['signatures_json']) if review else None
                item['stale'] = self._artifact_stale(db, item)
                result.append(item)
            return result

    @staticmethod
    def _artifact_stale(db, artifact):
        doc = db.execute('SELECT version FROM documents WHERE id=?', (artifact['document_id'],)).fetchone()
        if not doc or doc['version'] != artifact['source_version']:
            return True
        active_ids = {row['id'] for row in db.execute(
            'SELECT id FROM cards WHERE document_id=? AND comparison_candidate=0', (artifact['document_id'],))}
        if active_ids != set(artifact['card_versions']):
            return True
        review = db.execute('SELECT signatures_json FROM artifact_reviews WHERE artifact_id=?', (artifact['id'],)).fetchone()
        if review is None or json.loads(review['signatures_json']) != WorkCardStore._review_signatures(db, artifact['document_id']):
            return True
        for key, version in artifact['card_versions'].items():
            current = db.execute('SELECT version FROM cards WHERE id=?', (key,)).fetchone()
            if not current or current['version'] != version:
                return True
        return False

    def artifact_is_stale(self, artifact):
        with self._db() as db:
            if isinstance(artifact, str):
                row = db.execute('SELECT * FROM artifacts WHERE id=?', (artifact,)).fetchone()
                if row is None:
                    raise KeyError(artifact)
                artifact = dict(row)
                artifact['card_versions'] = json.loads(artifact.pop('card_versions_json'))
            return self._artifact_stale(db, artifact)
