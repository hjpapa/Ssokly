"""Read-only capture desk widgets; no storage, clipboard or network side effects."""
from collections import OrderedDict
import math
from pathlib import Path
import tkinter as tk
from tkinter import font as tkfont, ttk

from PIL import Image, ImageTk

from services.source_review import highlight_source, review_spans, source_table_blocks


class ZoomImageView(ttk.Frame):
    """One owned source image, with only its visible area rendered at zoom scale."""

    def __init__(self, parent, **kwargs):
        super().__init__(parent, **kwargs)
        self._image = None
        self._photo = None
        self._pending_render = None
        self.scale = 1.0
        self.fit_mode = True
        self._origin = (0, 0)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        toolbar = ttk.Frame(self)
        toolbar.grid(row=0, column=0, columnspan=2, sticky='ew')
        for label, command in (('맞춤', self.fit), ('−', self.zoom_out),
                               ('+', self.zoom_in), ('100%', self.original_size)):
            ttk.Button(toolbar, text=label, width=5, command=command).pack(side=tk.LEFT, padx=1)
        self.status = tk.StringVar(self, '이미지를 선택하세요')
        ttk.Label(self, textvariable=self.status).grid(row=3, column=0, columnspan=2, sticky='w')
        self.canvas = tk.Canvas(self, width=1, height=1, highlightthickness=0,
                                background='#edf1f3', cursor='fleur')
        self.canvas.grid(row=1, column=0, sticky='nsew')
        self.vertical = ttk.Scrollbar(self, orient=tk.VERTICAL, command=self._yview)
        self.horizontal = ttk.Scrollbar(self, orient=tk.HORIZONTAL, command=self._xview)
        self.vertical.grid(row=1, column=1, sticky='ns')
        self.horizontal.grid(row=2, column=0, sticky='ew')
        self.canvas.configure(xscrollcommand=self.horizontal.set, yscrollcommand=self.vertical.set)
        self._image_item = self.canvas.create_image(0, 0, anchor=tk.NW)
        self.canvas.bind('<Configure>', self._on_resize)
        self.canvas.bind('<ButtonPress-1>', self._pan_start)
        self.canvas.bind('<B1-Motion>', self._pan_move)
        self.canvas.bind('<MouseWheel>', self._on_wheel)
        self.canvas.bind('<Button-4>', lambda _event: self._scroll_units(-3))
        self.canvas.bind('<Button-5>', lambda _event: self._scroll_units(3))
        self.bind('<Destroy>', self._on_destroy, add='+')

    @property
    def image_size(self):
        return self._image.size if self._image is not None else None

    def set_image(self, image):
        """Copy a caller-owned PIL image; replacing/clearing releases the old copy."""
        if image is not None and not isinstance(image, Image.Image):
            raise TypeError('image must be a PIL image or None')
        owned = image.copy() if image is not None else None
        if self._image is not None:
            self._image.close()
        self._image = owned
        self._photo = None
        self.canvas.itemconfigure(self._image_item, image='')
        self.canvas.xview_moveto(0)
        self.canvas.yview_moveto(0)
        self.fit_mode = True
        self.scale = 1.0
        if owned is None:
            self.canvas.configure(scrollregion=(0, 0, 0, 0))
            self.status.set('이미지를 선택하세요')
        else:
            self.fit()

    def load_path(self, path):
        """Load a local image only. A bad/missing path never leaves a stale preview."""
        if path is None:
            self.set_image(None)
            return True
        try:
            with Image.open(path) as image:
                self.set_image(image)
            return True
        except (OSError, ValueError, SyntaxError, EOFError, Image.DecompressionBombError):
            self.set_image(None)
            self.status.set('이미지를 열 수 없습니다')
            return False

    def _viewport(self):
        return max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())

    def fit(self):
        self.fit_mode = True
        if self._image is None:
            return
        width, height = self._viewport()
        self.scale = min(width / self._image.width, height / self._image.height, 1.0)
        self.canvas.xview_moveto(0)
        self.canvas.yview_moveto(0)
        self._render()

    def zoom_in(self):
        self._set_scale(self.scale * 1.25)

    def zoom_out(self):
        self._set_scale(self.scale / 1.25)

    def original_size(self):
        self._set_scale(1.0)

    def _set_scale(self, scale):
        if self._image is None:
            return
        width, height = self._viewport()
        center_x = (self.canvas.canvasx(width / 2) - self._origin[0]) / self.scale
        center_y = (self.canvas.canvasy(height / 2) - self._origin[1]) / self.scale
        self.scale = min(16.0, max(0.01, scale))
        self.fit_mode = False
        self._set_scrollregion()
        full_width = max(width, self._image.width * self.scale)
        full_height = max(height, self._image.height * self.scale)
        self.canvas.xview_moveto((center_x * self.scale + self._origin[0] - width / 2) / full_width)
        self.canvas.yview_moveto((center_y * self.scale + self._origin[1] - height / 2) / full_height)
        self._render()

    def _set_scrollregion(self):
        width, height = self._viewport()
        image_width, image_height = self._image.width * self.scale, self._image.height * self.scale
        self._origin = (max(0, (width - image_width) / 2), max(0, (height - image_height) / 2))
        self.canvas.configure(scrollregion=(0, 0, max(width, image_width), max(height, image_height)))

    def _render(self):
        if self._image is None:
            return
        self._set_scrollregion()
        width, height = self._viewport()
        ox, oy = self._origin
        x, y = self.canvas.canvasx(0), self.canvas.canvasy(0)
        left = max(0, min(self._image.width, math.floor((x - ox) / self.scale)))
        top = max(0, min(self._image.height, math.floor((y - oy) / self.scale)))
        right = max(left, min(self._image.width, math.ceil((x + width - ox) / self.scale)))
        bottom = max(top, min(self._image.height, math.ceil((y + height - oy) / self.scale)))
        if right > left and bottom > top:
            crop = self._image.crop((left, top, right, bottom))
            display = crop.resize((max(1, round((right - left) * self.scale)),
                                   max(1, round((bottom - top) * self.scale))), Image.Resampling.LANCZOS)
            try:
                self._photo = ImageTk.PhotoImage(display, master=self.canvas)
            finally:
                display.close()
                crop.close()
            self.canvas.itemconfigure(self._image_item, image=self._photo)
            self.canvas.coords(self._image_item, ox + left * self.scale, oy + top * self.scale)
        else:
            self.canvas.itemconfigure(self._image_item, image='')
            self._photo = None
        mode = '맞춤 · ' if self.fit_mode else ''
        self.status.set(f'{mode}{self.scale:.0%} · {self._image.width} × {self._image.height}')

    def _on_resize(self, _event):
        if self._pending_render is None:
            self._pending_render = self.after_idle(self._resize_render)

    def _resize_render(self):
        self._pending_render = None
        if self.fit_mode:
            self.fit()
        else:
            self._render()

    def _xview(self, *args):
        self.canvas.xview(*args)
        self._render()

    def _yview(self, *args):
        self.canvas.yview(*args)
        self._render()

    def _pan_start(self, event):
        self.canvas.scan_mark(event.x, event.y)

    def _pan_move(self, event):
        self.canvas.scan_dragto(event.x, event.y, gain=1)
        self._render()

    def _scroll_units(self, units):
        self._yview('scroll', units, 'units')
        return 'break'

    def _on_wheel(self, event):
        if event.state & 0x0004:
            self.zoom_in() if event.delta > 0 else self.zoom_out()
            return 'break'
        return self._scroll_units(-3 if event.delta > 0 else 3)

    def _on_destroy(self, event):
        if event.widget is not self:
            return
        if self._pending_render is not None:
            self.after_cancel(self._pending_render)
            self._pending_render = None
        if self._image is not None:
            self._image.close()
            self._image = None
        self._photo = None


