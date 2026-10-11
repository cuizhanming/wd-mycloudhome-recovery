"""Stop the operating system (and the user) from accidentally formatting the source disk."""
from __future__ import annotations

import ctypes
import re
import sys

IOCTL_DISK_GET_DISK_ATTRIBUTES = 0x000700F0
IOCTL_DISK_SET_DISK_ATTRIBUTES = 0x0007C0F4
DISK_ATTRIBUTE_READ_ONLY = 0x2

WARNING = (
    "Never let the operating system initialise, format or erase this disk.\n\n"
    "Windows: if Disk Management or File Explorer asks to 'Initialize disk' or 'Format disk', "
    "click Cancel.\n"
    "macOS: if a message says the disk is not readable, click 'Ignore' (or 'Eject'), never "
    "'Initialize...'. In Disk Utility, never click Erase or Partition on this disk.\n\n"
    "The disk only looks empty or unreadable because Windows and macOS do not understand its "
    "Linux format. This app reads it without changing it."
)


class _SetDiskAttributes(ctypes.Structure):
    _fields_ = [
        ("Version", ctypes.c_uint32),
        ("Persist", ctypes.c_ubyte),
        ("Reserved1", ctypes.c_ubyte * 3),
        ("Attributes", ctypes.c_uint64),
        ("AttributesMask", ctypes.c_uint64),
        ("Reserved2", ctypes.c_uint32 * 4),
    ]


class _GetDiskAttributes(ctypes.Structure):
    _fields_ = [("Version", ctypes.c_uint32), ("Reserved1", ctypes.c_uint32), ("Attributes", ctypes.c_uint64)]


def _windows_handle(path: str):
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                    wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    # The attribute IOCTL demands a read/write handle; it is used for that IOCTL only, never for WriteFile.
    h = k32.CreateFileW(path, 0x80000000 | 0x40000000, 0x1 | 0x2, None, 3, 0, None)
    if h in (None, wintypes.HANDLE(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    return k32, h


def windows_is_read_only(path: str) -> bool:
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    h = k32.CreateFileW(path, 0x80000000, 0x1 | 0x2, None, 3, 0, None)
    if h in (None, wintypes.HANDLE(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        out = _GetDiskAttributes()
        ret = wintypes.DWORD()
        if not k32.DeviceIoControl(wintypes.HANDLE(h), IOCTL_DISK_GET_DISK_ATTRIBUTES, None, 0,
                                   ctypes.byref(out), ctypes.sizeof(out), ctypes.byref(ret), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return bool(out.Attributes & DISK_ATTRIBUTE_READ_ONLY)
    finally:
        k32.CloseHandle(wintypes.HANDLE(h))


def windows_set_read_only(path: str) -> None:
    """Mark a physical disk read-only in Windows until it is unplugged or the PC restarts.

    Persist=False: the flag lives only in the running Windows, nothing is stored on the disk.
    Afterwards any attempt to initialise, format or partition the disk fails with
    'The media is write protected', while raw reads keep working.
    """
    from ctypes import wintypes

    k32, h = _windows_handle(path)
    try:
        req = _SetDiskAttributes()
        req.Version = ctypes.sizeof(_SetDiskAttributes)
        req.Persist = 0
        req.Attributes = DISK_ATTRIBUTE_READ_ONLY
        req.AttributesMask = DISK_ATTRIBUTE_READ_ONLY
        ret = wintypes.DWORD()
        if not k32.DeviceIoControl(h, IOCTL_DISK_SET_DISK_ATTRIBUTES, ctypes.byref(req), ctypes.sizeof(req),
                                   None, 0, ctypes.byref(ret), None):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        k32.CloseHandle(h)


def protect_disk(path: str) -> str:
    """Apply whatever OS-level write protection is available. Returns a short status message."""
    if sys.platform == "win32" and re.match(r"^\\\\\.\\PhysicalDrive\d+$", path, re.I):
        try:
            if not windows_is_read_only(path):
                windows_set_read_only(path)
            if windows_is_read_only(path):
                return ("Windows write protection is ON for this disk (until it is unplugged or the PC "
                        "restarts): Windows cannot initialise or format it.")
            return "Could not confirm Windows write protection for this disk."
        except OSError as exc:
            return f"Could not turn on Windows write protection ({exc}). Never initialise or format this disk."
    if sys.platform == "darwin" and path.startswith("/dev/"):
        return ("macOS cannot lock this disk against Disk Utility. If asked, click 'Ignore', "
                "never 'Initialize...'.")
    return ""
