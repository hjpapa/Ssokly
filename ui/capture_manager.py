"""Visual capture organizer; all mutations go through versioned library methods."""
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

from ui.desk_widgets import ZoomImageView, ThumbnailCache


class CaptureManager(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.library = app.library
        self.title('캡처 관리')
        self.geometry('1000x650')
        self.minsize(660, 480)
        self.transient(app)
        self.query = tk.StringVar(self)
        self.label = tk.StringVar(self, '전체 라벨')
        self.sort = tk.StringVar(self, '최근 수정순')
        self.note = tk.StringVar(self)
        self.multi = tk.BooleanVar(self, False)
        self._filters_open = False
        self.limit = 150
        self.cache = ThumbnailCache(self)
        self.photos = {}
        top = ttk.Frame(self, padding=8)
        top.pack(fill='x')
        ttk.Label(top, text='검색').pack(side='left')
        entry = ttk.Entry(top, textvariable=self.query)
        entry.pack(side='left', fill='x', expand=True, padx=6)
        entry.bind('<Return>', lambda e: self.render())
        ttk.Button(top, text='검색', command=self.render).pack(side='left')
        self.filter_button = ttk.Button(top, text='필터·정렬', command=self.toggle_filters)
        self.filter_button.pack(side='left', padx=4)
        filters = ttk.Frame(self, padding=(8, 0, 8, 6))
        self.filters = filters
        self.labels = ttk.Combobox(filters, state='readonly', textvariable=self.label, width=16)
        self.labels.pack(side='left')
        self.labels.bind('<<ComboboxSelected>>', lambda e: self.render())
        order = ttk.Combobox(filters, state='readonly', textvariable=self.sort,
                            values=('최근 수정순', '이름순', '문서·쪽 순'), width=14)
        order.pack(side='left', padx=5)
        order.bind('<<ComboboxSelected>>', lambda e: self.render())
        ttk.Button(filters, text='전체 보기', command=self.reset_filters).pack(side='left')
        ttk.Button(filters, text='새로고침', command=self.refresh).pack(side='left', padx=4)
        actions = ttk.Frame(self, padding=(8, 0, 8, 6))
        self.actions = actions
        actions.pack(fill='x')
        self.open_button = ttk.Button(actions, text='열기', command=self.open_selected, state='disabled')
        self.open_button.pack(side='left', padx=2)
        self.edit_menu = tk.Menu(self, tearoff=False)
        self.edit_menu.add_command(label='이름 변경', command=self.rename)
        self.edit_menu.add_command(label='라벨·메모', command=self.edit_details)
        self.edit_button = ttk.Menubutton(actions, text='편집', menu=self.edit_menu, state='disabled')
        self.edit_button.pack(side='left', padx=2)
        self.move_button = ttk.Button(actions, text='이동', command=self.move, state='disabled')
        self.move_button.pack(side='left', padx=2)
        self.trash_button = ttk.Button(actions, text='삭제', command=self.trash, state='disabled')
        self.trash_button.pack(side='left', padx=2)
        selection = ttk.Frame(self, padding=(8, 0))
        selection.pack(fill='x')
        ttk.Checkbutton(selection, text='여러 장 선택', variable=self.multi, command=self.toggle_multi).pack(side='left')
        self.select_all = ttk.Button(selection, text='표시된 항목 전체 선택', command=lambda: self.tree.selection_set(self.tree.get_children()))
        self.more_button = ttk.Button(selection, text='더 보기', command=self.more)
        ttk.Label(self, textvariable=self.note, padding=8, wraplength=640).pack(side='bottom', fill='x')
        panes = tk.PanedWindow(self, orient='horizontal', sashwidth=6)
        panes.pack(fill='both', expand=True, padx=8, pady=8)
        listing = ttk.Frame(panes)
        self.tree = ttk.Treeview(listing, columns=('name',), show='tree headings', selectmode='browse', style='CaptureManager.Treeview')
        ttk.Style(self).configure('CaptureManager.Treeview', rowheight=90)
        self.tree.heading('#0', text='미리보기')
        self.tree.column('#0', width=110, minwidth=105, stretch=False)
        self.tree.heading('name', text='캡처 이름 / 문서 · 라벨')
        self.tree.column('name', width=240, minwidth=100)
        scroll = ttk.Scrollbar(listing, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.tree.pack(fill='both', expand=True)
        self.preview = ZoomImageView(panes)
        panes.add(listing, minsize=270, width=430)
        panes.add(self.preview, minsize=180)
        self.tree.bind('<<TreeviewSelect>>', self.preview_selected)
        self.tree.bind('<Double-1>', lambda e: None if self.multi.get() else self.open_selected())
        self.tree.bind('<Button-3>', self.context_menu)
        self.tree.bind('<Button-1>', self.select_clicked)
        self.refresh()
        self.grab_set()

    def toggle_filters(self):
        self._filters_open = not self._filters_open
        if self._filters_open:
            self.filters.pack(fill='x', before=self.actions)
        else:
            self.filters.pack_forget()
        self.update_filter_caption()

    def update_filter_caption(self):
        active = self.label.get() != '전체 라벨' or self.sort.get() != '최근 수정순'
        self.filter_button.configure(text=('필터·정렬 닫기' if self._filters_open else '필터·정렬') + (' · 적용 중' if active else ''))

    def reset_filters(self):
        self.query.set('')
        self.label.set('전체 라벨')
        self.sort.set('최근 수정순')
        self.render()

    def toggle_multi(self):
        self.tree.configure(selectmode='extended' if self.multi.get() else 'browse')
        if self.multi.get():
            self.select_all.pack(side='left', padx=6)
        else:
            self.select_all.pack_forget()
            selected = self.tree.selection()
            self.tree.selection_set(selected[:1])
        self.preview_selected()

    def select_clicked(self, event):
        identity = self.tree.identify_row(event.y)
        if self.multi.get() and identity and not event.state & 5:
            if identity in self.tree.selection():
                self.tree.selection_remove(identity)
            else:
                self.tree.selection_add(identity)
            self.tree.focus(identity)
            return 'break'

    def selected(self, multiple=True):
        ids = set(self.tree.selection())
        rows = [self.by_id[i] for i in self.tree.get_children() if i in ids]
        if not rows or (not multiple and len(rows) != 1):
            raise ValueError('캡처를 선택하세요.' if multiple else '캡처 한 장을 선택하세요.')
        if any(p.get('document_readonly') for p in rows):
            raise ValueError('기존 기록은 열기 후 새 문서로 가져와서 정리하세요.')
        return rows

    def refresh(self):
        try:
            rows = self.library.capture_pages()
            self.rows = rows
            self.by_id = {r['id']: r for r in rows}
            names = sorted({label for r in rows for label in r['labels']})
            self.labels.configure(values=('전체 라벨', *names))
            if self.label.get() not in names:
                self.label.set('전체 라벨')
            self.render()
        except Exception:
            self.rows = []
            self.by_id = {}
            self.tree.delete(*self.tree.get_children())
            self.preview.set_image(None)
            self.note.set('캡처 목록을 읽지 못했습니다. 새로고침하세요.')

    def render(self):
        selected = set(self.tree.selection())
        query = self.query.get().strip().casefold()
        rows = [p for p in self.rows if (self.label.get() == '전체 라벨' or self.label.get() in p['labels']) and
                (not query or query in ' '.join([p['source_name'], p['document_title'], p['memo'], p['text'], p.get('ocr_text', ''), *p['labels']]).casefold())]
        if self.sort.get() == '이름순':
            rows.sort(key=lambda p: (p['source_name'].casefold(), p['id']))
        elif self.sort.get() == '문서·쪽 순':
            rows.sort(key=lambda p: (p['document_title'].casefold(), p['document_id'], p['position']))
        else:
            rows.sort(key=lambda p: (p['document_version'], p['position'], p['id']), reverse=True)
        self.tree.delete(*self.tree.get_children())
        self.photos.clear()
        for page in rows[:self.limit]:
            photo = self.cache.get(page.get('path'), (100, 78)) if page.get('path') else None
            if photo:
                self.photos[page['id']] = photo
            caption = page['source_name'] or '캡처'
            detail = f"{page['document_title']} · {page['position'] + 1}쪽"
            labels = ', '.join(page['labels']) or '라벨 없음'
            self.tree.insert('', 'end', iid=page['id'], text='' if photo else '이미지 없음',
                             values=(caption + '\n' + detail + '\n' + labels,), **({'image': photo} if photo else {}))
        self.tree.selection_set([i for i in self.tree.get_children() if i in selected])
        self.visible_total = (min(len(rows), self.limit), len(rows))
        if len(rows) > self.limit:
            self.more_button.pack(side='right')
        else:
            self.more_button.pack_forget()
        self.update_filter_caption()
        self.preview_selected()

    def more(self):
        self.limit += 150
        self.render()

    def preview_selected(self, _event=None):
        ids = self.tree.selection()
        editable = bool(ids) and all(not self.by_id[i].get('document_readonly') for i in ids)
        self.open_button.configure(state='normal' if len(ids) == 1 else 'disabled')
        self.edit_button.configure(state='normal' if editable and len(ids) == 1 else 'disabled')
        for button in (self.move_button, self.trash_button):
            button.configure(state='normal' if editable else 'disabled')
        visible, total = getattr(self, 'visible_total', (0, 0))
        self.note.set(f'{len(ids)}장 선택 · 삭제한 캡처는 휴지통에서 복원할 수 있습니다.' if ids else
                      (f'{total}장 · 캡처를 선택하세요.' if visible == total else f'{total}장 중 {visible}장 표시 · 더 보기로 이어서 확인하세요.'))
        if not ids:
            self.preview.set_image(None)
            return
        page = self.by_id.get(ids[0])
        if page:
            self.preview.load_path(page.get('path'))

    def open_selected(self):
        ids = self.tree.selection()
        if not ids:
            self.note.set('열 캡처를 선택하세요.')
            return
        page = self.by_id[ids[0]]
        if self.app.open_document(page['document_id']):
            index = next((i for i, p in enumerate(self.app.page_records) if p['id'] == page['id']), None)
            if index is not None:
                self.app._load_page(index)
            self.destroy()

    def context_menu(self, event):
        identity = self.tree.identify_row(event.y)
        if not identity:
            return
        if identity not in self.tree.selection():
            self.tree.selection_set(identity)
        menu = tk.Menu(self, tearoff=False)
        ids = self.tree.selection()
        editable = all(not self.by_id[i].get('document_readonly') for i in ids)
        for text, command in (('열기', self.open_selected), ('이름 변경', self.rename), ('라벨·메모', self.edit_details),
                              ('선택 캡처 이동', self.move), ('선택 캡처 휴지통으로', self.trash)):
            allowed = len(ids) == 1 if text == '열기' else editable and (len(ids) == 1 if text in ('이름 변경', '라벨·메모') else True)
            menu.add_command(label=text, command=command, state='normal' if allowed else 'disabled')
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
            self.grab_set()

    def changed(self, affected):
        for job in self.app._jobs.values():
            if job.get('document_id') in affected:
                job['cancel'].set()
        if self.app.document and self.app.document['id'] in affected:
            identity = self.app.document['id']
            self.app._clear_document()
            doc = self.library.get_document(identity)
            if doc and not doc.get('trashed'):
                self.app.open_document(identity)
        self.app.refresh_library()
        self.refresh()

    def rename(self):
        try:
            page = self.selected(False)[0]
            name = simpledialog.askstring('캡처 이름', '이 문서에서 표시할 캡처 이름', initialvalue=page['source_name'], parent=self)
            if name is None:
                return
            self.library.rename_page(page['id'], name, expected_updated_at=page['document_version'])
            self.changed({page['document_id']})
            self.note.set('캡처 이름을 변경했습니다.')
        except ValueError as error:
            self.note.set(str(error))
        except Exception:
            self.note.set('이름을 저장하지 못했습니다. 새로고침 후 다시 시도하세요.')

    def edit_details(self):
        try:
            page = self.selected(False)[0]
        except ValueError as error:
            self.note.set(str(error))
            return
        dialog = tk.Toplevel(self)
        dialog.title('문서 라벨·메모')
        dialog.transient(self)
        dialog.geometry('480x270')
        ttk.Label(dialog, text=page['document_title'] + '\n이 문서의 모든 캡처에 적용됩니다.', padding=10).pack(fill='x')
        labels = tk.StringVar(dialog, ', '.join(page['labels']))
        memo = tk.StringVar(dialog, page['memo'])
        for caption, variable in (('라벨 (쉼표 구분)', labels), ('메모', memo)):
            ttk.Label(dialog, text=caption).pack(anchor='w', padx=10)
            ttk.Entry(dialog, textvariable=variable).pack(fill='x', padx=10, pady=4)
        note = tk.StringVar(dialog)
        ttk.Label(dialog, textvariable=note, wraplength=450).pack(fill='x', padx=10)
        def save():
            try:
                self.library.update_details(page['document_id'], labels.get(), memo.get(), expected_updated_at=page['document_version'])
                self.changed({page['document_id']})
                close()
            except ValueError as error:
                note.set(str(error))
            except Exception:
                note.set('저장하지 못했습니다. 입력을 복사해 보관하고 새로고침하세요.')
        def close():
            dialog.destroy()
            self.grab_set()
        ttk.Button(dialog, text='저장', command=save).pack(side='left', padx=10, pady=8)
        ttk.Button(dialog, text='취소', command=close).pack(side='right', padx=10)
        dialog.protocol('WM_DELETE_WINDOW', close)
        dialog.grab_set()

    def trash(self):
        try:
            rows = self.selected()
            if not messagebox.askyesno('선택 캡처 삭제', f'{len(rows)}장을 휴지통으로 옮길까요? 마지막 쪽을 삭제하면 문서도 이동합니다. 복원할 수 있습니다.', parent=self):
                return
            versions = {r['document_id']: r['document_version'] for r in rows}
            self.library.trash_pages([r['id'] for r in rows], expected_versions=versions)
            self.changed(set(versions))
            self.note.set(f'{len(rows)}장을 휴지통으로 옮겼습니다.')
        except ValueError as error:
            self.note.set(str(error))
        except Exception:
            self.note.set('삭제하지 못했습니다. 새로고침 후 다시 시도하세요.')

    def move(self):
        try:
            rows = self.selected()
            sources = {r['document_id'] for r in rows}
            targets = [d for d in self.library.list_documents() if not d.get('readonly') and d['id'] not in sources]
            if not targets:
                raise ValueError('이동할 다른 문서가 없습니다. 새 문서를 만든 후 다시 시도하세요.')
        except ValueError as error:
            self.note.set(str(error))
            return
        except Exception:
            self.note.set('대상 문서를 읽지 못했습니다. 새로고침 후 다시 시도하세요.')
            return
        dialog = tk.Toplevel(self)
        dialog.title('선택 캡처 이동')
        dialog.geometry('520x350')
        dialog.transient(self)
        ttk.Label(dialog, text=f'{len(rows)}장을 목록에 표시된 순서대로 대상 문서 뒤에 붙입니다.\n라벨·메모는 대상 문서를 따르며 기존 AI 결과는 원래 문서에 남습니다.', wraplength=480, padding=10).pack(fill='x')
        frame = ttk.Frame(dialog)
        frame.pack(fill='both', expand=True, padx=10)
        listing = ttk.Treeview(frame, show='tree', selectmode='browse')
        scrollbar = ttk.Scrollbar(frame, command=listing.yview)
        listing.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right', fill='y')
        listing.pack(fill='both', expand=True)
        for doc in targets:
            listing.insert('', 'end', iid=doc['id'], text=f"{doc['title']} · {doc['page_count']}쪽")
        note = tk.StringVar(dialog)
        ttk.Label(dialog, textvariable=note, wraplength=480).pack(fill='x', padx=10)
        def apply():
            ids = listing.selection()
            if not ids:
                note.set('대상 문서를 선택하세요.')
                return
            try:
                target = next(d for d in targets if d['id'] == ids[0])
                versions = {r['document_id']: r['document_version'] for r in rows}
                versions[target['id']] = target['updated_at']
                self.library.move_pages([r['id'] for r in rows], target['id'], expected_versions=versions)
                self.changed(set(versions))
                self.note.set(f'{len(rows)}장을 이동했습니다.')
                close()
            except ValueError as error:
                note.set(str(error))
            except Exception:
                note.set('이동하지 못했습니다. 새로고침 후 다시 시도하세요.')
        def close():
            dialog.destroy()
            self.grab_set()
        ttk.Button(dialog, text='선택 문서로 이동', command=apply).pack(side='left', padx=10, pady=8)
        ttk.Button(dialog, text='취소', command=close).pack(side='right', padx=10)
        dialog.protocol('WM_DELETE_WINDOW', close)
        dialog.grab_set()
