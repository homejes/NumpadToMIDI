"""Global numpad key hook for NumpadToMIDI.

Translates raw OS keyboard events into logical numpad key names and guarantees
*exactly one* "down" and one "up" per physical press:

* On Windows, installs a direct Win32 ``WH_KEYBOARD_LL`` hook via ``ctypes`` on a
  dedicated high-priority message-pump thread (``GetMessageW`` / ``TranslateMessage``
  / ``DispatchMessageW``) and dispatches events on a separate worker queue so the
  hook procedure returns ``LRESULT(1)`` in microseconds without ever calling
  ``CallNextHookEx`` for intercepted keys.
* Explicitly intercepts multimedia launcher keys (``VK_BROWSER_HOME`` 0xAC,
  ``VK_BROWSER_SEARCH`` 0xAA, ``VK_LAUNCH_MAIL`` 0xB4, ``VK_LAUNCH_APP1`` 0xB6,
  ``VK_LAUNCH_APP2`` 0xB7, ``VK_BACK`` 0x08) even when Windows marks HID Consumer
  Control events with ``LLKHF_INJECTED`` or ``LLKHF_EXTENDED``.
* OS auto-repeat is swallowed (a key that is already down cannot go down again).
* Stray releases are ignored.
* The "000" key of financial numpads, which hardware-wise just emits three quick
  ``0`` presses, is recognised as a burst and reported as a single ``000`` key.

The module is pure logic on top of ``_dispatch`` so it can be unit-tested
without a keyboard (see ``tests/test_key_logic.py``).
"""
from __future__ import annotations

import ctypes
import logging
import queue
import sys
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger("NumpadToMIDI.hook")

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

if not IS_WIN:
    from pynput import keyboard  # used on macOS / non-Windows platforms
else:
    from ctypes import wintypes

# (name, row, col) listed in CHROMATIC NOTE ORDER (index 0 -> base note C1 / 36).
# Matches the physical 6-row M-Tech 23-key USB numpad from bottom-left to top-right:
#   Row 0 (Top):    [ Home ]    [ Tab ]  [ Email ] [ Calc ]       (notes 55..58)
#   Row 1:          [ NumLock ] [ / ]    [ * ]     [ Backspace ]  (notes 51..54)
#   Row 2:          [ 7 ]       [ 8 ]    [ 9 ]     [ - ]          (notes 47..50)
#   Row 3:          [ 4 ]       [ 5 ]    [ 6 ]     [ + ]          (notes 43..46)
#   Row 4:          [ 1 ]       [ 2 ]    [ 3 ]     [ Enter ]      (notes 39..42, Enter spans rows 4-5)
#   Row 5 (Bottom): [ 0 ]       [ 000 ]  [ . ]     [ (Enter) ]    (notes 36..38)
KEY_LAYOUT: list[tuple[str, int, int]] = [
    ("0", 5, 0), ("000", 5, 1), (".", 5, 2),
    ("1", 4, 0), ("2", 4, 1), ("3", 4, 2), ("Enter", 4, 3),
    ("4", 3, 0), ("5", 3, 1), ("6", 3, 2), ("+", 3, 3),
    ("7", 2, 0), ("8", 2, 1), ("9", 2, 2), ("-", 2, 3),
    ("NumLock", 1, 0), ("/", 1, 1), ("*", 1, 2), ("Backspace", 1, 3),
    ("Home", 0, 0), ("Tab", 0, 1), ("Email", 0, 2), ("Calc", 0, 3),
]
KEY_SPAN: dict[str, tuple[int, int]] = {
    "Enter": (2, 1),  # tall 2-row key covering (row 4, col 3) and (row 5, col 3)
}
KEY_DISPLAY: dict[str, str] = {
    "Home": "\u2302 Home",
    "Backspace": "Backspace \u2190",
    ".": ". (Del)",
}
KEY_NAMES: list[str] = [k[0] for k in KEY_LAYOUT]
KEY_INDEX: dict[str, int] = {name: i for i, name in enumerate(KEY_NAMES)}

