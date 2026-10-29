# NumpadToMIDI

Turn a 23-key USB numpad into a MIDI controller (Windows 11 & macOS). Every key is a
pad in a Drum Rack layout: the bottom-left `0` key is **C1 (MIDI note 36)** and the
notes climb left→right, bottom→top (23 keys → notes 36–58).

## Features
- Exactly **one Note On per press** and **one Note Off per release** (OS auto-repeat is ignored).
- The **`000`** key (which hardware sends as three fast `0` presses) is detected as a single key.
- Supported keys: `0-9`, `.`, `000`, `Enter`, `+`, `-`, `*`, `/`, `Tab`, `Backspace`, `Calc`, `Email`
  (plus `NumLock` and `=` to fill the 23 pads).
- Dark PyQt6 UI: lighting pad grid (click pads with the mouse too), live MIDI monitor,
  octave shift (−3…+5), MIDI channel 1–16, velocity, panic button.
- macOS: native **virtual MIDI source** called `NumpadToMIDI`.
- Windows: WinMM cannot create virtual ports → connects to a **loopback** port
  (e.g. [loopMIDI](https://www.tobias-erichsen.de/software/loopmidi.html)); falls back to the first
  available output, or runs silently if none exists.

## Pad layout (note order from C1)
```
 Calc(55) Email(56) NumLock(57) Backspace(58)
 Tab(51)  /(52)   *(53)     -(54)
 7(47)    8(48)   9(49)     +(50)
 4(43)    5(44)   6(45)     =(46)
 1(39)    2(40)    3(41)    Enter(42)
 0(36)    000(37)  .(38)
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

## Releases / CI
`.github/workflows/build-release.yml` builds both platforms on every `v*` tag (and manual
`workflow_dispatch`) and attaches `NumpadToMIDI-Windows.exe` and `NumpadToMIDI-macOS.zip`
to the GitHub Release.

## Notes & limitations
- **Windows:** keep **NumLock on**. Only the *extended* (numpad) Enter is recognised.
  `Tab`/`Backspace` cannot be told apart from the main keyboard's keys.
- **macOS:** grant the app **Input Monitoring** (and, for blocking keys, **Accessibility**) in
  *System Settings → Privacy & Security*. The CI build is ad-hoc signed only, so on first run
  right-click → *Open* (or `xattr -dr com.apple.quarantine NumpadToMIDI.app`).
  `Calc`/`Email` keys are not delivered to apps by macOS; those pads work via mouse only.
- *Block numpad keys from other apps* prevents the numpad from also typing into the focused app.
- Settings are stored in `%APPDATA%\NumpadToMIDI\config.json` (Windows) or
  `~/Library/Application Support/NumpadToMIDI/config.json` (macOS); `config.json` in this repo holds the defaults.
