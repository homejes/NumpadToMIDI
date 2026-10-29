"""NumpadToMIDI - turn a 23-key USB numpad into a (virtual) MIDI controller."""
from __future__ import annotations

import ctypes
import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import (
    QAbstractNativeEventFilter, QObject, QSize, QTimer, Qt, QVariantAnimation, pyqtSignal,
)
from PyQt6.QtGui import (
    QAction, QCloseEvent, QColor, QFont, QFontDatabase, QIcon, QPainter, QPen, QPixmap,
)
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QMainWindow, QMenu, QPlainTextEdit, QPushButton, QSizePolicy, QSlider,
    QSystemTrayIcon, QVBoxLayout, QWidget,
)

from key_hook import KEY_DISPLAY, KEY_INDEX, KEY_LAYOUT, KEY_NAMES, KEY_SPAN, KeyHook
from midi_engine import MidiEngine, note_name
from version import VERSION

if sys.platform == "win32":
    from ctypes import wintypes

APP_NAME = "NumpadToMIDI"
OCTAVE_MIN, OCTAVE_MAX = -3, 5

# Win32 WM_APPCOMMAND constants (0x0319)
WM_APPCOMMAND = 0x0319
APPCOMMAND_BROWSER_SEARCH = 5
APPCOMMAND_BROWSER_HOME = 7
APPCOMMAND_LAUNCH_MAIL = 15
APPCOMMAND_LAUNCH_APP1 = 17
APPCOMMAND_LAUNCH_APP2 = 18
APPCOMMAND_BROWSER_HOME_ALT = 19

APPCOMMAND_TO_KEY: dict[int, str] = {
    APPCOMMAND_BROWSER_SEARCH: "Home",
    APPCOMMAND_BROWSER_HOME: "Home",
    APPCOMMAND_BROWSER_HOME_ALT: "Home",
    APPCOMMAND_LAUNCH_MAIL: "Email",
    APPCOMMAND_LAUNCH_APP1: "Calc",
    APPCOMMAND_LAUNCH_APP2: "Calc",
}


def get_appcommand_lparam(lparam: int) -> int:
    """Win32 GET_APPCOMMAND_LPARAM(lParam) macro."""
    return (int(lparam) >> 16) & 0xFFF


