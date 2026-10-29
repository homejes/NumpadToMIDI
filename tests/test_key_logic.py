import unittest

from key_hook import KEY_INDEX, KEY_LAYOUT, KEY_SPAN, KeyHook, win_key_name


class Recorder:
    def __init__(self):
        self.events = []

    def __call__(self, name, down):
        self.events.append((name, down))


def make(**kw):
    rec = Recorder()
    return KeyHook(rec, burst_window_ms=kw.pop("burst_window_ms", 20), **kw), rec


class LayoutTests(unittest.TestCase):
    def test_23_unique_keys_on_unique_cells(self):
        self.assertEqual(len(KEY_LAYOUT), 23)
        self.assertEqual(len({k[0] for k in KEY_LAYOUT}), 23)
        self.assertEqual(len({(k[1], k[2]) for k in KEY_LAYOUT}), 23)
        self.assertEqual(KEY_INDEX["0"], 0)
        self.assertEqual(KEY_SPAN.get("Enter"), (2, 1))

    def test_mtech_hardware_row_alignment_and_chromatic_order(self):
        by_row = {r: [name for name, row, _ in KEY_LAYOUT if row == r] for r in range(6)}
        self.assertEqual(by_row[0], ["Home", "Tab", "Email", "Calc"])
        self.assertEqual(by_row[1], ["NumLock", "/", "*", "Backspace"])
        self.assertEqual(by_row[2], ["7", "8", "9", "-"])
        self.assertEqual(by_row[3], ["4", "5", "6", "+"])
        self.assertEqual(by_row[4], ["1", "2", "3", "Enter"])
        self.assertEqual(by_row[5], ["0", "000", "."])

    def test_required_keys_present(self):
        for name in ["0", "1", "2", "3", "4", "5", "6", "7", "8", "9", ".", "000", "Enter",
                     "+", "-", "*", "/", "Tab", "Backspace", "Calc", "Email", "Home", "NumLock"]:
            self.assertIn(name, KEY_INDEX)

    def test_windows_mapping(self):
        self.assertEqual(win_key_name(96, 0, False), "0")          # VK_NUMPAD0 (NumLock ON)
        self.assertEqual(win_key_name(0x2D, 0x52, False), "0")     # keypad VK_INSERT (NumLock OFF)
        self.assertEqual(win_key_name(0x2D, 0, False), "0")        # keypad VK_INSERT (scan 0)
        self.assertEqual(win_key_name(0x00, 0x52, False), "0")     # raw keypad scancode 0x52
        # Both standard and extended VK_INSERT map to keypad 0 for NumLock-independent burst detection
        self.assertEqual(win_key_name(0x2D, 0x52, True), "0")
        self.assertEqual(win_key_name(0x2D, 0x52, True, keypad_verified=True), "0")
        self.assertEqual(win_key_name(0x6E, 0x53, False), ".")     # VK_DECIMAL (NumLock ON)
        self.assertEqual(win_key_name(0x2E, 0x53, False), ".")     # keypad VK_DELETE (NumLock OFF)
        self.assertEqual(win_key_name(0x2E, 0, False), ".")        # keypad VK_DELETE (scan 0)
        self.assertIsNone(win_key_name(0x2E, 0x53, True))          # dedicated Delete key ignored
        self.assertEqual(win_key_name(105, 0, False), "9")
        self.assertEqual(win_key_name(13, 0, True), "Enter")
        self.assertIsNone(win_key_name(13, 0, False))  # main Enter is ignored
        self.assertEqual(win_key_name(0xAC, 0x32, True), "Home")   # VK_BROWSER_HOME
        self.assertEqual(win_key_name(0xAA, 0, False), "Home")     # VK_BROWSER_SEARCH
        self.assertEqual(win_key_name(0x00, 0x32, True), "Home")   # scancode 0xE032
        self.assertEqual(win_key_name(0xB4, 0, False), "Email")    # VK_LAUNCH_MAIL
        self.assertEqual(win_key_name(0xB6, 0, False), "Calc")     # VK_LAUNCH_APP1
        self.assertEqual(win_key_name(0xB7, 0, False), "Calc")     # VK_LAUNCH_APP2
        self.assertEqual(win_key_name(0x08, 0, False), "Backspace")
        self.assertEqual(win_key_name(36, 0x47, False), "7")       # NumLock-off numpad 7
        self.assertEqual(win_key_name(36, 0, False), "7")          # NumLock-off VK_HOME (scan 0)
        self.assertIsNone(win_key_name(65, 0, False))

    def test_exclusive_mode_default_on(self):
        hook, _ = make()
        self.assertTrue(hook._suppress)

    def test_start_and_stop_hook(self):
        hook, _ = make()
        hook.start()
        hook.stop()


