"""Minimal read-only ext2/3/4 filesystem reader (pure Python, no drivers)."""
from __future__ import annotations

import struct
from dataclasses import dataclass

EXT_MAGIC = 0xEF53
INCOMPAT_FILETYPE = 0x2
INCOMPAT_META_BG = 0x10
INCOMPAT_64BIT = 0x80
RO_COMPAT_SPARSE_SUPER = 0x1
COMPAT_SPARSE_SUPER2 = 0x200
EXTENTS_FL = 0x80000
INLINE_DATA_FL = 0x10000000
ENCRYPT_FL = 0x800
EXTENT_MAGIC = 0xF30A
XATTR_MAGIC = 0xEA020000

S_IFMT = 0xF000
S_IFDIR = 0x4000
S_IFREG = 0x8000


class Ext4Error(Exception):
    pass


@dataclass
class Inode:
    ino: int
    mode: int
    size: int
    mtime: int
    flags: int
    block: bytes
    raw: bytes

    @property
    def is_dir(self) -> bool:
        return self.mode & S_IFMT == S_IFDIR

    @property
    def is_file(self) -> bool:
        return self.mode & S_IFMT == S_IFREG


def probe_ext(dev) -> bool:
    sb = dev.read_cached(1024, 1024)
    return len(sb) >= 0x3A and struct.unpack_from("<H", sb, 0x38)[0] == EXT_MAGIC