# --- Windows virtual-key codes ------------------------------------------------
VK_BACK = 0x08            # 8   - Backspace
VK_TAB = 0x09             # 9   - Tab
VK_RETURN = 0x0D          # 13  - Enter (extended = numpad Enter)
VK_HOME = 0x24            # 36  - Home (extended = dedicated/numpad Home)
VK_NUMLOCK = 0x90         # 144 - NumLock
VK_BROWSER_SEARCH = 0xAA  # 170 - Browser Search multimedia key
VK_BROWSER_HOME = 0xAC    # 172 - Browser Home multimedia key (scancode 0xE032)
VK_LAUNCH_MAIL = 0xB4     # 180 - Launch Mail multimedia key
VK_LAUNCH_APP1 = 0xB6     # 182 - Launch App1 (My Computer / Calc variant)
VK_LAUNCH_APP2 = 0xB7     # 183 - Launch App2 (Calculator multimedia key)

WIN_VK: dict[int, str] = {
    **{96 + i: str(i) for i in range(10)},  # VK_NUMPAD0..9 (0x60..0x69)
    110: ".",                  # VK_DECIMAL (0x6E)
    107: "+",                  # VK_ADD (0x6B)
    109: "-",                  # VK_SUBTRACT (0x6D)
    106: "*",                  # VK_MULTIPLY (0x6A)
    111: "/",                  # VK_DIVIDE (0x6F)
    VK_TAB: "Tab",             # 0x09
    VK_BACK: "Backspace",      # 0x08
    VK_NUMLOCK: "NumLock",     # 0x90
    VK_BROWSER_SEARCH: "Home", # 0xAA (170)
    VK_BROWSER_HOME: "Home",   # 0xAC (172)
    VK_LAUNCH_MAIL: "Email",   # 0xB4 (180)
    VK_LAUNCH_APP1: "Calc",    # 0xB6 (182)
    VK_LAUNCH_APP2: "Calc",    # 0xB7 (183)
}

# Hardware numpad scan codes when NumLock is OFF (non-extended, so dedicated
# arrow/navigation cluster keys - which carry LLKHF_EXTENDED - are unaffected).
_WIN_NUMPAD_SCANCODES: dict[int, str] = {
    0x52: "0", 0x4F: "1", 0x50: "2", 0x51: "3", 0x4B: "4",
    0x4C: "5", 0x4D: "6", 0x47: "7", 0x48: "8", 0x49: "9",
    0x53: ".",
}
_WIN_NUMLOCK_OFF_VKS = frozenset({12, 33, 34, 35, 36, 37, 38, 39, 40, 45, 46})
_WIN_SCAN_BROWSER_HOME = 0x32
_WIN_SCAN_LAUNCH_MAIL = 0x6C
_WIN_SCAN_LAUNCH_CALC = 0x21
_ALWAYS_SUPPRESS_KEYS = frozenset({"Home", "Email", "Calc"})

_WH_KEYBOARD_LL = 13
_HC_ACTION = 0
_WM_KEYDOWN, _WM_KEYUP, _WM_SYSKEYDOWN, _WM_SYSKEYUP = 0x0100, 0x0101, 0x0104, 0x0105
_WM_QUIT = 0x0012
_LLKHF_EXTENDED = 0x01
_THREAD_PRIORITY_HIGHEST = 2

# --- macOS key codes (kVK_ANSI_Keypad*) ----------------------------------------
MAC_VK: dict[int, str] = {
    82: "0", 83: "1", 84: "2", 85: "3", 86: "4", 87: "5", 88: "6", 89: "7",
    91: "8", 92: "9",
    65: ".", 67: "*", 69: "+", 71: "NumLock",  # keypad Clear == NumLock
    75: "/", 76: "Enter", 78: "-", 115: "Home",
    48: "Tab", 51: "Backspace",
}


def win_key_name(vk: int, scan: int, extended: bool) -> Optional[str]:
    """Map a Windows low-level keyboard event to a logical numpad key name."""
    if vk == VK_RETURN:  # only the *extended* Enter is the numpad one
        return "Enter" if extended else None
    if vk in (VK_BROWSER_HOME, VK_BROWSER_SEARCH) or (scan == _WIN_SCAN_BROWSER_HOME and extended):
        return "Home"
    if vk == VK_LAUNCH_MAIL or (scan == _WIN_SCAN_LAUNCH_MAIL and extended):
        return "Email"
    if vk in (VK_LAUNCH_APP1, VK_LAUNCH_APP2) or (scan == _WIN_SCAN_LAUNCH_CALC and extended):
        return "Calc"
    if vk == VK_HOME and extended:
        return "Home"
    if not extended and vk in _WIN_NUMLOCK_OFF_VKS and scan in _WIN_NUMPAD_SCANCODES:
        return _WIN_NUMPAD_SCANCODES[scan]
    return WIN_VK.get(vk)