class DebounceTests(unittest.TestCase):
    def test_autorepeat_gives_single_note_on(self):
        hook, rec = make()
        for _ in range(10):
            hook._dispatch("5", True)
        hook._dispatch("5", False)
        hook._dispatch("5", False)  # stray release
        self.assertEqual(rec.events, [("5", True), ("5", False)])

    def test_stray_release_ignored(self):
        hook, rec = make()
        hook._dispatch("7", False)
        self.assertEqual(rec.events, [])

    def test_single_zero_press(self):
        hook, rec = make()
        hook._dispatch("0", True)
        import time
        time.sleep(0.12)
        hook._dispatch("0", False)
        self.assertEqual(rec.events, [("0", True), ("0", False)])

    def test_zero_autorepeat_while_held(self):
        hook, rec = make()
        import time
        hook._dispatch("0", True)
        time.sleep(0.12)
        for _ in range(5):
            hook._dispatch("0", True)
        hook._dispatch("0", False)
        self.assertEqual(rec.events, [("0", True), ("0", False)])

    def test_triple_zero_down_up_pairs(self):
        hook, rec = make(burst_window_ms=2000)
        for _ in range(3):
            hook._dispatch("0", True)
            hook._dispatch("0", False)
        self.assertEqual(rec.events, [("000", True), ("000", False)])

    def test_triple_zero_all_downs_then_ups(self):
        hook, rec = make(burst_window_ms=2000)
        for _ in range(3):
            hook._dispatch("0", True)
        for _ in range(3):
            hook._dispatch("0", False)
        self.assertEqual(rec.events, [("000", True), ("000", False)])

    def test_zero_after_triple_still_works(self):
        hook, rec = make(burst_window_ms=500)
        import time
        for _ in range(3):
            hook._dispatch("0", True)
            hook._dispatch("0", False)
        hook._dispatch("0", True)
        time.sleep(0.7)
        hook._dispatch("0", False)
        self.assertEqual(
            rec.events,
            [("000", True), ("000", False), ("0", True), ("0", False)],
        )

    def test_triple_detection_can_be_disabled(self):
        hook, rec = make(detect_triple_zero=False)
        for _ in range(3):
            hook._dispatch("0", True)
            hook._dispatch("0", False)
        self.assertEqual(rec.events, [("0", True), ("0", False)] * 3)

    def test_stop_releases_held_keys(self):
        hook, rec = make()
        hook._dispatch("1", True)
        hook.stop()
        self.assertEqual(rec.events, [("1", True), ("1", False)])

    def test_rolling_50ms_triplet_burst_and_raw_dedup(self):
        from unittest.mock import patch
        from key_hook import KeyboardDevice, RI_KEY_BREAK

        # Default burst_window must be 75 ms (0.075 s)
        default_hook, _ = KeyHook(Recorder()), None
        self.assertAlmostEqual(default_hook.burst_window, 0.075, places=4)

        # 3 consecutive '0' presses spaced 30 ms apart (total span 60 ms > 50 ms,
        # but each consecutive interval is 30 ms < 50 ms) must fire '000' and never '0'
        clock = [100.0]
        with patch("key_hook.time.perf_counter", side_effect=lambda: clock[0]):
            hook, rec = make(burst_window_ms=50)
            for idx in range(3):
                if idx > 0:
                    clock[0] += 0.030  # +30 ms between consecutive presses (total 60 ms > 50 ms)
                hook._dispatch("0", True)
                clock[0] += 0.005
                hook._dispatch("0", False)
            hook.stop()
        self.assertEqual(rec.events, [("000", True), ("000", False)])

        # Verify _queue_from_ll + _process_raw_keyboard deduplication prevents duplicate
        # KEYDOWN/KEYUP events from corrupting the '000' state machine
        hook2, rec2 = make(burst_window_ms=500)
        hook2._devices = [KeyboardDevice(r"HID\VID_1C4F&PID_0002", "USB Keypad", "", {3003})]
        hook2._device_by_handle = {3003: hook2._devices[0]}
        hook2.set_target_device(r"HID\VID_1C4F&PID_0002")
        for _ in range(3):
            hook2._queue_from_ll("0", True)
            hook2._process_raw_keyboard(3003, 0x60, 0x52, 0)
            hook2._process_raw_keyboard(3003, 0x60, 0x52, RI_KEY_BREAK)
        while not hook2._win_queue.empty():
            item = hook2._win_queue.get_nowait()
            if item:
                hook2._dispatch(item[0], item[1])
        hook2.stop()
        self.assertEqual(rec2.events, [("000", True), ("000", False)])