# =============================================================================
# Configuration
# =============================================================================
def resource_path(name: str) -> Path:
    """Locate a bundled resource (works from source and from PyInstaller)."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / name


def user_config_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path.home() / ".config" / APP_NAME


class Config:
    DEFAULTS = {
        "base_note": 36,
        "octave": 0,
        "channel": 1,
        "velocity": 100,
        "port_name": "",
        "suppress_keys": True,
        "exclusive_mode": True,
        "triple_zero_detection": True,
        "burst_window_ms": 35,
    }

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or (user_config_dir() / "config.json")
        self.data: dict = dict(self.DEFAULTS)
        user_has_exclusive = False
        for source in (resource_path("config.json"), self.path):
            try:
                loaded = json.loads(Path(source).read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    if source == self.path and "exclusive_mode" in loaded:
                        user_has_exclusive = True
                    self.data.update({k: v for k, v in loaded.items() if k in self.DEFAULTS})
            except (OSError, ValueError):
                pass
        # Upgrade path from v1.0/v1.1: if the user config pre-dates `exclusive_mode`,
        # default Exclusive Mode (and `suppress_keys`) to True.
        if not user_has_exclusive:
            self.data["exclusive_mode"] = True
            self.data["suppress_keys"] = True
        else:
            self.data["suppress_keys"] = bool(self.data["exclusive_mode"])
        self._sanitize()

    def _sanitize(self) -> None:
        d = self.data
        d["base_note"] = max(0, min(127, int(d["base_note"])))
        d["octave"] = max(OCTAVE_MIN, min(OCTAVE_MAX, int(d["octave"])))
        d["channel"] = max(1, min(16, int(d["channel"])))
        d["velocity"] = max(1, min(127, int(d["velocity"])))
        d["burst_window_ms"] = max(5, min(200, float(d["burst_window_ms"])))
        d["exclusive_mode"] = bool(d["exclusive_mode"])
        d["suppress_keys"] = bool(d["suppress_keys"])

    def __getitem__(self, key: str):
        return self.data[key]

    def __setitem__(self, key: str, value) -> None:
        self.data[key] = value

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        except OSError:
            pass


# =============================================================================
# Controller: key events -> MIDI + UI signals
# =============================================================================
class Controller(QObject):
    key_state = pyqtSignal(str, bool)   # key name, is_down
    mute_changed = pyqtSignal(bool)     # is_muted
    log = pyqtSignal(str)

    def __init__(self, config: Config, engine: MidiEngine) -> None:
        super().__init__()
        self.config = config
        self.engine = engine
        self.muted = False
        self._lock = threading.Lock()
        self._active: dict[str, tuple[int, int]] = {}  # key -> (note, channel)
        self.hook = KeyHook(
            self._on_hook_key,
            detect_triple_zero=bool(config["triple_zero_detection"]),
            burst_window_ms=float(config["burst_window_ms"]),
            suppress=bool(config["suppress_keys"]),
        )

    # ---- mapping -------------------------------------------------------------
    def note_for(self, name: str) -> Optional[int]:
        note = self.config["base_note"] + self.config["octave"] * 12 + KEY_INDEX[name]
        return note if 0 <= note <= 127 else None

    # ---- events --------------------------------------------------------------
    def _on_hook_key(self, name: str, down: bool) -> None:
        self.handle_key(name, down, "key")

    def handle_key(self, name: str, down: bool, source: str = "key") -> None:
        """Called from the hook thread or the GUI (mouse). Thread-safe."""
        with self._lock:
            if down:
                if name in self._active:
                    return
                note = self.note_for(name)
                if note is None:
                    self.log.emit(f"{self._ts()}  {name}: note out of range (change octave)")
                    return
                channel, velocity = self.config["channel"], self.config["velocity"]
                self._active[name] = (note, channel)
                if not self.muted:
                    self.engine.note_on(channel, note, velocity)
                label = "MUTE ON " if self.muted else "NOTE ON "
            else:
                entry = self._active.pop(name, None)
                if entry is None:
                    return
                note, channel = entry
                velocity = 0
                if not self.muted:
                    self.engine.note_off(channel, note)
                label = "MUTE OFF" if self.muted else "NOTE OFF"
        self.key_state.emit(name, down)
        self.log.emit(
            f"{self._ts()}  ch{channel:>2}  {label}  {note:>3} {note_name(note):<4} "
            f"vel {velocity:>3}   <- {name} ({source})"
        )

    def handle_appcommand(self, cmd: int) -> bool:
        """Consume Win32 WM_APPCOMMAND for Browser/Mail/Calc keys and trigger MIDI if needed."""
        name = APPCOMMAND_TO_KEY.get(cmd)
        if name is None:
            return False
        with self._lock:
            already_active = name in self._active
        if not already_active:
            self.handle_key(name, True, "appcmd")
            QTimer.singleShot(90, lambda n=name: self.handle_key(n, False, "appcmd"))
        return True

    @staticmethod
    def _ts() -> str:
        now = time.time()
        return time.strftime("%H:%M:%S", time.localtime(now)) + f".{int(now * 1000) % 1000:03d}"

    # ---- settings ------------------------------------------------------------
    def set_channel(self, channel: int) -> None:
        self.config["channel"] = max(1, min(16, channel))

    def set_octave(self, octave: int) -> None:
        self.config["octave"] = max(OCTAVE_MIN, min(OCTAVE_MAX, octave))

    def set_velocity(self, velocity: int) -> None:
        self.config["velocity"] = max(1, min(127, velocity))

    def set_suppress(self, value: bool) -> None:
        enabled = bool(value)
        self.config["suppress_keys"] = enabled
        self.config["exclusive_mode"] = enabled
        self.hook.set_suppress(enabled)

    def set_muted(self, value: bool) -> None:
        muted = bool(value)
        if self.muted == muted:
            return
        if muted:
            self.panic()
        self.muted = muted
        self.mute_changed.emit(muted)
        self.log.emit(f"{self._ts()}  MIDI {'muted' if muted else 'unmuted'}")

    def panic(self) -> None:
        with self._lock:
            held = list(self._active.items())
            self._active.clear()
        for name, (note, channel) in held:
            self.engine.note_off(channel, note)
            self.key_state.emit(name, False)
        self.engine.all_notes_off()
        self.log.emit(f"{self._ts()}  PANIC - all notes off")

    def shutdown(self) -> None:
        self.hook.stop()
        self.panic()
        self.config.save()
        self.engine.close()


class WinAppCommandFilter(QAbstractNativeEventFilter):
    """Application-wide native event filter that traps WM_APPCOMMAND (0x0319) on Windows."""

    def __init__(self, ctrl: Controller) -> None:
        super().__init__()
        self.ctrl = ctrl

    def nativeEventFilter(self, eventType, message):
        if sys.platform == "win32" and message:
            try:
                msg = wintypes.MSG.from_address(int(message))
                if msg.message == WM_APPCOMMAND:
                    cmd = get_appcommand_lparam(msg.lParam)
                    if self.ctrl.handle_appcommand(cmd):
                        return True, 1
            except Exception:
                pass
        return False, 0


# =============================================================================
# UI
# =============================================================================
STYLE = """
* { font-family: "Segoe UI", "SF Pro Text", "Helvetica Neue", Arial; font-size: 13px; }
QMainWindow, QWidget#root { background: #0f1117; }
QLabel { color: #c9d1e0; }
QLabel#title { font-size: 22px; font-weight: 700; color: #ffffff; }
QLabel#subtitle { color: #6b7592; }
QLabel#section { color: #6b7592; font-size: 11px; font-weight: 700; letter-spacing: 1px; }
QLabel#value { color: #ffffff; font-weight: 600; min-width: 90px; }
QFrame#card { background: #161a24; border: 1px solid #232938; border-radius: 12px; }
QPushButton { background: #232938; color: #e6ebf5; border: 1px solid #2d3447;
              border-radius: 8px; padding: 6px 14px; }
QPushButton:hover { background: #2d3447; }
QPushButton:pressed { background: #3a4360; }
QPushButton#danger { background: #3a1f28; border-color: #5c2c3a; color: #ff9bb0; }
QPushButton#danger:hover { background: #4b2531; }
QPushButton#step { padding: 4px 0px; min-width: 34px; font-weight: 700; font-size: 15px; }
QComboBox { background: #1c2130; color: #e6ebf5; border: 1px solid #2d3447;
            border-radius: 8px; padding: 5px 10px; min-width: 70px; }
QComboBox QAbstractItemView { background: #1c2130; color: #e6ebf5; selection-background-color: #3a4360; }
QCheckBox { color: #c9d1e0; spacing: 8px; font-weight: 600; }
QMenu { background: #161a24; color: #e6ebf5; border: 1px solid #2d3447; padding: 4px; }
QMenu::item { padding: 6px 20px; border-radius: 6px; }
QMenu::item:selected { background: #2d3447; }
QSlider::groove:horizontal { height: 6px; background: #232938; border-radius: 3px; }
QSlider::sub-page:horizontal { background: #7c5cff; border-radius: 3px; }
QSlider::handle:horizontal { background: #ffffff; width: 16px; margin: -6px 0; border-radius: 8px; }
QPlainTextEdit { background: #0b0d13; color: #8fe3b0; border: 1px solid #232938;
                 border-radius: 10px; padding: 8px; selection-background-color: #3a4360; }
QScrollBar:vertical { background: #0b0d13; width: 10px; }
QScrollBar::handle:vertical { background: #2d3447; border-radius: 5px; min-height: 24px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""


def make_app_icon() -> QIcon:
    """Render a crisp 64x64 pad-grid icon for the window and system tray."""
    pix = QPixmap(64, 64)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#161a24"))
    p.setPen(QPen(QColor("#7c5cff"), 3))
    p.drawRoundedRect(4, 4, 56, 56, 14, 14)
    colors = ("#7c5cff", "#3ddc84", "#ffb454", "#ff6b81")
    for idx in range(9):
        r, c = divmod(idx, 3)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(colors[(r + c) % len(colors)]))
        p.drawRoundedRect(13 + c * 14, 13 + r * 14, 10, 10, 3, 3)
    p.end()
    return QIcon(pix)


def mix(a: QColor, b: QColor, t: float) -> QColor:
    t = max(0.0, min(1.0, t))
    return QColor(
        int(a.red() + (b.red() - a.red()) * t),
        int(a.green() + (b.green() - a.green()) * t),
        int(a.blue() + (b.blue() - a.blue()) * t),
    )


class PadWidget(QWidget):
    """One lit pad of the grid. Glows while held and fades out on release."""

    pressed = pyqtSignal(str)
    released = pyqtSignal(str)

    def __init__(
        self,
        name: str,
        index: int,
        display: Optional[str] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.name = name
        self.display = display or KEY_DISPLAY.get(name, name)
        self._note_text = ""
        self._glow = 0.0
        self._color = QColor.fromHsv(int(index * 360 / len(KEY_NAMES)) % 360, 170, 255)
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(380)
        self._anim.valueChanged.connect(self._set_glow)
        self.setMinimumSize(78, 78)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def sizeHint(self) -> QSize:
        return QSize(96, 96)

    def set_note_text(self, text: str) -> None:
        self._note_text = text
        self.update()

    def light(self, on: bool) -> None:
        self._anim.stop()
        if on:
            self._glow = 1.0
        else:
            self._anim.setStartValue(float(self._glow))
            self._anim.setEndValue(0.0)
            self._anim.start()
        self.update()

    def _set_glow(self, value) -> None:
        self._glow = float(value)
        self.update()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.pressed.emit(self.name)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.released.emit(self.name)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(4, 4, -4, -4)
        base, edge = QColor("#1b2030"), QColor("#2a3145")
        g = self._glow
        if g > 0.01:  # soft halo
            for i, alpha in ((6, 28), (4, 46), (2, 70)):
                halo = QColor(self._color)
                halo.setAlpha(int(alpha * g))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(halo)
                p.drawRoundedRect(rect.adjusted(-i // 2, -i // 2, i // 2, i // 2), 14, 14)
        p.setBrush(mix(base, self._color, g * 0.92))
        p.setPen(QPen(mix(edge, self._color, g), 1.5))
        p.drawRoundedRect(rect, 12, 12)

        text_color = mix(QColor("#d6dcec"), QColor("#0b0d14"), g)
        p.setPen(text_color)
        big = QFont(self.font())
        big.setBold(True)
        cell_dim = min(rect.width(), rect.height())
        divisor = 3 if len(self.display) < 4 else (5 if len(self.display) < 8 else 6)
        big.setPixelSize(max(11, cell_dim // divisor))
        p.setFont(big)
        p.drawText(rect.adjusted(4, 0, -4, -rect.height() // 5), Qt.AlignmentFlag.AlignCenter, self.display)
        small = QFont(self.font())
        small.setPixelSize(max(9, cell_dim // 8))
        p.setFont(small)
        sub = mix(QColor("#6b7592"), QColor("#1a1d29"), g)
        p.setPen(sub)
        p.drawText(rect.adjusted(0, 0, 0, -6), Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom,
                   self._note_text)
        p.end()


class MainWindow(QMainWindow):
    def __init__(self, ctrl: Controller) -> None:
        super().__init__()
        self.ctrl = ctrl
        self._quitting = False
        self.setWindowTitle(f"NumpadToMIDI {VERSION}")
        self.setWindowIcon(make_app_icon())
        self.resize(1040, 680)
        self.setMinimumSize(880, 600)
        self._pads: dict[str, PadWidget] = {}
        self._build()
        self._setup_tray()
        ctrl.key_state.connect(self._on_key_state)
        ctrl.mute_changed.connect(self._on_mute_changed)
        ctrl.log.connect(self._append_log)
        self._refresh_notes()
        self._refresh_status()

    # ---- construction --------------------------------------------------------
    def _card(self) -> tuple[QFrame, QVBoxLayout]:
        frame = QFrame()
        frame.setObjectName("card")
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)
        return frame, lay

    @staticmethod
    def _section(text: str) -> QLabel:
        label = QLabel(text.upper())
        label.setObjectName("section")
        return label

    def _build(self) -> None:
        cfg = self.ctrl.config
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(22, 18, 22, 18)
        outer.setSpacing(14)

        # header
        head = QHBoxLayout()
        titles = QVBoxLayout()
        title = QLabel("NumpadToMIDI")
        title.setObjectName("title")
        sub = QLabel("23-key M-Tech USB numpad  \u2192  MIDI  (Chromatic Drum Rack layout, C1 = 36)")
        sub.setObjectName("subtitle")
        titles.addWidget(title)
        titles.addWidget(sub)
        head.addLayout(titles)
        head.addStretch(1)
        self.status = QLabel()
        head.addWidget(self.status)
        outer.addLayout(head)

        body = QHBoxLayout()
        body.setSpacing(14)
        outer.addLayout(body, 1)

        # pad grid (6 rows x 4 columns; Enter spans rows 4-5 in column 3)
        pads_card, pads_lay = self._card()
        pads_lay.addWidget(self._section("Pads (M-Tech 23-Key Layout)"))
        grid = QGridLayout()
        grid.setSpacing(6)
        for index, (name, row, col) in enumerate(KEY_LAYOUT):
            pad = PadWidget(name, index)
            pad.pressed.connect(lambda n=name: self.ctrl.handle_key(n, True, "mouse"))
            pad.released.connect(lambda n=name: self.ctrl.handle_key(n, False, "mouse"))
            rspan, cspan = KEY_SPAN.get(name, (1, 1))
            grid.addWidget(pad, row, col, rspan, cspan)
            self._pads[name] = pad
        for r in range(6):
            grid.setRowStretch(r, 1)
        for c in range(4):
            grid.setColumnStretch(c, 1)
        pads_lay.addLayout(grid, 1)
        body.addWidget(pads_card, 5)

        # right column
        right = QVBoxLayout()
        right.setSpacing(14)
        body.addLayout(right, 4)

        ctl_card, ctl = self._card()
        ctl.addWidget(self._section("Controls"))
        form = QGridLayout()
        form.setVerticalSpacing(12)
        form.setHorizontalSpacing(10)

        form.addWidget(QLabel("MIDI channel"), 0, 0)
        self.channel = QComboBox()
        self.channel.addItems([str(i) for i in range(1, 17)])
        self.channel.setCurrentIndex(cfg["channel"] - 1)
        self.channel.currentIndexChanged.connect(lambda i: self.ctrl.set_channel(i + 1))
        form.addWidget(self.channel, 0, 1, 1, 3)

        form.addWidget(QLabel("Octave shift"), 1, 0)
        minus, plus = QPushButton("\u2212"), QPushButton("+")
        for b in (minus, plus):
            b.setObjectName("step")
        self.octave_label = QLabel()
        self.octave_label.setObjectName("value")
        self.octave_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        minus.clicked.connect(lambda: self._shift_octave(-1))
        plus.clicked.connect(lambda: self._shift_octave(+1))
        form.addWidget(minus, 1, 1)
        form.addWidget(self.octave_label, 1, 2)
        form.addWidget(plus, 1, 3)

        form.addWidget(QLabel("Velocity"), 2, 0)
        self.velocity = QSlider(Qt.Orientation.Horizontal)
        self.velocity.setRange(1, 127)
        self.velocity.setValue(cfg["velocity"])
        self.velocity_label = QLabel(str(cfg["velocity"]))
        self.velocity_label.setObjectName("value")
        self.velocity_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.velocity.valueChanged.connect(self._on_velocity)
        form.addWidget(self.velocity, 2, 1, 1, 2)
        form.addWidget(self.velocity_label, 2, 3)

        form.addWidget(QLabel("MIDI output"), 3, 0)
        self.port = QComboBox()
        self.port.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.port.setMinimumContentsLength(14)
        self.port.activated.connect(self._on_port_chosen)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self._reopen_ports)
        form.addWidget(self.port, 3, 1, 1, 2)
        form.addWidget(refresh, 3, 3)
        ctl.addLayout(form)

        self.suppress = QCheckBox("Exclusive Mode (Block OS Keystrokes)")
        self.suppress.setChecked(bool(cfg["suppress_keys"]))
        self.suppress.toggled.connect(self.ctrl.set_suppress)
        ctl.addWidget(self.suppress)

        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.hint.setObjectName("subtitle")
        ctl.addWidget(self.hint)
        right.addWidget(ctl_card)

        mon_card, mon = self._card()
        top = QHBoxLayout()
        top.addWidget(self._section("MIDI monitor"))
        top.addStretch(1)
        panic = QPushButton("Panic")
        panic.setObjectName("danger")
        panic.clicked.connect(self.ctrl.panic)
        clear = QPushButton("Clear")
        clear.clicked.connect(lambda: self.monitor.clear())
        top.addWidget(panic)
        top.addWidget(clear)
        mon.addLayout(top)
        self.monitor = QPlainTextEdit()
        self.monitor.setReadOnly(True)
        self.monitor.setMaximumBlockCount(600)
        self.monitor.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.monitor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        mon.addWidget(self.monitor, 1)
        right.addWidget(mon_card, 1)

    # ---- system tray & native events -----------------------------------------
    def nativeEvent(self, eventType, message):
        if sys.platform == "win32" and message:
            try:
                msg = wintypes.MSG.from_address(int(message))
                if msg.message == WM_APPCOMMAND:
                    cmd = get_appcommand_lparam(msg.lParam)
                    if self.ctrl.handle_appcommand(cmd):
                        return True, 1
            except Exception:
                pass
        return super().nativeEvent(eventType, message)

    def _setup_tray(self) -> None:
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        self.tray.setToolTip(f"{APP_NAME} {VERSION}")

        self.tray_menu = QMenu(self)
        self.action_open = QAction("Open NumpadToMIDI", self)
        self.action_open.triggered.connect(self.restore_from_tray)
        self.tray_menu.addAction(self.action_open)

        self.action_mute = QAction("Mute MIDI", self)
        self.action_mute.setCheckable(True)
        self.action_mute.setChecked(self.ctrl.muted)
        self.action_mute.toggled.connect(self.ctrl.set_muted)
        self.tray_menu.addAction(self.action_mute)

        self.tray_menu.addSeparator()

        self.action_quit = QAction("Quit NumpadToMIDI", self)
        self.action_quit.triggered.connect(self.quit_app)
        self.tray_menu.addAction(self.action_quit)

        self.tray.setContextMenu(self.tray_menu)
        self.tray.activated.connect(self._on_tray_activated)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.restore_from_tray()

    def _on_mute_changed(self, muted: bool) -> None:
        self.action_mute.blockSignals(True)
        self.action_mute.setChecked(muted)
        self.action_mute.blockSignals(False)

    def restore_from_tray(self) -> None:
        self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    def quit_app(self) -> None:
        self._quitting = True
        self.tray.hide()
        QApplication.quit()

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self._quitting and QSystemTrayIcon.isSystemTrayAvailable() and self.tray.isVisible():
            event.ignore()
            self.hide()
            self.tray.showMessage(
                APP_NAME,
                "NumpadToMIDI is running in the system tray",
                QSystemTrayIcon.MessageIcon.Information,
                3000,
            )
            return
        event.accept()

    # ---- behaviour -----------------------------------------------------------
    def _shift_octave(self, delta: int) -> None:
        self.ctrl.panic()  # avoid hanging notes when the mapping moves
        self.ctrl.set_octave(self.ctrl.config["octave"] + delta)
        self._refresh_notes()

    def _on_velocity(self, value: int) -> None:
        self.ctrl.set_velocity(value)
        self.velocity_label.setText(str(value))

    def _on_port_chosen(self, index: int) -> None:
        name = self.port.itemData(index)
        self.ctrl.panic()
        if name:
            self.ctrl.engine.open_port_by_name(name)
            self.ctrl.config["port_name"] = name
        elif self.ctrl.engine.supports_virtual:  # "built-in virtual port" entry
            self.ctrl.config["port_name"] = ""
            self.ctrl.engine.open_auto("")
        self._refresh_status()

    def _reopen_ports(self) -> None:
        self.ctrl.panic()
        self.ctrl.engine.open_auto(self.ctrl.config["port_name"])
        self._refresh_status()

    def _refresh_notes(self) -> None:
        cfg = self.ctrl.config
        base = cfg["base_note"] + cfg["octave"] * 12
        self.octave_label.setText(f"{cfg['octave']:+d}  ({note_name(max(0, min(127, base)))})")
        for name, pad in self._pads.items():
            note = self.ctrl.note_for(name)
            pad.set_note_text("--" if note is None else f"{note_name(note)} \u00b7 {note}")

    def _refresh_status(self) -> None:
        engine = self.ctrl.engine
        color = {"virtual": "#3ddc84", "loopback": "#3ddc84", "fallback": "#ffb454"}.get(engine.mode, "#ff6b81")
        self.status.setText(f'<span style="color:{color}">\u25cf</span>&nbsp; {engine.description}')
        self.status.setStyleSheet("color:#c9d1e0; font-weight:600;")

        self.port.blockSignals(True)
        self.port.clear()
        ports = engine.list_ports()
        if sys.platform == "darwin":  # macOS: always the native virtual source
            self.port.addItem(f"Virtual: {engine.port_name or engine.CLIENT_NAME}", None)
            self.port.setEnabled(False)
        else:
            self.port.setEnabled(True)
            if engine.supports_virtual:
                self.port.addItem(f"Virtual port: {engine.CLIENT_NAME} (built-in)", None)
            for name in ports:
                self.port.addItem(name, name)
            if engine.mode == "virtual":
                self.port.setCurrentIndex(0)
            elif engine.port_name in ports:
                self.port.setCurrentIndex(ports.index(engine.port_name) + (1 if engine.supports_virtual else 0))
            if self.port.count() == 0:
                self.port.addItem("(no MIDI outputs found)", None)
        self.port.blockSignals(False)

        if engine.mode == "virtual":
            self.hint.setText(
                f"Select \u201c{engine.port_name}\u201d as a MIDI input in your DAW."
            )
        elif sys.platform == "win32":
            self.hint.setText(
                "The virtual MIDI driver is not installed. Re-run the NumpadToMIDI installer "
                "(it installs the driver), or install loopMIDI, create a port and press Refresh."
            )
        else:
            self.hint.setText("")

    def _on_key_state(self, name: str, down: bool) -> None:
        pad = self._pads.get(name)
        if pad is not None:
            pad.light(down)

    def _append_log(self, text: str) -> None:
        self.monitor.appendPlainText(text)


# =============================================================================
# Entry points
# =============================================================================
def build_app(start_hook: bool = True) -> tuple[QApplication, Controller, MainWindow]:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyleSheet(STYLE)
    if QSystemTrayIcon.isSystemTrayAvailable():
        app.setQuitOnLastWindowClosed(False)
    config = Config()
    engine = MidiEngine()
    engine.open_auto(config["port_name"])
    ctrl = Controller(config, engine)
    win = MainWindow(ctrl)
    # Keep a reference to the native event filter on the window so it isn't GC'd
    win._native_filter = WinAppCommandFilter(ctrl)
    app.installNativeEventFilter(win._native_filter)
    app.aboutToQuit.connect(ctrl.shutdown)
    ctrl.log.emit(f"{ctrl._ts()}  {engine.description}")
    if start_hook:
        ctrl.hook.start()
        if sys.platform == "darwin":
            ctrl.log.emit(
                f"{ctrl._ts()}  macOS: if keys are not detected, grant Input Monitoring / "
                "Accessibility to this app in System Settings > Privacy & Security."
            )
    return app, ctrl, win


def selftest() -> int:
    """Headless smoke test used by CI against the frozen binary."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    lines: list[str] = []
    code = 0
    try:
        app, ctrl, win = build_app(start_hook=False)
        ctrl.log.connect(lines.append)
        win.show()
        ctrl.handle_key("7", True)
        ctrl.handle_key("7", False)
        assert ctrl.handle_appcommand(APPCOMMAND_BROWSER_HOME_ALT) is True
        assert ctrl.handle_appcommand(APPCOMMAND_LAUNCH_MAIL) is True
        assert ctrl.handle_appcommand(APPCOMMAND_LAUNCH_APP2) is True
        app.processEvents()
        assert len(win._pads) == len(KEY_NAMES) == 23
        assert ctrl.note_for("0") == 36
        assert ctrl.note_for("Home") == 55
        assert ctrl.note_for("Calc") == 58
        assert win.suppress.isChecked() is True
        assert [a.text() for a in win.tray_menu.actions() if not a.isSeparator()] == [
            "Open NumpadToMIDI",
            "Mute MIDI",
            "Quit NumpadToMIDI",
        ]
        lines.append(f"SELFTEST OK ({ctrl.engine.description})")
        ctrl.shutdown()
    except Exception:
        code = 1
        lines.append("SELFTEST FAILED\n" + traceback.format_exc())
    try:
        Path("selftest.log").write_text("\n".join(lines), encoding="utf-8")
    except OSError:
        pass
    return code


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    app, ctrl, win = build_app()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
