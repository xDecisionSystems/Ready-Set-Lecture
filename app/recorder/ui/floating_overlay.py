"""Small, draggable, capture-excluded recording control surface."""
from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QEvent, QPoint, QPointF, QRectF, Qt, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QGuiApplication, QMouseEvent, QPainter, QPen, QPolygonF, QRadialGradient
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QHBoxLayout, QLabel, QPushButton, QStyle, QStyleOptionButton, QStylePainter, QToolTip, QWidget,
)

from app.recorder.core.audio_meter import AudioLevelMeter
from app.recorder.ui.capture_exclusion import exclude_from_capture
from app.recorder.ui.level_bar import AudioLevelBar

_PAUSE, _RESUME, _RESUMING = "Pause", "Resume", "Resuming…"  # every text the pause button ever shows


class _ClapperButton(QPushButton):
    """The Marker Break button: a drawn clapperboard that lights up for a moment when it is pressed.

    The icon is painted rather than taken from a font, so it looks the same on every machine. The widget is a few
    pixels larger than the button face on every side; the glow spills into that border so it reads as light, not
    just a colour change, and the size never changes while it glows.
    """

    MARGIN = 4
    GLOW_MS = 900
    ICON_WIDTH, ICON_HEIGHT = 22.0, 19.0

    def __init__(self) -> None:
        super().__init__()
        self.setAccessibleName("Marker Break")
        self._glow = 0.0
        self._fade = QVariantAnimation(self)
        self._fade.setStartValue(1.0)
        self._fade.setEndValue(0.0)
        self._fade.setDuration(self.GLOW_MS)
        self._fade.setEasingCurve(QEasingCurve.Type.InQuad)  # stays bright for a beat, then fades away
        self._fade.valueChanged.connect(self._set_glow)
        self.clicked.connect(self.flash)

    def fit_face(self, width: int, height: int) -> None:
        self.setFixedSize(width + 2 * self.MARGIN, height + 2 * self.MARGIN)

    @property
    def glow(self) -> float:
        """0 = dark, 1 = fully lit."""
        return self._glow

    def flash(self) -> None:
        self._fade.stop()
        self._set_glow(1.0)
        self._fade.start()

    def _set_glow(self, value) -> None:
        self._glow = max(0.0, min(1.0, float(value)))
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        m = self.MARGIN
        face = QRectF(self.rect().adjusted(m, m, -m, -m))
        painter = QStylePainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._glow > 0:
            self._paint_halo(painter, face)
        option = QStyleOptionButton()
        self.initStyleOption(option)
        option.rect = face.toRect()
        painter.drawControl(QStyle.ControlElement.CE_PushButton, option)  # the platform button face, like Pause and Stop
        if self._glow > 0:
            self._paint_light(painter, face)
        self._paint_clapperboard(painter, face)

    def _paint_halo(self, painter: QPainter, face: QRectF) -> None:
        painter.setPen(Qt.PenStyle.NoPen)
        for step in range(self.MARGIN, 0, -1):  # widest and faintest layer first
            strength = 1 - (step - 0.5) / self.MARGIN
            painter.setBrush(QColor(255, 196, 50, int(120 * self._glow * strength ** 1.5)))
            painter.drawRoundedRect(face.adjusted(-step, -step, step, step), 4 + step, 4 + step)

    def _paint_light(self, painter: QPainter, face: QRectF) -> None:
        gradient = QRadialGradient(face.center(), face.width() * 0.6)
        gradient.setColorAt(0.0, QColor(255, 248, 200, int(245 * self._glow)))
        gradient.setColorAt(0.6, QColor(255, 205, 70, int(215 * self._glow)))
        gradient.setColorAt(1.0, QColor(255, 170, 20, int(150 * self._glow)))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(gradient)
        painter.drawRoundedRect(face, 4, 4)

    def _paint_clapperboard(self, painter: QPainter, face: QRectF) -> None:
        ink = self.palette().buttonText().color()
        left = face.center().x() - self.ICON_WIDTH / 2 + 1
        top = face.center().y() - self.ICON_HEIGHT / 2
        width = self.ICON_WIDTH - 2
        pen = QPen(ink, 1.5)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(pen)
        painter.drawRoundedRect(QRectF(left + 0.75, top + 9.75, width - 1.5, 8.5), 1.5, 1.5)  # the board
        painter.drawLine(QPointF(left + 4, top + 15.5), QPointF(left + width - 4, top + 15.5))  # a line of writing
        self._striped_bar(painter, QRectF(left, top + 9.4, width, 3.8), ink)  # the fixed stripes on the board
        painter.save()  # the clapper, propped open about its left-hand hinge
        painter.translate(left, top + 9.0)
        painter.rotate(-16)
        self._striped_bar(painter, QRectF(0, -3.6, width, 3.6), ink)
        painter.restore()

    @staticmethod
    def _striped_bar(painter: QPainter, bar: QRectF, ink: QColor) -> None:
        painter.save()
        painter.setClipRect(bar, Qt.ClipOperation.IntersectClip)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(ink)
        slant, stripe, period = bar.height(), 3.0, 6.0
        x = bar.left() - slant
        while x < bar.right():
            painter.drawPolygon(QPolygonF([QPointF(x, bar.bottom()), QPointF(x + stripe, bar.bottom()),
                                           QPointF(x + stripe + slant, bar.top()), QPointF(x + slant, bar.top())]))
            x += period
        painter.restore()
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(ink, 1.3))
        painter.drawRect(bar.adjusted(0.65, 0.65, -0.65, -0.65))


