"""Read a folder inside WSL (\\\\wsl$\\<distro>\\...) as root, read-only.

Home Duo disks keep restsdk/ readable only by root, and \\\\wsl$ opens files as the normal
WSL user, so plain Windows file access fails with "Permission denied". This source runs a
tiny helper inside WSL (as root, via wsl.exe -u root; no password needed) that only
stats and reads files, and talks to it over stdin/stdout.
"""
from __future__ import annotations

import base64
import json
import posixpath
import re
import struct
import subprocess
import sys
import threading

from .sources import DB_PATH, FileStat, Source, SourceError, SourceLost

_UNC = re.compile(r"^\\\\(?:\?\\UNC\\)?(wsl\$|wsl\.localhost)\\([^\\]+)((?:\\.*)?)$", re.I)
# \\wsl$ shows Linux characters that Windows forbids as U+F000 + code; map them back.
_UNMAP = {0xF000 + ord(c): c for c in '<>:"\\|?*'}

_HELPER = r'''
import json, os, stat, struct, sys
inp, out = sys.stdin.buffer, sys.stdout.buffer
def send(kind, payload=b""):
    out.write(kind + struct.pack("<I", len(payload)) + payload)
    out.flush()
for line in inp:
    try:
        req = json.loads(line)
        op, p = req["op"], req.get("path", "")
        if op == "stat":
            try:
                st = os.stat(p)
                res = {"size": st.st_size, "mtime": int(st.st_mtime), "file": stat.S_ISREG(st.st_mode),
                       "dir": stat.S_ISDIR(st.st_mode)}
            except OSError as e:
                res = {"errno": e.errno}
            send(b"S", json.dumps(res).encode())
        elif op == "read":
            with open(p, "rb") as f:
                f.seek(req["offset"])
                send(b"D", f.read(req["length"]))
        elif op == "ping":
            send(b"S", b"{}")
        else:
            send(b"X", b"unknown op")
    except Exception as e:
        send(b"X", repr(e).encode("utf-8", "replace"))
'''


def parse_wsl_path(path: str) -> tuple[str, str] | None:
    """'\\\\wsl.localhost\\Ubuntu\\mnt\\ssd' -> ('Ubuntu', '/mnt/ssd')."""
    m = _UNC.match(path.rstrip("\\"))
    if not m:
        return None
    parts = [p.translate(_UNMAP) for p in m.group(3).split("\\") if p]
    return m.group(2), "/" + "/".join(parts)


class WslSource(Source):
    def __init__(self, unc_path: str):
        parsed = parse_wsl_path(unc_path)
        if not parsed:
            raise SourceError(f"not a WSL path: {unc_path}")
        self.distro, start = parsed
        self._lock = threading.Lock()
        self._proc = None
        self._start()
        root = self._find_root(start)
        if root is None:
            self.close()
            raise SourceError(
                f"no My Cloud Home data (restsdk/data/db/index.db) found in or above {start} in WSL '{self.distro}'. "
                "Check that the disk is mounted there.")
        self.root = root
        self.label = f"WSL {self.distro}:{root} (read as root, read-only)"
        self.fingerprint = f"wsl:{self.distro}:{root}"

    # -- helper process ---------------------------------------------------
    def _start(self) -> None:
        code = "exec(__import__('base64').b64decode('%s'))" % base64.b64encode(_HELPER.encode()).decode()
        flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
        # Run in the init mount namespace, which is what \\wsl$ shows; fall back if nsenter is unavailable.
        for prefix in (["nsenter", "-t", "1", "-m", "--"], []):
            proc = subprocess.Popen(
                ["wsl.exe", "-d", self.distro, "-u", "root", "-e", *prefix, "python3", "-u", "-c", code],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=flags)
            self._proc = proc
            try:
                self._call({"op": "ping"})
                return
            except Exception:
                self._kill()
        raise SourceError(f"could not start the read helper in WSL '{self.distro}' (is python3 installed there?)")

    def _kill(self) -> None:
        if self._proc is not None:
            try:
                self._proc.kill()
            except OSError:
                pass
            self._proc = None

    def _call(self, req: dict) -> tuple[bytes, bytes]:
        with self._lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                raise SourceLost("WSL read helper is not running")
            try:
                proc.stdin.write(json.dumps(req).encode() + b"\n")
                proc.stdin.flush()
                head = _read_exact(proc.stdout, 5)
                kind, n = head[:1], struct.unpack("<I", head[1:])[0]
                payload = _read_exact(proc.stdout, n)
            except (OSError, EOFError) as exc:
                raise SourceLost(f"lost connection to WSL: {exc}") from exc
        if kind == b"X":
            raise OSError(payload.decode("utf-8", "replace"))
        return kind, payload

    def _abs(self, rel: str) -> str:
        return posixpath.join(self.root, *[p for p in rel.split("/") if p])

    def _stat_abs(self, path: str) -> dict:
        return json.loads(self._call({"op": "stat", "path": path})[1])

    def _find_root(self, start: str) -> str | None:
        cur = start or "/"
        while True:
            if self._stat_abs(posixpath.join(cur, DB_PATH)).get("file"):
                return cur
            if cur == "/":
                return None
            cur = posixpath.dirname(cur)

    # -- Source API -------------------------------------------------------
    def stat(self, rel):
        res = self._stat_abs(self._abs(rel))
        if not res.get("file"):
            return None
        return FileStat(res["size"], res["mtime"])

    def iter_file(self, rel, chunk=8 * 1024 * 1024):
        path = self._abs(rel)
        offset = 0
        while True:
            data = self._call({"op": "read", "path": path, "offset": offset, "length": chunk})[1]
            if not data:
                return
            offset += len(data)
            yield data

    def probe(self):
        try:
            return bool(self._stat_abs(posixpath.join(self.root, DB_PATH)).get("file"))
        except Exception:
            return False

    def reopen(self) -> None:
        self._kill()
        self._start()
        if not self.probe():
            raise SourceLost(f"{self.root} is no longer available in WSL '{self.distro}'")

    def close(self) -> None:
        if self._proc is not None:
            try:
                self._proc.stdin.close()
                self._proc.wait(timeout=3)
            except Exception:
                pass
            self._kill()


def _read_exact(stream, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        part = stream.read(n - len(buf))
        if not part:
            raise EOFError("helper closed the connection")
        buf += part
    return bytes(buf)
