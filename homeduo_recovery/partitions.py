"""Partition-table (GPT/MBR) and Linux md-RAID superblock parsing."""
from __future__ import annotations

import struct
import uuid
from dataclasses import dataclass

GPT_SIG = b"EFI PART"
MD_MAGIC = 0xA92B4EFC
LINUX_RAID_GUID = "a19d880f-05fc-4d3b-a006-743f0f84911e"


@dataclass
class Partition:
    index: int
    offset: int  # bytes
    size: int  # bytes
    type_guid: str = ""
    name: str = ""


@dataclass
class RaidInfo:
    version: str
    level: int
    raid_disks: int
    data_offset: int  # bytes, relative to partition start
    data_size: int | None  # bytes
    name: str = ""


def find_partitions(dev) -> list[Partition]:
    """Return partitions from a GPT or MBR table; whole device if neither exists."""
    for sector in (512, 4096):
        hdr = dev.read_cached(sector, 92)
        if hdr[:8] != GPT_SIG:
            continue
        entries_lba, count, entry_size = struct.unpack_from("<QII", hdr, 72)
        table = dev.read_cached(entries_lba * sector, count * entry_size)
        parts = []
        for i in range(count):
            e = table[i * entry_size:(i + 1) * entry_size]
            if len(e) < 128 or e[:16] == b"\0" * 16:
                continue
            first, last = struct.unpack_from("<QQ", e, 32)
            name = e[56:128].decode("utf-16-le", "ignore").rstrip("\0")
            parts.append(Partition(
                index=i + 1,
                offset=first * sector,
                size=(last - first + 1) * sector,
                type_guid=str(uuid.UUID(bytes_le=bytes(e[:16]))),
                name=name,
            ))
        return parts

    mbr = dev.read_cached(0, 512)
    if mbr[510:512] == b"\x55\xaa":
        parts = []
        for i in range(4):
            ent = mbr[446 + i * 16: 446 + (i + 1) * 16]
            ptype = ent[4]
            start, count = struct.unpack_from("<II", ent, 8)
            if ptype and ptype != 0xEE and count:
                parts.append(Partition(i + 1, start * 512, count * 512, f"mbr:{ptype:02x}"))
        if parts:
            return parts
    return [Partition(0, 0, 0, "", "whole device")]


def read_md_superblock(dev, part_offset: int, part_size: int) -> RaidInfo | None:
    """Detect a Linux md superblock (v1.2, v1.1, v1.0 or v0.90) in a partition."""
    candidates = [("1.2", part_offset + 4096), ("1.1", part_offset)]
    if part_size:
        sectors = part_size // 512
        candidates.append(("1.0", part_offset + ((sectors - 16) & ~7) * 512))
        candidates.append(("0.90", part_offset + ((part_size & ~(65536 - 1)) - 65536)))

    for version, off in candidates:
        if off < 0:
            continue
        sb = dev.read_cached(off, 256)
        if len(sb) < 256 or struct.unpack_from("<I", sb, 0)[0] != MD_MAGIC:
            continue
        major = struct.unpack_from("<I", sb, 4)[0]
        if version == "0.90":
            if major != 0:
                continue
            level = struct.unpack_from("<i", sb, 7 * 4)[0]
            raid_disks = struct.unpack_from("<I", sb, 10 * 4)[0]
            return RaidInfo("0.90", level, raid_disks, 0, part_size and (part_size & ~(65536 - 1)) - 65536)
        if major != 1:
            continue
        name = sb[32:64].split(b"\0", 1)[0].decode("utf-8", "replace")
        level = struct.unpack_from("<i", sb, 72)[0]
        raid_disks = struct.unpack_from("<I", sb, 92)[0]
        data_offset, data_size = struct.unpack_from("<QQ", sb, 128)
        return RaidInfo(version, level, raid_disks, data_offset * 512, data_size * 512, name)
    return None
