"""Offline UI replay of all supported file types and ten fictional notices."""
import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.transfer_policy import make_text_snapshot
from services.document_service import SUPPORTED_FILE_EXTENSIONS
from tools.file_fixtures import create_supported_fixtures
from tools.official_document_cases import CASES
from ui.capture_desk import CaptureDeskApp


def verify():
    with tempfile.TemporaryDirectory(prefix='ssokly-release-') as directory, ExitStack() as guards:
        guards.enter_context(patch('socket.socket.connect', side_effect=AssertionError('offline')))
        guards.enter_context(patch('socket.getaddrinfo', side_effect=AssertionError('offline')))
        errors = guards.enter_context(patch('ui.capture_desk.messagebox.showerror'))
        guards.enter_context(patch('services.text_actions.generate_text_action',
                                   side_effect=lambda text, *a, **kw: text))
        root = Path(directory)
        fixtures = create_supported_fixtures(root / 'fixtures')
        assert set(fixtures) == set(SUPPORTED_FILE_EXTENSIONS)
        app = CaptureDeskApp(app_data_dir=root / 'store')
        app.withdraw()
        app.capture_mode.set('보관만')
        records = []
        expected = {}
        try:
            for extension, path in sorted(fixtures.items()):
                count = len(app.library.list_documents())
                app.import_file(path)
                app.update()
                assert len(app.library.list_documents()) == count + 1, extension
                assert app.page_records, extension
                records.append(extension)
            app._choose_transfer = lambda **kwargs: make_text_snapshot(kwargs['text'])
            for case in CASES:
                document = app.library.create_document(case['id'] + ' ' + case['topic'])
                app.library.add_text_page(document['id'], case['text'])
                assert app.open_document(document['id'])
                app.update()
                app.labels_var.set('합성 검증')
                app.memo_var.set('조건 보존 확인')
                assert app.flush_edits()
                for mode in ('요약', '일정·할 일 정리', '안내문'):
                    app.ai_mode.set(mode)
                    app.generate()
                    deadline = time.monotonic() + 10
                    while app._jobs and time.monotonic() < deadline:
                        app.update()
                        time.sleep(.01)
                    assert not app._jobs
                    assert app.output_editor.get('1.0', 'end-1c') == case['text'], (case['id'], mode)
                edited = case['text'] + '\n합성 검수 완료'
                app.output_editor.delete('1.0', 'end')
                app.output_editor.insert('1.0', edited)
                app.output_editor.edit_modified(True)
                app._output_modified()
                assert app.flush_edits()
                assert app.library.document_text(document['id']) == case['text']
                app.library.trash(document['id'])
                app.library.restore(document['id'])
                expected[document['id']] = edited
            errors.assert_not_called()
        finally:
            app.destroy()
        app = CaptureDeskApp(app_data_dir=root / 'store')
        app.withdraw()
        try:
            for identifier, text in expected.items():
                assert app.open_document(identifier)
                app.update()
                assert app.output_editor.get('1.0', 'end-1c') == text
                assert app.memo_var.get() == '조건 보존 확인'
                assert identifier in [row['id'] for row in app.library.list_documents(query='조건 보존 확인')]
        finally:
            app.destroy()
        return {'passed': True, 'file_formats': records, 'documents': len(CASES),
                'mock_ai_actions': len(CASES) * 3, 'api_calls': 0,
                'checks': ['UI file import', 'three action modes', 'edited result persistence',
                           'source unchanged', 'labels/memo', 'search', 'trash/restore', 'new app reopen'],
                'limits': ['mock model output', 'synthetic data', 'not native capture/clipboard']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('.local-results/release-scenarios.json'))
    args = parser.parse_args()
    report = verify()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report))
