"""End-to-end check of the virtual MIDI port (Windows, driver installed).

Creates the "NumpadToMIDI" virtual port through MidiEngine, opens it as a MIDI
*input* with python-rtmidi (like a DAW would) and checks that Note On / Off sent
by the engine arrive. Used by CI after the silent installer has put the driver
in place:   python tests/e2e_windows_driver.py
"""
import ctypes
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import rtmidi  # noqa: E402

from midi_engine import MidiEngine, TeVirtualMidiPort  # noqa: E402


def main() -> int:
    print("teVirtualMIDI DLL available:", TeVirtualMidiPort.available())
    if not TeVirtualMidiPort.available():
        print("FAIL: teVirtualMIDI64.dll not found")
        return 1

    # Diagnostics: which creation flags does this driver accept?
    dll = TeVirtualMidiPort._load()
    for flags in (8, 12, 0):
        ctypes.SetLastError(0)
        handle = dll.virtualMIDICreatePortEx2("NumpadToMIDI-probe", None, 0, 65535, flags)
        print(f"probe flags={flags}: handle={'ok' if handle else None} lasterr={ctypes.GetLastError()}")
        if handle:
            dll.virtualMIDIClosePort(handle)

    engine = MidiEngine()
    mode = engine.open_auto("")
    print("engine mode:", mode, "-", engine.description)
    if mode != "virtual":
        print("FAIL: engine did not create the virtual port")
        return 1

    midi_in = rtmidi.MidiIn()
    ports = midi_in.get_ports()
    print("MIDI inputs:", ports)
    index = next((i for i, p in enumerate(ports) if p.startswith("NumpadToMIDI")), None)
    if index is None:
        print("FAIL: virtual port is not visible as a MIDI input")
        return 1
    midi_in.open_port(index)
    time.sleep(0.3)

    engine.note_on(1, 36, 100)
    engine.note_off(1, 36)
    engine.note_on(16, 58, 64)
    time.sleep(0.5)

    received = []
    while True:
        message = midi_in.get_message()
        if message is None:
            break
        received.append(message[0])
    print("received:", received)
    midi_in.close_port()
    engine.close()

    expected = [[0x90, 36, 100], [0x80, 36, 0], [0x9F, 58, 64]]
    if received != expected:
        print("FAIL: unexpected MIDI data, expected", expected)
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
