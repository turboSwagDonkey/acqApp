"""The Save tab — destination, template and the capacity estimate."""
from __future__ import annotations

import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QWidget,
)

from acqApp.saving.config import (_DEFAULT_SUBDIR, DEFAULT_TEMPLATE, TOKENS,
                                  SaveConfig, _gb, benchmark_drive,
                                  default_folder, free_bytes, list_drives)


class SavePanel(QWidget):
    """Settings tab: destination drive/folder, naming, and capacity readout."""

    settings_changed = pyqtSignal()

    # A short burst reads fast on any SSD — its own SLC write cache absorbs
    # it — which is exactly the trap that hid a SATA drive's real ceiling
    # behind the writer/GIL for a whole session (PLAN.md sec 6 item 1). 1 GiB
    # is enough to run past that cache on the drives this rig actually has.
    _SCAN_SIZE_MB = 1024
    # The writer's own overhead over a raw write, measured in
    # docs/CAMERA_TRANSFER.md (direct-chunk 2696 -> 2464 MB/s through the
    # whole path, ~9%) — derate the raw measurement before calling it safe.
    _SCAN_DERATE = 0.85

    def __init__(self, config: SaveConfig | None = None, parent=None):
        super().__init__(parent)
        self._cfg = config or SaveConfig()
        if not self._cfg.folder.strip():
            self._cfg.folder = str(default_folder())
        self._rate_mbps: float = 0.0        # set by the owner from the cam config
        self._writer_mbps: float = 0.0      # …and what the writer sustains
        self._active_fov: str = ""          # set by the owner (MainWindow), live
        self._build()
        self._refresh()

    # ── UI ───────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        grp = QGroupBox("Save configuration")
        lay = QFormLayout(grp)
        lay.setSpacing(4)

        # Drive shortcut: picking one rewrites the folder to that drive.
        self._cmb_drive = QComboBox()
        self._reload_drives()
        self._cmb_drive.activated.connect(self._on_drive_picked)
        self._btn_scan = QPushButton("Scan drives")
        self._btn_scan.setToolTip(
            "Write ~1 GiB to each drive to measure its real sustained write "
            "speed, and flag any that would drop frames at current "
            "acquisition rate.")
        self._btn_scan.clicked.connect(self._on_scan_drives)
        drive_row = QHBoxLayout()
        drive_row.setContentsMargins(0, 0, 0, 0)
        drive_row.addWidget(self._cmb_drive, 1)
        drive_row.addWidget(self._btn_scan)
        drive_row_w = QWidget()
        drive_row_w.setLayout(drive_row)
        lay.addRow("Drive:", drive_row_w)

        self._lbl_scan = QLabel()
        self._lbl_scan.setWordWrap(True)
        self._lbl_scan.setStyleSheet("color:#8a8a8a;")
        lay.addRow("Drive scan:", self._lbl_scan)

        self._ed_folder = QLineEdit(self._cfg.folder)
        self._ed_folder.editingFinished.connect(self._on_edited)
        btn_browse = QPushButton("Browse…")
        btn_browse.clicked.connect(self._on_browse)
        btn_open = QPushButton("Open")
        btn_open.setToolTip("Open this folder in Explorer")
        btn_open.clicked.connect(self._on_open)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self._ed_folder, 1)
        row.addWidget(btn_browse)
        row.addWidget(btn_open)
        row_w = QWidget()
        row_w.setLayout(row)
        lay.addRow("Folder:", row_w)

        self._ed_mouse_id = QLineEdit(self._cfg.mouse_id)
        self._ed_mouse_id.setPlaceholderText("animal / mouse ID")
        self._ed_mouse_id.editingFinished.connect(self._on_edited)
        lay.addRow("Mouse ID:", self._ed_mouse_id)

        self._ed_project = QLineEdit(self._cfg.project)
        self._ed_project.setPlaceholderText("optional project label")
        self._ed_project.editingFinished.connect(self._on_edited)
        lay.addRow("Project:", self._ed_project)

        self._ed_template = QLineEdit(self._cfg.template)
        self._ed_template.setToolTip("Tokens: " + "  ".join(TOKENS))
        self._ed_template.editingFinished.connect(self._on_edited)
        lay.addRow("Filename:", self._ed_template)

        self._chk_fov = QCheckBox("Append active FOV name")
        self._chk_fov.setChecked(self._cfg.append_fov)
        self._chk_fov.toggled.connect(self._on_edited)
        lay.addRow("", self._chk_fov)
        self._update_fov_checkbox()

        self._chk_subfolder = QCheckBox("Give each recording its own subfolder")
        self._chk_subfolder.setChecked(self._cfg.subfolder)
        self._chk_subfolder.toggled.connect(self._on_edited)
        lay.addRow("", self._chk_subfolder)

        self._chk_split = QCheckBox(
            "Split into per-device files (instead of one composite .h5)")
        self._chk_split.setChecked(self._cfg.split)
        self._chk_split.setToolTip(
            "Each device in its own file — TIFF image stacks for "
            "cameras, one combined CSV for wheel/puffer/pupil-fit/routine "
            "step, one JSON for settings — instead of everything bundled "
            "into one .h5. Always gets its own session folder.")
        self._chk_split.toggled.connect(self._on_split_toggled)
        lay.addRow("", self._chk_split)

        self._cmb_orca_format = QComboBox()
        self._cmb_orca_format.addItem("TIFF — per-frame timestamps", "tiff")
        self._cmb_orca_format.addItem("DCIMG — native, faster", "dcimg")
        # Preview DOES survive a .dcimg recording (measured 2026-09-23): the
        # driver keeps filling the ring, so read_newest_image still answers.
        # What stops is per-frame access — read_multiple_images returns
        # nothing, so there is no _timestamps.csv. Routines DO run on it:
        # they count the recorder's own frame total instead.
        self._cmb_orca_format.setToolTip(
            "TIFF: frames go through acqApp, each stamped on the shared "
            "clock.\n"
            "DCIMG: the camera driver writes the file itself — faster, and "
            "routines still run, but the only times recorded are when the "
            "file opened and closed (cam_dcimg_t0_s/t1_s), not per frame.")
        idx = self._cmb_orca_format.findData(self._cfg.orca_format)
        self._cmb_orca_format.setCurrentIndex(max(0, idx))
        self._cmb_orca_format.currentIndexChanged.connect(self._on_edited)
        self._cmb_orca_format.setEnabled(self._cfg.split)
        lay.addRow("ORCA format:", self._cmb_orca_format)

        self._lbl_preview = QLabel()
        self._lbl_preview.setWordWrap(True)
        self._lbl_preview.setStyleSheet("color:#8a8a8a;")
        lay.addRow("Next file:", self._lbl_preview)

        # The point of the whole panel: how long can this actually record?
        self._lbl_space = QLabel()
        self._lbl_space.setWordWrap(True)
        lay.addRow("Capacity:", self._lbl_space)

        root = QFormLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addRow(grp)

    def _reload_drives(self) -> None:
        self._cmb_drive.blockSignals(True)
        self._cmb_drive.clear()
        for root, free, total in list_drives():
            self._cmb_drive.addItem(
                f"{root}   {_gb(free)} free of {_gb(total)}", root)
        self._cmb_drive.blockSignals(False)
        self._sync_drive_combo()

    def _sync_drive_combo(self) -> None:
        """Point the combo at whichever drive the current folder lives on."""
        try:
            anchor = os.path.splitdrive(str(Path(self._cfg.folder)))[0].upper()
        except (ValueError, OSError):
            return
        for i in range(self._cmb_drive.count()):
            data = self._cmb_drive.itemData(i) or ""
            if os.path.splitdrive(data)[0].upper() == anchor:
                self._cmb_drive.blockSignals(True)
                self._cmb_drive.setCurrentIndex(i)
                self._cmb_drive.blockSignals(False)
                return

    # ── Handlers ─────────────────────────────────────────────────────────────

    def _on_drive_picked(self, idx: int) -> None:
        root = self._cmb_drive.itemData(idx)
        if root:
            self._ed_folder.setText(str(Path(root) / _DEFAULT_SUBDIR))
            self._on_edited()

    def _on_browse(self) -> None:
        start = self._cfg.folder or str(default_folder())
        chosen = QFileDialog.getExistingDirectory(self, "Session folder", start)
        if chosen:
            self._ed_folder.setText(chosen)
            self._on_edited()

    def _on_open(self) -> None:
        folder = self._cfg.resolved_folder()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(str(folder))                    # noqa: S606 (Windows)
        except (OSError, AttributeError) as e:
            self._lbl_space.setText(f"Could not open {folder}: {e}")

    def _on_scan_drives(self) -> None:
        """Benchmark every fixed drive and flag any too slow for the current
        acquisition rate. Synchronous (like dialogs.py's device probe loop) —
        each drive is only ~1 GiB, a couple of seconds even on SATA, and
        `processEvents()` between drives keeps the window from looking frozen.
        """
        self._btn_scan.setEnabled(False)
        html = []
        try:
            for root, free, total in list_drives():
                self._lbl_scan.setText(f"scanning {root}…")
                self._lbl_scan.setStyleSheet("color:#8a8a8a;")
                QApplication.processEvents()
                need = (self._SCAN_SIZE_MB << 20) * 4     # leave the drive most of its room
                if free < need:
                    html.append(f"{root}&nbsp;&nbsp;skipped — only {_gb(free)} free "
                                f"(need some room to test meaningfully)")
                    continue
                mbps = benchmark_drive(root, self._SCAN_SIZE_MB << 20)
                if mbps is None:
                    html.append(f"{root}&nbsp;&nbsp;write test failed (permissions?)")
                    continue
                line = f"{root}&nbsp;&nbsp;{mbps:.0f} MB/s"
                if self._rate_mbps > 0:
                    safe = mbps * self._SCAN_DERATE
                    if safe < self._rate_mbps:
                        pct = 100 * (1 - safe / self._rate_mbps)
                        line = (f'<span style="color:#c62828; font-weight:bold;">'
                                f'{line} ⚠ would drop ~{pct:.0f}% of frames '
                                f'at {self._rate_mbps:.0f} MB/s</span>')
                    else:
                        line += ' <span style="color:#2e7d32;">— OK at the current rate</span>'
                html.append(line)
            if self._rate_mbps <= 0:
                html.append("(no acquisition rate known yet — showing raw "
                            "write speed only)")
            self._lbl_scan.setStyleSheet("")
            self._lbl_scan.setText("<br>".join(html))
        finally:
            self._btn_scan.setEnabled(True)

    def _on_split_toggled(self, on: bool) -> None:
        self._cmb_orca_format.setEnabled(on)
        self._on_edited()

    def _on_edited(self, *_a) -> None:
        self._cfg.folder    = self._ed_folder.text().strip()
        self._cfg.mouse_id  = self._ed_mouse_id.text().strip()
        self._cfg.project   = self._ed_project.text().strip()
        self._cfg.template  = (self._ed_template.text().strip()
                              or DEFAULT_TEMPLATE)
        self._cfg.subfolder = self._chk_subfolder.isChecked()
        self._cfg.split       = self._chk_split.isChecked()
        self._cfg.orca_format = self._cmb_orca_format.currentData()
        self._cfg.append_fov  = self._chk_fov.isChecked()
        self._sync_drive_combo()
        self._refresh()
        self.settings_changed.emit()

    def _current_fov(self) -> str:
        """The name to append to the stem right now — empty unless a FOV is
        active AND the operator opted in, so turning the box on with nothing
        active is a silent no-op rather than an empty trailing underscore."""
        return self._active_fov if self._chk_fov.isChecked() else ""

    def _update_fov_checkbox(self) -> None:
        self._chk_fov.setEnabled(bool(self._active_fov))
        self._chk_fov.setToolTip(
            f'Appends "{self._active_fov}" to the filename.' if self._active_fov
            else "No FOV is active — go to one on the Stage tab first.")

    # ── Public API ───────────────────────────────────────────────────────────

    @property
    def settings(self) -> SaveConfig:
        return self._cfg

    def as_dict(self) -> dict:
        return asdict(self._cfg)

    def resolve(self, when: datetime | None = None, *,
                unique: bool = False) -> Path:
        return self._cfg.resolve(when, unique=unique, fov=self._current_fov())

    def resolve_dir(self, when: datetime | None = None, *,
                    unique: bool = False) -> Path:
        return self._cfg.resolve_dir(when, unique=unique, fov=self._current_fov())

    def resolve_routine(self, fov: str, trial: int,
                        when: datetime | None = None, *,
                        unique: bool = False) -> Path:
        return self._cfg.resolve_routine(fov, trial, when, unique=unique)

    def resolve_routine_dir(self, fov: str, trial: int,
                            when: datetime | None = None, *,
                            unique: bool = False) -> Path:
        return self._cfg.resolve_routine_dir(fov, trial, when, unique=unique)

    def set_active_fov(self, name: str) -> None:
        """The Stage tab's current FOV, or "" once the stage drifts off it —
        called on the shared display tick (MainWindow.active_fov_name())."""
        name = name or ""
        if name == self._active_fov:
            return
        self._active_fov = name
        self._update_fov_checkbox()
        self._refresh()

    def set_expected_rate(self, mbps: float, writer_mbps: float = 0.0) -> None:
        """Data rate of the current acquisition config, for the capacity estimate.

        `writer_mbps` is what the write path can actually sustain. Passed in
        rather than imported: it's a camera-side measurement, and `saving/`
        doesn't depend on `devices/` (see docs/STRUCTURE.md). 0 means unknown,
        and the estimate then assumes everything offered is written.
        """
        self._rate_mbps = max(0.0, float(mbps))
        self._writer_mbps = max(0.0, float(writer_mbps))
        self._refresh()

    def writable_error(self) -> str | None:
        """Human-readable reason the target is unusable, or None if it's fine."""
        folder = self._cfg.resolved_folder()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return f"can't create {folder}: {e}"
        if not os.access(str(folder), os.W_OK):
            return f"{folder} isn't writable"
        return None

    # ── Readouts ─────────────────────────────────────────────────────────────

    def _refresh(self) -> None:
        # Preview the path a recording started now would actually get, so a
        # template that collides shows its `_001` here rather than surprising
        # the operator in the status line after the fact.
        resolve = self.resolve_dir if self._cfg.split else self.resolve
        plain = resolve()
        unique = resolve(unique=True)
        self._lbl_preview.setText(str(unique))
        self._lbl_preview.setToolTip(
            f"{plain.name} exists — next recording is auto-numbered."
            if unique != plain else "")

        free = free_bytes(self._cfg.folder or str(default_folder()))
        if free is None:
            self._lbl_space.setText("Target folder doesn't exist yet.")
            self._lbl_space.setStyleSheet("color:#c47f00;")
            return

        txt = f"{_gb(free)} free"
        warn = free < (10 << 30)          # under 10 GB isn't a usable target
        if self._rate_mbps > 0:
            # The disk fills at what's WRITTEN, not what the camera offers, and
            # those differ: full frame at bin 1 acquires ~2200 MB/s against a
            # writer that sustains ~1000. Estimating from the offered rate both
            # halved the time and — worse — showed a configuration that sheds
            # half its frames in the same green as a healthy one.
            cap = self._writer_mbps or self._rate_mbps
            written = min(self._rate_mbps, cap)
            secs = free / (written * (1 << 20))
            txt += f" — about {secs / 60:.1f} min at {written:.0f} MB/s"
            if self._rate_mbps > cap:
                txt += (f"; the camera offers {self._rate_mbps:.0f} MB/s, so "
                        f"~{100 * (1 - written / self._rate_mbps):.0f}% of "
                        f"frames can't be written — see the Voltage cam tab")
                warn = True
            warn = warn or secs < 120     # under 2 minutes of headroom
        self._lbl_space.setText(txt)
        self._lbl_space.setStyleSheet(
            "color:#c47f00; font-weight:bold;" if warn else "color:#2e7d32;")
