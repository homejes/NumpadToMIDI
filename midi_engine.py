"""MIDI output engine for NumpadToMIDI.

* macOS:   creates a native *virtual MIDI source* (python-rtmidi
           ``open_virtual_port``) that shows up as an input in any DAW.
* Windows: WinMM cannot create virtual ports. If the teVirtualMIDI driver is
           installed (the Windows installer sets it up for you) the app creates
           its own port called "NumpadToMIDI" through ``teVirtualMIDI64.dll``,
           which is loaded dynamically from the system - nothing of it is
           bundled with the app. Without the driver we fall back to an existing
           loopback port (loopMIDI, ...), then to the first available output,
           and finally to a silent ``mode == "none"`` so the UI keeps working.
"""
from __future__ import annotations

import ctypes
import sys
import threading
from typing import Optional

try:
    import rtmidi

    _RTMIDI_ERROR: Optional[str] = None
except Exception as exc:  # pragma: no cover - missing/broken native module
    rtmidi = None  # type: ignore[assignment]
    _RTMIDI_ERROR = str(exc)

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def note_name(note: int) -> str:
    """MIDI note -> name using the Ableton convention (60 == C3, 36 == C1)."""
    return f"{NOTE_NAMES[note % 12]}{note // 12 - 2}"


class TeVirtualMidiPort:
    """Minimal ctypes binding to the teVirtualMIDI driver library (Windows only).

    The DLL is installed system-wide by the driver (it ships with loopMIDI /
    rtpMIDI) and is only ever loaded from the user's system.
    """

    # The application only *sends* data into the port, so only the TX side is
    # instantiated (the port appears as a MIDI *input* to other applications).
    FLAGS_INSTANTIATE_TX_ONLY = 8
    ERROR_ALREADY_EXISTS = 183
    _dll = None
    _dll_checked = False

    @classmethod
    def _load(cls):
        if not cls._dll_checked:
            cls._dll_checked = True
            if IS_WIN:
                name = "teVirtualMIDI64.dll" if sys.maxsize > 2**32 else "teVirtualMIDI.dll"
                try:
                    dll = ctypes.WinDLL(name)
                    dll.virtualMIDICreatePortEx2.restype = ctypes.c_void_p
                    dll.virtualMIDICreatePortEx2.argtypes = [
                        ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_size_t,
                        ctypes.c_uint32, ctypes.c_uint32,
                    ]
                    dll.virtualMIDISendData.restype = ctypes.c_int
                    dll.virtualMIDISendData.argtypes = [
                        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                    ]
                    dll.virtualMIDIClosePort.restype = None
                    dll.virtualMIDIClosePort.argtypes = [ctypes.c_void_p]
                    cls._dll = dll
                except (OSError, AttributeError):
                    cls._dll = None
        return cls._dll

    @classmethod
    def available(cls) -> bool:
        return cls._load() is not None

    def __init__(self) -> None:
        self._handle: Optional[int] = None
        self.name = ""

    def open(self, name: str) -> bool:
        dll = self._load()
        if dll is None:
            return False
        handle = dll.virtualMIDICreatePortEx2(name, None, 0, 65535, self.FLAGS_INSTANTIATE_TX_ONLY)
        if not handle:
            return False
        self._handle, self.name = handle, name
        return True

    def send(self, message: list[int]) -> bool:
        if not self._handle:
            return False
        buf = (ctypes.c_ubyte * len(message))(*message)
        return bool(self._load().virtualMIDISendData(self._handle, buf, len(message)))

    def close(self) -> None:
        if self._handle:
            try:
                self._load().virtualMIDIClosePort(self._handle)
            finally:
                self._handle, self.name = None, ""


