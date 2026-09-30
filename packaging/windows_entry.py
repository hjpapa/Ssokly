"""Frozen desktop entry; optional isolated offline diagnostics for packaging."""
import json
from pathlib import Path
import sys
import tempfile


def self_test(report_path):
    import tkinter as tk
    import httpx
    import openai
    import mss
    import docx
    import pptx
    import openpyxl
    import pypdfium2 as pdfium
    from PIL import Image
    from striprtf.striprtf import rtf_to_text
    from main import _enable_windows_dpi_awareness
    from ui.capture_desk import CaptureDeskApp
    from services.document_library import DocumentLibrary
    from services.ai_relay import server_url
    from services.source_review import source_table_blocks
    from services.work_image import image_request, validate_png
    from ui.work_image import WorkImageWindow
    import io
    import time
    _enable_windows_dpi_awareness()
    with tempfile.TemporaryDirectory(prefix='ssokly-package-test-') as temporary:
        root = Path(temporary)
        # Load the native PDFium library and render a page, not just import it.
        pdf = pdfium.PdfDocument.new()
        page = pdf.new_page(200, 200)
        bitmap = page.render()
        assert bitmap.to_pil().size == (200, 200)
        bitmap.close()
        page.close()
        pdf.close()
        docx.Document().save(root / 'sample.docx')
        docx.Document(root / 'sample.docx')
        pptx.Presentation().save(root / 'sample.pptx')
        pptx.Presentation(root / 'sample.pptx')
        workbook = openpyxl.Workbook()
        workbook.save(root / 'sample.xlsx')
        workbook.close()
        openpyxl.load_workbook(root / 'sample.xlsx').close()
        assert rtf_to_text(r'{\rtf1 synthetic}') == 'synthetic'
        app = CaptureDeskApp(library=DocumentLibrary(root / 'store'))
        try:
            app.withdraw()
            app.accept_capture(Image.new('RGB', (100, 100), 'white'), auto_read=False)
            app.update_idletasks()
            assert len(app.library.list_documents()) == 1
            assert len(source_table_blocks('A | B\nC | D')) == 1
            sample = io.BytesIO()
            Image.new('RGB', (100, 150), 'white').save(sample, 'PNG')
            assert image_request('합성 업무')['model'] == 'gpt-image-2.5-flare'
            dialog = WorkImageWindow(app, '합성 업무 · 희망자만')
            dialog.started = time.monotonic()
            dialog.results.put((validate_png(sample.getvalue()), '합성 업무 · 희망자만', None))
            dialog.poll()
            app.update_idletasks()
            assert dialog.data == sample.getvalue()
            assert dialog.view._image.size == (100, 150)
            dialog.close()
        finally:
            for callback in app.tk.splitlist(app.tk.call('after', 'info')):
                app.after_cancel(callback)
            app.update_idletasks()
            app.destroy()
        assert len(DocumentLibrary(root / 'store').list_documents()) == 1
    Path(report_path).write_text(json.dumps({
        'passed': True, 'frozen': bool(getattr(sys, 'frozen', False)),
        'python': sys.version.split()[0], 'relay': server_url(),
        'checks': ['Tk', 'PDFium render', 'DOCX', 'PPTX', 'XLSX', 'RTF',
                   'capture persistence', 'pipe table', 'OpenAI/httpx imports', 'work image preview'],
        'api_calls': 0,
    }, indent=2), encoding='utf-8')


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--self-test-report':
        try:
            self_test(sys.argv[2])
        except Exception as error:
            Path(sys.argv[2]).write_text(json.dumps({'passed': False, 'error_type': type(error).__name__}), encoding='utf-8')
            raise
    else:
        from main import main
        main()
