"""Reusable work-card editor. Persistence and outgoing AI work stay with callbacks."""
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

from services.work_card_store import CARD_FIELDS, FIELD_LABELS, CardConflictError


OUTPUT_KINDS = ('업무 일정·체크리스트', '교직원 메신저', '학부모 메신저', '가정통신문 초안')


class WorkCardsPanel(ttk.Frame):
    def __init__(self, master, store, document_id, on_changed=None, on_add_todo=None, on_generate=None, **kwargs):
        super().__init__(master, **kwargs)
        self.store = store
        self.document_id = document_id
        self.on_changed = on_changed
        self.on_add_todo = on_add_todo
        self.on_generate = on_generate
        self._card = None
        self._loading = False
        self._dirty = False
        self._drafts = {}
        self._selections = {}
        self._displayed_document = None
        self._cards = {}
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self.status_var = tk.StringVar(value='공문을 분석하면 업무 카드가 나타납니다. 검수 전에도 편집할 수 있습니다.')
        self.status_label = ttk.Label(self, textvariable=self.status_var, wraplength=900)
        self.status_label.grid(row=0, column=0, sticky='ew', padx=8, pady=6)
        self.content = ttk.Frame(self)
        self.content.grid(row=1, column=0, sticky='nsew')
        self.list_frame = left = ttk.Frame(self.content)
        self.editor_frame = right = ttk.Frame(self.content)
        self._narrow = None
        self.bind('<Configure>', self._resize_layout)
        self.tree = ttk.Treeview(left, columns=('condition', 'deadline', 'state'), show='tree headings', selectmode='browse', height=9)
        self.tree.heading('#0', text='해야 할 일')
        self.tree.column('#0', width=180, minwidth=110)
        for name, label, width in (('condition', '조건', 80), ('deadline', '기한', 110), ('state', '상태', 110)):
            self.tree.heading(name, text=label)
            self.tree.column(name, width=width, minwidth=60)
        self.tree.pack(side='left', fill='both', expand=True)
        scrollbar = ttk.Scrollbar(left, orient='vertical', command=self.tree.yview)
        scrollbar.pack(side='right', fill='y')
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.bind('<<TreeviewSelect>>', self._selection_changed)
        self.canvas = tk.Canvas(right, highlightthickness=0)
        form_scroll = ttk.Scrollbar(right, orient='vertical', command=self.canvas.yview)
        form_scroll.pack(side='right', fill='y')
        self.canvas.pack(side='left', fill='both', expand=True)
        self.canvas.configure(yscrollcommand=form_scroll.set)
        form = ttk.Frame(self.canvas)
        form_window = self.canvas.create_window((0, 0), window=form, anchor='nw')
        form.bind('<Configure>', lambda event: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>', lambda event: self.canvas.itemconfigure(form_window, width=event.width))
        form.columnconfigure(1, weight=1)
        self.variables = {}
        self.confirm_vars = {}
        self.field_status = {}
        for row, name in enumerate(CARD_FIELDS):
            ttk.Label(form, text=FIELD_LABELS[name]).grid(row=row * 2, column=0, sticky='nw', padx=6, pady=(5, 0))
            var = tk.StringVar()
            self.variables[name] = var
            entry = ttk.Entry(form, textvariable=var)
            entry.grid(row=row * 2, column=1, sticky='ew', padx=4, pady=(5, 0))
            var.trace_add('write', lambda *args, field=name: self._edited(field))
            confirmed = tk.BooleanVar(value=False)
            self.confirm_vars[name] = confirmed
            confirmed.trace_add('write', lambda *args: self._edited())
            ttk.Checkbutton(form, text='확인', variable=confirmed).grid(row=row * 2, column=2, sticky='e')
            ttk.Button(form, text='근거', command=lambda field=name: self.show_evidence(field)).grid(row=row * 2, column=3, padx=5)
            status = tk.StringVar()
            self.field_status[name] = status
            ttk.Label(form, textvariable=status, foreground='#526c6d', wraplength=420).grid(row=row * 2 + 1, column=1, columnspan=3, sticky='w', padx=4)
        actions = ttk.Frame(self)
        actions.grid(row=2, column=0, sticky='ew', padx=8, pady=6)
        for index, (label, callback) in enumerate((
            ('수정 저장', self.save_selected), ('확인 상태 저장', self.confirm_selected_fields),
            ('편집 취소·현재값 사용', self.discard_selected), ('내 할 일에 담기', self.add_selected_todo),
            ('비교 후보 채택', self.adopt_selected), ('충돌 비교·재적용', self.show_conflict_comparison),
        )):
            ttk.Button(actions, text=label, command=callback).grid(row=index // 3, column=index % 3, sticky='ew', padx=2, pady=2)
        actions.columnconfigure((0, 1, 2), weight=1)
        generation = ttk.Frame(self)
        generation.grid(row=3, column=0, sticky='ew', padx=8, pady=(0, 6))
        self.kind_var = tk.StringVar(value=OUTPUT_KINDS[0])
        ttk.Combobox(generation, textvariable=self.kind_var, values=OUTPUT_KINDS, state='readonly', width=22).pack(side='left', padx=2)
        ttk.Button(generation, text='현재 정보로 새 초안 만들기', command=self.generate_current).pack(side='left', padx=4)
        ttk.Label(self, text='담기·복사는 업무 완료가 아닙니다.').grid(row=4, column=0, sticky='w', padx=10, pady=(0, 4))
        self._resize_layout()

    def _resize_layout(self, event=None):
        width = self.winfo_width()
        if event is not None and event.widget is not self:
            return
        self.status_label.configure(wraplength=max(200, width - 24))
        narrow = width < 900
        self.tree.configure(height=(1 if self.winfo_height() < 440 else 4) if narrow else 9)
        if narrow == self._narrow:
            return
        self._narrow = narrow
        self.list_frame.grid_forget()
        self.editor_frame.grid_forget()
        self.content.rowconfigure((0, 1), weight=0)
        self.content.columnconfigure((0, 1), weight=0)
        if narrow:
            self.list_frame.grid(row=0, column=0, sticky='nsew')
            self.editor_frame.grid(row=1, column=0, sticky='nsew')
            self.content.columnconfigure(0, weight=1)
            self.content.rowconfigure(1, weight=1)
        else:
            self.list_frame.grid(row=0, column=0, sticky='nsew')
            self.editor_frame.grid(row=0, column=1, sticky='nsew')
            self.content.columnconfigure(0, weight=1)
            self.content.columnconfigure(1, weight=3)
            self.content.rowconfigure(0, weight=1)

    @property
    def selected_card(self):
        return self._card

    @property
    def has_unsaved_changes(self):
        return self._dirty or bool(self._drafts)

    def _edited(self, field=None):
        if not self._loading and self._card:
            if field and self.variables[field].get() != self._card[field] and self.confirm_vars[field].get():
                self._loading = True
                self.confirm_vars[field].set(False)
                self._loading = False
            self._dirty = any(self.variables[name].get() != self._card[name] or
                              self.confirm_vars[name].get() != self._card['fields'][name]['confirmed'] for name in CARD_FIELDS)
            if self._dirty:
                self.status_var.set('저장하지 않은 수정이 있습니다. 저장 실패 시에도 이 편집값은 유지됩니다.')
            else:
                self._drafts.pop(self._card['id'], None)
            if self.tree.exists(self._card['id']):
                values = list(self.tree.item(self._card['id'], 'values'))
                if len(values) == 3:
                    values[-1] = '미저장 편집' if self._dirty else self._state(self._card)
                    self.tree.item(self._card['id'], values=values)

    def _stash(self):
        if self._card and self._dirty:
            self._drafts[self._card['id']] = (self._card, {name: var.get() for name, var in self.variables.items()},
                                             {name: var.get() for name, var in self.confirm_vars.items()})

    @staticmethod
    def _state(card):
        if card.get('source_stale'):
            return '이전 원문 · 재확인'
        if card.get('comparison_candidate'):
            return '비교 후보'
        if any(field['edited'] for field in card['fields'].values()):
            return '교사 수정'
        return '검수 전'

    def refresh(self):
        self._stash()
        document_id = self.document_id()
        self._displayed_document = document_id
        cards = self.store.list_cards(document_id) if document_id else []
        self._cards = {card['id']: card for card in cards}
        self._loading = True
        try:
            self.tree.delete(*self.tree.get_children())
            for card in cards:
                self.tree.insert('', 'end', iid=card['id'], text=card['action'] or '업무 내용 미지정', values=(
                    card['condition'] or card['obligation'] or '미지정', card['deadline'] or '미지정',
                    '미저장 편집' if card['id'] in self._drafts else self._state(card)))
            chosen = self._selections.get(document_id)
            if chosen not in self._cards:
                chosen = cards[0]['id'] if cards else None
            if chosen:
                self.tree.selection_set(chosen)
                self._selections[document_id] = chosen
            self._load(chosen)
        finally:
            self._loading = False
        if not cards:
            self.status_var.set('가져온 범위에서는 업무 카드를 확인하지 못했습니다. 원문·붙임 또는 분석 결과를 확인하세요.')

    def _selection_changed(self, event=None):
        if self._loading:
            return
        selection = self.tree.selection()
        if not selection:
            return
        key = selection[0]
        if self._card and key == self._card['id']:
            return
        self._stash()
        self._selections[self._displayed_document] = key
        self._load(key)

    def _load(self, key):
        was_loading = self._loading
        self._loading = True
        try:
            card = self._cards.get(key)
            draft = self._drafts.get(key)
            if draft:
                card, values, confirmations = draft
            else:
                values = card or {}
                confirmations = {name: field['confirmed'] for name, field in card['fields'].items()} if card else {}
            self._card = card
            self._dirty = bool(draft)
            for name in CARD_FIELDS:
                self.variables[name].set(values.get(name, ''))
                field = card['fields'][name] if card else None
                self.confirm_vars[name].set(bool(confirmations.get(name)))
                if not field:
                    status = ''
                else:
                    evidence = field['evidence']
                    states = []
                    if field['edited']:
                        states.append('교사 수정' + (' · 명시적 빈값' if not field['value'] else ''))
                    states.append('확인 완료' if field['confirmed'] else '검수 전')
                    if evidence['stale']:
                        states.append('원문 변경 · 근거 재확인')
                    elif evidence['ambiguous']:
                        states.append('원문 여러 위치 · 위치 확인')
                    elif evidence['verified']:
                        states.append('참조 원문과 인용 일치')
                    elif name not in ('preparation_date', 'notes') and field['value']:
                        states.append('근거 확인 필요')
                    if not field['value']:
                        states.append('미지정')
                    states.extend(field.get('issues', []))
                    status = ' · '.join(states)
                self.field_status[name].set(status)
            if card:
                status = f'업무 버전 {card["version"]} · 원문 버전 {card["source_version"]} · {self._state(card)}'
                if draft:
                    status += ' · 저장하지 않은 편집 복원'
                issues = card.get('ai_proposal', {}).get('issues') or []
                if issues:
                    status += f'\n확인 필요 {len(issues)}항목 · 각 필드의 상태와 근거에서 상세 내용을 확인하세요.'
                self.status_var.set(status)
        finally:
            self._loading = was_loading

    def save_selected(self):
        if not self._card:
            return False
        if not self._dirty:
            return True
        changes = {name: var.get() for name, var in self.variables.items() if var.get() != self._card[name]}
        confirmations = {name: var.get() for name, var in self.confirm_vars.items()
                         if var.get() != self._card['fields'][name]['confirmed'] or name in changes}
        try:
            card = self.store.update_card(self._card['id'], changes, self._card['version'],
                confirmation_changes=confirmations, expected_review_signature=self._card['review_signature'])
        except Exception as exc:
            self._stash()
            self.status_var.set('저장하지 못했습니다. 편집값을 유지했습니다. 현재 값을 확인한 뒤 다시 시도하세요.')
            detail = str(exc) if isinstance(exc, CardConflictError) else '저장소에 기록하지 못했습니다. 편집값은 화면에 남아 있습니다.'
            messagebox.showerror('업무 수정 저장 실패', detail, parent=self)
            return False
        self._drafts.pop(card['id'], None)
        self._cards[card['id']] = card
        self._load(card['id'])
        if self.tree.exists(card['id']):
            self.tree.item(card['id'], text=card['action'] or '업무 내용 미지정', values=(card['condition'] or card['obligation'] or '미지정', card['deadline'] or '미지정', self._state(card)))
        self.status_var.set(f'수정 저장 완료 · 업무 버전 {card["version"]}. 기존 초안은 자동 변경하지 않습니다.')
        if self.on_changed:
            self.on_changed(card)
        return True

    def confirm_selected_fields(self):
        return self.save_selected()

    def discard_selected(self, *, ask=True):
        if not self._card:
            return False
        if ask and self._dirty and not messagebox.askyesno('편집 취소', '저장하지 않은 이 카드의 편집과 확인 체크 변경을 취소하고 현재 저장값을 사용할까요?', parent=self):
            return False
        try:
            current = self.store.get_card(self._card['id'])
        except Exception:
            self.status_var.set('현재 저장값을 읽지 못했습니다. 편집값은 유지했습니다.')
            return False
        if current is None:
            return False
        self._drafts.pop(current['id'], None)
        self._cards[current['id']] = current
        self._load(current['id'])
        self.refresh()
        self.status_var.set('편집 취소 완료 · 현재 저장값을 불러왔습니다. 저장된 업무는 변경하지 않았습니다.')
        return True

    def show_conflict_comparison(self):
        if not self._card:
            return
        try:
            latest = self.store.get_card(self._card['id'])
        except Exception:
            self.status_var.set('현재 저장값을 읽지 못했습니다. 편집값은 유지했습니다.')
            return
        if latest is None:
            return
        popup = tk.Toplevel(self)
        popup.title('업무 편집 비교 · 저장값은 선택 후 변경')
        popup.geometry('880x640')
        description = ttk.Label(popup, text=f'편집 시작 v{self._card["version"]} → 현재 v{latest["version"]}. 각 필드에서 보존할 값을 선택하세요. 수정하지 않은 필드는 최신 저장값을 유지합니다.', wraplength=840)
        description.pack(fill='x', padx=12, pady=8)
        canvas = tk.Canvas(popup, highlightthickness=0)
        scrollbar = ttk.Scrollbar(popup, orient='vertical', command=canvas.yview)
        scrollbar.pack(side='right', fill='y')
        canvas.pack(fill='both', expand=True, padx=8)
        canvas.configure(yscrollcommand=scrollbar.set)
        form = ttk.Frame(canvas)
        window_id = canvas.create_window((0, 0), window=form, anchor='nw')
        canvas.bind('<Configure>', lambda event: canvas.itemconfigure(window_id, width=event.width))
        form.bind('<Configure>', lambda event: canvas.configure(scrollregion=canvas.bbox('all')))
        choices = {}
        for name in CARD_FIELDS:
            local = self.variables[name].get()
            local_confirmed = self.confirm_vars[name].get()
            edited = local != self._card[name] or local_confirmed != self._card['fields'][name]['confirmed']
            row = ttk.LabelFrame(form, text=FIELD_LABELS[name], padding=6)
            row.pack(fill='x', padx=4, pady=3)
            ttk.Label(row, text=f'편집 시작: {self._card[name] or "(빈값)"}\n현재 저장: {latest[name] or "(빈값)"} · 확인 {latest["fields"][name]["confirmed"]}\n내 편집: {local or "(명시적 빈값)"} · 확인 {local_confirmed}', wraplength=780).pack(anchor='w')
            choice = tk.StringVar(value='mine' if edited else 'current')
            choices[name] = choice
            ttk.Radiobutton(row, text='내 편집 사용', variable=choice, value='mine').pack(side='left')
            ttk.Radiobutton(row, text='현재 저장값 사용', variable=choice, value='current').pack(side='left')
        controls = ttk.Frame(popup)
        controls.pack(fill='x', padx=10, pady=8)
        def apply():
            if self.resolve_conflict({name: var.get() for name, var in choices.items()}, latest):
                popup.destroy()
        ttk.Button(controls, text='선택대로 저장·내 변경 재적용', command=apply).pack(side='left')
        ttk.Button(controls, text='내 편집 취소·현재 저장값 사용', command=lambda: popup.destroy() if self.discard_selected() else None).pack(side='left', padx=6)
        ttk.Button(controls, text='계속 편집', command=popup.destroy).pack(side='right')
        return popup

    def resolve_conflict(self, choices, latest):
        """Explicit field choices, against the version shown in the comparison."""
        if not self._card or latest['id'] != self._card['id']:
            return False
        changes, confirmations = {}, {}
        for name in CARD_FIELDS:
            if choices.get(name) != 'mine':
                continue
            value, confirmed = self.variables[name].get(), self.confirm_vars[name].get()
            if value != self._card[name] or value != latest[name]:
                changes[name] = value  # Includes deliberate deletion, even if latest is empty.
            if confirmed != self._card['fields'][name]['confirmed'] or confirmed != latest['fields'][name]['confirmed'] or name in changes:
                confirmations[name] = confirmed
        try:
            card = self.store.update_card(latest['id'], changes, latest['version'],
                confirmation_changes=confirmations, expected_review_signature=latest['review_signature'])
        except Exception:
            self._stash()
            self.status_var.set('비교 이후 다시 변경되었거나 저장하지 못했습니다. 편집은 유지했습니다. 비교창을 다시 열어 현재 값을 확인하세요.')
            return False
        self._drafts.pop(card['id'], None)
        self._cards[card['id']] = card
        self._load(card['id'])
        self.refresh()
        if self.on_changed:
            self.on_changed(card)
        return True

    def add_selected_todo(self):
        if not self._card or not self.save_selected():
            return
        if self.on_add_todo:
            self.on_add_todo(self._card)

    def adopt_selected(self):
        if not self._card or not self._card['comparison_candidate'] or not self.save_selected():
            return False
        if not messagebox.askyesno('비교 후보 채택', '이 카드를 기존 업무와 별개의 업무로 채택할까요? 기존 업무의 수정값·완료 상태는 옮기지 않습니다.', parent=self):
            return False
        try:
            card = self.store.adopt_card(self._card['id'], self._card['version'])
        except Exception:
            self.status_var.set('비교 후보를 채택하지 못했습니다. 기존 데이터는 유지했습니다.')
            return False
        self._cards[card['id']] = card
        self._load(card['id'])
        self.refresh()
        if self.on_changed:
            self.on_changed(card)
        return True

    def generate_current(self):
        if not self._card or not self.save_selected():
            return
        if self.on_generate:
            self.on_generate(self.kind_var.get())

    def show_evidence(self, name):
        if not self._card or name not in CARD_FIELDS:
            return
        field = self._card['fields'][name]
        evidence = field['evidence']
        document = self.store.get_document(self._card['document_id'], evidence['source_version'])
        popup = tk.Toplevel(self)
        popup.title(FIELD_LABELS[name] + ' · 원문 근거')
        popup.geometry('720x480')
        text = scrolledtext.ScrolledText(popup, wrap='word', padx=12, pady=12)
        text.pack(fill='both', expand=True)
        location = '위치 미확인'
        if evidence['ambiguous']:
            location = f'같은 인용이 {len(evidence["locations"])}곳에 있습니다. 유일한 위치로 표시하지 않습니다.'
        elif evidence['locations']:
            location = f'추출 텍스트 {evidence["locations"][0]["line"]}행 (원본 페이지 번호 아님)'
            if evidence['locations'][0].get('cell'):
                location += f' · 표 셀 {evidence["locations"][0]["cell"]}'
        status = '참조한 원문과 발췌문 일치' if evidence.get('quote_verified', evidence['verified']) else '발췌문 확인 필요'
        if not evidence['verified'] and field['value']:
            status += ' · 현재 AI 제안값을 뒷받침하는지 확인 필요'
        if evidence['stale']:
            status += ' · 현재 원문이 변경되어 재확인 필요'
        lines = [f'문서 ID: {self._card["document_id"]}',
                 f'출처: {(document or {}).get("source_ref") or "현재 원문"}',
                 f'원문 종류: {(document or {}).get("source_kind", "알 수 없음")} · 버전 {evidence["source_version"]}',
                 f'가져온 범위: {(document or {}).get("scope", "알 수 없음")}', location, status, '',
                 '현재 업무 값: ' + (self.variables[name].get() or '(명시적 빈값)' if field['edited'] else self.variables[name].get() or '(미지정)'),
                 'AI 제안값: ' + (field['ai_value'] or '(미지정)'), '',
                 '실제 발췌문:', evidence['quote'] or '(확인 가능한 발췌문 없음)', '',
                 '인식본/검수본과 일치한다는 뜻이며 원본 이미지의 정확도를 보증하지 않습니다. 업무 수정은 원문을 바꾸지 않습니다.']
        text.insert('1.0', '\n'.join(lines))
        text.configure(state='disabled')
        ttk.Button(popup, text='닫기', command=popup.destroy).pack(pady=6)
        return popup
