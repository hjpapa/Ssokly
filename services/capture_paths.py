"""Validate app-owned capture entries without confusing Windows OS aliases.

Windows AppData virtualization may resolve a *file* to a package-private path
while its directory resolves to the logical AppData path. Keep the logical
entry for all I/O; an outside resolution is accepted only when the OS reports
the exact same, stable, single-linked regular file at both names. This does not
whitelist package directories, infer package names, or follow reparse points.
"""
import os
from pathlib import Path
import stat


_REPARSE_POINT = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
_WINDOWS_ALIASES = os.name == 'nt'
_RESERVED = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}


def _identity(value):
    return value.st_dev, value.st_ino


def _file_identity(value):
    return _identity(value), value.st_size, value.st_mtime_ns


def _is_reparse(path, value):
    return (stat.S_ISLNK(value.st_mode) or bool(getattr(value, 'st_file_attributes', 0) & _REPARSE_POINT)
            or path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()))


def _regular_entry(path):
    value = path.lstat()
    if _is_reparse(path, value) or not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
        raise ValueError('capture entry must be a regular, single-linked file')
    return value


def _directory_chain(directory):
    # The stored root was canonical when the store opened. Do not silently
    # authorize a replacement junction in that root or any of its ancestors.
    identities = []
    for path in (directory, *directory.parents):
        value = path.lstat()
        if not stat.S_ISDIR(value.st_mode) or _is_reparse(path, value):
            raise ValueError('capture inbox ancestry must not contain reparse points')
        identities.append(_identity(value))
    return tuple(identities)


def owned_capture_path(directory: Path, storage_name: str) -> Path:
    if not isinstance(storage_name, str) or not storage_name:
        raise ValueError('storage_name must be a non-empty filename')
    if (Path(storage_name).name != storage_name or storage_name in {'.', '..'}
            or any(character in storage_name for character in ('/', '\\', ':', '\x00'))
            or storage_name.endswith(('.', ' ')) or storage_name.split('.', 1)[0].upper() in _RESERVED):
        raise ValueError('storage_name must contain only a safe filename')

    directory = directory.absolute()
    ancestry_before = _directory_chain(directory)
    root = directory.resolve(strict=True)
    root_before = root.lstat()
    if not stat.S_ISDIR(root_before.st_mode) or _is_reparse(root, root_before):
        raise ValueError('capture inbox must be a regular directory')
    if root != directory and (not root_before.st_ino or _identity(root_before) != ancestry_before[0]):
        raise ValueError('capture inbox resolution is not the same directory')
    candidate = root / storage_name
    try:
        before = _regular_entry(candidate)
    except FileNotFoundError:
        # A fresh output name has no OS alias yet. Never approve an outside
        # resolution or a dangling link as a future writable entry.
        if candidate.resolve(strict=False) != candidate:
            raise ValueError('capture path is outside the app-owned inbox')
        before = None
    else:
        resolved = candidate.resolve(strict=True)
        if resolved != candidate:
            if not _WINDOWS_ALIASES or resolved.name != storage_name:
                raise ValueError('capture path is outside the app-owned inbox')
            target = _regular_entry(resolved)
            after = _regular_entry(candidate)
            if (not before.st_ino or not target.st_ino
                    or _file_identity(before) != _file_identity(target)
                    or _file_identity(before) != _file_identity(after)):
                raise ValueError('capture path is outside the app-owned inbox')
        elif _file_identity(before) != _file_identity(_regular_entry(candidate)):
            raise ValueError('capture entry changed during validation')

    root_after = root.lstat()
    if (_identity(root_before) != _identity(root_after) or not stat.S_ISDIR(root_after.st_mode)
            or _is_reparse(root, root_after) or directory.resolve(strict=True) != root
            or _directory_chain(directory) != ancestry_before):
        raise ValueError('capture inbox changed during validation')
    return candidate
