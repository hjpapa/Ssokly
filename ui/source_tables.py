"""Read-only table snapshots with exact copying and per-span review hints."""
import tkinter as tk
from tkinter import font as tkfont, scrolledtext, ttk

from services.source_review import highlight_source, review_spans


class SourceTablesWindow(tk.Toplevel):
    def __init__(self, parent, blocks, on_copy):
        super().__init__(parent)
        self.blocks = blocks
        self.on_copy = on_copy
        self.trees = []
        self.details = []
        self.title('인식된 표 · 원문은 변경되지 않습니다')
        self.geometry('880x500')
        self.transient(parent)
        ttk.Label(self, text='열 때의 원문을 표시합니다. 행 번호는 추출 텍스트 기준이며 원본 페이지 번호가 아닙니다.\n'
                  '↳는 병합 셀의 이어진 값입니다. 행을 선택하면 전체 셀과 확인할 부분의 빨간 밑줄을 볼 수 있습니다.',
                  wraplength=760).pack(anchor=tk.W, padx=10, pady=8)
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=10)
        table_font = tkfont.nametofont('TkDefaultFont')
        for index, block in enumerate(blocks):
            rows = block.rows
            frame = ttk.Frame(self.notebook)
            self.notebook.add(frame, text=f'표 {index + 1} · {len(rows)}행')
            detail = scrolledtext.ScrolledText(frame, height=5, wrap=tk.WORD)
            detail.pack(side=tk.BOTTOM, fill=tk.X)
            detail.configure(state=tk.DISABLED)
            columns = [str(i) for i in range(max(map(len, rows)))]
            tree = ttk.Treeview(frame, columns=columns, show='tree headings', selectmode='browse')
            tree.heading('#0', text='원문 행 · 확인 표시')
            tree.column('#0', width=135, stretch=False)
            for i in columns:
                tree.heading(i, text=f'열 {int(i) + 1}')
                width = max((table_font.measure(row[int(i)]) + 24 for row in rows if int(i) < len(row)), default=150)
                tree.column(i, width=max(100, min(620, width)), stretch=False)
            vertical = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=tree.yview)
            horizontal = ttk.Scrollbar(frame, orient=tk.HORIZONTAL, command=tree.xview)
            tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
            vertical.pack(side=tk.RIGHT, fill=tk.Y)
            horizontal.pack(side=tk.BOTTOM, fill=tk.X)
            tree.pack(fill=tk.BOTH, expand=True)
            for row_index, row in enumerate(rows):
                notice = ' · 확인' if review_spans('\t'.join(row)) else ''
                tree.insert('', tk.END, iid=str(row_index), text=f'{block.start_line + row_index}행{notice}',
                            values=row + [''] * (len(columns) - len(row)))
            self.trees.append(tree)
            self.details.append(detail)
            tree.bind('<<TreeviewSelect>>', lambda _event, i=index: self.show_row(i))
            tree.selection_set('0')
            self.show_row(index)
        actions = ttk.Frame(self)
        actions.pack(pady=8)
        ttk.Button(actions, text='선택한 행 복사', command=self.copy_row).pack(side=tk.LEFT, padx=4)
        ttk.Button(actions, text='선택한 표 복사', command=self.copy_table).pack(side=tk.LEFT, padx=4)

    def show_row(self, index):
        selection = self.trees[index].selection()
        if not selection:
            return
        row_index = int(selection[0])
        block = self.blocks[index]
        values = block.rows[row_index]
        text = self.details[index]
        text.configure(state=tk.NORMAL)
        text.delete('1.0', tk.END)
        text.insert('1.0', f'원문 {block.start_line + row_index}행\n' + '\n'.join(
            f'열 {i + 1}: {value if value else "(빈 셀)"}' for i, value in enumerate(values)))
        highlight_source(text)
        text.configure(state=tk.DISABLED)

    def _copy(self, text, label):
        self.clipboard_clear()
        self.clipboard_append(text)
        self.on_copy(f'{label}를 복사했습니다. 빈 셀을 유지하여 스프레드시트에 붙여넣을 수 있습니다.')

    def copy_table(self):
        block = self.blocks[self.notebook.index(self.notebook.select())]
        self._copy('\n'.join('\t'.join(row) for row in block.rows), '표')

    def copy_row(self):
        index = self.notebook.index(self.notebook.select())
        selection = self.trees[index].selection()
        if selection:
            self._copy('\t'.join(self.blocks[index].rows[int(selection[0])]), '행')