class _GlassesButton(QPushButton):
    """The camera Preview switch: a pair of glasses, lenses lit blue while the preview is showing, struck through
    and dimmed while it is hidden. Painted rather than taken from a font, like the clapperboard."""

    ICON_WIDTH, ICON_HEIGHT = 24.0, 17.0
    LENS_RADIUS = 4.6

    def __init__(self) -> None:
        super().__init__()
        self.setCheckable(True)
        self.setAccessibleName("Camera preview")

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        on = self.isChecked()
        if on:
            self._paint_lit_face()  # painted here, not by the style, so "on" looks the same on every platform theme
        else:
            super().paintEvent(event)  # the platform button face, like Pause and Stop
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        ink = QColor(255, 255, 255) if on else QColor(self.palette().buttonText().color())
        if not on:
            ink.setAlpha(150)
        left = self.rect().center().x() - self.ICON_WIDTH / 2 + 0.5
        top = self.rect().center().y() - self.ICON_HEIGHT / 2 + 0.5
        radius = self.LENS_RADIUS
        lenses = (QPointF(left + 6.4, top + 10.4), QPointF(left + 17.6, top + 10.4))
        pen = QPen(ink, 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(QColor(255, 255, 255, 80) if on else Qt.BrushStyle.NoBrush)  # lit lenses while on
        for centre in lenses:
            painter.drawEllipse(centre, radius, radius)
        painter.drawLine(QPointF(left + 10.9, top + 9.2), QPointF(left + 13.1, top + 9.2))  # the bridge
        painter.drawLine(QPointF(left + 2.9, top + 7.4), QPointF(left + 1.2, top + 3.2))  # the arms
        painter.drawLine(QPointF(left + 21.1, top + 7.4), QPointF(left + 22.8, top + 3.2))
        if on:
            painter.setPen(QPen(QColor(255, 255, 255, 235), 1.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            for centre in lenses:  # a glint on each lens
                painter.drawLine(QPointF(centre.x() - 2.3, centre.y() - 0.4), QPointF(centre.x() - 0.7, centre.y() - 2.3))
        else:
            slash = QPen(self.palette().buttonText().color(), 1.9)
            slash.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(slash)
            painter.drawLine(QPointF(left + 2.0, top + 16.2), QPointF(left + 22.0, top + 2.4))

    def _paint_lit_face(self) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        fill = "#2569c0" if self.isDown() else ("#4a92ea" if self.underMouse() else "#2f7fe0")
        painter.setPen(QPen(QColor("#1d55a0"), 1))
        painter.setBrush(QColor(fill))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(2, 2, -2, -2), 4, 4)  # the platform face is inset about 1.5 px


class _RedSquareButton(QPushButton):
    """Switch for 'turn the screen red while paused': a square that is red when on and grey when off."""

    SIDE = 14.0

    def __init__(self) -> None:
        super().__init__()
        self.setCheckable(True)
        self.setAccessibleName("Red screen while paused")

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        painter = QStylePainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        option = QStyleOptionButton()
        self.initStyleOption(option)
        # Always the ordinary button face (Windows would paint a checked button accent-blue); the square carries the state.
        option.state = (option.state | QStyle.StateFlag.State_Off) & ~QStyle.StateFlag.State_On
        painter.drawControl(QStyle.ControlElement.CE_PushButton, option)
        on = self.isChecked()
        square = QRectF(0, 0, self.SIDE, self.SIDE)
        square.moveCenter(QRectF(self.rect()).center())
        painter.setPen(QPen(QColor("#a51d1d" if on else "#7f8994"), 1.2))
        painter.setBrush(QColor("#e5342f" if on else "#a3adb7"))
        painter.drawRoundedRect(square, 2.5, 2.5)


class _DragGrip(QWidget):
    """A visible grip (two columns of dots) at the overlay's left edge; dragging it moves the whole overlay."""

    def __init__(self, overlay: "FloatingOverlay") -> None:
        super().__init__(overlay)
        self._overlay = overlay
        self.setFixedWidth(16)
        self.setCursor(Qt.CursorShape.SizeAllCursor)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#8b98a5"))
        rows, columns, spacing, radius = 4, 2, 6, 1.6
        left = (self.width() - (columns - 1) * spacing) / 2
        top = (self.height() - (rows - 1) * spacing) / 2
        for row in range(rows):
            for column in range(columns):
                painter.drawEllipse(left + column * spacing - radius, top + row * spacing - radius, radius * 2, radius * 2)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton:
            self._overlay.begin_drag(event.globalPosition().toPoint())
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        if event.buttons() & Qt.MouseButton.LeftButton:
            self._overlay.drag_to(event.globalPosition().toPoint())
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        self._overlay.end_drag()
        event.accept()


class FloatingOverlay(QWidget):
    record_pause_clicked = Signal()
    marker_break_clicked = Signal()
    stop_clicked = Signal()
    hide_cursor_toggled = Signal(bool)  # True = leave the mouse cursor out of the recording
    preview_toggled = Signal(bool)  # True = show the full-screen camera preview
    pause_tint_toggled = Signal(bool)  # True = turn the screen red while the recording is paused

    def __init__(self, audio_meter: AudioLevelMeter, parent=None) -> None:
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # The rounded dark panel is painted by hand in paintEvent: a style-sheet background is never drawn on a frameless
        # translucent window, which left the controls floating directly on the desktop.
        self.setStyleSheet("QLabel, QCheckBox { color: white; } QPushButton { padding: 5px 9px; }")
        self._drag_offset: QPoint | None = None
        self.dot = QLabel("●")
        self.dot.setStyleSheet("color: #43c76b; font-size: 17px;")
        self.dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.elapsed = QLabel("00:00")
        self.pause_button = QPushButton(_PAUSE)
        self.marker_button = _ClapperButton()
        self.stop_button = QPushButton("Stop")
        self.stop_button.setStyleSheet("background: #b94444;")
        self.hide_cursor_checkbox = QCheckBox("Hide cursor")
        self.hide_cursor_checkbox.setVisible(False)  # only meaningful for a screen recording
        self.preview_button = _GlassesButton()
        self.preview_button.setVisible(False)  # only meaningful for a camera recording
        self.pause_tint_button = _RedSquareButton()
        self.level_bar = AudioLevelBar()
        self.level_bar.setMinimumWidth(75)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 3, 10, 3)  # the clapperboard's glow border makes up the rest of the height
        self.grip = _DragGrip(self)
        for widget in (self.grip, self.dot, self.elapsed, self.pause_button, self.marker_button, self.stop_button,
                       self.hide_cursor_checkbox, self.preview_button, self.level_bar, self.pause_tint_button):
            layout.addWidget(widget)
        self._fix_sizes()
        # Qt's own tooltip would be a separate window that the recording could capture, so tips are shown by
        # eventFilter, which hides the tip window from capture. (These widgets deliberately have no toolTip set.)
        self._tips = {self.grip: "Drag to move these controls",
                      self.marker_button: "Marker Break: mark this moment to find it in the editor",
                      self.preview_button: "",
                      self.pause_tint_button: ""}
        self._refresh_preview_tip()
        self._refresh_pause_tint_tip()
        for widget in self._tips:
            widget.installEventFilter(self)
        self.hide_cursor_checkbox.toggled.connect(self.hide_cursor_toggled)
        self.preview_button.toggled.connect(self._preview_button_toggled)
        self.pause_tint_button.toggled.connect(self._pause_tint_button_toggled)
        self.pause_button.clicked.connect(self.record_pause_clicked)
        self.marker_button.clicked.connect(self.marker_break_clicked)
        self.stop_button.clicked.connect(self.stop_clicked)
        audio_meter.level_changed.connect(self.level_bar.set_level)

    def _fix_sizes(self) -> None:
        """Give each widget whose text changes room for its longest text, so nothing moves when the text does.

        Without this the pause button grew from "Pause" to "Resume" to "Resuming…", the panel grew with it, and the
        dot and timer were left stretched over the extra room.
        """
        self.ensurePolished()
        self.pause_button.setFixedWidth(self._widest_hint(self.pause_button, (_PAUSE, _RESUME, _RESUMING)))
        self.dot.setFixedWidth(self._widest_hint(self.dot, ("●", "○")))
        metrics = self.elapsed.fontMetrics()
        digit = max(metrics.horizontalAdvance(str(d)) for d in range(10))
        self.elapsed.setFixedWidth(5 * digit + metrics.horizontalAdvance(":"))  # MMM:SS, room for recordings over 99 minutes
        # A HUD is for the mouse. Keeping keyboard focus off it means a clicker's Space or Enter can never press a
        # focused button on top of the action it was set up to trigger (which would cancel it out).
        for control in (self.pause_button, self.marker_button, self.stop_button, self.preview_button, self.pause_tint_button,
                        self.hide_cursor_checkbox):
            control.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        face_height = self.pause_button.sizeHint().height()
        self.marker_button.fit_face(40, face_height)
        self.preview_button.setFixedSize(40, face_height)
        self.pause_tint_button.setFixedSize(34, face_height)

    @staticmethod
    def _widest_hint(widget: QWidget, texts: tuple[str, ...]) -> int:
        original, widths = widget.text(), []
        for text in texts:
            widget.setText(text)
            widths.append(widget.sizeHint().width())
        widget.setText(original)
        return max(widths)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt override
        if event.type() == QEvent.Type.ToolTip and watched in self._tips:
            QToolTip.showText(event.globalPos(), self._tips[watched], watched)
            for window in QApplication.topLevelWidgets():  # the tip window that was just opened
                if window.windowType() == Qt.WindowType.ToolTip:
                    exclude_from_capture(window)
            return True
        return super().eventFilter(watched, event)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#526171"), 1))
        painter.setBrush(QColor("#202a34"))
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 9, 9)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        exclude_from_capture(self)

    def show_cursor_option(self, hidden: bool) -> None:
        """Offer the 'Hide cursor' checkbox (screen recordings), starting in the state the recording started in."""
        self.hide_cursor_checkbox.blockSignals(True)
        self.hide_cursor_checkbox.setChecked(hidden)
        self.hide_cursor_checkbox.blockSignals(False)
        self.hide_cursor_checkbox.setVisible(True)

    def show_preview_option(self, shown: bool) -> None:
        """Offer the glasses switch for the camera preview (camera recordings), starting in the given state."""
        self.set_preview_checked(shown)
        self.preview_button.setVisible(True)

    def set_preview_checked(self, shown: bool) -> None:
        """Change the switch without announcing it, e.g. when the preview was dismissed by double-click."""
        self.preview_button.blockSignals(True)
        self.preview_button.setChecked(shown)
        self.preview_button.blockSignals(False)
        self._refresh_preview_tip()

    def _preview_button_toggled(self, shown: bool) -> None:
        self._refresh_preview_tip()
        self.preview_toggled.emit(shown)

    def _refresh_preview_tip(self) -> None:
        on = self.preview_button.isChecked()
        self._tips[self.preview_button] = f"Camera preview: {'on. Click to hide it' if on else 'off. Click to show it'}"

    def set_pause_tint_checked(self, on: bool) -> None:
        """Set the red-square switch without announcing it, e.g. to the state remembered from last time."""
        self.pause_tint_button.blockSignals(True)
        self.pause_tint_button.setChecked(on)
        self.pause_tint_button.blockSignals(False)
        self._refresh_pause_tint_tip()

    def _pause_tint_button_toggled(self, on: bool) -> None:
        self._refresh_pause_tint_tip()
        self.pause_tint_toggled.emit(on)

    def _refresh_pause_tint_tip(self) -> None:
        on = self.pause_tint_button.isChecked()
        self._tips[self.pause_tint_button] = f"Screen turns red while paused: {'on. Click to turn it off' if on else 'off. Click to turn it on'}"

    def set_recording_state(self, state: str) -> None:
        recording = state == "recording"
        self.dot.setText("●" if recording else "○")
        self.dot.setStyleSheet(f"color: {'#43c76b' if recording else '#c8d0d8'}; font-size: 17px;")
        self.pause_button.setText(_PAUSE if recording else _RESUME)
        self.pause_button.setEnabled(True)

    def set_resuming(self) -> None:
        """Between pressing Resume and the countdown ending the recording is still paused; the button says so."""
        self.pause_button.setText(_RESUMING)
        self.pause_button.setEnabled(False)

    def set_elapsed(self, seconds: float) -> None:
        total = max(0, round(seconds))
        self.elapsed.setText(f"{total // 60:02d}:{total % 60:02d}")

    def begin_drag(self, global_pos: QPoint) -> None:
        self._drag_offset = global_pos - self.frameGeometry().topLeft()

    def drag_to(self, global_pos: QPoint) -> None:
        """Move with the pointer, but never off the screen: the controls are hidden from the recording, so a
        window dragged out of reach would be hard to find again."""
        if self._drag_offset is None:
            return
        target = global_pos - self._drag_offset
        screen = QGuiApplication.screenAt(global_pos) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        x = max(area.left(), min(target.x(), area.left() + area.width() - self.width()))
        y = max(area.top(), min(target.y(), area.top() + area.height() - self.height()))
        self.move(x, y)

    def end_drag(self) -> None:
        self._drag_offset = None

    # The grip is the visible handle, but the empty background of the overlay drags it too.
    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and not self.childAt(event.position().toPoint()):
            self.begin_drag(event.globalPosition().toPoint())
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.buttons() & Qt.MouseButton.LeftButton:
            self.drag_to(event.globalPosition().toPoint())
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self.end_drag()
