"""Rebuild the original Home Duo folder tree from restsdk's index.db."""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field

from .naming import unique_names
from .sources import DB_PATH, Source

DIR_MIME = "application/x.wd.dir"
FILES_DIR = "restsdk/data/files"


@dataclass(eq=False)
class Node:
    id: str
    parent_id: str | None
    name: str
    is_dir: bool
    content_id: str = ""
    size: int = 0
    mtime_ms: int = 0
    storage_id: str = ""
    parent: "Node | None" = None
    children: list["Node"] = field(default_factory=list)
    total_size: int = 0
    file_count: int = 0
    safe_name: str = ""

    def path_nodes(self) -> list["Node"]:
        chain = []
        n: Node | None = self
        while n is not None:
            chain.append(n)
            n = n.parent
        chain.reverse()
        return chain

    def display_path(self) -> str:
        return "/".join(n.name for n in self.path_nodes())


class Catalog:
    def __init__(self, nodes: dict[str, Node], roots: list[Node], filesystems: dict[str, str], db_info: str):
        self.nodes = nodes
        self.roots = roots
        self.filesystems = filesystems
        self.db_info = db_info
        unique_names(self.roots)
        for n in nodes.values():
            if n.children:
                unique_names(n.children)

    # -- loading ----------------------------------------------------------
    @classmethod
    def load(cls, source: Source, include_trash: bool = True, progress=None) -> "Catalog":
        tmp = tempfile.mkdtemp(prefix="homeduo-db-")
        try:
            db_file = os.path.join(tmp, "index.db")
            if progress:
                progress("Reading index.db from the disk...")
            with open(db_file, "wb") as f:
                for chunk in source.iter_file(DB_PATH):
                    f.write(chunk)
            # A WAL file holds the most recent, not yet checkpointed changes.
            if source.stat(DB_PATH + "-wal"):
                with open(db_file + "-wal", "wb") as f:
                    for chunk in source.iter_file(DB_PATH + "-wal"):
                        f.write(chunk)
            if progress:
                progress("Building folder tree...")
            return cls.from_db_file(db_file, include_trash)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @classmethod
    def from_db_file(cls, db_file: str, include_trash: bool = True) -> "Catalog":
        con = sqlite3.connect(db_file)
        try:
            cols = {r[1].lower(): r[1] for r in con.execute("PRAGMA table_info(Files)")}
            if not cols:
                raise ValueError("index.db has no Files table")

            def col(name, default="NULL"):
                return cols.get(name.lower(), default)

            select = ", ".join([
                col("id"), col("parentID"), col("name"), col("mimeType", "''"), col("contentID"),
                col("size", "0"), col("mTime"), col("cTime"), col("birthTime"), col("storageID", "''"),
            ])
            rows = con.execute(f"SELECT {select} FROM Files").fetchall()
            filesystems = {}
            try:
                for fid, path in con.execute("SELECT id, path FROM Filesystems"):
                    filesystems[str(fid)] = path or ""
            except sqlite3.Error:
                pass
            version = ""
            try:
                version = con.execute("PRAGMA user_version").fetchone()[0]
            except sqlite3.Error:
                pass
        finally:
            con.close()

        nodes: dict[str, Node] = {}
        for fid, parent, name, mime, cid, size, mtime, ctime, btime, storage in rows:
            if fid is None:
                continue
            nodes[str(fid)] = Node(
                id=str(fid),
                parent_id=str(parent) if parent else None,
                name=name or "",
                is_dir=(mime or "") == DIR_MIME,
                content_id=cid or "",
                size=int(size or 0),
                mtime_ms=int(mtime or ctime or btime or 0),
                storage_id=storage or "",
            )

        roots: list[Node] = []
        for n in nodes.values():
            p = nodes.get(n.parent_id) if n.parent_id else None
            if p is not None and p is not n:
                n.parent = p
                p.children.append(n)
            else:
                roots.append(n)

        # Anything not reachable from a root sits in a parent cycle; surface it instead of losing it.
        reachable = set()
        stack = list(roots)
        while stack:
            n = stack.pop()
            reachable.add(n.id)
            stack.extend(n.children)
        lost = [n for n in nodes.values() if n.id not in reachable]
        if lost:
            holder = Node("__unlinked__", None, "Unlinked items", True)
            for n in lost:
                if n.parent is not None and n.parent.id not in reachable:
                    n.parent.children.remove(n)
                n.parent = holder
                holder.children.append(n)
            nodes[holder.id] = holder
            roots.append(holder)

        if not include_trash:
            roots = [r for r in roots if r.name != "Trash"]

        for n in nodes.values():
            n.children.sort(key=lambda c: (not c.is_dir, c.name.casefold(), c.id))
        roots.sort(key=lambda r: (r.name == "Trash", r.name.casefold()))
        _aggregate(roots)
        info = f"{len(rows):,} entries, schema version {version}"
        return cls(nodes, roots, filesystems, info)

    # -- content resolution -----------------------------------------------
    def content_candidates(self, node: Node) -> list[str]:
        """Relative paths on the volume where this file's bytes may be stored."""
        out: list[str] = []
        fs_path = self.filesystems.get(node.storage_id) if node.storage_id not in ("", "local") else None
        if fs_path:
            base = _volume_relative(fs_path)
            chain = node.path_nodes()
            top = len(chain) - 1
            while top > 0 and chain[top - 1].storage_id == node.storage_id:
                top -= 1
            below = [n.name for n in chain[top + 1:]]
            including = [n.name for n in chain[top:]]
            out.append("/".join([base] + below))
            out.append("/".join([base] + including))
        cid = node.content_id
        if cid:
            out.append(f"{FILES_DIR}/{cid[0]}/{cid}")
            out.append(f"{FILES_DIR}/{cid}")
        seen = set()
        return [p for p in out if not (p in seen or seen.add(p))]

    # -- selection --------------------------------------------------------
    def iter_files(self, selected: list[Node]):
        """Yield every file under the selected nodes exactly once."""
        seen: set[str] = set()
        for top in selected:
            stack = [top]
            while stack:
                n = stack.pop()
                if n.id in seen:
                    continue
                seen.add(n.id)
                if n.is_dir:
                    stack.extend(reversed(n.children))
                else:
                    yield top, n

    @staticmethod
    def dest_rel(node: Node, top: Node | None = None, keep_full_path: bool = True) -> str:
        chain = node.path_nodes()
        if not keep_full_path and top is not None:
            idx = chain.index(top)
            chain = chain[idx:]
        return "/".join(n.safe_name for n in chain)


def _volume_relative(path: str) -> str:
    p = path.replace("\\", "/")
    for marker in ("/userStorage/", "userStorage/"):
        i = p.find(marker)
        if i >= 0:
            return p[i:].strip("/")
    i = p.find("diskVolume0/")
    if i >= 0:
        return p[i + len("diskVolume0/"):].strip("/")
    return p.strip("/")


def _aggregate(roots: list[Node]) -> None:
    order: list[Node] = []
    stack = list(roots)
    while stack:
        n = stack.pop()
        order.append(n)
        stack.extend(n.children)
    for n in reversed(order):
        if n.is_dir:
            n.total_size = sum(c.total_size for c in n.children)
            n.file_count = sum(c.file_count for c in n.children)
        else:
            n.total_size = n.size
            n.file_count = 1
