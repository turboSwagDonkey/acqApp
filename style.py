"""Shared colours, the dark theme and QSS. `apply_theme(app)` once at startup
sets the Fusion palette, base stylesheet and pyqtgraph colours."""
from PyQt6.QtGui import QColor, QPalette

# ── Per-subsystem accent colors ───────────────────────────────────────────────
HEX = {
    "voltage_cam": "#4488ff",   # blue
    "pupil_cam":   "#44bb66",   # green
    "wheel":       "#ff8833",   # orange
    "puffer":      "#dd4444",   # red
    "stage":       "#1aa3b8",   # teal
    "dmd":         "#d6459b",   # magenta
    "vis_stim":    "#7cab2b",   # lime (cyan sat beside stage's teal)
    "mirror":      "#8796ad",   # steel, General's family (pink sat beside dmd)
    "routines":    "#9b5de5",   # violet (indigo sat beside voltage_cam's blue)
    "sync":        "#6c7a8e",   # slate: session-wide controls, no device's hue
    "saving":      "#c9a227",   # gold
}

_T = dict(border="#454a4e", pane="#3a3e42", status="#9aa0a6",
          plot_bg="#1b1e20", plot_fg="#c8ccd0")

WARN = "#cc8866"        # amber: "this won't do what you expect"


def muted() -> str:
    return _T["status"]


def line() -> str:
    return _T["border"]


def plot_bg() -> str:
    return _T["plot_bg"]


# ── Reusable QSS snippets ─────────────────────────────────────────────────────
# Every button style has a :disabled rule: a stylesheet background overrides
# the palette, so a disabled button would otherwise still look pressable.

def _alpha(c: str, a: int = 40) -> str:
    """The accent, translucent over whatever is behind it."""
    q = QColor(c)
    return f"rgba({q.red()},{q.green()},{q.blue()},{a})"


def toggle_btn(key: str) -> str:
    """An accent outline when off, full accent when checked."""
    c = HEX[key]
    return (
        "QPushButton{"
        f"background:{_alpha(c)};border:1px solid {c};"
        "border-radius:4px;padding:3px 8px}"
        f"QPushButton:hover{{background:{_alpha(c, 90)}}}"
        f"QPushButton:checked{{background:{c};color:white;font-weight:bold;"
        f"border:1px solid {c}}}"
        "QPushButton:disabled{background:#3a3a3a;color:#777;"
        "border:1px solid #555}"
    )


def record_btn(key: str) -> str:
    """Bigger, and red while recording: readable from across the rig."""
    c = HEX[key]
    return (
        "QPushButton{"
        f"background:{_alpha(c)};border:2px solid {c};"
        "border-radius:5px;padding:6px 22px;font-weight:bold;font-size:11pt}"
        f"QPushButton:hover{{background:{_alpha(c, 90)}}}"
        f"QPushButton:checked{{background:#c62828;color:white;"
        "border:2px solid #ff5252}"
        "QPushButton:disabled{background:#3a3a3a;color:#777;border:2px solid #555}"
    )


def quiet_toggle(key: str, fill: bool = True) -> str:
    """Neutral while off; accent while on, even disabled: a mode that stays
    on through a session must not grey out then. `fill=False`: on is an
    accent outline, for a state that is normal, not a warning."""
    c = HEX[key]
    on = (f"background:{c};color:white;" if fill
          else f"background:{_alpha(c)};color:{c};")
    return (
        "QPushButton{background:transparent;border:1px solid palette(mid);"
        "border-radius:4px;padding:3px 8px}"
        "QPushButton:disabled{color:palette(mid)}"
        f"QPushButton:checked,QPushButton:checked:disabled{{{on}"
        f"font-weight:bold;border:1px solid {c}}}"
    )


def solid_btn(key: str) -> str:
    """A panel's primary action."""
    c = HEX[key]
    return (
        f"QPushButton{{background:{c};color:white;font-weight:bold;"
        "border-radius:4px;padding:3px 8px}"
        f"QPushButton:pressed{{background:{QColor(c).darker(120).name()}}}"
        f"QPushButton:disabled{{background:{QColor(c).darker(260).name()};"
        f"color:{muted()}}}"
    )


def stop_btn() -> str:
    """The emergency control: larger than anything near it."""
    return solid_btn("puffer") + "QPushButton{font-size:12pt;padding:8px}"


