# Compact Linux release

Native x86_64 Linux soundboard inspired by Soundpad.

- Compact AppImage with the same soundboard features; no personal library or sounds included.
- Includes Python, Qt, FFmpeg audio codecs, editor, recorder, offline speech and Arabic/English fonts.
- Omits optional bundled Mesa drivers, LLVM/Z3 and Intel video acceleration plugins.
- Tested on CachyOS / KDE Plasma / Wayland with PipeWire. Other environments are not fully verified.
- Microphone routing requires PipeWire and its PulseAudio service. Global shortcut behavior depends on desktop environment and input permissions; see README.md.

Download `soundpad-like-x86_64.AppImage`, make it executable and launch:

```sh
chmod +x soundpad-like-x86_64.AppImage
./soundpad-like-x86_64.AppImage
```

If FUSE is unavailable, launch with `--appimage-extract-and-run`.

Validation: dependency resolution for all ten bundled commands; fine seek positioning and keypad distinction; audio delivery to two isolated capture streams without changing source IDs; microphone mute restoration; waveform editing, undo/redo, normalization, MP3 export and backup on save; desktop window/preferences and offline speech smoke checks.