class Ext4Volume:
    def __init__(self, dev):
        self.dev = dev
        sb = dev.read_cached(1024, 1024)
        if len(sb) < 1024 or struct.unpack_from("<H", sb, 0x38)[0] != EXT_MAGIC:
            raise Ext4Error("no ext2/3/4 filesystem found")
        u32 = lambda o: struct.unpack_from("<I", sb, o)[0]  # noqa: E731
        u16 = lambda o: struct.unpack_from("<H", sb, o)[0]  # noqa: E731
        self.block_size = 1024 << u32(0x18)
        self.incompat = u32(0x60)
        self.ro_compat = u32(0x64)
        self.compat = u32(0x5C)
        self.backup_bgs = {u32(0x24C), u32(0x250)} - {0}
        self.is64 = bool(self.incompat & INCOMPAT_64BIT)
        self.blocks_count = u32(0x4) | ((u32(0x150) << 32) if self.is64 else 0)
        self.first_data_block = u32(0x14)
        self.blocks_per_group = u32(0x20)
        self.inodes_per_group = u32(0x28)
        self.inode_size = u16(0x58) if u32(0x4C) >= 1 else 128
        self.desc_size = (u16(0xFE) or 64) if self.is64 else 32
        self.first_meta_bg = u32(0x104)
        self.uuid = sb[0x68:0x78].hex()
        self.label = sb[0x78:0x88].split(b"\0", 1)[0].decode("utf-8", "replace")
        self._dir_cache: dict[int, dict[str, tuple[int, int]]] = {}

    # -- layout -----------------------------------------------------------
    def _has_super(self, group: int) -> bool:
        if self.compat & COMPAT_SPARSE_SUPER2:
            return group == 0 or group in self.backup_bgs
        if not self.ro_compat & RO_COMPAT_SPARSE_SUPER or group in (0, 1):
            return True
        for p in (3, 5, 7):
            n = p
            while n < group:
                n *= p
            if n == group:
                return True
        return False

    def _gd_offset(self, group: int) -> int:
        bs = self.block_size
        per_block = bs // self.desc_size
        meta_group = group // per_block
        if self.incompat & INCOMPAT_META_BG and meta_group >= self.first_meta_bg:
            first = meta_group * per_block
            blk = self.first_data_block + first * self.blocks_per_group + (1 if self._has_super(first) else 0)
        else:
            blk = self.first_data_block + 1 + meta_group
        return blk * bs + (group % per_block) * self.desc_size

    def inode(self, ino: int) -> Inode:
        if ino < 1:
            raise Ext4Error(f"invalid inode {ino}")
        group, index = divmod(ino - 1, self.inodes_per_group)
        gd = self.dev.read_cached(self._gd_offset(group), self.desc_size)
        table = struct.unpack_from("<I", gd, 0x8)[0]
        if self.is64 and self.desc_size >= 64:
            table |= struct.unpack_from("<I", gd, 0x28)[0] << 32
        raw = self.dev.read_cached(table * self.block_size + index * self.inode_size, self.inode_size)
        mode, size_lo, = struct.unpack_from("<HxxI", raw, 0)
        mtime = struct.unpack_from("<I", raw, 0x10)[0]
        flags = struct.unpack_from("<I", raw, 0x20)[0]
        size_hi = struct.unpack_from("<I", raw, 0x6C)[0]
        return Inode(ino, mode, size_lo | (size_hi << 32), mtime, flags, raw[0x28:0x28 + 60], raw)

    # -- block mapping ----------------------------------------------------
    def _extent_runs(self, node: bytes, out: list, depth_left: int = 8) -> None:
        magic, entries, _max, depth = struct.unpack_from("<HHHH", node, 0)
        if magic != EXTENT_MAGIC:
            raise Ext4Error("corrupt extent header")
        for i in range(entries):
            off = 12 + 12 * i
            if depth == 0:
                lblk, length, hi, lo = struct.unpack_from("<IHHI", node, off)
                if length > 32768:  # uninitialized (preallocated) extent reads as zeros
                    out.append((lblk, None, length - 32768))
                else:
                    out.append((lblk, (hi << 32) | lo, length))
            else:
                if depth_left <= 0:
                    raise Ext4Error("extent tree too deep")
                _lblk, lo, hi = struct.unpack_from("<IIH", node, off)
                child = self.dev.read_cached(((hi << 32) | lo) * self.block_size, self.block_size)
                self._extent_runs(child, out, depth_left - 1)

    def _indirect_runs(self, inode: Inode) -> list:
        bs = self.block_size
        per = bs // 4
        total = -(-inode.size // bs)
        ptrs = struct.unpack_from("<15I", inode.block, 0)
        runs: list = []

        def emit(lblk, pblk):
            pblk = pblk or None
            if runs:
                l0, p0, n0 = runs[-1]
                if l0 + n0 == lblk and ((p0 is None and pblk is None) or (p0 is not None and pblk == p0 + n0)):
                    runs[-1] = (l0, p0, n0 + 1)
                    return
            runs.append((lblk, pblk, 1))

        def walk(blk, level, lstart):
            span = per ** level
            if lstart >= total:
                return
            if not blk:
                return  # hole; gaps are filled with zeros while reading
            table = struct.unpack_from(f"<{per}I", self.dev.read_cached(blk * bs, bs), 0)
            for i, p in enumerate(table):
                ls = lstart + i * (span // per)
                if ls >= total:
                    break
                if level == 1:
                    if p:
                        emit(ls, p)
                else:
                    walk(p, level - 1, ls)

        for i in range(12):
            if i < total and ptrs[i]:
                emit(i, ptrs[i])
        walk(ptrs[12], 1, 12)
        walk(ptrs[13], 2, 12 + per)
        walk(ptrs[14], 3, 12 + per + per * per)
        return runs

    def _inline_data(self, inode: Inode) -> bytes:
        data = inode.block
        if inode.size <= 60 or len(inode.raw) <= 128:
            return data
        extra = struct.unpack_from("<H", inode.raw, 0x80)[0]
        base = 128 + extra
        raw = inode.raw
        if base + 4 <= len(raw) and struct.unpack_from("<I", raw, base)[0] == XATTR_MAGIC:
            first = base + 4
            off = first
            while off + 16 <= len(raw):
                name_len, name_index, value_offs, _inum, value_size = struct.unpack_from("<BBHII", raw, off)
                if name_len == 0 and name_index == 0:
                    break
                name = raw[off + 16: off + 16 + name_len]
                if name_index == 7 and name == b"data":
                    return data + raw[first + value_offs: first + value_offs + value_size]
                off += (16 + name_len + 3) & ~3
        return data

    def runs(self, inode: Inode) -> list:
        if inode.flags & EXTENTS_FL:
            out: list = []
            self._extent_runs(inode.block, out)
            out.sort(key=lambda r: r[0])
            return out
        return self._indirect_runs(inode)

    # -- reading ----------------------------------------------------------
    def iter_file(self, inode: Inode, chunk: int = 8 * 1024 * 1024):
        """Yield the file content in chunks; holes and preallocated ranges read as zeros."""
        if inode.flags & ENCRYPT_FL:
            raise Ext4Error("file is encrypted (ext4 fscrypt)")
        size = inode.size
        if inode.flags & INLINE_DATA_FL:
            yield self._inline_data(inode)[:size]
            return
        bs = self.block_size
        pos = 0

        def zeros(n):
            while n > 0:
                k = min(n, chunk)
                yield bytes(k)
                n -= k

        for lblk, pblk, length in self.runs(inode):
            start = lblk * bs
            if start >= size:
                break
            if start > pos:
                yield from zeros(start - pos)
                pos = start
            if start < pos:  # overlapping/corrupt map: skip the overlap
                skip = pos - start
                if skip >= length * bs:
                    continue
            else:
                skip = 0
            nbytes = min(length * bs, size - start) - skip
            if pblk is None:
                yield from zeros(nbytes)
            else:
                base = pblk * bs + skip
                done = 0
                while done < nbytes:
                    k = min(chunk, nbytes - done)
                    data = self.dev.read(base + done, k)
                    if len(data) != k:
                        raise Ext4Error("short read from device")
                    yield data
                    done += k
            pos += nbytes
        if pos < size:
            yield from zeros(size - pos)

    def read_file(self, inode: Inode) -> bytes:
        return b"".join(self.iter_file(inode))

    # -- directories ------------------------------------------------------
    def listdir_ino(self, ino: int) -> dict[str, tuple[int, int]]:
        cached = self._dir_cache.get(ino)
        if cached is not None:
            return cached
        inode = self.inode(ino)
        if not inode.is_dir:
            raise Ext4Error(f"inode {ino} is not a directory")
        entries: dict[str, tuple[int, int]] = {}
        filetype = bool(self.incompat & INCOMPAT_FILETYPE)
        if inode.flags & INLINE_DATA_FL:
            data = self._inline_data(inode)
            self._parse_dirents(data[4:60], entries, filetype, len(data))
            if len(data) > 60:
                self._parse_dirents(data[60:], entries, filetype, len(data))
        else:
            data = self.read_file(inode)
            self._parse_dirents(data, entries, filetype, self.block_size)
        self._dir_cache[ino] = entries
        return entries

    @staticmethod
    def _parse_dirents(data: bytes, entries: dict, filetype: bool, block: int) -> None:
        off = 0
        n = len(data)
        while off + 8 <= n:
            ino, rec_len, name_len, ftype = struct.unpack_from("<IHBB", data, off)
            if not filetype:
                name_len |= ftype << 8
                ftype = 0
            if rec_len < 8 or off + rec_len > n or (rec_len < 8 + name_len):
                off = (off // block + 1) * block  # corrupt entry: skip to next block
                continue
            if ino and name_len:
                name = data[off + 8: off + 8 + name_len].decode("utf-8", "surrogateescape")
                if name not in (".", ".."):
                    entries[name] = (ino, ftype)
            off += rec_len

    def lookup(self, path: str) -> int | None:
        ino = 2
        for part in [p for p in path.replace("\\", "/").split("/") if p]:
            try:
                entry = self.listdir_ino(ino).get(part)
            except Ext4Error:
                return None
            if entry is None:
                return None
            ino = entry[0]
        return ino
