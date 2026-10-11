"""Sources the app can read a Home Duo volume from: raw disk, disk image, or mounted folder."""
from __future__ import annotations

import os
from dataclasses import dataclass

from .blockdev import SubDevice, open_device
from .ext4 import Ext4Error, Ext4Volume, probe_ext
from .partitions import find_partitions, read_md_superblock

DB_PATH = "restsdk/data/db/index.db"
_WSL_MAPPED = '<>:"\\|?*'
_WSL_MAP = {ord(c): 0xF000 + ord(c) for c in _WSL_MAPPED}


class SourceError(Exception):
    pass


class SourceLost(SourceError):
    """The source disk stopped responding (unplugged, sleep, I/O failure)."""


@dataclass
class FileStat:
    size: int
    mtime: int  # seconds


class Source:
    label = ""
    fingerprint = ""
    protection = ""

    def stat(self, rel: str) -> FileStat | None:
        raise NotImplementedError

    def iter_file(self, rel: str, chunk: int = 8 * 1024 * 1024):
        raise NotImplementedError

    def read_bytes(self, rel: str) -> bytes:
        return b"".join(self.iter_file(rel))

    def probe(self) -> bool:
        """True if the source still responds."""
        raise NotImplementedError

    def reopen(self) -> None:
        pass

    def close(self) -> None:
        pass


class FolderSource(Source):
    """An already-mounted Home Duo volume (e.g. /mnt/ssd, \\\\wsl$\\Ubuntu\\mnt\\ssd)."""

    def __init__(self, root: str):
        root = os.path.abspath(root)
        if not os.path.isdir(root):
            raise SourceError(f"folder not found: {root}")
        self.root = root
        found = self._find_volume_root(root)
        if found is None:
            denied = self._permission_blocked(root)
            if denied:
                raise SourceError(
                    f"'{denied}' exists but cannot be read: on Home Duo disks it is readable only by the Linux "
                    "root user, and \\\\wsl$ opens files as your normal user. Choose the folder through "
                    "\\\\wsl$ / \\\\wsl.localhost so the app can read it via WSL, or use 'Open disk'.")
            raise SourceError(
                f"no My Cloud Home data found in or above {root}. Choose the top folder of the disk, "
                "the one that contains 'restsdk' (for WSL: \\\\wsl$\\Ubuntu\\mnt\\ssd).")
        self.root = found
        self.label = f"Folder {self.root}"
        self.fingerprint = f"folder:{self.root}"

    def _find_volume_root(self, start: str) -> str | None:
        """Accept the volume root, any folder inside it, or a parent one level above."""
        cur = start
        for _ in range(8):
            self.root = cur
            if self.stat(DB_PATH):
                return cur
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent
        try:
            for name in sorted(os.listdir(start)):
                sub = os.path.join(start, name)
                self.root = sub
                if os.path.isdir(sub) and self.stat(DB_PATH):
                    return sub
        except OSError:
            pass
        return None

    @staticmethod
    def _permission_blocked(start: str) -> str | None:
        cur = start
        for _ in range(8):
            rs = os.path.join(cur, "restsdk")
            if os.path.isdir(rs):
                try:
                    os.listdir(os.path.join(rs, "data", "db"))
                except PermissionError:
                    return rs
                except OSError:
                    pass
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent
        return None

    def _path(self, rel: str) -> str:
        path = os.path.join(self.root, *[p for p in rel.split("/") if p])
        if os.name == "nt" and not os.path.exists(path) and any(c in rel for c in _WSL_MAPPED):
            # \\wsl$ shows Linux names containing <>:"\|?* with those characters moved to U+F000+code.
            mapped = os.path.join(self.root, *[p.translate(_WSL_MAP) for p in rel.split("/") if p])
            if os.path.exists(mapped):
                return mapped
        return path

    def stat(self, rel):
        try:
            st = os.stat(self._path(rel))
        except OSError:
            return None
        if not os.path.isfile(self._path(rel)):
            return None
        return FileStat(st.st_size, int(st.st_mtime))

    def iter_file(self, rel, chunk=8 * 1024 * 1024):
        with open(self._path(rel), "rb") as f:
            while True:
                data = f.read(chunk)
                if not data:
                    break
                yield data

    def probe(self):
        try:
            os.listdir(self.root)
            return True
        except OSError:
            return False


