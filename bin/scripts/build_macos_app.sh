#!/usr/bin/env bash
# Builds "Loop X.app": a thin macOS bundle that launches bin/desktop_app.py
# from THIS checkout inside its own venv (pywebview and PyYAML live there, not in the
# repo's stdlib-only runtime). Config and run state stay in ~/.loop-engineering
# and outputs/, never inside the bundle, so the app is tied to this checkout.
#
# Usage: build_macos_app.sh [--output-dir DIR] [--python PATH] [--skip-venv] [--skip-icon]
#                           [--desktop-shortcut] [--desktop-dir DIR]
# --dmg also packs the app into DIR/Loop X.dmg (drag-to-Applications disk image).
# --desktop-shortcut puts a Finder alias to the app on ~/Desktop; --desktop-dir
# DIR puts a plain symlink in DIR instead (no Finder automation prompt).
set -euo pipefail

LOOP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUTPUT_DIR="$LOOP_DIR/dist"
PYTHON="python3"
SKIP_VENV=0
SKIP_ICON=0
SHORTCUT=0
DMG=0
DESKTOP_DIR=""
APP_NAME="Loop X"

while [ $# -gt 0 ]; do
  case "$1" in
    --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
    --python) PYTHON="$2"; shift 2 ;;
    --skip-venv) SKIP_VENV=1; shift ;;
    --skip-icon) SKIP_ICON=1; shift ;;
    --dmg) DMG=1; shift ;;
    --desktop-shortcut) SHORTCUT=1; shift ;;
    --desktop-dir) SHORTCUT=1; DESKTOP_DIR="$2"; shift 2 ;;
    *) echo "Usage: build_macos_app.sh [--output-dir DIR] [--python PATH] [--skip-venv] [--skip-icon] [--dmg] [--desktop-shortcut] [--desktop-dir DIR]" >&2; exit 1 ;;
  esac
done

APP="$OUTPUT_DIR/$APP_NAME.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>$APP_NAME</string>
  <key>CFBundleDisplayName</key><string>$APP_NAME</string>
  <key>CFBundleIdentifier</key><string>app.loopx.desktop</string>
  <key>CFBundleExecutable</key><string>loop-x</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.1.0</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

cat > "$APP/Contents/MacOS/loop-x" <<LAUNCHER
#!/usr/bin/env bash
# GUI apps don't inherit the shell PATH; the loop shells out to claude/git/etc.
export PATH="\$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
RES="\$(cd "\$(dirname "\$0")/../Resources" && pwd)"
PY="\$RES/venv/bin/python"
[ -x "\$PY" ] || PY="python3"
exec "\$PY" "$LOOP_DIR/bin/desktop_app.py" "\$@"
LAUNCHER
chmod +x "$APP/Contents/MacOS/loop-x"

if [ "$SKIP_VENV" -eq 0 ]; then
  "$PYTHON" -m venv "$APP/Contents/Resources/venv"
  "$APP/Contents/Resources/venv/bin/python" -m pip install --quiet --upgrade pip pywebview pyyaml
fi

if [ "$SKIP_ICON" -eq 0 ]; then
  work="$(mktemp -d)"
  trap 'rm -rf "$work"' EXIT
  # app-icon.png (rendered from app-icon.svg) is already 1024px on the macOS icon grid
  square="$LOOP_DIR/assets/app-icon.png"
  iconset="$work/AppIcon.iconset"
  mkdir "$iconset"
  for s in 16 32 128 256 512; do
    sips -z "$s" "$s" "$square" --out "$iconset/icon_${s}x${s}.png" >/dev/null
    sips -z "$((s * 2))" "$((s * 2))" "$square" --out "$iconset/icon_${s}x${s}@2x.png" >/dev/null
  done
  iconutil -c icns "$iconset" -o "$APP/Contents/Resources/AppIcon.icns"
fi

echo "Built: $APP"

if [ "$DMG" -eq 1 ]; then
  stage="$(mktemp -d)"
  cp -R "$APP" "$stage/"
  ln -s /Applications "$stage/Applications"
  rm -f "$OUTPUT_DIR/$APP_NAME.dmg"
  hdiutil create -quiet -volname "$APP_NAME" -srcfolder "$stage" -ov -format UDZO "$OUTPUT_DIR/$APP_NAME.dmg"
  rm -rf "$stage"
  echo "DMG: $OUTPUT_DIR/$APP_NAME.dmg"
fi

if [ "$SHORTCUT" -eq 1 ]; then
  if [ -n "$DESKTOP_DIR" ]; then
    mkdir -p "$DESKTOP_DIR"
    ln -sfn "$APP" "$DESKTOP_DIR/$APP_NAME"
  else
    DESKTOP_DIR="$HOME/Desktop"
    mkdir -p "$DESKTOP_DIR"
    rm -f "$DESKTOP_DIR/$APP_NAME"
    osascript <<OSA >/dev/null
tell application "Finder"
  make new alias file to (POSIX file "$APP" as alias) at (POSIX file "$DESKTOP_DIR" as alias) with properties {name:"$APP_NAME"}
end tell
OSA
  fi
  echo "Shortcut: $DESKTOP_DIR/$APP_NAME"
fi
