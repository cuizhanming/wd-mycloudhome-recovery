"""Resumable copy of selected Home Duo files to a target folder.

Every job keeps a manifest (SQLite) in <target>/.homeduo-recovery/. A file is written
to "<name>.partial", fsync'd, size-checked and only then renamed into place and
marked done. Retrying or restarting re-runs everything not yet marked done; an
interrupted file is simply copied again from the beginning.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
from dataclasses import dataclass
from typing import Callable, Iterable

from .sources import Source, SourceLost

MANIFEST_DIR = ".homeduo-recovery"
MANIFEST_NAME = "manifest.sqlite"
PARTIAL = ".partial"

PENDING, DONE, FAILED, MISSING = "pending", "done", "failed", "missing"


class TargetError(Exception):
    """The target disk is unavailable or full."""


class Stopped(Exception):
    pass


class _SourceReadError(Exception):
    pass


@dataclass
class PlanItem:
    file_id: str
    candidates: list[str]
    dest_rel: str
    size: int
    mtime_ms: int


@dataclass
class Progress:
    files_done: int
    files_total: int
    bytes_done: int
    bytes_total: int
    current: str
    status: str = ""


def long_path(path: str) -> str:
    """Windows: bypass the 260-character MAX_PATH limit."""
    if sys.platform != "win32":
        return path
    path = os.path.abspath(path)
    if path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


def has_manifest(target: str) -> bool:
    return os.path.isfile(os.path.join(target, MANIFEST_DIR, MANIFEST_NAME))


class CopyJob:
    def __init__(self, target: str):
        self.target = os.path.abspath(target)
        mdir = os.path.join(self.target, MANIFEST_DIR)
        os.makedirs(mdir, exist_ok=True)
        if sys.platform == "win32":
            try:
                import ctypes

                ctypes.windll.kernel32.SetFileAttributesW(mdir, 0x2)  # hidden
            except Exception:
                pass
        self.db = sqlite3.connect(os.path.join(mdir, MANIFEST_NAME), check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS items(
                file_id TEXT PRIMARY KEY,
                candidates TEXT NOT NULL,
                dest_rel TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime_ms INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                source_rel TEXT,
                error TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                updated REAL
            );
            CREATE INDEX IF NOT EXISTS items_status ON items(status);
        """)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(items)")}
        for name, decl in (("copied_size", "INTEGER"), ("digest", "TEXT")):
            if name not in cols:
                self.db.execute(f"ALTER TABLE items ADD COLUMN {name} {decl}")
        self.db.commit()

    def bind_source(self, fingerprint: str, force: bool = False) -> str | None:
        """Tie this job to one source disk. Returns the other fingerprint on mismatch (unless forced)."""
        prev = self.get_meta("source")
        if prev and prev != fingerprint and not force:
            return prev
        if not prev:
            self.set_meta("source", fingerprint)
        return None

    def reconcile(self) -> int:
        """Re-queue 'done' files whose output is gone or has the wrong size. Returns how many."""
        bad = []
        for file_id, dest_rel, copied in self.db.execute(
                "SELECT file_id, dest_rel, copied_size FROM items WHERE status=?", (DONE,)):
            lp = long_path(os.path.join(self.target, *dest_rel.split("/")))
            try:
                ok = os.path.getsize(lp) == copied if copied is not None else os.path.isfile(lp)
            except OSError:
                ok = False
            if not ok:
                bad.append((file_id,))
        if bad:
            self.db.executemany(
                "UPDATE items SET status='pending', error='output missing or changed; re-queued' WHERE file_id=?", bad)
            self.db.commit()
        return len(bad)

    def close(self) -> None:
        self.db.close()

    # -- metadata ---------------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, value))
        self.db.commit()

    # -- planning ---------------------------------------------------------
    def add(self, items: Iterable[PlanItem]) -> int:
        """Add files to the job; files already in the manifest keep their status."""
        before = self.db.total_changes
        self.db.executemany(
            "INSERT OR IGNORE INTO items(file_id, candidates, dest_rel, size, mtime_ms) VALUES(?,?,?,?,?)",
            ((i.file_id, json.dumps(i.candidates), i.dest_rel, i.size, i.mtime_ms) for i in items),
        )
        self.db.commit()
        return self.db.total_changes - before

    def counts(self) -> dict[str, tuple[int, int]]:
        out = {s: (0, 0) for s in (PENDING, DONE, FAILED, MISSING)}
        for status, n, b in self.db.execute("SELECT status, COUNT(*), COALESCE(SUM(size),0) FROM items GROUP BY status"):
            out[status] = (n, b)
        return out

    def problems(self) -> list[tuple[str, str, str]]:
        return self.db.execute(
            "SELECT status, dest_rel, COALESCE(error,'') FROM items WHERE status IN (?,?) ORDER BY dest_rel",
            (FAILED, MISSING),
        ).fetchall()

    def write_report(self) -> str:
        import csv

        path = os.path.join(self.target, MANIFEST_DIR, "problems.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["status", "path", "error"])
            w.writerows(self.problems())
        return path

    def _mark(self, file_id: str, status: str, error: str | None = None, source_rel: str | None = None,
              dest_rel: str | None = None, copied_size: int | None = None, digest: str | None = None) -> None:
        self.db.execute(
            "UPDATE items SET status=?, error=?, source_rel=COALESCE(?, source_rel), "
            "dest_rel=COALESCE(?, dest_rel), copied_size=?, digest=?, attempts=attempts+1, updated=? "
            "WHERE file_id=?",
            (status, error, source_rel, dest_rel, copied_size, digest, time.time(), file_id),
        )
        self.db.commit()

    # -- copying ----------------------------------------------------------
    def run(self, source: Source, should_stop: Callable[[], bool] = lambda: False,
            on_progress: Callable[[Progress], None] | None = None,
            on_log: Callable[[str], None] | None = None,
            include_missing: bool = False) -> str:
        """Copy everything not yet done. Returns 'completed', 'stopped', 'source_lost' or 'target_error'."""
        log = on_log or (lambda m: None)
        try:
            requeued = self.reconcile()
        except (OSError, sqlite3.Error) as exc:
            log(f"Target problem: {exc}")
            return "target_error"
        if requeued:
            log(f"{requeued:,} previously copied files are missing or changed in the target; copying them again")
        statuses = (PENDING, FAILED, MISSING) if include_missing else (PENDING, FAILED)
        marks = ",".join("?" * len(statuses))
        todo = self.db.execute(
            f"SELECT file_id, candidates, dest_rel, size, mtime_ms FROM items WHERE status IN ({marks}) ORDER BY dest_rel",
            statuses,
        ).fetchall()
        counts = self.counts()
        files_total = sum(n for n, _ in counts.values())
        bytes_total = sum(b for _, b in counts.values())
        files_done = counts[DONE][0] + (0 if include_missing else counts[MISSING][0])
        bytes_done = counts[DONE][1] + (0 if include_missing else counts[MISSING][1])
        try:
            self._check_target()
        except TargetError as exc:
            log(str(exc))
            return "target_error"
        for file_id, cands, dest_rel, size, mtime_ms in todo:
            if should_stop():
                return "stopped"
            state = {"bytes": 0}

            def progress(n, _dest=dest_rel):
                state["bytes"] += n
                if on_progress:
                    on_progress(Progress(files_done, files_total, bytes_done + state["bytes"], bytes_total, _dest))

            try:
                self._copy_one(source, file_id, json.loads(cands), dest_rel, size, mtime_ms, should_stop, progress, log)
            except Stopped:
                return "stopped"
            except SourceLost as exc:
                log(f"Source disk stopped responding: {exc}")
                return "source_lost"
            except TargetError as exc:
                log(f"Target problem: {exc}")
                return "target_error"
            except sqlite3.Error as exc:  # the manifest lives on the target disk
                log(f"Target problem (manifest): {exc}")
                return "target_error"
            files_done += 1
            bytes_done += size
            if on_progress:
                on_progress(Progress(files_done, files_total, bytes_done, bytes_total, dest_rel))
        return "completed"

    def _check_target(self) -> None:
        if not os.path.isdir(self.target):
            raise TargetError(f"target folder is not available: {self.target}")

    def _copy_one(self, source: Source, file_id, candidates, dest_rel, size, mtime_ms, should_stop, progress, log):
        src_rel = None
        st = None
        for cand in candidates:
            try:
                st = source.stat(cand)
            except Exception:
                st = None
            if st is not None:
                src_rel = cand
                break
        if src_rel is None:
            if not source.probe():
                raise SourceLost("cannot read the source disk")
            self._mark(file_id, MISSING, "content not found on disk: " + " | ".join(candidates))
            log(f"MISSING  {dest_rel}")
            return

        real_size = st.size

        def read_source():
            it = source.iter_file(src_rel)
            while True:
                if should_stop():
                    raise Stopped()
                try:
                    chunk = next(it)
                except StopIteration:
                    return
                except Exception as exc:
                    raise _SourceReadError(exc) from exc
                yield chunk

        tmp = None
        try:
            # A file already at the destination counts only if its bytes match the source.
            names = _name_candidates(dest_rel)
            for name in names:
                lp = long_path(os.path.join(self.target, *name.split("/")))
                if not os.path.exists(lp):
                    break
                if os.path.isfile(lp) and os.path.getsize(lp) == real_size and _same_content(read_source(), lp):
                    self._mark(file_id, DONE, "identical file already present", src_rel, name,
                               copied_size=real_size)
                    progress(real_size)
                    return
            first_free = name
            lp = long_path(os.path.join(self.target, *first_free.split("/")))
            os.makedirs(os.path.dirname(lp), exist_ok=True)
            tmp = lp + "." + hashlib.sha1(file_id.encode()).hexdigest()[:10] + PARTIAL
            _remove(tmp)  # leftover of this job's own earlier attempt
            digest = hashlib.sha256()
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o644)
            with os.fdopen(fd, "wb") as out:
                for chunk in read_source():
                    out.write(chunk)
                    digest.update(chunk)
                    progress(len(chunk))
                out.flush()
                os.fsync(out.fileno())
            written = os.path.getsize(tmp)
            if written != real_size:
                raise _SourceReadError(f"size mismatch: expected {real_size}, got {written}")
            if mtime_ms:
                try:
                    ns = int(mtime_ms) * 1_000_000
                    os.utime(tmp, ns=(ns, ns))
                except OSError:
                    pass
            final = None
            for name in names[names.index(first_free):]:
                lp = long_path(os.path.join(self.target, *name.split("/")))
                try:
                    _install_no_replace(tmp, lp)
                except FileExistsError:
                    continue  # something appeared there meanwhile; try the next free name
                final = name
                break
            if final is None:
                raise OSError(errno.EEXIST, "no free destination name", dest_rel)
            tmp = None
            _sync_dir(os.path.dirname(lp))
        except Stopped:
            if tmp:
                _remove(tmp)
            raise
        except _SourceReadError as exc:
            if tmp:
                _remove(tmp)
            if not source.probe():
                raise SourceLost(str(exc.args[0] if exc.args else exc)) from exc
            self._mark(file_id, FAILED, f"read error: {exc}", src_rel)
            log(f"FAILED   {dest_rel}: {exc}")
            return
        except OSError as exc:
            if tmp:
                _remove(tmp)
            if exc.errno in (errno.ENOSPC, errno.EROFS, errno.ENODEV) or not os.path.isdir(self.target):
                free = _free_space(self.target)
                raise TargetError(f"{exc} (free space: {free})") from exc
            self._mark(file_id, FAILED, f"write error: {exc}", src_rel)
            log(f"FAILED   {dest_rel}: {exc}")
            return

        if final != dest_rel:
            log(f"EXISTS   {dest_rel} is a different file; saved as {final}")
        note = None if real_size == size else f"index.db size {size} != disk size {real_size}"
        self._mark(file_id, DONE, note, src_rel, final, copied_size=real_size, digest=digest.hexdigest())


