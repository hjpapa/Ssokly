import ctypes
import os
import sqlite3


def _enable_windows_dpi_awareness() -> None:
    if os.name != "nt":
        return

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


from ui.capture_desk import CaptureDeskApp


def main() -> None:
    _enable_windows_dpi_awareness()
    try:
        app = CaptureDeskApp()
    except (OSError, ValueError, sqlite3.Error) as error:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror('Ssokly를 시작하지 못했습니다', str(error), parent=root)
        root.destroy()
        return
    app.mainloop()


if __name__ == "__main__":
    main()
