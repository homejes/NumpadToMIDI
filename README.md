# NumpadToMIDI

Turn a 23-key M-Tech USB numpad into a MIDI controller (Windows 11 & macOS). Every key is a
pad in a chromatic Drum Rack layout: the bottom-left `0` key is **C1 (MIDI note 36)** and the
notes climb left→right, bottom→top (23 keys → notes 36–58).

## Features
- Exactly **one Note On per press** and **one Note Off per release** (OS auto-repeat is ignored).
- The **`000`** key (which hardware sends as a rapid 3-stroke burst of the physical `0` key) is detected as a single key (MIDI Note 37 / `C#1`, velocity 127) **regardless of whether NumLock is ON or OFF** (`VK_NUMPAD0` `0x60` and `VK_INSERT` `0x2D` / scancode `0x52`).
- Physical **M-Tech 23-key layout**: `Home` (`VK_BROWSER_HOME` / `0xAC`), `Tab`, `Email` (`0xB4`),
  `Calc` (`0xB7`), `NumLock`, `/`, `*`, `Backspace`, `0-9`, `.`, `000`, `+`, `-`, and a
  2-row tall `Enter` key.
- **Target USB Keypad selector (Win32 Raw Input API)**: choose `"All Connected Keypads (Default)"` or lock NumpadToMIDI to a specific external USB keypad (`HID\VID_xxxx&PID_xxxx`). On Windows, **`WM_INPUT` owns 100% of the MIDI generation**: every Raw Input keyboard event is matched by its exact `hDevice`/device path against the UI selection, so only the selected keypad produces notes — the laptop's built-in keyboard (including its embedded numpad `0`/`Insert` key and its real arrow/Home/End keys) can never trigger MIDI, and unmatched events are dropped with a debug log entry. `WH_KEYBOARD_LL` is **swallow-only**: it blocks keystrokes strictly correlated (via the Raw Input origin tracking) with the selected keypad and falls through to `CallNextHookEx` for everything else — including unattributed events (e.g. if the Raw Input sink has not reported yet), which are never blocked in either mode. When the keypad's NumLock is OFF (or desynced) its digits arrive as navigational Virtual Keys (`VK_DOWN` for `2`, `VK_HOME` for `7`, …); those variants are swallowed for the keypad and mapped back to the correct MIDI note by the constant physical scancode.
- **Anti-stuck pad state tracking (v1.9.5)**: a pad-state tracker debounces the OS auto-repeat stream — while a pad is held, only the first key-down produces a Note ON (no repeat spam into the DAW), and key-up events (`RI_KEY_BREAK` in `WM_INPUT`, `WM_KEYUP`/`WM_SYSKEYUP` in `WH_KEYBOARD_LL`) are always processed so every Note ON is guaranteed exactly one Note OFF. Key-UP events of trackable keys are **never swallowed** by the low-level hook — a swallowed break is the one edge whose loss hangs a note and leaves a pad lit (and a leaked break is inert to the OS: DAW shortcuts trigger on key-DOWN only). Stray releases (whose key-down never registered) still dispatch their Note OFF and clear the UI pad highlight, so no pad can remain stuck lit.
- **Exclusive Mode (Block OS Keystrokes)** enabled by default: intercepts and swallows keypad
  and multimedia keys (`VK_BROWSER_HOME`, `VK_LAUNCH_MAIL`, `VK_LAUNCH_APP2`, `VK_BACK`, digits)
  in `WH_KEYBOARD_LL` (`return 1`) so Chrome, Mail, or Calculator never launch and DAW text
  inputs are never polluted. `VK_RETURN` is shared between the main Enter and Numpad Enter:
  only the extended-flag (`LLKHF_EXTENDED`, E0-prefixed) variant is treated as a numpad key —
  the main Enter of any keyboard is never swallowed (plain-Return keypads still get their
  MIDI through the `WM_INPUT` channel), and key-UP events are never swallowed at all so no
  Note OFF can be lost (v1.9.5).
- **macOS dual-interface HID capture**: numeric keys live on the GenericDesktop
  Keyboard/Keypad interface (usage page `0x01`, usages `6`/`7`) while `Home`/`Email`/`Calc`
  live on the Consumer Control interface (page `0x0C`). Native `IOHIDManager` binds to BOTH
  interfaces simultaneously via `IOHIDManagerSetDeviceMatchingMultiple`, maps keypad usages
  `0x59`–`0x62` (keypad 1..0) to their MIDI notes, and an `CGEventTap` swallows the keypad
  codes so they never type numbers into the macOS UI.
