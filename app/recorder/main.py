"""Standalone entry point for Ready, Set, Lecture! Recorder."""
from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from app.recorder.ui.setup_window import RecorderMainWindow


def main() -> int:
    app = QApplication(sys.argv)
    window = RecorderMainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
