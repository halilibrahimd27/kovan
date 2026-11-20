#!/bin/bash
# Builds ~/Applications/kovan.app — a Dock icon that starts kovan (if needed) and opens it in its own window.
#
#   scripts/make-macos-app.sh --dir ~/code/project-a --dir ~/code/project-b
#
# Any options are passed to kovan every time the app opens. Without --dir, kovan uses the "dirs" list
# from ~/.config/kovan/config.json (apps launched from the Dock have no useful working directory).
set -euo pipefail
[ "$(uname)" = Darwin ] || { echo "macOS only" >&2; exit 1; }

repo=$(cd "$(dirname "$0")/.." && pwd)
app="$HOME/Applications/kovan.app"

if command -v kovan >/dev/null 2>&1; then
  launch=("$(command -v kovan)")
else
  launch=(/usr/bin/env "PYTHONPATH=$repo" "$(command -v python3)" -m kovan)
fi
if [ "$#" -eq 0 ] && [ ! -f "$HOME/.config/kovan/config.json" ]; then
  echo "note: no --dir given and no ~/.config/kovan/config.json — the app will watch '/' (nothing)." >&2
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
{
  echo '#!/bin/sh'
  printf 'exec'; printf ' %q' "${launch[@]}" --window "$@"; echo
} > "$tmp/launch.sh"
chmod +x "$tmp/launch.sh"

mkdir -p "$HOME/Applications"
rm -rf "$app"
osacompile -o "$app" -e 'do shell script quoted form of (POSIX path of (path to resource "launch.sh")) & " > /dev/null 2>&1 &"'
cp "$tmp/launch.sh" "$app/Contents/Resources/launch.sh"

# Icon from docs/icon.png
iconset="$tmp/kovan.iconset"; mkdir "$iconset"
for size in 16 32 128 256 512; do
  sips -z $size $size "$repo/docs/icon.png" --out "$iconset/icon_${size}x${size}.png" >/dev/null
  sips -z $((size * 2)) $((size * 2)) "$repo/docs/icon.png" --out "$iconset/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$iconset" -o "$app/Contents/Resources/applet.icns"
/usr/libexec/PlistBuddy -c "Delete :CFBundleIconName" "$app/Contents/Info.plist" 2>/dev/null || true
rm -f "$app/Contents/Resources/Assets.car"
/usr/libexec/PlistBuddy -c "Set :CFBundleName kovan" "$app/Contents/Info.plist" 2>/dev/null || true
codesign --force --sign - "$app" >/dev/null 2>&1 || true
touch "$app"
echo "created $app — drag it to the Dock."
