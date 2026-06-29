"""
Shared color, font, and QSS definitions.
Every UI panel imports from here — nothing is hardcoded elsewhere.
"""
from PyQt5.QtGui import QColor, QFont

# ── Per-subsystem accent colors ───────────────────────────────────────────────
HEX = {
    "voltage_cam": "#4488ff",   # blue
    "pupil_cam":   "#44bb66",   # green
    "wheel":       "#ff8833",   # orange
    "puffer":      "#dd4444",   # red
    "sync":        "#8844cc",   # purple
    "neutral":     "#666666",
}

COLOR = {k: QColor(v) for k, v in HEX.items()}

# ── Fonts ─────────────────────────────────────────────────────────────────────
FONT_TITLE = QFont("Arial", 10, QFont.Bold)
FONT_LABEL = QFont("Arial", 9)
FONT_MONO  = QFont("Courier New", 9)

# ── Reusable QSS snippets ─────────────────────────────────────────────────────

def toggle_btn(key: str) -> str:
    """Checkable button: grey when off, accent color when checked."""
    c = HEX[key]
    return (
        "QPushButton{"
        "background:#c8c8c8;color:#444;border-radius:4px;padding:3px 8px}"
        f"QPushButton:checked{{background:{c};color:white;font-weight:bold}}"
    )


def solid_btn(key: str) -> str:
    """Always-on accent-coloured button."""
    c = HEX[key]
    return (
        f"QPushButton{{background:{c};color:white;font-weight:bold;"
        "border-radius:4px;padding:3px 8px}"
        f"QPushButton:pressed{{background:{QColor(c).darker(120).name()}}}"
    )


# Global stylesheet applied to QApplication
APP_QSS = """
QMainWindow, QWidget { font-family: Arial; font-size: 9pt; }

QGroupBox {
    font-weight: bold;
    border: 1px solid #bbb;
    border-radius: 5px;
    margin-top: 10px;
    padding-top: 6px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
}

QTabWidget::pane  { border: 1px solid #ccc; }
QTabBar::tab      { padding: 4px 12px; }
QTabBar::tab:selected { font-weight: bold; }

QStatusBar { font-size: 8pt; color: #555; }
"""