class MidiEngine:
    CLIENT_NAME = "NumpadToMIDI"
    LOOPBACK_HINTS = ("loopmidi", "numpadtomidi", "loopback", "virtual", "iac driver", "iac bus")
    LAST_RESORT_HINTS = ("microsoft gs wavetable",)

    def __init__(self) -> None:
        self._out = None
        self._te: Optional[TeVirtualMidiPort] = None
        self._lock = threading.Lock()
        self.mode = "none"  # "virtual" | "loopback" | "fallback" | "none"
        self.port_name = ""
        self.error = _RTMIDI_ERROR or ""
        if rtmidi is not None:
            try:
                self._out = rtmidi.MidiOut(name=self.CLIENT_NAME)
            except Exception as exc:  # pragma: no cover
                self.error = str(exc)

    # ------------------------------------------------------------------ queries
    @property
    def is_open(self) -> bool:
        return self.mode != "none"

    @property
    def supports_virtual(self) -> bool:
        """True when this machine can host its own virtual MIDI source."""
        return IS_MAC or (IS_WIN and TeVirtualMidiPort.available())

    def list_ports(self) -> list[str]:
        if self._out is None:
            return []
        try:
            return list(self._out.get_ports())
        except Exception:  # pragma: no cover
            return []

    @property
    def description(self) -> str:
        if self.mode == "virtual":
            return f"Virtual MIDI source: {self.port_name}"
        if self.mode == "loopback":
            return f"Loopback port: {self.port_name}"
        if self.mode == "fallback":
            return f"Fallback port (no loopback found): {self.port_name}"
        if self.error:
            return f"No MIDI output ({self.error})"
        if IS_WIN:
            return "No virtual MIDI driver - reinstall NumpadToMIDI or install loopMIDI"
        return "No MIDI output"

    # ------------------------------------------------------------------- opening
    def open_auto(self, preferred: str = "") -> str:
        """Open the best available output. Returns the resulting mode.

        ``preferred`` is a hardware/loopback port name the user picked earlier;
        an empty string means "use the built-in virtual port if possible".
        """
        self._close_port()
        ports = self.list_ports()
        if preferred and preferred in ports:
            return self.open_port_by_name(preferred)
        if self._try_te_virtual():
            return self.mode
        if self._out is None:
            return self.mode
        if not IS_WIN and self._try_virtual():  # macOS / Linux: native virtual source
            return self.mode
        for idx, name in enumerate(ports):
            if any(h in name.lower() for h in self.LOOPBACK_HINTS):
                return self._open_index(idx, name, "loopback")
        for idx, name in enumerate(ports):
            if not any(h in name.lower() for h in self.LAST_RESORT_HINTS):
                return self._open_index(idx, name, "fallback")
        for idx, name in enumerate(ports):
            return self._open_index(idx, name, "fallback")
        return self.mode

    def open_port_by_name(self, name: str) -> str:
        if self._out is None:
            return self.mode
        ports = self.list_ports()
        if name not in ports:
            return self.mode
        self._close_port()
        mode = "loopback" if any(h in name.lower() for h in self.LOOPBACK_HINTS) else "fallback"
        return self._open_index(ports.index(name), name, mode)

    def _try_te_virtual(self) -> bool:
        if not (IS_WIN and TeVirtualMidiPort.available()):
            return False
        for name in (self.CLIENT_NAME, f"{self.CLIENT_NAME} 2", f"{self.CLIENT_NAME} 3"):
            port = TeVirtualMidiPort()
            if port.open(name):
                self._te = port
                self.mode, self.port_name, self.error = "virtual", name, ""
                return True
        self.error = "could not create virtual port"
        return False

    def _try_virtual(self) -> bool:
        try:
            self._out.open_virtual_port(self.CLIENT_NAME)
        except Exception:  # NotImplementedError on WinMM, or CoreMIDI failure
            return False
        self.mode, self.port_name, self.error = "virtual", self.CLIENT_NAME, ""
        return True

    def _open_index(self, index: int, name: str, mode: str) -> str:
        try:
            self._out.open_port(index, self.CLIENT_NAME)
        except Exception as exc:
            self.error = str(exc)
            self.mode, self.port_name = "none", ""
            return self.mode
        self.mode, self.port_name, self.error = mode, name, ""
        return self.mode

    def _close_port(self) -> None:
        with self._lock:
            if self._te is not None:
                self._te.close()
                self._te = None
            if self._out is not None:
                try:
                    self._out.close_port()
                except Exception:  # pragma: no cover
                    pass
            self.mode, self.port_name = "none", ""

    # ------------------------------------------------------------------ sending
    def _send(self, message: list[int]) -> None:
        with self._lock:
            if self.mode == "none":
                return
            try:
                if self._te is not None:
                    self._te.send(message)
                elif self._out is not None:
                    self._out.send_message(message)
            except Exception as exc:  # pragma: no cover
                self.error = str(exc)

    def note_on(self, channel: int, note: int, velocity: int = 100) -> None:
        """channel is 1-16."""
        self._send([0x90 | ((channel - 1) & 0x0F), note & 0x7F, max(1, min(127, velocity))])

    def note_off(self, channel: int, note: int) -> None:
        self._send([0x80 | ((channel - 1) & 0x0F), note & 0x7F, 0])

    def all_notes_off(self, channel: Optional[int] = None) -> None:
        channels = range(1, 17) if channel is None else [channel]
        for ch in channels:
            self._send([0xB0 | ((ch - 1) & 0x0F), 123, 0])  # CC123 All Notes Off

    def close(self) -> None:
        self.all_notes_off()
        self._close_port()
        with self._lock:
            if self._out is not None:
                try:
                    self._out.delete()
                except Exception:  # pragma: no cover
                    pass
                self._out = None
