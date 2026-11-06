# NumpadToMIDI

Turn a 23-key M-Tech USB numpad into a MIDI controller (Windows 11 & macOS). Every key is a
pad in a chromatic Drum Rack layout: the bottom-left `0` key is **C1 (MIDI note 36)** and the
notes climb left→right, bottom→top (23 keys → notes 36–58).

> **Created by Ryan Dharmawan | IG: @ryan.dharmawan**

## Features
- **Windows `WH_KEYBOARD_LL` hook engine (scancode-based)**: a dedicated high-priority hook
  thread receives every keystroke and resolves it **by hardware OEM scancode** — the physical
  `'0'` key always reports **scancode `0x52`** (NumLock ON → `VK_NUMPAD0`, NumLock OFF →
  `VK_INSERT`, same physical key). The hook only *decides* and *queues*: routed strokes are
  pushed to an async worker (`_queue_from_ll`) that runs all MIDI/debounce logic, and the
  callback returns `LRESULT(1)` in microseconds, so the system never trips
  `LowLevelHooksTimeout`. `hidapi` is **not** used on Windows (the kernel blocks opening
  standard keyboard HID endpoints from user space).
- Exactly **one Note On per press** and **one Note Off per release** (OS auto-repeat is ignored).
- The **`000`** key (which hardware sends as 3 rapid presses of the `'0'` key **scancode
  `0x52`** within an 80 ms window) is detected as a single key (MIDI Note 37 / `C#1`,
  velocity 127). The triplet detector is **completely NumLock-independent** — both the
  NumLock-ON and NumLock-OFF variants of the stroke feed the same detector.
- Physical **M-Tech 23-key layout**: `Home` (`VK_BROWSER_HOME` / `0xAC`), `Tab`, `Email` (`0xB4`),
  `Calc` (`0xB7`), `NumLock`, `/`, `*`, `Backspace`, `0-9`, `.`, `000`, `+`, `-`, and a
  2-row tall `Enter` key.
- **Target USB Keypad selector (Win32 Raw Input API)**: choose `"All Connected Keypads (Default)"` or lock NumpadToMIDI to a specific external USB keypad (`HID\VID_xxxx&PID_xxxx`) so your primary laptop/desktop keyboard is unaffected.
- **Exclusive Mode (Block OS Keystrokes)** enabled by default: routed keypad strokes are
  swallowed by the `WH_KEYBOARD_LL` hook (`return 1`, no `CallNextHookEx`) so they never type
  digits/`Insert` into text fields or launch Chrome, Mail, Calculator. MIDI generation keeps
  working even with Exclusive Mode off, and Win32 Raw Input (`WM_INPUT`) is used solely to
  correlate which physical device typed (never for MIDI).
- **macOS dual-interface HID capture**: numeric keys live on the GenericDesktop
  Keyboard/Keypad interface (usage page `0x01`, usages `6`/`7`) while `Home`/`Email`/`Calc`
  live on the Consumer Control interface (page `0x0C`). Native `IOHIDManager` binds to BOTH
  interfaces simultaneously via `IOHIDManagerSetDeviceMatchingMultiple`, maps keypad usages
  `0x59`–`0x62` (keypad 1..0) to their MIDI notes, and an `CGEventTap` swallows the keypad
  codes so they never type numbers into the macOS UI.
- **Crash-safe, once-only macOS Accessibility flow**: `AXIsProcessTrusted` is called **exactly
  once per process** at startup (plain, non-prompting, non-blocking). Without the permission
  the `CGEventTap` is never created/attached (no crash loop), a native `QMessageBox.warning`
  appears **at most once**, and the app keeps running — the IOHIDManager capture and MIDI keep
  working without keystroke swallowing.
- **System Tray background mode**: clicking the window close button (`X`) minimizes to the
  system tray with a notification (`Open NumpadToMIDI`, `Mute MIDI`, `Quit NumpadToMIDI`;
  double-click the tray icon to restore).
- Dark PyQt6 UI with custom 3x3 pad-grid icon (`assets/icon.ico`, `assets/icon.icns`, `assets/icon.png`), lighting pad grid (click pads with the mouse too), live MIDI monitor,
  octave shift (−3…+5), MIDI channel 1–16, velocity, panic button.
