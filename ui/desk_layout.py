"""Responsive pane layout and read-only views for the capture desk."""
import tkinter as tk
from tkinter import scrolledtext, ttk

from services.source_review import highlight_source
from ui.desk_text import fingerprint


class LayoutMixin:
    def toggle_library(self):
        # A pane can still be present after its sash was dragged to zero.
        # Restore it with one click rather than first hiding that invisible pane.
        present = str(self.library_panel) in tuple(map(str, self.main_split.panes()))
        collapsed = (present and self.library_panel.winfo_ismapped() and len(self.main_split.panes()) == 2
                     and self.main_split.sashpos(0) < self._layout_metrics()[0])
        if self._is_compact():
            self._compact_library = True if collapsed else not self._compact_library
        else:
            self._library_requested = True if collapsed else not self._library_requested
        self._reset_main_sash = True
        self._apply_layout()

    def toggle_compact_view(self):
        # Kept as the reset command for existing callers; neither pane is hidden.
        self._compact_library = False
        self._reset_work_sash = True
        self._apply_layout()

    def _on_resize(self, event):
        if event.widget == self:
            self._apply_layout()
            self.status_label.configure(wraplength=max(350, self.winfo_width() - 200))

    def _layout_metrics(self):
        # Tk scaling is points-to-pixels. Requested Text widths are deliberately
        # not used: an 80-column editor used to squeeze the image to zero.
        scale = max(1.0, float(self.winfo_fpixels('1i')) / 96.0)
        library_min = round(180 * scale)
        library_width = round(220 * scale)
        image_min = max(round(280 * scale), self._page_bar.winfo_reqwidth(), self._page_actions.winfo_reqwidth())
        editor_min = max(round(320 * scale), self._copy_bar.winfo_reqwidth() + 20,
                         self._ai_mode_picker.winfo_reqwidth() + self.generate_button.winfo_reqwidth() + 20)
        return library_min, library_width, image_min, editor_min

    def toggle_details(self):
        if self.details_panel.winfo_manager():
            self.details_panel.pack_forget()
            self.details_button.configure(text='라벨·메모')
        else:
            self.details_panel.pack(fill='x', before=self.work_split)
            self.details_button.configure(text='라벨·메모 닫기')
        self._reset_work_sash = True
        self._schedule_layout()

    def _is_compact(self):
        _library_min, library_width, image_min, editor_min = self._layout_metrics()
        reserved = library_width + 6 if self._library_requested else 0
        return self.winfo_width() - 40 - reserved < image_min + editor_min + 6

    def _schedule_layout(self, _event=None):
        if not self._closing and self._layout_id is None:
            self._layout_id = self.after_idle(self._settle_layout)

    def _settle_layout(self):
        self._layout_id = None
        if self._closing:
            return
        self._apply_layout(schedule=False)
        library_min, library_width, image_min, editor_min = self._layout_metrics()
        if len(self.main_split.panes()) == 2:
            width = self.main_split.winfo_width()
            work_min = max(image_min, editor_min) if self._layout_compact else image_min + editor_min + 16
            upper = width - work_min - 16
            if upper >= library_min:
                current = self.main_split.sashpos(0)
                preferred = library_width if self._reset_main_sash else current
                target = min(upper, max(library_min, preferred))
                if current != target:
                    self.main_split.sashpos(0, target)
        if len(self.work_split.panes()) == 2:
            vertical = str(self.work_split.cget('orient')) == 'vertical'
            extent = self.work_split.winfo_height() if vertical else self.work_split.winfo_width()
            if vertical:
                # Reserve editable content as well as tabs/copy/re-read controls.
                # A fixed 160px editor pane left only one text line while busy.
                tab_height = max(panel.winfo_reqheight() for panel in (self.text_panel, self.table_panel, self.ai_panel))
                chrome = (self.editor_tabs.winfo_reqheight() - tab_height
                          + self.text_panel.winfo_reqheight() - self.source_editor.winfo_reqheight())
                page_bar = self._page_bar.winfo_reqheight()
                content_min = min(110, max(24, (extent - page_bar - chrome - 12) // 2))
                editor_height = chrome + content_min
                upper = extent - editor_height - 7
                lower = min(max(page_bar + content_min, int(extent * .40)), upper)
                lower = max(1, lower)
            else:
                lower, upper = image_min, extent - editor_min - 7
            if upper >= lower:
                current = self.work_split.sashpos(0)
                preferred = int(extent * (.50 if vertical else .52)) if self._reset_work_sash else current
                target = min(upper, max(lower, preferred))
                if current != target:
                    self.work_split.sashpos(0, target)
        self._reset_main_sash = self._reset_work_sash = False

    @staticmethod
    def _set_panes(split, desired):
        if tuple(map(str, split.panes())) == tuple(str(panel) for panel, _weight in desired):
            return False
        for pane in split.panes():
            split.forget(pane)
        for panel, weight in desired:
            split.add(panel, weight=weight)
        return True

    def _apply_layout(self, *, schedule=True):
        if self.document and self.document.get('readonly') and not self.document.get('trashed'):
            if not self.adopt_button.winfo_manager():
                self.adopt_button.pack(side='right', padx=5)
        else:
            self.adopt_button.pack_forget()
        compact = self._is_compact()
        library_min, _library_width, image_min, editor_min = self._layout_metrics()
        show_library = self._compact_library if compact else self._library_requested
        work_min = max(image_min, editor_min)
        library_only = compact and show_library and self.winfo_width() - 40 < library_min + work_min
        hint_width = max(220, self.winfo_width() - 40) if library_only else 220
        if int(self.search_hint.cget('wraplength')) != hint_width:
            self.search_hint.configure(wraplength=hint_width)
        main = [(self.library_panel, 0)] if show_library else []
        if not library_only:
            main.append((self.workspace, 1))
        if self._set_panes(self.main_split, main):
            self._reset_main_sash = True
        # Decide from the actual work area, after the library has been folded.
        # A compact sidebar must not force a shallow image strip on a wide desk.
        available = min(self.workspace.winfo_width() - 10, self.winfo_width() - 40)
        orientation = 'horizontal' if available >= image_min + editor_min + 16 else 'vertical'
        if str(self.work_split.cget('orient')) != orientation:
            self.work_split.configure(orient=orientation)
            self._reset_work_sash = True
        desired = [self.image_panel, self.editor_panel]
        if self._set_panes(self.work_split, [(panel, 1) for panel in desired]):
            self._reset_work_sash = True
        self._layout_compact = compact
        self.image_view.set_compact_tools(compact)
        self.view_button.configure(text='화면 비율 초기화')
        if compact:
            self._page_actions.pack_forget()
        elif not self._page_actions.winfo_manager():
            self._page_actions.pack(fill='x', before=self._page_bar)
        self.recovery_label.configure(wraplength=max(250, self.winfo_width() - 330))
        header_width = (self._header_logo.winfo_reqwidth() + self._header_primary.winfo_reqwidth()
                        + self._header_secondary.winfo_reqwidth() + 52)
        wrapped_header = self.winfo_width() < header_width
        self._header_primary.grid_configure(row=1 if wrapped_header else 0,
            column=0 if wrapped_header else 1, columnspan=3 if wrapped_header else 1)
        self._header_secondary.grid_configure(column=2)
        if compact:
            self._transfer_hint.pack_forget()
        elif not self._transfer_hint.winfo_manager():
            self._transfer_hint.pack(side='left')
        ai_width = self._ai_controls.winfo_width()
        if ai_width > 1:
            self.output_note_label.configure(wraplength=max(200, ai_width - 12), font=('Malgun Gothic', 8 if compact else 10))
            mode_width = self._ai_mode_picker.winfo_reqwidth() + 4
            audience_width = self.audience_picker.winfo_reqwidth() + 4 if self.ai_mode.get() == '안내문' else 0
            audience_row = int(mode_width + audience_width > ai_width)
            if audience_width:
                self._place_grid(self.audience_picker, row=audience_row, column=0 if audience_row else 1)
            generate_row = audience_row + 1 if mode_width + audience_width + self.generate_button.winfo_reqwidth() + 4 > ai_width else 0
            self._place_grid(self.generate_button, row=generate_row, column=0 if generate_row else 2)
        if schedule:
            self._schedule_layout()

    @staticmethod
    def _place_grid(widget, **coordinates):
        current = widget.grid_info()
        if any(str(current.get(key)) != str(value) for key, value in coordinates.items()):
            widget.grid_configure(**coordinates)

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
