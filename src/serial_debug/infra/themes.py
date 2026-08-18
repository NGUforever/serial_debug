"""主题配色：VSCode Dark+ (深色) + Breakers Light (浅色)。

商业化 UI 风格：扁平按钮 + 三态 + 状态点 + accent primary。
配色取自 VSCode Dark+ 与 Sublime Breakers，对应关系：
  ts      ← comment        时间戳/注释灰
  send    ← keyword/blue   发送命令
  resp    ← string/green   OK 响应
  note    ← warning/yellow 警告/超时
  log     ← foreground      周期日志
  accent  ← accent blue     primary 按钮强调
"""
from __future__ import annotations

import os
import subprocess
import sys


def _config_dir() -> str:
    """返回平台适配的配置目录（与 config.py 保持一致）。"""
    home = os.path.expanduser('~')
    if sys.platform == 'win32':
        base = os.environ.get('LOCALAPPDATA',
                              os.path.join(home, 'AppData', 'Local'))
        return os.path.join(base, 'serial_debug')
    if sys.platform == 'darwin':
        return os.path.join(home, 'Library', 'Application Support',
                            'serial_debug')
    xdg = os.environ.get('XDG_CONFIG_HOME',
                         os.path.join(home, '.config'))
    return os.path.join(xdg, 'serial_debug')


CONFIG_DIR = _config_dir()
THEME_FILE = os.path.join(CONFIG_DIR, 'theme.txt')   # auto / light / dark

# 字体族按平台选择（Linux DejaVu / Windows Segoe UI / macOS Helvetica Neue）
if sys.platform == 'win32':
    FONT_FAMILY = 'Segoe UI'
    MONO_FAMILY = 'Consolas'
elif sys.platform == 'darwin':
    FONT_FAMILY = 'Helvetica Neue'
    MONO_FAMILY = 'Menlo'
else:
    FONT_FAMILY = 'DejaVu Sans'
    MONO_FAMILY = 'DejaVu Sans Mono'
DEFAULT_FONT_SIZE = 10

# 全局字体大小（由 app 设置后覆盖到所有 style）
_font_size = DEFAULT_FONT_SIZE


def get_font_size() -> int:
    return _font_size


def set_font_size(size: int):
    global _font_size
    _font_size = max(8, min(20, int(size)))


def _font(weight='normal'):
    return (FONT_FAMILY, _font_size, weight)


def _mono():
    return (MONO_FAMILY, _font_size)

THEMES = {
    'dark': {
        # 基础背景
        'bg': '#1e1e1e',                # 应用背景
        'panel_bg': '#252526',          # 面板/输入框背景
        'panel_fg': '#cccccc',
        'fg': '#cccccc',
        'cursor': '#aeafad',
        'select_bg': '#264f78',         # 选中
        # 按钮三态
        'btn_bg': '#3a3d41',
        'btn_hover': '#4c5052',
        'btn_active': '#2d2d30',
        'btn_primary': '#0e639c',       # accent primary
        'btn_primary_hover': '#1177bb',
        # 状态点
        'status_ok': '#4ec9b0',
        'status_wait': '#dcdcaa',
        'status_err': '#f48771',
        'status_idle': '#6e7681',
        # 日志 tag
        'ts': '#6a9955',
        'send': '#569cd6',
        'resp': '#ce9178',
        'note': '#dcdcaa',
        'log': '#d4d4d4',
        'muted': '#858585',
        'accent': '#569cd6',
        'border': '#3c3c3c',
    },
    'light': {
        'bg': '#f5f5f5',
        'panel_bg': '#ffffff',
        'panel_fg': '#333333',
        'fg': '#333333',
        'cursor': '#333333',
        'select_bg': '#cce5ff',
        'btn_bg': '#e1e1e1',
        'btn_hover': '#d0d0d0',
        'btn_active': '#c0c0c0',
        'btn_primary': '#0e639c',
        'btn_primary_hover': '#1177bb',
        'status_ok': '#2e8b57',
        'status_wait': '#b8860b',
        'status_err': '#c0392b',
        'status_idle': '#999999',
        'ts': '#6a9955',
        'send': '#0e639c',
        'resp': '#267f99',
        'note': '#b5a642',
        'log': '#333333',
        'muted': '#888888',
        'accent': '#0e639c',
        'border': '#dcdcdc',
    },
}


def detect_system_theme() -> str:
    """检测系统深/浅色主题。
    Linux: gsettings（GNOME）；macOS: defaults；Windows: 注册表。
    检测失败默认返回 'dark'。
    """
    if sys.platform == 'darwin':
        try:
            r = subprocess.run(
                ['defaults', 'read', '-g', 'AppleInterfaceStyle'],
                capture_output=True, text=True, timeout=2)
            if 'Dark' in r.stdout:
                return 'dark'
            return 'light'
        except Exception:
            return 'dark'
    if sys.platform == 'win32':
        try:
            r = subprocess.run(
                ['reg', 'query',
                 r'HKCU\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize',
                 '/v', 'AppsUseLightTheme'],
                capture_output=True, text=True, timeout=2)
            if '0x0' in r.stdout:
                return 'dark'
            return 'light'
        except Exception:
            return 'dark'
    # Linux: gsettings（GNOME）；非 GNOME 环境会失败，降级 dark
    try:
        r = subprocess.run(
            ['gsettings', 'get', 'org.gnome.desktop.interface', 'color-scheme'],
            capture_output=True, text=True, timeout=2)
        out = r.stdout.lower()
        if 'dark' in out:
            return 'dark'
        if 'light' in out:
            return 'light'
    except Exception:
        pass
    return 'dark'


def load_pref() -> str:
    """读持久化偏好：auto / light / dark。委托给 config 模块。"""
    try:
        import config
        return config.load().get('theme_pref', 'auto')
    except Exception:
        return 'auto'


def save_pref(pref: str):
    """保存偏好：合并到 config 文件。"""
    try:
        import config
        cfg = config.load()
        cfg['theme_pref'] = pref
        config.save(cfg)
    except Exception:
        pass


def resolve(pref: str) -> str:
    """pref(auto/light/dark) → 具体主题名(dark/light)。"""
    if pref == 'light':
        return 'light'
    if pref == 'dark':
        return 'dark'
    return detect_system_theme()

