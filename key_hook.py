"""Global numpad key hook for NumpadToMIDI.

Translates raw OS keyboard events (via ``pynput``) into logical numpad key
names and guarantees *exactly one* "down" and one "up" per physical press:

* OS auto-repeat is swallowed (a key that is already down cannot go down again).
* Stray releases are ignored.
* The "000" key of financial numpads, which hardware-wise just emits three quick
  ``0`` presses, is recognised as a burst and reported as a single ``000`` key.

The module is pure logic on top of ``_dispatch`` so it can be unit-tested
without a keyboard (see ``tests/test_key_logic.py``).
"""
from __future__ import annotations

import sys
import threading
import time
from typing import Callable, Optional

from pynput import keyboard

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

# (name, row, col) listed in NOTE ORDER (index 0 -> base note, i.e. C1 / 36).
# The order follows a drum-rack: bottom-left pad first, then left->right,
# bottom->top. 23 keys in total.
KEY_LAYOUT: list[tuple[str, int, int]] = [
    ("0", 5, 0), ("000", 5, 1), (".", 5, 2),
    ("1", 4, 0), ("2", 4, 1), ("3", 4, 2), ("Enter", 4, 3),
    ("4", 3, 0), ("5", 3, 1), ("6", 3, 2), ("=", 3, 3),
    ("7", 2, 0), ("8", 2, 1), ("9", 2, 2), ("+", 2, 3),
    ("Tab", 1, 0), ("/", 1, 1), ("*", 1, 2), ("-", 1, 3),
    ("Calc", 0, 0), ("Email", 0, 1), ("NumLock", 0, 2), ("Backspace", 0, 3),
]
KEY_NAMES: list[str] = [k[0] for k in KEY_LAYOUT]
KEY_INDEX: dict[str, int] = {name: i for i, name in enumerate(KEY_NAMES)}

# --- Windows virtual-key codes ------------------------------------------------
WIN_VK: dict[int, str] = {
    **{96 + i: str(i) for i in range(10)},  # VK_NUMPAD0..9
    110: ".",        # VK_DECIMAL
    107: "+",        # VK_ADD
    109: "-",        # VK_SUBTRACT
    106: "*",        # VK_MULTIPLY
    111: "/",        # VK_DIVIDE
    9: "Tab",        # VK_TAB
    8: "Backspace",  # VK_BACK
    183: "Calc",     # VK_LAUNCH_APP2 (calculator key)
    180: "Email",    # VK_LAUNCH_MAIL
    144: "NumLock",  # VK_NUMLOCK
}
_WIN_VK_ENTER = 13
_WIN_VK_OEM_PLUS = 187
_WIN_SCAN_NUMPAD_EQUALS = 0x59
_WM_KEYDOWN, _WM_KEYUP, _WM_SYSKEYDOWN, _WM_SYSKEYUP = 0x100, 0x101, 0x104, 0x105
_LLKHF_EXTENDED = 0x01

# --- macOS key codes (kVK_ANSI_Keypad*) ----------------------------------------
MAC_VK: dict[int, str] = {
    82: "0", 83: "1", 84: "2", 85: "3", 86: "4", 87: "5", 88: "6", 89: "7",
    91: "8", 92: "9",
    65: ".", 67: "*", 69: "+", 71: "NumLock",  # keypad Clear == NumLock
    75: "/", 76: "Enter", 78: "-", 81: "=",
    48: "Tab", 51: "Backspace",
}


def win_key_name(vk: int, scan: int, extended: bool) -> Optional[str]:
    """Map a Windows low-level keyboard event to a logical numpad key name."""
    if vk == _WIN_VK_ENTER:  # only the *extended* Enter is the numpad one
        return "Enter" if extended else None
    if vk == _WIN_VK_OEM_PLUS and scan == _WIN_SCAN_NUMPAD_EQUALS:
        return "="
    return WIN_VK.get(vk)


KeyCallback = Callable[[str, bool], None]


