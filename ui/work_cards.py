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
        self.status_var = tk.StringVar(value='공문을 분석하면 업무 카드가 나타납니다. 검수 전에도 편집할 수 있습니다.')
        ttk.Label(self, textvariable=self.status_var, wraplength=900).pack(fill='x', padx=8, pady=6)
        panes = ttk.Panedwindow(self, orient='horizontal')
        panes.pack(fill='both', expand=True)
        left = ttk.Frame(panes)
        right = ttk.Frame(panes)
        panes.add(left, weight=1)
        panes.add(right, weight=3)
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
            var.trace_add('write', self._edited)
            confirmed = tk.BooleanVar(value=False)
            self.confirm_vars[name] = confirmed
            ttk.Checkbutton(form, text='확인', variable=confirmed).grid(row=row * 2, column=2, sticky='e')
            ttk.Button(form, text='근거', command=lambda field=name: self.show_evidence(field)).grid(row=row * 2, column=3, padx=5)
            status = tk.StringVar()
            self.field_status[name] = status
            ttk.Label(form, textvariable=status, foreground='#526c6d', wraplength=420).grid(row=row * 2 + 1, column=1, columnspan=3, sticky='w', padx=4)
        actions = ttk.Frame(self)
        actions.pack(fill='x', padx=8, pady=6)
        for index, (label, callback) in enumerate((
            ('수정 저장', self.save_selected), ('선택 항목 확인 완료', self.confirm_selected_fields),
            ('내 할 일에 담기', self.add_selected_todo), ('비교 후보를 별도 업무로 채택', self.adopt_selected),
        )):
            ttk.Button(actions, text=label, command=callback).grid(row=index // 2, column=index % 2, sticky='ew', padx=2, pady=2)
        actions.columnconfigure((0, 1), weight=1)
        generation = ttk.Frame(self)
        generation.pack(fill='x', padx=8, pady=(0, 6))
        self.kind_var = tk.StringVar(value=OUTPUT_KINDS[0])
        ttk.Combobox(generation, textvariable=self.kind_var, values=OUTPUT_KINDS, state='readonly', width=22).pack(side='left', padx=2)
        ttk.Button(generation, text='현재 정보로 새 초안 만들기', command=self.generate_current).pack(side='left', padx=4)
        ttk.Label(self, text='담기·복사는 업무 완료가 아닙니다.').pack(anchor='w', padx=10, pady=(0, 4))

    @property
    def selected_card(self):
        return self._card

    @property
    def has_unsaved_changes(self):
        return self._dirty or bool(self._drafts)

    def _edited(self, *args):
        if not self._loading and self._card:
            self._dirty = any(self.variables[name].get() != self._card[name] for name in CARD_FIELDS)
            if self._dirty:
                self.status_var.set('저장하지 않은 수정이 있습니다. 저장 실패 시에도 이 편집값은 유지됩니다.')
            else:
                self._drafts.pop(self._card['id'], None)

    def _stash(self):
        if self._card and self._dirty:
            self._drafts[self._card['id']] = (self._card, {name: var.get() for name, var in self.variables.items()})

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
                    card['condition'] or card['obligation'] or '미지정', card['deadline'] or '미지정', self._state(card)))
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
                card, values = draft
            else:
                values = card or {}
            self._card = card
            self._dirty = bool(draft)
            for name in CARD_FIELDS:
                self.variables[name].set(values.get(name, ''))
                field = card['fields'][name] if card else None
                self.confirm_vars[name].set(bool(field and field['confirmed']))
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
                    status += '\n확인 필요: ' + ' · '.join(str(item) for item in issues)
                self.status_var.set(status)
        finally:
            self._loading = was_loading

    def save_selected(self):
        if not self._card:
            return False
        if not self._dirty:
            return True
        changes = {name: var.get() for name, var in self.variables.items() if var.get() != self._card[name]}
        try:
            card = self.store.update_card(self._card['id'], changes, self._card['version'])
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
        names = [name for name, variable in self.confirm_vars.items() if variable.get()]
        if not self._card or not names:
            self.status_var.set('확인 완료로 표시할 필드의 확인 상자를 선택하세요.')
            return False
        if not self.save_selected():
            return False
        try:
            card = self.store.confirm_fields(self._card['id'], names, expected_version=self._card['version'])
        except Exception:
            self.status_var.set('확인 상태를 저장하지 못했습니다. 입력값은 유지했습니다.')
            return False
        self._cards[card['id']] = card
        self._load(card['id'])
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
        status = '참조한 원문과 발췌문 일치' if evidence.get('quote_verified', evidence['verified']) else '발췌문 확인 필요'
        if not evidence['verified'] and field['value']:
            status += ' · 현재 AI 제안값을 뒷받침하는지 확인 필요'
        if evidence['stale']:
            status += ' · 현재 원문이 변경되어 재확인 필요'
        lines = [f'문서 ID: {self._card["document_id"]}',
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
