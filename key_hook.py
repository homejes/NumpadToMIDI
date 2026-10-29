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
import re
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


def list_keyboard_devices() -> list[KeyboardDevice]:
    """Enumerate connected keyboard/keypad devices via Win32 Raw Input API."""
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
        burst_window_ms: float = 35.0,
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
        # 000-burst detection state
        self._zbuf: list[bool] = []
        self._zfirst = 0.0
        self._timer: Optional[threading.Timer] = None
        self._triple_ups_left = 0
        self._triple_started = 0.0

        # Raw Input device filtering state
        self._devices: list[KeyboardDevice] = []
        self._device_by_handle: dict[int, KeyboardDevice] = {}
        self._target_handles: set[int] = set()
        self._last_raw_hdevice: int = 0
        self._last_raw_matched: bool = not bool(self._target_device)

        # Non-Windows pynput listener
        self._listener = None
        # Windows native WH_KEYBOARD_LL + WM_INPUT state
        self._running = False
        self._win_thread: Optional[threading.Thread] = None
        self._win_worker: Optional[threading.Thread] = None
        self._win_thread_id: int = 0
        self._win_hook_handle = None
        self._win_raw_hwnd = None
        self._win_proc_ref = None  # prevent GC of ctypes callback
        self._win_queue: queue.SimpleQueue[Optional[tuple[str, bool]]] = queue.SimpleQueue()
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
            self._last_raw_matched = not bool(self._target_device)

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
        return list(devices)

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

    def _process_raw_keyboard(self, h_device: int, vk: int, scan: int, flags: int) -> bool:
        """Filter and dispatch a Win32 RAWKEYBOARD event by its ``hDevice``."""
        matched = self.matches_device(h_device)
        self._last_raw_hdevice = h_device
        self._last_raw_matched = matched
        if not matched:
            return False
        extended = bool(flags & RI_KEY_E0)
        is_down = not bool(flags & RI_KEY_BREAK)
        name = win_key_name(vk, scan, extended)
        if name is None:
            return False
        self._win_queue.put_nowait((name, is_down))
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
                            name = win_key_name(int(kb.vkCode), int(kb.scanCode), extended)
                            if name is not None:
                                is_down = wParam in (_WM_KEYDOWN, _WM_SYSKEYDOWN)
                                if not self._target_device:
                                    # "All Connected Keypads (Default)" mode:
                                    # Dispatch immediately and suppress if Exclusive Mode is on.
                                    self._win_queue.put_nowait((name, is_down))
                                    if self._suppress or name in _ALWAYS_SUPPRESS_KEYS:
                                        return 1
                                else:
                                    # Specific USB keypad selected:
                                    # Only suppress when the active Raw Input device matches
                                    # the selected USB keypad (or for dedicated launcher keys
                                    # when the last device matched), and let WM_INPUT verify
                                    # RAWINPUT.header.hDevice for key releases and non-matching
                                    # keyboards so primary keyboard typing is never blocked.
                                    if self._last_raw_matched and name not in ("Tab", "Backspace"):
                                        if is_down and (
                                            self._suppress or name in _ALWAYS_SUPPRESS_KEYS
                                        ):
                                            self._win_queue.put_nowait((name, True))
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