def accent_panel(key: str) -> str:
    """Group boxes in the subsystem accent. The checkable-box indicator is
    hidden: `widgets.collapsible` shows ▾/▸ in the title instead."""
    c = HEX[key]
    return (
        "QGroupBox{"
        f"border:1px solid {c};border-radius:5px;margin-top:10px;"
        "padding-top:6px;font-weight:bold}"
        "QGroupBox::title{subcontrol-origin:margin;left:10px;padding:0 4px;"
        f"color:{c}}}"
        "QGroupBox::indicator{width:0px;height:0px;margin:0px}"
        # A folded "advanced" box is a heading over a line, not an empty frame.
        f"QGroupBox#advanced:unchecked{{border:none;"
        f"border-bottom:1px solid {c};border-radius:0px;padding-top:0px;margin-top:4px}}"
    )


def dock_accent(key: str) -> str:
    """A dock's title bar: a bold, tinted band in its accent."""
    c = HEX[key]
    return (
        "QDockWidget{font-weight:bold}"
        f"QDockWidget::title{{background:{_alpha(c, 70)};"
        f"border:1px solid {c};border-bottom:2px solid {c};"
        "border-top-left-radius:4px;border-top-right-radius:4px;padding:4px}"
    )


def zone(key: str) -> str:
    """A dock's body, framed in its accent under the title band: each region
    reads as one block."""
    c = HEX[key]
    return (f"QFrame#zone{{border:1px solid {c};border-top:none;"
            "border-bottom-left-radius:4px;border-bottom-right-radius:4px}")


# ── Theme ─────────────────────────────────────────────────────────────────────

def _qss(t: dict) -> str:
    return f"""
QMainWindow, QWidget {{ font-family: Arial; font-size: 9pt; }}

QGroupBox {{
    font-weight: bold;
    border: 1px solid {t['border']};
    border-radius: 5px;
    margin-top: 10px;
    padding-top: 6px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
}}

QTabWidget::pane  {{ border: 1px solid {t['pane']}; }}
QTabBar::tab      {{ padding: 4px 12px; }}
QTabBar::tab:selected {{ font-weight: bold; }}

QStatusBar {{ font-size: 8pt; color: {t['status']}; }}

/* The gaps between docks: wide enough to read as borders, and to grab. */
QMainWindow::separator {{ width: 8px; height: 8px; }}
"""


def _dark_palette() -> QPalette:
    p = QPalette()
    window   = QColor("#232629")
    base     = QColor("#1b1e20")
    alt      = QColor("#2b2f33")
    text     = QColor("#e6e6e6")
    disabled = QColor("#7a7f83")
    C = QPalette.ColorRole
    p.setColor(C.Window, window);          p.setColor(C.WindowText, text)
    p.setColor(C.Base, base);              p.setColor(C.AlternateBase, alt)
    p.setColor(C.Text, text);              p.setColor(C.Button, window)
    p.setColor(C.ButtonText, text);        p.setColor(C.BrightText, QColor("#ff5555"))
    p.setColor(C.ToolTipBase, base);       p.setColor(C.ToolTipText, text)
    p.setColor(C.PlaceholderText, disabled)
    p.setColor(C.Highlight, QColor(HEX["voltage_cam"]))
    p.setColor(C.HighlightedText, QColor("#ffffff"))
    p.setColor(C.Link, QColor(HEX["voltage_cam"]))
    for role in (C.WindowText, C.Text, C.ButtonText):
        p.setColor(QPalette.ColorGroup.Disabled, role, disabled)
    return p


_MPL_SCAN_DEFERRED = False


def _defer_matplotlib_scan() -> None:
    """pyqtgraph's LUT colormap menu imports matplotlib (~0.4 s, at the first
    LUT widget) only to decide whether to list an "Others" submenu. List it
    always and do the scan when that submenu opens. Any change in pyqtgraph's
    internals leaves it untouched: slower start, same behaviour."""
    global _MPL_SCAN_DEFERRED
    if _MPL_SCAN_DEFERRED:
        return
    _MPL_SCAN_DEFERRED = True
    try:
        from pyqtgraph.widgets import ColorMapMenu as cmm
        from pyqtgraph.Qt import QtCore
        real = cmm.find_mpl_leftovers

        @QtCore.Slot()
        def build_others(self):
            self.buildSubMenu(real(), "matplotlib")

        cmm.find_mpl_leftovers = lambda: True
        cmm.ColorMapMenu.buildMplOthersSubMenu = build_others
    except (ImportError, AttributeError):
        pass


def apply_theme(app) -> None:
    """Palette + QSS + pyqtgraph. Before building windows/plots."""
    import pyqtgraph as pg
    _defer_matplotlib_scan()
    app.setStyle("Fusion")
    app.setPalette(_dark_palette())
    app.setStyleSheet(_qss(_T))
    pg.setConfigOption("background", _T["plot_bg"])
    pg.setConfigOption("foreground", _T["plot_fg"])
