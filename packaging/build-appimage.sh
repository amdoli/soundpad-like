#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
mkdir -p build/cache dist
python3 packaging/collect.py
python3 packaging/assemble.py
if [ "${SOUNDPAD_FULL_RUNTIME:-0}" != 1 ]; then
 python3 packaging/compact.py
fi
if [ ! -f build/cache/appimagetool.AppImage ]; then
 curl -fL --retry 3 https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage -o build/cache/appimagetool.AppImage
fi
chmod +x build/cache/appimagetool.AppImage
ARCH=x86_64 build/cache/appimagetool.AppImage --appimage-extract-and-run -n --mksquashfs-opt -processors --mksquashfs-opt 2 build/soundpad-like.AppDir dist/soundpad-like-x86_64.AppImage
sha256sum dist/soundpad-like-x86_64.AppImage > dist/soundpad-like-x86_64.AppImage.sha256
