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

import atexit
import ctypes
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger("NumpadToMIDI.midi")

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
    last_error_code: int = 0
    last_error_message: str = ""
    _active_ports: dict[str, "TeVirtualMidiPort"] = {}
    _instances: set["TeVirtualMidiPort"] = set()

    @classmethod
    def _candidate_dll_paths(cls) -> list[str]:
        candidates: list[str] = []
        is_64 = sys.maxsize > 2**32
        primary = "teVirtualMIDI64.dll" if is_64 else "teVirtualMIDI.dll"
        secondary = "teVirtualMIDI.dll" if is_64 else "teVirtualMIDI32.dll"

        sys_dir = os.environ.get("WINDIR", r"C:\Windows")
        sys32 = os.path.join(sys_dir, "System32")
        syswow = os.path.join(sys_dir, "SysWOW64")

        # System directory candidates
        if is_64:
            candidates.extend([
                os.path.join(sys32, primary),
                os.path.join(sys32, secondary),
                os.path.join(syswow, "teVirtualMIDI32.dll"),
            ])
        else:
            candidates.extend([
                os.path.join(sys32, primary),
                os.path.join(syswow, secondary),
                os.path.join(sys32, "teVirtualMIDI32.dll"),
            ])

        # Application / bundle directory candidates
        for base in (
            Path(sys.executable).resolve().parent,
            Path(__file__).resolve().parent,
            Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)),
        ):
            candidates.append(str(base / primary))
            candidates.append(str(base / secondary))

        # Bare names for standard Windows DLL search order
        candidates.extend([primary, secondary, "teVirtualMIDI.dll", "teVirtualMIDI64.dll", "teVirtualMIDI32.dll"])
        return [c for c in candidates if c]

    @classmethod
    def _load(cls):
        if not cls._dll_checked:
            cls._dll_checked = True
            if IS_WIN:
                candidates = cls._candidate_dll_paths()
                loaded_dll = None
                last_exc = None
                for path in candidates:
                    try:
                        if os.path.isabs(path) and not os.path.exists(path):
                            continue
                        try:
                            dll = ctypes.WinDLL(path, use_last_error=True)
                        except Exception:
                            dll = ctypes.windll.LoadLibrary(path)
                        if dll and hasattr(dll, "virtualMIDICreatePortEx2"):
                            loaded_dll = dll
                            logger.info("Loaded virtual MIDI driver library from: %s", path)
                            break
                    except Exception as exc:
                        last_exc = exc
                        continue

                if loaded_dll is not None:
                    try:
                        loaded_dll.virtualMIDICreatePortEx2.restype = ctypes.c_void_p
                        loaded_dll.virtualMIDICreatePortEx2.argtypes = [
                            ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_size_t,
                            ctypes.c_uint32, ctypes.c_uint32,
                        ]
                        if hasattr(loaded_dll, "virtualMIDICreatePortEx3"):
                            loaded_dll.virtualMIDICreatePortEx3.restype = ctypes.c_void_p
                            loaded_dll.virtualMIDICreatePortEx3.argtypes = [
                                ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_size_t,
                                ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p,
                            ]
                        loaded_dll.virtualMIDISendData.restype = ctypes.c_int
                        loaded_dll.virtualMIDISendData.argtypes = [
                            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                        ]
                        loaded_dll.virtualMIDIClosePort.restype = None
                        loaded_dll.virtualMIDIClosePort.argtypes = [ctypes.c_void_p]
                        if hasattr(loaded_dll, "virtualMIDIShutdown"):
                            loaded_dll.virtualMIDIShutdown.restype = None
                            loaded_dll.virtualMIDIShutdown.argtypes = [ctypes.c_void_p]
                        cls._dll = loaded_dll
                    except Exception as exc:
                        logger.warning("Error configuring teVirtualMIDI function signatures: %s", exc)
                        cls._dll = None
                else:
                    cls.last_error_message = f"Driver DLL not found ({last_exc})"
                    logger.warning(
                        "Could not load teVirtualMIDI DLL (falling back to standard MIDI ports): %s",
                        last_exc,
                    )
                    cls._dll = None
        return cls._dll

    @classmethod
    def available(cls) -> bool:
        return cls._load() is not None

    @classmethod
    def cleanup_orphaned(cls, name: Optional[str] = None) -> None:
        """Close any open in-process handles for the given port name (or all active handles)."""
        if name:
            port = cls._active_ports.pop(name, None)
            if port is not None:
                try:
                    port.close()
                except Exception:
                    pass
        else:
            ports = list(cls._active_ports.values())
            cls._active_ports.clear()
            for p in ports:
                try:
                    p.close()
                except Exception:
                    pass

    @classmethod
    def close_all(cls) -> None:
        """Process-exit cleanup registered with atexit."""
        cls.cleanup_orphaned()
        for p in list(cls._instances):
            try:
                p.close()
            except Exception:
                pass

    @classmethod
    def _try_recover_driver(cls) -> bool:
        """Attempt non-blocking restart of ROOT\\MEDIA\\0000 if kernel driver is locked."""
        if not IS_WIN:
            return False
        try:
            logger.info("Attempting driver recovery for ROOT\\MEDIA\\0000...")
            res = subprocess.run(
                ["pnputil.exe", "/restart-device", r"ROOT\MEDIA\0000"],
                capture_output=True,
                text=True,
                timeout=4,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            logger.info("pnputil restart-device returned %d: %s", res.returncode, res.stdout.strip())
            return res.returncode == 0
        except Exception as exc:
            logger.debug("Driver recovery attempt ignored: %s", exc)
            return False

    def __init__(self) -> None:
        self._handle: Optional[int] = None
        self.name = ""
        TeVirtualMidiPort._instances.add(self)

    def open(self, name: str) -> bool:
        dll = self._load()
        if dll is None:
            TeVirtualMidiPort.last_error_message = "teVirtualMIDI library is not loaded"
            return False

        # Proactively clean up any previous instance holding this port name
        TeVirtualMidiPort.cleanup_orphaned(name)
        if self._handle:
            self.close()

        handle = None
        if hasattr(ctypes, "set_last_error"):
            ctypes.set_last_error(0)
        try:
            handle = dll.virtualMIDICreatePortEx2(
                name, None, 0, 65535, self.FLAGS_INSTANTIATE_TX_ONLY
            )
        except Exception as exc:
            logger.warning("virtualMIDICreatePortEx2(%s) exception: %s", name, exc)

        # Fallback to virtualMIDICreatePortEx3 if available and Ex2 returned null
        if not handle and hasattr(dll, "virtualMIDICreatePortEx3"):
            try:
                handle = dll.virtualMIDICreatePortEx3(
                    name, None, 0, 65535, self.FLAGS_INSTANTIATE_TX_ONLY, None, None
                )
            except Exception as exc:
                logger.debug("virtualMIDICreatePortEx3(%s) exception: %s", name, exc)

        if not handle:
            err = ctypes.get_last_error() if hasattr(ctypes, "get_last_error") else 0
            if not err and hasattr(ctypes, "windll"):
                try:
                    err = int(ctypes.windll.kernel32.GetLastError())
                except Exception:
                    pass

            # Gracefully handle Win32 Error Code 5 (ERROR_ACCESS_DENIED):
            # Proactively close any existing port handle and retry once
            if err == 5:
                logger.warning(
                    "virtualMIDICreatePortEx2(%r) encountered Win32 Error Code 5 (Access Denied). "
                    "Closing existing port handles and retrying...",
                    name,
                )
                TeVirtualMidiPort.cleanup_orphaned(name)
                try:
                    handle = dll.virtualMIDICreatePortEx2(
                        name, None, 0, 65535, self.FLAGS_INSTANTIATE_TX_ONLY
                    )
                except Exception as exc:
                    logger.debug("Retry virtualMIDICreatePortEx2(%s) error: %s", name, exc)
                if not handle and hasattr(dll, "virtualMIDICreatePortEx3"):
                    try:
                        handle = dll.virtualMIDICreatePortEx3(
                            name, None, 0, 65535, self.FLAGS_INSTANTIATE_TX_ONLY, None, None
                        )
                    except Exception as exc:
                        logger.debug("Retry virtualMIDICreatePortEx3(%s) error: %s", name, exc)

        if not handle:
            err = ctypes.get_last_error() if hasattr(ctypes, "get_last_error") else 0
            if not err and hasattr(ctypes, "windll"):
                try:
                    err = int(ctypes.windll.kernel32.GetLastError())
                except Exception:
                    pass
            err_desc = ""
            if err:
                try:
                    err_desc = ctypes.FormatError(err).strip()
                except Exception:
                    err_desc = f"Win32 error code {err}"
            TeVirtualMidiPort.last_error_code = err
            TeVirtualMidiPort.last_error_message = (
                f"error {err} ({err_desc})" if err_desc else f"error {err}"
            )
            logger.error(
                "virtualMIDICreatePortEx2(%r) returned NULL (Win32 error %d: %s)",
                name,
                err,
                err_desc or "unknown",
            )
            return False

        self._handle, self.name = handle, name
        TeVirtualMidiPort._active_ports[name] = self
        TeVirtualMidiPort.last_error_code = 0
        TeVirtualMidiPort.last_error_message = ""
        logger.info("Successfully opened virtual MIDI port %r (handle=0x%X)", name, int(handle))
        return True

    def send(self, message: list[int]) -> bool:
        if not self._handle:
            return False
        dll = self._load()
        if dll is None:
            return False
        try:
            buf = (ctypes.c_ubyte * len(message))(*message)
            return bool(dll.virtualMIDISendData(self._handle, buf, len(message)))
        except Exception as exc:
            logger.warning("virtualMIDISendData failed: %s", exc)
            return False

    def close(self) -> None:
        if self._handle:
            h = self._handle
            self._handle = None
            if self.name and TeVirtualMidiPort._active_ports.get(self.name) is self:
                TeVirtualMidiPort._active_ports.pop(self.name, None)
            self.name = ""
            try:
                dll = self._load()
                if dll is not None:
                    if hasattr(dll, "virtualMIDIShutdown"):
                        try:
                            dll.virtualMIDIShutdown(h)
                        except Exception:
                            pass
                    dll.virtualMIDIClosePort(h)
            except Exception as exc:
                logger.warning("virtualMIDIClosePort failed: %s", exc)
        TeVirtualMidiPort._instances.discard(self)


atexit.register(TeVirtualMidiPort.close_all)


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
            if TeVirtualMidiPort.last_error_message:
                return f"Virtual MIDI driver error: {TeVirtualMidiPort.last_error_message}"
            return "No virtual MIDI driver - reinstall NumpadToMIDI or install loopMIDI"
        return "No MIDI output"

    # ------------------------------------------------------------------- opening
    def open_auto(self, preferred: str = "") -> str:
        """Open the best available output. Returns the resulting mode.

        ``preferred`` is a hardware/loopback port name the user picked earlier;
        an empty string (or virtual port request) means "use the built-in virtual port if possible".
        """
        self._close_port()
        ports = self.list_ports()

        # If preferred is empty or refers to the built-in virtual port, prioritize virtual port creation
        if not preferred or preferred in (self.CLIENT_NAME, f"Virtual port: {self.CLIENT_NAME}", "__virtual__"):
            if self._try_te_virtual():
                return self.mode
            if not IS_WIN and self._try_virtual():
                return self.mode
            preferred = ""

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

    def reinitialize_virtual_port(self) -> str:
        """Force a fresh cleanup and initialization of the built-in virtual port."""
        self._close_port()
        TeVirtualMidiPort.cleanup_orphaned()
        TeVirtualMidiPort._dll_checked = False
        TeVirtualMidiPort._dll = None
        return self.open_auto("")

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
        try:
            for attempt in (1, 2):
                for name in (self.CLIENT_NAME, f"{self.CLIENT_NAME} 2", f"{self.CLIENT_NAME} 3"):
                    port = TeVirtualMidiPort()
                    if port.open(name):
                        self._te = port
                        self.mode, self.port_name, self.error = "virtual", name, ""
                        return True
                # If all port creation attempts failed with locked driver errors, try recovery once
                if attempt == 1 and TeVirtualMidiPort.last_error_code in (5, 31, 2, 1167, 183):
                    TeVirtualMidiPort.cleanup_orphaned()
                    if TeVirtualMidiPort._try_recover_driver():
                        continue
                break
            err_detail = TeVirtualMidiPort.last_error_message or "could not create virtual port"
            self.error = f"virtual port creation failed ({err_detail})"
            logger.warning(self.error)
            return False
        except Exception as exc:
            self.error = f"virtual port creation exception: {exc}"
            logger.error("Exception in _try_te_virtual: %s", exc)
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
