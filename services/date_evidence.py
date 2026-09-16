"""Date surface normalization and source-based schedules; no current-year guessing."""
from dataclasses import dataclass
from datetime import date
import re

DATE = re.compile(r'(?<![\d.])(?:(?P<y>\d{4})\s*[년./-]\s*)?(?P<m>\d{1,2})\s*[월./-]\s*(?P<d>\d{1,2})(?:\s*일|\.(?!\d)|(?=\s|\(|$|~|～|–|부터|까지))')
TIME = re.compile(r'(\d{1,2})\s*(?::|시)\s*(?:(\d{1,2})\s*분?)?')
TIME_TOKEN = r'(?:(?:오전|오후) *)?\d{1,2}(?::\d{2}|시(?: *\d{1,2}분)?)'
PARSED_TIME = re.compile(r'(?:(오전|오후) *)?(\d{1,2})(?::(\d{2})|시(?: *(\d{1,2})분)?)')
RELATIVE_DATE = re.compile(r'(?:행사|대회|교육|연수|공문\s*접수|접수|통보|안내)(?:일)?\s*(?:후|전)?\s*\d+\s*(?:일|주|개월)\s*(?:전|후|이내)')
WEEKDAYS = '월화수목금토일'

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
    weekday: str = ''
    calendar_weekday: str = ''
    partial_range: bool = False

    @property
    def weekday_matches(self):
        if not self.weekday or not self.calendar_weekday:
            return None
        return self.weekday == self.calendar_weekday

def date_mentions(text):
    matches = list(DATE.finditer(text))
    result = []
    for i, match in enumerate(matches):
        year = int(match['y']) if match['y'] else None
        month, day = int(match['m']), int(match['d'])
        limit = matches[i+1].start() if i+1 < len(matches) else len(text)
        tail = text[match.end():limit]
        # Time belongs to this date only when adjacent, never across a cell/line.
        suffix = re.match(r' *(?:\([월화수목금토일](?:요일)?\))? *(?:(?:/ *)?' + TIME_TOKEN
                          + r'(?: *[~～–-] *' + TIME_TOKEN + r')?)? *(?:까지|예정)?', tail)
        end = match.end() + (suffix.end() if suffix else 0)
        # A shortened range endpoint ("15일 ~ 16일") is not another DATE
        # match. Keep its source surface rather than silently retaining day 15.
        remainder = text[end:limit]
        short_range = re.match(r' *[~～–-] *\d{1,2}(?: *일|\.)? *(?:\([월화수목금토일](?:요일)?\))?'
                               r'(?: *' + TIME_TOKEN + r')? *(?:까지|예정)?(?=$|[^\d])', remainder)
        partial_range = bool(short_range)
        if short_range:
            end += short_range.end()
        time_matches = list(PARSED_TIME.finditer(text[match.end():end]))
        times = []
        valid_meridiem = True
        for token in time_matches:
            hour, minute = int(token[2]), int(token[3] or token[4] or 0)
            if token[1]:
                valid_meridiem = valid_meridiem and 1 <= hour <= 12
                hour = hour % 12 + (12 if token[1] == '오후' else 0)
            times.append((hour, minute))
        times = tuple(times)
        try:
            date(year or 2000, month, day)
            valid = valid_meridiem and all(h < 24 and minute < 60 for h, minute in times)
        except ValueError:
            valid = False
        weekday_match = re.match(r' *\(([월화수목금토일])(?:요일)?\)', tail)
        weekday = weekday_match[1] if weekday_match else ''
        calendar_weekday = WEEKDAYS[date(year, month, day).weekday()] if year and valid else ''
        result.append(DateMention(year, month, day, times, text[match.start():end].strip(), match.start(), end, valid,
                                  weekday, calendar_weekday, partial_range))
    return result


def date_status(value):
    """Explain uncertainty without filling an omitted year/time or relative base."""
    dates = date_mentions(value)
    issues = []
    for item in dates:
        if not item.valid:
            issues.append('유효하지 않은 날짜·시간')
        if item.year is None:
            issues.append('연도 미지정')
        if item.weekday_matches is False:
            issues.append(f'날짜·요일 불일치: 원문 {item.weekday}요일 / 달력 {item.calendar_weekday}요일')
        if item.partial_range:
            issues.append('축약된 기간 끝: 원문 전체 표현의 적용 범위 확인 필요')
        # Unsupported suffixes remain in the field; do not claim precise parsing.
        remainder = value[item.end:]
        if re.match(r' *(?:반|오전|오후|[~～–-]|부터)', remainder):
            issues.append('날짜·시간 표현 일부 해석: 원문 전체 확인 필요')
    relative = bool(RELATIVE_DATE.search(value))
    if relative:
        issues.append('상대 기한: 기준일 확인 필요')
    return {'raw': value, 'year_specified': bool(dates) and all(d.year is not None for d in dates),
            'time_specified': bool(dates) and all(bool(d.times) for d in dates),
            'relative': relative, 'issues': list(dict.fromkeys(issues))}

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
    surface = evidence[actual[0].start:actual[-1].end].strip()
    compact_value, compact_evidence = re.sub(r'\s+', '', value), re.sub(r'\s+', '', evidence)
    # Literal longer date expressions may contain syntax we do not parse yet.
    # A successful prefix match is never permission to delete their remainder.
    if compact_value in compact_evidence and len(compact_value) > len(re.sub(r'\s+', '', surface)):
        return value
    return surface

def source_schedule_entries(source):
    """Source dates with exact local row/cell/character positions, never pages.

    Unlike the legacy tuple view, equal dates in separate cells remain separate
    entries so a teacher's edited field can cover only its own source occurrence.
    """
    output, table = [], []
    def add(context, text, line, cell, offset):
        uncertain = [(m.start(), m.end()) for m in re.finditer(r'⟦[^⟧]*(?:⟧|$)|\[확인 필요\]', text)]
        for d in date_mentions(text):
            if d.valid and not any(a < d.end and b > d.start for a, b in uncertain):
                output.append({'context': context.strip(), 'text': d.text, 'line': line, 'cell': cell,
                               'start': offset + d.start, 'end': offset + d.end})
    def flush():
        if not table:
            return
        first = table[0][0]
        header = first if not date_mentions('\t'.join(first)) else [''] * len(first)
        for row, line, offset in table:
            label = ' · '.join(dict.fromkeys(c.removeprefix('↳ ').strip() for c in row[:2] if c and not date_mentions(c)))
            cell_offset = offset
            for i, cell in enumerate(row):
                title = header[i].removeprefix('↳ ') if i < len(header) else ''
                clean = cell.removeprefix('↳ ')
                add(' · '.join(filter(None, (title, label))), clean, line, i + 1,
                    cell_offset + len(cell) - len(clean))
                cell_offset += len(cell) + 1
        table.clear()
    offset = 0
    for number, raw in enumerate(source.splitlines(keepends=True), 1):
        line = raw.rstrip('\r\n')
        if '\t' in line:
            table.append((line.split('\t'), number, offset))
        else:
            flush()
            add(line.strip(), line, number, 0, offset)
        offset += len(raw)
    flush()
    return output


def source_schedules(source):
    """Backward-compatible deduplicated labels for the legacy text renderer."""
    return list(dict.fromkeys((item['context'], item['text']) for item in source_schedule_entries(source)))
