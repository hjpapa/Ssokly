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


from ui.capture_desk import CaptureDeskApp


def main() -> None:
    _enable_windows_dpi_awareness()
    app = CaptureDeskApp()
    app.mainloop()


if __name__ == "__main__":
    main()
