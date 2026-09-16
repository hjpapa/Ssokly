"""Date surface normalization and source-based schedules; no current-year guessing."""
from dataclasses import dataclass
from datetime import date
import re

DATE = re.compile(r'(?<![\d.])(?:(?P<y>\d{4})\s*[년./-]\s*)?(?P<m>\d{1,2})\s*[월./-]\s*(?P<d>\d{1,2})(?:\s*일|\.(?!\d)|(?=\s|\(|$|~|～|–|부터|까지))')
TIME = re.compile(r'(\d{1,2})\s*(?::|시)\s*(?:(\d{1,2})\s*분?)?')
MERIDIEM = r'(?:오전|오후|(?<![A-Za-z])(?:A\.?M\.?|P\.?M\.?)(?![A-Za-z]))'
TIME_TOKEN = r'(?:' + MERIDIEM + r' *)?\d{1,2}(?::\d{2}|시(?: *\d{1,2}분)?)(?: *' + MERIDIEM + r')?'
PARSED_TIME = re.compile(r'(' + MERIDIEM + r')? *(\d{1,2})(?::(\d{2})|시(?: *(\d{1,2})분)?)(?: *(' + MERIDIEM + r'))?', re.IGNORECASE)
DATE_QUALIFIER = r'(?:까지|예정|이전|이후|부터|이내|경(?=까지|예정|$|[^가-힣]))'
QUALIFIERS = r'(?: *' + DATE_QUALIFIER + r'){0,2}'
UNPARSED_TIME_SUFFIX = r'(?:반(?=까지|경|예정|이전|이후|부터|이내|$|[^가-힣])|:\d{2}(?!\d)|\d{1,2} *초|전후|무렵|쯤)'
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
    precision_issues: tuple = ()

    @property
    def needs_review(self):
        return not self.valid or self.weekday_matches is False or self.partial_range or bool(self.precision_issues)

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
                          + r'(?: *[~～–-] *' + TIME_TOKEN + r')?)? *' + QUALIFIERS, tail, re.IGNORECASE)
        end = match.end() + (suffix.end() if suffix else 0)
        # A shortened range endpoint ("15일 ~ 16일") is not another DATE
        # match. Keep its source surface rather than silently retaining day 15.
        remainder = text[end:limit]
        short_range = re.match(r' *[~～–-] *\d{1,2}(?: *일|\.)? *(?:\([월화수목금토일](?:요일)?\))?'
                               r'(?: *' + TIME_TOKEN + r')? *' + QUALIFIERS + r'(?=$|[^\d])', remainder, re.IGNORECASE)
        partial_range = bool(short_range)
        if short_range:
            end += short_range.end()
        precision_issues = []
        # Keep an adjacent suffix even when it is outside the supported time
        # grammar. The date card, source fallback and highlighting all share
        # this same full source span, not a falsely precise parsed prefix.
        for _ in range(8):
            unparsed = re.match(r' *' + UNPARSED_TIME_SUFFIX + r' *' + QUALIFIERS, text[end:limit])
            if unparsed:
                end += unparsed.end()
                if not precision_issues:
                    precision_issues.append('날짜·시간 표현 일부 해석: 원문 전체 확인 필요')
                continue
            # A half-hour suffix may precede the second endpoint. Preserve the
            # adjacent range too, while keeping the entire expression unparsed.
            continuation = re.match(r' *[~～–-] *' + TIME_TOKEN + r' *' + QUALIFIERS,
                                    text[end:limit], re.IGNORECASE) if precision_issues else None
            if continuation:
                end += continuation.end()
                continue
            break
        time_matches = list(PARSED_TIME.finditer(text[match.end():end]))
        times = []
        valid_meridiem = True
        meridiems = []
        for token in time_matches:
            hour, minute = int(token[2]), int(token[3] or token[4] or 0)
            labels = [label.replace('.', '').lower() for label in (token[1], token[5]) if label]
            labels = [('오후' if label == 'pm' else '오전' if label == 'am' else label) for label in labels]
            label = labels[0] if labels else ''
            meridiems.append(label)
            if labels:
                valid_meridiem = valid_meridiem and len(set(labels)) == 1
                valid_meridiem = valid_meridiem and 1 <= hour <= 12
                hour = hour % 12 + (12 if label == '오후' else 0)
            times.append((hour, minute))
        times = tuple(times)
        if len(times) > 1 and any(meridiems) and any(not label and hour <= 12 for label, (hour, _) in zip(meridiems, times)):
            precision_issues.append('시간 범위의 오전·오후 일부 생략: 원문 기준 확인 필요')
        if re.search(r'(?:경|전후|무렵|쯤)(?:까지|예정)? *$', text[match.end():end]):
            precision_issues.append('대략적인 시각: 원문 표현 확인 필요')
        try:
            date(year or 2000, month, day)
            valid = valid_meridiem and all(h < 24 and minute < 60 for h, minute in times)
        except ValueError:
            valid = False
        weekday_match = re.match(r' *\(([월화수목금토일])(?:요일)?\)', tail)
        weekday = weekday_match[1] if weekday_match else ''
        calendar_weekday = WEEKDAYS[date(year, month, day).weekday()] if year and valid else ''
        result.append(DateMention(year, month, day, times, text[match.start():end].strip(), match.start(), end, valid,
                                  weekday, calendar_weekday, partial_range, tuple(precision_issues)))
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
        issues.extend(item.precision_issues)
        # Unsupported suffixes remain in the field; do not claim precise parsing.
        remainder = value[item.end:]
        if re.match(r' *(?:' + UNPARSED_TIME_SUFFIX + r'|오전|오후|[~～–-]|부터)', remainder):
            issues.append('날짜·시간 표현 일부 해석: 원문 전체 확인 필요')
    relative = bool(RELATIVE_DATE.search(value))
    if relative:
        issues.append('상대 기한: 기준일 확인 필요')
    return {'raw': value, 'year_specified': bool(dates) and all(d.year is not None for d in dates),
            'time_specified': bool(dates) and all(bool(d.times) and not d.precision_issues for d in dates),
            'relative': relative, 'issues': list(dict.fromkeys(issues))}

