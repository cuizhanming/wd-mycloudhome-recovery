"""Read-only access to raw disks and disk images.

Raw devices on Windows (\\\\.\\PhysicalDriveN) and macOS (/dev/rdiskN) only accept
reads whose offset and length are multiples of the sector size, so every read is
widened to ALIGN-byte boundaries. The device is never opened for writing.
"""
from __future__ import annotations

import os
import sys
import threading
from collections import OrderedDict

ALIGN = 4096  # covers both 512-byte and 4Kn sector devices
CACHE_BLOCK = 64 * 1024
CACHE_BLOCKS = 512  # 32 MiB of metadata cache
MAX_READ = 16 * 1024 * 1024


class DeviceError(OSError):
    pass


class BlockDevice:
    def __init__(self, path: str, fd: int | None = None):
        self.path = path
        if fd is None:
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
            fd = os.open(path, flags)
        self._fd = fd
        self._lock = threading.Lock()
        self._cache: OrderedDict[int, bytes] = OrderedDict()
        self._closed = False

    def _read_aligned(self, offset: int, length: int) -> bytes:
        if self._closed:
            raise DeviceError(f"device {self.path} is closed")
        chunks = []
        remaining = length
        pos = offset
        while remaining > 0:
            n = min(remaining, MAX_READ)
            if hasattr(os, "pread"):
                buf = os.pread(self._fd, n, pos)
            else:
                with self._lock:
                    os.lseek(self._fd, pos, os.SEEK_SET)
                    buf = os.read(self._fd, n)
            if not buf:
                break  # end of device
            chunks.append(buf)
            pos += len(buf)
            remaining -= len(buf)
        return b"".join(chunks)

    def read(self, offset: int, length: int) -> bytes:
        """Uncached read of an arbitrary byte range (used for bulk file data)."""
        if length <= 0:
            return b""
        start = offset - (offset % ALIGN)
        end = -(-(offset + length) // ALIGN) * ALIGN
        data = self._read_aligned(start, end - start)
        return data[offset - start: offset - start + length]

    def read_cached(self, offset: int, length: int) -> bytes:
        """Cached read for small metadata structures."""
        out = []
        pos = offset
        end = offset + length
        while pos < end:
            blk = pos // CACHE_BLOCK
            with self._lock:
                data = self._cache.get(blk)
                if data is not None:
                    self._cache.move_to_end(blk)
            if data is None:
                data = self._read_aligned(blk * CACHE_BLOCK, CACHE_BLOCK)
                with self._lock:
                    self._cache[blk] = data
                    if len(self._cache) > CACHE_BLOCKS:
                        self._cache.popitem(last=False)
            lo = pos - blk * CACHE_BLOCK
            hi = min(CACHE_BLOCK, end - blk * CACHE_BLOCK)
            piece = data[lo:hi]
            if not piece:
                break
            out.append(piece)
            pos += len(piece)
        return b"".join(out)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                os.close(self._fd)
            except OSError:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class SubDevice:
    """A byte window (partition or RAID data area) inside another device."""

    def __init__(self, parent, offset: int, size: int | None = None):
        self.parent = parent
        self.offset = offset
        self.size = size
        self.path = getattr(parent, "path", "?")

    def read(self, offset: int, length: int) -> bytes:
        return self.parent.read(self.offset + offset, length)

    def read_cached(self, offset: int, length: int) -> bytes:
        return self.parent.read_cached(self.offset + offset, length)

    def close(self) -> None:
        self.parent.close()


def open_macos_authorized(path: str) -> int:
    """Ask macOS for permission to read a raw disk and return an open fd.

    /usr/libexec/authopen shows the standard admin prompt and passes the opened
    descriptor back over a UNIX socket (SCM_RIGHTS), so the app never runs as root.
    """
    import socket
    import subprocess

    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        proc = subprocess.Popen(
            ["/usr/libexec/authopen", "-stdoutpipe", "-o", str(os.O_RDONLY), path],
            stdout=child.fileno(),
        )
        child.close()
        _msg, fds, _flags, _addr = socket.recv_fds(parent, 1024, 1)
        proc.wait()
    finally:
        parent.close()
    if not fds:
        raise DeviceError(f"authorization to read {path} was denied")
    return fds[0]


def open_device(path: str) -> BlockDevice:
    """Open a disk or image read-only, requesting authorization on macOS if needed."""
    try:
        return BlockDevice(path)
    except PermissionError:
        if sys.platform == "darwin" and path.startswith("/dev/"):
            return BlockDevice(path, fd=open_macos_authorized(path))
        raise
