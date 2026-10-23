#!/usr/bin/env bash
set -euo pipefail

# Builds the native LoopX.app (macos/LoopX, Swift) as a universal bundle:
#
#   bin/scripts/build_loopx_app.sh <path-to-loopx-binary> [--out dist/LoopX.app] [--arch universal|arm64|x86_64]
#
# The bundle embeds <path-to-loopx-binary> as Contents/Helpers/loopx plus a LaunchAgent plist under
# Contents/Library/LaunchAgents, which the app registers with SMAppService so the backend outlives
# the app. It is ad-hoc signed (`codesign -s -`); Developer ID signing and notarization are P5.
# Nothing here registers a login item or touches ~/Library/LaunchAgents: the app does that itself,
# on the machine that runs it.
#
# Needs only the Command Line Tools (no Xcode). For a universal app, pass a universal `loopx`
# (lipo -create the two cargo targets); an arm64-only one still builds but only runs on Apple silicon.

LOOP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PKG="$LOOP_DIR/macos/LoopX"
OUT="$LOOP_DIR/dist/LoopX.app"
ARCH="universal"
LOOPX_BIN=""
BUNDLE_ID="com.hermes.loopx"
AGENT_LABEL="com.hermes.loopx.backend"

usage() { sed -n '4,6p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit "${1:-2}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT="$2"; shift 2 ;;
    --arch) ARCH="$2"; shift 2 ;;
    -h|--help) usage 0 ;;
    -*) echo "unknown option: $1" >&2; usage ;;
    *) [ -z "$LOOPX_BIN" ] && LOOPX_BIN="$1" || { echo "unexpected argument: $1" >&2; usage; }; shift ;;
  esac
done

[ -n "$LOOPX_BIN" ] || { echo "error: path to the loopx binary is required" >&2; usage; }
[ -x "$LOOPX_BIN" ] || { echo "error: $LOOPX_BIN is not an executable file" >&2; exit 1; }
case "$ARCH" in
  universal) ARCH_FLAGS=(--arch arm64 --arch x86_64) ;;
  arm64|x86_64) ARCH_FLAGS=(--arch "$ARCH") ;;
  *) echo "error: --arch must be universal, arm64 or x86_64" >&2; exit 2 ;;
esac

# `loopx --version` prints "loopx X.Y.Z"; the app compares this with the running backend's version.
VERSION="$("$LOOPX_BIN" --version | awk '{print $NF}')"
case "$VERSION" in
  [0-9]*.[0-9]*.[0-9]*) ;;
  *) echo "error: could not read a version from '$LOOPX_BIN --version' (got '$VERSION')" >&2; exit 1 ;;
esac

echo "==> building LoopX (release, $ARCH)"
swift build -c release --package-path "$PKG" "${ARCH_FLAGS[@]}" --product LoopX
BIN_DIR="$(swift build -c release --package-path "$PKG" "${ARCH_FLAGS[@]}" --show-bin-path)"
APP_BIN="$BIN_DIR/LoopX"
[ -x "$APP_BIN" ] || { echo "error: build did not produce $APP_BIN" >&2; exit 1; }

echo "==> assembling $OUT"
case "$OUT" in /|""|"$HOME"|"$HOME/") echo "error: refusing to use '$OUT' as output" >&2; exit 2 ;; esac
rm -rf "$OUT"
mkdir -p "$OUT/Contents/MacOS" "$OUT/Contents/Helpers" "$OUT/Contents/Library/LaunchAgents" "$OUT/Contents/Resources"
cp "$APP_BIN" "$OUT/Contents/MacOS/LoopX"
cp "$LOOPX_BIN" "$OUT/Contents/Helpers/loopx"
chmod 755 "$OUT/Contents/MacOS/LoopX" "$OUT/Contents/Helpers/loopx"

cat > "$OUT/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>LoopX</string>
  <key>CFBundleDisplayName</key><string>LoopX</string>
  <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
  <key>CFBundleExecutable</key><string>LoopX</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>LoopXBundledVersion</key><string>$VERSION</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSSupportsAutomaticGraphicsSwitching</key><true/>
</dict>
</plist>
PLIST

# SMAppService.agent(plistName:) looks for exactly this file name under Contents/Library/LaunchAgents.
# KeepAlive.Crashed only: a clean "another backend already owns loopx.lock" exit must not respawn-loop.
cat > "$OUT/Contents/Library/LaunchAgents/$AGENT_LABEL.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$AGENT_LABEL</string>
  <key>BundleProgram</key><string>Contents/Helpers/loopx</string>
  <key>ProgramArguments</key>
  <array>
    <string>Contents/Helpers/loopx</string>
    <string>serve</string>
  </array>
  <key>AssociatedBundleIdentifiers</key>
  <array><string>$BUNDLE_ID</string></array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>Crashed</key><true/></dict>
  <key>ProcessType</key><string>Background</string>
</dict>
</plist>
PLIST

plutil -lint "$OUT/Contents/Info.plist" "$OUT/Contents/Library/LaunchAgents/$AGENT_LABEL.plist" >/dev/null

echo "==> ad-hoc signing"
codesign --force --deep -s - "$OUT"
codesign --verify --deep --strict "$OUT"

# Keep build output out of git without touching the repo's own .gitignore.
case "$OUT" in "$LOOP_DIR/dist/"*) printf '*\n' > "$LOOP_DIR/dist/.gitignore" ;; esac

echo "==> done: $OUT (loopx $VERSION)"
echo "    app:   $(lipo -archs "$OUT/Contents/MacOS/LoopX")"
echo "    loopx: $(lipo -archs "$OUT/Contents/Helpers/loopx")"
if [ "$ARCH" = universal ] && [ "$(lipo -archs "$OUT/Contents/Helpers/loopx" | wc -w | tr -d ' ')" -lt 2 ]; then
  echo "    note: the embedded loopx is not universal, so the bundle only runs on $(lipo -archs "$OUT/Contents/Helpers/loopx")"
fi
