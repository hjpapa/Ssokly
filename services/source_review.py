"""Non-destructive source review hints and TSV/pipe table detection."""
import re
from dataclasses import dataclass
from services.date_evidence import date_mentions

REVIEW_PATTERN = re.compile(r"⟦[^⟧\n]*(?:⟧|$)|\[확인 필요\]", re.MULTILINE)

def _review_dates(text):
    # The date grammar accepts whitespace. Review must not combine numbers
    # from different TSV cells or extracted-text rows into an invented date.
    for cell in re.finditer(r'[^\t\r\n]+', text):
        for mention in date_mentions(cell.group()):
            yield cell.start(), mention


def review_spans(text):
    return sorted(set([(m.start(), m.end()) for m in REVIEW_PATTERN.finditer(text)] +
                      [(offset + d.start, offset + d.end) for offset, d in _review_dates(text) if d.needs_review]))

@dataclass
class SourceTableBlock:
    rows: list[list[str]]
    start_line: int
    source_lines: list[int] = None

    def line_number(self, index):
        return self.source_lines[index] if self.source_lines is not None else self.start_line + index


def _pipe_cells(line):
    # Only spaced separators or outer table borders; ordinary a|b stays prose.
    value = line.strip()
    if not (value.startswith('|') and value.endswith('|')) and not re.search(r'\s\|\s', value):
        return None
    if value.startswith('|') and value.endswith('|') and not value.endswith('\\|'):
        value = value[1:-1]
    cells = [cell.strip().replace('\\|', '|') for cell in re.split(r'(?<!\\)\|', value)]
    return cells if len(cells) >= 2 else None


def source_table_blocks(text):
    """Preserve cells and extracted-text line numbers without guessing headers."""
    blocks, current, lines = [], [], []
    start_line, marked, kind = 0, False, None
    def flush():
        if len(current) >= 2 or (current and marked and kind == 'tsv'):
            blocks.append(SourceTableBlock(current[:], start_line, lines[:]))
    for number, line in enumerate(text.splitlines() + [""], 1):
        cells = line.split('\t') if '\t' in line else _pipe_cells(line)
        row_kind = 'tsv' if '\t' in line else 'pipe'
        if cells is not None:
            if current and (kind != row_kind or (kind == 'pipe' and len(cells) != len(current[0]))):
                flush()
                current, lines, marked = [], [], False
            if not current:
                start_line = number
            kind = row_kind
            # Markdown alignment rows are syntax, not source data. Keep actual
            # source line numbers for every displayed row after skipping them.
            if kind == 'pipe' and all(re.fullmatch(r':?-{3,}:?', c) for c in cells):
                continue
            current.append(cells)
            lines.append(number)
        else:
            flush()
            current, lines, kind = [], [], None
            # A single tabbed line is a table only with an explicit HWPX marker.
            marked = line == '[표 · ↳는 병합 셀에서 이어지는 값]'
    return blocks


def tabular_blocks(text):
    """Compatibility view containing only the original rows and empty cells."""
    return [block.rows for block in source_table_blocks(text)]

def highlight_source(widget):
    text = widget.get('1.0', 'end-1c')
    widget.tag_configure('source_review', foreground='#b42318', underline=True)
    widget.tag_remove('source_review', '1.0', 'end')
    widget.tag_configure('source_date', foreground='#117568')
    widget.tag_remove('source_date', '1.0', 'end')
    for offset, d in _review_dates(text):
        if not d.needs_review:
            widget.tag_add('source_date', f'1.0+{offset + d.start}c', f'1.0+{offset + d.end}c')
    for start, end in review_spans(text):
        # Text's +Nc traversal counts Unicode characters, unlike Tcl string length.
        widget.tag_add('source_review', f'1.0+{start}c', f'1.0+{end}c')
    widget.tag_raise('source_review')
