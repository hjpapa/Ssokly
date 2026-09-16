"""Non-destructive source review hints and tab-separated table detection."""
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


def source_table_blocks(text):
    """Preserve cells and extracted-text line numbers without guessing headers."""
    blocks, current = [], []
    start_line, marked = 0, False
    for number, line in enumerate(text.splitlines() + [""], 1):
        if '\t' in line:
            if not current:
                start_line = number
            current.append(line.split('\t'))
        else:
            if len(current) >= 2 or (current and marked):
                blocks.append(SourceTableBlock(current, start_line))
            current = []
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
