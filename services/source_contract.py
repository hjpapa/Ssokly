"""The model selects source coordinates; the app, not the model, copies quotes."""
import json
from pydantic import BaseModel, ConfigDict
from services.analysis_document import Action, AnalysisDocument


class SourceRef(BaseModel):
    model_config = ConfigDict(extra='forbid')
    line: int
    cell: int  # 0 = full line; tabular cells are one-based.


class FieldRefs(BaseModel):
    model_config = ConfigDict(extra='forbid')
    owner: SourceRef
    deadline: SourceRef
    deliverable: SourceRef
    destination: SourceRef


class SourceAction(Action):
    evidence: SourceRef
    field_evidence: FieldRefs


class SourceDocument(AnalysisDocument):
    actions: list[SourceAction]


def source_input(source):
    return json.dumps({'source_lines': [{'line': i, 'text': line} for i, line in enumerate(source.splitlines(), 1)]}, ensure_ascii=False)


def resolve_ref(ref, source):
    lines = source.splitlines()
    if ref.line < 1 or ref.line > len(lines) or ref.cell < 0:
        return ''
    text = lines[ref.line-1]
    if ref.cell:
        cells = text.split('\t')
        return cells[ref.cell-1] if ref.cell <= len(cells) else ''
    return text


def resolve_action(action, source):
    data = action.model_dump()
    data['evidence'] = resolve_ref(action.evidence, source)
    data['field_evidence'] = {key: resolve_ref(getattr(action.field_evidence, key), source)
                              for key in ('owner', 'deadline', 'deliverable', 'destination')}
    resolved = Action.model_validate(data)
    if not resolved.requires_submission:
        resolved.deliverable = resolved.destination = ''
        resolved.field_evidence.deliverable = resolved.field_evidence.destination = ''
    return resolved


def resolve_document(document, source):
    data = document.model_dump()
    data['actions'] = [resolve_action(action, source).model_dump() for action in document.actions]
    return AnalysisDocument.model_validate(data)


REFERENCE_INSTRUCTIONS = '''근거 문장을 직접 다시 작성하지 않는다. evidence와 field_evidence는 source_lines의 좌표 {line, cell}로 반환한다.
line은 제공된 1부터 시작하는 줄 번호, cell=0은 그 줄 전체, 탭 표의 cell=1,2,...는 해당 셀이다. 근거가 없으면 {line:0, cell:0}이다.
evidence는 행동을 뒷받침하는 줄/셀, 각 field_evidence는 같은 행사·대상·업무에 연결되는 별도 줄/셀이다. 다른 열의 날짜를 사용하지 않는다.
owner/deliverable/destination 값은 근거에 실제 있는 짧은 명칭 그대로 쓴다. 예: 원문이 '제출처: 교육지원청'이면 destination='교육지원청'이지 '교육지원청 제출'이 아니다.
requires_submission은 서류·파일·신청 등을 내거나 제출하는 행동일 때만 true이다. 수칙 안내·명단 발표·행사 참여는 false이다.
destination은 제출처이지 안내를 받는 사람이 아니다. '담임은 학생에게 수칙을 안내한다'는 requires_submission=false, deliverable='', destination=''이다.
deadline은 근거 날짜·시간·까지/예정을 그대로 쓴다. 제출 업무가 아닌 안내/발표에는 deliverable과 destination을 억지로 채우지 않는다.
확인 질문은 실행을 막는 실제 충돌에만 사용한다. 원문에 없는 처리 절차, 발표 채널, 접수 방법을 관성적으로 질문하지 않는다.'''
