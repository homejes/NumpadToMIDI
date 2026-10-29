"""NumpadToMIDI - turn a 23-key USB numpad into a (virtual) MIDI controller."""
from __future__ import annotations

import ctypes
import json
import logging
import os
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import Optional

APP_NAME = "NumpadToMIDI"
LOG_FILE_TEMP = os.path.join(tempfile.gettempdir(), "NumpadToMIDI.log")
LOG_FILE_APPDATA = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME, "debug.log"
)


def _setup_crash_logging() -> logging.Logger:
    """Configure file logging and fatal exception hooks before any GUI imports."""
    log = logging.getLogger(APP_NAME)
    if log.handlers:
        return log
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    for log_path in (LOG_FILE_TEMP, LOG_FILE_APPDATA):
        try:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            fh = logging.FileHandler(log_path, encoding="utf-8")
            fh.setFormatter(fmt)
            log.addHandler(fh)
        except Exception:
            pass
    if not log.handlers:
        log.addHandler(logging.NullHandler())
    return log


logger = _setup_crash_logging()


def _show_fatal_dialog(title: str, details: str) -> None:
    """Display a native Windows MessageBoxW error dialog so crashes are never silent."""
    if "--selftest" in sys.argv:
        return
    if sys.platform == "win32":
        try:
            msg = (
                f"{details}\n\n"
                f"Diagnostic log saved to:\n{LOG_FILE_TEMP}\n{LOG_FILE_APPDATA}"
            )
            ctypes.windll.user32.MessageBoxW(0, msg[:3500], title, 0x00000010)
        except Exception:
            pass


def _excepthook(exc_type, exc_value, exc_tb) -> None:
    tb_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    logger.critical("Unhandled fatal exception:\n%s", tb_text)
    _show_fatal_dialog(f"{APP_NAME} Fatal Error", tb_text)


def _threading_excepthook(args) -> None:
    tb_text = "".join(
        traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)
    )
    logger.critical("Unhandled thread exception (%s):\n%s", getattr(args, "thread", None), tb_text)


sys.excepthook = _excepthook
threading.excepthook = _threading_excepthook

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

from key_hook import (
    ALL_DEVICES_ID, ALL_DEVICES_LABEL, KEY_DISPLAY, KEY_INDEX, KEY_LAYOUT, KEY_NAMES,
    KEY_SPAN, KeyHook,
)
from midi_engine import MidiEngine, note_name
from version import VERSION

if sys.platform == "win32":
    from ctypes import wintypes

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
        return Path(os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME))
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path.home() / ".config" / APP_NAME


def user_config_path() -> Path:
    if sys.platform == "win32":
        return Path(
            os.path.join(
                os.environ.get("APPDATA", os.path.expanduser("~")),
                APP_NAME,
                "config.json",
            )
        )
    return user_config_dir() / "config.json"


