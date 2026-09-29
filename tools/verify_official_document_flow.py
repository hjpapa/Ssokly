"""Replay synthetic evaluation results through the real UI/storage, offline."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
from services.document_library import DocumentLibrary
from ui.capture_desk import CaptureDeskApp
from ui.desk_text import fingerprint
from tools.official_document_cases import CASES


def results(path, operation):
    report = json.loads(path.read_text(encoding='utf-8'))
    if report.get('synthetic_only') is not True:
        raise ValueError('Only synthetic evaluation reports are accepted')
    return {r['case']: r['text'] for r in report['results']
            if r['operation'] == operation and r.get('completed')}


def verify(ocr_report, summary_report):
    ocr, summaries = results(ocr_report, 'ocr'), results(summary_report, 'summary')
    with tempfile.TemporaryDirectory(prefix='ssokly-official-flow-') as directory, \
            patch('socket.socket.connect', side_effect=AssertionError('network forbidden')), \
            patch('socket.getaddrinfo', side_effect=AssertionError('network forbidden')):
        library = DocumentLibrary(directory)
        app = CaptureDeskApp(library=library)
        app.attributes('-alpha', 0)
        identifiers = {}
        try:
            for case in CASES:
                key = case['id']
                with Image.open(ocr_report.parent / (key + '.png')) as image:
                    page = app.accept_capture(image, auto_read=False, title=key + ' ' + case['topic'])
                library.remember_initial_ocr(page['id'], ocr[key])
                library.capture_store.update_ocr(page['capture_id'], ocr[key], profile='gpt-5-nano')
                library.save_output(page['document_id'], '요약', summaries[key], fingerprint(ocr[key]))
                identifiers[key] = page['document_id']
            assert len(library.list_documents()) == 10
            app.close()
        finally:
            if not app._closing:
                app.destroy()
        # New app/library objects must reload all content, images and outputs.
        app = CaptureDeskApp(app_data_dir=directory)
        app.attributes('-alpha', 0)
        try:
            for key, identifier in identifiers.items():
                app.query.set('')
                app.refresh_library()
                app.update()  # Drain the prior search's queued selection event.
                assert app.open_document(identifier)
                app.update()
                assert app.source_editor.get('1.0', 'end-1c') == ocr[key], key
                assert app.output_editor.get('1.0', 'end-1c') == summaries[key]
                assert app.image_view._photo is not None
                app.query.set(key)
                app.refresh_library()
                app.update()
                assert identifier in app.document_tree.get_children()
                app.library.trash(identifier)
                app.library.restore(identifier)
                assert app.open_document(identifier)
                assert app.output_editor.get('1.0', 'end-1c') == summaries[key]
            app.close()
        finally:
            if not app._closing:
                app.destroy()
    return {'passed': True, 'documents': 10, 'paid_requests': 0,
            'checks': ['image/OCR/summary storage', 'new app reopen', 'UI text equality', 'image rendering', 'search', 'trash/restore']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ocr-report', type=Path, required=True)
    parser.add_argument('--summary-report', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.ocr_report, args.summary_report)))
