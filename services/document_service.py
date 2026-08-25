from pathlib import Path
import zipfile
from xml.etree import ElementTree


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".tsv", ".json", ".xml", ".html"}
OPENAI_DOCUMENT_MIME_TYPES = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".rtf": "application/rtf",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
HWPX_EXTENSION = ".hwpx"
LEGACY_HWP_EXTENSION = ".hwp"


def attachment_kind(path: Path) -> str:
    extension = path.suffix.lower()
    if extension in IMAGE_EXTENSIONS:
        return "image"
    if extension in TEXT_EXTENSIONS:
        return "text"
    if extension == HWPX_EXTENSION:
        return "hwpx"
    if extension == LEGACY_HWP_EXTENSION:
        return "legacy_hwp"
    if extension in OPENAI_DOCUMENT_MIME_TYPES:
        return "openai_document"
    return "unsupported"


def mime_type_for(path: Path) -> str:
    return OPENAI_DOCUMENT_MIME_TYPES[path.suffix.lower()]


def read_text_file(path: Path) -> str:
    for encoding in ("utf-8-sig", "cp949", "utf-8"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError("unknown", b"", 0, 1, "지원하지 않는 텍스트 인코딩입니다.")


def read_hwpx_file(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        section_names = sorted(
            name
            for name in archive.namelist()
            if name.startswith("Contents/section") and name.endswith(".xml")
        )
        if not section_names:
            raise ValueError("HWPX 본문 섹션을 찾지 못했습니다.")

        paragraphs: list[str] = []
        for section_name in section_names:
            root = ElementTree.fromstring(archive.read(section_name))
            for paragraph in root.iter():
                if _local_name(paragraph.tag) != "p":
                    continue
                text = "".join(
                    node.text or ""
                    for node in paragraph.iter()
                    if _local_name(node.tag) == "t"
                ).strip()
                if text:
                    paragraphs.append(text)
        return "\n".join(paragraphs)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
