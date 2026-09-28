"""User paths independent of the working directory and EXE extraction folder."""
import ctypes
import os
from pathlib import Path


def default_app_data_dir():
    local = os.getenv('LOCALAPPDATA', '').strip()
    return (Path(local).expanduser() / 'Ssokly' if local else Path.home() / '.ssokly').resolve()


def desktop_directory():
    if os.name == 'nt':
        # The shell resolves redirected/OneDrive desktops, unlike ~/Desktop.
        buffer = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buffer) != 0:
            raise OSError('Windows 바탕화면 위치를 확인할 수 없습니다.')
        return Path(buffer.value)
    return Path.home() / 'Desktop'