def supported_deadline(value, evidence):
    proposed, actual = date_mentions(value), date_mentions(evidence)
    compact_value, compact_evidence = re.sub(r'\s+', '', value), re.sub(r'\s+', '', evidence)
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
        if p.times and a.precision_issues and compact_value not in compact_evidence:
            # A partially parsed or shared-AM/PM source is not evidence for a
            # new precise clock value. Its original expression remains usable.
            return None
        if p.times and p.times != a.times:
            return None
    # Preserve source qualifiers and omitted times instead of fabricating precision.
    surface = evidence[actual[0].start:actual[-1].end].strip()
    # Literal longer date expressions may contain syntax we do not parse yet.
    # A successful prefix match is never permission to delete their remainder.
    if compact_value in compact_evidence and len(compact_value) > len(re.sub(r'\s+', '', surface)):
        return value
    return surface

# Literal table heading vocabulary, not an inference of dates or event ownership.
# Unknown layouts deliberately fall back to source row/cell labels.
_TABLE_STUB_HEADINGS = {
    '구분', '항목', '번호', '연번', '순번', '순', '대상', '학교급', '학교', '학교명',
    '학년', '학급', '지역', '기관', '기관명', '행사', '행사명', '대회', '대회명',
    '사업', '사업명', '프로그램', '프로그램명', '업무', '업무명', '단계', '절차', '내용',
}
_TABLE_DETAIL_HEADINGS = {
    '신청', '접수', '신청일', '신청 기한', '신청 마감', '접수 기한', '접수 마감', '기한', '마감',
    '일시', '일정', '날짜', '기간', '행사', '행사일', '실시', '실시일', '대회일',
    '보고', '보고일', '발표', '결과 발표', '장소', '담당', '대상', '초', '중', '고',
    '초등', '중등', '고등', '초등학교', '중학교', '고등학교',
}
_TABLE_STRONG_HEADINGS = {'구분', '항목', '번호', '연번', '순번', '순'}
_TABLE_ENTITY_HEADINGS = {'행사명', '대회명', '사업명', '프로그램명', '학교명', '기관명', '업무명'}


