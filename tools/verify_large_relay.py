"""One paid OCR on a synthetic >4.5MB PNG; no local API key or user documents."""
import argparse
import json
import os
from pathlib import Path
import random
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import httpx
from PIL import Image, ImageDraw, ImageFont
from services.ocr_service import extract_text_from_image, _image_to_data_url, _prepare_image_for_model


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    if not args.live:
        parser.error('--live required: one synthetic paid OCR request')
    width, height = 2400, 1600
    image = Image.frombytes('RGB', (width, height), random.Random(31).randbytes(width * height * 3))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, width, 480), fill='white')
    font = ImageFont.truetype('C:/Windows/Fonts/malgun.ttf', 50)
    lines = ['합성 대용량 OCR 시험', '신청 마감: 2026년 10월 2일 오후 4시',
             '행사: 2026년 10월 16일 오후 2시', '대상: 희망하는 3학년 학생 / 참가비: 무료']
    for index, line in enumerate(lines):
        draw.text((60, 35 + 105 * index), line, font=font, fill='black')
    prepared = _image_to_data_url(_prepare_image_for_model(image))
    data_size = len(__import__('base64').b64decode(prepared.split(',', 1)[1]))
    report = {'png_bytes': data_size, 'old_json_bytes': len(json.dumps({'operation': 'ocr', 'image': prepared}).encode()),
              'function_request_bytes': [], 'upload_status': None, 'anonymous_read_status': None, 'cleanup_status': None}
    assert data_size > 4_500_000
    original = httpx.Client
    class ObservedClient(original):
        def post(self, url, **kwargs):
            body = kwargs.get('content') or json.dumps(kwargs.get('json', {})).encode()
            report['function_request_bytes'].append(len(body))
            response = super().post(url, **kwargs)
            if kwargs.get('json', {}).get('action') == 'delete':
                report['cleanup_status'] = response.status_code
            return response

        def put(self, url, **kwargs):
            response = super().put(url, **kwargs)
            report['upload_status'] = response.status_code
            if response.status_code in (200, 201):
                private_url = response.json()['url']
                with original(timeout=30, follow_redirects=False) as anonymous:
                    report['anonymous_read_status'] = anonymous.get(private_url).status_code
            return response
    start = time.perf_counter()
    try:
        with patch.dict(os.environ, {'SSOKLY_API_URL': 'https://ssokly-ai-relay.vercel.app', 'OPENAI_API_KEY': ''}), \
                patch('services.ocr_service._load_openai_settings', side_effect=AssertionError('Local key forbidden')), \
                patch('services.ai_relay.httpx.Client', ObservedClient):
            result = extract_text_from_image(image, raise_errors=True)
        report['facts_pass'] = all(item in result for item in ('10월 2일', '10월 16일', '3학년', '무료'))
        report['passed'] = (report['facts_pass'] and report['anonymous_read_status'] in (401, 403, 404)
                            and report['cleanup_status'] == 200 and max(report['function_request_bytes']) < 4_500_000)
    except Exception as error:
        report['passed'] = False
        report['error_type'] = type(error).__name__
        # User-safe exception only; never record grant URLs or service response bodies.
    report['seconds'] = round(time.perf_counter() - start, 3)
    destination = ROOT / '.local-results' / 'large-relay-report.json'
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))
    if not report['passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
