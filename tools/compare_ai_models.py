"""Opt-in direct OpenAI comparison on synthetic notices; never changes the relay."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import httpx
from dotenv import dotenv_values
from services.ocr_service import OCR_PROMPT, _image_to_data_url, _prepare_image_for_model
from services.text_actions import build_instructions
from tools.evaluate_official_documents import render_notice, checks
from tools.official_document_cases import CASES

CONFIGS = (
    {'name': 'nano', 'model': 'gpt-5-nano', 'ocr': 'minimal', 'summary': 'low'},
    {'name': 'luna-none', 'model': 'gpt-6-luna', 'ocr': 'none', 'summary': 'none'},
    {'name': 'luna-low', 'model': 'gpt-6-luna', 'ocr': 'low', 'summary': 'low'},
)


def normalized(text):
    # Ignore layout whitespace and printed table delimiters, not letters/digits.
    return re.sub(r'[\s|]', '', text)


def edit_distance(left, right):
    previous = list(range(len(right) + 1))
    for i, char in enumerate(left, 1):
        current = [i]
        for j, other in enumerate(right, 1):
            current.append(min(current[-1] + 1, previous[j] + 1,
                               previous[j - 1] + (char != other)))
        previous = current
    return previous[-1]


def request_body(config, operation, content):
    instructions = OCR_PROMPT if operation == 'ocr' else build_instructions('요약', '교직원')
    parts = ([{'type': 'input_text', 'text': '이 이미지를 고정밀 OCR로 전사해 주세요.'},
              {'type': 'input_image', 'image_url': content, 'detail': 'high'}]
             if operation == 'ocr' else [{'type': 'input_text', 'text': content}])
    return {'model': config['model'], 'store': False, 'text': {'verbosity': 'low'},
            'reasoning': {'effort': config[operation]}, 'instructions': instructions,
            'input': [{'role': 'user', 'content': parts}]}


def completed_text(data):
    if data.get('status') != 'completed' or data.get('error') or data.get('incomplete_details'):
        raise ValueError('incomplete')
    pieces = []
    for item in data.get('output', []):
        if item.get('type') != 'message':
            continue
        if item.get('status') != 'completed':
            raise ValueError('incomplete_message')
        for part in item.get('content', []):
            if part.get('type') == 'refusal':
                raise ValueError('refusal')
            if part.get('type') == 'output_text':
                pieces.append(part['text'])
    text = ''.join(pieces).strip()
    if not text:
        raise ValueError('empty')
    return text


def safe_usage(data):
    usage = data.get('usage') or {}
    return {**{key: usage.get(key) for key in ('input_tokens', 'output_tokens', 'total_tokens')},
            'cached_input_tokens': (usage.get('input_tokens_details') or {}).get('cached_tokens'),
            'reasoning_tokens': (usage.get('output_tokens_details') or {}).get('reasoning_tokens')}


def aggregate(report):
    rows = report['results']
    if not rows or not all(row.get('completed') for row in rows):
        raise ValueError('Cannot summarize unfinished or failed comparisons')
    metrics = []
    for config in CONFIGS:
        selected = [r for r in rows if r['configuration'] == config['name']]
        if not selected:
            raise ValueError('Missing configuration')
        cases = sorted({r['case'] for r in selected})
        pairs = []
        for case in cases:
            pair = [r for r in selected if r['case'] == case]
            if len(pair) != 2 or {r['operation'] for r in pair} != {'ocr', 'summary'}:
                raise ValueError('Incomplete OCR/summary pair')
            pairs.append(sum(r['total_seconds'] for r in pair))
        ocr = [r for r in selected if r['operation'] == 'ocr']
        chars = sum(r['reference_characters'] for r in ocr)
        edits = sum(r['character_edits'] for r in ocr)
        metrics.append({'configuration': config['name'], 'documents': len(cases), 'requests': len(selected),
                        'ocr_median_seconds': statistics.median(r['total_seconds'] for r in ocr),
                        'summary_median_seconds': statistics.median(r['total_seconds'] for r in selected if r['operation'] == 'summary'),
                        'pipeline_median_seconds': statistics.median(pairs),
                        'pipeline_mean_seconds': statistics.mean(pairs), 'pipeline_max_seconds': max(pairs),
                        'ocr_exact_documents': sum(r['character_edits'] == 0 for r in ocr),
                        'character_edits': edits, 'reference_characters': chars,
                        'character_error_rate': edits / chars,
                        'usage': {k: sum(r['usage'][k] for r in selected) for k in
                                  ('input_tokens', 'output_tokens', 'total_tokens', 'reasoning_tokens', 'cached_input_tokens')}})
    return metrics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Paid calls, only after explicit user authorization.')
    parser.add_argument('--preflight-only', action='store_true', help='Read model availability; no generation calls.')
    parser.add_argument('--summarize', type=Path, help='Summarize an existing report offline; does not load keys.')
    parser.add_argument('--output', type=Path, default=ROOT / '.local-results/model-comparison')
    args = parser.parse_args()
    if args.summarize:
        print(json.dumps(aggregate(json.loads(args.summarize.read_text(encoding='utf-8'))), indent=2))
        return 0
    if not args.live and not args.preflight_only:
        print(json.dumps({'configs': CONFIGS, 'cases': len(CASES), 'planned_generation_calls': 60}))
        return 0
    key = os.environ.get('OPENAI_API_KEY') or dotenv_values(ROOT / '.env').get('OPENAI_API_KEY')
    if not key:
        raise SystemExit('Existing authorized key not available')
    args.output.mkdir(parents=True, exist_ok=True)
    report_path = args.output / ('preflight.json' if args.preflight_only else 'report.json')
    if report_path.exists():
        raise SystemExit('Use a new output directory; existing results are never overwritten')
    report = {'transport': 'direct_openai_no_vercel', 'synthetic_only': True, 'configs': CONFIGS,
              'generation_attempts': 0, 'availability': [], 'results': [],
              'prompts_sha256': hashlib.sha256((OCR_PROMPT + build_instructions('요약', '교직원')).encode()).hexdigest()}
    def persist():
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    with httpx.Client(timeout=300, follow_redirects=False,
                      headers={'Authorization': 'Bearer ' + key}) as client:
        for model in dict.fromkeys(c['model'] for c in CONFIGS):
            try:
                response = client.get('https://api.openai.com/v1/models/' + model)
                row = {'model': model, 'http_status': response.status_code, 'available': response.status_code == 200}
                if response.status_code != 200:
                    error = response.json().get('error', {})
                    row['error_code'] = error.get('code')
                    row['error_type'] = error.get('type')
            except Exception as error:
                row = {'model': model, 'available': False, 'exception_type': type(error).__name__}
            report['availability'].append(row)
            persist()
            print(json.dumps(row), flush=True)
        if args.preflight_only or not all(r['available'] for r in report['availability']):
            return 0 if all(r['available'] for r in report['availability']) else 2
        for index, case in enumerate(CASES):
            with render_notice(case['text']) as rendered:
                rendered.save(args.output / (case['id'] + '.png'))
            # Rotate order to avoid always measuring one model first.
            configs = CONFIGS[index % 3:] + CONFIGS[:index % 3]
            for config in configs:
                ocr = None
                for operation in ('ocr', 'summary'):
                    if operation == 'summary' and ocr is None:
                        continue
                    row = {'case': case['id'], 'configuration': config['name'], 'operation': operation}
                    report['results'].append(row)
                    started = time.perf_counter()
                    try:
                        if operation == 'ocr':
                            with render_notice(case['text']) as image, _prepare_image_for_model(image) as prepared:
                                content = _image_to_data_url(prepared)
                        else:
                            content = ocr
                        body = request_body(config, operation, content)
                        report['generation_attempts'] += 1
                        persist()
                        sent = time.perf_counter()
                        response = client.post('https://api.openai.com/v1/responses', json=body)
                        row.update(http_status=response.status_code, request_seconds=round(time.perf_counter() - sent, 3))
                        data = response.json()
                        if response.status_code != 200:
                            row['error_code'] = (data.get('error') or {}).get('code')
                            row['error_type'] = (data.get('error') or {}).get('type')
                            raise ValueError('request_failed')
                        row.update(model_returned=data.get('model'), usage=safe_usage(data))
                        text = completed_text(data)
                        row.update(completed=True, text=text, checks=checks(case, text))
                        if operation == 'ocr':
                            ocr = text
                            reference, actual = normalized(case['text']), normalized(text)
                            row['character_edits'] = edit_distance(reference, actual)
                            row['reference_characters'] = len(reference)
                    except Exception as error:
                        row.update(completed=False, exception_type=type(error).__name__)
                    row['total_seconds'] = round(time.perf_counter() - started, 3)
                    persist()
                    print(json.dumps({k: v for k, v in row.items() if k not in ('text', 'checks')}, ensure_ascii=False), flush=True)
                    if not row['completed']:
                        return 1  # No retry or continued paid batch after a failure.
    report['aggregate'] = aggregate(report)
    persist()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
