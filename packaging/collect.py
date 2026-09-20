from pathlib import Path
import urllib.request,lzma,hashlib,re,subprocess,concurrent.futures,json
base=Path(__file__).resolve().parents[1]/'build';base.mkdir(exist_ok=True);cache=base/'cache';cache.mkdir(exist_ok=True);root=base/'soundpad-like.AppDir';root.mkdir(exist_ok=True)
packages={}
for host,suite in [('https://deb.debian.org/debian','bookworm'),('https://security.debian.org/debian-security','bookworm-security')]:
 dest=cache/(suite+'.xz')
 if not dest.exists():urllib.request.urlretrieve(host+'/dists/'+suite+'/main/binary-amd64/Packages.xz',dest)
 for block in lzma.decompress(dest.read_bytes()).decode().split('\n\n'):
  fields={};key=None
  for line in block.splitlines():
   if line.startswith(' ') and key:fields[key]+=' '+line.strip()
   elif ': ' in line:key,value=line.split(': ',1);fields[key]=value
  if 'Package' in fields:fields['host']=host;packages[fields['Package']]=fields
seeds=['python3','python3-pyqt6','python3-pyqt6.qtsvg','qt6-wayland','ffmpeg','pulseaudio-utils','pipewire-bin','espeak-ng','fonts-dejavu-core']
selected={}
def add(name):
 if name in selected:return
 if name not in packages:raise ValueError(name)
 p=packages[name];selected[name]=p
 for dep in (p.get('Pre-Depends','')+','+p.get('Depends','')).split(','):
  choices=[re.split(r'[ (:\[]',x.strip())[0] for x in dep.split('|') if x.strip()]
  if not choices:continue
  choice=next((n for n in choices if n in packages),None)
  if choice:add(choice)
  else:
   providers=[n for n,p in packages.items() if any(c in [x.strip().split(' ')[0] for x in p.get('Provides','').split(',')] for c in choices)]
   if providers:add(sorted(providers)[0])
   else:raise ValueError(choices)
for name in seeds:add(name)
print('Packages:',len(selected),'download MB:',round(sum(int(p['Size']) for p in selected.values())/1e6),flush=True)
def fetch(p):
 path=cache/Path(p['Filename']).name
 if not path.exists():urllib.request.urlretrieve(p['host']+'/'+p['Filename'],path)
 assert hashlib.sha256(path.read_bytes()).hexdigest()==p['SHA256'],path
 return path
with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:paths=list(pool.map(fetch,selected.values()))
for path in paths:
 member=next(n for n in subprocess.check_output(['ar','t',str(path)],text=True).splitlines() if n.startswith('data.tar'))
 ar=subprocess.Popen(['ar','p',str(path),member],stdout=subprocess.PIPE);subprocess.run(['tar','-x',{'xz':'-J','gz':'-z','zst':'--zstd'}[member.rsplit('.',1)[-1]],'-C',str(root)],stdin=ar.stdout,check=True);ar.stdout.close();assert ar.wait()==0
(base/'packages.json').write_text(json.dumps([{k:p.get(k) for k in ['Package','Version','Filename','SHA256','host']} for p in selected.values()],indent=2));print('Extracted',root,flush=True)
