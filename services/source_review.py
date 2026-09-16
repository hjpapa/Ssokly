"""Non-destructive source review hints and tab-separated table detection."""
import re
from services.date_evidence import date_mentions

REVIEW_PATTERN = re.compile(r"⟦[^⟧\n]*(?:⟧|$)|\[확인 필요\]", re.MULTILINE)

def review_spans(text):
    return sorted(set([(m.start(), m.end()) for m in REVIEW_PATTERN.finditer(text)] +
                      [(d.start, d.end) for d in date_mentions(text) if not d.valid]))

def tabular_blocks(text):
    """Keep empty cells and row order; never infer merged cells or headers."""
    blocks, current = [], []
    for line in text.splitlines() + [""]:
        if '\t' in line:
            current.append(line.split('\t'))
        else:
            if len(current) >= 2:
                blocks.append(current)
            current = []
    return blocks

def highlight_source(widget):
    text = widget.get('1.0', 'end-1c')
    widget.tag_configure('source_review', foreground='#b42318', underline=True)
    widget.tag_remove('source_review', '1.0', 'end')
    widget.tag_configure('source_date', foreground='#117568')
    widget.tag_remove('source_date', '1.0', 'end')
    for d in date_mentions(text):
        if d.valid:
            widget.tag_add('source_date', f'1.0+{d.start}c', f'1.0+{d.end}c')
    for start, end in review_spans(text):
        # Text's +Nc traversal counts Unicode characters, unlike Tcl string length.
        widget.tag_add('source_review', f'1.0+{start}c', f'1.0+{end}c')
    widget.tag_raise('source_review')
