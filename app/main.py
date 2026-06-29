"""Entry point — python -m app.main (or acqapp CLI once packaged)."""
import sys

from PyQt6.QtWidgets import QApplication

from .main_window import AcqMainWindow


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("acqApp")
    win = AcqMainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
