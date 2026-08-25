import os
import queue
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Any, Callable, Optional
from uuid import uuid4

from PIL import Image, ImageTk

from services.ai_service import analyze_document_task
from services.capture_service import capture_selected_region
from services.capture_store import CaptureRecord, CaptureStore
from services.document_service import (
    attachment_kind,
    mime_type_for,
    read_hwpx_file,
    read_text_file,
)
from services.ocr_service import extract_text_from_file, extract_text_from_image
from services.task_store import TaskRecord, TaskStore
from services.workflow_service import extract_section


PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_ICON_PATH = PROJECT_ROOT / "assets" / "ssokly-app-icon.png"
MINT_CANVAS = "#eef9f6"
MINT_SURFACE = "#ffffff"
MINT_PANEL = "#ddf3ed"
MINT_TEXT_AREA = "#f7fcfa"
MINT_TAB = "#d3eee7"
TEAL_DARK = "#174f50"
TEAL_DEEP = "#103d40"
TEAL_INK = "#153d3d"
TEAL_PRIMARY = "#168b7c"
TEAL_PRIMARY_ACTIVE = "#117568"
TEAL_MUTED = "#6d9692"
TEAL_DISABLED = "#a9cbc5"
CORAL = "#f47f73"
CORAL_ACTIVE = "#df6b61"
CORAL_SOFT = "#fbd6d1"
SIDEBAR_TEXT = "#effaf7"
AUTOSAVE_DELAY_MS = 700
TASK_SEARCH_DELAY_MS = 200
OPERATION_PROGRESS_INTERVAL_MS = 250
DEFAULT_OUTPUT_MODE = "통합 실행안"
OPERATION_LABELS = {
    "ocr": "이미지 OCR",
    "document": "문서 읽기",
    "analysis": "업무 분석",
}


