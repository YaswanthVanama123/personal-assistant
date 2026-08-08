#!/bin/bash
# Build the Jeeves native helper as a proper application bundle.
#
# Why a bundle rather than a bare executable: TCC — the privacy system — does not
# read usage-description strings out of a Mach-O's embedded __TEXT,__info_plist
# section for every service. Microphone, Calendars, Reminders and Contacts accept
# it; Speech Recognition does not, and kills the process outright:
#
#   namespace: TCC
#   "This app has crashed because it attempted to access privacy-sensitive data
#    without a usage description. The app's Info.plist must contain an
#    NSSpeechRecognitionUsageDescription key…"
#
# A bundle with a real Contents/Info.plist is read reliably by every service. The
# binary is still run straight from the command line; macOS walks up from
# Contents/MacOS/ to find the bundle, so it gets the app identity either way.
#
# The bundle is ad-hoc signed so its identity is stable across rebuilds and
# permission grants persist instead of being requested again every time.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"
BUILD="$ROOT/native/build"
APP="$BUILD/Jeeves.app"
CONTENTS="$APP/Contents"
BIN="$CONTENTS/MacOS/jeeves-native"

rm -rf "$APP"
mkdir -p "$CONTENTS/MacOS" "$CONTENTS/Resources"

cat > "$CONTENTS/Info.plist" <<'PLIST_EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key>            <string>local.jeeves.native</string>
  <key>CFBundleName</key>                  <string>Jeeves</string>
  <key>CFBundleDisplayName</key>           <string>Jeeves</string>
  <key>CFBundleExecutable</key>            <string>jeeves-native</string>
  <key>CFBundlePackageType</key>           <string>APPL</string>
  <key>CFBundleSignature</key>             <string>????</string>
  <key>CFBundleShortVersionString</key>    <string>1.0.0</string>
  <key>CFBundleVersion</key>               <string>1</string>
  <key>LSMinimumSystemVersion</key>        <string>14.0</string>
  <key>LSUIElement</key>                   <true/>
  <key>NSMicrophoneUsageDescription</key>
  <string>Jeeves listens to your voice commands and transcribes them on this Mac.</string>
  <key>NSSpeechRecognitionUsageDescription</key>
  <string>Jeeves converts your spoken requests to text using on-device speech recognition.</string>
  <key>NSCalendarsFullAccessUsageDescription</key>
  <string>Jeeves reads your schedule and creates events when you ask it to.</string>
  <key>NSRemindersFullAccessUsageDescription</key>
  <string>Jeeves reads and creates reminders when you ask it to.</string>
  <key>NSContactsUsageDescription</key>
  <string>Jeeves looks up phone numbers and email addresses so it can message the right person.</string>
  <key>NSAppleEventsUsageDescription</key>
  <string>Jeeves controls apps such as Notes, Mail and Music on your behalf.</string>
  <key>NSDesktopFolderUsageDescription</key>
  <string>Jeeves finds and organises files you ask it about.</string>
  <key>NSDocumentsFolderUsageDescription</key>
  <string>Jeeves finds and organises files you ask it about.</string>
  <key>NSDownloadsFolderUsageDescription</key>
  <string>Jeeves finds and organises files you ask it about.</string>
</dict>
</plist>
PLIST_EOF

printf 'APPL????' > "$CONTENTS/PkgInfo"

echo "==> compiling native/*.swift for $(uname -m)"
# The plist is also embedded in the Mach-O. Belt and braces: the bundle is what
# TCC reads, but the embedded copy keeps Bundle.main working if the binary is
# ever copied out of the bundle.
swiftc \
  -O -whole-module-optimization \
  -target "$(uname -m)-apple-macos14.0" \
  -framework AppKit -framework Vision -framework EventKit -framework ApplicationServices \
  -framework Speech -framework AVFoundation -framework Contacts \
  -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker "$CONTENTS/Info.plist" \
  -o "$BIN" \
  "$ROOT/native/main.swift" "$ROOT/native/Accessibility.swift"

echo "==> ad-hoc signing the bundle"
codesign --force --deep --sign - --identifier local.jeeves.native --timestamp=none "$APP"

# Convenience path so existing commands and muscle memory keep working.
#
# This must be an exec wrapper, not a symlink. Running the binary through a
# symlink makes Bundle.main resolve to the symlink's directory rather than the
# .app, so TCC stops seeing the usage strings and Speech Recognition kills the
# process again. `exec` replaces this shell with the real binary at its real
# path, which keeps the bundle identity intact.
cat > "$BUILD/jeeves-native" <<'SHIM_EOF'
#!/bin/sh
exec "$(dirname "$0")/Jeeves.app/Contents/MacOS/jeeves-native" "$@"
SHIM_EOF
chmod +x "$BUILD/jeeves-native"

echo "==> verifying"
codesign -dv "$APP" 2>&1 | sed -n '1,5p' | sed 's/^/    /'
echo "    bundle id:  $(defaults read "$CONTENTS/Info.plist" CFBundleIdentifier 2>/dev/null)"
if defaults read "$CONTENTS/Info.plist" NSSpeechRecognitionUsageDescription >/dev/null 2>&1; then
  echo "    speech key: present in Contents/Info.plist"
else
  echo "    speech key: MISSING — voice will crash"
fi
"$BIN" 2>&1 | head -1 || true

echo
echo "Built $APP"
echo "Run as: $BUILD/jeeves-native <command>"
echo
echo "Permissions now appear as “Jeeves” in System Settings → Privacy & Security,"
echo "rather than being attributed to your terminal."
