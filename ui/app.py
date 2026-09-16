import os
import queue
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk, font as tkfont
from typing import Any, Callable, Optional
from uuid import uuid4

from PIL import Image, ImageTk

from services.ai_service import analyze_document_task
from services.analysis_document import action_card_data
from services.card_outputs import render_current_cards
from services.work_card_store import WorkCardStore
from services.workspace_state import WorkspaceStateStore, text_fingerprint
from services.transfer_policy import TransferPolicyStore, ScopeExpansionRequired, make_text_snapshot, risk_candidates
from ui.transfer_dialog import choose_transfer
from ui.work_cards import WorkCardsPanel
from services.personal_todos import PersonalTodoStore, checklist_items
from services.source_review import highlight_source, tabular_blocks
from services.local_ocr import extract_local_text
from services.diagram_service import generate_workflow_image
from io import BytesIO
from services.capture_service import capture_selected_region
from services.capture_store import (
    CaptureConflictError,
    CaptureRecord,
    CaptureStore,
)
from services.document_service import (
    attachment_kind,
    mime_type_for,
    read_hwpx_file,
    read_text_file,
)
from services.ocr_service import extract_text_from_file, extract_text_from_image
from services.settings_store import (
    MAX_OPACITY_PERCENT,
    MIN_OPACITY_PERCENT,
    WindowSettings,
    WindowSettingsStore,
)
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
CAPTURE_INBOX_PAGE_LIMIT = 250
MAX_BATCH_OCR_ITEMS = 20
OPERATION_PROGRESS_INTERVAL_MS = 250
WINDOW_SETTINGS_SAVE_DELAY_MS = 250
DEFAULT_OUTPUT_MODE = "업무 일정·체크리스트"
NORMAL_WINDOW_GEOMETRY = "1480x920"
NORMAL_WINDOW_MIN_SIZE = (1180, 760)
COMPACT_WINDOW_SIZE = (680, 720)
COMPACT_WINDOW_MIN_SIZE = (620, 560)
OPERATION_LABELS = {
    "ocr": "이미지 OCR",
    "capture_batch": "캡처 정밀 OCR",
    "document": "문서 읽기",
    "analysis": "업무 분석",
    "diagram": "업무 도식화",
}
OCR_REVIEW_MARKERS = ("⟦불확실", "⟦판독불가⟧")