class SsoklyApp(tk.Tk):
    def __init__(
        self,
        task_store: Optional[TaskStore] = None,
        capture_store: Optional[CaptureStore] = None,
    ) -> None:
        super().__init__()

        self.title("Ssokly - 공문 실행 정리")
        self.geometry("1480x920")
        self.minsize(1180, 760)
        self._set_window_icon()

        self.capture_store = capture_store or CaptureStore()
        self.task_store = task_store or TaskStore()
        self.capture_records: list[CaptureRecord] = []
        self.task_records: list[TaskRecord] = []
        self.preview_photo: Optional[ImageTk.PhotoImage] = None
        self.source_preview_photo: Optional[ImageTk.PhotoImage] = None

        self.current_task_id: Optional[str] = None
        self.current_task_updated_at: Optional[datetime] = None
        self.current_task_status = "open"
        self.current_source_kind = "manual"
        self.current_source_name = ""
        self.current_source_path: Optional[Path] = None
        self.current_capture_path: Optional[Path] = None
        self.current_output_mode = DEFAULT_OUTPUT_MODE
        self.current_context_id = uuid4().hex
        self.current_dirty = False
        self.title_is_auto = True
        self._autosave_failed = False
        self._suspend_change_tracking = False
        self._autosave_after_id: Optional[str] = None
        self._task_search_after_id: Optional[str] = None
        self._source_revision = 0
        self._result_revision = 0
        self._active_operation_id: Optional[str] = None
        self._active_operation_context: Optional[str] = None
        self._active_operation_kind: Optional[str] = None
        self._active_operation_revisions: Optional[tuple[int, int]] = None
        self._operation_started_at: Optional[float] = None
        self._operation_progress_after_id: Optional[str] = None
        self._closing = False
        self._worker_results = queue.Queue()
        self._worker_poll_after_id: Optional[str] = None
        self._hold_active_operation_results = False
        self._held_worker_results: list[tuple[Any, ...]] = []

        self.recent_window: Optional[tk.Toplevel] = None
        self.recent_list: Optional[tk.Listbox] = None
        self.preview_label: Optional[tk.Label] = None
        self.reopen_button: Optional[tk.Button] = None
        self.delete_capture_button: Optional[tk.Button] = None
        self.open_capture_folder_button: Optional[tk.Button] = None

        self._build_styles()
        self._build_ui()
        self._bind_change_tracking()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._refresh_task_library()
        self._refresh_recent_captures()
        self._worker_poll_after_id = self.after(50, self._drain_worker_results)
        self._render_workspace_state()

    def _set_window_icon(self) -> None:
        if not APP_ICON_PATH.exists():
            return

        try:
            self.window_icon = tk.PhotoImage(file=str(APP_ICON_PATH))
            self.iconphoto(True, self.window_icon)
        except tk.TclError:
            self.window_icon = None

    def _build_styles(self) -> None:
        self.configure(bg=MINT_CANVAS)
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("App.TFrame", background=MINT_CANVAS)
        style.configure("Surface.TFrame", background=MINT_SURFACE)
        style.configure("Toolbar.TFrame", background=MINT_SURFACE)
        style.configure("TLabel", background=MINT_CANVAS, foreground=TEAL_INK, font=("Malgun Gothic", 10))
        style.configure("Surface.TLabel", background=MINT_SURFACE, foreground=TEAL_INK, font=("Malgun Gothic", 10))
        style.configure("Title.TLabel", background=MINT_CANVAS, foreground=TEAL_DEEP, font=("Malgun Gothic", 21, "bold"))
        style.configure("Section.TLabel", background=MINT_SURFACE, foreground=TEAL_DEEP, font=("Malgun Gothic", 12, "bold"))
        style.configure("Muted.TLabel", background=MINT_SURFACE, foreground=TEAL_MUTED, font=("Malgun Gothic", 9))
        style.configure("Status.TLabel", background=MINT_CANVAS, foreground=TEAL_PRIMARY_ACTIVE, font=("Malgun Gothic", 9))
        style.configure(
            "Processing.Horizontal.TProgressbar",
            background=CORAL,
            troughcolor=MINT_PANEL,
            bordercolor=MINT_PANEL,
            lightcolor=CORAL,
            darkcolor=CORAL_ACTIVE,
        )
        style.configure("TButton", font=("Malgun Gothic", 10), padding=(12, 8))
        style.configure("Primary.TButton", background=TEAL_PRIMARY, foreground="#ffffff", font=("Malgun Gothic", 10, "bold"))
        style.map("Primary.TButton", background=[("active", TEAL_PRIMARY_ACTIVE), ("disabled", TEAL_DISABLED)])
        style.configure("Secondary.TButton", background=CORAL_SOFT, foreground=TEAL_DEEP)
        style.map("Secondary.TButton", background=[("active", CORAL), ("disabled", "#f4dedb")])
        style.configure(
            "Task.Treeview",
            background=TEAL_DEEP,
            fieldbackground=TEAL_DEEP,
            foreground=SIDEBAR_TEXT,
            borderwidth=0,
            rowheight=34,
            font=("Malgun Gothic", 9),
        )
        style.map(
            "Task.Treeview",
            background=[("selected", CORAL)],
            foreground=[("selected", "#ffffff")],
        )
        style.configure(
            "Task.Treeview.Heading",
            background=TEAL_DARK,
            foreground=CORAL_SOFT,
            relief=tk.FLAT,
            font=("Malgun Gothic", 8, "bold"),
        )
        style.map("Task.Treeview.Heading", background=[("active", TEAL_DARK)])
        style.configure("Notebook.TNotebook", background=MINT_CANVAS, borderwidth=0)
        style.configure(
            "Notebook.TNotebook.Tab",
            background=MINT_TAB,
            foreground=TEAL_MUTED,
            font=("Malgun Gothic", 10, "bold"),
            padding=(16, 9),
        )
        style.map(
            "Notebook.TNotebook.Tab",
            background=[("selected", MINT_SURFACE)],
            foreground=[("selected", TEAL_PRIMARY_ACTIVE)],
        )

    def _build_ui(self) -> None:
        root = ttk.Frame(self, style="App.TFrame")
        root.pack(fill=tk.BOTH, expand=True)

        self._build_sidebar(root)

        workspace = ttk.Frame(root, style="App.TFrame", padding=(24, 18, 24, 22))
        workspace.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._build_header(workspace)
        self._build_footer(workspace)

        self.notebook = ttk.Notebook(workspace, style="Notebook.TNotebook")
        self.notebook.pack(fill=tk.BOTH, expand=True, pady=(16, 0))

        self.source_tab = ttk.Frame(self.notebook, style="Surface.TFrame", padding=18)
        self.result_tab = ttk.Frame(self.notebook, style="Surface.TFrame", padding=18)
        self.notebook.add(self.source_tab, text="원문 검수")
        self.notebook.add(self.result_tab, text="업무 실행안")

        self._build_source_tab()
        self._build_result_tab()

    def _build_footer(self, workspace: ttk.Frame) -> None:
        self.creator_footer = tk.Frame(workspace, bg=MINT_CANVAS, height=30)
        self.creator_footer.pack(side=tk.BOTTOM, fill=tk.X, pady=(8, 0))
        self.creator_footer.pack_propagate(False)
        self.creator_label = tk.Label(
            self.creator_footer,
            text="Created by  Park jun ho",
            bg=MINT_CANVAS,
            fg=CORAL_ACTIVE,
            font=("Segoe UI", 9, "bold"),
        )
        self.creator_label.pack(side=tk.RIGHT, anchor=tk.E, pady=(6, 0))

    def _build_sidebar(self, root: ttk.Frame) -> None:
        sidebar = tk.Frame(root, bg=TEAL_DARK, width=340)
        sidebar.pack(side=tk.LEFT, fill=tk.Y)
        sidebar.pack_propagate(False)
        self.sidebar = sidebar

        brand = tk.Frame(sidebar, bg=TEAL_DARK)
        brand.pack(fill=tk.X, padx=18, pady=(22, 18))
        tk.Label(
            brand,
            text="SSOKLY",
            bg=TEAL_DARK,
            fg=SIDEBAR_TEXT,
            font=("Segoe UI", 18, "bold"),
        ).pack(anchor=tk.W)
        tk.Label(
            brand,
            text="DOCUMENT ACTION DESK",
            bg=TEAL_DARK,
            fg=CORAL_SOFT,
            font=("Segoe UI", 8, "bold"),
        ).pack(anchor=tk.W, pady=(4, 0))

        library_header = tk.Frame(sidebar, bg=TEAL_DARK)
        library_header.pack(fill=tk.X, padx=18, pady=(4, 8))
        tk.Label(
            library_header,
            text="업무 보관함",
            bg=TEAL_DARK,
            fg=SIDEBAR_TEXT,
            font=("Malgun Gothic", 11, "bold"),
        ).pack(side=tk.LEFT)
        self.task_count_var = tk.StringVar(value="0개")
        tk.Label(
            library_header,
            textvariable=self.task_count_var,
            bg=TEAL_DARK,
            fg=CORAL_SOFT,
            font=("Segoe UI", 9),
        ).pack(side=tk.RIGHT)

        tk.Label(
            sidebar,
            text="제목, 원문, 실행안 검색",
            bg=TEAL_DARK,
            fg=CORAL_SOFT,
            font=("Malgun Gothic", 8),
        ).pack(anchor=tk.W, padx=18, pady=(2, 5))

        self.task_search_var = tk.StringVar()
        self.task_search_entry = tk.Entry(
            sidebar,
            textvariable=self.task_search_var,
            bg=TEAL_DEEP,
            fg=SIDEBAR_TEXT,
            insertbackground="#ffffff",
            selectbackground=CORAL,
            borderwidth=0,
            highlightthickness=0,
            font=("Malgun Gothic", 9),
            relief=tk.FLAT,
        )
        self.task_search_entry.pack(fill=tk.X, padx=14, ipady=8)

        self.task_filter_var = tk.StringVar(value="open")
        filter_bar = tk.Frame(sidebar, bg=TEAL_DARK)
        filter_bar.pack(fill=tk.X, padx=14, pady=(10, 8))
        self.task_filter_buttons: dict[str, tk.Button] = {}
        for label, value in (("진행중", "open"), ("완료", "completed"), ("전체", "all")):
            button = tk.Button(
                filter_bar,
                text=label,
                command=lambda selected=value: self._set_task_filter(selected),
                bg=CORAL if value == "open" else TEAL_DEEP,
                fg="#ffffff" if value == "open" else SIDEBAR_TEXT,
                activebackground=CORAL_ACTIVE,
                activeforeground="#ffffff",
                relief=tk.FLAT,
                borderwidth=0,
                font=("Malgun Gothic", 8, "bold"),
                pady=6,
            )
            button.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4) if value != "all" else 0)
            self.task_filter_buttons[value] = button

        tree_frame = tk.Frame(sidebar, bg=TEAL_DARK)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=14)
        self.task_tree = ttk.Treeview(
            tree_frame,
            columns=("title", "updated"),
            show="headings",
            selectmode="browse",
            style="Task.Treeview",
        )
        self.task_tree.heading("title", text="업무")
        self.task_tree.heading("updated", text="수정")
        self.task_tree.column("title", width=205, minwidth=120, stretch=True, anchor=tk.W)
        self.task_tree.column("updated", width=86, minwidth=72, stretch=False, anchor=tk.CENTER)
        task_scrollbar = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.task_tree.yview)
        self.task_tree.configure(yscrollcommand=task_scrollbar.set)
        self.task_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        task_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.task_tree.bind("<Double-1>", lambda _event: self.open_selected_task())
        self.task_tree.bind("<Return>", lambda _event: self.open_selected_task())
        self.task_tree.bind("<<TreeviewSelect>>", self._on_task_selection_changed)

        task_actions = tk.Frame(sidebar, bg=TEAL_DARK)
        task_actions.pack(fill=tk.X, padx=14, pady=(10, 0))
        self.task_open_button = tk.Button(
            task_actions,
            text="열기",
            command=self.open_selected_task,
            bg=MINT_PANEL,
            fg=TEAL_DEEP,
            activebackground="#ffffff",
            relief=tk.FLAT,
            font=("Malgun Gothic", 9, "bold"),
            padx=8,
            pady=7,
        )
        self.task_open_button.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        self.task_status_button = tk.Button(
            task_actions,
            text="완료",
            command=self.toggle_selected_task_status,
            bg=TEAL_PRIMARY,
            fg="#ffffff",
            activebackground=TEAL_PRIMARY_ACTIVE,
            activeforeground="#ffffff",
            relief=tk.FLAT,
            font=("Malgun Gothic", 9),
            padx=8,
            pady=7,
        )
        self.task_status_button.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        self.task_delete_button = tk.Button(
            task_actions,
            text="삭제",
            command=self.delete_selected_task,
            bg=CORAL,
            fg="#ffffff",
            activebackground=CORAL_ACTIVE,
            activeforeground="#ffffff",
            relief=tk.FLAT,
            font=("Malgun Gothic", 9),
            padx=8,
            pady=7,
        )
        self.task_delete_button.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))

        tk.Label(
            sidebar,
            text="업무 원문과 실행안은 이 PC에만 저장됩니다.",
            bg=TEAL_DARK,
            fg=CORAL_SOFT,
            font=("Malgun Gothic", 8),
            justify=tk.LEFT,
        ).pack(side=tk.BOTTOM, anchor=tk.W, padx=18, pady=18)

    def _build_header(self, workspace: ttk.Frame) -> None:
        header = ttk.Frame(workspace, style="App.TFrame")
        header.pack(fill=tk.X)

        title_block = ttk.Frame(header, style="App.TFrame")
        title_block.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(title_block, text="공문 실행 정리", style="Title.TLabel").pack(anchor=tk.W)
        ttk.Label(
            title_block,
            text="문서를 읽고, 놓치지 않을 순서와 일정으로 바꿉니다.",
            style="TLabel",
        ).pack(anchor=tk.W, pady=(4, 0))

        button_bar = ttk.Frame(header, style="App.TFrame")
        button_bar.pack(side=tk.RIGHT, anchor=tk.NE)
        self.recent_count_var = tk.StringVar(value="최근 캡처 0/8")
        self.recent_button = ttk.Button(
            button_bar,
            textvariable=self.recent_count_var,
            command=self.show_recent_captures,
        )
        self.recent_button.pack(side=tk.LEFT, padx=(0, 8))
        self.capture_button = ttk.Button(
            button_bar,
            text="화면 캡처",
            style="Primary.TButton",
            command=self.capture_area,
        )
        self.capture_button.pack(side=tk.LEFT, padx=(0, 8))
        self.load_button = ttk.Button(
            button_bar,
            text="첨부 파일 열기",
            style="Secondary.TButton",
            command=self.load_attachment_file,
        )
        self.load_button.pack(side=tk.LEFT, padx=(0, 8))
        self.input_button = ttk.Button(
            button_bar,
            text="직접 입력",
            command=self.focus_ocr_text,
        )
        self.input_button.pack(side=tk.LEFT)

        status_row = ttk.Frame(workspace, style="App.TFrame")
        status_row.pack(fill=tk.X, pady=(10, 0))
        self.status_var = tk.StringVar(value="새 문서를 캡처하거나 첨부 파일을 열어 주세요.")
        ttk.Label(
            status_row,
            textvariable=self.status_var,
            style="Status.TLabel",
        ).pack(side=tk.LEFT, anchor=tk.W, fill=tk.X, expand=True)

        self.processing_frame = ttk.Frame(status_row, style="App.TFrame")
        self.processing_var = tk.StringVar(value="")
        ttk.Label(
            self.processing_frame,
            textvariable=self.processing_var,
            style="Status.TLabel",
        ).pack(side=tk.LEFT, padx=(8, 8))
        self.processing_progress = ttk.Progressbar(
            self.processing_frame,
            mode="indeterminate",
            length=110,
            style="Processing.Horizontal.TProgressbar",
        )
        self.processing_progress.pack(side=tk.LEFT)

        task_bar = tk.Frame(workspace, bg=MINT_PANEL, padx=12, pady=10)
        task_bar.pack(fill=tk.X, pady=(12, 0))
        tk.Label(
            task_bar,
            text="업무 제목",
            bg=MINT_PANEL,
            fg=TEAL_DEEP,
            font=("Malgun Gothic", 9, "bold"),
        ).pack(side=tk.LEFT, padx=(0, 8))
        self.task_title_var = tk.StringVar(value=self._fallback_task_title())
        self.task_title_entry = tk.Entry(
            task_bar,
            textvariable=self.task_title_var,
            bg="#ffffff",
            fg=TEAL_INK,
            insertbackground=TEAL_PRIMARY_ACTIVE,
            relief=tk.FLAT,
            borderwidth=0,
            font=("Malgun Gothic", 10),
        )
        self.task_title_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=7)

        self.save_state_var = tk.StringVar(value="저장 전")
        self.save_state_label = tk.Label(
            task_bar,
            textvariable=self.save_state_var,
            bg=MINT_PANEL,
            fg=TEAL_MUTED,
            font=("Malgun Gothic", 8),
        )
        self.save_state_label.pack(side=tk.LEFT, padx=10)
        self.save_task_button = ttk.Button(
            task_bar,
            text="업무로 저장",
            style="Primary.TButton",
            command=lambda: self.save_current_task(allow_during_operation=True),
        )
        self.save_task_button.pack(side=tk.LEFT, padx=(0, 6))
        self.current_status_button = ttk.Button(
            task_bar,
            text="완료로 표시",
            command=self.toggle_current_task_status,
        )
        self.current_status_button.pack(side=tk.LEFT, padx=(0, 6))
        self.reread_source_button = ttk.Button(
            task_bar,
            text="원문 다시 읽기",
            command=self.reread_current_source,
        )
        self.reread_source_button.pack(side=tk.LEFT, padx=(0, 6))
        self.open_source_button = ttk.Button(
            task_bar,
            text="원본 보기",
            command=self.open_current_source,
        )
        self.open_source_button.pack(side=tk.LEFT)

    def _build_source_tab(self) -> None:
        ttk.Label(self.source_tab, text="인식된 원문", style="Section.TLabel").pack(anchor=tk.W)
        ttk.Label(
            self.source_tab,
            text="날짜, 첨부파일명, 제출 방법을 원문과 대조한 뒤 필요한 양식을 선택하세요.",
            style="Muted.TLabel",
        ).pack(anchor=tk.W, pady=(4, 10))

        template_panel = tk.Frame(self.source_tab, bg=MINT_PANEL, padx=12, pady=10)
        template_panel.pack(fill=tk.X, pady=(0, 12))
        tk.Label(
            template_panel,
            text="원문 검수 후 만들기",
            bg=MINT_PANEL,
            fg=TEAL_DEEP,
            font=("Malgun Gothic", 10, "bold"),
        ).pack(side=tk.LEFT, padx=(0, 12))

        self.template_buttons: list[tk.Button] = []
        for label, output_mode in (
            ("일정 확인표", "일정 확인표"),
            ("업무 프로세스", "업무 프로세스"),
            ("활용 안내 양식", "활용 안내 양식"),
            ("통합 실행안", "통합 실행안"),
        ):
            button = tk.Button(
                template_panel,
                text=label,
                command=lambda mode=output_mode: self.analyze_text(mode),
                bg="#ffffff" if output_mode != "통합 실행안" else CORAL,
                fg=TEAL_DEEP if output_mode != "통합 실행안" else "#ffffff",
                activebackground=MINT_TAB if output_mode != "통합 실행안" else CORAL_ACTIVE,
                activeforeground=TEAL_DEEP if output_mode != "통합 실행안" else "#ffffff",
                relief=tk.FLAT,
                font=("Malgun Gothic", 9, "bold"),
                padx=12,
                pady=7,
            )
            button.pack(side=tk.LEFT, padx=(0, 7))
            self.template_buttons.append(button)

        self.ocr_text = scrolledtext.ScrolledText(
            self.source_tab,
            wrap=tk.WORD,
            font=("Malgun Gothic", 10),
            undo=True,
            relief=tk.FLAT,
            borderwidth=0,
            padx=12,
            pady=12,
            background=MINT_TEXT_AREA,
            foreground=TEAL_INK,
            insertbackground=TEAL_PRIMARY_ACTIVE,
        )
        self.ocr_text.pack(fill=tk.BOTH, expand=True)

        footer = ttk.Frame(self.source_tab, style="Surface.TFrame")
        footer.pack(fill=tk.X, pady=(14, 0))
        ttk.Label(
            footer,
            text="위 버튼을 누르면 검수한 원문을 기준으로 선택한 양식을 만듭니다.",
            style="Muted.TLabel",
        ).pack(side=tk.LEFT)

    def _build_result_tab(self) -> None:
        header = ttk.Frame(self.result_tab, style="Surface.TFrame")
        header.pack(fill=tk.X)
        title_block = ttk.Frame(header, style="Surface.TFrame")
        title_block.pack(side=tk.LEFT)
        ttk.Label(title_block, text="업무 실행안", style="Section.TLabel").pack(anchor=tk.W)
        ttk.Label(
            title_block,
            text="필요한 부분만 바로 복사해 일정 등록과 전달 업무에 활용하세요.",
            style="Muted.TLabel",
        ).pack(anchor=tk.W, pady=(4, 0))

        action_bar = ttk.Frame(header, style="Toolbar.TFrame")
        action_bar.pack(side=tk.RIGHT)
        self.schedule_button = ttk.Button(
            action_bar,
            text="일정 복사",
            command=lambda: self.copy_result_section("schedule", "일정 메모"),
        )
        self.schedule_button.pack(side=tk.LEFT, padx=(0, 6))
        self.checklist_button = ttk.Button(
            action_bar,
            text="체크리스트 복사",
            command=lambda: self.copy_result_section("checklist", "체크리스트"),
        )
        self.checklist_button.pack(side=tk.LEFT, padx=(0, 6))
        self.message_button = ttk.Button(
            action_bar,
            text="전달문 복사",
            command=lambda: self.copy_result_section("message", "전달 문구"),
        )
        self.message_button.pack(side=tk.LEFT, padx=(0, 6))
        self.follow_up_button = ttk.Button(
            action_bar,
            text="첨부 후속 복사",
            command=lambda: self.copy_result_section("follow_up", "첨부파일별 후속 실행"),
        )
        self.follow_up_button.pack(side=tk.LEFT, padx=(0, 6))
        self.copy_button = ttk.Button(
            action_bar,
            text="전체 복사",
            style="Secondary.TButton",
            command=self.copy_result,
        )
        self.copy_button.pack(side=tk.LEFT)

        self.result_text = scrolledtext.ScrolledText(
            self.result_tab,
            wrap=tk.WORD,
            font=("Malgun Gothic", 10),
            relief=tk.FLAT,
            borderwidth=0,
            padx=12,
            pady=12,
            background=MINT_TEXT_AREA,
            foreground=TEAL_INK,
            insertbackground=TEAL_PRIMARY_ACTIVE,
        )
        self.result_text.pack(fill=tk.BOTH, expand=True, pady=(14, 0))

    def capture_area(self) -> None:
        if self._active_operation_id is not None:
            messagebox.showinfo("처리 중", "현재 작업이 끝난 뒤 새 화면을 캡처해 주세요.")
            return

        try:
            self._close_recent_window()
            self.status_var.set("인식할 공문 영역을 드래그해 주세요. 취소하려면 Esc를 누르세요.")
            self.withdraw()
            self.after_idle(self._capture_after_hide)
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
            self.status_var.set("캡처가 취소되었습니다.")
            return

        if not self._prepare_to_leave_current("새 캡처"):
            return

        try:
            record = self.capture_store.save(image)
        except Exception as exc:
            messagebox.showerror("캡처 저장 오류", f"캡처 이미지를 임시 저장하지 못했습니다.\n\n{exc}")
            self.status_var.set("캡처 이미지를 저장하지 못했습니다.")
            return

        self._refresh_recent_captures(record)
        self._start_new_workspace(
            source_kind="capture",
            source_name="화면 캡처",
            capture_path=record.path,
        )
        self._run_ocr(image)

    def load_attachment_file(self) -> None:
        if self._active_operation_id is not None:
            messagebox.showinfo("처리 중", "현재 작업이 끝난 뒤 첨부 파일을 열어 주세요.")
            return

        path_string = filedialog.askopenfilename(
            title="첨부 파일 선택",
            filetypes=[
                (
                    "지원 파일",
                    "*.png;*.jpg;*.jpeg;*.bmp;*.webp;*.pdf;*.doc;*.docx;*.rtf;*.odt;"
                    "*.ppt;*.pptx;*.xls;*.xlsx;*.txt;*.md;*.csv;*.tsv;*.hwpx;*.hwp",
                ),
                ("이미지", "*.png;*.jpg;*.jpeg;*.bmp;*.webp"),
                ("PDF", "*.pdf"),
                ("문서", "*.doc;*.docx;*.rtf;*.odt;*.hwpx;*.hwp"),
                ("프레젠테이션", "*.ppt;*.pptx"),
                ("표 파일", "*.xls;*.xlsx;*.csv;*.tsv"),
                ("텍스트", "*.txt;*.md;*.json;*.xml;*.html"),
                ("모든 파일", "*.*"),
            ],
        )
        if not path_string:
            return

        path = Path(path_string)
        kind = attachment_kind(path)
        if kind == "text":
            self._load_text_attachment(path)
            return

        if kind == "hwpx":
            self._load_hwpx_attachment(path)
            return

        if kind == "openai_document":
            if not self._prepare_to_leave_current("새 첨부 파일"):
                return
            self._start_new_workspace(
                source_kind="file",
                source_name=path.name,
                source_path=path,
            )
            self._run_document_extraction(path)
            return

        if kind == "legacy_hwp":
            messagebox.showinfo(
                "HWP 파일 안내",
                "구형 HWP 파일은 직접 읽기 안정성이 낮습니다.\n"
                "한글에서 HWPX 또는 PDF로 저장한 뒤 다시 열거나, 필요한 영역을 화면 캡처해 주세요.",
            )
            self.status_var.set("구형 HWP 파일은 HWPX 또는 PDF 변환 후 열어 주세요.")
            return

        if kind != "image":
            messagebox.showinfo(
                "지원하지 않는 형식",
                "현재 지원하지 않는 첨부 형식입니다. 필요한 영역을 화면 캡처해 주세요.",
            )
            self.status_var.set("지원하지 않는 첨부 형식입니다. 화면 캡처를 이용해 주세요.")
            return

        try:
            with Image.open(path) as source_image:
                image = source_image.copy()
            if not self._prepare_to_leave_current("새 이미지 첨부"):
                return
            record = self.capture_store.save(image, source=f"file_{path.stem}")
            self._refresh_recent_captures(record)
            self._start_new_workspace(
                source_kind="file",
                source_name=path.name,
                source_path=path,
            )
            self._run_ocr(image)
        except Exception as exc:
            messagebox.showerror(
                "첨부 파일 오류",
                f"이미지 파일을 불러오지 못했습니다.\n\n오류 내용: {exc}",
            )
            self.status_var.set("이미지 첨부 파일을 불러오지 못했습니다.")

    def _load_text_attachment(self, path: Path) -> None:
        if not self._prepare_to_leave_current("새 텍스트 첨부"):
            return
        self._start_new_workspace(
            source_kind="file",
            source_name=path.name,
            source_path=path,
        )
        self._run_local_document_extraction(path, read_text_file)

    def _load_hwpx_attachment(self, path: Path) -> None:
        if not self._prepare_to_leave_current("새 HWPX 첨부"):
            return
        self._start_new_workspace(
            source_kind="file",
            source_name=path.name,
            source_path=path,
        )
        self._run_local_document_extraction(path, read_hwpx_file)

    def _run_local_document_extraction(
        self,
        path: Path,
        reader: Callable[[Path], str],
    ) -> None:
        self.status_var.set(f"{path.name} 문서를 로컬에서 읽는 중입니다...")
        self._start_worker_operation(
            "document",
            lambda: reader(path),
            {"filename": path.name},
        )

    def _run_document_extraction(self, path: Path) -> None:
        self.status_var.set(f"{path.name} 첨부 문서를 읽는 중입니다...")
        self._start_worker_operation(
            "document",
            lambda: extract_text_from_file(
                path,
                mime_type_for(path),
                raise_errors=True,
            ),
            {"filename": path.name},
        )

    def _finish_document_extraction(self, text: str, filename: str) -> None:
        self._replace_ocr_text(text, track_change=True)
        self._refresh_auto_title()
        self.notebook.select(self.source_tab)
        self.status_var.set(f"{filename} 문서를 읽었습니다. 원문을 검수해 주세요.")

    def _run_ocr(self, image: Image.Image) -> None:
        self.status_var.set("OpenAI 이미지 OCR로 원문을 읽는 중입니다...")
        self._start_worker_operation(
            "ocr",
            lambda: extract_text_from_image(image, raise_errors=True),
        )

    def _finish_ocr(self, text: str) -> None:
        self._replace_ocr_text(text, track_change=True)
        self._refresh_auto_title()
        self.notebook.select(self.source_tab)
        if text.strip():
            self.status_var.set("원문 추출이 완료되었습니다. 날짜와 첨부파일명을 검수해 주세요.")
        else:
            self.status_var.set("OCR 결과가 비어 있습니다. 이미지 품질을 확인해 주세요.")
            messagebox.showinfo("OCR 결과 없음", "이미지에서 텍스트를 찾지 못했습니다.")

    def focus_ocr_text(self) -> None:
        if self._active_operation_id is not None:
            messagebox.showinfo("처리 중", "현재 작업이 끝난 뒤 새 직접 입력을 시작해 주세요.")
            return
        if not self._prepare_to_leave_current("새 직접 입력"):
            return
        self._start_new_workspace(source_kind="manual", source_name="직접 입력")
        self.notebook.select(self.source_tab)
        self.ocr_text.focus_set()
        self.status_var.set("원문 영역에 직접 입력하거나 붙여넣을 수 있습니다.")

    def analyze_text(self, output_mode: str = DEFAULT_OUTPUT_MODE) -> None:
        if self._active_operation_id is not None:
            messagebox.showinfo("처리 중", "현재 작업이 끝난 뒤 실행안을 만들어 주세요.")
            return

        text = self.ocr_text.get("1.0", "end-1c").strip()
        if not text:
            message = analyze_document_task(text, output_mode)
            self.status_var.set("분석할 원문이 없습니다.")
            messagebox.showinfo("분석할 원문 없음", message)
            self.ocr_text.focus_set()
            return

        self.status_var.set(f"{output_mode}을 만드는 중입니다...")
        self._start_worker_operation(
            "analysis",
            lambda: analyze_document_task(
                text,
                output_mode,
                raise_errors=True,
            ),
            {"output_mode": output_mode},
        )

    def _finish_analysis(self, result: str, output_mode: str) -> None:
        self.current_output_mode = output_mode
        self._replace_result_text(result, track_change=True)
        self.notebook.select(self.result_tab)
        self.status_var.set("업무 실행안이 완성되었습니다. 필요한 항목을 바로 복사할 수 있습니다.")

    def _start_worker_operation(
        self,
        kind: str,
        work: Callable[[], str],
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        if self._active_operation_id is not None:
            raise RuntimeError("another operation is already running")

        operation_id = uuid4().hex
        context_id = self.current_context_id
        self._active_operation_id = operation_id
        self._active_operation_context = context_id
        self._active_operation_kind = kind
        self._active_operation_revisions = (
            self._source_revision,
            self._result_revision,
        )
        self._show_operation_progress(operation_id, kind)
        self._render_workspace_state()

        def worker() -> None:
            try:
                payload = work()
                succeeded = True
            except Exception as exc:
                succeeded = False
                payload = str(exc) or "처리 중 알 수 없는 오류가 발생했습니다."
            self._worker_results.put(
                (
                    operation_id,
                    context_id,
                    kind,
                    succeeded,
                    payload,
                    metadata or {},
                )
            )

        threading.Thread(target=worker, daemon=True).start()

    def _drain_worker_results(self) -> None:
        if self._closing:
            return

        try:
            while True:
                result = self._worker_results.get_nowait()
                (
                    operation_id,
                    context_id,
                    kind,
                    succeeded,
                    payload,
                    metadata,
                ) = result
                is_current_operation = (
                    operation_id == self._active_operation_id
                    and context_id == self.current_context_id
                    and context_id == self._active_operation_context
                )
                if self._hold_active_operation_results and is_current_operation:
                    self._held_worker_results.append(result)
                    continue
                if not is_current_operation:
                    continue

                operation_revisions = self._active_operation_revisions
                self._active_operation_id = None
                self._active_operation_context = None
                self._active_operation_kind = None
                self._active_operation_revisions = None
                self._hide_operation_progress()
                self._render_workspace_state()

                if not succeeded:
                    if kind == "analysis":
                        title = "업무 분석 오류"
                    elif kind == "document":
                        title = "첨부 문서 읽기 오류"
                    else:
                        title = "이미지 OCR 오류"
                    self.status_var.set(
                        "처리에 실패했습니다. 기존 원문과 실행안은 그대로 유지됩니다."
                    )
                    messagebox.showerror(title, payload)
                    continue

                source_changed = (
                    operation_revisions is not None
                    and operation_revisions[0] != self._source_revision
                )
                result_changed = (
                    operation_revisions is not None
                    and operation_revisions[1] != self._result_revision
                )
                if source_changed or (kind == "analysis" and result_changed):
                    self.status_var.set(
                        "처리 중 원문이나 실행안이 수정되어 도착한 결과를 적용하지 않았습니다."
                    )
                    messagebox.showinfo(
                        "처리 결과 미적용",
                        "AI 처리 중 편집한 내용을 보호하기 위해 늦게 도착한 결과를 "
                        "화면에 적용하지 않았습니다. 필요하면 다시 실행해 주세요.",
                    )
                    continue

                if kind == "document":
                    self._finish_document_extraction(payload, metadata.get("filename", "첨부 파일"))
                elif kind == "analysis":
                    self._finish_analysis(
                        payload,
                        metadata.get("output_mode", DEFAULT_OUTPUT_MODE),
                    )
                else:
                    self._finish_ocr(payload)
        except queue.Empty:
            pass

        if not self._closing:
            self._worker_poll_after_id = self.after(50, self._drain_worker_results)

    def _show_operation_progress(self, operation_id: str, kind: str) -> None:
        self._hide_operation_progress()
        self._operation_started_at = time.monotonic()
        self.processing_frame.pack(side=tk.RIGHT, padx=(12, 0))
        self.processing_progress.start(12)
        self.processing_var.set(OPERATION_LABELS.get(kind, "처리 중") + " · 0초")
        self._update_operation_progress(operation_id)

    def _update_operation_progress(self, operation_id: str) -> None:
        self._operation_progress_after_id = None
        if (
            self._closing
            or operation_id != self._active_operation_id
            or self._operation_started_at is None
        ):
            return
        elapsed = max(0, int(time.monotonic() - self._operation_started_at))
        label = OPERATION_LABELS.get(self._active_operation_kind or "", "처리 중")
        self.processing_var.set(f"{label} · {elapsed}초")
        self._operation_progress_after_id = self.after(
            OPERATION_PROGRESS_INTERVAL_MS,
            lambda: self._update_operation_progress(operation_id),
        )

    def _hide_operation_progress(self) -> None:
        if self._operation_progress_after_id is not None:
            try:
                self.after_cancel(self._operation_progress_after_id)
            except tk.TclError:
                pass
            self._operation_progress_after_id = None
        if hasattr(self, "processing_progress"):
            self.processing_progress.stop()
        if hasattr(self, "processing_frame"):
            self.processing_frame.pack_forget()
        self._operation_started_at = None
        if hasattr(self, "processing_var"):
            self.processing_var.set("")

    def copy_result(self) -> None:
        self._copy_to_clipboard(self.result_text.get("1.0", "end-1c").strip(), "전체 실행안")

    def copy_result_section(self, section_key: str, label: str) -> None:
        result = self.result_text.get("1.0", "end-1c").strip()
        section = extract_section(result, section_key)
        if not section:
            messagebox.showinfo("복사할 내용 없음", f"{label} 항목을 찾지 못했습니다.")
            return
        self._copy_to_clipboard(section, label)

    def _copy_to_clipboard(self, text: str, label: str) -> None:
        if not text:
            messagebox.showinfo("복사할 내용 없음", "먼저 업무 실행안을 만들어 주세요.")
            return

        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.update()
            self.status_var.set(f"{label}을 클립보드에 복사했습니다.")
        except Exception as exc:
            messagebox.showerror("복사 오류", f"클립보드에 복사하지 못했습니다.\n\n오류 내용: {exc}")
            self.status_var.set("클립보드 복사에 실패했습니다.")

    def show_recent_captures(self) -> None:
        if self.recent_window is not None and self.recent_window.winfo_exists():
            self._refresh_recent_captures()
            self.recent_window.lift()
            self.recent_window.focus_force()
            return

        window = tk.Toplevel(self)
        window.title("Ssokly - 최근 캡처")
        window.geometry("760x520")
        window.minsize(650, 440)
        window.configure(bg=MINT_CANVAS)
        window.transient(self)
        window.protocol("WM_DELETE_WINDOW", self._close_recent_window)
        if APP_ICON_PATH.exists():
            try:
                window.iconphoto(True, self.window_icon)
            except (AttributeError, tk.TclError):
                pass
        self.recent_window = window

        header = tk.Frame(window, bg=MINT_CANVAS)
        header.pack(fill=tk.X, padx=18, pady=(16, 10))
        tk.Label(
            header,
            text="최근 캡처",
            bg=MINT_CANVAS,
            fg=TEAL_DEEP,
            font=("Malgun Gothic", 16, "bold"),
        ).pack(side=tk.LEFT)
        tk.Label(
            header,
            text="임시 캡처는 최신 8개까지 유지됩니다.",
            bg=MINT_CANVAS,
            fg=TEAL_MUTED,
            font=("Malgun Gothic", 9),
        ).pack(side=tk.LEFT, padx=(12, 0), pady=(5, 0))

        content = tk.Frame(window, bg=MINT_CANVAS)
        content.pack(fill=tk.BOTH, expand=True, padx=18)
        self.recent_list = tk.Listbox(
            content,
            width=28,
            bg=TEAL_DEEP,
            fg=SIDEBAR_TEXT,
            selectbackground=CORAL,
            selectforeground="#ffffff",
            activestyle="none",
            borderwidth=0,
            highlightthickness=0,
            font=("Malgun Gothic", 9),
        )
        self.recent_list.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 12))
        self.recent_list.bind("<<ListboxSelect>>", self._on_recent_capture_selected)

        preview_frame = tk.Frame(content, bg=TEAL_DEEP)
        preview_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.preview_label = tk.Label(
            preview_frame,
            text="캡처를 선택하면 미리보기가 표시됩니다.",
            bg=TEAL_DEEP,
            fg=CORAL_SOFT,
            font=("Malgun Gothic", 9),
            justify=tk.CENTER,
        )
        self.preview_label.pack(fill=tk.BOTH, expand=True)

        actions = tk.Frame(window, bg=MINT_CANVAS)
        actions.pack(fill=tk.X, padx=18, pady=14)
        self.reopen_button = tk.Button(
            actions,
            text="새 업무로 읽기",
            command=self.reopen_recent_capture,
            bg=TEAL_PRIMARY,
            fg="#ffffff",
            activebackground=TEAL_PRIMARY_ACTIVE,
            activeforeground="#ffffff",
            relief=tk.FLAT,
            font=("Malgun Gothic", 9, "bold"),
            padx=12,
            pady=8,
        )
        self.reopen_button.pack(side=tk.LEFT, padx=(0, 6))
        self.delete_capture_button = tk.Button(
            actions,
            text="임시 캡처 삭제",
            command=self.delete_recent_capture,
            bg=CORAL,
            fg="#ffffff",
            activebackground=CORAL_ACTIVE,
            activeforeground="#ffffff",
            relief=tk.FLAT,
            font=("Malgun Gothic", 9),
            padx=12,
            pady=8,
        )
        self.delete_capture_button.pack(side=tk.LEFT, padx=(0, 6))
        self.open_capture_folder_button = tk.Button(
            actions,
            text="임시 폴더 열기",
            command=self.open_capture_folder,
            bg=MINT_PANEL,
            fg=TEAL_DEEP,
            activebackground="#ffffff",
            relief=tk.FLAT,
            font=("Malgun Gothic", 9),
            padx=12,
            pady=8,
        )
        self.open_capture_folder_button.pack(side=tk.LEFT)
        ttk.Button(actions, text="닫기", command=self._close_recent_window).pack(side=tk.RIGHT)

        self._refresh_recent_captures()
        self._render_workspace_state()

    def _close_recent_window(self) -> None:
        if self.recent_window is not None and self.recent_window.winfo_exists():
            self.recent_window.destroy()
        self.recent_window = None
        self.recent_list = None
        self.preview_label = None
        self.preview_photo = None
        self.reopen_button = None
        self.delete_capture_button = None
        self.open_capture_folder_button = None

    def reopen_recent_capture(self) -> None:
        record = self._selected_capture()
        if record is None:
            messagebox.showinfo("최근 캡처", "새 업무로 읽을 캡처 이미지를 선택해 주세요.")
            return
        if self._active_operation_id is not None:
            messagebox.showinfo("처리 중", "현재 작업이 끝난 뒤 최근 캡처를 읽어 주세요.")
            return

        try:
            image = self.capture_store.load(record)
        except OSError as exc:
            messagebox.showerror("최근 캡처 오류", f"캡처 이미지를 다시 열지 못했습니다.\n\n{exc}")
            self._refresh_recent_captures()
            return

        if not self._prepare_to_leave_current("최근 캡처 새 업무"):
            return
        self._close_recent_window()
        self._start_new_workspace(
            source_kind="capture",
            source_name="최근 캡처",
            capture_path=record.path,
        )
        self._run_ocr(image)

    def delete_recent_capture(self) -> None:
        record = self._selected_capture()
        if record is None:
            messagebox.showinfo("최근 캡처", "삭제할 임시 캡처를 선택해 주세요.")
            return
        if (
            self.current_task_id is None
            and self.current_source_kind == "capture"
            and self.current_capture_path is not None
            and self.current_capture_path.resolve(strict=False) == record.path.resolve(strict=False)
        ):
            messagebox.showinfo("사용 중인 캡처", "현재 저장 전 업무가 사용하는 캡처는 삭제할 수 없습니다.")
            return
        if not messagebox.askyesno(
            "임시 캡처 삭제",
            "선택한 임시 캡처를 삭제할까요?\n저장된 업무의 영구 캡처는 삭제되지 않습니다.",
            icon=messagebox.WARNING,
            default=messagebox.NO,
        ):
            return

        try:
            self.capture_store.delete(record)
        except OSError as exc:
            messagebox.showerror(
                "임시 캡처 삭제 오류",
                f"선택한 임시 캡처를 삭제하지 못했습니다.\n\n{exc}",
            )
            self.status_var.set("임시 캡처를 삭제하지 못했습니다.")
            return
        self._refresh_recent_captures()
        self.status_var.set("선택한 임시 캡처를 삭제했습니다.")

    def open_capture_folder(self) -> None:
        try:
            os.startfile(str(self.capture_store.directory))
            self.status_var.set("임시 캡처 저장 폴더를 열었습니다.")
        except OSError as exc:
            messagebox.showerror("폴더 열기 오류", f"임시 저장 폴더를 열지 못했습니다.\n\n{exc}")
            self.status_var.set("임시 캡처 저장 폴더를 열지 못했습니다.")

    def _refresh_recent_captures(self, selected: Optional[CaptureRecord] = None) -> None:
        try:
            self.capture_records = self.capture_store.list_recent()
        except OSError:
            self.capture_records = []
        self.recent_count_var.set(
            f"최근 캡처 {len(self.capture_records)}/{self.capture_store.max_items}"
        )

        if self.recent_list is None or not self.recent_list.winfo_exists():
            return
        self.recent_list.delete(0, tk.END)
        for record in self.capture_records:
            self.recent_list.insert(tk.END, record.label)

        if not self.capture_records:
            self.preview_photo = None
            if self.preview_label is not None:
                self.preview_label.configure(image="", text="아직 임시 캡처가 없습니다.")
            return

        selected_path = selected.path if selected else self.capture_records[0].path
        for index, record in enumerate(self.capture_records):
            if record.path == selected_path:
                self.recent_list.selection_set(index)
                self.recent_list.activate(index)
                self._show_capture_preview(record)
                break

    def _on_recent_capture_selected(self, _event: tk.Event) -> None:
        record = self._selected_capture()
        if record is not None:
            self._show_capture_preview(record)

    def _selected_capture(self) -> Optional[CaptureRecord]:
        if self.recent_list is None:
            return None
        selection = self.recent_list.curselection()
        if not selection:
            return None
        index = selection[0]
        if index >= len(self.capture_records):
            return None
        return self.capture_records[index]

    def _show_capture_preview(self, record: CaptureRecord) -> None:
        if self.preview_label is None:
            return
        try:
            image = self.capture_store.load(record)
            image.thumbnail((450, 330), Image.Resampling.LANCZOS)
            self.preview_photo = ImageTk.PhotoImage(image)
            self.preview_label.configure(image=self.preview_photo, text="")
        except OSError:
            self.preview_photo = None
            self.preview_label.configure(image="", text="미리보기를 열 수 없습니다.")

    def _bind_change_tracking(self) -> None:
        self.task_title_var.trace_add("write", self._on_title_changed)
        self.task_search_var.trace_add("write", self._on_task_search_changed)
        self.ocr_text.bind("<<Modified>>", self._on_editor_modified)
        self.result_text.bind("<<Modified>>", self._on_editor_modified)
        self.ocr_text.edit_modified(False)
        self.result_text.edit_modified(False)

    def _on_title_changed(self, *_args: object) -> None:
        if self._suspend_change_tracking:
            return
        self.title_is_auto = False
        self._mark_workspace_dirty()

    def _on_task_search_changed(self, *_args: object) -> None:
        self._cancel_task_search_refresh()
        if self._closing:
            return
        self._task_search_after_id = self.after(
            TASK_SEARCH_DELAY_MS,
            self._apply_task_search,
        )

    def _apply_task_search(self) -> None:
        self._task_search_after_id = None
        self._refresh_task_library()

    def _cancel_task_search_refresh(self) -> None:
        if self._task_search_after_id is None:
            return
        try:
            self.after_cancel(self._task_search_after_id)
        except tk.TclError:
            pass
        self._task_search_after_id = None

    def _on_editor_modified(self, event: tk.Event) -> None:
        widget = event.widget
        if not isinstance(widget, tk.Text) or not widget.edit_modified():
            return
        widget.edit_modified(False)
        if not self._suspend_change_tracking:
            if widget is self.ocr_text:
                self._source_revision += 1
            elif widget is self.result_text:
                self._result_revision += 1
            self._mark_workspace_dirty()

    def _mark_workspace_dirty(self) -> None:
        self.current_dirty = True
        self._autosave_failed = False
        if self.current_task_id is not None:
            self._schedule_autosave()
        self._render_workspace_state()

    def _schedule_autosave(self) -> None:
        self._cancel_autosave()
        if self.current_task_id is None or self._closing:
            return
        task_id = self.current_task_id
        context_id = self.current_context_id
        self._autosave_after_id = self.after(
            AUTOSAVE_DELAY_MS,
            lambda: self._autosave_if_current(task_id, context_id),
        )

    def _cancel_autosave(self) -> None:
        if self._autosave_after_id is None:
            return
        try:
            self.after_cancel(self._autosave_after_id)
        except tk.TclError:
            pass
        self._autosave_after_id = None

    def _autosave_if_current(self, task_id: str, context_id: str) -> None:
        self._autosave_after_id = None
        if (
            self._closing
            or task_id != self.current_task_id
            or context_id != self.current_context_id
            or not self.current_dirty
        ):
            return
        self.save_current_task(
            show_success=False,
            allow_during_operation=True,
        )

    def _fallback_task_title(self) -> str:
        return f"새 업무 {datetime.now():%Y-%m-%d %H:%M}"

    @staticmethod
    def _clean_title(value: str) -> str:
        return " ".join(value.split())[:60].strip()

    def _generate_default_title(self) -> str:
        if self.current_source_kind == "file" and self.current_source_name:
            file_title = self._clean_title(Path(self.current_source_name).stem)
            if file_title:
                return file_title

        source_text = self.ocr_text.get("1.0", "end-1c")
        for line in source_text.splitlines():
            line_title = self._clean_title(line)
            if line_title:
                return line_title
        return self._fallback_task_title()

    def _set_title_programmatically(self, title: str) -> None:
        previous = self._suspend_change_tracking
        self._suspend_change_tracking = True
        try:
            self.task_title_var.set(title)
        finally:
            self._suspend_change_tracking = previous

    def _refresh_auto_title(self) -> None:
        if not self.title_is_auto:
            return
        self._set_title_programmatically(self._generate_default_title())
        self._render_workspace_state()

    def _has_workspace_content(self) -> bool:
        return bool(
            self.ocr_text.get("1.0", "end-1c").strip()
            or self.result_text.get("1.0", "end-1c").strip()
            or self.current_source_path is not None
            or self.current_capture_path is not None
        )

    def _start_new_workspace(
        self,
        *,
        source_kind: str,
        source_name: str = "",
        source_path: Optional[Path] = None,
        capture_path: Optional[Path] = None,
    ) -> None:
        self._cancel_autosave()
        self.current_context_id = uuid4().hex
        self.current_task_id = None
        self.current_task_updated_at = None
        self.current_task_status = "open"
        self.current_source_kind = source_kind
        self.current_source_name = source_name
        self.current_source_path = source_path.resolve(strict=False) if source_path else None
        self.current_capture_path = capture_path.resolve(strict=False) if capture_path else None
        self.current_output_mode = DEFAULT_OUTPUT_MODE
        self._source_revision = 0
        self._result_revision = 0
        self.current_dirty = False
        self._autosave_failed = False
        self.title_is_auto = True

        if source_kind == "file" and source_name:
            initial_title = self._clean_title(Path(source_name).stem) or self._fallback_task_title()
        else:
            initial_title = self._fallback_task_title()
        self._set_title_programmatically(initial_title)
        self._replace_ocr_text("")
        self._replace_result_text("")
        for item in self.task_tree.selection():
            self.task_tree.selection_remove(item)
        self._render_workspace_state()

    def save_current_task(
        self,
        show_success: bool = True,
        allow_during_operation: bool = False,
    ) -> bool:
        if self._active_operation_id is not None and not allow_during_operation:
            messagebox.showinfo("처리 중", "현재 작업이 끝난 뒤 업무를 저장해 주세요.")
            return False
        if self.current_task_id is None and not self._has_workspace_content():
            if show_success:
                messagebox.showinfo("저장할 내용 없음", "원문을 입력하거나 문서를 먼저 읽어 주세요.")
            return False

        self._cancel_autosave()
        title = self._clean_title(self.task_title_var.get())
        if self.title_is_auto or not title:
            title = self._generate_default_title()
            self._set_title_programmatically(title)

        source_text = self.ocr_text.get("1.0", "end-1c")
        analysis_text = self.result_text.get("1.0", "end-1c")
        source_path = str(self.current_source_path) if self.current_source_path else None

        try:
            if self.current_task_id is None:
                record = self.task_store.create(
                    title=title,
                    status=self.current_task_status,
                    source_kind=self.current_source_kind,
                    source_name=self.current_source_name,
                    source_path=source_path,
                    source_text=source_text,
                    analysis_text=analysis_text,
                    output_mode=self.current_output_mode,
                    capture_path=(
                        self.current_capture_path
                        if self.current_source_kind == "capture"
                        else None
                    ),
                )
            else:
                record = self.task_store.update(
                    self.current_task_id,
                    expected_updated_at=self.current_task_updated_at,
                    title=title,
                    source_name=self.current_source_name,
                    source_path=source_path,
                    source_text=source_text,
                    analysis_text=analysis_text,
                    output_mode=self.current_output_mode,
                )
                if record is None:
                    raise RuntimeError("저장된 업무를 찾을 수 없습니다.")
        except Exception as exc:
            self.current_dirty = True
            self._autosave_failed = True
            self.status_var.set("업무 저장에 실패했습니다. 현재 내용은 화면에 유지됩니다.")
            self._render_workspace_state()
            if show_success:
                messagebox.showerror("업무 저장 오류", f"업무를 저장하지 못했습니다.\n\n{exc}")
            return False

        self.current_task_id = record.id
        self.current_task_updated_at = record.updated_at
        self.current_task_status = record.status
        self.current_source_kind = record.source_kind
        self.current_source_name = record.source_name
        self.current_source_path = Path(record.source_path) if record.source_path else None
        self.current_capture_path = Path(record.capture_path) if record.capture_path else None
        self.current_output_mode = record.output_mode
        self.current_dirty = False
        self._autosave_failed = False
        self.title_is_auto = False
        self._refresh_task_library(selected_id=record.id)
        self._render_workspace_state()
        if show_success:
            self.status_var.set("업무 보관함에 저장했습니다. 이후 수정은 자동 저장됩니다.")
        return True

    def _prepare_to_leave_current(self, reason: str) -> bool:
        discard_operation = False
        if self._active_operation_id is not None:
            self._hold_active_operation_results = True
            discard_operation = messagebox.askyesno(
                "처리 중인 작업",
                f"{reason}으로 이동하면 현재 AI 처리 결과는 반영되지 않습니다.\n계속할까요?",
                icon=messagebox.WARNING,
                default=messagebox.NO,
            )
            if not discard_operation:
                self._resume_held_worker_results()
                return False

        if self.current_task_id is not None and self.current_dirty:
            if not self.save_current_task(
                show_success=True,
                allow_during_operation=discard_operation,
            ):
                self._resume_held_worker_results()
                return False
        elif self.current_task_id is None and self._has_workspace_content():
            answer = messagebox.askyesnocancel(
                "저장되지 않은 업무",
                f"{reason} 전에 현재 업무를 저장할까요?\n"
                "예: 저장  /  아니요: 저장하지 않음  /  취소: 계속 편집",
                icon=messagebox.WARNING,
                default=messagebox.YES,
            )
            if answer is None:
                self._resume_held_worker_results()
                return False
            if answer and not self.save_current_task(
                show_success=True,
                allow_during_operation=discard_operation,
            ):
                self._resume_held_worker_results()
                return False

        self._cancel_autosave()
        if discard_operation:
            self._discard_held_worker_results()
            self._invalidate_active_operation()
        return True

    def _resume_held_worker_results(self) -> None:
        self._hold_active_operation_results = False
        for result in self._held_worker_results:
            self._worker_results.put(result)
        self._held_worker_results.clear()

    def _discard_held_worker_results(self) -> None:
        self._hold_active_operation_results = False
        self._held_worker_results.clear()

    def _invalidate_active_operation(self) -> None:
        self._active_operation_id = None
        self._active_operation_context = None
        self._active_operation_kind = None
        self._active_operation_revisions = None
        self._discard_held_worker_results()
        self._hide_operation_progress()
        self._render_workspace_state()

    def _on_close(self) -> None:
        if self._closing:
            return
        if not self._prepare_to_leave_current("앱 종료"):
            return

        self._closing = True
        self._cancel_autosave()
        self._cancel_task_search_refresh()
        self._active_operation_id = None
        self._active_operation_context = None
        self._active_operation_kind = None
        self._active_operation_revisions = None
        self._hide_operation_progress()
        if self._worker_poll_after_id is not None:
            try:
                self.after_cancel(self._worker_poll_after_id)
            except tk.TclError:
                pass
            self._worker_poll_after_id = None
        self._close_recent_window()
        self.destroy()

    def _set_task_filter(self, value: str) -> None:
        self.task_filter_var.set(value)
        self._update_task_filter_buttons()
        self._refresh_task_library()

    def _update_task_filter_buttons(self) -> None:
        selected = self.task_filter_var.get()
        for value, button in self.task_filter_buttons.items():
            active = value == selected
            button.configure(
                bg=CORAL if active else TEAL_DEEP,
                fg="#ffffff" if active else SIDEBAR_TEXT,
            )

    def _refresh_task_library(self, selected_id: Optional[str] = None) -> None:
        status_filter = self.task_filter_var.get()
        status = None if status_filter == "all" else status_filter
        try:
            self.task_records = self.task_store.search(
                self.task_search_var.get(),
                status=status,
            )
        except Exception as exc:
            self.task_records = []
            self.status_var.set(f"업무 보관함을 읽지 못했습니다: {exc}")

        for item in self.task_tree.get_children():
            self.task_tree.delete(item)
        for record in self.task_records:
            self.task_tree.insert(
                "",
                tk.END,
                iid=record.id,
                values=(
                    record.title,
                    record.updated_at.astimezone().strftime("%m/%d %H:%M"),
                ),
            )
        self.task_count_var.set(f"{len(self.task_records)}개")

        target_id = selected_id or self.current_task_id
        if target_id and self.task_tree.exists(target_id):
            self.task_tree.selection_set(target_id)
            self.task_tree.focus(target_id)
            self.task_tree.see(target_id)
        self._render_workspace_state()

    def _selected_task_id(self) -> Optional[str]:
        selection = self.task_tree.selection()
        return selection[0] if selection else None

    def _selected_task_record(self) -> Optional[TaskRecord]:
        task_id = self._selected_task_id()
        if task_id is None:
            return None
        for record in self.task_records:
            if record.id == task_id:
                return record
        return self.task_store.get(task_id)

    def _on_task_selection_changed(self, _event: tk.Event) -> None:
        self._render_workspace_state()

    def open_selected_task(self) -> None:
        if self._active_operation_id is not None:
            messagebox.showinfo(
                "처리 중",
                "현재 작업이 끝난 뒤 다른 업무를 열어 주세요.",
            )
            return
        task_id = self._selected_task_id()
        if task_id is None:
            messagebox.showinfo("업무 보관함", "열 업무를 선택해 주세요.")
            return
        if task_id == self.current_task_id:
            self.status_var.set("이미 열려 있는 업무입니다.")
            return
        if not self._prepare_to_leave_current("다른 업무"):
            if self.current_task_id and self.task_tree.exists(self.current_task_id):
                self.task_tree.selection_set(self.current_task_id)
            return

        record = self.task_store.get(task_id)
        if record is None:
            messagebox.showinfo("업무 없음", "선택한 업무를 찾을 수 없습니다.")
            self._refresh_task_library()
            return
        self._load_task_record(record)

    def _load_task_record(self, record: TaskRecord) -> None:
        self._cancel_autosave()
        self.current_context_id = uuid4().hex
        self.current_task_id = record.id
        self.current_task_updated_at = record.updated_at
        self.current_task_status = record.status
        self.current_source_kind = record.source_kind
        self.current_source_name = record.source_name
        self.current_source_path = Path(record.source_path) if record.source_path else None
        self.current_capture_path = Path(record.capture_path) if record.capture_path else None
        self.current_output_mode = record.output_mode
        self._source_revision = 0
        self._result_revision = 0
        self.current_dirty = False
        self._autosave_failed = False
        self.title_is_auto = False
        self._set_title_programmatically(record.title)
        self._replace_ocr_text(record.source_text)
        self._replace_result_text(record.analysis_text)
        if self.task_tree.exists(record.id):
            self.task_tree.selection_set(record.id)
            self.task_tree.focus(record.id)
        self.notebook.select(self.result_tab if record.analysis_text.strip() else self.source_tab)
        self.status_var.set("저장된 업무를 불러왔습니다. API를 다시 호출하지 않았습니다.")
        self._render_workspace_state()

    def toggle_selected_task_status(self) -> None:
        record = self._selected_task_record()
        if record is None:
            messagebox.showinfo("업무 보관함", "상태를 바꿀 업무를 선택해 주세요.")
            return
        self._set_task_status(record.id, "completed" if record.status == "open" else "open")

    def toggle_current_task_status(self) -> None:
        if self.current_task_id is None:
            messagebox.showinfo("저장된 업무 없음", "업무를 먼저 보관함에 저장해 주세요.")
            return
        new_status = "completed" if self.current_task_status == "open" else "open"
        self._set_task_status(self.current_task_id, new_status)

    def _set_task_status(self, task_id: str, status: str) -> None:
        if task_id == self.current_task_id and self.current_dirty:
            if not self.save_current_task(show_success=True):
                return
        try:
            if task_id == self.current_task_id:
                expected_updated_at = self.current_task_updated_at
            else:
                selected_record = next(
                    (item for item in self.task_records if item.id == task_id),
                    None,
                )
                expected_updated_at = (
                    selected_record.updated_at if selected_record is not None else None
                )
            record = self.task_store.set_status(
                task_id,
                status,
                expected_updated_at=expected_updated_at,
            )
        except Exception as exc:
            messagebox.showerror("상태 변경 오류", f"업무 상태를 변경하지 못했습니다.\n\n{exc}")
            return
        if record is None:
            messagebox.showinfo("업무 없음", "선택한 업무를 찾을 수 없습니다.")
            self._refresh_task_library()
            return
        if task_id == self.current_task_id:
            self.current_task_status = record.status
            self.current_task_updated_at = record.updated_at
        self._refresh_task_library(selected_id=task_id)
        self.status_var.set("업무를 완료로 표시했습니다." if status == "completed" else "업무를 다시 진행중으로 표시했습니다.")

    def delete_selected_task(self) -> None:
        record = self._selected_task_record()
        if record is None:
            messagebox.showinfo("업무 보관함", "삭제할 업무를 선택해 주세요.")
            return
        if not messagebox.askyesno(
            "업무 삭제",
            f"'{record.title}' 업무를 삭제할까요?\n"
            "저장된 캡처 사본은 함께 삭제되지만 외부 첨부 원본은 유지됩니다.",
            icon=messagebox.WARNING,
            default=messagebox.NO,
        ):
            return

        try:
            deleted = self.task_store.delete(record.id)
        except Exception as exc:
            messagebox.showerror("업무 삭제 오류", f"업무를 삭제하지 못했습니다.\n\n{exc}")
            return
        if not deleted:
            self._refresh_task_library()
            return

        if record.id == self.current_task_id:
            self._cancel_autosave()
            self._invalidate_active_operation()
            self._start_new_workspace(source_kind="manual", source_name="직접 입력")
        self._refresh_task_library()
        self.status_var.set("선택한 업무를 삭제했습니다. 외부 원본 파일은 유지됩니다.")

    def reread_current_source(self) -> None:
        if self._active_operation_id is not None:
            messagebox.showinfo("처리 중", "현재 작업이 끝난 뒤 원문을 다시 읽어 주세요.")
            return

        if self.current_source_kind == "capture":
            if self.current_capture_path is None or not self.current_capture_path.exists():
                messagebox.showinfo("원본 없음", "다시 읽을 캡처 이미지를 찾을 수 없습니다.")
                return
            try:
                with Image.open(self.current_capture_path) as source_image:
                    image = source_image.copy()
            except OSError as exc:
                messagebox.showerror("캡처 원본 오류", f"캡처 이미지를 열지 못했습니다.\n\n{exc}")
                return
            self._run_ocr(image)
            return

        if self.current_source_kind != "file":
            messagebox.showinfo("직접 입력 업무", "직접 입력한 업무에는 다시 읽을 원본 파일이 없습니다.")
            return
        if self.current_source_path is None or not self.current_source_path.exists():
            messagebox.showinfo(
                "원본 파일 없음",
                "첨부 원본이 이동되었거나 삭제되었습니다.\n저장된 원문은 계속 사용할 수 있습니다.",
            )
            return

        path = self.current_source_path
        kind = attachment_kind(path)
        try:
            if kind == "image":
                with Image.open(path) as source_image:
                    image = source_image.copy()
                self._run_ocr(image)
            elif kind == "text":
                self._finish_document_extraction(read_text_file(path), path.name)
            elif kind == "hwpx":
                self._finish_document_extraction(read_hwpx_file(path), path.name)
            elif kind == "openai_document":
                self._run_document_extraction(path)
            else:
                messagebox.showinfo("다시 읽기 안내", "이 원본 형식은 다시 읽기를 지원하지 않습니다.")
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            messagebox.showerror("원문 다시 읽기 오류", f"원본을 다시 읽지 못했습니다.\n\n{exc}")

    def open_current_source(self) -> None:
        if self.current_source_kind == "capture":
            if self.current_capture_path is None or not self.current_capture_path.exists():
                messagebox.showinfo("원본 없음", "연결된 캡처 이미지를 찾을 수 없습니다.")
                return
            self._show_source_preview(self.current_capture_path, self.task_title_var.get())
            return

        if self.current_source_kind == "file":
            if self.current_source_path is None or not self.current_source_path.exists():
                messagebox.showinfo(
                    "원본 파일 없음",
                    "첨부 원본이 이동되었거나 삭제되었습니다.\n저장된 원문과 실행안은 계속 사용할 수 있습니다.",
                )
                return
            try:
                os.startfile(str(self.current_source_path))
                self.status_var.set("연결된 원본 파일을 열었습니다.")
            except OSError as exc:
                messagebox.showerror("원본 열기 오류", f"원본 파일을 열지 못했습니다.\n\n{exc}")
            return

        messagebox.showinfo("직접 입력 업무", "직접 입력한 업무에는 별도 원본 파일이 없습니다.")

    def _show_source_preview(self, path: Path, title: str) -> None:
        try:
            with Image.open(path) as source_image:
                image = source_image.copy()
            image.thumbnail((900, 650), Image.Resampling.LANCZOS)
            photo = ImageTk.PhotoImage(image)
        except OSError as exc:
            messagebox.showerror("캡처 원본 오류", f"캡처 이미지를 열지 못했습니다.\n\n{exc}")
            return

        window = tk.Toplevel(self)
        window.title(f"원본 캡처 - {title}")
        window.configure(bg=TEAL_DEEP)
        window.transient(self)
        label = tk.Label(window, image=photo, bg=TEAL_DEEP)
        label.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)
        setattr(window, "_preview_photo", photo)

    def _replace_ocr_text(self, text: str, track_change: bool = False) -> None:
        self._replace_text_widget(self.ocr_text, text, track_change)

    def _replace_result_text(self, text: str, track_change: bool = False) -> None:
        self._replace_text_widget(self.result_text, text, track_change)

    def _replace_text_widget(
        self,
        widget: tk.Text,
        text: str,
        track_change: bool,
    ) -> None:
        original_state = str(widget.cget("state"))
        previous = self._suspend_change_tracking
        self._suspend_change_tracking = True
        try:
            if original_state == tk.DISABLED:
                widget.configure(state=tk.NORMAL)
            widget.delete("1.0", tk.END)
            widget.insert(tk.END, text)
            widget.edit_modified(False)
        finally:
            if original_state == tk.DISABLED:
                widget.configure(state=tk.DISABLED)
            self._suspend_change_tracking = previous
        if track_change:
            if widget is self.ocr_text:
                self._source_revision += 1
            elif widget is self.result_text:
                self._result_revision += 1
            self._mark_workspace_dirty()

    @staticmethod
    def _set_widget_state(widget: Any, state: str) -> None:
        if widget is None:
            return
        try:
            widget.configure(state=state)
        except tk.TclError:
            pass

    def _render_workspace_state(self) -> None:
        processing = self._active_operation_id is not None
        context_state = tk.DISABLED if processing else tk.NORMAL
        for widget in (
            self.capture_button,
            self.load_button,
            self.input_button,
            *self.template_buttons,
        ):
            self._set_widget_state(widget, context_state)
        self._set_widget_state(self.task_title_entry, tk.NORMAL)

        selected_record = self._selected_task_record()
        selected_state = tk.NORMAL if selected_record is not None and not processing else tk.DISABLED
        self._set_widget_state(self.task_open_button, selected_state)
        self._set_widget_state(self.task_status_button, selected_state)
        self._set_widget_state(self.task_delete_button, selected_state)
        if selected_record is not None:
            self.task_status_button.configure(
                text="다시 진행" if selected_record.status == "completed" else "완료"
            )
        else:
            self.task_status_button.configure(text="완료")

        has_content = self._has_workspace_content()
        if processing:
            if self.current_task_id is None:
                self.save_state_var.set("처리 중 · 저장 전")
                self.save_state_label.configure(fg=TEAL_PRIMARY_ACTIVE)
                self.save_task_button.configure(
                    text="업무로 저장",
                    state=tk.NORMAL if has_content else tk.DISABLED,
                )
            elif self.current_dirty:
                self.save_state_var.set("처리 중 · 저장 필요")
                self.save_state_label.configure(fg=CORAL_ACTIVE)
                self.save_task_button.configure(text="지금 저장", state=tk.NORMAL)
            else:
                self.save_state_var.set("처리 중 · 자동 저장됨")
                self.save_state_label.configure(fg=TEAL_PRIMARY_ACTIVE)
                self.save_task_button.configure(text="저장됨", state=tk.DISABLED)
        elif self.current_task_id is None:
            self.save_state_var.set("저장 전")
            self.save_state_label.configure(fg=TEAL_MUTED)
            self.save_task_button.configure(
                text="업무로 저장",
                state=tk.NORMAL if has_content else tk.DISABLED,
            )
        elif self.current_dirty:
            self.save_state_var.set("저장 실패" if self._autosave_failed else "저장 필요")
            self.save_state_label.configure(fg=CORAL_ACTIVE)
            self.save_task_button.configure(text="지금 저장", state=tk.NORMAL)
        else:
            self.save_state_var.set("자동 저장됨")
            self.save_state_label.configure(fg=TEAL_PRIMARY_ACTIVE)
            self.save_task_button.configure(text="저장됨", state=tk.DISABLED)

        self.current_status_button.configure(
            text="다시 진행" if self.current_task_status == "completed" else "완료로 표시",
            state=tk.NORMAL if self.current_task_id is not None and not processing else tk.DISABLED,
        )
        has_source = (
            self.current_capture_path is not None
            if self.current_source_kind == "capture"
            else self.current_source_path is not None
            if self.current_source_kind == "file"
            else False
        )
        self.open_source_button.configure(
            state=tk.NORMAL if has_source else tk.DISABLED
        )
        self.reread_source_button.configure(
            state=tk.NORMAL if has_source and not processing else tk.DISABLED
        )

        self._set_widget_state(self.ocr_text, tk.NORMAL)
        self._set_widget_state(self.result_text, tk.NORMAL)

        recent_action_state = tk.DISABLED if processing else tk.NORMAL
        self._set_widget_state(self.reopen_button, recent_action_state)
        self._set_widget_state(self.delete_capture_button, recent_action_state)
