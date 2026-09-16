"""Explicit transmission-copy selection with local text/pixel redaction."""
from pathlib import Path
import math
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

from services.transfer_policy import (
    ScopeExpansionRequired, TransferPolicy, TransferSnapshot, make_file_snapshot, make_image_snapshot,
    make_text_snapshot, restore_image_snapshot, risk_candidates,
)


def choose_transfer(parent, *, kind: str, text: str = "", image=None,
                    path: Path | str | None = None,
                    previous: TransferPolicy | None = None,
                    title: str = "AI 전송 대상 확인") -> TransferSnapshot | None:
    """Return only an explicitly approved immutable copy, or None on cancel.

    For files without local text extraction, masking uses user-provided text or
    a selected page image; it never pretends that the original file was masked.
    """
    dialog = TransferDialog(parent, kind=kind, text=text, image=image, path=path,
                            previous=previous, title=title)
    parent.wait_window(dialog.window)
    return dialog.result


class TransferDialog:
    def __init__(self, parent, *, kind: str, text: str = "", image=None,
                 path=None, previous=None, title="AI 전송 대상 확인"):
        self.parent, self.kind, self.original_text = parent, kind, text
        self.path, self.previous = Path(path) if path is not None else None, previous
        self.original_image = image
        self.result = None
        self.rectangles, self.excluded = [], []
        self.masking = False
        self._start = None
        self._serial = 0
        self.window = tk.Toplevel(parent)
        self.window.title(title)
        self.window.geometry("860x700")
        self.window.minsize(580, 460)
        self.window.transient(parent)
        self.window.protocol("WM_DELETE_WINDOW", self.cancel)
        self.window.bind("<Escape>", lambda _event: self.cancel())
        body = ttk.Frame(self.window, padding=14)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="선택한 사본만 OpenAI API에 전송됩니다. 비용이 발생할 수 있습니다.",
                  wraplength=810).pack(anchor="w")
        ttk.Label(body, text="원본은 PC에 보존됩니다. 가림은 학교의 외부 AI 이용 기준을 대신 판단하지 않습니다.",
                  wraplength=810).pack(anchor="w", pady=(2, 8))
        if previous is not None and previous.redacted:
            ttk.Label(body, text="기존 가림 이후 전송 범위를 다시 선택합니다. 추가·복원한 내용을 확인하세요.",
                      foreground="#ad3b22", wraplength=810).pack(anchor="w", pady=(0, 8))
        candidates = risk_candidates(text) if kind == "text" else []
        if candidates:
            categories = ", ".join(sorted({candidate.category for candidate in candidates}))
            caution = f"로컬 확인 후보: {categories}. 전체 탐지가 아니며 필요한 부분은 직접 가려 주세요."
        elif kind == "image":
            caution = "이미지의 개인정보는 전송 전 자동 확인하지 못합니다. 필요한 부분은 직접 가려 주세요."
        elif kind == "file":
            caution = "이 파일의 내용을 전송 전에 자동 검사하지 못합니다. 가림은 추출 텍스트 사본 또는 직접 선택한 페이지 이미지로만 가능합니다."
        else:
            caution = "문맥·패턴으로 일부 후보만 확인합니다. 후보가 없어도 개인정보가 없다는 뜻은 아닙니다."
        ttk.Label(body, text=caution, wraplength=810, foreground="#7a5426").pack(anchor="w", pady=(0, 8))
        controls = ttk.Frame(body)
        controls.pack(fill="x", pady=(0, 8))
        self.direct_button = ttk.Button(controls, text="확인 후 그대로 분석" if candidates else "AI로 분석", command=self.accept_direct)
        self.direct_button.pack(side="left")
        if previous is not None and previous.redacted:
            self.direct_button.configure(text="원본으로 전송 범위 확대…")
        self.mask_button = ttk.Button(controls, text="가리고 분석", command=self.start_masking)
        self.mask_button.pack(side="left", padx=8)
        ttk.Button(controls, text="취소", command=self.cancel).pack(side="left")
        self.note = tk.StringVar(value="아래에서 실제 전송 대상을 확인하세요.")
        ttk.Label(body, textvariable=self.note, wraplength=810).pack(anchor="w", pady=(0, 6))
        self.preview = ttk.Frame(body)
        self.preview.pack(fill="both", expand=True)
        self.tools = ttk.Frame(body)
        self.tools.pack(fill="x", pady=(8, 0))
        self.finish_button = ttk.Button(body, text="가린 내용으로 분석", command=self.accept_redacted)
        self.finish_button.pack(anchor="e", pady=(8, 0))
        self.finish_button.state(["disabled"])
        if kind == "text":
            self._show_text(text, editable=False)
        elif kind == "image":
            if image is None:
                self.cancel()
                raise ValueError("전송할 이미지가 없습니다.")
            # Same normalized, metadata-free pixels will be used for drawing.
            self.original_image = make_image_snapshot(image).as_image()
            if previous is not None and previous.image_rectangles:
                try:
                    restore_image_snapshot(self.original_image, previous)
                    self.rectangles = list(previous.image_rectangles)
                    self.note.set("이전에 승인한 가림 범위를 복원했습니다. '가리고 분석'에서 그대로 확인하거나 추가로 가릴 수 있습니다.")
                except ValueError:
                    self.note.set("원본 이미지가 바뀌어 이전 가림 위치를 재사용하지 않았습니다. 필요한 영역을 다시 선택해 주세요.")
            self._show_image()
        elif kind == "file":
            ttk.Label(self.preview, text=f"선택한 파일: {self.path.name if self.path else '(없음)'}\n\n'AI로 분석'은 선택한 원본 파일 전체를 전송합니다.\n가리려면 '가리고 분석'에서 필요한 텍스트만 붙여넣거나 페이지 이미지를 선택하세요.",
                      wraplength=790).pack(anchor="nw", pady=12)
        else:
            self.cancel()
            raise ValueError("지원하지 않는 전송 종류입니다.")
        self.window.grab_set()

    def _clear_preview(self):
        for child in self.preview.winfo_children():
            child.destroy()
        for child in self.tools.winfo_children():
            child.destroy()

    def _show_text(self, text, *, editable):
        self._clear_preview()
        self.editor = tk.Text(self.preview, wrap="word", undo=True, font=("맑은 고딕", 10))
        scrollbar = ttk.Scrollbar(self.preview, orient="vertical", command=self.editor.yview)
        self.editor.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.editor.pack(side="left", fill="both", expand=True)
        self.editor.insert("1.0", text)
        self.editor.configure(state="normal" if editable else "disabled")
        if editable:
            ttk.Button(self.tools, text="선택한 텍스트 가리기", command=self.mask_selection).pack(side="left")
            ttk.Label(self.tools, text="가릴 내용을 선택하거나 전송용 사본을 직접 수정하세요.").pack(side="left", padx=8)
            if self.kind == "file":
                ttk.Button(self.tools, text="페이지 이미지로 전환", command=self.select_page_image).pack(side="right")

    def _show_image(self):
        self._clear_preview()
        self.canvas = tk.Canvas(self.preview, background="#e4e7e6", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _event: self._draw_image())
        self.canvas.bind("<ButtonPress-1>", self.begin_rectangle)
        self.canvas.bind("<B1-Motion>", self.drag_rectangle)
        self.canvas.bind("<ButtonRelease-1>", self.end_rectangle)
        if self.masking:
            ttk.Button(self.tools, text="가림 초기화", command=self.reset_rectangles).pack(side="left")
            ttk.Label(self.tools, text="마우스로 사각형을 그리세요. 검은 영역은 실제 전송 픽셀에 합성됩니다.").pack(side="left", padx=8)
        self._draw_image()

    def _draw_image(self):
        if not hasattr(self, "canvas") or not self.canvas.winfo_exists():
            return
        snapshot = make_image_snapshot(self.original_image, rectangles=self.rectangles)
        self.safe_image = snapshot.as_image()
        available = (max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height()))
        width, height = self.safe_image.size
        self.scale = min(available[0] / width, available[1] / height, 1.0)
        size = (max(1, round(width * self.scale)), max(1, round(height * self.scale)))
        self.offset = ((available[0] - size[0]) // 2, (available[1] - size[1]) // 2)
        preview = self.safe_image.resize(size, Image.Resampling.LANCZOS)
        self.photo = ImageTk.PhotoImage(preview, master=self.window)
        self.canvas.delete("all")
        self.canvas.create_image(*self.offset, anchor="nw", image=self.photo)

    def start_masking(self):
        self.masking = True
        self.direct_button.state(["disabled"])
        self.mask_button.state(["disabled"])
        self.finish_button.state(["!disabled"])
        self.note.set("미리 본 전송용 사본만 보냅니다. 원본 파일·숨은 텍스트는 함께 보내지 않습니다.")
        if self.kind in {"text", "file"}:
            self._show_text(self.original_text if self.kind == "text" else "", editable=True)
            if self.kind == "file":
                self.note.set("필요한 텍스트만 붙여 넣어 가려 주세요. 또는 페이지 이미지를 선택하세요. 원본 파일은 전송하지 않습니다.")
        else:
            self._show_image()

    def mask_selection(self):
        try:
            start, end = self.editor.index("sel.first"), self.editor.index("sel.last")
            secret = self.editor.get(start, end)
        except tk.TclError:
            self.note.set("가릴 텍스트를 먼저 선택해 주세요.")
            return
        self.excluded.append(secret)
        self._serial += 1
        self.editor.delete(start, end)
        self.editor.insert(start, f"[가림{self._serial}]")

    def select_page_image(self):
        path = filedialog.askopenfilename(parent=self.window, title="가릴 페이지 이미지 선택",
                                         filetypes=[("이미지", "*.png *.jpg *.jpeg *.bmp *.webp")])
        if not path:
            return
        try:
            with Image.open(path) as image:
                image.load()
                self.original_image = make_image_snapshot(image).as_image()
            self.rectangles = []
            self._show_image()
            self.note.set("선택한 페이지 이미지만 전송됩니다. 필요한 부분을 드래그로 가려 주세요. 원본 문서는 첨부하지 않습니다.")
        except Exception:
            messagebox.showerror("이미지 열기 실패", "페이지 이미지를 열지 못했습니다. 원본 파일로 대체해 보내지 않습니다.", parent=self.window)

    def _point(self, event):
        x = (event.x - self.offset[0]) / self.scale
        y = (event.y - self.offset[1]) / self.scale
        width, height = self.original_image.size
        return max(0.0, min(width, x)), max(0.0, min(height, y))

    def begin_rectangle(self, event):
        if self.masking:
            self._start = self._point(event)

    def drag_rectangle(self, event):
        if not self.masking or self._start is None:
            return
        end = self._point(event)
        self.canvas.delete("selection")
        self.canvas.create_rectangle(self.offset[0] + self._start[0] * self.scale,
                                     self.offset[1] + self._start[1] * self.scale,
                                     self.offset[0] + end[0] * self.scale,
                                     self.offset[1] + end[1] * self.scale,
                                     fill="black", outline="#b93827", tags="selection")

    def end_rectangle(self, event):
        if not self.masking or self._start is None:
            return
        end, start = self._point(event), self._start
        self._start = None
        rect = (math.floor(min(start[0], end[0])), math.floor(min(start[1], end[1])),
                math.ceil(max(start[0], end[0])), math.ceil(max(start[1], end[1])))
        if rect[0] < rect[2] and rect[1] < rect[3]:
            self.rectangles.append(rect)
        self._draw_image()

    def reset_rectangles(self):
        self.rectangles = []
        self._draw_image()

    def accept_direct(self):
        if self.previous is not None and self.previous.redacted:
            if not messagebox.askyesno("전송 범위 확대 확인",
                                       "이 자료의 가림을 해제하고 현재 원본 전체를 전송합니다. 가렸던 정보와 새로 추가한 내용이 포함될 수 있습니다.\n\n이 범위로 새로 승인하시겠습니까?",
                                       parent=self.window, default="no"):
                return
        try:
            if self.kind == "text":
                self.result = make_text_snapshot(self.original_text)
            elif self.kind == "image":
                self.result = make_image_snapshot(self.original_image)
            else:
                self.result = make_file_snapshot(self.path)
        except Exception:
            messagebox.showerror("전송 사본 생성 실패", "전송용 사본을 만들지 못했습니다. 요청을 시작하지 않았습니다.", parent=self.window)
            return
        self.window.destroy()

    def accept_redacted(self):
        if not self.masking:
            return
        try:
            if self.original_image is not None and hasattr(self, "canvas") and self.canvas.winfo_exists():
                if not self.rectangles:
                    raise ValueError("가릴 이미지 영역을 먼저 선택해 주세요.")
                selected = make_image_snapshot(self.original_image, rectangles=self.rectangles)
            else:
                safe = self.editor.get("1.0", "end-1c")
                if not safe.strip():
                    raise ValueError("전송할 텍스트 사본을 입력해 주세요.")
                if self.kind == "text" and safe == self.original_text:
                    raise ValueError("가릴 내용을 선택하거나 사본을 수정해 주세요.")
                selected = make_text_snapshot(safe, original_text=self.original_text,
                                              excluded_strings=self.excluded)
                if self.kind == "file" and not selected.policy.redacted:
                    # User-selected text instead of the file is still a
                    # restricted scope even when no characters were removed.
                    from dataclasses import replace
                    selected = replace(selected, policy=replace(selected.policy, redacted=True))
            if self._expands_previous(selected):
                if not messagebox.askyesno("이전 가림 범위 변경 확인",
                                           "새 사본에 이전에 가렸거나 승인 사본에 없던 내용이 포함될 수 있습니다. 일부를 새로 가렸어도 이전 가림 해제는 별도 범위 확대입니다.\n\n현재 미리보기의 범위로 새로 승인하시겠습니까?",
                                           parent=self.window, default="no"):
                    return
            self.result = selected
        except ValueError as exc:
            messagebox.showerror("가림 확인", str(exc), parent=self.window)
            return
        except Exception:
            messagebox.showerror("가림 실패", "가린 사본을 만들지 못했습니다. 원본으로 대체해 보내지 않습니다.", parent=self.window)
            return
        self.window.destroy()

    def _expands_previous(self, selected):
        previous = self.previous
        if previous is None or not previous.redacted:
            return False
        if selected.kind == "text":
            try:
                previous.guard_text(selected.text)
                return False
            except ScopeExpansionRequired:
                return True
        if selected.kind == "image":
            if (not previous.image_source_digest or not previous.image_rectangles
                    or previous.image_source_digest != selected.policy.image_source_digest):
                return True
            # Compare actual transmitted pixels, not canvas overlays. Several
            # new rectangles may jointly preserve an old masked rectangle.
            masked = selected.as_image()
            for rect in previous.image_rectangles:
                extrema = masked.crop(rect).getextrema()
                if any(low != 0 or high != 0 for low, high in extrema):
                    return True
            return False
        return True

    def cancel(self):
        self.result = None
        self.window.destroy()