class SsoklyApp(tk.Tk):
    def __init__(
        self,
        task_store: Optional[TaskStore] = None,
        capture_store: Optional[CaptureStore] = None,
        settings_store: Optional[WindowSettingsStore] = None,
    ) -> None:
        super().__init__()

        self.title("Ssokly - 공문 실행 정리")
        self.geometry(NORMAL_WINDOW_GEOMETRY)
        self.minsize(*NORMAL_WINDOW_MIN_SIZE)
        self._set_window_icon()

        self.capture_store = capture_store or CaptureStore()
        self.task_store = task_store or TaskStore()
        self.settings_store = settings_store or WindowSettingsStore(
            app_data_dir=self.task_store.app_data_dir
        )
        loaded_window_settings = self.settings_store.load()
        self.work_cards = WorkCardStore(self.task_store.app_data_dir)
        self.workspace_state = WorkspaceStateStore(self.task_store.app_data_dir)
        self.transfer_policies = TransferPolicyStore(self.task_store.app_data_dir)
        self.personal_todos = PersonalTodoStore(self.task_store.app_data_dir, card_store=self.work_cards)
        self.document_id = uuid4().hex
        self._current_artifact = None
        self._source_sync_after_id = None
        # Product defaults intentionally supersede older saved model selections.
        self.ocr_engine_var = tk.StringVar(value="OpenAI 정밀 OCR")
        self.analysis_model_var = tk.StringVar(value="gpt-5-nano")
        self.ocr_model_var = tk.StringVar(value="gpt-5-nano")
        self._analysis_metrics = {}
        self.compact_mode = False
        self.always_on_top = loaded_window_settings.always_on_top
        self.opacity_percent = loaded_window_settings.opacity_percent
        self._initial_compact_mode = loaded_window_settings.compact_mode
        self._normal_geometry = NORMAL_WINDOW_GEOMETRY
        self._normal_window_state = "normal"
        self._settings_save_after_id: Optional[str] = None
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
        self.current_capture_ids: list[str] = []
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
        self._active_operation_cancel_event: Optional[threading.Event] = None
        self._operation_started_at: Optional[float] = None
        self._operation_progress_after_id: Optional[str] = None
        self._closing = False
        self._worker_results = queue.Queue()
        self._worker_poll_after_id: Optional[str] = None
        self._hold_active_operation_results = False
        self._held_worker_results: list[tuple[Any, ...]] = []

        self.recent_window: Optional[tk.Toplevel] = None
        self.capture_inbox_tree: Optional[ttk.Treeview] = None
        self.capture_inbox_filter_var = tk.StringVar(value="unclassified")
        self.capture_search_var = tk.StringVar(value="")
        self.capture_inbox_filter_buttons: dict[str, tk.Button] = {}
        self.capture_search_entry: Optional[tk.Entry] = None
        self._capture_search_after_id: Optional[str] = None
        self.capture_thumbnail_photos: dict[str, ImageTk.PhotoImage] = {}
        self._capture_thumbnail_generation = 0
        self._capture_thumbnail_after_id: Optional[str] = None
        self.preview_label: Optional[tk.Label] = None
        self.capture_preview_text: Optional[scrolledtext.ScrolledText] = None
        self.capture_selected_count_var: Optional[tk.StringVar] = None
        self.capture_review_window: Optional[tk.Toplevel] = None
        self.capture_review_text: Optional[scrolledtext.ScrolledText] = None
        self.capture_review_raw_text: Optional[scrolledtext.ScrolledText] = None
        self.capture_review_id: Optional[str] = None
        self.capture_review_initial_text = ""
        self.capture_review_dirty = False
        self.capture_review_expected_updated_at: Optional[datetime] = None
        self.capture_review_workspace_context: Optional[str] = None
        self.capture_review_workspace_revision: Optional[int] = None
        self.capture_review_workspace_text = ""
        self.capture_review_workspace_format: Optional[str] = None
        self.capture_review_workspace_records: list[CaptureRecord] = []
        self.reopen_button: Optional[tk.Button] = None
        self.add_capture_button: Optional[tk.Button] = None
        self.review_capture_button: Optional[tk.Button] = None
        self.reread_capture_button: Optional[tk.Button] = None
        self.delete_capture_button: Optional[tk.Button] = None
        self.restore_capture_button: Optional[tk.Button] = None
        self.open_capture_folder_button: Optional[tk.Button] = None

        self._build_styles()
        self._build_ui()
        self._apply_initial_window_settings()
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

    def destroy(self):
        # Tests and window-manager teardown can bypass _on_close.
        # Cancel callbacks owned by this Tcl interpreter before its commands vanish.
        try:
            for callback in self.tk.call('after', 'info'):
                self.after_cancel(callback)
        except tk.TclError:
            pass
        super().destroy()

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
        style.configure(
            "Capture.Treeview",
            background=TEAL_DEEP,
            fieldbackground=TEAL_DEEP,
            foreground=SIDEBAR_TEXT,
            borderwidth=0,
            rowheight=58,
            font=("Malgun Gothic", 9),
        )
        style.map(
            "Capture.Treeview",
            background=[("selected", CORAL)],
            foreground=[("selected", "#ffffff")],
        )
        style.configure(
            "Capture.Treeview.Heading",
            background=TEAL_DARK,
            foreground=CORAL_SOFT,
            relief=tk.FLAT,
            font=("Malgun Gothic", 8, "bold"),
        )
        style.map("Capture.Treeview.Heading", background=[("active", TEAL_DARK)])
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
        self.root_frame = ttk.Frame(self, style="App.TFrame")
        self.root_frame.pack(fill=tk.BOTH, expand=True)

        self._build_sidebar(self.root_frame)

        self.workspace = ttk.Frame(
            self.root_frame,
            style="App.TFrame",
            padding=(24, 18, 24, 22),
        )
        self.workspace.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._build_window_controls(self.workspace)
        self._build_header(self.workspace)
        self._build_footer(self.workspace)

        self.notebook = ttk.Notebook(self.workspace, style="Notebook.TNotebook")
        self.notebook.pack(fill=tk.BOTH, expand=True, pady=(16, 0))

        self.source_tab = ttk.Frame(self.notebook, style="Surface.TFrame", padding=18)
        self.result_tab = ttk.Frame(self.notebook, style="Surface.TFrame", padding=18)
        self.notebook.add(self.source_tab, text="원문 검수")
        self.notebook.add(self.result_tab, text="업무 실행안")

        self._build_source_tab()
        self._build_result_tab()
        self._build_cards_tab()

    def _build_cards_tab(self):
        self.cards_tab = ttk.Frame(self.notebook, style="Surface.TFrame", padding=12)
        self.notebook.insert(0, self.cards_tab, text="업무 카드")
        actions = ttk.Frame(self.cards_tab)
        actions.pack(fill=tk.X, pady=(0, 8))
        ttk.Button(actions, text="AI로 업무 추출 / 재분석", command=lambda: self.analyze_text(force=True)).pack(side=tk.LEFT)
        ttk.Button(actions, text="원문 펼치기", command=lambda: self.notebook.select(self.source_tab)).pack(side=tk.LEFT, padx=6)
        ttk.Button(actions, text="초안 이력", command=self.show_artifact_history).pack(side=tk.LEFT)
        self.card_panel = WorkCardsPanel(self.cards_tab, self.work_cards, lambda: self.document_id,
            on_changed=self._card_changed, on_add_todo=self._add_card_todo, on_generate=self.generate_card_draft)
        self.card_panel.pack(fill=tk.BOTH, expand=True)
        self.notebook.select(self.cards_tab)

    def _build_window_controls(self, workspace: ttk.Frame) -> None:
        self.window_controls = ttk.Frame(workspace, style="App.TFrame")
        self.window_controls.pack(fill=tk.X, pady=(0, 10))

        controls = ttk.Frame(self.window_controls, style="App.TFrame")
        controls.pack(side=tk.RIGHT)

        self.compact_button = ttk.Button(
            controls,
            text="컴팩트",
            command=lambda: self.set_compact_mode(not self.compact_mode),
        )
        self.compact_button.pack(side=tk.LEFT, padx=(0, 10))

        self.always_on_top_var = tk.BooleanVar(value=self.always_on_top)
        self.topmost_button = ttk.Checkbutton(
            controls,
            text="항상 위",
            variable=self.always_on_top_var,
            command=lambda: self.set_always_on_top(self.always_on_top_var.get()),
        )
        self.topmost_button.pack(side=tk.LEFT, padx=(0, 12))

        self.opacity_label_var = tk.StringVar(
            value=f"투명도 {self.opacity_percent}%"
        )
        ttk.Label(
            controls,
            textvariable=self.opacity_label_var,
            style="TLabel",
        ).pack(side=tk.LEFT, padx=(0, 7))
        self.opacity_scale_var = tk.DoubleVar(value=float(self.opacity_percent))
        self.opacity_scale = ttk.Scale(
            controls,
            from_=MIN_OPACITY_PERCENT,
            to=MAX_OPACITY_PERCENT,
            length=96,
            variable=self.opacity_scale_var,
            command=self._on_opacity_scale,
        )
        self.opacity_scale.pack(side=tk.LEFT)

    def _apply_initial_window_settings(self) -> None:
        self.set_always_on_top(self.always_on_top, persist=False)
        self.set_opacity_percent(self.opacity_percent, persist=False)
        if self._initial_compact_mode:
            self.set_compact_mode(True, persist=False, initial=True)
        else:
            self.compact_button.configure(text="컴팩트")

    def set_compact_mode(
        self,
        enabled: bool,
        *,
        persist: bool = True,
        initial: bool = False,
    ) -> None:
        enabled = bool(enabled)
        if enabled == self.compact_mode:
            self.compact_button.configure(
                text="전체 화면" if enabled else "컴팩트"
            )
            if persist:
                self._schedule_window_settings_save()
            return

        if enabled:
            self.update_idletasks()
            if not initial:
                try:
                    self._normal_window_state = str(self.state())
                except tk.TclError:
                    self._normal_window_state = "normal"
                if self._normal_window_state == "zoomed":
                    try:
                        self.state("normal")
                        self.update_idletasks()
                    except tk.TclError:
                        self._normal_window_state = "normal"
                geometry = self.geometry()
                if not geometry.startswith("1x1"):
                    self._normal_geometry = geometry

            x = self.winfo_x()
            y = self.winfo_y()
            self.compact_mode = True
            self.sidebar.pack_forget()
            self.header_title_block.pack_forget()
            self.creator_footer.pack_forget()
            self.task_detail_actions.pack_forget()
            self.result_title_block.pack_forget()
            for button in self.partial_copy_buttons:
                button.pack_forget()
            self.workspace.configure(padding=(12, 10, 12, 12))
            self.minsize(*COMPACT_WINDOW_MIN_SIZE)
            compact_width, compact_height = COMPACT_WINDOW_SIZE
            self.geometry(f"{compact_width}x{compact_height}{x:+d}{y:+d}")
            self.compact_button.configure(text="전체 화면")
        else:
            self.compact_mode = False
            self.workspace.configure(padding=(24, 18, 24, 22))
            self.minsize(*NORMAL_WINDOW_MIN_SIZE)
            if not self.sidebar.winfo_manager():
                self.sidebar.pack(
                    side=tk.LEFT,
                    fill=tk.Y,
                    before=self.workspace,
                )
            if not self.header_title_block.winfo_manager():
                self.header_title_block.pack(
                    side=tk.LEFT,
                    fill=tk.X,
                    expand=True,
                    before=self.header_button_bar,
                )
            if not self.creator_footer.winfo_manager():
                self.creator_footer.pack(side=tk.BOTTOM, fill=tk.X, pady=(8, 0))
            if not self.task_detail_actions.winfo_manager():
                self.task_detail_actions.pack(fill=tk.X, pady=(8, 0))
            if not self.result_title_block.winfo_manager():
                self.result_title_block.pack(
                    side=tk.LEFT,
                    before=self.result_action_bar,
                )
            for button in self.partial_copy_buttons:
                if not button.winfo_manager():
                    button.pack(side=tk.LEFT, padx=(0, 6), before=self.copy_button)
            self.geometry(self._normal_geometry)
            self.compact_button.configure(text="컴팩트")
            if self._normal_window_state == "zoomed":
                self.after_idle(self._restore_zoomed_state)

        if persist:
            self._schedule_window_settings_save()

    def _restore_zoomed_state(self) -> None:
        if self._closing or self.compact_mode:
            return
        try:
            self.state("zoomed")
        except tk.TclError:
            pass

    def set_always_on_top(self, enabled: bool, *, persist: bool = True) -> None:
        self.always_on_top = bool(enabled)
        self.always_on_top_var.set(self.always_on_top)
        try:
            self.attributes("-topmost", self.always_on_top)
        except tk.TclError:
            if hasattr(self, "status_var"):
                self.status_var.set("이 환경에서는 항상 위 설정을 적용할 수 없습니다.")
        if persist:
            self._schedule_window_settings_save()

    def _on_opacity_scale(self, value: str) -> None:
        try:
            opacity_percent = round(float(value))
        except (TypeError, ValueError):
            opacity_percent = MAX_OPACITY_PERCENT
        self.set_opacity_percent(opacity_percent)

    def set_opacity_percent(self, value: int, *, persist: bool = True) -> None:
        if isinstance(value, bool):
            opacity_percent = MAX_OPACITY_PERCENT
        else:
            try:
                opacity_percent = round(float(value))
            except (TypeError, ValueError):
                opacity_percent = MAX_OPACITY_PERCENT
        opacity_percent = min(
            MAX_OPACITY_PERCENT,
            max(MIN_OPACITY_PERCENT, opacity_percent),
        )
        self.opacity_percent = opacity_percent
        if abs(self.opacity_scale_var.get() - opacity_percent) > 0.01:
            self.opacity_scale_var.set(float(opacity_percent))
        self.opacity_label_var.set(f"투명도 {opacity_percent}%")
        try:
            self.attributes("-alpha", opacity_percent / 100)
        except tk.TclError:
            if hasattr(self, "status_var"):
                self.status_var.set("이 환경에서는 투명도 설정을 적용할 수 없습니다.")
        if persist:
            self._schedule_window_settings_save()

    def _schedule_window_settings_save(self) -> None:
        if self._closing:
            return
        self._cancel_window_settings_save()
        self._settings_save_after_id = self.after(
            WINDOW_SETTINGS_SAVE_DELAY_MS,
            self._flush_window_settings,
        )

    def _cancel_window_settings_save(self) -> None:
        if self._settings_save_after_id is None:
            return
        try:
            self.after_cancel(self._settings_save_after_id)
        except tk.TclError:
            pass
        self._settings_save_after_id = None

    def _flush_window_settings(self) -> bool:
        self._cancel_window_settings_save()
        try:
            self.settings_store.save(
                WindowSettings(
                    compact_mode=self.compact_mode,
                    always_on_top=self.always_on_top,
                    opacity_percent=self.opacity_percent,
                    role="담당 미지정",
                    ocr_engine=self.ocr_engine_var.get(),
                    analysis_model=self.analysis_model_var.get(),
                    ocr_model=self.ocr_model_var.get(),
                )
            )
        except Exception as exc:
            if hasattr(self, "status_var"):
                self.status_var.set(f"창 설정을 저장하지 못했습니다: {exc}")
            return False
        return True

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
        self.header_frame = ttk.Frame(workspace, style="App.TFrame")
        self.header_frame.pack(fill=tk.X)

        self.header_title_block = ttk.Frame(self.header_frame, style="App.TFrame")
        self.header_title_block.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(
            self.header_title_block,
            text="공문 실행 정리",
            style="Title.TLabel",
        ).pack(anchor=tk.W)
        ttk.Label(
            self.header_title_block,
            text="문서를 읽고, 놓치지 않을 순서와 일정으로 바꿉니다.",
            style="TLabel",
        ).pack(anchor=tk.W, pady=(4, 0))

        self.header_button_bar = ttk.Frame(self.header_frame, style="App.TFrame")
        self.header_button_bar.pack(side=tk.RIGHT, anchor=tk.NE)
        self.recent_count_var = tk.StringVar(value="캡처함 · 미분류 0")
        self.recent_button = ttk.Button(
            self.header_button_bar,
            textvariable=self.recent_count_var,
            command=self.show_recent_captures,
        )
        self.recent_button.pack(side=tk.LEFT, padx=(0, 8))
        self.capture_button = ttk.Button(
            self.header_button_bar,
            text="화면 캡처",
            style="Primary.TButton",
            command=self.capture_area,
        )
        self.capture_button.pack(side=tk.LEFT, padx=(0, 8))
        self.load_button = ttk.Button(
            self.header_button_bar,
            text="첨부 파일 열기",
            style="Secondary.TButton",
            command=self.load_attachment_file,
        )
        self.load_button.pack(side=tk.LEFT, padx=(0, 8))
        self.input_button = ttk.Button(
            self.header_button_bar,
            text="직접 입력",
            command=self.focus_ocr_text,
        )
        self.input_button.pack(side=tk.LEFT)

        self.status_row = ttk.Frame(workspace, style="App.TFrame")
        self.status_row.pack(fill=tk.X, pady=(10, 0))
        self.status_var = tk.StringVar(value="새 문서를 캡처하거나 첨부 파일을 열어 주세요.")
        ttk.Label(
            self.status_row,
            textvariable=self.status_var,
            style="Status.TLabel",
        ).pack(side=tk.LEFT, anchor=tk.W, fill=tk.X, expand=True)

        self.processing_frame = ttk.Frame(self.status_row, style="App.TFrame")
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

        self.task_bar = tk.Frame(workspace, bg=MINT_PANEL, padx=12, pady=10)
        self.task_bar.pack(fill=tk.X, pady=(12, 0))
        self.task_identity_row = tk.Frame(self.task_bar, bg=MINT_PANEL)
        self.task_identity_row.pack(fill=tk.X)
        tk.Label(
            self.task_identity_row,
            text="업무 제목",
            bg=MINT_PANEL,
            fg=TEAL_DEEP,
            font=("Malgun Gothic", 9, "bold"),
        ).pack(side=tk.LEFT, padx=(0, 8))
        self.task_title_var = tk.StringVar(value=self._fallback_task_title())
        self.task_title_entry = tk.Entry(
            self.task_identity_row,
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
            self.task_identity_row,
            textvariable=self.save_state_var,
            bg=MINT_PANEL,
            fg=TEAL_MUTED,
            font=("Malgun Gothic", 8),
        )
        self.save_state_label.pack(side=tk.LEFT, padx=10)
        self.save_task_button = ttk.Button(
            self.task_identity_row,
            text="업무로 저장",
            style="Primary.TButton",
            command=lambda: self.save_current_task(allow_during_operation=True),
        )
        self.save_task_button.pack(side=tk.LEFT)

        self.task_detail_actions = tk.Frame(self.task_bar, bg=MINT_PANEL)
        self.task_detail_actions.pack(fill=tk.X, pady=(8, 0))
        self.current_status_button = ttk.Button(
            self.task_detail_actions,
            text="완료로 표시",
            command=self.toggle_current_task_status,
        )
        self.current_status_button.pack(side=tk.RIGHT, padx=(6, 0))
        self.reread_source_button = ttk.Button(
            self.task_detail_actions,
            text="정밀 재인식",
            command=self.reread_current_source,
        )
        self.reread_source_button.pack(side=tk.RIGHT, padx=(6, 0))
        self.open_source_button = ttk.Button(
            self.task_detail_actions,
            text="원본 보기",
            command=self.open_current_source,
        )
        self.open_source_button.pack(side=tk.RIGHT)

    def _build_source_tab(self) -> None:
        preferences = ttk.Frame(self.source_tab, style="Surface.TFrame")
        preferences.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(preferences, text="담당·대상별 업무 카드 · GPT-5 nano\n명시적 AI 실행 시 OpenAI 전송 · 근거 재검토 최대 1회(비용 발생)",
                  style="Muted.TLabel").pack(anchor=tk.W)
        self.transfer_status = tk.StringVar(value="아직 외부 전송하지 않음 · 원문 편집·저장은 로컬 처리")
        ttk.Label(preferences, textvariable=self.transfer_status, wraplength=800).pack(anchor=tk.W)
        ttk.Button(preferences, text="전송 사본 선택 / 가리기", command=self.choose_text_transfer).pack(anchor=tk.W, pady=4)
        ttk.Button(preferences, text="내 할 일 보기", command=self.show_personal_todos).pack(anchor=tk.W, pady=(4, 0))
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
            text="업무 카드 추출 · 저장한 카드로 새 초안 만들기",
            bg=MINT_PANEL,
            fg=TEAL_DEEP,
            font=("Malgun Gothic", 10, "bold"),
        ).pack(anchor=tk.W, pady=(0, 6))

        self.template_buttons: list[tk.Button] = []
        button_grid = tk.Frame(template_panel, bg=MINT_PANEL)
        button_grid.pack(fill=tk.X)
        for column in range(2):
            button_grid.columnconfigure(column, weight=1)
        for index, (label, output_mode) in enumerate((
            ("업무 일정·체크리스트", "업무 일정·체크리스트"),
            ("교직원 메신저", "교직원 메신저"),
            ("학부모 메신저", "학부모 메신저"),
            ("가정통신문 초안", "가정통신문 초안"),
        )):
            button = tk.Button(
                button_grid,
                text=label,
                command=lambda mode=output_mode: self.analyze_text(mode),
                bg=CORAL if index == 0 else "#ffffff",
                fg="#ffffff" if index == 0 else TEAL_DEEP,
                activebackground=CORAL_ACTIVE if index == 0 else MINT_TAB,
                activeforeground="#ffffff" if index == 0 else TEAL_DEEP,
                relief=tk.FLAT,
                font=("Malgun Gothic", 9, "bold"),
                padx=12,
                pady=7,
            )
            button.grid(row=index // 2, column=index % 2, sticky="ew", padx=3, pady=3)
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

        review_tools = ttk.Frame(self.source_tab, style="Surface.TFrame")
        review_tools.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(review_tools, text="표 보기", command=self.show_source_tables).pack(side=tk.LEFT)
        ttk.Button(review_tools, text="원문 요약", command=lambda: self.analyze_text("원문 요약")).pack(side=tk.LEFT, padx=6)
        ttk.Label(self.source_tab, text="청록색: 인식된 날짜 · 빨간 밑줄: 판독 불확실·잘못된 날짜·요일 충돌 (원본 대조 필요)",
                  style="Muted.TLabel", wraplength=520).pack(anchor=tk.W, pady=(4, 0))

        footer = ttk.Frame(self.source_tab, style="Surface.TFrame")
        footer.pack(fill=tk.X, pady=(14, 0))
        ttk.Label(
            footer,
            text="위 버튼을 누르면 검수한 원문을 기준으로 선택한 양식을 만듭니다.",
            style="Muted.TLabel",
        ).pack(side=tk.LEFT)

    def _build_result_tab(self) -> None:
        self.result_header = ttk.Frame(self.result_tab, style="Surface.TFrame")
        self.result_header.pack(fill=tk.X)
        self.result_title_block = ttk.Frame(
            self.result_header,
            style="Surface.TFrame",
        )
        self.result_title_block.pack(side=tk.LEFT)
        ttk.Label(
            self.result_title_block,
            text="업무 실행안",
            style="Section.TLabel",
        ).pack(anchor=tk.W)
        ttk.Label(
            self.result_title_block,
            text="필요한 부분만 바로 복사해 일정 등록과 전달 업무에 활용하세요.",
            style="Muted.TLabel",
        ).pack(anchor=tk.W, pady=(4, 0))

        self.result_action_bar = ttk.Frame(
            self.result_header,
            style="Toolbar.TFrame",
        )
        self.result_action_bar.pack(side=tk.RIGHT)
        self.schedule_button = ttk.Button(
            self.result_action_bar,
            text="일정 복사",
            command=lambda: self.copy_result_section("schedule", "일정 메모"),
        )
        self.schedule_button.pack(side=tk.LEFT, padx=(0, 6))
        self.checklist_button = ttk.Button(
            self.result_action_bar,
            text="체크리스트 복사",
            command=lambda: self.copy_result_section("checklist", "체크리스트"),
        )
        self.checklist_button.pack(side=tk.LEFT, padx=(0, 6))
        self.message_button = ttk.Button(
            self.result_action_bar,
            text="전달문 복사",
            command=lambda: self.copy_result_section("message", "전달 문구"),
        )
        self.message_button.pack(side=tk.LEFT, padx=(0, 6))
        self.follow_up_button = ttk.Button(
            self.result_action_bar,
            text="첨부 후속 복사",
            command=lambda: self.copy_result_section("follow_up", "첨부파일별 후속 실행"),
        )
        self.follow_up_button.pack(side=tk.LEFT, padx=(0, 6))
        self.copy_button = ttk.Button(
            self.result_action_bar,
            text="전체 복사",
            style="Secondary.TButton",
            command=self.copy_result,
        )
        self.copy_button.pack(side=tk.LEFT)
        personal_bar = ttk.Frame(self.result_tab, style="Surface.TFrame")
        personal_bar.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(personal_bar, text="내 할 일로 담기", command=self.choose_personal_todos).pack(side=tk.LEFT)
        ttk.Button(personal_bar, text="내 할 일 보기", command=self.show_personal_todos).pack(side=tk.LEFT, padx=6)
        ttk.Button(personal_bar, text="초안 이력", command=self.show_artifact_history).pack(side=tk.LEFT, padx=6)
        self.artifact_status = tk.StringVar(value="")
        ttk.Label(self.result_tab, textvariable=self.artifact_status, foreground="#9b492c", wraplength=800).pack(anchor=tk.W)
        ttk.Button(self.result_tab, text="실행안으로 업무 도식화 만들기", command=self.create_workflow_diagram).pack(anchor=tk.E, pady=(8, 0))
        self.partial_copy_buttons = [
            self.schedule_button,
            self.checklist_button,
            self.message_button,
            self.follow_up_button,
        ]

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
        self.result_text.tag_configure("heading", font=("Malgun Gothic", 13, "bold"), foreground=TEAL_PRIMARY_ACTIVE, spacing1=14, spacing3=7)
        self.result_text.tag_configure("title", font=("Malgun Gothic", 16, "bold"), foreground=TEAL_DEEP, spacing3=10)
        self.result_text.tag_configure("recommendation", foreground="#876035")
        self.stream_preview = scrolledtext.ScrolledText(self.result_tab, height=9, wrap=tk.WORD,
            font=("Malgun Gothic", 10), bg=MINT_PANEL, relief=tk.FLAT, state=tk.DISABLED)

    def show_source_tables(self):
        blocks = tabular_blocks(self.ocr_text.get("1.0", "end-1c"))
        if not blocks:
            messagebox.showinfo("표 인식 안내", "탭으로 구분된 표를 찾지 못했습니다. 표 전체를 선명하게 캡처해 다시 인식해 주세요. 병합 셀은 원본과 대조해야 합니다.")
            return
        window = tk.Toplevel(self)
        window.title("인식된 표 · 원문은 변경되지 않습니다")
        window.geometry("800x440")
        window.transient(self)
        ttk.Label(window, text="행·열을 유지합니다. ↳는 병합 셀에서 이어진 내용입니다. 행을 선택하면 전체 내용을 볼 수 있습니다.").pack(anchor=tk.W, padx=10, pady=8)
        notebook = ttk.Notebook(window)
        notebook.pack(fill=tk.BOTH, expand=True, padx=10)
        for index, rows in enumerate(blocks, 1):
            frame = ttk.Frame(notebook)
            notebook.add(frame, text=f"표 {index}")
            detail = scrolledtext.ScrolledText(frame, height=4, wrap=tk.WORD)
            detail.pack(side=tk.BOTTOM, fill=tk.X)
            detail.configure(state=tk.DISABLED)
            columns = [str(i) for i in range(max(map(len, rows)))]
            tree = ttk.Treeview(frame, columns=columns, show="headings")
            table_font = tkfont.nametofont('TkDefaultFont')
            for i in columns:
                tree.heading(i, text=f"열 {int(i)+1}")
                width = max((table_font.measure(row[int(i)]) + 24 for row in rows if int(i) < len(row)), default=150)
                tree.column(i, width=max(100, min(620, width)), stretch=False)
            vertical = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=tree.yview)
            horizontal = ttk.Scrollbar(frame, orient=tk.HORIZONTAL, command=tree.xview)
            tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
            vertical.pack(side=tk.RIGHT, fill=tk.Y)
            horizontal.pack(side=tk.BOTTOM, fill=tk.X)
            tree.pack(fill=tk.BOTH, expand=True)
            tree.tag_configure("review", foreground="#b42318")
            from services.source_review import review_spans
            for row in rows:
                tree.insert('', tk.END, values=row + [''] * (len(columns)-len(row)), tags=('review',) if review_spans('\t'.join(row)) else ())
            def show_row(event, table=tree, text=detail):
                if not table.selection():
                    return
                values = table.item(table.selection()[0], 'values')
                text.configure(state=tk.NORMAL)
                text.delete('1.0', tk.END)
                text.insert('1.0', '\n'.join(f'열 {i+1}: {value}' for i, value in enumerate(values)))
                text.configure(state=tk.DISABLED)
            tree.bind('<<TreeviewSelect>>', show_row)
        def copy():
            rows = blocks[notebook.index(notebook.select())]
            self.clipboard_clear()
            self.clipboard_append('\n'.join('\t'.join(row) for row in rows))
            self.status_var.set("표를 복사했습니다. 스프레드시트에 붙여넣을 수 있습니다.")
        ttk.Button(window, text="선택한 표 복사", command=copy).pack(pady=8)

    def _todo_window(self, title):
        window = tk.Toplevel(self)
        window.title(title)
        window.geometry("720x520")
        window.transient(self)
        window.grab_set()
        ttk.Label(window, text="항목을 클릭해 선택하세요. 아래에서 상세 내용과 원문을 확인할 수 있습니다.").pack(anchor=tk.W, padx=12, pady=8)
        frame = ttk.Frame(window)
        frame.pack(fill=tk.BOTH, expand=True, padx=12)
        box = tk.Listbox(frame, selectmode=tk.MULTIPLE, exportselection=False)
        bar = ttk.Scrollbar(frame, command=box.yview)
        box.configure(yscrollcommand=bar.set)
        bar.pack(side=tk.RIGHT, fill=tk.Y)
        box.pack(fill=tk.BOTH, expand=True)
        details = scrolledtext.ScrolledText(window, height=8, wrap=tk.WORD, state=tk.DISABLED)
        details.pack(fill=tk.X, padx=12, pady=8)
        def show(value):
            details.configure(state=tk.NORMAL)
            details.delete("1.0", tk.END)
            details.insert("1.0", value)
            details.configure(state=tk.DISABLED)
        buttons = ttk.Frame(window)
        buttons.pack(fill=tk.X, padx=12, pady=8)
        ttk.Button(buttons, text="닫기", command=window.destroy).pack(side=tk.RIGHT)
        return window, box, buttons, show

    def choose_personal_todos(self):
        if self._active_operation_id is not None:
            messagebox.showinfo("처리 중", "분석이 끝난 뒤 항목을 선택해 주세요.")
            return
        if self.work_cards.list_cards(self.document_id):
            self.notebook.select(self.cards_tab)
            self.status_var.set('업무 카드를 선택해 ‘내 할 일에 담기’를 누르세요. 이후 카드 수정도 연결됩니다.')
            return
        items = checklist_items(self.result_text.get("1.0", "end-1c"))
        if not items:
            messagebox.showinfo("체크리스트 필요", "먼저 ‘업무 일정·체크리스트’를 만든 뒤 필요한 항목을 선택하세요.")
            return
        source = self.ocr_text.get("1.0", "end-1c").strip()
        result = self.result_text.get("1.0", "end-1c").strip()
        try:
            matched = self.personal_todos.matches_analysis(result, source)
        except Exception as exc:
            messagebox.showerror("원문 연결 확인 실패", str(exc))
            return
        if not matched:
            messagebox.showinfo("다시 분석 필요", "원문 또는 결과가 바뀌었거나 이전 버전의 결과입니다. 현재 원문으로 ‘업무 일정·체크리스트’를 다시 만든 뒤 담아 주세요.")
            return
        window, box, buttons, show = self._todo_window("내 할 일로 담기")
        for item in items:
            box.insert(tk.END, item.replace("\n", " · "))
        box.bind("<<ListboxSelect>>", lambda event: show("\n\n".join(items[i] for i in box.curselection())))
        def save():
            selected = [items[i] for i in box.curselection()]
            if not selected:
                messagebox.showinfo("항목 선택", "담을 항목을 선택해 주세요.", parent=window)
                return
            try:
                count = self.personal_todos.add(selected, source)
            except Exception as exc:
                messagebox.showerror("저장 실패", str(exc), parent=window)
                return
            self.status_var.set(f"내 할 일 {count}개 저장 · 이미 담은 항목은 중복 저장하지 않았습니다.")
            window.destroy()
        ttk.Button(buttons, text="선택한 항목 담기", command=save).pack(side=tk.LEFT)

    def show_personal_todos(self):
        window, box, buttons, show = self._todo_window("내 할 일 · 앱을 다시 열어도 유지됩니다")
        rows = []
        def refresh():
            try:
                rows[:] = self.personal_todos.list()
            except Exception as exc:
                messagebox.showerror("불러오기 실패", str(exc), parent=window)
                return
            box.delete(0, tk.END)
            for row in rows:
                box.insert(tk.END, ("[완료] " if row['done'] else "[할 일] ") + row['item'].replace("\n", " · "))
            show("저장된 할 일이 없습니다." if not rows else "완료 처리하거나 다시 할 일로 되돌릴 항목을 선택하세요.")
        def details(event):
            show("\n\n".join(rows[i]['item'] + "\n\n담을 당시 원문:\n" + rows[i]['source'] for i in box.curselection()))
        box.bind("<<ListboxSelect>>", details)
        def change(done=None):
            ids = [rows[i]['id'] for i in box.curselection()]
            if not ids:
                return
            if done is None and not messagebox.askyesno("내 할 일 삭제", "선택한 내 할 일을 삭제할까요? 원문 업무는 유지되며 필요하면 다시 담을 수 있습니다.", parent=window):
                return
            try:
                if done is None:
                    self.personal_todos.delete(ids)
                else:
                    self.personal_todos.set_done(ids, done)
            except Exception as exc:
                messagebox.showerror("변경 실패", str(exc), parent=window)
                return
            refresh()
        ttk.Button(buttons, text="완료", command=lambda: change(True)).pack(side=tk.LEFT)
        ttk.Button(buttons, text="다시 할 일로", command=lambda: change(False)).pack(side=tk.LEFT, padx=6)
        ttk.Button(buttons, text="삭제", command=change).pack(side=tk.LEFT)
        refresh()

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
            messagebox.showerror(
                "캡처 저장 오류",
                f"캡처 이미지를 보관함에 저장하지 못했습니다.\n\n{exc}",
            )
            self.status_var.set("캡처 이미지를 저장하지 못했습니다.")
            return

        self._refresh_recent_captures(record)
        self._start_new_workspace(
            source_kind="capture",
            source_name="화면 캡처",
            capture_path=record.path,
            capture_ids=[record.id],
        )
        self._run_ocr(image, capture_id=record.id)

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
                capture_ids=[record.id],
            )
            self._run_ocr(image, capture_id=record.id)
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
        snapshot = self._select_transfer(kind="file", path=path, force=True)
        if snapshot is None:
            return
        self.status_var.set(f"{path.name} 첨부 문서를 읽는 중입니다...")
        self._start_worker_operation(
            "document",
            lambda: self._read_transfer_snapshot(snapshot, path=path),
            {"filename": path.name, "transfer_snapshot": snapshot,
             "document_policy_scope": snapshot.policy.scope_id},
        )

    def _finish_document_extraction(self, text: str, filename: str) -> None:
        self._replace_ocr_text(text, track_change=True)
        self._record_imported_source(text, '파일 추출본')
        self._refresh_auto_title()
        self.notebook.select(self.source_tab)
        self.status_var.set(f"{filename} 문서를 읽었습니다. 원문을 검수해 주세요.")

    def _run_ocr(
        self,
        image: Image.Image,
        *,
        capture_id: Optional[str] = None,
        apply_mode: str = "replace",
    ) -> None:
        local = self.ocr_engine_var.get() == "Windows 기본 OCR" and apply_mode == "replace"
        selected_model = self.ocr_model_var.get()
        snapshot = None if local else self._select_transfer(kind="image", image=image, capture_id=capture_id, force=True)
        if not local and snapshot is None:
            return
        self.status_var.set("Windows 기본 OCR로 읽는 중입니다..." if local else "OpenAI 정밀 OCR로 읽는 중입니다...")
        self._start_worker_operation(
            "ocr",
            lambda: extract_local_text(image) if local else self._read_transfer_snapshot(snapshot, model=selected_model),
            {
                "capture_id": capture_id,
                "apply_mode": apply_mode,
                "ocr_profile": "windows-ko-v1" if local else f"{selected_model}/high-table-v2",
                "transfer_snapshot": snapshot,
                "document_policy_scope": ((policy.scope_id if policy else None)
                    if (policy := self.transfer_policies.get('document:' + self.document_id)) is not None else None),
            },
        )

    def _finish_ocr(self, text: str, apply_mode: str = "replace") -> None:
        if apply_mode == "metadata_only":
            self.status_var.set("캡처함의 정밀 OCR 결과를 갱신했습니다.")
            return
        if apply_mode == "review" and self.ocr_text.get("1.0", "end-1c").strip():
            self._show_ocr_comparison(text)
            return
        self._replace_ocr_text(text, track_change=True)
        self._record_imported_source(text, 'OCR 원본')
        self._refresh_auto_title()
        self.notebook.select(self.source_tab)
        if text.strip():
            self.status_var.set("원문 추출이 완료되었습니다. 날짜와 첨부파일명을 검수해 주세요.")
        else:
            self.status_var.set("OCR 결과가 비어 있습니다. 이미지 품질을 확인해 주세요.")
            messagebox.showinfo("OCR 결과 없음", "이미지에서 텍스트를 찾지 못했습니다.")

    def _show_ocr_comparison(
        self,
        candidate_text: str,
        *,
        title: str = "정밀 OCR 결과 비교",
        description: str = (
            "기존 검수본은 자동으로 바꾸지 않습니다. 원본과 비교한 뒤 적용하세요."
        ),
        candidate_label: str = "새 정밀 OCR",
        apply_button_text: str = "새 OCR 적용",
        applied_status: str = "확인한 정밀 OCR 결과를 원문에 적용했습니다.",
    ) -> None:
        context_id = self.current_context_id
        source_revision = self._source_revision
        window = tk.Toplevel(self)
        window.title(f"Ssokly - {title}")
        window.geometry("1120x680")
        window.minsize(900, 560)
        window.configure(bg=MINT_CANVAS)
        window.transient(self)

        header = tk.Frame(window, bg=MINT_CANVAS)
        header.pack(fill=tk.X, padx=18, pady=(16, 10))
        tk.Label(
            header,
            text=title,
            bg=MINT_CANVAS,
            fg=TEAL_DEEP,
            font=("Malgun Gothic", 16, "bold"),
        ).pack(anchor=tk.W)
        tk.Label(
            header,
            text=description,
            bg=MINT_CANVAS,
            fg=TEAL_MUTED,
            font=("Malgun Gothic", 9),
        ).pack(anchor=tk.W, pady=(4, 0))

        content = tk.Frame(window, bg=MINT_CANVAS)
        content.pack(fill=tk.BOTH, expand=True, padx=18)
        for column, label, value in (
            (0, "현재 검수 원문", self.ocr_text.get("1.0", "end-1c")),
            (1, candidate_label, candidate_text),
        ):
            panel = tk.Frame(content, bg=MINT_SURFACE)
            panel.grid(row=0, column=column, sticky="nsew", padx=(0, 6) if column == 0 else (6, 0))
            tk.Label(
                panel,
                text=label,
                bg=MINT_SURFACE,
                fg=TEAL_DEEP,
                font=("Malgun Gothic", 10, "bold"),
            ).pack(anchor=tk.W, padx=12, pady=(10, 6))
            editor = scrolledtext.ScrolledText(
                panel,
                wrap=tk.WORD,
                font=("Malgun Gothic", 10),
                bg=MINT_TEXT_AREA,
                fg=TEAL_INK,
                relief=tk.FLAT,
                padx=10,
                pady=10,
            )
            editor.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 12))
            editor.insert("1.0", value)
            editor.configure(state=tk.DISABLED)
        content.grid_columnconfigure(0, weight=1)
        content.grid_columnconfigure(1, weight=1)
        content.grid_rowconfigure(0, weight=1)

        actions = tk.Frame(window, bg=MINT_CANVAS)
        actions.pack(fill=tk.X, padx=18, pady=14)

        def apply_candidate() -> None:
            if (
                self.current_context_id != context_id
                or self._source_revision != source_revision
            ):
                messagebox.showinfo(
                    f"{title} 미적용",
                    "비교 창을 연 뒤 업무 원문이 변경되어 후보 원문을 적용하지 않았습니다.",
                    parent=window,
                )
                return
            self._replace_ocr_text(candidate_text, track_change=True)
            self._refresh_auto_title()
            self.notebook.select(self.source_tab)
            self.status_var.set(applied_status)
            window.destroy()

        ttk.Button(
            actions,
            text=apply_button_text,
            style="Primary.TButton",
            command=apply_candidate,
        ).pack(side=tk.RIGHT)
        ttk.Button(actions, text="기존 원문 유지", command=window.destroy).pack(
            side=tk.RIGHT,
            padx=(0, 8),
        )

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

    def _capture_policy_scopes(self):
        return {key: policy.scope_id for key in self.current_capture_ids
                if (policy := self.transfer_policies.get('capture:' + key)) is not None}

    def _transfer_policy(self, capture_id=None):
        policy = self.transfer_policies.get("capture:" + capture_id) if capture_id else None
        if policy is None:
            policy = self.transfer_policies.get("document:" + self.document_id)
        if not capture_id:
            acknowledged = self.workspace_state.get(self.document_id).get('capture_policy_scopes', {})
            protected = [item for key in self.current_capture_ids
                         if (item := self.transfer_policies.get('capture:' + key)) is not None
                         and item.redacted and acknowledged.get(key) != item.scope_id]
            if protected:
                from services.transfer_policy import TransferPolicy
                restrictions = protected + ([policy] if policy and policy.redacted else [])
                policy = TransferPolicy(uuid4().hex, 'text', True,
                    '\n'.join(item.approved_text for item in restrictions),
                    tuple({token for item in restrictions for token in item.protected}))
        return policy

    def choose_text_transfer(self):
        if self._active_operation_id is not None:
            messagebox.showinfo('처리 중', '현재 요청이 끝나거나 취소된 뒤 전송 범위를 변경해 주세요.')
            return
        self._select_transfer(kind="text", text=self.ocr_text.get("1.0", "end-1c"), force=True)

    def _select_transfer(self, *, kind, text='', image=None, path=None,
                         capture_id=None, force=False, purpose='source'):
        """The only UI-to-network selection gate. Failure/cancel never sends."""
        try:
            previous = self._transfer_policy(capture_id)
            state = self.workspace_state.get(self.document_id)
            snapshot = None
            if kind == 'text' and not force and previous is not None:
                if (previous.redacted and state.get('approved_source_hash') == text_fingerprint(text)
                    and state.get('approved_source_scope') == previous.scope_id):
                    # Reuse the immutable redacted text, not the original still in the editor.
                    snapshot = make_text_snapshot(previous.approved_text, previous=previous)
                else:
                    try:
                        previous.guard_text(text)
                        if not risk_candidates(text) or text == previous.approved_text:
                            snapshot = make_text_snapshot(text, previous=previous)
                    except ScopeExpansionRequired:
                        pass
            if snapshot is None:
                snapshot = choose_transfer(self, kind=kind, text=text, image=image, path=path,
                    previous=previous, title="도식화에 보낼 사본" if purpose == 'diagram' else "AI 전송 대상 선택")
            if snapshot is None:
                self.status_var.set("전송을 취소했습니다. 원본은 로컬에 유지됩니다.")
                return None
            key = "capture:" + capture_id if capture_id else "document:" + self.document_id
            # Diagram grants are request-scoped. They must not loosen source policy.
            if purpose == 'source':
                self.transfer_policies.save(key, snapshot.policy)
                if kind == 'text':
                    self.workspace_state.update(self.document_id, approved_source_hash=text_fingerprint(text),
                        approved_source_scope=snapshot.policy.scope_id, capture_policy_scopes=self._capture_policy_scopes())
            label = {'text': '텍스트', 'image': '이미지 픽셀', 'file': '선택 파일 전체'}[snapshot.kind]
            self.transfer_status.set(f"전송 대상: {'가린 사본' if snapshot.policy.redacted else '선택 사본'} · {label} · OpenAI API")
            return snapshot
        except Exception:
            self.status_var.set("전송 사본을 준비하지 못해 요청하지 않았습니다.")
            messagebox.showerror("전송 중단", "가림·전송 정책을 안전하게 준비하거나 저장하지 못했습니다. 원본을 대신 보내지 않았습니다. 다시 선택해 주세요.")
            return None

    @staticmethod
    def _read_transfer_snapshot(snapshot, *, path=None, model=None):
        if snapshot.kind == 'text':
            return snapshot.text
        if snapshot.kind == 'image':
            return extract_text_from_image(snapshot.as_image(), detail='high', raise_errors=True, model_override=model)
        if snapshot.kind == 'file':
            selected_path = Path(snapshot.file_name)
            return extract_text_from_file(selected_path, mime_type_for(selected_path), raise_errors=True,
                                          file_bytes=snapshot.file_bytes, filename=snapshot.file_name)
        raise ValueError("승인된 전송 사본이 없습니다.")

    def _remember_transfer_output(self, metadata, text):
        snapshot = metadata.get('transfer_snapshot')
        if snapshot is None:
            return
        # Only OCR output from approved bytes extends an image/file policy.
        policy = snapshot.policy.with_safe_text(text)
        capture_id = metadata.get('capture_id')
        if capture_id:
            self.transfer_policies.save('capture:' + capture_id, policy, expected_scope_id=snapshot.policy.scope_id)
        if metadata.get('apply_mode', 'replace') != 'metadata_only':
            self.transfer_policies.save('document:' + self.document_id, policy,
                expected_scope_id=metadata.get('document_policy_scope'))
            self.workspace_state.update(self.document_id, approved_source_hash=text_fingerprint(text),
                approved_source_scope=policy.scope_id, capture_policy_scopes=self._capture_policy_scopes())

    def _sync_work_source(self):
        text = self.ocr_text.get('1.0', 'end-1c')
        state = self.workspace_state.get(self.document_id)
        current = self.work_cards.get_document(self.document_id)
        if current is None or state.get('source_hash') != text_fingerprint(text):
            current = self.work_cards.ensure_document(self.document_id, text, source_kind='검수본',
                source_ref=self.current_source_name, scope='불러온 범위 · 붙임 확인 전')
            self.workspace_state.update(self.document_id, source_hash=text_fingerprint(text))
        return current

    def _record_imported_source(self, text, kind):
        try:
            self.work_cards.ensure_document(self.document_id, text, source_kind=kind,
                source_ref=self.current_source_name, scope='불러온 범위 · 붙임 확인 전')
            self.workspace_state.update(self.document_id, source_hash=text_fingerprint(text))
        except Exception:
            messagebox.showwarning('원문 버전 저장 실패', '추출한 텍스트는 화면에 유지했습니다. 업무 저장을 다시 시도해 주세요.')

    def _refresh_source_version(self, context):
        self._source_sync_after_id = None
        if self._closing or context != self.current_context_id:
            return
        try:
            self._sync_work_source()
            self.card_panel.refresh()
            self._refresh_artifact_status()
        except Exception:
            self.status_var.set('원문 버전 저장 실패 · 화면의 편집 내용은 유지했습니다. 저장 후 다시 시도하세요.')

    def _card_versions(self):
        return {card['id']: card['version'] for card in self.work_cards.list_cards(self.document_id)
                if not card['comparison_candidate']}

    def _card_changed(self, card):
        self._refresh_artifact_status()
        if self.save_current_task(show_success=False, allow_during_operation=True):
            self.status_var.set('업무 카드 수정 저장 완료 · 내 할 일에 즉시 반영됩니다. 이전 안내문은 그대로 보존됩니다.')
        else:
            messagebox.showwarning('업무 보관함 저장 필요', '카드 수정은 저장했으나 보관함 연결 저장은 실패했습니다. 현재 화면에서 업무 저장을 다시 시도해 주세요.')

    def _add_card_todo(self, card):
        try:
            if card.get('comparison_candidate'):
                messagebox.showinfo('비교 후보', '별도 업무로 채택한 뒤 내 할 일에 담아 주세요.')
                return
            count = self.personal_todos.add_cards([card])
            self.status_var.set(f'내 할 일 {count}개 추가 · 같은 업무는 중복 추가하지 않습니다.')
        except Exception:
            messagebox.showerror('내 할 일 저장 실패', '업무 카드는 유지했습니다. 저장 공간을 확인하고 다시 시도하세요.')

    def _refresh_artifact_status(self):
        if self._current_artifact:
            try:
                stale = self.work_cards.artifact_is_stale(self._current_artifact)
                self.artifact_status.set(('이전 정보 기반 · 현재 정보로 새 초안 만들기 필요' if stale else '현재 업무 버전 기반')
                    + f" · 원문 v{self._current_artifact['source_version']} · {self._current_artifact['kind']}")
            except Exception:
                self.artifact_status.set('결과물 버전을 확인하지 못했습니다. 화면의 내용은 유지됩니다.')
        else:
            self.artifact_status.set('기존 텍스트 기록 · 카드 버전 연결 전' if self.result_text.get('1.0', 'end-1c').strip() else '')

    def _activate_artifact(self, artifact):
        self.workspace_state.update(self.document_id, artifact_id=artifact['id'])
        self._current_artifact = artifact
        self._refresh_artifact_status()

    def _archive_visible_draft(self):
        text = self.result_text.get('1.0', 'end-1c')
        if not text.strip() or (self._current_artifact and self._current_artifact['content'] == text):
            return True
        try:
            source = self._sync_work_source()
            old = self._current_artifact
            artifact = self.work_cards.save_artifact(self.document_id,
                (old['kind'].split(' · ')[0] + ' · 교사 편집') if old else self.current_output_mode + ' · 기존 기록',
                text, old['card_versions'] if old else {}, old['source_version'] if old else source['version'])
            self._activate_artifact(artifact)
            return True
        except Exception:
            messagebox.showerror('초안 저장 실패', '편집한 안내문을 이력에 저장하지 못했습니다. 화면의 내용을 유지합니다. 저장 공간 확인 후 다시 시도하세요.')
            return False

    def generate_card_draft(self, mode=DEFAULT_OUTPUT_MODE):
        if self._active_operation_id is not None:
            messagebox.showinfo('처리 중', '현재 AI 작업이 끝나거나 취소한 뒤 새 초안을 만들어 주세요.')
            return
        if self.card_panel.has_unsaved_changes:
            messagebox.showinfo('카드 저장 필요', '편집한 업무 카드를 먼저 저장해 주세요. 입력값은 유지했습니다.')
            return
        try:
            source = self._sync_work_source()
            cards = self.work_cards.list_cards(self.document_id)
            if not cards:
                self.analyze_text(mode)
                return
            if not self._archive_visible_draft():
                return
            content = self._render_card_output(cards, mode, source['version'])
            artifact = self.work_cards.save_artifact(self.document_id, mode, content, self._card_versions(), source['version'])
            self._activate_artifact(artifact)
            self._finish_analysis(content, mode)
            self._refresh_artifact_status()
            self.status_var.set('저장한 업무 카드로 새 초안을 만들었습니다. 추가 AI 요청 없음 · 이전 초안은 이력에 보존됩니다.')
        except Exception:
            messagebox.showerror('새 초안 생성 실패', '기존 초안과 업무 카드는 유지했습니다. 저장 상태를 확인하고 다시 시도하세요.')

    def show_artifact_history(self):
        if not self._archive_visible_draft():
            return
        try:
            self._sync_work_source()
            history = self.work_cards.list_artifacts(self.document_id)
        except Exception:
            messagebox.showerror('이력 조회 실패', '초안 이력을 읽지 못했습니다. 현재 내용은 유지됩니다.')
            return
        window = tk.Toplevel(self)
        window.title('초안 이력 · 이전 초안도 열람·복사 가능')
        window.geometry('900x660')
        listing = tk.Listbox(window, height=7)
        listing.pack(fill=tk.X, padx=12, pady=12)
        for item in history:
            listing.insert(tk.END, f"{'이전 정보 기반' if item['stale'] else '현재 버전'} | {item['kind']} | 원문 v{item['source_version']} | {item['created_at'][:19]}")
        editor = scrolledtext.ScrolledText(window, wrap=tk.WORD)
        editor.pack(fill=tk.BOTH, expand=True, padx=12)
        def select(_event=None):
            if listing.curselection():
                item = history[listing.curselection()[0]]
                editor.configure(state=tk.NORMAL)
                editor.delete('1.0', tk.END)
                editor.insert('1.0', item['content'])
                editor.configure(state=tk.DISABLED)
        listing.bind('<<ListboxSelect>>', select)
        ttk.Button(window, text='선택 초안 복사 (버전 확인 후 사용)',
            command=lambda: self._copy_to_clipboard(editor.get('1.0', 'end-1c'), '이력 초안')).pack(pady=10)
        if history:
            listing.selection_set(0)
            select()

    def _store_analysis_cards(self, metadata, *, late=False):
        document = metadata.get('document')
        if document is None or late:
            return None
        doc_id = metadata['document_id']
        source_text = metadata['source_text']
        if doc_id == self.document_id:
            self._sync_work_source()
        cards = self.work_cards.merge_analysis(doc_id,
            [action_card_data(action, source_text) for action in document.actions], metadata['source_version'])
        self.workspace_state.update(doc_id, analysis_title=document.title, analysis_summary=document.summary,
            analysis_questions=document.questions, summary_source_version=metadata['source_version'])
        self.card_panel.refresh()
        return cards

    def _render_card_output(self, cards, mode, source_version):
        if mode == '원문 요약':
            state = self.workspace_state.get(self.document_id)
            if state.get('summary_source_version') == source_version and state.get('analysis_summary'):
                return '# 원문 요약\n\n' + state['analysis_summary'] + '\n\n불러온 원문 범위 기준입니다. 카드의 교사 수정값은 새 업무 초안에 반영됩니다.'
            return '# 원문 요약\n\n원문이 바뀌었거나 아직 요약하지 않았습니다. 업무 카드의 ‘AI로 업무 추출 / 재분석’을 실행해 주세요.\n이전 요약과 초안은 이력에서 볼 수 있습니다.'
        # The saved task title may be an OCR first line containing an old date.
        # Factual dates in new drafts come only from the versioned card fields.
        return render_current_cards(cards, mode, title='학교 업무 정리')

    def analyze_text(self, output_mode: str = DEFAULT_OUTPUT_MODE, *, force=False) -> None:
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

        if self.work_cards.list_cards(self.document_id) and not force:
            self.generate_card_draft(output_mode)
            return
        snapshot = self._select_transfer(kind="text", text=text)
        if snapshot is None:
            return
        try:
            current_source = self._sync_work_source()
            source = self.work_cards.ensure_document(self.document_id, snapshot.text,
                source_kind="가린 전송 사본" if snapshot.policy.redacted else current_source['source_kind'],
                source_ref=self.current_source_name, scope="불러온 범위 · 붙임 확인 전")
            if not self._archive_visible_draft():
                return
        except Exception:
            messagebox.showerror("저장 실패", "분석 입력 버전을 저장하지 못했습니다. 현재 내용을 유지했습니다. 저장 공간을 확인하고 다시 시도하세요.")
            return

        self.status_var.set(f"{output_mode}을 만드는 중입니다...")
        model = self.analysis_model_var.get()
        context = self.current_context_id
        revisions = (self._source_revision, self._result_revision)
        cancel_event = threading.Event()
        self._analysis_metrics = {}
        metadata = {"output_mode": output_mode, "document_id": self.document_id,
                    "source_version": source['version'], "source_text": snapshot.text,
                    "card_versions": self._card_versions(), "transfer_snapshot": snapshot}
        self.notebook.select(self.result_tab)
        self.stream_preview.pack(fill=tk.X, before=self.result_text, pady=(8, 0))
        self._set_stream_preview("원문에서 할 일과 근거를 추출하고 있습니다...")
        def preview(value):
            self._worker_results.put((cancel_event, context, "analysis_preview", True, value, {"revisions": revisions}))
        self._start_worker_operation(
            "analysis",
            lambda: analyze_document_task(
                snapshot.text,
                DEFAULT_OUTPUT_MODE,
                raise_errors=True,
                model=model, on_preview=preview, cancel_event=cancel_event,
                cache_dir=self.task_store.app_data_dir,
                on_metrics=lambda metrics: metadata.update(metrics=metrics),
                on_document=lambda document: metadata.update(document=document),
            ),
            metadata,
            cancel_event=cancel_event,
        )

    def _set_stream_preview(self, value):
        self.stream_preview.configure(state=tk.NORMAL)
        self.stream_preview.delete("1.0", tk.END)
        self.stream_preview.insert("1.0", value)
        self.stream_preview.configure(state=tk.DISABLED)

    def create_workflow_diagram(self):
        if self._active_operation_id is not None:
            messagebox.showinfo("처리 중", "현재 처리가 끝난 뒤 도식화를 만들어 주세요.")
            return
        text = self.result_text.get("1.0", "end-1c").strip()
        if not text:
            messagebox.showinfo("실행안 필요", "원문을 분석한 뒤 실행안을 확인해 주세요.")
            return
        if not messagebox.askyesno("업무 도식화", "현재 실행안 텍스트를 OpenAI 이미지 API로 보내 업무 흐름도를 만듭니다.\n별도 이미지 생성 비용이 발생합니다. 날짜와 내용을 확인했나요?"):
            return
        snapshot = self._select_transfer(kind="text", text=text, force=True, purpose="diagram")
        if snapshot is None:
            return
        self._start_worker_operation("diagram", lambda: generate_workflow_image(snapshot.text))

    def _show_workflow_diagram(self, data):
        window = tk.Toplevel(self)
        window.title("업무 도식화 · AI 이미지 검수")
        window.geometry("1000x760")
        ttk.Label(window, text="날짜·이름·화살표 관계를 실행안과 대조한 후 저장하세요.").pack(pady=8)
        image = Image.open(BytesIO(data))
        image.thumbnail((940, 640))
        photo = ImageTk.PhotoImage(image)
        label = ttk.Label(window, image=photo)
        label.image = photo
        label.pack(fill=tk.BOTH, expand=True)
        def save():
            path = filedialog.asksaveasfilename(parent=window, defaultextension=".png", filetypes=[("PNG 이미지", "*.png")], initialfile="업무도식화.png")
            if path:
                try:
                    Path(path).write_bytes(data)
                except OSError as exc:
                    messagebox.showerror("이미지 저장 실패", str(exc), parent=window)
        ttk.Button(window, text="PNG로 저장", command=save).pack(pady=8)

    def _finish_analysis(self, result: str, output_mode: str) -> None:
        # Persist source/result provenance so stale results remain blocked after restart.
        origin_saved = True
        try:
            self.personal_todos.remember_analysis(result, self.ocr_text.get("1.0", "end-1c"))
        except Exception:
            origin_saved = False
        self.stream_preview.pack_forget()
        self.current_output_mode = output_mode
        self._replace_result_text(result, track_change=True)
        self.notebook.select(self.result_tab)
        self.status_var.set("업무 실행안이 완성되었습니다. 필요한 항목을 바로 복사할 수 있습니다.")
        if self._analysis_metrics:
            m = self._analysis_metrics
            self.status_var.set(f"{output_mode} 완료 · {m['seconds']}초 · {m['model']}" + (" · 저장된 사실 재사용" if m['cached'] else " · 원문 근거를 확인해 주세요"))
        if not origin_saved:
            self.status_var.set("분석은 완료됐지만 원문 연결 저장에 실패했습니다. 내 할 일 담기 전 저장 공간을 확인하고 다시 분석해 주세요.")

    def _start_worker_operation(
        self,
        kind: str,
        work: Callable[[], Any],
        metadata: Optional[dict[str, Any]] = None,
        *,
        cancel_event: Optional[threading.Event] = None,
    ) -> None:
        if self._active_operation_id is not None:
            raise RuntimeError("another operation is already running")

        operation_id = uuid4().hex
        context_id = self.current_context_id
        self._active_operation_id = operation_id
        self._active_operation_context = context_id
        self._active_operation_kind = kind
        self._active_operation_cancel_event = cancel_event or threading.Event()
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

    def _run_capture_batch_ocr(
        self,
        records: list[CaptureRecord],
        *,
        apply_mode: str = "metadata_only",
    ) -> None:
        targets = [(record.id, record.path) for record in records]
        if not targets:
            return
        if len(targets) > MAX_BATCH_OCR_ITEMS:
            messagebox.showinfo(
                "정밀 OCR 선택 제한",
                f"한 번에 최대 {MAX_BATCH_OCR_ITEMS}개까지 정밀 OCR할 수 있습니다.\n"
                "비용과 대기 시간을 확인하기 쉽도록 나누어 실행해 주세요.",
            )
            return
        if len(targets) > 1 and not messagebox.askyesno(
            "여러 캡처 정밀 OCR",
            f"캡처 {len(targets)}개를 한 장씩 읽으며 OpenAI API를 {len(targets)}회 호출합니다.\n"
            "완료까지 시간이 걸리고 API 사용 비용이 발생할 수 있습니다. 계속할까요?",
            icon=messagebox.WARNING,
            default=messagebox.NO,
        ):
            return
        selected_model = self.ocr_model_var.get()
        profile = f"{selected_model}/high-table-v2"
        cancel_event = threading.Event()
        approved = []
        for capture_id, path in targets:
            try:
                with Image.open(path) as source_image:
                    snapshot = self._select_transfer(kind='image', image=source_image.copy(),
                        capture_id=capture_id, force=True)
                if snapshot is None:
                    return  # Nothing is submitted until every selection is approved.
                approved.append((capture_id, snapshot))
            except Exception:
                messagebox.showerror('캡처 전송 중단', '이미지 사본을 준비하지 못했습니다. 선택 원본을 대신 보내지 않았습니다.')
                return

        def read_each_original() -> list[dict[str, Any]]:
            results: list[dict[str, Any]] = []
            for capture_id, snapshot in approved:
                if cancel_event.is_set():
                    break
                try:
                    text = self._read_transfer_snapshot(snapshot, model=selected_model)
                    self.transfer_policies.save('capture:' + capture_id, snapshot.policy.with_safe_text(text),
                        expected_scope_id=snapshot.policy.scope_id)
                    results.append(
                        {
                            "capture_id": capture_id,
                            "succeeded": True,
                            "text": text,
                        }
                    )
                except Exception as exc:
                    error = str(exc) or "OCR 중 알 수 없는 오류가 발생했습니다."
                    results.append(
                        {
                            "capture_id": capture_id,
                            "succeeded": False,
                            "error": error,
                        }
                    )
            return results

        self.status_var.set(
            f"캡처 {len(targets)}개를 원본 해상도로 한 장씩 정밀 OCR하는 중입니다..."
        )
        self._start_worker_operation(
            "capture_batch",
            read_each_original,
            {
                "ocr_profile": profile,
                "capture_count": len(targets),
                "apply_mode": apply_mode,
            },
            cancel_event=cancel_event,
        )

    def _persist_single_ocr_result(
        self,
        capture_id: Optional[str],
        *,
        text: Optional[str] = None,
        error: Optional[str] = None,
        profile: str = "",
    ) -> Optional[str]:
        if not capture_id:
            return None
        try:
            if error is not None:
                self.capture_store.set_ocr_failure(
                    capture_id,
                    error,
                    profile=profile,
                )
            else:
                self.capture_store.update_ocr(
                    capture_id,
                    text or "",
                    profile=profile,
                )
        except Exception as exc:
            return str(exc)
        return None

    def _finish_capture_batch_ocr(
        self,
        results: list[dict[str, Any]],
        *,
        profile: str,
        apply_mode: str = "metadata_only",
    ) -> None:
        succeeded_count = 0
        failed_count = 0
        storage_errors: list[str] = []
        for item in results:
            capture_id = str(item.get("capture_id", ""))
            if item.get("succeeded"):
                storage_error = self._persist_single_ocr_result(
                    capture_id,
                    text=str(item.get("text", "")),
                    profile=profile,
                )
                succeeded_count += 1
            else:
                storage_error = self._persist_single_ocr_result(
                    capture_id,
                    error=str(item.get("error", "OCR에 실패했습니다.")),
                    profile=profile,
                )
                failed_count += 1
            if storage_error:
                storage_errors.append(str(storage_error))

        self._refresh_recent_captures()
        if storage_errors:
            self.status_var.set(
                "OCR 결과는 도착했지만 일부 캡처의 메타데이터를 저장하지 못했습니다."
            )
            messagebox.showerror(
                "캡처 OCR 저장 오류",
                "캡처함에 일부 OCR 결과를 기록하지 못했습니다. 원본 이미지는 유지됩니다.\n\n"
                + "\n".join(storage_errors[:3]),
            )
            return
        if failed_count:
            self.status_var.set(
                f"정밀 OCR 완료: 성공 {succeeded_count}개, 실패 {failed_count}개. 기존 성공 원문은 보존했습니다."
            )
            messagebox.showinfo(
                "캡처 정밀 OCR 완료",
                f"성공 {succeeded_count}개 / 실패 {failed_count}개\n\n"
                "실패한 캡처도 이전 OCR 원문은 지우지 않았습니다. 이미지 품질을 확인해 다시 시도하세요.",
            )
        else:
            self.status_var.set(
                f"캡처 {succeeded_count}개의 정밀 OCR 원문을 갱신했습니다."
            )
            if apply_mode == "review_bundle":
                refreshed_records: list[CaptureRecord] = []
                try:
                    for item in results:
                        record = self.capture_store.get(
                            str(item.get("capture_id", ""))
                        )
                        if record is not None:
                            refreshed_records.append(record)
                except Exception as exc:
                    self.status_var.set(
                        "정밀 OCR은 저장했지만 비교용 묶음을 다시 불러오지 못했습니다."
                    )
                    messagebox.showwarning(
                        "OCR 비교 준비 오류",
                        "캡처별 OCR 원문은 보관함에 저장됐지만 비교 창을 열지 못했습니다.\n\n"
                        f"{exc}",
                    )
                    return
                if len(refreshed_records) == len(results):
                    self._show_ocr_comparison(
                        self._capture_bundle_text(
                            refreshed_records,
                            prefer_verified=False,
                        )
                    )

    def _drain_worker_results(self) -> None:
        if self._worker_poll_after_id is not None:
            try:
                self.after_cancel(self._worker_poll_after_id)
            except tk.TclError:
                pass
            self._worker_poll_after_id = None
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
                if kind == "analysis_preview":
                    if (operation_id is self._active_operation_cancel_event
                        and context_id == self.current_context_id
                        and metadata["revisions"] == (self._source_revision, self._result_revision)
                        and not self._hold_active_operation_results):
                        self._set_stream_preview(payload)
                    continue
                is_current_operation = (
                    operation_id == self._active_operation_id
                    and context_id == self.current_context_id
                    and context_id == self._active_operation_context
                )
                if self._hold_active_operation_results and is_current_operation:
                    self._held_worker_results.append(result)
                    continue
                if not is_current_operation:
                    if succeeded and kind == 'analysis' and metadata.get('document_id'):
                        try:
                            self.work_cards.save_artifact(metadata['document_id'], '지연 응답 · 미적용', str(payload),
                                metadata.get('card_versions', {}), metadata['source_version'])
                        except Exception:
                            pass  # Abandoned responses never overwrite the active workspace.
                    continue

                if kind == "analysis":
                    self.stream_preview.pack_forget()
                    self._analysis_metrics = metadata.get("metrics", {})

                operation_revisions = self._active_operation_revisions
                self._active_operation_id = None
                self._active_operation_context = None
                self._active_operation_kind = None
                self._active_operation_revisions = None
                self._active_operation_cancel_event = None
                self._hide_operation_progress()
                self._render_workspace_state()

                if not succeeded:
                    if kind == "ocr":
                        storage_error = self._persist_single_ocr_result(
                            metadata.get("capture_id"),
                            error=str(payload),
                            profile=metadata.get("ocr_profile", ""),
                        )
                        self._refresh_recent_captures()
                        if storage_error:
                            payload = f"{payload}\n\n캡처함 기록 오류: {storage_error}"
                    if kind == "diagram":
                        title = "업무 도식화 오류"
                    elif kind == "analysis":
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

                if kind == "capture_batch":
                    self._finish_capture_batch_ocr(
                        payload,
                        profile=metadata.get("ocr_profile", ""),
                        apply_mode=metadata.get("apply_mode", "metadata_only"),
                    )
                    continue

                if kind in ('ocr', 'document'):
                    try:
                        self._remember_transfer_output(metadata, str(payload))
                    except Exception:
                        self.status_var.set('전송 정책 저장 실패 · 기존 원문 유지. 정책 저장 후 다시 시도하세요.')
                        messagebox.showerror('전송 정책 저장 실패', '승인 사본의 정책을 보존하지 못해 새 전사 결과를 적용하지 않았습니다. 원본 자동 재전송은 하지 않습니다.')
                        continue

                if kind == "ocr":
                    storage_error = self._persist_single_ocr_result(
                        metadata.get("capture_id"),
                        text=str(payload),
                        profile=metadata.get("ocr_profile", ""),
                    )
                    self._refresh_recent_captures()
                    if storage_error:
                        messagebox.showwarning(
                            "캡처 OCR 저장 오류",
                            "OCR 원문은 화면에 적용하지만 캡처함에는 기록하지 못했습니다.\n\n"
                            f"{storage_error}",
                        )

                source_changed = (
                    operation_revisions is not None
                    and operation_revisions[0] != self._source_revision
                )
                result_changed = (
                    operation_revisions is not None
                    and operation_revisions[1] != self._result_revision
                )
                card_changed = (kind == 'analysis' and 'card_versions' in metadata
                    and metadata['card_versions'] != self._card_versions())
                if source_changed or (kind in ("analysis", "diagram") and result_changed) or card_changed:
                    if kind == 'analysis' and metadata.get('document_id'):
                        try:
                            self._store_analysis_cards(metadata, late=True)
                            self.work_cards.save_artifact(metadata['document_id'], '지연 응답 · 이전 입력 기반', str(payload),
                                metadata['card_versions'], metadata['source_version'])
                            self.card_panel.refresh()
                            self._refresh_artifact_status()
                        except Exception:
                            messagebox.showwarning('지연 응답 보존 실패', '편집값은 유지했습니다. 늦은 응답 이력 저장은 실패했습니다.')
                    self.status_var.set(
                        "처리 중 원문이나 실행안이 수정되어 도착한 결과를 적용하지 않았습니다."
                    )
                    messagebox.showinfo(
                        "처리 결과 미적용",
                        "AI 처리 중 편집한 내용을 보호하기 위해 늦게 도착한 결과를 "
                        "화면에 적용하지 않았습니다. 필요하면 다시 실행해 주세요.",
                    )
                    continue

                if kind == "diagram":
                    self._show_workflow_diagram(payload)
                    self.status_var.set("업무 도식화를 만들었습니다. 이미지 내용을 검수한 뒤 저장하세요.")
                elif kind == "document":
                    self._finish_document_extraction(payload, metadata.get("filename", "첨부 파일"))
                elif kind == "analysis":
                    if metadata.get('document') is not None:
                        try:
                            self._store_analysis_cards(metadata)
                            mode = metadata.get('output_mode', DEFAULT_OUTPUT_MODE)
                            cards = self.work_cards.list_cards(self.document_id)
                            payload = self._render_card_output(cards, mode, metadata['source_version'])
                            artifact = self.work_cards.save_artifact(self.document_id, mode, payload,
                                self._card_versions(), metadata['source_version'])
                            self._activate_artifact(artifact)
                        except Exception:
                            self.status_var.set('업무 카드·초안 저장 실패 · 이전 화면을 유지했습니다.')
                            messagebox.showerror('분석 저장 실패', '기존 편집 내용을 유지했습니다. 저장 공간을 확인하고 다시 시도하세요.')
                            continue
                    self._finish_analysis(
                        payload,
                        metadata.get("output_mode", DEFAULT_OUTPUT_MODE),
                    )
                    if metadata.get('document') is not None:
                        self.notebook.select(self.cards_tab)
                        self._refresh_artifact_status()
                        self.save_current_task(show_success=False)
                else:
                    self._finish_ocr(
                        payload,
                        metadata.get("apply_mode", "replace"),
                    )
        except queue.Empty:
            pass
        finally:
            if not self._closing:
                self._worker_poll_after_id = self.after(
                    50,
                    self._drain_worker_results,
                )

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
        window.title("Ssokly - 캡처 보관함")
        window.geometry("1040x680")
        window.minsize(860, 560)
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
            text="캡처 보관함",
            bg=MINT_CANVAS,
            fg=TEAL_DEEP,
            font=("Malgun Gothic", 16, "bold"),
        ).pack(side=tk.LEFT)
        tk.Label(
            header,
            text="무손실 원본과 OCR 전문을 장별로 보존합니다.",
            bg=MINT_CANVAS,
            fg=TEAL_MUTED,
            font=("Malgun Gothic", 9),
        ).pack(side=tk.LEFT, padx=(12, 0), pady=(5, 0))

        filters = tk.Frame(window, bg=MINT_CANVAS)
        filters.pack(fill=tk.X, padx=18, pady=(0, 10))
        self.capture_inbox_filter_buttons = {}
        for label, value in (
            ("미분류", "unclassified"),
            ("업무 연결", "linked"),
            ("전체", "all"),
            ("휴지통", "trash"),
        ):
            button = tk.Button(
                filters,
                text=label,
                command=lambda selected=value: self._set_capture_inbox_filter(selected),
                relief=tk.FLAT,
                font=("Malgun Gothic", 9, "bold"),
                padx=14,
                pady=6,
            )
            button.pack(side=tk.LEFT, padx=(0, 6))
            self.capture_inbox_filter_buttons[value] = button
        self.capture_selected_count_var = tk.StringVar(value="0개 선택")
        tk.Label(
            filters,
            textvariable=self.capture_selected_count_var,
            bg=MINT_CANVAS,
            fg=TEAL_MUTED,
            font=("Malgun Gothic", 9),
        ).pack(side=tk.RIGHT)
        self.capture_search_entry = tk.Entry(
            filters,
            textvariable=self.capture_search_var,
            width=24,
            relief=tk.FLAT,
            bg=MINT_SURFACE,
            fg=TEAL_INK,
            insertbackground=TEAL_PRIMARY_ACTIVE,
            font=("Malgun Gothic", 9),
        )
        self.capture_search_entry.pack(side=tk.RIGHT, padx=(8, 12), ipady=6)
        tk.Label(
            filters,
            text="캡처 검색",
            bg=MINT_CANVAS,
            fg=TEAL_MUTED,
            font=("Malgun Gothic", 9),
        ).pack(side=tk.RIGHT)

        content = tk.Frame(window, bg=MINT_CANVAS)
        content.pack(fill=tk.BOTH, expand=True, padx=18)
        tree_frame = tk.Frame(content, bg=TEAL_DEEP, width=500)
        tree_frame.pack(side=tk.LEFT, fill=tk.BOTH, padx=(0, 12))
        tree_frame.pack_propagate(False)
        self.capture_inbox_tree = ttk.Treeview(
            tree_frame,
            columns=("captured", "state"),
            show="tree headings",
            selectmode="extended",
            style="Capture.Treeview",
        )
        self.capture_inbox_tree.heading("#0", text="OCR 첫 줄")
        self.capture_inbox_tree.heading("captured", text="캡처 시각")
        self.capture_inbox_tree.heading("state", text="상태")
        self.capture_inbox_tree.column("#0", width=300, minwidth=220)
        self.capture_inbox_tree.column("captured", width=105, anchor=tk.CENTER)
        self.capture_inbox_tree.column("state", width=80, anchor=tk.CENTER)
        tree_scrollbar = ttk.Scrollbar(
            tree_frame,
            orient=tk.VERTICAL,
            command=self.capture_inbox_tree.yview,
        )
        self.capture_inbox_tree.configure(yscrollcommand=tree_scrollbar.set)
        tree_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.capture_inbox_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.capture_inbox_tree.bind(
            "<<TreeviewSelect>>",
            self._on_recent_capture_selected,
        )

        preview_frame = tk.Frame(content, bg=MINT_SURFACE)
        preview_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.preview_label = tk.Label(
            preview_frame,
            text="캡처를 선택하면 미리보기가 표시됩니다.",
            bg=TEAL_DEEP,
            fg=CORAL_SOFT,
            font=("Malgun Gothic", 9),
            justify=tk.CENTER,
            height=13,
        )
        self.preview_label.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
        tk.Label(
            preview_frame,
            text="업무에 사용할 원문",
            bg=MINT_SURFACE,
            fg=TEAL_DEEP,
            font=("Malgun Gothic", 9, "bold"),
        ).pack(anchor=tk.W, padx=10, pady=(8, 4))
        self.capture_preview_text = scrolledtext.ScrolledText(
            preview_frame,
            wrap=tk.WORD,
            height=9,
            font=("Malgun Gothic", 9),
            bg=MINT_TEXT_AREA,
            fg=TEAL_INK,
            relief=tk.FLAT,
            padx=8,
            pady=8,
        )
        self.capture_preview_text.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        self.capture_preview_text.configure(state=tk.DISABLED)

        actions = tk.Frame(window, bg=MINT_CANVAS)
        actions.pack(fill=tk.X, padx=18, pady=14)
        self.reopen_button = tk.Button(
            actions,
            text="새 업무로 묶기",
            command=self.bundle_selected_captures,
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
        self.add_capture_button = tk.Button(
            actions,
            text="현재 업무에 추가",
            command=self.add_selected_captures_to_current_task,
            bg=MINT_PANEL,
            fg=TEAL_DEEP,
            activebackground="#ffffff",
            relief=tk.FLAT,
            font=("Malgun Gothic", 9, "bold"),
            padx=12,
            pady=8,
        )
        self.add_capture_button.pack(side=tk.LEFT, padx=(0, 6))
        self.review_capture_button = tk.Button(
            actions,
            text="원문 검수",
            command=self.review_selected_capture,
            bg=MINT_PANEL,
            fg=TEAL_DEEP,
            activebackground="#ffffff",
            relief=tk.FLAT,
            font=("Malgun Gothic", 9, "bold"),
            padx=12,
            pady=8,
        )
        self.review_capture_button.pack(side=tk.LEFT, padx=(0, 6))
        self.reread_capture_button = tk.Button(
            actions,
            text="정밀 OCR",
            command=self.reread_selected_captures,
            bg=CORAL_SOFT,
            fg=TEAL_DEEP,
            activebackground=CORAL,
            relief=tk.FLAT,
            font=("Malgun Gothic", 9),
            padx=12,
            pady=8,
        )
        self.reread_capture_button.pack(side=tk.LEFT, padx=(0, 6))
        self.delete_capture_button = tk.Button(
            actions,
            text="휴지통으로",
            command=self.trash_selected_captures,
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
        self.restore_capture_button = tk.Button(
            actions,
            text="복원",
            command=self.restore_selected_captures,
            bg=MINT_PANEL,
            fg=TEAL_DEEP,
            activebackground="#ffffff",
            relief=tk.FLAT,
            font=("Malgun Gothic", 9),
            padx=12,
            pady=8,
        )
        self.open_capture_folder_button = tk.Button(
            actions,
            text="원본 폴더 열기",
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

    def _close_recent_window(self) -> bool:
        if not self._close_capture_review_window():
            return False
        self._capture_thumbnail_generation += 1
        self._cancel_capture_thumbnail_load()
        self._cancel_capture_search_refresh()
        if self.recent_window is not None and self.recent_window.winfo_exists():
            self.recent_window.destroy()
        self.recent_window = None
        self.capture_inbox_tree = None
        self.capture_search_entry = None
        self.preview_label = None
        self.preview_photo = None
        self.capture_preview_text = None
        self.capture_selected_count_var = None
        self.capture_thumbnail_photos.clear()
        self.reopen_button = None
        self.add_capture_button = None
        self.review_capture_button = None
        self.reread_capture_button = None
        self.delete_capture_button = None
        self.restore_capture_button = None
        self.open_capture_folder_button = None
        return True

    def reopen_recent_capture(self) -> None:
        self.bundle_selected_captures()

    def delete_recent_capture(self) -> None:
        self.trash_selected_captures()

    def _set_capture_inbox_filter(self, value: str) -> None:
        if value not in {"unclassified", "linked", "all", "trash"}:
            value = "unclassified"
        self.capture_inbox_filter_var.set(value)
        self._update_capture_inbox_filter_buttons()
        self._refresh_recent_captures()

    def _update_capture_inbox_filter_buttons(self) -> None:
        selected = self.capture_inbox_filter_var.get()
        for value, button in self.capture_inbox_filter_buttons.items():
            active = value == selected
            button.configure(
                bg=CORAL if active else MINT_PANEL,
                fg="#ffffff" if active else TEAL_DEEP,
            )

    @staticmethod
    def _capture_ocr_preview(record: CaptureRecord) -> str:
        for line in record.effective_text.splitlines():
            normalized = " ".join(line.split()).strip()
            if normalized:
                return normalized[:70]
        if record.ocr_status == "failed":
            return "OCR 실패 · 다시 읽기가 필요합니다"
        return "아직 읽지 않은 캡처"

    @staticmethod
    def _capture_state_label(record: CaptureRecord) -> str:
        if record.trashed_at is not None:
            return "휴지통"
        if record.review_status == "ocr_updated":
            return "OCR 갱신"
        if record.is_verified:
            return "검수 완료"
        if record.linked_task_ids:
            return "업무 연결"
        if record.ocr_status == "ready":
            return "검수 필요"
        if record.ocr_status == "failed":
            return "읽기 실패"
        return "읽지 않음"

    def review_selected_capture(self) -> None:
        records = self._selected_captures()
        if len(records) != 1:
            messagebox.showinfo(
                "원문 검수",
                "원본과 정확히 대조할 캡처 한 장만 선택해 주세요.",
            )
            return
        if self._active_operation_id is not None:
            messagebox.showinfo("처리 중", "현재 작업이 끝난 뒤 원문을 검수해 주세요.")
            return
        record = records[0]
        if not self._require_active_captures(records):
            return
        try:
            review_source_image = self.capture_store.load(record)
        except (OSError, ValueError) as exc:
            messagebox.showerror(
                "원본 열기 오류",
                "원본 이미지를 열 수 없어 검수를 시작하지 않았습니다.\n\n"
                f"{exc}",
            )
            return
        if self.capture_review_window is not None:
            try:
                if self.capture_review_window.winfo_exists():
                    if self.capture_review_id == record.id:
                        self.capture_review_window.lift()
                        self.capture_review_window.focus_force()
                        return
                    if not self._close_capture_review_window():
                        return
            except tk.TclError:
                self._reset_capture_review_state()

        parent = self.recent_window if self.recent_window is not None else self
        window = tk.Toplevel(parent)
        window.title("Ssokly - OCR 원문 검수")
        window.geometry("1180x760")
        window.minsize(940, 620)
        window.configure(bg=MINT_CANVAS)
        window.transient(parent)
        window.protocol("WM_DELETE_WINDOW", self._close_capture_review_window)
        if APP_ICON_PATH.exists():
            try:
                window.iconphoto(True, self.window_icon)
            except (AttributeError, tk.TclError):
                pass

        self.capture_review_window = window
        self.capture_review_id = record.id
        self.capture_review_initial_text = record.effective_text
        self.capture_review_dirty = False
        self.capture_review_expected_updated_at = record.updated_at
        if record.id in self.current_capture_ids:
            current_records = self._current_capture_records()
            self.capture_review_workspace_records = list(current_records)
            workspace_text = self.ocr_text.get("1.0", "end-1c")
            self.capture_review_workspace_context = self.current_context_id
            self.capture_review_workspace_revision = self._source_revision
            self.capture_review_workspace_text = workspace_text
            if len(current_records) == 1 and workspace_text == record.effective_text:
                self.capture_review_workspace_format = "single"
            elif workspace_text == self._capture_bundle_text(current_records):
                self.capture_review_workspace_format = "bundle"
            elif len(current_records) == 1:
                self.capture_review_workspace_format = "compare_single"
            else:
                self.capture_review_workspace_format = "compare_bundle"

        header = tk.Frame(window, bg=MINT_CANVAS)
        header.pack(fill=tk.X, padx=18, pady=(16, 10))
        tk.Label(
            header,
            text="OCR 원문 검수",
            bg=MINT_CANVAS,
            fg=TEAL_DEEP,
            font=("Malgun Gothic", 16, "bold"),
        ).pack(anchor=tk.W)
        tk.Label(
            header,
            text=(
                "원본 업무 문맥을 익명화하거나 자동 교정하지 않습니다. "
                "이미지와 대조해 최종본을 그대로 저장하세요."
            ),
            bg=MINT_CANVAS,
            fg=TEAL_MUTED,
            font=("Malgun Gothic", 9),
        ).pack(anchor=tk.W, pady=(4, 0))

        content = tk.Frame(window, bg=MINT_CANVAS)
        content.pack(fill=tk.BOTH, expand=True, padx=18)

        image_panel = tk.Frame(content, bg=TEAL_DEEP, width=500)
        image_panel.pack(side=tk.LEFT, fill=tk.BOTH, padx=(0, 12))
        image_panel.pack_propagate(False)
        review_source_image.thumbnail((480, 610), Image.Resampling.LANCZOS)
        review_photo = ImageTk.PhotoImage(review_source_image)
        image_label = tk.Label(image_panel, image=review_photo, bg=TEAL_DEEP)
        setattr(window, "_capture_review_photo", review_photo)
        image_label.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

        editor_panel = tk.Frame(content, bg=MINT_SURFACE)
        editor_panel.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        raw_label = "AI OCR 원문 (비교용)"
        if record.ocr_profile:
            raw_label += f" · {record.ocr_profile}"
        tk.Label(
            editor_panel,
            text=raw_label,
            bg=MINT_SURFACE,
            fg=TEAL_DEEP,
            font=("Malgun Gothic", 10, "bold"),
        ).pack(anchor=tk.W, padx=12, pady=(10, 6))
        raw_text = scrolledtext.ScrolledText(
            editor_panel,
            wrap=tk.WORD,
            height=10,
            font=("Malgun Gothic", 10),
            bg=MINT_TEXT_AREA,
            fg=TEAL_INK,
            relief=tk.FLAT,
            padx=10,
            pady=10,
        )
        raw_text.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 10))
        raw_text.insert("1.0", record.ocr_text)
        highlight_source(raw_text)
        raw_text.configure(state=tk.DISABLED)
        self.capture_review_raw_text = raw_text

        review_label = "교사 검수 최종본 (업무에 우선 사용)"
        if record.review_status == "ocr_updated":
            review_label += " · 검수 뒤 AI OCR 갱신됨"
        tk.Label(
            editor_panel,
            text=review_label,
            bg=MINT_SURFACE,
            fg=TEAL_DEEP,
            font=("Malgun Gothic", 10, "bold"),
        ).pack(anchor=tk.W, padx=12, pady=(0, 6))
        review_text = scrolledtext.ScrolledText(
            editor_panel,
            wrap=tk.WORD,
            height=14,
            undo=True,
            font=("Malgun Gothic", 10),
            bg="#ffffff",
            fg=TEAL_INK,
            insertbackground=TEAL_PRIMARY_ACTIVE,
            relief=tk.FLAT,
            padx=10,
            pady=10,
        )
        review_text.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 12))
        review_text.insert("1.0", record.effective_text)
        highlight_source(review_text)
        review_text.edit_modified(False)
        review_text.bind("<<Modified>>", self._on_capture_review_modified)
        self.capture_review_text = review_text

        footer = tk.Frame(window, bg=MINT_CANVAS)
        footer.pack(fill=tk.X, padx=18, pady=(10, 14))
        tk.Label(
            footer,
            text=(
                "검수본은 이 PC의 로컬 DB에 원문 그대로 저장됩니다. "
                "정밀 OCR을 실행할 때만 선택한 원본 이미지가 OpenAI API로 전송됩니다."
            ),
            bg=MINT_CANVAS,
            fg=TEAL_MUTED,
            font=("Malgun Gothic", 8),
        ).pack(anchor=tk.W, pady=(0, 8))

        def open_original() -> None:
            try:
                os.startfile(str(record.path))
            except OSError as exc:
                messagebox.showerror(
                    "원본 열기 오류",
                    f"원본 이미지를 열지 못했습니다.\n\n{exc}",
                    parent=window,
                )

        tk.Button(
            footer,
            text="원본 크게 보기",
            command=open_original,
            bg=MINT_PANEL,
            fg=TEAL_DEEP,
            relief=tk.FLAT,
            padx=12,
            pady=8,
        ).pack(side=tk.LEFT, padx=(0, 6))
        if record.is_verified:
            tk.Button(
                footer,
                text="검수 해제",
                command=self._clear_capture_review,
                bg=MINT_PANEL,
                fg=TEAL_DEEP,
                relief=tk.FLAT,
                padx=12,
                pady=8,
            ).pack(side=tk.LEFT)
        ttk.Button(
            footer,
            text="닫기",
            command=self._close_capture_review_window,
        ).pack(side=tk.RIGHT)
        tk.Button(
            footer,
            text="검수본 저장",
            command=self._save_capture_review,
            bg=TEAL_PRIMARY,
            fg="#ffffff",
            activebackground=TEAL_PRIMARY_ACTIVE,
            activeforeground="#ffffff",
            relief=tk.FLAT,
            font=("Malgun Gothic", 9, "bold"),
            padx=14,
            pady=8,
        ).pack(side=tk.RIGHT, padx=(0, 8))

        window.bind("<Control-s>", self._save_capture_review_shortcut)
        window.grab_set()
        review_text.focus_set()

    def _on_capture_review_modified(self, event: tk.Event) -> None:
        editor = event.widget
        if not isinstance(editor, tk.Text) or not editor.edit_modified():
            return
        editor.edit_modified(False)
        highlight_source(editor)
        self.capture_review_dirty = (
            editor.get("1.0", "end-1c") != self.capture_review_initial_text
        )

    def _save_capture_review_shortcut(self, _event: tk.Event) -> str:
        self._save_capture_review()
        return "break"

    def _save_capture_review(self, *, close_after: bool = True) -> bool:
        if self.capture_review_id is None or self.capture_review_text is None:
            return False
        text = self.capture_review_text.get("1.0", "end-1c")
        try:
            record = self.capture_store.save_verified_text(
                self.capture_review_id,
                text,
                expected_updated_at=self.capture_review_expected_updated_at,
            )
        except CaptureConflictError as exc:
            messagebox.showerror(
                "캡처 검수 충돌",
                "다른 Ssokly 창에서 이 캡처가 먼저 변경되었습니다. "
                "검수 중인 텍스트는 그대로 유지했습니다.\n\n"
                f"{exc}",
                parent=self.capture_review_window,
            )
            self.status_var.set("다른 창의 캡처 변경과 충돌해 검수본을 저장하지 않았습니다.")
            return False
        except Exception as exc:
            messagebox.showerror(
                "검수본 저장 오류",
                "검수 중인 텍스트는 유지했습니다. 로컬 보관함에 저장하지 못했습니다."
                f"\n\n{exc}",
                parent=self.capture_review_window,
            )
            self.status_var.set("검수본을 저장하지 못했습니다.")
            return False

        self.capture_review_initial_text = text
        self.capture_review_dirty = False
        self.capture_review_expected_updated_at = record.updated_at
        self.capture_review_text.edit_modified(False)
        self._refresh_recent_captures(selected=record)
        workspace_action, comparison_candidate = (
            self._sync_saved_capture_review_to_workspace(record)
        )
        if workspace_action == "applied":
            self.status_var.set(
                "검수본을 로컬에 저장하고 현재 업무 원문에 안전하게 반영했습니다."
            )
        elif workspace_action == "compare":
            self.status_var.set(
                "검수본을 저장했습니다. 현재 업무의 교사 편집을 보호하기 위해 비교 창을 엽니다."
            )
        else:
            self.status_var.set(
                "캡처별 교사 검수 최종본을 로컬에 저장했습니다."
            )
        if close_after:
            self._destroy_capture_review_window()
        if comparison_candidate is not None:
            self.after_idle(
                lambda candidate=comparison_candidate: self._show_ocr_comparison(
                    candidate,
                    title="검수본 적용 비교",
                    description=(
                        "현재 업무 원문에 교사 편집이 있어 자동으로 바꾸지 않았습니다. "
                        "두 원문을 비교한 뒤 적용하세요."
                    ),
                    candidate_label="캡처별 교사 검수본",
                    apply_button_text="검수본 적용",
                    applied_status="교사 검수본을 현재 업무 원문에 적용했습니다.",
                )
            )
        return True

    def _sync_saved_capture_review_to_workspace(
        self,
        saved_record: CaptureRecord,
    ) -> tuple[str, Optional[str]]:
        review_format = self.capture_review_workspace_format
        if (
            review_format is None
            or self.capture_review_workspace_context is None
            or saved_record.id not in self.current_capture_ids
        ):
            return "none", None
        snapshot_records = self.capture_review_workspace_records
        if [record.id for record in snapshot_records] != self.current_capture_ids:
            return "none", None
        current_records = [
            saved_record if record.id == saved_record.id else record
            for record in snapshot_records
        ]
        if review_format in {"single", "compare_single"}:
            candidate = saved_record.effective_text
        else:
            candidate = self._capture_bundle_text(current_records)

        current_text = self.ocr_text.get("1.0", "end-1c")
        workspace_unchanged = (
            self.current_context_id == self.capture_review_workspace_context
            and self._source_revision == self.capture_review_workspace_revision
            and current_text == self.capture_review_workspace_text
        )
        if current_text == candidate:
            return "applied", None
        if workspace_unchanged and review_format in {"single", "bundle"}:
            self._replace_ocr_text(candidate, track_change=True)
            self._refresh_auto_title()
            return "applied", None
        return "compare", candidate

    def _clear_capture_review(self) -> None:
        if self.capture_review_id is None:
            return
        if not messagebox.askyesno(
            "검수 해제",
            "교사 검수 최종본을 해제하고 AI OCR 원문을 다시 사용할까요?\n"
            "원본 이미지와 AI OCR 원문은 삭제되지 않습니다.",
            icon=messagebox.WARNING,
            default=messagebox.NO,
            parent=self.capture_review_window,
        ):
            return
        try:
            record = self.capture_store.clear_verified_text(
                self.capture_review_id,
                expected_updated_at=self.capture_review_expected_updated_at,
            )
        except CaptureConflictError as exc:
            messagebox.showerror(
                "캡처 검수 충돌",
                "다른 Ssokly 창에서 이 캡처가 먼저 변경되어 검수본을 해제하지 않았습니다."
                f"\n\n{exc}",
                parent=self.capture_review_window,
            )
            return
        except Exception as exc:
            messagebox.showerror(
                "검수 해제 오류",
                f"검수본을 해제하지 못했습니다.\n\n{exc}",
                parent=self.capture_review_window,
            )
            return
        self._refresh_recent_captures(selected=record)
        workspace_action, comparison_candidate = (
            self._sync_saved_capture_review_to_workspace(record)
        )
        if workspace_action == "applied":
            self.status_var.set(
                "교사 검수본을 해제하고 현재 업무에도 AI OCR 원문을 반영했습니다."
            )
        elif workspace_action == "compare":
            self.status_var.set(
                "검수본을 해제했습니다. 현재 업무의 교사 편집을 보호하기 위해 비교 창을 엽니다."
            )
        else:
            self.status_var.set(
                "교사 검수본을 해제하고 저장된 AI OCR 원문으로 돌아갔습니다."
            )
        self._destroy_capture_review_window()
        if comparison_candidate is not None:
            self.after_idle(
                lambda candidate=comparison_candidate: self._show_ocr_comparison(
                    candidate,
                    title="검수 해제 반영 비교",
                    description=(
                        "현재 업무 원문에 교사 편집이 있어 자동으로 바꾸지 않았습니다. "
                        "AI OCR 원문으로 돌아갈지 비교해 결정하세요."
                    ),
                    candidate_label="저장된 AI OCR 원문",
                    apply_button_text="AI OCR 적용",
                    applied_status="AI OCR 원문을 현재 업무에 적용했습니다.",
                )
            )

    def _close_capture_review_window(self) -> bool:
        window = self.capture_review_window
        if window is None:
            return True
        try:
            if not window.winfo_exists():
                self._reset_capture_review_state()
                return True
        except tk.TclError:
            self._reset_capture_review_state()
            return True
        if self.capture_review_dirty:
            decision = messagebox.askyesnocancel(
                "검수본 저장",
                "수정한 검수본을 저장할까요?",
                icon=messagebox.WARNING,
                parent=window,
            )
            if decision is None:
                return False
            if decision and not self._save_capture_review(close_after=False):
                return False
        self._destroy_capture_review_window()
        return True

    def _destroy_capture_review_window(self) -> None:
        window = self.capture_review_window
        self._reset_capture_review_state()
        if window is None:
            return
        try:
            window.grab_release()
        except tk.TclError:
            pass
        try:
            if window.winfo_exists():
                window.destroy()
        except tk.TclError:
            pass

    def _reset_capture_review_state(self) -> None:
        self.capture_review_window = None
        self.capture_review_text = None
        self.capture_review_raw_text = None
        self.capture_review_id = None
        self.capture_review_initial_text = ""
        self.capture_review_dirty = False
        self.capture_review_expected_updated_at = None
        self.capture_review_workspace_context = None
        self.capture_review_workspace_revision = None
        self.capture_review_workspace_text = ""
        self.capture_review_workspace_format = None
        self.capture_review_workspace_records = []

    def _selected_captures(self) -> list[CaptureRecord]:
        if self.capture_inbox_tree is None:
            return []
        selected_ids = set(self.capture_inbox_tree.selection())
        records = [record for record in self.capture_records if record.id in selected_ids]
        return sorted(records, key=lambda record: (record.created_at, record.id))

    def _selected_capture(self) -> Optional[CaptureRecord]:
        records = self._selected_captures()
        return records[0] if records else None

    def _capture_bundle_text(
        self,
        records: list[CaptureRecord],
        *,
        prefer_verified: bool = True,
    ) -> str:
        sections = []
        for index, record in enumerate(records, start=1):
            header = f"[캡처 {index} · {record.created_at.astimezone():%Y-%m-%d %H:%M}]"
            text = record.effective_text if prefer_verified else record.ocr_text
            sections.append(f"{header}\n{text}")
        return "\n\n".join(sections)

    def _captures_have_text(self, records: list[CaptureRecord]) -> bool:
        unread = [record for record in records if not record.effective_text.strip()]
        if unread:
            messagebox.showinfo(
                "정밀 OCR 필요",
                f"선택한 캡처 중 {len(unread)}개는 아직 텍스트가 없습니다.\n"
                "먼저 '정밀 OCR'로 장별 원문을 읽어 주세요.",
            )
            return False
        failed_with_previous = [
            record
            for record in records
            if record.ocr_status == "failed"
            and not record.is_verified
            and record.ocr_text.strip()
        ]
        if failed_with_previous and not messagebox.askyesno(
            "직전 OCR 원문 사용",
            f"선택한 캡처 중 {len(failed_with_previous)}개는 마지막 재인식에 실패해 "
            "직전에 성공한 OCR 원문을 보존하고 있습니다.\n\n"
            "보존된 원문을 사용해 계속할까요?",
            icon=messagebox.WARNING,
            default=messagebox.NO,
        ):
            return False
        return True

    @staticmethod
    def _captures_are_active(records: list[CaptureRecord]) -> bool:
        return bool(records) and all(record.trashed_at is None for record in records)

    def _require_active_captures(self, records: list[CaptureRecord]) -> bool:
        if self._captures_are_active(records):
            return True
        messagebox.showinfo(
            "캡처 복원 필요",
            "휴지통의 캡처는 먼저 복원한 뒤 업무에 넣거나 정밀 OCR해 주세요.",
        )
        return False

    def bundle_selected_captures(self) -> None:
        records = self._selected_captures()
        if not records:
            messagebox.showinfo("캡처 보관함", "새 업무로 묶을 캡처를 선택해 주세요.")
            return
        if self._active_operation_id is not None:
            messagebox.showinfo("처리 중", "현재 작업이 끝난 뒤 캡처를 묶어 주세요.")
            return
        if not self._require_active_captures(records):
            return
        if not self._captures_have_text(records):
            return
        if not self._prepare_to_leave_current("캡처 묶음 새 업무"):
            return

        combined_text = self._capture_bundle_text(records)
        self._close_recent_window()
        self._start_new_workspace(
            source_kind="capture",
            source_name=f"캡처 {len(records)}개",
            capture_path=records[0].path,
            capture_ids=[record.id for record in records],
        )
        self._replace_ocr_text(combined_text, track_change=True)
        first_title = self._capture_ocr_preview(records[0])
        self._set_title_programmatically(
            self._clean_title(first_title) or self._fallback_task_title()
        )
        self.notebook.select(self.source_tab)
        self.status_var.set(
            f"캡처 {len(records)}개의 장별 OCR 원문을 시간순으로 묶었습니다. 원본과 대조해 주세요."
        )

    def add_selected_captures_to_current_task(self) -> None:
        records = self._selected_captures()
        if not records:
            messagebox.showinfo("캡처 보관함", "현재 업무에 추가할 캡처를 선택해 주세요.")
            return
        if self._active_operation_id is not None:
            messagebox.showinfo("처리 중", "현재 작업이 끝난 뒤 캡처를 추가해 주세요.")
            return
        if not self._require_active_captures(records):
            return
        existing_capture_ids = set(self.current_capture_ids)
        records = [
            record for record in records if record.id not in existing_capture_ids
        ]
        if not records:
            messagebox.showinfo(
                "이미 추가된 캡처",
                "선택한 캡처는 모두 현재 업무에 이미 포함되어 있습니다.",
            )
            return
        if not self._captures_have_text(records):
            return

        existing = self.ocr_text.get("1.0", "end-1c")
        addition = self._capture_bundle_text(records)
        combined = f"{existing}\n\n{addition}" if existing else addition
        self._replace_ocr_text(combined, track_change=True)
        self.current_capture_ids = list(
            dict.fromkeys(
                [*self.current_capture_ids, *(record.id for record in records)]
            )
        )
        if self.current_capture_path is None:
            self.current_capture_path = records[0].path
        if self.current_task_id is not None and not self.save_current_task(show_success=False):
            messagebox.showerror(
                "캡처 추가 저장 오류",
                "캡처 원문은 화면에 유지됐지만 업무 보관함에는 저장하지 못했습니다.",
            )
            return
        self._close_recent_window()
        self.notebook.select(self.source_tab)
        self.status_var.set(
            f"현재 업무에 캡처 {len(records)}개의 OCR 원문을 추가했습니다."
        )

    def reread_selected_captures(self) -> None:
        records = self._selected_captures()
        if not records:
            messagebox.showinfo("캡처 보관함", "정밀 OCR로 다시 읽을 캡처를 선택해 주세요.")
            return
        if self._active_operation_id is not None:
            messagebox.showinfo("처리 중", "현재 작업이 끝난 뒤 다시 읽어 주세요.")
            return
        if not self._require_active_captures(records):
            return
        self._run_capture_batch_ocr(records)

    def trash_selected_captures(self) -> None:
        records = self._selected_captures()
        if not records:
            messagebox.showinfo("캡처 보관함", "휴지통으로 옮길 캡처를 선택해 주세요.")
            return
        linked = [record for record in records if record.linked_task_ids]
        if linked:
            messagebox.showinfo(
                "연결된 캡처 보호",
                "업무에 연결된 캡처는 휴지통으로 옮길 수 없습니다.\n"
                "업무를 삭제하거나 연결을 해제한 뒤 정리해 주세요.",
            )
            return
        current_paths = (
            {self.current_capture_path.resolve(strict=False)}
            if self.current_capture_path is not None
            else set()
        )
        current_ids = set(self.current_capture_ids)
        if any(
            record.id in current_ids
            or record.path.resolve(strict=False) in current_paths
            for record in records
        ):
            messagebox.showinfo(
                "사용 중인 캡처",
                "현재 업무가 사용하는 캡처는 다른 업무로 이동한 뒤 정리해 주세요.",
            )
            return
        if not messagebox.askyesno(
            "캡처 휴지통",
            f"선택한 미분류 캡처 {len(records)}개를 휴지통으로 옮길까요?\n"
            "원본 첨부 파일은 삭제되지 않으며 캡처함에서 복원할 수 있습니다.",
            icon=messagebox.WARNING,
            default=messagebox.NO,
        ):
            return

        try:
            self.capture_store.trash([record.id for record in records])
        except Exception as exc:
            messagebox.showerror(
                "캡처 정리 오류",
                f"선택한 캡처를 휴지통으로 옮기지 못했습니다.\n\n{exc}",
            )
            self.status_var.set("캡처를 정리하지 못했습니다.")
            return
        self._refresh_recent_captures()
        self.status_var.set("선택한 미분류 캡처를 휴지통으로 옮겼습니다.")

    def restore_selected_captures(self) -> None:
        records = self._selected_captures()
        if not records:
            messagebox.showinfo("캡처 보관함", "복원할 캡처를 선택해 주세요.")
            return
        try:
            self.capture_store.restore([record.id for record in records])
        except Exception as exc:
            messagebox.showerror("캡처 복원 오류", f"캡처를 복원하지 못했습니다.\n\n{exc}")
            return
        self._refresh_recent_captures()
        self.status_var.set("선택한 캡처를 복원했습니다.")

    def open_capture_folder(self) -> None:
        try:
            os.startfile(str(self.capture_store.directory))
            self.status_var.set("캡처 보관함 원본 폴더를 열었습니다.")
        except OSError as exc:
            messagebox.showerror(
                "폴더 열기 오류",
                f"캡처 보관함 원본 폴더를 열지 못했습니다.\n\n{exc}",
            )
            self.status_var.set("캡처 보관함 폴더를 열지 못했습니다.")

    def _refresh_recent_captures(self, selected: Optional[CaptureRecord] = None) -> None:
        filter_value = self.capture_inbox_filter_var.get()
        try:
            unclassified_count = self.capture_store.count("unclassified")
        except Exception:
            unclassified_count = 0
        self.recent_count_var.set(f"캡처함 · 미분류 {unclassified_count}")

        if self.capture_inbox_tree is None or not self.capture_inbox_tree.winfo_exists():
            self.capture_records = []
            return
        try:
            records = self.capture_store.search(
                query=self.capture_search_var.get(),
                link_filter="all" if filter_value == "trash" else filter_value,
                trashed=filter_value == "trash",
                limit=CAPTURE_INBOX_PAGE_LIMIT,
            )
        except Exception:
            records = []
        self._update_capture_inbox_filter_buttons()
        self._populate_capture_inbox_records(
            records,
            selected_ids=[selected.id] if selected is not None else None,
        )

    def _populate_capture_inbox_records(
        self,
        records: list[CaptureRecord],
        *,
        selected_ids: Optional[list[str]] = None,
        count_label: Optional[str] = None,
    ) -> None:
        if self.capture_inbox_tree is None or not self.capture_inbox_tree.winfo_exists():
            return
        self.capture_records = list(records)
        self._cancel_capture_thumbnail_load()
        self._capture_thumbnail_generation += 1
        generation = self._capture_thumbnail_generation
        self.capture_thumbnail_photos.clear()
        for item in self.capture_inbox_tree.get_children():
            self.capture_inbox_tree.delete(item)
        for record in self.capture_records:
            self.capture_inbox_tree.insert(
                "",
                tk.END,
                iid=record.id,
                text=self._capture_ocr_preview(record),
                values=(
                    record.created_at.astimezone().strftime("%m/%d %H:%M"),
                    self._capture_state_label(record),
                ),
            )

        if self.capture_selected_count_var is not None:
            limit_hint = (
                "최대 "
                if len(self.capture_records) >= CAPTURE_INBOX_PAGE_LIMIT
                else ""
            )
            self.capture_selected_count_var.set(
                count_label
                or f"{limit_hint}{len(self.capture_records)}개 표시 · 0개 선택"
            )

        if not self.capture_records:
            self.preview_photo = None
            if self.preview_label is not None:
                self.preview_label.configure(image="", text="이 분류에는 캡처가 없습니다.")
            self._set_capture_preview_text("")
            self._render_capture_inbox_actions()
            return

        valid_selected_ids = [
            capture_id
            for capture_id in (selected_ids or [self.capture_records[0].id])
            if self.capture_inbox_tree.exists(capture_id)
        ]
        if valid_selected_ids:
            self.capture_inbox_tree.selection_set(*valid_selected_ids)
            selected_id = valid_selected_ids[0]
            self.capture_inbox_tree.focus(selected_id)
            chosen = next(
                record for record in self.capture_records if record.id == selected_id
            )
            self._show_capture_preview(chosen)
            if self.capture_selected_count_var is not None and count_label is None:
                limit_hint = (
                    "최대 "
                    if len(self.capture_records) >= CAPTURE_INBOX_PAGE_LIMIT
                    else ""
                )
                self.capture_selected_count_var.set(
                    f"{limit_hint}{len(self.capture_records)}개 표시 · "
                    f"{len(valid_selected_ids)}개 선택"
                )
        self._capture_thumbnail_after_id = self.after_idle(
            lambda: self._load_next_capture_thumbnail(generation, 0)
        )
        self._render_capture_inbox_actions()

    def _on_recent_capture_selected(self, _event: tk.Event) -> None:
        records = self._selected_captures()
        if records:
            self._show_capture_preview(records[0])
        if self.capture_selected_count_var is not None:
            display_count = len(self.capture_records)
            limit_hint = "최대 " if display_count >= CAPTURE_INBOX_PAGE_LIMIT else ""
            self.capture_selected_count_var.set(
                f"{limit_hint}{display_count}개 표시 · {len(records)}개 선택"
            )
        self._render_capture_inbox_actions()

    def _show_capture_preview(self, record: CaptureRecord) -> None:
        if self.preview_label is None:
            return
        if record.is_verified:
            review_note = "[교사 검수 최종본]"
            if record.review_status == "ocr_updated":
                review_note += " · 검수 뒤 AI OCR이 갱신됨"
            detail_parts = [review_note, record.verified_text]
            if record.ocr_text or record.ocr_error:
                detail_parts.extend(["[AI OCR 원문 · 비교용]", record.ocr_text])
        else:
            detail_parts = ["[AI OCR 원문 · 검수 필요]", record.ocr_text]
        if record.ocr_error:
            detail_parts.extend(["[마지막 OCR 오류]", record.ocr_error])
        detail = "\n".join(detail_parts)
        try:
            image = self.capture_store.load(record)
            image.thumbnail((450, 330), Image.Resampling.LANCZOS)
            self.preview_photo = ImageTk.PhotoImage(image)
            self.preview_label.configure(image=self.preview_photo, text="")
            self._set_capture_preview_text(detail)
        except OSError:
            self.preview_photo = None
            self.preview_label.configure(image="", text="미리보기를 열 수 없습니다.")
            self._set_capture_preview_text(detail)

    def _set_capture_preview_text(self, text: str) -> None:
        if self.capture_preview_text is None:
            return
        self.capture_preview_text.configure(state=tk.NORMAL)
        self.capture_preview_text.delete("1.0", tk.END)
        self.capture_preview_text.insert("1.0", text)
        self.capture_preview_text.configure(state=tk.DISABLED)

    def _load_next_capture_thumbnail(self, generation: int, index: int) -> None:
        self._capture_thumbnail_after_id = None
        if (
            generation != self._capture_thumbnail_generation
            or self.capture_inbox_tree is None
            or not self.capture_inbox_tree.winfo_exists()
            or index >= len(self.capture_records)
        ):
            return
        record = self.capture_records[index]
        try:
            image = self.capture_store.load(record)
            image.thumbnail((72, 48), Image.Resampling.LANCZOS)
            photo = ImageTk.PhotoImage(image)
            self.capture_thumbnail_photos[record.id] = photo
            if self.capture_inbox_tree.exists(record.id):
                self.capture_inbox_tree.item(record.id, image=photo)
        except OSError:
            pass
        self._capture_thumbnail_after_id = self.after_idle(
            lambda: self._load_next_capture_thumbnail(generation, index + 1)
        )

    def _cancel_capture_thumbnail_load(self) -> None:
        if self._capture_thumbnail_after_id is None:
            return
        try:
            self.after_cancel(self._capture_thumbnail_after_id)
        except tk.TclError:
            pass
        self._capture_thumbnail_after_id = None

    def _render_capture_inbox_actions(self) -> None:
        records = self._selected_captures()
        processing = self._active_operation_id is not None
        trash_view = self.capture_inbox_filter_var.get() == "trash"
        selected_state = tk.NORMAL if records and not processing else tk.DISABLED
        active_buttons = (
            self.reopen_button,
            self.add_capture_button,
            self.review_capture_button,
            self.reread_capture_button,
        )
        if trash_view:
            for widget in active_buttons:
                if widget is not None:
                    widget.pack_forget()
            if self.delete_capture_button is not None:
                self.delete_capture_button.pack_forget()
        else:
            if self.delete_capture_button is not None:
                self.delete_capture_button.configure(
                    text="휴지통으로",
                    command=self.trash_selected_captures,
                )
                if not self.delete_capture_button.winfo_manager():
                    self.delete_capture_button.pack(
                        side=tk.LEFT,
                        padx=(0, 6),
                        before=self.open_capture_folder_button,
                    )
            for widget in active_buttons:
                if (
                    widget is not None
                    and not widget.winfo_manager()
                    and self.delete_capture_button is not None
                ):
                    widget.pack(
                        side=tk.LEFT,
                        padx=(0, 6),
                        before=self.delete_capture_button,
                    )
                self._set_widget_state(
                    widget,
                    tk.NORMAL if records and not processing else tk.DISABLED,
                )
            self._set_widget_state(
                self.delete_capture_button,
                tk.NORMAL
                if records
                and not processing
                and not any(record.linked_task_ids for record in records)
                else tk.DISABLED,
            )
            self._set_widget_state(
                self.review_capture_button,
                tk.NORMAL
                if len(records) == 1 and not processing
                else tk.DISABLED,
            )
        if self.restore_capture_button is not None:
            if trash_view:
                if not self.restore_capture_button.winfo_manager():
                    self.restore_capture_button.pack(
                        side=tk.LEFT,
                        padx=(0, 6),
                        before=self.open_capture_folder_button,
                    )
                self._set_widget_state(self.restore_capture_button, selected_state)
            else:
                self.restore_capture_button.pack_forget()

    def _bind_change_tracking(self) -> None:
        self.task_title_var.trace_add("write", self._on_title_changed)
        self.task_search_var.trace_add("write", self._on_task_search_changed)
        self.capture_search_var.trace_add("write", self._on_capture_search_changed)
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

    def _on_capture_search_changed(self, *_args: object) -> None:
        self._cancel_capture_search_refresh()
        if self._closing or self.recent_window is None:
            return
        self._capture_search_after_id = self.after(
            TASK_SEARCH_DELAY_MS,
            self._apply_capture_search,
        )

    def _apply_capture_search(self) -> None:
        self._capture_search_after_id = None
        self._refresh_recent_captures()

    def _cancel_capture_search_refresh(self) -> None:
        if self._capture_search_after_id is None:
            return
        try:
            self.after_cancel(self._capture_search_after_id)
        except tk.TclError:
            pass
        self._capture_search_after_id = None

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
                highlight_source(widget)
                self._queue_source_version()
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
        capture_ids: Optional[list[str]] = None,
    ) -> None:
        self._cancel_autosave()
        self.current_context_id = uuid4().hex
        self.document_id = uuid4().hex
        self._current_artifact = None
        self.current_task_id = None
        self.current_task_updated_at = None
        self.current_task_status = "open"
        self.current_source_kind = source_kind
        self.current_source_name = source_name
        self.current_source_path = source_path.resolve(strict=False) if source_path else None
        self.current_capture_path = capture_path.resolve(strict=False) if capture_path else None
        self.current_capture_ids = list(dict.fromkeys(capture_ids or []))
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
        self.card_panel.refresh()
        self._refresh_artifact_status()
        self.transfer_status.set('아직 외부 전송하지 않음 · 원문 편집·저장은 로컬 처리')
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
            self._sync_work_source()
            if not self._archive_visible_draft():
                return False
            if self.current_task_id is None:
                record = self.task_store.create(
                    task_id=self.document_id,
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

        capture_link_error: Optional[str] = None
        if self.current_capture_ids:
            try:
                self.capture_store.link_to_task(
                    self.current_capture_ids,
                    record.id,
                )
            except Exception as exc:
                capture_link_error = str(exc) or "캡처 연결 중 알 수 없는 오류가 발생했습니다."

        self.current_task_id = record.id
        self.current_task_updated_at = record.updated_at
        self.current_task_status = record.status
        self.current_source_kind = record.source_kind
        self.current_source_name = record.source_name
        self.current_source_path = Path(record.source_path) if record.source_path else None
        self.current_capture_path = Path(record.capture_path) if record.capture_path else None
        self.current_output_mode = record.output_mode
        self.current_dirty = capture_link_error is not None
        self._autosave_failed = capture_link_error is not None
        self.title_is_auto = False
        self._refresh_task_library(selected_id=record.id)
        self._refresh_recent_captures()
        self._render_workspace_state()
        if capture_link_error:
            self.status_var.set(
                "업무 내용은 저장했지만 캡처 원본 연결을 완료하지 못했습니다."
            )
            if show_success:
                messagebox.showwarning(
                    "캡처 원본 연결 오류",
                    "업무 원문과 실행안은 저장됐지만 캡처 원본 연결에 실패했습니다.\n"
                    "현재 캡처는 캡처함에 그대로 보존됩니다. 저장 버튼으로 다시 시도해 주세요.\n\n"
                    f"{capture_link_error}",
                )
        elif show_success:
            self.status_var.set("업무 보관함에 저장했습니다. 이후 수정은 자동 저장됩니다.")
        return capture_link_error is None

    def _capture_ids_for_task(
        self,
        task_id: str,
        *,
        raise_errors: bool = False,
    ) -> list[str]:
        try:
            records = self.capture_store.captures_for_task(task_id)
        except Exception:
            if raise_errors:
                raise
            return []
        return [record.id for record in records]

    def _prepare_to_leave_current(self, reason: str) -> bool:
        if self.card_panel.has_unsaved_changes:
            messagebox.showinfo('업무 카드 편집 중', '이동·종료 전에 업무 카드의 수정 저장을 눌러 주세요. 저장 실패 시 편집값을 유지합니다.')
            self.notebook.select(self.cards_tab)
            return False
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
        self.stream_preview.pack_forget()
        if self._active_operation_cancel_event is not None:
            self._active_operation_cancel_event.set()
        self._active_operation_id = None
        self._active_operation_context = None
        self._active_operation_kind = None
        self._active_operation_revisions = None
        self._active_operation_cancel_event = None
        self._discard_held_worker_results()
        self._hide_operation_progress()
        self._render_workspace_state()

    def _on_close(self) -> None:
        if self._closing:
            return
        if not self._close_capture_review_window():
            return
        if not self._prepare_to_leave_current("앱 종료"):
            return

        self._closing = True
        if self._source_sync_after_id is not None:
            self.after_cancel(self._source_sync_after_id)
            self._source_sync_after_id = None
        self._cancel_autosave()
        self._cancel_task_search_refresh()
        if not self._flush_window_settings():
            messagebox.showwarning(
                "창 설정 저장 오류",
                "컴팩트·항상 위·투명도 설정을 저장하지 못했습니다.\n"
                "업무 원문과 실행안에는 영향이 없습니다.",
            )
        self._active_operation_id = None
        self._active_operation_context = None
        self._active_operation_kind = None
        self._active_operation_revisions = None
        if self._active_operation_cancel_event is not None:
            self._active_operation_cancel_event.set()
        self._active_operation_cancel_event = None
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
        self.document_id = record.id
        self._current_artifact = None
        self.current_task_id = record.id
        self.current_task_updated_at = record.updated_at
        self.current_task_status = record.status
        self.current_source_kind = record.source_kind
        self.current_source_name = record.source_name
        self.current_source_path = Path(record.source_path) if record.source_path else None
        self.current_capture_path = Path(record.capture_path) if record.capture_path else None
        self.current_capture_ids = self._capture_ids_for_task(record.id)
        self.current_output_mode = record.output_mode
        self._source_revision = 0
        self._result_revision = 0
        self.current_dirty = False
        self._autosave_failed = False
        self.title_is_auto = False
        self._set_title_programmatically(record.title)
        self._replace_ocr_text(record.source_text)
        self._replace_result_text(record.analysis_text)
        try:
            self._sync_work_source()
            artifact_id = self.workspace_state.get(self.document_id).get('artifact_id')
            self._current_artifact = next((item for item in self.work_cards.list_artifacts(self.document_id)
                                           if item['id'] == artifact_id), None)
            policy = self._transfer_policy()
            self.transfer_status.set('이전 가림 정책 유지 · 후속 전송도 선택 사본만 사용' if policy and policy.redacted else 'AI 버튼 실행 시 선택 텍스트를 OpenAI로 전송')
        except Exception:
            self.transfer_status.set('저장된 버전·정책 확인 필요 · 전송 시 재확인')
        self.card_panel.refresh()
        self._refresh_artifact_status()
        if self.task_tree.exists(record.id):
            self.task_tree.selection_set(record.id)
            self.task_tree.focus(record.id)
        self.notebook.select(self.result_tab if record.analysis_text.strip() else self.source_tab)
        if self.work_cards.list_cards(self.document_id):
            self.notebook.select(self.cards_tab)
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
            "업무 전용 캡처 사본만 삭제되며 캡처 보관함 원본과 외부 파일은 유지됩니다.",
            icon=messagebox.WARNING,
            default=messagebox.NO,
        ):
            return

        try:
            linked_capture_ids = self._capture_ids_for_task(
                record.id,
                raise_errors=True,
            )
        except Exception as exc:
            messagebox.showerror(
                "업무 삭제 준비 오류",
                "캡처 보관함 연결을 확인하지 못해 업무를 삭제하지 않았습니다.\n\n"
                f"{exc}",
            )
            return
        if linked_capture_ids:
            try:
                self.capture_store.unlink_task(linked_capture_ids, record.id)
            except Exception as exc:
                messagebox.showerror(
                    "업무 삭제 준비 오류",
                    "캡처 원본 연결을 안전하게 해제하지 못해 업무를 삭제하지 않았습니다.\n\n"
                    f"{exc}",
                )
                return
        try:
            deleted = self.task_store.delete(record.id)
        except Exception as exc:
            relink_error: Optional[str] = None
            try:
                task_still_exists = self.task_store.get(record.id) is not None
            except Exception:
                task_still_exists = True
            if linked_capture_ids and task_still_exists:
                try:
                    self.capture_store.link_to_task(linked_capture_ids, record.id)
                except Exception as relink_exc:
                    relink_error = str(relink_exc)
            detail = f"업무를 삭제하지 못했습니다.\n\n{exc}"
            if relink_error:
                detail += (
                    "\n\n캡처 연결 복구에도 실패했습니다. 캡처 원본은 보관함에 유지됩니다.\n"
                    f"{relink_error}"
                )
            messagebox.showerror("업무 삭제 오류", detail)
            self._refresh_recent_captures()
            return
        if not deleted:
            self._refresh_task_library()
            self._refresh_recent_captures()
            return

        if record.id == self.current_task_id:
            self._cancel_autosave()
            self._invalidate_active_operation()
            self._start_new_workspace(source_kind="manual", source_name="직접 입력")
        self._refresh_task_library()
        self._refresh_recent_captures()
        self.status_var.set(
            "선택한 업무를 삭제했습니다. 캡처 원본과 외부 파일은 유지됩니다."
        )

    def reread_current_source(self) -> None:
        if self._active_operation_id is not None:
            messagebox.showinfo("처리 중", "현재 작업이 끝난 뒤 원문을 다시 읽어 주세요.")
            return

        capture_records = self._current_capture_records()
        if capture_records:
            if len(capture_records) > 1:
                self._run_capture_batch_ocr(
                    capture_records,
                    apply_mode="review_bundle",
                )
                return
            record = capture_records[0]
            try:
                image = self.capture_store.load(record)
            except OSError as exc:
                messagebox.showerror(
                    "캡처 원본 오류",
                    f"캡처 보관함의 원본을 열지 못했습니다.\n\n{exc}",
                )
                return
            self._run_ocr(
                image,
                capture_id=record.id,
                apply_mode="review",
            )
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
            self._run_ocr(image, apply_mode="review")
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
                self._run_ocr(image, apply_mode="review")
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
        capture_records = self._current_capture_records()
        if len(capture_records) > 1:
            self.show_recent_captures()
            self.capture_search_var.set("")
            self._cancel_capture_search_refresh()
            self.capture_inbox_filter_var.set("all")
            self._update_capture_inbox_filter_buttons()
            self._populate_capture_inbox_records(
                capture_records,
                selected_ids=[record.id for record in capture_records],
                count_label=f"현재 업무 원본 {len(capture_records)}개",
            )
            self.status_var.set(
                f"현재 업무에 연결된 원본 캡처 {len(capture_records)}개를 표시했습니다."
            )
            return
        if capture_records:
            record = capture_records[0]
            self._show_source_preview(record.path, self.task_title_var.get())
            return

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

    def _current_capture_records(self) -> list[CaptureRecord]:
        records: list[CaptureRecord] = []
        for capture_id in self.current_capture_ids:
            try:
                record = self.capture_store.get(capture_id)
            except Exception:
                continue
            if record is not None and record.trashed_at is None and record.path.exists():
                records.append(record)
        return records

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
        highlight_source(self.ocr_text)
        if track_change:
            self._queue_source_version()

    def _queue_source_version(self):
        if self._source_sync_after_id is not None:
            self.after_cancel(self._source_sync_after_id)
        self._source_sync_after_id = self.after(AUTOSAVE_DELAY_MS,
            lambda context=self.current_context_id: self._refresh_source_version(context))
        if self._current_artifact:
            self.artifact_status.set('원문이 변경됨 · 이전 초안을 보존하고 있습니다.')

    def _replace_result_text(self, text: str, track_change: bool = False) -> None:
        self._replace_text_widget(self.result_text, text, track_change)
        for i, line in enumerate(text.splitlines(), 1):
            tag = "heading" if line.startswith("## ") else "title" if line.startswith("# ") else "recommendation" if "[추천 실행]" in line else None
            if tag:
                self.result_text.tag_add(tag, f"{i}.0", f"{i}.end")

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
        has_source = bool(self.current_capture_ids) or (
            self.current_capture_path is not None
            if self.current_source_kind == "capture"
            else self.current_source_path is not None
            if self.current_source_kind == "file"
            else False
        )
        capture_count = len(self.current_capture_ids)
        self.open_source_button.configure(
            text=f"원본 보기 ({capture_count})" if capture_count > 1 else "원본 보기",
            state=tk.NORMAL if has_source else tk.DISABLED,
        )
        self.reread_source_button.configure(
            text="정밀 재인식",
            state=tk.NORMAL if has_source and not processing else tk.DISABLED
        )

        self._set_widget_state(self.ocr_text, tk.NORMAL)
        self._set_widget_state(self.result_text, tk.NORMAL)

        self._render_capture_inbox_actions()
