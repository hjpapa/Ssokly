"""Drain Tcl theme idle work while Tk still exists; cancel application timers."""


def prepare_destroy(root):
    # ttk's ThemeChanged is queued by Tcl itself, not an `after` Python timer.
    # Cancelling after callbacks alone leaves it for the next Tk interpreter's
    # event loop, where the old interpreter's `event` command is already gone.
    for callback in root.tk.call('after', 'info'):
        root.after_cancel(callback)
    root.update_idletasks()
    for callback in root.tk.call('after', 'info'):
        root.after_cancel(callback)