class Config:
    DEFAULTS = {
        "base_note": 36,
        "octave": 0,
        "channel": 1,
        "velocity": 100,
        "port_name": "",
        "target_device": "",
        "suppress_keys": True,
        "exclusive_mode": True,
        "exclusive_pad_mode": False,
        "triple_zero_detection": True,
        "burst_window_ms": 50,
    }

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else user_config_path()
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
        d["burst_window_ms"] = max(50.0, min(200.0, float(d["burst_window_ms"])))
        d["target_device"] = str(d.get("target_device", "") or "")
        d["exclusive_mode"] = bool(d["exclusive_mode"])
        d["suppress_keys"] = bool(d["suppress_keys"])
        d["exclusive_pad_mode"] = bool(d.get("exclusive_pad_mode", False))

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
    exclusive_pad_mode_changed = pyqtSignal(bool)  # is_exclusive_pad_mode
    log = pyqtSignal(str)

    def __init__(self, config: Config, engine: MidiEngine) -> None:
        super().__init__()
        self.config = config
        self.engine = engine
        self.muted = False
        self._lock = threading.Lock()
        self._active: dict[str, tuple[int, int]] = {}  # key -> (note, channel)
        self._physically_down: set[str] = set()
        self._dot_held = False
        self._active_choke_key: Optional[str] = None
        self.engine.set_exclusive_pad_mode(bool(config["exclusive_pad_mode"]))
        self.hook = KeyHook(
            self._on_hook_key,
            detect_triple_zero=bool(config["triple_zero_detection"]),
            burst_window_ms=float(config["burst_window_ms"]),
            suppress=bool(config["suppress_keys"]),
            target_device=str(config["target_device"]),
        )

    @property
    def exclusive_pad_mode(self) -> bool:
        return bool(self.config["exclusive_pad_mode"])

    @property
    def currently_active_note(self) -> Optional[int]:
        return self.engine.currently_active_note

    # ---- mapping -------------------------------------------------------------
    TRIPLE_ZERO_VELOCITY = 127

    def _velocity_for(self, name: str) -> int:
        """'000' always fires at full velocity (127); other pads use the velocity slider."""
        if name == "000":
            return self.TRIPLE_ZERO_VELOCITY
        return int(self.config["velocity"])

    def note_for(self, name: str) -> Optional[int]:
        if name == ".":
            return None
        note = self.config["base_note"] + self.config["octave"] * 12 + KEY_INDEX[name]
        return note if 0 <= note <= 127 else None

    # ---- events --------------------------------------------------------------
    def _on_hook_key(self, name: str, down: bool) -> None:
        self.handle_key(name, down, "key")

    def handle_key(self, name: str, down: bool, source: str = "key") -> None:
        """Called from the hook thread or the GUI (mouse). Thread-safe."""
        if name == ".":
            with self._lock:
                if down:
                    if self._dot_held:
                        return
                    self._dot_held = True
                else:
                    self._dot_held = False
                    return
            self.toggle_exclusive_pad_mode(source=source)
            return

        if self.exclusive_pad_mode:
            if not down:
                with self._lock:
                    self._physically_down.discard(name)
                return
            with self._lock:
                if name in self._physically_down:
                    return
                self._physically_down.add(name)
                note = self.note_for(name)
                if note is None:
                    self.log.emit(f"{self._ts()}  {name}: note out of range (change octave)")
                    return
                channel, velocity = self.config["channel"], self._velocity_for(name)
                prev_key = self._active_choke_key
                choked, started = self.engine.trigger_exclusive_note(
                    channel, note, velocity, muted=self.muted
                )
                if not started:
                    choked_note, choked_ch = choked if choked is not None else (note, channel)
                    if prev_key:
                        self._active.pop(prev_key, None)
                    self._active_choke_key = None
                else:
                    if choked is not None and prev_key:
                        self._active.pop(prev_key, None)
                    self._active_choke_key = name
                    self._active[name] = (note, channel)
            if not started:
                self.key_state.emit(prev_key or name, False)
                self.log.emit(
                    f"{self._ts()}  ch{choked_ch:>2}  CHOKE OFF {choked_note:>3} {note_name(choked_note):<4} "
                    f"vel   0   <- {name} ({source})"
                )
                return
            if choked is not None:
                choked_note, choked_ch = choked
                if prev_key and prev_key != name:
                    self.key_state.emit(prev_key, False)
                self.log.emit(
                    f"{self._ts()}  ch{choked_ch:>2}  CHOKE OFF {choked_note:>3} {note_name(choked_note):<4} "
                    f"vel   0   <- choked by {name}"
                )
            label = "MUTE ON " if self.muted else "NOTE ON "
            self.key_state.emit(name, True)
            self.log.emit(
                f"{self._ts()}  ch{channel:>2}  {label}  {note:>3} {note_name(note):<4} "
                f"vel {velocity:>3}   <- {name} ({source})"
            )
            return

        with self._lock:
            if down:
                if name in self._active:
                    return
                note = self.note_for(name)
                if note is None:
                    self.log.emit(f"{self._ts()}  {name}: note out of range (change octave)")
                    return
                channel, velocity = self.config["channel"], self._velocity_for(name)
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
            already_active = (
                (name in self._physically_down)
                if self.exclusive_pad_mode
                else (name in self._active)
            )
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
        if self.exclusive_pad_mode and self.currently_active_note is not None:
            self.panic()
        self.config["channel"] = max(1, min(16, channel))

    def set_octave(self, octave: int) -> None:
        if self.exclusive_pad_mode and self.currently_active_note is not None:
            self.panic()
        self.config["octave"] = max(OCTAVE_MIN, min(OCTAVE_MAX, octave))

    def set_velocity(self, velocity: int) -> None:
        self.config["velocity"] = max(1, min(127, velocity))

    def set_suppress(self, value: bool) -> None:
        enabled = bool(value)
        self.config["suppress_keys"] = enabled
        self.config["exclusive_mode"] = enabled
        self.hook.set_suppress(enabled)

    def set_exclusive_pad_mode(self, value: bool, source: str = "ui") -> None:
        enabled = bool(value)
        if bool(self.config["exclusive_pad_mode"]) == enabled and self.engine.exclusive_pad_mode == enabled:
            return
        self.panic()
        self.config["exclusive_pad_mode"] = enabled
        self.engine.set_exclusive_pad_mode(enabled)
        self.config.save()
        self.exclusive_pad_mode_changed.emit(enabled)
        state_str = "ON (Monophonic Auto-Choke)" if enabled else "OFF (Polyphonic Hold)"
        self.log.emit(f"{self._ts()}  Exclusive Pad Mode: {state_str} [{source}]")

    def toggle_exclusive_pad_mode(self, source: str = ".") -> bool:
        new_state = not self.exclusive_pad_mode
        self.set_exclusive_pad_mode(new_state, source=source)
        return new_state

    def set_target_device(self, device_id: str) -> None:
        target = (device_id or "").strip()
        self.panic()
        self.config["target_device"] = target
        self.hook.set_target_device(target)
        self.config.save()
        label = target if target else ALL_DEVICES_LABEL
        self.log.emit(f"{self._ts()}  Target USB keypad: {label}")

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
            self._physically_down.clear()
            self._active_choke_key = None
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
                if b"windows" in bytes(eventType):
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
    """Load the high-res 3x3 pad-grid icon from assets/icon.png (with vector fallback)."""
    for candidate in (resource_path("assets/icon.png"), Path("assets/icon.png")):
        if candidate.is_file():
            icon = QIcon(str(candidate))
            if not icon.isNull():
                return icon
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
        self._mode_active = False
        self._color = QColor.fromHsv(int(index * 360 / len(KEY_NAMES)) % 360, 170, 255)
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(380)
        self._anim.valueChanged.connect(self._set_glow)
        self.setMinimumSize(78, 78)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def sizeHint(self) -> QSize:
        return QSize(96, 96)

    def set_note_text(self, text: str) -> None:
        self._note_text = text
        self.update()

    def set_mode_active(self, active: bool) -> None:
        active = bool(active)
        if self._mode_active != active:
            self._mode_active = active
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
        if self._mode_active:
            cyan, purple = QColor("#00e5ff"), QColor("#8b5cf6")
            for i, alpha in ((6, 36), (4, 58), (2, 88)):
                halo = QColor(cyan)
                halo.setAlpha(alpha)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(halo)
                p.drawRoundedRect(rect.adjusted(-i // 2, -i // 2, i // 2, i // 2), 14, 14)
            p.setBrush(mix(mix(base, purple, 0.42), cyan, g * 0.6))
            p.setPen(QPen(cyan, 2.2))
            p.drawRoundedRect(rect, 12, 12)
            text_color = QColor("#ffffff")
            sub_color = QColor("#00e5ff")
        else:
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
            sub_color = mix(QColor("#6b7592"), QColor("#1a1d29"), g)

        p.setPen(text_color)
        big = QFont(self.font())
        big.setBold(True)
        cell_dim = min(rect.width(), rect.height())
        if len(self.display) >= 12:
            divisor = 8
        elif len(self.display) >= 8:
            divisor = 6
        elif len(self.display) >= 4:
            divisor = 5
        else:
            divisor = 3
        big.setPixelSize(max(10, cell_dim // divisor))
        p.setFont(big)
        p.drawText(
            rect.adjusted(4, 2, -4, -rect.height() // 5),
            Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
            self.display,
        )
        small = QFont(self.font())
        small.setPixelSize(max(9, cell_dim // 8))
        small.setBold(self._mode_active)
        p.setFont(small)
        p.setPen(sub_color)
        p.drawText(rect.adjusted(0, 0, 0, -6), Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom,
                   self._note_text)
        p.end()


class NoKeyComboBox(QComboBox):
    """QComboBox that never accepts keyboard focus or key events (e.g. '1', '2', arrows)."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def keyPressEvent(self, event) -> None:
        event.ignore()

    def keyReleaseEvent(self, event) -> None:
        event.ignore()


class MainWindow(QMainWindow):
    def __init__(self, ctrl: Controller) -> None:
        super().__init__()
        self.ctrl = ctrl
        self._quitting = False
        self.setWindowTitle(f"NumpadToMIDI {VERSION}")
        self.setWindowIcon(make_app_icon())
        self.resize(1040, 700)
        self.setMinimumSize(880, 620)
        self._pads: dict[str, PadWidget] = {}
        self._build()
        self._setup_tray()
        ctrl.key_state.connect(self._on_key_state)
        ctrl.mute_changed.connect(self._on_mute_changed)
        ctrl.exclusive_pad_mode_changed.connect(self._on_exclusive_pad_mode_changed)
        ctrl.log.connect(self._append_log)
        self._refresh_notes()
        self._refresh_status()
        self._refresh_devices()

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
        self.channel = NoKeyComboBox()
        self.channel.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.channel.addItems([str(i) for i in range(1, 17)])
        self.channel.setCurrentIndex(cfg["channel"] - 1)
        self.channel.currentIndexChanged.connect(lambda i: self.ctrl.set_channel(i + 1))
        form.addWidget(self.channel, 0, 1, 1, 3)

        form.addWidget(QLabel("Octave shift"), 1, 0)
        minus, plus = QPushButton("\u2212"), QPushButton("+")
        for b in (minus, plus):
            b.setObjectName("step")
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
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
        self.velocity.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.velocity.setRange(1, 127)
        self.velocity.setValue(cfg["velocity"])
        self.velocity_label = QLabel(str(cfg["velocity"]))
        self.velocity_label.setObjectName("value")
        self.velocity_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.velocity.valueChanged.connect(self._on_velocity)
        form.addWidget(self.velocity, 2, 1, 1, 2)
        form.addWidget(self.velocity_label, 2, 3)

        form.addWidget(QLabel("MIDI output"), 3, 0)
        self.port = NoKeyComboBox()
        self.port.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.port.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.port.setMinimumContentsLength(14)
        self.port.activated.connect(self._on_port_chosen)
        refresh = QPushButton("Refresh")
        refresh.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        refresh.clicked.connect(self._reopen_ports)
        form.addWidget(self.port, 3, 1, 1, 2)
        form.addWidget(refresh, 3, 3)

        form.addWidget(QLabel("Target USB Keypad"), 4, 0)
        self.device = NoKeyComboBox()
        self.device.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.device_combo = self.device
        self.device.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.device.setMinimumContentsLength(14)
        self.device.activated.connect(self._on_device_chosen)
        self.refresh_device_btn = QPushButton("Refresh")
        self.refresh_device_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.refresh_device_btn.clicked.connect(self._refresh_devices)
        form.addWidget(self.device, 4, 1, 1, 2)
        form.addWidget(self.refresh_device_btn, 4, 3)
        ctl.addLayout(form)

        self.suppress = QCheckBox("Exclusive Mode (Block OS Keystrokes)")
        self.suppress.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.suppress.setChecked(bool(cfg["suppress_keys"]))
        self.suppress.toggled.connect(self.ctrl.set_suppress)
        ctl.addWidget(self.suppress)

        self.exclusive_pad = QCheckBox(
            "Exclusive Pad Mode (Auto-Choke / LivePadRig) [Shortcut: .]"
        )
        self.exclusive_pad.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.exclusive_pad.setChecked(bool(cfg["exclusive_pad_mode"]))
        self.exclusive_pad.toggled.connect(self.ctrl.set_exclusive_pad_mode)
        ctl.addWidget(self.exclusive_pad)

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
        panic.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        panic.clicked.connect(self.ctrl.panic)
        clear = QPushButton("Clear")
        clear.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        clear.clicked.connect(lambda: self.monitor.clear())
        top.addWidget(panic)
        top.addWidget(clear)
        mon.addLayout(top)
        self.monitor = QPlainTextEdit()
        self.monitor.setReadOnly(True)
        self.monitor.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.monitor.setMaximumBlockCount(600)
        self.monitor.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.monitor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        mon.addWidget(self.monitor, 1)
        right.addWidget(mon_card, 1)

    # ---- system tray ---------------------------------------------------------
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

    def _on_exclusive_pad_mode_changed(self, active: bool) -> None:
        self.exclusive_pad.blockSignals(True)
        self.exclusive_pad.setChecked(active)
        self.exclusive_pad.blockSignals(False)
        self._refresh_notes()

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
        self.port.clearFocus()

    def _reopen_ports(self) -> None:
        self.ctrl.panic()
        self.ctrl.engine.open_auto(self.ctrl.config["port_name"])
        self._refresh_status()

    def _on_device_chosen(self, index: int) -> None:
        dev_id = self.device.itemData(index) or ALL_DEVICES_ID
        self.ctrl.set_target_device(str(dev_id))
        self.device.clearFocus()

    def _refresh_devices(self) -> None:
        devices = self.ctrl.hook.refresh_devices()
        target = str(self.ctrl.config["target_device"] or "")
        self.device.blockSignals(True)
        self.device.clear()
        self.device.addItem(ALL_DEVICES_LABEL, ALL_DEVICES_ID)
        selected_idx = 0
        for idx, dev in enumerate(devices, start=1):
            self.device.addItem(dev.label, dev.device_id)
            if target and (
                target.upper() == dev.device_id.upper()
                or target.upper() in dev.device_id.upper()
                or target.upper() == dev.label.upper()
            ):
                selected_idx = idx
        if target and selected_idx == 0:
            self.device.addItem(f"{target} (disconnected)", target)
            selected_idx = self.device.count() - 1
        self.device.setCurrentIndex(selected_idx)
        self.device.blockSignals(False)

    def _refresh_notes(self) -> None:
        cfg = self.ctrl.config
        base = cfg["base_note"] + cfg["octave"] * 12
        self.octave_label.setText(f"{cfg['octave']:+d}  ({note_name(max(0, min(127, base)))})")
        choke_active = bool(cfg["exclusive_pad_mode"])
        for name, pad in self._pads.items():
            if name == ".":
                pad.set_mode_active(choke_active)
                pad.set_note_text("ACTIVE \u00b7 ON" if choke_active else "OFF \u00b7 Press .")
                continue
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
    logger.info("Starting %s %s (platform=%s)", APP_NAME, VERSION, sys.platform)
    if sys.platform == "win32":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                f"{APP_NAME}.App.{VERSION}"
            )
        except Exception:
            pass
    app = QApplication.instance() or QApplication(sys.argv)
    app.setWindowIcon(make_app_icon())
    app.setStyleSheet(STYLE)
    if QSystemTrayIcon.isSystemTrayAvailable():
        app.setQuitOnLastWindowClosed(False)
    config = Config()
    logger.info("Loaded configuration from %s", config.path)
    engine = MidiEngine()
    engine.open_auto(config["port_name"])
    logger.info("MIDI engine initialized: %s", engine.description)
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
    """Smoke test used locally and by CI against the frozen binary."""
    if sys.platform != "win32":
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    lines: list[str] = []
    code = 0
    try:
        app, ctrl, win = build_app(start_hook=(sys.platform == "win32"))
        ctrl.log.connect(lines.append)
        win.show()
        app.processEvents()
        orig_excl = ctrl.exclusive_pad_mode
        ctrl.set_exclusive_pad_mode(False)
        ctrl.handle_key("7", True)
        ctrl.handle_key("7", False)
        assert ctrl.handle_appcommand(APPCOMMAND_BROWSER_HOME_ALT) is True
        assert ctrl.handle_appcommand(APPCOMMAND_LAUNCH_MAIL) is True
        assert ctrl.handle_appcommand(APPCOMMAND_LAUNCH_APP2) is True
        ctrl.hook.set_target_device(r"HID\VID_04D9&PID_1203")
        assert ctrl.handle_appcommand(APPCOMMAND_BROWSER_HOME_ALT) is True
        ctrl.hook.set_target_device("")
        if sys.platform == "win32" and os.environ.get("QT_QPA_PLATFORM") != "offscreen":
            hwnd = int(win.winId())
            res = ctypes.windll.user32.SendMessageW(
                hwnd, WM_APPCOMMAND, hwnd, APPCOMMAND_BROWSER_HOME_ALT << 16
            )
            assert res == 1, f"Expected WM_APPCOMMAND LRESULT 1, got {res}"
        app.processEvents()
        assert len(win._pads) == len(KEY_NAMES) == 23
        assert not win.windowIcon().isNull()
        assert win.device.count() >= 1
        assert win.device.itemText(0) == ALL_DEVICES_LABEL
        assert win.device.focusPolicy() == Qt.FocusPolicy.NoFocus
        assert win.port.focusPolicy() == Qt.FocusPolicy.NoFocus
        assert win.channel.focusPolicy() == Qt.FocusPolicy.NoFocus
        assert win.velocity.focusPolicy() == Qt.FocusPolicy.NoFocus
        assert win.suppress.focusPolicy() == Qt.FocusPolicy.NoFocus
        assert win.exclusive_pad.focusPolicy() == Qt.FocusPolicy.NoFocus
        assert win.monitor.focusPolicy() == Qt.FocusPolicy.NoFocus
        assert ctrl.note_for("0") == 36
        assert ctrl.note_for("000") == 37
        assert ctrl.note_for(".") is None
        assert ctrl.note_for("Home") == 55
        assert ctrl.note_for("Calc") == 58
        assert win._pads["."].display == "[ Mode: Choke (.) ]"
        # Test '.' hardware/UI toggle & Monophonic Choke Pad Mode
        ctrl.handle_key(".", True)
        ctrl.handle_key(".", False)
        assert ctrl.exclusive_pad_mode is True
        assert win.exclusive_pad.isChecked() is True
        assert win._pads["."]._mode_active is True
        ctrl.handle_key("1", True)
        ctrl.handle_key("1", False)
        assert ctrl.currently_active_note == ctrl.note_for("1")
        ctrl.handle_key("2", True)
        ctrl.handle_key("2", False)
        assert ctrl.currently_active_note == ctrl.note_for("2")
        # Pressing the same pad again toggles it off
        ctrl.handle_key("2", True)
        ctrl.handle_key("2", False)
        assert ctrl.currently_active_note is None
        # Toggle off via '.'
        ctrl.handle_key(".", True)
        ctrl.handle_key(".", False)
        assert ctrl.exclusive_pad_mode is False
        assert win.exclusive_pad.isChecked() is False
        assert win._pads["."]._mode_active is False
        ctrl.set_exclusive_pad_mode(orig_excl)
        assert ctrl.config["burst_window_ms"] >= 50
        assert win.suppress.isChecked() is True
        assert str(ctrl.config.path).endswith("config.json")
        if sys.platform == "win32":
            assert APP_NAME in str(ctrl.config.path)
        assert [a.text() for a in win.tray_menu.actions() if not a.isSeparator()] == [
            "Open NumpadToMIDI",
            "Mute MIDI",
            "Quit NumpadToMIDI",
        ]
        win.hide()
        lines.append(f"SELFTEST OK ({ctrl.engine.description})")
        ctrl.shutdown()
    except Exception:
        code = 1
        err = "SELFTEST FAILED\n" + traceback.format_exc()
        lines.append(err)
        logger.error(err)
    payload = "\n".join(lines)
    targets = [
        Path("selftest.log"),
        Path(os.path.join(tempfile.gettempdir(), "NumpadToMIDI-selftest.log")),
    ]
    env_target = os.environ.get("SELFTEST_LOG")
    if env_target:
        targets.insert(0, Path(env_target))
    for target in targets:
        try:
            target.write_text(payload, encoding="utf-8")
        except OSError:
            pass
    return code


def main() -> int:
    try:
        if "--selftest" in sys.argv:
            return selftest()
        app, ctrl, win = build_app()
        win.show()
        logger.info("Main window shown; entering Qt event loop")
        return app.exec()
    except Exception as exc:
        tb_text = traceback.format_exc()
        logger.critical("Fatal error in main(): %s\n%s", exc, tb_text)
        _show_fatal_dialog(f"{APP_NAME} Startup Error", tb_text)
        return 1


if __name__ == "__main__":
    sys.exit(main())
