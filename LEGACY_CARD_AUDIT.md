# 구형 카드 흐름 삭제 후보 판단 — 2026-09-29

코드 삭제 없이 `main.py`·`services`·`ui`·`tools`·`tests`의 import와 함수 참조를 점검했다. 아래 후보는 현재 앱의 실행 경로에서 호출되지 않는다. 테스트가 직접 참조하는 항목과 그 내부 의존성을 함께 구분했다. 삭제는 별도 사용자 승인 후 진행한다.

| 위치 | 후보 | 삭제 영향 |
| --- | --- | --- |
| `services/analysis_document.py` | `AnalysisDocument`, `Action`, `FieldEvidence`, `EvidenceLocation` | 구형 구조화 분석·필드 근거 모델. 현재 OCR/텍스트 정리는 쓰지 않지만 카드·근거 테스트와 아래 함수들의 타입/기본 인자가 의존한다. 함께 정리하지 않으면 import 오류가 난다. |
| 같은 파일 | `render_document`, `action_details`, `partial_actions` | 구형 결과 양식·행동 상세·스트리밍 JSON 표시 제거. 현재 앱은 평문 AI 결과를 표시하므로 기본 화면 영향 없음. 구형 렌더링/스트리밍 회귀 검사를 정리해야 한다. |
| 같은 파일 | `action_card_data`, `verify_evidence`, `inspect_action`, `assess_conditions`, `scope_notice` | 구형 카드 변환·근거 검증·조건 분류·범위 안내 제거. 현재 앱의 새 요약 요청에는 사용하지 않는다. V2 의미/날짜/인용 검증 테스트가 다수 의존한다. |
| 같은 파일의 내부 전용 함수 | `_normalized`, `valid_quote`, `_quote_locations` | 위 카드 변환·검증만 지원한다. 상위 후보 정리 후 잔여 참조를 확인해 함께 제거할 수 있다. |
| `services/card_outputs.py` | `render_current_cards`, `unlinked_source_schedules`, `is_public_person`, `_has_blocking_fact_review` | 구형 카드 기반 일정·메신저·공개 초안 렌더링 제거. 현재 텍스트 AI 정리 및 저장된 과거 초안 열람에는 불필요하다. 공개 대상/원문 일정 보충/카드 출력 검사는 영향을 받는다. |
| `services/work_card_store.py` | `WorkCardStore` 클래스 전체, `CardConflictError`, `evidence_record` 및 저장 클래스 전용 보조 함수 | 현재 앱은 클래스를 생성하지 않고 `legacy_reader`가 기존 DB를 읽기 전용 SQL로 조회한다. 삭제 자체가 과거 DB를 삭제하는 것은 아니다. 단, 과거 카드 DB를 만드는 테스트 fixture와 저장·수정 충돌·초안 버전 회귀는 재구성해야 한다. 상수 사용처는 별도 보존해야 한다. |

## 반드시 남길 실제 사용 부분

- `analysis_document.ParentDraft`, `review_parent_draft`: `services/text_actions.py`의 공개 대상 결과 필터에서 사용한다. 모듈 전체를 삭제하면 현재 안내문 생성 경로가 깨진다.
- `card_outputs.current_value`, `_action_without_duplicate_dates`, 이 함수가 쓰는 `DATE_FIELDS`: `services/legacy_reader.py`가 과거 카드/할 일의 교사 수정값·확인 상태·날짜를 읽을 때 사용한다. 모듈 전체 삭제는 기존 기록 열람을 깨뜨린다.
- `work_card_store.FIELD_LABELS`: 기존 기록의 항목 이름을 표시한다. `CARD_FIELDS` 등 상수도 살아 있는 import를 먼저 정리해야 한다. 이 파일을 통째로 없애면 `card_outputs`의 import도 실패한다.
- `date_evidence`, `source_review`의 공용 날짜·원문 기능은 현재 텍스트 검수에도 쓰이므로 카드 코드와 함께 일괄 삭제하면 안 된다.

## 승인 후 필요한 후속 작업

1. 실제 사용하는 필터·기존 기록 표시 함수와 상수를 보존하고 후보만 제거한다. 사용자 DB·설정·백업은 건드리지 않는다.
2. `test_analysis_pipeline`, `test_context_review`, `test_card_outputs`, `test_public_audience`, `test_source_schedule_fallback`, `test_v2_semantics`, `test_work_card_store` 등에서 삭제 대상 전용 검사와 현재 기능 검사를 구분한다. 날짜·원문 검수, 공개 결과 필터, 기존 기록 열람 검사를 유지한다.
3. `tools/verify_v2_release.py`의 시험 목록과 기준 개수를 실제 잔여 범위에 맞춰 갱신한다. 삭제로 감소한 시험 수를 기능 검증 증가로 해석하지 않는다.
4. 전체 오프라인·V2·종료/재열기와 합성 과거 DB 읽기를 다시 검증한다.

이번 점검의 관련 회귀 45개 통과. 이 문서는 삭제 제안이며 위 함수·클래스는 모두 보존했다.
