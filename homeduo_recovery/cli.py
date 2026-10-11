"""Command-line interface (same engine as the desktop app).

  python -m homeduo_recovery disks
  python -m homeduo_recovery tree   SOURCE [--depth 2]
  python -m homeduo_recovery copy   SOURCE TARGET --path "auth0|.../Photos" [--path ...] [--relative]
  python -m homeduo_recovery resume SOURCE TARGET
  python -m homeduo_recovery status TARGET

SOURCE is a raw disk (\\\\.\\PhysicalDrive1, /dev/rdisk4, /dev/sde), a disk image file,
or a mounted folder containing 'restsdk'.
"""
from __future__ import annotations

import argparse
import sys
import time

from .catalog import Catalog
from .copier import DONE, FAILED, MISSING, PENDING, CopyJob, plan_items
from .disks import list_disks
from .power import KeepAwake
from .sources import open_source


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:,.1f} {unit}"
        n /= 1000
    return str(n)


def _counts(job: CopyJob) -> str:
    c = job.counts()
    return (f"{c[DONE][0]:,} done, {c[PENDING][0]:,} pending, {c[FAILED][0]:,} failed, "
            f"{c[MISSING][0]:,} missing")


def _find(catalog: Catalog, path: str):
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    level = catalog.roots
    node = None
    for part in parts:
        node = next((n for n in level if n.name == part or n.safe_name == part), None)
        if node is None:
            raise SystemExit(f"not found in Home Duo tree: {path!r} (at {part!r})")
        level = node.children
    if node is None:
        raise SystemExit("empty --path")
    return node


def _run(job: CopyJob, src) -> str:
    last = [0.0]

    def progress(p):
        now = time.monotonic()
        if now - last[0] > 1:
            last[0] = now
            print(f"\r{p.files_done:,}/{p.files_total:,} files  {_human(p.bytes_done)}/{_human(p.bytes_total)}   ",
                  end="", flush=True)

    try:
        with KeepAwake():
            result = job.run(src, on_progress=progress, on_log=lambda m: print("\n" + m))
    except KeyboardInterrupt:
        result = "stopped"
    print(f"\n{result}: {_counts(job)}")
    if job.counts()[FAILED][0] or job.counts()[MISSING][0]:
        print("problems listed in", job.write_report())
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="homeduo_recovery", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("disks")
    t = sub.add_parser("tree")
    t.add_argument("source")
    t.add_argument("--depth", type=int, default=2)
    c = sub.add_parser("copy")
    c.add_argument("source")
    c.add_argument("target")
    c.add_argument("--path", action="append", required=True, help="Home Duo folder/file to copy (repeatable)")
    c.add_argument("--relative", action="store_true", help="put selected folders directly under TARGET")
    c.add_argument("--force", action="store_true", help="allow a target that belongs to another source")
    r = sub.add_parser("resume")
    r.add_argument("source")
    r.add_argument("target")
    r.add_argument("--force", action="store_true", help="allow a target that belongs to another source")
    s = sub.add_parser("status")
    s.add_argument("target")
    a = ap.parse_args(argv)

    if a.cmd == "disks":
        for d in list_disks():
            print(d)
        return 0
    if a.cmd == "status":
        print(_counts(CopyJob(a.target)))
        return 0

    src = open_source(a.source)
    print("source:", src.label)

    def open_job():
        job = CopyJob(a.target)
        other = job.bind_source(src.fingerprint, force=a.force)
        if other:
            raise SystemExit(f"{a.target} belongs to a job from another source ({other}); "
                             "use --force only if it is the same Home Duo disk")
        return job

    if a.cmd == "resume":
        return 0 if _run(open_job(), src) == "completed" else 1

    cat = Catalog.load(src)
    if a.cmd == "tree":
        def show(nodes, depth, indent=""):
            for n in nodes:
                if n.is_dir:
                    print(f"{indent}{n.name}/  ({n.file_count:,} files, {_human(n.total_size)})")
                    if depth > 1:
                        show(n.children, depth - 1, indent + "    ")
        show(cat.roots, a.depth)
        return 0

    nodes = [_find(cat, p) for p in a.path]
    job = open_job()
    items = plan_items(cat, nodes, keep_full_path=not a.relative)
    print(f"planned {len(items):,} files ({job.add(items):,} new)")
    return 0 if _run(job, src) == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
