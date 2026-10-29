#!/usr/bin/env bash
# Build NumpadToMIDI-macOS-v<version>.dmg from dist/NumpadToMIDI.app using only
# built-in macOS tools (codesign + hdiutil), so it runs unchanged on CI runners.
#
# usage: installer/make_dmg.sh [version]        e.g. installer/make_dmg.sh 1.4.2
set -euo pipefail

VERSION="${1:-1.4.2}"
APP_NAME="NumpadToMIDI"
APP_PATH="dist/${APP_NAME}.app"
DMG_PATH="dist/${APP_NAME}-macOS-v${VERSION}.dmg"
VOLNAME="${APP_NAME} ${VERSION}"
STAGE="$(mktemp -d)/dmg-root"

[ -d "$APP_PATH" ] || { echo "error: $APP_PATH not found - run pyinstaller first" >&2; exit 1; }

echo "==> Ad-hoc signing the app bundle"
codesign --force --deep --sign - "$APP_PATH"
codesign --verify --deep --strict --verbose=2 "$APP_PATH"

echo "==> Staging DMG contents"
mkdir -p "$STAGE"
ditto "$APP_PATH" "$STAGE/${APP_NAME}.app"
ln -s /Applications "$STAGE/Applications"
cp installer/dmg_readme.txt "$STAGE/READ ME FIRST.txt"

echo "==> Creating $DMG_PATH"
rm -f "$DMG_PATH"
for attempt in 1 2 3 4 5; do
  if hdiutil create -volname "$VOLNAME" -srcfolder "$STAGE" -fs HFS+ -format UDZO \
        -imagekey zlib-level=9 -ov "$DMG_PATH"; then
    break
  fi
  [ "$attempt" -lt 5 ] || { echo "error: hdiutil create kept failing" >&2; exit 1; }
  echo "hdiutil create failed (attempt $attempt), retrying..."
  sleep 5
done

echo "==> Verifying the image"
hdiutil verify "$DMG_PATH"
MOUNT="$(mktemp -d)"
hdiutil attach "$DMG_PATH" -mountpoint "$MOUNT" -nobrowse -readonly -quiet
trap 'hdiutil detach "$MOUNT" -quiet -force || true' EXIT
ls -la "$MOUNT"
[ -d "$MOUNT/${APP_NAME}.app" ]            || { echo "error: app missing in DMG" >&2; exit 1; }
[ "$(readlink "$MOUNT/Applications")" = "/Applications" ] || { echo "error: Applications symlink missing" >&2; exit 1; }
[ -f "$MOUNT/READ ME FIRST.txt" ]          || { echo "error: README missing in DMG" >&2; exit 1; }
echo "==> OK: $DMG_PATH"
