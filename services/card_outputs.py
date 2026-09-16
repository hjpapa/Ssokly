"""Local drafts: copy current card facts exactly, never re-infer dates from OCR."""
import re
from services.work_card_store import CARD_FIELDS, FIELD_LABELS
from services.date_evidence import date_mentions, date_status, source_schedule_entries, RELATIVE_DATE


DATE_FIELDS = ('deadline', 'event_date', 'report_date')
SOFT_DATE_ISSUES = {'연도 미지정', '연도 미기재', '상대 기한: 기준일 확인 필요'}
PUBLIC_PERSON = re.compile(r'(?<![가-힣])(?:초등학생|중학생|고등학생|학생|학부모|보호자|가정)(?:들|님)?(?:은|는|이|가|에게|와|과)?(?=$|[\s,·/()])')
STAFF_ACTOR = re.compile(r'교사|담임|교직원|부장|행정|교무|연구부|학생부|학생지원|학생안전|학생생활|교육청|지원청|센터|위원회|학부모회|학생회|담당자|담당교')


def is_public_person(value):
    """Match recipient roles, not organizations which happen to contain 학생."""
    return bool(PUBLIC_PERSON.search(value)) and not STAFF_ACTOR.search(value)


def _action_without_duplicate_dates(card, value):
    """AI action wording must not silently restore a cleared/changed date field."""
    spans = [(item.start, item.end) for item in date_mentions(value)]
    spans.extend(match.span() for match in RELATIVE_DATE.finditer(value))
    if not spans:
        return value
    separated = any(card['fields'][name]['value'] or card['fields'][name]['edited'] for name in DATE_FIELDS)
    if not separated:
        return value + ' [확인 필요: 행동 설명에 포함된 날짜의 역할을 확인하세요]'
    for start, end in sorted(set(spans), reverse=True):
        value = value[:start] + '[최신 날짜 항목 참조]' + value[end:]
    return value + ' [안내: AI 행동 문구의 날짜는 별도 일정 필드에서 확인]'


def current_value(card, field):
    data = card['fields'][field]
    value = data['value']
    if not value:
        return ''
    if data['edited']:
        return value
    if data['confirmed']:
        return _action_without_duplicate_dates(card, value) if field == 'action' else value
    evidence = data['evidence']
    issues = data.get('issues', card.get('ai_proposal', {}).get('field_issues', {}).get(field, []))
    if evidence.get('verified') and not evidence.get('stale'):
        if not issues:
            return _action_without_duplicate_dates(card, value) if field == 'action' else value
        if field in DATE_FIELDS:
            # Missing year/base/weekday conflict does not erase the date read
            # from the source. Keep its surface and its explicit review reason.
            return value + ' [확인 필요: ' + ' · '.join(issues) + ']'
    if field == 'obligation':
        return value  # Classification is explicitly labelled, not a source quotation.
    return '[확인 필요]'


def _has_blocking_fact_review(card, values):
    for name, data in card['fields'].items():
        if not data['value'] or data['edited'] or data['confirmed']:
            continue
        if values[name] == '[확인 필요]':
            return True
        issues = data.get('issues', [])
        if name in DATE_FIELDS and any(issue not in SOFT_DATE_ISSUES for issue in issues):
            return True
    return False


def unlinked_source_schedules(cards, source):
    """Keep unassigned source dates separate from versioned current card facts.

    Covered dates remain covered even after a teacher clears/changes a value.
    Only verified, current, unambiguous source spans can hide an occurrence;
    matching date text alone must not hide the same date in another event cell.
    """
    entries = source_schedule_entries(source) if source else []
    covered = set()
    for card in cards:
        if card.get('comparison_candidate'):
            continue
        for name in ('action', *DATE_FIELDS):
            field = card.get('fields', {}).get(name) or {}
            evidence = field.get('evidence') or {}
            if (not evidence.get('verified') or evidence.get('stale') or evidence.get('ambiguous')
                    or evidence.get('location_invalid')):
                continue
            quote = ''.join(str(evidence.get('quote') or '').split())
            if not quote:
                continue
            for location in evidence.get('locations', []):
                left, right = location.get('start'), location.get('end')
                if (not isinstance(left, int) or not isinstance(right, int)
                        or not 0 <= left < right <= len(source)
                        or ''.join(source[left:right].split()) != quote):
                    continue
                overlaps = [(index, entry) for index, entry in enumerate(entries)
                            if left < entry['end'] and right > entry['start']]
                # A broad action quote spanning multiple event columns does not
                # prove that every neighboring event date belongs to this card.
                table_cells = {(entry['line'], entry['cell']) for _, entry in overlaps if entry['cell']}
                if len(table_cells) > 1:
                    continue
                covered.update(index for index, _ in overlaps)
    return [entry for index, entry in enumerate(entries) if index not in covered]


