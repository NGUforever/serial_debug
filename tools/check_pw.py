import sys, os, types
# 仓库根在 tools/ 上一层，src 在根下
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, 'src'))
themes = types.ModuleType('themes')
themes.THEMES = {}
themes.resolve = lambda p: p
themes.load_pref = lambda: 'auto'
sys.modules['serial_debug.infra.themes'] = themes
import tkinter as tk
import traceback
root = tk.Tk()
from serial_debug.ui import monitor_window as mw
for cls in [mw.BmsMonitorPanel, mw.MotorMonitorPanel, mw.ImuMonitorPanel]:
    try:
        p = cls(root)
        n = len(p.winfo_children())
        print(f'{cls.__name__}: OK, children={n}')
    except Exception:
        print(f'{cls.__name__}: FAILED')
        traceback.print_exc()
root.destroy()