def _table_context_cell(value):
    value = value.removeprefix('↳ ').strip()
    return '' if value == '↳' or re.search(r'⟦|\[확인 필요\]', value) else value


def _table_row_contexts(table):
    """Keep only explicit same-column header layers and leading stub labels."""
    headers, header_open = [], False
    for row, line, _ in table:
        clean = [_table_context_cell(cell) for cell in row]
        dated = any(date_mentions(cell) for cell in row)
        anchor = bool(clean and clean[0] in _TABLE_STUB_HEADINGS)
        merged_title = (not dated and len(row) > 1 and all(clean)
                        and len(set(clean)) == 1 and any(cell.startswith('↳ ') for cell in row[1:]))
        same_width = bool(headers) and len(clean) == len(headers[-1])
        children = [value for value in clean[1:] if value]
        child_headings = bool(children) and all(value in _TABLE_DETAIL_HEADINGS for value in children)
        continuation = (headers and header_open and same_width and not dated
                        and ((not clean[0] and child_headings)
                             or (row[0].startswith('↳ ') and clean[0] in _TABLE_STUB_HEADINGS
                                 and any(clean[0] == header[0] for header in headers))
                             or (anchor and child_headings and any(clean[0] == header[0] for header in headers))))
        new_header = (anchor and (not headers or clean[0] in _TABLE_STRONG_HEADINGS
                                 or clean in headers or any(clean[0] == header[0] for header in headers)))
        if not dated and (new_header or merged_title or continuation):
            previous_merged_title = bool(headers) and len(set(headers[-1])) == 1 and bool(headers[-1][0])
            if continuation or (anchor and header_open and same_width and previous_merged_title):
                headers.append(clean)
            else:
                # A repeated/changed heading starts a new context block. Never
                # carry the original event order through a reordered long table.
                headers = [clean]
            header_open = True
            yield None
            continue
        header_open = False
        usable = bool(headers) and all(len(header) == len(clean) for header in headers)
        stub_count = 0
        if usable:
            for header in headers:
                count = 0
                for value in header:
                    if value not in _TABLE_STUB_HEADINGS:
                        break
                    count += 1
                stub_count = max(stub_count, count)
            # Generic words such as "내용" can be either a stub heading or the
            # date-bearing value column of a key/value table. The first actual
            # dated cell bounds the leading row-label region for this row.
            first_date = next((index for index, cell in enumerate(row) if date_mentions(cell)), len(row))
            stub_count = min(stub_count, first_date)
        # Repeated entity columns separated by date columns may describe
        # parallel event blocks. Leading labels are not shared ownership for
        # those blocks; keep only direct column titles and an explicit location.
        parallel_entities = usable and any(
            value in _TABLE_ENTITY_HEADINGS and value in header[:stub_count]
            for header in headers for value in header[stub_count:])
        labels = clean[:stub_count] if usable and stub_count else clean[:1]
        labels = [value for value in labels if value and not date_mentions(value)]
        if parallel_entities:
            labels = []
        contexts = []
        for index in range(len(clean)):
            titles = [header[index] for header in headers] if usable and index >= stub_count else []
            missing_title = not titles or any(not value for value in titles)
            parts = list(dict.fromkeys(value for value in [*titles, *labels] if value))
            if missing_title:
                parts.append(f'원문 {line}행 {index + 1}열 (열 제목 확인 필요)')
            elif parallel_entities:
                parts.append(f'원문 {line}행 {index + 1}열 (행사 연결 확인 필요)')
            contexts.append(' · '.join(parts))
        yield contexts


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
        for (row, line, offset), contexts in zip(table, _table_row_contexts(table)):
            if contexts is None:
                continue
            cell_offset = offset
            for i, cell in enumerate(row):
                clean = cell.removeprefix('↳ ')
                add(contexts[i], clean, line, i + 1, cell_offset + len(cell) - len(clean))
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
