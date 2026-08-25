import os
from pathlib import Path

from dotenv import load_dotenv


DEFAULT_MODEL = "gpt-5-nano"
OPENAI_REQUEST_TIMEOUT_SECONDS = 120.0
OPENAI_MAX_RETRIES = 1
PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = PROJECT_ROOT / ".env"

EMPTY_TEXT_MESSAGE = (
    "분석할 공문 텍스트가 없습니다.\n"
    "먼저 영역을 캡처하거나 이미지 파일을 불러오거나 텍스트를 직접 입력해 주세요."
)

MISSING_KEY_MESSAGE = (
    "OpenAI API 키가 설정되어 있지 않습니다.\n\n"
    "프로젝트 폴더에 .env 파일을 만들고 다음 내용을 입력해 주세요.\n"
    "OPENAI_API_KEY=your_api_key_here\n"
    "OPENAI_ANALYSIS_MODEL=gpt-5-nano"
)

ANALYSIS_PROMPT = """
너는 한국 초등학교 교사의 학교 업무를 돕는 실행 비서다.
다음 공문 텍스트를 읽고 교사가 실제로 수행할 업무 흐름을 정리해라.

중요한 원칙:
- 단순 요약이 아니라 행동 순서와 후속 실행 중심으로 정리한다.
- 공문에 없는 내용은 임의로 단정하지 않는다.
- 불확실한 내용은 “확인 필요”이라고 표시한다.
- 날짜, 반복 업무, 대상, 제출 방법, 첨부파일, 전달 대상, 안내문 초안, 체크리스트를 구분한다.
- 첨부파일이 언급되면 파일별로 열람, 작성, 취합, 제출, 전달 중 필요한 후속 실행을 구체적으로 적는다.
- 일정은 캘린더에 옮기기 쉽게 날짜 또는 확인해야 할 날짜 조건을 앞에 둔다.
- 업무 순서는 지금 바로 확인할 일, 기한 전 처리할 일, 마무리 확인으로 나눈다.
- 학교 업무 맥락에 맞게 간결하게 정리한다.
- 교직원 메신저용 안내문은 너무 딱딱하지 않게 작성한다.
- 개인정보나 민감한 내용이 있을 수 있으므로 과도하게 상세한 개인정보를 반복하지 않는다.
- 최종 판단은 교사가 원문을 확인해야 함을 안내한다.

출력 형식은 반드시 아래 Markdown 구조를 따른다.

## 한눈에 보기
-

## 업무 순서
### 지금 바로
-

### 기한 전
-

### 마무리 확인
-

## 반복 루틴
-

## 일정 메모
-

## 첨부파일별 후속 실행
-

## 준비물 및 제출
-

## 전달 문구
-

## 체크리스트
- [ ]

## 주의할 점
- AI 분석 결과입니다. 최종 판단은 공문 원문을 확인해 주세요.
- 공문에 날짜나 첨부파일 정보가 불명확하면 원문과 첨부파일을 다시 확인해 주세요.
""".strip()


def analyze_document_task(
    text: str,
    output_mode: str = "통합 실행안",
    *,
    raise_errors: bool = False,
) -> str:
    text = text.strip()
    if not text:
        if raise_errors:
            raise ValueError(EMPTY_TEXT_MESSAGE)
        return EMPTY_TEXT_MESSAGE

    load_dotenv(dotenv_path=ENV_PATH)
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    model = (
        os.getenv("OPENAI_ANALYSIS_MODEL", "").strip()
        or os.getenv("OPENAI_MODEL", "").strip()
        or DEFAULT_MODEL
    )

    if not api_key or api_key == "your_api_key_here":
        if raise_errors:
            raise RuntimeError(MISSING_KEY_MESSAGE)
        return MISSING_KEY_MESSAGE

    try:
        from openai import OpenAI

        client = OpenAI(
            api_key=api_key,
            timeout=OPENAI_REQUEST_TIMEOUT_SECONDS,
            max_retries=OPENAI_MAX_RETRIES,
        )
        response = client.responses.create(
            model=model,
            instructions=ANALYSIS_PROMPT,
            input=(
                f"사용자가 선택한 결과 양식: {output_mode}\n\n"
                "선택한 양식을 가장 실용적이고 자세하게 작성하되, "
                "공통 Markdown 섹션 제목은 모두 유지해라. "
                "해당하지 않는 섹션에는 '- 해당 없음'이라고 적어라.\n\n"
                f"공문 텍스트:\n\n{text}"
            ),
        )
    except Exception as exc:
        message = (
            "AI 업무 분석 중 오류가 발생했습니다.\n\n"
            "API 키, 네트워크 연결, 모델명, OpenAI API 사용 가능 상태를 확인해 주세요.\n"
            f"오류 내용: {exc}"
        )
        if raise_errors:
            raise RuntimeError(message) from exc
        return message

    result = response.output_text.strip()
    if not result:
        message = (
            "AI가 분석 결과를 비어 있는 응답으로 반환했습니다.\n\n"
            "공문 텍스트를 조금 더 길게 입력하거나 다시 시도해 주세요."
        )
        if raise_errors:
            raise RuntimeError(message)
        return message
    return result
