"""Source-linked facts and deterministic presentation templates."""
import json
import re
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from services.date_evidence import supported_deadline, source_schedules

class FieldEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner: str = ""
    deadline: str = ""
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
    task_type: Literal["학교 업무", "외부 기관 업무", "참고 일정"] = "학교 업무"
    requires_submission: bool = True
    field_evidence: FieldEvidence = Field(default_factory=FieldEvidence)

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

def inspect_action(action, source):
    """One action-level issue, or field-level issues; never cascade both."""
    if not valid_quote(action.evidence, source):
        return ['업무 근거']
    issues = []
    for field, label in (("owner", "담당"), ("deadline", "기한"), ("deliverable", "제출물"), ("destination", "제출처")):
        if not action.requires_submission and field in ('deliverable', 'destination'):
            continue
        value = getattr(action, field)
        if not value:
            continue  # Not supplied / not applicable is not a validation error.
        quote = getattr(action.field_evidence, field) or action.evidence
        supported = valid_quote(quote, source)
        if field == 'deadline':
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
        for field, label in (("owner", "담당"), ("deadline", "기한"), ("deliverable", "제출물"), ("destination", "제출처")):
            value = getattr(action, field)
            if not value:
                setattr(action.field_evidence, field, "")
            if label in issues or '업무 근거' in issues:
                setattr(action, field, "")
                setattr(action.field_evidence, field, "")
            elif field == 'deadline' and value:
                action.deadline = supported_deadline(value, action.field_evidence.deadline or action.evidence)
        if issues:
            action.kind = "확인 필요"
            document.questions.append(f"{action.action}: {'·'.join(issues)} 연결 확인 필요")
    document.questions = list(dict.fromkeys(document.questions))
    return document

def action_details(action):
    values = [('담당', action.owner or '원문 미기재'), ('기한', action.deadline), ('제출물', action.deliverable), ('제출처', action.destination)]
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
