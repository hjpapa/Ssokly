"""Local HWPX check; --live additionally sends only a synthetic table to OpenAI."""
import argparse
import json
import sys
import tempfile
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.document_service import read_hwpx_file
from services.date_evidence import source_schedules
from services.source_review import tabular_blocks, review_spans


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('hwpx', type=Path)
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    source = read_hwpx_file(args.hwpx)
    schedules = source_schedules(source)
    assert tabular_blocks(source) and schedules
    print(json.dumps({'tables': len(tabular_blocks(source)), 'schedules': schedules,
                      'red_marks': len(review_spans(source))}, ensure_ascii=True), flush=True)
    if not args.live:
        return
    from PIL import Image, ImageDraw, ImageFont
    from services.ocr_service import extract_text_from_image
    from services.ai_service import analyze_document_task
    image = Image.new('RGB', (1800, 360), 'white')
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype('C:/Windows/Fonts/malgun.ttf', 28)
    rows = [('업무', '시 행사', '도 행사'),
            ('참가접수', '2026. 8. 28.(금) 16:00까지', '2026. 9. 30.(수) 18:00까지'),
            ('결과발표', '2026. 9. 24.(목) 예정', '2026. 10. 26.(월) 예정')]
    xs = [10, 270, 1030, 1790]
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            draw.rectangle((xs[c], 10+r*110, xs[c+1], 120+r*110), outline='black', width=2)
            draw.text((xs[c]+15, 40+r*110), value, fill='black', font=font)
    start = time.perf_counter()
    ocr = extract_text_from_image(image, model_override='gpt-5-nano', raise_errors=True)
    seconds = round(time.perf_counter()-start, 2)
    actual = source_schedules(ocr)
    expected = source_schedules('\n'.join('\t'.join(row) for row in rows))
    assert actual == expected, (ocr, actual, expected)
    assert not review_spans(ocr)
    with tempfile.TemporaryDirectory() as directory:
        start = time.perf_counter()
        result = analyze_document_task(ocr, cache_dir=Path(directory), raise_errors=True)
        for context, when in expected:
            assert context in result and when in result, result
        print(json.dumps({'synthetic_ocr_seconds': seconds, 'analysis_seconds': round(time.perf_counter()-start, 2),
                          'capture_table_to_schedule_passed': True}, ensure_ascii=True), flush=True)


if __name__ == '__main__':
    main()