class KeyHook:
    """Debounced global numpad listener.

    ``on_key(name, is_down)`` is called from the listener (or a timer) thread.
    """

    TRIPLE_STALE_SECONDS = 3.0

    def __init__(
        self,
        on_key: KeyCallback,
        *,
        detect_triple_zero: bool = True,
        burst_window_ms: float = 35.0,
        suppress: bool = False,
    ) -> None:
        self._on_key = on_key
        self.detect_triple_zero = detect_triple_zero
        self.burst_window = max(0.005, burst_window_ms / 1000.0)
        self._suppress = suppress

        self._lock = threading.RLock()
        self._down: set[str] = set()
        # 000-burst detection state
        self._zbuf: list[bool] = []
        self._zfirst = 0.0
        self._timer: Optional[threading.Timer] = None
        self._triple_ups_left = 0
        self._triple_started = 0.0

        self._listener: Optional[keyboard.Listener] = None

    # ------------------------------------------------------------------ control
    def set_suppress(self, value: bool) -> None:
        """Block numpad keys from reaching other apps (best effort)."""
        self._suppress = bool(value)

    def start(self) -> None:
        if self._listener is not None:
            return
        kwargs: dict = {"on_press": lambda key: None, "on_release": lambda key: None}
        if IS_WIN:
            kwargs["win32_event_filter"] = self._win_filter
        elif IS_MAC:
            kwargs["darwin_intercept"] = self._mac_intercept
        else:  # pragma: no cover - Linux is not a supported target
            kwargs["on_press"] = self._generic_press
            kwargs["on_release"] = self._generic_release
        self._listener = keyboard.Listener(**kwargs)
        self._listener.daemon = True
        self._listener.start()

    def stop(self) -> None:
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.stop()
        with self._lock:
            self._cancel_timer()
            self._zbuf.clear()
            self._triple_ups_left = 0
            for name in list(self._down):
                self._emit(name, False)

    # ----------------------------------------------------- platform event sources
    def _win_filter(self, msg: int, data) -> bool:
        """pynput low-level hook filter. Handles everything, then returns False
        so pynput's own callbacks are skipped (we do not need them)."""
        try:
            name = win_key_name(
                int(data.vkCode), int(data.scanCode), bool(data.flags & _LLKHF_EXTENDED)
            )
            if name is not None and not (data.flags & 0x10):  # ignore injected
                if msg in (_WM_KEYDOWN, _WM_SYSKEYDOWN):
                    self._dispatch(name, True)
                elif msg in (_WM_KEYUP, _WM_SYSKEYUP):
                    self._dispatch(name, False)
                if self._suppress:
                    self._listener.suppress_event()  # raises SuppressException
        except Exception as exc:  # never break the OS hook chain
            if exc.__class__.__name__ == "SuppressException":
                raise
        return False

    def _mac_intercept(self, event_type, event):
        """pynput CGEventTap intercept. Returns None to block the event."""
        try:
            import Quartz  # provided by pyobjc (pynput dependency on macOS)

            if event_type not in (Quartz.kCGEventKeyDown, Quartz.kCGEventKeyUp):
                return event
            vk = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
            name = MAC_VK.get(int(vk))
            if name is None:
                return event
            self._dispatch(name, event_type == Quartz.kCGEventKeyDown)
            return None if self._suppress else event
        except Exception:
            return event

    def _generic_press(self, key) -> None:  # pragma: no cover
        name = self._generic_name(key)
        if name:
            self._dispatch(name, True)

    def _generic_release(self, key) -> None:  # pragma: no cover
        name = self._generic_name(key)
        if name:
            self._dispatch(name, False)

    @staticmethod
    def _generic_name(key) -> Optional[str]:  # pragma: no cover
        char = getattr(key, "char", None)
        return char if char in KEY_INDEX else None

    # ------------------------------------------------------------- debounce core
    def _dispatch(self, name: str, down: bool) -> None:
        with self._lock:
            if name == "0" and self.detect_triple_zero:
                self._zero_event(down)
            else:
                self._emit(name, down)

    def _emit(self, name: str, down: bool) -> None:
        """Held-set debounce: exactly one down and one up per press."""
        if down:
            if name in self._down:
                return  # auto-repeat / duplicate
            self._down.add(name)
        else:
            if name not in self._down:
                return  # stray release
            self._down.discard(name)
        try:
            self._on_key(name, down)
        except Exception:  # a UI/MIDI error must not kill the hook thread
            pass

    def _zero_event(self, down: bool) -> None:
        now = time.monotonic()
        # --- finishing a recognised 000 press (waiting for its releases)
        if self._triple_ups_left:
            if down and now - self._triple_started > self.TRIPLE_STALE_SECONDS:
                # releases were lost; recover and treat this as a new press
                self._triple_ups_left = 0
                self._emit("000", False)
            else:
                if not down:
                    self._triple_ups_left -= 1
                    if self._triple_ups_left == 0:
                        self._emit("000", False)
                return
        # --- a burst whose window has expired is flushed *now*, so correctness
        # never depends on how quickly the flush timer thread gets scheduled
        if self._zbuf and now - self._zfirst > self.burst_window:
            self._cancel_timer()
            pending, self._zbuf = self._zbuf, []
            for pending_down in pending:
                self._emit("0", pending_down)
        # --- no burst in progress
        if not self._zbuf:
            if "0" in self._down:
                self._emit("0", down)  # auto-repeat is ignored, release passes
                return
            if not down:
                return  # stray release
        if not self._zbuf:
            self._zfirst = now
        self._zbuf.append(down)
        downs = sum(self._zbuf)
        if downs >= 3:
            self._cancel_timer()
            ups = len(self._zbuf) - downs
            self._zbuf.clear()
            self._emit("000", True)
            self._triple_started = now
            self._triple_ups_left = 3 - ups
            if self._triple_ups_left == 0:
                self._emit("000", False)
            return
        if self._timer is None:
            self._timer = threading.Timer(self.burst_window, self._flush_zero)
            self._timer.daemon = True
            self._timer.start()

    def _flush_zero(self) -> None:
        with self._lock:
            if self._timer is not threading.current_thread():
                return  # cancelled while we were waiting for the lock
            self._timer = None
            pending, self._zbuf = self._zbuf, []
            for down in pending:
                self._emit("0", down)

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
