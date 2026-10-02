"""Render one settings panel offscreen to a PNG, for layout checks.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\snap.py <module_key|save|review>
      [--width 380] [--out file.png]

module_key is a config.MODULES key (voltage_cam, pupil_cam, wheel, ...); `save`
is the Save tab, `review` the standalone pupil review. Mock window, isolated
user state: nothing on the rig is touched or rewritten. Not part of run_all.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile

# Before the first Qt import: windowless, with real fonts to measure text by.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")

from _harness import isolate_user_state, make_window, qt_app  # noqa: E402


def _panel(key: str):
    """-> (widget to grab, the window holding it or None)."""
    if key == "review":
        from acqApp.devices.pupil_cam.review_dialog import ReviewWidget
        return ReviewWidget(), None
    from acqApp import config
    win = make_window({key} if key in config.MODULES else set())
    if key == "save":
        return win._save_panel, win
    m = win._module(key)
    if m is None or m.panel is None:
        sys.exit(f"no panel for {key!r}; known: save, review, "
                 + ", ".join(config.MODULES))
    return m.panel, win


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("key", help="a config.MODULES key, 'save' or 'review'")
    ap.add_argument("--width", type=int, default=380)
    ap.add_argument("--out", default="",
                    help="default: snap_<key>.png in the temp dir")
    a = ap.parse_args()

    isolate_user_state()
    app = qt_app()      # held: a collected QApplication takes the process down
    w, win = _panel(a.key)
    w.setParent(None)   # out of its scroll area, so the width is ours to set
    w.resize(a.width, 100)
    w.show()
    app.processEvents()
    h = w.heightForWidth(a.width) if w.hasHeightForWidth() else -1
    w.resize(a.width, max(h, w.sizeHint().height(), w.minimumSizeHint().height()))
    app.processEvents()
    out = a.out or os.path.join(tempfile.gettempdir(), f"snap_{a.key}.png")
    if not w.grab().save(out):
        print(f"could not write {out}")
        return 1
    print(f"{out}: {w.width()}x{w.height()}")
    w.close()
    if win is not None:
        win.close()
    app.processEvents()
    return 0


if __name__ == "__main__":
    sys.exit(main())
