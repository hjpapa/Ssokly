"""Synthetic file-open and opt-in live OCR check; never uses real documents."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.document_library import DocumentLibrary
from services.file_import import import_local_document
from tools.file_fixtures import create_fixtures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Send two synthetic page images for paid OCR.')
    parser.add_argument('--output', type=Path, default=Path('.local-results/file-quality'))
    args = parser.parse_args()
    paths = create_fixtures(args.output)
    report = {'synthetic_only': True, 'local': [], 'requests': []}
    with tempfile.TemporaryDirectory(prefix='ssokly-files-') as directory:
        library = DocumentLibrary(directory)
        for path in paths:
            document, unread = import_local_document(library, path)
            pages = library.pages(document['id'])
            local_ok = all(value in pages[0]['text'] for value in ('독서교실', '2026. 10. 2. 16:00', '2026. 10. 16. 14:00'))
            report['local'].append({'format': path.suffix, 'pages': len(pages), 'ocr_needed': unread, 'passed': local_ok})
            if args.live:
                from services.ocr_service import extract_text_from_image
                from PIL import Image
                started = time.perf_counter()
                try:
                    with Image.open(pages[1]['path']) as image:
                        text = extract_text_from_image(image, model_override='gpt-5-nano', raise_errors=True)
                    compact = ''.join(text.split())
                    passed = all(value in compact for value in ('10월2일', '오후4시', '10월16일', '오후2시', '3학년', '무료'))
                    result = {'format': path.suffix, 'completed': True, 'passed': passed, 'text': text}
                except Exception as error:
                    result = {'format': path.suffix, 'completed': False, 'passed': False, 'error_type': type(error).__name__}
                result['seconds'] = round(time.perf_counter() - started, 3)
                report['requests'].append(result)
                print(json.dumps({key: value for key, value in result.items() if key != 'text'}), flush=True)
    report['passed'] = all(item['passed'] for item in report['local'] + report['requests'])
    report['limits'] = ['synthetic fixtures only', 'no general accuracy guarantee', 'PDF page image OCR, not whole-file API']
    (args.output / 'reading-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'passed': report['passed'], 'api_calls': len(report['requests'])}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
