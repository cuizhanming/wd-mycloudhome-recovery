"""Desktop UI (PySide6) for browsing a Home Duo disk and copying folders off it."""
from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from datetime import datetime

from PySide6.QtCore import QAbstractItemModel, QModelIndex, QObject, Qt, QThread, Signal
from PySide6.QtGui import QAction, QFont
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSplitter, QStyle,
    QTreeView, QVBoxLayout, QWidget,
)

from . import __version__
from .catalog import Catalog, Node
from .copier import DONE, FAILED, MISSING, PENDING, CopyJob, has_manifest, plan_items
from .disks import DiskInfo, is_admin, list_disks, relaunch_as_admin, usb_disks_held_by_wsl
from .power import KeepAwake
from .protect import WARNING
from .sources import DiskSource, Source, SourceLost, open_source


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1000
    return str(n)


# ---------------------------------------------------------------- tree model
class CatalogModel(QAbstractItemModel):
    HEADERS = ("Name", "Size", "Files", "Modified")
    checksChanged = Signal()

    def __init__(self, catalog: Catalog, style):
        super().__init__()
        self.catalog = catalog
        self.checked: set[str] = set()
        self.partial: set[str] = set()
        self._row: dict[str, int] = {}
        for i, r in enumerate(catalog.roots):
            self._row[r.id] = i
        for n in catalog.nodes.values():
            for i, c in enumerate(n.children):
                self._row[c.id] = i
        self._dir_icon = style.standardIcon(QStyle.SP_DirIcon)
        self._file_icon = style.standardIcon(QStyle.SP_FileIcon)

    # structure
    def _children(self, parent: QModelIndex) -> list[Node]:
        return parent.internalPointer().children if parent.isValid() else self.catalog.roots

    def index(self, row, column, parent=QModelIndex()):
        kids = self._children(parent)
        if 0 <= row < len(kids):
            return self.createIndex(row, column, kids[row])
        return QModelIndex()

    def parent(self, index):
        if not index.isValid():
            return QModelIndex()
        p = index.internalPointer().parent
        if p is None:
            return QModelIndex()
        return self.createIndex(self._row[p.id], 0, p)

    def rowCount(self, parent=QModelIndex()):
        if parent.column() > 0:
            return 0
        return len(self._children(parent))

    def columnCount(self, parent=QModelIndex()):
        return len(self.HEADERS)

    def hasChildren(self, parent=QModelIndex()):
        return bool(self._children(parent))

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self.HEADERS[section]
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.NoItemFlags
        f = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if index.column() == 0:
            f |= Qt.ItemIsUserCheckable
        return f

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        n: Node = index.internalPointer()
        col = index.column()
        if role == Qt.DisplayRole:
            if col == 0:
                return n.name or "(no name)"
            if col == 1:
                return human(n.total_size)
            if col == 2:
                return f"{n.file_count:,}" if n.is_dir else ""
            if col == 3 and n.mtime_ms:
                return datetime.fromtimestamp(n.mtime_ms / 1000).strftime("%Y-%m-%d %H:%M")
        elif role == Qt.DecorationRole and col == 0:
            return self._dir_icon if n.is_dir else self._file_icon
        elif role == Qt.CheckStateRole and col == 0:
            return self.check_state(n)
        elif role == Qt.TextAlignmentRole and col in (1, 2):
            return int(Qt.AlignRight | Qt.AlignVCenter)
        elif role == Qt.ToolTipRole:
            return n.display_path()
        return None

    # check handling: `checked` holds the top-most fully selected nodes
    def _covered(self, n: Node) -> bool:
        while n is not None:
            if n.id in self.checked:
                return True
            n = n.parent
        return False

    def check_state(self, n: Node):
        if self._covered(n):
            return Qt.Checked
        if n.id in self.partial:
            return Qt.PartiallyChecked
        return Qt.Unchecked

    def setData(self, index, value, role=Qt.EditRole):
        if role != Qt.CheckStateRole or not index.isValid():
            return False
        n: Node = index.internalPointer()
        if self._covered(n):
            self._uncheck(n)
        else:
            self._check(n)
        self._recompute_partial()
        self.checksChanged.emit()
        return True

    def _check(self, n: Node):
        for cid in list(self.checked):
            c = self.catalog.nodes.get(cid)
            p = c.parent if c else None
            while p is not None:
                if p is n:
                    self.checked.discard(cid)
                    break
                p = p.parent
        self.checked.add(n.id)
        # collapse upwards when every sibling is selected
        p = n.parent
        while p is not None and all(c.id in self.checked for c in p.children):
            for c in p.children:
                self.checked.discard(c.id)
            self.checked.add(p.id)
            p = p.parent

    def _uncheck(self, n: Node):
        if n.id in self.checked:
            self.checked.discard(n.id)
            return
        path = []
        a = n
        while a is not None and a.id not in self.checked:
            path.append(a)
            a = a.parent
        if a is None:
            return
        self.checked.discard(a.id)
        path.reverse()  # from just below a down to n
        cur = a
        for step in path:
            for c in cur.children:
                if c is not step:
                    self.checked.add(c.id)
            cur = step

    def _recompute_partial(self):
        self.partial = set()
        for cid in self.checked:
            p = self.catalog.nodes[cid].parent
            while p is not None and p.id not in self.partial:
                self.partial.add(p.id)
                p = p.parent

    def set_all(self, on: bool):
        self.checked = {r.id for r in self.catalog.roots} if on else set()
        self._recompute_partial()
        self.checksChanged.emit()

    def selected_nodes(self) -> list[Node]:
        return [self.catalog.nodes[i] for i in sorted(self.checked)]


