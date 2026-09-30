"""Offline DPI/window/state matrix on synthetic temporary data; no OS DPI edits."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
from ui.capture_desk import CaptureDeskApp
from ui.desk_theme import apply_theme


def visible(widget, root):
    x, y = widget.winfo_rootx() - root.winfo_rootx(), widget.winfo_rooty() - root.winfo_rooty()
    return bool(widget.winfo_ismapped() and widget.winfo_width() > 10 and widget.winfo_height() > 10
                and x >= 0 and y >= 0 and x + widget.winfo_width() <= root.winfo_width()
                and y + widget.winfo_height() <= root.winfo_height())


def evaluate(percentages=(100, 125, 150, 175, 200, 225, 250, 300)):
    rows = []
    for percent in percentages:
        with tempfile.TemporaryDirectory() as directory:
            def theme(root):
                root.tk.call('tk', 'scaling', percent / 75)
                return apply_theme(root)
            with patch('ui.capture_desk.apply_theme', theme):
                app = CaptureDeskApp(app_data_dir=directory)
            app.attributes('-alpha', 0)
            try:
                app.accept_capture(Image.new('RGB', (800, 1100), 'white'), auto_read=False)
                for geometry in ('720x680', '1024x768', '1280x800', '1920x1080'):
                    for state in ('idle', 'busy', 'cancelled', 'details', 'notice', 'library'):
                        app.geometry(geometry)
                        app._jobs.clear()
                        app.status.set('합성 문서를 보관했습니다.')
                        if app.details_panel.winfo_manager():
                            app.toggle_details()
                        app._compact_library = False
                        app.editor_tabs.select(app.text_panel)
                        app.ai_mode.set('요약')
                        app._ai_mode_changed()
                        app.update()
                        if state in ('busy', 'cancelled'):
                            app._jobs['synthetic'] = {'kind': 'ocr', 'cancel': threading.Event(), 'started': time.monotonic() - 35}
                        app.job_progress.refresh(app._jobs)
                        if state == 'cancelled':
                            app.cancel_jobs()
                        elif state == 'details':
                            app.toggle_details()
                        elif state == 'notice':
                            app.ai_mode.set('안내문')
                            app._ai_mode_changed()
                            app.editor_tabs.select(app.ai_panel)
                        elif state == 'library':
                            if not app.library_panel.winfo_ismapped():
                                app.toggle_library()
                        app._reset_work_sash = True
                        app._apply_layout()
                        start = time.perf_counter()
                        app.update()
                        widgets = {'capture': app.capture_button, 'library_button': app.library_button}
                        if state == 'library':
                            widgets['document_list'] = app.document_tree
                        else:
                            widgets.update(title=app.title_entry, original=app.image_view.canvas)
                            if state == 'notice':
                                widgets.update(generate=app.generate_button, audience=app.audience_picker,
                                               editor=app.output_editor, work_image=app.work_image_button)
                            else:
                                widgets.update(editor=app.source_editor, read=app.read_button)
                        issues = [name + '_clipped' for name, w in widgets.items() if not visible(w, app)]
                        if state != 'library':
                            if app.image_view.canvas.winfo_height() < 100:
                                issues.append('original_under_100px')
                            if widgets['editor'].winfo_height() < 70:
                                issues.append('editor_under_70px')
                        rows.append({'dpi_percent': percent, 'geometry': geometry, 'state': state,
                                     'actual_geometry': f'{app.winfo_width()}x{app.winfo_height()}',
                                     'original_height': app.image_view.canvas.winfo_height(),
                                     'editor_height': widgets.get('editor', app.source_editor).winfo_height(),
                                     'layout_ms': round((time.perf_counter() - start) * 1000, 1), 'issues': issues})
            finally:
                app.destroy()
        print(json.dumps({'dpi_percent': percent, 'cases': len(rows), 'issue_cases': sum(bool(r['issues']) for r in rows)}), flush=True)
    return rows


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('.local-results/usability/matrix.json'))
    parser.add_argument('--percent', type=int, action='append')
    args = parser.parse_args()
    results = evaluate(args.percent or (100, 125, 150, 175, 200, 225, 250, 300))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'cases': len(results), 'issue_cases': sum(bool(r['issues']) for r in results)}))
