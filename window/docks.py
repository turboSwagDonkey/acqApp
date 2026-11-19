"""The window's layout: General column, one dock per own-dock module,
the status bar, and saving/restoring the dock layout."""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QEvent, QSize, Qt, QSettings, QUrl
from PyQt6.QtGui import QAction, QDesktopServices, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QComboBox, QDockWidget, QFrame, QLabel, QHBoxLayout, QMainWindow,
    QPushButton, QScrollArea, QStatusBar, QToolButton, QVBoxLayout,
    QWidget,
)

from acqApp import config, style
from acqApp.saving import SaveConfig, SavePanel
from acqApp.sidebar import Fold, MiniText
from acqApp.widgets import align_grids, compact
from acqApp.window.common import MODE_NONE, MODULES_TIP, module_rank


# Saved dock layouts of an older version are ignored, not half-applied.
_LAYOUT_VERSION = 4
# An own-dock module's default place, (column, row). General tops column
# _GENERAL_COL; the others lie to its right.
_GENERAL_COL = -1
# Path 2's camera sits right of path 1's (its DMD under it); without path 2,
# its empty column is skipped and the layout is path 1's alone.
_SLOTS = {"stage": (_GENERAL_COL, 1),
          "voltage_cam": (0, 0), "routines": (0, 1),
          "voltage_cam_2": (1, 0), "dmd_2": (1, 1),
          "dmd": (2, 0), "pupil_cam": (2, 1)}
# Default column widths (px, as ratios) by how many columns there are.
_WIDTHS = {3: [370, 750, 480], 4: [370, 620, 620, 420]}
# Height ratio in a column of docks (else 3 for the top, 2 below): a DMD's
# page is its pattern alone, so it takes little.
_SMALL = {"dmd": 1, "dmd_2": 1}
# A hidden view -> where its column-mates go meanwhile: with path 2's camera
# merged into path 1's view, its DMD joins path 1's rather than fill a column.
_MERGED_SLOTS = {"voltage_cam_2": {"dmd_2": (2, 0)}}


def _section_rank(key: str) -> int:
    return -1 if key == "saving" else module_rank(key)


def _slot(key: str) -> tuple[int, int]:
    return _SLOTS.get(key, (1, 99))


def _dress(panel: QWidget, key: str) -> None:
    """What every settings panel gets: its accent and aligned grids. Its
    boxes don't fold: the card does, so one click shows them all."""
    panel.setStyleSheet(style.accent_panel(key))
    align_grids(panel)


class _VScroll(QScrollArea):
    """Scrolls only up and down: never narrower than what it holds, so a
    column can't be dragged so thin that its sections need a sideways bar."""

    def __init__(self, w: QWidget, *, fixed: bool = False) -> None:
        super().__init__()
        self._fixed = fixed     # never sizes its dock: scrolls both ways instead
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded if fixed
            else Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setWidget(w)
        w.installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Type.LayoutRequest:
            self.updateGeometry()       # a section opened or shut
        return super().eventFilter(obj, event)

    def minimumSizeHint(self) -> QSize:
        hint = super().minimumSizeHint()
        if self._fixed:
            return hint
        bar = self.verticalScrollBar().sizeHint().width()
        hint.setWidth(self.widget().minimumSizeHint().width() + bar)
        return hint


