import ctypes
import os


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


_enable_windows_dpi_awareness()

from ui.app import SsoklyApp


def main() -> None:
    app = SsoklyApp()
    app.mainloop()


if __name__ == "__main__":
    main()
