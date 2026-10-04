#!/bin/bash
# pipefail so a failed xcodebuild isn't masked by grep's exit status (which
# would otherwise let the script package a stale binary).
set -euo pipefail

cd "$(dirname "$0")"

# Locate an Xcode install; fall back to xcode-select if the default path is absent.
if [ -d "/Applications/Xcode.app/Contents/Developer" ]; then
    export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer
fi
if ! xcodebuild -version >/dev/null 2>&1; then
    echo "error: xcodebuild not found — full Xcode is required (not just Command Line Tools)." >&2
    exit 1
fi

echo "Building Scribe..."
# Filter noisy output but preserve xcodebuild's real exit status via PIPESTATUS.
set +e
xcodebuild -scheme ScribeApp \
    -configuration Release \
    -derivedDataPath .xcode-build \
    -destination "platform=macOS" \
    build 2>&1 | grep -E "error:|warning:|BUILD"
BUILD_STATUS=${PIPESTATUS[0]}
set -e
if [ "$BUILD_STATUS" -ne 0 ]; then
    echo "error: xcodebuild failed (status $BUILD_STATUS) — not packaging stale binary." >&2
    exit 1
fi

BINARY=".xcode-build/Build/Products/Release/ScribeApp"
if [ ! -x "$BINARY" ]; then
    echo "error: build did not produce $BINARY — aborting." >&2
    exit 1
fi

# Create .app bundle
rm -rf build/Scribe.app
mkdir -p build/Scribe.app/Contents/MacOS
mkdir -p build/Scribe.app/Contents/Resources

cp "$BINARY" build/Scribe.app/Contents/MacOS/Scribe
cp Sources/Info.plist build/Scribe.app/Contents/Info.plist

# Stamp the build so Settings can show exactly which build is running. Set
# CFBundleVersion to a build timestamp and record the git commit (with a "+" if
# the tree had uncommitted changes). Done before signing so the seal covers it.
PLIST="build/Scribe.app/Contents/Info.plist"
BUILD_STAMP="$(date +%Y%m%d.%H%M)"
GIT_COMMIT="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
git diff --quiet 2>/dev/null || GIT_COMMIT="${GIT_COMMIT}+"
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion $BUILD_STAMP" "$PLIST" >/dev/null 2>&1 || true
/usr/libexec/PlistBuddy -c "Add :ScribeGitCommit string $GIT_COMMIT" "$PLIST" >/dev/null 2>&1 \
  || /usr/libexec/PlistBuddy -c "Set :ScribeGitCommit $GIT_COMMIT" "$PLIST" >/dev/null 2>&1 || true
echo "Stamped build $BUILD_STAMP ($GIT_COMMIT)"

# Copy app icon
cp Scribe.icns build/Scribe.app/Contents/Resources/
cp mic_idle.png mic_idle@2x.png build/Scribe.app/Contents/Resources/

# Copy resource bundles from dependencies (e.g. KeyboardShortcuts localizations)
for bundle in .xcode-build/Build/Products/Release/*.bundle; do
    [ -d "$bundle" ] && cp -r "$bundle" build/Scribe.app/Contents/Resources/
done

# Ad-hoc sign so Gatekeeper allows it to run. Sign nested bundles first, then the
# app (replaces deprecated --deep). Note: ad-hoc identity changes each build, so
# TCC (microphone) grants may need re-approval after an --install.
for bundle in build/Scribe.app/Contents/Resources/*.bundle; do
    [ -d "$bundle" ] && codesign --force --sign - "$bundle"
done
codesign --force --sign - build/Scribe.app

echo ""
echo "Built: $(pwd)/build/Scribe.app"

if [ "${1:-}" = "--install" ]; then
    pkill -x Scribe 2>/dev/null || true
    pkill -f "src.cli record" 2>/dev/null || true
    # Wait for the old instance to fully exit before relaunching — opening too soon
    # after the kill fails with a transient launchd/RunningBoard error (the old job
    # is still being reaped).
    for _ in $(seq 1 25); do pgrep -x Scribe >/dev/null 2>&1 || break; sleep 0.2; done
    sleep 0.5
    cp -r build/Scribe.app /Applications/
    echo "Installed to /Applications/Scribe.app"
    # The first spawn right after replacing a running app can still transiently
    # fail; retry a few times, and don't fail the build if the launch doesn't take.
    opened=""
    for _ in 1 2 3; do
        if open /Applications/Scribe.app 2>/dev/null; then opened=1; break; fi
        sleep 1
    done
    [ -n "$opened" ] || echo "note: couldn't auto-launch (transient) — open /Applications/Scribe.app yourself."
fi

echo ""
echo "Usage:"
echo "  ./build.sh            # build only"
echo "  ./build.sh --install  # build + copy to /Applications + launch"
echo ""
echo "To start on login:"
echo "  System Settings > General > Login Items & Extensions > +"
echo "  Add /Applications/Scribe.app"