class _DockTitle(QFrame):
    """A dock's title band: its name, its module's status (and controls, e.g.
    the DMD switch), then ⚙ Settings, which swaps the view for its settings.
    Clicks elsewhere on it fall through, so the dock still drags by it."""

    def __init__(self, title: str, accent: str, fold: Fold | None,
                 mini: QWidget | None) -> None:
        super().__init__()
        self.setObjectName("dockTitle")
        self.setStyleSheet(style.dock_title(accent))
        self._home = (title, accent)
        lay = self._lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 2, 3, 2)
        lay.setSpacing(10)
        self.name = QLabel(title)
        self.name.setObjectName("dockName")
        lay.addWidget(self.name)
        if mini is not None:
            lay.addWidget(mini, 1)
        else:
            lay.addStretch(1)
        # Borrowed by a section that takes the tab over (General's).
        self._lent: tuple | None = None         # (mini, its card)
        self.back = QToolButton()
        self.back.setText(f"◂ {title}")
        self.back.setToolTip(f"Back to the {title} list.")
        self.back.hide()
        self._on_back = None
        self.back.clicked.connect(self._back_clicked)
        lay.addWidget(self.back)
        self.button = None
        if fold is not None and fold.body is not None:
            b = self.button = QToolButton()
            b.setText("⚙ Settings")
            b.setCheckable(True)
            b.setChecked(fold.is_open())
            b.setToolTip(f"Show the {title} settings in place of its view; "
                         "again to go back.")
            b.toggled.connect(fold.set_open)
            fold.toggled.connect(b.setChecked)  # equal state: no echo
            lay.addWidget(b)
            if isinstance(mini, MiniText):      # a warning opens where it's fixed
                mini.warn_hint = "Click to open Settings."
                mini.warning_clicked.connect(lambda: fold.set_open(True))

    def take_over(self, title: str, accent: str, fold: Fold) -> None:
        """Wear `fold`'s section: its name, colour and status (its card's
        mini view, borrowed), and ◂ back, which shuts it."""
        self.give_back()
        self.name.setText(title)
        self.setStyleSheet(style.dock_title(accent))
        mini = fold.card.mini
        if mini is not None:
            self._lay.insertWidget(1, mini, 1)
            self._lent = (mini, fold.card)
        self._on_back = lambda: fold.set_open(False)
        self.back.show()

    def _back_clicked(self) -> None:
        if self._on_back is not None:
            self._on_back()

    def give_back(self) -> None:
        """Its own name and colour again; a borrowed mini view goes home."""
        if self._lent is not None:
            mini, card = self._lent
            self._lent = None
            card.reclaim_mini(mini)
        title, accent = self._home
        self.name.setText(title)
        self.setStyleSheet(style.dock_title(accent))
        self._on_back = None
        self.back.hide()


