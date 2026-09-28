"""Isolated manual UI preview; never opens the default user library or calls AI."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ui.capture_desk import CaptureDeskApp
from services.document_library import DocumentLibrary
from tools.evaluate_capture_quality import fixtures
from main import _enable_windows_dpi_awareness


def main():
    _enable_windows_dpi_awareness()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('.local-results/manual-desk'))
    args = parser.parse_args()
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    images = fixtures(root / 'fixtures')
    library = DocumentLibrary(root / 'store')
    app = CaptureDeskApp(library=library)
    app.title('Ssokly · 합성 실사용 점검')
    if not library.list_documents():
        first = app.accept_capture(images['table'], auto_read=False, title='체험 프로그램 일정')
        app.accept_capture(images['notice'], document_id=first['document_id'], auto_read=False)
        app.accept_capture(images['notice'], auto_read=False, title='독서교실 학부모 안내')
    else:
        app.open_document(library.list_documents()[0]['id'])
    app.mainloop()


if __name__ == '__main__':
    main()
