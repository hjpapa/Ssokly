"""Generate 10 fictional notices; --live sends exactly one OCR+summary per case.

Uses the deployed relay only, never local credentials. Outputs are persisted per
request to an ignored directory; --case permits explicit targeted rechecks.
"""
import argparse
import json
import os
from pathlib import Path
import re
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from PIL import Image, ImageDraw, ImageFont
import httpx
from tools.official_document_cases import CASES


def render_notice(text):
    font = ImageFont.truetype('C:/Windows/Fonts/malgun.ttf', 30)
    lines = []
    for paragraph in text.splitlines():
        line = ''
        for char in paragraph:
            if font.getlength(line + char) > 1420:
                lines.append(line)
                line = ''
            line += char
        lines.append(line)
    image = Image.new('RGB', (1540, max(720, 130 + len(lines) * 54)), 'white')
    draw = ImageDraw.Draw(image)
    for index, line in enumerate(lines):
        draw.text((60, 60 + 54 * index), line, font=font, fill='black')
    return image


def checks(case, text):
    compact = re.sub(r'\s+', '', text)
    return {
        'missing_literal_facts': [fact for fact in case['facts'] if re.sub(r'\s+', '', fact) not in compact],
        'forbidden_additions': [value for value in case['forbidden'] if re.sub(r'\s+', '', value) in compact],
        'settings_leak': bool(re.search(r'(?:수신대상|대상)[:：\-•]*교직원', compact)),
        'invented_form': bool(re.search(r'\[입력[^\]]*\]|_{3,}|□', text)),
        'note': 'Literal checks are triage; every result requires semantic review.',
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--reuse-ocr', type=Path, help='Existing report.json: recheck summaries without paying for OCR again.')
    parser.add_argument('--case', choices=[c['id'] for c in CASES], action='append')
    parser.add_argument('--output', type=Path, default=ROOT / '.local-results/official-evaluation')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    cases = [c for c in CASES if not args.case or c['id'] in args.case]
    reused = {}
    if args.reuse_ocr:
        previous = json.loads(args.reuse_ocr.read_text(encoding='utf-8'))
        reused = {row['case']: row['text'] for row in previous['results']
                  if row['operation'] == 'ocr' and row.get('completed')}
        for case in cases:
            if case['id'] not in reused or (args.reuse_ocr.parent / (case['id'] + '.txt')).read_text(encoding='utf-8') != case['text']:
                raise SystemExit('Reusable OCR is missing or the synthetic source changed.')
    for case in cases:
        (args.output / (case['id'] + '.txt')).write_text(case['text'], encoding='utf-8')
        render_notice(case['text']).save(args.output / (case['id'] + '.png'))
    if not args.live:
        print(json.dumps({'fixtures': len(cases), 'paid_requests': 0}))
        return 0
    from services.ocr_service import extract_text_from_image
    from services.text_actions import generate_text_action
    from services.ai_relay import server_url
    url = server_url()
    if url != 'https://ssokly-ai-relay.vercel.app':
        raise SystemExit('Expected the configured production relay; direct calls are forbidden.')
    # Never overwrite a completed paid evaluation by accident.
    report_path = args.output / 'report.json'
    if report_path.exists():
        raise SystemExit('Use a new output directory for an explicitly requested recheck.')
    report = {'relay': url, 'synthetic_only': True, 'paid_request_attempts': 0, 'results': [],
              'reused_ocr': bool(reused), 'token_usage': 'not returned by relay', 'cost': 'not measured'}
    original_post = httpx.Client.post
    def observed_post(client, target, **kwargs):
        if target != url + '/api/ai':
            raise AssertionError('Unexpected transmission destination')
        report['paid_request_attempts'] += 1
        persist()
        response = original_post(client, target, **kwargs)
        report['results'][-1]['http_status'] = response.status_code
        if response.status_code == 200:
            report['results'][-1]['upstream_ms'] = response.json().get('upstream_ms')
        return response
    def persist():
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    with patch.dict(os.environ, {'SSOKLY_API_URL': url, 'OPENAI_API_KEY': ''}), \
            patch('services.ocr_service._load_openai_settings', side_effect=AssertionError('Local key forbidden')), \
            patch('services.text_actions.load_dotenv', side_effect=AssertionError('Local key forbidden')), \
            patch('httpx.Client.post', observed_post):
        for case in cases:
            ocr = reused.get(case['id'])
            for operation in (('summary',) if reused else ('ocr', 'summary')):
                if operation == 'summary' and ocr is None:
                    continue
                row = {'case': case['id'], 'topic': case['topic'], 'operation': operation}
                report['results'].append(row)
                started = time.perf_counter()
                try:
                    if operation == 'ocr':
                        with Image.open(args.output / (case['id'] + '.png')) as image:
                            text = extract_text_from_image(image, raise_errors=True)
                        ocr = text
                    else:
                        text = generate_text_action(ocr, '요약', '교직원')
                    row.update(completed=True, text=text, checks=checks(case, text))
                except Exception as error:
                    row.update(completed=False, error_type=type(error).__name__)
                row['seconds'] = round(time.perf_counter() - started, 3)
                persist()
                print(json.dumps({k: v for k, v in row.items() if k not in ('text', 'checks')}, ensure_ascii=False), flush=True)
    expected = len(cases) * (1 if reused else 2)
    return 0 if len(report['results']) == expected and all(r['completed'] for r in report['results']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
