"""Turn Home Duo names into names that are valid on Windows, macOS and exFAT targets."""
from __future__ import annotations

import os
import unicodedata

_INVALID = set('<>:"/\\|?*') | {chr(i) for i in range(32)}
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
MAX_NAME_BYTES = 240  # leave room for the ".partial" suffix within the 255 limit


def sanitize(name: str) -> str:
    # Lone surrogates come from non-UTF-8 bytes in ext4 names; they cannot be written on Windows/macOS.
    name = name.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    out = "".join("-" if ch in _INVALID else ch for ch in name)
    out = out.rstrip(" .")
    if not out or out in (".", ".."):
        out = "_"
    if out.split(".", 1)[0].upper() in _RESERVED:
        out = "_" + out
    if len(out.encode("utf-8")) > MAX_NAME_BYTES:
        stem, ext = os.path.splitext(out)
        if len(ext.encode("utf-8")) > 20:
            stem, ext = out, ""
        budget = MAX_NAME_BYTES - len(ext.encode("utf-8"))
        stem = stem.encode("utf-8")[:budget].decode("utf-8", "ignore")
        out = stem + ext
    return out


def _key(name: str) -> str:
    # macOS (APFS/HFS+) and Windows treat case and Unicode normalisation variants as the same name.
    return unicodedata.normalize("NFD", name).casefold()


def unique_names(siblings) -> None:
    """Assign node.safe_name so siblings never collide (case- and normalisation-insensitively)."""
    used: set[str] = set()
    for node in sorted(siblings, key=lambda n: (_key(sanitize(n.name)), n.id)):
        base = sanitize(node.name)
        candidate = base
        i = 2
        while _key(candidate) in used:
            stem, ext = os.path.splitext(base) if not node.is_dir else (base, "")
            suffix = f" ({i})"
            room = MAX_NAME_BYTES - len((suffix + ext).encode("utf-8"))
            candidate = stem.encode("utf-8")[:room].decode("utf-8", "ignore") + suffix + ext
            i += 1
        used.add(_key(candidate))
        node.safe_name = candidate
