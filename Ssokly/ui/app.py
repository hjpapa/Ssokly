import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from PIL import Image

from services.ai_service import analyze_document_task
from services.capture_service import capture_selected_region
from services.ocr_service import extract_text_from_image


class SsoklyApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()

        self.title("Ssokly")
        self.geometry("980x760")
        self.minsize(820, 640)

        self._build_styles()
        self._build_ui()

    def _build_styles(self) -> None:
        self.configure(bg="#f7f7f5")
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background="#f7f7f5")
        style.configure("TLabel", background="#f7f7f5", foreground="#222222")
        style.configure("Title.TLabel", font=("Segoe UI", 26, "bold"))
        style.configure("Subtitle.TLabel", font=("Segoe UI", 12))
        style.configure("Desc.TLabel", font=("Malgun Gothic", 12))
        style.configure("TButton", font=("Malgun Gothic", 10), padding=(12, 7))
        style.configure("Primary.TButton", font=("Malgun Gothic", 11, "bold"), padding=(14, 8))

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=20)
        root.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(root)
        header.pack(fill=tk.X)

        ttk.Label(header, text="Ssokly", style="Title.TLabel").pack(anchor=tk.W)
        ttk.Label(header, text="Pull tasks out of documents.", style="Subtitle.TLabel").pack(anchor=tk.W, pady=(4, 0))
        ttk.Label(header, text="드래그한 공문을 해야 할 일로 바꿔요.", style="Desc.TLabel").pack(anchor=tk.W, pady=(6, 0))

        button_bar = ttk.Frame(root)
        button_bar.pack(fill=tk.X, pady=(18, 12))

        self.capture_button = ttk.Button(button_bar, text="영역 드래그 캡처하기", command=self.capture_area)
        self.capture_button.pack(side=tk.LEFT, padx=(0, 8))

        self.load_button = ttk.Button(button_bar, text="이미지 파일 불러오기", command=self.load_image_file)
        self.load_button.pack(side=tk.LEFT, padx=(0, 8))

        self.input_button = ttk.Button(button_bar, text="텍스트 직접 입력하기", command=self.focus_ocr_text)
        self.input_button.pack(side=tk.LEFT)

        self.status_var = tk.StringVar(value="준비되었습니다.")
        ttk.Label(root, textvariable=self.status_var).pack(anchor=tk.W, pady=(0, 8))

        panes = ttk.PanedWindow(root, orient=tk.VERTICAL)
        panes.pack(fill=tk.BOTH, expand=True)

        ocr_frame = ttk.Frame(panes)
        result_frame = ttk.Frame(panes)
        panes.add(ocr_frame, weight=1)
        panes.add(result_frame, weight=1)

        ttk.Label(ocr_frame, text="OCR 추출 텍스트", font=("Malgun Gothic", 11, "bold")).pack(anchor=tk.W)
        self.ocr_text = scrolledtext.ScrolledText(
            ocr_frame,
            wrap=tk.WORD,
            height=12,
            font=("Malgun Gothic", 10),
            undo=True,
        )
        self.ocr_text.pack(fill=tk.BOTH, expand=True, pady=(6, 12))

        action_bar = ttk.Frame(root)
        action_bar.pack(fill=tk.X, pady=(12, 0))

        self.analyze_button = ttk.Button(
            action_bar,
            text="업무 분석하기",
            style="Primary.TButton",
            command=self.analyze_text,
        )
        self.analyze_button.pack(side=tk.LEFT, padx=(0, 8))

        self.copy_button = ttk.Button(action_bar, text="결과 복사", command=self.copy_result)
        self.copy_button.pack(side=tk.LEFT)

        ttk.Label(result_frame, text="분석 결과", font=("Malgun Gothic", 11, "bold")).pack(anchor=tk.W)
        self.result_text = scrolledtext.ScrolledText(
            result_frame,
            wrap=tk.WORD,
            height=12,
            font=("Malgun Gothic", 10),
        )
        self.result_text.pack(fill=tk.BOTH, expand=True, pady=(6, 0))

    def capture_area(self) -> None:
        try:
            self.status_var.set("드래그할 영역을 선택해 주세요. 취소하려면 Esc를 누르세요.")
            self.withdraw()
            self.after(200, self._capture_after_hide)
        except Exception as exc:
            self.deiconify()
            messagebox.showerror("캡처 오류", f"화면 캡처를 시작하지 못했습니다.\n\n{exc}")
            self.status_var.set("캡처를 시작하지 못했습니다.")

    def _capture_after_hide(self) -> None:
        try:
            image = capture_selected_region(self)
        except Exception as exc:
            self.deiconify()
            messagebox.showerror("캡처 오류", f"선택 영역을 캡처하지 못했습니다.\n\n{exc}")
            self.status_var.set("캡처에 실패했습니다.")
            return

        self.deiconify()
        self.lift()

        if image is None:
            messagebox.showinfo("캡처 취소", "캡처가 취소되었거나 선택한 영역이 너무 작습니다.")
            self.status_var.set("캡처가 취소되었습니다.")
            return

        self._run_ocr(image)

    def load_image_file(self) -> None:
        path = filedialog.askopenfilename(
            title="이미지 파일 선택",
            filetypes=[
                ("이미지 파일", "*.png;*.jpg;*.jpeg"),
                ("PNG", "*.png"),
                ("JPG", "*.jpg;*.jpeg"),
            ],
        )
        if not path:
            return

        try:
            image = Image.open(path)
            self._run_ocr(image)
        except Exception as exc:
            messagebox.showerror(
                "이미지 불러오기 오류",
                f"이미지 파일을 불러오지 못했습니다.\n\n파일 형식이나 경로를 확인해 주세요.\n오류 내용: {exc}",
            )
            self.status_var.set("이미지를 불러오지 못했습니다.")

    def _run_ocr(self, image: Image.Image) -> None:
        self.status_var.set("OCR로 텍스트를 추출하는 중입니다...")
        self._set_buttons_state(tk.DISABLED)

        def worker() -> None:
            try:
                text = extract_text_from_image(image)
            except Exception as exc:
                text = (
                    "OCR 처리 중 예상하지 못한 오류가 발생했습니다.\n\n"
                    f"오류 내용: {exc}"
                )
            self.after(0, lambda: self._finish_ocr(text))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_ocr(self, text: str) -> None:
        self.ocr_text.delete("1.0", tk.END)
        self.ocr_text.insert(tk.END, text)
        self._set_buttons_state(tk.NORMAL)
        if text.strip():
            self.status_var.set("OCR 텍스트 추출이 완료되었습니다.")
        else:
            self.status_var.set("OCR 결과가 비어 있습니다. 이미지 품질을 확인해 주세요.")
            messagebox.showinfo("OCR 결과 없음", "이미지에서 텍스트를 찾지 못했습니다. 더 선명한 이미지로 다시 시도해 주세요.")

    def focus_ocr_text(self) -> None:
        self.ocr_text.focus_set()
        self.status_var.set("OCR 텍스트 영역에 직접 입력하거나 붙여넣을 수 있습니다.")

    def analyze_text(self) -> None:
        text = self.ocr_text.get("1.0", tk.END).strip()
        if not text:
            message = analyze_document_task(text)
            self.result_text.delete("1.0", tk.END)
            self.result_text.insert(tk.END, message)
            self.status_var.set("분석할 텍스트가 없습니다.")
            messagebox.showinfo("분석할 텍스트 없음", message)
            self.ocr_text.focus_set()
            return

        self.status_var.set("분석 중입니다...")
        self.result_text.delete("1.0", tk.END)
        self.result_text.insert(tk.END, "분석 중입니다...")
        self._set_buttons_state(tk.DISABLED)

        def worker() -> None:
            try:
                result = analyze_document_task(text)
            except Exception as exc:
                result = (
                    "AI 업무 분석 중 예상하지 못한 오류가 발생했습니다.\n\n"
                    f"오류 내용: {exc}"
                )
            self.after(0, lambda: self._finish_analysis(result))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_analysis(self, result: str) -> None:
        self.result_text.delete("1.0", tk.END)
        self.result_text.insert(tk.END, result)
        self._set_buttons_state(tk.NORMAL)
        self.status_var.set("업무 분석이 완료되었습니다.")

    def copy_result(self) -> None:
        result = self.result_text.get("1.0", tk.END).strip()
        if not result:
            messagebox.showinfo("복사할 결과 없음", "먼저 업무 분석 결과를 생성해 주세요.")
            return

        try:
            self.clipboard_clear()
            self.clipboard_append(result)
            self.update()
            messagebox.showinfo("복사 완료", "분석 결과를 클립보드에 복사했습니다.")
            self.status_var.set("분석 결과를 클립보드에 복사했습니다.")
        except Exception as exc:
            messagebox.showerror("복사 오류", f"클립보드에 복사하지 못했습니다.\n\n오류 내용: {exc}")
            self.status_var.set("클립보드 복사에 실패했습니다.")

    def _set_buttons_state(self, state: str) -> None:
        for button in (
            self.capture_button,
            self.load_button,
            self.input_button,
            self.analyze_button,
            self.copy_button,
        ):
            button.configure(state=state)
