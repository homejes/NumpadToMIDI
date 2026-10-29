# NumpadToMIDI

Turn a 23-key M-Tech USB numpad into a MIDI controller (Windows 11 & macOS). Every key is a
pad in a chromatic Drum Rack layout: the bottom-left `0` key is **C1 (MIDI note 36)** and the
notes climb left→right, bottom→top (23 keys → notes 36–58).

## Features
- Exactly **one Note On per press** and **one Note Off per release** (OS auto-repeat is ignored).
- The **`000`** key (which hardware sends as a rapid 3-stroke burst of the physical `0` key) is detected as a single key (MIDI Note 37 / `C#1`) **regardless of whether NumLock is ON or OFF** (`VK_NUMPAD0` `0x60` and `VK_INSERT` `0x2D` / scancode `0x52`).
- **Exclusive Pad Mode (Auto-Choke / LivePadRig) `[Shortcut: .]`**: optional monophonic radio-style pad latch mode where pressing a new pad automatically sends MIDI Note Off for the previously active note before triggering the new Note On (and pressing the same pad again toggles it off). Pressing the physical `.` (`Del`) key on the USB Numpad—or clicking the `[ Mode: Choke (.) ]` pad in the UI—toggles Exclusive Pad Mode on/off in real time without sending a MIDI note.
- Physical **M-Tech 23-key layout**: `Home` (`VK_BROWSER_HOME` / `0xAC`), `Tab`, `Email` (`0xB4`),
  `Calc` (`0xB7`), `NumLock`, `/`, `*`, `Backspace`, `0-9`, `[ Mode: Choke (.) ]`, `000`, `+`, `-`, and a
  2-row tall `Enter` key.
- **Target USB Keypad selector (Win32 Raw Input API)**: choose `"All Connected Keypads (Default)"` or lock NumpadToMIDI to a specific external USB keypad (`HID\VID_xxxx&PID_xxxx`) so your primary laptop/desktop keyboard is unaffected.
- **Exclusive Mode (Block OS Keystrokes)** enabled by default: intercepts and swallows keypad
  and multimedia keys (`VK_BROWSER_HOME`, `VK_LAUNCH_MAIL`, `VK_LAUNCH_APP2`, `VK_BACK`, digits)
  in `WH_KEYBOARD_LL` (`return 1`) so Chrome, Mail, or Calculator never launch and DAW text
  inputs are never polluted.
- **System Tray background mode**: clicking the window close button (`X`) minimizes to the
  system tray with a notification (`Open NumpadToMIDI`, `Mute MIDI`, `Quit NumpadToMIDI`;
  double-click the tray icon to restore).
- Dark PyQt6 UI with custom 3x3 pad-grid icon (`assets/icon.ico`, `assets/icon.icns`, `assets/icon.png`), lighting pad grid (click pads with the mouse too), live MIDI monitor,
  octave shift (−3…+5), MIDI channel 1–16, velocity, panic button.
- macOS: native **virtual MIDI source** called `NumpadToMIDI`.
- Windows: creates a virtual MIDI port called **`NumpadToMIDI`** directly via the
  [teVirtualMIDI](https://www.tobias-erichsen.de/software/virtualmidi.html) driver
  (automatically installed by `NumpadToMIDI-Setup-v1.4.2.exe`). If the driver is not
  present, falls back to any existing loopback port (e.g.
  [loopMIDI](https://www.tobias-erichsen.de/software/loopmidi.html)), then to the first
  available output, or runs silently if none exists.

## Pad layout (M-Tech 23-key chromatic note order from C1 = 36)
```
 Row 1 (Top):    [ Home (55) ]    [ Tab (56) ]  [ Email (57) ]           [ Calc (58) ]
 Row 2:          [ NumLock (51) ] [ / (52) ]    [ * (53) ]               [ Backspace (54) ]
 Row 3:          [ 7 (47) ]       [ 8 (48) ]    [ 9 (49) ]               [ - (50) ]
 Row 4:          [ 4 (43) ]       [ 5 (44) ]    [ 6 (45) ]               [ + (46) ]
 Row 5:          [ 1 (39) ]       [ 2 (40) ]    [ 3 (41) ]               [ Enter (42) ]
 Row 6 (Bottom): [ 0 (36) ]       [ 000 (37) ]  [ Mode: Choke (.) ]      [ (Enter)    ]
```

## Run from source
```bash
python -m venv .venv && . .venv/Scripts/activate   # macOS: source .venv/bin/activate
pip install -r requirements.txt
python main.py
```
Run the unit tests: `python -m unittest discover -s tests -v`

## Build locally
```bash
pyinstaller build.spec --noconfirm
```
Output: `dist/NumpadToMIDI-Windows.exe` (Windows) or `dist/NumpadToMIDI.app` (macOS).

To build the platform installers:
- **Windows (Inno Setup 6.3+):**
  ```powershell
  ISCC.exe /DAppVersion=1.4.2 installer\windows_installer.iss
  ```
  Produces `dist/NumpadToMIDI-Setup-v1.4.2.exe` (bundles the app, creates Desktop and
  Start Menu shortcuts, elevates to Administrator, and silently downloads + installs
  the `teVirtualMIDI` virtual MIDI driver if missing).
- **macOS (DMG with `/Applications` link and setup guide):**
  ```bash
  bash installer/make_dmg.sh 1.4.2
  ```
  Produces `dist/NumpadToMIDI-macOS-v1.4.2.dmg`.

## Releases / CI
`.github/workflows/build-release.yml` builds both platforms on every `v*` tag (and manual
`workflow_dispatch`) and attaches `NumpadToMIDI-Setup-v1.4.2.exe` and
`NumpadToMIDI-macOS-v1.4.2.dmg` to the GitHub Release.

## Notes & limitations
- **Windows:** works with NumLock on or off. Only the *extended* (numpad) Enter is recognised.
  `Tab`/`Backspace` cannot be told apart from the main keyboard's keys.
- **macOS:** grant the app **Input Monitoring** (and, for blocking keys, **Accessibility**) in
  *System Settings → Privacy & Security*. The CI build is ad-hoc signed only, so on first run
  right-click → *Open* (or `xattr -dr com.apple.quarantine NumpadToMIDI.app`).
  `Calc`/`Email` keys are not delivered to apps by macOS; those pads work via mouse only.
- *Exclusive Mode (Block OS Keystrokes)* prevents the numpad from typing into the focused app or
  launching browser/mail/calculator shortcuts.
- Settings are stored in `%APPDATA%\NumpadToMIDI\config.json` (Windows) or
  `~/Library/Application Support/NumpadToMIDI/config.json` (macOS); `config.json` in this repo holds the defaults.
- Diagnostic logs are written to `%TEMP%\NumpadToMIDI.log` and `%APPDATA%\NumpadToMIDI\debug.log`.
