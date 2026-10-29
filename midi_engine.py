"""MIDI output engine for NumpadToMIDI (python-rtmidi).

* macOS:   creates a native *virtual MIDI source* (``open_virtual_port``) that
           shows up as an input in any DAW (Ableton, Logic, ...).
* Windows: WinMM cannot create virtual ports, so we connect to an existing
           loopback port (loopMIDI, etc.). If none exists we fall back to the
           first available output (or run silently with ``mode == "none"`` so the
           UI keeps working).
"""
from __future__ import annotations

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


class MidiEngine:
    CLIENT_NAME = "NumpadToMIDI"
    LOOPBACK_HINTS = ("loopmidi", "numpadtomidi", "loopback", "virtual", "iac driver", "iac bus")
    LAST_RESORT_HINTS = ("microsoft gs wavetable",)

    def __init__(self) -> None:
        self._out = None
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
        return self._out is not None and self.mode != "none"

    @property
    def supports_virtual(self) -> bool:
        return IS_MAC

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
        return "No MIDI output - install loopMIDI and press Refresh"

    # ------------------------------------------------------------------- opening
    def open_auto(self, preferred: str = "") -> str:
        """Open the best available output. Returns the resulting mode."""
        if self._out is None:
            return self.mode
        self._close_port()
        if not IS_WIN and self._try_virtual():  # macOS / Linux: native virtual source
            return self.mode
        ports = self.list_ports()
        if preferred and preferred in ports:
            return self.open_port_by_name(preferred)
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
            if self._out is not None:
                try:
                    self._out.close_port()
                except Exception:  # pragma: no cover
                    pass
            self.mode, self.port_name = "none", ""

    # ------------------------------------------------------------------ sending
    def _send(self, message: list[int]) -> None:
        with self._lock:
            if self._out is None or self.mode == "none":
                return
            try:
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
        with self._lock:
            if self._out is not None:
                try:
                    self._out.close_port()
                except Exception:  # pragma: no cover
                    pass
                try:
                    self._out.delete()
                except Exception:  # pragma: no cover
                    pass
                self._out = None
            self.mode, self.port_name = "none", ""