class StartupSafetyTests(unittest.TestCase):
    def test_config_path_uses_user_data_dir(self):
        import sys
        from main import APP_NAME, Config, user_config_path

        cfg = Config()
        self.assertEqual(cfg.path, user_config_path())
        self.assertTrue(str(cfg.path).endswith("config.json"))
        if sys.platform == "win32":
            self.assertIn(APP_NAME, str(cfg.path))

    def test_mainwindow_does_not_override_nativeevent(self):
        from main import MainWindow

        self.assertNotIn("nativeEvent", MainWindow.__dict__)

    def test_icon_assets_exist_and_valid(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1] / "assets"
        for name in ("icon.png", "icon.ico", "icon.icns"):
            p = root / name
            self.assertTrue(p.is_file(), f"Missing asset {p}")
            self.assertGreater(p.stat().st_size, 1024)

    def test_extract_hardware_id_and_raw_device_filtering(self):
        from key_hook import KeyboardDevice, RI_KEY_BREAK, extract_hardware_id

        self.assertEqual(
            extract_hardware_id(r"\\?\HID#VID_04D9&PID_1203&MI_00#7&1234#0"),
            r"HID\VID_04D9&PID_1203",
        )
        self.assertEqual(
            extract_hardware_id(
                r"HID\{00001812-0000-1000-8000-00805f9b34fb}_Dev_VID&021235_PID&aa22_REV&0001"
            ),
            r"HID\VID_1235&PID_AA22",
        )

        hook, _ = make()
        hook._devices = [
            KeyboardDevice(r"ACPI\PNP0303", "Laptop Keyboard", "", {1001}),
            KeyboardDevice(r"HID\VID_04D9&PID_1203", "M-Tech USB Keypad", "", {2002}),
        ]
        hook._device_by_handle = {
            1001: hook._devices[0],
            2002: hook._devices[1],
        }
        # Default (""): all devices match
        hook.set_target_device("")
        self.assertTrue(hook.matches_device(1001))
        self.assertTrue(hook.matches_device(2002))

        # Select only the M-Tech USB keypad (handle 2002)
        hook.set_target_device(r"HID\VID_04D9&PID_1203")
        self.assertFalse(hook.matches_device(1001))
        self.assertTrue(hook.matches_device(2002))

        # Laptop keyboard (1001) is ignored; USB keypad (2002) is processed
        self.assertFalse(hook._process_raw_keyboard(1001, 0x67, 0x47, 0))
        self.assertTrue(hook._process_raw_keyboard(2002, 0x67, 0x47, 0))
        self.assertTrue(hook._process_raw_keyboard(2002, 0x67, 0x47, RI_KEY_BREAK))

    def test_home_appcommand_swallowed_with_target_device_and_combobox_nofocus(self):
        from PyQt6.QtCore import QEvent, Qt
        from PyQt6.QtGui import QKeyEvent
        from PyQt6.QtWidgets import QApplication
        from main import (
            APPCOMMAND_BROWSER_HOME_ALT,
            APPCOMMAND_LAUNCH_APP2,
            APPCOMMAND_LAUNCH_MAIL,
            Config,
            Controller,
            MainWindow,
            MidiEngine,
        )

        app = QApplication.instance() or QApplication([])
        cfg = Config()
        engine = MidiEngine()
        ctrl = Controller(cfg, engine)
        win = MainWindow(ctrl)
        try:
            # Verify all interactive controls refuse keyboard focus
            for widget in (
                win.device,
                win.device_combo,
                win.port,
                win.channel,
                win.velocity,
                win.suppress,
                win.monitor,
            ):
                self.assertEqual(widget.focusPolicy(), Qt.FocusPolicy.NoFocus)
            for pad in win._pads.values():
                self.assertEqual(pad.focusPolicy(), Qt.FocusPolicy.NoFocus)

            # Verify pressing '1', '2', Up, Down does not change Target USB Keypad combo selection
            win.device.clear()
            win.device.addItem("All Connected Keypads (Default)", "")
            win.device.addItem(r"USB Keypad (HID\VID_04D9&PID_1203)", r"HID\VID_04D9&PID_1203")
            win.device.setCurrentIndex(1)
            for key, text in (
                (Qt.Key.Key_1, "1"),
                (Qt.Key.Key_2, "2"),
                (Qt.Key.Key_Up, ""),
                (Qt.Key.Key_Down, ""),
            ):
                ev = QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier, text)
                QApplication.sendEvent(win.device, ev)
            self.assertEqual(win.device.currentIndex(), 1)

            # Verify Home/Mail/Calc WM_APPCOMMAND is swallowed even when a specific USB keypad is targeted
            ctrl.set_target_device(r"HID\VID_04D9&PID_1203")
            ctrl.hook._last_raw_matched = False
            self.assertTrue(ctrl.handle_appcommand(APPCOMMAND_BROWSER_HOME_ALT))
            self.assertTrue(ctrl.handle_appcommand(APPCOMMAND_LAUNCH_MAIL))
            self.assertTrue(ctrl.handle_appcommand(APPCOMMAND_LAUNCH_APP2))
        finally:
            ctrl.set_target_device("")
            ctrl.shutdown()

    def test_numlock_off_triple_zero_burst(self):
        from key_hook import KeyboardDevice, RI_KEY_BREAK, RI_KEY_E0, VK_INSERT

        # When NumLock is OFF, the '000' hardware key sends 3 rapid VK_INSERT (0x2D, scan 0x52)
        # strokes. With a verified target keypad, even E0-flagged strokes must trigger '000'.
        for flags_down in (0, RI_KEY_E0):
            hook, rec = make(burst_window_ms=500)
            hook._devices = [KeyboardDevice(r"HID\VID_1C4F&PID_0002", "USB Keypad", "", {3003})]
            hook._device_by_handle = {3003: hook._devices[0]}
            hook.set_target_device(r"HID\VID_1C4F&PID_0002")
            for _ in range(3):
                name = win_key_name(
                    VK_INSERT, 0x52, bool(flags_down & RI_KEY_E0), keypad_verified=True
                )
                self.assertEqual(name, "0")
                hook._queue_from_ll(name, True, VK_INSERT)
                hook._process_raw_keyboard(3003, VK_INSERT, 0x52, flags_down)
                hook._process_raw_keyboard(3003, VK_INSERT, 0x52, flags_down | RI_KEY_BREAK)
            while not hook._win_queue.empty():
                item = hook._win_queue.get_nowait()
                if item:
                    hook._dispatch(*item)
            hook.stop()
            self.assertEqual(rec.events, [("000", True), ("000", False)])

    def test_triple_zero_burst_numlock_on_and_off_identical(self):
        """3x VK_NUMPAD0 (NumLock ON) and 3x keypad VK_INSERT (NumLock OFF) both yield '000'."""
        from unittest.mock import patch
        from key_hook import VK_INSERT, VK_NUMPAD0

        scenarios = {
            "NumLock ON (3x VK_NUMPAD0)": [VK_NUMPAD0] * 3,
            "NumLock OFF (3x VK_INSERT)": [VK_INSERT] * 3,
            "Mixed (NumLock toggled mid-burst)": [VK_NUMPAD0, VK_INSERT, VK_INSERT],
        }
        for label, vks in scenarios.items():
            with self.subTest(label):
                clock = [500.0]
                with patch("key_hook.time.perf_counter", side_effect=lambda: clock[0]):
                    hook, rec = make(burst_window_ms=50)
                    for vk in vks:
                        # Real M-Tech timing: ~8 ms between every down/up edge
                        name = win_key_name(vk, 0x52, False)
                        self.assertEqual(name, "0")
                        hook._dispatch(name, True, vk)
                        clock[0] += 0.008
                        hook._dispatch(name, False, vk)
                        clock[0] += 0.008
                    hook.stop()
                self.assertEqual(rec.events, [("000", True), ("000", False)], label)

        # A single slow press still yields a plain '0' in both NumLock states
        for vk in (VK_NUMPAD0, VK_INSERT):
            with self.subTest(f"single press vk=0x{vk:02X}"):
                clock = [900.0]
                with patch("key_hook.time.perf_counter", side_effect=lambda: clock[0]):
                    hook, rec = make(burst_window_ms=50)
                    hook._dispatch("0", True, vk)
                    clock[0] += 0.200
                    hook._dispatch("0", False, vk)
                    hook.stop()
                self.assertEqual(rec.events, [("0", True), ("0", False)])

    def test_triple_zero_fires_note_37_at_velocity_127(self):
        from PyQt6.QtWidgets import QApplication
        from main import Config, Controller, MidiEngine

        QApplication.instance() or QApplication([])
        engine = MidiEngine()
        sent = []
        engine.note_on = lambda ch, n, vel=100: sent.append(("ON", n, vel))
        engine.note_off = lambda ch, n: sent.append(("OFF", n))
        ctrl = Controller(Config(), engine)
        orig_excl = ctrl.exclusive_pad_mode
        try:
            ctrl.set_exclusive_pad_mode(False)
            sent.clear()
            ctrl.handle_key("000", True)
            ctrl.handle_key("000", False)
            self.assertEqual(sent, [("ON", 37, 127), ("OFF", 37)])
        finally:
            ctrl.set_exclusive_pad_mode(orig_excl)
            ctrl.shutdown()

    def test_exclusive_pad_mode_choke_and_dot_toggle(self):
        from PyQt6.QtWidgets import QApplication
        from main import Config, Controller, MainWindow, MidiEngine

        app = QApplication.instance() or QApplication([])
        cfg = Config()
        engine = MidiEngine()
        midi_log = []
        orig_on = engine.note_on
        orig_off = engine.note_off
        engine.note_on = lambda ch, n, vel=100: (midi_log.append(("ON", ch, n, vel)), orig_on(ch, n, vel))
        engine.note_off = lambda ch, n: (midi_log.append(("OFF", ch, n, 0)), orig_off(ch, n))
        ctrl = Controller(cfg, engine)
        win = MainWindow(ctrl)
        orig_excl = ctrl.exclusive_pad_mode
        try:
            ctrl.set_exclusive_pad_mode(False)
            midi_log.clear()

            # '.' key must never map to a MIDI note and must toggle exclusive_pad_mode
            self.assertIsNone(ctrl.note_for("."))
            ctrl.handle_key(".", True)
            ctrl.handle_key(".", False)
            self.assertTrue(ctrl.exclusive_pad_mode)
            self.assertTrue(win.exclusive_pad.isChecked())
            self.assertTrue(win._pads["."]._mode_active)
            self.assertEqual(midi_log, [])

            # Press Pad '1' (Note 39): sends Note On, and releasing key does NOT send Note Off
            ctrl.handle_key("1", True)
            ctrl.handle_key("1", False)
            self.assertEqual(ctrl.currently_active_note, 39)
            self.assertEqual(midi_log, [("ON", 1, 39, 100)])

            # Press Pad '2' (Note 40): immediately chokes Note 39 (Note Off) BEFORE sending Note 40 On
            ctrl.handle_key("2", True)
            ctrl.handle_key("2", False)
            self.assertEqual(ctrl.currently_active_note, 40)
            self.assertEqual(
                midi_log,
                [
                    ("ON", 1, 39, 100),
                    ("OFF", 1, 39, 0),
                    ("ON", 1, 40, 100),
                ],
            )

            # Pressing the same Pad '2' again toggles Note 40 off and sets currently_active_note = None
            ctrl.handle_key("2", True)
            ctrl.handle_key("2", False)
            self.assertIsNone(ctrl.currently_active_note)
            self.assertEqual(
                midi_log,
                [
                    ("ON", 1, 39, 100),
                    ("OFF", 1, 39, 0),
                    ("ON", 1, 40, 100),
                    ("OFF", 1, 40, 0),
                ],
            )

            # Pressing '.' again disables exclusive_pad_mode
            ctrl.handle_key(".", True)
            ctrl.handle_key(".", False)
            self.assertFalse(ctrl.exclusive_pad_mode)
            self.assertFalse(win.exclusive_pad.isChecked())
            self.assertFalse(win._pads["."]._mode_active)
        finally:
            ctrl.set_exclusive_pad_mode(orig_excl)
            ctrl.shutdown()

    def test_numlock_off_vk_insert_hardware_cadence_burst(self):
        """M-Tech hardware cadence (~50-80 ms between edges, ~110-130 ms between KEYDOWNs) yields Note 37."""
        from unittest.mock import patch
        from key_hook import VK_INSERT

        clock = [2000.0]
        with patch("key_hook.time.perf_counter", side_effect=lambda: clock[0]):
            hook, rec = make(burst_window_ms=60)
            # Timestamps matching live logs: down at 0, up at +48, down at +120, up at +192, down at +248, up at +312
            hook._dispatch("0", True, VK_INSERT)
            clock[0] += 0.048
            hook._dispatch("0", False, VK_INSERT)
            clock[0] += 0.072  # +72 ms from up (120 ms from 1st down)
            hook._dispatch("0", True, VK_INSERT)
            clock[0] += 0.072
            hook._dispatch("0", False, VK_INSERT)
            clock[0] += 0.056  # +56 ms from up (128 ms from 2nd down)
            hook._dispatch("0", True, VK_INSERT)
            clock[0] += 0.064
            hook._dispatch("0", False, VK_INSERT)
            hook.stop()
        self.assertEqual(rec.events, [("000", True), ("000", False)])

    def test_virtual_midi_port_teardown_and_reinitialize(self):
        from unittest.mock import MagicMock
        from midi_engine import MidiEngine, TeVirtualMidiPort

        port1 = TeVirtualMidiPort()
        port1._handle = 12345
        port1.name = "NumpadToMIDI"
        TeVirtualMidiPort._active_ports["NumpadToMIDI"] = port1

        # Test proactive cleanup of active ports
        self.assertIn("NumpadToMIDI", TeVirtualMidiPort._active_ports)
        TeVirtualMidiPort.cleanup_orphaned("NumpadToMIDI")
        self.assertNotIn("NumpadToMIDI", TeVirtualMidiPort._active_ports)
        self.assertIsNone(port1._handle)

        # Test MidiEngine.reinitialize_virtual_port sequence
        engine = MidiEngine()
        engine.open_auto = MagicMock(return_value="virtual")
        res = engine.reinitialize_virtual_port()
        engine.open_auto.assert_called_once_with("")
        self.assertEqual(res, "virtual")

    def test_uac_admin_in_build_spec(self):
        from pathlib import Path

        spec_text = (Path(__file__).resolve().parents[1] / "build.spec").read_text(encoding="utf-8")
        self.assertIn("uac_admin=True", spec_text)

    def test_tevirtualmidi_error_5_fallback(self):
        from unittest.mock import MagicMock, patch
        from midi_engine import MidiEngine, TeVirtualMidiPort

        engine = MidiEngine()
        # Mock port creation failure with Error Code 5 (Access Denied)
        with patch.object(TeVirtualMidiPort, "available", return_value=True):
            with patch.object(TeVirtualMidiPort, "open", return_value=False):
                TeVirtualMidiPort.last_error_code = 5
                TeVirtualMidiPort.last_error_message = "error 5 (Access is denied.)"
                engine.list_ports = MagicMock(return_value=["loopMIDI Port"])
                engine._open_index = MagicMock(return_value="loopback")
                mode = engine.open_auto("")
                # Verify it does not crash, catches error 5, and falls back to loopMIDI
                self.assertEqual(mode, "loopback")
                engine._open_index.assert_called_once_with(0, "loopMIDI Port", "loopback")

    def test_async_queue_000_burst_detection(self):
        """Worker thread processing items from _win_queue fires '000' on 3 presses."""
        from key_hook import VK_INSERT, VK_NUMPAD0

        hook, rec = make(burst_window_ms=75)
        # Simulate pushing 3 rapid strokes into _win_queue
        for vk in (VK_NUMPAD0, VK_INSERT, VK_NUMPAD0):
            hook._win_queue.put(("0", True, vk))
            hook._win_queue.put(("0", False, vk))

        # Process through worker dispatch
        while not hook._win_queue.empty():
            item = hook._win_queue.get_nowait()
            if item:
                hook._dispatch(item[0], item[1], item[2])
        hook.stop()
        self.assertEqual(rec.events, [("000", True), ("000", False)])


if __name__ == "__main__":
    unittest.main()
