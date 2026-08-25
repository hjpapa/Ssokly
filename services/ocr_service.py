import base64
from io import BytesIO
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from PIL import Image, ImageOps


DEFAULT_MODEL = "gpt-5-nano"
OPENAI_REQUEST_TIMEOUT_SECONDS = 120.0
OPENAI_MAX_RETRIES = 1
PNG_UPLOAD_COMPRESS_LEVEL = 3
PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = PROJECT_ROOT / ".env"
MAX_IMAGE_SIDE = 2200
MIN_IMAGE_SIDE_FOR_OCR = 1000
MAX_IMAGE_UPSCALE = 1.5
MAX_FILE_BYTES = 50 * 1024 * 1024

MISSING_KEY_MESSAGE = (
    "OpenAI API 키가 설정되어 있지 않아 이미지 OCR을 실행할 수 없습니다.\n\n"
    "프로젝트 폴더의 .env 파일에 다음 값을 설정해 주세요.\n"
    "OPENAI_API_KEY=your_api_key_here\n"
    "OPENAI_OCR_MODEL=gpt-5-nano"
)

OCR_PROMPT = """
너는 한국 학교 공문과 행정 문서 이미지를 읽는 고정밀 OCR 엔진이다.

이미지에 보이는 글자를 그대로 전사해라.

원칙:
- 요약, 해석, 업무 정리는 하지 말고 OCR 전사만 출력한다.
- 보이는 글자, 숫자, 괄호, 물결표, 콜론, 마침표, 영문 약어를 최대한 그대로 보존한다.
- 표는 행과 열 구조가 드러나도록 Markdown 표 또는 탭 구분 텍스트로 정리한다.
- 날짜, 기간, 제출 기한, 대상, 기관명, 첨부파일명은 특히 정확하게 읽는다.
- 확실하지 않은 글자는 임의로 고치지 말고 [?]를 붙인다.
- 이미지에 없는 내용은 절대 추가하지 않는다.
- 출력 앞뒤에 설명을 붙이지 말고 전사된 텍스트만 출력한다.
""".strip()

DOCUMENT_PROMPT = """
너는 한국 학교 공문과 행정 문서를 읽는 고정밀 문서 전사 엔진이다.

첨부 문서에 포함된 텍스트를 원문 구조가 드러나도록 전사해라.

원칙:
- 요약, 해석, 업무 정리는 하지 말고 원문 전사만 출력한다.
- 제목, 본문, 표, 날짜, 기간, 제출 기한, 대상, 기관명, 첨부파일명을 최대한 정확하게 보존한다.
- 표는 Markdown 표 또는 탭 구분 텍스트로 정리한다.
- 확실하지 않은 글자는 임의로 고치지 말고 [?]를 붙인다.
- 문서에 없는 내용은 추가하지 않는다.
- 출력 앞뒤에 설명을 붙이지 말고 전사된 텍스트만 출력한다.
""".strip()


def _load_openai_settings() -> tuple[str, str]:
    load_dotenv(dotenv_path=ENV_PATH)
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    model = (
        os.getenv("OPENAI_OCR_MODEL", "").strip()
        or os.getenv("OPENAI_MODEL", "").strip()
        or DEFAULT_MODEL
    )
    return api_key, model


def _prepare_image_for_model(image: Any) -> Image.Image:
    try:
        orientation = image.getexif().get(274)
    except (AttributeError, TypeError, ValueError):
        orientation = None
    prepared_image = (
        ImageOps.exif_transpose(image)
        if orientation not in (None, 1)
        else image
    )
    if prepared_image.mode not in ("RGB", "L"):
        prepared_image = prepared_image.convert("RGB")

    width, height = prepared_image.size
    longest_side = max(width, height)

    if longest_side < MIN_IMAGE_SIDE_FOR_OCR:
        scale = min(MAX_IMAGE_UPSCALE, MAX_IMAGE_SIDE / max(longest_side, 1))
        new_size = (round(width * scale), round(height * scale))
        prepared_image = prepared_image.resize(new_size, Image.Resampling.LANCZOS)
    elif longest_side > MAX_IMAGE_SIDE:
        scale = MAX_IMAGE_SIDE / longest_side
        new_size = (round(width * scale), round(height * scale))
        prepared_image = prepared_image.resize(new_size, Image.Resampling.LANCZOS)

    return prepared_image


