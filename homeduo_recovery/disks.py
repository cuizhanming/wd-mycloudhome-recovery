"""List physical disks and handle the privileges needed to read them."""
from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
from dataclasses import dataclass


@dataclass
class DiskInfo:
    path: str
    name: str
    size: int
    bus: str = ""
    is_system: bool = False

    def __str__(self) -> str:
        gb = self.size / 1e9
        sysmark = " [system disk]" if self.is_system else ""
        bus = f" {self.bus}" if self.bus else ""
        return f"{self.name} ({gb:,.0f} GB{bus}) - {self.path}{sysmark}"


def _run(cmd: list[str]) -> bytes:
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    return subprocess.run(cmd, capture_output=True, check=True, timeout=30, **kwargs).stdout


def _list_windows() -> list[DiskInfo]:
    out = _run([
        "powershell", "-NoProfile", "-Command",
        "Get-Disk | Select-Object Number,FriendlyName,Size,BusType,IsBoot,IsSystem | ConvertTo-Json -Compress",
    ])
    data = json.loads(out.decode("utf-8", "replace") or "[]")
    if isinstance(data, dict):
        data = [data]
    bus_names = {7: "USB", 17: "NVMe", 11: "SATA", 8: "RAID", 10: "SAS", 1: "SCSI", 3: "ATA"}
    disks = []
    for d in data:
        bus = d.get("BusType")
        disks.append(DiskInfo(
            path=f"\\\\.\\PhysicalDrive{d['Number']}",
            name=d.get("FriendlyName") or f"Disk {d['Number']}",
            size=int(d.get("Size") or 0),
            bus=bus_names.get(bus, str(bus)) if isinstance(bus, int) else str(bus or ""),
            is_system=bool(d.get("IsBoot") or d.get("IsSystem")),
        ))
    return disks


def _list_macos() -> list[DiskInfo]:
    listing = plistlib.loads(_run(["diskutil", "list", "-plist", "physical"]))
    disks = []
    for ident in listing.get("WholeDisks", []):
        try:
            info = plistlib.loads(_run(["diskutil", "info", "-plist", ident]))
        except Exception:
            info = {}
        disks.append(DiskInfo(
            path=f"/dev/r{ident}",
            name=info.get("MediaName") or ident,
            size=int(info.get("TotalSize") or info.get("Size") or 0),
            bus=info.get("BusProtocol", ""),
            is_system=bool(info.get("Internal")) and ident == "disk0",
        ))
    return disks


def _list_linux() -> list[DiskInfo]:
    disks = []
    for name in sorted(os.listdir("/sys/block")):
        if name.startswith(("loop", "ram", "zram", "md", "dm-")):
            continue
        base = f"/sys/block/{name}"
        try:
            size = int(open(f"{base}/size").read()) * 512
        except OSError:
            continue
        model = ""
        try:
            model = open(f"{base}/device/model").read().strip()
        except OSError:
            pass
        disks.append(DiskInfo(f"/dev/{name}", model or name, size))
    return disks


def list_disks() -> list[DiskInfo]:
    try:
        if sys.platform == "win32":
            return _list_windows()
        if sys.platform == "darwin":
            return _list_macos()
        return _list_linux()
    except Exception:
        return []


def usb_disks_held_by_wsl() -> list[str]:
    """Windows: USB storage devices passed to WSL with usbipd (they are invisible to Windows)."""
    if sys.platform != "win32":
        return []
    exe = os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "usbipd-win", "usbipd.exe")
    if not os.path.isfile(exe):
        return []
    try:
        out = _run([exe, "list"]).decode("utf-8", "replace")
    except Exception:
        return []
    held = []
    for line in out.splitlines():
        parts = line.split()
        if (parts and "-" in parts[0] and "Storage" in line
                and ("Attached" in line or "Shared" in line)):
            held.append(parts[0])
    return held


def is_admin() -> bool:
    if sys.platform == "win32":
        import ctypes

        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    return os.geteuid() == 0


def relaunch_as_admin() -> bool:
    """Windows only: restart this program elevated (UAC prompt). Returns True if launched."""
    if sys.platform != "win32":
        return False
    import ctypes

    if getattr(sys, "frozen", False):
        exe, params, cwd = sys.executable, subprocess.list2cmdline(sys.argv[1:]), os.getcwd()
    else:
        exe = sys.executable
        params = subprocess.list2cmdline(["-m", "homeduo_recovery"] + sys.argv[1:])
        cwd = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, cwd, 1)
    return rc > 32
