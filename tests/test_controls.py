import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
import importlib.util, tempfile, unittest
from pathlib import Path
sandbox = tempfile.TemporaryDirectory()
os.environ['XDG_DATA_HOME'] = sandbox.name
spec = importlib.util.spec_from_file_location('soundpad_like', Path(__file__).resolve().parents[1] / 'src/soundpad_like.py')
appmod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(appmod)
from PyQt6 import QtCore, QtWidgets
from PyQt6.QtTest import QTest
app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

class Controls(unittest.TestCase):
    def test_fractional_pointer_position(self):
        slider = appmod.SeekSlider(QtCore.Qt.Orientation.Horizontal)
        slider.setRange(0, 1000000)
        slider.resize(600, 30)
        self.assertNotEqual(slider.value_at(100.1), slider.value_at(100.4))
        self.assertEqual(slider.value_at(-100), 0)
        self.assertEqual(slider.value_at(10000), 1000000)

    def test_native_keypad_without_qt_modifier(self):
        from PyQt6 import QtGui
        edit = appmod.HotkeyEdit()
        for scan, expected in [(83, 'Num+4'), (13, '4')]:
            event = QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_4,
                                   QtCore.Qt.KeyboardModifier.NoModifier, scan, ord('4'), 0, '4')
            edit.capture(event)
            self.assertEqual(edit.keySequence().toString(), expected)
        edit.close()

    def test_numpad_without_numlock(self):
        edit = appmod.HotkeyEdit()
        for navigation, digit in appmod.NUMPAD_KEYS.items():
            QTest.keyClick(edit, navigation, QtCore.Qt.KeyboardModifier.KeypadModifier)
            key = edit.keySequence()[0]
            self.assertEqual(int(key.key()), ord(digit))
            self.assertTrue(key.keyboardModifiers() & QtCore.Qt.KeyboardModifier.KeypadModifier)
        edit.close()

if __name__ == '__main__':
    unittest.main()