class DockLayout:
    """MainWindow's UI build and dock arrangement."""

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        """One window, all docks (no central widget, so every pane drags):
        General over stage | voltage cam over routines | DMD over pupil cam."""
        self.resize(1600, 900)
        # Nested, never tabbed: a tab would hide one region behind another
        # (General behind the stage, say).
        self.setDockOptions(QMainWindow.DockOption.AnimatedDocks
                            | QMainWindow.DockOption.AllowNestedDocks)

        self._build_general()
        # Every panel before any view: controllers read them.
        for m in self._modules:
            m.build_panel()
        for m in self._modules:
            self._place_module(m)

        self._build_status_bar()
        self._arrange_docks()
        self._restore_layout()

    def _build_general(self) -> None:
        """The left dock: Save and every module without a dock of its own,
        each folded under its card, over the session-wide controls."""
        save_cfg = config.load_dataclass(SaveConfig, "saving")
        if not save_cfg.mouse_id:
            # Legacy key: `subject` became mouse_id on 2026-09-14.
            old_subject = config.load_settings("saving").get("subject")
            if isinstance(old_subject, str) and old_subject.strip():
                save_cfg.mouse_id = old_subject.strip()
        self._save_panel = SavePanel(save_cfg)
        self._save_panel.settings_changed.connect(self._save_save_settings)
        # The capacity warning's link: the camera whose rate it is about.
        self._save_panel.camera_settings_requested.connect(
            lambda: self._open_settings("voltage_cam"))
        _dress(self._save_panel, "saving")

        inner = QWidget()
        col = QVBoxLayout(inner)
        col.setContentsMargins(4, 4, 4, 4)
        self._sections = QVBoxLayout()
        self._sections.setSpacing(6)
        col.addLayout(self._sections)
        col.addStretch(1)
        self._add_section("saving", "Save", self._mini("saving"), self._save_panel)

        host = QWidget()
        lay = QVBoxLayout(host)
        lay.setContentsMargins(0, 0, 0, 0)
        # Fixed: an opened section never widens the tab (it scrolls instead).
        self._general_scroll = _VScroll(inner, fixed=True)
        lay.addWidget(self._general_scroll, 1)
        lay.addWidget(self._build_controls())
        self._general_dock = self._make_dock(
            "General", host, Qt.DockWidgetArea.LeftDockWidgetArea)
        self._sync_sections()       # Save may have been left open

    def _build_controls(self) -> QWidget:
        """One line: Mode, then small buttons (icons, bar Modules)."""
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(4, 4, 4, 4)
        row.setSpacing(4)

        self._modes = config.load_modes()
        self._mode_combo = compact(QComboBox())
        self._mode_combo.addItems((MODE_NONE, *self._modes))
        self._mode_combo.setToolTip(
            "Apply a named preset across modules (DMD illumination, camera "
            "capture area, ...), defined in modes.json. \"None\" leaves "
            "everything as set.")
        self._mode_combo.currentTextChanged.connect(self.set_mode)
        row.addWidget(QLabel("Mode:"))
        row.addWidget(self._mode_combo)

        self._save_mode_action = QAction("💾", self)
        self._save_mode_action.setToolTip(
            "Save as preset: capture the DMD's and voltage camera's CURRENT "
            "settings as a new (or replacement) entry in modes.json.")
        self._save_mode_action.triggered.connect(self._save_mode_as)

        self._modules_action = QAction("🧩 Modules", self)
        self._modules_action.setToolTip(MODULES_TIP)
        self._modules_action.triggered.connect(self._open_modules_dialog)

        self._reset_action = QAction("⟲", self)
        self._reset_action.setToolTip(
            "Reset layout: put every pane back where it started (drag a pane "
            "by its title bar to move it; drag the gaps between panes to "
            "resize)")
        self._reset_action.triggered.connect(self._reset_layout)

        row.addStretch()
        for act in (self._save_mode_action, self._modules_action,
                    self._reset_action):
            b = QToolButton()
            b.setDefaultAction(act)
            row.addWidget(b)
        return box

    def _mini(self, key: str) -> QWidget | None:
        """A fresh live view for `key`'s card."""
        if key == "saving":
            return MiniText(self._save_mini_text)
        m = self._module(key)
        return m.mini_view() if m is not None else None

    def _place_module(self, m) -> None:
        """Its settings folded under its card: in its own dock over its
        view, else as a section of General."""
        if m.panel is not None:
            _dress(m.panel, m.key)
        if self._rides(m) and m.rides_on in self._docks:
            self._lend(m)                   # its host is here: no section
            return
        mini = m.mini_view()
        if not m.own_dock:
            if m.panel is not None or mini is not None:
                self._add_section(m.key, m.tab_label, mini, m.panel)
            return
        view = m.view_widget()
        body = (_VScroll(m.panel, fixed=view is not None)
                if m.panel is not None else None)
        # No card: its status and Settings button sit on the title bar.
        fold = Fold(m.key, "Settings", None, body, default_open=view is None,
                    card_toggles=False)
        self._folds[m.key] = fold
        self._docks[m.key] = self._make_dock(
            m.tab_label, self._dock_body(fold, view),
            Qt.DockWidgetArea.LeftDockWidgetArea, accent=m.key,
            fold=fold, mini=mini)
        if m.key in self._hidden_views:     # asked for before it existed
            self._docks[m.key].hide()
        self._seat_riders()                 # a host arrived: its riders move in

    # ── riders: a module shown on another's dock (the ETL on the Stage) ──
    @staticmethod
    def _rides(m) -> bool:
        return bool(m.rides_on) and hasattr(m.panel, "strip")

    def _seat_riders(self) -> None:
        """A rider still in General whose host is now loaded moves onto it."""
        for m in self._modules:
            if self._rides(m) and m.rides_on in self._docks and m.key in self._folds:
                fold = self._folds.pop(m.key)
                self._sections.removeWidget(fold)
                m.panel.setParent(None)     # out of the fold, which goes
                self._sync_sections()       # (its status may be on the title)
                fold.setParent(None)
                fold.deleteLater()
                self._lend(m)

    def _lend(self, m) -> None:
        """`m`'s strip on top of its host's view (hidden while the host's
        Settings are open), its calibration at the foot of those Settings."""
        hfold = self._folds[m.rides_on]
        strip, calib = m.panel.strip, m.panel.calibration
        hfold.parentWidget().layout().insertWidget(0, strip)

        def follow(settings_open: bool) -> None:
            strip.setVisible(not settings_open)

        hfold.toggled.connect(follow)
        follow(hfold.is_open())
        scroll = hfold.body
        inner = scroll.takeWidget()         # the host's own panel, kept
        holder = QWidget()
        lay = QVBoxLayout(holder)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(inner)
        lay.addWidget(calib)
        lay.addStretch(1)                   # right under the host's own, not at the foot
        scroll.setWidget(holder)
        holder.installEventFilter(scroll)   # its height follows, as inner's did
        self._riding[m.key] = (m.rides_on, follow, scroll, inner, holder)

    def _unlend(self, m) -> None:
        """Undo `_lend`: the host's Settings as they were, the strip and
        calibration home in `m`'s panel."""
        host, follow, scroll, inner, holder = self._riding.pop(m.key)
        hfold = self._folds.get(host)
        if hfold is not None:
            hfold.toggled.disconnect(follow)
        m.panel.take_back()
        scroll.takeWidget()
        scroll.setWidget(inner)
        holder.deleteLater()

    def _host_leaving(self, key: str) -> None:
        """`key`'s dock is about to go: its riders back to General."""
        for m in self._modules:
            if self._riding.get(m.key, (None,))[0] == key:
                self._unlend(m)
                self._add_section(m.key, m.tab_label, m.mini_view(), m.panel)

    def set_view_shown(self, key: str, shown: bool) -> None:
        """Show or hide `key`'s dock; remembered, so a dock built later, a
        Reset layout and a restored layout all keep it. Hiding keeps the
        operator's arrangement (only its `_MERGED_SLOTS` mates move);
        showing again puts the arrangement back as it was before."""
        if shown == (key not in self._hidden_views):
            return                          # no change: leave the layout be
        if shown:
            self._hidden_views.discard(key)
        else:
            self._hidden_views.add(key)
        dock = self._docks.get(key)
        if dock is None:
            return
        if not shown:
            self._before_hide[key] = self.saveState(_LAYOUT_VERSION)
            dock.hide()
            for mate, slot in _MERGED_SLOTS.get(key, {}).items():
                host = next((k for k, s in _SLOTS.items() if s == slot
                             and k in self._docks and k not in self._hidden_views),
                            None)
                if mate in self._docks and host is not None:
                    self.splitDockWidget(self._docks[host], self._docks[mate],
                                         Qt.Orientation.Vertical)
            return
        saved = self._before_hide.pop(key, None)
        if saved is None or not self.restoreState(saved, _LAYOUT_VERSION):
            self._arrange_docks()           # nothing to go back to
        for k, d in self._docks.items():
            d.setVisible(k not in self._hidden_views)

    def _reset_layout(self) -> None:
        """⟲ Reset layout: the default, and no merge to undo back to."""
        self._before_hide.clear()
        self._arrange_docks()

    def _add_section(self, key: str, label: str, mini: QWidget | None,
                     panel: QWidget | None) -> None:
        fold = Fold(key, label, mini, panel)
        at = sum(1 for k in self._folds
                 if k not in self._docks and _section_rank(k) < _section_rank(key))
        self._sections.insertWidget(at, fold)
        self._folds[key] = fold
        fold.toggled.connect(lambda on, k=key: self._on_section_toggled(k, on))
        if fold.is_open():          # one section at a time, even from saved state
            self._on_section_toggled(key, True)
        else:
            self._sync_sections()

    def _general_folds(self) -> dict[str, Fold]:
        return {k: f for k, f in self._folds.items() if k not in self._docks}

    def _on_section_toggled(self, key: str, on: bool) -> None:
        """An opened General section takes over the tab, as a dock's
        Settings do: the others shut (one at a time) and step aside."""
        if on:
            for k, f in self._general_folds().items():
                if k != key and f.is_open():
                    f.set_open(False)
        self._sync_sections()

    def _sync_sections(self) -> None:
        """The open section alone, its card on top (a click goes back to
        the list); none open, every card."""
        folds = self._general_folds()
        shown = next((k for k, f in folds.items() if f.is_open()), None)
        for k, f in folds.items():
            f.setVisible(shown is None or k == shown)
            f.set_card_shown(k != shown)    # the title bar stands in for it
        dock = getattr(self, "_general_dock", None)
        if dock is None:                    # still being built
            return
        title = dock.titleBarWidget()
        accent = "sync" if shown is None else shown
        if shown is None:
            title.give_back()
        else:
            title.take_over(folds[shown].label, shown, folds[shown])
        dock.widget().setStyleSheet(style.zone(accent))

    @staticmethod
    def _dock_body(fold: Fold, view: QWidget | None) -> QWidget:
        """The view over the fold's card. Open, the Settings take the view's
        place (card on top) and the dock keeps its size; with no view, the
        fold fills the dock."""
        host = QWidget()
        lay = QVBoxLayout(host)
        if view is None:
            lay.setContentsMargins(4, 4, 4, 4)
            lay.addWidget(fold, 1)
            lay.addStretch(0)
            return host
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        lay.addWidget(view, 1)
        lay.addWidget(fold, 0)

        def opened(on: bool) -> None:
            view.setVisible(not on)
            lay.setStretchFactor(fold, 1 if on else 0)
        fold.toggled.connect(opened)
        opened(fold.is_open())      # built open from the saved state
        return host

    def _build_status_bar(self) -> None:
        self._btn_emulate = QPushButton("Emulate")
        self._btn_emulate.setCheckable(True)
        self._btn_emulate.setChecked(self._emulate)
        self._btn_emulate.setStyleSheet(style.quiet_toggle("wheel", fill=False))
        self._btn_emulate.setToolTip("Use simulated signals instead of hardware")
        self._btn_emulate.toggled.connect(self._on_emulate_toggled)
        self._btn_emulate.toggled.connect(self._label_emulate)
        self._label_emulate(self._emulate)

        self._btn_run = QPushButton("Live view")
        self._btn_run.setCheckable(True)
        self._btn_run.setStyleSheet(style.toggle_btn("sync"))
        self._btn_run.setToolTip("Show live signals from all devices (not saved)."
                                 " Ctrl+L starts and stops it.")
        self._btn_run.toggled.connect(self._on_run_toggled)

        self._btn_rec = QPushButton("● Record")
        self._btn_rec.setCheckable(True)
        self._btn_rec.setStyleSheet(style.record_btn("puffer"))
        self._btn_rec.setToolTip("Live view and save every stream to disk."
                                 " Ctrl+R starts and stops it.")
        # On the window, not the buttons (a button's setText resets its
        # shortcut). Ctrl-chords: a bare key would fire while typing an ID.
        self._shortcuts = {}
        for keys, btn in (("Ctrl+L", self._btn_run), ("Ctrl+R", self._btn_rec)):
            sc = QShortcut(QKeySequence(keys), self)
            sc.activated.connect(btn.click)
            self._shortcuts[keys] = sc
        self._btn_rec.toggled.connect(self._on_record_toggled)

        # Permanent labels, not status(): any status() call would wipe them.
        self._lbl_rec = QLabel("")
        self._lbl_rec.setStyleSheet("color:#9aa0a6;")
        self._lbl_time = QLabel("")
        self._lbl_time.setStyleSheet("color:#9aa0a6;")

        # After a recording: one click to the folder it went to.
        self._btn_open_last = QPushButton("📂 Open recording")
        self._btn_open_last.setFlat(True)
        self._btn_open_last.hide()
        self._btn_open_last.clicked.connect(self._open_last_recording)
        self._last_rec: Path | None = None

        sb = QStatusBar()
        self.setStatusBar(sb)
        sb.addPermanentWidget(self._btn_open_last)
        sb.addPermanentWidget(self._lbl_time)
        sb.addPermanentWidget(self._lbl_rec)
        sb.addPermanentWidget(self._btn_emulate)
        sb.addPermanentWidget(self._btn_run)
        sb.addPermanentWidget(self._btn_rec)
        sb.showMessage("Ready")
        for m in self._modules:
            self._add_status_widget(m)
        self._size_status_boxes()

    def _add_status_widget(self, m) -> None:
        """Place the control a module offers (the DMD's light switch) beside Record."""
        w = m.status_widget()
        if w is not None:
            self.statusBar().addPermanentWidget(w)
            w.show()
            self._status_widgets[m.key] = w

    def _size_status_boxes(self) -> None:
        """Record and each module switch get one fixed box, wide enough for
        every label any of them shows: no resizing between states, no clipping."""
        rec, keep = self._btn_rec, self._btn_rec.text()
        w = h = 0
        for text in ("● Record", "■ Stop rec"):
            rec.setText(text)
            hint = rec.sizeHint()
            w, h = max(w, hint.width()), max(h, hint.height())
        rec.setText(keep)
        # Buttons only: a switch (the mirror's) keeps its own shape.
        buttons = [b for b in self._status_widgets.values()
                   if isinstance(b, QPushButton)]
        for b in buttons:
            w, h = max(w, b.minimumWidth(), b.sizeHint().width()), max(h, b.sizeHint().height())
        for b in (rec, *buttons):
            b.setFixedSize(w, h)

    def _remove_status_widget(self, key: str) -> None:
        w = self._status_widgets.pop(key, None)
        if w is not None:
            self.statusBar().removeWidget(w)
            w.setParent(None)
            w.deleteLater()

    def _make_dock(self, title: str, widget: QWidget,
                   area: "Qt.DockWidgetArea", accent: str = "sync",
                   fold: Fold | None = None,
                   mini: QWidget | None = None) -> QDockWidget:
        dock = QDockWidget(title, self)
        dock.setObjectName(f"dock_{title}")
        dock.setTitleBarWidget(_DockTitle(title, accent, fold, mini))
        zone = QFrame()                 # the accent frame under the title band
        zone.setObjectName("zone")
        zone.setStyleSheet(style.zone(accent))
        lay = QVBoxLayout(zone)
        lay.setContentsMargins(3, 3, 3, 3)
        lay.addWidget(widget)
        dock.setWidget(zone)
        dock.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)
        # Rearranged within the window, never torn out of it or closed.
        dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetMovable)
        dock.setStyleSheet(style.dock_accent(accent))
        self.addDockWidget(area, dock)
        return dock

    def _open_settings(self, key: str) -> None:
        """`key`'s Settings open, its dock to the front (if it's loaded)."""
        fold = self._folds.get(key)
        if fold is not None:
            fold.set_open(True)
        dock = self._docks.get(key)
        if dock is not None:
            dock.raise_()

    def _offer_last_recording(self, path: Path | None) -> None:
        """Show (a path) or drop (None) the status bar's Open button."""
        self._last_rec = path
        self._btn_open_last.setVisible(path is not None)
        if path is not None:
            self._btn_open_last.setToolTip(f"Open {path} in Explorer.")

    def _open_last_recording(self) -> None:
        if self._last_rec is not None:
            target = self._last_rec if self._last_rec.exists() else self._last_rec.parent
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def _save_mini_text(self) -> tuple[str, str]:
        path = self.recording_path()
        if path is not None:
            return "● REC", path.name
        s = self._save_panel.settings
        return "", f"{s.mouse_id or 'no mouse ID'} → {s.resolved_folder().name}"

    # ── Dock layout ─────────────────────────────────────────────────────────────

    def _label_emulate(self, on: bool) -> None:
        self._btn_emulate.setText("EMULATE: no hardware" if on else "Emulate")

    def _arrange_docks(self) -> None:
        """The default layout, also Reset layout: a column per `_SLOTS`
        column, its docks stacked by row, General on top of the first."""
        general = self._general_dock
        for d in (general, *self._docks.values()):
            d.setFloating(False)
            self.removeDockWidget(d)
        cols: dict[int, list] = {_GENERAL_COL: [general]}
        # A hidden view stays out of the layout: no empty column for it.
        shown = [k for k in self._docks if k not in self._hidden_views]
        for key in sorted(shown, key=lambda k: (self._slot_of(k), module_rank(k))):
            cols.setdefault(self._slot_of(key)[0], []).append(self._docks[key])
        stacks = [cols[c] for c in sorted(cols)]
        # Columns first, then each split into rows: a row split first would
        # put the next column beside one row only.
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, general)
        for prev, stack in zip(stacks, stacks[1:]):
            self.splitDockWidget(prev[0], stack[0], Qt.Orientation.Horizontal)
        for stack in stacks:
            for upper, lower in zip(stack, stack[1:]):
                self.splitDockWidget(upper, lower, Qt.Orientation.Vertical)
        general.show()
        for key, d in self._docks.items():
            d.setVisible(key not in self._hidden_views)
        # Narrow left: the stage map fills it.
        widths = _WIDTHS.get(len(stacks), _WIDTHS[3][:len(stacks)])
        self.resizeDocks([s[0] for s in stacks], widths,
                         Qt.Orientation.Horizontal)
        key_of = {d: k for k, d in self._docks.items()}
        for stack in stacks:
            if len(stack) < 2:
                continue
            # resizeDocks treats sizes as ratios, so these must add up.
            if stack[0] is general and len(stack) == 2:
                # General takes only its cards' height: the stage map the rest.
                h = self._general_height()
                sizes = [h, max(1, self.height() - h)]
            else:
                sizes = [_SMALL.get(key_of.get(d), 3 if i == 0 else 2)
                         for i, d in enumerate(stack)]
            self.resizeDocks(stack, sizes, Qt.Orientation.Vertical)

    def _slot_of(self, key: str) -> tuple[int, int]:
        """`_slot`, but moved while a view it shares a column with is hidden."""
        for hidden in self._hidden_views:
            moved = _MERGED_SLOTS.get(hidden, {})
            if key in moved:
                return moved[key]
        return _slot(key)

    def _general_height(self) -> int:
        """The dock's hint with the scroll area's stock ~24 lines swapped for
        what it holds."""
        scroll = self._general_scroll
        return (self._general_dock.sizeHint().height()
                - scroll.sizeHint().height()
                + scroll.widget().sizeHint().height())

    def _dock_beside(self, key: str) -> None:
        """A dock loaded mid-session: under a docked neighbour in its column
        (General heads its own), else right of General."""
        dock = self._docks.get(key)
        if dock is None:
            return
        col = self._slot_of(key)[0]
        sib = next((d for k, d in self._docks.items()
                    if k != key and self._slot_of(k)[0] == col
                    and not d.isFloating() and k not in self._hidden_views),
                   self._general_dock if col == _GENERAL_COL else None)
        if sib is not None:
            self.splitDockWidget(sib, dock, Qt.Orientation.Vertical)
        else:
            self.splitDockWidget(self._general_dock, dock,
                                 Qt.Orientation.Horizontal)
        if key in self._hidden_views:       # a split shows it again
            dock.hide()

    def _restore_layout(self) -> None:
        state = QSettings("acqApp", "acqApp").value("dockState")
        if state is not None:
            self.restoreState(state, _LAYOUT_VERSION)
        # A layout saved while floating was allowed would restore a dock outside.
        for d in (self._general_dock, *self._docks.values()):
            d.setFloating(False)
        # The saved state may show a view the session hid (an overlay), or
        # hide one it shows.
        for key, d in self._docks.items():
            d.setVisible(key not in self._hidden_views)

    def _save_layout(self) -> None:
        QSettings("acqApp", "acqApp").setValue(
            "dockState", self.saveState(_LAYOUT_VERSION))