def _image_to_data_url(image: Image.Image) -> str:
    buffer = BytesIO()
    image.save(
        buffer,
        format="PNG",
        compress_level=PNG_UPLOAD_COMPRESS_LEVEL,
    )
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _ocr_detail() -> str:
    detail = os.getenv("OPENAI_OCR_DETAIL", "auto").strip().lower()
    return detail if detail in {"low", "auto", "high"} else "auto"


def _response_text_or_message(
    response: Any,
    empty_message: str,
    *,
    raise_errors: bool = False,
) -> str:
    text = response.output_text.strip()
    if text:
        return text
    if raise_errors:
        raise RuntimeError(empty_message)
    return empty_message


def extract_text_from_image(image: Any, *, raise_errors: bool = False) -> str:
    """Extract text from a PIL image using OpenAI vision OCR."""
    if image is None:
        if raise_errors:
            raise ValueError("OCR로 읽을 이미지가 없습니다.")
        return ""

    api_key, model = _load_openai_settings()
    if not api_key or api_key == "your_api_key_here":
        if raise_errors:
            raise RuntimeError(MISSING_KEY_MESSAGE)
        return MISSING_KEY_MESSAGE

    try:
        data_url = _image_to_data_url(_prepare_image_for_model(image))
        from openai import OpenAI

        client = OpenAI(
            api_key=api_key,
            timeout=OPENAI_REQUEST_TIMEOUT_SECONDS,
            max_retries=OPENAI_MAX_RETRIES,
        )
        response = client.responses.create(
            model=model,
            instructions=OCR_PROMPT,
            input=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "이 이미지를 고정밀 OCR로 전사해 주세요.",
                        },
                        {
                            "type": "input_image",
                            "image_url": data_url,
                            "detail": _ocr_detail(),
                        },
                    ],
                }
            ],
        )
    except Exception as exc:
        message = (
            "OpenAI 이미지 OCR 중 오류가 발생했습니다.\n\n"
            "API 키, 네트워크 연결, 모델명, OpenAI API 사용 가능 상태를 확인해 주세요.\n"
            f"오류 내용: {exc}"
        )
        if raise_errors:
            raise RuntimeError(message) from exc
        return message

    return _response_text_or_message(
        response,
        (
            "OpenAI OCR이 빈 결과를 반환했습니다.\n\n"
            "캡처 영역을 조금 더 넓게 잡거나 더 선명한 화면에서 다시 시도해 주세요."
        ),
        raise_errors=raise_errors,
    )


def extract_text_from_file(
    path: Path,
    mime_type: str,
    *,
    raise_errors: bool = False,
) -> str:
    """Extract text from an OpenAI-supported document using an input_file item."""
    api_key, model = _load_openai_settings()
    if not api_key or api_key == "your_api_key_here":
        if raise_errors:
            raise RuntimeError(MISSING_KEY_MESSAGE)
        return MISSING_KEY_MESSAGE

    file_size = path.stat().st_size
    if file_size > MAX_FILE_BYTES:
        message = "첨부 파일이 50MB를 초과합니다. 파일을 나누거나 필요한 부분만 캡처해 주세요."
        if raise_errors:
            raise ValueError(message)
        return message

    try:
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        from openai import OpenAI

        client = OpenAI(
            api_key=api_key,
            timeout=OPENAI_REQUEST_TIMEOUT_SECONDS,
            max_retries=OPENAI_MAX_RETRIES,
        )
        response = client.responses.create(
            model=model,
            instructions=DOCUMENT_PROMPT,
            input=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "이 첨부 문서를 원문 구조가 드러나도록 정확하게 전사해 주세요.",
                        },
                        {
                            "type": "input_file",
                            "filename": path.name,
                            "file_data": f"data:{mime_type};base64,{encoded}",
                        },
                    ],
                }
            ],
        )
    except Exception as exc:
        message = (
            "OpenAI 첨부 문서 읽기 중 오류가 발생했습니다.\n\n"
            "API 키, 네트워크 연결, 모델명, 파일 형식, OpenAI API 사용 가능 상태를 확인해 주세요.\n"
            f"오류 내용: {exc}"
        )
        if raise_errors:
            raise RuntimeError(message) from exc
        return message

    return _response_text_or_message(
        response,
        "OpenAI 문서 읽기가 빈 결과를 반환했습니다. 파일 내용을 확인해 주세요.",
        raise_errors=raise_errors,
    )
