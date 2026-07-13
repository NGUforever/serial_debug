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
import tkinter as tk
from tkinter import ttk

CONFIG_DIR = os.path.expanduser('~/.config/serial_debug')
THEME_FILE = os.path.join(CONFIG_DIR, 'theme.txt')   # auto / light / dark
FONT_FAMILY = 'DejaVu Sans'        # 控件字体
MONO_FAMILY = 'DejaVu Sans Mono'   # 等宽字体（日志）
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
    """Linux GNOME：读 gsettings color-scheme，dark → dark，否则 light。"""
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


def apply_theme(root: tk.Tk, theme_name: str):
    """把指定主题应用到 root 及其 ttk/tk 子控件。

    调用方在创建完所有控件后调用一次；切换主题时再调用。
    Text 控件的 tag_config 需要调用方自行重新配置（用 THEMES[name]）。
    """
    t = THEMES[theme_name]
    style = ttk.Style(root)
    try:
        style.theme_use('clam')
    except Exception:
        pass

    # ttk 控件统一配色
    style.configure('TFrame', background=t['bg'], foreground=t['fg'])
    style.configure('TLabel', background=t['bg'], foreground=t['fg'])
    style.configure('TButton', background=t['btn_bg'], foreground=t['fg'],
                    borderwidth=0, relief='flat', padding=(12, 6),
                    font=_font())
    style.map('TButton',
              background=[('active', t['btn_hover']),
                          ('pressed', t['btn_active'])])
    # Primary 按钮（accent 色）
    style.configure('Primary.TButton', background=t['btn_primary'],
                    foreground='#ffffff', borderwidth=0, relief='flat',
                    padding=(14, 6), font=_font('bold'))
    style.map('Primary.TButton',
              background=[('active', t['btn_primary_hover']),
                          ('pressed', t['btn_primary'])])
    # 危险按钮（红色）
    style.configure('Danger.TButton', background=t['status_err'],
                    foreground='#ffffff', borderwidth=0, relief='flat',
                    padding=(12, 6), font=_font())
    style.map('Danger.TButton',
              background=[('active', t['status_err'])])

    style.configure('TEntry', fieldbackground=t['panel_bg'],
                    foreground=t['panel_fg'], insertcolor=t['cursor'],
                    bordercolor=t['border'], lightcolor=t['border'],
                    darkcolor=t['border'], padding=6,
                    font=_font())
    style.map('TEntry',
              bordercolor=[('focus', t['accent'])],
              lightcolor=[('focus', t['accent'])],
              darkcolor=[('focus', t['accent'])])
    style.configure('TCombobox', fieldbackground=t['panel_bg'],
                    foreground=t['panel_fg'], background=t['btn_bg'],
                    bordercolor=t['border'], arrowcolor=t['fg'],
                    padding=6, font=_font())
    style.map('TCombobox',
              fieldbackground=[('readonly', t['panel_bg'])],
              foreground=[('readonly', t['panel_fg'])],
              background=[('active', t['btn_hover'])])
    style.configure('TScrollbar', background=t['btn_bg'],
                    troughcolor=t['bg'], arrowcolor=t['fg'],
                    bordercolor=t['bg'], gripcount=0)
    style.configure('Vertical.TScrollbar', background=t['btn_bg'],
                    troughcolor=t['bg'], arrowcolor=t['fg'],
                    bordercolor=t['bg'], gripcount=0, width=12)
    style.configure('TSeparator', background=t['border'])
    style.configure('TLabel', background=t['bg'], foreground=t['fg'],
                    font=_font())
    style.configure('TCheckbutton', background=t['bg'], foreground=t['fg'],
                    indicatorcolor=t['panel_bg'], bordercolor=t['border'],
                    font=_font(), focuscolor='', padding=6,
                    indicatorsize=18)
    style.map('TCheckbutton',
              background=[('active', t['bg'])],
              foreground=[('active', t['fg'])],
              indicatorcolor=[('selected', t['accent']),
                              ('!selected', t['panel_bg'])])

    # Treeview（AT 命令表格）
    style.configure('Treeview',
                    background=t['panel_bg'], foreground=t['panel_fg'],
                    fieldbackground=t['panel_bg'], borderwidth=0,
                    rowheight=26, font=_mono())
    style.map('Treeview',
              background=[('selected', t['select_bg'])],
              foreground=[('selected', '#ffffff')])
    style.configure('Treeview.Heading',
                    background=t['btn_bg'], foreground=t['fg'],
                    borderwidth=0, relief='flat', font=_font('bold'))
    style.map('Treeview.Heading',
              background=[('active', t['btn_hover'])])

    # tk.Menu（不通过 ttk Style 控制，需要手动 configure）
    # VSCode 风格：菜单栏比主背景略亮，hover 蓝色高亮
    # tkinter Menu 不支持 padding/margin/圆角，只能靠配色+字体模拟专业感
    t['_menu_colors'] = {
        'bg': t['bg'],
        'fg': t['fg'],
        'active_bg': t['select_bg'],
        'active_fg': '#ffffff',
        'border': t['border'],
        'disabled': t['muted'],
    }

    # root 背景
    root.configure(bg=t['bg'])
    return t


def apply_menu_theme(menubar, theme: dict):
    """递归给 tk.Menu 及所有子 menu 配色（VSCode 风格）。
    主窗口 _apply_theme 和 _build_menubar 都调用。
    """
    if menubar is None:
        return
    c = theme.get('_menu_colors') or {
        'bg': theme['bg'], 'fg': theme['fg'],
        'active_bg': theme['select_bg'], 'active_fg': '#ffffff',
        'border': theme['border'], 'disabled': theme['muted'],
    }
    def _style_menu(menu):
        try:
            menu.configure(
                bg=c['bg'], fg=c['fg'],
                activebackground=c['active_bg'],
                activeforeground=c['active_fg'],
                activeborderwidth=0, borderwidth=0,
                disabledforeground=c['disabled'],
                relief='flat', tearoff=False,
                font=_font())
        except Exception:
            for k, v in [('bg', c['bg']), ('fg', c['fg']),
                         ('activebackground', c['active_bg']),
                         ('activeforeground', c['active_fg']),
                         ('activeborderwidth', 0), ('borderwidth', 0),
                         ('disabledforeground', c['disabled']),
                         ('relief', 'flat'), ('tearoff', False),
                         ('font', _font())]:
                try:
                    menu.configure(**{k: v})
                except Exception:
                    pass
        # 递归子菜单
        try:
            last = menu.index('end')
            if last is not None:
                for i in range(last + 1):
                    try:
                        sub = menu.nametowidget(menu.entrycget(i, 'menu'))
                    except Exception:
                        sub = None
                    if sub is not None:
                        _style_menu(sub)
        except Exception:
            pass
    _style_menu(menubar)
