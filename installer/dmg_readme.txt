NumpadToMIDI for macOS
======================

INSTALL
  1. Drag "NumpadToMIDI" onto the "Applications" folder.
  2. Eject this disk image.

FIRST LAUNCH (one time)
  This app is not notarized by Apple, so macOS may say it cannot verify the
  developer. To open it anyway:
    - In Finder, open Applications, RIGHT-CLICK (or Control-click) NumpadToMIDI
      and choose "Open", then click "Open" again.
    - If macOS still blocks it: System Settings > Privacy & Security, scroll
      down and click "Open Anyway" next to NumpadToMIDI.
    - Terminal alternative:
        xattr -dr com.apple.quarantine /Applications/NumpadToMIDI.app

ALLOW KEY DETECTION: "INPUT MONITORING" (one time)
  NumpadToMIDI listens to your numpad globally, which macOS only allows after
  you grant permission:
    1. Open System Settings > Privacy & Security > Input Monitoring.
    2. Click "+", choose NumpadToMIDI from Applications (or switch it on if it is
       already listed).
    3. Quit and re-open NumpadToMIDI.
  The "Block numpad keys from other apps" option also needs
  System Settings > Privacy & Security > Accessibility.

USE IT IN YOUR DAW
  NumpadToMIDI creates a virtual MIDI source called "NumpadToMIDI" - no extra
  driver needed. Enable it as a MIDI input (Track/Remote) in Ableton Live,
  Logic, GarageBand, etc. Default layout: C1 (note 36) = the "0" key.

Project page: https://github.com/homejes/NumpadToMIDI
