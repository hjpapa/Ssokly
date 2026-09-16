"""Capture-first desktop. Pages are the editable source; AI outputs are separate."""
import hashlib
import os
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
from uuid import uuid4

from PIL import Image

from services.capture_service import capture_selected_region
from services.document_library import DocumentLibrary, LibraryConflictError
from services.document_service import attachment_kind, mime_type_for, read_hwpx_file, read_text_file
from services.source_review import highlight_source
from ui.desk_widgets import InlineTableView, ThumbnailCache, ZoomImageView


def fingerprint(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


class CaptureDeskApp(tk.Tk):
    """All widget and persistence changes run on Tk's thread, never a worker."""
    def __init__(self, *, library=None, app_data_dir=None):
        super().__init__()
        local_data = os.getenv('LOCALAPPDATA', '').strip()
        root = Path(app_data_dir) if app_data_dir else (Path(local_data) / 'Ssokly' if local_data else Path.home() / '.ssokly')
        self.library = library or DocumentLibrary(root)
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
        self._compact_image = False
        self._layout_compact = None
        self._visible_count = 250
        self.title('Ssokly · 캡처와 텍스트')
        self.geometry('1280x800')
        self.minsize(720, 560)
        self.configure(bg='#f5f7f8')
        style = ttk.Style(self)
        style.configure('Desk.TButton', padding=(9, 6))
        style.configure('Desk.Treeview', rowheight=62)
        self.option_add('*Font', ('Malgun Gothic', 10))
        self._build_ui()
        self.thumbnails = ThumbnailCache(self)
        self.protocol('WM_DELETE_WINDOW', self.close)
        self.bind('<Control-s>', lambda _event: self._save_shortcut())
        self.bind('<Configure>', self._on_resize)
        self.refresh_library()
        self._poll_id = self.after(80, self._poll_results)

    def _build_ui(self):
        header = ttk.Frame(self, padding=(12, 10))
        header.pack(fill='x')
        ttk.Label(header, text='SSOKLY', font=('Segoe UI', 16, 'bold')).pack(side='left', padx=(0, 14))
        self.capture_button = ttk.Button(header, text='새 캡처', command=self.capture_new, style='Desk.TButton')
        self.capture_button.pack(side='left', padx=3)
        self.add_button = ttk.Button(header, text='+ 페이지', command=self.capture_page, style='Desk.TButton')
        self.add_button.pack(side='left', padx=3)
        ttk.Button(header, text='파일 열기', command=self.open_file, style='Desk.TButton').pack(side='left', padx=3)
        ttk.Button(header, text='보관함', command=self.toggle_library).pack(side='right')
        menu = tk.Menu(self, tearoff=False)
        menu.add_command(label='새 텍스트 문서', command=self.new_text_document)
        menu.add_command(label='기존 기록 보기', command=self.show_legacy_records)
        menu.add_command(label='원래 인식 내용 보기', command=self.show_original_text)
        menu.add_command(label='이전 AI 결과', command=self.show_output_history)
        menu.add_command(label='저장된 최신값 다시 열기…', command=self.reload_saved)
        menu.add_separator()
        menu.add_command(label='현재 문서 휴지통 / 복원', command=self.toggle_trash)
        more = ttk.Menubutton(header, text='더보기', menu=menu)
        more.pack(side='right', padx=6)

        options = ttk.Frame(self, padding=(12, 0, 12, 8))
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
        ttk.Label(options, text='이미지는 먼저 PC에 보관 · AI 실행 시 OpenAI 전송', foreground='#667085').pack(side='left')
        self.cancel_button = ttk.Button(options, text='처리 취소', command=self.cancel_jobs)
        self.cancel_button.pack(side='right')

        bottom = ttk.Frame(self, padding=(12, 6))
        bottom.pack(side='bottom', fill='x')
        self.status = tk.StringVar(value='캡처하거나 파일을 열어 시작하세요.')
        self.save_state = tk.StringVar(value='')
        self.status_label = ttk.Label(bottom, textvariable=self.status, wraplength=750)
        self.status_label.pack(side='left', fill='x', expand=True)
        ttk.Label(bottom, textvariable=self.save_state, foreground='#177568').pack(side='right', padx=8)
        self.main_split = ttk.Panedwindow(self, orient='horizontal')
        self.main_split.pack(fill='both', expand=True, padx=12, pady=(0, 8))
        self.library_panel = ttk.Frame(self.main_split, width=230)
        self.main_split.add(self.library_panel, weight=0)
        self.query = tk.StringVar()
        search = ttk.Entry(self.library_panel, textvariable=self.query)
        search.pack(fill='x', pady=(0, 5))
        search.bind('<KeyRelease>', self._search_changed)
        self.show_trash = tk.BooleanVar(value=False)
        ttk.Checkbutton(self.library_panel, text='휴지통', variable=self.show_trash,
                        command=self.refresh_library).pack(anchor='w')
        listing = ttk.Frame(self.library_panel)
        listing.pack(fill='both', expand=True)
        self.document_tree = ttk.Treeview(listing, show='tree', selectmode='browse', style='Desk.Treeview')
        self.document_tree.column('#0', width=220, stretch=True)
        bar = ttk.Scrollbar(listing, command=self.document_tree.yview)
        self.document_tree.configure(yscrollcommand=bar.set)
        bar.pack(side='right', fill='y')
        self.document_tree.pack(fill='both', expand=True)
        self.document_tree.bind('<<TreeviewSelect>>', self._document_selected)
        ttk.Button(self.library_panel, text='더 보기', command=self._show_more).pack(fill='x', pady=4)

        self.workspace = ttk.Frame(self.main_split)
        self.main_split.add(self.workspace, weight=1)
        identity = ttk.Frame(self.workspace, padding=(10, 0, 0, 7))
        identity.pack(fill='x')
        self.title_var = tk.StringVar()
        self.title_entry = ttk.Entry(identity, textvariable=self.title_var)
        self.title_entry.pack(side='left', fill='x', expand=True)
        self.title_entry.bind('<KeyRelease>', lambda _event: self._schedule_save())
        self.adopt_button = ttk.Button(identity, text='새 문서로 가져오기', command=self.adopt_current)
        self.adopt_button.pack(side='right', padx=5)
        self.view_button = ttk.Button(identity, text='원본 / 텍스트', command=self.toggle_compact_view)
        self.view_button.pack(side='right', padx=3)
        self.work_split = ttk.Panedwindow(self.workspace, orient='horizontal')
        self.work_split.pack(fill='both', expand=True, padx=(10, 0))
        self.image_panel = ttk.Frame(self.work_split)
        self.editor_panel = ttk.Frame(self.work_split)
        self.work_split.add(self.image_panel, weight=1)
        self.work_split.add(self.editor_panel, weight=1)
        page_bar = ttk.Frame(self.image_panel)
        page_bar.pack(fill='x', pady=(0, 5))
        self.page_selector = ttk.Combobox(page_bar, state='readonly', width=20)
        self.page_selector.pack(side='left', fill='x', expand=True)
        self.page_selector.bind('<<ComboboxSelected>>', self._page_selected)
        ttk.Button(page_bar, text='↑', width=3, command=lambda: self.move_page(-1)).pack(side='left', padx=2)
        ttk.Button(page_bar, text='↓', width=3, command=lambda: self.move_page(1)).pack(side='left')
        self.image_view = ZoomImageView(self.image_panel)
        self.image_view.pack(fill='both', expand=True)

        self.editor_tabs = ttk.Notebook(self.editor_panel)
        self.editor_tabs.pack(fill='both', expand=True, padx=(7, 0))
        self.text_panel = ttk.Frame(self.editor_tabs)
        self.table_panel = ttk.Frame(self.editor_tabs)
        self.ai_panel = ttk.Frame(self.editor_tabs)
        self.editor_tabs.add(self.text_panel, text='텍스트')
        self.editor_tabs.add(self.table_panel, text='표')
        self.editor_tabs.add(self.ai_panel, text='AI 정리')
        self.editor_tabs.bind('<<NotebookTabChanged>>', self._tab_changed)
        copies = ttk.Frame(self.text_panel)
        copies.pack(fill='x', pady=6)
        for text, command in (('선택 복사', self.copy_selection), ('페이지 복사', self.copy_page),
                              ('전체 복사', self.copy_document)):
            ttk.Button(copies, text=text, command=command).pack(side='left', padx=2)
        self.source_editor = scrolledtext.ScrolledText(self.text_panel, wrap='word', undo=True, height=8,
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
        ai_controls.pack(fill='x', pady=6)
        self.ai_mode = tk.StringVar(value='요약')
        mode_picker = ttk.Combobox(ai_controls, textvariable=self.ai_mode, state='readonly', width=16,
                                   values=('요약', '일정·할 일 정리', '안내문'))
        mode_picker.pack(side='left', padx=2)
        mode_picker.bind('<<ComboboxSelected>>', self._ai_mode_changed)
        self.audience = tk.StringVar(value='교직원')
        self.audience_picker = ttk.Combobox(ai_controls, textvariable=self.audience,
            state='readonly', width=11, values=('교직원', '학부모', '가정통신문'))
        self.audience_picker.pack(side='left', padx=2)
        self.audience_picker.pack_forget()
        self.generate_button = ttk.Button(ai_controls, text='정리하기', command=self.generate)
        self.generate_button.pack(side='left', padx=2)
        self.selection_only = tk.BooleanVar(value=False)
        ttk.Checkbutton(self.ai_panel, text='선택한 텍스트만 정리 (기본: 문서 전체)', variable=self.selection_only).pack(anchor='w')
        self.output_note = tk.StringVar(value='요약·일정·안내문을 필요할 때만 만드세요.')
        ttk.Label(self.ai_panel, textvariable=self.output_note, wraplength=440, foreground='#667085').pack(fill='x', pady=5)
        self.output_editor = scrolledtext.ScrolledText(self.ai_panel, wrap='word', undo=True, height=8,
            font=('Malgun Gothic', 11), relief='flat', padx=12, pady=12)
        self.output_editor.bind('<<Modified>>', self._output_modified)
        ai_footer = ttk.Frame(self.ai_panel)
        ai_footer.pack(side='bottom', fill='x', pady=5)
        ttk.Button(ai_footer, text='결과 복사', command=lambda: self.copy_text(self.output_editor.get('1.0', 'end-1c'))).pack(side='left')
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

    def refresh_library(self, select_id=None):
        previous = select_id or (self.document['id'] if self.document else None)
        try:
            records = self.library.list_documents(self.query.get(), trashed=self.show_trash.get())
        except Exception:
            self.status.set('보관함을 읽지 못했습니다. 기존 화면은 유지합니다.')
            return
        self._listing = {item['id']: item for item in records}
        self.document_tree.delete(*self.document_tree.get_children())
        self._library_photos = {}
        for item in records[:self._visible_count]:
            photo = self.thumbnails.get(item.get('thumbnail_path'), (52, 44)) if item.get('thumbnail_path') else None
            detail = f"{str(item.get('updated_at', ''))[:10]} · {item['page_count']}쪽"
            if item.get('legacy'):
                detail += ' · 기존 기록'
            opts = {'image': photo} if photo else {}
            if photo:
                self._library_photos[item['id']] = photo
            self.document_tree.insert('', 'end', iid=item['id'], text=f"{item['title']}\n{detail}", **opts)
        if previous and self.document_tree.exists(previous):
            self.document_tree.selection_set(previous)

    def _search_changed(self, _event=None):
        if self._refresh_id:
            self.after_cancel(self._refresh_id)
        self._visible_count = 250
        self._refresh_id = self.after(250, self._run_search)

    def _run_search(self):
        self._refresh_id = None
        self.refresh_library()

    def _show_more(self):
        self._visible_count += 250
        self.refresh_library()

    def _document_selected(self, _event=None):
        selected = self.document_tree.selection()
        if selected and (not self.document or selected[0] != self.document['id']):
            self.open_document(selected[0])

    def open_document(self, document_id, *, _discard_edits=False):
        if not _discard_edits and not self.flush_edits():
            if self.document and self.document_tree.exists(self.document['id']):
                self.document_tree.selection_set(self.document['id'])
            return False
        try:
            document = self.library.get_document(document_id)
            pages = self.library.pages(document_id)
            outputs = self.library.outputs(document_id) if not document.get('readonly') else []
            active_id = self.library.get_setting('active_output:' + document_id)
        except Exception:
            self.status.set('문서를 열지 못했습니다. 기존 입력은 유지합니다.')
            return False
        self.document, self.page_records = document, pages
        self.title_var.set(document['title'])
        locked = document.get('readonly') or document.get('trashed')
        self.title_entry.configure(state='readonly' if locked else 'normal')
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
            self.status.set('휴지통 자료 · 더보기 → 현재 문서 휴지통 / 복원으로 복원한 뒤 편집하세요.')
        self.save_state.set('읽기 전용' if locked else '저장됨')
        if self.winfo_width() < 1050:
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
        self.image_view.load_path(self.page.get('path') if self.page else None)
        self.table_view.set_text(self.page['text'] if self.page else '')

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
        except Exception:
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
                except Exception:
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
            latest = self.library.get_document(self.document['id'])
            if title_changed and latest['title'] != self.document['title']:
                raise LibraryConflictError('제목이 다른 창에서 변경되었습니다.')
            if self._source_dirty and self.page:
                saved = self.library.save_page_text(self.page['id'], self.source_editor.get('1.0', 'end-1c'),
                    expected_updated_at=self.page['updated_at'])
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
            self.document = self.library.get_document(self.document['id'])
            self.title_var.set(self.document['title'])
            self.save_state.set('저장됨')
            self._update_output_staleness()
            if title_changed:
                self.refresh_library()
            return True
        except Exception:
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
        if self.editor_tabs.select() == str(self.table_panel):
            self.table_view.set_text(self.source_editor.get('1.0', 'end-1c'))

    def _ai_mode_changed(self, _event=None):
        if self.ai_mode.get() == '안내문':
            self.audience_picker.pack(side='left', padx=2, before=self.generate_button)
        else:
            self.audience_picker.pack_forget()

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
        except Exception:
            self.status.set('페이지 순서를 저장하지 못했습니다. 원래 순서는 유지합니다.')

    def new_text_document(self):
        if not self.flush_edits():
            return
        try:
            doc = self.library.create_document('새 텍스트 문서')
            self.library.add_text_page(doc['id'], '', source_name='직접 입력')
            self.refresh_library(doc['id'])
            self.open_document(doc['id'])
        except Exception:
            self.status.set('문서를 만들지 못했습니다. 저장 공간을 확인하세요.')

    def adopt_current(self):
        if not self.document or not self.document.get('readonly') or self.document.get('trashed') or not self.flush_edits():
            return
        try:
            if str(self.document['id']).startswith('capture:'):
                capture = self.library.capture_store.get(self.document['id'].split(':', 1)[1])
                doc = self.library.create_document(self.document['title'])
                self._transfer().inherit_document_policy(capture.linked_task_ids, [capture.id], doc['id'])
                self.library.add_capture(doc['id'], capture.id)
            else:
                doc = self.library.create_document(self.document['title'] + ' · 사본')
                # Preserve an old integrated manuscript; never guess page splits.
                self._copy_legacy_policy(self.document['id'], doc['id'])
                self.library.add_text_page(doc['id'], self.library.document_text(self.document['id']), source_name='기존 통합 원문 사본')
            self.refresh_library(doc['id'])
            self.open_document(doc['id'])
        except Exception:
            self.status.set('가져오지 못했습니다. 기존 기록은 변경하지 않았습니다.')

    def toggle_trash(self):
        if not self.document or not self.flush_edits():
            return
        restore = self.document.get('trashed', False)
        raw_capture = str(self.document['id']).startswith('capture:')
        if self.document.get('readonly') and not (restore and raw_capture):
            self.status.set('기존 기록은 읽기 전용으로 보존합니다.')
            return
        if not restore and not messagebox.askyesno('휴지통', '문서를 휴지통으로 옮길까요? 원본은 삭제되지 않으며 복원할 수 있습니다.', parent=self):
            return
        try:
            if raw_capture:
                self.library.capture_store.restore([self.document['id'].split(':', 1)[1]])
            else:
                (self.library.restore if restore else self.library.trash)(self.document['id'], expected_updated_at=self.document['updated_at'])
            self.document = None
            self.page = None
            self.output = None
            self.page_records = []
            self.title_var.set('')
            self.page_selector.configure(values=[])
            self.page_selector.set('')
            self._replace(self.source_editor, '', readonly=True)
            self._replace(self.output_editor, '', readonly=True)
            self.image_view.set_image(None)
            self.table_view.set_text('')
            self.refresh_library()
            self.status.set('복원했습니다.' if restore else '휴지통으로 옮겼습니다. 원본은 보존됩니다.')
        except Exception:
            self.status.set('휴지통 상태를 저장하지 못했습니다.')

    def toggle_library(self):
        if self.winfo_width() < 1050:
            self._compact_library = not self._compact_library
        else:
            self._library_requested = not self._library_requested
        self._apply_layout()

    def toggle_compact_view(self):
        self._compact_image = not self._compact_image
        self._apply_layout()

    def _on_resize(self, event):
        if event.widget == self:
            self._apply_layout()
            self.status_label.configure(wraplength=max(350, self.winfo_width() - 200))

    def _apply_layout(self):
        compact = self.winfo_width() < 1050
        show_library = self._compact_library if compact else self._library_requested
        panes = self.main_split.panes()
        if show_library and str(self.library_panel) not in panes:
            self.main_split.insert(0, self.library_panel, weight=0)
        elif not show_library and str(self.library_panel) in panes:
            self.main_split.forget(self.library_panel)
        desired = [self.image_panel if self._compact_image else self.editor_panel] if compact else [self.image_panel, self.editor_panel]
        if list(self.work_split.panes()) != [str(panel) for panel in desired]:
            for pane in self.work_split.panes():
                self.work_split.forget(pane)
            for panel in desired:
                self.work_split.add(panel, weight=1)
        self._layout_compact = compact

    def _readonly_view(self, title, text):
        window = tk.Toplevel(self)
        window.title(title)
        window.geometry('760x580')
        editor = scrolledtext.ScrolledText(window, wrap='word', font=('Malgun Gothic', 11), padx=12, pady=12)
        editor.pack(fill='both', expand=True)
        editor.insert('1.0', text)
        highlight_source(editor)
        editor.configure(state='disabled')
        ttk.Button(window, text='전체 복사', command=lambda: self.copy_text(text)).pack(pady=7)
        return window

    def show_original_text(self):
        if self.page:
            latest = self.page.get('ocr_text', '')
            initial = self.library.initial_ocr(self.page['id']) if not self.page.get('legacy') else None
            text = initial if initial is not None else latest
            if initial is not None and initial != latest:
                text = '처음 인식한 내용\n\n' + initial + '\n\n────────\n최근 인식한 내용\n\n' + latest
            return self._readonly_view('원래 인식 내용 · 현재 수정본은 유지됩니다', text)

    def show_legacy_records(self):
        from services.legacy_reader import read_legacy_records
        try:
            return self._readonly_view('기존 기록 · 읽기 전용', read_legacy_records(self.library.app_data_dir))
        except Exception:
            self.status.set('기존 기록을 읽지 못했습니다. 원본 DB는 변경하지 않았습니다.')

    def show_output_history(self):
        if not self.document or self.document.get('readonly'):
            return
        outputs = self.library.outputs(self.document['id'])
        window = tk.Toplevel(self)
        window.title('이전 AI 결과 · 선택해서 열기')
        window.geometry('460x360')
        listing = tk.Listbox(window, exportselection=False)
        listing.pack(fill='both', expand=True, padx=10, pady=10)
        for item in outputs:
            listing.insert('end', f"{item['mode']} · {item['created_at'][:19]}")
        document_id = self.document['id']
        def choose():
            selection = listing.curselection()
            if selection and self.document and self.document['id'] == document_id and self.flush_edits():
                try:
                    selected_id = outputs[selection[0]]['id']
                    fresh = next(item for item in self.library.outputs(document_id) if item['id'] == selected_id)
                    self._load_output(fresh)
                    self.editor_tabs.select(self.ai_panel)
                    window.destroy()
                except Exception:
                    self.status.set('저장된 결과를 읽지 못했습니다. 현재 입력은 유지합니다.')
        ttk.Button(window, text='열기', command=choose).pack(pady=8)
        return window

    def _update_output_staleness(self):
        if self.document and self.output:
            current = fingerprint(self.library.document_text(self.document['id']))
            if current != self.output.get('source_fingerprint'):
                self.output_note.set('이전 원문 또는 선택 부분 기반 결과 · 현재 원문과 대조하세요.')

    # OCR/AI operations are connected below; storage and editing never require AI.
    def _capture_mode_changed(self, _event=None):
        mode = self.capture_mode.get()
        try:
            if mode == '자동 인식' and self.library.get_setting('automatic_ocr_consent', False) is not True:
                approved = messagebox.askyesno('자동 OCR 설정',
                    '앞으로 새로 캡처한 이미지가 자동으로 OpenAI API에 전송되고 비용이 발생합니다.\n'
                    '기관의 외부 AI 이용 기준을 확인해 주세요. 민감한 자료는 캡처 전에 보관만 또는 가리고 읽기를 선택하세요.\n\n자동 인식을 켤까요?', parent=self)
                if not approved:
                    self.capture_mode.set('보관만')
                    return
                self.library.set_setting('automatic_ocr_consent', True)
            self.library.set_setting('capture_mode', self.capture_mode.get())
        except Exception:
            self.capture_mode.set('보관만')
            self.status.set('설정을 저장하지 못해 보관만 모드로 유지합니다.')

    def capture_new(self):
        self._capture(add=False)

    def capture_page(self):
        self._capture(add=True)

    def _capture(self, add=False):
        if not self.flush_edits():
            return
        if add and (not self.document or self.document.get('readonly') or self.document.get('trashed')):
            self.status.set('페이지를 추가할 새 문서를 먼저 열어 주세요.')
            return
        doc_id = self.document['id'] if add else None
        self.withdraw()
        def select():
            try:
                image = capture_selected_region(self)
            except Exception:
                self.status.set('캡처하지 못했습니다. 다시 시도하세요.')
                image = None
            finally:
                self.deiconify()
            if image is not None:
                self.accept_capture(image, document_id=doc_id)
        self.after_idle(select)

    def accept_capture(self, image, *, document_id=None, source='capture', auto_read=True):
        """Durable original first; recoverable orphan if document linking fails."""
        record = None
        try:
            record = self.library.capture_store.save(image, source=source)
            doc = self.library.get_document(document_id) if document_id else self.library.create_document('새 캡처')
            page = self.library.add_capture(doc['id'], record.id)
            self.refresh_library(doc['id'])
            self.open_document(doc['id'])
            self._load_page(next(i for i, value in enumerate(self.page_records) if value['id'] == page['id']))
            self.status.set('캡처를 보관했습니다.')
            if auto_read and self.capture_mode.get() != '보관만':
                self._read_image(page, automatic=True)
            return page
        except Exception:
            self.refresh_library()
            self.status.set('원본 이미지는 보관했습니다. 문서 연결은 다시 시도하세요.' if record else
                            '캡처 보관을 완료하지 못했습니다. 저장 공간과 보관함을 확인하세요. 저장된 PNG는 조회·재시작 때 복구합니다.')
            return None

    def _transfer(self):
        if not hasattr(self, '_desk_transfer'):
            from services.desk_transfer import DeskTransfer
            from services.transfer_policy import TransferPolicyStore
            self._desk_transfer = DeskTransfer(TransferPolicyStore(self.library.app_data_dir))
        return self._desk_transfer

    def _choose_transfer(self, **kwargs):
        from ui.transfer_dialog import choose_transfer
        return choose_transfer(self, **kwargs)

    def _scope_tokens(self, document_id, capture_ids=(), document_ids=()):
        keys = ['document:' + value for value in dict.fromkeys([document_id, *document_ids])]
        keys += ['capture:' + value for value in capture_ids]
        store = self._transfer().policy_store
        return {key: (policy.to_dict() if (policy := store.get(key)) else None) for key in keys}

    def _scopes_match(self, tokens):
        store = self._transfer().policy_store
        return all((policy.to_dict() if (policy := store.get(key)) else None) == expected for key, expected in tokens.items())

    def _copy_legacy_policy(self, old_id, new_id):
        captures = self.library.capture_store.captures_for_task(old_id)
        self._transfer().inherit_document_policy([old_id], [capture.id for capture in captures], new_id)

    def read_current_page(self):
        if not self.page or not self.document or self.document.get('readonly') or self.document.get('trashed') or not self.flush_edits():
            self.status.set('편집할 문서를 선택하세요. 기존 기록은 먼저 새 문서로 가져오세요.')
            return
        if self.page.get('capture_id'):
            self._read_image(self.page, automatic=False)
        elif self.page.get('source_path'):
            self._read_file(self.page)
        else:
            self.status.set('이 페이지는 직접 입력한 텍스트입니다.')

    def _read_image(self, page, *, automatic=False):
        from services.ocr_service import extract_text_from_image
        if any(meta['kind'] == 'ocr' and meta.get('capture_id') == page['capture_id'] for meta in self._jobs.values()):
            self.status.set('이 페이지를 읽고 있습니다.')
            return
        try:
            capture = self.library.capture_store.get(page['capture_id'])
            image = self.library.capture_store.load(capture)
            mode = self.capture_mode.get()
            auto_allowed = automatic and mode == '자동 인식' and self.library.get_setting('automatic_ocr_consent', False) is True
            document_ids = tuple(dict.fromkeys([page['document_id'], *capture.linked_task_ids]))
            snapshot = self._transfer().image_snapshot(capture.id, image, auto_allowed=auto_allowed,
                force_mask=mode == '가리고 읽기', document_ids=document_ids, choose=self._choose_transfer)
            if snapshot is None:
                self.status.set('읽기를 취소했습니다. 캡처 원본은 보관되어 있습니다.')
                return
            metadata = {'document_id': page['document_id'], 'page_id': page['id'], 'capture_id': capture.id,
                'snapshot': snapshot, 'scopes': self._scope_tokens(page['document_id'], [capture.id], document_ids)}
            self._start_job('ocr', lambda cancel: extract_text_from_image(snapshot.as_image(), detail='high', raise_errors=True), metadata)
        except Exception:
            self.status.set('읽기를 시작하지 못했습니다. 전송 범위와 원본을 확인하세요. 원본으로 대체 전송하지 않았습니다.')

    def open_file(self):
        if not self.flush_edits():
            return
        selected = filedialog.askopenfilename(parent=self, title='이미지 또는 문서 열기', filetypes=[
            ('지원 파일', '*.png;*.jpg;*.jpeg;*.bmp;*.webp;*.hwpx;*.txt;*.md;*.csv;*.tsv;*.pdf;*.docx;*.pptx;*.xlsx;*.doc;*.ppt;*.xls;*.rtf;*.odt;*.odp;*.ods'),
            ('모든 파일', '*.*')])
        if selected:
            self.import_file(Path(selected))

    def import_file(self, path):
        if not self.flush_edits():
            return
        path = Path(path)
        kind = attachment_kind(path)
        try:
            if kind == 'image':
                with Image.open(path) as image:
                    page = self.accept_capture(image.copy(), source='file_' + path.stem, auto_read=False)
                # File attachments require explicit choice, regardless of auto OCR.
                if page and self.capture_mode.get() != '보관만':
                    self._read_image(page, automatic=False)
                return
            if kind in ('legacy_hwp', 'unsupported'):
                self.status.set('지원하지 않는 형식입니다. HWPX/PDF로 변환하거나 화면을 캡처하세요.')
                return
            text = (read_hwpx_file(path) if kind == 'hwpx' else read_text_file(path)) if kind in ('hwpx', 'text') else ''
            doc = self.library.create_document(path.stem)
            page = self.library.add_text_page(doc['id'], text, source_name=path.name, source_path=str(path))
            self.refresh_library(doc['id'])
            self.open_document(doc['id'])
            if kind == 'openai_document':
                self._read_file(page)
            else:
                self.status.set('로컬에서 읽었습니다. 외부 전송 없음.')
        except Exception:
            self.status.set('파일을 읽지 못했습니다. 원본 형식과 표 구조를 확인하세요. 기존 자료는 유지합니다.')

    def _read_file(self, page):
        from services.ocr_service import extract_text_from_file, extract_text_from_image
        path = Path(page['source_path'])
        kind = attachment_kind(path)
        try:
            if kind in ('hwpx', 'text'):
                text = read_hwpx_file(path) if kind == 'hwpx' else read_text_file(path)
                self.library.remember_initial_ocr(page['id'], page.get('ocr_text') or text)
                self.library.update_page_ocr(page['id'], text, expected_updated_at=page['updated_at'])
                self._refresh_current_page(page['document_id'], page['id'])
                self.status.set('로컬 원문을 다시 읽었습니다. 수정본은 유지합니다.')
                return
            policy_store = self._transfer().policy_store
            key = 'document:' + page['document_id']
            previous = policy_store.get(key)
            snapshot = self._choose_transfer(kind='file', path=path, previous=previous, title='첨부 파일 전송 확인')
            if snapshot is None:
                self.status.set('전송을 취소했습니다. 파일 경로는 문서에 남아 있습니다.')
                return
            # Only the selected immutable copy reaches a worker.
            policy_store.save(key, snapshot.policy, expected_scope_id=previous.scope_id if previous else None)
            metadata = {'document_id': page['document_id'], 'page_id': page['id'],
                'expected_updated_at': page['updated_at'], 'snapshot': snapshot,
                'scopes': self._scope_tokens(page['document_id'])}
            def read(cancel):
                if snapshot.kind == 'text':
                    return snapshot.text
                if snapshot.kind == 'image':
                    return extract_text_from_image(snapshot.as_image(), detail='high', raise_errors=True)
                return extract_text_from_file(path, mime_type_for(path), raise_errors=True,
                    file_bytes=snapshot.file_bytes, filename=snapshot.file_name)
            self._start_job('file', read, metadata)
        except Exception:
            self.status.set('파일 읽기를 시작하지 못했습니다. 원본과 전송 범위를 확인하세요.')

    def generate(self):
        from services.text_actions import generate_text_action
        if not self.document or self.document.get('readonly') or self.document.get('trashed') or not self.flush_edits():
            self.status.set('편집할 문서를 먼저 열어 주세요.')
            return
        if any(meta['kind'] == 'ai' and meta['document_id'] == self.document['id'] for meta in self._jobs.values()):
            self.status.set('이 문서의 AI 정리가 진행 중입니다.')
            return
        full_text = self.library.document_text(self.document['id'])
        text = full_text
        if self.selection_only.get():
            try:
                text = self.source_editor.get('sel.first', 'sel.last')
            except tk.TclError:
                self.status.set('텍스트 탭에서 정리할 부분을 선택하세요.')
                return
        if not text.strip():
            self.status.set('먼저 캡처를 읽거나 텍스트를 입력하세요.')
            return
        try:
            capture_ids = [page['capture_id'] for page in self.library.pages(self.document['id']) if page.get('capture_id')]
            snapshot = self._transfer().text_snapshot(self.document['id'], text, page_policies=capture_ids, choose=self._choose_transfer)
            if snapshot is None:
                self.status.set('AI 정리를 취소했습니다. 원문과 기존 결과는 유지합니다.')
                return
            mode = self.ai_mode.get()
            audience = self.audience.get() if mode == '안내문' else '교직원'
            label = mode + (' · ' + audience if mode == '안내문' else '') + (' · 선택 부분' if self.selection_only.get() else '')
            metadata = {'document_id': self.document['id'], 'mode': label,
                'source_fingerprint': fingerprint(full_text), 'output_revision': self._output_revision,
                'snapshot': snapshot, 'scopes': self._scope_tokens(self.document['id'], capture_ids)}
            self._start_job('ai', lambda cancel: generate_text_action(snapshot.text, mode, audience, cancel_event=cancel), metadata)
            self.editor_tabs.select(self.ai_panel)
        except Exception:
            self.status.set('AI 정리를 시작하지 못했습니다. 전송 범위를 다시 확인하세요.')

    def _start_job(self, kind, operation, metadata):
        snapshot = metadata['snapshot']
        scope_key = ('capture:' + metadata['capture_id']) if kind == 'ocr' else ('document:' + metadata['document_id'])
        if metadata['scopes'].get(scope_key) != snapshot.policy.to_dict() or not self._scopes_match(metadata['scopes']):
            raise ValueError('전송 범위가 변경되어 요청을 시작하지 않았습니다.')
        identifier = uuid4().hex
        cancel = threading.Event()
        self._jobs[identifier] = {**metadata, 'kind': kind, 'cancel': cancel}
        def work():
            try:
                if cancel.is_set():
                    self._results.put((identifier, False, '취소됨'))
                    return
                if not self._scopes_match(metadata['scopes']):
                    raise ValueError('전송 범위 변경')
                value = operation(cancel)
                self._results.put((identifier, True, value))
            except Exception:
                # SDK exception strings may contain request details; never persist them.
                self._results.put((identifier, False, '요청이 완료되지 않았습니다. 연결과 입력을 확인한 뒤 다시 시도하세요.'))
        try:
            threading.Thread(target=work, daemon=True).start()
        except Exception:
            self._jobs.pop(identifier, None)
            raise
        self.status.set('텍스트를 읽고 있습니다… 원본은 보관됨' if kind != 'ai' else 'AI 정리 중… 기존 결과는 유지됩니다.')
        return identifier

    def cancel_jobs(self):
        for metadata in self._jobs.values():
            metadata['cancel'].set()
        self.status.set('처리를 취소했습니다. 이미 전송된 요청의 비용은 발생할 수 있습니다. 원본은 유지합니다.')

    def _poll_results(self):
        while not self._results.empty():
            identifier, success, value = self._results.get_nowait()
            metadata = self._jobs.pop(identifier, None)
            if not metadata or metadata['cancel'].is_set() or self._closing:
                continue
            try:
                doc = self.library.get_document(metadata['document_id'])
                if doc.get('trashed') or not self._scopes_match(metadata.get('scopes', {})):
                    self.status.set('문서 또는 전송 범위가 변경되어 이전 응답을 적용하지 않았습니다.')
                    continue
                if not success:
                    if metadata['kind'] == 'ocr':
                        before = next(p for p in self.library.pages(metadata['document_id']) if p['id'] == metadata['page_id'])
                        self.library.capture_store.set_ocr_failure(metadata['capture_id'], value, profile='gpt-5-nano')
                        self._refresh_current_page(metadata['document_id'], metadata['page_id'], previous_token=before['updated_at'])
                    self.status.set(value + ' 원본과 마지막 성공 결과는 유지했습니다.')
                    continue
                if not isinstance(value, str) or not value.strip():
                    self.status.set('완료된 텍스트가 없어 기존 내용을 유지했습니다.')
                    continue
                if metadata['kind'] == 'ocr':
                    before = next(p for p in self.library.pages(metadata['document_id']) if p['id'] == metadata['page_id'])
                    self.library.remember_initial_ocr(metadata['page_id'], before.get('ocr_text') or value)
                    self._transfer().record_ocr(metadata['capture_id'], metadata['snapshot'], value)
                    self.library.capture_store.update_ocr(metadata['capture_id'], value, profile='gpt-5-nano')
                    self._refresh_current_page(metadata['document_id'], metadata['page_id'], previous_token=before['updated_at'])
                    self.status.set('텍스트 인식 완료 · 직접 수정한 텍스트는 유지합니다.')
                elif metadata['kind'] == 'file':
                    before = next(p for p in self.library.pages(metadata['document_id']) if p['id'] == metadata['page_id'])
                    self.library.remember_initial_ocr(metadata['page_id'], before.get('ocr_text') or value)
                    self.library.update_page_ocr(metadata['page_id'], value, expected_updated_at=metadata['expected_updated_at'])
                    self._refresh_current_page(metadata['document_id'], metadata['page_id'], previous_token=metadata['expected_updated_at'])
                    self.status.set('파일 읽기 완료 · 원문과 대조해 활용하세요.')
                else:
                    output = self.library.save_output(metadata['document_id'], metadata['mode'], value, metadata['source_fingerprint'])
                    current = self.document and self.document['id'] == metadata['document_id']
                    unchanged = current and not self._source_dirty and not self._output_dirty and self._output_revision == metadata['output_revision']
                    unchanged = unchanged and fingerprint(self.library.document_text(metadata['document_id'])) == metadata['source_fingerprint']
                    if unchanged:
                        self._load_output(output)
                        self.editor_tabs.select(self.ai_panel)
                        self.status.set('AI 정리를 보관했습니다. 사용 전 원문과 대조하세요.')
                    else:
                        self.status.set('AI 정리는 이전 결과 목록에 보관했습니다. 작업 중인 입력은 유지합니다.')
                self.refresh_library()
            except Exception:
                self.status.set('응답을 저장하지 못했습니다. 현재 입력과 원본을 유지합니다. 저장 상태를 확인하세요.')
        if not self._closing:
            self._poll_id = self.after(80, self._poll_results)

    def _refresh_current_page(self, document_id, page_id, previous_token=None):
        if not self.document or self.document['id'] != document_id or not self.page or self.page['id'] != page_id:
            return
        fresh = self.library.pages(document_id)
        index = next(i for i, page in enumerate(fresh) if page['id'] == page_id)
        if self._source_dirty:
            # Rebase only an OCR-only update against the same saved manuscript.
            if previous_token is not None and self.page['updated_at'] == previous_token:
                self.page_records, self.page = fresh, fresh[index]
            return
        self.page_records = fresh
        self._load_page(index)

    def close(self):
        if not self.flush_edits():
            return False
        if self._jobs and not messagebox.askyesno('처리 중', '처리를 취소하고 닫을까요? 이미 보관한 원본과 저장한 텍스트는 남습니다.', parent=self):
            return False
        self._closing = True
        self.cancel_jobs()
        for callback in (self._poll_id, self._autosave_id, self._refresh_id):
            if callback:
                try:
                    self.after_cancel(callback)
                except tk.TclError:
                    pass
        self.thumbnails.clear()
        self.destroy()
        return True


if __name__ == '__main__':
    CaptureDeskApp().mainloop()
