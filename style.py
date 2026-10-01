"""Shared colours, theme and QSS. `apply_theme(app, "dark"|"light")` once at
startup sets the Fusion palette, base stylesheet and pyqtgraph colours."""
from PyQt6.QtGui import QColor, QPalette

# ── Per-subsystem accent colors (readable on either theme) ────────────────────
HEX = {
    "voltage_cam": "#4488ff",   # blue
    "pupil_cam":   "#44bb66",   # green
    "wheel":       "#ff8833",   # orange
    "puffer":      "#dd4444",   # red
    "stage":       "#1aa3b8",   # teal
    "dmd":         "#d6459b",   # magenta
    "vis_stim":    "#22c7d6",   # cyan
    "mirror":      "#ff4fa3",   # pink
    "closed_loop": "#9ecf2a",   # chartreuse
    "routines":    "#6f7bf7",   # indigo
    "sync":        "#8844cc",   # purple (session-wide controls)
    "saving":      "#c9a227",   # gold
}

_THEME = {
    "light": dict(border="#bbb", pane="#ccc", status="#555", plot_bg="w",       plot_fg="k"),
    "dark":  dict(border="#454a4e", pane="#3a3e42", status="#9aa0a6", plot_bg="#1b1e20", plot_fg="#c8ccd0"),
}

_ACTIVE = "dark"                # so panels can ask for neutral tones

WARN = "#cc8866"        # amber: "this won't do what you expect"


def muted() -> str:
    return _THEME[_ACTIVE]["status"]


def line() -> str:
    return _THEME[_ACTIVE]["border"]


# ── Reusable QSS snippets ─────────────────────────────────────────────────────
# Every button style has a :disabled rule: a stylesheet background overrides
# the palette, so a disabled button would otherwise still look pressable.

def _tint(c: str) -> str:
    return QColor(c).lighter(185).name()


def toggle_btn(key: str) -> str:
    """Pale accent when off, full accent when checked."""
    c = HEX[key]
    return (
        "QPushButton{"
        f"background:{_tint(c)};color:#333;border:1px solid {c};"
        "border-radius:4px;padding:3px 8px}"
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
        f"background:{_tint(c)};color:#333;border:2px solid {c};"
        "border-radius:5px;padding:6px 22px;font-weight:bold;font-size:11pt}"
        f"QPushButton:checked{{background:#c62828;color:white;"
        "border:2px solid #ff5252}"
        "QPushButton:disabled{background:#3a3a3a;color:#777;border:2px solid #555}"
    )


def solid_btn(key: str) -> str:
    """A panel's primary action."""
    c = HEX[key]
    return (
        f"QPushButton{{background:{c};color:white;font-weight:bold;"
        "border-radius:4px;padding:3px 8px}"
        f"QPushButton:pressed{{background:{QColor(c).darker(120).name()}}}"
        f"QPushButton:disabled{{background:{QColor(c).darker(260).name()};"
        f"color:{_THEME['dark']['status']}}}"
    )


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
    )


def dock_accent(key: str) -> str:
    c = HEX[key]
    return f"QDockWidget::title{{border-bottom:2px solid {c};padding:4px}}"


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


def plot_colors(theme: str) -> tuple[str, str]:
    """(background, foreground) for pyqtgraph."""
    t = _THEME.get(theme, _THEME["dark"])
    return (t["plot_bg"], t["plot_fg"])


def apply_theme(app, theme: str) -> None:
    """Palette + QSS + pyqtgraph. Before building windows/plots."""
    import pyqtgraph as pg
    global _ACTIVE
    dark = theme == "dark"
    _ACTIVE = "dark" if dark else "light"
    app.setStyle("Fusion")
    app.setPalette(_dark_palette() if dark else app.style().standardPalette())
    app.setStyleSheet(_qss(_THEME[_ACTIVE]))
    bg, fg = plot_colors(_ACTIVE)
    pg.setConfigOption("background", bg)
    pg.setConfigOption("foreground", fg)
