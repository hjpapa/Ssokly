"""Measure synthetic direct/relay OCR. Explicit --live opts into three paid calls."""
import argparse
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from PIL import Image
import httpx
from services.ocr_service import extract_text_from_image, _prepare_image_for_model, _image_to_data_url
from tools.file_fixtures import create_fixtures


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', required=True)
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    if not args.live:
        parser.error('--live is required: this sends three synthetic OCR requests.')
    directory = ROOT / '.local-results' / 'relay-quality'
    directory.mkdir(parents=True, exist_ok=True)
    create_fixtures(directory)
    results = []
    with Image.open(directory / 'scan-source.png') as image:
        image.load()
        start = time.perf_counter()
        with patch.dict(os.environ, {'SSOKLY_API_URL': ''}):
            text = extract_text_from_image(image, raise_errors=True)
        results.append({'path': 'direct', 'seconds': round(time.perf_counter() - start, 3), 'text': text})
        payload = {'operation': 'ocr', 'image': _image_to_data_url(_prepare_image_for_model(image)), 'detail': 'high'}
        for label in ('relay-first', 'relay-repeat'):
            start = time.perf_counter()
            response = httpx.post(args.url.rstrip('/') + '/api/ai', json=payload, timeout=120, follow_redirects=False)
            elapsed = time.perf_counter() - start
            if response.status_code != 200:
                raise RuntimeError('Relay returned HTTP ' + str(response.status_code))
            result = response.json()
            if result.get('status') != 'completed':
                raise RuntimeError('Incomplete relay response')
            results.append({'path': label, 'seconds': round(elapsed, 3),
                            'upstream_ms': result['upstream_ms'], 'text': result['text'],
                            'outside_upstream_seconds': round(elapsed - result['upstream_ms'] / 1000, 3)})
    for result in results:
        result['facts_pass'] = all(value in result['text'] for value in ('10월 2일', '10월 16일', '3학년', '무료'))
    (directory / 'report.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps([{k: v for k, v in result.items() if k != 'text'} for result in results], ensure_ascii=False))
    if not all(result['facts_pass'] for result in results):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