@dataclass
class VolumeCandidate:
    description: str
    volume: Ext4Volume
    has_db: bool
    size: int


def scan_volumes(dev) -> tuple[list[VolumeCandidate], list[str]]:
    """Find ext filesystems on a device, looking through md RAID1 members."""
    found: list[VolumeCandidate] = []
    notes: list[str] = []
    for part in find_partitions(dev):
        data = SubDevice(dev, part.offset, part.size or None)
        desc = f"partition {part.index}" if part.index else "whole disk"
        raid = read_md_superblock(dev, part.offset, part.size)
        if raid:
            if raid.level != 1 and raid.raid_disks > 1:
                notes.append(f"{desc}: RAID level {raid.level} is not supported (only RAID1/mirror)")
                continue
            data = SubDevice(dev, part.offset + raid.data_offset, raid.data_size)
            desc += f" (md RAID{raid.level} v{raid.version} member '{raid.name}')"
        try:
            if not probe_ext(data):
                continue
            vol = Ext4Volume(data)
        except (Ext4Error, OSError) as exc:
            notes.append(f"{desc}: {exc}")
            continue
        found.append(VolumeCandidate(desc, vol, vol.lookup(DB_PATH) is not None, part.size))
    return found, notes


class DiskSource(Source):
    """Reads a raw disk or disk image directly. Never writes."""

    def __init__(self, device_path: str):
        self.device_path = device_path
        self.dev = None
        self.vol: Ext4Volume | None = None
        self._open(device_path)

    def _open(self, path: str, want_uuid: str | None = None) -> None:
        dev = open_device(path)
        try:
            cands, notes = scan_volumes(dev)
        except Exception:
            dev.close()
            raise
        if want_uuid:
            cands = [c for c in cands if c.volume.uuid == want_uuid]
        cands.sort(key=lambda c: (c.has_db, c.size), reverse=True)
        if not cands or not cands[0].has_db:
            dev.close()
            extra = ("; " + "; ".join(notes)) if notes else ""
            raise SourceError(f"no My Cloud Home data volume (restsdk) found on {path}{extra}")
        best = cands[0]
        self.dev = dev
        self.device_path = path
        self.vol = best.volume
        self.description = best.description
        self.label = f"{path} - {best.description}"
        self.fingerprint = f"ext4:{best.volume.uuid}"
        self.protection = self._protect(path)

    @staticmethod
    def _protect(path: str) -> str:
        from .disks import list_disks
        from .protect import protect_disk

        if any(d.path.lower() == path.lower() and d.is_system for d in list_disks()):
            return ""  # never touch attributes of the disk Windows/macOS runs from
        return protect_disk(path)

    def stat(self, rel):
        ino = self.vol.lookup(rel)
        if ino is None:
            return None
        inode = self.vol.inode(ino)
        if not inode.is_file:
            return None
        return FileStat(inode.size, inode.mtime)

    def iter_file(self, rel, chunk=8 * 1024 * 1024):
        ino = self.vol.lookup(rel)
        if ino is None:
            raise FileNotFoundError(rel)
        yield from self.vol.iter_file(self.vol.inode(ino), chunk)

    def probe(self):
        try:
            sb = self.vol.dev.read(1024, 1024)  # uncached: really touches the disk
            return len(sb) == 1024 and sb[0x38:0x3A] == b"\x53\xef"
        except Exception:
            return False

    def reopen(self) -> None:
        """Re-open after a disconnect; the disk may come back under a new number."""
        from .disks import list_disks

        uuid = self.vol.uuid if self.vol else None
        self.close()
        paths = [self.device_path] + [d.path for d in list_disks() if d.path != self.device_path]
        last: Exception | None = None
        for p in paths:
            try:
                self._open(p, want_uuid=uuid)
                return
            except Exception as exc:  # try the next disk
                last = exc
        raise SourceLost(f"source disk not found again: {last}")

    def close(self) -> None:
        if self.dev is not None:
            self.dev.close()
            self.dev = None


def open_source(spec: str) -> Source:
    """WSL path -> WslSource; folder -> FolderSource; device path or image file -> DiskSource."""
    from .wsl_source import WslSource, parse_wsl_path

    if parse_wsl_path(spec):
        return WslSource(spec)
    if os.path.isdir(spec):
        return FolderSource(spec)
    return DiskSource(spec)
