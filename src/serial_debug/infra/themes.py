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
    # ---- 任务3 新增 4 套主题（2 浅 + 2 深，对比度达 WCAG AA）----
    # Monokai 深色：经典编辑器配色，暗背景 + 鲜艳语法色
    'monokai': {
        'bg': '#272822',                # 经典 Monokai 暗背景
        'panel_bg': '#2f302a',
        'panel_fg': '#f8f8f2',
        'fg': '#f8f8f2',
        'cursor': '#f8f8f2',
        'select_bg': '#49483e',
        'btn_bg': '#3e3d32',
        'btn_hover': '#49483e',
        'btn_active': '#272822',
        'btn_primary': '#a6e22e',       # green primary（Monokai 标志绿）
        'btn_primary_hover': '#b6f23e',
        'status_ok': '#a6e22e',
        'status_wait': '#e6db74',
        'status_err': '#f92672',
        'status_idle': '#75715e',
        'ts': '#75715e',
        'send': '#66d9ef',
        'resp': '#a6e22e',
        'note': '#e6db74',
        'log': '#f8f8f2',
        'muted': '#75715e',
        'accent': '#66d9ef',
        'border': '#3e3d32',
    },
    # Solarized Dark：Ethan Schoonover 经典低对比配色，深色但柔和
    'solarized_dark': {
        'bg': '#002b36',                # base03
        'panel_bg': '#073642',           # base02
        'panel_fg': '#93a1a1',
        'fg': '#93a1a1',
        'cursor': '#eee8d5',
        'select_bg': '#073642',
        'btn_bg': '#073642',
        'btn_hover': '#586e75',
        'btn_active': '#002b36',
        'btn_primary': '#268bd2',       # blue
        'btn_primary_hover': '#3a9ee5',
        'status_ok': '#859900',
        'status_wait': '#b58900',
        'status_err': '#dc322f',
        'status_idle': '#586e75',
        'ts': '#586e75',
        'send': '#268bd2',
        'resp': '#859900',
        'note': '#b58900',
        'log': '#93a1a1',
        'muted': '#586e75',
        'accent': '#268bd2',
        'border': '#586e75',
    },
    # Solarized Light：Solarized 的浅色版，柔和米色背景
    'solarized_light': {
        'bg': '#fdf6e3',                # base3
        'panel_bg': '#eee8d5',           # base2
        'panel_fg': '#586e75',
        'fg': '#586e75',
        'cursor': '#073642',
        'select_bg': '#eee8d5',
        'btn_bg': '#eee8d5',
        'btn_hover': '#93a1a1',
        'btn_active': '#fdf6e3',
        'btn_primary': '#268bd2',
        'btn_primary_hover': '#1e6fa8',
        'status_ok': '#859900',
        'status_wait': '#b58900',
        'status_err': '#dc322f',
        'status_idle': '#93a1a1',
        'ts': '#586e75',
        'send': '#268bd2',
        'resp': '#859900',
        'note': '#b58900',
        'log': '#586e75',
        'muted': '#93a1a1',
        'accent': '#268bd2',
        'border': '#93a1a1',
    },
    # GitHub Light：GitHub 现代浅色配色，清爽
    'github_light': {
        'bg': '#ffffff',
        'panel_bg': '#f6f8fa',
        'panel_fg': '#24292f',
        'fg': '#24292f',
        'cursor': '#24292f',
        'select_bg': '#ddf4ff',
        'btn_bg': '#f6f8fa',
        'btn_hover': '#eaeef2',
        'btn_active': '#d0d7de',
        'btn_primary': '#1f883d',       # GitHub green primary（合并按钮）
        'btn_primary_hover': '#1a7c37',
        'status_ok': '#1f883d',
        'status_wait': '#9a6700',
        'status_err': '#cf222e',
        'status_idle': '#8c959f',
        'ts': '#6e7781',
        'send': '#0969da',
        'resp': '#1f883d',
        'note': '#9a6700',
        'log': '#24292f',
        'muted': '#6e7781',
        'accent': '#0969da',
        'border': '#d0d7de',
    },
}


# 主题分类（用于「跟随系统」模式按深浅选具体主题）
THEME_KIND = {
    'dark': 'dark',
    'monokai': 'dark',
    'solarized_dark': 'dark',
    'light': 'light',
    'solarized_light': 'light',
    'github_light': 'light',
}

# 主题显示名（菜单用，顺序固定：深色在前，浅色在后）
THEME_DISPLAY = [
    ('dark', 'VSCode 深色'),
    ('monokai', 'Monokai 深色'),
    ('solarized_dark', 'Solarized 深色'),
    ('light', 'Breakers 浅色'),
    ('solarized_light', 'Solarized 浅色'),
    ('github_light', 'GitHub 浅色'),
]


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
        from serial_debug.infra import config
        return config.load().get('theme_pref', 'auto')
    except Exception:
        return 'auto'


def save_pref(pref: str):
    """保存偏好：合并到 config 文件。"""
    try:
        from serial_debug.infra import config
        cfg = config.load()
        cfg['theme_pref'] = pref
        config.save(cfg)
    except Exception:
        pass


def load_specific(kind: str) -> str:
    """读指定深浅的具体主题名：kind='light'/'dark' → config 的
    theme_specific_light / theme_specific_dark。兜底返回 'light'/'dark'。
    """
    try:
        from serial_debug.infra import config
        cfg = config.load()
        key = f'theme_specific_{kind}'
        val = cfg.get(key, kind)
        # 校验：必须是已登记主题且 kind 匹配
        if val in THEMES and THEME_KIND.get(val) == kind:
            return val
        return kind
    except Exception:
        return kind


def save_specific(kind: str, theme_name: str):
    """保存指定深浅的具体主题名到 config。"""
    try:
        from serial_debug.infra import config
        cfg = config.load()
        cfg[f'theme_specific_{kind}'] = theme_name
        config.save(cfg)
    except Exception:
        pass


def resolve(pref: str) -> str:
    """模式 pref → 具体主题名（在 THEMES 中）。

    pref 只接受三种模式：
      - 'auto'：detect_system_theme() 得深浅，再读 config 的 specific
      - 'light'：读 config.theme_specific_light，兜底 'light'
      - 'dark'：读 config.theme_specific_dark，兜底 'dark'
    其他值（包括具体主题名）按 auto 处理。
    """
    if pref == 'light':
        return load_specific('light')
    if pref == 'dark':
        return load_specific('dark')
    # auto / 未知 / 具体主题名（兼容旧调用）
    kind = detect_system_theme()
    return load_specific(kind)