# ---------------------------------------------------------------- workers
class Worker(QObject):
    log = Signal(str)
    progress = Signal(object)
    finished = Signal(str, object)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self):
        try:
            result = self.fn(self)
            self.finished.emit("ok", result)
        except Exception as exc:  # surfaced to the UI
            self.finished.emit("error", exc)


# ---------------------------------------------------------------- main window
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Home Duo Recovery {__version__}  (read-only)")
        self.resize(1100, 760)
        self.source: Source | None = None
        self.catalog: Catalog | None = None
        self.model: CatalogModel | None = None
        self.job: CopyJob | None = None
        self.stop_flag = threading.Event()
        self.thread: QThread | None = None
        self.worker: Worker | None = None
        self.last_result = ""
        self._on_done = None
        self._on_worker_progress = None
        self._build_ui()
        self.refresh_disks()

    # UI construction
    def _build_ui(self):
        w = QWidget()
        root = QVBoxLayout(w)

        src_row = QHBoxLayout()
        src_row.addWidget(QLabel("Source disk:"))
        self.disk_combo = QComboBox()
        self.disk_combo.setMinimumWidth(420)
        src_row.addWidget(self.disk_combo, 1)
        b = QPushButton("Refresh")
        b.clicked.connect(self.refresh_disks)
        src_row.addWidget(b)
        self.open_disk_btn = QPushButton("Open disk")
        self.open_disk_btn.clicked.connect(self.open_selected_disk)
        src_row.addWidget(self.open_disk_btn)
        b = QPushButton("Open image file...")
        b.clicked.connect(self.open_image)
        src_row.addWidget(b)
        b = QPushButton("Open mounted folder...")
        b.clicked.connect(self.open_folder)
        src_row.addWidget(b)
        root.addLayout(src_row)

        self.source_label = QLabel(
            "No source opened. The source is only ever read, never written.  If Windows or macOS asks to "
            "initialise, format or erase the Home Duo disk: always Cancel / Ignore.")
        self.source_label.setWordWrap(True)
        root.addWidget(self.source_label)

        split = QSplitter(Qt.Vertical)
        tree_box = QWidget()
        tl = QVBoxLayout(tree_box)
        tl.setContentsMargins(0, 0, 0, 0)
        sel_row = QHBoxLayout()
        self.trash_cb = QCheckBox("Show Trash")
        self.trash_cb.setChecked(True)
        self.trash_cb.toggled.connect(self.reload_catalog)
        sel_row.addWidget(self.trash_cb)
        b = QPushButton("Select all")
        b.clicked.connect(lambda: self.model and self.model.set_all(True))
        sel_row.addWidget(b)
        b = QPushButton("Clear selection")
        b.clicked.connect(lambda: self.model and self.model.set_all(False))
        sel_row.addWidget(b)
        sel_row.addStretch(1)
        self.sel_label = QLabel("Selected: nothing")
        sel_row.addWidget(self.sel_label)
        tl.addLayout(sel_row)
        self.tree = QTreeView()
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        tl.addWidget(self.tree)
        split.addWidget(tree_box)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        self.log_view.setFont(QFont("Consolas" if sys.platform == "win32" else "Menlo", 9))
        split.addWidget(self.log_view)
        split.setSizes([520, 160])
        root.addWidget(split, 1)

        tgt_row = QHBoxLayout()
        tgt_row.addWidget(QLabel("Copy to:"))
        self.target_edit = QLineEdit()
        self.target_edit.setPlaceholderText("Folder on the destination disk")
        self.target_edit.editingFinished.connect(self.target_changed)
        self.target_edit.textChanged.connect(lambda _t: self._update_buttons())
        tgt_row.addWidget(self.target_edit, 1)
        b = QPushButton("Browse...")
        b.clicked.connect(self.browse_target)
        tgt_row.addWidget(b)
        self.fullpath_cb = QCheckBox("Keep full Home Duo path")
        self.fullpath_cb.setChecked(True)
        self.fullpath_cb.setToolTip("On: target/<user>/<folders>/file.  Off: target/<selected folder>/file")
        tgt_row.addWidget(self.fullpath_cb)
        root.addLayout(tgt_row)

        act_row = QHBoxLayout()
        self.job_label = QLabel("")
        act_row.addWidget(self.job_label, 1)
        self.start_btn = QPushButton("Copy selected")
        self.start_btn.clicked.connect(self.start_copy)
        act_row.addWidget(self.start_btn)
        self.resume_btn = QPushButton("Retry / Resume remaining")
        self.resume_btn.clicked.connect(lambda: self.resume_copy())
        act_row.addWidget(self.resume_btn)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self.stop_copy)
        act_row.addWidget(self.stop_btn)
        root.addLayout(act_row)

        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        root.addWidget(self.progress)
        self.status_label = QLabel("")
        root.addWidget(self.status_label)
        self.setCentralWidget(w)

        m = self.menuBar().addMenu("&Help")
        a = QAction("About", self)
        a.triggered.connect(lambda: QMessageBox.information(self, "About", ABOUT))
        m.addAction(a)
        self._update_buttons()

    # helpers
    def append_log(self, msg: str):
        self.log_view.appendPlainText(f"{datetime.now():%H:%M:%S}  {msg}")

    def busy(self) -> bool:
        return self.thread is not None

    def _update_buttons(self):
        busy = self.busy()
        has_sel = bool(self.model and self.model.checked)
        target = self.target_edit.text().strip()
        self.start_btn.setEnabled(not busy and self.source is not None and has_sel and bool(target))
        self.resume_btn.setEnabled(not busy and self.source is not None and bool(target) and has_manifest(target))
        self.stop_btn.setEnabled(busy)
        self.open_disk_btn.setEnabled(not busy)

    def run_in_thread(self, fn, on_done, on_progress=None):
        self.thread = QThread()
        self.worker = Worker(fn)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        # Bound methods of this (main-thread) window => queued, executed on the UI thread.
        self.worker.log.connect(self.append_log)
        self.worker.progress.connect(self._worker_progress)
        self.worker.finished.connect(self._worker_finished)
        self._on_done = on_done
        self._on_worker_progress = on_progress
        self.thread.start()
        self._update_buttons()

    def _worker_progress(self, p):
        if self._on_worker_progress:
            self._on_worker_progress(p)

    def _worker_finished(self, kind, result):
        self.thread.quit()
        self.thread.wait()
        self.thread = None
        self.worker = None
        cb, self._on_done = self._on_done, None
        if cb:
            cb(kind, result)
        self._update_buttons()

    # source handling
    def refresh_disks(self):
        self.disk_combo.clear()
        for d in list_disks():
            self.disk_combo.addItem(str(d), d)
        for busid in usb_disks_held_by_wsl():
            self.disk_combo.addItem(f"USB disk {busid} is attached to WSL (usbipd) - not visible to Windows", None)
            self.append_log(
                f"A USB disk (bus {busid}) is passed through to WSL, so Windows cannot see it. To use it here, finish "
                f"any copy in WSL, unmount it there, then run as admin: usbipd detach --busid {busid} && "
                f"usbipd unbind --busid {busid}, and click Refresh. (Or use 'Open mounted folder...' with "
                r"\\wsl$\Ubuntu\mnt\ssd while it stays in WSL.)")
        if self.disk_combo.count() == 0:
            self.disk_combo.addItem("(no disks found - use 'Open image file' or 'Open mounted folder')", None)
        else:  # preselect the first non-system disk
            for i in range(self.disk_combo.count()):
                d = self.disk_combo.itemData(i)
                if d and not d.is_system:
                    self.disk_combo.setCurrentIndex(i)
                    break

    def open_selected_disk(self):
        d: DiskInfo | None = self.disk_combo.currentData()
        if d is None:
            return
        if d.is_system and QMessageBox.question(self, "System disk", "This is your system disk. Open it anyway?") != QMessageBox.Yes:
            return
        if sys.platform == "win32" and not is_admin():
            r = QMessageBox.question(
                self, "Administrator rights needed",
                "Reading a physical disk directly requires administrator rights.\n\nRestart the app as administrator?")
            if r == QMessageBox.Yes and relaunch_as_admin():
                QApplication.quit()
            return
        self._open_source(lambda: DiskSource(d.path))

    def open_image(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open disk image", "", "Disk images (*.img *.dd *.raw *.bin *.iso);;All files (*)")
        if path:
            self._open_source(lambda: DiskSource(path))

    def open_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Folder that contains 'restsdk' (e.g. \\\\wsl$\\Ubuntu\\mnt\\ssd)")
        if path:
            self._open_source(lambda: open_source(os.path.normpath(path)))

    def _open_source(self, factory):
        if self.busy():
            return
        if self.source is not None:
            self.source.close()
            self.source = None
        self.tree.setModel(None)
        self.model = None
        self.source_label.setText("Opening...")
        include_trash = self.trash_cb.isChecked()

        def work(worker: Worker):
            src = factory()
            worker.log.emit(f"Opened {src.label}")
            cat = Catalog.load(src, include_trash, progress=worker.log.emit)
            return src, cat

        def done(kind, result):
            if kind == "error":
                self.source_label.setText(f"Could not open source: {result}")
                self.append_log(f"ERROR: {result}")
                return
            self.source, cat = result
            self._set_catalog(cat)
            if isinstance(self.source, DiskSource) and not os.path.isfile(self.source.device_path):
                if self.source.protection:
                    self.append_log(self.source.protection)
                QMessageBox.warning(self, "Protect this disk",
                                    (self.source.protection + "\n\n" if self.source.protection else "") + WARNING)

        self.run_in_thread(work, done)

    def reload_catalog(self):
        if self.source is None or self.busy():
            return
        src = self.source
        include_trash = self.trash_cb.isChecked()

        def done(kind, result):
            if kind == "error":
                self.append_log(f"ERROR: {result}")
            else:
                self._set_catalog(result)

        self.run_in_thread(lambda w: Catalog.load(src, include_trash, progress=w.log.emit), done)

    def _set_catalog(self, cat: Catalog):
        self.catalog = cat
        self.model = CatalogModel(cat, self.style())
        self.model.checksChanged.connect(self._selection_changed)
        self.tree.setModel(self.model)
        h = self.tree.header()
        h.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in (1, 2, 3):
            h.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        total = sum(r.total_size for r in cat.roots)
        files = sum(r.file_count for r in cat.roots)
        self.source_label.setText(f"Source: {self.source.label}   |   {files:,} files, {human(total)}   |   {cat.db_info}")
        self.append_log(f"Loaded catalog: {files:,} files, {human(total)}")
        self._selection_changed()

    def _selection_changed(self):
        self.tree.viewport().update()
        if not self.model or not self.model.checked:
            self.sel_label.setText("Selected: nothing")
        else:
            nodes = self.model.selected_nodes()
            self.sel_label.setText(f"Selected: {sum(n.file_count for n in nodes):,} files, "
                                   f"{human(sum(n.total_size for n in nodes))}")
        self._update_buttons()

    # target handling
    def browse_target(self):
        path = QFileDialog.getExistingDirectory(self, "Copy files to")
        if path:
            self.target_edit.setText(os.path.normpath(path))
            self.target_changed()

    def target_changed(self):
        target = self.target_edit.text().strip()
        self.job_label.setText("")
        if target and has_manifest(target):
            try:
                job = CopyJob(target)
                self.job_label.setText("Previous job here: " + self._counts_text(job.counts()))
                job.close()
            except Exception as exc:
                self.job_label.setText(f"Cannot read previous job: {exc}")
        self._update_buttons()

    @staticmethod
    def _counts_text(c) -> str:
        return (f"{c[DONE][0]:,} done, {c[PENDING][0]:,} remaining ({human(c[PENDING][1])}), "
                f"{c[FAILED][0]:,} failed, {c[MISSING][0]:,} missing")

    # copying
    def start_copy(self):
        target = self.target_edit.text().strip()
        nodes = self.model.selected_nodes()
        need = sum(n.total_size for n in nodes)
        try:
            os.makedirs(target, exist_ok=True)
            free = shutil.disk_usage(target).free
        except OSError as exc:
            QMessageBox.warning(self, "Target", f"Cannot use target folder:\n{exc}")
            return
        if need > free and QMessageBox.question(
                self, "Not enough space",
                f"Selected {human(need)} but only {human(free)} is free on the target. Continue anyway?") != QMessageBox.Yes:
            return
        catalog = self.catalog
        keep = self.fullpath_cb.isChecked()
        if not self._confirm_source(target):
            return

        def plan(worker: Worker):
            job = CopyJob(target)
            items = plan_items(catalog, nodes, keep)
            added = job.add(items)
            worker.log.emit(f"Planned {len(items):,} files ({added:,} new, {len(items) - added:,} already in this job)")
            job.close()

        def planned(kind, result):
            if kind == "error":
                self.append_log(f"ERROR planning copy: {result}")
                return
            self.resume_copy(confirmed=True)

        self.run_in_thread(plan, planned)

    def _confirm_source(self, target: str) -> bool:
        if not has_manifest(target):
            return True
        probe = CopyJob(target)
        other = probe.bind_source(self.source.fingerprint)
        probe.close()
        return not other or QMessageBox.question(
            self, "Different source",
            f"This target folder belongs to a job started from another source:\n{other}\n\n"
            f"Current source:\n{self.source.fingerprint}\n\nOnly continue if this is the same Home Duo disk "
            "(for example opened once as a folder and once as a raw disk). Continue?") == QMessageBox.Yes

    def resume_copy(self, confirmed: bool = False):
        target = self.target_edit.text().strip()
        src = self.source
        if src is None or not target:
            return
        if not confirmed and not self._confirm_source(target):
            return
        self.stop_flag.clear()
        self._t0 = time.monotonic()
        self._b0 = None
        fingerprint = src.fingerprint
        reopen = self.last_result == "source_lost"

        def work(worker: Worker):
            if reopen or not src.probe():
                worker.log.emit("Reconnecting to the source disk...")
                src.reopen()
                if not src.probe():
                    raise SourceLost("the source disk is still not readable")
            job = CopyJob(target)
            try:
                job.bind_source(fingerprint, force=True)
                worker.log.emit("Starting: " + self._counts_text(job.counts()))
                last = [0.0]

                def on_progress(p):
                    now = time.monotonic()
                    if now - last[0] > 0.25:
                        last[0] = now
                        worker.progress.emit(p)

                with KeepAwake():
                    result = job.run(src, self.stop_flag.is_set, on_progress, worker.log.emit)
                counts = job.counts()
                report = job.write_report() if counts[FAILED][0] or counts[MISSING][0] else None
            finally:
                job.close()
            return result, counts, report

        def done(kind, result):
            if kind == "error":
                self.last_result = "source_lost" if isinstance(result, (SourceLost, OSError)) else "error"
                self.status_label.setText(f"Error: {result}")
                self.append_log(f"ERROR: {result}")
                return
            res, counts, report = result
            self.last_result = res
            text = self._counts_text(counts)
            self.job_label.setText("This job: " + text)
            msgs = {
                "completed": "Finished.",
                "stopped": "Stopped. Press 'Retry / Resume remaining' to continue.",
                "source_lost": "The source disk disconnected. Reconnect it, then press 'Retry / Resume remaining'.",
                "target_error": "Problem with the target disk (full or disconnected). Fix it, then press 'Retry / Resume remaining'.",
            }
            self.status_label.setText(msgs.get(res, res) + "  " + text)
            self.append_log(msgs.get(res, res))
            if res == "completed":
                self.progress.setValue(1000)
            if report:
                self.append_log(f"List of failed/missing files: {report}")
            if res != "completed" or counts[FAILED][0]:
                QMessageBox.information(self, "Copy", msgs.get(res, res) + "\n\n" + text)

        self.run_in_thread(work, done, self._on_progress)

    def _on_progress(self, p):
        if p.bytes_total:
            self.progress.setValue(int(1000 * p.bytes_done / p.bytes_total))
        now = time.monotonic()
        if self._b0 is None:
            self._b0 = p.bytes_done
            self._t0 = now
        rate = (p.bytes_done - self._b0) / max(now - self._t0, 1e-6)
        eta = ""
        if rate > 0:
            secs = (p.bytes_total - p.bytes_done) / rate
            eta = f", about {int(secs // 3600)}h {int(secs % 3600 // 60)}m left"
        self.status_label.setText(
            f"{p.files_done:,}/{p.files_total:,} files, {human(p.bytes_done)} of {human(p.bytes_total)} "
            f"({human(rate)}/s{eta})  -  {p.current}")

    def stop_copy(self):
        self.stop_flag.set()
        self.append_log("Stopping after the current read...")

    def closeEvent(self, event):
        if self.busy():
            if QMessageBox.question(self, "Copy running", "A copy is running. Stop it and quit?") != QMessageBox.Yes:
                event.ignore()
                return
            self.stop_flag.set()
            if self.thread:
                self.thread.quit()
                self.thread.wait(10000)
        if self.source:
            self.source.close()
        event.accept()


ABOUT = f"""Home Duo Recovery {__version__}

Reads a WD My Cloud Home / Home Duo disk (Linux md RAID1 + ext4) directly and
rebuilds the original folder structure from restsdk/data/db/index.db.

- The source disk is opened read-only and never written to.
- Copies are resumable: progress is stored in <target>/.homeduo-recovery/.
- Files are written as *.partial and renamed only when complete.
"""


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Home Duo Recovery")
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
