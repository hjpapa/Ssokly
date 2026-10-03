"""Explicit, independently saved image rendition of the current result text."""
import io
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, scrolledtext
from pathlib import Path

from PIL import Image
from services.work_image import generate_work_image
from services.image_clipboard import copy_image
from services.diagnostics import log_failure
from ui.desk_widgets import ZoomImageView
from ui.transfer_dialog import choose_transfer


def open_work_image(app):
    if any(job['kind'] == 'ai' for job in app._jobs.values()):
        messagebox.showinfo('업무 이미지', '텍스트 정리가 끝난 뒤 이미지를 만들어 주세요.', parent=app)
        return
    text = app.output_editor.get('1.0', 'end-1c')
    if not text.strip():
        messagebox.showinfo('업무 이미지', '먼저 정리하기로 결과를 만든 뒤 내용을 확인해 주세요.', parent=app)
        return
    existing = getattr(app, '_work_image_window', None)
    if existing is not None and existing.window.winfo_exists():
        existing.window.lift()
        messagebox.showinfo('업무 이미지', '열려 있는 이미지 창을 닫으면 현재 결과로 새 창을 열 수 있습니다.', parent=app)
        return
    app._work_image_window = WorkImageWindow(app, text)


class WorkImageWindow:
    def __init__(self, parent, text):
        self.text, self.data, self.busy = text, None, False
        self.results = queue.Queue()
        self.after_id = None
        self.window = tk.Toplevel(parent)
        self.window.title('업무 이미지 · 한글과 날짜를 대조해 주세요')
        self.window.geometry('900x740')
        self.window.minsize(620, 540)
        self.window.protocol('WM_DELETE_WINDOW', self.close)
        body = ttk.Frame(self.window, padding=10)
        body.pack(fill='both', expand=True)
        heading = ttk.Label(body, text='현재 결과의 사본입니다. 이미지의 한글·날짜·조건을 대조한 뒤 저장하세요.', wraplength=580)
        heading.pack(anchor='w')
        self.source = scrolledtext.ScrolledText(body, height=3, width=1, wrap='word', font=('Malgun Gothic', 11))
        self.source.pack(fill='x', pady=6)
        self.show_source(text)
        controls = ttk.Frame(body)
        controls.pack(fill='x')
        self.generate_button = ttk.Button(controls, text='이미지 생성', command=self.generate)
        self.generate_button.pack(side='left')
        self.save_button = ttk.Button(controls, text='PNG 저장', command=self.save, state='disabled')
        self.save_button.pack(side='left', padx=5)
        self.copy_button = ttk.Button(controls, text='이미지 복사', command=self.copy, state='disabled')
        self.copy_button.pack(side='left')
        self.note = tk.StringVar(self.window, '생성·다시 생성마다 별도 AI 비용이 발생합니다. 원래 텍스트는 유지됩니다.')
        note_label = ttk.Label(body, textvariable=self.note, wraplength=580)
        note_label.pack(fill='x', pady=6)
        self.view = ZoomImageView(body)
        self.view.pack(fill='both', expand=True)
        # Reserve real font/button space before assigning the preview remainder.
        self.window.update_idletasks()
        minimum_width = max(620, controls.winfo_reqwidth() + 20,
                            self.view.toolbar.winfo_reqwidth() + 20)
        heading.configure(wraplength=minimum_width - 20)
        note_label.configure(wraplength=minimum_width - 20)
        self.window.update_idletasks()
        minimum_height = max(540, body.winfo_reqheight() - self.view.canvas.winfo_reqheight() + 180)
        self.window.minsize(minimum_width, minimum_height)

    def show_source(self, text):
        self.source.configure(state='normal')
        self.source.delete('1.0', 'end')
        self.source.insert('1.0', text)
        self.source.configure(state='disabled')

    def generate(self):
        if self.busy:
            return
        snapshot = choose_transfer(self.window, kind='text', text=self.text,
                                   title='업무 이미지 전송 확인', action='이미지 만들기')
        if snapshot is None:
            return
        self.busy = True
        self.started = time.monotonic()
        self.generate_button.state(['disabled'])
        # Workers only use immutable text and a queue; no Tk calls after close.
        def work():
            try:
                self.results.put((generate_work_image(snapshot.text), snapshot.text, None))
            except Exception as error:
                log_failure('work_image.window_generate', error)
                self.results.put((None, None, '이미지를 만들지 못했습니다. 서버 배포·연결·모델 권한을 확인해 주세요.'))
        try:
            threading.Thread(target=work, daemon=True).start()
        except Exception as error:
            log_failure('work_image.start', error)
            self.busy = False
            self.generate_button.state(['!disabled'])
            self.note.set('이미지 생성을 시작하지 못했습니다.')
            return
        self.poll()

    def poll(self):
        self.after_id = None
        try:
            data, text, error = self.results.get_nowait()
        except queue.Empty:
            self.note.set(f'이미지를 만드는 중 · {int(time.monotonic() - self.started)}초 · 창을 닫아도 전송된 요청은 계속될 수 있습니다.')
            self.after_id = self.window.after(200, self.poll)
            return
        self.busy = False
        self.generate_button.state(['!disabled'])
        if error:
            self.note.set(error + (' 이전 이미지는 유지했습니다.' if self.data else ''))
            return
        try:
            with Image.open(io.BytesIO(data)) as image:
                self.view.set_image(image)
        except Exception as exception:
            log_failure('work_image.preview', exception)
            self.note.set('이미지 미리보기를 열지 못했습니다.')
            return
        self.data = data
        self.text = text  # Regeneration cannot silently restore redacted text.
        self.show_source(text)
        self.generate_button.configure(text='다시 생성')
        self.save_button.state(['!disabled'])
        self.copy_button.state(['!disabled'])
        self.note.set(f'생성 완료 · {time.monotonic() - self.started:.1f}초 · 위 전송 본문과 대조하세요. 저장 전에는 파일로 보관되지 않습니다.')

    def save(self):
        if not self.data:
            return
        path = filedialog.asksaveasfilename(parent=self.window, defaultextension='.png',
                    initialfile='업무 정리.png', filetypes=[('PNG 이미지', '*.png')])
        if path:
            try:
                Path(path).write_bytes(self.data)
                self.note.set('PNG 이미지를 저장했습니다.')
            except OSError as error:
                log_failure('work_image.save', error)
                messagebox.showerror('저장 실패', '이미지를 저장하지 못했습니다.', parent=self.window)

    def copy(self):
        if not self.data:
            return
        try:
            copy_image(self.data, owner=self.window.winfo_id())
            self.note.set('이미지를 복사했습니다. 문서나 메신저에 붙여넣으세요.')
        except Exception as error:
            log_failure('work_image.copy', error)
            messagebox.showerror('복사 실패', '이미지를 복사하지 못했습니다. PNG 저장을 이용해 주세요.', parent=self.window)

    def close(self):
        if self.after_id is not None:
            self.window.after_cancel(self.after_id)
            self.after_id = None
        self.window.destroy()