if IS_WIN:
    LRESULT = ctypes.c_ssize_t

    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("vkCode", wintypes.DWORD),
            ("scanCode", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    LOWLEVELKEYBOARDPROC = ctypes.WINFUNCTYPE(
        LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
    )

    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32

    _user32.SetWindowsHookExW.restype = wintypes.HHOOK
    _user32.SetWindowsHookExW.argtypes = [
        ctypes.c_int, LOWLEVELKEYBOARDPROC, wintypes.HINSTANCE, wintypes.DWORD
    ]
    _user32.CallNextHookEx.restype = LRESULT
    _user32.CallNextHookEx.argtypes = [
        wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
    ]
    _user32.UnhookWindowsHookEx.restype = wintypes.BOOL
    _user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
    _user32.GetMessageW.restype = wintypes.BOOL
    _user32.GetMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT
    ]
    _user32.TranslateMessage.restype = wintypes.BOOL
    _user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    _user32.DispatchMessageW.restype = LRESULT
    _user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    _user32.PostThreadMessageW.restype = wintypes.BOOL
    _user32.PostThreadMessageW.argtypes = [
        wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    ]
    _kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    _kernel32.GetCurrentThread.restype = wintypes.HANDLE
    _kernel32.SetThreadPriority.restype = wintypes.BOOL
    _kernel32.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]


KeyCallback = Callable[[str, bool], None]


