"""Bounded live evaluation using generated, non-personal Korean examples only."""
import argparse
import json
from pathlib import Path
import re
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image, ImageDraw, ImageFont

ROWS = [
    ['구분', '신청 마감', '행사일'],
    ['독서교실', '2026. 10. 2.(금) 16:00까지', '2026. 10. 16.(금) 14:00'],
    ['과학교실', '2026. 10. 6.(화) 15:00까지', '2026. 10. 20.(화) 10:00'],
]
NOTICE = ('독서교실 안내\n'
          '행사: 2026년 10월 16일(금) 오후 2시, 학교 도서관\n'
          '대상: 참여를 희망하는 3학년 학생 / 참가비: 무료\n'
          '학부모는 10월 2일 오후 4시까지 담임에게 신청합니다.\n'
          '희망자만 신청하며 참여하지 않으면 회신하지 않아도 됩니다.\n'
          '교직원 내부 업무: 담임은 10월 7일까지 연구부에 명단을 제출합니다.')


def fixtures(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    font = ImageFont.truetype('C:/Windows/Fonts/malgun.ttf', 32)
    title = ImageFont.truetype('C:/Windows/Fonts/malgunbd.ttf', 40)
    table = Image.new('RGB', (1640, 680), 'white')
    draw = ImageDraw.Draw(table)
    draw.text((40, 35), '방과후 체험 프로그램 · 합성 시험 문서', font=title, fill='#173c46')
    xs = [40, 310, 970, 1600]
    for r, row in enumerate(ROWS):
        for c, value in enumerate(row):
            box = (xs[c], 145 + r * 120, xs[c + 1], 265 + r * 120)
            draw.rectangle(box, fill='#e3f2ef' if r == 0 else 'white', outline='#71868c', width=2)
            draw.text((xs[c] + 15, 184 + r * 120), value, font=font, fill='#182f39')
    draw.text((40, 565), '희망자만 신청합니다. 참가비는 무료입니다.', font=font, fill='#182f39')
    table.save(directory / 'synthetic-table.png')
    notice = Image.new('RGB', (1640, 680), 'white')
    ImageDraw.Draw(notice).multiline_text((40, 50), NOTICE, font=font, fill='#182f39', spacing=32)
    notice.save(directory / 'synthetic-notice.png')
    return {'table': table, 'notice': notice}


def compact(value):
    return re.sub(r'\s+', '', value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Authorize two OCR and three text generation calls.')
    parser.add_argument('--output', type=Path, default=Path('.local-results/quality'))
    args = parser.parse_args()
    images = fixtures(args.output)
    if not args.live:
        print(json.dumps({'fixtures_created': 2, 'api_calls': 0}))
        return 0
    from services.ocr_service import extract_text_from_image
    from services.text_actions import generate_text_action
    records = []
    outputs = {}
    def run(name, operation):
        started = time.perf_counter()
        try:
            outputs[name] = operation()
            records.append({'case': name, 'completed': True, 'seconds': round(time.perf_counter() - started, 3)})
        except Exception as error:
            # SDK errors may contain request material; retain the type only.
            records.append({'case': name, 'completed': False, 'error_type': type(error).__name__,
                            'seconds': round(time.perf_counter() - started, 3)})
        print(json.dumps(records[-1]), flush=True)
    for name, image in images.items():
        run('ocr_' + name, lambda image=image: extract_text_from_image(image, model_override='gpt-5-nano', raise_errors=True))
    if 'ocr_table' in outputs:
        run('schedule', lambda: generate_text_action(outputs['ocr_table'], '일정·할 일 정리'))
    if 'ocr_notice' in outputs:
        run('summary', lambda: generate_text_action(outputs['ocr_notice'], '요약'))
        run('parent_notice', lambda: generate_text_action(outputs['ocr_notice'], '안내문', '학부모'))
    actual_rows = [line.split('\t') for line in outputs.get('ocr_table', '').splitlines() if '\t' in line]
    checks = {
        'table_cells_and_dates': all(any([compact(c) for c in row] == [compact(c) for c in expected] for row in actual_rows) for expected in ROWS),
        'notice_dates_and_conditions': all(compact(s) in compact(outputs.get('ocr_notice', '')) for s in ('10월 16일', '10월 2일', '오후 4시', '희망자만', '회신하지 않아도', '10월 7일')),
        'parent_has_event_and_application': all(s in outputs.get('parent_notice', '') for s in ('16일', '2일', '3학년', '무료')),
        'parent_excludes_internal_work': all(s not in outputs.get('parent_notice', '') for s in ('연구부', '명단', '7일')) and bool(outputs.get('parent_notice')),
        'all_five_requests_completed': len(outputs) == 5,
    }
    report = {'synthetic_only': True, 'model': 'gpt-5-nano', 'requests': records, 'checks': checks,
              'automated_pass': all(checks.values()), 'outputs': outputs,
              'limits': ['two_synthetic_images_only', 'human_fact_review_required', 'not_real_school_document_accuracy']}
    (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'checks': checks, 'automated_pass': report['automated_pass']}))
    return 0 if report['automated_pass'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
