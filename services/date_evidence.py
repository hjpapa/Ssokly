"""Date surface normalization and source-based schedules; no current-year guessing."""
from dataclasses import dataclass
from datetime import date
import re

DATE = re.compile(r'(?<![\d.])(?:(?P<y>\d{4})\s*[년./-]\s*)?(?P<m>\d{1,2})\s*[월./-]\s*(?P<d>\d{1,2})(?:\s*일|\.(?!\d)|(?=\s|\(|$|~|～|–|부터|까지))')
TIME = re.compile(r'(\d{1,2})\s*(?::|시)\s*(?:(\d{1,2})\s*분?)?')

@dataclass
class DateMention:
    year: int | None
    month: int
    day: int
    times: tuple
    text: str
    start: int
    end: int
    valid: bool

def date_mentions(text):
    matches = list(DATE.finditer(text))
    result = []
    for i, match in enumerate(matches):
        year = int(match['y']) if match['y'] else None
        month, day = int(match['m']), int(match['d'])
        limit = matches[i+1].start() if i+1 < len(matches) else len(text)
        tail = text[match.end():limit]
        # Time belongs to this date only when adjacent, never across a cell/line.
        suffix = re.match(r'(?:\([월화수목금토일](?:요일)?\))? *(?:(?:/ *)?(?:\d{1,2}(?::\d{2}|시(?: *\d{1,2}분)?))(?: *[~～–-] *\d{1,2}(?::\d{2}|시(?: *\d{1,2}분)?))?)? *(?:까지|예정)?', tail)
        end = match.end() + (suffix.end() if suffix else 0)
        times = tuple((int(t[0]), int(t[1] or 0)) for t in TIME.findall(text[match.end():end]))
        try:
            date(year or 2000, month, day)
            valid = all(h < 24 and minute < 60 for h, minute in times)
        except ValueError:
            valid = False
        result.append(DateMention(year, month, day, times, text[match.start():end].strip(), match.start(), end, valid))
    return result

def supported_deadline(value, evidence):
    proposed, actual = date_mentions(value), date_mentions(evidence)
    if not proposed:
        return value if value and re.sub(r'\s+', '', value) in re.sub(r'\s+', '', evidence) else None
    if not actual or any(not d.valid for d in proposed + actual):
        return None
    # Multi-date evidence cannot establish which single deadline belongs to a task.
    if len(proposed) != len(actual):
        return None
    for p, a in zip(proposed, actual):
        if (p.month, p.day) != (a.month, a.day):
            return None
        if p.year is not None and p.year != a.year:
            return None
        if p.times and p.times != a.times:
            return None
    # Preserve source qualifiers and omitted times instead of fabricating precision.
    return evidence[actual[0].start:actual[-1].end].strip()

def source_schedules(source):
    """Keep row label and column heading with every dated cell."""
    output, table = [], []
    def add(context, text):
        uncertain = [(m.start(), m.end()) for m in re.finditer(r'⟦[^⟧]*(?:⟧|$)|\[확인 필요\]', text)]
        for d in date_mentions(text):
            if d.valid and not any(a < d.end and b > d.start for a, b in uncertain):
                value = (context.strip(), d.text)
                if value not in output:
                    output.append(value)
    def flush():
        if not table:
            return
        header = table[0] if not date_mentions('\t'.join(table[0])) else [''] * len(table[0])
        for row in table:
            label = ' · '.join(dict.fromkeys(c.removeprefix('↳ ').strip() for c in row[:2] if c and not date_mentions(c)))
            for i, cell in enumerate(row):
                title = header[i].removeprefix('↳ ') if i < len(header) else ''
                add(' · '.join(filter(None, (title, label))), cell.removeprefix('↳ '))
        table.clear()
    for line in source.splitlines():
        if '\t' in line:
            table.append(line.split('\t'))
        else:
            flush()
            add(line.strip(), line)
    flush()
    return output
