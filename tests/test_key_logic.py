import unittest

from key_hook import KEY_INDEX, KEY_LAYOUT, KeyHook, win_key_name


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

    def test_required_keys_present(self):
        for name in ["0", "1", "2", "3", "4", "5", "6", "7", "8", "9", ".", "000", "Enter",
                     "+", "-", "*", "/", "Tab", "Backspace", "Calc", "Email"]:
            self.assertIn(name, KEY_INDEX)

    def test_windows_mapping(self):
        self.assertEqual(win_key_name(96, 0, False), "0")
        self.assertEqual(win_key_name(105, 0, False), "9")
        self.assertEqual(win_key_name(13, 0, True), "Enter")
        self.assertIsNone(win_key_name(13, 0, False))  # main Enter is ignored
        self.assertEqual(win_key_name(183, 0, False), "Calc")
        self.assertEqual(win_key_name(180, 0, False), "Email")
        self.assertIsNone(win_key_name(65, 0, False))


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


if __name__ == "__main__":
    unittest.main()
