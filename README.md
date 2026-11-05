# NumpadToMIDI2

Turn a 23-key M-Tech USB numpad into a MIDI controller (Windows 11 & macOS). Every key is a
pad in a chromatic Drum Rack layout: the bottom-left `0` key is **C1 (MIDI note 36)** and the
notes climb left→right, bottom→top (23 keys → notes 36–58).

## Features
- **Unified HID engine**: all MIDI generation runs on a dedicated background `hidapi` worker
  that reads the raw 8-byte USB reports from BOTH composite interfaces of the keypad — the
  **Keyboard interface (Usage Page `0x01`, usages 6/7)** for digits/operators and the
  **Consumer interface (Usage Page `0x0C`)** for `Home`/`Email`/`Calc` — bypassing the OS
  keyboard stack entirely.
- Exactly **one Note On per press** and **one Note Off per release** (OS auto-repeat is ignored).
- The **`000`** key (which hardware sends as 3 rapid presses of the `'0'` key **HID Usage ID
  `0x62`** within an 80 ms window) is detected as a single key (MIDI Note 37 / `C#1`,
  velocity 127). Because the engine reads raw HID usages directly, this burst is **completely
  NumLock-independent** and still works if the keypad hardware toggles the physical `0` key to
  the PC navigation cluster (`0x49` Insert) while a keypad is targeted.
- Physical **M-Tech 23-key layout**: `Home` (`VK_BROWSER_HOME` / `0xAC`), `Tab`, `Email` (`0xB4`),
  `Calc` (`0xB7`), `NumLock`, `/`, `*`, `Backspace`, `0-9`, `.`, `000`, `+`, `-`, and a
  2-row tall `Enter` key.
- **Target USB Keypad selector (Win32 Raw Input API)**: choose `"All Connected Keypads (Default)"` or lock NumpadToMIDI2 to a specific external USB keypad (`HID\VID_xxxx&PID_xxxx`) so your primary laptop/desktop keyboard is unaffected.
- **Exclusive Mode (Block OS Keystrokes)** enabled by default: the OS-level hooks are reduced
  to *dumb swallow gatekeepers* — on Windows a minimalist `WH_KEYBOARD_LL` that only returns
  `1` (`LRESULT`) when the stroke belongs to the selected keypad (never launching Chrome,
  Mail, Calculator or typing digits/`Insert` into text fields), and no MIDI logic at all.
  Win32 Raw Input (`WM_INPUT`) is used solely to correlate which physical device typed.
- **macOS dual-interface HID capture**: numeric keys live on the GenericDesktop
  Keyboard/Keypad interface (usage page `0x01`, usages `6`/`7`) while `Home`/`Email`/`Calc`
  live on the Consumer Control interface (page `0x0C`). Native `IOHIDManager` binds to BOTH
  interfaces simultaneously via `IOHIDManagerSetDeviceMatchingMultiple`, maps keypad usages
  `0x59`–`0x62` (keypad 1..0) to their MIDI notes, and an `CGEventTap` swallows the keypad
  codes so they never type numbers into the macOS UI.
- **Crash-safe macOS `CGEventTap`**: Accessibility is verified via
  `AXIsProcessTrustedWithOptions` *before* the tap is created. Without the permission (or when
  `CGEventTapCreate` returns `NULL`) the tap is never attached to the run loop; instead a
  native `QMessageBox.warning` tells the user to enable *NumpadToMIDI2* under
  *System Settings → Privacy & Security → Accessibility*, and the app keeps running safely —
  raw HID capture and MIDI keep working without it.
- **System Tray background mode**: clicking the window close button (`X`) minimizes to the
  system tray with a notification (`Open NumpadToMIDI2`, `Mute MIDI`, `Quit NumpadToMIDI2`;
  double-click the tray icon to restore).
- Dark PyQt6 UI with custom 3x3 pad-grid icon (`assets/icon.ico`, `assets/icon.icns`, `assets/icon.png`), lighting pad grid (click pads with the mouse too), live MIDI monitor,
  octave shift (−3…+5), MIDI channel 1–16, velocity, panic button.
