"""Local drafts: copy current card facts exactly, never re-infer dates from OCR."""
import re
from services.work_card_store import CARD_FIELDS, FIELD_LABELS
from services.date_evidence import date_mentions, RELATIVE_DATE


DATE_FIELDS = ('deadline', 'event_date', 'report_date')
SOFT_DATE_ISSUES = {'연도 미지정', '연도 미기재', '상대 기한: 기준일 확인 필요'}


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
    if data['edited'] or data['confirmed']:
        return value
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


def render_current_cards(cards, mode, *, title='업무 정리'):
    cards = [card for card in cards if not card.get('comparison_candidate')]
    values = [{field: current_value(card, field) for field in CARD_FIELDS} for card in cards]
    notice = '현재 불러온 범위 기준입니다. 미확인 붙임·추가 공문은 포함하지 않습니다.'
    if mode in {'학부모 메신저', '가정통신문 초안'}:
        # Internal staff deadlines/destinations/notes never enter public drafts.
        lines = []
        internal = re.compile(r'내부|결재|취합|보고|교무|연구부|행정실')
        for value in values:
            audience = value['target']
            if not re.search(r'학생|학부모|보호자|가정', audience):
                continue
            public_action = bool(re.search(r'학부모|보호자|학생', value['owner']))
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
    return header + '\n## 일정 메모\n' + ('\n'.join(schedules) or '명시된 일정이 없습니다. 임의 기한은 만들지 않았습니다.') + '\n\n## 체크리스트\n' + '\n'.join(rows) + supplemental
