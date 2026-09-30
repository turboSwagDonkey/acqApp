"""The Save tab's "Match to Bpod…" dialog: check, then apply, bpod_match."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable

from PyQt6.QtGui import QFontDatabase
from PyQt6.QtWidgets import (
    QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLineEdit, QMessageBox,
    QPlainTextEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from acqApp.saving import bpod_match
from acqApp.saving.config import SaveConfig


def newest_edge_log(cfg: SaveConfig) -> Path | None:
    """Today's most recent routine edge log, if any."""
    folder = cfg.routine_base(datetime.now())
    try:
        logs = sorted(folder.glob("routine_edges_*.csv"),
                      key=lambda p: p.stat().st_mtime)
    except OSError:
        return None
    return logs[-1] if logs else None


class BpodMatchDialog(QDialog):
    def __init__(self, cfg: SaveConfig, recording: Callable[[], bool],
                 on_folder: Callable[[], None], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Match trials to Bpod")
        self.resize(640, 480)
        self._cfg = cfg
        self._recording = recording
        self._on_folder = on_folder         # persist the remembered Bpod folder
        self._plan: bpod_match.Plan | None = None

        edges = newest_edge_log(cfg)
        self._ed_edges = QLineEdit(str(edges) if edges else "")
        self._ed_edges.setPlaceholderText("routine_edges_….csv")
        self._ed_bpod = QLineEdit()
        self._ed_bpod.setPlaceholderText("Bpod session data file (.mat)")
        self._spn_first = QSpinBox()
        self._spn_first.setRange(1, 99999)
        self._spn_first.setToolTip(
            "The first Bpod trial the routine was imaging. Unmatched trials "
            "from here on are marked VOID.")
        for ed in (self._ed_edges, self._ed_bpod):
            ed.textChanged.connect(self._stale)
        self._spn_first.valueChanged.connect(self._stale)

        form = QFormLayout()
        form.addRow("Edge log:", self._row(self._ed_edges, self._browse_edges))
        form.addRow("Bpod file:", self._row(self._ed_bpod, self._browse_bpod))
        form.addRow("First trial:", self._spn_first)

        self._report = QPlainTextEdit()
        self._report.setReadOnly(True)
        self._report.setFont(QFontDatabase.systemFont(
            QFontDatabase.SystemFont.FixedFont))

        self._btn_check = QPushButton("Check")
        self._btn_check.clicked.connect(self._check)
        self._btn_apply = QPushButton("Apply")
        self._btn_apply.setEnabled(False)
        self._btn_apply.clicked.connect(self._apply)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.accept)
        buttons = QHBoxLayout()
        buttons.addWidget(self._btn_check)
        buttons.addWidget(self._btn_apply)
        buttons.addStretch(1)
        buttons.addWidget(btn_close)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(self._report, 1)
        lay.addLayout(buttons)

    @staticmethod
    def _row(edit: QLineEdit, browse) -> QWidget:
        btn = QPushButton("Browse…")
        btn.clicked.connect(browse)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(edit, 1)
        row.addWidget(btn)
        w = QWidget()
        w.setLayout(row)
        return w

    def _browse_edges(self) -> None:
        start = self._ed_edges.text() or str(self._cfg.routine_base(datetime.now()))
        path, _ = QFileDialog.getOpenFileName(
            self, "Routine edge log", start, "Edge logs (routine_edges_*.csv)")
        if path:
            self._ed_edges.setText(path)

    def _browse_bpod(self) -> None:
        start = self._ed_bpod.text() or self._cfg.bpod_folder
        path, _ = QFileDialog.getOpenFileName(
            self, "Bpod session data", start, "MATLAB files (*.mat)")
        if path:
            self._ed_bpod.setText(path)
            self._cfg.bpod_folder = str(Path(path).parent)
            self._on_folder()

    def _stale(self, *_a) -> None:
        # Any change invalidates the plan; Check again before Apply.
        self._plan = None
        self._btn_apply.setEnabled(False)

    def _check(self) -> None:
        self._stale()
        try:
            lines, pl = bpod_match.check(Path(self._ed_edges.text()),
                                         Path(self._ed_bpod.text()),
                                         self._spn_first.value())
        except Exception as e:          # noqa: BLE001 — unreadable/wrong file
            self._report.setPlainText(f"Could not read the files: "
                                      f"{type(e).__name__}: {e}")
            return
        self._report.setPlainText("\n".join(lines))
        if pl is not None and (pl.renames or pl.voids):
            self._plan = pl
            self._btn_apply.setEnabled(True)

    def _apply(self) -> None:
        pl = self._plan
        if pl is None:
            return
        if self._recording():
            QMessageBox.warning(self, "Recording",
                                "Stop recording first: a trial folder that is "
                                "still open can't be renamed.")
            return
        if QMessageBox.question(
                self, "Apply",
                f"Rename {len(pl.renames)} trial folder(s) and mark "
                f"{len(pl.voids)} trial(s) VOID?\n\nEvery change is logged "
                f"to renumber_log.csv beside the edge log.") \
                != QMessageBox.StandardButton.Yes:
            return
        edges = Path(self._ed_edges.text())
        try:
            done = bpod_match.apply(pl, edges.parent)
        except OSError as e:
            self._report.appendPlainText(
                f"\nSTOPPED: {e}\n(Usually a file still open; close whatever "
                f"holds it and Check again.)")
        else:
            self._report.appendPlainText(
                f"\nDone: {len(done)} change(s), logged in "
                f"{edges.parent / 'renumber_log.csv'}")
        self._stale()
