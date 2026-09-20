from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from app.core.video_encoder import prefetch_encoders
from app.ui.main_window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    prefetch_encoders()  # so the first precise export doesn't wait to find out whether the graphics chip can encode
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