- **System Tray background mode**: clicking the window close button (`X`) minimizes to the
  system tray with a notification (`Open NumpadToMIDI`, `Mute MIDI`, `Quit NumpadToMIDI`;
  double-click the tray icon to restore).
- Dark PyQt6 UI with custom 3x3 pad-grid icon (`assets/icon.ico`, `assets/icon.icns`, `assets/icon.png`), lighting pad grid (click pads with the mouse too), live MIDI monitor,
  octave shift (−3…+5), MIDI channel 1–16, velocity, panic button.
- macOS: native **virtual MIDI source** called `NumpadToMIDI` with native builds for both Intel (`x86_64`) and Apple Silicon (`arm64`).
- Windows: creates a virtual MIDI port called **`NumpadToMIDI`** directly via the
  [teVirtualMIDI](https://www.tobias-erichsen.de/software/virtualmidi.html) driver
  (automatically installed by `NumpadToMIDI-Setup-v1.9.5.exe`). If the driver is not
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
  ISCC.exe /DAppVersion=1.9.5 installer\windows_installer.iss
  ```
  Produces `dist/NumpadToMIDI-Setup-v1.9.5.exe` (bundles the app, creates Desktop and
  Start Menu shortcuts, elevates to Administrator, and silently downloads + installs
  the `teVirtualMIDI` virtual MIDI driver if missing).
- **macOS (DMG with `/Applications` link and setup guide):**
  ```bash
  bash installer/make_dmg.sh 1.9.5 Intel         # Intel x86_64 Macs
  bash installer/make_dmg.sh 1.9.5 AppleSilicon   # Apple Silicon (M1/M2/M3/M4) Macs
  ```
  Produces `dist/NumpadToMIDI-macOS-Intel-v1.9.5.dmg` and `dist/NumpadToMIDI-macOS-AppleSilicon-v1.9.5.dmg` (and `dist/NumpadToMIDI-macOS-v1.9.5.dmg`).

## Releases / CI
`.github/workflows/build-release.yml` builds both platforms on every `v*` tag (and manual
`workflow_dispatch`) and attaches `NumpadToMIDI-Setup-v1.9.5.exe`,
`NumpadToMIDI-macOS-Intel-v1.9.5.dmg`, and `NumpadToMIDI-macOS-AppleSilicon-v1.9.5.dmg`
to the GitHub Release.

## Notes & limitations
- **Windows:** works with NumLock on or off. Only the *extended* (numpad) Enter is recognised.
  `Tab`/`Backspace` cannot be told apart from the main keyboard's keys.
- **macOS:** raw keypad capture via `IOHIDManager` needs no macOS permission. Grant the app
  **Accessibility** in *System Settings → Privacy & Security* only for Exclusive Mode
  (blocking keys requires a `CGEventTap`; capture itself keeps working without it). The CI
  build is ad-hoc signed only, so on first run right-click → *Open* (or
  `xattr -dr com.apple.quarantine NumpadToMIDI.app`).
  `Calc`/`Email` keys are not delivered to apps by macOS; those pads work via mouse only.
- *Exclusive Mode (Block OS Keystrokes)* prevents the numpad from typing into the focused app or
  launching browser/mail/calculator shortcuts. Blocking is device-aware (Windows): keys from
  your laptop's built-in keyboard — including its `0` key — always reach the OS normally.
  NumLock-OFF navigational variants emitted by the keypad (arrow/Home/End keys) are blocked as
  well, so a `2` never lands in your DAW as a Down arrow. The keypad's Enter/Backspace/Tab and
  math operators (`+ - * /`) are blocked too — numpad Enter never triggers Play in your DAW,
  while your laptop's own Enter/Backspace/Tab keys keep working normally.
- Settings are stored in `%APPDATA%\NumpadToMIDI\config.json` (Windows) or
  `~/Library/Application Support/NumpadToMIDI/config.json` (macOS); `config.json` in this repo holds the defaults.
- Diagnostic logs are written to `%TEMP%\NumpadToMIDI.log` and `%APPDATA%\NumpadToMIDI\debug.log`.