def _name_candidates(dest_rel: str, limit: int = 1000) -> list[str]:
    stem, ext = os.path.splitext(dest_rel)
    out = [dest_rel]
    for i in range(1, limit):
        out.append(f"{stem} (recovered{'' if i == 1 else ' ' + str(i)}){ext}")
    return out


def _same_content(chunks, path: str) -> bool:
    with open(path, "rb") as f:
        for chunk in chunks:
            if f.read(len(chunk)) != chunk:
                return False
        return f.read(1) == b""


def _install_no_replace(tmp: str, dst: str) -> None:
    """Move tmp to dst, failing with FileExistsError instead of replacing anything."""
    if sys.platform == "win32":
        os.rename(tmp, dst)  # Windows rename never replaces an existing file
        return
    try:
        os.link(tmp, dst)  # atomic, fails if dst exists
    except FileExistsError:
        raise
    except OSError:  # filesystem without hard links (exFAT, FAT32, some NAS shares)
        if os.path.lexists(dst):
            raise FileExistsError(errno.EEXIST, "exists", dst)
        os.rename(tmp, dst)
        return
    os.unlink(tmp)


def _sync_dir(path: str) -> None:
    if sys.platform == "win32":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _free_space(path: str) -> str:
    try:
        return f"{shutil.disk_usage(path).free / 1e9:,.1f} GB"
    except OSError:
        return "unknown"


def plan_items(catalog, selected, keep_full_path: bool = True) -> list[PlanItem]:
    items = []
    for top, node in catalog.iter_files(selected):
        items.append(PlanItem(
            file_id=node.id,
            candidates=catalog.content_candidates(node),
            dest_rel=catalog.dest_rel(node, top, keep_full_path),
            size=node.size,
            mtime_ms=node.mtime_ms,
        ))
    return items
