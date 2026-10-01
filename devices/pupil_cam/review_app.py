"""The pupil review window as its own program: no rig, no hardware, no acqApp
window. `python run_pupil_review.py [clip.avi]` (the repo-root launcher sets up its own
environment), or `python -m acqApp.devices.pupil_cam.review_app` in a ready one.
`--check` builds the window and exits.
"""
from __future__ import annotations

import sys

from acqApp.console import enable_safe_console


def main(argv: list[str] | None = None) -> int:
    enable_safe_console()
    argv = sys.argv[1:] if argv is None else argv
    from PyQt6.QtWidgets import QApplication
    from acqApp.devices.pupil_cam.review_dialog import PupilReviewDialog

    check = "--check" in argv
    argv = [a for a in argv if a != "--check"]
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    dlg = PupilReviewDialog(video=argv[0] if argv else "")
    if check:                   # built fine; don't enter the event loop
        return 0
    dlg.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
