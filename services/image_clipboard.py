"""Copy local images to the Windows clipboard as document-compatible bitmaps."""
import ctypes
from ctypes import wintypes
import io
import time

from PIL import Image


def copy_image(image_or_bytes, *, owner):
    """Transfer a full-size CF_DIB; Windows owns the memory after success.

    The caller supplies its live window handle and runs this on the UI thread.
    Prepare the bitmap before opening/emptying the clipboard so invalid images
    and a busy clipboard do not replace the previous clipboard contents.
    """
    if not owner:
        raise ValueError('A clipboard owner window is required.')
    with io.BytesIO() as buffer:
        if isinstance(image_or_bytes, Image.Image):
            _write_bitmap(image_or_bytes, buffer)
        else:
            with Image.open(io.BytesIO(image_or_bytes)) as image:
                _write_bitmap(image, buffer)
        dib = buffer.getvalue()[14:]  # CF_DIB excludes the BMP file header.

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    user = ctypes.WinDLL('user32', use_last_error=True)
    kernel.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel.GlobalLock.restype = ctypes.c_void_p
    kernel.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel.GlobalUnlock.restype = wintypes.BOOL
    kernel.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel.GlobalFree.restype = wintypes.HGLOBAL
    user.OpenClipboard.argtypes = [wintypes.HWND]
    user.OpenClipboard.restype = wintypes.BOOL
    user.EmptyClipboard.argtypes = []
    user.EmptyClipboard.restype = wintypes.BOOL
    user.CloseClipboard.argtypes = []
    user.CloseClipboard.restype = wintypes.BOOL
    user.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user.SetClipboardData.restype = wintypes.HANDLE

    handle = kernel.GlobalAlloc(0x0002, len(dib))  # GMEM_MOVEABLE
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    opened = False
    try:
        pointer = kernel.GlobalLock(handle)
        if not pointer:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            ctypes.memmove(pointer, dib, len(dib))
        finally:
            kernel.GlobalUnlock(handle)
        for attempt in range(5):
            opened = bool(user.OpenClipboard(owner))
            if opened:
                break
            if attempt < 4:
                time.sleep(0.02)
        if not opened or not user.EmptyClipboard() or not user.SetClipboardData(8, handle):
            raise ctypes.WinError(ctypes.get_last_error())
        handle = None
    finally:
        if opened:
            user.CloseClipboard()
        if handle:
            kernel.GlobalFree(handle)


def _write_bitmap(image, buffer):
    # Flatten transparent imported images onto white for ordinary documents.
    with image.convert('RGBA') as rgba, Image.new('RGB', image.size, 'white') as bitmap:
        bitmap.paste(rgba, mask=rgba.getchannel('A'))
        bitmap.save(buffer, 'BMP')
