bndrgpt says:

A native Linux soundboard inspired by Soundpad, with an independent implementation and a red application icon. Not affiliated with Leppsoft.

## Features

- Sound library, categories, drag reordering, undo/redo and per-sound hotkeys.
- Speaker and microphone playback, separate local monitoring volume, repeat and precise seeking.
- PipeWire links alongside the selected physical microphone; application microphone selection stays unchanged.
- Waveform editor with selection, cut/copy/paste, trimming, gain, fades and backups when overwriting audio.
- Recorder, offline eSpeak NG speech generation, light/dark themes and bundled English/Arabic fonts.

## Supported environment

Tested on CachyOS with KDE Plasma / Wayland, x86_64. Other distributions have not been exhaustively tested. Microphone routing needs a running PipeWire server and its PulseAudio compatibility service. Global shortcuts use KDE KGlobalAccel. Auto Keys requires access to `/dev/uinput`; pass-through and global numpad shortcuts require access to keyboard input devices. These permissions are not changed by the app.

## Run from source

Use Python 3.11 or newer, PyQt6 (including QtSvg and QtDBus), FFmpeg, PulseAudio command-line clients and PipeWire command-line tools. Install these using your distribution's package manager, or install PyQt6 using `requirements.txt` in a virtual environment.

```sh
python3 src/soundpad_like.py
```

The required command-line tools are `ffmpeg`, `ffprobe`, `pactl`, `paplay`, `parec`, `parecord`, `pw-dump` and `pw-link`. The AppImage build also bundles the offline speech runtime; a plain source checkout needs an eSpeak runtime configured through `SOUNDPAD_LIKE_SPEECH` (containing `bin/espeak-ng` and `share/espeak-ng-data`).

## Build the AppImage

On x86_64 Linux, install Python 3, curl, binutils (`ar`), GNU tar and xz tools, then run:

```sh
sh packaging/build-appimage.sh
```

This downloads Debian Bookworm runtime packages and appimagetool into `build/`, verifies Debian package checksums against the package index, and creates `dist/soundpad-like-x86_64.AppImage`. Internet access and several GB of free disk space are needed. Package versions and the continuous appimagetool release may change between builds; this is not a bit-reproducible build.

Send the AppImage from **GitHub Releases**, not as a Git source file. The generated binary is intentionally ignored by `.gitignore`.

The default build is the compact release (about 154 MiB). It keeps the application, Python, Qt, audio codecs, editor, recorder, speech engine and fonts. Optional bundled Mesa GPU drivers, LLVM/Z3 and Intel Media SDK acceleration plugins are omitted. The application uses software audio processing; graphics compatibility on other distributions remains unverified. To retain those optional libraries, build with `SOUNDPAD_FULL_RUNTIME=1 sh packaging/build-appimage.sh`.

## Data and compatibility

No personal sound files, library, preferences or recordings are included in this repository. User data continues to use `~/.local/share/ziyad-soundboard` for compatibility with earlier builds. Internal identifiers retaining the original project name are also intentional. Windows sound-list paths may need remapping on Linux.

The source is currently one executable Python file. Earlier UI classes serve as base classes for the enhanced implementations later in the file; the last definitions are the active ones.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

These focused tests use Qt's offscreen platform and do not capture microphone audio or inject keyboard events.

## Licensing before publication

No project-wide license has been selected for this source snapshot. Add your chosen project license before presenting the repository as open source. Bundled Noto fonts retain their SIL Open Font License in `assets/fonts/OFL.txt`. See `THIRD_PARTY.md` for runtime dependency notices; a project license does not replace those terms.
