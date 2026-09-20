"""Export settings dialog: output folder (persisted across runs) and
fast/precise mode choice."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
)

from app.core import settings as app_settings
from app.core.exporter import ExportSettings


class ExportDialog(QDialog):
    def __init__(self, default_base_name: str, default_output_dir: Path, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export")
        self.setMinimumWidth(480)

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Output file name (without extension):"))
        self.name_edit = QLineEdit(default_base_name)
        self.name_edit.setToolTip(
            "Multiple output files (from split points) get _part1, _part2, etc. "
            "appended; a single output file gets _edited appended."
        )
        layout.addWidget(self.name_edit)

        layout.addWidget(QLabel("Output folder:"))
        folder_row = QHBoxLayout()
        self.folder_edit = QLineEdit(str(default_output_dir))
        browse_button = QPushButton("Browse…")
        browse_button.clicked.connect(self._browse)
        folder_row.addWidget(self.folder_edit, stretch=1)
        folder_row.addWidget(browse_button)
        layout.addLayout(folder_row)

        layout.addWidget(QLabel("Cut precision:"))
        self.fast_radio = QRadioButton("Fast (stream copy) — near-instant, cuts may drift up to ~1-2s")
        self.precise_radio = QRadioButton("Precise (re-encode) — exact cuts, much slower on 4K")
        self.fast_radio.setChecked(True)
        layout.addWidget(self.fast_radio)
        layout.addWidget(self.precise_radio)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._default_base_name = default_base_name

    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Choose output folder", self.folder_edit.text())
        if chosen:
            self.folder_edit.setText(chosen)

    def export_settings(self) -> ExportSettings:
        output_dir = Path(self.folder_edit.text())
        app_settings.set_output_dir(output_dir)

        base_name = self.name_edit.text().strip()
        for invalid_char in '<>:"/\\|?*':
            base_name = base_name.replace(invalid_char, "_")
        if not base_name:
            base_name = self._default_base_name

        return ExportSettings(
            output_dir=output_dir,
            precise=self.precise_radio.isChecked(),
            base_name=base_name,
        )
