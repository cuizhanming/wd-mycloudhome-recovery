"""Build a synthetic My Cloud Home volume layout (index.db + content files) for tests.

Usage: python fixture_builder.py <root> [--posix]
--posix adds content that only a Linux filesystem can hold (names containing '|').
Writes <root>/../expected.json mapping display path -> sha256 (None for missing content).
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import sqlite3
import sys

DIR = "application/x.wd.dir"


def build(root: str, posix: bool = False, big_mb: int = 24) -> dict:
    rnd = random.Random(42)
    os.makedirs(os.path.join(root, "restsdk", "data", "db"), exist_ok=True)
    db = sqlite3.connect(os.path.join(root, "restsdk", "data", "db", "index.db"))
    db.executescript("""
        CREATE TABLE Files(id TEXT NOT NULL PRIMARY KEY, parentID TEXT, contentID TEXT, version INTEGER NOT NULL DEFAULT 1,
            name TEXT NOT NULL, birthTime INTEGER NOT NULL DEFAULT 0, cTime INTEGER NOT NULL DEFAULT 0, uTime INTEGER,
            mTime INTEGER, size INTEGER NOT NULL DEFAULT 0, mimeType TEXT NOT NULL DEFAULT '', storageID TEXT NOT NULL DEFAULT 'local',
            hidden INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE Filesystems(id TEXT NOT NULL PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL, cTime INTEGER NOT NULL DEFAULT 0, mTime INTEGER);
    """)
    expected: dict[str, str | None] = {}
    counter = [0]

    def new_id():
        counter[0] += 1
        return f"id{counter[0]:06d}"

    def cid():
        return "".join(rnd.choice("abcdefghijklmnopqrstuvwxyz234567") for _ in range(24))

    def add_dir(name, parent, storage="local"):
        i = new_id()
        db.execute("INSERT INTO Files(id,parentID,name,mimeType,storageID,cTime,mTime) VALUES(?,?,?,?,?,?,?)",
                   (i, parent, name, DIR, storage, 1583532462650, 1583532462650))
        return i

    def write_content(rel, data_writer):
        path = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            data_writer(f)
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return os.path.getsize(path), h.hexdigest()

    def add_file(name, parent, display, data=None, writer=None, storage="local", content=True, rel=None, mtime=1583532799861):
        i = new_id()
        c = cid()
        size, digest = 0, None
        if content:
            rel = rel or f"restsdk/data/files/{c[0]}/{c}"
            size, digest = write_content(rel, writer or (lambda f: f.write(data)))
        else:
            size = len(data or b"x" * 100)
        db.execute("INSERT INTO Files(id,parentID,contentID,name,mimeType,size,storageID,cTime,mTime) VALUES(?,?,?,?,?,?,?,?,?)",
                   (i, parent, c, name, "image/jpeg", size, storage, 1583530592274, mtime))
        expected[display] = digest
        return i

    user = "auth0|5e62c7d40ec9700d5c82bb89"
    u = add_dir(user, None)
    trash = add_dir("Trash", None)
    photos = add_dir("Photos", u)
    roll = add_dir("Camera Roll Backup", photos)
    for k in range(600):  # big directory -> htree-indexed on ext4
        add_file(f"IMG_{k:04d}.JPG", roll, f"{user}/Photos/Camera Roll Backup/IMG_{k:04d}.JPG",
                 data=rnd.randbytes(rnd.randint(0, 9000)))
    docs = add_dir("Docs: 2020?", u)
    add_file("report|final.txt", docs, f"{user}/Docs: 2020?/report|final.txt", data=b"pipe in name\n")
    add_file("CON.txt", docs, f"{user}/Docs: 2020?/CON.txt", data=b"reserved name\n")
    add_file("Photo.JPG", docs, f"{user}/Docs: 2020?/Photo.JPG", data=b"upper")
    add_file("photo.jpg", docs, f"{user}/Docs: 2020?/photo.jpg", data=b"lower case twin")
    add_file("中文 文件 é.txt", docs, f"{user}/Docs: 2020?/中文 文件 é.txt", data="unicode ✓".encode())
    add_file("empty.bin", docs, f"{user}/Docs: 2020?/empty.bin", data=b"")
    add_file("trailing dot.", docs, f"{user}/Docs: 2020?/trailing dot.", data=b"dot")
    add_file("lost.jpg", docs, f"{user}/Docs: 2020?/lost.jpg", content=False)
    add_dir("Empty Folder", u)
    deep = u
    for lvl in range(12):
        deep = add_dir(f"level {lvl} with a fairly long folder name to exceed MAX_PATH", deep)
    deep_path = "/".join(f"level {lvl} with a fairly long folder name to exceed MAX_PATH" for lvl in range(12))
    add_file("deep.txt", deep, f"{user}/{deep_path}/deep.txt", data=b"deep file")

    videos = add_dir("Videos", u)

    def big_writer(f):
        r = random.Random(7)
        for _ in range(big_mb):
            f.write(r.randbytes(1 << 20))

    add_file("big.mov", videos, f"{user}/Videos/big.mov", writer=big_writer)

    def sparse_writer(f):
        r = random.Random(9)
        for k in range(40):  # data every other 4 KiB block -> many extents / holes
            f.seek(k * 8192)
            f.write(r.randbytes(4096))
        f.truncate(40 * 8192 + 5000)

    add_file("sparse.bin", videos, f"{user}/Videos/sparse.bin", writer=sparse_writer)
    add_file("deleted.jpg", trash, "Trash/deleted.jpg", data=b"in the trash")

    if posix:
        fs_path = f"/data/wd/diskVolume0/userStorage/{user}"
        db.execute("INSERT INTO Filesystems(id,name,path) VALUES(?,?,?)", ("fs1", user, fs_path))
        cam = add_dir("Pixel Camera Backup", u, storage="fs1")
        add_file("PXL_0001.jpg", cam, f"{user}/Pixel Camera Backup/PXL_0001.jpg", data=b"userStorage layout",
                 storage="fs1", rel=f"userStorage/{user}/Pixel Camera Backup/PXL_0001.jpg")

    db.commit()
    db.close()
    with open(os.path.join(os.path.dirname(os.path.abspath(root)), "expected.json"), "w", encoding="utf-8") as f:
        json.dump(expected, f, ensure_ascii=False, indent=1)
    return expected


if __name__ == "__main__":
    build(sys.argv[1], posix="--posix" in sys.argv)
