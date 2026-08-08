#!/bin/bash
# Build the Jeeves native helper.
#
# Two details matter for a command-line tool that touches private data:
#   1. The usage strings must be embedded in a __TEXT,__info_plist section,
#      otherwise macOS refuses to show a permission prompt and just denies.
#   2. The binary is ad-hoc signed so it keeps a stable identity; without that,
#      every rebuild looks like a new program and permission grants are lost.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"
BUILD="$ROOT/native/build"
PLIST="$BUILD/Info.plist"
OUT="$BUILD/jeeves-native"

mkdir -p "$BUILD"

cat > "$PLIST" <<'PLIST_EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key>            <string>local.jeeves.native</string>
  <key>CFBundleName</key>                  <string>Jeeves</string>
  <key>CFBundledisplayName</key>           <string>Jeeves</string>
  <key>CFBundleShortVersionString</key>    <string>1.0.0</string>
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
</dict>
</plist>
PLIST_EOF

echo "==> compiling native/Jeeves.swift"
# Build for whichever architecture this Mac is, so the project is portable
# between Apple silicon and Intel machines.
HOST_ARCH="$(uname -m)"
swiftc \
  -O -whole-module-optimization \
  -target "${HOST_ARCH}-apple-macos14.0" \
  -framework AppKit -framework Vision -framework EventKit \
  -framework Speech -framework AVFoundation -framework Contacts \
  -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker "$PLIST" \
  -o "$OUT" \
  "$ROOT/native/Jeeves.swift"

echo "==> ad-hoc signing"
codesign --force --sign - --identifier local.jeeves.native --timestamp=none "$OUT"

echo "==> verifying"
codesign -dv "$OUT" 2>&1 | sed 's/^/    /' || true
"$OUT" 2>&1 | head -1 || true

echo
echo "Built $OUT"
echo "First use of voice, calendar, reminders or contacts will prompt for permission."
