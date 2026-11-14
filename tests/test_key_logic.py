import time
import unittest

from key_hook import (
    KEY_INDEX,
    KEY_LAYOUT,
    KEY_SPAN,
    KeyboardDevice,
    KeyHook,
    VK_ADD,
    VK_BACK,
    VK_DECIMAL,
    VK_DIVIDE,
    VK_MULTIPLY,
    VK_NUMLOCK,
    VK_NUMPAD0,
    VK_RETURN,
    VK_SUBTRACT,
    VK_TAB,
    win_key_name,
)


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
        # v1.9.8 strict isolation: "" means "no device selected" -> nothing
        # matches, so the WM_INPUT channel stays silent for every keyboard.
        hook.set_target_device("")
        self.assertFalse(hook.matches_device(1001))
        self.assertFalse(hook.matches_device(2002))

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
            win.device.addItem(r"USB Keypad A (HID\VID_04D9&PID_1203)", r"HID\VID_04D9&PID_1203")
            win.device.addItem(r"USB Keypad B (HID\VID_1C4F&PID_0002)", r"HID\VID_1C4F&PID_0002")
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

    def test_zero_vk_label_reports_scancode_0x52(self):
        """Scancode-tracked '0' strokes are labelled with the hardware scancode."""
        from key_hook import zero_vk_label, VK_NUMPAD0, VK_INSERT

        self.assertEqual(
            zero_vk_label(VK_NUMPAD0, 0x52), "VK_NUMPAD0/NumLock-ON [Scancode 0x52]"
        )
        self.assertEqual(
            zero_vk_label(VK_INSERT, 0x52), "VK_INSERT/NumLock-OFF [Scancode 0x52]"
        )
        self.assertEqual(zero_vk_label(0, 0x52), "KEYPAD0 [Scancode 0x52]")
        self.assertEqual(zero_vk_label(VK_NUMPAD0), "VK_NUMPAD0/NumLock-ON")

    def test_scancode_threaded_triplet_burst(self):
        """Queue items carry the hardware scancode; 3 rapid 0x52 strokes fire '000'.

        This is the exact tuple shape produced by WH_KEYBOARD_LL (_queue_from_ll)
        and Win32 Raw Input (_process_raw_keyboard): (name, is_down, vk, scan).
        """
        from unittest.mock import patch
        from key_hook import VK_NUMPAD0, VK_INSERT

        scenarios = {
            "NumLock ON (3x VK_NUMPAD0, scan 0x52)": [VK_NUMPAD0] * 3,
            "NumLock OFF (3x VK_INSERT, scan 0x52)": [VK_INSERT] * 3,
            "Mixed with scancode 0x52": [VK_NUMPAD0, VK_INSERT, VK_NUMPAD0],
        }
        for label, vks in scenarios.items():
            with self.subTest(label):
                clock = [10.0]
                with patch("key_hook.time.perf_counter", side_effect=lambda: clock[0]):
                    hook, rec = make(burst_window_ms=75)
                    for vk in vks:
                        hook._win_queue.put(("0", True, vk, 0x52))
                        clock[0] += 0.008
                        hook._win_queue.put(("0", False, vk, 0x52))
                        clock[0] += 0.008
                    while not hook._win_queue.empty():
                        item = hook._win_queue.get_nowait()
                        if item:
                            hook._dispatch(*item)
                    hook.stop()
                self.assertEqual(rec.events, [("000", True), ("000", False)], label)

        # A single slow scancode-0x52 press still yields a plain '0'
        with self.subTest("single slow press (scan 0x52)"):
            hook, rec = make(burst_window_ms=75)
            hook._win_queue.put(("0", True, VK_NUMPAD0, 0x52))
            hook._dispatch(*hook._win_queue.get_nowait())
            import time

            time.sleep(0.12)
            hook._win_queue.put(("0", False, VK_NUMPAD0, 0x52))
            hook._dispatch(*hook._win_queue.get_nowait())
            hook.stop()
            self.assertEqual(rec.events, [("0", True), ("0", False)])

    def test_mac_usage_name_maps_both_interfaces(self):
        """mac_usage_name routes GenericDesktop/Keyboard/Consumer pages correctly."""
        from key_hook import (
            HID_PAGE_CONSUMER,
            HID_PAGE_GENERIC_DESKTOP,
            HID_PAGE_KEYBOARD,
            mac_usage_name,
        )

        # Keyboard interface: keypad digits usages 0x59..0x62
        self.assertEqual(mac_usage_name(HID_PAGE_KEYBOARD, 0x59), "1")
        self.assertEqual(mac_usage_name(HID_PAGE_KEYBOARD, 0x62), "0")
        self.assertEqual(mac_usage_name(HID_PAGE_KEYBOARD, 0x63), ".")
        self.assertEqual(mac_usage_name(HID_PAGE_KEYBOARD, 0x58), "Enter")
        # GenericDesktop primary page also routes to the keypad map
        self.assertEqual(mac_usage_name(HID_PAGE_GENERIC_DESKTOP, 0x5A), "2")
        # Consumer interface: Home / Email / Calc
        self.assertEqual(mac_usage_name(HID_PAGE_CONSUMER, 0x0223), "Home")
        self.assertEqual(mac_usage_name(HID_PAGE_CONSUMER, 0x018A), "Email")
        self.assertEqual(mac_usage_name(HID_PAGE_CONSUMER, 0x0192), "Calc")
        # Unmapped page (e.g. digitizer 0x0D) is ignored
        self.assertIsNone(mac_usage_name(0x0D, 0x01))

    def test_hid_keypad_usages_route_to_midi_notes(self):
        """Usages 0x59 (keypad 1) .. 0x62 (keypad 0) resolve to MIDI notes ≧ 36."""
        from key_hook import HID_KEYPAD_USAGE_MAP, KEY_INDEX

        for usage in range(0x59, 0x63):  # keypad 1..0, per the task spec
            name = HID_KEYPAD_USAGE_MAP[usage]
            note = 36 + KEY_INDEX[name] if name != "000" else 37
            self.assertTrue(36 <= note <= 127, f"usage 0x{usage:02X} -> note {note}")

    def test_mac_tap_code_sets(self):
        """CGEventTap code sets: keypad cluster swallowed; shared keys protected."""
        from key_hook import _MAC_TAP_NAV_CODES, _MAC_TAP_NUMPAD_CODES

        # Tab (48) / Backspace (51) are shared with the internal keyboard
        self.assertNotIn(48, _MAC_TAP_NUMPAD_CODES)
        self.assertNotIn(51, _MAC_TAP_NUMPAD_CODES)
        # The full kVK_ANSI keypad cluster is always swallow-eligible
        for code in (65, 67, 69, 71, 75, 76, 78,
                     82, 83, 84, 85, 86, 87, 88, 89, 91, 92):
            self.assertIn(code, _MAC_TAP_NUMPAD_CODES)
        self.assertEqual(len(_MAC_TAP_NUMPAD_CODES), 17)
        # The PC-navigation cluster never overlaps the always-safe set
        self.assertTrue(_MAC_TAP_NUMPAD_CODES.isdisjoint(_MAC_TAP_NAV_CODES))
        self.assertEqual(
            _MAC_TAP_NAV_CODES,
            frozenset({114, 115, 116, 117, 119, 121, 123, 124, 125, 126}),
        )

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

        # Verify HID_KEYPAD_USAGE_MAP coverage
        from key_hook import HID_KEYPAD_USAGE_MAP, HID_CONSUMER_USAGE_MAP
        self.assertEqual(HID_KEYPAD_USAGE_MAP[0x59], "1")
        self.assertEqual(HID_KEYPAD_USAGE_MAP[0x62], "0")
        self.assertEqual(HID_KEYPAD_USAGE_MAP[0x63], ".")
        self.assertEqual(HID_KEYPAD_USAGE_MAP[0x58], "Enter")
        self.assertEqual(HID_KEYPAD_USAGE_MAP[0x57], "+")
        self.assertEqual(HID_KEYPAD_USAGE_MAP[0x56], "-")
        self.assertEqual(HID_KEYPAD_USAGE_MAP[0x55], "*")
        self.assertEqual(HID_KEYPAD_USAGE_MAP[0x54], "/")
        self.assertEqual(HID_KEYPAD_USAGE_MAP[0x53], "NumLock")
        self.assertEqual(HID_KEYPAD_USAGE_MAP[0x1E], "1")
        self.assertEqual(HID_KEYPAD_USAGE_MAP[0x27], "0")
        self.assertEqual(HID_CONSUMER_USAGE_MAP[0x0223], "Home")
        self.assertEqual(HID_CONSUMER_USAGE_MAP[0x018A], "Email")
        self.assertEqual(HID_CONSUMER_USAGE_MAP[0x0192], "Calc")

        # Verify dispatch of keyboard and consumer keys
        hook._dispatch(HID_KEYPAD_USAGE_MAP[0x59], True, vk=0x59)
        hook._dispatch(HID_KEYPAD_USAGE_MAP[0x59], False, vk=0x59)
        hook._dispatch(HID_CONSUMER_USAGE_MAP[0x0223], True, vk=0x0223)
        hook._dispatch(HID_CONSUMER_USAGE_MAP[0x0223], False, vk=0x0223)
        self.assertEqual(rec.events, [("1", True), ("1", False), ("Home", True), ("Home", False)])

        hook.stop()


