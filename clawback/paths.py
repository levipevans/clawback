"""Profile and filesystem locations.

The assistant never guesses at user data. It only sees:

* its own profile directory (config, audit, state, workspace), and
* the read-only roots we explicitly mount into the sandbox.

On Windows the standard user folders are discovered through the shell API so
that redirected folders (OneDrive, etc.) resolve correctly.
"""

from __future__ import annotations

import ctypes
import os
import sys
import uuid
from pathlib import Path

APP_DIR_NAME = "Clawback"

# Known folder GUIDs from <knownfolders.h>.
_FOLDER_IDS: dict[str, str] = {
    "Desktop": "B4BFCC3A-DB2C-424C-B029-7FE99A87C641",
    "Documents": "FDD39AD0-238F-46AF-ADB4-6C85480369C7",
    "Downloads": "374DE290-123F-4565-9164-39C4925E467B",
    "Pictures": "33E28130-4E1E-4676-835A-98395C3BC3BB",
    "Music": "4BD8D571-6D19-48D3-BE97-422220080E43",
    "Videos": "18989B1D-99B5-455B-841C-AB7C74E4DDFC",
}

_FORBIDDEN_NAMES = {
    "appdata",
    "application data",
    "local settings",
    "ntuser.dat",
    "ntuser.ini",
}


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _known_folder(name: str) -> Path | None:
    if sys.platform != "win32":
        fallback = Path.home() / name
        return fallback if fallback.exists() else None

    uid = uuid.UUID(_FOLDER_IDS[name])
    raw = ctypes.create_string_buffer(uid.bytes_le, 16)
    ptr = ctypes.cast(raw, ctypes.POINTER(_GUID))
    out = ctypes.c_wchar_p()

    shell32 = ctypes.windll.shell32
    shell32.SHGetKnownFolderPath.argtypes = [
        ctypes.POINTER(_GUID),
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_wchar_p),
    ]
    shell32.SHGetKnownFolderPath.restype = ctypes.c_long

    hr = shell32.SHGetKnownFolderPath(ptr, 0, None, ctypes.byref(out))
    if hr != 0 or not out.value:
        return None
    try:
        return Path(out.value)
    finally:
        ole32 = ctypes.windll.ole32
        ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        ole32.CoTaskMemFree(ctypes.cast(out, ctypes.c_void_p))


def profile_dir() -> Path:
    """The assistant's own private directory. Never a user document folder."""
    override = os.environ.get("CLAWBACK_PROFILE_DIR")
    if override:
        path = Path(os.path.expandvars(override)).expanduser()
    elif os.environ.get("APPDATA"):
        path = Path(os.environ["APPDATA"]) / APP_DIR_NAME
    else:
        path = Path.home() / f".{APP_DIR_NAME.lower()}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def workspace_dir(profile: Path, configured: str | None) -> Path:
    """The single writable folder for the sandbox."""
    if configured:
        path = Path(os.path.expandvars(configured)).expanduser()
    else:
        path = profile / "workspace"
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_layout(profile: Path) -> Path:
    """Create the profile layout and return the empty-mount helpers."""
    for name in ("workspace", "runtime"):
        (profile / name).mkdir(parents=True, exist_ok=True)
    empty_dir = profile / "runtime" / "empty_dir"
    empty_dir.mkdir(parents=True, exist_ok=True)
    empty_file = profile / "runtime" / "empty_file"
    if not empty_file.exists():
        empty_file.write_bytes(b"")
    return profile


def is_sensitive_name(path: Path) -> bool:
    """True for dotfiles and the Windows credential/config folders."""
    return any(
        part.startswith(".") or part.lower() in _FORBIDDEN_NAMES
        for part in path.parts
        if part not in ("\\", "/", "")
    )


def standard_read_roots() -> dict[str, Path]:
    """Standard user folders, read-only. Missing or sensitive ones omitted."""
    discovered: dict[str, Path] = {}
    for name in _FOLDER_IDS:
        path = _known_folder(name)
        if path is None:
            continue
        try:
            if not path.exists() or not path.is_dir():
                continue
        except OSError:
            continue
        if is_sensitive_name(path):
            continue
        discovered[name] = path.resolve()
    return discovered


def normalize(path: str | Path) -> Path:
    """Expand env vars/user home and resolve without requiring existence."""
    return Path(os.path.expandvars(str(path))).expanduser().resolve()
