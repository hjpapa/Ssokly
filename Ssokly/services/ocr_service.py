import os
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
import pytesseract


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = PROJECT_ROOT / ".env"
LOCAL_TESSDATA_DIR = PROJECT_ROOT / "tessdata"


def _candidate_tesseract_paths() -> list[Path]:
    candidates = []
    for base in (
        os.getenv("PROGRAMFILES"),
        os.getenv("PROGRAMFILES(X86)"),
        os.getenv("LOCALAPPDATA"),
    ):
        if base:
            candidates.append(Path(base) / "Tesseract-OCR" / "tesseract.exe")
            candidates.append(Path(base) / "Programs" / "Tesseract-OCR" / "tesseract.exe")
    return candidates


def configure_tesseract() -> Optional[Path]:
    """Configure pytesseract from .env or common Windows install paths."""
    load_dotenv(dotenv_path=ENV_PATH)

    configured_path = os.getenv("TESSERACT_CMD", "").strip().strip('"')
    if configured_path:
        path = Path(configured_path)
        if path.exists():
            pytesseract.pytesseract.tesseract_cmd = str(path)
            _configure_tessdata_dir()
            return path

    for path in _candidate_tesseract_paths():
        if path.exists():
            pytesseract.pytesseract.tesseract_cmd = str(path)
            _configure_tessdata_dir()
            return path

    _configure_tessdata_dir()
    return None


def _configure_tessdata_dir() -> Optional[Path]:
    configured_dir = os.getenv("TESSDATA_DIR", "").strip().strip('"')
    candidates = []
    if configured_dir:
        candidates.append(Path(configured_dir))
    candidates.append(LOCAL_TESSDATA_DIR)

    for path in candidates:
        if path.exists():
            os.environ["TESSDATA_PREFIX"] = str(path)
            return path

    return None


def _ocr_error_help() -> str:
    local_kor_path = LOCAL_TESSDATA_DIR / "kor.traineddata"
    return (
        "Tesseract OCR과 한국어 언어팩(kor)이 설치되어 있는지 확인해 주세요.\n"
        "한국어 언어팩이 없으면 kor.traineddata 파일이 필요합니다.\n"
        f"관리자 권한 없이 쓰려면 {local_kor_path} 위치에 파일을 넣을 수 있습니다.\n"
        ".env 파일에 TESSDATA_DIR=tessdata 처럼 직접 지정할 수도 있습니다."
    )


def extract_text_from_image(image: Any) -> str:
    """Extract Korean and English text from a PIL image."""
    if image is None:
        return ""

    configure_tesseract()

    try:
        text = pytesseract.image_to_string(image, lang="kor+eng")
        return text.strip()
    except pytesseract.TesseractNotFoundError:
        return (
            "OCR 실행에 실패했습니다.\n\n"
            "Tesseract OCR 프로그램이 설치되어 있지 않거나 실행 경로를 찾을 수 없습니다.\n"
            "Windows에서는 Tesseract OCR을 설치한 뒤, tesseract.exe 경로가 PATH에 등록되어 있는지 확인해 주세요.\n"
            "일반적인 설치 경로 예시는 C:\\Program Files\\Tesseract-OCR\\tesseract.exe 입니다.\n"
            ".env 파일에 TESSERACT_CMD=C:\\Program Files\\Tesseract-OCR\\tesseract.exe 처럼 직접 지정할 수도 있습니다."
        )
    except pytesseract.TesseractError as exc:
        return (
            "OCR 실행 중 오류가 발생했습니다.\n\n"
            f"{_ocr_error_help()}\n"
            f"오류 내용: {exc}"
        )
    except Exception as exc:
        return (
            "이미지에서 텍스트를 추출하지 못했습니다.\n\n"
            "이미지 품질을 확인하거나 다시 시도해 주세요.\n"
            f"오류 내용: {exc}"
        )
