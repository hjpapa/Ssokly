"""Source-linked facts and deterministic presentation templates."""
import json
import re
from typing import Literal
from pydantic import BaseModel, ConfigDict
from services.date_evidence import supported_deadline, source_schedules

class Action(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str
    owner: str
    deadline: str
    deliverable: str
    destination: str
    evidence: str
    kind: Literal["명시된 의무", "추천 실행", "확인 필요"]

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

def verify_evidence(document, source):
    document = document.model_copy(deep=True)
    for action in document.actions:
        uncertain = False
        if not action.evidence or re.sub(r'\s+', '', action.evidence) not in re.sub(r'\s+', '', source):
            action.evidence = ""
            uncertain = True
            document.questions.append(f"원문 근거 확인 필요: {action.action}")
        # Date formatting may differ, but the cited task must support the date.
        evidence = re.sub(r"\s+", "", action.evidence)
        for field, label in (("owner", "담당"), ("deadline", "기한"), ("deliverable", "제출물"), ("destination", "제출처")):
            value = getattr(action, field)
            if field == 'deadline' and value:
                matched = supported_deadline(value, action.evidence)
                if matched is not None:
                    action.deadline = matched
                    continue
                action.deadline = ''
                uncertain = True
                document.questions.append(f"기한 확인 필요: {action.action} (해당 업무의 근거와 날짜가 다르거나 모호합니다.)")
                continue
            if value and re.sub(r"\s+", "", value) not in evidence:
                setattr(action, field, "")
                uncertain = True
                document.questions.append(f"{label} 확인 필요: {action.action} (근거에서 해당 값을 확인하지 못했습니다.)")
        if uncertain:
            action.kind = "확인 필요"
    return document

def partial_actions(buffer):
    """Decode complete objects only, never truncated JSON."""
    match = re.search(r'"actions"\s*:\s*\[', buffer)
    if not match:
        return []
    tail = buffer[match.end():].lstrip()
    result = []
    while tail and tail[0] != "]":
        try:
            item, end = json.JSONDecoder().raw_decode(tail)
            result.append(Action.model_validate(item))
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
            lines.extend(f"- {a.deadline or '기한 확인 필요'} · {a.action} · 담당: {a.owner or '담당 확인 필요'}" for a in document.actions)
        lines.extend(["", "## 체크리스트", ""])
        for a in document.actions:
            lines.append(f"- [ ] [{a.kind}] {a.action}\n  담당: {a.owner or '담당 확인 필요'} · 기한: {a.deadline or '기한 확인 필요'}")
            if a.deliverable or a.destination:
                lines.append(f"  제출물: {a.deliverable or '확인 필요'} · 제출처: {a.destination or '확인 필요'}")
        if not document.actions:
            lines.append("- 원문에서 실행할 업무를 확인하지 못했습니다. 원문을 확인해 주세요.")
        if document.questions:
            lines.extend(["", "## 확인할 사항", "", *[f"- {q}" for q in dict.fromkeys(document.questions)]])
        evidence = list(dict.fromkeys(a.evidence for a in document.actions if a.evidence))
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