class ThumbnailCache:
    """Bounded Tk thumbnails, invalidated by a file's modification metadata."""

    def __init__(self, master, max_items=128):
        self.master = master
        self.max_items = min(128, max(1, int(max_items)))
        self._items = OrderedDict()

    def __len__(self):
        return len(self._items)

    def get(self, path, size=(96, 72)):
        if path is None:
            return None
        size = tuple(map(int, size))
        if len(size) != 2 or min(size) < 1:
            raise ValueError('thumbnail size must contain two positive dimensions')
        path = Path(path).resolve()
        try:
            stat = path.stat()
        except OSError:
            self._discard_path(path)
            return None
        key = (path, stat.st_mtime_ns, stat.st_size, size)
        if key in self._items:
            self._items.move_to_end(key)
            return self._items[key]
        self._discard_path(path, keep_version=key[1:3])
        try:
            with Image.open(path) as image:
                image.thumbnail(size, Image.Resampling.LANCZOS)
                photo = ImageTk.PhotoImage(image, master=self.master)
        except (OSError, ValueError, SyntaxError, EOFError, Image.DecompressionBombError):
            return None
        self._items[key] = photo
        while len(self._items) > self.max_items:
            self._items.popitem(last=False)
        return photo

    def _discard_path(self, path, keep_version=None):
        for key in list(self._items):
            if key[0] == path and key[1:3] != keep_version:
                del self._items[key]

    def clear(self):
        self._items.clear()


