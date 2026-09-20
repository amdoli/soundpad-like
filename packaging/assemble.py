from pathlib import Path
import shutil,ast
project=Path(__file__).resolve().parents[1];base=project/'build';root=base/'soundpad-like.AppDir'
(root/'app').mkdir(exist_ok=True);shutil.copy2(project/'src/soundpad_like.py',root/'app/soundpad-like.py')
source=(project/'src/soundpad_like.py').read_text();logo=next(ast.literal_eval(n.value) for n in ast.parse(source).body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='APP_LOGO' for t in n.targets));(root/'soundpad-like.svg').write_text(logo)
(root/'soundpad-like.desktop').write_text('[Desktop Entry]\nType=Application\nName=soundpad like\nExec=AppRun\nIcon=soundpad-like\nCategories=AudioVideo;Audio;\nTerminal=false\n')
(root/'.DirIcon').unlink(missing_ok=True);(root/'.DirIcon').symlink_to('soundpad-like.svg')
(root/'bin').mkdir(exist_ok=True)
lib='$APPDIR/lib/x86_64-linux-gnu:$APPDIR/usr/lib/x86_64-linux-gnu:$APPDIR/usr/lib/x86_64-linux-gnu/pulseaudio'
for name in ['python3','ffmpeg','ffprobe','pactl','paplay','parec','parecord','pw-dump','pw-link','espeak-ng']:
 p=root/'bin'/name;p.write_text('#!/bin/sh\nunset LD_LIBRARY_PATH LD_PRELOAD\nexec "$APPDIR/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2" --library-path "'+lib+'" "$APPDIR/usr/bin/'+name+'" "$@"\n');p.chmod(0o755)
for name in ['pw-dump','pw-link']:
 p=root/'bin'/name;body=p.read_text();p.write_text(body.replace('unset LD_LIBRARY_PATH LD_PRELOAD', 'unset LD_LIBRARY_PATH LD_PRELOAD\nif [ -x /usr/bin/'+name+' ]; then\n unset SPA_PLUGIN_DIR PIPEWIRE_MODULE_DIR PIPEWIRE_CONFIG_DIR\n exec /usr/bin/'+name+' "$@"\nfi'))
(root/'speech/bin').mkdir(parents=True,exist_ok=True);shutil.copy2(root/'bin/espeak-ng',root/'speech/bin/espeak-ng');(root/'speech/share').mkdir(exist_ok=True);link=root/'speech/share/espeak-ng-data';link.unlink(missing_ok=True);link.symlink_to('../../usr/lib/x86_64-linux-gnu/espeak-ng-data')
(root/'app/fonts').mkdir(exist_ok=True)
for name in ['NotoSans-Regular.ttf','NotoSansArabic-Regular.ttf']:
 shutil.copy2(project/'assets/fonts'/name,root/'app/fonts'/name)
(root/'fonts.conf').write_text('<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "fonts.dtd"><fontconfig><dir prefix="relative">app/fonts</dir><dir prefix="relative">usr/share/fonts</dir><cachedir prefix="xdg">soundpad-like/fontconfig</cachedir><alias><family>sans-serif</family><prefer><family>Noto Sans</family><family>Noto Sans Arabic</family></prefer></alias></fontconfig>')

run=root/'AppRun';run.write_text('''#!/bin/sh
APPDIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export APPDIR
export PATH="$APPDIR/bin:$PATH"
export PYTHONHOME="$APPDIR/usr"
export PYTHONPATH="$APPDIR/usr/lib/python3/dist-packages"
export PYTHONNOUSERSITE=1
export PYTHONDONTWRITEBYTECODE=1
export QT_PLUGIN_PATH="$APPDIR/usr/lib/x86_64-linux-gnu/qt6/plugins"
export QT_QPA_PLATFORM_PLUGIN_PATH="$QT_PLUGIN_PATH/platforms"
export QT_QUICK_BACKEND=software
export SPA_PLUGIN_DIR="$APPDIR/usr/lib/x86_64-linux-gnu/spa-0.2"
export PIPEWIRE_MODULE_DIR="$APPDIR/usr/lib/x86_64-linux-gnu/pipewire-0.3"
export PIPEWIRE_CONFIG_DIR="$APPDIR/usr/share/pipewire"
export FONTCONFIG_FILE="$APPDIR/fonts.conf"
export FONTCONFIG_PATH="$APPDIR"
export XKB_CONFIG_ROOT="$APPDIR/usr/share/X11/xkb"
export SOUNDPAD_LIKE_SPEECH="$APPDIR/speech"
if [ "$1" = "--python" ]; then shift; exec "$APPDIR/bin/python3" "$@"; fi
exec "$APPDIR/bin/python3" "$APPDIR/app/soundpad-like.py" "$@"
''');run.chmod(0o755)
shutil.copy2(base/'packages.json',root/'app/BUNDLED-PACKAGES.json');shutil.copy2(project/'assets/fonts/OFL.txt',root/'app/fonts/OFL.txt')
# Retain all dependency copyright notices, but not unused manuals/caches.
for p in [root/'usr/share/man',root/'usr/share/locale',root/'var',root/'etc/systemd']:
 if p.exists():shutil.rmtree(p)
print(root)
