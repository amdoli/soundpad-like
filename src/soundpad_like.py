#!/usr/bin/env python3
"""Native Linux soundboard. Requires system PyQt6, ffmpeg, paplay and pactl."""
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from PyQt6 import QtCore, QtGui, QtWidgets as W

DATA = Path(os.environ.get('XDG_DATA_HOME', str(Path.home()/'.local/share'))) / 'ziyad-soundboard'
DATA.mkdir(parents=True, exist_ok=True)
CONFIG = DATA / 'library.json'
PREFIX = 'ziyad_board_'
CLIPS, MIX, MIC = PREFIX+'clips', PREFIX+'mix', PREFIX+'mic'
FORMATS = {'.mp3','.wav','.ogg','.flac','.m4a','.aac','.opus','.wma','.mp4','.webm'}

def pa(*args):
    p = subprocess.run(['pactl', *map(str,args)], capture_output=True, text=True, timeout=5)
    if p.returncode:
        raise RuntimeError(p.stderr.strip() or 'تعذر الاتصال بنظام الصوت')
    return p.stdout.strip()

def devices(kind):
    return json.loads(pa('-f','json','list',kind))

class Bridge:
    """Add clip links alongside the physical mic, without moving app sources."""
    def __init__(self):
        self.modules=[];self.route_source=None;self.links={};self.mute_before=None
        self.blocked=False;self.include_voice=True;self.route_sources=[];self.mutes={}
    def cleanup_stale(self):
        for line in reversed(pa('list','short','modules').splitlines()):
            columns=line.split('\t')
            if len(columns)>2 and PREFIX in columns[2]:pa('unload-module',columns[0])
    def start(self,source,sink,hear=True,route_source=None):
        self.stop();self.route_sources=list(route_source) if isinstance(route_source,(list,tuple)) else [route_source or source];self.route_sources=[x for x in self.route_sources if x];self.route_source=self.route_sources[0] if self.route_sources else None;self.include_voice=bool(source)
        try:
            self.modules.append(pa('load-module','module-null-sink',f'sink_name={CLIPS}',
                'rate=48000','channels=1','channel_map=mono','sink_properties=device.description=Soundboard_Clips_Output'))
            if hear:self.modules.append(pa('load-module','module-loopback',f'source={CLIPS}.monitor',f'sink={sink}','latency_msec=30'))
        except Exception:self.stop();raise
    def graph(self):
        return json.loads(subprocess.check_output(['pw-dump'],text=True,timeout=5))
    def link(self,*args):
        subprocess.run(['pw-link',*map(str,args)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=3)
    def route_apps(self):
        if not self.modules or not self.route_source:return []
        sources={s['name']:s for s in devices('sources')}
        active_sources={sources[n]['index'] for n in self.route_sources if n in sources}
        if not active_sources:raise RuntimeError('Selected microphones are disconnected')
        graph=self.graph();ports={o['id']:o['info']['props'] for o in graph if o['type'].endswith(':Port')}
        nodes={o['id']:o['info']['props'] for o in graph if o['type'].endswith(':Node')}
        existing={(o['info']['output-port-id'],o['info']['input-port-id']):o for o in graph if o['type'].endswith(':Link')}
        clip_ports=[i for i,p in ports.items() if p.get('port.direction')=='out' and nodes.get(p.get('node.id'),{}).get('node.name')==CLIPS]
        if not clip_ports:return []
        out=clip_ports[0];wanted=set();apps=[]
        for stream in devices('source-outputs'):
            if stream.get('owner_module') is not None or stream.get('client') is None:continue
            if stream['source'] not in active_sources:continue
            props=stream.get('properties',{});serial=str(props.get('object.serial',''))
            node=next((i for i,p in nodes.items() if str(p.get('object.serial'))==serial),None)
            if node is None:continue
            incoming=[i for i,p in ports.items() if p.get('node.id')==node and p.get('port.direction')=='in']
            for dest in incoming:wanted.add((out,dest))
            apps.append(props.get('application.process.binary') or props.get('application.name','Audio app'))
        # Remove only links we created, validating port serials against ID reuse.
        for pair,identity in list(self.links.items()):
            current=tuple(ports.get(i,{}).get('object.serial') for i in pair)
            if pair not in wanted or current!=identity:
                if pair in existing and current==identity:
                    try:self.link('-d',*pair)
                    except Exception:pass
                del self.links[pair]
        for pair in wanted:
            if pair not in existing:
                try:self.link(*pair)
                except Exception:continue
                self.links[pair]=tuple(ports[i].get('object.serial') for i in pair)
        return sorted(set(apps))
    def block_voice(self,enabled):
        enabled=bool(enabled or (not self.include_voice and self.modules))
        if not self.route_sources or enabled==self.blocked:return
        if enabled:
            for source in self.route_sources:
                try:
                    self.mutes[source]=pa('get-source-mute',source).endswith('yes')
                    pa('set-source-mute',source,1)
                except Exception:pass
            self.blocked=True
        else:
            for source,muted in self.mutes.items():
                try:pa('set-source-mute',source,int(muted))
                except Exception:pass
            self.mutes={};self.blocked=False
    def stop(self):
        self.include_voice=True
        try:self.block_voice(False)
        except Exception:pass
        try:
            graph=self.graph();ports={o['id']:o['info']['props'] for o in graph if o['type'].endswith(':Port')}
            for pair,identity in self.links.items():
                if tuple(ports.get(i,{}).get('object.serial') for i in pair)==identity:
                    try:self.link('-d',*pair)
                    except Exception:pass
        except Exception:pass
        self.links={}
        for module in reversed(self.modules):
            try:pa('unload-module',module)
            except Exception:pass
        self.modules=[];self.route_source=None;self.route_sources=[];self.blocked=False;self.mute_before=None;self.mutes={}

class Player:
    def __init__(self):
        self.decode=self.playback=None
        self.errorfile=None
        self.paused=False
    def stop(self):
        for process in (self.playback,self.decode):
            if process and process.poll() is None:
                if self.paused:process.send_signal(signal.SIGCONT)
                process.terminate()
        for process in (self.playback,self.decode):
            if process:
                try:process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill();process.wait()
        self.decode=self.playback=None
        self.paused=False
        if self.errorfile:self.errorfile.close();self.errorfile=None
    def play(self,path,sink,volume,offset=0,filters=None):
        self.stop()
        self.errorfile=tempfile.TemporaryFile()
        try:
            command=['ffmpeg','-nostdin','-v','error','-ss',str(offset),'-i',str(path),'-vn']
            if filters:command+=['-af',filters]
            self.decode=subprocess.Popen(command+['-f','s16le','-ac','2','-ar','48000','pipe:1'],stdout=subprocess.PIPE,stderr=self.errorfile)
            self.playback=subprocess.Popen(['paplay','--raw','--format=s16le','--rate=48000','--channels=2','--latency-msec=40',f'--device={sink}',f'--volume={int(volume*65536/100)}','--client-name=soundpad-like','--stream-name=soundpad-like'],stdin=self.decode.stdout,stdout=subprocess.DEVNULL,stderr=self.errorfile)
            self.decode.stdout.close()
        except Exception:
            self.stop();raise
    def active(self):
        return self.playback is not None and self.playback.poll() is None
    def pause(self,paused):
        for process in (self.decode,self.playback):
            if process and process.poll() is None:process.send_signal(signal.SIGSTOP if paused else signal.SIGCONT)
        self.paused=paused
    def error(self):
        if self.errorfile:
            self.errorfile.seek(0);return self.errorfile.read().decode(errors='replace').strip()
        return ''
    def volume(self,value):
        if not self.active():return
        for stream in devices('sink-inputs'):
            props=stream.get('properties',{})
            if props.get('application.process.id')==str(self.playback.pid):
                pa('set-sink-input-volume',stream['index'],f'{value}%')
import copy, random, uuid, csv, threading, concurrent.futures
from datetime import datetime
from xml.etree import ElementTree as ET
from PyQt6 import QtSvg

DEFAULTS={'volume':70,'monitor_muted':False,'mic_volume':70,'voice_db':68.0,'normalization':'dynamic','fixed_db':-15.0,
 'block_voice':False,'activation':False,'activation_file':'','ptt_delay':0,'karaoke_delay':0,
 'duck':False,'duck_percent':80,'mode':'both','continue':False,'repeat_list':False,'repeat':False,
 'auto_stop':True,'link':True,'mic_voice':True,'global_keys':True,'alternating':False,
 'font_size':11,'dark_mode':False,'rec_rate':48000,'rec_folder':str(DATA/'recordings'),'editor':'',
 'show_all_categories':False,'notifications':False,'stop_hotkey':'','pause_hotkey':'','accent':'#3298ff'}
STYLE='''
QWidget {font-family:"Noto Sans","Noto Sans Arabic";font-size:11px;color:#000;background:#f4f4f4;}
QMainWindow,QTableWidget,QTreeWidget,QTextEdit,QLineEdit,QAbstractItemView {background:white;}
QMenuBar {background:white;spacing:0px;} QMenuBar::item {padding:2px 5px;}
QMenuBar::item:selected,QMenu::item:selected {background:#3298ff;color:white;}
QMenu {background:white;border:1px solid #aaa;padding:2px;}
QMenu::item {padding:3px 24px 3px 18px;} QMenu::separator {height:1px;background:#aaa;margin:3px;}
QMenu::item:disabled {color:#929292;}
QToolBar {spacing:1px;border-bottom:1px solid #aaa;background:#f5f5f5;padding:1px;}
QToolButton {border:0;padding:1px;background:transparent;} QToolButton:hover {background:#c8e3ff;}
QHeaderView::section {background:white;border:0;border-right:1px solid #bbb;padding:2px;text-align:left;}
QTableWidget,QTreeWidget {border:0;gridline-color:white;selection-background-color:#3298ff;selection-color:white;outline:0;}
QTableWidget::item,QTreeWidget::item {padding:2px;}
QSplitter::handle {background:#ddd;width:3px;}
QPushButton {background:#fafafa;border:1px solid #aaa;border-radius:2px;padding:4px 12px;min-height:18px;}
QPushButton:hover {background:#e4f1ff;border-color:#4a90ca;}
QPushButton:disabled {color:#aaa;}
QLineEdit,QComboBox,QSpinBox,QDoubleSpinBox,QKeySequenceEdit {background:white;border:1px solid #aaa;padding:2px;min-height:18px;}
QSlider::groove:horizontal {height:5px;background:#e6e6e6;border:1px solid white;}
QSlider::handle:horizontal {background:#0762a7;width:9px;margin:-5px 0;}
QSlider::sub-page:horizontal {background:#287fbb;}
QStatusBar {border-top:1px solid #aaa;background:#f4f4f4;}
QTabWidget::pane {background:white;border:1px solid #aaa;}
QTabBar::tab {background:#eee;border:1px solid #aaa;padding:3px 6px;}
QTabBar::tab:selected {background:white;border-bottom-color:white;}
QScrollArea,QScrollArea>QWidget>QWidget {background:white;border:0;}
QCheckBox,QRadioButton,QLabel {background:transparent;}
QCheckBox::indicator,QRadioButton::indicator {width:12px;height:12px;border:1px solid #888;background:white;}
QRadioButton::indicator {border-radius:7px;}
QCheckBox::indicator:checked,QRadioButton::indicator:checked {background:#0762a7;border:2px solid #bcdcff;}
QGroupBox {border:1px solid #aaa;margin-top:12px;padding-top:8px;background:white;}
QGroupBox::title {subcontrol-origin:margin;left:8px;padding:0 4px;}
QToolTip {background:#ffffdd;color:black;border:1px solid #777;padding:3px;}
'''
DARK_STYLE='''
QWidget {color:#e6e9ee;background:#25282e;}
QMainWindow,QTableWidget,QTreeWidget,QTextEdit,QPlainTextEdit,QLineEdit,QAbstractItemView {background:#1c1f24;color:#e6e9ee;alternate-background-color:#242931;}
QMenuBar,QMenu {background:#25282e;color:#e6e9ee;}
QMenu {border-color:#535b68;} QMenu::separator {background:#535b68;}
QMenuBar::item:selected,QMenu::item:selected {background:#276fbb;color:white;}
QToolBar,QStatusBar {background:#282d34;border-color:#4a515c;}
QHeaderView::section {background:#2c323b;color:#dfe6ef;border-color:#4a515c;}
QSplitter::handle {background:#444b56;}
QPushButton {background:#343b46;border-color:#606a78;color:#e6e9ee;}
QPushButton:hover,QToolButton:hover {background:#3d536e;}
QPushButton:disabled,QMenu::item:disabled {color:#7c8797;}
QLineEdit,QComboBox,QSpinBox,QDoubleSpinBox,QKeySequenceEdit {background:#1c222a;color:#e6e9ee;border-color:#596373;}
QTabWidget::pane {background:#25282e;border-color:#535b68;}
QTabBar::tab {background:#343b46;color:#e6e9ee;border-color:#535b68;}
QTabBar::tab:selected {background:#25282e;border-bottom-color:#25282e;}
QScrollArea,QScrollArea>QWidget>QWidget {background:#25282e;}
QSlider::groove:horizontal {background:#454e5b;border-color:#555f6d;}
QSlider::sub-page:horizontal {background:#3a91d8;}
QSlider::handle:horizontal {background:#70baff;}
QTableWidget,QTreeWidget {selection-background-color:#286db5;selection-color:white;}
QCheckBox,QRadioButton,QLabel {background:transparent;}
QCheckBox::indicator,QRadioButton::indicator {background:#1c222a;border-color:#9aa9bb;}
QCheckBox::indicator:checked,QRadioButton::indicator:checked {background:#70baff;border-color:#314b69;}
QGroupBox {background:#25282e;border-color:#535b68;}
QToolTip {background:#333d4a;color:#edf3fc;border-color:#667891;}
'''
def configure_fonts():
 app=W.QApplication.instance()
 if not app.property('soundpad_fonts_loaded'):
  roots=[Path(os.environ.get('APPDIR',''))/'app/fonts',Path(__file__).resolve().parents[1]/'assets/fonts',Path('/usr/share/fonts/noto')]
  for root in roots:
   for name in ['NotoSans-Regular.ttf','NotoSansArabic-Regular.ttf']:
    path=root/name
    if path.is_file():QtGui.QFontDatabase.addApplicationFont(str(path))
  app.setProperty('soundpad_fonts_loaded',True)
 font=QtGui.QFont();font.setFamilies(['Noto Sans','Noto Sans Arabic']);font.setPixelSize(12);app.setFont(font);W.QToolTip.setFont(font)

NUMPAD_KEYS={QtCore.Qt.Key.Key_Insert:'0',QtCore.Qt.Key.Key_End:'1',QtCore.Qt.Key.Key_Down:'2',QtCore.Qt.Key.Key_PageDown:'3',QtCore.Qt.Key.Key_Left:'4',QtCore.Qt.Key.Key_Clear:'5',QtCore.Qt.Key.Key_Right:'6',QtCore.Qt.Key.Key_Home:'7',QtCore.Qt.Key.Key_Up:'8',QtCore.Qt.Key.Key_PageUp:'9',QtCore.Qt.Key.Key_Delete:'.'}
# XKB keycodes used by Qt on Linux are evdev scan codes plus eight.
KEYPAD_SCANS={90:'0',87:'1',88:'2',89:'3',83:'4',84:'5',85:'6',79:'7',80:'8',81:'9',91:'.',86:'+',82:'-',63:'*',106:'/',104:'Enter'}
KEYPAD_SYMBOLS={**{0xffb0+n:str(n) for n in range(10)},0xff9e:'0',0xff9c:'1',0xff99:'2',0xff9b:'3',0xff96:'4',0xff9d:'5',0xff98:'6',0xff95:'7',0xff97:'8',0xff9a:'9',0xff9f:'.',0xffae:'.',0xffab:'+',0xffad:'-',0xffaa:'*',0xffaf:'/',0xff8d:'Enter'}
def normalized_key(event):
 key=event.key();mods=event.modifiers()
 digit=KEYPAD_SYMBOLS.get(event.nativeVirtualKey()) or KEYPAD_SCANS.get(event.nativeScanCode())
 if digit is not None:
  key=int(QtCore.Qt.Key.Key_Enter) if digit=='Enter' else ord(digit);mods|=QtCore.Qt.KeyboardModifier.KeypadModifier
 elif mods&QtCore.Qt.KeyboardModifier.KeypadModifier and key in NUMPAD_KEYS:key=ord(NUMPAD_KEYS[key])
 return key,mods

class HotkeyEdit(W.QKeySequenceEdit):
 def __init__(self,*args):
  super().__init__(*args)
  for child in self.findChildren(W.QLineEdit):child.installEventFilter(self)
 def capture(self,event):
  key,mods=normalized_key(event)
  if key in [QtCore.Qt.Key.Key_Control,QtCore.Qt.Key.Key_Shift,QtCore.Qt.Key.Key_Alt,QtCore.Qt.Key.Key_Meta]:return True
  if key in (QtCore.Qt.Key.Key_Backspace,QtCore.Qt.Key.Key_Delete) and not mods:self.clear();return True
  if mods&QtCore.Qt.KeyboardModifier.KeypadModifier:
   if key in NUMPAD_KEYS:key=ord(NUMPAD_KEYS[key])
  self.setKeySequence(QtGui.QKeySequence(QtCore.QKeyCombination(mods,QtCore.Qt.Key(key))));event.accept();return True
 def keyPressEvent(self,event):self.capture(event)
 def eventFilter(self,obj,event):
  if event.type()==QtCore.QEvent.Type.ShortcutOverride:event.accept();return True
  if event.type()==QtCore.QEvent.Type.KeyPress:return self.capture(event)
  if event.type()==QtCore.QEvent.Type.KeyRelease:return True
  return super().eventFilter(obj,event)

def apply_theme(dark):
 configure_fonts();app=W.QApplication.instance();asset=DATA/'check.svg';asset.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12"><path d="M2 6l3 3 5-6" fill="none" stroke="white" stroke-width="2"/></svg>');app.setStyleSheet(STYLE+(DARK_STYLE if dark else '')+'QCheckBox::indicator:checked {image:url("'+str(asset)+'");}')
 palette=app.style().standardPalette()
 for role,light,night in [
  ('Window','#f4f4f4','#25282e'),('Base','#ffffff','#1c1f24'),('AlternateBase','#f4f7fa','#242931'),
  ('Text','#000000','#e6e9ee'),('WindowText','#000000','#e6e9ee'),('Button','#f4f4f4','#343b46'),
  ('ButtonText','#000000','#e6e9ee'),('Highlight','#3298ff','#286db5'),('HighlightedText','#ffffff','#ffffff')]:
  palette.setColor(getattr(QtGui.QPalette.ColorRole,role),QtGui.QColor(night if dark else light))
 app.setPalette(palette)

class SeekSlider(W.QSlider):
 """Absolute pointer seek using the actual styled handle travel, not page steps."""
 def value_at(self,x):
  option=W.QStyleOptionSlider();self.initStyleOption(option)
  groove=self.style().subControlRect(W.QStyle.ComplexControl.CC_Slider,option,W.QStyle.SubControl.SC_SliderGroove,self)
  handle=self.style().subControlRect(W.QStyle.ComplexControl.CC_Slider,option,W.QStyle.SubControl.SC_SliderHandle,self)
  span=max(1,groove.width()-handle.width())
  fraction=max(0.0,min(1.0,(x-groove.x()-handle.width()/2)/span))
  if option.upsideDown:fraction=1-fraction
  return round(self.minimum()+fraction*(self.maximum()-self.minimum()))
 def mousePressEvent(self,event):
  if event.button()==QtCore.Qt.MouseButton.LeftButton:
   self.setFocus();self.setSliderDown(True);self.setValue(self.value_at(event.position().x()));event.accept()
  else:super().mousePressEvent(event)
 def mouseMoveEvent(self,event):
  if self.isSliderDown():self.setValue(self.value_at(event.position().x()));event.accept()
  else:super().mouseMoveEvent(event)
 def mouseReleaseEvent(self,event):
  if event.button()==QtCore.Qt.MouseButton.LeftButton and self.isSliderDown():
   self.setValue(self.value_at(event.position().x()));self.setSliderDown(False);event.accept()
  else:super().mouseReleaseEvent(event)

SHAPES={
 'play':'<path d="M5 3 L21 12 L5 21 Z"/>',
 'stop':'<rect x="4" y="4" width="16" height="16"/>',
 'pause':'<path d="M5 3h5v18H5zM14 3h5v18h-5z"/>',
 'next':'<path d="M3 4l13 8L3 20zM17 4h4v16h-4z"/>',
 'prev':'<path d="M21 4L8 12l13 8zM3 4h4v16H3z"/>',
 'home':'<path d="M1 11L12 1l11 10-3 3-2-2v11h-5v-8h-3v8H5V12l-2 2z"/>',
 'headphones':'<path fill="none" stroke="#0762a7" stroke-width="2.8" d="M3 14V11a9 9 0 0118 0v3"/><rect x="2" y="12" width="5" height="9" rx="2"/><rect x="17" y="12" width="5" height="9" rx="2"/>',
 'mic':'<rect x="9" y="1" width="6" height="13" rx="3"/><path fill="none" stroke="#0762a7" stroke-width="2" d="M6 10v3a6 6 0 0012 0v-3M12 19v3M8 22h8"/>',
 'wrench':'<path d="M21 2l-4 4 1 3 3 1 3-4c1 6-3 10-8 8L6 23l-4-4 10-9c-2-6 2-10 9-8z"/>',
 'save':'<path d="M3 2h16l3 3v17H2V2zm3 1v6h12V3zm0 11v7h12v-7z"/>',
 'repeat':'<path fill="none" stroke="#0762a7" stroke-width="2" d="M4 8h15l-4-4m4 4l-4 4M20 16H5l4 4m-4-4l4-4"/>',
 'add':'<path d="M10 3h4v7h7v4h-7v7h-4v-7H3v-4h7z"/>'}
def icon(name):
 svg='<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24"><g fill="#0762a7">'+SHAPES.get(name,SHAPES['play'])+'</g></svg>'
 pix=QtGui.QPixmap(24,24);pix.fill(QtCore.Qt.GlobalColor.transparent);p=QtGui.QPainter(pix);QtSvg.QSvgRenderer(svg.encode()).render(p);p.end();return QtGui.QIcon(pix)
APP_LOGO='<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 256 256"><rect x="12" y="12" width="232" height="232" rx="56" fill="#db243b"/><path d="M91 67L187 128L91 189Z" fill="white"/><path d="M51 99v58M66 86v84" stroke="white" stroke-width="9" stroke-linecap="round"/></svg>'
def app_icon():
 pix=QtGui.QPixmap(256,256);pix.fill(QtCore.Qt.GlobalColor.transparent);p=QtGui.QPainter(pix);QtSvg.QSvgRenderer(APP_LOGO.encode()).render(p);p.end();return QtGui.QIcon(pix)
def duration(seconds):
 s=max(0,int(seconds or 0));return f'{s//60}:{s%60:02d}'
def entry(path,**kwargs):
 return dict(id=uuid.uuid4().hex,path=str(path),tag=Path(path).stem,category='My Sounds',hotkey='',color='',duration=0,plays=0,**kwargs)
def probe(path):
 try:return float(subprocess.check_output(['ffprobe','-v','error','-show_entries','format=duration','-of','default=noprint_wrappers=1:nokey=1',path],stderr=subprocess.DEVNULL,timeout=12).strip())
 except Exception:return 0

class Hotkeys(QtCore.QObject):
 def __init__(self,window):
  super().__init__(window);self.window=window;self.actions={};self.local=[];self.error=''
  from PyQt6 import QtDBus
  self.qt=QtDBus;self.bus=QtDBus.QDBusConnection.sessionBus()
  self.bus.connect('org.kde.kglobalaccel','','org.kde.kglobalaccel.Component','globalShortcutPressed',self.pressed)
  try:self.call('allMainComponents')
  except Exception as ex:self.error=str(ex);self.bus=None
 def call(self,name,*args):
  m=self.qt.QDBusMessage.createMethodCall('org.kde.kglobalaccel','/kglobalaccel','org.kde.KGlobalAccel',name);m.setArguments(list(args));r=self.bus.call(m)
  if r.type()==self.qt.QDBusMessage.MessageType.ErrorMessage:raise RuntimeError(r.errorMessage())
  return r.arguments()[0] if r.arguments() else None
 @QtCore.pyqtSlot(str,str,'qlonglong')
 def pressed(self,component,action,timestamp):
  if component=='ziyad_soundboard' and action in self.actions:self.actions[action]()
 def clear(self):
  if self.bus:
   for name in self.actions:
    try:self.call('unregister','ziyad_soundboard',name)
    except Exception:pass
  self.actions={}
  for shortcut in self.local:shortcut.setEnabled(False);shortcut.deleteLater()
  self.local=[]
 def install(self):
  self.clear();errors=[]
  specs=[(e['id'],e['hotkey'],e['tag'],lambda ident=e['id']:self.window.play_id(ident)) for e in self.window.entries if e.get('hotkey')]
  specs.extend([('stop',self.window.prefs['stop_hotkey'],'Stop',self.window.stop),('pause',self.window.prefs['pause_hotkey'],'Pause',self.window.pause)])
  seen=set()
  for name,key,label,fn in specs:
   if not key:continue
   if key in seen:errors.append('Duplicate shortcut: '+key);continue
   seen.add(key);seq=QtGui.QKeySequence(key)
   if self.bus and self.window.prefs['global_keys']:
    try:
     code=seq[0].toCombined()
     if not self.call('isGlobalShortcutAvailable',code,'ziyad_soundboard'):raise RuntimeError('Already used by another application: '+key)
     aid=self.qt.QDBusArgument(['ziyad_soundboard',name,'soundpad like',label],QtCore.QMetaType.Type.QStringList.value)
     keys=self.qt.QDBusArgument();keys.beginArray(QtCore.QMetaType.Type.Int.value);keys.add(code);keys.endArray()
     self.call('doRegister',aid);accepted=self.call('setShortcut',aid,keys,self.qt.QDBusArgument(6,QtCore.QMetaType.Type.UInt.value))
     if code not in accepted:raise RuntimeError('Shortcut was not accepted: '+key)
     self.actions[name]=fn
    except Exception as ex:errors.append(str(ex))
   else:
    shortcut=QtGui.QShortcut(seq,self.window);shortcut.activated.connect(fn);self.local.append(shortcut)
  return errors
 def close(self):
  self.clear()
  if self.bus:self.bus.disconnect('org.kde.kglobalaccel','','org.kde.kglobalaccel.Component','globalShortcutPressed',self.pressed)

class Preferences(W.QDialog):
 def __init__(self,owner,tab=0):
  super().__init__(owner);self.owner=owner;self.setWindowTitle('Preferences');self.resize(610,860);self.values={}
  layout=W.QVBoxLayout(self);layout.setContentsMargins(6,6,6,6);self.tabs=W.QTabWidget();layout.addWidget(self.tabs)
  self.audio();self.hotkeys();self.interface();self.device_tab();self.recorder();self.editor();self.notifications()
  row=W.QHBoxLayout();reset=W.QPushButton('Reset');reset.clicked.connect(self.reset);row.addWidget(reset);row.addStretch()
  for label,fn in [('OK',self.accept_apply),('Cancel',self.reject),('Apply',self.apply)]:
   b=W.QPushButton(label);b.clicked.connect(fn);row.addWidget(b)
  layout.addLayout(row);self.tabs.setCurrentIndex(tab)
 def page(self,name):
  content=W.QWidget();v=W.QVBoxLayout(content);v.setContentsMargins(14,12,14,12);v.setSpacing(10)
  scroll=W.QScrollArea();scroll.setWidgetResizable(True);scroll.setWidget(content);self.tabs.addTab(scroll,name);return v
 def heading(self,v,text,desc=''):
  label=W.QLabel(text);label.setStyleSheet('font-size:16px;font-weight:bold;');v.addWidget(label)
  if desc:
   label=W.QLabel(desc);label.setWordWrap(True);v.addWidget(label)
 def check(self,v,key,text):
  w=W.QCheckBox(text);w.setChecked(bool(self.owner.prefs[key]));self.values[key]=w;v.addWidget(w);return w
 def spin(self,v,key,label,low,high,suffix='',decimals=0):
  row=W.QHBoxLayout();row.addWidget(W.QLabel(label));w=W.QDoubleSpinBox() if decimals else W.QSpinBox();w.setRange(low,high)
  if decimals:w.setDecimals(decimals)
  w.setValue(self.owner.prefs[key]);w.setSuffix(suffix);row.addWidget(w);row.addStretch();v.addLayout(row);self.values[key]=w;return w
 def path(self,v,key,label,file=True):
  v.addWidget(W.QLabel(label));row=W.QHBoxLayout();w=W.QLineEdit(self.owner.prefs[key]);self.values[key]=w;row.addWidget(w);button=W.QPushButton('Browse');row.addWidget(button)
  def browse():
   value=W.QFileDialog.getOpenFileName(self,label)[0] if file else W.QFileDialog.getExistingDirectory(self,label)
   if value:w.setText(value)
  button.clicked.connect(browse);v.addLayout(row);return w
 def audio(self):
  v=self.page('Audio');self.heading(v,'Volume normalization','Adjusts sound file volume so your voice and clips are easier to balance.')
  self.spin(v,'voice_db','Voice volume:',40,86,' dB',1).setToolTip('Relative reference: 68 dB maps to a -20 dBFS RMS target; not an acoustic microphone measurement.')
  self.dynamic=W.QRadioButton('Dynamic volume adjustment (recommended)');self.fixed=W.QRadioButton('Fixed volume adjustment (advanced)')
  self.dynamic.setChecked(self.owner.prefs['normalization']=='dynamic');self.fixed.setChecked(not self.dynamic.isChecked());v.addWidget(self.dynamic);v.addWidget(self.fixed)
  fixed=self.spin(v,'fixed_db','Fixed gain:',-60,20,' dB',1);fixed.setEnabled(self.fixed.isChecked());self.fixed.toggled.connect(fixed.setEnabled)
  self.heading(v,'Block voice');self.check(v,'block_voice','Block voice while playing sounds on microphone')
  self.heading(v,'Play voice activation sound','Plays the selected activation sound before the main clip.');self.check(v,'activation','Enable');self.path(v,'activation_file','Activation sound:')
  self.heading(v,'Push-to-Talk delay','Delays playback to give you time to press your voice app’s Push-to-Talk key.');self.spin(v,'ptt_delay','Delay in ms:',0,10000,' ms')
  self.heading(v,'Karaoke','Starts the microphone copy later than the speaker copy.');self.spin(v,'karaoke_delay','Delay sound on microphone:',0,10000,' ms')
  self.heading(v,'Reduce volume of other applications','Restores the original volume when playback stops.');self.check(v,'duck','Enable')
  self.duck=W.QComboBox()
  for text,value in [('Mute all other sounds',100),('Reduce volume of other sounds by 80%',80),('Reduce volume of other sounds by 50%',50)]:self.duck.addItem(text,value)
  self.duck.setCurrentIndex(max(0,self.duck.findData(self.owner.prefs['duck_percent'])));v.addWidget(self.duck);v.addStretch()
 def hotkeys(self):
  v=self.page('Hotkeys');self.heading(v,'Hotkeys','Assign per-sound keys from the Hotkey column or right-click → Set hotkey.')
  self.check(v,'global_keys','Use global shortcuts (KDE / Wayland)')
  for key,label in [('stop_hotkey','Stop playback'),('pause_hotkey','Pause / resume')]:
   row=W.QHBoxLayout();row.addWidget(W.QLabel(label));w=HotkeyEdit(QtGui.QKeySequence(self.owner.prefs[key]));row.addWidget(w);v.addLayout(row);self.values[key]=w
  v.addWidget(W.QLabel('Global shortcut service: '+('available' if self.owner.hotkeys.bus else 'unavailable; keys work in this window')));v.addStretch()
 def interface(self):
  v=self.page('Interface');self.heading(v,'Appearance');self.check(v,'dark_mode','Dark mode');self.heading(v,'Sound list');self.check(v,'alternating','Alternating row colors');self.spin(v,'font_size','Font size:',9,18,' px')
  self.heading(v,'Default play mode');self.mode=W.QComboBox()
  for label,key in [('Speakers and microphone','both'),('Speakers only','speakers'),('Microphone only','mic')]:self.mode.addItem(label,key)
  self.mode.setCurrentIndex(self.mode.findData(self.owner.prefs['mode']));v.addWidget(self.mode);v.addStretch()
 def device_tab(self):
  v=self.page('Devices');self.heading(v,'Playback device');self.out=W.QComboBox();v.addWidget(self.out)
  self.heading(v,'Recording device','Choose your physical microphone here. Keep Discord on that same microphone; routing is automatic.');self.mic=W.QComboBox();v.addWidget(self.mic)
  try:
   for kind,combo,selected in [('sinks',self.out,self.owner.output),('sources',self.mic,self.owner.source)]:
    for d in devices(kind):
     if d['name'].startswith((PREFIX,'codex_soundpad_')) or d['name'].endswith('.monitor'):continue
     combo.addItem(d.get('description',d['name']),d['name'])
    i=combo.findData(selected)
    if i>=0:combo.setCurrentIndex(i)
  except Exception as e:v.addWidget(W.QLabel(str(e)))
  self.check(v,'link','Automatically connect applications using the selected microphone');self.check(v,'mic_voice','Include physical microphone voice');v.addWidget(W.QLabel(self.owner.route_label.text()));v.addStretch()
 def recorder(self):
  v=self.page('Recorder');self.heading(v,'Recording');self.path(v,'rec_folder','Save recordings in:',False);self.spin(v,'rec_rate','Sample rate:',8000,96000,' Hz');v.addWidget(W.QLabel('Recordings are saved as uncompressed WAV files.'));v.addStretch()
 def editor(self):
  v=self.page('Editor');self.heading(v,'Sound editor','Edit file opens the built-in trim editor. You can optionally choose an external editor executable.');self.path(v,'editor','External editor:');v.addStretch()
 def notifications(self):
  v=self.page('Notifications');self.heading(v,'Playback notifications');self.check(v,'notifications','Show a desktop notification when a clip starts');v.addStretch()
 def collect(self):
  p=copy.deepcopy(self.owner.prefs)
  for key,w in self.values.items():
   if isinstance(w,W.QCheckBox):p[key]=w.isChecked()
   elif isinstance(w,(W.QSpinBox,W.QDoubleSpinBox)):p[key]=w.value()
   elif isinstance(w,W.QKeySequenceEdit):p[key]=w.keySequence().toString()
   else:p[key]=w.text()
  p.update(normalization='dynamic' if self.dynamic.isChecked() else 'fixed',duck_percent=self.duck.currentData(),mode=self.mode.currentData());return p
 def apply(self):
  p=self.collect()
  if p['activation'] and not Path(p['activation_file']).is_file():W.QMessageBox.warning(self,'Activation sound','Choose an existing activation sound first.');return False
  self.owner.stop();self.owner.prefs=p;apply_theme(p['dark_mode']);self.owner.output=self.out.currentData();self.owner.source=self.mic.currentData();self.owner.configure_bridge();self.owner.save();self.owner.render();self.owner.set_mode(p['mode']);self.owner.install_hotkeys();return True
 def accept_apply(self):
  if self.apply():self.accept()
 def reset(self):
  for key,w in self.values.items():
   val=DEFAULTS[key]
   if isinstance(w,W.QCheckBox):w.setChecked(val)
   elif isinstance(w,(W.QSpinBox,W.QDoubleSpinBox)):w.setValue(val)
   elif isinstance(w,W.QKeySequenceEdit):w.setKeySequence(QtGui.QKeySequence(val))
   else:w.setText(val)
  self.dynamic.setChecked(True);self.duck.setCurrentIndex(1);self.mode.setCurrentIndex(0)

class Window(W.QMainWindow):
 def __init__(self):
  super().__init__();self.setWindowTitle('soundpad like');self.setWindowIcon(app_icon());self.resize(790,560);self.setMinimumSize(660,450);self.setAcceptDrops(True)
  try:raw=json.loads(CONFIG.read_text())
  except Exception:raw={}
  self.prefs={**DEFAULTS,**raw.get('prefs',{})};self.prefs['volume']=raw.get('volume',self.prefs['volume']);self.prefs['mic_volume']=raw.get('prefs',{}).get('mic_volume',self.prefs['volume']);apply_theme(self.prefs['dark_mode'])
  self.entries=raw.get('entries') or [entry(p) for p in raw.get('files',[])]
  self.categories=raw.get('categories',['My Sounds']);self.current_category='My Sounds';self.output=(pa('get-default-sink') if self.prefs.get('default_output') else raw.get('output')) or pa('get-default-sink');self.source=raw.get('input') or pa('get-default-source')
  if self.source.startswith(PREFIX):self.source=next((d['name'] for d in devices('sources') if d['name'].startswith('alsa_input.')),None)
  self.recent_lists=raw.get('recent_lists',[]);self.list_path=raw.get('list_path','');self.history=raw.get('history',[]);self.undo_stack=[];self.redo_stack=[];self.clipboard=[]
  self.bridge=Bridge();self.players=[];self.now=None;self.current_mode='both';self.position=0;self.started=0;self.paused=False;self.pause_time=0;self.ducked={};self.token=0;self.activation_pending=None
  self.pool=concurrent.futures.ThreadPoolExecutor(max_workers=2);self.pending={};self.dialogs=[];self.dirty=False
  self.build();self.hotkeys=Hotkeys(self);self.render();self.install_hotkeys();self.set_mode(self.prefs['mode'])
  self.timer=QtCore.QTimer(self);self.timer.timeout.connect(self.tick);self.timer.start(100)
  self.progress_timer=QtCore.QTimer(self);self.progress_timer.setTimerType(QtCore.Qt.TimerType.PreciseTimer);self.progress_timer.timeout.connect(self.update_progress);self.progress_timer.start(16)
  self.route_timer=QtCore.QTimer(self);self.route_timer.timeout.connect(self.route_apps);self.route_timer.start(500)
  QtCore.QTimer.singleShot(0,self.configure_bridge)
  self.backup()
 def act(self,menu,text,fn,key=None,check=None,ico=None):
  a=QtGui.QAction(icon(ico) if ico else QtGui.QIcon(),text,self)
  if key:a.setShortcut(QtGui.QKeySequence(key))
  if check is not None:a.setCheckable(True);a.setChecked(check)
  a.triggered.connect(fn);menu.addAction(a);return a
 def build(self):
  self.build_menus();bar=W.QToolBar();bar.setMovable(False);bar.setIconSize(QtCore.QSize(18,18));self.addToolBar(bar)
  for name,tip,fn in [('play','Play selected file',self.play_selected),('headphones','Play on speakers',lambda:self.play_selected('speakers')),('mic','Play on microphone',lambda:self.play_selected('mic')),('pause','Pause / resume',self.pause),('stop','Stop',self.stop),('prev','Previous',lambda:self.step(-1)),('next','Next',lambda:self.step(1))]:
   a=bar.addAction(icon(name),tip);a.triggered.connect(lambda checked=False,f=fn:f())
  bar.addSeparator();self.clock=W.QLabel(' 0:00 ');bar.addWidget(self.clock);self.seek=SeekSlider(QtCore.Qt.Orientation.Horizontal);self.seek.setRange(0,1000000);self.seek.setMinimumWidth(180);self.seek.setSizePolicy(W.QSizePolicy.Policy.Expanding,W.QSizePolicy.Policy.Preferred);bar.addWidget(self.seek);self.seek.sliderReleased.connect(self.seek_to)
  bar.addSeparator();self.monitor_action=bar.addAction(icon('headphones'),'Mute headphones only');self.monitor_action.setCheckable(True);self.monitor_action.setChecked(self.prefs['monitor_muted']);self.monitor_action.toggled.connect(self.set_monitor_mute)
  self.volume=SeekSlider(QtCore.Qt.Orientation.Horizontal);self.volume.setRange(0,100);self.volume.setValue(self.prefs['volume']);self.volume.setFixedWidth(100);bar.addWidget(self.volume);self.volume.valueChanged.connect(self.set_volume);self.refresh_monitor_icon()
  central=W.QWidget();v=W.QVBoxLayout(central);v.setContentsMargins(0,0,0,0);v.setSpacing(0);self.setCentralWidget(central)
  self.search=W.QLineEdit();self.search.setPlaceholderText('Search sounds…  (Esc to stop playback)');self.search.setVisible(False);self.search.textChanged.connect(self.render_table);v.addWidget(self.search)
  self.split=W.QSplitter();v.addWidget(self.split,1)
  self.tree=W.QTreeWidget();self.tree.setHeaderLabels(['Category','#']);self.tree.header().setStretchLastSection(False);self.tree.setRootIsDecorated(False);self.tree.setColumnWidth(0,145);self.tree.header().setSectionResizeMode(0,W.QHeaderView.ResizeMode.Stretch);self.tree.header().setSectionResizeMode(1,W.QHeaderView.ResizeMode.Fixed);self.tree.setColumnWidth(1,30);self.tree.setMinimumWidth(130);self.tree.itemSelectionChanged.connect(self.category_changed);self.tree.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu);self.tree.customContextMenuRequested.connect(self.category_menu);self.split.addWidget(self.tree)
  right=W.QWidget();rv=W.QVBoxLayout(right);rv.setContentsMargins(0,0,0,0);rv.setSpacing(0);self.split.addWidget(right);self.split.setSizes([180,600])
  self.table=SoundTable(self);self.table.setHorizontalHeaderLabels(['Index','Tag','Duration','Hotkey']);self.table.verticalHeader().hide();self.table.setShowGrid(False);self.table.setSelectionBehavior(W.QAbstractItemView.SelectionBehavior.SelectRows);self.table.setSelectionMode(W.QAbstractItemView.SelectionMode.ExtendedSelection);self.table.setEditTriggers(W.QAbstractItemView.EditTrigger.NoEditTriggers)
  h=self.table.horizontalHeader();h.setDefaultAlignment(QtCore.Qt.AlignmentFlag.AlignLeft|QtCore.Qt.AlignmentFlag.AlignVCenter);font=h.font();font.setBold(False);h.setFont(font);h.setSectionResizeMode(0,W.QHeaderView.ResizeMode.Fixed);h.setSectionResizeMode(1,W.QHeaderView.ResizeMode.Stretch);self.table.setColumnWidth(0,43);self.table.setColumnWidth(2,52);self.table.setColumnWidth(3,110);self.table.verticalHeader().setDefaultSectionSize(21)
  self.table.itemDoubleClicked.connect(lambda _:self.play_selected());self.table.itemSelectionChanged.connect(self.summary);self.table.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu);self.table.customContextMenuRequested.connect(self.context_menu);rv.addWidget(self.table,1)
  summary=W.QFrame();summary.setFixedHeight(65);sv=W.QHBoxLayout(summary);sv.setContentsMargins(8,4,4,4);home=W.QLabel();home.setPixmap(icon('home').pixmap(24,24).scaled(48,48,QtCore.Qt.AspectRatioMode.KeepAspectRatio,QtCore.Qt.TransformationMode.SmoothTransformation));sv.addWidget(home);self.summary_text=W.QLabel();sv.addWidget(self.summary_text,1);button=W.QToolButton();button.setIcon(icon('wrench'));button.clicked.connect(self.preferences);sv.addWidget(button);rv.addWidget(summary)
  self.hotbar=W.QToolBar();self.hotbar.setVisible(False);v.addWidget(self.hotbar)
  sb=self.statusBar();sb.setSizeGripEnabled(False);self.mode_button=W.QToolButton();self.mode_button.setIcon(icon('play'));self.mode_button.setToolTip('Play mode');self.mode_button.clicked.connect(self.cycle_mode);sb.addWidget(self.mode_button)
  self.hotkey_button=W.QToolButton();self.hotkey_button.setText('H');self.hotkey_button.setToolTip('Enable / disable hotkeys');self.hotkey_button.setCheckable(True);self.hotkey_button.setChecked(True);self.hotkey_button.toggled.connect(lambda _:self.install_hotkeys());sb.addWidget(self.hotkey_button)
  self.status=W.QLabel('Ready');sb.addWidget(self.status,1);self.route_label=W.QLabel('');self.route_label.setMaximumWidth(210);sb.addPermanentWidget(self.route_label);self.repeat_button=W.QToolButton();self.repeat_button.setIcon(icon('repeat'));self.repeat_button.setToolTip('Repeat current file');self.repeat_button.setCheckable(True);self.repeat_button.setChecked(self.prefs['repeat']);self.repeat_button.toggled.connect(lambda b:self.flag('repeat',b));sb.addPermanentWidget(self.repeat_button)
  for key,fn in [('Return',self.play_selected),('Space',self.pause),('Escape',self.stop),('F2',self.rename_file),('Shift+F2',self.edit_tag),('Ctrl+E',self.edit_file)]:
   s=QtGui.QShortcut(QtGui.QKeySequence(key),self);s.activated.connect(fn)
 def build_menus(self):
  file=self.menuBar().addMenu('File');self.act(file,'Add sound files',self.add,'Ctrl+O');file.addSeparator();self.act(file,'New sound list',self.new_list);self.act(file,'Load sound list',self.load_list);self.act(file,'Save sound list',self.save_list,'Ctrl+S');self.act(file,'Save sound list as…',lambda:self.save_list(True))
  self.recent_menu=file.addMenu('Load recent sound list');self.recent_menu.aboutToShow.connect(self.fill_recent)
  self.restore_menu=file.addMenu('Restore sound list');self.restore_menu.aboutToShow.connect(self.fill_backups)
  export=file.addMenu('Export');self.act(export,'Sound list (CSV)',self.export_csv);self.act(export,'Selected sound files',self.export_files)
  stats=file.addMenu('Stats');self.act(stats,'Show statistics',self.stats);self.act(stats,'Reset play counts',self.reset_stats);file.addSeparator();self.act(file,'Preferences',self.preferences,ico='wrench');file.addSeparator();self.act(file,'Exit',self.close,'Alt+F4')
  edit=self.menuBar().addMenu('Edit');self.undo_action=self.act(edit,'Undo',self.undo,'Ctrl+Z');self.redo_action=self.act(edit,'Redo',self.redo,'Ctrl+Y');edit.addSeparator();self.act(edit,'Cut',lambda:self.copy_entries(True),'Ctrl+X');self.act(edit,'Copy',self.copy_entries,'Ctrl+C');self.act(edit,'Paste',self.paste,'Ctrl+V');self.act(edit,'Remove selected entries',self.remove,'Del');self.act(edit,'Shuffle list',self.shuffle);edit.addSeparator();self.act(edit,'Select all',self.table_select_all,'Ctrl+A');self.select_recent=edit.addMenu('Select recently played file');self.select_recent.aboutToShow.connect(lambda:self.fill_history(self.select_recent,False));self.act(edit,'Find dead entries',self.find_dead);edit.addSeparator();self.act(edit,'Search',self.show_search,'Ctrl+F')
  play=self.menuBar().addMenu('Play')
  for text,mode in [('Play selected file (speakers and microphone)','both'),('Play selected file on speakers only','speakers'),('Play selected file on microphone only','mic')]:self.act(play,text,lambda checked=False,m=mode:self.play_selected(m))
  play.addSeparator()
  for text,mode in [('Play random file (speakers and microphone)','both'),('Play random file on speakers only','speakers'),('Play random file on microphone only','mic')]:self.act(play,text,lambda checked=False,m=mode:self.random_play(m))
  play.addSeparator();allrandom=play.addMenu('Play random file from all categories')
  for text,mode in [('Speakers and microphone','both'),('Speakers only','speakers'),('Microphone only','mic')]:self.act(allrandom,text,lambda checked=False,m=mode:self.random_play(m,True))
  self.play_recent=play.addMenu('Play recently played file (speakers and microphone)');self.play_recent.aboutToShow.connect(lambda:self.fill_history(self.play_recent,True));play.addSeparator()
  for text,fn in [('Stop playback',self.stop),('Pause/resume playback',self.pause),('Play previous file',lambda:self.step(-1)),('Play next file',lambda:self.step(1))]:self.act(play,text,fn)
  play.addSeparator();self.flag_actions={}
  for text,key in [('Continue playback after current file','continue'),('Repeat playback of sound list','repeat_list'),('Repeat playback of current file','repeat'),('Auto stop (recommended)','auto_stop')]:self.flag_actions[key]=self.act(play,text,lambda checked,k=key:self.flag(k,checked),check=self.prefs[key])
  play.addSeparator();modes=play.addMenu('Play mode');self.mode_actions={};group=QtGui.QActionGroup(self)
  for text,mode in [('Speakers and microphone','both'),('Speakers only','speakers'),('Microphone only','mic')]:
   a=self.act(modes,text,lambda checked=False,m=mode:self.set_mode(m),check=self.prefs['mode']==mode);group.addAction(a);self.mode_actions[mode]=a
  win=self.menuBar().addMenu('Window');cats=win.addMenu('Categories');self.act(cats,'Show categories',lambda b:self.tree.setVisible(b),check=True);self.act(cats,'Add category',self.add_category);self.act(cats,'Show All sounds category',self.toggle_all,check=self.prefs['show_all_categories'])
  hotbar=win.addMenu('Hotbar');self.act(hotbar,'Show hotbar',lambda b:self.hotbar.setVisible(b),check=False)
  keys=win.addMenu('Hotkeys');self.act(keys,'Manage hotkeys',lambda:self.preferences(1));self.act(win,'Sound recorder',self.recorder,'Ctrl+R');self.act(win,'Color tool',self.color);self.act(win,'Text to speech',self.tts,'Ctrl+T')
  helpmenu=self.menuBar().addMenu('Help');self.act(helpmenu,'How to use',self.help);self.act(helpmenu,'About',lambda:W.QMessageBox.about(self,'soundpad like','Native Linux soundboard.\nIndependent implementation with a classic Soundpad-style layout.\nNot affiliated with Leppsoft.'))
 def table_select_all(self):self.table.selectAll()
 def state(self):return copy.deepcopy((self.entries,self.categories))
 def checkpoint(self):self.undo_stack.append(self.state());self.undo_stack=self.undo_stack[-50:];self.redo_stack.clear()
 def undo(self):
  if self.undo_stack:self.redo_stack.append(self.state());self.entries,self.categories=self.undo_stack.pop();self.changed()
 def redo(self):
  if self.redo_stack:self.undo_stack.append(self.state());self.entries,self.categories=self.redo_stack.pop();self.changed()
 def changed(self):self.dirty=True;self.render();self.save();self.install_hotkeys()
 def save(self):
  data={'version':2,'entries':self.entries,'files':[e['path'] for e in self.entries],'categories':self.categories,'prefs':self.prefs,'volume':self.prefs['volume'],'output':self.output,'input':self.source,'history':self.history[-30:],'list_path':self.list_path,'recent_lists':self.recent_lists[-10:]}
  tmp=CONFIG.with_suffix('.tmp');tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2));tmp.replace(CONFIG)
 def backup(self):
  folder=DATA/'backups';folder.mkdir(exist_ok=True)
  if CONFIG.exists():shutil.copy2(CONFIG,folder/(datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'.json'))
  for p in sorted(folder.glob('*.json'))[:-15]:p.unlink()
 def render(self):
  self.tree.blockSignals(True);self.tree.clear()
  for category in (['All sounds'] if self.prefs['show_all_categories'] or self.current_category=='All sounds' else [])+self.categories:
   count=len(self.entries) if category=='All sounds' else sum(e['category']==category for e in self.entries)
   item=W.QTreeWidgetItem([category,str(count)]);item.setIcon(0,icon('home'));self.tree.addTopLevelItem(item)
   if category==self.current_category:self.tree.setCurrentItem(item)
  if not self.tree.currentItem():self.tree.setCurrentItem(self.tree.topLevelItem(0));self.current_category='My Sounds'
  self.tree.blockSignals(False);self.table.setAlternatingRowColors(self.prefs['alternating']);self.table.setStyleSheet(f'font-size:{round(self.prefs["font_size"]*self.prefs.get("zoom",100)/100)}px;');self.render_table();self.undo_action.setEnabled(bool(self.undo_stack));self.redo_action.setEnabled(bool(self.redo_stack));self.refresh_hotbar()
 def visible(self):
  q=self.search.text().casefold()
  return [e for e in self.entries if (self.current_category=='All sounds' or e['category']==self.current_category) and (q in e['tag'].casefold() or q in Path(e['path']).name.casefold())]
 def render_table(self):
  selected={e['id'] for e in self.selected()} if self.table.rowCount() else set();self.table.blockSignals(True);self.table.setRowCount(0)
  for row,e in enumerate(self.visible()):
   self.table.insertRow(row)
   for col,value in enumerate([str(self.entries.index(e)+1),e['tag'],duration(e.get('duration')),e.get('hotkey','')]):
    item=W.QTableWidgetItem(value);item.setData(QtCore.Qt.ItemDataRole.UserRole,e['id']);item.setToolTip(e['path'])
    if e.get('color'):item.setBackground(QtGui.QColor(e['color']))
    if not Path(e['path']).is_file():item.setForeground(QtGui.QColor('#b22'))
    self.table.setItem(row,col,item)
   if e['id'] in selected:self.table.selectionModel().select(self.table.model().index(row,0),QtCore.QItemSelectionModel.SelectionFlag.Select|QtCore.QItemSelectionModel.SelectionFlag.Rows)
   if not e.get('duration') and not e.get('probed') and e['path'] not in self.pending and Path(e['path']).is_file():self.pending[e['path']]=self.pool.submit(probe,e['path'])
  if not self.table.selectedItems() and self.table.rowCount():self.table.selectRow(0)
  self.table.blockSignals(False);self.summary()
 def selected(self):
  ids={self.table.item(i.row(),0).data(QtCore.Qt.ItemDataRole.UserRole) for i in self.table.selectionModel().selectedRows() if self.table.item(i.row(),0)}
  return [e for e in self.entries if e['id'] in ids]
 def summary(self):
  if hasattr(self,'summary_text'):self.summary_text.setText(f'{self.current_category}        Sounds:   {len(self.visible())}        Play count:   {sum(e.get("plays",0) for e in self.entries)}\n\n                          Selected:   {len(self.selected())}')
 def toggle_all(self,value):
  self.prefs['show_all_categories']=value
  if not value and self.current_category=='All sounds':self.current_category='My Sounds'
  self.render();self.save()
 def category_changed(self):
  if self.tree.currentItem():self.current_category=self.tree.currentItem().text(0);self.render_table()
 def category_menu(self,pos):
  m=W.QMenu(self);self.act(m,'Add category',self.add_category)
  if self.current_category not in ('My Sounds','All sounds'):self.act(m,'Remove category (keep sounds)',self.remove_category)
  m.exec(self.tree.viewport().mapToGlobal(pos))
 def add_category(self):
  name,ok=W.QInputDialog.getText(self,'Add category','Category name:')
  if ok and name.strip() and name.strip() not in self.categories+['All sounds']:self.checkpoint();self.categories.append(name.strip());self.current_category=name.strip();self.changed()
 def remove_category(self):
  self.checkpoint()
  for e in self.entries:
   if e['category']==self.current_category:e['category']='My Sounds'
  self.categories.remove(self.current_category);self.current_category='My Sounds';self.changed()
 def add_paths(self,paths):
  valid=[str(Path(p).resolve()) for p in paths if Path(p).is_file() and Path(p).suffix.lower() in FORMATS]
  if not valid:return
  self.checkpoint();existing={e['path'] for e in self.entries}
  for path in valid:
   if path not in existing:
    e=entry(path);e['category']=self.current_category if self.current_category!='All sounds' else 'My Sounds';self.entries.append(e);existing.add(path)
  self.changed()
 def add(self):self.add_paths(W.QFileDialog.getOpenFileNames(self,'Add sound files',str(Path.home()),'Audio files (*.mp3 *.wav *.ogg *.flac *.m4a *.aac *.opus *.wma *.mp4 *.webm)')[0])
 def remove(self):
  ids={e['id'] for e in self.selected()}
  if ids:self.checkpoint();self.entries=[e for e in self.entries if e['id'] not in ids];self.changed()
 def copy_entries(self,cut=False):
  self.clipboard=copy.deepcopy(self.selected())
  if cut:self.remove()
 def paste(self):
  if not self.clipboard:return
  self.checkpoint()
  for original in self.clipboard:
   e=copy.deepcopy(original);e['id']=uuid.uuid4().hex;e['hotkey']='';e['category']=self.current_category if self.current_category!='All sounds' else 'My Sounds';self.entries.append(e)
  self.changed()
 def shuffle(self):self.checkpoint();random.shuffle(self.entries);self.changed()
 def find_dead(self):
  self.current_category='All sounds';self.search.clear();self.render();self.table.clearSelection()
  for row,e in enumerate(self.visible()):
   if not Path(e['path']).is_file():self.table.selectionModel().select(self.table.model().index(row,0),QtCore.QItemSelectionModel.SelectionFlag.Select|QtCore.QItemSelectionModel.SelectionFlag.Rows)
  self.summary();self.status.setText(f'{len(self.selected())} missing files selected')
 def show_search(self):self.search.show();self.search.setFocus();self.search.selectAll()
 def context_menu(self,pos):
  if not self.selected():return
  m=W.QMenu(self)
  for text,mode in [('Play (speakers and microphone)','both'),('Play on speakers','speakers'),('Play on microphone','mic')]:self.act(m,text,lambda checked=False,md=mode:self.play_selected(md))
  m.addSeparator();self.act(m,'Remove',self.remove);m.addSeparator();self.act(m,'Set hotkey',self.set_hotkey);self.act(m,'Remove hotkey',self.remove_hotkey)
  colors=m.addMenu('Color');self.act(colors,'Choose color…',self.color);self.act(colors,'Reset color',lambda:self.set_color(''))
  cats=m.addMenu('Move to category')
  for cat in self.categories:self.act(cats,cat,lambda checked=False,c=cat:self.move_category(c))
  m.addSeparator();self.act(m,'Select in explorer',self.explorer);self.act(m,'Rename file\tF2',self.rename_file);self.act(m,'Edit tag text\tShift+F2',self.edit_tag);m.addSeparator();self.act(m,'Edit file\tCtrl+E',self.edit_file);m.exec(self.table.viewport().mapToGlobal(pos))
 def move_category(self,cat):
  self.checkpoint()
  for e in self.selected():e['category']=cat
  self.changed()
 def set_color(self,color):
  if not self.selected():return
  self.checkpoint()
  for e in self.selected():e['color']=color
  self.changed()
 def color(self):
  c=W.QColorDialog.getColor(parent=self)
  if c.isValid():self.set_color(c.name())
 def edit_tag(self):
  selected=self.selected()
  if not selected:return
  text,ok=W.QInputDialog.getText(self,'Edit tag text','Tag:',text=selected[0]['tag'])
  if ok:
   self.checkpoint()
   for e in selected:e['tag']=text
   self.changed()
 def rename_file(self):
  selected=self.selected()
  if len(selected)!=1:W.QMessageBox.information(self,'Rename file','Select one file to rename.');return
  e=selected[0];path=Path(e['path']);name,ok=W.QInputDialog.getText(self,'Rename file','File name:',text=path.name)
  if not ok or name==path.name:return
  if not name or Path(name).name!=name:W.QMessageBox.warning(self,'Rename file','Enter a file name without folders.');return
  dest=path.with_name(name)
  if dest.exists():W.QMessageBox.warning(self,'Rename file','A file with that name already exists.');return
  try:
   self.stop();path.rename(dest)
   for item in self.entries:
    if item['path']==str(path):item['path']=str(dest)
   self.undo_stack.clear();self.redo_stack.clear();self.changed()
  except OSError as ex:W.QMessageBox.warning(self,'Rename file',str(ex))
 def explorer(self):
  if not self.selected():return
  url=QtCore.QUrl.fromLocalFile(self.selected()[0]['path']).toString()
  subprocess.Popen(['dbus-send','--session','--dest=org.freedesktop.FileManager1','--type=method_call','/org/freedesktop/FileManager1','org.freedesktop.FileManager1.ShowItems','array:string:'+url,'string:'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
 def set_hotkey(self):
  selected=self.selected()
  if len(selected)!=1:W.QMessageBox.information(self,'Set hotkey','Select one sound to assign a unique shortcut.');return
  d=W.QDialog(self);d.setWindowTitle('Set hotkey');v=W.QVBoxLayout(d);v.addWidget(W.QLabel(selected[0]['tag']));key=HotkeyEdit(QtGui.QKeySequence(selected[0]['hotkey']));getattr(key,'setMaximumSequenceLength',lambda n:None)(1);v.addWidget(key);v.addWidget(W.QLabel('Works globally on KDE while this soundboard is running.'));buttons=W.QDialogButtonBox(W.QDialogButtonBox.StandardButton.Ok|W.QDialogButtonBox.StandardButton.Cancel);buttons.accepted.connect(d.accept);buttons.rejected.connect(d.reject);v.addWidget(buttons)
  if d.exec():
   value=key.keySequence().toString()
   if value and any(e['id']!=selected[0]['id'] and e['hotkey']==value for e in self.entries):W.QMessageBox.warning(self,'Hotkey','That shortcut belongs to another sound.');return
   self.checkpoint();selected[0]['hotkey']=value;self.changed()
 def remove_hotkey(self):
  self.checkpoint()
  for e in self.selected():e['hotkey']=''
  self.changed()
 def install_hotkeys(self):
  if not hasattr(self,'hotkeys'):return
  if not self.hotkey_button.isChecked():self.hotkeys.clear();return
  errors=self.hotkeys.install()
  if errors:self.status.setText('Hotkey: '+errors[0])
 def refresh_hotbar(self):
  self.hotbar.clear()
  for e in self.visible()[:12]:self.hotbar.addAction(e['tag'],lambda ident=e['id']:self.play_id(ident))
 def dragEnterEvent(self,e):
  if e.mimeData().hasUrls():e.acceptProposedAction()
 def dropEvent(self,e):self.add_paths([u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]);e.acceptProposedAction()
 def flag(self,key,value):
  self.prefs[key]=value
  if key in self.flag_actions:self.flag_actions[key].setChecked(value)
  if key=='repeat':self.repeat_button.blockSignals(True);self.repeat_button.setChecked(value);self.repeat_button.blockSignals(False)
  self.save()
 def set_mode(self,mode):
  self.prefs['mode']=mode;self.mode_actions[mode].setChecked(True);self.mode_button.setIcon(icon({'both':'play','speakers':'headphones','mic':'mic'}[mode]));self.save()
 def cycle_mode(self):
  modes=['both','speakers','mic'];self.set_mode(modes[(modes.index(self.prefs['mode'])+1)%3])
 def preferences(self,tab=0):
  if isinstance(tab,bool):tab=0
  Preferences(self,tab).exec()
 def configure_bridge(self):
  self.stop();self.bridge.stop()
  if self.prefs['link'] and self.source:
   try:self.bridge.start(self.source if self.prefs['mic_voice'] else None,self.output,False,route_source=self.source);self.route_apps()
   except Exception as ex:self.status.setText('Microphone routing: '+str(ex))
  else:self.route_label.setText('Mic link disabled')
 def route_apps(self):
  if not self.bridge.modules:return
  try:
   apps=self.bridge.route_apps();self.route_label.setText('Linked: '+', '.join(apps) if apps else 'Mic link ready');self.route_label.setToolTip(self.source or '')
   self.sync_voice_block()
   if self.now and self.prefs['duck']:self.duck_audio()
  except Exception as ex:self.route_label.setText('Mic disconnected');self.route_label.setToolTip(str(ex))
 def sync_voice_block(self):
  playing=bool(self.now and not self.paused and self.current_mode in ('both','mic')
               and self.players and self.players[-1].active())
  self.bridge.block_voice(bool(self.prefs['block_voice'] and playing))
 def play_selected(self,mode=None):
  selected=self.selected()
  if selected:self.play_id(selected[0]['id'],mode if isinstance(mode,str) else None)
 def play_id(self,ident,mode=None):
  e=next((e for e in self.entries if e['id']==ident),None)
  if not e:return
  mode=mode or self.prefs['mode']
  if not Path(e['path']).is_file():self.status.setText('File not found: '+e['path']);return
  if mode!='speakers' and not self.bridge.modules:self.status.setText('Enable automatic microphone linking in Preferences → Devices first.');return
  self.stop();token=self.token;self.current_mode=mode;self.status.setText('Preparing: '+e['tag'])
  def begin():
   if self.token!=token:return
   if self.prefs['activation'] and Path(self.prefs['activation_file']).is_file():
    self.activation_pending=e;self.start_players(entry(self.prefs['activation_file']),mode,0,False)
   else:self.start_players(e,mode)
  QtCore.QTimer.singleShot(self.prefs['ptt_delay'],begin)
 def audio_filters(self,mode):
  # SPL calibration is unavailable; use a documented relative RMS reference.
  if self.prefs['normalization']=='dynamic':
   rms=min(.8,max(.004,10**((self.prefs['voice_db']-88)/20)));filters=f'dynaudnorm=f=75:g=3:r={rms:.6f}:m=10:p=0.95'
  else:filters=f'volume={self.prefs["fixed_db"]}dB'
  if mode=='mic' and self.current_mode=='both' and self.prefs['karaoke_delay']:filters+=f',adelay={self.prefs["karaoke_delay"]}:all=1'
  return filters
 def start_players(self,e,mode,offset=0,count=True):
  self.now=e;self.current_mode=mode;self.position=offset;self.started=time.monotonic();self.paused=False;self.players=[]
  try:
   targets=[]
   if mode in ('both','speakers'):targets.append((self.output,'speakers'))
   if mode in ('both','mic'):targets.append((CLIPS,'mic'))
   for sink,kind in targets:
    p=Player();p.output_kind=kind;level=(0 if self.prefs['monitor_muted'] else self.monitor_gain()) if kind=='speakers' else self.prefs['mic_volume'];p.play(e['path'],sink,level,offset,self.audio_filters(kind));self.players.append(p)
   self.sync_voice_block();self.started=time.monotonic()
   if self.prefs['duck']:self.duck_audio()
   if count:
    e['plays']=e.get('plays',0)+1;self.history=[i for i in self.history if i!=e['id']]+[e['id']];self.summary()
    if self.prefs['notifications'] and shutil.which('notify-send'):subprocess.Popen(['notify-send','soundpad like',e['tag']])
  except Exception as ex:self.stop();self.status.setText(str(ex))
 def stop_players(self):
  for p in self.players:p.stop()
  self.players=[]
 def stop(self):
  self.token+=1;self.activation_pending=None;self.stop_players();self.now=None;self.paused=False
  try:self.bridge.block_voice(False)
  except Exception:pass
  self.restore_audio()
  if hasattr(self,'status'):self.status.setText('Stopped');self.clock.setText(' 0:00 ');self.seek.setValue(0)
 def pause(self):
  if not self.now:self.play_selected();return
  self.paused=not self.paused
  for p in self.players:p.pause(self.paused)
  self.sync_voice_block()
  if self.paused:self.pause_time=time.monotonic();self.status.setText('Paused')
  else:self.started+=time.monotonic()-self.pause_time;self.status.setText('Playing: '+self.now['tag'])
 def elapsed(self):return self.position+((self.pause_time if self.paused else time.monotonic())-self.started)
 def seek_to(self):
  if not self.now:return
  e=self.now;mode=self.current_mode;length=e.get('duration',0)
  if length<=0:return
  offset=min(max(0,length-.001),length*self.seek.value()/self.seek.maximum());was_paused=self.paused
  self.stop_players();self.start_players(e,mode,offset,False)
  if was_paused:
   self.pause();self.pause_time=self.started
  self.update_progress()
 def update_progress(self):
  if not self.now:return
  t=max(0,self.elapsed());self.clock.setText(' '+duration(t)+' ')
  if not self.seek.isSliderDown():self.seek.setValue(min(self.seek.maximum(),round(t/max(.001,self.now.get('duration',0))*self.seek.maximum())))
 def refresh_monitor_icon(self):
  muted=self.prefs['monitor_muted'];base=icon('headphones')
  if muted:
   pix=base.pixmap(24,24);painter=QtGui.QPainter(pix);painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing);painter.setPen(QtGui.QPen(QtGui.QColor('#ed5d64'),3));painter.drawLine(3,3,21,21);painter.end();base=QtGui.QIcon(pix)
  self.monitor_action.setIcon(base);self.monitor_action.setToolTip('Unmute headphones only' if muted else 'Mute headphones only')
  self.volume.setToolTip(f"Headphone playback: {self.prefs['volume']}%"+(' (muted)' if muted else ''))
 def apply_monitor_volume(self):
  level=0 if self.prefs['monitor_muted'] else self.prefs['volume']
  for p in self.players:
   if getattr(p,'output_kind',None)=='speakers':
    try:p.volume(level)
    except Exception:pass
  self.refresh_monitor_icon()
 def set_monitor_mute(self,muted):
  self.prefs['monitor_muted']=muted;self.apply_monitor_volume();self.save()
 def set_volume(self,value):
  self.prefs['volume']=value;self.apply_monitor_volume()
 def duck_audio(self):
  for s in devices('sink-inputs'):
   if s.get('owner_module') is not None or s['properties'].get('application.name')=='soundpad-like':continue
   serial=s['properties'].get('object.serial');key=s['index']
   if key in self.ducked:continue
   values=[v['value'] for v in s['volume'].values()];scaled=[int(v*(100-self.prefs['duck_percent'])/100) for v in values]
   try:pa('set-sink-input-volume',key,*scaled);self.ducked[key]=(serial,values,scaled)
   except Exception:pass
 def restore_audio(self):
  if not self.ducked:return
  try:
   for s in devices('sink-inputs'):
    old=self.ducked.get(s['index'])
    if old and s['properties'].get('object.serial')==old[0]:
     # Respect volume adjustments made by the user while the clip played.
     current=[v['value'] for v in s['volume'].values()]
     if current==old[2]:pa('set-sink-input-volume',s['index'],*old[1])
  except Exception:pass
  self.ducked={}
 def step(self,delta):
  visible=self.visible()
  if not visible:return
  current=self.now or (self.selected()[0] if self.selected() else None);index=next((i for i,e in enumerate(visible) if current and e['id']==current['id']),0)
  e=visible[(index+delta)%len(visible)];self.select_id(e['id']);self.play_id(e['id'])
 def random_play(self,mode,all_categories=False):
  choices=self.entries if all_categories else self.visible()
  if choices:self.play_id(random.choice(choices)['id'],mode)
 def select_id(self,ident):
  e=next((e for e in self.entries if e['id']==ident),None)
  if not e:return
  self.current_category=e['category'];self.search.clear();self.render()
  for row,item in enumerate(self.visible()):
   if item['id']==ident:self.table.selectRow(row);self.table.scrollToItem(self.table.item(row,0));break
 def tick(self):
  updated=False
  for path,future in list(self.pending.items()):
   if future.done():
    result=future.result()
    for e in self.entries:
     if e['path']==path:e['duration']=result;e['probed']=True
    del self.pending[path];updated=True
  if updated:
   for row,e in enumerate(self.visible()):
    if self.table.item(row,2):self.table.item(row,2).setText(duration(e.get('duration')))
  if not self.now or self.paused:return
  if any(p.active() for p in self.players):
   self.status.setText('Playing: '+self.now['tag'])
  else:
   e=self.now;mode=self.current_mode;activation=self.activation_pending;errors='\n'.join(p.error() for p in self.players if p.error());self.stop_players();self.now=None
   if errors:self.stop();self.status.setText(errors[-220:]);return
   if activation:self.activation_pending=None;self.start_players(activation,mode);return
   if self.prefs['repeat']:self.start_players(e,mode);return
   if self.prefs['continue'] or self.prefs['repeat_list']:
    visible=self.visible();index=next((i for i,x in enumerate(visible) if x['id']==e['id']),-1)+1
    if index<len(visible):self.start_players(visible[index],mode);return
    if visible and self.prefs['repeat_list']:self.start_players(visible[0],mode);return
   self.stop();self.status.setText('Finished: '+e['tag']);self.save()
 def new_list(self):
  self.backup();self.checkpoint();self.stop();self.entries=[];self.categories=['My Sounds'];self.current_category='My Sounds';self.list_path='';self.changed()
 def read_list(self,path):
  p=Path(path)
  if p.suffix.lower()=='.spl':
   tree=ET.parse(p);entries=[]
   for s in tree.findall('.//Soundlist/Sound') or tree.getroot().findall('Sound'):
    raw=s.get('url','')
    if raw.startswith('Z:'):raw=raw[2:].replace('\\','/')
    elif len(raw)>2 and raw[1]==':':raw=raw[2:].lstrip('/\\').replace('\\','/')
    q=Path(raw)
    if not q.is_absolute():q=p.parent/q
    e=entry(q);e['tag']=s.get('title') or q.stem;entries.append(e)
   return entries,['My Sounds']
  data=json.loads(p.read_text());return data.get('entries') or [entry(s) for s in data.get('files',[])],data.get('categories',['My Sounds'])
 def load_list(self,path=None):
  if not isinstance(path,str):path=W.QFileDialog.getOpenFileName(self,'Load sound list','','Sound lists (*.zsl *.json *.spl)')[0]
  if not path:return
  try:entries,cats=self.read_list(path)
  except Exception as ex:W.QMessageBox.warning(self,'Load sound list',str(ex));return
  self.backup();self.checkpoint();self.stop();self.entries=entries;self.categories=list(dict.fromkeys(['My Sounds']+cats));self.current_category='My Sounds';self.list_path=path;self.recent_lists=[p for p in self.recent_lists if p!=path]+[path];self.changed()
 def save_list(self,as_new=False):
  path=self.list_path
  if as_new or not path:path=W.QFileDialog.getSaveFileName(self,'Save sound list',str(DATA/'My Sounds.zsl'),'soundpad like list (*.zsl);;Soundpad list (*.spl)')[0]
  if not path:return
  try:
   p=Path(path)
   if p.suffix.lower()=='.spl':
    root=ET.Element('Soundlist')
    for e in self.entries:ET.SubElement(root,'Sound',url='Z:'+e['path'].replace('/','\\'),title=e['tag'],duration=duration(e.get('duration',0)))
    ET.ElementTree(root).write(p,encoding='utf-8',xml_declaration=True)
   else:p.write_text(json.dumps({'entries':self.entries,'categories':self.categories},ensure_ascii=False,indent=2))
   self.list_path=path;self.dirty=False;self.save();self.status.setText('Saved: '+path)
  except Exception as ex:W.QMessageBox.warning(self,'Save sound list',str(ex))
 def fill_recent(self):
  self.recent_menu.clear()
  for path in reversed(self.recent_lists):self.act(self.recent_menu,Path(path).name,lambda checked=False,p=path:self.load_list(p))
 def fill_backups(self):
  self.restore_menu.clear()
  for path in reversed(sorted((DATA/'backups').glob('*.json'))):self.act(self.restore_menu,path.stem,lambda checked=False,p=str(path):self.load_list(p))
 def fill_history(self,menu,play):
  menu.clear()
  for ident in reversed(self.history[-15:]):
   e=next((e for e in self.entries if e['id']==ident),None)
   if e:self.act(menu,e['tag'],lambda checked=False,i=ident:self.play_id(i,'both') if play else self.select_id(i))
 def export_csv(self):
  path=W.QFileDialog.getSaveFileName(self,'Export sound list','','CSV (*.csv)')[0]
  if path:
   with open(path,'w',newline='',encoding='utf-8-sig') as f:
    writer=csv.writer(f);writer.writerow(['Index','Tag','Duration','Hotkey','Category','Path']);writer.writerows([i+1,e['tag'],duration(e.get('duration')),e['hotkey'],e['category'],e['path']] for i,e in enumerate(self.entries))
 def export_files(self):
  folder=W.QFileDialog.getExistingDirectory(self,'Export selected sound files')
  if not folder:return
  for e in self.selected():
   source=Path(e['path']);target=Path(folder)/source.name;i=2
   while target.exists():target=Path(folder)/(source.stem+f' ({i})'+source.suffix);i+=1
   try:shutil.copy2(source,target)
   except OSError as ex:W.QMessageBox.warning(self,'Export',str(ex));return
  self.status.setText('Exported selected files')
 def stats(self):W.QMessageBox.information(self,'Statistics',f'Sounds: {len(self.entries)}\nCategories: {len(self.categories)}\nPlay count: {sum(e.get("plays",0) for e in self.entries)}\nTotal duration: {duration(sum(e.get("duration",0) for e in self.entries))}')
 def reset_stats(self):
  self.checkpoint()
  for e in self.entries:e['plays']=0
  self.changed()
 def help(self):W.QMessageBox.information(self,'How to use','Add files or drag them into the list.\nDouble-click to play. Right-click for sound actions.\nChoose your physical microphone in File → Preferences → Devices.\nLeave Discord on that same microphone; connection is automatic.\nGlobal hotkeys work through KDE while the app is running.\nClosing the app restores microphone connections and application volumes.')
 def closeEvent(self,event):
  self.timer.stop();self.progress_timer.stop();self.route_timer.stop();self.stop();self.bridge.stop();self.hotkeys.close()
  for dialog in self.dialogs:dialog.close()
  self.save();self.pool.shutdown(wait=False,cancel_futures=True);event.accept()
 def recorder(self):
  d=Recorder(self);self.dialogs.append(d);d.show()
 def edit_file(self):
  if len(self.selected())!=1:W.QMessageBox.information(self,'Edit file','Select one sound to edit.');return
  e=self.selected()[0]
  d=Editor(self,e);self.dialogs.append(d);d.show()
 def tts(self):
  d=Speech(self);self.dialogs.append(d);d.show()

class Recorder(W.QDialog):
 def __init__(self,owner):
  super().__init__(owner);self.owner=owner;self.proc=None;self.path=None;self.setWindowTitle('Sound recorder');self.resize(480,220)
  v=W.QVBoxLayout(self);v.addWidget(W.QLabel('Recording device'));self.source=W.QComboBox();v.addWidget(self.source)
  for s in devices('sources'):
   if not s['name'].startswith(PREFIX):self.source.addItem(s.get('description',s['name']),s['name'])
  i=self.source.findData(owner.source)
  if i>=0:self.source.setCurrentIndex(i)
  self.status=W.QLabel('Ready. Recording starts only when you press Record.');v.addWidget(self.status);row=W.QHBoxLayout();v.addLayout(row)
  self.record=W.QPushButton('● Record');self.record.clicked.connect(self.start);row.addWidget(self.record);self.finish=W.QPushButton('■ Stop and add to sound list');self.finish.clicked.connect(self.stop);row.addWidget(self.finish);self.finish.setEnabled(False)
  self.timer=QtCore.QTimer(self);self.timer.timeout.connect(self.tick)
 def start(self):
  folder=Path(self.owner.prefs['rec_folder']);folder.mkdir(parents=True,exist_ok=True);self.path=folder/(datetime.now().strftime('Recording-%Y%m%d-%H%M%S-%f')+'.wav')
  self.proc=subprocess.Popen(['parecord','--latency-msec=40','--file-format=wav',f'--device={self.source.currentData()}',f'--rate={self.owner.prefs["rec_rate"]}','--channels=2',str(self.path)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);self.start_time=time.monotonic();self.record.setEnabled(False);self.finish.setEnabled(True);self.source.setEnabled(False);self.timer.start(200)
 def tick(self):
  if self.proc and self.proc.poll() is not None:self.stop();self.status.setText('Recording ended. Check the selected device.');return
  self.status.setText('Recording  '+duration(time.monotonic()-self.start_time))
 def stop(self):
  if not self.proc:return
  if self.proc.poll() is None:
   self.proc.send_signal(signal.SIGINT)
   try:self.proc.wait(timeout=2)
   except subprocess.TimeoutExpired:self.proc.kill();self.proc.wait()
  self.proc=None;self.timer.stop();self.record.setEnabled(True);self.finish.setEnabled(False);self.source.setEnabled(True)
  if self.path and self.path.exists() and self.path.stat().st_size>44:self.owner.add_paths([str(self.path)]);self.status.setText('Saved: '+str(self.path))
 def closeEvent(self,event):self.stop();event.accept()

class Editor(W.QDialog):
 def __init__(self,owner,e):
  super().__init__(owner);self.owner=owner;self.entry=e;self.process=None;self.setWindowTitle('Sound editor — '+e['tag']);self.resize(480,300)
  v=W.QVBoxLayout(self);v.addWidget(W.QLabel('Trim and adjust a sound. Saving creates a new WAV file.'));form=W.QFormLayout();v.addLayout(form)
  self.start=W.QDoubleSpinBox();self.end=W.QDoubleSpinBox();self.gain=W.QDoubleSpinBox()
  for spin in [self.start,self.end]:spin.setRange(0,86400);spin.setDecimals(3);spin.setSuffix(' sec')
  self.end.setValue(e.get('duration') or probe(e['path']));self.gain.setRange(-60,20);self.gain.setSuffix(' dB')
  form.addRow('Start',self.start);form.addRow('End',self.end);form.addRow('Gain',self.gain)
  self.status=W.QLabel('Original file will be preserved.');v.addWidget(self.status);self.button=W.QPushButton('Save edited copy and add to list');self.button.clicked.connect(self.save);v.addWidget(self.button)
 def save(self):
  if self.end.value()<=self.start.value():self.status.setText('End must be after start.');return
  path=W.QFileDialog.getSaveFileName(self,'Save edited copy',str(Path(self.entry['path']).with_name(Path(self.entry['path']).stem+'-edited.wav')),'WAV (*.wav)')[0]
  if not path:return
  if Path(path).resolve()==Path(self.entry['path']).resolve():self.status.setText('Choose a new file name.');return
  self.target=path;self.process=QtCore.QProcess(self);self.process.finished.connect(self.done);self.button.setEnabled(False);self.status.setText('Saving…')
  self.process.start('ffmpeg',['-y','-v','error','-ss',str(self.start.value()),'-i',self.entry['path'],'-t',str(self.end.value()-self.start.value()),'-af',f'volume={self.gain.value()}dB','-c:a','pcm_s16le',path])
 def done(self,code,status):
  self.button.setEnabled(True)
  if code==0:self.owner.add_paths([self.target]);self.status.setText('Saved: '+self.target)
  else:self.status.setText(bytes(self.process.readAllStandardError()).decode(errors='replace'))
 def closeEvent(self,event):
  if self.process and self.process.state()!=QtCore.QProcess.ProcessState.NotRunning:self.process.terminate();self.process.waitForFinished(1000)
  event.accept()

class Speech(W.QDialog):
 def __init__(self,owner):
  super().__init__(owner);self.owner=owner;self.process=None;self.setWindowTitle('Text to speech');self.resize(490,330)
  self.runtime=Path(os.environ.get('SOUNDPAD_LIKE_SPEECH',str(Path.home()/'.local/share/ziyad-soundboard/speech-runtime')));self.exe=self.runtime/'bin/espeak-ng'
  v=W.QVBoxLayout(self);v.addWidget(W.QLabel('Convert text into a sound file, then play it like any other clip.'));self.voice=W.QComboBox()
  for label,code in [('العربية','ar'),('English','en-us'),('English (UK)','en-gb'),('French','fr'),('German','de'),('Spanish','es')]:self.voice.addItem(label,code)
  v.addWidget(self.voice);self.text=W.QPlainTextEdit();self.text.setPlaceholderText('اكتب النص هنا…');v.addWidget(self.text);row=W.QHBoxLayout();v.addLayout(row)
  self.rate=W.QSpinBox();self.rate.setRange(80,350);self.rate.setValue(160);row.addWidget(W.QLabel('Words / minute'));row.addWidget(self.rate)
  self.button=W.QPushButton('Generate and add to sound list');self.button.clicked.connect(self.generate);row.addWidget(self.button)
  self.status=W.QLabel('Generated audio is stored in your sound library.');v.addWidget(self.status)
  if not self.exe.exists():self.status.setText('Speech runtime is missing.');self.button.setEnabled(False)
 def generate(self):
  text=self.text.toPlainText().strip()
  if not text:return
  folder=DATA/'speech';folder.mkdir(exist_ok=True);self.target=folder/(datetime.now().strftime('Speech-%Y%m%d-%H%M%S-%f')+'.wav');self.tag=text[:65]
  self.process=QtCore.QProcess(self);env=QtCore.QProcessEnvironment.systemEnvironment();env.insert('LD_LIBRARY_PATH',str(self.runtime/'lib'));self.process.setProcessEnvironment(env);self.process.finished.connect(self.done);self.button.setEnabled(False);self.status.setText('Generating…')
  self.process.start(str(self.exe),['--path='+str(self.runtime/'share'),'-v',self.voice.currentData(),'-s',str(self.rate.value()),'-w',str(self.target),'--stdin']);self.process.write(text.encode('utf-8'));self.process.closeWriteChannel()
 def done(self,code,status):
  self.button.setEnabled(True)
  if code==0 and self.target.exists():
   self.owner.add_paths([str(self.target)])
   for e in self.owner.entries:
    if e['path']==str(self.target):e['tag']=self.tag;self.owner.select_id(e['id'])
   self.owner.save();self.owner.render();self.status.setText('Added to the sound list. Select it and press Play.')
  else:self.status.setText(bytes(self.process.readAllStandardError()).decode(errors='replace')[-200:])
 def closeEvent(self,event):
  if self.process and self.process.state()!=QtCore.QProcess.ProcessState.NotRunning:self.process.terminate();self.process.waitForFinished(1000)
  event.accept()


# Extended native settings matching the supplied Soundpad preference layouts.
import struct,fcntl,math,array,re
SPECIAL=[
 ('stop','Stop playback',None),('start','Start playback',None),('pause','Pause/resume playback',None),
 ('previous','Play previous file',None),('next','Play next file',None),('select_previous','Select previous file',None),('select_next','Select next file',None),('selected','Play selected file',None),
 ('again','Play current file again',None),('previous_played','Play previously played file',None),('random','Play random file',None),('random_all','Play random file from all categories',None),
 ('category_previous','Select previous category',None),('category_next','Select next category',None),
 ('record','Start recording',None),('record_speakers','Start to record speakers',None),('record_mic','Start to record microphone',None),('record_stop','Stop recording',None),
 ('back5','Jump back by',5),('back30','Jump back by',30),('forward5','Jump forward by',5),('forward30','Jump forward by',30),
 ('down5','Volume down by',5),('down20','Volume down by',20),('up5','Volume up by',5),('up20','Volume up by',20),('volume0','Set volume to',0),('volume100','Set volume to',100),('mute','Mute',None),
 ('mode_both','Set play mode to default',None),('mode_speakers','Set play mode to speakers',None),('mode_mic','Set play mode to microphone',None),('mode_cycle','Switch to next play mode',None),
 ('auto_enable','Enable Auto Keys',None),('auto_disable','Disable Auto Keys',None),('auto_toggle','Toggle Auto Keys',None),
 ('keys_enable','Enable Hotkeys',None),('keys_disable','Disable Hotkeys',None),('keys_toggle','Toggle Hotkeys',None),
 ('tts_both','Play TTS',None),('tts_speakers','Play TTS on speakers',None),('tts_mic','Play TTS on microphone',None),('hotbar_previous','Select previous hotbar page',None),('hotbar_next','Select next hotbar page',None)]
DEFAULTS.update(special_hotkeys={},numcode_enabled=False,index_enabled=False,numcode_modifier='Alt',index_modifier='Ctrl+Alt',auto_keys=[],auto_keys_enabled=True,pass_hotkeys=False,left_right_modifiers=True,same_category_keys=False,
 language='system',date_format='default',zoom=100,toolbar_size=18,icon_color='#0762a7',progress_rate=60,show_mic_level=False,linear_volume=True,resume_mode=False,mode_cycle='all',
 autostart=False,start_minimized=False,show_tray=True,minimize_tray=False,close_tray=False,always_top=False,disable_sorting=True,typing_search=True,typing_tts=False,remember_state=False,device_check=True,selected_sources=[],device_boosts={},
 rec_format='wav',rec_bitrate=320,rec_category='selected',rec_position='end',rec_normalize=False,rec_db=89.0,rec_trim=False,rec_max_seconds=300)
BasePreferences=Preferences;BaseWindow=Window;BaseHotkeys=Hotkeys;BaseRecorder=Recorder
SHAPES.update(record='<circle cx="12" cy="12" r="9" fill="#c00030"/>',shuffle='<path d="M2 5h5l10 12h3v-3l4 4-4 4v-3h-4L6 7H2zM2 17h4l3-4 2 2-4 4H2zM13 7l3-2h4V2l4 4-4 4V7h-3l-2 2z"/>')
OriginalIcon=icon
ICON_COLOR='#0762a7'
def icon(name):
 result=OriginalIcon(name)
 if ICON_COLOR=='#0762a7':return result
 pix=result.pixmap(24,24);paint=QtGui.QPainter(pix);paint.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_SourceIn);paint.fillRect(pix.rect(),QtGui.QColor(ICON_COLOR));paint.end();return QtGui.QIcon(pix)

class AutoKeys:
 """Emit only explicitly configured shortcuts; never reads keyboard events."""
 def __init__(self):self.fd=None;self.held=[]
 def open(self):
  if self.fd is not None:return
  self.fd=os.open('/dev/uinput',os.O_WRONLY|os.O_NONBLOCK)
  try:
   fcntl.ioctl(self.fd,0x40045564,1)
   for key in range(1,256):fcntl.ioctl(self.fd,0x40045565,key)
   fcntl.ioctl(self.fd,0x405c5503,struct.pack('HHHH80sI',3,0x1234,0x5678,1,b'soundpad like Auto Keys',0));fcntl.ioctl(self.fd,0x5501)
  except Exception:os.close(self.fd);self.fd=None;raise
 def codes(self,text):
  seq=QtGui.QKeySequence(text)
  if seq.isEmpty():return []
  combo=seq[0];key=int(combo.key());mods=combo.keyboardModifiers();codes=[]
  for flag,code in [(QtCore.Qt.KeyboardModifier.ControlModifier,29),(QtCore.Qt.KeyboardModifier.AltModifier,56),(QtCore.Qt.KeyboardModifier.ShiftModifier,42),(QtCore.Qt.KeyboardModifier.MetaModifier,125)]:
   if mods&flag:codes.append(code)
  lookup={}
  for letters,values in [('QWERTYUIOP',range(16,26)),('ASDFGHJKL',range(30,39)),('ZXCVBNM',range(44,51)),('1234567890',range(2,12))]:lookup.update({ord(c):v for c,v in zip(letters,values)})
  lookup.update({int(QtCore.Qt.Key.Key_Space):57,int(QtCore.Qt.Key.Key_Return):28,int(QtCore.Qt.Key.Key_Enter):96,int(QtCore.Qt.Key.Key_Tab):15,int(QtCore.Qt.Key.Key_Escape):1,int(QtCore.Qt.Key.Key_Backspace):14,int(QtCore.Qt.Key.Key_Up):103,int(QtCore.Qt.Key.Key_Down):108,int(QtCore.Qt.Key.Key_Left):105,int(QtCore.Qt.Key.Key_Right):106})
  for i in range(12):lookup[int(QtCore.Qt.Key.Key_F1)+i]=(59+i if i<10 else 87+i-10)
  if mods&QtCore.Qt.KeyboardModifier.KeypadModifier and 48<=key<=57:code=[82,79,80,81,75,76,77,71,72,73][key-48]
  elif mods&QtCore.Qt.KeyboardModifier.KeypadModifier and key in {ord('.'):83,ord('+'):78,ord('-'):74,ord('*'):55,ord('/'):98}:code={ord('.'):83,ord('+'):78,ord('-'):74,ord('*'):55,ord('/'):98}[key]
  elif key in lookup:code=lookup[key]
  else:raise ValueError('This Auto Key is not supported; use letters, numbers, F1–F12 or navigation keys.')
  return codes+[code]
 def emit(self,code,value):
  os.write(self.fd,struct.pack('llHHi',0,0,1,code,value)+struct.pack('llHHi',0,0,0,0,0))
 def press(self,text,hold=False):
  codes=self.codes(text);self.open()
  for c in codes:self.emit(c,1)
  if hold:self.held.extend(c for c in codes if c not in self.held)
  else:
   for c in reversed(codes):self.emit(c,0)
 def release(self):
  if self.fd is not None:
   for c in reversed(self.held):
    try:self.emit(c,0)
    except OSError:pass
  self.held=[]
 def close(self):
  self.release()
  if self.fd is not None:
   fcntl.ioctl(self.fd,0x5502);os.close(self.fd);self.fd=None

class RawHotkeys(QtCore.QObject):
 """Non-grabbing input observer, used only for configured hotkeys/modifiers.
 No text is reconstructed, logged or stored; reads are disabled when not needed.
 """
 def __init__(self,owner):
  super().__init__(owner.window);self.owner=owner;self.devices={};self.bindings=[];self.enabled=False
  self.scan_timer=QtCore.QTimer(self);self.scan_timer.timeout.connect(self.scan);self.scan_timer.start(2000)
 def configure(self,bindings,enabled):
  self.bindings=bindings;self.enabled=enabled
  if not enabled:self.close_devices()
  else:self.scan()
 def close_devices(self):
  for fd,notifier in self.devices.values():notifier.setEnabled(False);notifier.deleteLater();os.close(fd)
  self.devices={}
 def scan(self):
  if not self.enabled:return
  paths=set(Path('/dev/input').glob('event*'))
  for path in list(self.devices):
   if path not in paths:
    fd,notifier=self.devices.pop(path);notifier.setEnabled(False);notifier.deleteLater();os.close(fd)
  for path in paths:
   if path in self.devices:continue
   fd=None
   try:
    name=(Path('/sys/class/input')/path.name/'device/name').read_text().strip()
    if name.startswith(('soundpad like','Ziyad Soundboard')):continue
    fd=os.open(path,os.O_RDONLY|os.O_NONBLOCK);bits=bytearray(32);fcntl.ioctl(fd,0x80204521,bits,True)
    if not all(bits[k//8]&(1<<(k%8)) for k in [30,28,57]):os.close(fd);continue
    notifier=QtCore.QSocketNotifier(fd,QtCore.QSocketNotifier.Type.Read,self);notifier.activated.connect(lambda _,f=fd:self.read(f));self.devices[path]=(fd,notifier)
   except (OSError,ValueError):
    if fd is not None:os.close(fd)
    continue
 def down(self):
  result=set()
  for fd,_ in self.devices.values():
   try:
    bits=bytearray(32);fcntl.ioctl(fd,0x80204518,bits,True);result.update(k for k in range(256) if bits[k//8]&(1<<(k%8)))
   except OSError:pass
  return result
 @staticmethod
 def modifiers(keys):
  return {left for left,right in [(29,97),(56,100),(42,54),(125,126)] if left in keys or right in keys}
 def read(self,fd):
  try:data=os.read(fd,24*128)
  except (BlockingIOError,OSError):return
  for offset in range(0,len(data)-23,24):
   _,_,kind,code,value=struct.unpack_from('llHHi',data,offset)
   if kind!=1:continue
   down=self.down()
   if value==0 and self.owner.window.number_buffer:
    modifier=self.owner.window.prefs[self.owner.window.number_kind+'_modifier'];required=set(AutoKeys().codes(modifier+'+A')[:-1])
    if not required.issubset(self.modifiers(down)):self.owner.window.finish_number()
   if value!=1:continue
   focus=W.QApplication.focusWidget()
   while focus and not isinstance(focus,HotkeyEdit):focus=focus.parentWidget()
   if focus:continue
   if not self.owner.window.prefs['left_right_modifiers'] and down&{97,100,54,126}:continue
   for text,fn in self.bindings:
    if not self.owner.window.prefs['pass_hotkeys'] and not is_keypad(text):continue
    if not self.owner.window.prefs['global_keys'] and not self.owner.window.isActiveWindow():continue
    try:keys=AutoKeys().codes(text)
    except ValueError:continue
    if keys and code==keys[-1] and self.modifiers(down)==set(keys[:-1]):fn();break
 def close(self):self.scan_timer.stop();self.close_devices()

def is_keypad(text):
 seq=QtGui.QKeySequence(text)
 return bool(not seq.isEmpty() and seq[0].keyboardModifiers()&QtCore.Qt.KeyboardModifier.KeypadModifier)

class Hotkeys(BaseHotkeys):
 def __init__(self,window):
  super().__init__(window);self.raw=RawHotkeys(self)
 def close(self):self.raw.close();super().close()
 @QtCore.pyqtSlot(str,str,'qlonglong')
 def pressed(self,component,action,timestamp):
  if not self.window.prefs['left_right_modifiers'] and self.raw.down()&{97,100,54,126}:return
  super().pressed(component,action,timestamp)
 def install(self):
  self.clear();w=self.window;specs=[];enabled=w.hotkey_button.isChecked()
  if enabled:
   for e in w.entries:
    if not e.get('hotkey'):continue
    if w.prefs['same_category_keys'] and w.current_category not in ('All sounds',e['category']):continue
    specs.append((e['id'],e['hotkey'],e['tag'],lambda ident=e['id']:w.play_id(ident)))
  for ident,label,value in SPECIAL:
   config=w.special_config(ident)
   if config['key'] and (enabled or ident in ('keys_enable','keys_toggle')):specs.append(('special_'+ident,config['key'],label,lambda i=ident:w.special_action(i)))
  if enabled:
   for kind in ['numcode','index']:
    if w.prefs[kind+'_enabled']:
     for digit in range(10):specs.append((f'{kind}_{digit}',w.prefs[kind+'_modifier']+'+Num+'+str(digit),kind+' '+str(digit),lambda k=kind,d=digit:w.number_key(k,d)))
  self.keypad_bindings=[(key,fn) for _,key,_,fn in specs if is_keypad(key)]
  errors=[];seen=set();self.raw.configure([(key,fn) for _,key,_,fn in specs],bool(w.prefs['pass_hotkeys'] or not w.prefs['left_right_modifiers'] or self.keypad_bindings))
  if w.prefs['pass_hotkeys']:
   return [] if self.raw.devices else ['Keyboard access is unavailable for pass-through hotkeys.']
  for ident,key,label,fn in specs:
   if key in seen:errors.append('Duplicate shortcut: '+key);continue
   seen.add(key);seq=QtGui.QKeySequence(key)
   if is_keypad(key):
    if not self.raw.devices:errors.append('Numpad hotkeys work in this window; background use needs keyboard device access.')
    continue
   if self.bus and w.prefs['global_keys']:
    try:
     code=seq[0].toCombined()
     if not self.call('isGlobalShortcutAvailable',code,'ziyad_soundboard'):raise RuntimeError('Shortcut already in use: '+key)
     aid=self.qt.QDBusArgument(['ziyad_soundboard',ident,'soundpad like',label],QtCore.QMetaType.Type.QStringList.value);keys=self.qt.QDBusArgument();keys.beginArray(QtCore.QMetaType.Type.Int.value);keys.add(code);keys.endArray();self.call('doRegister',aid)
     accepted=self.call('setShortcut',aid,keys,self.qt.QDBusArgument(6,QtCore.QMetaType.Type.UInt.value))
     if code not in accepted:raise RuntimeError('Shortcut not accepted: '+key)
     self.actions[ident]=fn
    except Exception as ex:errors.append(str(ex))
   else:
    shortcut=QtGui.QShortcut(seq,w);shortcut.activated.connect(fn);self.local.append(shortcut)
  return errors

class Preferences(BasePreferences):
 def __init__(self,owner,tab=0):
  self.special=copy.deepcopy(owner.prefs['special_hotkeys']);self.autos=copy.deepcopy(owner.prefs['auto_keys']);self.selected_sources=list(owner.sources());self.boosts=copy.deepcopy(owner.prefs['device_boosts']);super().__init__(owner,tab);self.resize(610,860)
 def group(self,layout,title):
  box=W.QGroupBox(title);box.setStyleSheet('QGroupBox {font-size:14px;font-weight:bold;border:1px solid #888;margin-top:10px;padding-top:9px;} QGroupBox::title {subcontrol-origin:margin;left:7px;}');v=W.QVBoxLayout(box);v.setSpacing(6);layout.addWidget(box);return v
 def combo(self,layout,key,label,choices):
  row=W.QHBoxLayout();row.addWidget(W.QLabel(label));c=W.QComboBox()
  for text,value in choices:c.addItem(text,value)
  c.setCurrentIndex(max(0,c.findData(self.owner.prefs[key])));row.addWidget(c,1);layout.addLayout(row);self.values[key]=c;return c
 def hotkeys(self):
  v=self.page('Hotkeys');group=self.group(v,'Special Hotkeys');self.special_table=W.QTableWidget(len(SPECIAL),3);self.special_table.setHorizontalHeaderLabels(['Action','','Hotkey']);self.special_table.verticalHeader().hide();self.special_table.setSelectionBehavior(W.QAbstractItemView.SelectionBehavior.SelectRows);self.special_table.setSelectionMode(W.QAbstractItemView.SelectionMode.SingleSelection);self.special_table.setEditTriggers(W.QAbstractItemView.EditTrigger.NoEditTriggers);self.special_table.setShowGrid(False);self.special_table.setFixedHeight(215);self.special_table.verticalHeader().setDefaultSectionSize(22);self.special_table.horizontalHeader().setDefaultAlignment(QtCore.Qt.AlignmentFlag.AlignLeft|QtCore.Qt.AlignmentFlag.AlignVCenter);self.special_table.horizontalHeader().setSectionResizeMode(0,W.QHeaderView.ResizeMode.Stretch);self.special_table.setColumnWidth(1,55);self.special_table.setColumnWidth(2,120);group.addWidget(self.special_table)
  row=W.QHBoxLayout();self.setkey=W.QPushButton('Set hotkey');self.setkey.clicked.connect(self.edit_special_key);row.addWidget(self.setkey);self.changevalue=W.QPushButton('Change value');self.changevalue.clicked.connect(self.edit_special_value);row.addWidget(self.changevalue);row.addStretch();group.addLayout(row);self.special_table.itemDoubleClicked.connect(lambda _:self.edit_special_key());self.special_table.itemSelectionChanged.connect(self.special_selection);self.render_special()
  group=self.group(v,'Numpad Hotkeys')
  for kind,label in [('numcode','Enable Alt+Numcode hotkeys'),('index','Enable Ctrl+Alt+Index hotkeys')]:
   row=W.QHBoxLayout();check=W.QCheckBox(label);check.setChecked(self.owner.prefs[kind+'_enabled']);self.values[kind+'_enabled']=check;row.addWidget(check);button=W.QPushButton('Change modifier');button.clicked.connect(lambda _,k=kind:self.modifier_dialog(k));row.addWidget(button);row.addStretch();group.addLayout(row)
  hint=W.QLabel('Hold the modifier, enter the numpad code, then release the modifier to play.');hint.setWordWrap(True);hint.setStyleSheet('color:#888;');group.addWidget(hint)
  for k in ['numcode_modifier','index_modifier']:
   self.values[k]=W.QLineEdit(self.owner.prefs[k]);self.values[k].hide()
  group=self.group(v,'Auto Keys');group.addWidget(W.QLabel('Presses keys automatically when you play sounds.'));self.auto_table=W.QTableWidget(0,2);self.auto_table.setHorizontalHeaderLabels(['Key','Type']);self.auto_table.horizontalHeader().setSectionResizeMode(W.QHeaderView.ResizeMode.Stretch);self.auto_table.verticalHeader().hide();self.auto_table.setSelectionBehavior(W.QAbstractItemView.SelectionBehavior.SelectRows);self.auto_table.setFixedHeight(100);self.auto_table.verticalHeader().setDefaultSectionSize(22);self.auto_table.setEditTriggers(W.QAbstractItemView.EditTrigger.NoEditTriggers);group.addWidget(self.auto_table);row=W.QHBoxLayout()
  for text,fn in [('Add',lambda:self.edit_auto(False)),('Edit',lambda:self.edit_auto(True)),('Remove',self.remove_auto)]:b=W.QPushButton(text);b.clicked.connect(fn);row.addWidget(b)
  row.addStretch();group.addLayout(row);self.render_auto()
  group=self.group(v,'Advanced options');self.check(group,'pass_hotkeys','Pass hotkeys');self.check(group,'auto_stop','Auto stop (recommended)');self.check(group,'left_right_modifiers','Allow left and right modifiers');self.check(group,'same_category_keys','Same hotkeys in all categories');v.addStretch()
 def render_special(self):
  for row,(ident,label,value) in enumerate(SPECIAL):
   config=self.special.get(ident,self.owner.special_config(ident));suffix='s' if ident.startswith(('back','forward')) else '%'
   for col,text in enumerate([label,'' if value is None else str(config.get('value',value))+suffix,config.get('key','')]):self.special_table.setItem(row,col,W.QTableWidgetItem(text))
   shape={'stop':'stop','pause':'pause','previous':'prev','next':'next','mode_speakers':'headphones','mode_mic':'mic','record_stop':'stop'}.get(ident)
   if ident.startswith(('down','up','volume')) or ident=='mute':shape='headphones'
   elif ident.startswith('record') and ident!='record_stop':shape='record'
   elif ident.startswith('random'):shape='shuffle'
   elif ident in ('start','selected','again','previous_played','mode_both') or ident.startswith('tts_'):shape='play'
   if shape:self.special_table.item(row,0).setIcon(icon(shape))
  self.special_selection()
 def special_selection(self):
  row=self.special_table.currentRow();self.setkey.setEnabled(row>=0);self.changevalue.setEnabled(row>=0 and SPECIAL[row][2] is not None)
 def edit_special_key(self):
  row=self.special_table.currentRow()
  if row<0:return
  ident,label,default=SPECIAL[row];config=copy.deepcopy(self.special.get(ident,self.owner.special_config(ident)));d=W.QDialog(self);d.setWindowTitle(label);v=W.QVBoxLayout(d);key=HotkeyEdit(QtGui.QKeySequence(config['key']));getattr(key,'setMaximumSequenceLength',lambda n:None)(1);v.addWidget(key);buttons=W.QDialogButtonBox(W.QDialogButtonBox.StandardButton.Ok|W.QDialogButtonBox.StandardButton.Cancel);buttons.accepted.connect(d.accept);buttons.rejected.connect(d.reject);v.addWidget(buttons)
  if d.exec():config['key']=key.keySequence().toString();self.special[ident]=config;self.render_special()
 def edit_special_value(self):
  row=self.special_table.currentRow()
  if row<0:return
  ident,label,default=SPECIAL[row]
  if default is None:return
  config=copy.deepcopy(self.special.get(ident,self.owner.special_config(ident)));value,ok=W.QInputDialog.getInt(self,label,'Seconds:' if ident.startswith(('back','forward')) else 'Percent:',config.get('value',default),0,3600 if ident.startswith(('back','forward')) else 100)
  if ok:config['value']=value;self.special[ident]=config;self.render_special()
 def modifier_dialog(self,kind):
  value,ok=W.QInputDialog.getItem(self,'Change modifier','Modifier:', ['Alt','Ctrl','Ctrl+Alt','Ctrl+Shift','Alt+Shift'],0,False)
  if ok:self.values[kind+'_modifier'].setText(value)
 def render_auto(self):
  self.auto_table.setRowCount(len(self.autos))
  for row,item in enumerate(self.autos):
   for col,text in enumerate([item['key'],{'hold':'Hold while playing','start':'Press at start','end':'Press at end'}[item['type']]]):self.auto_table.setItem(row,col,W.QTableWidgetItem(text))
 def edit_auto(self,edit):
  row=self.auto_table.currentRow()
  if edit and row<0:return
  item=self.autos[row] if edit else {'key':'','type':'hold'};d=W.QDialog(self);d.setWindowTitle('Auto Key');v=W.QVBoxLayout(d);key=HotkeyEdit(QtGui.QKeySequence(item['key']));getattr(key,'setMaximumSequenceLength',lambda n:None)(1);v.addWidget(key);kind=W.QComboBox()
  for text,value in [('Hold while playing','hold'),('Press at start','start'),('Press at end','end')]:kind.addItem(text,value)
  kind.setCurrentIndex(kind.findData(item['type']));v.addWidget(kind);buttons=W.QDialogButtonBox(W.QDialogButtonBox.StandardButton.Ok|W.QDialogButtonBox.StandardButton.Cancel);buttons.accepted.connect(d.accept);buttons.rejected.connect(d.reject);v.addWidget(buttons)
  if d.exec() and not key.keySequence().isEmpty():
   try:AutoKeys().codes(key.keySequence().toString())
   except ValueError as ex:W.QMessageBox.warning(self,'Auto Key',str(ex));return
   new={'key':key.keySequence().toString(),'type':kind.currentData()}
   if edit:self.autos[row]=new
   else:self.autos.append(new)
   self.render_auto()
 def remove_auto(self):
  row=self.auto_table.currentRow()
  if row>=0:del self.autos[row];self.render_auto()
 def interface(self):
  v=self.page('Interface');g=self.group(v,'Language');self.combo(g,'language','Language:',[('System default','system'),('English','en'),('العربية (القوائم الرئيسية)','ar')]);self.combo(g,'date_format','Date format:',[('Language default','default'),('YYYY-MM-DD','iso'),('DD/MM/YYYY','dmy'),('MM/DD/YYYY','mdy')])
  g=self.group(v,'Toolbar');self.combo(g,'dark_mode','Style:',[('Light',False),('Dark',True)]);self.combo(g,'zoom','Zoom:',[(f'{n}%',n) for n in [75,100,125,150,175,200]]);self.combo(g,'toolbar_size','Toolbar size:',[('Small',18),('Medium',24),('Large',32)]);self.combo(g,'icon_color','Toolbar and icon color:',[('Blue','#0762a7'),('Green','#178a58'),('Orange','#db8400'),('Red','#c93441'),('White','#dddddd')])
  row=W.QHBoxLayout();row.addWidget(W.QLabel('Playback position update rate:'));rate=W.QSlider(QtCore.Qt.Orientation.Horizontal);rate.setRange(1,144);rate.setValue(self.owner.prefs['progress_rate']);label=W.QLabel(str(rate.value())+'/s');rate.valueChanged.connect(lambda n:label.setText(str(n)+'/s'));row.addWidget(rate);row.addWidget(label);g.addLayout(row);self.values['progress_rate']=rate
  self.check(g,'show_mic_level','Display microphone level on toolbar');self.check(g,'linear_volume','Volume slider works linearly')
  g=self.group(v,'Play mode');text=W.QLabel('Determines whether sounds play on speakers, microphone, or both when played by hotkey, double-click, Enter or Play.');text.setWordWrap(True);g.addWidget(text);self.check(g,'resume_mode','Allow resume in another play mode');self.combo(g,'mode_cycle','Hotkey switches between:',[('default and speakers','speakers'),('default and microphone','mic'),('all play modes','all')])
  g=self.group(v,'Misc')
  for key,text in [('autostart','Run soundboard when Linux starts'),('start_minimized','Start minimized'),('show_tray','Show system tray icon'),('minimize_tray','Minimize to system tray'),('close_tray','Minimize instead of close'),('always_top','Show always on top'),('disable_sorting','Disable sorting in sound list'),('alternating','Use alternating row colors'),('typing_search','Typing begins search'),('typing_tts','Typing prefers TTS when TTS is open'),('remember_state','Remember playback state throughout application starts')]:self.check(g,key,text)
  self.values['remember_state'].setToolTip('Restores the last clip and position in a paused state, without broadcasting audio at login.');v.addStretch()
 def device_tab(self):
  v=self.page('Devices');g=self.group(v,'Playback device');self.out=W.QComboBox();self.out.addItem('Default','');g.addWidget(self.out)
  for d in devices('sinks'):
   if not d['name'].startswith((PREFIX,'codex_soundpad_')):self.out.addItem(d.get('description',d['name']),d['name'])
  self.out.setCurrentIndex(max(0,self.out.findData(self.owner.output if not self.owner.prefs.get('default_output') else '')))
  g=self.group(v,'Recording devices');g.addWidget(W.QLabel('Sounds will be played on the recording devices selected here.'));self.check(g,'device_check','Check device configuration on startup');g.addWidget(W.QLabel('Check at least one device:'))
  self.device_table=W.QTableWidget(0,4);self.device_table.setHorizontalHeaderLabels(['Use','Device','Boost','Status']);self.device_table.verticalHeader().hide();self.device_table.horizontalHeader().setSectionResizeMode(1,W.QHeaderView.ResizeMode.Stretch);self.device_table.setColumnWidth(0,34);self.device_table.setColumnWidth(2,72);self.device_table.setColumnWidth(3,60);self.device_table.setMinimumHeight(400);self.device_table.setEditTriggers(W.QAbstractItemView.EditTrigger.NoEditTriggers);g.addWidget(self.device_table,1)
  row=W.QHBoxLayout();self.device_status=W.QLabel();row.addWidget(self.device_status,1);refresh=W.QPushButton('⟳');refresh.clicked.connect(self.populate_devices);row.addWidget(refresh);g.addLayout(row);self.populate_devices()
  self.check(g,'link','Automatically link applications using the selected devices');self.check(g,'mic_voice','Include physical microphone voice')
 def populate_devices(self):
  if self.device_table.rowCount():
   self.selected_sources=[self.device_table.item(r,1).data(QtCore.Qt.ItemDataRole.UserRole) for r in range(self.device_table.rowCount()) if self.device_table.cellWidget(r,0).isChecked()]
   self.boosts={self.device_table.item(r,1).data(QtCore.Qt.ItemDataRole.UserRole):self.device_table.cellWidget(r,2).value() for r in range(self.device_table.rowCount())}
  self.device_table.setRowCount(0)
  for d in devices('sources'):
   name=d['name']
   if name.endswith('.monitor') or name.startswith((PREFIX,'codex_soundpad_')):continue
   row=self.device_table.rowCount();self.device_table.insertRow(row);check=W.QCheckBox();check.setChecked(name in self.selected_sources);self.device_table.setCellWidget(row,0,check);item=W.QTableWidgetItem('Microphone\n('+d.get('description',name)+')');item.setData(QtCore.Qt.ItemDataRole.UserRole,name);self.device_table.setItem(row,1,item);boost=W.QDoubleSpinBox();boost.setRange(-30,20);boost.setSuffix(' dB');boost.setValue(self.boosts.get(name,0));self.device_table.setCellWidget(row,2,boost);status=W.QTableWidgetItem('Good');status.setForeground(QtGui.QColor('#16a35c'));self.device_table.setItem(row,3,status);self.device_table.setRowHeight(row,56)
  self.device_status.setText('Status: '+('Good' if self.device_table.rowCount() else 'No recording devices'))
 def recorder(self):
  v=self.page('Recorder');g=self.group(v,'Recorder');self.path(g,'rec_folder','Save recordings in:',False);fmt=self.combo(g,'rec_format','Save as:',[('WAV','wav'),('MP3','mp3'),('M4A','m4a'),('FLAC','flac'),('OGG','ogg')]);bit=self.combo(g,'rec_bitrate','Bitrate:',[(str(n)+' Kbps',n) for n in [64,96,128,192,256,320]]);bit.setEnabled(fmt.currentData() in ['mp3','m4a','ogg']);fmt.currentIndexChanged.connect(lambda _:bit.setEnabled(fmt.currentData() in ['mp3','m4a','ogg']))
  self.combo(g,'rec_category','Add to category:',[('Selected category','selected')]+[(c,c) for c in self.owner.categories]);self.combo(g,'rec_position','Insert position in sound list:',[('End','end'),('Beginning','beginning'),('After selected sound','after')]);self.check(g,'rec_normalize','Automatically normalize recordings to:');self.spin(g,'rec_db','Reference level:',60,94,' dB',1);self.check(g,'rec_trim','Trim silence');self.spin(g,'rec_max_seconds','Maximum recording time in seconds:',1,86400);v.addStretch()
 def collect(self):
  p=copy.deepcopy(self.owner.prefs)
  for key,w in self.values.items():
   if isinstance(w,W.QComboBox):p[key]=w.currentData()
   elif isinstance(w,W.QCheckBox):p[key]=w.isChecked()
   elif isinstance(w,(W.QSpinBox,W.QDoubleSpinBox,W.QSlider)):p[key]=w.value()
   elif isinstance(w,W.QKeySequenceEdit):p[key]=w.keySequence().toString()
   else:p[key]=w.text()
  p.update(normalization='dynamic' if self.dynamic.isChecked() else 'fixed',duck_percent=self.duck.currentData(),special_hotkeys=self.special,auto_keys=self.autos)
  p['selected_sources']=[];p['device_boosts']={}
  for row in range(self.device_table.rowCount()):
   name=self.device_table.item(row,1).data(QtCore.Qt.ItemDataRole.UserRole)
   if self.device_table.cellWidget(row,0).isChecked():p['selected_sources'].append(name)
   p['device_boosts'][name]=self.device_table.cellWidget(row,2).value()
  p['default_output']=not self.out.currentData();return p
 def apply(self):
  p=self.collect()
  if p['link'] and not p['selected_sources']:W.QMessageBox.warning(self,'Recording devices','Select at least one microphone.');return False
  if p['activation'] and not Path(p['activation_file']).is_file():W.QMessageBox.warning(self,'Activation sound','Choose an existing file.');return False
  if p['auto_keys'] and not os.access('/dev/uinput',os.W_OK):W.QMessageBox.warning(self,'Auto Keys','The current user has no access to /dev/uinput.');return False
  globalkeys={e.get('hotkey') for e in self.owner.entries}|{i.get('key') for i in p['special_hotkeys'].values()}
  if any(i['key'] in globalkeys for i in p['auto_keys']):W.QMessageBox.warning(self,'Auto Keys','An Auto Key cannot also be a soundboard hotkey.');return False
  old=copy.deepcopy(self.owner.prefs);self.owner.stop();self.owner.prefs=p;self.owner.output=self.out.currentData() or pa('get-default-sink');self.owner.source=p['selected_sources'][0] if p['selected_sources'] else None
  for name,boost in p['device_boosts'].items():
   if boost!=old['device_boosts'].get(name,0):
    try:pa('set-source-volume',name,f'{boost}dB')
    except Exception as ex:W.QMessageBox.warning(self,'Microphone boost',str(ex))
  self.owner.configure_bridge();self.owner.prepare_auto_keys();self.owner.apply_interface();self.owner.save();self.owner.render();self.owner.install_hotkeys();return True
 def reset(self):
  for key,w in self.values.items():
   value=DEFAULTS.get(key)
   if isinstance(w,W.QComboBox):w.setCurrentIndex(max(0,w.findData(value)))
   elif isinstance(w,W.QCheckBox):w.setChecked(bool(value))
   elif isinstance(w,(W.QSpinBox,W.QDoubleSpinBox,W.QSlider)):w.setValue(value)
   elif isinstance(w,W.QKeySequenceEdit):w.setKeySequence(QtGui.QKeySequence(value))
   elif value is not None:w.setText(str(value))
  self.special={i:{'key':'','value':v} for i,_,v in SPECIAL};self.autos=[];self.render_special();self.render_auto();self.dynamic.setChecked(True);self.duck.setCurrentIndex(1)

class SoundTable(W.QTableWidget):
 MIME='application/x-ziyad-soundboard-rows'
 def __init__(self,owner):
  super().__init__(0,4);self.owner=owner;self.drop_boundary=None;self.drag_position=None;self.setDragEnabled(True);self.setAcceptDrops(True);self.setDragDropMode(W.QAbstractItemView.DragDropMode.DragDrop);self.setDefaultDropAction(QtCore.Qt.DropAction.MoveAction);self.setDragDropOverwriteMode(False);self.setAutoScroll(False);self.setDropIndicatorShown(False);self.setAutoScrollMargin(30);self.drag_scroll=QtCore.QTimer(self);self.drag_scroll.setInterval(60);self.drag_scroll.timeout.connect(self.scroll_drag)
 def update_drop_position(self,pos):
  # Drag events use viewport coordinates. QCursor.pos() can lag behind them
  # during a native Wayland drag, so never mix it into the indicator position.
  self.drag_position=QtCore.QPoint(pos);boundary=self.boundary(pos)
  if boundary!=self.drop_boundary:self.drop_boundary=boundary;self.viewport().update()
  near_edge=0<=pos.x()<self.viewport().width() and (0<=pos.y()<30 or self.viewport().height()-30<pos.y()<self.viewport().height())
  if near_edge:
   if not self.drag_scroll.isActive():self.drag_scroll.start()
  else:self.drag_scroll.stop()
 def clear_drop_position(self):
  self.drag_scroll.stop();self.drag_position=None;self.drop_boundary=None;self.viewport().update()
 def scroll_drag(self):
  pos=self.drag_position
  if pos is None:self.drag_scroll.stop();return
  bar=self.verticalScrollBar();old=bar.value()
  if pos.y()<30:bar.setValue(old-bar.singleStep())
  elif pos.y()>self.viewport().height()-30:bar.setValue(old+bar.singleStep())
  if bar.value()!=old:self.update_drop_position(pos)
 def startDrag(self,actions):
  ids=[self.item(i.row(),0).data(QtCore.Qt.ItemDataRole.UserRole) for i in sorted(self.selectionModel().selectedRows(),key=lambda i:i.row())]
  if not ids:return
  mime=QtCore.QMimeData();mime.setData(self.MIME,json.dumps(ids).encode());drag=QtGui.QDrag(self);drag.setMimeData(mime);drag.exec(QtCore.Qt.DropAction.MoveAction);self.clear_drop_position()
 def internal(self,event):return event.source() is self and event.mimeData().hasFormat(self.MIME)
 def boundary(self,pos):
  row=self.rowAt(pos.y())
  if row<0:return 0 if pos.y()<0 else self.rowCount()
  rect=self.visualRect(self.model().index(row,0));return row+(pos.y()>=rect.center().y())
 def dragEnterEvent(self,event):
  if self.internal(event):event.setDropAction(QtCore.Qt.DropAction.MoveAction);event.accept()
  elif event.mimeData().hasUrls():event.acceptProposedAction()
  else:event.ignore()
 def dragMoveEvent(self,event):
  if self.internal(event):
   self.update_drop_position(event.position().toPoint());event.setDropAction(QtCore.Qt.DropAction.MoveAction);event.accept()
  elif event.mimeData().hasUrls():event.acceptProposedAction()
  else:event.ignore()
 def dragLeaveEvent(self,event):self.clear_drop_position();event.accept()
 def dropEvent(self,event):
  self.drag_scroll.stop()
  if self.internal(event):
   ids=json.loads(bytes(event.mimeData().data(self.MIME)));self.owner.reorder_sounds(ids,self.boundary(event.position().toPoint()));event.setDropAction(QtCore.Qt.DropAction.MoveAction);event.accept()
  elif event.mimeData().hasUrls():self.owner.add_paths([u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]);event.acceptProposedAction()
  else:event.ignore()
  self.clear_drop_position()
 def paintEvent(self,event):
  super().paintEvent(event)
  if self.drop_boundary is not None:
   row=self.drop_boundary
   if not self.rowCount():y=1
   elif row>=self.rowCount():y=self.visualRect(self.model().index(self.rowCount()-1,0)).bottom()
   else:y=self.visualRect(self.model().index(row,0)).top()
   painter=QtGui.QPainter(self.viewport());painter.setPen(QtGui.QPen(QtGui.QColor('#3298ff'),2));painter.drawLine(0,y,self.viewport().width(),y)

class Window(BaseWindow):
 def __init__(self):
  self.route_future=None;self.auto_injector=AutoKeys();self.auto_active=False;self.hotbar_page=0;self.record_dialog=None;self.speech_dialog=None;self.tray=None;self.quitting=False;self.meter_process=None;self.restoring=False;self.number_buffer='';self.number_kind='';self.last_tts=None
  super().__init__()
  self.number_timer=QtCore.QTimer(self);self.number_timer.setSingleShot(True);self.number_timer.timeout.connect(self.finish_number)
  self.level=W.QProgressBar();self.level.setRange(0,100);self.level.setTextVisible(False);self.level.setFixedWidth(60);self.level.setFixedHeight(12);self.level_action=self.findChild(W.QToolBar).addWidget(self.level)
  self.meter_timer=QtCore.QTimer(self);self.meter_timer.timeout.connect(self.meter_tick);self.meter_timer.start(50)
  self.table.horizontalHeader().sectionClicked.connect(self.sort_column)
  self.table.installEventFilter(self);self.installEventFilter(self)
  self.apply_interface();self.prepare_auto_keys()
  for action in self.menuBar().actions()[0].menu().actions():
   if action.property('english_text')=='Exit' or action.text()=='Exit':
    action.triggered.disconnect();action.triggered.connect(self.quit_app)
  if self.prefs['device_check']:QtCore.QTimer.singleShot(100,self.check_devices)
  QtCore.QTimer.singleShot(200,self.restore_session)
  QtCore.QTimer.singleShot(250,self.initial_visibility)
 def check_devices(self):
  available={d['name'] for d in devices('sources')};missing=[n for n in self.sources() if n not in available]
  if missing:self.status.setText('Recording devices disconnected: '+', '.join(missing))
  elif self.prefs['link'] and not self.bridge.modules:self.status.setText('Microphone connection needs attention — open Preferences → Devices.')
 def reorder_sounds(self,ids,boundary):
  visible=self.visible();selected=set(ids);moving=[e for e in visible if e['id'] in selected]
  if not moving:return
  boundary=max(0,min(len(visible),boundary));insert=sum(e['id'] not in selected for e in visible[:boundary]);remaining=[e for e in visible if e['id'] not in selected];ordered=remaining[:insert]+moving+remaining[insert:]
  if [e['id'] for e in ordered]==[e['id'] for e in visible]:return
  self.checkpoint();visible_ids={e['id'] for e in visible};iterator=iter(ordered);self.entries=[next(iterator) if e['id'] in visible_ids else e for e in self.entries];self.sort_state=None;self.changed();self.table.clearSelection()
  for row,e in enumerate(self.visible()):
   if e['id'] in selected:self.table.selectionModel().select(self.table.model().index(row,0),QtCore.QItemSelectionModel.SelectionFlag.Select|QtCore.QItemSelectionModel.SelectionFlag.Rows)
  self.summary();self.status.setText('Sound order saved')
 def sources(self):return self.prefs.get('selected_sources') or ([self.source] if self.source else [])
 def special_config(self,ident):
  default=next((v for i,l,v in SPECIAL if i==ident),None);value={'key':'','value':default}
  if ident=='stop':value['key']=self.prefs.get('stop_hotkey','')
  if ident=='pause':value['key']=self.prefs.get('pause_hotkey','')
  value.update(self.prefs.get('special_hotkeys',{}).get(ident,{}));return value
 def special_action(self,ident):
  value=self.special_config(ident)['value']
  direct={'stop':self.stop,'start':self.play_selected,'selected':self.play_selected,'pause':self.pause,'previous':lambda:self.step(-1),'next':lambda:self.step(1),'random':lambda:self.random_play(self.prefs['mode']),'random_all':lambda:self.random_play(self.prefs['mode'],True),'mute':lambda:self.monitor_action.trigger(),'mode_cycle':self.cycle_mode,'record_stop':self.stop_recording}
  if ident in direct:direct[ident]();return
  if ident in ('again','previous_played'):
   key=self.now['id'] if ident=='again' and self.now else (self.history[-2] if ident=='previous_played' and len(self.history)>1 else self.history[-1] if self.history else None)
   if key:self.stop();self.play_id(key)
  elif ident.startswith('select_'):self.select_step(-1 if ident.endswith('previous') else 1)
  elif ident.startswith('category_'):
   current=self.categories.index(self.current_category) if self.current_category in self.categories else 0;self.current_category=self.categories[(current+(-1 if ident.endswith('previous') else 1))%len(self.categories)];self.render();self.install_hotkeys()
  elif ident.startswith(('back','forward')):
   if self.now:
    position=max(0,self.elapsed()+(-value if ident.startswith('back') else value));self.seek.setValue(round(position/max(.001,self.now.get('duration',0))*self.seek.maximum()));self.seek_to()
  elif ident.startswith(('up','down')):self.volume.setValue(self.volume.value()+(-value if ident.startswith('down') else value))
  elif ident.startswith('volume'):self.volume.setValue(value)
  elif ident.startswith('mode_'):self.set_mode(ident[5:])
  elif ident.startswith('keys_'):
   enabled=not self.hotkey_button.isChecked() if ident=='keys_toggle' else ident=='keys_enable';self.hotkey_button.setChecked(enabled)
  elif ident.startswith('auto_'):
   self.prefs['auto_keys_enabled']=not self.prefs['auto_keys_enabled'] if ident=='auto_toggle' else ident=='auto_enable'
   if not self.prefs['auto_keys_enabled']:self.auto_end(False)
  elif ident.startswith('record'):self.start_recording(ident)
  elif ident.startswith('tts_'):self.play_tts(ident[4:])
  elif ident.startswith('hotbar_'):
   pages=max(1,math.ceil(len(self.visible())/12));self.hotbar_page=(self.hotbar_page+(-1 if ident.endswith('previous') else 1))%pages;self.hotbar.show();self.refresh_hotbar()
 def select_step(self,step):
  count=self.table.rowCount()
  if count:self.table.selectRow((max(0,self.table.currentRow())+step)%count)
 def refresh_hotbar(self):
  self.hotbar.clear();visible=self.visible();start=(getattr(self,'hotbar_page',0)*12)%max(1,len(visible))
  for e in visible[start:start+12]:self.hotbar.addAction(e['tag'],lambda ident=e['id']:self.play_id(ident))
 def install_hotkeys(self):
  if not hasattr(self,'hotkeys'):return
  errors=self.hotkeys.install()
  if errors:self.status.setText('Hotkey: '+errors[0])
 def category_changed(self):super().category_changed();self.install_hotkeys()
 def number_key(self,kind,digit):
  if self.number_kind!=kind:self.number_buffer=''
  self.number_kind=kind;self.number_buffer=(self.number_buffer+str(digit))[-6:];self.status.setText(kind+': '+self.number_buffer);self.number_timer.start(10000 if self.hotkeys.raw.devices else 650)
  if self.hotkeys.raw.devices:
   required=set(AutoKeys().codes(self.prefs[kind+'_modifier']+'+A')[:-1])
   if not required.issubset(self.hotkeys.raw.modifiers(self.hotkeys.raw.down())):self.number_timer.start(35)
 def finish_number(self):
  number=self.number_buffer;self.number_buffer=''
  if not number:return
  if self.number_kind=='index':
   index=int(number)-1;items=self.visible()
   if 0<=index<len(items):self.play_id(items[index]['id'])
  else:
   matches=[e for e in self.entries if str(e.get('numcode',''))==number and (not self.prefs['same_category_keys'] or e['category']==self.current_category)]
   if matches:self.play_id(matches[0]['id'])
 def set_hotkey(self):
  selected=self.selected()
  if len(selected)!=1:W.QMessageBox.information(self,'Set hotkey','Select one sound.');return
  e=selected[0];d=W.QDialog(self);d.setWindowTitle('Set hotkey');v=W.QFormLayout(d);key=HotkeyEdit(QtGui.QKeySequence(e.get('hotkey','')));getattr(key,'setMaximumSequenceLength',lambda n:None)(1);v.addRow('Hotkey',key);code=W.QLineEdit(str(e.get('numcode','')));code.setValidator(QtGui.QRegularExpressionValidator(QtCore.QRegularExpression('[0-9]{0,6}')));v.addRow('Alt + Numcode',code);buttons=W.QDialogButtonBox(W.QDialogButtonBox.StandardButton.Ok|W.QDialogButtonBox.StandardButton.Cancel);buttons.accepted.connect(d.accept);buttons.rejected.connect(d.reject);v.addRow(buttons)
  if not d.exec():return
  text=key.keySequence().toString();num=code.text()
  for other in self.entries:
   if other['id']==e['id'] or (self.prefs['same_category_keys'] and other['category']!=e['category']):continue
   if (text and other.get('hotkey')==text) or (num and other.get('numcode')==num):W.QMessageBox.warning(self,'Hotkey','This shortcut or code is already assigned.');return
  self.checkpoint();e.update(hotkey=text,numcode=num);self.changed()
 def prepare_auto_keys(self):
  if self.prefs['auto_keys']:
   try:self.auto_injector.open()
   except OSError as ex:self.status.setText('Auto Keys: '+str(ex))
 def auto_start(self,resume=False):
  if not self.prefs['auto_keys_enabled'] or self.auto_active:return
  try:
   for item in self.prefs['auto_keys']:
    if item['type']=='hold':self.auto_injector.press(item['key'],True)
    elif item['type']=='start' and not resume:self.auto_injector.press(item['key'])
   self.auto_active=True
  except Exception as ex:self.auto_injector.release();self.auto_active=False;self.status.setText('Auto Keys: '+str(ex))
 def auto_end(self,end=True):
  active=self.auto_active;self.auto_injector.release();self.auto_active=False
  if active and end and self.prefs['auto_keys_enabled']:
   for item in self.prefs['auto_keys']:
    if item['type']=='end':
     try:self.auto_injector.press(item['key'])
     except Exception as ex:self.status.setText('Auto Keys: '+str(ex))
 def play_id(self,ident,mode=None):
  if self.paused and self.now and self.now['id']==ident and self.prefs['resume_mode']:
   e=self.now;position=self.elapsed();self.stop_players();self.start_players(e,mode or self.prefs['mode'],position,False);return
  super().play_id(ident,mode)
 def start_players(self,e,mode,offset=0,count=True):
  self.auto_start();super().start_players(e,mode,offset,count)
  if self.now:self.started=time.monotonic()
  if not self.now:self.auto_end(False)
 def pause(self):
  # A restored session has no decoder process until the user resumes.
  if self.restoring and self.now:
   e=self.now;position=self.position;self.restoring=False;self.start_players(e,self.current_mode,position,False);return
  super().pause()
  if self.paused:self.auto_end(False)
  elif self.now:self.auto_start(True)
 def stop(self):
  self.restoring=False
  if hasattr(self,'auto_injector'):self.auto_end()
  super().stop()
 def cycle_mode(self):
  modes=['both','speakers','mic'] if self.prefs['mode_cycle']=='all' else ['both',self.prefs['mode_cycle']];self.set_mode(modes[(modes.index(self.prefs['mode'])+1)%len(modes)] if self.prefs['mode'] in modes else 'both')
 def wait_for_routing(self):
  if self.route_future:
   try:self.route_future.result()
   except Exception:pass
   self.route_future=None
 def route_apps(self):
  if not self.bridge.modules:return
  if self.route_future:
   if not self.route_future.done():return
   try:
    apps=self.route_future.result();self.route_label.setText('Linked: '+', '.join(apps) if apps else 'Mic link ready');self.route_label.setToolTip(self.source or '')
   except Exception as ex:self.route_label.setText('Mic disconnected');self.route_label.setToolTip(str(ex))
   self.route_future=None
  self.sync_voice_block()
  if self.now and self.prefs['duck']:self.duck_audio()
  self.route_future=self.pool.submit(self.bridge.route_apps)
 def configure_bridge(self):
  self.wait_for_routing();self.stop();self.bridge.stop()
  if self.prefs['link'] and self.sources():
   try:self.bridge.start(self.sources()[0] if self.prefs['mic_voice'] else None,self.output,False,route_source=self.sources());self.route_apps()
   except Exception as ex:self.status.setText('Microphone routing: '+str(ex))
  else:self.route_label.setText('Mic link disabled')
 def monitor_gain(self):
  value=self.prefs['volume'];return value if self.prefs['linear_volume'] else round(100*(value/100)**2)
 def apply_monitor_volume(self):
  level=0 if self.prefs['monitor_muted'] else self.monitor_gain()
  for p in self.players:
   if getattr(p,'output_kind',None)=='speakers':
    try:p.volume(level)
    except Exception:pass
  self.refresh_monitor_icon()
 def apply_interface(self):
  global ICON_COLOR
  ICON_COLOR=self.prefs['icon_color'];apply_theme(self.prefs['dark_mode']);zoom=self.prefs['zoom']/100
  stylesheet=W.QApplication.instance().styleSheet();stylesheet=re.sub(r'font-size:(\d+)px',lambda m:'font-size:'+str(round(int(m[1])*zoom))+'px',stylesheet) if 're' in globals() else stylesheet;W.QApplication.instance().setStyleSheet(stylesheet)
  toolbar=self.findChild(W.QToolBar);toolbar.setIconSize(QtCore.QSize(round(self.prefs['toolbar_size']*zoom),round(self.prefs['toolbar_size']*zoom)))
  names=['play','headphones','mic','pause','stop','prev','next']
  for action,name in zip([a for a in toolbar.actions() if not a.icon().isNull()][:7],names):action.setIcon(icon(name))
  self.refresh_monitor_icon();self.progress_timer.setInterval(max(7,round(1000/self.prefs['progress_rate'])))
  visible=self.isVisible();self.setWindowFlag(QtCore.Qt.WindowType.WindowStaysOnTopHint,self.prefs['always_top'])
  if visible:self.show()
  if self.tray is None:
   self.tray=W.QSystemTrayIcon(app_icon(),self);self.tray.setToolTip('soundpad like');menu=W.QMenu(self);menu.addAction('Show',self.restore_window);menu.addAction('Play / pause',self.pause);menu.addAction('Stop',self.stop);menu.addAction('Exit',self.quit_app);self.tray.setContextMenu(menu);self.tray.activated.connect(lambda reason:self.restore_window() if reason in (W.QSystemTrayIcon.ActivationReason.Trigger,W.QSystemTrayIcon.ActivationReason.DoubleClick) else None)
  self.tray.setVisible(self.prefs['show_tray']);self.install_autostart()
  if hasattr(self,'level'):self.level_action.setVisible(self.prefs['show_mic_level']);self.set_meter(self.prefs['show_mic_level'])
  self.translate_ui()
 def install_autostart(self):
  folder=Path(os.environ.get('XDG_CONFIG_HOME',str(Path.home()/'.config')))/'autostart';path=folder/'ziyad-soundboard.desktop'
  if self.prefs['autostart']:
   folder.mkdir(parents=True,exist_ok=True);path.write_text('[Desktop Entry]\nType=Application\nName=soundpad like\nExec="'+os.environ.get('APPIMAGE',str(Path.home()/'Desktop/soundpad-like-x86_64.AppImage'))+'"\nTerminal=false\nX-GNOME-Autostart-enabled=true\n')
  elif path.exists():path.unlink()
 def initial_visibility(self):
  if self.prefs['start_minimized']:
   if self.prefs['minimize_tray'] and self.prefs['show_tray']:self.hide()
   else:self.showMinimized()
 def restore_window(self):self.showNormal();self.raise_();self.activateWindow()
 def changeEvent(self,event):
  super().changeEvent(event)
  if event.type()==QtCore.QEvent.Type.WindowStateChange and self.isMinimized() and self.prefs.get('minimize_tray') and self.prefs.get('show_tray'):QtCore.QTimer.singleShot(0,self.hide)
 def eventFilter(self,obj,event):
  key_event=event.type() in (QtCore.QEvent.Type.ShortcutOverride,QtCore.QEvent.Type.KeyPress)
  key,mods=normalized_key(event) if key_event else (0,QtCore.Qt.KeyboardModifier.NoModifier)
  if obj in (self,self.table) and key_event and mods&QtCore.Qt.KeyboardModifier.KeypadModifier:
   if event.type()==QtCore.QEvent.Type.ShortcutOverride:event.accept();return True
   if not event.isAutoRepeat() and not self.hotkeys.raw.devices:
    for text,fn in self.hotkeys.keypad_bindings:
     combo=QtGui.QKeySequence(text)[0]
     if int(combo.key())==key and combo.keyboardModifiers()==mods:fn();break
   event.accept();return True

  if event.type()==QtCore.QEvent.Type.KeyPress and obj in (self,self.table) and event.text() and event.text().isprintable() and not event.modifiers()&(QtCore.Qt.KeyboardModifier.ControlModifier|QtCore.Qt.KeyboardModifier.AltModifier|QtCore.Qt.KeyboardModifier.MetaModifier):
   if event.text()==' ':return False
   if self.prefs.get('typing_tts') and self.speech_dialog and self.speech_dialog.isVisible():self.speech_dialog.text.setFocus();self.speech_dialog.text.insertPlainText(event.text());return True
   if self.prefs.get('typing_search'):self.search.show();self.search.setFocus();self.search.setText(self.search.text()+event.text());return True
  return super().eventFilter(obj,event)
 def sort_column(self,col):
  if self.prefs['disable_sorting']:return
  self.checkpoint();reverse=getattr(self,'sort_state',None)==(col,False);self.sort_state=(col,reverse)
  key=lambda e:e.get(['id','tag','duration','hotkey'][col],0 if col==2 else '')
  self.entries.sort(key=key,reverse=reverse);self.changed()
 def save(self):
  # The baseline persists prefs atomically; retain session state there as well.
  if self.prefs.get('remember_state') and self.now:self.prefs['session']={'id':self.now['id'],'position':self.elapsed(),'mode':self.current_mode}
  super().save()
 def restore_session(self):
  session=self.prefs.get('session',{})
  if self.prefs['remember_state'] and session:
   e=next((e for e in self.entries if e['id']==session.get('id')),None)
   if e:self.now=e;self.position=session.get('position',0);self.started=self.pause_time=time.monotonic();self.paused=True;self.restoring=True;self.current_mode=session.get('mode','both');self.select_id(e['id']);self.status.setText('Restored — paused');self.update_progress()
 def quit_app(self):self.quitting=True;self.close()
 def closeEvent(self,event):
  if self.prefs.get('close_tray') and self.prefs.get('show_tray') and not self.quitting:self.hide();event.ignore();return
  self.wait_for_routing();self.save();self.stop();self.auto_injector.close();self.set_meter(False)
  if hasattr(self,'meter_timer'):self.meter_timer.stop()
  if self.tray:self.tray.hide()
  super().closeEvent(event)
 def set_meter(self,enabled):
  if self.meter_process:
   self.meter_process.terminate()
   try:self.meter_process.wait(timeout=1)
   except subprocess.TimeoutExpired:self.meter_process.kill();self.meter_process.wait()
   self.meter_process=None
  if enabled and self.source:
   self.meter_process=subprocess.Popen(['parec','--device='+self.source,'--format=s16le','--rate=16000','--channels=1','--latency-msec=40'],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL);os.set_blocking(self.meter_process.stdout.fileno(),False)
 def meter_tick(self):
  if not self.meter_process:return
  try:data=os.read(self.meter_process.stdout.fileno(),65536)
  except (BlockingIOError,OSError):return
  if data:
   samples=array.array('h',data[:len(data)//2*2]);peak=max(abs(x) for x in samples)/32768;db=20*math.log10(max(.001,peak));self.level.setValue(round((db+60)*100/60))
 def start_recording(self,kind='record'):
  if self.record_dialog and self.record_dialog.isVisible():
   self.record_dialog.raise_()
   if self.record_dialog.proc:return
  else:self.record_dialog=Recorder(self);self.dialogs.append(self.record_dialog)
  source=self.output+'.monitor' if kind=='record_speakers' else self.source
  index=self.record_dialog.source.findData(source)
  if index>=0:self.record_dialog.source.setCurrentIndex(index)
  self.record_dialog.show();self.record_dialog.start()
 def stop_recording(self):
  if self.record_dialog:self.record_dialog.stop()
 def recorder(self):
  self.record_dialog=Recorder(self);self.dialogs.append(self.record_dialog);self.record_dialog.show()
 def tts(self):
  if self.speech_dialog and self.speech_dialog.isVisible():self.speech_dialog.raise_();return
  self.speech_dialog=Speech(self);self.dialogs.append(self.speech_dialog);self.speech_dialog.show()
 def play_tts(self,mode):
  if self.speech_dialog and self.speech_dialog.text.toPlainText().strip():self.speech_dialog.pending_mode=mode;self.speech_dialog.generate()
  elif self.last_tts:self.play_id(self.last_tts,mode)
  else:self.tts();self.status.setText('Enter text in the TTS window first.')
 def fill_backups(self):
  self.restore_menu.clear()
  for path in reversed(sorted((DATA/'backups').glob('*.json'))):
   stamp=datetime.fromtimestamp(path.stat().st_mtime);fmt={'default':'%x %X','iso':'%Y-%m-%d %H:%M:%S','dmy':'%d/%m/%Y %H:%M:%S','mdy':'%m/%d/%Y %H:%M:%S'}[self.prefs['date_format']];self.act(self.restore_menu,stamp.strftime(fmt),lambda checked=False,p=str(path):self.load_list(p))
 def translate_ui(self):
  lang=self.prefs['language'];arabic=lang=='ar' or lang=='system' and QtCore.QLocale.system().language()==QtCore.QLocale.Language.Arabic
  dictionary={'File':'ملف','Edit':'تحرير','Play':'تشغيل','Window':'نافذة','Help':'مساعدة','Preferences':'التفضيلات','Add sound files':'إضافة ملفات صوت','New sound list':'قائمة جديدة','Load sound list':'فتح قائمة','Save sound list':'حفظ القائمة','Save sound list as…':'حفظ باسم…','Exit':'خروج','Undo':'تراجع','Redo':'إعادة','Cut':'قص','Copy':'نسخ','Paste':'لصق','Remove selected entries':'إزالة المحدد','Shuffle list':'خلط القائمة','Select all':'تحديد الكل','Search':'بحث','Stop playback':'إيقاف التشغيل','Pause/resume playback':'إيقاف مؤقت / استئناف','Play previous file':'المقطع السابق','Play next file':'المقطع التالي','Categories':'التصنيفات','Hotkeys':'الاختصارات','Hotbar':'الشريط السريع','Sound recorder':'مسجل الصوت','Color tool':'الألوان','Text to speech':'تحويل النص إلى صوت','About':'حول البرنامج','How to use':'طريقة الاستخدام','Stats':'الإحصائيات','Export':'تصدير','Load recent sound list':'القوائم الأخيرة','Restore sound list':'استعادة قائمة'}
  for action in self.findChildren(QtGui.QAction):
   original=action.property('english_text')
   if original is None:original=action.text();action.setProperty('english_text',original)
   action.setText(dictionary.get(original,original) if arabic else original)
  self.table.setHorizontalHeaderLabels(['الرقم','الاسم','المدة','الاختصار'] if arabic else ['Index','Tag','Duration','Hotkey']);self.tree.setHeaderLabels(['التصنيف','#'] if arabic else ['Category','#'])

class Recorder(BaseRecorder):
 def __init__(self,owner):
  self.encoder=None;self.raw=None;self.target_category=None;self.after_id=None;super().__init__(owner)
 def start(self):
  if self.proc or self.encoder and self.encoder.state()!=QtCore.QProcess.ProcessState.NotRunning:return
  try:
   self.target_category=self.owner.current_category if self.owner.prefs['rec_category']=='selected' else self.owner.prefs['rec_category']
   if self.target_category=='All sounds':self.target_category='My Sounds'
   self.after_id=self.owner.selected()[0]['id'] if self.owner.selected() else None
   super().start()
  except Exception as ex:self.status.setText(str(ex))
 def tick(self):
  super().tick()
  if self.proc and time.monotonic()-self.start_time>=self.owner.prefs['rec_max_seconds']:self.stop()
 def stop(self):
  if not self.proc:return
  if self.proc.poll() is None:
   self.proc.send_signal(signal.SIGINT)
   try:self.proc.wait(timeout=2)
   except subprocess.TimeoutExpired:self.proc.kill();self.proc.wait()
  self.proc=None;self.timer.stop();self.finish.setEnabled(False);self.record.setEnabled(False);self.source.setEnabled(True)
  if not self.path or not self.path.exists() or self.path.stat().st_size<=44:self.status.setText('Recording was empty.');self.record.setEnabled(True);return
  prefs=self.owner.prefs;fmt=prefs['rec_format'];self.raw=self.path;self.target=self.path.with_name(self.path.stem+'-processed.'+fmt);filters=[]
  if prefs['rec_trim']:filters.append('silenceremove=start_periods=1:start_duration=0.08:start_threshold=-50dB,areverse,silenceremove=start_periods=1:start_duration=0.08:start_threshold=-50dB,areverse')
  if prefs['rec_normalize']:filters.append(f'loudnorm=I={max(-50,min(-5,prefs["rec_db"]-112))}:TP=-1.5:LRA=11')
  args=['-nostdin','-v','error','-i',str(self.raw)]
  if filters:args+=['-af',','.join(filters)]
  args+=['-ar',str(prefs['rec_rate'])]
  codecs={'wav':'pcm_s16le','mp3':'libmp3lame','m4a':'aac','ogg':'libvorbis','flac':'flac'};args+=['-c:a',codecs[fmt]]
  if fmt in ('mp3','m4a','ogg'):args+=['-b:a',str(prefs['rec_bitrate'])+'k']
  args+=[str(self.target)];self.encoder=QtCore.QProcess(self);self.encoder.finished.connect(self.encoded);self.encoder.start('ffmpeg',args);self.status.setText('Processing recording…')
 def encoded(self,code,status):
  self.record.setEnabled(True)
  if code!=0:
   self.status.setText('Processing failed; original preserved: '+str(self.raw));return
  self.owner.add_paths([str(self.target)]);e=next((e for e in self.owner.entries if e['path']==str(self.target)),None)
  if e:
   e['category']=self.target_category if self.target_category in self.owner.categories else 'My Sounds';position=self.owner.prefs['rec_position']
   if position!='end':
    self.owner.entries.remove(e);index=0 if position=='beginning' else next((i+1 for i,x in enumerate(self.owner.entries) if x['id']==self.after_id),len(self.owner.entries));self.owner.entries.insert(index,e)
   self.owner.changed();self.owner.select_id(e['id'])
  self.raw.unlink(missing_ok=True);self.status.setText('Saved: '+str(self.target))
 def closeEvent(self,event):
  if self.proc:self.stop()
  if self.encoder and self.encoder.state()!=QtCore.QProcess.ProcessState.NotRunning:
   self.encoder.waitForFinished(10000)
   if self.encoder.state()!=QtCore.QProcess.ProcessState.NotRunning:event.ignore();return
  event.accept()

BaseSpeech=Speech
class Speech(BaseSpeech):
 def __init__(self,owner):self.pending_mode=None;super().__init__(owner)
 def done(self,code,status):
  super().done(code,status)
  if code==0:
   e=next((e for e in self.owner.entries if e['path']==str(self.target)),None)
   if e:
    self.owner.last_tts=e['id']
    if self.pending_mode:self.owner.play_id(e['id'],self.pending_mode)
  self.pending_mode=None

# Waveform editor: immutable PCM revisions, asynchronous processing and atomic saves.
import wave
SHAPES.update(editor_cut='<circle cx="6" cy="18" r="3" fill="none" stroke="#0762a7" stroke-width="2"/><circle cx="18" cy="18" r="3" fill="none" stroke="#0762a7" stroke-width="2"/><path d="M5 2l13 13M19 2L6 15" stroke="#0762a7" stroke-width="2"/>',editor_trim='<path d="M5 2v17h17v-3H8V2zM2 5h17v17h-3V8H2z"/>',editor_undo='<path d="M10 3L2 10l8 7v-5c8-1 10 4 8 9 8-9 2-13-8-12z"/>',editor_redo='<path d="M14 3l8 7-8 7v-5c-8-1-10 4-8 9-8-9-2-13 8-12z"/>',editor_minus='<path d="M3 10h18v4H3z"/>',editor_selection='<path fill="none" stroke="#0762a7" stroke-width="2" d="M3 4h18v16H3zM7 16V9a5 5 0 0110 0v7M7 11v6M17 11v6"/>')
class Waveform(W.QWidget):
 selectionChanged=QtCore.pyqtSignal()
 seekRequested=QtCore.pyqtSignal(float)
 def __init__(self,editor):
  super().__init__();self.editor=editor;self.setMinimumHeight(165);self.setMouseTracking(True);self.anchor=0
 def seconds(self,x):
  e=self.editor;return min(e.length,max(0,e.view_start+x/max(1,self.width())*e.view_span()))
 def mousePressEvent(self,event):
  if event.button()==QtCore.Qt.MouseButton.LeftButton:
   self.anchor=self.seconds(event.position().x());self.editor.selection=(self.anchor,self.anchor);self.editor.cursor=self.anchor;self.selectionChanged.emit();self.update()
 def mouseMoveEvent(self,event):
  if event.buttons()&QtCore.Qt.MouseButton.LeftButton:
   self.editor.selection=tuple(sorted((self.anchor,self.seconds(event.position().x()))));self.selectionChanged.emit();self.update()
 def mouseReleaseEvent(self,event):
  if event.button()==QtCore.Qt.MouseButton.LeftButton:
   if abs(self.seconds(event.position().x())-self.anchor)<self.editor.view_span()/max(1,self.width())*3:self.seekRequested.emit(self.anchor)
   self.selectionChanged.emit()
 def paintEvent(self,event):
  e=self.editor;p=QtGui.QPainter(self);p.fillRect(self.rect(),QtGui.QColor('#191b1e' if e.owner.prefs['dark_mode'] else '#fafafa'));w=self.width();h=self.height();span=e.view_span();left=e.view_start
  p.fillRect(0,0,w,26,QtGui.QColor('#3b3d40' if e.owner.prefs['dark_mode'] else '#e6e6e6'))
  a,z=e.selection
  if z>a:p.fillRect(QtCore.QRectF((a-left)/span*w,26,(z-a)/span*w,h-26),QtGui.QColor(40,115,185,110))
  p.setPen(QtGui.QColor('#cccccc' if e.owner.prefs['dark_mode'] else '#555555'));step=span/8;power=10**math.floor(math.log10(max(.001,step)));step=next((n*power for n in [1,2,2.5,5,10] if n*power>=step),10*power)
  t=math.ceil(left/step)*step
  while t<=left+span:
   x=(t-left)/span*w;p.drawLine(int(x),19,int(x),26);p.drawText(QtCore.QRectF(x+3,0,80,19),str(round(t,2))+' s');t+=step
  center=26+(h-26)/2;p.setPen(QtGui.QColor('#bcbcbc' if e.owner.prefs['dark_mode'] else '#596b7e'))
  if e.peaks and e.length:
   count=len(e.peaks)
   for x in range(w):
    i=max(0,min(count-1,int((left+x/w*span)/e.length*count)));j=max(i+1,min(count,int((left+(x+1)/w*span)/e.length*count)));low=min(v[0] for v in e.peaks[i:j]);high=max(v[1] for v in e.peaks[i:j]);p.drawLine(x,int(center-high*(h-30)/2),x,int(center-low*(h-30)/2))
  p.setPen(QtGui.QColor('#279bea'));x=(e.cursor-left)/span*w;p.drawLine(int(x),26,int(x),h)
  if not e.peaks:p.drawText(self.rect(),QtCore.Qt.AlignmentFlag.AlignCenter,'Loading waveform…' if e.busy else 'No audio')

class Editor(W.QDialog):
 def __init__(self,owner,e):
  super().__init__(owner);self.owner=owner;self.entry=e;self.original=Path(e['path']);self.setWindowTitle('Sound Editor - '+str(self.original));self.resize(760,440);self.setMinimumSize(600,360)
  self.temp=tempfile.TemporaryDirectory(prefix='soundboard-editor-');self.folder=Path(self.temp.name);self.revisions=[];self.revision=-1;self.saved_path=None;self.peaks=[];self.length=0;self.selection=(0,0);self.cursor=0;self.zoom=1;self.view_start=0;self.busy=False;self.process=None;self.future=None;self.player=Player();self.play_end=0;self.clipboard=None;self.closed=False;self.pending_save=None
  layout=W.QVBoxLayout(self);layout.setContentsMargins(3,2,3,8);layout.setSpacing(3);bar=W.QMenuBar();layout.setMenuBar(bar);self.menus={n:bar.addMenu(n) for n in ['File','Edit','View','Play','Effects']};self.actions={}
  def action(key,title,fn,menu,shortcut=None,shape=None):
   a=QtGui.QAction(icon(shape) if shape else QtGui.QIcon(),title,self);a.triggered.connect(fn)
   if shortcut:a.setShortcut(shortcut)
   self.menus[menu].addAction(a);self.actions[key]=a;return a
  action('saveas','Save as…',self.save_as,'File','Ctrl+Shift+S');action('save','Save',self.save,'File','Ctrl+S');self.menus['File'].addSeparator();action('close','Close',self.close,'File')
  action('undo','Undo',lambda:self.history(-1),'Edit','Ctrl+Z','prev');action('redo','Redo',lambda:self.history(1),'Edit','Ctrl+Y','next');self.menus['Edit'].addSeparator();action('cut','Cut',self.cut,'Edit','Ctrl+X');action('copy','Copy',self.copy_audio,'Edit','Ctrl+C');action('paste','Paste',self.paste_audio,'Edit','Ctrl+V');action('delete','Delete selection',self.delete_selection,'Edit','Del');action('trim','Trim to selection',self.trim,'Edit','Ctrl+T');action('select','Select all',self.select_all,'Edit','Ctrl+A')
  action('zoom_in','Zoom in',lambda:self.set_zoom(self.zoom*2),'View','+','add');action('zoom_out','Zoom out',lambda:self.set_zoom(self.zoom/2),'View','-');action('fit','Fit entire sound',lambda:self.set_zoom(1),'View','Ctrl+0')
  action('play','Play / pause',self.play,'Play','Space','headphones');action('selection','Play selection',lambda:self.play(True),'Play',None,'play');action('stop','Stop',self.stop,'Play','Escape','stop')
  action('normalize','Adjust volume to target',self.normalize,'Effects');action('gain','Change gain…',self.gain,'Effects');action('fadein','Fade in selection',lambda:self.fade(True),'Effects');action('fadeout','Fade out selection',lambda:self.fade(False),'Effects');action('silence','Silence selection',self.silence,'Effects')
  for key,shape in [('cut','editor_cut'),('trim','editor_trim'),('undo','editor_undo'),('redo','editor_redo'),('selection','editor_selection'),('zoom_out','editor_minus')]:self.actions[key].setIcon(icon(shape))
  toolbar=W.QToolBar();toolbar.setIconSize(QtCore.QSize(20,20));layout.addWidget(toolbar)
  for key in ['play','selection','stop','cut','trim','undo','redo','zoom_in','zoom_out']:
   if key in ['cut','undo','zoom_in']:toolbar.addSeparator()
   toolbar.addAction(self.actions[key])
  row=W.QHBoxLayout();row.addStretch();adjust=W.QPushButton('Adjust volume to');adjust.clicked.connect(self.normalize);row.addWidget(adjust);self.target_db=W.QDoubleSpinBox();self.target_db.setRange(40,112);self.target_db.setValue(95);self.target_db.setSuffix(' dB');self.target_db.setToolTip('Relative RMS reference: 112 dB = 0 dBFS. Not a physical loudness measurement.');row.addWidget(self.target_db);layout.addLayout(row);self.adjust=adjust
  self.wave=Waveform(self);self.wave.selectionChanged.connect(self.refresh_actions);self.wave.seekRequested.connect(self.seek_preview);layout.addWidget(self.wave,1);self.scroll=W.QScrollBar(QtCore.Qt.Orientation.Horizontal);self.scroll.valueChanged.connect(self.scrolled);layout.addWidget(self.scroll)
  self.info=W.QLabel();self.info.setMinimumHeight(52);layout.addWidget(self.info);self.status=W.QLabel('Loading…');layout.addWidget(self.status)
  row=W.QHBoxLayout();row.addStretch();self.saveas_button=W.QPushButton('Save as…');self.saveas_button.clicked.connect(self.save_as);row.addWidget(self.saveas_button);self.save_button=W.QPushButton('Save');self.save_button.clicked.connect(self.save);row.addWidget(self.save_button);cancel=W.QPushButton('Cancel');cancel.clicked.connect(self.close);row.addWidget(cancel);layout.addLayout(row)
  self.timer=QtCore.QTimer(self);self.timer.timeout.connect(self.tick);self.timer.start(20);self.run(['-i',str(self.original),'-vn','-c:a','pcm_s16le'],self.loaded)
 def current(self):return self.revisions[self.revision] if self.revision>=0 else None
 def run(self,args,callback):
  if self.busy:return
  self.stop();self.busy=True;self.status.setText('Processing…');self.refresh_actions();target=self.folder/(uuid.uuid4().hex+'.wav');self.process=QtCore.QProcess(self)
  def done(code,status):
   self.busy=False
   if code:self.status.setText(bytes(self.process.readAllStandardError()).decode(errors='replace')[-800:] or 'Could not process audio.')
   else:callback(target)
   self.refresh_actions()
  self.process.finished.connect(done);self.process.errorOccurred.connect(lambda _:self.status.setText('Could not start ffmpeg.'));self.process.start('ffmpeg',['-nostdin','-v','error',*args,str(target)])
 def loaded(self,path):
  self.revisions=self.revisions[:self.revision+1]+[path];self.revision+=1;self.saved_path=self.saved_path or path;self.load_wave()
 @staticmethod
 def analyze(path):
  peaks=[];energy=0;count=0
  with wave.open(str(path),'rb') as f:
   rate=f.getframerate();channels=f.getnchannels();frames=f.getnframes()
   while data:=f.readframes(512):
    a=array.array('h',data);peaks.append((min(a)/32768,max(a)/32768));energy+=sum(x*x for x in a);count+=len(a)
  rms=math.sqrt(energy/max(1,count))/32768
  return peaks,frames/rate,rate,channels,112+20*math.log10(max(1e-6,rms))
 def load_wave(self):
  self.busy=True;self.selection=(0,0);self.cursor=0;self.view_start=0;self.future=self.owner.pool.submit(self.analyze,self.current());self.refresh_actions()
 def tick(self):
  if self.future and self.future.done():
   try:self.peaks,self.length,self.rate,self.channels,self.db=self.future.result();self.info.setText(f'Size: {self.original.stat().st_size//1024} KB       Duration: {duration(self.length)} + {int(self.length%1*1000)} ms\nFormat: {self.original.suffix[1:].upper()}       Channels: {"stereo" if self.channels==2 else str(self.channels)}\nSample rate: {self.rate} Hz       Volume: {self.db:.1f} dB');self.status.setText('Ready — drag on the waveform to select audio.');self.set_zoom(self.zoom)
   except Exception as ex:self.status.setText(str(ex))
   self.future=None;self.busy=False;self.refresh_actions();self.wave.update()
  if self.player.active() and not self.player.paused:
   self.cursor=min(self.play_end,self.play_offset+time.monotonic()-self.play_started)
   if self.cursor>=self.play_end:self.stop()
   self.wave.update()
 def view_span(self):return max(.001,self.length/self.zoom)
 def set_zoom(self,z):
  self.zoom=max(1,min(128,z));self.scroll.setRange(0,round(max(0,self.length-self.view_span())*1000));self.scroll.setPageStep(max(1,round(self.view_span()*1000)));self.scroll.setValue(round(min(self.view_start,max(0,self.length-self.view_span()))*1000));self.wave.update()
 def scrolled(self,n):self.view_start=n/1000;self.wave.update()
 def refresh_actions(self):
  ready=not self.busy and self.current() is not None;selected=self.selection[1]-self.selection[0]>.001
  for k,a in self.actions.items():a.setEnabled(ready or k=='close')
  for k in ['cut','copy','delete','trim','fadein','fadeout','silence']:self.actions[k].setEnabled(ready and selected)
  self.actions['paste'].setEnabled(ready and self.clipboard is not None);self.actions['undo'].setEnabled(ready and self.revision>0);self.actions['redo'].setEnabled(ready and self.revision<len(self.revisions)-1);self.actions['save'].setEnabled(ready and self.current()!=self.saved_path);self.save_button.setEnabled(self.actions['save'].isEnabled());self.saveas_button.setEnabled(ready);self.adjust.setEnabled(ready)
 def select_all(self):self.selection=(0,self.length);self.refresh_actions();self.wave.update()
 def history(self,step):
  if not self.busy and 0<=self.revision+step<len(self.revisions):self.stop();self.revision+=step;self.load_wave()
 def stop(self):self.player.stop()
 def play(self,selection=False):
  if self.busy or not self.current():return
  if self.player.active() and not selection:
   if self.player.paused:self.play_started+=time.monotonic()-self.paused_at;self.player.pause(False)
   else:self.paused_at=time.monotonic();self.player.pause(True)
   return
  self.stop();a,z=self.selection if selection and self.selection[1]>self.selection[0] else (self.cursor,self.length)
  if a>=z:a=0
  self.play_offset=a;self.play_end=z;self.play_started=time.monotonic();self.player.play(str(self.current()),self.owner.output,0 if self.owner.prefs['monitor_muted'] else self.owner.monitor_gain(),a)
 def seek_preview(self,seconds):
  playing=self.player.active();self.stop();self.cursor=seconds
  if playing:self.play()
 def copy_audio(self):
  if self.selection[1]>self.selection[0]:self.clipboard=(self.current(),*self.selection);self.refresh_actions()
 def cut(self):self.copy_audio();self.delete_selection()
 def filtered(self,filters):self.run(['-i',str(self.current()),'-af',filters,'-c:a','pcm_s16le'],self.loaded)
 def trim(self):
  a,z=self.selection
  if z>a:self.filtered(f'atrim=start={a}:end={z},asetpts=PTS-STARTPTS')
 def delete_selection(self):
  a,z=self.selection
  if z-a<.001:return
  if a<=.001 and z>=self.length-.001:self.status.setText('At least a small part of the sound must remain.');return
  if a<=.001:self.filtered(f'atrim=start={z},asetpts=PTS-STARTPTS')
  elif z>=self.length-.001:self.filtered(f'atrim=end={a},asetpts=PTS-STARTPTS')
  else:self.run(['-i',str(self.current()),'-filter_complex',f'[0:a]asplit=2[x][y];[x]atrim=end={a},asetpts=PTS-STARTPTS[l];[y]atrim=start={z},asetpts=PTS-STARTPTS[r];[l][r]concat=n=2:v=0:a=1[out]','-map','[out]','-c:a','pcm_s16le'],self.loaded)
 def paste_audio(self):
  if not self.clipboard:return
  path,a,z=self.clipboard;at=min(self.length,max(0,self.cursor));parts=[];graph=[]
  if at>.001:graph.append(f'[0:a]atrim=end={at},asetpts=PTS-STARTPTS[l]');parts.append('[l]')
  graph.append(f'[1:a]atrim=start={a}:end={z},asetpts=PTS-STARTPTS[c]');parts.append('[c]')
  if at<self.length-.001:graph.append(f'[0:a]atrim=start={at},asetpts=PTS-STARTPTS[r]');parts.append('[r]')
  graph.append(''.join(parts)+f'concat=n={len(parts)}:v=0:a=1[out]');self.run(['-i',str(self.current()),'-i',str(path),'-filter_complex',';'.join(graph),'-map','[out]','-c:a','pcm_s16le'],self.loaded)
 def normalize(self):
  if self.current() and not self.busy:self.filtered(f'volume={self.target_db.value()-self.db:.5f}dB,alimiter=limit=0.98:level=false')
 def gain(self):
  n,ok=W.QInputDialog.getDouble(self,'Change gain','Gain (dB):',0,-60,30,1)
  if ok:self.filtered(f'volume={n}dB')
 def fade(self,fadein):
  a,z=self.selection
  if z>a:self.filtered(f'afade=t={"in" if fadein else "out"}:st={a}:d={z-a}:enable=\'between(t,{a},{z})\'')
 def silence(self):
  a,z=self.selection
  if z>a:self.filtered(f"volume=0:enable='between(t,{a},{z})'")
 def save_as(self):
  path=W.QFileDialog.getSaveFileName(self,'Save sound as',str(self.original.with_name(self.original.stem+'-edited.wav')),'Audio (*.wav *.mp3 *.flac *.ogg *.m4a *.mp4)')[0]
  if path:self.export(Path(path))
 def save(self):self.export(self.original)
 def export(self,target):
  if self.busy or not self.current():return
  codecs={'.wav':'pcm_s16le','.mp3':'libmp3lame','.flac':'flac','.ogg':'libvorbis','.m4a':'aac','.mp4':'aac'}
  if target.suffix.lower() not in codecs:self.status.setText('Use Save as with WAV, MP3, FLAC, OGG, M4A or MP4.');return
  self.stop();self.busy=True;self.refresh_actions();self.pending_save=target.parent/('.'+target.stem+'-'+uuid.uuid4().hex+target.suffix);self.process=QtCore.QProcess(self);self.process.finished.connect(lambda code,status:self.exported(code,target));args=['-nostdin','-v','error','-i',str(self.current())]
  if target.resolve()==self.original.resolve() and target.suffix.lower()=='.mp4':args+=['-i',str(self.original),'-map','0:a:0','-map','1:v?','-c:v','copy','-shortest']
  self.process.start('ffmpeg',args+['-c:a',codecs[target.suffix.lower()],str(self.pending_save)]);self.status.setText('Saving…')
 def exported(self,code,target):
  self.busy=False
  try:
   if code:raise RuntimeError(bytes(self.process.readAllStandardError()).decode(errors='replace'))
   if target.exists():
    backup=DATA/'backups'/'audio';backup.mkdir(parents=True,exist_ok=True);shutil.copy2(target,backup/(target.stem+'-'+uuid.uuid4().hex+target.suffix))
   os.replace(self.pending_save,target)
   if target.resolve()==self.original.resolve():self.entry['duration']=self.length;self.owner.changed();self.saved_path=self.current()
   else:self.owner.add_paths([str(target)])
   self.status.setText('Saved: '+str(target))
  except Exception as ex:self.status.setText('Save failed: '+str(ex))
  finally:
   if self.pending_save:self.pending_save.unlink(missing_ok=True)
   self.pending_save=None;self.refresh_actions()
 def closeEvent(self,event):
  self.timer.stop();self.stop()
  if self.process and self.process.state()!=QtCore.QProcess.ProcessState.NotRunning:self.process.kill();self.process.waitForFinished(2000)
  if self.future:
   try:self.future.result(timeout=10)
   except Exception:pass
  if self.pending_save:self.pending_save.unlink(missing_ok=True)
  self.temp.cleanup();event.accept()

def main():
 app=W.QApplication(sys.argv);app.setStyle('Fusion');app.setApplicationName('soundpad like');app.setDesktopFileName('soundpad-like');app.setWindowIcon(app_icon());app.setStyleSheet(STYLE)
 palette=QtGui.QPalette();palette.setColor(QtGui.QPalette.ColorRole.Window,QtGui.QColor('#f4f4f4'));palette.setColor(QtGui.QPalette.ColorRole.Base,QtGui.QColor('white'));palette.setColor(QtGui.QPalette.ColorRole.Text,QtGui.QColor('black'));palette.setColor(QtGui.QPalette.ColorRole.WindowText,QtGui.QColor('black'));palette.setColor(QtGui.QPalette.ColorRole.ButtonText,QtGui.QColor('black'));palette.setColor(QtGui.QPalette.ColorRole.Highlight,QtGui.QColor('#3298ff'));palette.setColor(QtGui.QPalette.ColorRole.HighlightedText,QtGui.QColor('white'));app.setPalette(palette)
 lock=QtCore.QLockFile(str(DATA/'app.lock'));lock.setStaleLockTime(0)
 if not lock.tryLock(100):W.QMessageBox.information(None,'soundpad like','البرنامج مفتوح بالفعل.');return
 try:Bridge().cleanup_stale();window=Window()
 except Exception as ex:W.QMessageBox.critical(None,'Soundboard',str(ex));raise
 window.show();signal.signal(signal.SIGTERM,lambda *_:window.quit_app());signal.signal(signal.SIGINT,lambda *_:window.quit_app());app.aboutToQuit.connect(window.stop);app.aboutToQuit.connect(window.bridge.stop);sys.exit(app.exec())

if __name__=='__main__':main()
