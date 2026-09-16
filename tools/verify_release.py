"""Synthetic release smoke test. --live opts into three paid nano calls."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image, ImageDraw, ImageFont
from services.ai_service import analyze_document_task
from services.ocr_service import extract_text_from_image
from services.personal_todos import PersonalTodoStore, checklist_items
from services.task_store import TaskStore
from services.capture_store import CaptureStore
from services.analysis_document import ParentDraft, review_parent_draft


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    source = '독서 행사 안내\n학부모는 10월 2일까지 담임에게 참가 여부를 알린다.\n담임은 10월 5일까지 명단을 연구부에 제출한다.'
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        image = Image.new('RGB', (1600, 360), 'white')
        ImageDraw.Draw(image).multiline_text((30, 30), source, font=ImageFont.truetype('C:/Windows/Fonts/malgun.ttf', 36), fill='black', spacing=20)
        capture_store = CaptureStore(directory=root / 'captures')
        capture = capture_store.save(image)
        assert capture.path.exists()
        if args.live:
            start = time.perf_counter()
            ocr = extract_text_from_image(image, model_override='gpt-5-nano', raise_errors=True)
            assert '10월' in ocr and '담임' in ocr
            print(json.dumps({'ocr_seconds': round(time.perf_counter()-start, 2)}, ensure_ascii=True), flush=True)
            result = analyze_document_task(ocr, cache_dir=root, raise_errors=True, on_metrics=lambda m: print(json.dumps(m), flush=True))
            parent = analyze_document_task(ocr, '학부모 메신저', cache_dir=root, raise_errors=True)
            assert '## 전달 문구' in parent
        else:
            ocr = source
            result = '## 체크리스트\n- [ ] 명단 제출\n  담당: 담임 · 기한: 10월 5일'
        items = checklist_items(result)
        assert items
        from ui.app import SsoklyApp
        store = TaskStore(app_data_dir=root / 'app')
        app = SsoklyApp(task_store=store, capture_store=capture_store)
        app.withdraw()
        try:
            app._replace_ocr_text(ocr, track_change=True)
            app._finish_analysis(result, '업무 일정·체크리스트')
            assert app.personal_todos.matches_analysis(result, ocr)
            app.personal_todos.add(items[:1], ocr)
            assert app.save_current_task(show_success=False)
            task_id = app.current_task_id
            assert store.get(task_id).analysis_text == result
            row = app.personal_todos.list()[0]
            app.personal_todos.set_done([row['id']], True)
            assert PersonalTodoStore(store.app_data_dir).list()[0]['done']
            assert not app.personal_todos.matches_analysis(result, ocr + ' 수정')
        finally:
            app._closing = True
            app._cancel_autosave()
            if app._worker_poll_after_id:
                app.after_cancel(app._worker_poll_after_id)
            app.destroy()
        reopened = SsoklyApp(task_store=TaskStore(app_data_dir=root / 'app'), capture_store=capture_store)
        reopened.withdraw()
        try:
            assert reopened.personal_todos.list()[0]['done']
            assert reopened.task_store.get(task_id).analysis_text == result
            assert reopened.personal_todos.matches_analysis(result, ocr)
        finally:
            reopened._closing = True
            if reopened._worker_poll_after_id:
                reopened.after_cancel(reopened._worker_poll_after_id)
            reopened.destroy()
        cleaned = review_parent_draft(ParentDraft(title='안내', message='학부모는 담임에게 회신해 주세요.\n담임은 신청서를 교무실에 전달합니다.\n010-1234-5678', questions=[]))
        assert '교무실' not in cleaned.message and '1234' not in cleaned.message
        assert '학부모는' in cleaned.message
        print(json.dumps({'passed': True, 'live': args.live, 'checks': ['image_capture_store', 'ocr_to_analysis', 'tk_result', 'task_save', 'todo_completion', 'app_reopen', 'stale_source_rejected', 'privacy_filter']}, ensure_ascii=True))


if __name__ == '__main__':
    main()
