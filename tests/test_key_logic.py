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
        self.assertEqual(win_key_name(96, 0, False), "0")
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
        time.sleep(0.08)
        hook._dispatch("0", False)
        self.assertEqual(rec.events, [("0", True), ("0", False)])

    def test_zero_autorepeat_while_held(self):
        hook, rec = make()
        import time
        hook._dispatch("0", True)
        time.sleep(0.08)
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


if __name__ == "__main__":
    unittest.main()
