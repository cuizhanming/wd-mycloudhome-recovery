import hashlib
import json
import os

import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from homeduo_recovery.catalog import Catalog  # noqa: E402
from homeduo_recovery.copier import DONE, MISSING, CopyJob, long_path, plan_items  # noqa: E402
from homeduo_recovery.naming import sanitize  # noqa: E402
from homeduo_recovery.sources import DiskSource, FolderSource, Source  # noqa: E402

DATA = os.path.join(os.path.dirname(__file__), "data")
IMAGES = ["homeduo_raid1_ext4", "plain_ext3_1k"]


def sha256(path):
    h = hashlib.sha256()
    with open(long_path(path), "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def image(name):
    path = os.path.join(DATA, name + ".img")
    if not os.path.exists(path):
        pytest.skip("test images missing; run tests/make_test_images.sh in WSL/Linux")
    with open(os.path.join(DATA, name + ".expected.json"), encoding="utf-8") as f:
        return path, json.load(f)


def verify(catalog, target, expected):
    by_display = {}
    for _top, node in catalog.iter_files(catalog.roots):
        by_display[node.display_path()] = node
    assert set(by_display) == set(expected)
    for display, digest in expected.items():
        dest = os.path.join(target, *catalog.dest_rel(by_display[display]).split("/"))
        if digest is None:
            assert not os.path.exists(long_path(dest)), display
        else:
            assert sha256(dest) == digest, display
    leftovers = [f for _r, _d, fs in os.walk(long_path(target)) for f in fs if f.endswith(".partial")]
    assert not leftovers


@pytest.mark.parametrize("name", IMAGES)
def test_full_copy_from_disk_image(tmp_path, name):
    path, expected = image(name)
    src = DiskSource(path)
    cat = Catalog.load(src)
    job = CopyJob(str(tmp_path))
    job.add(plan_items(cat, cat.roots))
    assert job.run(src) == "completed"
    counts = job.counts()
    assert counts[MISSING][0] == 1  # lost.jpg has no content on disk
    assert counts[DONE][0] == len(expected) - 1
    verify(cat, str(tmp_path), expected)
    src.close()


class FlakySource(Source):
    """Wraps a source and simulates the disk disappearing mid-copy."""

    def __init__(self, inner, fail_after_bytes):
        self.inner = inner
        self.fail_after = fail_after_bytes
        self.read_bytes_total = 0
        self.connected = True
        self.opened = []

    def stat(self, rel):
        if not self.connected:
            raise OSError("device not ready")
        return self.inner.stat(rel)

    def iter_file(self, rel, chunk=1 << 20):
        self.opened.append(rel)
        for data in self.inner.iter_file(rel, chunk):
            if not self.connected:
                raise OSError("device not ready")
            self.read_bytes_total += len(data)
            if self.fail_after is not None and self.read_bytes_total > self.fail_after:
                self.connected = False
                raise OSError(5, "I/O error")
            yield data

    def probe(self):
        return self.connected


def test_disconnect_then_retry_resumes(tmp_path):
    path, expected = image("homeduo_raid1_ext4")
    inner = DiskSource(path)
    cat = Catalog.load(inner)
    job = CopyJob(str(tmp_path))
    job.add(plan_items(cat, cat.roots))

    flaky = FlakySource(inner, fail_after_bytes=6 * 1024 * 1024)
    assert job.run(flaky) == "source_lost"
    done_before = job.counts()[DONE][0]
    assert 0 < done_before < len(expected) - 1
    assert not [f for _r, _d, fs in os.walk(tmp_path) for f in fs if f.endswith(".partial")]

    # "Reconnect" and retry: only the remaining files are read again.
    flaky.connected, flaky.fail_after, flaky.opened = True, None, []
    assert job.run(flaky) == "completed"
    assert len(flaky.opened) == len(expected) - 1 - done_before
    verify(cat, str(tmp_path), expected)


def test_stop_and_restart_from_manifest(tmp_path):
    path, expected = image("homeduo_raid1_ext4")
    src = DiskSource(path)
    cat = Catalog.load(src)
    job = CopyJob(str(tmp_path))
    job.add(plan_items(cat, cat.roots))
    calls = {"n": 0}

    def stop_soon():
        calls["n"] += 1
        return calls["n"] > 200

    assert job.run(src, should_stop=stop_soon) == "stopped"
    job.close()

    # A new app session re-opens the same target and continues.
    job2 = CopyJob(str(tmp_path))
    assert job2.add(plan_items(cat, cat.roots)) == 0  # nothing new, statuses kept
    assert job2.run(src) == "completed"
    verify(cat, str(tmp_path), expected)


def test_existing_different_file_is_not_overwritten(tmp_path):
    path, _expected = image("homeduo_raid1_ext4")
    src = DiskSource(path)
    cat = Catalog.load(src)
    docs = next(c for c in cat.roots[0].children if c.name.startswith("Docs"))
    target_file = os.path.join(tmp_path, *cat.dest_rel(next(c for c in docs.children if c.name == "CON.txt")).split("/"))
    os.makedirs(os.path.dirname(target_file))
    impostor = b"SAME LENGTH!!\n"  # same size as the real content, different bytes
    assert len(impostor) == len(b"reserved name\n")
    with open(target_file, "wb") as f:
        f.write(impostor)
    job = CopyJob(str(tmp_path))
    job.add(plan_items(cat, [docs]))
    assert job.run(src) == "completed"
    with open(target_file, "rb") as f:
        assert f.read() == impostor
    stem, ext = os.path.splitext(target_file)
    with open(f"{stem} (recovered){ext}", "rb") as f:
        assert f.read() == b"reserved name\n"


def test_identical_existing_file_is_reused(tmp_path):
    path, _ = image("homeduo_raid1_ext4")
    src = DiskSource(path)
    cat = Catalog.load(src)
    docs = next(c for c in cat.roots[0].children if c.name.startswith("Docs"))
    con = next(c for c in docs.children if c.name == "CON.txt")
    target_file = os.path.join(tmp_path, *cat.dest_rel(con).split("/"))
    os.makedirs(os.path.dirname(target_file))
    with open(target_file, "wb") as f:
        f.write(b"reserved name\n")
    job = CopyJob(str(tmp_path))
    job.add(plan_items(cat, [con]))
    assert job.run(src) == "completed"
    assert job.counts()[DONE][0] == 1
    assert sorted(os.listdir(os.path.dirname(target_file))) == ["_CON.txt"]


def test_resume_recopies_deleted_or_changed_outputs(tmp_path):
    path, expected = image("homeduo_raid1_ext4")
    src = DiskSource(path)
    cat = Catalog.load(src)
    videos = next(c for c in cat.roots[0].children if c.name == "Videos")
    job = CopyJob(str(tmp_path))
    job.add(plan_items(cat, [videos]))
    assert job.run(src) == "completed"
    big = os.path.join(tmp_path, *cat.dest_rel(videos.children[0]).split("/"))
    sparse = os.path.join(tmp_path, *cat.dest_rel(videos.children[1]).split("/"))
    os.remove(big)
    with open(sparse, "r+b") as f:
        f.truncate(10)
    assert job.run(src) == "completed"
    assert sha256(big) == expected[videos.children[0].display_path()]
    # A damaged output is kept as-is; the good copy is saved next to it.
    stem, ext = os.path.splitext(sparse)
    assert os.path.getsize(sparse) == 10
    assert sha256(f"{stem} (recovered){ext}") == expected[videos.children[1].display_path()]


def test_job_is_bound_to_its_source(tmp_path):
    job = CopyJob(str(tmp_path))
    assert job.bind_source("ext4:aaa") is None
    assert job.bind_source("ext4:aaa") is None
    assert job.bind_source("ext4:bbb") == "ext4:aaa"
    assert job.bind_source("ext4:bbb", force=True) is None
    assert job.get_meta("source") == "ext4:aaa"


def test_unicode_normalisation_collisions():
    from types import SimpleNamespace

    from homeduo_recovery.naming import unique_names

    nfc, nfd = "caf\u00e9.txt", "cafe\u0301.txt"
    nodes = [SimpleNamespace(id="1", name=nfc, is_dir=False, safe_name=""),
             SimpleNamespace(id="2", name=nfd, is_dir=False, safe_name="")]
    unique_names(nodes)
    assert nodes[0].safe_name == nfc and nodes[1].safe_name.endswith(" (2).txt")
    long_nodes = [SimpleNamespace(id=str(i), name="x" * 300 + ".jpg", is_dir=False, safe_name="") for i in range(3)]
    unique_names(long_nodes)
    assert len({n.safe_name for n in long_nodes}) == 3
    assert all(len(n.safe_name.encode()) <= 240 and n.safe_name.endswith(".jpg") for n in long_nodes)


def test_folder_source(tmp_path):
    import fixture_builder

    src_root = tmp_path / "volume" / "root"
    expected = fixture_builder.build(str(src_root), posix=False, big_mb=2)
    src = FolderSource(str(src_root))
    cat = Catalog.load(src)
    target = tmp_path / "out"
    target.mkdir()
    job = CopyJob(str(target))
    job.add(plan_items(cat, cat.roots))
    assert job.run(src) == "completed"
    verify(cat, str(target), expected)
    # Picking a folder inside the volume (as users do) still finds the volume root.
    inner = FolderSource(str(src_root / "restsdk" / "data"))
    assert inner.root == str(src_root)


def test_selection_relative_paths(tmp_path):
    path, _ = image("homeduo_raid1_ext4")
    src = DiskSource(path)
    cat = Catalog.load(src)
    videos = next(c for c in cat.roots[0].children if c.name == "Videos")
    items = plan_items(cat, [videos], keep_full_path=False)
    assert sorted(i.dest_rel for i in items) == ["Videos/big.mov", "Videos/sparse.bin"]


def test_wsl_unc_paths_map_back_to_linux():
    from homeduo_recovery.wsl_source import parse_wsl_path

    assert parse_wsl_path(r"\\wsl.localhost\Ubuntu\mnt\ssd\userStorage\auth0" + "\uf07c63ea") == (
        "Ubuntu", "/mnt/ssd/userStorage/auth0|63ea")
    assert parse_wsl_path("\\\\wsl$\\Debian\\") == ("Debian", "/")
    assert parse_wsl_path(r"C:\Users\me") is None


def test_sanitize_and_collisions():
    assert sanitize("report|final.txt") == "report-final.txt"
    assert sanitize("CON.txt") == "_CON.txt"
    assert sanitize("trailing dot.") == "trailing dot"
    assert sanitize("a:b?") == "a-b-"
    assert len(sanitize("x" * 300 + ".jpg").encode()) <= 240
    assert sanitize("x" * 300 + ".jpg").endswith(".jpg")
    _path, _ = image("homeduo_raid1_ext4")
    cat = Catalog.load(DiskSource(_path))
    docs = next(c for c in cat.roots[0].children if c.name.startswith("Docs"))
    names = sorted(c.safe_name for c in docs.children)
    assert "Photo.JPG" in names and "photo (2).jpg" in names