class WindowsRawInputOriginTests(unittest.TestCase):
    """Device-origin correlation between WH_KEYBOARD_LL and WM_INPUT (Windows fix).

    The laptop's built-in keyboard (a general typing keyboard) must never have
    its '0'/NumLock/Backspace/Tab keys swallowed; only keys correlated with the
    selected USB keypad are blocked. These tests drive the pure decision logic
    (`_ll_origin_device`, `_ll_decide`, `_process_raw_keyboard`) without any OS.
    """

    LAPTOP_H = 1001
    KEYPAD_H = 2002

    def setUp(self):
        self.hook, self.rec = make()
        self.laptop = KeyboardDevice(
            r"ACPI\PNP0303", "Laptop Keyboard", r"\\?\ACPI#PNP0303#4&1", {self.LAPTOP_H}
        )
        self.keypad = KeyboardDevice(
            r"HID\VID_04D9&PID_1203",
            "M-Tech USB Keypad",
            r"\\?\HID#VID_04D9&PID_1203&MI_00#7&2",
            {self.KEYPAD_H},
        )
        self.hook._devices = [self.laptop, self.keypad]
        self.hook._device_by_handle = {self.LAPTOP_H: self.laptop, self.KEYPAD_H: self.keypad}

    def tearDown(self):
        self.hook.stop()

    def flag_laptop(self):
        """A typed 'A' via WM_INPUT flags the laptop handle as a general keyboard."""
        self.hook._process_raw_keyboard(self.LAPTOP_H, 0x41, 0x1E, 0)  # VK_A, scan 0x1E
        self.assertIn(self.LAPTOP_H, self.hook._general_kb_handles)
        self.assertNotIn(self.KEYPAD_H, self.hook._general_kb_handles)

    def test_letter_flags_device_as_general_keyboard(self):
        self.flag_laptop()

    def test_tracking_updates_for_every_device_even_unmatched(self):
        self.hook.set_target_device(r"HID\VID_04D9&PID_1203")
        # Laptop is not the target: WM_INPUT is rejected for MIDI, but the
        # origin tracking must still record it.
        self.assertFalse(self.hook._process_raw_keyboard(self.LAPTOP_H, 0x60, 0x52, 0))
        entry = self.hook._raw_last_by_key.get((0x60, False))
        self.assertIsNotNone(entry)
        self.assertEqual(entry[0], self.LAPTOP_H)
        self.assertEqual(self.hook._raw_recent_hdevice, self.LAPTOP_H)

    def test_no_selection_laptop_zero_is_strict_noop(self):
        self.flag_laptop()
        now = time.perf_counter()
        self.hook._raw_last_by_key[(0x60, False)] = (self.LAPTOP_H, now)
        # v1.9.8: with no device selected the hook is completely inert - the
        # laptop's embedded numpad '0' generates no MIDI and is not swallowed.
        queue, swallow = self.hook._ll_decide("0", 0x60, 0x52, False, now)
        self.assertEqual((queue, swallow), (False, False))

    def test_no_selection_keypad_zero_is_strict_noop(self):
        now = time.perf_counter()
        self.hook._raw_last_by_key[(0x60, False)] = (self.KEYPAD_H, now)
        # v1.9.8: the former "All Connected Keypads" mode is gone; until the
        # user picks a specific device nothing generates MIDI.
        queue, swallow = self.hook._ll_decide("0", 0x60, 0x52, False, now)
        self.assertEqual((queue, swallow), (False, False))

    def test_no_selection_unknown_origin_is_strict_noop(self):
        self.hook._raw_recent_ts = 0.0
        queue, swallow = self.hook._ll_decide("0", 0x60, 0x52, False, time.perf_counter())
        self.assertEqual((queue, swallow), (False, False))

    def test_specific_target_laptop_zero_is_never_swallowed(self):
        self.flag_laptop()
        self.hook.set_target_device(r"HID\VID_04D9&PID_1203")
        now = time.perf_counter()
        self.hook._raw_last_by_key[(0x60, False)] = (self.LAPTOP_H, now)
        # v2.0 strict hardware device isolation: a keystroke originating from
        # the laptop (or any non-target keyboard) must NEVER generate MIDI and
        # must NEVER be swallowed (CallNextHookEx passes it through untouched).
        queue, swallow = self.hook._ll_decide("0", 0x60, 0x52, False, now)
        self.assertEqual((queue, swallow), (False, False))

    def test_specific_target_keypad_zero_is_swallowed(self):
        self.hook.set_target_device(r"HID\VID_04D9&PID_1203")
        now = time.perf_counter()
        self.hook._raw_last_by_key[(0x60, False)] = (self.KEYPAD_H, now)
        queue, swallow = self.hook._ll_decide("0", 0x60, 0x52, False, now)
        self.assertEqual((queue, swallow), (True, True))

    def test_cold_unknown_origin_numpad_keys_never_swallowed(self):
        self.hook.set_target_device(r"HID\VID_04D9&PID_1203")
        self.hook._raw_recent_ts = 0.0
        # v2.0 strict hardware device isolation: without a proven origin on the
        # selected USB keypad, WH_KEYBOARD_LL neither queues MIDI nor swallows
        # the keystroke (WM_INPUT handles MIDI generation strictly by hDevice).
        queue, swallow = self.hook._ll_decide("0", 0x60, 0x52, False, time.perf_counter())
        self.assertEqual((queue, swallow), (False, False))
        queue, swallow = self.hook._ll_decide("Enter", 0x0D, 0x1C, True, time.perf_counter())
        self.assertEqual((queue, swallow), (False, False))
        queue, swallow = self.hook._ll_decide("+", 0x6B, 0x4E, False, time.perf_counter())
        self.assertEqual((queue, swallow), (False, False))

    def test_cold_typing_keys_still_never_swallowed(self):
        self.hook.set_target_device(r"HID\VID_04D9&PID_1203")
        self.hook._raw_recent_ts = 0.0
        now = time.perf_counter()
        queue, swallow = self.hook._ll_decide("Backspace", 0x08, 0x0E, False, now)
        self.assertEqual((queue, swallow), (False, False))
        self.assertFalse(self.hook._ll_global_swallow(0xBB, False))
        self.assertFalse(self.hook._ll_global_swallow(0x31, False))
        self.assertFalse(self.hook._ll_global_swallow(0x41, False))

    # --- v2.0: strict device-gated aggressive swallow --------------------------

    def test_global_swallow_covers_unambiguous_numpad_vks_for_selected_target(self):
        self.hook.set_target_device(r"HID\VID_04D9&PID_1203")
        self.hook._raw_last_by_key.clear()
        self.hook._raw_recent_ts = 0.0
        for vk in (VK_ADD, VK_SUBTRACT, VK_MULTIPLY, VK_DIVIDE, VK_DECIMAL):
            self.assertTrue(
                self.hook._ll_global_swallow(vk, False), f"VK 0x{vk:02X} must be swallowed"
            )
        self.assertTrue(self.hook._ll_global_swallow(VK_RETURN, True))  # Numpad Enter (E0)
        for vk in range(VK_NUMPAD0, VK_NUMPAD0 + 10):
            self.assertTrue(
                self.hook._ll_global_swallow(vk, False), f"VK 0x{vk:02X} must be swallowed"
            )

    def test_global_swallow_never_touches_main_keyboard_keys(self):
        self.hook.set_target_device(r"HID\VID_04D9&PID_1203")
        # Main Enter of every keyboard: VK_RETURN, NON-extended.
        self.assertFalse(self.hook._ll_global_swallow(VK_RETURN, False))
        # Top-row '+', digits, letters, Tab/Backspace/NumLock: different VKs.
        for vk in (0xBB, 0x31, 0x41, VK_TAB, VK_BACK, VK_NUMLOCK):
            self.assertFalse(
                self.hook._ll_global_swallow(vk, False), f"VK 0x{vk:02X} must pass"
            )

    def test_global_swallow_requires_target_and_exclusive_mode(self):
        self.hook.set_target_device(r"HID\VID_04D9&PID_1203")
        self.hook.set_suppress(False)
        try:
            # Exclusive Mode off: the user asked for keys to pass to the OS.
            self.assertFalse(self.hook._ll_global_swallow(VK_ADD, False))
            self.assertFalse(self.hook._ll_global_swallow(VK_RETURN, True))
        finally:
            self.hook.set_suppress(True)
        # No device selected: completely inert (the laptop's embedded numpad
        # keeps typing normally).
        self.hook.set_target_device("")
        self.assertFalse(self.hook._ll_global_swallow(VK_ADD, False))
        self.assertFalse(self.hook._ll_global_swallow(VK_RETURN, True))

    def test_target_numpad_swallows_all_operators_digits_and_enter_but_foreign_device_passes(self):
        self.hook.set_target_device(r"HID\VID_04D9&PID_1203")
        now = time.perf_counter()
        # Selected USB Numpad origin: aggressively swallows Enter, +, -, *, /, 0-9
        self.hook._raw_recent_hdevice = self.KEYPAD_H
        self.hook._raw_recent_ts = now
        self.assertEqual(
            self.hook._ll_decide("Enter", 0x0D, 0x1C, True, now), (True, True)
        )
        self.assertEqual(self.hook._ll_decide("+", 0x6B, 0x4E, False, now), (True, True))
        self.assertEqual(self.hook._ll_decide("-", 0x6D, 0x4A, False, now), (True, True))
        self.assertEqual(self.hook._ll_decide("*", 0x6A, 0x37, False, now), (True, True))
        self.assertEqual(self.hook._ll_decide("/", 0x6F, 0x35, True, now), (True, True))
        for idx in range(10):
            self.assertEqual(
                self.hook._ll_decide(str(idx), VK_NUMPAD0 + idx, 0, False, now),
                (True, True),
            )
        # Non-extended VK_RETURN (main Enter) is never swallowed even on target:
        self.assertEqual(
            self.hook._ll_decide("Enter", 0x0D, 0x1C, False, now), (False, False)
        )
        # Foreign device (laptop or another USB keyboard): NEVER swallowed, NO MIDI:
        self.hook._raw_recent_hdevice = self.LAPTOP_H
        self.hook._raw_recent_ts = now + 0.1
        self.assertEqual(
            self.hook._ll_decide("Enter", 0x0D, 0x1C, True, now + 0.1), (False, False)
        )
        self.assertEqual(
            self.hook._ll_decide("+", 0x6B, 0x4E, False, now + 0.1), (False, False)
        )

    def test_specific_target_tab_backspace_pass_through(self):
        self.hook.set_target_device(r"HID\VID_04D9&PID_1203")
        now = time.perf_counter()
        self.hook._raw_last_by_key[(0x09, False)] = (self.KEYPAD_H, now)
        queue, swallow = self.hook._ll_decide("Tab", 0x09, 0x0F, False, now)
        self.assertEqual((queue, swallow), (False, False))
        self.hook._raw_last_by_key[(0x08, False)] = (self.KEYPAD_H, now)
        queue, swallow = self.hook._ll_decide("Backspace", 0x08, 0x0E, False, now)
        self.assertEqual((queue, swallow), (False, False))

    def test_numlock_from_laptop_passes_and_keypad_numlock_is_swallowed(self):
        self.flag_laptop()
        # With no target device selected, VK_NUMLOCK is not swallowed:
        now = time.perf_counter()
        self.assertEqual(
            self.hook._ll_decide("NumLock", 0x90, 0x45, False, now), (False, False)
        )
        # Once a Target USB Numpad is active in the UI, VK_NUMLOCK (0x90) is
        # aggressively swallowed in WH_KEYBOARD_LL (even before WM_INPUT updates
        # hDevice) so it can never leak to the OS, while WM_INPUT only queues
        # MIDI Note 51 when hDevice matches the selected Target USB Keypad.
        self.hook.set_target_device(r"HID\VID_04D9&PID_1203")
        self.hook._raw_last_by_key[(0x90, False)] = (self.LAPTOP_H, now)
        queue, swallow = self.hook._ll_decide("NumLock", 0x90, 0x45, False, now)
        self.assertEqual((queue, swallow), (True, True))
        self.assertFalse(self.hook._process_raw_keyboard(self.LAPTOP_H, 0x90, 0x45, 0))
        self.hook._raw_last_by_key[(0x90, False)] = (self.KEYPAD_H, now)
        self.hook._raw_recent_hdevice = self.KEYPAD_H
        self.hook._raw_recent_ts = now
        queue, swallow = self.hook._ll_decide("NumLock", 0x90, 0x45, False, now)
        self.assertEqual((queue, swallow), (True, True))

    def test_specific_target_unknown_origin_numlock_is_swallowed_immediately(self):
        # v2.0.2 race condition fix: WH_KEYBOARD_LL receives VK_NUMLOCK (0x90)
        # before WM_INPUT updates hDevice. When a Target USB Numpad is active in
        # the UI, VK_NUMLOCK is swallowed immediately even with unknown origin.
        self.hook.set_target_device(r"HID\VID_04D9&PID_1203")
        self.hook._raw_recent_ts = 0.0
        queue, swallow = self.hook._ll_decide(
            "NumLock", 0x90, 0x45, False, time.perf_counter()
        )
        self.assertEqual((queue, swallow), (True, True))

    def test_laptop_home_key_is_device_gated_media_home_is_not(self):
        self.flag_laptop()
        self.hook.set_target_device(r"HID\VID_04D9&PID_1203")
        now = time.perf_counter()
        # Dedicated keyboard Home key (VK_HOME 0x24 + E0, scancode 0x47) is Raw
        # Input trackable -> device-gated, so the laptop's Home is completely
        # ignored (no MIDI, no swallow).
        self.hook._raw_last_by_key[(0x24, True)] = (self.LAPTOP_H, now)
        queue, swallow = self.hook._ll_decide("Home", 0x24, 0x47, True, now)
        self.assertEqual((queue, swallow), (False, False))
        # The M-Tech multimedia Home (VK_BROWSER_HOME 0xAC) never emits
        # WM_INPUT: while a specific keypad is selected it keeps the v1.9.1
        # unconditional swallow (the key is untrackable in either direction).
        queue, swallow = self.hook._ll_decide("Home", 0xAC, 0x32, True, now)
        self.assertEqual((queue, swallow), (True, True))

    def test_no_selection_media_home_is_inert(self):
        # v1.9.8: the unconditional multimedia-key swallow only applies while
        # a specific device is selected; with no selection the app is inert.
        self.hook._raw_recent_ts = 0.0
        queue, swallow = self.hook._ll_decide("Home", 0xAC, 0x32, True, time.perf_counter())
        self.assertEqual((queue, swallow), (False, False))

    def test_recent_typing_keyboard_overrides_stale_per_key_memory(self):
        self.flag_laptop()
        self.hook.set_target_device(r"HID\VID_04D9&PID_1203")
        now = time.perf_counter()
        # Per-key memory says keypad (played '0' 2s ago), but the laptop is hot
        # (typing 'a' 0.2s ago): the actively typing device must win so the
        # laptop's '0' is attributed to the LAPTOP - not misattributed to the
        # stale keypad record.
        self.hook._raw_last_by_key[(0x60, False)] = (self.KEYPAD_H, now - 2.0)
        self.hook._raw_last_by_key[(0x41, False)] = (self.LAPTOP_H, now - 0.2)
        self.hook._raw_recent_hdevice = self.LAPTOP_H
        self.hook._raw_recent_ts = now - 0.2
        origin = self.hook._ll_origin_device(0x60, False, now)
        self.assertEqual(origin, self.LAPTOP_H)
        # v2.0 strict hardware device isolation: proven foreign origin under a
        # specific target generates NO MIDI and is NEVER swallowed.
        queue, swallow = self.hook._ll_decide("0", 0x60, 0x52, False, now)
        self.assertEqual((queue, swallow), (False, False))

    def test_recent_activity_fallback_covers_new_keys_in_drum_cadence(self):
        self.hook.set_target_device(r"HID\VID_04D9&PID_1203")
        now = time.perf_counter()
        # '9' has no per-key memory, but the keypad was just played (0.3s ago):
        # the active-device fallback must correlate it with the keypad.
        self.hook._raw_last_by_key[(0x67, False)] = (self.KEYPAD_H, now - 0.3)
        self.hook._raw_recent_hdevice = self.KEYPAD_H
        self.hook._raw_recent_ts = now - 0.3
        origin = self.hook._ll_origin_device(0x69, False, now)
        self.assertEqual(origin, self.KEYPAD_H)
        queue, swallow = self.hook._ll_decide("9", 0x69, 0x49, False, now)
        self.assertEqual((queue, swallow), (True, True))

    def test_stale_memory_and_idle_fallback_returns_unknown(self):
        now = time.perf_counter()
        self.hook._raw_last_by_key[(0x60, False)] = (self.KEYPAD_H, now - 30.0)
        self.hook._raw_recent_hdevice = self.LAPTOP_H
        self.hook._raw_recent_ts = now - 5.0
        self.assertEqual(self.hook._ll_origin_device(0x60, False, now), 0)

    def test_wm_input_no_selection_flagged_laptop_zero_is_ignored(self):
        # v1.9.8 strict isolation: the WM_INPUT channel must not queue MIDI
        # for any device when no specific keypad is selected (v1.9.1 kept
        # v1.6 "All" parity here, which is exactly the laptop-numpad leak).
        self.flag_laptop()
        before = self.hook._win_queue.qsize()
        ok = self.hook._process_raw_keyboard(self.LAPTOP_H, 0x60, 0x52, 0)
        self.assertFalse(ok)
        self.assertEqual(self.hook._win_queue.qsize(), before)
        # And the matching LL decision is a strict no-op as well:
        now = time.perf_counter()
        queue, swallow = self.hook._ll_decide("0", 0x60, 0x52, False, now)
        self.assertEqual((queue, swallow), (False, False))

    def test_wm_input_specific_target_rejects_laptop_and_accepts_keypad(self):
        # Strict hDevice equality in the WM_INPUT channel: only the selected
        # keypad's hardware handle is allowed through.
        self.hook.set_target_device(r"HID\VID_04D9&PID_1203")
        before = self.hook._win_queue.qsize()
        self.assertFalse(self.hook._process_raw_keyboard(self.LAPTOP_H, 0x60, 0x52, 0))
        self.assertEqual(self.hook._win_queue.qsize(), before)
        self.assertTrue(self.hook._process_raw_keyboard(self.KEYPAD_H, 0x60, 0x52, 0))
        self.assertEqual(self.hook._win_queue.qsize(), before + 1)

    def test_matched_handle_cache_rebuilt_on_target_switch(self):
        self.hook.set_target_device(r"HID\VID_04D9&PID_1203")
        self.assertEqual(
            self.hook._matched_handle_cache.get(self.LAPTOP_H), False
        )
        self.assertEqual(
            self.hook._matched_handle_cache.get(self.KEYPAD_H), True
        )
        self.hook.set_target_device("")
        # v1.9.8: with no device selected nothing is matched anymore.
        self.assertEqual(
            self.hook._matched_handle_cache.get(self.LAPTOP_H), False
        )
        self.assertEqual(
            self.hook._matched_handle_cache.get(self.KEYPAD_H), False
        )

    def test_target_numlock_swallowed_for_both_extended_and_non_extended_flags(self):
        """Windows WH_KEYBOARD_LL sets LLKHF_EXTENDED on VK_NUMLOCK (0x90, scan 0x45)
        while WM_INPUT reports Flags=0; target keypad NumLock must be swallowed in both
        cases (even >1s after the last keypad press) while still triggering MIDI via WM_INPUT.
        """
        from key_hook import RI_KEY_BREAK

        self.hook.set_target_device(r"HID\VID_04D9&PID_1203")
        now = time.perf_counter()
        # Simulate prior activity on the target USB keypad 5 seconds ago (> _RAW_RECENT_TTL)
        self.hook._process_raw_keyboard(self.KEYPAD_H, 0x61, 0x4F, 0)
        self.hook._process_raw_keyboard(self.KEYPAD_H, 0x61, 0x4F, RI_KEY_BREAK)
        self.hook._raw_recent_ts = now - 5.0
        while not self.hook._win_queue.empty():
            self.hook._win_queue.get_nowait()

        # WH_KEYBOARD_LL receives VK_NUMLOCK (0x90, scan 0x45) with extended=True (or False):
        for ext in (True, False):
            name = win_key_name(VK_NUMLOCK, 0x45, ext, keypad_verified=True)
            self.assertEqual(name, "NumLock")
            queue, swallow = self.hook._ll_decide(name, VK_NUMLOCK, 0x45, ext, now)
            self.assertEqual(
                (queue, swallow),
                (True, True),
                f"Target keypad VK_NUMLOCK (extended={ext}) must be swallowed (return 1)",
            )

        # WM_INPUT still triggers the MIDI note for NumLock from the target keypad:
        self.assertTrue(self.hook._process_raw_keyboard(self.KEYPAD_H, VK_NUMLOCK, 0x45, 0))
        self.assertTrue(
            self.hook._process_raw_keyboard(self.KEYPAD_H, VK_NUMLOCK, 0x45, RI_KEY_BREAK)
        )
        while not self.hook._win_queue.empty():
            item = self.hook._win_queue.get_nowait()
            if item:
                self.hook._dispatch(*item)
        self.assertEqual(self.rec.events, [("NumLock", True), ("NumLock", False)])

        # While a Target USB Numpad is active in the UI, VK_NUMLOCK (0x90) is
        # aggressively swallowed in WH_KEYBOARD_LL (no race condition), while
        # WM_INPUT rejects laptop NumLock so it never triggers MIDI Note 51:
        self.assertFalse(self.hook._process_raw_keyboard(self.LAPTOP_H, VK_NUMLOCK, 0x45, 0))
        now2 = time.perf_counter()
        for ext in (True, False):
            self.assertEqual(
                self.hook._ll_decide("NumLock", VK_NUMLOCK, 0x45, ext, now2),
                (True, True),
            )

    def test_all_numlock_off_navigational_keys_mapped_and_swallowed_on_target(self):
        """All NumLock-OFF navigational keys (VK_END, VK_DOWN, VK_NEXT, VK_LEFT,
        VK_CLEAR, VK_RIGHT, VK_HOME, VK_UP, VK_PRIOR, VK_DELETE, VK_INSERT) from
        the Target USB Numpad map to their numeric MIDI pads and are swallowed in
        WH_KEYBOARD_LL, while passing through untouched for the laptop keyboard.
        """
        from key_hook import (
            VK_CLEAR,
            VK_DELETE,
            VK_DOWN,
            VK_END,
            VK_HOME,
            VK_INSERT,
            VK_LEFT,
            VK_NEXT,
            VK_PRIOR,
            VK_RIGHT,
            VK_UP,
        )

        self.hook.set_target_device(r"HID\VID_04D9&PID_1203")
        now = time.perf_counter()
        self.hook._raw_recent_hdevice = self.KEYPAD_H
        self.hook._raw_recent_ts = now - 3.0  # >1s idle, target keypad still active device

        nav_cases = [
            (VK_INSERT, 0x52, "0"),
            (VK_DELETE, 0x53, "."),
            (VK_END, 0x4F, "1"),
            (VK_DOWN, 0x50, "2"),
            (VK_NEXT, 0x51, "3"),
            (VK_LEFT, 0x4B, "4"),
            (VK_CLEAR, 0x4C, "5"),
            (VK_RIGHT, 0x4D, "6"),
            (VK_HOME, 0x47, "7"),
            (VK_UP, 0x48, "8"),
            (VK_PRIOR, 0x49, "9"),
        ]
        for vk, scan, expected_pad in nav_cases:
            with self.subTest(vk=hex(vk), scan=hex(scan), expected_pad=expected_pad):
                mapped = win_key_name(vk, scan, False, keypad_verified=True)
                self.assertEqual(mapped, expected_pad)
                self.assertTrue(self.hook._ll_global_swallow(vk, False))
                queue, swallow = self.hook._ll_decide(mapped, vk, scan, False, now)
                self.assertEqual((queue, swallow), (True, True))

        # Foreign device (laptop keyboard) pressing any of these keys is NEVER swallowed:
        self.hook._raw_recent_hdevice = self.LAPTOP_H
        self.hook._raw_recent_ts = now
        for vk, scan, expected_pad in nav_cases:
            queue, swallow = self.hook._ll_decide(expected_pad, vk, scan, False, now)
            self.assertEqual((queue, swallow), (False, False))

    def test_numlock_off_000_burst_swallowed_and_orphaned_zero_recovered(self):
        """3 rapid VK_INSERT / scancode 0x52 strokes in NumLock-OFF state are
        swallowed in WH_KEYBOARD_LL and trigger '000' even if a prior '0' press
        was left stuck in self._down.
        """
        from unittest.mock import patch
        from key_hook import VK_INSERT

        self.hook.set_target_device(r"HID\VID_04D9&PID_1203")
        clock = [1000.0]
        with patch("key_hook.time.perf_counter", side_effect=lambda: clock[0]):
            # Previously played in NumLock-ON (0x60, scan 0x52) on the target keypad:
            self.hook._process_raw_keyboard(self.KEYPAD_H, 0x60, 0x52, 0)
            while not self.hook._win_queue.empty():
                self.hook._win_queue.get_nowait()

            # Simulate an orphaned "0" stuck in self._down from 3 seconds earlier:
            self.hook._down.add("0")
            self.hook._last_zero_ts = clock[0]
            clock[0] += 3.127

            # Now in NumLock-OFF state, 3 rapid VK_INSERT (0x2D, scan 0x52) strokes arrive:
            for _ in range(3):
                self.assertEqual(
                    self.hook._ll_decide("0", VK_INSERT, 0x52, False, clock[0]),
                    (True, True),
                )
                self.hook._dispatch("", True, VK_INSERT, 0x52)
                clock[0] += 0.008
                self.assertEqual(
                    self.hook._ll_decide("0", VK_INSERT, 0x52, False, clock[0]),
                    (True, True),
                )
                self.hook._dispatch("", False, VK_INSERT, 0x52)
                clock[0] += 0.008

        self.assertIn(("000", True), self.rec.events)
        self.assertIn(("000", False), self.rec.events)

    def test_scancode_0x52_debouncer_exemption_consecutive_keydowns(self):
        """Scancode 0x52 (82) is exempted from the standard held-key debouncer
        block: 3 consecutive KeyDown events for Scancode 0x52 within ~100ms
        pass through into the burst detector, trigger '000' (MIDI Note 37),
        and reset the burst counter whether seen as VK_NUMPAD0 or VK_INSERT.
        """
        from unittest.mock import patch
        from key_hook import VK_INSERT

        clock = [2000.0]
        with patch("key_hook.time.perf_counter", side_effect=lambda: clock[0]):
            for vk in (VK_NUMPAD0, VK_INSERT):
                hook, rec = make(burst_window_ms=100)
                # Even if "0" is already in the active debouncer set (_down),
                # 3 consecutive KeyDown strokes of Scancode 0x52 (82) within
                # ~100ms must pass through, fire '000', and reset _zbuf:
                hook._down.add("0")
                for _ in range(3):
                    hook._dispatch("0", True, vk, 0x52)
                    clock[0] += 0.015
                self.assertEqual(hook._zbuf, [])
                self.assertIn(("000", True), rec.events)
                hook.stop()

    def test_global_target_device_active_and_vk_insert_extended_burst_and_single_fallback(self):
        """Verify:
        1. GLOBAL_TARGET_DEVICE_ACTIVE is set True when a target USB keypad is
           selected and False when disconnected/cleared, and governs VK_NUMLOCK
           swallowing even without relying on self._target_device.
        2. VK_INSERT (0x2D) with LLKHF_EXTENDED (extended=True) and extended=False
           is swallowed in WH_KEYBOARD_LL for the target keypad, exempted from
           _down, interchangeable with VK_NUMPAD0 (0x60) in the '000' burst
           buffer (Note 37), and falls back to single '0' (Note 36) after
           timeout.
        """
        import key_hook
        from key_hook import RI_KEY_BREAK, RI_KEY_E0, VK_INSERT, set_global_target_device_active

        self.hook.set_target_device("")
        self.assertFalse(key_hook.GLOBAL_TARGET_DEVICE_ACTIVE)

        self.hook.set_target_device(r"HID\VID_04D9&PID_1203")
        self.assertTrue(key_hook.GLOBAL_TARGET_DEVICE_ACTIVE)

        # Even if self._target_device were cleared/unavailable across threads,
        # GLOBAL_TARGET_DEVICE_ACTIVE = True still guarantees VK_NUMLOCK (0x90)
        # is swallowed:
        saved_target = self.hook._target_device
        try:
            self.hook._target_device = ""
            set_global_target_device_active(True)
            now = time.perf_counter()
            self.assertEqual(
                self.hook._ll_decide("NumLock", VK_NUMLOCK, 0x45, True, now),
                (True, True),
            )
            self.assertEqual(
                self.hook._ll_decide("NumLock", VK_NUMLOCK, 0x45, False, now),
                (True, True),
            )
        finally:
            self.hook._target_device = saved_target
            set_global_target_device_active(bool(saved_target))

        # Both extended=True (LLKHF_EXTENDED) and extended=False VK_INSERT (0x2D)
        # from the target USB keypad are swallowed in WH_KEYBOARD_LL:
        now = time.perf_counter()
        self.hook._process_raw_keyboard(self.KEYPAD_H, VK_INSERT, 0x52, 0)
        self.hook._process_raw_keyboard(self.KEYPAD_H, VK_INSERT, 0x52, RI_KEY_BREAK)
        while not self.hook._win_queue.empty():
            self.hook._win_queue.get_nowait()

        for ext in (True, False):
            self.assertTrue(self.hook._ll_global_swallow(VK_INSERT, ext))
            self.assertEqual(
                self.hook._ll_decide("0", VK_INSERT, 0x52, ext, now),
                (True, True),
            )

        # Mixed burst of VK_INSERT (0x2D, extended=True) and VK_NUMPAD0 (0x60)
        # within ~100ms triggers '000' (Note 37) and clears _zbuf:
        hook_mix, rec_mix = make(burst_window_ms=80)
        hook_mix._down.add("0")  # Exempted even if "0" was in _down
        for vk in (VK_INSERT, VK_NUMPAD0, VK_INSERT):
            hook_mix._dispatch("0", True, vk, 0x52)
            hook_mix._dispatch("0", False, vk, 0x52)
        self.assertEqual(hook_mix._zbuf, [])
        self.assertIn(("000", True), rec_mix.events)
        self.assertIn(("000", False), rec_mix.events)
        hook_mix.stop()

        # Single VK_INSERT (0x2D, with RI_KEY_E0 / extended=True) press falls
        # back to Note 36 ("0") after burst timeout even if "0" was in _down:
        hook_single, rec_single = make(burst_window_ms=30)
        hook_single._devices = self.hook._devices
        hook_single._device_by_handle = self.hook._device_by_handle
        hook_single.set_target_device(r"HID\VID_04D9&PID_1203")
        hook_single._down.add("0")
        self.assertTrue(
            hook_single._process_raw_keyboard(self.KEYPAD_H, VK_INSERT, 0x52, RI_KEY_E0)
        )
        item_down = hook_single._win_queue.get_nowait()
        hook_single._dispatch(*item_down)
        time.sleep(0.12)
        self.assertTrue(
            hook_single._process_raw_keyboard(
                self.KEYPAD_H, VK_INSERT, 0x52, RI_KEY_E0 | RI_KEY_BREAK
            )
        )
        item_up = hook_single._win_queue.get_nowait()
        hook_single._dispatch(*item_up)
        self.assertIn(("0", True), rec_single.events)
        self.assertEqual(rec_single.events[-1], ("0", False))
        hook_single.stop()

    def test_focus_lost_failsafe_releases_stuck_keys_and_preserves_background_hook(self):
        """Verify that losing window focus (Deactivate / Blur / FocusLost)
        triggers the fail-safe Panic, sending NOTE OFF for all held pads,
        clearing both Controller._active and KeyHook._down, and resetting GUI
        pad highlights while keeping the background hook active.
        """
        from PyQt6.QtWidgets import QApplication
        from main import Config, Controller, MainWindow, MidiEngine

        app = QApplication.instance() or QApplication([])
        cfg = Config()
        engine = MidiEngine()
        ctrl = Controller(cfg, engine)
        win = MainWindow(ctrl)
        try:
            ctrl.hook._dispatch("5", True)
            ctrl.hook._dispatch("8", True)
            self.assertIn("5", ctrl.hook._down)
            self.assertIn("8", ctrl.hook._down)
            self.assertIn("5", ctrl._active)
            self.assertIn("8", ctrl._active)
            self.assertGreater(win._pads["5"]._glow, 0.0)
            self.assertGreater(win._pads["8"]._glow, 0.0)

            # Simulate window losing focus (e.g. user clicks into Ableton Live):
            win._on_focus_lost()

            self.assertEqual(len(ctrl.hook._down), 0)
            self.assertEqual(len(ctrl._active), 0)
            # Subsequent presses in the background must respond immediately
            # (never blocked as stuck/auto-repeat):
            ctrl.hook._dispatch("5", True)
            self.assertIn("5", ctrl.hook._down)
            self.assertIn("5", ctrl._active)
            ctrl.hook._dispatch("5", False)
            self.assertNotIn("5", ctrl.hook._down)
            self.assertNotIn("5", ctrl._active)
        finally:
            ctrl.shutdown()


if __name__ == "__main__":
    unittest.main()
