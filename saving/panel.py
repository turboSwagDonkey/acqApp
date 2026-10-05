"""The Save tab — destination, template and the capacity estimate."""
from __future__ import annotations

import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

from acqApp.saving.config import (_DEFAULT_SUBDIR, DEFAULT_TEMPLATE, TOKENS,
                                  SaveConfig, _gb, benchmark_drive,
                                  default_folder, free_bytes, list_drives)
from acqApp.widgets import compact

# Match to Bpod… hidden at the operator's request (2026-10-01); the edge log
# is still written, so a session can be matched later.
SHOW_BPOD_MATCH = False


class SavePanel(QWidget):
    """Settings tab: destination drive/folder, naming, and capacity readout."""

    settings_changed = pyqtSignal()

    # Must run past the SSD's SLC cache: a short burst once hid a SATA
    # drive's real ceiling. 1 GiB does on this rig's drives.
    _SCAN_SIZE_MB = 1024
    # Writer overhead over a raw write: 2696 -> 2464 MB/s, ~9%
    # (docs/CAMERA_TRANSFER.md).
    _SCAN_DERATE = 0.85

    def __init__(self, config: SaveConfig | None = None, parent=None):
        super().__init__(parent)
        self._cfg = config or SaveConfig()
        if not self._cfg.folder.strip():
            self._cfg.folder = str(default_folder())
        self._rate_mbps: float = 0.0
        self._writer_mbps: float = 0.0
        self._active_fov: str = ""
        self._recording = False             # blocks Bpod renaming
        self._build()
        self._refresh()

    # ── UI ───────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        grp = QGroupBox("Save configuration")
        lay = QFormLayout(grp)
        lay.setSpacing(4)

        self._cmb_drive = compact(QComboBox())
        self._reload_drives()
        self._cmb_drive.activated.connect(self._on_drive_picked)
        self._cmb_drive.setToolTip("The drive recordings go to, with its free space.")
        self._btn_scan = QPushButton("Scan drives")
        self._btn_scan.setToolTip(
            "Write ~1 GiB to each drive to measure its real sustained write "
            "speed, and flag any that would drop frames at current "
            "acquisition rate.")
        self._btn_scan.clicked.connect(self._on_scan_drives)
        drive_row = QHBoxLayout()
        drive_row.setContentsMargins(0, 0, 0, 0)
        drive_row.addWidget(self._cmb_drive)
        drive_row.addWidget(self._btn_scan)
        drive_row.addStretch()
        drive_row_w = QWidget()
        drive_row_w.setLayout(drive_row)
        lay.addRow("Drive:", drive_row_w)

        self._lbl_scan = QLabel()
        self._lbl_scan.setWordWrap(True)
        self._lbl_scan.setStyleSheet("color:#8a8a8a;")
        lay.addRow("Drive scan:", self._lbl_scan)

        self._ed_folder = QLineEdit(self._cfg.folder)
        self._ed_folder.editingFinished.connect(self._on_edited)
        self._ed_folder.setToolTip("Where each session's folder is made.")
        btn_browse = QPushButton("Browse…")
        btn_browse.setToolTip("Pick the folder.")
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

        self._ed_mouse_id = compact(QLineEdit(self._cfg.mouse_id), chars=16)
        self._ed_mouse_id.setPlaceholderText("animal / mouse ID")
        self._ed_mouse_id.setToolTip("Goes in the filename as {mouse_id}.")
        self._ed_mouse_id.editingFinished.connect(self._on_edited)
        lay.addRow("Mouse ID:", self._ed_mouse_id)

        self._ed_project = compact(QLineEdit(self._cfg.project), chars=16)
        self._ed_project.setPlaceholderText("optional project label")
        self._ed_project.setToolTip("Goes in the filename as {project}.")
        self._ed_project.editingFinished.connect(self._on_edited)
        lay.addRow("Project:", self._ed_project)

        self._ed_template = compact(QLineEdit(self._cfg.template), chars=28)
        self._ed_template.setToolTip("Tokens: " + "  ".join(TOKENS))
        self._ed_template.editingFinished.connect(self._on_edited)
        lay.addRow("Filename:", self._ed_template)

        self._chk_fov = QCheckBox("Append active FOV name")
        self._chk_fov.setChecked(self._cfg.append_fov)
        self._chk_fov.setToolTip("Add the name of the FOV the stage is at to the filename.")
        self._chk_fov.toggled.connect(self._on_edited)
        lay.addRow("", self._chk_fov)
        self._update_fov_checkbox()

        self._cmb_orca_format = compact(QComboBox())
        self._cmb_orca_format.addItem("DCIMG — native, faster", "dcimg")
        self._cmb_orca_format.addItem("TIFF — per-frame timestamps", "tiff")
        # Preview survives a .dcimg recording (2026-09-23); per-frame reads
        # don't, so there are no per-frame timestamps.
        self._cmb_orca_format.setToolTip(
            "Each recording is a folder: this camera's frames, the pupil "
            "camera as .avi, everything else as CSV, settings as JSON.\n"
            "DCIMG: the camera driver writes the file itself — faster, and "
            "routines still run, but the only times recorded are when the "
            "file opened and closed (cam_dcimg_t0_s/t1_s), not per frame.\n"
            "TIFF: frames go through acqApp, each stamped on the shared clock.")
        idx = self._cmb_orca_format.findData(self._cfg.orca_format)
        self._cmb_orca_format.setCurrentIndex(max(0, idx))
        self._cmb_orca_format.currentIndexChanged.connect(self._on_edited)
        lay.addRow("Voltage cam:", self._cmb_orca_format)

        self._lbl_preview = QLabel()
        self._lbl_preview.setWordWrap(True)
        self._lbl_preview.setStyleSheet("color:#8a8a8a;")
        lay.addRow("Next file:", self._lbl_preview)

        self._lbl_space = QLabel()
        self._lbl_space.setWordWrap(True)
        lay.addRow("Capacity:", self._lbl_space)

        align = QGroupBox("Stim rig alignment")
        al = QVBoxLayout(align)
        btn_bpod = QPushButton("Match to Bpod…")
        btn_bpod.setToolTip(
            "After a routine: match its trigger edges to Bpod's trials, "
            "renumber trial folders to Bpod's trial numbers and mark missed "
            "trials VOID. Checks first; changes nothing until you Apply.")
        btn_bpod.clicked.connect(self._on_match_bpod)
        al.addWidget(btn_bpod)

        root = QFormLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addRow(grp)
        root.addRow(align)
        align.setVisible(SHOW_BPOD_MATCH)

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
        """Benchmark every drive and flag any too slow for the current rate.
        Synchronous: a couple of seconds per drive, with processEvents()."""
        self._btn_scan.setEnabled(False)
        html = []
        try:
            for root, free, total in list_drives():
                self._lbl_scan.setText(f"scanning {root}…")
                self._lbl_scan.setStyleSheet("color:#8a8a8a;")
                QApplication.processEvents()
                size = self._SCAN_SIZE_MB << 20
                if free < size * 4:
                    html.append(f"{root}&nbsp;&nbsp;skipped — only {_gb(free)} free "
                                f"(need some room to test meaningfully)")
                    continue
                mbps = benchmark_drive(root, size)
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

    def _on_match_bpod(self) -> None:
        from acqApp.saving.bpod_dialog import BpodMatchDialog
        BpodMatchDialog(self._cfg, lambda: self._recording,
                        self.settings_changed.emit, self).exec()

    def _on_edited(self, *_a) -> None:
        self._cfg.folder    = self._ed_folder.text().strip()
        self._cfg.mouse_id  = self._ed_mouse_id.text().strip()
        self._cfg.project   = self._ed_project.text().strip()
        self._cfg.template  = (self._ed_template.text().strip()
                              or DEFAULT_TEMPLATE)
        self._cfg.orca_format = self._cmb_orca_format.currentData()
        self._cfg.append_fov  = self._chk_fov.isChecked()
        self._sync_drive_combo()
        self._refresh()
        self.settings_changed.emit()

    def _current_fov(self) -> str:
        """The FOV name to append, or "" (box off or no FOV active)."""
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

    def resolve_dir(self, when: datetime | None = None, *,
                    unique: bool = False) -> Path:
        return self._cfg.resolve_dir(when, unique=unique, fov=self._current_fov())

    def resolve_routine_dir(self, fov: str, trial: int,
                            when: datetime | None = None, *,
                            unique: bool = False) -> Path:
        return self._cfg.resolve_routine_dir(fov, trial, when, unique=unique)

    def set_active_fov(self, name: str) -> None:
        """The Stage tab's current FOV, or "" off it. Called every display
        tick, so a no-op unless it changed."""
        name = name or ""
        if name == self._active_fov:
            return
        self._active_fov = name
        self._update_fov_checkbox()
        self._refresh()

    def set_recording_active(self, on: bool) -> None:
        self._recording = bool(on)

    def set_expected_rate(self, mbps: float, writer_mbps: float = 0.0) -> None:
        """Offered data rate, and what the writer sustains (0 = unknown:
        assume all is written). Passed in: `saving/` doesn't import devices."""
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
        # The path a recording now would really get, `_001` included.
        plain = self.resolve_dir()
        unique = self.resolve_dir(unique=True)
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
        warn = free < (10 << 30)
        if self._rate_mbps > 0:
            # The disk fills at what's WRITTEN: full frame offers ~2200 MB/s
            # to a writer that sustains less, and sheds the rest.
            cap = self._writer_mbps or self._rate_mbps
            written = min(self._rate_mbps, cap)
            secs = free / (written * (1 << 20))
            txt += f" — about {secs / 60:.1f} min at {written:.0f} MB/s"
            if self._rate_mbps > cap:
                txt += (f"; the camera offers {self._rate_mbps:.0f} MB/s, so "
                        f"~{100 * (1 - written / self._rate_mbps):.0f}% of "
                        f"frames can't be written — see the Voltage cam tab")
                warn = True
            warn = warn or secs < 120
        self._lbl_space.setText(txt)
        self._lbl_space.setStyleSheet(
            "color:#c47f00; font-weight:bold;" if warn else "color:#2e7d32;")
