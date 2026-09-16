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
        sections = {}
        for name in archive.namelist():
            if not (name.startswith('Contents/section') and name.endswith('.xml')):
                continue
            match = re.fullmatch(r'Contents/section([0-9]+)\.xml', name)
            if match is None:
                raise ValueError('HWPX 본문 섹션 이름이 올바르지 않습니다.')
            number = match.group(1).lstrip('0') or '0'
            if number in sections:
                raise ValueError('HWPX 본문 섹션 번호가 중복되었습니다.')
            sections[number] = name
        # Numeric order without unbounded integer parsing of archive names.
        section_names = [sections[number] for number in sorted(sections, key=lambda number: (len(number), number))]
        if not section_names:
            raise ValueError("HWPX 본문 섹션을 찾지 못했습니다.")

        paragraphs: list[str] = []
        for section_name in section_names:
            root = ElementTree.fromstring(archive.read(section_name))
            paragraphs.extend(_read_blocks(root))
        return "\n".join(paragraphs)


def _text_content(node):
    """Preserve control boundaries and XML tails inside a text run."""
    parts = [node.text or '']
    for child in node:
        kind = _local_name(child.tag)
        parts.append('\n' if kind == 'lineBreak' else '\t' if kind == 'tab' else _text_content(child))
        parts.append(child.tail or '')
    return ''.join(parts)


def _read_blocks(node, *, in_cell=False):
    """Traverse each text run exactly once; tables own their nested paragraphs."""
    tag = _local_name(node.tag)
    if tag == 'tbl':
        return _read_table(node, nested=in_cell)
    if tag == 'p':
        chunks, text = [], []
        def visit(child):
            kind = _local_name(child.tag)
            if kind == 'tbl':
                if text:
                    chunks.append(''.join(text).strip())
                    text.clear()
                chunks.extend(_read_table(child, nested=in_cell))
            elif kind == 't':
                text.append(_text_content(child))
            elif kind == 'lineBreak':
                text.append('\n')
            elif kind == 'tab':
                text.append('\t')
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
        result.extend(_read_blocks(child, in_cell=in_cell))
    return result


def _table_integer(node, attribute, message):
    value = node.get(attribute)
    if value is None or re.fullmatch(r'[0-9]+', value) is None:
        raise ValueError(message)
    try:
        return int(value)
    except ValueError:
        raise ValueError(message) from None


def _read_table(table, *, nested=False):
    size_error = 'HWPX 표 크기가 올바르지 않거나 지원 범위를 벗어났습니다.'
    cell_error = 'HWPX 표 셀 위치 또는 병합 범위가 올바르지 않습니다.'
    rows, cols = (_table_integer(table, name, size_error) for name in ('rowCnt', 'colCnt'))
    if rows < 1 or cols < 1 or rows * cols > 100000:
        raise ValueError(size_error)
    grid = [[''] * cols for _ in range(rows)]
    occupied = set()
    for tr in table:
        if _local_name(tr.tag) != 'tr':
            continue
        for cell in tr:
            if _local_name(cell.tag) != 'tc':
                continue
            props = {}
            for child in cell:
                kind = _local_name(child.tag)
                if kind in {'cellAddr', 'cellSpan', 'subList'} and kind in props:
                    raise ValueError('HWPX 표 셀 구조가 중복되었습니다.')
                props[kind] = child
            addr, span = props.get('cellAddr'), props.get('cellSpan')
            if addr is None:
                raise ValueError(cell_error)
            r, c = (_table_integer(addr, name, cell_error) for name in ('rowAddr', 'colAddr'))
            rs = _table_integer(span, 'rowSpan', cell_error) if span is not None else 1
            cs = _table_integer(span, 'colSpan', cell_error) if span is not None else 1
            if rs < 1 or cs < 1 or r >= rows or c >= cols or r + rs > rows or c + cs > cols:
                raise ValueError(cell_error)
            positions = [(ri, ci) for ri in range(r, r + rs) for ci in range(c, c + cs)]
            if any(position in occupied for position in positions):
                raise ValueError('HWPX 표 셀 위치 또는 병합 범위가 겹칩니다.')
            occupied.update(positions)
            value = ' / '.join(_read_blocks(props['subList'], in_cell=True)) if 'subList' in props else ''
            value = value.replace('\t', ' | ').replace('\n', ' / ')
            for ri, ci in positions:
                grid[ri][ci] = value if (ri, ci) == (r, c) else ('↳ ' + value if value else '')
    if cols == 1:
        return [row[0] for row in grid if row[0]]
    lines = ['\t'.join(row) for row in grid]
    return lines if nested else ['[표 · ↳는 병합 셀에서 이어지는 값]', *lines, '[/표]', '']


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