class KeyHook:
    """Debounced global numpad listener.

    ``on_key(name, is_down)`` is called from the worker (or timer) thread.
    """

    TRIPLE_STALE_SECONDS = 3.0

    def __init__(
        self,
        on_key: KeyCallback,
        *,
        detect_triple_zero: bool = True,
        burst_window_ms: float = 35.0,
        suppress: bool = True,
    ) -> None:
        self._on_key = on_key
        self.detect_triple_zero = detect_triple_zero
        self.burst_window = max(0.005, burst_window_ms / 1000.0)
        self._suppress = bool(suppress)

        self._lock = threading.RLock()
        self._down: set[str] = set()
        # 000-burst detection state
        self._zbuf: list[bool] = []
        self._zfirst = 0.0
        self._timer: Optional[threading.Timer] = None
        self._triple_ups_left = 0
        self._triple_started = 0.0

        # Non-Windows pynput listener
        self._listener = None
        # Windows native WH_KEYBOARD_LL state
        self._running = False
        self._win_thread: Optional[threading.Thread] = None
        self._win_worker: Optional[threading.Thread] = None
        self._win_thread_id: int = 0
        self._win_hook_handle = None
        self._win_proc_ref = None  # prevent GC of ctypes callback
        self._win_queue: queue.SimpleQueue[Optional[tuple[str, bool]]] = queue.SimpleQueue()
        self._win_ready = threading.Event()

    # ------------------------------------------------------------------ control
    def set_suppress(self, value: bool) -> None:
        """Enable or disable Exclusive Mode (swallowing numpad keys from the OS)."""
        self._suppress = bool(value)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        try:
            if IS_WIN:
                self._win_ready.clear()
                self._win_worker = threading.Thread(
                    target=self._win_dispatch_loop, name="NumpadDispatchWorker", daemon=True
                )
                self._win_worker.start()
                self._win_thread = threading.Thread(
                    target=self._win_message_pump, name="NumpadKeyboardHookPump", daemon=True
                )
                self._win_thread.start()
                self._win_ready.wait(timeout=2.0)
                return

            kwargs: dict = {"on_press": lambda key: None, "on_release": lambda key: None}
            if IS_MAC:
                kwargs["darwin_intercept"] = self._mac_intercept
            else:  # pragma: no cover - Linux is not a supported target
                kwargs["on_press"] = self._generic_press
                kwargs["on_release"] = self._generic_release
            self._listener = keyboard.Listener(**kwargs)
            self._listener.daemon = True
            self._listener.start()
        except Exception as exc:
            logger.error("Failed to start keyboard hook: %s", exc, exc_info=True)

    def stop(self) -> None:
        if not self._running and self._listener is None:
            with self._lock:
                self._cancel_timer()
                self._zbuf.clear()
                self._triple_ups_left = 0
                for name in list(self._down):
                    self._emit(name, False)
            return
        self._running = False
        try:
            if IS_WIN:
                tid = self._win_thread_id
                if tid:
                    try:
                        _user32.PostThreadMessageW(tid, _WM_QUIT, 0, 0)
                    except Exception:
                        pass
                self._win_queue.put(None)
                if self._win_thread is not None:
                    self._win_thread.join(timeout=1.5)
                    self._win_thread = None
                if self._win_worker is not None:
                    self._win_worker.join(timeout=1.5)
                    self._win_worker = None
            else:
                listener, self._listener = self._listener, None
                if listener is not None:
                    listener.stop()
        except Exception as exc:
            logger.warning("Error stopping keyboard hook: %s", exc)
        with self._lock:
            self._cancel_timer()
            self._zbuf.clear()
            self._triple_ups_left = 0
            for name in list(self._down):
                self._emit(name, False)

    # --------------------------------------- Windows dedicated WH_KEYBOARD_LL
    def _win_message_pump(self) -> None:  # pragma: no cover - real OS hook loop
        """Dedicated high-priority Win32 message loop owning WH_KEYBOARD_LL."""
        try:
            self._win_thread_id = int(_kernel32.GetCurrentThreadId())
            try:
                _kernel32.SetThreadPriority(_kernel32.GetCurrentThread(), _THREAD_PRIORITY_HIGHEST)
            except Exception:
                pass

            # Force message queue creation before installing hook
            msg = wintypes.MSG()
            _user32.PostThreadMessageW(self._win_thread_id, 0, 0, 0)
            _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)

            def _ll_proc(nCode: int, wParam: int, lParam: int) -> int:
                try:
                    if nCode == _HC_ACTION and lParam:
                        if wParam in (_WM_KEYDOWN, _WM_SYSKEYDOWN, _WM_KEYUP, _WM_SYSKEYUP):
                            kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                            extended = bool(kb.flags & _LLKHF_EXTENDED)
                            name = win_key_name(int(kb.vkCode), int(kb.scanCode), extended)
                            if name is not None:
                                is_down = wParam in (_WM_KEYDOWN, _WM_SYSKEYDOWN)
                                self._win_queue.put_nowait((name, is_down))
                                if self._suppress or name in _ALWAYS_SUPPRESS_KEYS:
                                    # Strictly return 1 without calling CallNextHookEx so Windows
                                    # never converts VK_BROWSER_HOME / VK_LAUNCH_MAIL / VK_LAUNCH_APP2
                                    # into ShellExecute or passes numpad digits to apps.
                                    return 1
                except Exception as exc:
                    logger.error("Exception in WH_KEYBOARD_LL callback: %s", exc)
                return int(_user32.CallNextHookEx(self._win_hook_handle, nCode, wParam, lParam))

            self._win_proc_ref = LOWLEVELKEYBOARDPROC(_ll_proc)
            self._win_hook_handle = _user32.SetWindowsHookExW(
                _WH_KEYBOARD_LL, self._win_proc_ref, None, 0
            )
            if not self._win_hook_handle:
                logger.error("SetWindowsHookExW(WH_KEYBOARD_LL) returned NULL")
                self._win_ready.set()
                return
            logger.info("Installed WH_KEYBOARD_LL hook (handle=%s)", self._win_hook_handle)
            self._win_ready.set()

            while self._running:
                ret = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret <= 0:  # 0 == WM_QUIT, -1 == error
                    break
                _user32.TranslateMessage(ctypes.byref(msg))
                _user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as exc:
            logger.error("Win32 hook message pump error: %s", exc, exc_info=True)
            self._win_ready.set()
        finally:
            if self._win_hook_handle:
                try:
                    _user32.UnhookWindowsHookEx(self._win_hook_handle)
                except Exception:
                    pass
                self._win_hook_handle = None
            self._win_thread_id = 0
            self._win_proc_ref = None

    def _win_dispatch_loop(self) -> None:  # pragma: no cover - background consumer
        while True:
            item = self._win_queue.get()
            if item is None:
                break
            name, is_down = item
            try:
                self._dispatch(name, is_down)
            except Exception:
                pass

    # ----------------------------------------------------- macOS / generic sources
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
            return None if (self._suppress or name in _ALWAYS_SUPPRESS_KEYS) else event
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
