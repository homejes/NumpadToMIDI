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

    def test_macos_mapping(self):
        from key_hook import MAC_VK
        # NumLock ON keycodes
        self.assertEqual(MAC_VK[82], "0")
        self.assertEqual(MAC_VK[65], ".")
        self.assertEqual(MAC_VK[71], "NumLock")
        self.assertEqual(MAC_VK[76], "Enter")
        # NumLock OFF keycodes
        self.assertEqual(MAC_VK[114], "0")          # kVK_Help / PC Insert -> '0' / '000'
        self.assertEqual(MAC_VK[117], ".")          # kVK_ForwardDelete / PC Delete -> '.'
        self.assertEqual(MAC_VK[119], "1")          # kVK_End
        self.assertEqual(MAC_VK[125], "2")          # kVK_Down
        self.assertEqual(MAC_VK[121], "3")          # kVK_PageDown
        self.assertEqual(MAC_VK[123], "4")          # kVK_Left
        self.assertEqual(MAC_VK[87], "5")           # 5
        self.assertEqual(MAC_VK[124], "6")          # kVK_Right
        self.assertEqual(MAC_VK[89], "7")           # 7
        self.assertEqual(MAC_VK[126], "8")          # kVK_Up
        self.assertEqual(MAC_VK[116], "9")          # kVK_PageUp
        self.assertEqual(MAC_VK[115], "Home")       # kVK_Home
        self.assertEqual(MAC_VK[48], "Tab")
        self.assertEqual(MAC_VK[51], "Backspace")

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

        # Default burst_window must be 100 ms (0.100 s)
        default_hook, _ = KeyHook(Recorder()), None
        self.assertAlmostEqual(default_hook.burst_window, 0.100, places=4)

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
        try:
            sent.clear()
            ctrl.handle_key("000", True)
            ctrl.handle_key("000", False)
            self.assertEqual(sent, [("ON", 37, 127), ("OFF", 37)])
        finally:
            ctrl.shutdown()

    def test_dot_key_maps_to_note_38(self):
        from PyQt6.QtWidgets import QApplication
        from main import Config, Controller, MainWindow, MidiEngine

        app = QApplication.instance() or QApplication([])
        cfg = Config()
        engine = MidiEngine()
        midi_log = []
        engine.note_on = lambda ch, n, vel=100: midi_log.append(("ON", ch, n, vel))
        engine.note_off = lambda ch, n: midi_log.append(("OFF", ch, n))
        ctrl = Controller(cfg, engine)
        win = MainWindow(ctrl)
        try:
            # '.' key maps to standard MIDI Note 38 (D1)
            self.assertEqual(ctrl.note_for("."), 38)
            self.assertEqual(win._pads["."].display, ".")
            ctrl.handle_key(".", True)
            ctrl.handle_key(".", False)
            self.assertEqual(midi_log, [("ON", 1, 38, 100), ("OFF", 1, 38)])
        finally:
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
        self.assertIn("uac_admin=False", spec_text)
        self.assertIn("NSAccessibilityUsageDescription", spec_text)
        self.assertIn("NSAppleEventsUsageDescription", spec_text)
        self.assertIn("LSUIElement", spec_text)

    def test_device_specific_numlock_suppression(self):
        from key_hook import _ALWAYS_SUPPRESS_KEYS, win_key_name, VK_NUMLOCK
        # NumLock is NOT globally in _ALWAYS_SUPPRESS_KEYS so primary keyboard is unaffected
        self.assertNotIn("NumLock", _ALWAYS_SUPPRESS_KEYS)
        self.assertEqual(win_key_name(VK_NUMLOCK, 0x45, False), "NumLock")

    def test_check_macos_permissions(self):
        from key_hook import check_macos_permissions
        self.assertTrue(check_macos_permissions(prompt=False))

    def test_tevirtualmidi_error_5_fallback(self):
        from unittest.mock import MagicMock, patch
        from midi_engine import MidiEngine, TeVirtualMidiPort

        engine = MidiEngine()
        # Mock port creation failure with Error Code 5 (Access Denied)
        with patch("midi_engine.IS_WIN", True):
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

    def test_macos_numlock_off_burst_and_labels(self):
        """macOS keycode 114 (PC Insert) maps to 0 and rapid triplet fires '000'."""
        from key_hook import zero_vk_label, VK_NUMPAD0, VK_INSERT

        self.assertEqual(zero_vk_label(VK_NUMPAD0), "VK_NUMPAD0/NumLock-ON")
        self.assertEqual(zero_vk_label(82), "VK_NUMPAD0/NumLock-ON")
        self.assertEqual(zero_vk_label(VK_INSERT), "VK_INSERT/NumLock-OFF")
        self.assertEqual(zero_vk_label(114), "VK_INSERT/NumLock-OFF")

        hook, rec = make(burst_window_ms=75)
        # 3 rapid strokes of keycode 114
        for _ in range(3):
            hook._dispatch("0", True, 114)
            hook._dispatch("0", False, 114)
        hook.stop()
        self.assertEqual(rec.events, [("000", True), ("000", False)])

    def test_check_macos_accessibility(self):
        from key_hook import check_macos_accessibility
        # Under Windows/Linux, always returns True
        self.assertTrue(check_macos_accessibility(prompt=False))

    def test_list_macos_keyboard_devices(self):
        from key_hook import list_macos_keyboard_devices
        # On non-macOS, returns empty list
        devs = list_macos_keyboard_devices()
        self.assertIsInstance(devs, list)

    def test_scancode_first_mapping_all_23_keys(self):
        """Scancode-First Mapping maps hardware OEM scancodes directly to correct pads."""
        from key_hook import win_key_name, VK_NUMLOCK

        # Non-extended numpad keys (independent of NumLock state)
        self.assertEqual(win_key_name(0, 0x52, False), "0")
        self.assertEqual(win_key_name(0x2D, 0x52, False), "0")     # VK_INSERT
        self.assertEqual(win_key_name(0x60, 0x52, False), "0")     # VK_NUMPAD0
        self.assertEqual(win_key_name(0x23, 0x4F, False), "1")     # VK_END
        self.assertEqual(win_key_name(0x61, 0x4F, False), "1")     # VK_NUMPAD1
        self.assertEqual(win_key_name(0x28, 0x50, False), "2")     # VK_DOWN
        self.assertEqual(win_key_name(0x62, 0x50, False), "2")     # VK_NUMPAD2
        self.assertEqual(win_key_name(0x22, 0x51, False), "3")     # VK_NEXT
        self.assertEqual(win_key_name(0x63, 0x51, False), "3")     # VK_NUMPAD3
        self.assertEqual(win_key_name(0x25, 0x4B, False), "4")     # VK_LEFT
        self.assertEqual(win_key_name(0x64, 0x4B, False), "4")     # VK_NUMPAD4
        self.assertEqual(win_key_name(0x0C, 0x4C, False), "5")     # VK_CLEAR
        self.assertEqual(win_key_name(0x65, 0x4C, False), "5")     # VK_NUMPAD5
        self.assertEqual(win_key_name(0x27, 0x4D, False), "6")     # VK_RIGHT
        self.assertEqual(win_key_name(0x66, 0x4D, False), "6")     # VK_NUMPAD6
        self.assertEqual(win_key_name(0x24, 0x47, False), "7")     # VK_HOME
        self.assertEqual(win_key_name(0x67, 0x47, False), "7")     # VK_NUMPAD7
        self.assertEqual(win_key_name(0x26, 0x48, False), "8")     # VK_UP
        self.assertEqual(win_key_name(0x68, 0x48, False), "8")     # VK_NUMPAD8
        self.assertEqual(win_key_name(0x21, 0x49, False), "9")     # VK_PRIOR
        self.assertEqual(win_key_name(0x69, 0x49, False), "9")     # VK_NUMPAD9
        self.assertEqual(win_key_name(0x2E, 0x53, False), ".")     # VK_DELETE
        self.assertEqual(win_key_name(0x6E, 0x53, False), ".")     # VK_DECIMAL
        self.assertEqual(win_key_name(0x6B, 0x4E, False), "+")     # VK_ADD
        self.assertEqual(win_key_name(0x6D, 0x4A, False), "-")     # VK_SUBTRACT
        self.assertEqual(win_key_name(0x6A, 0x37, False), "*")     # VK_MULTIPLY
        self.assertEqual(win_key_name(VK_NUMLOCK, 0x45, False), "NumLock")
        self.assertEqual(win_key_name(0x08, 0x0E, False), "Backspace")
        self.assertEqual(win_key_name(0x09, 0x0F, False), "Tab")

        # Extended numpad keys
        self.assertEqual(win_key_name(0x6F, 0x35, True), "/")      # Numpad /
        self.assertEqual(win_key_name(0x0D, 0x1C, True), "Enter")  # Numpad Enter
        self.assertEqual(win_key_name(0xAC, 0x32, True), "Home")   # Browser Home
        self.assertEqual(win_key_name(0xB4, 0x6C, True), "Email")  # Launch Mail
        self.assertEqual(win_key_name(0xB7, 0x21, True), "Calc")   # Launch Calc

    def test_triplet_burst_by_scancode_0x52(self):
        """3 rapid presses of Scancode 0x52 (<80ms) cleanly fire '000' (MIDI Note 37)."""
        from key_hook import win_key_name

        hook, rec = make(burst_window_ms=75)
        # Verify win_key_name maps 0x52 to "0" across different VKs
        for vk in (0x2D, 0x60, 0x00):
            self.assertEqual(win_key_name(vk, 0x52, False), "0")
            hook._dispatch("0", True, vk)
            hook._dispatch("0", False, vk)
        hook.stop()
        self.assertEqual(rec.events, [("000", True), ("000", False)])

    def test_parse_hid_keyboard_report(self):
        """parse_hid_keyboard_report correctly extracts Key Down / Key Up from USB HID reports."""
        from key_hook import parse_hid_keyboard_report

        # Initial press of Keypad 1 (usage 0x59)
        report1 = [0, 0, 0x59, 0, 0, 0, 0, 0]
        events1, held1 = parse_hid_keyboard_report(report1, set())
        self.assertEqual(events1, [("1", True, 0x59)])
        self.assertEqual(held1, {0x59})

        # Add Keypad 2 (usage 0x5A) while holding Keypad 1
        report2 = [0, 0, 0x59, 0x5A, 0, 0, 0, 0]
        events2, held2 = parse_hid_keyboard_report(report2, held1)
        self.assertEqual(events2, [("2", True, 0x5A)])
        self.assertEqual(held2, {0x59, 0x5A})

        # Release both
        report_empty = [0, 0, 0, 0, 0, 0, 0, 0]
        events3, held3 = parse_hid_keyboard_report(report_empty, held2)
        self.assertEqual(events3, [("1", False, 0x59), ("2", False, 0x5A)])
        self.assertEqual(held3, set())

        # Test 9-byte report with report ID for Keypad 0 (usage 0x62)
        report_id = [1, 0, 0, 0x62, 0, 0, 0, 0, 0]
        events4, held4 = parse_hid_keyboard_report(report_id, set())
        self.assertEqual(events4, [("0", True, 0x62)])
        self.assertEqual(held4, {0x62})

    def test_parse_hid_consumer_report(self):
        """parse_hid_consumer_report translates Consumer Usage IDs (Page 0x0C)."""
        from key_hook import parse_hid_consumer_report

        # AC Home (usage 0x0223)
        report_home = [0x23, 0x02]
        events1, held1 = parse_hid_consumer_report(report_home, set())
        self.assertEqual(events1, [("Home", True, 0x0223)])
        self.assertEqual(held1, {0x0223})

        # Release
        events_rel, held_rel = parse_hid_consumer_report([0, 0], held1)
        self.assertEqual(events_rel, [("Home", False, 0x0223)])
        self.assertEqual(held_rel, set())

        # AL Email (0x018A) with report ID (3-byte)
        report_email = [2, 0x8A, 0x01]
        events2, held2 = parse_hid_consumer_report(report_email, set())
        self.assertEqual(events2, [("Email", True, 0x018A)])

        # AL Calculator (0x0192)
        report_calc = [2, 0x92, 0x01]
        events3, held3 = parse_hid_consumer_report(report_calc, set())
        self.assertEqual(events3, [("Calc", True, 0x0192)])

    def test_direct_hid_000_triplet_burst(self):
        """Direct HID 8-byte reports of usage 0x62 trigger '000' (MIDI Note 37)."""
        from key_hook import parse_hid_keyboard_report

        hook, rec = make(burst_window_ms=75)
        held = set()
        for _ in range(3):
            # Press usage 0x62 (Keypad 0)
            events_down, held = parse_hid_keyboard_report([0, 0, 0x62, 0, 0, 0, 0, 0], held)
            for name, is_down, usage in events_down:
                hook._dispatch(name, is_down, usage)
            # Release
            events_up, held = parse_hid_keyboard_report([0, 0, 0, 0, 0, 0, 0, 0], held)
            for name, is_down, usage in events_up:
                hook._dispatch(name, is_down, usage)

        hook.stop()
        self.assertEqual(rec.events, [("000", True), ("000", False)])

    def test_list_macos_hid_devices(self):
        from key_hook import list_macos_hid_devices
        devs = list_macos_hid_devices()
        self.assertIsInstance(devs, list)

    def test_mac_multi_interface_separation_and_dispatch(self):
        """_find_mac_device_interfaces separates keyboard and consumer endpoints on composite USB numpads."""
        from unittest.mock import patch
        import key_hook

        mock_devices = [
            # Interface 0: Generic Desktop Keyboard (Digits 0-9, ., Enter, etc.)
            {
                "vendor_id": 0x1234,
                "product_id": 0x5678,
                "path": b"/dev/hidraw_kb",
                "usage_page": 1,
                "usage": 6,
                "interface_number": 0,
            },
            # Interface 1: Consumer Control (Home, Email, Calc)
            {
                "vendor_id": 0x1234,
                "product_id": 0x5678,
                "path": b"/dev/hidraw_consumer",
                "usage_page": 0x0C,
                "usage": 1,
                "interface_number": 1,
            },
        ]

        hook, rec = make()
        with patch.object(key_hook, "HAS_HID", True):
            if hasattr(key_hook, "hid"):
                with patch.object(key_hook.hid, "enumerate", return_value=mock_devices):
                    # Test unassigned target device finds both interfaces
                    kb_paths, cons_paths = hook._find_mac_device_interfaces()
                    self.assertIn(b"/dev/hidraw_kb", kb_paths)
                    self.assertIn(b"/dev/hidraw_consumer", cons_paths)

                    # Test target matching VID/PID
                    hook.set_target_device("HID\\VID_1234&PID_5678")
                    kb_paths, cons_paths = hook._find_mac_device_interfaces()
                    self.assertEqual(kb_paths, [b"/dev/hidraw_kb"])
                    self.assertEqual(cons_paths, [b"/dev/hidraw_consumer"])

                    # Test non-matching target filters out
                    hook.set_target_device("HID\\VID_9999&PID_9999")
                    kb_paths, cons_paths = hook._find_mac_device_interfaces()
                    self.assertEqual(kb_paths, [])
                    self.assertEqual(cons_paths, [])

        # Verify dual-interface report dispatching: keyboard key + consumer key
        hook._dispatch("1", True)
        hook._dispatch("1", False)
        hook._dispatch("Home", True)
        hook._dispatch("Home", False)
        self.assertEqual(rec.events, [("1", True), ("1", False), ("Home", True), ("Home", False)])
        hook.stop()

    def test_mac_target_device_switch_safe_and_intercept(self):
        """Selecting another target device does not crash or restart threads while running."""
        import key_hook
        from key_hook import MAC_VK, VK_INSERT

        hook, rec = make()
        hook._running = True

        # Switching target device while running must not call _mac_stop / _mac_start
        # and must update cleanly without crashing
        hook.set_target_device("HID\\VID_04D9&PID_1203")
        self.assertEqual(hook.target_device, "HID\\VID_04D9&PID_1203")
        self.assertTrue(hook._running)

        hook.set_target_device("")
        self.assertEqual(hook.target_device, "")
        self.assertTrue(hook._running)

        # Test _mac_intercept simulation
        class MockEvent:
            pass

        class MockQuartz:
            kCGEventKeyDown = 1
            kCGEventKeyUp = 2
            kCGKeyboardEventKeycode = 0

            @staticmethod
            def CGEventGetIntegerValueField(evt, field):
                if field == 0:
                    return getattr(evt, "keycode", 0)
                if field == 87:
                    return getattr(evt, "sender_id", 0)
                return 0

        # Simulate keycode 83 (Keypad 1) down
        evt = MockEvent()
        evt.keycode = 83
        evt.sender_id = 0

        from unittest.mock import patch
        with patch.dict("sys.modules", {"Quartz": MockQuartz}):
            res = hook._mac_intercept(MockQuartz.kCGEventKeyDown, evt)
            # Default suppress is True, so intercept returns None (swallows)
            self.assertIsNone(res)
            self.assertEqual(rec.events[-1], ("1", True))

            # Simulate keycode 76 (Keypad Enter) down
            evt.keycode = 76
            res2 = hook._mac_intercept(MockQuartz.kCGEventKeyDown, evt)
            self.assertIsNone(res2)
            self.assertEqual(rec.events[-1], ("Enter", True))

            # Simulate keycode 65 (Keypad .) down
            evt.keycode = 65
            res3 = hook._mac_intercept(MockQuartz.kCGEventKeyDown, evt)
            self.assertIsNone(res3)
            self.assertEqual(rec.events[-1], (".", True))

        hook.stop()


if __name__ == "__main__":
    unittest.main()