- macOS: native **virtual MIDI source** called `NumpadToMIDI` with native builds for both Intel (`x86_64`) and Apple Silicon (`arm64`).
- Windows: creates a virtual MIDI port called **`NumpadToMIDI`** directly via the
  [teVirtualMIDI](https://www.tobias-erichsen.de/software/virtualmidi.html) driver
  (automatically installed by `NumpadToMIDI-Setup-v1.8.0.exe`). If the driver is not
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
Output: `dist/NumpadToMIDI-Windows.exe` (Windows) or `dist/NumpadToMIDI.app` (macOS).

To build the platform installers:
- **Windows (Inno Setup 6.3+):**
  ```powershell
  ISCC.exe /DAppVersion=1.8.0 installer\windows_installer.iss
  ```
  Produces `dist/NumpadToMIDI-Setup-v1.8.0.exe` (bundles the app, creates Desktop and
  Start Menu shortcuts, elevates to Administrator, and silently downloads + installs
  the `teVirtualMIDI` virtual MIDI driver if missing).
- **macOS (DMG with `/Applications` link and setup guide):**
  ```bash
  bash installer/make_dmg.sh 1.8.0 Intel         # Intel x86_64 Macs
  bash installer/make_dmg.sh 1.8.0 AppleSilicon   # Apple Silicon (M1/M2/M3/M4) Macs
  ```
  Produces `dist/NumpadToMIDI-macOS-Intel-v1.8.0.dmg` and `dist/NumpadToMIDI-macOS-AppleSilicon-v1.8.0.dmg` (and `dist/NumpadToMIDI-macOS-v1.8.0.dmg`).

## Releases / CI
`.github/workflows/build-release.yml` builds both platforms on every `v*` tag (and manual
`workflow_dispatch`) and attaches `NumpadToMIDI-Setup-v1.8.0.exe`,
`NumpadToMIDI-macOS-Intel-v1.8.0.dmg`, and `NumpadToMIDI-macOS-AppleSilicon-v1.8.0.dmg`
to the GitHub Release.

## Notes & limitations
- **Windows:** MIDI is generated inside the `WH_KEYBOARD_LL` pipeline (identified by hardware
  scancode), so the Windows NumLock state is irrelevant — the physical keypad digits always
  map the same way. In *"All Connected Keypads"* mode all strokes that `win_key_name` maps to
  a keypad key route to MIDI while Exclusive Mode decides OS swallowing; when a specific
  keypad is targeted, strokes are routed as soon as Raw Input correlation matches the keypad
  (or the stroke is keypad `0` / scancode `0x52`, so a `000` burst never leaks mid-correlation).
  Only the *extended* (numpad) Enter is recognised.
- **macOS:** raw keypad capture via `IOHIDManager` needs no macOS permission. Grant the app
  **Accessibility** in *System Settings → Privacy & Security* only for Exclusive Mode
  (blocking keys requires a `CGEventTap`; capture itself keeps working without it, the check
  is non-prompting and runs exactly once, and the app never crashes or blocks when the
  permission is missing).
  The CI build is ad-hoc signed only, so on first run right-click → *Open* (or
  `xattr -dr com.apple.quarantine NumpadToMIDI.app`).
  `Calc`/`Email` keys are not delivered to apps by macOS; those pads work via mouse only.
- *Exclusive Mode (Block OS Keystrokes)* prevents the numpad from typing into the focused app or
  launching browser/mail/calculator shortcuts.
- Settings are stored in `%APPDATA%\NumpadToMIDI\config.json` (Windows) or
  `~/Library/Application Support/NumpadToMIDI/config.json` (macOS); `config.json` in this repo holds the defaults.
- Diagnostic logs are written to `%TEMP%\NumpadToMIDI.log` and `%APPDATA%\NumpadToMIDI\debug.log`.

## Credits
- **Created by Ryan Dharmawan** — Instagram [@ryan.dharmawan](https://instagram.com/ryan.dharmawan)
- Virtual MIDI on Windows uses [teVirtualMIDI](https://www.tobias-erichsen.de/software/virtualmidi.html)
  / loopMIDI by Tobias Erichsen (driver downloaded and installed separately, never bundled).
