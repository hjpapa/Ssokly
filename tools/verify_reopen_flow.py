"""Cross-process capture/edit/search/reopen check using temporary synthetic data."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
from main import _enable_windows_dpi_awareness
from services.document_library import DocumentLibrary
from ui.capture_desk import CaptureDeskApp

TEXT = '합성 재실행 확인: 신청 마감 2026. 10. 2. 16:00'


def phase(root, reopen):
    _enable_windows_dpi_awareness()
    library = DocumentLibrary(root)
    app = CaptureDeskApp(library=library)
    app.attributes('-alpha', 0)
    try:
        if not reopen:
            with Image.new('RGB', (900, 1200), '#e5f2f1') as image:
                first = app.accept_capture(image, auto_read=False, title='합성 재실행 문서')
                app.accept_capture(image, document_id=first['document_id'], auto_read=False)
                app.accept_capture(image, document_id=first['document_id'], auto_read=False)
            app.source_editor.delete('1.0', 'end')
            app.source_editor.insert('1.0', TEXT)
            app.source_editor.edit_modified(True)
            app._source_modified()
            app.labels_var.set('재실행검증, 일정')
            app.memo_var.set('합성 메모 검색')
            # close() must flush pending edits without waiting for autosave.
        else:
            records = library.list_documents()
            assert len(records) == 1
            doc = records[0]
            assert doc['labels'] == ['재실행검증', '일정']
            assert doc['memo'] == '합성 메모 검색'
            pages = library.pages(doc['id'])
            assert len(pages) == 3
            assert pages[-1]['text'] == TEXT
            for page in pages:
                with Image.open(page['path']) as image:
                    assert image.size == (900, 1200)
            app.open_document(doc['id'])
            for query in ('재실행 문서', '신청 마감', '재실행검증', '합성 메모'):
                app.query.set(query)
                app.refresh_library()
                assert app.document_tree.get_children() == (doc['id'],), query
            app.update()
            assert app.image_view._photo is not None
        app.close()
        assert app._closing
    finally:
        if not app._closing:
            app.destroy()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path)
    parser.add_argument('--phase', choices=('write', 'reopen'))
    args = parser.parse_args()
    if args.phase:
        phase(args.root, args.phase == 'reopen')
        return
    with tempfile.TemporaryDirectory(prefix='ssokly-reopen-') as directory:
        for step in ('write', 'reopen'):
            subprocess.run([sys.executable, __file__, '--root', directory, '--phase', step], check=True)
    print(json.dumps({'passed': True, 'captures': 3, 'processes': 2,
                      'checks': ['pending edit on close', 'labels', 'memo', 'four search fields', 'image reload']}))


if __name__ == '__main__':
    main()
