"""Library, trash, label and page-organization actions for the capture desk."""
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from services.diagnostics import log_failure


class LibraryMixin:
    def refresh_library(self, select_id=None):
        previous = select_id or (self.document['id'] if self.document else None)
        try:
            records = self.library.list_documents(self.query.get(), trashed=self.show_trash.get())
        except Exception as error:
            log_failure('desk_library.refresh_library', error)
            self.status.set('보관함을 읽지 못했습니다. 기존 화면은 유지합니다.')
            return
        self._listing = {item['id']: item for item in records}
        available_labels = sorted({label for item in records for label in item.get('labels', [])}, key=str.casefold)
        current_filter = self.label_filter.get()
        if current_filter != '전체 라벨' and current_filter not in available_labels and not self.query.get().strip():
            current_filter = '전체 라벨'
            self.label_filter.set(current_filter)
        self.label_picker.configure(values=('전체 라벨', *sorted(set(available_labels + ([current_filter] if current_filter != '전체 라벨' else [])), key=str.casefold)))
        if current_filter != '전체 라벨':
            records = [item for item in records if current_filter in item.get('labels', [])]
        self.library_count.set(f"{'검색 결과' if self.query.get().strip() else '자료'} {len(records)}건")
        self.document_tree.delete(*self.document_tree.get_children())
        self._library_photos = {}
        for row_index, item in enumerate(records[:self._visible_count]):
            photo = self.thumbnails.get(item.get('thumbnail_path'), (52, 44)) if item.get('thumbnail_path') else None
            detail = f"{str(item.get('updated_at', ''))[:10]} · {item['page_count']}쪽"
            context = []
            if item.get('legacy'):
                context.append('미분류 캡처' if str(item['id']).startswith('capture:') else '기존 기록')
            if item.get('labels'):
                context.append('#' + item['labels'][0][:12])
            if item.get('recovery_required'):
                context.append('복구 필요')
            opts = {'image': photo} if photo else {}
            if photo:
                self._library_photos[item['id']] = photo
            memo = item.get('memo', '').replace('\n', ' ').strip()
            if memo and not context:
                context.append(memo[:20])
            caption = f"{item['title']}\n{detail}" + (f"\n{' · '.join(context)}" if context else '')
            self.document_tree.insert('', 'end', iid=item['id'], text=caption,
                                      tags=('alternate',) if row_index % 2 else (), **opts)
        if previous and self.document_tree.exists(previous):
            self.document_tree.selection_set(previous)
        self.document_trash_button.configure(state='normal' if self.document and self.document_tree.exists(self.document['id']) else 'disabled')

    def _search_changed(self, *_args):
        if self._refresh_id:
            self.after_cancel(self._refresh_id)
        self._visible_count = 250
        self._refresh_id = self.after(250, self._run_search)

    def _run_search(self):
        if self._refresh_id:
            self.after_cancel(self._refresh_id)
        self._refresh_id = None
        self.refresh_library()

    def _show_more(self):
        self._visible_count += 250
        self.refresh_library()

    def _document_selected(self, _event=None):
        selected = self.document_tree.selection()
        if selected and (not self.document or selected[0] != self.document['id']):
            self.open_document(selected[0])

    def new_text_document(self):
        if not self.flush_edits():
            return
        try:
            doc = self.library.create_document('새 텍스트 문서')
            self.library.add_text_page(doc['id'], '', source_name='직접 입력')
            self.refresh_library(doc['id'])
            self.open_document(doc['id'])
        except Exception as error:
            log_failure('desk_library.new_text_document', error)
            self.status.set('문서를 만들지 못했습니다. 저장 공간을 확인하세요.')

    def adopt_current(self):
        if not self.document or not self.document.get('readonly') or self.document.get('trashed') or self.document.get('recovery_required') or not self.flush_edits():
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
        except Exception as error:
            log_failure('desk_library.adopt_current', error)
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
            self._clear_document()
            self.refresh_library()
            self.status.set('복원했습니다.' if restore else '휴지통으로 옮겼습니다. 원본은 보존됩니다.')
        except Exception as error:
            log_failure('desk_library.toggle_trash', error)
            self.status.set('휴지통 상태를 저장하지 못했습니다.')

    def _clear_document(self):
        self.document = self.page = self.output = None
        self.page_records = []
        self.title_var.set('')
        self.labels_var.set('')
        self.memo_var.set('')
        self.page_selector.configure(values=[])
        self.page_selector.set('')
        self._replace(self.source_editor, '', readonly=True)
        self._replace(self.output_editor, '', readonly=True)
        self.image_view.set_image(None)
        self.table_view.set_text('')

    def _purge_snapshot(self, snapshot, parent=None):
        documents, pages = snapshot['documents'], snapshot['pages']
        if not documents and not pages:
            self.status.set('휴지통이 비어 있습니다.')
            return False
        if not messagebox.askyesno('영구 삭제',
                f'문서 {len(documents)}개(포함된 모든 페이지), 개별 페이지 {len(pages)}개를 영구 삭제할까요?\n'
                '라벨·메모·텍스트·결과와 다른 문서가 사용하지 않는 캡처 원본이 삭제됩니다. 복원할 수 없습니다.', parent=parent or self):
            return False
        try:
            result = self.library.purge_trash(snapshot)
            for job in self._jobs.values():
                if job.get('document_id') in documents or job.get('page_id') in pages:
                    job['cancel'].set()
            current = self.document['id'] if self.document else None
            if current in documents:
                self._clear_document()
            elif current:
                self.open_document(current)
            self.refresh_library()
            self.status.set('영구 삭제했습니다.' if not result['pending_files'] else
                            '목록에서 삭제했습니다. 사용 중인 일부 이미지 파일의 정리가 남아 있습니다.')
            return True
        except Exception as error:
            log_failure('desk_library._purge_snapshot', error)
            self.status.set('영구 삭제하지 못했습니다. 다른 창에서 변경했거나 파일을 사용 중일 수 있습니다. 휴지통을 새로고침하세요.')
            return False

    def purge_current_document(self):
        if not self.document or not self.document.get('trashed') or not self.flush_edits():
            self.status.set('문서 휴지통에서 영구 삭제할 문서를 선택하세요.')
            return False
        return self._purge_snapshot({'documents': {self.document['id']: self.document['updated_at']}, 'pages': {}})

    def empty_trash(self):
        if not self.flush_edits():
            return False
        try:
            snapshot = self.library.trash_snapshot()
        except Exception as error:
            log_failure('desk_library.empty_trash', error)
            self.status.set('휴지통 목록을 읽지 못했습니다.')
            return False
        return self._purge_snapshot(snapshot)

    def _page_context_menu(self, event):
        menu = tk.Menu(self, tearoff=False)
        allowed = self.page and self.document and not self.document.get('readonly') and not self.document.get('trashed')
        menu.add_command(label='이 페이지 삭제 · 휴지통으로', command=self.delete_current_page,
                         state='normal' if allowed else 'disabled')
        menu.add_command(label='삭제한 페이지 복원…', command=self.show_deleted_pages)
        menu.add_command(label='쪽을 새 문서로 분리…', command=self.show_split_dialog,
                         state='normal' if allowed and len(self.page_records) > 1 else 'disabled')
        menu.add_separator()
        menu.add_command(label='이미지 화면에 맞추기', command=self.image_view.fit)
        menu.add_command(label='이미지 확대', command=self.image_view.zoom_in)
        menu.add_command(label='이미지 축소', command=self.image_view.zoom_out)
        menu.add_command(label='이미지 100%', command=self.image_view.original_size)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return 'break'

    def _document_context_menu(self, event):
        selected = self.document_tree.identify_row(event.y)
        if not selected or not self.open_document(selected):
            return 'break'
        self.document_tree.selection_set(selected)
        menu = tk.Menu(self, tearoff=False)
        menu.add_command(label='문서 복원' if self.document.get('trashed') else '문서 삭제 · 휴지통으로', command=self.toggle_trash)
        if self.document.get('trashed'):
            menu.add_command(label='문서 영구 삭제…', command=self.purge_current_document)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return 'break'

    def delete_current_page(self):
        if not self.page or not self.document or self.document.get('readonly') or self.document.get('trashed'):
            return False
        if not self.flush_edits():
            return False
        index = next(i for i, page in enumerate(self.page_records) if page['id'] == self.page['id'])
        if not messagebox.askyesno('페이지 삭제', f'{index + 1}쪽을 휴지통으로 옮길까요?\n원본과 수정 텍스트는 보존되며 삭제한 페이지에서 복원할 수 있습니다.', parent=self):
            return False
        try:
            page_id, document_id = self.page['id'], self.document['id']
            self.library.set_page_trash(page_id, True, expected_updated_at=self.document['updated_at'])
            for job in self._jobs.values():
                if job.get('page_id') == page_id:
                    job['cancel'].set()
            if self.library.get_document(document_id).get('trashed'):
                self._clear_document()
            else:
                self.open_document(document_id)
            if self.page_records:
                self._load_page(min(index, len(self.page_records) - 1))
            self.refresh_library()
            self.status.set('페이지를 삭제했습니다. 삭제한 페이지 · 복원에서 되돌릴 수 있습니다.')
            return True
        except Exception as error:
            log_failure('desk_library.delete_current_page', error)
            self.status.set('페이지를 삭제하지 못했습니다. 최신 문서 상태를 확인하세요.')
            return False

    def show_deleted_pages(self):
        if not self.flush_edits():
            return
        dialog = tk.Toplevel(self)
        dialog.title('페이지 휴지통 · 복원 / 영구 삭제')
        dialog.geometry('640x420')
        dialog.transient(self)
        ttk.Label(dialog, text='마지막 페이지를 삭제하면 문서도 휴지통으로 이동합니다. 직접 삭제한 문서는 문서를 먼저 복원하세요.',
                  wraplength=590, padding=10).pack(fill='x')
        listing = ttk.Treeview(dialog, columns=('document', 'page'), show='headings', selectmode='browse')
        listing.heading('document', text='문서')
        listing.heading('page', text='삭제한 페이지')
        listing.pack(fill='both', expand=True, padx=10)
        note = tk.StringVar(dialog)
        ttk.Label(dialog, textvariable=note, padding=8).pack(fill='x')
        versions = {}

        def refresh():
            listing.delete(*listing.get_children())
            versions.clear()
            try:
                for page in self.library.deleted_pages():
                    versions[page['id']] = self.library.get_document(page['document_id'])['updated_at']
                    listing.insert('', 'end', iid=page['id'], values=(
                        ('[문서 휴지통] ' if page['document_trashed'] else '') + page['document_title'],
                        f"{page['position'] + 1}쪽 · {page['source_name'] or '캡처'}"))
                note.set(f'삭제한 페이지 {len(versions)}개')
            except Exception as error:
                log_failure('desk_library.refresh', error)
                note.set('삭제한 페이지를 읽지 못했습니다.')

        def restore():
            selected = listing.selection()
            if not selected or not self.flush_edits():
                return
            try:
                document_id = self.library.set_page_trash(selected[0], False, expected_updated_at=versions[selected[0]])
                self.open_document(document_id)
                self._load_page(next(i for i, page in enumerate(self.page_records) if page['id'] == selected[0]))
                self.refresh_library()
                refresh()
                note.set('페이지를 복원했습니다.')
            except Exception as error:
                log_failure('desk_library.restore', error)
                note.set('복원하지 못했습니다. 문서를 먼저 복원하거나 목록을 새로고침하세요.')

        actions = ttk.Frame(dialog, padding=10)
        actions.pack(fill='x')
        ttk.Button(actions, text='선택 페이지 복원', command=restore).pack(side='left')
        def purge():
            selected = listing.selection()
            if selected and self.flush_edits():
                if self._purge_snapshot({'documents': {}, 'pages': {selected[0]: versions[selected[0]]}}, parent=dialog):
                    refresh()
                    note.set('선택 페이지를 영구 삭제했습니다.')
                else:
                    note.set(self.status.get())
        ttk.Button(actions, text='선택 페이지 영구 삭제', command=purge).pack(side='left', padx=5)
        ttk.Button(actions, text='새로고침', command=refresh).pack(side='left', padx=5)
        ttk.Button(actions, text='닫기', command=dialog.destroy).pack(side='right')
        refresh()

    def choose_labels(self):
        if not self.document or self.document.get('readonly') or self.document.get('trashed') or not self.flush_edits():
            return
        document_id = self.document['id']
        try:
            names = list(self.library.label_counts())
        except Exception as error:
            log_failure('desk_library.choose_labels', error)
            self.status.set('라벨 목록을 읽지 못했습니다.')
            return
        dialog = tk.Toplevel(self)
        dialog.title('문서 라벨 선택')
        dialog.transient(self)
        ttk.Label(dialog, text='라벨을 클릭해 선택하거나 해제하세요. 새 라벨은 입력칸에서 추가합니다.', wraplength=380, padding=10).pack()
        listing = tk.Listbox(dialog, selectmode='multiple', exportselection=False, width=35, height=12)
        listing.pack(fill='both', expand=True, padx=10)
        for index, name in enumerate(names):
            listing.insert('end', name)
            if name in self.document.get('labels', []):
                listing.selection_set(index)

        def apply():
            chosen = [names[index] for index in listing.curselection()]
            if len(chosen) > 8:
                messagebox.showerror('라벨 선택', '라벨은 최대 8개까지 지정할 수 있습니다.', parent=dialog)
                return
            if not self.document or self.document['id'] != document_id:
                dialog.destroy()
                return
            self.labels_var.set(', '.join(chosen))
            if self.flush_edits():
                dialog.destroy()

        ttk.Button(dialog, text='선택 적용', command=apply).pack(pady=10)
        dialog.grab_set()

    def manage_labels(self):
        if not self.flush_edits():
            return
        dialog = tk.Toplevel(self)
        dialog.title('라벨 관리 · 이름 변경')
        dialog.transient(self)
        ttk.Label(dialog, text='이름 변경은 휴지통을 포함한 모든 문서에 반영됩니다.\n이미 있는 이름으로 바꾸면 라벨이 합쳐집니다.', padding=10).pack()
        listing = ttk.Treeview(dialog, columns=('name', 'count'), show='headings', selectmode='browse', height=12)
        listing.heading('name', text='라벨')
        listing.heading('count', text='문서 수')
        listing.pack(fill='both', expand=True, padx=10)
        note = tk.StringVar(dialog)
        ttk.Label(dialog, textvariable=note, padding=8).pack()

        def refresh():
            listing.delete(*listing.get_children())
            try:
                for name, count in self.library.label_counts().items():
                    listing.insert('', 'end', values=(name, count))
            except Exception as error:
                log_failure('desk_library.refresh', error)
                note.set('라벨을 읽지 못했습니다.')

        def rename():
            selected = listing.selection()
            if not selected or not self.flush_edits():
                return
            old = listing.item(selected[0], 'values')[0]
            new = simpledialog.askstring('라벨 이름 변경', f'「{old}」의 새 이름', initialvalue=old, parent=dialog)
            if new is None:
                return
            try:
                count = self.library.rename_label(old, new)
                if self.label_filter.get() == old:
                    self.label_filter.set(new.strip())
                if self.document:
                    self.document = self.library.get_document(self.document['id'])
                    self.labels_var.set(', '.join(self.document.get('labels', [])))
                self.refresh_library()
                refresh()
                note.set(f'{count}개 문서의 라벨을 변경했습니다.')
            except (ValueError, TypeError) as error:
                note.set(str(error))
            except Exception as error:
                log_failure('desk_library.rename', error)
                note.set('라벨 이름을 변경하지 못했습니다.')

        ttk.Button(dialog, text='선택 라벨 이름 변경', command=rename).pack(pady=8)
        refresh()
        dialog.grab_set()

    def _can_organize(self):
        if not self.document or self.document.get('readonly') or self.document.get('trashed'):
            self.status.set('편집 가능한 문서를 먼저 선택하세요.')
            return False
        return self.flush_edits()

    def show_capture_manager(self):
        if self.flush_edits():
            from ui.capture_manager import CaptureManager
            CaptureManager(self)

    def _finish_organize(self, document_id, affected_ids):
        for job in self._jobs.values():
            if job.get('document_id') in affected_ids:
                job['cancel'].set()
        self.show_trash.set(False)
        self.query.set('')
        self.open_document(document_id)
        self.refresh_library(select_id=document_id)

    def show_merge_dialog(self):
        if not self._can_organize():
            return
        target = dict(self.document)
        try:
            candidates = self.library.related_documents(target['id'])
        except Exception as error:
            log_failure('desk_library.show_merge_dialog', error)
            self.status.set('합칠 문서 목록을 읽지 못했습니다.')
            return
        dialog = tk.Toplevel(self)
        dialog.title('같은 라벨·메모 · 쪽 합치기')
        dialog.geometry('700x450')
        dialog.transient(self)
        ttk.Label(dialog, text=f"현재 문서: {target['title']}\n선택한 문서의 쪽을 아래 목록 순서대로 뒤에 붙입니다. Ctrl/Shift로 여러 문서를 선택하세요.",
                  wraplength=650, padding=10).pack(fill='x')
        listing = ttk.Treeview(dialog, columns=('title', 'pages', 'match'), show='headings', selectmode='extended')
        for name, caption, width in (('title', '문서', 260), ('pages', '쪽 수', 60), ('match', '같은 분류', 280)):
            listing.heading(name, text=caption)
            listing.column(name, width=width)
        listing.pack(fill='both', expand=True, padx=10)
        by_id = {doc['id']: doc for doc in candidates}
        for doc in candidates:
            listing.insert('', 'end', iid=doc['id'], values=(doc['title'], doc['page_count'], ' · '.join(doc['match_reasons'])))
        note = tk.StringVar(dialog, value='공통 라벨 또는 같은 메모가 있는 문서가 없습니다.' if not candidates else '합칠 문서를 선택하세요.')
        ttk.Label(dialog, textvariable=note, wraplength=650, padding=10).pack(fill='x')
        ttk.Label(dialog, text='현재 문서의 제목·라벨·메모는 유지됩니다.\n쪽을 모두 옮긴 문서는 휴지통으로 이동하며 기존 AI 결과·메모·삭제한 쪽은 그 문서에 남습니다.',
                  wraplength=650, padding=(10, 0)).pack(fill='x')

        def summarize(_event=None):
            selected = listing.selection()
            count = sum(by_id[identity]['page_count'] for identity in selected)
            note.set(f'{len(selected)}개 문서 · {count}쪽을 현재 문서 뒤에 붙입니다.')

        def apply():
            selected = [identity for identity in listing.get_children() if identity in listing.selection()]
            if not selected:
                note.set('합칠 문서를 먼저 선택하세요.')
                return
            try:
                versions = {identity: by_id[identity]['updated_at'] for identity in selected}
                versions[target['id']] = target['updated_at']
                self.library.merge_documents(target['id'], selected, expected_versions=versions)
                self._finish_organize(target['id'], set(versions))
                self.status.set(f'{len(selected)}개 문서의 쪽을 합쳤습니다. 원본과 수정 텍스트는 유지됩니다.')
                dialog.destroy()
            except (ValueError, KeyError) as error:
                note.set(str(error))
            except Exception as error:
                log_failure('desk_library.apply', error)
                note.set('합치기를 완료하지 못했습니다. 원본 쪽은 유지합니다. 저장 상태를 확인하세요.')

        listing.bind('<<TreeviewSelect>>', summarize)
        actions = ttk.Frame(dialog, padding=10)
        actions.pack(fill='x')
        ttk.Button(actions, text='선택 문서의 쪽 합치기', command=apply).pack(side='left')
        ttk.Button(actions, text='취소', command=dialog.destroy).pack(side='right')
        dialog.grab_set()

    def show_split_dialog(self):
        if not self._can_organize():
            return
        if len(self.page_records) < 2:
            self.status.set('쪽을 분리하려면 문서에 두 쪽 이상이 필요합니다.')
            return
        source = dict(self.document)
        dialog = tk.Toplevel(self)
        dialog.title('선택한 쪽을 새 문서로 분리')
        dialog.geometry('580x430')
        dialog.transient(self)
        ttk.Label(dialog, text='새 문서로 옮길 쪽을 선택하세요. 현재 문서에는 한 쪽 이상 남겨 둡니다.', wraplength=540, padding=10).pack(fill='x')
        title = tk.StringVar(dialog, value=source['title'] + ' (분리)')
        title_row = ttk.Frame(dialog, padding=(10, 0, 10, 10))
        title_row.pack(fill='x')
        ttk.Label(title_row, text='새 문서 제목').pack(side='left', padx=(0, 8))
        ttk.Entry(title_row, textvariable=title).pack(side='left', fill='x', expand=True)
        listing = ttk.Treeview(dialog, columns=('page', 'name'), show='headings', selectmode='extended')
        listing.heading('page', text='쪽')
        listing.column('page', width=60)
        listing.heading('name', text='원본')
        listing.pack(fill='both', expand=True, padx=10)
        for index, page in enumerate(self.page_records):
            listing.insert('', 'end', iid=page['id'], values=(index + 1, page.get('source_name') or '캡처'))
        if self.page:
            listing.selection_set(self.page['id'])
        note = tk.StringVar(dialog, value='라벨·메모와 원본·수정본·가림 설정을 유지합니다. AI 결과는 기존 문서에 남습니다.')
        ttk.Label(dialog, textvariable=note, wraplength=540, padding=10).pack(fill='x')

        def apply():
            try:
                doc = self.library.split_pages(source['id'], listing.selection(), title.get(), expected_updated_at=source['updated_at'])
                self._finish_organize(doc['id'], {source['id']})
                self.status.set('선택한 쪽을 새 문서로 분리했습니다.')
                dialog.destroy()
            except (ValueError, KeyError) as error:
                note.set(str(error))
            except Exception as error:
                log_failure('desk_library.apply', error)
                note.set('분리를 완료하지 못했습니다. 원본 쪽은 유지합니다. 저장 상태를 확인하세요.')

        actions = ttk.Frame(dialog, padding=10)
        actions.pack(fill='x')
        ttk.Button(actions, text='선택한 쪽 분리', command=apply).pack(side='left')
        ttk.Button(actions, text='취소', command=dialog.destroy).pack(side='right')
        dialog.grab_set()
