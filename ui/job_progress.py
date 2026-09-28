"""Non-modal activity indicator; elapsed time is not a progress estimate."""
import time
import tkinter as tk
from tkinter import ttk


class JobProgress(ttk.Frame):
    def __init__(self, parent, cancel):
        super().__init__(parent, padding=(12, 6))
        self.spinner = tk.Canvas(self, width=28, height=28, highlightthickness=0,
                                 background='#f0fdfa')
        self.spinner.pack(side='left', padx=(0, 10))
        self.arc = self.spinner.create_arc(5, 5, 23, 23, start=0, extent=265,
                                           style='arc', outline='#177568', width=3)
        self.text = tk.StringVar()
        self.label = ttk.Label(self, textvariable=self.text, wraplength=480)
        self.label.pack(side='left', fill='x', expand=True)
        self.cancel_button = ttk.Button(self, text='처리 취소', command=cancel)
        self.cancel_button.pack(side='right', padx=(10, 0))
        self.bind('<Configure>', self._resize)

    def _resize(self, event):
        self.label.configure(wraplength=max(180, event.width - 190))

    def refresh(self, jobs, now=None):
        active = [job for job in jobs.values() if not job['cancel'].is_set()]
        if not jobs:
            self.pack_forget()
            return
        now = time.monotonic() if now is None else now
        visible = active or list(jobs.values())
        elapsed = max(0, int(now - min(job.get('started', now) for job in visible)))
        kinds = {job['kind'] for job in visible}
        label = {'ocr': '이미지 읽는 중', 'ai': 'AI 정리 중',
                 'file': '파일 읽는 중'}.get(next(iter(kinds)), '처리 중')
        if len(active) > 1:
            label = f'{len(active)}개 작업 처리 중'
        clock = f'{elapsed // 60}분 {elapsed % 60:02d}초' if elapsed >= 60 else f'{elapsed}초'
        hint = ('오래 걸리고 있습니다. 기다리거나 취소하세요.' if elapsed >= 30
                else '원본 확인·편집을 계속할 수 있습니다.')
        if not active:
            label = '취소됨 · 전송 종료 대기'
            hint = '결과는 적용하지 않습니다. 종료 후 재실행하세요.'
        self.cancel_button.configure(state='normal' if active else 'disabled')
        self.text.set(f'{label} · {clock}\n{hint}')
        self.spinner.itemconfigure(self.arc, start=-(int(now * 240) % 360))
        if not self.winfo_manager():
            self.pack(fill='x', before=self.master.main_split)
