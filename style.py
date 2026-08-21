"""
Shared color, theme, and QSS definitions.
Every UI panel imports from here — nothing is hardcoded elsewhere.

Theming: call apply_theme(app, "dark"|"light") once at startup. It sets a Fusion
palette (which colours all standard widgets), the base stylesheet, and
pyqtgraph's plot background/foreground. The subsystem accents in HEX are bright
enough to read on either background, so per-widget accent styling is unchanged.
"""
from PyQt6.QtGui import QColor, QPalette

# ── Per-subsystem accent colors ───────────────────────────────────────────────
HEX = {
    "voltage_cam": "#4488ff",   # blue
    "pupil_cam":   "#44bb66",   # green
    "wheel":       "#ff8833",   # orange
    "puffer":      "#dd4444",   # red
    "stage":       "#1aa3b8",   # teal
    "dmd":         "#d6459b",   # magenta
    "closed_loop": "#9ecf2a",   # chartreuse (rule: signal in, actuation out)
    "sync":        "#8844cc",   # purple  (session-wide controls)
    "saving":      "#c9a227",   # gold    (where recordings are written)
}

# ── Reusable QSS snippets ─────────────────────────────────────────────────────

def toggle_btn(key: str) -> str:
    """Checkable button: a pale accent tint when off, full accent when checked."""
    c = HEX[key]
    tint = QColor(c).lighter(185).name()
    return (
        "QPushButton{"
        f"background:{tint};color:#333;border:1px solid {c};"
        "border-radius:4px;padding:3px 8px}"
        f"QPushButton:checked{{background:{c};color:white;font-weight:bold;"
        f"border:1px solid {c}}}"
    )


def record_btn(key: str) -> str:
    """Like `toggle_btn`, but sized and weighted for the one control whose
    accidental state costs an experiment. Bigger hit area, a red ring while
    armed, so "am I recording?" is answerable from across the rig."""
    c = HEX[key]
    tint = QColor(c).lighter(185).name()
    return (
        "QPushButton{"
        f"background:{tint};color:#333;border:2px solid {c};"
        "border-radius:5px;padding:6px 22px;font-weight:bold;font-size:11pt}"
        f"QPushButton:checked{{background:#c62828;color:white;"
        "border:2px solid #ff5252}"
        "QPushButton:disabled{background:#3a3a3a;color:#777;border:2px solid #555}"
    )


def solid_btn(key: str) -> str:
    """Always-on accent-coloured button."""
    c = HEX[key]
    return (
        f"QPushButton{{background:{c};color:white;font-weight:bold;"
        "border-radius:4px;padding:3px 8px}"
        f"QPushButton:pressed{{background:{QColor(c).darker(120).name()}}}"
    )


def accent_panel(key: str) -> str:
    """Tint a panel's group boxes with the subsystem accent (border + title)."""
    c = HEX[key]
    return (
        "QGroupBox{"
        f"border:1px solid {c};border-radius:5px;margin-top:10px;"
        "padding-top:6px;font-weight:bold}"
        "QGroupBox::title{subcontrol-origin:margin;left:10px;padding:0 4px;"
        f"color:{c}}}"
        # A collapsible section is a disclosure, not an on/off switch. Qt's
        # checkable group box draws a tick box, which reads as "enable this" —
        # so the box is hidden and `widgets.collapsible` puts a ▾/▸ in the
        # title instead. The whole title stays clickable either way.
        "QGroupBox::indicator{width:0px;height:0px;margin:0px}"
    )


def dock_accent(key: str) -> str:
    """Thin accent underline on a dock's title bar."""
    c = HEX[key]
    return f"QDockWidget::title{{border-bottom:2px solid {c};padding:4px}}"


# ── Theme ─────────────────────────────────────────────────────────────────────

# Neutral (non-accent) tones per theme: (groupbox border, pane border, status
# text, plot background, plot foreground).
_THEME = {
    "light": dict(border="#bbb", pane="#ccc", status="#555", plot_bg="w",       plot_fg="k"),
    "dark":  dict(border="#454a4e", pane="#3a3e42", status="#9aa0a6", plot_bg="#1b1e20", plot_fg="#c8ccd0"),
}


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


# Kept for backward-compat; apply_theme() is the entry point now.
APP_QSS = _qss(_THEME["light"])


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
    """(background, foreground) for pyqtgraph in the given theme."""
    t = _THEME.get(theme, _THEME["dark"])
    return (t["plot_bg"], t["plot_fg"])


def apply_theme(app, theme: str) -> None:
    """Apply a full dark/light theme to the QApplication (palette + QSS +
    pyqtgraph). Call once at startup, before building windows/plots."""
    import pyqtgraph as pg
    dark = theme == "dark"
    app.setStyle("Fusion")
    app.setPalette(_dark_palette() if dark else app.style().standardPalette())
    app.setStyleSheet(_qss(_THEME["dark" if dark else "light"]))
    bg, fg = plot_colors("dark" if dark else "light")
    pg.setConfigOption("background", bg)
    pg.setConfigOption("foreground", fg)
