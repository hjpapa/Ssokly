"""Shared light theme and scalable capture/document mark for the desktop."""
from tkinter import font as tkfont, ttk
from PIL import Image, ImageDraw, ImageTk

BACKGROUND = '#f3f6f8'
INK = '#193640'
ACCENT = '#087f80'


def brand_mark(size=64):
    """Original geometric artwork: a page inside four capture corners."""
    image = Image.new('RGBA', (256, 256), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((8, 8, 248, 248), radius=56, fill=ACCENT)
    draw.rounded_rectangle((79, 54, 177, 202), radius=12, fill='#ffffff')
    for y, end in ((94, 151), (119, 151), (144, 137)):
        draw.line((103, y, end, y), fill=ACCENT, width=9)
    for points in (((66, 44), (44, 44), (44, 76)), ((190, 44), (212, 44), (212, 76)),
                   ((44, 180), (44, 212), (66, 212)), ((212, 180), (212, 212), (190, 212))):
        draw.line(points, fill='#b9f4d4', width=10, joint='curve')
    return image.resize((size, size), Image.Resampling.LANCZOS)


def apply_theme(root):
    style = ttk.Style(root)
    if 'clam' in style.theme_names():
        style.theme_use('clam')
    root.configure(bg=BACKGROUND)
    root.option_add('*Font', ('Malgun Gothic', 10))
    style.configure('.', background=BACKGROUND, foreground=INK, font=('Malgun Gothic', 10))
    style.configure('TFrame', background=BACKGROUND)
    style.configure('TLabel', background=BACKGROUND, foreground=INK)
    style.configure('TButton', background='#ffffff', foreground=INK, bordercolor='#d3dee3',
                    lightcolor='#ffffff', darkcolor='#ffffff', padding=(5, 2), focusthickness=1)
    style.map('TButton', background=[('active', '#e5f2f1')], foreground=[('disabled', '#84949b')],
              bordercolor=[('focus', ACCENT)])
    style.configure('Desk.TButton', padding=(8, 3))
    style.configure('Primary.TButton', background=ACCENT, foreground='#ffffff', padding=(8, 3), bordercolor=ACCENT)
    style.map('Primary.TButton', background=[('active', '#08676b'), ('disabled', '#b4ccce')],
              foreground=[('disabled', '#f3f6f8'), ('!disabled', '#ffffff')])
    style.configure('TEntry', fieldbackground='#ffffff', bordercolor='#d3dee3', padding=1)
    style.map('TEntry', bordercolor=[('focus', ACCENT)])
    style.configure('TCombobox', fieldbackground='#ffffff', background='#ffffff', arrowcolor=INK, padding=1)
    style.configure('TNotebook', borderwidth=0, background=BACKGROUND)
    style.configure('TNotebook.Tab', padding=(9, 2), background='#e8eef1', foreground='#506670')
    style.map('TNotebook.Tab', background=[('selected', '#ffffff')], foreground=[('selected', ACCENT)])
    style.configure('Treeview', background='#ffffff', fieldbackground='#ffffff', foreground=INK,
                    borderwidth=0, font=('Malgun Gothic', 10))
    style.map('Treeview', background=[('selected', '#d6efeb')], foreground=[('selected', '#064e50')])
    linespace = tkfont.Font(root=root, family='Malgun Gothic', size=10).metrics('linespace')
    style.configure('Treeview', rowheight=linespace + 8)
    style.configure('Desk.Treeview', rowheight=max(82, linespace * 3 + 14))
    root.window_icon = ImageTk.PhotoImage(brand_mark(64), master=root)
    root.brand_icon = ImageTk.PhotoImage(brand_mark(34), master=root)
    root.iconphoto(True, root.window_icon)
    return style