def render_current_cards(cards, mode, *, title='업무 정리', source=''):
    cards = [card for card in cards if not card.get('comparison_candidate')]
    values = [{field: current_value(card, field) for field in CARD_FIELDS} for card in cards]
    notice = '현재 불러온 범위 기준입니다. 미확인 붙임·추가 공문은 포함하지 않습니다.'
    if mode in {'학부모 메신저', '가정통신문 초안'}:
        # Internal staff deadlines/destinations/notes never enter public drafts.
        lines = []
        internal = re.compile(r'내부|결재|취합|보고|교무|연구부|행정실')
        for value in values:
            audience = value['target']
            if not is_public_person(audience):
                continue
            public_action = is_public_person(value['owner'])
            if not value['event_date'] and not public_action:
                continue
            parts = [f"대상: {audience}"]
            if value['event_date']:
                parts.append(f"행사일: {value['event_date']}")
            # The recipient must see opt-in/eligibility conditions even when a
            # teacher owns the organising task. Never turn optional into universal.
            if value['condition']:
                parts.append(f"조건: {value['condition']}")
            if public_action and not internal.search(value['action']):
                parts.append(f"안내: {value['action']}")
                if value['deadline']:
                    parts.append(f"신청 기한: {value['deadline']}")
                for field, label in (('deliverable', '준비·회신 자료'), ('destination', '회신처')):
                    if value[field] and value[field] != '[확인 필요]' and not internal.search(value[field]):
                        parts.append(f'{label}: {value[field]}')
            lines.append(' · '.join(parts))
        if not lines:
            return '# 학교 안내 초안\n\n## 발송 전 확인\n학부모에게 안내할 대상·행사일·참여 행동의 근거를 카드에서 확인해 주세요. 내부 제출 업무는 본문에 넣지 않았습니다.'
        numbered = '\n'.join(f'{index}. {line}' for index, line in enumerate(lines, 1))
        body = '학부모님 안녕하세요.\n다음 내용을 안내드립니다.\n\n' + numbered
        if mode == '가정통신문 초안':
            body += '\n\n협조 사항: 위 대상·조건에 해당하는 경우 안내 내용을 확인해 주세요.\n문의처: [공개 가능한 학교 연락처 확인 필요]\n발행 정보: [확인 필요]'
        # Conservative public contact/identifier guard; no AI request here.
        from services.analysis_document import ParentDraft, review_parent_draft
        checked = review_parent_draft(ParentDraft(title='학교 안내', message=body, questions=[]))
        questions = ['발송 전 대상·일정·학교 공개 기준을 확인하세요.', *checked.questions]
        return '# 학교 안내 초안\n\n## 전달 문구\n' + checked.message + '\n\n## 발송 전 확인\n' + '\n'.join(questions)

    rows = []
    references = []
    proposals = []
    pending = []
    schedules = []
    for card, value in zip(cards, values):
        action = value['action'] or '[업무 내용 확인 필요]'
        classification = value['obligation'] or '판단 유보'
        task_type = card.get('ai_proposal', {}).get('task_type', '학교 업무')
        detail = [f'{FIELD_LABELS[field]}: {value[field]}' for field in CARD_FIELDS
                  if field not in {'action', 'obligation'} and value[field]]
        body = f'[{classification}] {action}' + ('\n  ' + ' · '.join(detail) if detail else '')
        if task_type in {'외부 기관 업무', '참고 일정'} or classification == '안내':
            references.append(f'- [{task_type}] {body}')
        elif classification == '추가 제안':
            proposals.append('- ' + body)
        elif classification == '판단 유보' or classification not in {'필수', '조건부'} or _has_blocking_fact_review(card, value):
            pending.append('- ' + body)
        else:
            rows.append('- [ ] ' + body)
        dates = [f'{FIELD_LABELS[field]}: {value[field]}' for field in ('deadline', 'event_date', 'report_date', 'preparation_date') if value[field]]
        if dates:
            schedules.append(f'- {action} — ' + ' · '.join(dates))
    if not rows:
        rows = ['현재 불러온 범위에서는 실행 체크리스트로 정리할 필수·조건부 업무를 확인하지 못했습니다. 비교 후보·원문·미확인 붙임을 확인하세요.']
    header = f'# {title}\n\n{notice}\n'
    supplemental = ''
    for label, items in (('참고 일정·외부 기관 안내', references), ('추가 제안 · 공문상 의무 아님', proposals), ('판단 유보 · 확인 후 적용', pending)):
        if items:
            supplemental += '\n\n## ' + label + '\n' + '\n'.join(items)
    if mode == '교직원 메신저':
        return header + '\n## 전달 문구\n업무 안내드립니다.\n' + '\n'.join(row.replace('- [ ] ', '- ') for row in rows) + supplemental
    if mode == '원문 요약':
        return header + f'\n## 업무 요약\n현재 업무 카드 {len(cards)}개를 기준으로 정리했습니다.\n' + '\n'.join(row.replace('- [ ] ', '- ') for row in rows) + supplemental
    if mode in {'업무 일정·체크리스트', '내 업무 일정·할 일'}:
        unlinked = unlinked_source_schedules(cards, source)
        if unlinked:
            supplemental += '\n\n## 원문 미연결 참고 일정 · 확인 후 업무에 반영\n'
            supplemental += '아래는 가져온 원문에 있으나 현재 업무 카드와 연결되지 않은 날짜입니다. 확정된 업무나 내 할 일이 아닙니다.\n'
            for entry in unlinked:
                position = f"원문 {entry['line']}행" + (f" · {entry['cell']}열" if entry['cell'] else '')
                issues = date_status(entry['text'])['issues']
                warning = ' [확인 필요: ' + ' · '.join(issues) + ']' if issues else ''
                supplemental += f"- {entry['text']} · {entry['context']} ({position}){warning}\n"
    return header + '\n## 일정 메모\n' + ('\n'.join(schedules) or '명시된 일정이 없습니다. 임의 기한은 만들지 않았습니다.') + '\n\n## 체크리스트\n' + '\n'.join(rows) + supplemental
