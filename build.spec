# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for NumpadToMIDI.
#   Windows -> dist/NumpadToMIDI-Windows.exe   (single file)
#   macOS   -> dist/NumpadToMIDI.app           (zipped to NumpadToMIDI-macOS.zip by CI)
import sys

from PyInstaller.utils.hooks import collect_submodules

_version = {}
exec(open("version.py", encoding="utf-8").read(), _version)
APP_VERSION = _version.get("VERSION", "1.5.0")

hiddenimports = collect_submodules("rtmidi")
if sys.platform == "win32":
    hiddenimports += ["pynput.keyboard._win32", "pynput.mouse._win32"]
elif sys.platform == "darwin":
    hiddenimports += ["pynput.keyboard._darwin", "pynput.mouse._darwin"]

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=[],
    datas=[("config.json", "."), ("assets", "assets")],
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)

if sys.platform == "darwin":
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="NumpadToMIDI",
        debug=False,
        strip=False,
        upx=False,
        console=False,
        argv_emulation=False,
        icon="assets/icon.icns",
    )
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="NumpadToMIDI")
    app = BUNDLE(
        coll,
        name="NumpadToMIDI.app",
        icon="assets/icon.icns",
        bundle_identifier="com.numpadtomidi.app",
        info_plist={
            "CFBundleName": "NumpadToMIDI",
            "CFBundleDisplayName": "NumpadToMIDI",
            "CFBundleShortVersionString": APP_VERSION,
            "CFBundleVersion": APP_VERSION,
            "NSHighResolutionCapable": True,
            "NSInputMonitoringUsageDescription":
                "NumpadToMIDI listens to your numpad to send MIDI notes.",
        },
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="NumpadToMIDI-Windows",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        runtime_tmpdir=None,
        console=False,
        icon="assets/icon.ico",
        uac_admin=True,
    )
