"""Source-linked facts and deterministic presentation templates."""
import json
import re
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from services.date_evidence import supported_deadline, source_schedules, date_status, date_mentions

OBLIGATIONS = ('필수', '조건부', '안내', '추가 제안', '판단 유보')
ACTION_FIELDS = ('action', 'owner', 'target', 'condition', 'obligation', 'deadline', 'event_date', 'report_date', 'deliverable', 'destination')
FIELD_LABELS = {'action': '해야 할 일', 'owner': '담당', 'target': '대상', 'condition': '업무 조건',
                'obligation': '업무 구분', 'deadline': '제출 기한', 'event_date': '행사일', 'report_date': '보고일',
                'deliverable': '제출물', 'destination': '제출처'}
EVIDENCE_FIELDS = ('owner', 'target', 'condition', 'deadline', 'event_date', 'report_date', 'deliverable', 'destination')

class EvidenceLocation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    line: int
    cell: int


class FieldEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner: str = ""
    target: str = ""
    condition: str = ""
    deadline: str = ""
    event_date: str = ""
    report_date: str = ""
    deliverable: str = ""
    destination: str = ""

class Action(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str
    owner: str
    deadline: str
    deliverable: str
    destination: str
    evidence: str
    kind: Literal["명시된 의무", "추천 실행", "확인 필요"]
    target: str = ""
    condition: str = ""
    obligation: Literal['필수', '조건부', '안내', '추가 제안', '판단 유보'] = '판단 유보'
    event_date: str = ""
    report_date: str = ""
    task_type: Literal["학교 업무", "외부 기관 업무", "참고 일정"] = "학교 업무"
    requires_submission: bool = True
    field_evidence: FieldEvidence = Field(default_factory=FieldEvidence)
    # Locally derived from SourceRef, persisted in the cache but never requested
    # as another model-generated coordinate/page field.
    evidence_locations: dict[str, list[EvidenceLocation]] = Field(default_factory=dict)

class AnalysisDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str
    summary: str
    actions: list[Action]
    questions: list[str]
    message: str

class ParentDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str
    message: str
    questions: list[str]

def review_parent_draft(draft):
    """Conservative extra check, not a substitute for the teacher's review."""
    draft = draft.model_copy(deep=True)
    internal = re.compile(r"결재|내부\s*보고|(?:(?:담임|교사|교직원)(?:은|는|이|가).*(?:제출|취합|전달|보고)|(?:명단|보고|신청서).*?(?:연구부|교무부|교무실|행정실).*?(?:제출|전달))")
    # Redact contact/identifier patterns in every output field, not just the body.
    private = re.compile(r"(?<!\d)(?:\+82[ -]?)?0?1[016789][ .-]?\d{3,4}[ .-]?\d{4}(?!\d)|(?<!\d)\d{6}[ -]?[1-4]\d{6}(?!\d)|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
    redacted = False
    def redact(value):
        nonlocal redacted
        cleaned, count = private.subn("[개인정보 제외 · 공개 연락처 확인 필요]", value)
        redacted = redacted or bool(count)
        return cleaned
    draft.title = redact(draft.title)
    draft.message = redact(draft.message)
    draft.questions = [redact(q) for q in draft.questions]
    parts = re.split(r"(?<=[.!?])\s+|\n", draft.message)
    kept = [part for part in parts if not internal.search(part)]
    if len(kept) != len(parts):
        draft.message = "\n".join(kept).strip()
        draft.questions = [q for q in draft.questions if not internal.search(q)]
        draft.questions.append("교직원 내부 처리로 보이는 문장을 제외했습니다. 안내 내용의 누락과 공개 범위를 확인하세요.")
    if redacted:
        draft.questions.append("연락처·식별정보를 가렸습니다. 공개 가능한 학교 연락처인지 확인 후 입력하세요.")
    return draft

def strict_schema(model):
    """Keep old local records readable, but require every field on the wire."""
    schema = model.model_json_schema()
    def visit(node):
        if isinstance(node, dict):
            node.pop('default', None)
            if node.get('type') == 'object':
                node.get('properties', {}).pop('evidence_locations', None)
                node['required'] = list(node.get('properties', {}))
                node['additionalProperties'] = False
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)
    visit(schema)
    return schema

def _normalized(text):
    # Preserve cell and paragraph boundaries; ignore only horizontal spaces.
    return re.sub(r'[^\S\n\t]+', '', text.replace('\r\n', '\n')).strip()

def valid_quote(quote, source):
    return bool(quote.strip()) and _normalized(quote) in _normalized(source) and not re.search(r'⟦|\[확인 필요\]', quote)

def assess_conditions(text, attachment_available=None):
    """Classify only an action's own cited passages, never unrelated event text."""
    compact = re.sub(r'\s+', '', text)
    result = {'obligation': '판단 유보', 'issues': []}
    if '붙임' in compact and ('참조' in compact or '세부' in compact) and attachment_available is not True:
        result['issues'].append('붙임 확인 필요: 가져온 자료만으로 세부 의무를 확정할 수 없습니다.')
        return result
    no_applicable = bool(re.search(r'해당(?:사항)?(?:이)?없', compact))
    omitted = bool(re.search(r'(?:제출|회신).*(?:생략|불필요|하지않)', compact))
    if no_applicable and omitted:
        result.update(obligation='조건부', issues=['해당 여부 확인 후 제출·회신 생략 조건 적용'])
    elif no_applicable and re.search(r'회신|보고|제출', compact):
        # "None" is a response value, not permission to drop the task.
        result.update(obligation='필수', issues=['해당 없음도 회신·보고·제출 필요'])
    elif re.search(r'희망(?:하는)?(?:학교|학급|학생|자)|(?:참가|참여)를?희망|해당(?:하는)?학교만', compact):
        result['obligation'] = '조건부'
    elif re.search(r'(?:모든|각|전체)학교.*(?:제출|회신|보고)', compact):
        result['obligation'] = '필수'
    elif re.search(r'참고(?:하시기|바랍니다|용)|정보성안내', compact) and not re.search(r'제출|회신|보고', compact):
        result['obligation'] = '안내'
    return result


def scope_notice(partial=True, has_actions=False):
    if has_actions:
        return '가져온 범위의 업무 초안입니다. 원문과 적용 조건을 확인하세요.'
    if partial:
        return '가져온 범위에서는 제출 업무를 확인하지 못했습니다. 누락된 페이지·붙임을 확인하세요.'
    return '분석한 자료에서 제출 업무를 확인하지 못했습니다. 업무가 없다는 확정은 아닙니다.'


def _quote_locations(quote, source, selected=None):
    """Local positions only. Multiple matches deliberately remain ambiguous."""
    if not quote or not valid_quote(quote, source):
        return []
    result = []
    wanted = _normalized(quote)
    if selected:
        lines = source.splitlines()
        verified = []
        for location in selected:
            data = location.model_dump() if hasattr(location, 'model_dump') else location
            number, cell = data.get('line', 0), data.get('cell', -1)
            if not isinstance(number, int) or not isinstance(cell, int) or not 1 <= number <= len(lines) or cell < 0:
                continue
            text = lines[number - 1]
            if cell:
                cells = text.split('\t')
                if cell > len(cells):
                    continue
                text = cells[cell - 1]
            if _normalized(text) == wanted:
                item = {'line': number, 'cell': cell}
                if item not in verified:
                    verified.append(item)
        if verified:
            return verified
    for number, line in enumerate(source.splitlines(), 1):
        if wanted not in _normalized(line):
            continue
        if '\t' in line:
            cells = [{'line': number, 'cell': i} for i, cell in enumerate(line.split('\t'), 1)
                     if wanted in _normalized(cell)]
            result.extend(cells or [{'line': number, 'cell': 0}])
        else:
            result.append({'line': number, 'cell': 0})
    return result


def action_card_data(action, source=None, *, attachment_available=None):
    """Keep raw suggestions with field-specific review facts; never erase them.

    Quote matching is not a proof of action semantics or original OCR accuracy.
    Source IDs/versions and durable teacher edits are added by the card store.
    """
    payload = action.model_dump()
    source = source or ''
    primary_valid = valid_quote(action.evidence, source)
    field_data, field_issues, date_states = {}, {}, {}
    issues = [] if primary_valid else ['업무 근거 확인 필요']
    for field in ACTION_FIELDS:
        value = getattr(action, field)
        quote = getattr(action.field_evidence, field, '') or action.evidence
        verified = valid_quote(quote, source)
        problem = []
        if field in ('action', 'obligation'):
            if not verified:
                problem.append('근거 확인 필요')
        elif value:
            if field in ('deadline', 'event_date', 'report_date'):
                supported = supported_deadline(value, quote) if verified else None
                verified = verified and supported is not None
                if supported is not None:
                    payload[field] = supported
                date_states[field] = date_status(payload[field])
                problem.extend(date_states[field]['issues'])
                if len(date_mentions(quote)) > 1 and not verified:
                    problem.append('여러 날짜의 역할·충돌 확인 필요')
            else:
                verified = verified and _normalized(value) in _normalized(quote)
            if not verified:
                problem.append('근거 확인 필요')
        else:
            verified = False
        if field in ('deliverable', 'destination') and not action.requires_submission:
            verified = False
            if value:
                problem.append('비제출 업무의 제출 정보 적용 여부 확인')
        selected = action.evidence_locations.get(field) or (
            action.evidence_locations.get('action') if quote == action.evidence else None)
        locations = _quote_locations(quote, source, selected)
        accepted_locations = []
        if selected:
            selected_values = [item.model_dump() if hasattr(item, 'model_dump') else item for item in selected]
            accepted_locations = [item for item in locations if item in selected_values]
            if len(accepted_locations) != len(selected_values):
                problem.append('선택한 원문 위치 확인 필요')
        if accepted_locations:
            # Carry the same locally revalidated selection through the payload;
            # the persistent store independently checks it against its version.
            payload['evidence_locations'][field] = accepted_locations
        if len(locations) > 1:
            problem.append('동일 근거가 여러 위치에 있음')
        field_data[field] = {'value': payload[field], 'quote': quote, 'verified': verified,
                             'issues': list(dict.fromkeys(problem)), 'locations': locations}
        if problem:
            field_issues[field] = list(dict.fromkeys(problem))
            issues.extend(f'{FIELD_LABELS[field]}: {item}' for item in field_issues[field])
    condition_quote = action.field_evidence.condition or action.evidence
    condition_text = '\n'.join(dict.fromkeys(q for q in (action.evidence, condition_quote) if valid_quote(q, source)))
    assessed = assess_conditions(condition_text, attachment_available)
    if assessed['obligation'] != '판단 유보' or assessed['issues']:
        payload['obligation'] = assessed['obligation']
    elif action.kind == '추천 실행':
        payload['obligation'] = '추가 제안'
    elif action.task_type == '참고 일정':
        payload['obligation'] = '안내'
    if not primary_valid and payload['obligation'] != '추가 제안':
        payload['obligation'] = '판단 유보'
    field_data['obligation']['value'] = payload['obligation']
    issues.extend(assessed['issues'])
    payload.update(fields=field_data, field_issues=field_issues, date_states=date_states,
                   issues=list(dict.fromkeys(issues)))
    return payload


def inspect_action(action, source):
    """One action-level issue, or field-level issues; never cascade both."""
    if not valid_quote(action.evidence, source):
        return ['업무 근거']
    issues = []
    for field, label in (("owner", "담당"), ("target", "대상"), ("condition", "조건"), ("deadline", "기한"),
                         ("event_date", "행사일"), ("report_date", "보고일"), ("deliverable", "제출물"), ("destination", "제출처")):
        if not action.requires_submission and field in ('deliverable', 'destination'):
            continue
        value = getattr(action, field)
        if not value:
            continue  # Not supplied / not applicable is not a validation error.
        quote = getattr(action.field_evidence, field) or action.evidence
        supported = valid_quote(quote, source)
        if field in ('deadline', 'event_date', 'report_date'):
            supported = supported and supported_deadline(value, quote) is not None
        else:
            supported = supported and _normalized(value) in _normalized(quote)
        if not supported:
            issues.append(label)
    return issues

def verify_evidence(document, source):
    document = document.model_copy(deep=True)
    for action in document.actions:
        if not action.requires_submission:
            action.deliverable = action.destination = ''
            action.field_evidence.deliverable = action.field_evidence.destination = ''
        issues = inspect_action(action, source)
        if '업무 근거' in issues:
            action.evidence = ""
        for field, label in (("owner", "담당"), ("target", "대상"), ("condition", "조건"), ("deadline", "기한"),
                             ("event_date", "행사일"), ("report_date", "보고일"), ("deliverable", "제출물"), ("destination", "제출처")):
            value = getattr(action, field)
            if not value:
                setattr(action.field_evidence, field, "")
            if label in issues or '업무 근거' in issues:
                setattr(action, field, "")
                setattr(action.field_evidence, field, "")
            elif field in ('deadline', 'event_date', 'report_date') and value:
                setattr(action, field, supported_deadline(value, getattr(action.field_evidence, field) or action.evidence))
        if issues:
            action.kind = "확인 필요"
            document.questions.append(f"{action.action}: {'·'.join(issues)} 연결 확인 필요")
    document.questions = list(dict.fromkeys(document.questions))
    return document

def action_details(action):
    values = [('담당', action.owner or '원문 미기재'), ('대상', action.target), ('조건', action.condition),
              ('기한', action.deadline), ('행사일', action.event_date), ('보고일', action.report_date),
              ('제출물', action.deliverable), ('제출처', action.destination)]
    return ' · '.join(f'{label}: {value}' for label, value in values if value)

def partial_actions(buffer, action_model=Action):
    """Decode complete objects only, never truncated JSON."""
    match = re.search(r'"actions"\s*:\s*\[', buffer)
    if not match:
        return []
    tail = buffer[match.end():].lstrip()
    result = []
    while tail and tail[0] != "]":
        try:
            item, end = json.JSONDecoder().raw_decode(tail)
            result.append(action_model.model_validate(item))
        except (ValueError, TypeError):
            break
        tail = tail[end:].lstrip()
        if not tail.startswith(","):
            break
        tail = tail[1:].lstrip()
    return result

SCHOOL_MODES = ("업무 일정·체크리스트", "교직원 메신저", "학부모 메신저", "가정통신문 초안")
PARENT_MODES = ("학부모 메신저", "가정통신문 초안")
MODES = SCHOOL_MODES + ("원문 요약", "내 업무 일정·할 일", "일정 확인표", "업무 프로세스", "활용 안내 양식", "통합 실행안")

def render_document(document, mode, source=None):
    if mode not in MODES:
        raise ValueError("지원하지 않는 결과 양식입니다.")
    if mode == "원문 요약":
        lines = [f"# {document.title}", "", "## 핵심 요약", "", document.summary or "요약할 내용을 확인하지 못했습니다.",
                 "", "AI 요약 · 날짜와 대상은 원문과 대조하세요."]
        if document.questions:
            lines.extend(["", "## 확인할 사항", "", *[f"- {q}" for q in dict.fromkeys(document.questions)]])
        return "\n".join(lines)
    if mode in ("교직원 메신저", *PARENT_MODES):
        body = document.message.strip()
        if not body:
            body = "이 원문만으로는 해당 대상에게 보낼 안내를 작성하기 어렵습니다. 안내 대상과 공개할 내용을 확인해 주세요."
        heading = "가정통신문 초안" if mode == "가정통신문 초안" else "전달 문구"
        output = [f"# {document.title}", "", f"## {heading}", "", body,
                  "", "## 발송 전 확인", "", "- 초안입니다. 대상·날짜·공개 범위를 확인한 뒤 발송하세요."]
        output.extend(f"- {q}" for q in dict.fromkeys(document.questions))
        return "\n".join(output)
    if mode in ("업무 일정·체크리스트", "내 업무 일정·할 일"):
        lines = [f"# {document.title}", "", "AI 초안 · 요약과 행동 문구도 원문 대조가 필요합니다.", document.summary, "", "## 일정 메모", ""]
        schedules = source_schedules(source) if source else []
        if schedules:
            lines.append("원문 명시 일정 · 내 담당 여부와 예정/마감 구분을 확인하세요.")
            lines.extend(f"- {when} · {context}" for context, when in schedules)
        else:
            dated = [a for a in document.actions if a.deadline]
            lines.extend(f"- {a.deadline} · {a.action}" for a in dated)
            if not dated:
                lines.append('원문에서 연결된 일정이 없습니다.')
        lines.extend(["", "## 체크리스트", ""])
        ready = [a for a in document.actions if a.task_type == '학교 업무' and a.kind != '확인 필요']
        for a in ready:
            lines.append(f"- [ ] [{a.kind}] {a.action}")
            if action_details(a):
                lines.append('  ' + action_details(a))
        if not ready:
            lines.append("- 검증된 학교 업무가 없습니다. 아래 참고·확인 항목을 검수하세요.")
        pending = [a for a in document.actions if a.kind == '확인 필요']
        if pending:
            lines.extend(['', '## 검수 대기 업무', '', '아직 내 할 일로 담지 않습니다. 원문을 확인한 뒤 재분석하세요.'])
            lines.extend(f'- {a.action}' + (f' · {action_details(a)}' if action_details(a) else '') for a in pending)
        reference = [a for a in document.actions if a.task_type != '학교 업무' and a.kind != '확인 필요']
        if reference:
            lines.extend(['', '## 외부 기관·참고 사항', ''])
            lines.extend(f'- [{a.task_type}] {a.action}' + (f' · {action_details(a)}' if action_details(a) else '') for a in reference)
        if document.questions:
            lines.extend(["", "## 확인할 사항", "", *[f"- {q}" for q in dict.fromkeys(document.questions)]])
        evidence = list(dict.fromkeys(q for a in document.actions for q in [a.evidence, *a.field_evidence.model_dump().values()] if q))
        if evidence:
            lines.extend(["", "## 원문 근거", "", *[f"- “{quote}”" for quote in evidence]])
        return "\n".join(lines)
    lines = [f"# {document.title}", "", document.summary, ""]
    def section(title, content):
        if content:
            lines.extend([f"## {title}", "", *content, ""])
    def label(a):
        index = document.actions.index(a) + 1
        return f"[{a.kind}] {a.action}" + (f" [근거 {index}]" if a.evidence else "")
    if mode in ("일정 확인표", "통합 실행안"):
        if mode == "통합 실행안":
            section("일정 메모", list(dict.fromkeys(f"- {a.deadline or '기한 확인 필요'} · {a.deliverable or a.action}" for a in document.actions)))
        else:
            section("일정 메모", [f"- {a.deadline or '기한 확인 필요'} · {label(a)}\n  제출물: {a.deliverable or '확인 필요'} · 제출처: {a.destination or '확인 필요'}" for a in document.actions])
    if mode in ("업무 프로세스", "통합 실행안"):
        section("업무 순서", [f"{i}. {label(a)}\n   담당: {a.owner or '담당 확인 필요'} · 기한: {a.deadline or '확인 필요'}\n   산출물: {a.deliverable or '확인 필요'} → {a.destination or '제출처 확인 필요'}" for i, a in enumerate(document.actions, 1)])
        if mode == "업무 프로세스":
            section("체크리스트", [f"- [ ] {label(a)}" for a in document.actions])
    if mode in ("활용 안내 양식", "통합 실행안"):
        section("전달 문구", [document.message] if document.message else [])
        if mode == "활용 안내 양식":
            section("첨부파일별 후속 실행", [f"- {a.deliverable}: {a.action} → {a.destination or '제출처 확인 필요'}" for a in document.actions if a.deliverable])
    section("원문 근거", [f"- [{i}] “{a.evidence}”" for i, a in enumerate(document.actions, 1) if a.evidence])
    section("확인할 사항", [f"- {q}" for q in dict.fromkeys(document.questions)])
    return "\n".join(lines).strip()