- macOS: native **virtual MIDI source** called `NumpadToMIDI2` with native builds for both Intel (`x86_64`) and Apple Silicon (`arm64`).
- Windows: creates a virtual MIDI port called **`NumpadToMIDI2`** directly via the
  [teVirtualMIDI](https://www.tobias-erichsen.de/software/virtualmidi.html) driver
  (automatically installed by `NumpadToMIDI2-Setup-v1.7.0.exe`). If the driver is not
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
 Row 6 (Bottom): [ 0 (36) ]       [ 000 (37) ]  [ . (38) ]               [ (Enter)    ]
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
Output: `dist/NumpadToMIDI2-Windows.exe` (Windows) or `dist/NumpadToMIDI2.app` (macOS).

To build the platform installers:
- **Windows (Inno Setup 6.3+):**
  ```powershell
  ISCC.exe /DAppVersion=1.7.0 installer\windows_installer.iss
  ```
  Produces `dist/NumpadToMIDI2-Setup-v1.7.0.exe` (bundles the app, creates Desktop and
  Start Menu shortcuts, elevates to Administrator, and silently downloads + installs
  the `teVirtualMIDI` virtual MIDI driver if missing).
- **macOS (DMG with `/Applications` link and setup guide):**
  ```bash
  bash installer/make_dmg.sh 1.7.0 Intel         # Intel x86_64 Macs
  bash installer/make_dmg.sh 1.7.0 AppleSilicon   # Apple Silicon (M1/M2/M3/M4) Macs
  ```
  Produces `dist/NumpadToMIDI2-macOS-Intel-v1.7.0.dmg` and `dist/NumpadToMIDI2-macOS-AppleSilicon-v1.7.0.dmg` (and `dist/NumpadToMIDI2-macOS-v1.7.0.dmg`).

## Releases / CI
`.github/workflows/build-release.yml` builds both platforms on every `v*` tag (and manual
`workflow_dispatch`) and attaches `NumpadToMIDI2-Setup-v1.7.0.exe`,
`NumpadToMIDI2-macOS-Intel-v1.7.0.dmg`, and `NumpadToMIDI2-macOS-AppleSilicon-v1.7.0.dmg`
to the GitHub Release.

## Notes & limitations
- **Windows:** MIDI is generated by the `hidapi` engine from raw USB reports, so the Windows
  NumLock state is irrelevant. In *"All Connected Keypads"* mode only the dedicated keypad
  usages (`0x53`–`0x63`, plus `Backspace`/`Tab` like the legacy engine) are honoured, so
  typing on a regular keyboard never produces MIDI; when a specific keypad is targeted the
  full usage map applies (its NumLock-OFF navigation codes work too). Only the *extended*
  (numpad) Enter is recognised.
- **macOS:** raw keypad capture via `IOHIDManager` needs no macOS permission. Grant the app
  **Accessibility** in *System Settings → Privacy & Security* only for Exclusive Mode
  (blocking keys requires a `CGEventTap`; capture itself keeps working without it, and the
  app no longer crashes when the permission is missing). The CI
  build is ad-hoc signed only, so on first run right-click → *Open* (or
  `xattr -dr com.apple.quarantine NumpadToMIDI2.app`).
  `Calc`/`Email` keys are not delivered to apps by macOS; those pads work via mouse only.
- *Exclusive Mode (Block OS Keystrokes)* prevents the numpad from typing into the focused app or
  launching browser/mail/calculator shortcuts.
- Settings are stored in `%APPDATA%\NumpadToMIDI2\config.json` (Windows) or
  `~/Library/Application Support/NumpadToMIDI2/config.json` (macOS); `config.json` in this repo holds the defaults.
- Diagnostic logs are written to `%TEMP%\NumpadToMIDI2.log` and `%APPDATA%\NumpadToMIDI2\debug.log`.
