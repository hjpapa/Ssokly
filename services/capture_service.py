import tkinter as tk
from typing import Optional, Tuple

import mss
from PIL import Image


MIN_CAPTURE_SIZE = 10


def _virtual_screen_bounds() -> Tuple[int, int, int, int]:
    with mss.MSS() as sct:
        monitor = sct.monitors[0]
        return (
            monitor["left"],
            monitor["top"],
            monitor["width"],
            monitor["height"],
        )


class RegionSelector:
    def __init__(self, parent: tk.Tk) -> None:
        self.parent = parent
        self.start_x = 0
        self.start_y = 0
        self.rect_id: Optional[int] = None
        self.selection: Optional[Tuple[int, int, int, int]] = None

        left, top, width, height = _virtual_screen_bounds()

        self.overlay = tk.Toplevel(parent)
        self.overlay.overrideredirect(True)
        self.overlay.geometry(f"{width}x{height}{left:+d}{top:+d}")
        self.overlay.attributes("-alpha", 0.25)
        self.overlay.attributes("-topmost", True)
        self.overlay.configure(bg="black")
        self.overlay.title("캡처 영역 선택")

        self.canvas = tk.Canvas(
            self.overlay,
            cursor="crosshair",
            bg="black",
            highlightthickness=0,
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)

        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.overlay.bind("<Escape>", self._on_cancel)

        self.overlay.update_idletasks()
        self.overlay.focus_force()
        self.overlay.grab_set()

    def select(self) -> Optional[Tuple[int, int, int, int]]:
        self.parent.wait_window(self.overlay)
        return self.selection

    def _on_press(self, event: tk.Event) -> None:
        self.start_x = event.x_root
        self.start_y = event.y_root
        self.rect_id = self.canvas.create_rectangle(
            event.x,
            event.y,
            event.x,
            event.y,
            outline="white",
            width=2,
        )

    def _on_drag(self, event: tk.Event) -> None:
        if self.rect_id is None:
            return

        start_canvas_x = self.start_x - self.overlay.winfo_rootx()
        start_canvas_y = self.start_y - self.overlay.winfo_rooty()
        self.canvas.coords(self.rect_id, start_canvas_x, start_canvas_y, event.x, event.y)

    def _on_release(self, event: tk.Event) -> None:
        x1 = min(self.start_x, event.x_root)
        y1 = min(self.start_y, event.y_root)
        x2 = max(self.start_x, event.x_root)
        y2 = max(self.start_y, event.y_root)

        if x2 - x1 < MIN_CAPTURE_SIZE or y2 - y1 < MIN_CAPTURE_SIZE:
            self.selection = None
        else:
            self.selection = (x1, y1, x2, y2)

        self.overlay.grab_release()
        self.overlay.destroy()

    def _on_cancel(self, _event: tk.Event) -> None:
        self.selection = None
        self.overlay.grab_release()
        self.overlay.destroy()


def select_region(parent: tk.Tk) -> Optional[Tuple[int, int, int, int]]:
    selector = RegionSelector(parent)
    return selector.select()


def capture_region(region: Tuple[int, int, int, int]) -> Image.Image:
    x1, y1, x2, y2 = region
    monitor = {
        "left": x1,
        "top": y1,
        "width": x2 - x1,
        "height": y2 - y1,
    }

    with mss.MSS() as sct:
        screenshot = sct.grab(monitor)
        return Image.frombytes(
            "RGB",
            screenshot.size,
            screenshot.raw,
            "raw",
            "BGRX",
        )


def capture_selected_region(parent: tk.Tk) -> Optional[Image.Image]:
    region = select_region(parent)
    if region is None:
        return None
    return capture_region(region)
