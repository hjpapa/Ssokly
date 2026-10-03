"""Capture-first desktop. Pages are the editable source; AI outputs are separate.

The window is assembled from mixins: library/trash/organize actions (desk_library),
pane layout and read-only views (desk_layout), capture/import/OCR/AI jobs (desk_jobs).
"""
import os
from pathlib import Path
import queue
import sqlite3
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from services.app_paths import default_app_data_dir
from services.capture_location import CaptureLocation
from services.capture_store import CaptureStore
from services.document_library import DocumentLibrary, LibraryConflictError
from services.diagnostics import configure_local_logging
from services.source_review import highlight_source
from ui.desk_widgets import InlineTableView, ResponsivePanedWindow, ThumbnailCache, ZoomImageView
from ui.desk_theme import apply_theme
from ui.job_progress import JobProgress
from ui.desk_jobs import JobsMixin
from ui.desk_layout import LayoutMixin
from ui.desk_library import LibraryMixin
from ui.desk_text import fingerprint  # re-exported for callers of ui.capture_desk.fingerprint
from services.diagnostics import log_failure


class CaptureDeskApp(LibraryMixin, LayoutMixin, JobsMixin, tk.Tk):
    """All widget and persistence changes run on Tk's thread, never a worker."""
    def __init__(self, *, library=None, app_data_dir=None):
        root = Path(app_data_dir) if app_data_dir is not None else default_app_data_dir()
        configure_local_logging(library.app_data_dir if library is not None else root)
        self._capture_location = CaptureLocation(root) if library is None and app_data_dir is None else None
        if library is None:
            captures = CaptureStore(self._capture_location.resolve()) if self._capture_location else None
            library = DocumentLibrary(root, capture_store=captures)
        self.library = library
        super().__init__()
        self.document = None
        self.page = None
        self.page_records = []
        self.output = None
        self._source_dirty = False
        self._output_dirty = False
        self._loading = False
        self._autosave_id = None
        self._refresh_id = None
        self._source_revision = 0
        self._output_revision = 0
        self._closing = False
        self._jobs = {}
        self._results = queue.Queue()
        self._library_requested = True
        self._compact_library = False
        self._layout_compact = None
        self._layout_id = None
        self._reset_main_sash = True
        self._reset_work_sash = True
        self._visible_count = 250
        self.title('Ssokly · 캡처와 텍스트')
        self.geometry('1280x800')
        self.minsize(720, 680)
        apply_theme(self)
        self._build_ui()
        self.thumbnails = ThumbnailCache(self)
        self.protocol('WM_DELETE_WINDOW', self.close)
        self.bind('<Control-s>', lambda _event: self._save_shortcut())
        self.bind('<F1>', lambda _event: self.show_help())
        self.bind('<Configure>', self._on_resize)
        self.refresh_library()
        self._poll_id = self.after(80, self._poll_results)

    def _build_ui(self):
        header = ttk.Frame(self, padding=(12, 10))
        self._header = header
        header.pack(fill='x')
        self._header_logo = ttk.Label(header, text=' Ssokly', image=self.brand_icon, compound='left', font=('Segoe UI', 16, 'bold'))
        self._header_logo.grid(row=0, column=0, sticky='w', padx=(0, 14))
        self._header_primary = ttk.Frame(header)
        self._header_primary.grid(row=0, column=1, sticky='w')
        self._header_secondary = ttk.Frame(header)
        self._header_secondary.grid(row=0, column=2, sticky='e')
        header.columnconfigure(1, weight=1)
        self.capture_button = ttk.Button(self._header_primary, text='새 캡처', width=-6, command=self.capture_new, style='Primary.TButton')
        self.capture_button.pack(side='left', padx=3)
        self.capture_copy_button = ttk.Button(self._header_primary, text='캡처 후 복사', width=-11,
            command=self.capture_and_copy, style='Primary.TButton')
        self.capture_copy_button.pack(side='left', padx=3)
        self.add_button = ttk.Button(self._header_primary, text='+ 페이지', width=-7, command=self.capture_page, style='Desk.TButton')
        self.add_button.pack(side='left', padx=3)
        ttk.Button(self._header_primary, text='파일 열기', width=-7, command=self.open_file, style='Desk.TButton').pack(side='left', padx=3)
        library_actions = ttk.Frame(self._header_secondary)
        library_actions.pack(side='right')
        self.library_button = ttk.Button(library_actions, text='보관함', width=-6, command=self.toggle_library)
        self.library_button.pack(fill='x')
        self.help_button = ttk.Button(library_actions, text='? 사용 설명서', command=self.show_help)
        self.help_button.pack(fill='x', pady=(3, 0))
        menu = tk.Menu(self, tearoff=False)
        menu.add_command(label='새 텍스트 문서', command=self.new_text_document)
        menu.add_command(label='캡처 관리 · 전체 이미지', command=self.show_capture_manager)
        menu.add_command(label='캡처 저장 폴더 설정', command=self.show_capture_location)
        menu.add_command(label='기존 기록 보기', command=self.show_legacy_records)
        menu.add_command(label='원래 인식 내용 보기', command=self.show_original_text)
        menu.add_command(label='원본 문서 파일 열기', command=self.open_source_file)
        menu.add_command(label='이전 AI 결과', command=self.show_output_history)
        menu.add_command(label='저장된 최신값 다시 열기…', command=self.reload_saved)
        menu.add_separator()
        menu.add_command(label='현재 문서 휴지통 / 복원', command=self.toggle_trash)
        menu.add_separator()
        menu.add_command(label='같은 라벨·메모 문서의 쪽 합치기', command=self.show_merge_dialog)
        menu.add_command(label='선택한 쪽을 새 문서로 분리', command=self.show_split_dialog)
        more = ttk.Menubutton(self._header_secondary, text='더보기', menu=menu)
        more.pack(side='right', padx=6)

        options = ttk.Frame(self, padding=(12, 0, 12, 8))
        self._capture_options = options
        options.pack(fill='x')
        consent = self.library.get_setting('automatic_ocr_consent', False) is True
        mode = self.library.get_setting('capture_mode', '보관만')
        if mode not in ('보관만', '가리고 읽기', '자동 인식') or (mode == '자동 인식' and not consent):
            mode = '보관만'
        self.capture_mode = tk.StringVar(value=mode)
        ttk.Label(options, text='캡처 후').pack(side='left')
        picker = ttk.Combobox(options, textvariable=self.capture_mode, values=('자동 인식', '보관만', '가리고 읽기'),
                              width=12, state='readonly')
        picker.pack(side='left', padx=7)
        picker.bind('<<ComboboxSelected>>', self._capture_mode_changed)
        self._transfer_hint = ttk.Label(options, text='이미지는 PC에 보관 · AI 실행 시 서버를 통해 OpenAI 전송', foreground='#667085')
        self._transfer_hint.pack(side='left')
        self.job_progress = JobProgress(self, self.cancel_jobs)
        self.cancel_button = self.job_progress.cancel_button

        bottom = ttk.Frame(self, padding=(12, 6))
        bottom.pack(side='bottom', fill='x')
        self.status = tk.StringVar(value='캡처하거나 파일을 열어 시작하세요.')
        self.save_state = tk.StringVar(value='')
        self.status_label = ttk.Label(bottom, textvariable=self.status, wraplength=750)
        self.status_label.pack(side='left', fill='x', expand=True)
        ttk.Label(bottom, textvariable=self.save_state, foreground='#177568').pack(side='right', padx=8)
        self.main_split = ttk.Panedwindow(self, orient='horizontal')
        self.main_split.pack(fill='both', expand=True, padx=12, pady=(0, 8))
        self.main_split.bind('<Configure>', self._schedule_layout)
        self.main_split.bind('<ButtonRelease-1>', self._schedule_layout)
        self.library_panel = ttk.Frame(self.main_split, width=250)
        self.main_split.add(self.library_panel, weight=0)
        ttk.Label(self.library_panel, text='자료 라이브러리', font=('Malgun Gothic', 11, 'bold')).pack(anchor='w', pady=(0, 5))
        self.search_hint = ttk.Label(self.library_panel, text='자료 검색 · 제목/본문/라벨/메모',
                                    foreground='#475467', wraplength=220)
        self.search_hint.pack(anchor='w')
        self.query = tk.StringVar()
        search_bar = ttk.Frame(self.library_panel)
        search_bar.pack(fill='x', pady=(3, 5))
        # Reserve the action first: the entry must shrink when the sidebar narrows.
        # A negative width is a minimum, so Korean glyphs still fit at high DPI.
        self.search_button = ttk.Button(search_bar, text='검색', width=-5, command=self._run_search)
        self.search_button.pack(side='right', padx=(5, 0))
        self.search_entry = ttk.Entry(search_bar, textvariable=self.query, width=1)
        self.search_entry.pack(side='left', fill='x', expand=True)
        self.search_entry.bind('<Return>', lambda _event: self._run_search())
        self.query.trace_add('write', self._search_changed)
        self.label_filter = tk.StringVar(value='전체 라벨')
        self.label_picker = ttk.Combobox(self.library_panel, textvariable=self.label_filter,
                                        values=('전체 라벨',), state='readonly')
        self.label_picker.pack(fill='x', pady=(0, 5))
        self.label_picker.bind('<<ComboboxSelected>>', lambda _event: self.refresh_library())
        organize = ttk.Menubutton(self.library_panel, text='자료 정리 · 라벨 / 쪽')
        organize_menu = tk.Menu(organize, tearoff=False)
        organize_menu.add_command(label='라벨 관리 · 이름 변경', command=self.manage_labels)
        organize_menu.add_command(label='쪽 합치기', command=self.show_merge_dialog)
        organize_menu.add_command(label='쪽 분리', command=self.show_split_dialog)
        organize.configure(menu=organize_menu)
        organize.pack(fill='x', pady=(0, 5))
        self.library_count = tk.StringVar(value='')
        ttk.Label(self.library_panel, textvariable=self.library_count, foreground='#667085').pack(anchor='w')
        self.show_trash = tk.BooleanVar(value=False)
        ttk.Checkbutton(self.library_panel, text='문서 휴지통 보기', variable=self.show_trash,
                        command=self.refresh_library).pack(anchor='w')
        self.document_trash_button = ttk.Button(self.library_panel, text='선택 문서 삭제 / 복원', command=self.toggle_trash, state='disabled')
        self.document_trash_button.pack(fill='x')
        trash_actions = ttk.Menubutton(self.library_panel, text='휴지통 관리')
        trash_menu = tk.Menu(trash_actions, tearoff=False)
        trash_menu.add_command(label='페이지 휴지통 열기…', command=self.show_deleted_pages)
        trash_menu.add_command(label='선택 휴지통 문서 영구 삭제', command=self.purge_current_document)
        trash_menu.add_separator()
        trash_menu.add_command(label='휴지통 비우기', command=self.empty_trash)
        trash_actions.configure(menu=trash_menu)
        trash_actions.pack(fill='x', pady=(3, 5))
        listing = ttk.Frame(self.library_panel)
        listing.pack(fill='both', expand=True)
        self.document_tree = ttk.Treeview(listing, show='tree', selectmode='browse', style='Desk.Treeview')
        self.document_tree.column('#0', width=220, stretch=True)
        bar = ttk.Scrollbar(listing, command=self.document_tree.yview)
        self.document_tree.configure(yscrollcommand=bar.set)
        bar.pack(side='right', fill='y')
        self.document_tree.pack(fill='both', expand=True)
        self.document_tree.bind('<<TreeviewSelect>>', self._document_selected)
        self.document_tree.bind('<Button-3>', self._document_context_menu)
        self.document_tree.tag_configure('alternate', background='#f4f8fa')
        ttk.Button(self.library_panel, text='더 보기', command=self._show_more).pack(fill='x', pady=4)

        self.workspace = ttk.Frame(self.main_split)
        self.main_split.add(self.workspace, weight=1)
        self.recovery_warning = tk.StringVar(value='')
        self.recovery_label = ttk.Label(self.workspace, textvariable=self.recovery_warning,
            foreground='#b42318', wraplength=700)
        identity = ttk.Frame(self.workspace, padding=(10, 0, 0, 7))
        self._identity = identity
        identity.pack(fill='x')
        self.title_var = tk.StringVar()
        self.title_entry = ttk.Entry(identity, textvariable=self.title_var)
        self.title_entry.pack(side='left', fill='x', expand=True)
        self.title_entry.bind('<KeyRelease>', lambda _event: self._schedule_save())
        self.adopt_button = ttk.Button(identity, text='새 문서로 가져오기', command=self.adopt_current)
        self.adopt_button.pack(side='right', padx=5)
        self.view_button = ttk.Button(identity, text='화면 비율 초기화', command=self.toggle_compact_view)
        self.view_button.pack(side='right', padx=3)
        self.details_button = ttk.Button(identity, text='라벨·메모', command=self.toggle_details)
        self.details_button.pack(side='right', padx=3)
        details = ttk.Frame(self.workspace, padding=(10, 0, 0, 7))
        self.details_panel = details
        ttk.Label(details, text='라벨 (쉼표 구분)').grid(row=0, column=0, sticky='w', padx=(0, 6))
        self.labels_var = tk.StringVar()
        self.labels_entry = ttk.Entry(details, textvariable=self.labels_var)
        self.labels_entry.grid(row=0, column=1, sticky='ew', padx=(0, 10))
        self.labels_entry.bind('<KeyRelease>', lambda _event: self._schedule_save())
        self.label_select_button = ttk.Button(details, text='라벨 선택', command=self.choose_labels)
        self.label_select_button.grid(row=0, column=2, padx=(0, 6))
        ttk.Label(details, text='메모').grid(row=1, column=0, sticky='w', padx=(0, 6), pady=(5, 0))
        self.memo_var = tk.StringVar()
        self.memo_entry = ttk.Entry(details, textvariable=self.memo_var)
        self.memo_entry.grid(row=1, column=1, sticky='ew', padx=(0, 10), pady=(5, 0))
        self.memo_entry.bind('<KeyRelease>', lambda _event: self._schedule_save())
        details.columnconfigure(1, weight=1)
        self.work_split = ResponsivePanedWindow(self.workspace, orient='horizontal')
        self.work_split.pack(fill='both', expand=True, padx=(10, 0))
        self.work_split.bind('<Configure>', self._schedule_layout)
        self.work_split.bind('<ButtonRelease-1>', self._schedule_layout)
        self.image_panel = ttk.Frame(self.work_split)
        self.editor_panel = ttk.Frame(self.work_split)
        self.work_split.add(self.image_panel, weight=1)
        self.work_split.add(self.editor_panel, weight=1)
        page_bar = ttk.Frame(self.image_panel)
        self._page_bar = page_bar
        page_bar.pack(fill='x', pady=(0, 5))
        self.image_copy_button = ttk.Button(page_bar, text='이미지 복사', width=-10,
            command=self.copy_current_image, state='disabled')
        self.image_copy_button.pack(side='left', padx=(0, 4))
        self.page_selector = ttk.Combobox(page_bar, state='readonly', width=20)
        self.page_selector.pack(side='left', fill='x', expand=True)
        self.page_selector.bind('<<ComboboxSelected>>', self._page_selected)
        self.page_selector.bind('<Button-3>', self._page_context_menu)
        ttk.Button(page_bar, text='↑', width=3, command=lambda: self.move_page(-1)).pack(side='left', padx=2)
        ttk.Button(page_bar, text='↓', width=3, command=lambda: self.move_page(1)).pack(side='left')
        self.image_view = ZoomImageView(self.image_panel)
        self.image_view.pack(fill='both', expand=True)
        self.image_view.canvas.bind('<Button-3>', self._page_context_menu)
        page_actions = ttk.Frame(self.image_panel)
        self._page_actions = page_actions
        page_actions.pack(fill='x', before=page_bar)
        self.page_delete_button = ttk.Button(page_actions, text='이 페이지 삭제', command=self.delete_current_page, state='disabled')
        self.page_delete_button.pack(side='left')
        ttk.Button(page_actions, text='페이지 휴지통', command=self.show_deleted_pages).pack(side='left', padx=3)

        self.editor_tabs = ttk.Notebook(self.editor_panel)
        self.editor_tabs.pack(fill='both', expand=True, padx=(7, 0))
        self.text_panel = ttk.Frame(self.editor_tabs)
        self.table_panel = ttk.Frame(self.editor_tabs)
        self.ai_panel = ttk.Frame(self.editor_tabs)
        self.editor_tabs.add(self.text_panel, text='텍스트')
        self.editor_tabs.add(self.table_panel, text='표')
        self.editor_tabs.add(self.ai_panel, text='업무 실행')
        self.editor_tabs.bind('<<NotebookTabChanged>>', self._tab_changed)
        copies = ttk.Frame(self.text_panel)
        self._copy_bar = copies
        copies.pack(fill='x', pady=6)
        for text, command in (('선택 복사', self.copy_selection), ('페이지 복사', self.copy_page),
                              ('전체 복사', self.copy_document)):
            ttk.Button(copies, text=text, command=command).pack(side='left', padx=2)
        self.source_editor = scrolledtext.ScrolledText(self.text_panel, wrap='word', undo=True, height=8, width=1,
            font=('Malgun Gothic', 11), relief='flat', padx=12, pady=12, exportselection=False)
        self.source_editor.bind('<<Modified>>', self._source_modified)
        review_bar = ttk.Frame(self.text_panel)
        review_bar.pack(side='bottom', fill='x', pady=5)
        self.read_button = ttk.Button(review_bar, text='다시 읽기', command=self.read_current_page)
        self.read_button.pack(side='left')
        ttk.Label(review_bar, text='빨간 밑줄: 확인할 부분', foreground='#b42318').pack(side='right')
        self.source_editor.pack(fill='both', expand=True)
        self.table_view = InlineTableView(self.table_panel, on_copy=self.copy_text)
        self.table_view.pack(fill='both', expand=True)
        ai_controls = ttk.Frame(self.ai_panel)
        self._ai_controls = ai_controls
        ai_controls.pack(fill='x', pady=6)
        self.ai_mode = tk.StringVar(value='요약')
        mode_picker = ttk.Combobox(ai_controls, textvariable=self.ai_mode, state='readonly', width=16,
                                   values=('요약', '일정·할 일 정리', '안내문'))
        self._ai_mode_picker = mode_picker
        mode_picker.grid(row=0, column=0, sticky='w', padx=2)
        mode_picker.bind('<<ComboboxSelected>>', self._ai_mode_changed)
        self.audience = tk.StringVar(value='교직원')
        self.audience_picker = ttk.Combobox(ai_controls, textvariable=self.audience,
            state='readonly', width=11, values=('교직원', '학부모', '가정통신문'))
        self.audience_picker.grid(row=0, column=1, sticky='w', padx=2)
        self.audience_picker.grid_remove()
        self.generate_button = ttk.Button(ai_controls, text='정리하기', command=self.generate)
        self.generate_button.grid(row=0, column=2, sticky='w', padx=2)
        ai_controls.bind('<Configure>', self._schedule_layout)
        self.selection_only = tk.BooleanVar(value=False)
        ttk.Checkbutton(self.ai_panel, text='선택한 텍스트만 정리 (기본: 문서 전체)', variable=self.selection_only).pack(anchor='w')
        self.output_note = tk.StringVar(value='요약·일정·안내문을 필요할 때만 만드세요.')
        self.output_note_label = ttk.Label(self.ai_panel, textvariable=self.output_note, wraplength=440, foreground='#667085')
        self.output_note_label.pack(fill='x', pady=5)
        self.output_editor = scrolledtext.ScrolledText(self.ai_panel, wrap='word', undo=True, height=8, width=1,
            font=('Malgun Gothic', 11), relief='flat', padx=12, pady=12)
        self.output_editor.bind('<<Modified>>', self._output_modified)
        ai_footer = ttk.Frame(self.ai_panel)
        ai_footer.pack(side='bottom', fill='x', pady=5)
        ttk.Button(ai_footer, text='결과 복사', command=lambda: self.copy_text(self.output_editor.get('1.0', 'end-1c'))).pack(side='left')
        from ui.work_image import open_work_image
        self.work_image_button = ttk.Button(ai_footer, text='이미지로 만들기', command=lambda: open_work_image(self))
        self.work_image_button.pack(side='left', padx=5)
        ttk.Button(ai_footer, text='이전 결과', command=self.show_output_history).pack(side='right')
        self.output_editor.pack(fill='both', expand=True)

    def _replace(self, editor, text, readonly=False):
        self._loading = True
        try:
            editor.configure(state='normal')
            editor.delete('1.0', 'end')
            editor.insert('1.0', text)
            editor.edit_reset()
            editor.edit_modified(False)
            highlight_source(editor)
            if readonly:
                editor.configure(state='disabled')
        finally:
            self._loading = False

    def open_document(self, document_id, *, _discard_edits=False, preserve_library=False):
        if not _discard_edits and not self.flush_edits():
            if self.document and self.document_tree.exists(self.document['id']):
                self.document_tree.selection_set(self.document['id'])
            return False
        try:
            document = self.library.get_document(document_id)
            pages = self.library.pages(document_id)
            outputs = self.library.outputs(document_id) if not document.get('readonly') else []
            active_id = self.library.get_setting('active_output:' + document_id)
        except Exception as error:
            log_failure('capture_desk.open_document', error)
            self.status.set('문서를 열지 못했습니다. 기존 입력은 유지합니다.')
            return False
        self.document, self.page_records = document, pages
        self.title_var.set(document['title'])
        locked = document.get('readonly') or document.get('trashed')
        self.title_entry.configure(state='readonly' if locked else 'normal')
        self.labels_var.set(', '.join(document.get('labels', [])))
        self.memo_var.set(document.get('memo', ''))
        self.labels_entry.configure(state='readonly' if locked else 'normal')
        self.label_select_button.configure(state='disabled' if locked else 'normal')
        self.document_trash_button.configure(text='선택 문서 복원' if document.get('trashed') else '선택 문서 삭제', state='normal')
        self.memo_entry.configure(state='readonly' if locked else 'normal')
        self.adopt_button.configure(state='normal' if document.get('readonly') and not document.get('trashed') else 'disabled')
        self.page_selector.configure(values=[f"{i + 1}쪽 · {page.get('source_name') or '캡처'}" for i, page in enumerate(pages)])
        self.page = None
        self._load_page(0 if pages else None)
        chosen = next((item for item in outputs if item['id'] == active_id), outputs[0] if outputs else None)
        self._load_output(chosen)
        if document.get('legacy') and not str(document_id).startswith('capture:'):
            old = self.library.task_store.get(document_id)
            if old and old.analysis_text:
                self._replace(self.output_editor, old.analysis_text, readonly=True)
                self.output_note.set('기존 실행안 · 읽기 전용. 새 문서에서는 카드 없이 정리합니다.')
        self.status.set('기존 기록은 읽기 전용입니다. 새 문서로 가져와 편집할 수 있습니다.' if document.get('readonly') else '페이지별로 대조·수정하세요. 수정 내용은 자동 저장됩니다.')
        if document.get('trashed'):
            self.status.set('휴지통 자료 · 보관함의 선택 문서 복원 버튼으로 복원한 뒤 편집하세요.')
        self.save_state.set('읽기 전용' if locked else '저장됨')
        self._show_recovery_warning()
        if self._is_compact() and not preserve_library:
            self._compact_library = False
            self._apply_layout()
        return True

    def _load_page(self, index):
        self.page = self.page_records[index] if index is not None else None
        self._source_dirty = False
        self._source_revision += 1
        if index is not None:
            self.page_selector.current(index)
        else:
            self.page_selector.set('')
        self._replace(self.source_editor, self.page['text'] if self.page else '',
                      readonly=not self.page or self.page.get('readonly', False) or self.document.get('trashed', False))
        image_loaded = self.image_view.load_path(self.page.get('path') if self.page else None)
        can_copy_image = image_loaded and self.page and self.page.get('path')
        self.image_copy_button.configure(state='normal' if can_copy_image else 'disabled')
        if self.page and not self.page.get('path') and str(self.page.get('source_path', '')).lower().endswith('.hwpx'):
            self.image_view.show_empty_message('HWPX 본문과 표를 읽었습니다.\n원본 지면은 더보기 → 원본 문서 파일 열기에서 확인하세요.\n삽입 이미지는 페이지 목록에서 따로 선택할 수 있습니다.')
        self.table_view.set_text(self.page['text'] if self.page else '')
        locked = not self.page or self.page.get('readonly') or self.document.get('readonly') or self.document.get('trashed')
        self.read_button.configure(state='disabled' if locked else 'normal')
        can_delete = self.page and self.document and not self.document.get('readonly') and not self.document.get('trashed')
        self.page_delete_button.configure(state='normal' if can_delete else 'disabled')
        self._show_recovery_warning()

    def _show_recovery_warning(self):
        # Storage supplies only generic, privacy-safe messages; never display a
        # raw exception or damaged path in the persistent recovery notice.
        warning = ((self.page or {}).get('warning') or (self.document or {}).get('warning') or '')
        self.recovery_warning.set(warning)
        if warning:
            self.recovery_label.pack(side='top', fill='x', padx=(10, 0), pady=(0, 5), before=self.work_split)
        else:
            self.recovery_label.pack_forget()

    def _page_selected(self, _event=None):
        index = self.page_selector.current()
        if index < 0:
            return
        selected_id = self.page_records[index]['id']
        if not self.flush_edits():
            if self.page:
                self.page_selector.current(next(i for i, p in enumerate(self.page_records) if p['id'] == self.page['id']))
            return
        try:
            fresh = self.library.pages(self.document['id'])
            index = next(i for i, page in enumerate(fresh) if page['id'] == selected_id)
            self.page_records = fresh
            self._load_page(index)
        except Exception as error:
            log_failure('capture_desk._page_selected', error)
            if self.page:
                self.page_selector.current(next(i for i, p in enumerate(self.page_records) if p['id'] == self.page['id']))
            self.status.set('페이지를 읽지 못했습니다. 현재 입력은 유지합니다.')

    def _load_output(self, output):
        self.output = output
        self._output_dirty = False
        self._output_revision += 1
        readonly = not self.document or self.document.get('readonly', False) or self.document.get('trashed') or output is None
        self._replace(self.output_editor, output['text'] if output else '', readonly=readonly)
        if output:
            self.output_note.set(f"{output['mode']} · 저장된 결과 · 발송 전 원문을 확인하세요.")
            if self.document:
                try:
                    self.library.set_setting('active_output:' + self.document['id'], output['id'])
                except Exception as error:
                    log_failure('capture_desk._load_output', error)
                    self.status.set('결과 본문은 보존됐지만 마지막 열람 위치를 저장하지 못했습니다.')
            self._update_output_staleness()
        else:
            self.output_note.set('필요한 정리만 실행하세요. 원문은 바뀌지 않습니다.')

    def _source_modified(self, _event=None):
        if not self.source_editor.edit_modified():
            return
        self.source_editor.edit_modified(False)
        if self._loading or not self.page or self.page.get('readonly'):
            return
        self._source_dirty = self.source_editor.get('1.0', 'end-1c') != self.page['text']
        self._source_revision += 1
        self._schedule_save()

    def _output_modified(self, _event=None):
        if not self.output_editor.edit_modified():
            return
        self.output_editor.edit_modified(False)
        if self._loading or not self.output or not self.document or self.document.get('readonly'):
            return
        self._output_dirty = self.output_editor.get('1.0', 'end-1c') != self.output['text']
        self._output_revision += 1
        self._schedule_save()

    def _schedule_save(self):
        if self._autosave_id:
            self.after_cancel(self._autosave_id)
        self.save_state.set('저장 대기')
        self._autosave_id = self.after(700, self._autosave)

    def _autosave(self):
        self._autosave_id = None
        self.flush_edits(show_error=False)

    def flush_edits(self, show_error=True):
        if self._autosave_id:
            self.after_cancel(self._autosave_id)
            self._autosave_id = None
        if not self.document or self.document.get('readonly') or self.document.get('trashed'):
            return True
        try:
            title = self.title_var.get().strip()
            title_changed = bool(title) and title != self.document['title']
            labels = [label.strip() for label in self.labels_var.get().split(',') if label.strip()]
            memo = self.memo_var.get().strip()
            details_changed = labels != self.document.get('labels', []) or memo != self.document.get('memo', '')
            latest = self.library.get_document(self.document['id'])
            if title_changed and latest['title'] != self.document['title']:
                raise LibraryConflictError('제목이 다른 창에서 변경되었습니다.')
            if details_changed and (latest.get('labels') != self.document.get('labels') or latest.get('memo') != self.document.get('memo')):
                raise LibraryConflictError('라벨 또는 메모가 다른 창에서 변경되었습니다.')
            if self._source_dirty and self.page:
                if self.page.get('readonly') or self.page.get('recovery_required'):
                    raise LibraryConflictError('복구가 필요한 페이지의 입력은 저장하지 않습니다.')
                saved = self.library.save_page_text(self.page['id'], self.source_editor.get('1.0', 'end-1c'),
                    expected_updated_at=self.page['updated_at'], expected_document_id=self.document['id'])
                self.page_records = [saved if p['id'] == saved['id'] else p for p in self.page_records]
                self.page = saved
                self._source_dirty = False
                highlight_source(self.source_editor)
            if self._output_dirty and self.output:
                self.output = self.library.update_output(self.output['id'], self.output_editor.get('1.0', 'end-1c'),
                    expected_updated_at=self.output['updated_at'])
                self._output_dirty = False
            if title_changed:
                latest = self.library.get_document(self.document['id'])
                if latest['title'] != self.document['title']:
                    raise LibraryConflictError('제목이 다른 창에서 변경되었습니다.')
                self.library.rename(self.document['id'], title, expected_updated_at=latest['updated_at'])
            if details_changed:
                latest = self.library.get_document(self.document['id'])
                self.library.update_details(self.document['id'], labels, memo, expected_updated_at=latest['updated_at'])
            self.document = self.library.get_document(self.document['id'])
            self.title_var.set(self.document['title'])
            self.labels_var.set(', '.join(self.document.get('labels', [])))
            self.memo_var.set(self.document.get('memo', ''))
            self.save_state.set('저장됨')
            self._update_output_staleness()
            if title_changed or details_changed:
                self.refresh_library()
            return True
        except Exception as error:
            log_failure('capture_desk.flush_edits', error)
            self.save_state.set('저장 실패 · 입력 유지')
            self.status.set('입력은 유지됩니다. 충돌 시 복사 후 더보기 → 저장된 최신값 다시 열기를 사용하세요.')
            if show_error:
                messagebox.showerror('저장하지 못함', '현재 입력을 유지했습니다. 저장 공간 또는 다른 창의 변경을 확인한 뒤 다시 시도하세요.', parent=self)
            return False

    def reload_saved(self):
        """Explicit conflict escape hatch; never discard input without consent."""
        if not self.document:
            return False
        if not messagebox.askyesno('저장된 최신값 다시 열기',
                '현재 미저장 입력을 버리고 저장된 내용을 다시 열까요?\n필요한 입력은 먼저 페이지 복사·결과 복사로 보관하세요.', parent=self):
            return False
        return self.open_document(self.document['id'], _discard_edits=True)

    def _save_shortcut(self):
        self.flush_edits()
        return 'break'

    def copy_text(self, text):
        self.clipboard_clear()
        self.clipboard_append(text)
        self.status.set('복사했습니다.')

    def copy_selection(self):
        try:
            self.copy_text(self.source_editor.get('sel.first', 'sel.last'))
        except tk.TclError:
            self.status.set('복사할 글자를 먼저 선택하세요.')

    def copy_page(self):
        self.copy_text(self.source_editor.get('1.0', 'end-1c'))

    def copy_document(self):
        if self.document and self.flush_edits():
            self.copy_text(self.library.document_text(self.document['id']))

    def _tab_changed(self, _event=None):
        self._schedule_layout()
        if self.editor_tabs.select() == str(self.table_panel):
            self.table_view.set_text(self.source_editor.get('1.0', 'end-1c'))

    def _ai_mode_changed(self, _event=None):
        if self.ai_mode.get() == '안내문':
            self.audience_picker.grid()
        else:
            self.audience_picker.grid_remove()
        self._schedule_layout()

    def move_page(self, direction):
        if not self.page or not self.document or self.document.get('readonly') or not self.flush_edits():
            return
        index = next(i for i, page in enumerate(self.page_records) if page['id'] == self.page['id'])
        target = index + direction
        if not 0 <= target < len(self.page_records):
            return
        ids = [page['id'] for page in self.page_records]
        ids[index], ids[target] = ids[target], ids[index]
        try:
            self.library.reorder_pages(self.document['id'], ids, expected_updated_at=self.document['updated_at'])
            self.open_document(self.document['id'])
            self._load_page(target)
        except Exception as error:
            log_failure('capture_desk.move_page', error)
            self.status.set('페이지 순서를 저장하지 못했습니다. 원래 순서는 유지합니다.')

    def show_help(self):
        from ui.user_manual import show_user_manual
        return show_user_manual(self)

    def show_capture_location(self):
        dialog = tk.Toplevel(self)
        dialog.title('캡처 저장 폴더')
        dialog.transient(self)
        dialog.resizable(False, False)
        body = ttk.Frame(dialog, padding=18)
        body.pack(fill='both', expand=True)
        ttk.Label(body, text='캡처 이미지와 OCR 정보', font=('Malgun Gothic', 11, 'bold')).pack(anchor='w')
        ttk.Label(body, text=str(self.library.capture_store.directory), wraplength=520, padding=(0, 8)).pack(anchor='w')
        ttk.Label(body, text='폴더를 변경하면 기존 캡처를 복사하고 앱을 종료합니다.\n다시 실행하면 새 위치를 사용합니다. 이전 원본은 그대로 남습니다.\n문서·라벨·메모와 AI 전송 설정은 기존 앱 데이터에 보존됩니다.',
                  wraplength=520).pack(anchor='w')
        actions = ttk.Frame(body, padding=(0, 14, 0, 0))
        actions.pack(fill='x')
        if os.name == 'nt':
            def open_folder():
                try:
                    os.startfile(str(self.library.capture_store.directory))
                except OSError:
                    messagebox.showerror('폴더 열기 실패', '저장 장치 연결과 폴더 위치를 확인하세요.', parent=dialog)
            ttk.Button(actions, text='폴더 열기', command=open_folder).pack(side='left')
        ttk.Button(actions, text='저장 폴더 변경…', command=self.change_capture_location,
                   state='normal' if self._capture_location else 'disabled').pack(side='left', padx=6)
        ttk.Button(actions, text='닫기', command=dialog.destroy).pack(side='right')
        return dialog

    def change_capture_location(self):
        if self._capture_location is None:
            return False
        if self._jobs:
            messagebox.showinfo('처리 중', 'OCR·AI 처리가 끝난 뒤 저장 폴더를 변경하세요.', parent=self)
            return False
        if not self.flush_edits():
            return False
        selected = filedialog.askdirectory(title='캡처를 복사할 빈 폴더 선택',
                                          initialdir=str(self.library.capture_store.directory.parent), parent=self)
        if not selected:
            return False
        if not messagebox.askyesno('캡처 저장 위치 변경',
                '다른 Ssokly 창을 모두 닫아 주세요.\n기존 캡처를 아래 폴더에 복사하고 이 앱도 종료합니다.\n'
                '다시 실행하면 새 위치를 사용하며 이전 원본은 남습니다.\n\n' + selected, parent=self):
            return False
        try:
            changed = self._capture_location.relocate(self.library.capture_store, selected)
        except (OSError, ValueError, sqlite3.Error) as error:
            messagebox.showerror('폴더 변경 실패',
                '기존 위치를 계속 사용합니다. 빈 폴더·남은 공간·파일 상태를 확인하세요.\n'
                '새 폴더에 복사본이 남아 있을 수 있습니다.\n\n' + str(error), parent=self)
            return False
        if changed:
            # Edits were flushed before copying. Do not perform another write
            # to the old capture store after publishing the new location.
            self._finish_close()
        return changed

    def close(self):
        if not self.flush_edits():
            return False
        if self._jobs and not messagebox.askyesno('처리 중', '처리를 취소하고 닫을까요? 이미 보관한 원본과 저장한 텍스트는 남습니다.', parent=self):
            return False
        return self._finish_close()

    def _finish_close(self):
        self._closing = True
        self.cancel_jobs()
        for callback in (self._poll_id, self._autosave_id, self._refresh_id, self._layout_id):
            if callback:
                try:
                    self.after_cancel(callback)
                except tk.TclError:
                    pass
        self.thumbnails.clear()
        self.destroy()
        return True

    def destroy(self):
        from ui.tk_lifecycle import prepare_destroy
        self._closing = True
        prepare_destroy(self)
        super().destroy()
