"""Remove optional GPU drivers; retain audio codecs and application features."""
from pathlib import Path
import json
import shutil

root = Path(__file__).resolve().parents[1] / 'build/soundpad-like.AppDir'
removed = []
for name in ('dri', 'mfx', 'libLLVM-15.so.1', 'libLLVM-15.so', 'libz3.so.4', 'libz3.so'):
    path = root / 'usr/lib/x86_64-linux-gnu' / name
    if path.is_symlink() or path.is_file():
        path.unlink()
        removed.append(str(path.relative_to(root)))
    elif path.is_dir():
        shutil.rmtree(path)
        removed.append(str(path.relative_to(root)))
(root / 'app/PACKAGING.json').write_text(json.dumps({
    'variant': 'compact',
    'removed_optional_components': removed,
    'note': 'No app features removed. Optional bundled Mesa drivers and Intel video acceleration omitted.'
}, indent=2) + '\n')
