from pathlib import Path
import zipfile
import re
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
        section_names = sorted((
            name
            for name in archive.namelist()
            if name.startswith("Contents/section") and name.endswith(".xml")
        ), key=lambda name: int(re.search(r'section(\d+)', name).group(1)))
        if not section_names:
            raise ValueError("HWPX 본문 섹션을 찾지 못했습니다.")

        paragraphs: list[str] = []
        for section_name in section_names:
            root = ElementTree.fromstring(archive.read(section_name))
            paragraphs.extend(_read_blocks(root))
        return "\n".join(paragraphs)


def _read_blocks(node):
    """Traverse each text run exactly once; tables own their nested paragraphs."""
    tag = _local_name(node.tag)
    if tag == 'tbl':
        return _read_table(node)
    if tag == 'p':
        chunks, text = [], []
        def visit(child):
            kind = _local_name(child.tag)
            if kind == 'tbl':
                if text:
                    chunks.append(''.join(text).strip())
                    text.clear()
                chunks.extend(_read_table(child))
            elif kind == 't':
                text.append(''.join(child.itertext()))
            elif kind == 'lineBreak':
                text.append(' ')
            else:
                for sub in child:
                    visit(sub)
        for child in node:
            visit(child)
        if text:
            chunks.append(''.join(text).strip())
        return [chunk for chunk in chunks if chunk]
    result = []
    for child in node:
        result.extend(_read_blocks(child))
    return result


def _read_table(table):
    rows, cols = int(table.get('rowCnt', '1')), int(table.get('colCnt', '1'))
    if rows < 1 or cols < 1 or rows * cols > 100000:
        raise ValueError('HWPX 표 크기가 지원 범위를 벗어났습니다.')
    grid = [[''] * cols for _ in range(rows)]
    for tr in table:
        if _local_name(tr.tag) != 'tr':
            continue
        for cell in tr:
            if _local_name(cell.tag) != 'tc':
                continue
            props = {_local_name(n.tag): n for n in cell}
            addr, span = props.get('cellAddr'), props.get('cellSpan')
            if addr is None:
                continue
            r, c = int(addr.get('rowAddr', '0')), int(addr.get('colAddr', '0'))
            rs = int(span.get('rowSpan', '1')) if span is not None else 1
            cs = int(span.get('colSpan', '1')) if span is not None else 1
            value = ' / '.join(_read_blocks(props['subList'])) if 'subList' in props else ''
            value = value.replace('\t', ' | ').replace('\n', ' / ')
            for ri in range(r, min(rows, r + rs)):
                for ci in range(c, min(cols, c + cs)):
                    grid[ri][ci] = value if (ri, ci) == (r, c) else ('↳ ' + value if value else '')
    if cols == 1:
        return [row[0] for row in grid if row[0]]
    return ['[표 · ↳는 병합 셀에서 이어지는 값]', *['\t'.join(row) for row in grid], '[/표]', '']


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
