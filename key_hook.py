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
  The triplet detector is NumLock-independent: it tracks the hardware OEM
  scancode ``0x52`` (physical '0'/'Insert' key) and explicitly both
  ``VK_NUMPAD0`` (NumLock ON) and ``VK_INSERT`` (NumLock OFF) received from the
  selected USB keypad device, suppresses all three OS strokes, and emits a
  single '000' key (MIDI Note 37 / C#1).
* On macOS the composite USB keypad exposes two HID interfaces: the numeric
  keys live on the GenericDesktop Keyboard/Keypad page (0x01 - usages 6/7)
  while Home/Email/Calc live on the Consumer Control page (0x0C). Native
  ``IOHIDManager`` listeners are attached to BOTH interfaces simultaneously
  (``IOHIDManagerSetDeviceMatchingMultiple``), and a ``CGEventTap`` swallows
  the keypad codes so they do not type numbers into the macOS UI.

The module is pure logic on top of ``_dispatch`` so it can be unit-tested
without a keyboard (see ``tests/test_key_logic.py``).
"""
from __future__ import annotations

import ctypes
import logging
import queue
import re
import sys
import threading
import time
from typing import Any, Callable, Optional

logger = logging.getLogger("NumpadToMIDI.hook")

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

try:
    import hid
    HAS_HID = True
except ImportError:
    HAS_HID = False
    hid = None

if not IS_WIN:
    from pynput import keyboard  # fallback on non-Windows platforms if hid unavailable
else:
    from ctypes import wintypes
    import winreg

ALL_DEVICES_ID = ""
ALL_DEVICES_LABEL = "All Connected Keypads (Default)"


class KeyboardDevice:
    """Metadata for a connected keyboard/keypad discovered via Win32 Raw Input."""

    def __init__(
        self,
        device_id: str,
        label: str,
        raw_path: str = "",
        handles: Optional[set[int]] = None,
    ) -> None:
        self.device_id = device_id
        self.label = label
        self.raw_path = raw_path
        self.handles: set[int] = set(handles or ())

    def __repr__(self) -> str:
        return (
            f"KeyboardDevice(device_id={self.device_id!r}, label={self.label!r}, "
            f"handles={sorted(self.handles)!r})"
        )

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
}
KEY_NAMES: list[str] = [k[0] for k in KEY_LAYOUT]
KEY_INDEX: dict[str, int] = {name: i for i, name in enumerate(KEY_NAMES)}

# --- Windows virtual-key codes ------------------------------------------------
VK_BACK = 0x08            # 8   - Backspace
VK_TAB = 0x09             # 9   - Tab
VK_CLEAR = 0x0C           # 12  - Clear (Numpad 5 when NumLock is OFF)
VK_RETURN = 0x0D          # 13  - Enter (extended = numpad Enter)
VK_PRIOR = 0x21           # 33  - Page Up (Numpad 9 when NumLock is OFF)
VK_NEXT = 0x22            # 34  - Page Down (Numpad 3 when NumLock is OFF)
VK_END = 0x23             # 35  - End (Numpad 1 when NumLock is OFF)
VK_HOME = 0x24            # 36  - Home (extended = dedicated/numpad Home; non-extended = Numpad 7)
VK_LEFT = 0x25            # 37  - Left (Numpad 4 when NumLock is OFF)
VK_UP = 0x26              # 38  - Up (Numpad 8 when NumLock is OFF)
VK_RIGHT = 0x27           # 39  - Right (Numpad 6 when NumLock is OFF)
VK_DOWN = 0x28            # 40  - Down (Numpad 2 when NumLock is OFF)
VK_INSERT = 0x2D          # 45  - Insert (Numpad 0 / '000' burst when NumLock is OFF)
VK_DELETE = 0x2E          # 46  - Delete (Numpad '.' when NumLock is OFF)
VK_NUMPAD0 = 0x60         # 96  - Numpad 0 (when NumLock is ON)
VK_DECIMAL = 0x6E         # 110 - Numpad '.' (when NumLock is ON)
VK_MULTIPLY = 0x6A        # 106 - Numpad * (when NumLock is ON)
VK_ADD = 0x6B             # 107 - Numpad + (when NumLock is ON)
VK_SUBTRACT = 0x6D        # 109 - Numpad - (when NumLock is ON)
VK_DIVIDE = 0x6F          # 111 - Numpad / (when NumLock is ON)
VK_NUMLOCK = 0x90         # 144 - NumLock
VK_BROWSER_SEARCH = 0xAA  # 170 - Browser Search multimedia key
VK_BROWSER_HOME = 0xAC    # 172 - Browser Home multimedia key (scancode 0xE032)
VK_LAUNCH_MAIL = 0xB4     # 180 - Launch Mail multimedia key
VK_LAUNCH_APP1 = 0xB6     # 182 - Launch App1 (My Computer / Calc variant)
VK_LAUNCH_APP2 = 0xB7     # 183 - Launch App2 (Calculator multimedia key)

WIN_VK: dict[int, str] = {
    **{96 + i: str(i) for i in range(10)},  # VK_NUMPAD0..9 (0x60..0x69)
    VK_DECIMAL: ".",           # 110 (0x6E)
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

# Hardware OEM Scan Codes -> Keypad key names (independent of NumLock state)
# Non-extended scan codes (standard numpad digits & navigation cluster on keypad):
_WIN_OEM_SCANCODES_NON_EXTENDED: dict[int, str] = {
    0x52: "0",          # Numpad 0 / Insert
    0x4F: "1",          # Numpad 1 / End
    0x50: "2",          # Numpad 2 / Down
    0x51: "3",          # Numpad 3 / PgDn
    0x4B: "4",          # Numpad 4 / Left
    0x4C: "5",          # Numpad 5 / Clear
    0x4D: "6",          # Numpad 6 / Right
    0x47: "7",          # Numpad 7 / Home
    0x48: "8",          # Numpad 8 / Up
    0x49: "9",          # Numpad 9 / PgUp
    0x53: ".",          # Numpad . / Del
    0x4E: "+",          # Numpad +
    0x4A: "-",          # Numpad -
    0x37: "*",          # Numpad *
    0x45: "NumLock",    # NumLock
    0x0E: "Backspace",  # Backspace
    0x0F: "Tab",        # Tab
}

_WIN_SCAN_BROWSER_HOME = 0x32
_WIN_SCAN_LAUNCH_MAIL = 0x6C
_WIN_SCAN_LAUNCH_CALC = 0x21
# OEM hardware scancode of the physical numpad '0' / 'Insert' key. This is the
# hardware-level identity of the M-Tech '000' key triplet (the '000' key sends a
# rapid 3-stroke burst of this key), completely independent of NumLock state.
_WIN_SCAN_KEYPAD_ZERO = 0x52
# Burst window floor (seconds): the triplet detector must recognize 3 rapid
# strokes of scancode 0x52 arriving within 80 ms of each other, per the M-Tech
# hardware timing spec.
_BURST_WINDOW_FLOOR = 0.080

# --- Raw Input origin correlation (WH_KEYBOARD_LL x WM_INPUT) -------------------
# The WH_KEYBOARD_LL callback for a keystroke always runs BEFORE the matching
# WM_INPUT is processed on the pump thread, so the origin device of the event
# being reported must be correlated from earlier Raw Input data:
#   1. Per-key memory: which hDevice last produced THIS (vk, extended) key.
#   2. Active-device fallback: the hDevice of the most recent WM_INPUT of any
#      key (drum cadences and typing bursts keep this window hot).
# A Raw Input handle that has ever sent a general typing key (A-Z / Space) is
# flagged as a general keyboard so keystrokes proven to come from it (e.g. the
# laptop's built-in keyboard) are never swallowed.
_RAW_KEY_TTL = 10.0     # seconds a per-key origin memory stays valid
_RAW_RECENT_TTL = 1.0   # seconds the active-device fallback window stays hot
_GENERAL_KB_VKS = frozenset([*range(0x41, 0x5B), 0x20])  # A-Z + Space

# Extended scan codes:
_WIN_OEM_SCANCODES_EXTENDED: dict[int, str] = {
    0x35: "/",          # Numpad /
    0x1C: "Enter",      # Numpad Enter
    _WIN_SCAN_BROWSER_HOME: "Home", # 0x32
    _WIN_SCAN_LAUNCH_MAIL: "Email", # 0x6C
    _WIN_SCAN_LAUNCH_CALC: "Calc",  # 0x21
}

_WIN_NUMPAD_SCANCODES: dict[int, str] = dict(_WIN_OEM_SCANCODES_NON_EXTENDED)

_WIN_NUMLOCK_OFF_VK_MAP: dict[int, str] = {
    VK_INSERT: "0",
    VK_END: "1",
    VK_DOWN: "2",
    VK_NEXT: "3",
    VK_LEFT: "4",
    VK_CLEAR: "5",
    VK_RIGHT: "6",
    VK_HOME: "7",
    VK_UP: "8",
    VK_PRIOR: "9",
    VK_DELETE: ".",
}
_WIN_NUMLOCK_OFF_VKS = frozenset(_WIN_NUMLOCK_OFF_VK_MAP.keys())

# Comprehensive WH_KEYBOARD_LL swallow list: every Virtual Key the selected
# USB keypad can emit depending on NumLock state. NumLock-ON keys are the
# numeric keypad VKs; NumLock-OFF (or a NumLock desync - e.g. a toggle that
# slipped through on the UAC secure desktop or the keypad firmware toggling
# internally while the app swallows the host-side keystroke) makes the SAME
# physical keys arrive as navigational variants: a '2' arrives as VK_DOWN
# and would move the selected track in Cubase. Swallowing is strictly gated
# by the Raw Input (WM_INPUT) device-origin correlation, so the laptop's
# real arrow/Home/End keys are never swallowed.
_NUMPAD_SWALLOW_VKS = frozenset(
    {
        *range(VK_NUMPAD0, VK_NUMPAD0 + 10),  # VK_NUMPAD0..9 (NumLock ON digits)
        VK_MULTIPLY, VK_ADD, VK_SUBTRACT, VK_DECIMAL, VK_DIVIDE,  # NumLock ON operators
        # Physical keypad keys whose Virtual Keys are shared with the main
        # keyboard (device-gated swallowing keeps the laptop's keys alive):
        VK_RETURN,  # Numpad Enter (E0 0x1C; plain-Return keypads send 0x1C) - Cubase "Play"
        VK_BACK,    # Numpad Backspace (scan 0x0E)
        VK_TAB,     # Numpad Tab (scan 0x0F) - DAW transport/track shortcuts
        # NumLock OFF navigational equivalents of the same physical keys:
        VK_INSERT,  # Numpad 0
        VK_END,     # Numpad 1
        VK_DOWN,    # Numpad 2
        VK_NEXT,    # Numpad 3 / Page Down
        VK_LEFT,    # Numpad 4
        VK_CLEAR,   # Numpad 5
        VK_RIGHT,   # Numpad 6
        VK_HOME,    # Numpad 7
        VK_UP,      # Numpad 8
        VK_PRIOR,   # Numpad 9 / Page Up
        VK_DELETE,  # Numpad .
    }
)
# Device-gated scancode overrides for keypad keys whose scancode is missing
# from the pure OEM table above (adding it there would map the laptop's main
# keys everywhere and fire MIDI from them in All mode). Consulted only when
# the Raw Input origin is verified as the selected keypad.
_WIN_KEYPAD_SCAN_OVERRIDES: dict[int, str] = {
    0x1C: "Enter",  # Numpad Enter (E0 0x1C); plain-Return keypads send 0x1C non-extended
}
_MAPVK_VSC_TO_VK = 1
_ALWAYS_SUPPRESS_KEYS = frozenset({"Home", "Email", "Calc"})

_WH_KEYBOARD_LL = 13
_HC_ACTION = 0
_WM_KEYDOWN, _WM_KEYUP, _WM_SYSKEYDOWN, _WM_SYSKEYUP = 0x0100, 0x0101, 0x0104, 0x0105
_WM_QUIT = 0x0012
_LLKHF_EXTENDED = 0x01
_THREAD_PRIORITY_HIGHEST = 2

# --- macOS key codes (kVK_ANSI_Keypad*) ----------------------------------------
MAC_VK: dict[int, str] = {
    # Keypad with NumLock ON (kVK_ANSI_Keypad*)
    82: "0", 83: "1", 84: "2", 85: "3", 86: "4", 87: "5", 88: "6", 89: "7",
    91: "8", 92: "9",
    65: ".", 67: "*", 69: "+", 71: "NumLock",  # keypad Clear == NumLock
    75: "/", 76: "Enter", 78: "-",
    48: "Tab", 51: "Backspace",
    # Keypad with NumLock OFF (navigation / PC cluster keycodes on USB numpads)
    114: "0",          # kVK_Help (PC Insert / '0' / '000' burst)
    117: ".",          # kVK_ForwardDelete (PC Delete / '.')
    119: "1",          # kVK_End (Numpad 1)
    125: "2",          # kVK_DownArrow (Numpad 2)
    121: "3",          # kVK_PageDown (Numpad 3)
    123: "4",          # kVK_LeftArrow (Numpad 4)
    124: "6",          # kVK_RightArrow (Numpad 6)
    126: "8",          # kVK_UpArrow (Numpad 8)
    116: "9",          # kVK_PageUp (Numpad 9)
    115: "Home",       # kVK_Home (top-left Home key or Numpad 7)
}

# USB HID Usage Page 0x07 (Keyboard/Keypad) Usage IDs to Keypad Names:
HID_KEYPAD_USAGE_MAP: dict[int, str] = {
    0x62: "0",          # Keypad 0 and Insert
    0x59: "1",          # Keypad 1 and End
    0x5A: "2",          # Keypad 2 and Down Arrow
    0x5B: "3",          # Keypad 3 and PageDown
    0x5C: "4",          # Keypad 4 and Left Arrow
    0x5D: "5",          # Keypad 5
    0x5E: "6",          # Keypad 6 and Right Arrow
    0x5F: "7",          # Keypad 7 and Home
    0x60: "8",          # Keypad 8 and Up Arrow
    0x61: "9",          # Keypad 9 and PageUp
    0x63: ".",          # Keypad . and Delete
    0x58: "Enter",      # Keypad Enter
    0x57: "+",          # Keypad +
    0x56: "-",          # Keypad -
    0x55: "*",          # Keypad *
    0x54: "/",          # Keypad /
    0x53: "NumLock",    # Keypad NumLock and Clear
    0x2A: "Backspace",  # Keyboard Delete (Backspace)
    0x2B: "Tab",        # Keyboard Tab
    0x4A: "Home",       # Keyboard Home
    0x28: "Enter",      # Keyboard Return
    0x4C: ".",          # Keyboard Delete
    0x4D: "1",          # Keyboard End
    0x4E: "3",          # Keyboard PageDown
    0x4B: "9",          # Keyboard PageUp
    0x51: "2",          # Keyboard DownArrow
    0x52: "8",          # Keyboard UpArrow
    0x50: "4",          # Keyboard LeftArrow
    0x4F: "6",          # Keyboard RightArrow
    0x49: "0",          # Keyboard Insert
    # Fallbacks for standard keyboard number row & symbols
    0x1E: "1",          # Keyboard 1
    0x1F: "2",          # Keyboard 2
    0x20: "3",          # Keyboard 3
    0x21: "4",          # Keyboard 4
    0x22: "5",          # Keyboard 5
    0x23: "6",          # Keyboard 6
    0x24: "7",          # Keyboard 7
    0x25: "8",          # Keyboard 8
    0x26: "9",          # Keyboard 9
    0x27: "0",          # Keyboard 0
    0x2D: "-",          # Keyboard -
    0x2E: "+",          # Keyboard = / +
    0x37: ".",          # Keyboard .
    0x38: "/",          # Keyboard /
}

# USB HID Usage Page 0x0C (Consumer Control) Usage IDs to Keypad Names:
HID_CONSUMER_USAGE_MAP: dict[int, str] = {
    0x0223: "Home",     # AC Home
    0x018A: "Email",    # AL Email Reader
    0x0192: "Calc",     # AL Calculator
    0x0194: "Home",     # AL Local Machine Browser
    0x0196: "Calc",     # AL Internet Browser / Calculator variant
    0x0221: "Home",     # AC Search
    0x0224: "Home",     # AC Back
}

# --- macOS HID usage pages -----------------------------------------------------
# The composite USB keypad exposes two HID interfaces on macOS:
#   * Generic Desktop page 0x01 - PrimaryUsage 6 (Keyboard) / 7 (Keypad) carries
#     the numeric keys (element Usage IDs live on the Keyboard/Keypad page 0x07,
#     e.g. 0x59-0x62 for keypad 1..0) plus operators and navigation codes.
#   * Consumer Control page 0x0C carries Home / Email / Calc.
# Both interfaces must be opened together or the numeric keys are never seen.
HID_PAGE_GENERIC_DESKTOP = 0x01
HID_PAGE_KEYBOARD = 0x07
HID_PAGE_CONSUMER = 0x0C

# IOHIDManager matching dictionaries (kIOHIDDeviceUsagePageKey / kIOHIDDeviceUsageKey):
_MAC_MATCHING_DICTS: tuple[tuple[int, int], ...] = (
    (HID_PAGE_GENERIC_DESKTOP, 6),   # GenericDesktop / Keyboard interface
    (HID_PAGE_GENERIC_DESKTOP, 7),   # GenericDesktop / Keypad interface
    (HID_PAGE_CONSUMER, 1),          # Consumer Control / Consumer interface
)

# macOS kVK keycodes that are ONLY ever produced by an external keypad plus the
# PC navigation cluster codes sent when NumLock is OFF. Tab (48) and Backspace
# (51) are shared with the internal keyboard and are never tapped. The numpad
# cluster (kVK_ANSI_Keypad*) is always safe to swallow; the PC navigation
# cluster (114/115/116/117/119/121/123..126) also exists on internal keyboards,
# so it is only suppressed when a specific external keypad is targeted.
_MAC_SHARED_KEYCODES = frozenset({48, 51})
_MAC_NAV_KEYCODES = frozenset({114, 115, 116, 117, 119, 121, 123, 124, 125, 126})
_MAC_TAP_NUMPAD_CODES = frozenset(
    code for code in MAC_VK
    if code not in _MAC_SHARED_KEYCODES and code not in _MAC_NAV_KEYCODES
)
_MAC_TAP_NAV_CODES = frozenset(MAC_VK.keys()) & _MAC_NAV_KEYCODES
# CGEventTap event mask: KeyDown (10), KeyUp (11), FlagsChanged (12)
_MAC_EVENT_MASK = (1 << 10) | (1 << 11) | (1 << 12)
# kCGKeyboardEventKeycode field index
_MAC_FIELD_KEYCODE = 9


def mac_usage_name(page: int, usage: int) -> Optional[str]:
    """Map an IOHID element (usage page, usage) to a logical numpad key name.

    The Keyboard interface reports elements on the Keyboard/Keypad page 0x07
    (with the device's GenericDesktop 0x01 primary page in some stacks), and the
    Consumer interface reports elements on the Consumer page 0x0C.
    """
    if page in (HID_PAGE_GENERIC_DESKTOP, HID_PAGE_KEYBOARD):
        return HID_KEYPAD_USAGE_MAP.get(usage)
    if page == HID_PAGE_CONSUMER:
        return HID_CONSUMER_USAGE_MAP.get(usage)
    return None


def parse_hid_keyboard_report(
    data: list[int] | bytes,
    prev_keys: set[int],
) -> tuple[list[tuple[str, bool, int]], set[int]]:
    """Parse standard USB HID keyboard report [modifiers, reserved, key1..key6]."""
    if len(data) < 8:
        return [], set(prev_keys)

    offset = 3 if len(data) >= 9 and data[2] == 0 else 2
    raw_keys = set(k for k in data[offset:offset + 6] if k != 0)

    events: list[tuple[str, bool, int]] = []
    for usage in sorted(raw_keys - prev_keys):
        name = HID_KEYPAD_USAGE_MAP.get(usage)
        if name:
            events.append((name, True, usage))

    for usage in sorted(prev_keys - raw_keys):
        name = HID_KEYPAD_USAGE_MAP.get(usage)
        if name:
            events.append((name, False, usage))

    return events, raw_keys


def parse_hid_consumer_report(
    data: list[int] | bytes,
    prev_keys: set[int],
) -> tuple[list[tuple[str, bool, int]], set[int]]:
    """Parse USB HID Consumer Control report (Page 0x0C) for Home, Email, Calc."""
    if len(data) < 2:
        return [], set(prev_keys)
    if len(data) >= 3:
        code = data[1] | (data[2] << 8)
    else:
        code = data[0] | (data[1] << 8)

    events: list[tuple[str, bool, int]] = []
    current_keys = {code} if code != 0 else set()

    for c in current_keys - prev_keys:
        name = HID_CONSUMER_USAGE_MAP.get(c)
        if name:
            events.append((name, True, c))
    for c in prev_keys - current_keys:
        name = HID_CONSUMER_USAGE_MAP.get(c)
        if name:
            events.append((name, False, c))

    return events, current_keys


def win_key_name(vk: int, scan: int, extended: bool, keypad_verified: bool = False) -> Optional[str]:
    """Map a Windows low-level keyboard event to a logical numpad key name.

    Scancode-First Mapping:
    Bypasses Windows NumLock state by checking OEM hardware scancodes first.
    Physical '0' (scancode 0x52) always maps to "0" whether NumLock is ON or OFF.
    Physical '.' (scancode 0x53) always maps to "." whether NumLock is ON or OFF.
    E0-extended navigational variants (NumLock OFF arrows/Home/End, emitted by
    USB keypads as HID navigation usages) intentionally map to ``None`` here -
    they are resolved device-gated by ``KeyHook._ll_resolve_name`` so general
    keyboards (the laptop's real arrows) keep their native keys.
    """
    if IS_WIN and vk in (0, 0xFF) and scan and not extended:
        try:
            mapped = int(_user32.MapVirtualKeyW(scan, _MAPVK_VSC_TO_VK))
            if mapped:
                vk = mapped
        except Exception:
            pass

    # 1. Check non-extended OEM scancodes directly (digits 0-9, dot, operators, NumLock, Backspace, Tab)
    if not extended and scan in _WIN_OEM_SCANCODES_NON_EXTENDED:
        return _WIN_OEM_SCANCODES_NON_EXTENDED[scan]

    # 2. Check extended OEM scancodes directly (/, Enter, Home, Email, Calc)
    if extended and scan in _WIN_OEM_SCANCODES_EXTENDED:
        return _WIN_OEM_SCANCODES_EXTENDED[scan]

    # 3. Special handling for 0/Insert and ./Del when scancode is 0x52 / 0x53 or vk matches
    if scan == 0x52 or vk == VK_INSERT:
        return "0"
    if scan == 0x53 or vk == VK_DELETE:
        return "." if (not extended or keypad_verified) else None

    # 4. Multimedia & special keys fallback by VK or extended scan
    if vk == VK_RETURN:
        return "Enter" if extended else None
    if vk in (VK_BROWSER_HOME, VK_BROWSER_SEARCH) or (scan == _WIN_SCAN_BROWSER_HOME and extended):
        return "Home"
    if vk == VK_LAUNCH_MAIL or (scan == _WIN_SCAN_LAUNCH_MAIL and extended):
        return "Email"
    if vk in (VK_LAUNCH_APP1, VK_LAUNCH_APP2) or (scan == _WIN_SCAN_LAUNCH_CALC and extended):
        return "Calc"
    if vk == VK_HOME and extended:
        return "Home"

    # 5. NumLock VK check
    if vk == VK_NUMLOCK:
        return "NumLock"

    # 6. VK fallbacks if scan was 0 or unmapped
    if not extended and vk in _WIN_NUMLOCK_OFF_VK_MAP:
        return _WIN_NUMLOCK_OFF_VK_MAP[vk]

    return WIN_VK.get(vk)


def zero_vk_label(vk: int = 0, scan: int = 0) -> str:
    """Human-readable source label for a physical '0' keystroke (for diagnostics)."""
    if vk in (VK_NUMPAD0, 82, 0x62):
        base = "VK_NUMPAD0/NumLock-ON"
    elif vk in (VK_INSERT, 114, 0x49):
        base = "VK_INSERT/NumLock-OFF"
    elif vk:
        base = f"VK_0x{vk:02X}"
    else:
        base = "KEYPAD0"
    if scan == _WIN_SCAN_KEYPAD_ZERO:
        base = f"{base} [Scancode 0x52]"
    return base


# --- Win32 Raw Input constants ------------------------------------------------
HID_USAGE_PAGE_GENERIC = 0x01
HID_USAGE_GENERIC_KEYBOARD = 0x06
RIDEV_INPUTSINK = 0x00000100
RIM_TYPEKEYBOARD = 1
RIDI_DEVICENAME = 0x20000007
RID_INPUT = 0x10000003
_WM_INPUT = 0x00FF
RI_KEY_BREAK = 0x0001
RI_KEY_E0 = 0x0002
RI_KEY_E1 = 0x0004


def _clean_reg_desc(val: str) -> str:
    if not val:
        return ""
    if ";" in val:
        val = val.split(";")[-1]
    return val.strip()


def extract_hardware_id(pnp_or_path: str) -> str:
    """Normalize a Win32 Raw Input device path or PnP ID into e.g. 'HID\\VID_04D9&PID_1203'."""
    if not pnp_or_path:
        return ""
    s = pnp_or_path.strip().lstrip("\\?").lstrip("#").replace("#", "\\")
    parts = [p for p in s.split("\\") if p]
    if len(parts) >= 2:
        bus = parts[0].upper()
        dev = parts[1].upper()
        m = re.search(r"(VID_[0-9A-F]{4}&PID_[0-9A-F]{4})", dev, re.I)
        if m:
            return f"{bus}\\{m.group(1).upper()}"
        m2 = re.search(r"VID&[0-9A-F]{2}([0-9A-F]{4})_PID&([0-9A-F]{4})", dev, re.I)
        if m2:
            return f"{bus}\\VID_{m2.group(1).upper()}&PID_{m2.group(2).upper()}"
        return f"{bus}\\{dev}"
    return s.upper()


if IS_WIN:
    LRESULT = ctypes.c_ssize_t
    _HWND_MESSAGE = wintypes.HWND(-3)

    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("vkCode", wintypes.DWORD),
            ("scanCode", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    class RAWINPUTDEVICE(ctypes.Structure):
        _fields_ = [
            ("usUsagePage", wintypes.USHORT),
            ("usUsage", wintypes.USHORT),
            ("dwFlags", wintypes.DWORD),
            ("hwndTarget", wintypes.HWND),
        ]

    class RAWINPUTDEVICELIST(ctypes.Structure):
        _fields_ = [
            ("hDevice", wintypes.HANDLE),
            ("dwType", wintypes.DWORD),
        ]

    class RAWINPUTHEADER(ctypes.Structure):
        _fields_ = [
            ("dwType", wintypes.DWORD),
            ("dwSize", wintypes.DWORD),
            ("hDevice", wintypes.HANDLE),
            ("wParam", wintypes.WPARAM),
        ]

    class RAWKEYBOARD(ctypes.Structure):
        _fields_ = [
            ("MakeCode", wintypes.USHORT),
            ("Flags", wintypes.USHORT),
            ("Reserved", wintypes.USHORT),
            ("VKey", wintypes.USHORT),
            ("Message", wintypes.UINT),
            ("ExtraInformation", wintypes.ULONG),
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
    _user32.CreateWindowExW.restype = wintypes.HWND
    _user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    ]
    _user32.DestroyWindow.restype = wintypes.BOOL
    _user32.DestroyWindow.argtypes = [wintypes.HWND]
    _user32.RegisterRawInputDevices.restype = wintypes.BOOL
    _user32.RegisterRawInputDevices.argtypes = [
        ctypes.POINTER(RAWINPUTDEVICE), wintypes.UINT, wintypes.UINT
    ]
    _user32.GetRawInputDeviceList.restype = wintypes.UINT
    _user32.GetRawInputDeviceList.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(wintypes.UINT), wintypes.UINT
    ]
    _user32.GetRawInputDeviceInfoW.restype = wintypes.UINT
    _user32.GetRawInputDeviceInfoW.argtypes = [
        wintypes.HANDLE, wintypes.UINT, ctypes.c_void_p, ctypes.POINTER(wintypes.UINT)
    ]
    _user32.GetRawInputData.restype = wintypes.UINT
    _user32.GetRawInputData.argtypes = [
        wintypes.HANDLE, wintypes.UINT, ctypes.c_void_p,
        ctypes.POINTER(wintypes.UINT), wintypes.UINT,
    ]
    _user32.MapVirtualKeyW.restype = wintypes.UINT
    _user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
    _kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    _kernel32.GetCurrentThread.restype = wintypes.HANDLE
    _kernel32.SetThreadPriority.restype = wintypes.BOOL
    _kernel32.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]


def _lookup_pnp_friendly_name(pnp_id: str, hw_id: str) -> str:
    """Resolve a human-readable keyboard description from the Windows PnP registry."""
    if not IS_WIN:
        return "Keyboard Device"
    desc = ""
    reg_path = f"SYSTEM\\CurrentControlSet\\Enum\\{pnp_id}"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, reg_path) as k:
            for field in ("FriendlyName", "DeviceDesc"):
                try:
                    val, _ = winreg.QueryValueEx(k, field)
                    cleaned = _clean_reg_desc(str(val))
                    if cleaned:
                        desc = cleaned
                        break
                except OSError:
                    pass
    except OSError:
        pass
    # Also check USB parent node if VID_xxxx&PID_xxxx is available
    m = re.search(r"(VID_[0-9A-F]{4}&PID_[0-9A-F]{4})", hw_id, re.I)
    if m:
        usb_root = f"SYSTEM\\CurrentControlSet\\Enum\\USB\\{m.group(1).upper()}"
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, usb_root) as uk:
                sub_idx = 0
                while True:
                    try:
                        sub = winreg.EnumKey(uk, sub_idx)
                    except OSError:
                        break
                    sub_idx += 1
                    try:
                        with winreg.OpenKey(uk, sub) as sk:
                            for field in ("FriendlyName", "BusReportedDeviceDesc", "DeviceDesc"):
                                try:
                                    val, _ = winreg.QueryValueEx(sk, field)
                                    cleaned = _clean_reg_desc(str(val))
                                    if cleaned and cleaned.lower() not in (
                                        "usb input device",
                                        "usb composite device",
                                    ):
                                        return f"{cleaned} - USB"
                                except OSError:
                                    pass
                    except OSError:
                        pass
        except OSError:
            pass
    if not desc:
        desc = "HID Keyboard Device" if hw_id.startswith("HID\\") else "Keyboard Device"
    if hw_id.startswith("HID\\") and "USB" not in desc.upper():
        desc = f"{desc} - USB"
    return desc


def _query_raw_device_path(h_device: int) -> str:
    """Query RIDI_DEVICENAME for a Raw Input device handle on Windows."""
    if not IS_WIN or not h_device:
        return ""
    try:
        size = wintypes.UINT(0)
        r1 = _user32.GetRawInputDeviceInfoW(
            wintypes.HANDLE(h_device), RIDI_DEVICENAME, None, ctypes.byref(size)
        )
        if r1 != 0xFFFFFFFF and size.value > 0:
            nbuf = ctypes.create_unicode_buffer(size.value + 2)
            r2 = _user32.GetRawInputDeviceInfoW(
                wintypes.HANDLE(h_device), RIDI_DEVICENAME, nbuf, ctypes.byref(size)
            )
            if r2 != 0xFFFFFFFF and nbuf.value:
                return nbuf.value
    except Exception:
        pass
    return ""


def check_macos_accessibility(prompt: bool = True) -> bool:
    """Check if the process has macOS Accessibility permission.

    If prompt=True and permission is not granted, macOS displays a system dialog
    directing the user to System Settings > Privacy & Security > Accessibility.
    """
    if not IS_MAC:
        return True
    try:
        from ctypes import (
            POINTER, byref, c_bool, c_char_p, c_long, c_uint32, c_void_p, cdll, util,
        )

        app_services_path = (
            util.find_library("ApplicationServices")
            or "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
        )
        cf_path = (
            util.find_library("CoreFoundation")
            or "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )

        app_services = cdll.LoadLibrary(app_services_path)
        cf = cdll.LoadLibrary(cf_path)

        ax_is_trusted = app_services.AXIsProcessTrustedWithOptions
        ax_is_trusted.argtypes = [c_void_p]
        ax_is_trusted.restype = c_bool

        if not prompt:
            return bool(ax_is_trusted(None))

        k_cf_boolean_true = c_void_p.in_dll(cf, "kCFBooleanTrue")

        cf_string_create = cf.CFStringCreateWithCString
        cf_string_create.argtypes = [c_void_p, c_char_p, c_uint32]
        cf_string_create.restype = c_void_p

        cf_dictionary_create = cf.CFDictionaryCreate
        cf_dictionary_create.argtypes = [
            c_void_p,
            POINTER(c_void_p),
            POINTER(c_void_p),
            c_long,
            c_void_p,
            c_void_p,
        ]
        cf_dictionary_create.restype = c_void_p

        cf_release = cf.CFRelease
        cf_release.argtypes = [c_void_p]

        key_str = cf_string_create(None, b"AXTrustedCheckOptionPrompt", 0x08000100)
        try:
            keys = (c_void_p * 1)(key_str)
            values = (c_void_p * 1)(k_cf_boolean_true.value)
            options = cf_dictionary_create(None, keys, values, 1, None, None)
            try:
                trusted = bool(ax_is_trusted(options))
                return trusted
            finally:
                if options:
                    cf_release(options)
        finally:
            if key_str:
                cf_release(key_str)
    except Exception as exc:
        logger.warning("check_macos_accessibility error: %s", exc)
        return True


def check_macos_permissions(prompt: bool = True, suppress: bool = False) -> bool:
    """Check macOS permissions required by the currently configured capture strategy.

    Raw HID capture (IOHIDManager listening on both composite interfaces, or
    hidapi) requires no macOS permission. Swallowing keypad keys ("Exclusive
    Mode") requires a CGEventTap, which needs the *Accessibility* permission
    (System Settings > Privacy & Security). When ``suppress`` is False the
    check passes unconditionally, so permissionless environments (e.g. CI) are
    never blocked.
    """
    if not IS_MAC:
        return True
    if not suppress:
        return True
    return check_macos_accessibility(prompt=prompt)


def list_macos_hid_devices() -> list[KeyboardDevice]:
    """Enumerate connected USB keyboard/keypad devices on macOS via hidapi."""
    devices: list[KeyboardDevice] = []
    if not HAS_HID:
        return devices
    try:
        raw_list = hid.enumerate()
    except Exception as exc:
        logger.warning("hid.enumerate failed on macOS: %s", exc)
        return devices

    seen: dict[str, KeyboardDevice] = {}
    for d in raw_list:
        usage_page = d.get("usage_page", 0)
        usage = d.get("usage", 0)
        is_kb = (usage_page == 1 and usage in (6, 7))
        is_consumer = (usage_page == 0x0C)
        if not (is_kb or is_consumer or usage_page in (0, 1)):
            continue

        raw_path = d.get("path")
        if not raw_path:
            continue
        path_str = raw_path.decode("utf-8", errors="replace") if isinstance(raw_path, bytes) else str(raw_path)

        vid = d.get("vendor_id", 0)
        pid = d.get("product_id", 0)
        prod = d.get("product_string") or "USB Keypad"
        hw_id = f"HID\\VID_{vid:04X}&PID_{pid:04X}"
        label = f"{prod} - USB ({hw_id})" if "USB" not in prod.upper() else f"{prod} ({hw_id})"

        h_val = abs(hash(path_str)) & 0x7FFFFFFF
        if hw_id in seen:
            seen[hw_id].handles.add(h_val)
            if path_str not in seen[hw_id].raw_path:
                seen[hw_id].raw_path = f"{seen[hw_id].raw_path};{path_str}"
        else:
            dev = KeyboardDevice(device_id=hw_id, label=label, raw_path=path_str, handles={h_val})
            seen[hw_id] = dev
            devices.append(dev)

    return devices


def list_macos_keyboard_devices() -> list[KeyboardDevice]:
    """Enumerate connected USB keyboard/keypad devices on macOS via IOKit IOHIDManager."""
    if not IS_MAC:
        return []
    devices: list[KeyboardDevice] = []
    try:
        from ctypes import (
            POINTER, byref, c_bool, c_char_p, c_int32, c_long, c_uint32, c_void_p, cdll,
            create_string_buffer, util,
        )

        iokit_path = (
            util.find_library("IOKit")
            or "/System/Library/Frameworks/IOKit.framework/IOKit"
        )
        cf_path = (
            util.find_library("CoreFoundation")
            or "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )

        iokit = cdll.LoadLibrary(iokit_path)
        cf = cdll.LoadLibrary(cf_path)

        # CF function declarations
        cf.CFRelease.argtypes = [c_void_p]
        cf.CFSetGetCount.argtypes = [c_void_p]
        cf.CFSetGetCount.restype = c_long
        cf.CFSetGetValues.argtypes = [c_void_p, POINTER(c_void_p)]
        cf.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, c_uint32]
        cf.CFStringCreateWithCString.restype = c_void_p
        cf.CFStringGetCString.argtypes = [c_void_p, c_char_p, c_long, c_uint32]
        cf.CFStringGetCString.restype = c_bool
        cf.CFNumberGetValue.argtypes = [c_void_p, c_int32, c_void_p]
        cf.CFNumberGetValue.restype = c_bool

        # IOHIDManager function declarations
        iokit.IOHIDManagerCreate.argtypes = [c_void_p, c_uint32]
        iokit.IOHIDManagerCreate.restype = c_void_p
        iokit.IOHIDManagerSetDeviceMatching.argtypes = [c_void_p, c_void_p]
        iokit.IOHIDManagerOpen.argtypes = [c_void_p, c_uint32]
        iokit.IOHIDManagerOpen.restype = c_int32
        iokit.IOHIDManagerClose.argtypes = [c_void_p, c_uint32]
        iokit.IOHIDManagerClose.restype = c_int32
        iokit.IOHIDManagerCopyDevices.argtypes = [c_void_p]
        iokit.IOHIDManagerCopyDevices.restype = c_void_p
        iokit.IOHIDDeviceGetProperty.argtypes = [c_void_p, c_void_p]
        iokit.IOHIDDeviceGetProperty.restype = c_void_p

        manager = iokit.IOHIDManagerCreate(None, 0)
        if not manager:
            return []

        try:
            iokit.IOHIDManagerSetDeviceMatching(manager, None)
            iokit.IOHIDManagerOpen(manager, 0)
            dev_set = iokit.IOHIDManagerCopyDevices(manager)
            if not dev_set:
                return []

            try:
                count = int(cf.CFSetGetCount(dev_set))
                if count <= 0:
                    return []
                dev_array = (c_void_p * count)()
                cf.CFSetGetValues(dev_set, dev_array)

                k_product = cf.CFStringCreateWithCString(None, b"Product", 0x08000100)
                k_vid = cf.CFStringCreateWithCString(None, b"VendorID", 0x08000100)
                k_pid = cf.CFStringCreateWithCString(None, b"ProductID", 0x08000100)
                k_page = cf.CFStringCreateWithCString(None, b"PrimaryUsagePage", 0x08000100)
                k_usage = cf.CFStringCreateWithCString(None, b"PrimaryUsage", 0x08000100)
                k_entry_id = cf.CFStringCreateWithCString(None, b"IORegistryEntryID", 0x08000100)

                seen_ids: dict[str, KeyboardDevice] = {}

                for i in range(count):
                    dev_ref = dev_array[i]
                    if not dev_ref:
                        continue

                    usage_page = c_int32(0)
                    page_prop = iokit.IOHIDDeviceGetProperty(dev_ref, k_page)
                    if page_prop:
                        cf.CFNumberGetValue(page_prop, 3, byref(usage_page))

                    usage = c_int32(0)
                    usage_prop = iokit.IOHIDDeviceGetProperty(dev_ref, k_usage)
                    if usage_prop:
                        cf.CFNumberGetValue(usage_prop, 3, byref(usage))

                    is_keyboard = (
                        usage_page.value in (0, HID_PAGE_GENERIC_DESKTOP)
                        and usage.value in (0, 6, 7)
                    )
                    is_consumer = usage_page.value == HID_PAGE_CONSUMER
                    if not (is_keyboard or is_consumer):
                        # Skip joysticks, mice, digitizers, vendor-defined, ...
                        continue

                    vid = c_int32(0)
                    vid_prop = iokit.IOHIDDeviceGetProperty(dev_ref, k_vid)
                    if vid_prop:
                        cf.CFNumberGetValue(vid_prop, 3, byref(vid))

                    pid = c_int32(0)
                    pid_prop = iokit.IOHIDDeviceGetProperty(dev_ref, k_pid)
                    if pid_prop:
                        cf.CFNumberGetValue(pid_prop, 3, byref(pid))

                    product_str = "USB Keypad"
                    prod_prop = iokit.IOHIDDeviceGetProperty(dev_ref, k_product)
                    if prod_prop:
                        buf = create_string_buffer(256)
                        if cf.CFStringGetCString(prod_prop, buf, 256, 0x08000100):
                            product_str = buf.value.decode("utf-8", errors="replace")

                    handle_val = int(dev_ref)
                    entry_prop = iokit.IOHIDDeviceGetProperty(dev_ref, k_entry_id)
                    if entry_prop:
                        entry_num = c_long(0)
                        if cf.CFNumberGetValue(entry_prop, 4, byref(entry_num)):
                            handle_val = int(entry_num.value)

                    hw_id = f"HID\\VID_{vid.value:04X}&PID_{pid.value:04X}"
                    label = (
                        f"{product_str} - USB ({hw_id})"
                        if "USB" not in product_str.upper()
                        else f"{product_str} ({hw_id})"
                    )

                    if hw_id in seen_ids:
                        seen_ids[hw_id].handles.add(handle_val)
                    else:
                        dev = KeyboardDevice(hw_id, label, f"IOHID:{hw_id}", {handle_val})
                        seen_ids[hw_id] = dev
                        devices.append(dev)

                for k in (k_product, k_vid, k_pid, k_page, k_usage, k_entry_id):
                    if k:
                        cf.CFRelease(k)
            finally:
                cf.CFRelease(dev_set)
        finally:
            iokit.IOHIDManagerClose(manager, 0)
            cf.CFRelease(manager)
    except Exception as exc:
        logger.warning("list_macos_keyboard_devices failed: %s", exc)

    return devices


def list_keyboard_devices() -> list[KeyboardDevice]:
    """Enumerate connected keyboard/keypad devices via Win32 Raw Input API or macOS IOKit."""
    if IS_MAC:
        hid_devs = list_macos_hid_devices()
        kb_devs = list_macos_keyboard_devices()
        if hid_devs and kb_devs:
            by_id = {d.device_id.upper(): d for d in hid_devs}
            for kd in kb_devs:
                hw = kd.device_id.upper()
                if hw in by_id:
                    by_id[hw].handles.update(kd.handles)
                else:
                    by_id[hw] = kd
                    hid_devs.append(kd)
            return hid_devs
        return hid_devs or kb_devs
    if not IS_WIN:
        return []
    raw_handles: list[int] = []
    handle_paths: dict[int, str] = {}
    try:
        num = wintypes.UINT(0)
        _user32.GetRawInputDeviceList(
            None, ctypes.byref(num), ctypes.sizeof(RAWINPUTDEVICELIST)
        )
        if num.value > 0:
            buf = (RAWINPUTDEVICELIST * (num.value + 4))()
            count = wintypes.UINT(num.value + 4)
            res = _user32.GetRawInputDeviceList(
                ctypes.byref(buf), ctypes.byref(count), ctypes.sizeof(RAWINPUTDEVICELIST)
            )
            if res != 0xFFFFFFFF:
                for i in range(int(res)):
                    if buf[i].dwType == RIM_TYPEKEYBOARD and buf[i].hDevice:
                        h = int(buf[i].hDevice)
                        raw_handles.append(h)
                        path = _query_raw_device_path(h)
                        if path:
                            handle_paths[h] = path
    except Exception as exc:
        logger.warning("GetRawInputDeviceList failed: %s", exc)

    # Read kbdclass\Enum as fallback / enrichment when device paths are restricted
    enum_pnp_ids: list[str] = []
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Services\kbdclass\Enum"
        ) as k:
            cnt, _ = winreg.QueryValueEx(k, "Count")
            for idx in range(int(cnt)):
                pnp_id, _ = winreg.QueryValueEx(k, str(idx))
                if pnp_id:
                    enum_pnp_ids.append(str(pnp_id))
    except OSError:
        pass

    devices_by_id: dict[str, KeyboardDevice] = {}
    total = max(len(raw_handles), len(enum_pnp_ids))
    for idx in range(total):
        h = raw_handles[idx] if idx < len(raw_handles) else 0
        path = handle_paths.get(h, "")
        if path:
            cleaned = path.lstrip("\\?").lstrip("#")
            parts = cleaned.split("#")
            pnp_id = "\\".join(parts[:3]) if len(parts) >= 3 else cleaned
        elif idx < len(enum_pnp_ids):
            pnp_id = enum_pnp_ids[idx]
            path = f"\\\\?\\{pnp_id.replace(chr(92), '#')}"
        else:
            pnp_id = f"HID\\KEYBOARD_{idx + 1}"
            path = pnp_id

        hw_id = extract_hardware_id(pnp_id)
        if not hw_id:
            continue
        desc = _lookup_pnp_friendly_name(pnp_id, hw_id)
        label = f"{desc} ({hw_id})"
        existing = devices_by_id.get(hw_id)
        if existing is None:
            handles = {h} if h else set()
            devices_by_id[hw_id] = KeyboardDevice(hw_id, label, path, handles)
        elif h:
            existing.handles.add(h)

    return list(devices_by_id.values())


KeyCallback = Callable[[str, bool], None]


class KeyHook:
    """Debounced global numpad listener with optional Raw Input USB device filtering.

    ``on_key(name, is_down)`` is called from the worker (or timer) thread.
    """

    TRIPLE_STALE_SECONDS = 3.0

    def __init__(
        self,
        on_key: KeyCallback,
        *,
        detect_triple_zero: bool = True,
        burst_window_ms: float = 100.0,
        suppress: bool = True,
        target_device: str = ALL_DEVICES_ID,
    ) -> None:
        self._on_key = on_key
        self.detect_triple_zero = detect_triple_zero
        self.burst_window = max(0.005, burst_window_ms / 1000.0)
        self._suppress = bool(suppress)
        self._target_device = (target_device or "").strip()

        self._lock = threading.RLock()
        self._down: set[str] = set()
        # v1.9.5: pads currently held as seen by the MIDI sources (WM_INPUT
        # and the LL media-key channel). Debounces OS auto-repeat and
        # guarantees every Note ON is paired with exactly one Note OFF.
        self._pad_down: set[str] = set()
        # 000-burst detection state (NumLock-independent triplet detector)
        self._zbuf: list[bool] = []
        self._zlast_ts: float = 0.0
        self._last_zero_ts: float = 0.0
        self._zlast_vk: int = 0
        self._timer: Optional[threading.Timer] = None
        self._triple_timer: Optional[threading.Timer] = None
        self._triple_ups_left = 0
        self._triple_started = 0.0

        # Raw Input device filtering state
        self._devices: list[KeyboardDevice] = []
        self._device_by_handle: dict[int, KeyboardDevice] = {}
        self._target_handles: set[int] = set()
        self._last_raw_hdevice: int = 0
        self._last_raw_matched: bool = True
        self._last_raw_time: float = 0.0
        self._pending_raw_dedup: list[tuple[str, bool, float]] = []

        # Raw Input origin correlation state (device-aware WH_KEYBOARD_LL
        # suppression). Only touched from the hook pump thread - the WM_INPUT
        # handler and the LL callback run sequentially on that one thread -
        # and from unit tests, so no locking is required.
        self._raw_last_by_key: dict[tuple[int, bool], tuple[int, float]] = {}
        self._raw_recent_hdevice: int = 0
        self._raw_recent_ts: float = 0.0
        self._general_kb_handles: set[int] = set()
        self._matched_handle_cache: dict[int, bool] = {}

        # Non-Windows fallback listener & macOS native IOHIDManager / CGEventTap state
        self._listener = None
        self._mac_thread: Optional[threading.Thread] = None
        self._mac_iohid_manager = None
        self._mac_value_cb_ref = None
        self._mac_tap_thread: Optional[threading.Thread] = None
        self._mac_tap_ref = None          # CFMachPortRef of the CGEventTap
        self._mac_tap_cb_ref = None       # keep the ctypes callback alive
        self._mac_tap_src_ref = None      # CFRunLoopSourceRef of the tap
        self._mac_tap_granted = False     # tap successfully created (Accessibility)
        # Windows native WH_KEYBOARD_LL + WM_INPUT state
        self._running = False
        self._win_thread: Optional[threading.Thread] = None
        self._win_worker: Optional[threading.Thread] = None
        self._win_thread_id: int = 0
        self._win_hook_handle = None
        self._win_raw_hwnd = None
        self._win_proc_ref = None  # prevent GC of ctypes callback
        self._win_queue: queue.SimpleQueue[Optional[tuple]] = queue.SimpleQueue()
        self._win_ready = threading.Event()
        self.refresh_devices()

    # ------------------------------------------------------------------ control
    def set_suppress(self, value: bool) -> None:
        """Enable or disable Exclusive Mode (swallowing numpad keys from the OS)."""
        self._suppress = bool(value)

    @property
    def target_device(self) -> str:
        return self._target_device

    def set_target_device(self, device_id: str) -> None:
        """Select a specific USB keypad hardware ID, or '' for all connected keypads."""
        with self._lock:
            self._target_device = (device_id or "").strip()
            self._rebuild_target_handles()
            self._last_raw_matched = True
            self._rebuild_matched_handle_cache()

    def refresh_devices(self) -> list[KeyboardDevice]:
        """Re-enumerate connected keyboard/keypad devices via Win32 Raw Input API."""
        devices = list_keyboard_devices()
        with self._lock:
            self._devices = devices
            self._device_by_handle = {}
            for dev in devices:
                for h in dev.handles:
                    self._device_by_handle[h] = dev
            self._rebuild_target_handles()
            self._rebuild_matched_handle_cache()
        return list(devices)

    def _rebuild_matched_handle_cache(self) -> None:
        """Refresh the matched-state cache used by the WH_KEYBOARD_LL hot path."""
        self._matched_handle_cache = {
            h: self.matches_device(h) for h in list(self._device_by_handle)
        }

    def _rebuild_target_handles(self) -> None:
        self._target_handles.clear()
        target = self._target_device.upper()
        if not target:
            return
        for dev in self._devices:
            if (
                target == dev.device_id.upper()
                or target in dev.device_id.upper()
                or target in dev.raw_path.upper()
                or target == dev.label.upper()
            ):
                self._target_handles.update(dev.handles)

    def matches_device(self, h_device: int) -> bool:
        """Return True if Raw Input handle ``h_device`` matches the selected target device."""
        target = self._target_device
        if not target:
            return True
        if not h_device:
            return False
        with self._lock:
            if h_device in self._target_handles:
                return True
            target_up = target.upper()
            if target_up == f"HDEVICE:{h_device}" or target_up == str(h_device):
                self._target_handles.add(h_device)
                return True
            if h_device not in self._device_by_handle:
                path = _query_raw_device_path(h_device)
                hw_id = extract_hardware_id(path) if path else f"HDEVICE:{h_device}"
                dev = KeyboardDevice(hw_id, hw_id, path, {h_device})
                self._device_by_handle[h_device] = dev
                if (
                    target_up == hw_id.upper()
                    or target_up in hw_id.upper()
                    or (path and target_up in path.upper())
                ):
                    self._target_handles.add(h_device)
                    return True
            return False

    def _origin_matches(self, h_device: int) -> bool:
        """Matched-state for a tracked Raw Input origin handle (LL hot path).

        ``h_device`` normally comes from WM_INPUT tracking, whose handler keeps
        ``_matched_handle_cache`` current; unknown handles fall back to a full
        device match.
        """
        if not h_device:
            return False
        if not self._target_device:
            return True
        cached = self._matched_handle_cache.get(h_device)
        if cached is None:
            cached = self.matches_device(h_device)
            self._matched_handle_cache[h_device] = cached
        return cached

    def _ll_origin_device(self, vk: int, extended: bool, now: float) -> int:
        """Best-effort ``hDevice`` of the keystroke WH_KEYBOARD_LL is reporting.

        The LL callback always fires before the matching WM_INPUT is processed,
        so the origin is correlated from earlier Raw Input data: the per-key
        memory wins, unless a flagged general keyboard (the user actively
        typing) is hot in the active-device window - then that device wins, so
        a stale per-key record cannot swallow keystrokes of a device switch.
        Returns 0 when the origin cannot be correlated at all.
        """
        entry = self._raw_last_by_key.get((vk, bool(extended)))
        if entry and (now - entry[1]) <= _RAW_KEY_TTL:
            key_device = entry[0]
            if (
                self._raw_recent_ts > 0.0
                and (now - self._raw_recent_ts) <= _RAW_RECENT_TTL
                and key_device != self._raw_recent_hdevice
                and self._raw_recent_hdevice in self._general_kb_handles
            ):
                return self._raw_recent_hdevice
            return key_device
        if self._raw_recent_ts > 0.0 and (now - self._raw_recent_ts) <= _RAW_RECENT_TTL:
            return self._raw_recent_hdevice
        return 0

    def _ll_nav_origin_verified(self, origin: int) -> bool:
        """True when ``origin`` is a keypad-like device under the current mode.

        Gate for the NumLock-OFF navigational swallow list: a general typing
        keyboard (flagged via Raw Input) or an uncorrelated origin is never
        trusted, so the laptop's real arrow/Home/End keys keep their native
        OS function and never trigger numpad handling.
        """
        if not origin:
            return False
        if self._target_device:
            return self._origin_matches(origin)
        return origin not in self._general_kb_handles

    def _ll_resolve_name(
        self, vk: int, scan: int, extended: bool, origin: int, origin_matched: bool
    ) -> Optional[str]:
        """Map a WH_KEYBOARD_LL event to a numpad key name (device-gated).

        Standard scancode-first mapping first; for NumLock-OFF navigational
        variants (vk in ``_NUMPAD_SWALLOW_VKS``) whose origin correlates with
        a keypad-like device, the constant physical scancode overrides the
        name so the correct MIDI note fires regardless of whether Windows
        translated the key as ``VK_NUMPAD2`` or ``VK_DOWN`` (the E0-extended
        arrows/Home/End previously fell through to ``None`` and leaked into
        DAWs). General keyboards are never overridden.
        """
        name = win_key_name(
            vk, scan, extended,
            keypad_verified=origin_matched and bool(self._target_device),
        )
        if vk in _NUMPAD_SWALLOW_VKS and self._ll_nav_origin_verified(origin):
            nav_name = _WIN_OEM_SCANCODES_NON_EXTENDED.get(
                scan
            ) or _WIN_KEYPAD_SCAN_OVERRIDES.get(scan)
            if nav_name:
                name = nav_name
        return name

    def _ll_should_swallow(
        self, name: str, vk: int, scan: int, extended: bool, now: float
    ) -> bool:
        """True when WH_KEYBOARD_LL must swallow (``return 1``) this keystroke.

        v1.9.4 architecture: the LL hook ONLY blocks keystrokes; WM_INPUT owns
        100% of the MIDI generation (exact ``hDevice`` match), so a failed LL
        device correlation can never make another keyboard trigger notes.
        Swallowing is strictly device-gated: only keystrokes correlated (via
        the Raw Input origin tracking) with the selected USB keypad are
        blocked; keys from any other keyboard fall through to
        ``CallNextHookEx`` untouched, and unattributed events (origin 0) are
        never swallowed either.
        """
        # VK_RETURN is shared between the main Enter and Numpad Enter: only
        # the E0-extended variant (LLKHF_EXTENDED) is a numpad key. The main
        # Enter is never swallowed; plain-Return keypads still get their MIDI
        # via the WM_INPUT channel.
        if vk == VK_RETURN and not extended:
            return False

        origin = self._ll_origin_device(vk, extended, now)

        if vk == VK_NUMLOCK or name == "NumLock" or (scan == 0x45 and not extended):
            # Keypad NumLock stays blocked even for unattributed events:
            # passing it through would toggle the M-Tech hardware into
            # navigation mode. Keystrokes proven to come from a general
            # keyboard reach the OS untouched.
            if self._target_device:
                if not origin:
                    return True
                return self._origin_matches(origin)
            return not (origin and origin in self._general_kb_handles)

        if vk in _NUMPAD_SWALLOW_VKS:
            # Comprehensive keypad swallow list (digits, operators,
            # NumLock-OFF navigational variants, Enter/Backspace/Tab): the
            # selected keypad's variants never reach the OS, while the
            # laptop's real navigation keys pass through untouched
            # (device-gated). Exclusive Mode (``self._suppress``) still
            # controls whether keypad keys are blocked, as in v1.6.
            if self._target_device:
                if not origin:
                    return False
                return self._origin_matches(origin) and bool(self._suppress)
            return (
                bool(origin)
                and origin not in self._general_kb_handles
                and bool(self._suppress)
            )

        if self._target_device:
            if not origin:
                # Strict device isolation: an unattributed keystroke is never
                # blocked (it could belong to the laptop), and its MIDI comes
                # from the exact-match WM_INPUT channel anyway.
                return False
            return self._origin_matches(origin) and bool(self._suppress)

        # "All Connected Keypads (Default)" mode:
        if not origin:
            return False
        if origin in self._general_kb_handles:
            # General typing keyboard (e.g. the laptop's built-in one): the
            # OS keystroke is never swallowed (WM_INPUT still plays MIDI as
            # v1.6 did).
            return False
        return bool(self._suppress)

    def _ll_handle_event(
        self, name: str, vk: int, scan: int, extended: bool, now: float, is_down: bool
    ) -> bool:
        """Handle one WH_KEYBOARD_LL event; returns True to swallow (return 1).

        WM_INPUT owns the MIDI generation for every Raw Input trackable key
        (exact ``hDevice`` match in ``_process_raw_keyboard``); the LL channel
        only swallows device-correlated keypad keystrokes. The sole exception
        is the multimedia launcher keys (Browser Home/Search, Mail, Calc):
        they are HID Consumer Control events that never emit RIM_TYPEKEYBOARD
        WM_INPUT packets, so the LL channel is their only MIDI source. The
        dedicated Home key of a normal keyboard (VK_HOME + E0, scancode 0x47)
        IS Raw Input trackable and is deliberately excluded from that branch
        so it stays device-gated like every other key.

        v1.9.5 (stuck-keys fix): key-UP events of trackable keys are NEVER
        swallowed - they always fall through to ``CallNextHookEx``. A break
        event that is removed from the input stream (``return 1``) is the one
        edge whose loss produces a hanging MIDI note (Note ON without Note
        OFF) and a permanently lit pad, and a swallowed break whose make
        reached the OS also leaves the OS key state stuck down. A leaked
        break is harmless: the OS has no matching make (it was swallowed) or
        already released the key, DAW shortcuts (e.g. Cubase "Play") trigger
        on key-DOWN only, and text input produces nothing from a lone key-up.
        Media keys keep the queue-then-swallow contract on BOTH edges: their
        Note OFF is queued before ``return 1`` (their only MIDI source), and
        they never emit WM_INPUT so nothing is lost from that channel.
        """
        if name in _ALWAYS_SUPPRESS_KEYS and not (vk == VK_HOME and extended):
            self._queue_from_ll(name, is_down, vk, scan)
            return True
        if not is_down:
            return False
        return self._ll_should_swallow(name, vk, scan, extended, now)

    def _queue_from_ll(self, name: str, is_down: bool, vk: int = 0, scan: int = 0) -> None:
        """Queue an event intercepted in WH_KEYBOARD_LL and record it to prevent WM_INPUT duplication.

        v1.9.5 pad-state debounce: OS auto-repeat floods the LL channel with
        WM_KEYDOWN while a pad is held; only the first make is queued for MIDI
        (no Note ON spam). Breaks are ALWAYS queued so every Note ON gets its
        Note OFF even when the make was debounced or lost.
        """
        now = time.perf_counter()
        if is_down:
            if name in self._pad_down:
                return  # auto-repeat: no MIDI, no UI update
            self._pad_down.add(name)
        else:
            self._pad_down.discard(name)
        # 50 ms dedup window, clearing opposite edge for this key to prevent dedup cross-stroke bleed
        self._pending_raw_dedup = [
            item for item in self._pending_raw_dedup
            if (now - item[2] < 0.050 and item[0] != name)
        ]
        self._pending_raw_dedup.append((name, is_down, now))
        self._win_queue.put_nowait((name, is_down, vk, scan))

    def _process_raw_keyboard(self, h_device: int, vk: int, scan: int, flags: int) -> bool:
        """Filter and dispatch a Win32 RAWKEYBOARD event by its ``hDevice``."""
        now = time.perf_counter()
        h_device = int(h_device)
        extended = bool(flags & RI_KEY_E0)
        # --- Raw Input origin tracking (runs for EVERY device, matched or not) ---
        self._raw_last_by_key[(vk, extended)] = (h_device, now)
        self._raw_recent_hdevice = h_device
        self._raw_recent_ts = now
        if vk in _GENERAL_KB_VKS:
            self._general_kb_handles.add(h_device)
        matched = self.matches_device(h_device)
        self._matched_handle_cache[h_device] = matched
        self._last_raw_hdevice = h_device
        self._last_raw_matched = matched
        self._last_raw_time = now
        self._pending_raw_dedup = [
            item for item in self._pending_raw_dedup if now - item[2] < 0.050
        ]
        if not matched:
            # Strict device isolation: only the exact selected device (UI
            # dropdown) generates MIDI here; every other keyboard - e.g. the
            # laptop's built-in one - is dropped by its Raw Input handle.
            logger.debug(
                "WM_INPUT dropped: hDevice=0x%X vk=0x%02X scan=0x%02X does not "
                "match target %r", h_device, vk, scan, self._target_device,
            )
            return False
        is_down = not bool(flags & RI_KEY_BREAK)
        name = win_key_name(vk, scan, extended, keypad_verified=bool(self._target_device))
        if vk in _NUMPAD_SWALLOW_VKS and (
            self._target_device or h_device not in self._general_kb_handles
        ):
            # NumLock-OFF navigational variant (e.g. '2' reported as VK_DOWN):
            # map by the constant physical scancode so the correct MIDI note
            # fires via WM_INPUT too, regardless of the VK Windows translated.
            # Flagged general keyboards (the laptop) are never overridden.
            nav_name = _WIN_OEM_SCANCODES_NON_EXTENDED.get(
                scan
            ) or _WIN_KEYPAD_SCAN_OVERRIDES.get(scan)
            if nav_name:
                name = nav_name
        if name is None:
            return False
        for idx, (q_name, q_down, _) in enumerate(self._pending_raw_dedup):
            if q_name == name and q_down == is_down:
                del self._pending_raw_dedup[idx]
                return True
        # Purge any opposite-edge dedup record for this key
        self._pending_raw_dedup = [
            item for item in self._pending_raw_dedup if item[0] != name or item[1] == is_down
        ]
        # v1.9.5 pad-state debounce (stuck-keys fix): the first make of a
        # physical press is queued for MIDI; OS auto-repeat makes are consumed
        # here (correlation above still ran) so neither MIDI nor the UI sees
        # Note ON spam. Breaks are ALWAYS queued - every Note ON is guaranteed
        # exactly one Note OFF, and a break whose make was dropped (device
        # gate flicker, name-less make, app started mid-press) still flows so
        # no note can hang.
        if is_down:
            if name in self._pad_down:
                logger.debug(
                    "WM_INPUT auto-repeat consumed: vk=0x%02X scan=0x%02X name=%r",
                    vk, scan, name,
                )
                return True
            self._pad_down.add(name)
        else:
            if name not in self._pad_down:
                # Make never arrived here; queue the break anyway so the
                # Note OFF / UI un-light still happens.
                logger.debug(
                    "WM_INPUT break without make: vk=0x%02X scan=0x%02X name=%r",
                    vk, scan, name,
                )
            self._pad_down.discard(name)
        self._win_queue.put_nowait((name, is_down, vk, scan))
        return True

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

            if IS_MAC:
                self._mac_start()
                return

            kwargs: dict = {
                "on_press": self._generic_press,
                "on_release": self._generic_release,
            }
            self._listener = keyboard.Listener(**kwargs)
            self._listener.daemon = True
            self._listener.start()
        except Exception as exc:
            logger.error("Failed to start keyboard hook: %s", exc, exc_info=True)
            self._running = False

    def stop(self) -> None:
        if (
            not self._running
            and self._listener is None
            and self._mac_thread is None
            and self._mac_tap_thread is None
        ):
            with self._lock:
                self._cancel_timer()
                self._cancel_triple_timer()
                self._zbuf.clear()
                self._zlast_ts = 0.0
                self._zlast_vk = 0
                self._triple_ups_left = 0
                self._pad_down.clear()
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
            elif IS_MAC:
                self._mac_stop()
            else:
                listener, self._listener = self._listener, None
                if listener is not None:
                    listener.stop()
        except Exception as exc:
            logger.warning("Error stopping keyboard hook: %s", exc)
        with self._lock:
            self._cancel_timer()
            self._cancel_triple_timer()
            self._zbuf.clear()
            self._zlast_ts = 0.0
            self._zlast_vk = 0
            self._triple_ups_left = 0
            self._pad_down.clear()
            for name in list(self._down):
                self._emit(name, False)

    # --------------------------------------- Windows dedicated WH_KEYBOARD_LL + WM_INPUT
    def _handle_wm_input(self, lparam: int) -> None:  # pragma: no cover - real OS message
        if not lparam:
            return
        try:
            size = wintypes.UINT(0)
            hdr_size = ctypes.sizeof(RAWINPUTHEADER)
            _user32.GetRawInputData(
                wintypes.HANDLE(lparam), RID_INPUT, None, ctypes.byref(size), hdr_size
            )
            if size.value < hdr_size + ctypes.sizeof(RAWKEYBOARD):
                return
            buf = ctypes.create_string_buffer(size.value)
            res = _user32.GetRawInputData(
                wintypes.HANDLE(lparam), RID_INPUT, buf, ctypes.byref(size), hdr_size
            )
            if res == 0xFFFFFFFF:
                return
            hdr = RAWINPUTHEADER.from_buffer(buf)
            if hdr.dwType != RIM_TYPEKEYBOARD:
                return
            rk = RAWKEYBOARD.from_buffer(buf, hdr_size)
            self._process_raw_keyboard(
                int(hdr.hDevice or 0), int(rk.VKey), int(rk.MakeCode), int(rk.Flags)
            )
        except Exception as exc:
            logger.debug("WM_INPUT parse error: %s", exc)

    def _win_message_pump(self) -> None:  # pragma: no cover - real OS hook loop
        """Dedicated high-priority Win32 message loop owning WH_KEYBOARD_LL and WM_INPUT."""
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

            # Register hidden message-only window for Raw Input (WM_INPUT)
            try:
                self._win_raw_hwnd = _user32.CreateWindowExW(
                    0, "Static", "NumpadToMIDIRawInputSink", 0,
                    0, 0, 0, 0, _HWND_MESSAGE, None, None, None,
                )
                if self._win_raw_hwnd:
                    rid = RAWINPUTDEVICE(
                        HID_USAGE_PAGE_GENERIC,
                        HID_USAGE_GENERIC_KEYBOARD,
                        RIDEV_INPUTSINK,
                        self._win_raw_hwnd,
                    )
                    if _user32.RegisterRawInputDevices(
                        ctypes.byref(rid), 1, ctypes.sizeof(RAWINPUTDEVICE)
                    ):
                        logger.info(
                            "Registered Win32 Raw Input keyboard sink (hwnd=%s)",
                            self._win_raw_hwnd,
                        )
            except Exception as exc:
                logger.warning("Could not register Raw Input sink: %s", exc)

            def _ll_proc(nCode: int, wParam: int, lParam: int) -> int:
                try:
                    if nCode == _HC_ACTION and lParam:
                        if wParam in (_WM_KEYDOWN, _WM_SYSKEYDOWN, _WM_KEYUP, _WM_SYSKEYUP):
                            kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                            extended = bool(kb.flags & _LLKHF_EXTENDED)
                            vk_code = int(kb.vkCode)
                            scan_code = int(kb.scanCode)
                            now = time.perf_counter()
                            # Correlate this event with Raw Input (WM_INPUT) device
                            # tracking, then apply the device-aware decision.
                            origin = self._ll_origin_device(vk_code, extended, now)
                            origin_matched = self._origin_matches(origin) if origin else False
                            name = self._ll_resolve_name(
                                vk_code, scan_code, extended, origin, origin_matched
                            )
                            if name is not None:
                                is_down = wParam in (_WM_KEYDOWN, _WM_SYSKEYDOWN)
                                # Device-aware swallow decision. WM_INPUT owns
                                # the MIDI generation for trackable keys (exact
                                # hDevice match); the LL channel only swallows.
                                if self._ll_handle_event(
                                    name, vk_code, scan_code, extended, now, is_down
                                ):
                                    return 1
                                # Not swallowed: fall through so the OS receives the key.
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
                if msg.message == _WM_INPUT:
                    self._handle_wm_input(int(msg.lParam))
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
            if self._win_raw_hwnd:
                try:
                    _user32.DestroyWindow(self._win_raw_hwnd)
                except Exception:
                    pass
                self._win_raw_hwnd = None
            self._win_thread_id = 0
            self._win_proc_ref = None

    def _win_dispatch_loop(self) -> None:  # pragma: no cover - background consumer
        while True:
            item = self._win_queue.get()
            if item is None:
                break
            name, is_down = item[0], item[1]
            vk = item[2] if len(item) > 2 else 0
            scan = item[3] if len(item) > 3 else 0
            try:
                self._dispatch(name, is_down, vk, scan)
            except Exception:
                pass

    # ----------------------------------------------------- macOS multi-interface HID & IOHIDManager
    def _find_mac_device_interfaces(self) -> tuple[list[bytes], list[bytes]]:
        """Return (keyboard_paths, consumer_paths) for the target device or all keypads."""
        kb_paths: list[bytes] = []
        cons_paths: list[bytes] = []
        if not HAS_HID:
            return kb_paths, cons_paths

        try:
            raw_list = hid.enumerate()
        except Exception:
            return kb_paths, cons_paths

        target = self._target_device.upper()
        for d in raw_list:
            vid = d.get("vendor_id", 0)
            pid = d.get("product_id", 0)
            if vid == 0x05AC and not target:  # skip Apple internal keyboard in all-devices mode
                continue

            hw_id = f"HID\\VID_{vid:04X}&PID_{pid:04X}"
            if target and (target != hw_id.upper() and target not in hw_id.upper()):
                continue

            raw_path = d.get("path")
            if not raw_path:
                continue
            p_bytes = raw_path if isinstance(raw_path, bytes) else str(raw_path).encode("utf-8")

            usage_page = d.get("usage_page", 0)
            usage = d.get("usage", 0)
            if (usage_page == 1 and usage in (6, 7)) or d.get("interface_number") == 0:
                if p_bytes not in kb_paths:
                    kb_paths.append(p_bytes)
            elif usage_page == 0x0C or d.get("interface_number") == 1:
                if p_bytes not in cons_paths:
                    cons_paths.append(p_bytes)
            else:
                if p_bytes not in kb_paths:
                    kb_paths.append(p_bytes)

        return kb_paths, cons_paths

    def _mac_start(self) -> None:
        """Start BOTH macOS native listeners:
        the IOHIDManager input pipeline (both composite HID interfaces) and the
        CGEventTap that swallows keypad codes when Exclusive Mode is enabled
        (the tap runs passive pass-through while suppression is off, so
        toggling Exclusive Mode takes effect without a restart)."""
        self._mac_thread = threading.Thread(
            target=self._mac_iohid_loop, name="NumpadMacIOHIDWorker", daemon=True
        )
        self._mac_thread.start()
        self._mac_tap_thread = threading.Thread(
            target=self._mac_tap_loop, name="NumpadMacTapWorker", daemon=True
        )
        self._mac_tap_thread.start()

    def _mac_stop(self) -> None:
        """Stop the macOS IOHIDManager run loop and the CGEventTap."""
        for thread in (self._mac_thread, self._mac_tap_thread):
            if thread is not None:
                thread.join(timeout=1.0)
        self._mac_thread = None
        self._mac_tap_thread = None
        self._mac_tap_ref, self._mac_tap_cb_ref, self._mac_tap_src_ref = None, None, None

    def _mac_iohid_loop(self) -> None:
        """Dedicated daemon thread running the native IOHIDManager input pipeline.

        The composite USB keypad exposes two HID interfaces: the numeric keys on
        the GenericDesktop Keyboard/Keypad interface (usage page 0x01, usages
        6/7) and Home/Email/Calc on the Consumer Control interface (page 0x0C).
        ``IOHIDManagerSetDeviceMatchingMultiple`` binds to BOTH interfaces of
        the composite device simultaneously, which fixes numeric keys not being
        seen when only the Consumer interface was previously bound.
        """
        if not IS_MAC:
            return
        manager = None
        current_rl = None
        k_cf_default_mode = None
        k_vid = None
        k_pid = None
        try:
            from ctypes import (
                CFUNCTYPE, POINTER, byref, c_bool, c_char_p, c_double, c_int32, c_long,
                c_uint8, c_uint32, c_void_p, cdll, util,
            )

            iokit_path = (
                util.find_library("IOKit")
                or "/System/Library/Frameworks/IOKit.framework/IOKit"
            )
            cf_path = (
                util.find_library("CoreFoundation")
                or "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
            )

            iokit = cdll.LoadLibrary(iokit_path)
            cf = cdll.LoadLibrary(cf_path)

            cf.CFRelease.argtypes = [c_void_p]
            cf.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, c_uint32]
            cf.CFStringCreateWithCString.restype = c_void_p
            cf.CFNumberGetValue.argtypes = [c_void_p, c_int32, c_void_p]
            cf.CFNumberGetValue.restype = c_bool
            cf.CFNumberCreate.argtypes = [c_void_p, c_int32, POINTER(c_int32)]
            cf.CFNumberCreate.restype = c_void_p
            cf.CFArrayCreateMutable.argtypes = [c_void_p, c_long, c_void_p]
            cf.CFArrayCreateMutable.restype = c_void_p
            cf.CFArrayAppendValue.argtypes = [c_void_p, c_void_p]
            cf.CFArrayAppendValue.restype = None
            cf.CFDictionaryCreateMutable.argtypes = [c_void_p, c_long, c_void_p, c_void_p]
            cf.CFDictionaryCreateMutable.restype = c_void_p
            cf.CFDictionarySetValue.argtypes = [c_void_p, c_void_p, c_void_p]
            cf.CFDictionarySetValue.restype = None
            cf.CFRunLoopGetCurrent.restype = c_void_p
            cf.CFRunLoopRunInMode.argtypes = [c_void_p, c_double, c_bool]
            cf.CFRunLoopRunInMode.restype = c_int32

            iokit.IOHIDManagerCreate.argtypes = [c_void_p, c_uint32]
            iokit.IOHIDManagerCreate.restype = c_void_p
            iokit.IOHIDManagerSetDeviceMatchingMultiple.argtypes = [c_void_p, c_void_p]
            iokit.IOHIDManagerSetDeviceMatchingMultiple.restype = None
            iokit.IOHIDManagerSetDeviceMatching.argtypes = [c_void_p, c_void_p]
            iokit.IOHIDManagerSetDeviceMatching.restype = None

            IOHIDValueCallback = CFUNCTYPE(
                None, c_void_p, c_int32, c_void_p, c_void_p
            )

            iokit.IOHIDManagerRegisterInputValueCallback.argtypes = [
                c_void_p, IOHIDValueCallback, c_void_p
            ]
            iokit.IOHIDManagerRegisterInputValueCallback.restype = None

            iokit.IOHIDValueGetElement.argtypes = [c_void_p]
            iokit.IOHIDValueGetElement.restype = c_void_p
            iokit.IOHIDElementGetUsagePage.argtypes = [c_void_p]
            iokit.IOHIDElementGetUsagePage.restype = c_uint32
            iokit.IOHIDElementGetUsage.argtypes = [c_void_p]
            iokit.IOHIDElementGetUsage.restype = c_uint32
            iokit.IOHIDValueGetIntegerValue.argtypes = [c_void_p]
            iokit.IOHIDValueGetIntegerValue.restype = c_long

            iokit.IOHIDManagerScheduleWithRunLoop.argtypes = [c_void_p, c_void_p, c_void_p]
            iokit.IOHIDManagerScheduleWithRunLoop.restype = None
            iokit.IOHIDManagerUnscheduleFromRunLoop.argtypes = [c_void_p, c_void_p, c_void_p]
            iokit.IOHIDManagerUnscheduleFromRunLoop.restype = None

            iokit.IOHIDManagerOpen.argtypes = [c_void_p, c_uint32]
            iokit.IOHIDManagerOpen.restype = c_int32
            iokit.IOHIDManagerClose.argtypes = [c_void_p, c_uint32]
            iokit.IOHIDManagerClose.restype = c_int32

            iokit.IOHIDDeviceGetProperty.argtypes = [c_void_p, c_void_p]
            iokit.IOHIDDeviceGetProperty.restype = c_void_p

            manager = iokit.IOHIDManagerCreate(None, 0)
            if not manager:
                logger.error("IOHIDManagerCreate returned NULL")
                return

            self._mac_iohid_manager = manager

            # Build an array of matching dictionaries so BOTH composite
            # interfaces bind: GenericDesktop Keyboard (0x01/6), GenericDesktop
            # Keypad (0x01/7) and Consumer Control (0x0C/1).
            matching_array = cf.CFArrayCreateMutable(None, c_long(0), None)
            dict_refs: list = []
            if matching_array:
                page_key = cf.CFStringCreateWithCString(
                    None, b"DeviceUsagePage", 0x08000100
                )
                usage_key = cf.CFStringCreateWithCString(
                    None, b"DeviceUsage", 0x08000100
                )
                for page_value, usage_value in _MAC_MATCHING_DICTS:
                    try:
                        page_ref = c_int32(page_value)
                        usage_ref = c_int32(usage_value)
                        page_num = cf.CFNumberCreate(None, 3, byref(page_ref))
                        usage_num = cf.CFNumberCreate(None, 3, byref(usage_ref))
                        device_dict = cf.CFDictionaryCreateMutable(None, c_long(0), None, None)
                        cf.CFDictionarySetValue(device_dict, page_key, page_num)
                        cf.CFDictionarySetValue(device_dict, usage_key, usage_num)
                        cf.CFArrayAppendValue(matching_array, device_dict)
                        # The dicts use NULL callbacks (no inherent retention), so all
                        # refs must outlive the SetDeviceMatchingMultiple call below.
                        dict_refs.append((page_num, usage_num, device_dict))
                    except Exception as exc:
                        logger.warning(
                            "Could not build IOHID matching dict %s: %s",
                            (page_value, usage_value), exc,
                        )

            if matching_array:
                iokit.IOHIDManagerSetDeviceMatchingMultiple(manager, matching_array)
                logger.info(
                    "IOHIDManager bound to BOTH composite interfaces via "
                    "SetDeviceMatchingMultiple (matching dicts: %d)",
                    len(dict_refs),
                )
                for ref_tuple in dict_refs:
                    for ref in ref_tuple:  # release after the manager copies what it needs
                        try:
                            cf.CFRelease(ref)
                        except Exception:
                            pass
                cf.CFRelease(matching_array)
                if page_key:
                    cf.CFRelease(page_key)
                if usage_key:
                    cf.CFRelease(usage_key)
            else:
                # Fallback: unconstrained matching (superset of all dicts).
                logger.warning(
                    "IOHIDManagerSetDeviceMatchingMultiple dictionaries unavailable; "
                    "falling back to unconstrained matching"
                )
                iokit.IOHIDManagerSetDeviceMatching(manager, None)

            k_vid = cf.CFStringCreateWithCString(None, b"VendorID", 0x08000100)
            k_pid = cf.CFStringCreateWithCString(None, b"ProductID", 0x08000100)
            k_cf_default_mode = cf.CFStringCreateWithCString(None, b"kCFRunLoopDefaultMode", 0x08000100)

            def _is_target_device(sender) -> bool:
                if not sender:
                    return False
                vid_val = c_int32(0)
                pid_val = c_int32(0)
                if k_vid:
                    v_prop = iokit.IOHIDDeviceGetProperty(sender, k_vid)
                    if v_prop:
                        cf.CFNumberGetValue(v_prop, 3, byref(vid_val))
                if k_pid:
                    p_prop = iokit.IOHIDDeviceGetProperty(sender, k_pid)
                    if p_prop:
                        cf.CFNumberGetValue(p_prop, 3, byref(pid_val))

                vid = vid_val.value
                pid = pid_val.value
                if vid == 0x05AC and not self._target_device:
                    return False

                target = self._target_device.upper().replace("/", "\\")
                if not target:
                    return True
                hw_id = f"HID\\VID_{vid:04X}&PID_{pid:04X}"
                if target == hw_id or target in hw_id or hw_id in target:
                    return True
                if f"VID_{vid:04X}" in target and f"PID_{pid:04X}" in target:
                    return True
                return False

            def _value_callback(context, result, sender, value_ref):
                try:
                    if not value_ref or not _is_target_device(sender):
                        return

                    elem = iokit.IOHIDValueGetElement(value_ref)
                    if not elem:
                        return

                    page = int(iokit.IOHIDElementGetUsagePage(elem))
                    usage = int(iokit.IOHIDElementGetUsage(elem))
                    int_val = int(iokit.IOHIDValueGetIntegerValue(value_ref))
                    is_down = bool(int_val != 0)

                    name = mac_usage_name(int(page), int(usage))

                    if name:
                        self._dispatch(name, is_down, vk=usage)
                except Exception as exc:
                    logger.debug("Exception in IOHIDManager value callback: %s", exc)

            self._mac_value_cb_ref = IOHIDValueCallback(_value_callback)
            iokit.IOHIDManagerRegisterInputValueCallback(
                manager, self._mac_value_cb_ref, None
            )

            current_rl = cf.CFRunLoopGetCurrent()
            iokit.IOHIDManagerScheduleWithRunLoop(
                manager, current_rl, k_cf_default_mode
            )

            ret = iokit.IOHIDManagerOpen(manager, 0)
            logger.info("IOHIDManagerOpen returned %d; starting macOS HID run loop", ret)

            while self._running:
                cf.CFRunLoopRunInMode(k_cf_default_mode, c_double(0.1), False)

        except Exception as exc:
            logger.error("macOS IOHIDManager error: %s", exc, exc_info=True)
        finally:
            try:
                if manager and current_rl and k_cf_default_mode:
                    iokit.IOHIDManagerUnscheduleFromRunLoop(
                        manager, current_rl, k_cf_default_mode
                    )
            except Exception:
                pass
            try:
                if manager:
                    iokit.IOHIDManagerClose(manager, 0)
                    cf.CFRelease(manager)
            except Exception:
                pass
            for ref in (k_vid, k_pid, k_cf_default_mode):
                if ref:
                    try:
                        cf.CFRelease(ref)
                    except Exception:
                        pass
            self._mac_iohid_manager = None
            self._mac_value_cb_ref = None

    # ---------------------------------------------------------------- macOS CGEventTap swallow
    def _mac_tap_loop(self) -> None:
        """Dedicated daemon thread hosting a CGEventTap that swallows keypad codes.

        Raw IOHID capture cannot withhold events the macOS window system has
        already delivered to the focused application; a *modifying* CGEventTap
        (``kCGEventTapOptionDefault``) is the userland way to swallow the
        external keypad's codes so they do not type numbers into the macOS UI
        (requires the Accessibility permission). Codes are the kVK_ANSI keypad
        cluster (always safe: internal MacBook keyboards never emit them) plus
        the PC-navigation cluster only when a specific external keypad is
        targeted, because those codes are shared with the internal keyboard.
        """
        if not IS_MAC:
            return
        cg = None
        cf = None
        tap = None
        source = None
        mode_ref = None
        try:
            from ctypes import (
                CFUNCTYPE, byref, c_bool, c_char_p, c_double, c_int32, c_longlong,
                c_uint32, c_uint64, c_void_p, cdll, util,
            )

            cg_path = (
                util.find_library("CoreGraphics")
                or "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
            )
            cf_path = (
                util.find_library("CoreFoundation")
                or "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
            )
            cg = cdll.LoadLibrary(cg_path)
            cf = cdll.LoadLibrary(cf_path)

            cf.CFRelease.argtypes = [c_void_p]
            cf.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, c_uint32]
            cf.CFStringCreateWithCString.restype = c_void_p
            cf.CFRunLoopGetCurrent.restype = c_void_p
            cf.CFRunLoopRunInMode.argtypes = [c_void_p, c_double, c_bool]
            cf.CFRunLoopRunInMode.restype = c_int32
            cf.CFRunLoopAddSource.argtypes = [c_void_p, c_void_p, c_void_p]
            cf.CFRunLoopAddSource.restype = None

            CGEventTapCallback = CFUNCTYPE(c_void_p, c_void_p, c_uint32, c_void_p, c_void_p)

            cg.CGEventTapCreate.argtypes = [
                c_uint32, c_uint32, c_uint32, c_uint64, CGEventTapCallback, c_void_p
            ]
            cg.CGEventTapCreate.restype = c_void_p
            cg.CGEventTapGetRunLoopSource.argtypes = [c_void_p]
            cg.CGEventTapGetRunLoopSource.restype = c_void_p
            cg.CGEventTapEnable.argtypes = [c_void_p, c_bool]
            cg.CGEventTapEnable.restype = None
            cg.CGEventTapIsEnabled.argtypes = [c_void_p]
            cg.CGEventTapIsEnabled.restype = c_bool
            cg.CGEventGetIntegerValueField.argtypes = [c_void_p, c_uint32]
            cg.CGEventGetIntegerValueField.restype = c_longlong

            tap_codes = set(_MAC_TAP_NUMPAD_CODES)
            with self._lock:
                targeted = bool(self._target_device)
            if targeted:
                tap_codes |= _MAC_TAP_NAV_CODES
            tap_codes = frozenset(tap_codes)

            def _tap_callback(proxy, ev_type, event_ref, refcon) -> int:
                try:
                    if event_ref:
                        # macOS disables taps that stall; re-enable and pass through.
                        if ev_type in (254, 255):  # disabled by user input / timeout
                            cg.CGEventTapEnable(tap, True)
                            return event_ref
                        keycode = int(
                            cg.CGEventGetIntegerValueField(event_ref, _MAC_FIELD_KEYCODE)
                        )
                        if keycode in tap_codes and self._suppress:
                            return 0  # NULL CGEventRef -> the OS event is swallowed
                    return event_ref
                except Exception as exc:
                    logger.debug("CGEventTap callback error: %s", exc)
                    return event_ref or 0

            self._mac_tap_cb_ref = CGEventTapCallback(_tap_callback)
            # kCGSessionEventTap (1), kCGHeadInsertEventTap (0), kCGEventTapOptionDefault (0)
            tap = cg.CGEventTapCreate(
                1, 0, 0, c_uint64(_MAC_EVENT_MASK), self._mac_tap_cb_ref, None
            )
            if not tap:
                logger.warning(
                    "CGEventTapCreate failed - keypad keys will not be blocked from the "
                    "macOS UI. Grant NumpadToMIDI Accessibility in System Settings > "
                    "Privacy & Security (raw HID capture keeps working without it)."
                )
                return
            self._mac_tap_ref = tap
            self._mac_tap_granted = True
            source = cg.CGEventTapGetRunLoopSource(tap)
            self._mac_tap_src_ref = source

            current_rl = cf.CFRunLoopGetCurrent()
            mode_ref = cf.CFStringCreateWithCString(
                None, b"kCFRunLoopDefaultMode", 0x08000100
            )
            cf.CFRunLoopAddSource(current_rl, source, mode_ref)
            cg.CGEventTapEnable(tap, True)
            logger.info(
                "CGEventTap installed - swallowing %d keypad keycodes (Exclusive Mode %s)",
                len(tap_codes),
                "on" if self._suppress else "off (tap is passive)",
            )

            while self._running:
                cf.CFRunLoopRunInMode(mode_ref, c_double(0.1), False)
        except Exception as exc:
            logger.warning("macOS CGEventTap error: %s", exc)
        finally:
            try:
                if cg is not None and tap:
                    cg.CGEventTapEnable(
                        self._mac_tap_ref if self._mac_tap_ref == tap else tap, False
                    )
            except Exception:
                pass
            for ref in (source, tap, mode_ref):
                try:
                    if ref:
                        cf.CFRelease(ref)
                except Exception:
                    pass
            if tap:
                self._mac_tap_ref = None
            self._mac_tap_src_ref = None
            self._mac_tap_granted = False

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
    def _dispatch(self, name: str, down: bool, vk: int = 0, scan: int = 0) -> None:
        with self._lock:
            if name == "0" and self.detect_triple_zero:
                # Both VK_NUMPAD0 (NumLock ON) and keypad VK_INSERT (NumLock OFF) arrive
                # here as "0", so mixed or NumLock-OFF bursts share one triplet detector.
                # ``scan`` is the hardware OEM scancode (0x52 = physical keypad 0), used
                # for scancode-first burst tracking and diagnostics.
                self._zero_event(down, vk, scan)
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
                # v1.9.5: a stray release (its make never reached us) is no
                # longer silently dropped - the OFF is still dispatched so
                # the UI pad un-lights and no Note ON can hang. The
                # Controller ignores an OFF for a key it never lit.
                logger.debug("Stray release for %r; forcing Note OFF dispatch", name)
            else:
                self._down.discard(name)
        try:
            self._on_key(name, down)
        except Exception:  # a UI/MIDI error must not kill the hook thread
            pass

    def _zero_event(self, down: bool, vk: int = 0, scan: int = 0) -> None:
        now = time.perf_counter()
        delta_ms = (now - self._last_zero_ts) * 1000.0 if self._last_zero_ts > 0.0 else 0.0
        self._last_zero_ts = now
        event_type = "KEYDOWN" if down else "KEYUP"
        source = zero_vk_label(vk, scan)
        logger.info(
            "Keypad 0 [%s] (%s, timestamp=%.6f, delta_ms=%.2f)",
            source,
            event_type,
            now,
            delta_ms,
        )

        eff_window = max(self.burst_window, _BURST_WINDOW_FLOOR)
        # Slower firmware debounce/scan cadence observed on NumLock-OFF VK_INSERT bursts
        if vk in (VK_INSERT, 114) or self._zlast_vk in (VK_INSERT, 114):
            eff_window = max(eff_window, 0.120)
        if vk:
            self._zlast_vk = vk

        # --- 1. Finishing a recognised '000' press (waiting for its releases)
        if self._triple_ups_left > 0:
            if down:
                if (now - self._triple_started) > eff_window:
                    # Previous '000' burst has finished; emit Note Off and start fresh
                    self._cancel_triple_timer()
                    self._triple_ups_left = 0
                    self._emit("000", False)
                else:
                    return
            else:
                self._triple_ups_left = max(0, self._triple_ups_left - 1)
                if self._triple_ups_left == 0:
                    self._cancel_triple_timer()
                    self._emit("000", False)
                return

        # --- 2. Sliding window timeout check:
        # If buffered events have expired without completing a triplet, flush them now
        if self._zbuf and self._zlast_ts > 0.0 and (now - self._zlast_ts) > eff_window:
            self._cancel_timer()
            pending, self._zbuf = self._zbuf[:], []
            self._zlast_ts = 0.0
            for pending_down in pending:
                self._emit("0", pending_down)

        # --- 3. No burst in progress
        if not self._zbuf:
            if "0" in self._down:
                self._emit("0", down)  # auto-repeat is ignored, release passes
                return
            if not down:
                return  # stray release

        # --- 4. Buffer the current event and evaluate triplet burst
        self._zbuf.append(down)
        self._zlast_ts = now

        downs = sum(1 for x in self._zbuf if x)
        if downs >= 3:
            self._cancel_timer()
            ups = len(self._zbuf) - downs
            self._zbuf.clear()
            self._zlast_ts = 0.0
            logger.info(
                "Keypad 0 triplet detected [%s] -> firing '000' Note On (MIDI Note 37)",
                source,
            )
            self._emit("000", True)
            self._triple_started = now
            self._triple_ups_left = max(0, 3 - ups)
            if self._triple_ups_left == 0:
                self._emit("000", False)
            else:
                self._start_triple_timer()
            return

        # Less than 3 downs so far: (re)schedule the flush timer
        self._cancel_timer()
        self._timer = threading.Timer(eff_window, self._flush_zero)
        self._timer.daemon = True
        self._timer.start()

    def _flush_zero(self) -> None:
        with self._lock:
            if self._timer is not threading.current_thread():
                return  # cancelled while we were waiting for the lock
            self._timer = None
            pending, self._zbuf = self._zbuf[:], []
            self._zlast_ts = 0.0
            for down in pending:
                self._emit("0", down)

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _start_triple_timer(self) -> None:
        self._cancel_triple_timer()
        delay = max(0.25, self.burst_window * 4.0)
        self._triple_timer = threading.Timer(delay, self._flush_triple)
        self._triple_timer.daemon = True
        self._triple_timer.start()

    def _flush_triple(self) -> None:
        with self._lock:
            if self._triple_timer is not threading.current_thread():
                return
            self._triple_timer = None
            if self._triple_ups_left > 0:
                self._triple_ups_left = 0
                self._emit("000", False)

    def _cancel_triple_timer(self) -> None:
        if self._triple_timer is not None:
            self._triple_timer.cancel()
            self._triple_timer = None