class InlineTableView(ttk.Frame):
    """Inline immutable TSV review; on_copy receives exact TSV, not a message."""

    def __init__(self, parent, on_copy=None, **kwargs):
        super().__init__(parent, **kwargs)
        self.on_copy = on_copy
        self.source_text = ''
        self.blocks = []
        self.selected_table = -1
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=3)
        self.rowconfigure(3, weight=1)
        self.selector = ttk.Combobox(self, state='disabled', width=16)
        self.selector.grid(row=0, column=0, columnspan=2, sticky='ew', pady=(0, 4))
        self.selector.bind('<<ComboboxSelected>>', self._select_event)
        table_frame = ttk.Frame(self, width=1, height=100)
        table_frame.grid(row=1, column=0, sticky='nsew')
        table_frame.grid_propagate(False)
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(table_frame, show='tree headings', height=5, selectmode='browse')
        self.tree.heading('#0', text='원문 행')
        self.tree.column('#0', width=90, minwidth=60, stretch=False)
        self.tree.grid(row=0, column=0, sticky='nsew')
        self.vertical = ttk.Scrollbar(self, orient=tk.VERTICAL, command=self.tree.yview)
        self.horizontal = ttk.Scrollbar(self, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.vertical.grid(row=1, column=1, sticky='ns')
        self.horizontal.grid(row=2, column=0, sticky='ew')
        self.tree.configure(yscrollcommand=self.vertical.set, xscrollcommand=self.horizontal.set)
        self.tree.bind('<<TreeviewSelect>>', lambda _event: self.show_row())
        detail_frame = ttk.Frame(self)
        detail_frame.columnconfigure(0, weight=1)
        detail_frame.rowconfigure(0, weight=1)
        detail_frame.grid(row=3, column=0, columnspan=2, sticky='nsew', pady=4)
        self.detail = tk.Text(detail_frame, width=1, height=4, wrap=tk.WORD, state=tk.DISABLED)
        self.detail.grid(row=0, column=0, sticky='nsew')
        detail_scroll = ttk.Scrollbar(detail_frame, orient=tk.VERTICAL, command=self.detail.yview)
        detail_scroll.grid(row=0, column=1, sticky='ns')
        self.detail.configure(yscrollcommand=detail_scroll.set)
        actions = ttk.Frame(self)
        actions.grid(row=4, column=0, columnspan=2, sticky='ew')
        self.copy_row_button = ttk.Button(actions, text='행 복사', command=self.copy_row)
        self.copy_table_button = ttk.Button(actions, text='표 복사', command=self.copy_table)
        self.copy_row_button.pack(side=tk.LEFT, padx=(0, 4))
        self.copy_table_button.pack(side=tk.LEFT)
        self.set_text('')

    def set_text(self, text):
        self.source_text = text or ''
        self.blocks = source_table_blocks(self.source_text)
        self.selector.configure(values=[f'표 {i + 1} · 원문 {block.start_line}행 · {len(block.rows)}행'
                                        for i, block in enumerate(self.blocks)])
        if self.blocks:
            self.selector.configure(state='readonly')
            self.select_table(0)
        else:
            self.selected_table = -1
            self.selector.set('인식된 표가 없습니다')
            self.selector.configure(state='disabled')
            self.tree.delete(*self.tree.get_children())
            self.tree.configure(columns=())
            self._set_detail('표가 없습니다. 탭으로 구분된 원문 표를 이곳에서 확인할 수 있습니다.')
        enabled = bool(self.blocks) and self.on_copy is not None
        for button in (self.copy_row_button, self.copy_table_button):
            button.configure(state=tk.NORMAL if enabled else tk.DISABLED)

    def _select_event(self, _event):
        self.select_table(self.selector.current())

    def select_table(self, index):
        if not 0 <= index < len(self.blocks):
            return
        self.selected_table = index
        self.selector.current(index)
        block = self.blocks[index]
        self.tree.delete(*self.tree.get_children())
        columns = [str(i) for i in range(max(map(len, block.rows)))]
        self.tree.configure(columns=columns)
        table_font = tkfont.nametofont('TkDefaultFont', root=self)
        for i in columns:
            column = int(i)
            self.tree.heading(i, text=f'열 {column + 1}')
            measured = max((table_font.measure(row[column]) + 24
                            for row in block.rows if column < len(row)), default=100)
            self.tree.column(i, width=max(90, min(380, measured)), minwidth=60, stretch=False)
        for i, row in enumerate(block.rows):
            notice = ' · 확인' if review_spans('\t'.join(row)) else ''
            self.tree.insert('', tk.END, iid=str(i), text=f'{block.start_line + i}행{notice}',
                             values=row + [''] * (len(columns) - len(row)))
        self.tree.xview_moveto(0)
        self.tree.yview_moveto(0)
        self.tree.selection_set('0')
        self.tree.focus('0')
        self.show_row()

    def _set_detail(self, content):
        self.detail.configure(state=tk.NORMAL)
        self.detail.delete('1.0', tk.END)
        self.detail.insert('1.0', content)
        highlight_source(self.detail)
        self.detail.configure(state=tk.DISABLED)

    def show_row(self):
        selection = self.tree.selection()
        if self.selected_table < 0 or not selection:
            return
        block = self.blocks[self.selected_table]
        row_index = int(selection[0])
        values = block.rows[row_index]
        self._set_detail(f'원문 {block.start_line + row_index}행 · ↳ 병합 이어짐\n' + '\n'.join(
            f'열 {i + 1}: {value if value else "(빈 셀)"}' for i, value in enumerate(values)))

    def copy_row(self):
        selection = self.tree.selection()
        if self.selected_table < 0 or not selection or self.on_copy is None:
            return
        self.on_copy('\t'.join(self.blocks[self.selected_table].rows[int(selection[0])]))

    def copy_table(self):
        if self.selected_table < 0 or self.on_copy is None:
            return
        self.on_copy('\n'.join('\t'.join(row) for row in self.blocks[self.selected_table].rows))
