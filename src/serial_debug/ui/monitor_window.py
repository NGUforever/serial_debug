"""MonitorWindow / MonitorPanel：实时数据监视（曲线 + 数据模式）。

变量定义：name + regex（第一捕获组为数值）+ color + y_min/y_max。
从主窗口 _drain 调用 feed(line) 喂入日志行，自动匹配提取。

- MonitorPanel(ttk.Frame)：可嵌入主窗口 PanedWindow 的面板。
- MonitorWindow(tk.Toplevel)：独立窗口，内部包装 MonitorPanel。

两种呈现共享同一 Panel 逻辑，切换模式时变量列表迁移。

- 曲线模式：Canvas 实时绘制时间序列，X=时间，Y=变量值，10fps。
- 数据模式：网格文本框，每个显示一个变量的最新值，5fps。

内存控制：
  - 每变量 deque(maxlen=600)，约 60s 数据 @ 10fps
  - 关闭/forget 时停止定时器，清空 buffer
"""
from __future__ import annotations

import re
import time
import math
import datetime as _dt
import tkinter as tk
from tkinter import ttk, messagebox, colorchooser, filedialog
from tkinter import font as tkfont
from collections import deque

try:
    import themes
except Exception:
    themes = None


# 日志时间戳（毫秒，10~13 位数字）。例：1783996615491
# 不限定行首：用户的日志格式可能是 "[HH:MM:SS.mmm] 1783996615491 ..."，
# 时间戳跟在 [time] 前缀后面，所以用 search 在整行里找第一个 10-13 位数字。
_LOG_TS_RE = re.compile(r'(?<!\d)(\d{10,13})(?!\d)')


def _extract_log_ts(line: str) -> float | None:
    """从日志行中提取时间戳，返回秒（float）。提取失败返回 None。"""
    m = _LOG_TS_RE.search(line)
    if not m:
        return None
    try:
        ms = int(m.group(1))
        # 13 位 = 毫秒，10 位 = 秒
        if len(m.group(1)) <= 10:
            return float(ms)
        return ms / 1000.0
    except ValueError:
        return None



_PALETTE = [
    '#569cd6', '#ce9178', '#4ec9b0', '#dcdcaa',
    '#c586c0', '#f48771', '#6a9955', '#9cdcfe',
    '#d16969', '#b5cea8',
]


def _default_theme():
    if themes is not None:
        try:
            return themes.THEMES[themes.resolve(themes.load_pref())]
        except Exception:
            pass
    return {
        'bg': '#1e1e1e', 'panel_bg': '#252526', 'fg': '#cccccc',
        'muted': '#858585', 'border': '#3c3c3c', 'select_bg': '#264f78',
    }


def _font(weight='normal'):
    if themes is not None:
        try:
            return themes._font(weight)
        except Exception:
            pass
    return ('DejaVu Sans', 10, weight)


def _mono():
    if themes is not None:
        try:
            return themes._mono()
        except Exception:
            pass
    return ('DejaVu Sans Mono', 10)


class _VarDialog(tk.Toplevel):
    """添加/编辑监视变量的对话框。"""

    def __init__(self, parent, initial=None, title='添加监视变量',
                 used_names=(), theme=None):
        super().__init__(parent)
        self.title(title)
        self.transient(parent)
        self.resizable(False, False)
        self.result = None
        self._used_names = set(used_names)
        if initial:
            self._used_names.discard(initial.get('name'))
        # 深色主题：Toplevel 默认灰白背景，显式设为主题色
        if theme:
            self.configure(bg=theme['bg'])

        f = ttk.Frame(self, padding=12)
        f.pack(fill='both', expand=True)

        ttk.Label(f, text='变量名').grid(row=0, column=0, sticky='w', pady=4)
        self.name_var = tk.StringVar(
            value=initial.get('name', '') if initial else '')
        ttk.Entry(f, textvariable=self.name_var, width=30
                  ).grid(row=0, column=1, pady=4, padx=8, columnspan=2)

        ttk.Label(f, text='正则（第一捕获组为数值）').grid(
            row=1, column=0, sticky='w', pady=4)
        self.regex_var = tk.StringVar(
            value=initial.get('regex', '') if initial else '')
        ttk.Entry(f, textvariable=self.regex_var, width=50
                  ).grid(row=1, column=1, pady=4, padx=8, columnspan=2)

        ttk.Label(f, text='颜色').grid(row=2, column=0, sticky='w', pady=4)
        self.color_var = tk.StringVar(
            value=(initial.get('color', _PALETTE[0]) if initial
                   else _PALETTE[0]))
        ttk.Button(f, text='选择…', command=self._pick_color
                   ).grid(row=2, column=1, sticky='w', pady=4, padx=8)
        self.color_preview = tk.Canvas(f, width=24, height=16,
                                        bg=self.color_var.get(),
                                        highlightthickness=0)
        self.color_preview.grid(row=2, column=2)

        ttk.Label(f, text='Y 最小（空=自动）').grid(
            row=3, column=0, sticky='w', pady=4)
        self.ymin_var = tk.StringVar(
            value=('' if not initial or initial.get('y_min') is None
                   else str(initial['y_min'])))
        ttk.Entry(f, textvariable=self.ymin_var, width=12
                  ).grid(row=3, column=1, sticky='w', pady=4, padx=8)

        ttk.Label(f, text='Y 最大（空=自动）').grid(
            row=4, column=0, sticky='w', pady=4)
        self.ymax_var = tk.StringVar(
            value=('' if not initial or initial.get('y_max') is None
                   else str(initial['y_max'])))
        ttk.Entry(f, textvariable=self.ymax_var, width=12
                  ).grid(row=4, column=1, sticky='w', pady=4, padx=8)

        ttk.Label(f, text='示例：RPM=(\\d+) 匹配日志中 RPM=1234',
                  foreground=_default_theme()['muted']
                  ).grid(row=5, column=0, columnspan=3, sticky='w', pady=4)

        btns = ttk.Frame(f)
        btns.grid(row=6, column=0, columnspan=3, pady=(10, 0))
        ttk.Button(btns, text='确定', command=self._ok
                   ).pack(side='left', padx=4)
        ttk.Button(btns, text='取消', command=self._cancel
                   ).pack(side='left', padx=4)

        self.protocol('WM_DELETE_WINDOW', self._cancel)
        self.bind('<Return>', lambda _e: self._ok())
        self.bind('<Escape>', lambda _e: self._cancel())

    def _pick_color(self):
        c = colorchooser.askcolor(self.color_var.get())
        if c and c[1]:
            self.color_var.set(c[1])
            self.color_preview.configure(bg=c[1])

    def _ok(self):
        name = self.name_var.get().strip()
        if not name:
            messagebox.showerror('错误', '变量名不能为空', parent=self)
            return
        if name in self._used_names:
            messagebox.showerror('错误', f'变量名 {name} 已存在', parent=self)
            return
        try:
            re.compile(self.regex_var.get())
        except re.error as e:
            messagebox.showerror('正则错误', f'{e}', parent=self)
            return
        y_min = None
        if self.ymin_var.get().strip():
            try:
                y_min = float(self.ymin_var.get())
            except ValueError:
                messagebox.showerror('错误', 'Y 最小必须是数字', parent=self)
                return
        y_max = None
        if self.ymax_var.get().strip():
            try:
                y_max = float(self.ymax_var.get())
            except ValueError:
                messagebox.showerror('错误', 'Y 最大必须是数字', parent=self)
                return
        self.result = {
            'name': name,
            'regex': self.regex_var.get(),
            'color': self.color_var.get(),
            'y_min': y_min,
            'y_max': y_max,
        }
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()


class _BmsVarDialog(tk.Toplevel):
    """添加/编辑 BMS 变量的对话框（用户只填变量名）。

    用户只需选择/输入变量名 + 中文说明 + 颜色 + Y 范围。
    定位日志中数值用的 log_tag / group_idx / pos_in_group 从
    BmsMonitorPanel 内置的变量注册表自动查表填入，用户无需关心。

    若变量名不在注册表中，匹配不到日志数据，仅显示 '—'。
    """

    def __init__(self, parent, initial=None, title='添加变量',
                 used_names=(), registry=None, plot_names=None,
                 initial_plot=None, theme=None):
        super().__init__(parent)
        self.title(title)
        self.transient(parent)
        self.resizable(True, True)
        self.result = None
        self._used_names = set(used_names)
        if initial:
            self._used_names.discard(initial.get('name'))
        self._registry: dict = registry or {}
        self._plot_names: list[str] = list(plot_names or [])
        # 深色主题：Toplevel 默认灰白背景，显式设为主题色
        if theme:
            self.configure(bg=theme['bg'])

        f = ttk.Frame(self, padding=12)
        f.pack(fill='both', expand=True)

        # 分组选择（第一级菜单）：按 log_tag 分组
        groups = self._build_groups()
        group_names = list(groups.keys())
        default_group = ''
        if initial:
            initial_tag = initial.get('log_tag', '')
            for gn in group_names:
                if gn.endswith(f'({initial_tag})') or gn == initial_tag:
                    default_group = gn
                    break
        ttk.Label(f, text='分组').grid(row=0, column=0, sticky='w', pady=4)
        self._group_var = tk.StringVar(value=default_group)
        group_combo = ttk.Combobox(f, textvariable=self._group_var, width=28,
                                   values=group_names, state='readonly')
        group_combo.grid(row=0, column=1, pady=4, padx=8, columnspan=2)
        group_combo.bind('<<ComboboxSelected>>', self._on_group_changed)

        # 变量名选择（第二级菜单）：根据分组筛选
        ttk.Label(f, text='变量名').grid(row=1, column=0, sticky='w', pady=4)
        self.name_var = tk.StringVar(
            value=initial.get('name', '') if initial else '')
        self._name_combo = ttk.Combobox(f, textvariable=self.name_var, width=28,
                                  values=self._filtered_names(default_group),
                                  state='normal')
        self._name_combo.grid(row=1, column=1, pady=4, padx=8, columnspan=2)
        self._name_combo.bind('<<ComboboxSelected>>', self._on_name_picked)
        self._name_combo.bind('<FocusOut>', lambda _e: self._on_name_picked())

        ttk.Label(f, text='中文说明').grid(row=2, column=0, sticky='w', pady=4)
        self.label_var = tk.StringVar(
            value=initial.get('label', '') if initial else '')
        ttk.Entry(f, textvariable=self.label_var, width=30
                  ).grid(row=2, column=1, pady=4, padx=8, columnspan=2)

        ttk.Label(f, text='颜色').grid(row=3, column=0, sticky='w', pady=4)
        self.color_var = tk.StringVar(
            value=(initial.get('color', _PALETTE[0]) if initial
                   else _PALETTE[0]))
        ttk.Button(f, text='选择…', command=self._pick_color
                   ).grid(row=3, column=1, sticky='w', pady=4, padx=8)
        self.color_preview = tk.Canvas(f, width=24, height=16,
                                        bg=self.color_var.get(),
                                        highlightthickness=0)
        self.color_preview.grid(row=3, column=2)

        ttk.Label(f, text='Y 最小（空=自动）').grid(
            row=4, column=0, sticky='w', pady=4)
        self.ymin_var = tk.StringVar(
            value=('' if not initial or initial.get('y_min') is None
                   else str(initial['y_min'])))
        ttk.Entry(f, textvariable=self.ymin_var, width=12
                  ).grid(row=4, column=1, sticky='w', pady=4, padx=8)

        ttk.Label(f, text='Y 最大（空=自动）').grid(
            row=5, column=0, sticky='w', pady=4)
        self.ymax_var = tk.StringVar(
            value=('' if not initial or initial.get('y_max') is None
                   else str(initial['y_max'])))
        ttk.Entry(f, textvariable=self.ymax_var, width=12
                  ).grid(row=5, column=1, sticky='w', pady=4, padx=8)

        ttk.Label(f, text='告警下限（空=不告警）').grid(
            row=6, column=0, sticky='w', pady=4)
        self.warn_min_var = tk.StringVar(
            value=('' if not initial or initial.get('warn_min') is None
                   else str(initial['warn_min'])))
        ttk.Entry(f, textvariable=self.warn_min_var, width=12
                  ).grid(row=6, column=1, sticky='w', pady=4, padx=8)

        ttk.Label(f, text='告警上限（空=不告警）').grid(
            row=7, column=0, sticky='w', pady=4)
        self.warn_max_var = tk.StringVar(
            value=('' if not initial or initial.get('warn_max') is None
                   else str(initial['warn_max'])))
        ttk.Entry(f, textvariable=self.warn_max_var, width=12
                  ).grid(row=7, column=1, sticky='w', pady=4, padx=8)

        ttk.Label(f, text='显示在图表').grid(row=8, column=0, sticky='w', pady=4)
        plot_choices = list(self._plot_names)
        if not plot_choices:
            plot_choices = ['（默认图表）']
        init_plot = initial_plot if initial_plot is not None else (
            plot_choices[0] if plot_choices else None)
        self.plot_var = tk.StringVar(value=init_plot or '')
        plot_combo = ttk.Combobox(f, textvariable=self.plot_var, width=18,
                                   values=plot_choices, state='readonly')
        plot_combo.grid(row=8, column=1, sticky='w', pady=4, padx=8)

        reg_hint = ('变量名匹配注册表时自动定位日志数据；'
                    '未注册变量将无法匹配，仅显示 —')
        ttk.Label(f, text=reg_hint, foreground=_default_theme()['muted']
                  ).grid(row=9, column=0, columnspan=3, sticky='w', pady=4)

        btns = ttk.Frame(f)
        btns.grid(row=10, column=0, columnspan=3, pady=(10, 0))
        ttk.Button(btns, text='确定', command=self._ok
                   ).pack(side='left', padx=4)
        ttk.Button(btns, text='取消', command=self._cancel
                   ).pack(side='left', padx=4)

        self.protocol('WM_DELETE_WINDOW', self._cancel)
        self.bind('<Return>', lambda _e: self._ok())
        self.bind('<Escape>', lambda _e: self._cancel())

    def _build_groups(self) -> dict[str, list[str]]:
        """按 log_tag 将注册表变量分组，返回 {组名: [变量名列表]}。

        组名格式：'VCUx(Lxx) MA/MB' 或原始 log_tag（如 '3wd1t_1'）。
        """
        groups: dict[str, list[str]] = {}
        for name, reg in self._registry.items():
            tag = reg.get('log_tag', '')
            if not tag:
                group_key = '其他'
            elif tag.endswith('_STD_DEBUG'):
                # L30_STD_DEBUG → VCU1(L30)
                l_num = tag.split('_')[0]
                vcu_num = int(l_num[1:]) - 30 + 1
                block_name = 'MA' if reg.get('block_idx', 0) == 0 else 'MB'
                group_key = f'VCU{vcu_num}({l_num}) {block_name}'
            else:
                group_key = tag
            if group_key not in groups:
                groups[group_key] = []
            groups[group_key].append(name)
        return groups

    def _filtered_names(self, group_key: str) -> list[str]:
        """根据选中的分组，返回该组下的变量名列表。"""
        groups = self._build_groups()
        names = groups.get(group_key, [])
        return sorted(names)

    def _on_group_changed(self, _e=None):
        """分组选择变化时，更新变量名下拉列表。"""
        group_key = self._group_var.get()
        names = self._filtered_names(group_key)
        self._name_combo.configure(values=names)
        if names:
            self.name_var.set(names[0])
            self._on_name_picked()

    def _on_name_picked(self, _e=None):
        """选择变量名时自动填充中文说明 + 颜色。"""
        name = self.name_var.get().strip()
        reg = self._registry.get(name)
        if not reg:
            return
        if not self.label_var.get().strip():
            self.label_var.set(reg.get('label', ''))
        # 颜色只在用户未改过时自动覆盖（与初始调色板第一色不同即视为改过）
        cur_color = self.color_var.get()
        if not initial_has_color(cur_color):
            self.color_var.set(reg.get('color', _PALETTE[0]))
            self.color_preview.configure(bg=self.color_var.get())

    def _pick_color(self):
        c = colorchooser.askcolor(self.color_var.get())
        if c and c[1]:
            self.color_var.set(c[1])
            self.color_preview.configure(bg=c[1])

    def _ok(self):
        name = self.name_var.get().strip()
        if not name:
            messagebox.showerror('错误', '变量名不能为空', parent=self)
            return
        if name in self._used_names:
            messagebox.showerror('错误', f'变量名 {name} 已存在', parent=self)
            return
        y_min = None
        if self.ymin_var.get().strip():
            try:
                y_min = float(self.ymin_var.get())
            except ValueError:
                messagebox.showerror('错误', 'Y 最小必须是数字', parent=self)
                return
        y_max = None
        if self.ymax_var.get().strip():
            try:
                y_max = float(self.ymax_var.get())
            except ValueError:
                messagebox.showerror('错误', 'Y 最大必须是数字', parent=self)
                return
        warn_min = None
        if self.warn_min_var.get().strip():
            try:
                warn_min = float(self.warn_min_var.get())
            except ValueError:
                messagebox.showerror('错误', '告警下限必须是数字',
                                     parent=self)
                return
        warn_max = None
        if self.warn_max_var.get().strip():
            try:
                warn_max = float(self.warn_max_var.get())
            except ValueError:
                messagebox.showerror('错误', '告警上限必须是数字',
                                     parent=self)
                return
        # 从注册表查 log_tag/group_idx/pos_in_group（用户不感知）
        reg = self._registry.get(name, {})
        self.result = {
            'name': name,
            'label': self.label_var.get().strip() or reg.get('label', ''),
            'color': self.color_var.get(),
            'y_min': y_min,
            'y_max': y_max,
            'warn_min': warn_min,
            'warn_max': warn_max,
            'log_tag': reg.get('log_tag', ''),
            'group_idx': reg.get('group_idx', 0),
            'pos_in_group': reg.get('pos_in_group', 0),
            'block_idx': reg.get('block_idx', 0),
            'plot_name': self.plot_var.get() or '',
        }
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()


def _color_in_palette(c: str) -> bool:
    return c in _PALETTE


def initial_has_color(cur: str) -> bool:
    """已选非默认颜色（用户改过）则返回 True。"""
    return cur not in _PALETTE


class _PlotDialog(tk.Toplevel):
    """配置曲线图表：标题 / X 轴标签 / Y 轴标签 / 包含变量（多选）。

    返回 result: {title, x_label, y_label, vars: [name,...]} 或 None。
    """

    def __init__(self, parent, all_vars, initial=None, title='配置图表',
                 theme=None):
        super().__init__(parent)
        self.title(title)
        self.transient(parent)
        self.resizable(True, True)
        self.result = None
        if theme:
            self.configure(bg=theme['bg'])

        f = ttk.Frame(self, padding=12)
        f.pack(fill='both', expand=True)

        ttk.Label(f, text='标题').grid(row=0, column=0, sticky='w', pady=4)
        self.title_var = tk.StringVar(
            value=initial.get('title', '数据曲线') if initial else '数据曲线')
        ttk.Entry(f, textvariable=self.title_var, width=30
                  ).grid(row=0, column=1, pady=4, padx=8, columnspan=2)

        ttk.Label(f, text='X 轴标签').grid(row=1, column=0, sticky='w', pady=4)
        self.x_var = tk.StringVar(
            value=initial.get('x_label', '时间 (s)') if initial else '时间 (s)')
        ttk.Entry(f, textvariable=self.x_var, width=30
                  ).grid(row=1, column=1, pady=4, padx=8, columnspan=2)

        ttk.Label(f, text='Y 轴标签').grid(row=2, column=0, sticky='w', pady=4)
        self.y_var = tk.StringVar(
            value=initial.get('y_label', '值') if initial else '值')
        ttk.Entry(f, textvariable=self.y_var, width=30
                  ).grid(row=2, column=1, pady=4, padx=8, columnspan=2)

        ttk.Label(f, text='包含变量（多选）', font=_font('bold')
                  ).grid(row=3, column=0, columnspan=3, sticky='w', pady=(8, 2))
        vars_wrap = ttk.Frame(f)
        vars_wrap.grid(row=4, column=0, columnspan=3, sticky='nsew',
                       padx=(0, 4))
        f.rowconfigure(4, weight=1)
        f.columnconfigure(1, weight=1)
        f.columnconfigure(2, weight=1)

        initial_vars = set(initial.get('vars', [])) if initial else set()
        self._var_checks: dict[str, tk.BooleanVar] = {}
        n_cols = 3
        for i, v in enumerate(all_vars):
            name = v['name']
            bv = tk.BooleanVar(value=name in initial_vars)
            label = v.get('label', '') or name
            # 标签太长截断，避免对话框过宽
            disp = label if len(label) <= 10 else label[:9] + '…'
            cb = FillCheck(vars_wrap, text=f'{disp} ({name})', variable=bv)
            cb.grid(row=i // n_cols, column=i % n_cols,
                    sticky='w', padx=4, pady=1)
            self._var_checks[name] = bv

        # 全选/全不选
        sel_btns = ttk.Frame(f)
        sel_btns.grid(row=5, column=0, columnspan=3, sticky='w', pady=(6, 0))
        ttk.Button(sel_btns, text='全选',
                   command=lambda: self._set_all(True)
                   ).pack(side='left', padx=4)
        ttk.Button(sel_btns, text='全不选',
                   command=lambda: self._set_all(False)
                   ).pack(side='left', padx=4)

        btns = ttk.Frame(f)
        btns.grid(row=6, column=0, columnspan=3, pady=(10, 0))
        ttk.Button(btns, text='确定', command=self._ok
                   ).pack(side='left', padx=4)
        ttk.Button(btns, text='取消', command=self._cancel
                   ).pack(side='left', padx=4)

        self.protocol('WM_DELETE_WINDOW', self._cancel)
        self.bind('<Escape>', lambda _e: self._cancel())

    def _set_all(self, val: bool):
        for bv in self._var_checks.values():
            bv.set(val)

    def _ok(self):
        title = self.title_var.get().strip() or '数据曲线'
        x_label = self.x_var.get().strip()
        y_label = self.y_var.get().strip()
        chosen = [n for n, bv in self._var_checks.items() if bv.get()]
        self.result = {
            'title': title, 'x_label': x_label, 'y_label': y_label,
            'vars': chosen,
        }
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()


class MonitorPanel(ttk.Frame):
    """可嵌入的监视面板：曲线模式 + 数据模式。

    可放进 ttk.PanedWindow（嵌入式）或 Toplevel（独立窗口）。
    子类覆盖 DEFAULT_VARS 提供特定领域默认变量。
    """

    REDRAW_MS = 150       # 曲线刷新间隔（≈6.6fps，多窗口同时打开时降 CPU）
    DATA_MS = 250         # 数据文本刷新间隔（4fps）
    MAX_POINTS = 600      # 每变量最大点数（约 60s @ 10fps）
    TIME_WINDOW_S = 60    # X 轴时间窗口

    DEFAULT_VARS: list = []

    def __init__(self, parent, variables=None, on_change=None, plots=None):
        super().__init__(parent)
        self._on_change = on_change
        self._mode = 'curve'
        self._vars: list[dict] = list(variables) if variables else list(self.DEFAULT_VARS)
        self._buffers: dict[str, deque] = {}
        self._compiled: dict[str, re.Pattern | None] = {}
        self._redraw_id: str | None = None
        self._closed = False
        self._loading = False  # 加载历史日志期间为 True，跳过定时重绘避免卡顿
        self._theme = _default_theme()
        # 多图表配置：每个图表 dict = {title, x_label, y_label, vars: [names]}
        # 若传入 plots 则用之；否则默认一个图表包含全部变量
        if plots:
            self._plots: list[dict] = [self._normalize_plot(p)
                                       for p in plots]
        else:
            self._plots = [{
                'title': '数据曲线',
                'x_label': '时间 (s)',
                'y_label': '值',
                'vars': [v['name'] for v in self._vars],
            }]
        self._plot_frames: list[ttk.Frame] = []
        self._plot_canvases: list[tk.Canvas] = []

        self._build_toolbar()
        self._build_content()
        self.apply_theme(self._theme)

        # 历史日志回放数据：加载日志文件后填充，绘制时优先用 _history
        # 形如 {var_name: list[(ts, val)]}，不限大小
        self._history: dict[str, list[tuple[float, float]]] | None = None
        # 告警指示色块：每变量一个 Canvas，超限时变红
        self._warn_indicators: dict[str, tk.Canvas] = {}
        # 缓存 Font 对象，避免每次 _draw_plot 都 tkfont.Font() 新建（创建销毁开销大）
        self._legend_font = tkfont.Font(font=('DejaVu Sans', 9))
        # 实时模式空闲跳过：记录上次绘图时的最新 ts，没新数据就不重绘
        self._last_drawn_ts: float = 0.0

        self._rebuild_buffers()
        self._refresh_var_list()
        self._rebuild_data_grid()
        self._rebuild_plots()
        self._show_mode()
        self._schedule_redraw()

    # ---- 工具栏 --------------------------------------------------------
    def _build_toolbar(self):
        bar = ttk.Frame(self, padding=(8, 6))
        bar.pack(fill='x')

        ttk.Button(bar, text='添加变量', command=self._add_var).pack(side='left')
        ttk.Button(bar, text='编辑选中', command=self._edit_var).pack(side='left', padx=4)
        ttk.Button(bar, text='删除选中', command=self._del_var).pack(side='left')

        ttk.Separator(bar, orient='vertical').pack(side='left', fill='y', padx=8)

        self._plot_btns_frame = ttk.Frame(bar)
        self._plot_btns_frame.pack(side='left')
        ttk.Button(self._plot_btns_frame, text='添加图表',
                   command=self._add_plot).pack(side='left')
        ttk.Button(self._plot_btns_frame, text='编辑图表',
                   command=self._edit_plot, style='').pack(side='left', padx=4)
        ttk.Button(self._plot_btns_frame, text='删除图表',
                   command=self._del_plot).pack(side='left')

        ttk.Separator(bar, orient='vertical').pack(side='left', fill='y', padx=8)

        ttk.Label(bar, text='模式').pack(side='left', padx=(0, 4))
        self._mode_var = tk.StringVar(value='曲线')
        mode_box = ttk.Combobox(bar, textvariable=self._mode_var, width=8,
                               state='readonly', values=['曲线', '数据'])
        mode_box.pack(side='left')
        mode_box.bind('<<ComboboxSelected>>', lambda _e: self._on_mode_change())

        ttk.Separator(bar, orient='vertical').pack(side='left', fill='y', padx=8)
        ttk.Button(bar, text='清空数据', command=self._clear_data).pack(side='left')

        ttk.Separator(bar, orient='vertical').pack(side='left', fill='y', padx=8)
        ttk.Button(bar, text='加载日志…',
                   command=self._load_history_log).pack(side='left')
        ttk.Button(bar, text='统计…',
                   command=self._show_statistics).pack(side='left', padx=4)
        ttk.Button(bar, text='导出 CSV…',
                   command=self._export_csv).pack(side='left')

        ttk.Separator(bar, orient='vertical').pack(side='left', fill='y', padx=8)
        self._fullscreen_btn = ttk.Button(bar, text='全屏',
                                          command=self._toggle_fullscreen)
        self._fullscreen_btn.pack(side='left')

        self._status_lbl = ttk.Label(bar, text='')
        self._status_lbl.pack(side='right')

    def _build_content(self):
        # 外层水平 PanedWindow：左变量列表 + 右曲线/数值面板
        pane = ttk.PanedWindow(self, orient='horizontal')
        pane.pack(fill='both', expand=True)

        left = ttk.Frame(pane, width=260)
        pane.add(left, weight=0)
        ttk.Label(left, text='变量列表', font=_font('bold')).pack(
            anchor='w', padx=8, pady=(8, 4))
        self._var_tree = ttk.Treeview(
            left, columns=('name', 'regex'), show='headings',
            height=12, selectmode='browse')
        self._var_tree.heading('name', text='名称')
        self._var_tree.heading('regex', text='正则')
        self._var_tree.column('name', width=90, stretch=False)
        self._var_tree.column('regex', width=160)
        self._var_tree.pack(fill='both', expand=True, padx=4, pady=4)
        self._var_tree.bind('<Double-1>', lambda _e: self._edit_var())

        right = ttk.Frame(pane)
        pane.add(right, weight=3)

        # 曲线模式：网格容器容纳 N 个图表（自动行列排列）
        self._plots_container = ttk.Frame(right)
        # 数据模式：单 frame 显示数值网格
        self._data_frame = ttk.Frame(right)
        self._data_labels: dict[str, tk.StringVar] = {}
        # 兼容 apply_theme 调用 self._canvas.configure：用第一个 plot 的 canvas
        self._canvas = tk.Canvas(right, bg=self._theme['panel_bg'],
                                 highlightthickness=0, borderwidth=0)

    # ---- 变量管理 ------------------------------------------------------
    def _rebuild_buffers(self):
        new_buf = {}
        new_pat = {}
        for v in self._vars:
            name = v['name']
            if name in self._buffers:
                new_buf[name] = self._buffers[name]
            else:
                new_buf[name] = deque(maxlen=self.MAX_POINTS)
            try:
                new_pat[name] = re.compile(v['regex'])
            except re.error:
                new_pat[name] = None
        self._buffers = new_buf
        self._compiled = new_pat

    def _refresh_var_list(self):
        for iid in self._var_tree.get_children():
            self._var_tree.delete(iid)
        for v in self._vars:
            self._var_tree.insert('', 'end', iid=v['name'],
                                  values=(v['name'], v['regex']))

    def _add_var(self):
        used = {v['name'] for v in self._vars}
        dlg = _VarDialog(self.winfo_toplevel(), used_names=used,
                         theme=self._theme)
        self.winfo_toplevel().wait_window(dlg)
        if dlg.result:
            self._vars.append(dlg.result)
            self._auto_add_var_to_plots(dlg.result['name'])
            self._rebuild_buffers()
            self._refresh_var_list()
            self._rebuild_data_grid()
            self._rebuild_plots()
            self._notify_change()

    def _edit_var(self):
        sel = self._var_tree.selection()
        if not sel:
            return
        old_name = sel[0]
        idx = next((i for i, v in enumerate(self._vars)
                    if v['name'] == old_name), None)
        if idx is None:
            return
        used = {v['name'] for v in self._vars}
        used.discard(old_name)
        dlg = _VarDialog(self.winfo_toplevel(), initial=self._vars[idx],
                         title='编辑变量', used_names=used,
                         theme=self._theme)
        self.winfo_toplevel().wait_window(dlg)
        if dlg.result:
            new_name = dlg.result['name']
            if new_name != old_name and old_name in self._buffers:
                self._buffers[new_name] = self._buffers.pop(old_name)
            self._vars[idx] = dlg.result
            if new_name != old_name:
                self._sync_plots_var_renamed(old_name, new_name)
            self._rebuild_buffers()
            self._refresh_var_list()
            self._rebuild_data_grid()
            self._rebuild_plots()
            self._notify_change()

    def _del_var(self):
        sel = self._var_tree.selection()
        if not sel:
            return
        name = sel[0]
        self._vars = [v for v in self._vars if v['name'] != name]
        self._buffers.pop(name, None)
        self._compiled.pop(name, None)
        self._sync_plots_var_removed(name)
        self._refresh_var_list()
        self._rebuild_data_grid()
        self._rebuild_plots()
        self._notify_change()

    def _clear_data(self):
        for buf in self._buffers.values():
            buf.clear()
        # 清空历史日志回放数据，回到实时模式
        self._history = None

    # ---- 历史日志回放 --------------------------------------------------
    def _extract_value(self, line: str, v: dict) -> float | None:
        """从日志行提取变量值。子类可覆盖以使用结构化解析。"""
        name = v['name']
        pat = self._compiled.get(name)
        if pat is None:
            return None
        m = pat.search(line)
        if m:
            try:
                return float(m.group(1))
            except (ValueError, IndexError):
                return None
        return None

    def _extract_value_parsed(self, line: str, v: dict,
                              parsed: dict) -> float | None:
        """基类默认：无解析缓存，直接走 _extract_value。
        结构化子类覆盖此方法以复用同一行的解析结果。"""
        return self._extract_value(line, v)

    def _load_history_log(self):
        """打开历史日志文件，分块解析填充 _history，避免 UI 卡死。"""
        try:
            path = filedialog.askopenfilename(
                title='加载历史日志文件', parent=self.winfo_toplevel(),
                filetypes=[('文本文件', '*.txt'), ('日志', '*.log'),
                           ('所有文件', '*.*')])
        except Exception as e:
            messagebox.showerror('打开失败', f'文件对话框打开失败：{e}',
                                 parent=self.winfo_toplevel())
            return
        if not path:
            return
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                lines = f.readlines()
        except Exception as e:
            messagebox.showerror('打开失败', f'{e}',
                                 parent=self.winfo_toplevel())
            return
        # 初始化 _history 容器（按最大数据量预分配，避免反复扩容）
        self._history = {v['name']: [] for v in self._vars}
        n_pts = [0]
        total = len(lines)
        chunk = 50000  # 加大批次：减少 after 调度次数 + 让单行处理更内联

        # 加载期间跳过定时重绘
        self._loading = True
        self._status_lbl.configure(text=f'加载日志中 0/{total} 行…')
        self.update_idletasks()

        def _process_batch(start: int):
            end = min(start + chunk, total)
            # 局部绑定，减少属性查找
            history = self._history
            variables = self._vars
            extract_ts = _extract_log_ts
            extract_val = self._extract_value_parsed
            for i in range(start, end):
                line = lines[i]
                # 不需要 rstrip：时间戳正则和结构化解析都不受尾部 \n 影响
                ts = extract_ts(line)
                if ts is None:
                    ts = 0.0
                parsed: dict = {}
                for v in variables:
                    val = extract_val(line, v, parsed)
                    if val is not None:
                        history[v['name']].append((ts, val))
                        n_pts[0] += 1
            self._status_lbl.configure(
                text=f'加载日志中 {end}/{total} 行…（{n_pts[0]} 个数据点）')
            self.update_idletasks()
            if end < total:
                self.after(0, lambda: _process_batch(end))
            else:
                # 完成
                self._loading = False
                if n_pts[0] == 0:
                    self._history = None
                    self._status_lbl.configure(text='')
                    messagebox.showinfo(
                        '提示',
                        '未在日志中匹配到任何变量数据。请检查变量正则是否对应日志格式。',
                        parent=self.winfo_toplevel())
                else:
                    self._status_lbl.configure(
                        text=f'历史模式：{n_pts[0]} 个数据点')
                    # 完成后立即重绘一次
                    try:
                        self._draw_curves()
                    except Exception:
                        pass
                    messagebox.showinfo(
                        '加载完成',
                        f'已加载 {total} 行日志，匹配 {n_pts[0]} 个数据点。\n'
                        f'当前显示历史数据；点击"清空数据"回到实时模式。',
                        parent=self.winfo_toplevel())

        _process_batch(0)

    def _export_csv(self):
        """导出当前所有变量数据点为 CSV。"""
        # 数据源：_history 优先，否则 _buffers
        if self._history is not None:
            data = self._history
            source_name = '历史日志'
        else:
            data = {n: list(buf) for n, buf in self._buffers.items()}
            source_name = '实时缓冲'
        if not any(pts for pts in data.values()):
            messagebox.showinfo('提示', '没有可导出的数据',
                                parent=self.winfo_toplevel())
            return
        path = filedialog.asksaveasfilename(
            title='导出 CSV', parent=self.winfo_toplevel(),
            defaultextension='.csv',
            filetypes=[('CSV', '*.csv'), ('所有文件', '*.*')])
        if not path:
            return
        try:
            with open(path, 'w', encoding='utf-8') as f:
                # 表头：时间戳 + 各变量名
                names = list(data.keys())
                f.write('timestamp,' + ','.join(names) + '\n')
                # 按时间戳合并
                # 每个变量独立时间轴，CSV 用稀疏表：每行一个 (ts, name, val)
                # 这里改为"时间戳 + 每变量当时最新值"格式（密集表）：
                # 收集所有时间戳排序
                all_ts = sorted({ts for pts in data.values() for ts, _ in pts})
                # 每变量按 ts 索引当前值
                cursor = {n: 0 for n in names}
                for ts in all_ts:
                    row = [f'{ts:.3f}']
                    for n in names:
                        pts = data[n]
                        # 推进 cursor 到 >= ts 的最新点
                        while cursor[n] < len(pts) - 1 and pts[cursor[n] + 1][0] <= ts:
                            cursor[n] += 1
                        if cursor[n] < len(pts) and pts[cursor[n]][0] <= ts:
                            row.append(f'{pts[cursor[n]][1]:g}')
                        else:
                            row.append('')
                    f.write(','.join(row) + '\n')
        except Exception as e:
            messagebox.showerror('导出失败', f'{e}',
                                 parent=self.winfo_toplevel())

    def _show_statistics(self):
        """弹窗显示每个变量的统计信息（最大/最小/平均/当前/点数）。"""
        if self._history is not None:
            data = self._history
            source = '历史日志'
        else:
            data = {n: list(buf) for n, buf in self._buffers.items()}
            source = '实时缓冲'
        dlg = tk.Toplevel(self.winfo_toplevel())
        dlg.title(f'统计信息（{source}）')
        dlg.geometry('680x480')
        dlg.transient(self.winfo_toplevel())
        try:
            dlg.configure(bg=self._theme['bg'])
        except Exception:
            pass
        tree = ttk.Treeview(dlg, columns=('name', 'label', 'current',
                                          'min', 'max', 'avg', 'n'),
                            show='headings', height=20)
        for col, label, w in [('name', '变量名', 140),
                              ('label', '说明', 160),
                              ('current', '当前值', 80),
                              ('min', '最小', 80), ('max', '最大', 80),
                              ('avg', '平均', 80), ('n', '点数', 60)]:
            tree.heading(col, text=label)
            tree.column(col, width=w, stretch=False)
        vsb = ttk.Scrollbar(dlg, orient='vertical', command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side='left', fill='both', expand=True, padx=(8, 0), pady=8)
        vsb.pack(side='right', fill='y', pady=8)
        for v in self._vars:
            pts = data.get(v['name'], [])
            if not pts:
                tree.insert('end', values=(v['name'],
                                           v.get('label', ''),
                                           '—', '—', '—', '—', 0))
                continue
            vals = [p[1] for p in pts]
            cur = vals[-1]
            tree.insert('end', values=(
                v['name'], v.get('label', ''),
                f'{cur:g}', f'{min(vals):g}', f'{max(vals):g}',
                f'{sum(vals)/len(vals):g}', len(vals)))
        ttk.Button(dlg, text='关闭', command=dlg.destroy
                   ).pack(side='bottom', pady=4)

    def _sync_plots_var_renamed(self, old_name: str, new_name: str):
        """变量改名后，同步更新所有 plot 的 vars 引用。"""
        for plot in self._plots:
            vars_list = plot.get('vars', [])
            if old_name in vars_list:
                plot['vars'] = [new_name if n == old_name else n
                                for n in vars_list]

    def _sync_plots_var_removed(self, name: str):
        """变量删除后，从所有 plot 的 vars 中移除。"""
        for plot in self._plots:
            if name in plot.get('vars', []):
                plot['vars'] = [n for n in plot.get('vars', []) if n != name]

    def _auto_add_var_to_plots(self, name: str):
        """新增变量自动加入第一个图表，便于立即看到曲线。"""
        if not self._plots:
            self._plots = [{'title': '数据曲线', 'x_label': '时间 (s)',
                            'y_label': '值', 'vars': [name]}]
            return
        first = self._plots[0]
        if name not in first.setdefault('vars', []):
            first['vars'].append(name)

    def _add_var_to_named_plot(self, name: str, plot_name: str):
        """把变量加入指定标题的图表；找不到则加入第一个图表。

        同时从其他图表移除该变量（一个变量只能在一个图表里）。
        """
        if not self._plots:
            self._plots = [{'title': plot_name or '数据曲线',
                            'x_label': '时间 (s)', 'y_label': '值',
                            'vars': [name]}]
            return
        target = None
        for plot in self._plots:
            if plot.get('title', '') == plot_name:
                target = plot
                break
        if target is None:
            target = self._plots[0]
        for plot in self._plots:
            if plot is target:
                continue
            if name in plot.get('vars', []):
                plot['vars'] = [n for n in plot.get('vars', []) if n != name]
        if name not in target.setdefault('vars', []):
            target['vars'].append(name)

    def _move_var_to_named_plot(self, name: str, plot_name: str):
        """编辑变量后，把变量迁移到用户选择的图表。"""
        if not self._plots:
            return
        target = None
        for plot in self._plots:
            if plot.get('title', '') == plot_name:
                target = plot
                break
        if target is None:
            target = self._plots[0]
        for plot in self._plots:
            if plot is target:
                continue
            if name in plot.get('vars', []):
                plot['vars'] = [n for n in plot.get('vars', []) if n != name]
        if name not in target.setdefault('vars', []):
            target['vars'].append(name)

    def _notify_change(self):
        if self._on_change:
            try:
                plots_norm = [self._normalize_plot(p) for p in self._plots]
                self._on_change(list(self._vars), plots_norm)
            except Exception:
                pass

    def _toggle_fullscreen(self):
        """切换包含此面板的 Toplevel 窗口全屏/还原。"""
        top = self.winfo_toplevel()
        if not isinstance(top, tk.Toplevel):
            return
        if top._is_fullscreen:
            top.attributes('-fullscreen', False)
            top._is_fullscreen = False
            self._fullscreen_btn.configure(text='全屏')
        else:
            top.attributes('-fullscreen', True)
            top._is_fullscreen = True
            self._fullscreen_btn.configure(text='还原')

    # ---- 模式切换 ------------------------------------------------------
    def _on_mode_change(self):
        m = self._mode_var.get()
        self._mode = 'curve' if m == '曲线' else 'data'
        self._show_mode()

    def _show_mode(self):
        if self._mode == 'curve':
            self._data_frame.pack_forget()
            self._plots_container.pack(fill='both', expand=True)
            # 图表管理按钮仅在曲线模式可用
            for child in self._plot_btns_frame.winfo_children():
                try:
                    child.configure(state='normal')
                except Exception:
                    pass
        else:
            self._plots_container.pack_forget()
            self._rebuild_data_grid()
            self._data_frame.pack(fill='both', expand=True)
            for child in self._plot_btns_frame.winfo_children():
                try:
                    child.configure(state='disabled')
                except Exception:
                    pass

    # ---- 图表管理（曲线模式多图表） -----------------------------------
    def _rebuild_plots(self):
        """根据 self._plots 重建网格容器中的 plot frame + canvas。

        自动计算行列数：n 个图表 → 近正方形网格排列。
        列数 = max(1, ceil(sqrt(n)))，行数 = ceil(n / cols)。
        """
        container = self._plots_container
        for child in list(container.winfo_children()):
            child.destroy()
        self._plot_frames = []
        self._plot_canvases = []
        # 清除旧的 grid 配置
        for i in range(container.grid_size()[0]):
            container.rowconfigure(i, weight=0)
        for i in range(container.grid_size()[1]):
            container.columnconfigure(i, weight=0)
        n = len(self._plots)
        if n == 0:
            return
        # 自动计算行列数（近正方形排列）
        cols = max(1, math.ceil(math.sqrt(n)))
        rows = max(1, math.ceil(n / cols))
        # 设置行列权重（等大小分配）
        for r in range(rows):
            container.rowconfigure(r, weight=1, minsize=100)
        for c in range(cols):
            container.columnconfigure(c, weight=1, minsize=100)
        for i, plot in enumerate(self._plots):
            r = i // cols
            c = i % cols
            frame = ttk.Frame(container, padding=2)
            frame.grid(row=r, column=c, sticky='nsew')
            canvas = tk.Canvas(frame, bg=self._theme['panel_bg'],
                               highlightthickness=0, borderwidth=0)
            canvas.pack(fill='both', expand=True)
            plot['_canvas'] = canvas
            plot['_frame'] = frame
            # 左键拖动 = 框选放大；中键/右键拖动 = 平移；
            # 双击 = 重置缩放；滚轮 = 以鼠标位置为中心缩放
            canvas.bind('<Button-1>', self._on_plot_zoom_start)
            canvas.bind('<B1-Motion>', self._on_plot_zoom_motion)
            canvas.bind('<ButtonRelease-1>', self._on_plot_zoom_release)
            canvas.bind('<Double-Button-1>', self._on_plot_double_click)
            canvas.bind('<Button-2>', self._on_plot_pan_start)
            canvas.bind('<B2-Motion>', self._on_plot_pan_motion)
            canvas.bind('<ButtonRelease-2>', self._on_plot_pan_release)
            canvas.bind('<Button-3>', self._show_plot_menu)
            # 滚轮 = 以鼠标为中心缩放；Shift+滚轮 = X 轴平移
            canvas.bind('<MouseWheel>', self._on_plot_wheel_zoom)
            canvas.bind('<Shift-MouseWheel>', self._on_plot_wheel_x)
            # 窗口/Canvas 大小变化时强制重绘（避免空闲跳过导致 resize 后画面不刷新）
            canvas.bind('<Configure>',
                        lambda _e: self._force_redraw())
            self._plot_frames.append(frame)
            self._plot_canvases.append(canvas)
        if self._plot_canvases:
            self._canvas = self._plot_canvases[0]

    # ---- 曲线图交互：平移 / 框选缩放 / 滚轮 slip / 右键菜单 / 单独显示 ----
    def _canvas_plot(self, canvas: tk.Canvas) -> dict | None:
        """根据 canvas 反查 plot 字典。"""
        for plot in self._plots:
            if plot.get('_canvas') is canvas or \
               plot.get('_popout_canvas') is canvas:
                return plot
        return None

    def _plot_geom(self, plot: dict):
        """获取绘图区几何（pad_l/pad_t/plot_w/plot_h/t_min/t_max/y_lo/y_hi）。
        若尚未绘制（_geom 缺失），返回 None。"""
        return plot.get('_geom')

    def _ensure_zoom_for_pan(self, plot: dict):
        """平移前若没有 zoom，把当前自动范围固化为 zoom 起点。"""
        if plot.get('_zoom'):
            return
        geom = plot.get('_geom')
        if not geom:
            return
        _, _, _, _, t_min, t_max, y_lo, y_hi = geom
        # 确保使用最新自动 y 范围（_auto_y 在 _draw_plot 中更新）
        auto_y = plot.get('_auto_y')
        if auto_y:
            y_lo, y_hi = auto_y
        plot['_zoom'] = (t_min, t_max, y_lo, y_hi)

    def _on_plot_pan_start(self, event):
        """左键按下（无 Shift）：准备平移。"""
        plot = self._canvas_plot(event.widget)
        if plot is None:
            return
        self._ensure_zoom_for_pan(plot)
        plot['_pan_start'] = (event.x, event.y)
        plot['_pan_zoom0'] = plot.get('_zoom')

    def _on_plot_pan_motion(self, event):
        """左键拖动（无 Shift）：平移视图。"""
        plot = self._canvas_plot(event.widget)
        if plot is None or '_pan_start' not in plot:
            return
        geom = plot.get('_geom')
        zoom0 = plot.get('_pan_zoom0')
        if not geom or not zoom0:
            return
        x0, y0 = plot['_pan_start']
        dx = event.x - x0
        dy = event.y - y0
        pad_l, pad_t, plot_w, plot_h, _t_min, _t_max, _y_lo, _y_hi = geom
        if plot_w <= 0 or plot_h <= 0:
            return
        t_min0, t_max0, y_lo0, y_hi0 = zoom0
        dt = (t_max0 - t_min0) * dx / plot_w
        dv = (y_hi0 - y_lo0) * dy / plot_h
        plot['_zoom'] = (t_min0 - dt, t_max0 - dt,
                         y_lo0 + dv, y_hi0 + dv)
        self._draw_plot(event.widget, plot)

    def _on_plot_pan_release(self, event):
        """中键松开：结束平移，保留当前视图位置（不清空 _zoom）。"""
        plot = self._canvas_plot(event.widget)
        if plot is None:
            return
        plot.pop('_pan_start', None)
        plot.pop('_pan_zoom0', None)

    def _on_plot_zoom_start(self, event):
        """左键按下：开始框选缩放（或单击不拖动则保留原状态）。"""
        plot = self._canvas_plot(event.widget)
        if plot is None:
            return
        self._ensure_zoom_for_pan(plot)
        plot['_zoom_drag_start'] = (event.x, event.y)
        plot['_zoom_rect'] = (event.x, event.y, event.x, event.y)
        plot['_zoom_moved'] = False
        # 记住起始 zoom 供缩放计算
        plot['_zoom_zoom0'] = plot.get('_zoom')

    def _on_plot_zoom_motion(self, event):
        """左键拖动：更新选框矩形。"""
        plot = self._canvas_plot(event.widget)
        if plot is None or '_zoom_drag_start' not in plot:
            return
        x0, y0 = plot['_zoom_drag_start']
        if abs(event.x - x0) > 3 or abs(event.y - y0) > 3:
            plot['_zoom_moved'] = True
        plot['_zoom_rect'] = (min(x0, event.x), min(y0, event.y),
                              max(x0, event.x), max(y0, event.y))
        canvas = event.widget
        if canvas.winfo_exists():
            self._draw_plot(canvas, plot)

    def _on_plot_zoom_release(self, event):
        """左键松开：应用框选缩放（保留 _zoom 锁定视图）。"""
        plot = self._canvas_plot(event.widget)
        if plot is None:
            return
        moved = plot.pop('_zoom_moved', False)
        rect = plot.pop('_zoom_rect', None)
        plot.pop('_zoom_drag_start', None)
        plot.pop('_zoom_zoom0', None)
        if not moved or rect is None:
            return
        x0, y0, x1, y1 = rect
        geom = plot.get('_geom')
        if not geom:
            return
        pad_l, pad_t, plot_w, plot_h, t_min, t_max, y_lo, y_hi = geom
        cx0 = max(pad_l, min(pad_l + plot_w, x0))
        cy0 = max(pad_t, min(pad_t + plot_h, y0))
        cx1 = max(pad_l, min(pad_l + plot_w, x1))
        cy1 = max(pad_t, min(pad_t + plot_h, y1))
        if cx0 == cx1 or cy0 == cy1:
            return
        new_t_min = t_min + (t_max - t_min) * (cx0 - pad_l) / plot_w
        new_t_max = t_min + (t_max - t_min) * (cx1 - pad_l) / plot_w
        new_y_hi = y_hi - (y_hi - y_lo) * (cy0 - pad_t) / plot_h
        new_y_lo = y_hi - (y_hi - y_lo) * (cy1 - pad_t) / plot_h
        # 保留 _zoom，锁定在选中区域
        plot['_zoom'] = (new_t_min, new_t_max, new_y_lo, new_y_hi)
        self._draw_plot(event.widget, plot)

    def _on_plot_double_click(self, event):
        """双击：重置缩放，恢复自动滚动。"""
        plot = self._canvas_plot(event.widget)
        if plot is None:
            return
        self._reset_plot_zoom(plot)

    def _on_plot_wheel_zoom(self, event):
        """Ctrl+滚轮（或裸滚轮）：以鼠标位置为中心缩放。"""
        plot = self._canvas_plot(event.widget)
        if plot is None:
            return
        self._ensure_zoom_for_pan(plot)
        zoom = plot.get('_zoom')
        geom = plot.get('_geom')
        if not zoom or not geom:
            return
        t_min, t_max, y_lo, y_hi = zoom
        pad_l, pad_t, plot_w, plot_h, _, _, _, _ = geom
        if plot_w <= 0 or plot_h <= 0:
            return
        # 鼠标位置在数据坐标系中的比例
        rx = (event.x - pad_l) / plot_w
        ry = (event.y - pad_t) / plot_h
        if not (0 <= rx <= 1):
            rx = max(0, min(1, rx))
        if not (0 <= ry <= 1):
            ry = max(0, min(1, ry))
        # 缩放因子：滚轮向上放大（数据范围缩小），向下缩小（范围扩大）
        factor = 0.8 if event.delta > 0 else 1.25
        # 限制缩放过度（防止范围过小或倒置）
        new_t_span = max((t_max - t_min) * factor, 0.1)
        new_y_span = max((y_hi - y_lo) * factor, 1e-6)
        # 鼠标位置保持不动，反推新的 t_min/y_lo
        new_t_min = t_min + rx * ((t_max - t_min) - new_t_span)
        new_t_max = new_t_min + new_t_span
        new_y_hi = y_lo + ry * (y_hi - y_lo) + new_y_span * (1 - ry)
        new_y_lo = new_y_hi - new_y_span
        plot['_zoom'] = (new_t_min, new_t_max, new_y_lo, new_y_hi)
        self._draw_plot(event.widget, plot)

    def _on_plot_wheel_x(self, event):
        """Shift + 滚轮：水平 slip（平移时间轴）。"""
        plot = self._canvas_plot(event.widget)
        if plot is None:
            return
        self._ensure_zoom_for_pan(plot)
        zoom = plot.get('_zoom')
        geom = plot.get('_geom')
        if not zoom or not geom:
            return
        t_min, t_max, y_lo, y_hi = zoom
        _, _, plot_w, _, _, _, _, _ = geom
        if plot_w <= 0:
            return
        step = (t_max - t_min) * 0.1
        # event.delta > 0 = 滚轮向上（在 Linux 上 button 4）
        d = -step if event.delta > 0 else step
        plot['_zoom'] = (t_min + d, t_max + d, y_lo, y_hi)
        self._draw_plot(event.widget, plot)

    def _on_plot_wheel_y(self, event):
        """滚轮：垂直 slip（平移 Y 轴）。"""
        plot = self._canvas_plot(event.widget)
        if plot is None:
            return
        self._ensure_zoom_for_pan(plot)
        zoom = plot.get('_zoom')
        geom = plot.get('_geom')
        if not zoom or not geom:
            return
        t_min, t_max, y_lo, y_hi = zoom
        _, _, _, plot_h, _, _, _, _ = geom
        if plot_h <= 0:
            return
        step = (y_hi - y_lo) * 0.1
        d = step if event.delta > 0 else -step
        plot['_zoom'] = (t_min, t_max, y_lo + d, y_hi + d)
        self._draw_plot(event.widget, plot)

    def _show_plot_menu(self, event):
        """右键单击：弹出菜单。"""
        canvas: tk.Canvas = event.widget
        plot = self._canvas_plot(canvas)
        if plot is None:
            return
        t = self._theme
        menu = tk.Menu(canvas, tearoff=False,
                       bg=t['bg'], fg=t['fg'],
                       activebackground=t['select_bg'],
                       activeforeground='#ffffff',
                       disabledforeground=t['muted'],
                       borderwidth=0, relief='flat')
        if plot.get('_zoom'):
            menu.add_command(label='  重置缩放',
                              command=lambda: self._reset_plot_zoom(plot))
        else:
            menu.add_command(label='  重置缩放', state='disabled')
        menu.add_separator()
        if plot.get('_popout_canvas') is not None:
            menu.add_command(label='  关闭单独显示',
                              command=lambda: self._close_plot_popout(plot))
        else:
            menu.add_command(label='  单独显示',
                              command=lambda: self._popout_plot(plot))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _reset_plot_zoom(self, plot: dict):
        plot['_zoom'] = None
        canvas = plot.get('_canvas')
        if canvas is not None and canvas.winfo_exists():
            self._draw_plot(canvas, plot)

    def _popout_plot(self, plot: dict):
        """把某个 plot 弹到独立 Toplevel 窗口放大显示。"""
        if plot.get('_popout_canvas') is not None:
            return
        t = self._theme
        top = tk.Toplevel(self.winfo_toplevel())
        title = plot.get('title', '曲线图')
        top.title(f'{title} - 单独显示')
        top.geometry('900x560')
        top.minsize(500, 320)
        top.configure(bg=t['bg'])
        top.resizable(True, True)
        try:
            top.lift(self.winfo_toplevel())
            top.focus_force()
        except Exception:
            pass
        canvas = tk.Canvas(top, bg=self._theme['panel_bg'],
                           highlightthickness=0, borderwidth=0)
        canvas.pack(fill='both', expand=True)
        plot['_popout_canvas'] = canvas
        plot['_popout_toplevel'] = top
        # 与内嵌 canvas 相同的交互绑定
        canvas.bind('<Button-1>', self._on_plot_zoom_start)
        canvas.bind('<B1-Motion>', self._on_plot_zoom_motion)
        canvas.bind('<ButtonRelease-1>', self._on_plot_zoom_release)
        canvas.bind('<Double-Button-1>', self._on_plot_double_click)
        canvas.bind('<Button-2>', self._on_plot_pan_start)
        canvas.bind('<B2-Motion>', self._on_plot_pan_motion)
        canvas.bind('<ButtonRelease-2>', self._on_plot_pan_release)
        canvas.bind('<Button-3>', self._show_plot_menu)
        canvas.bind('<MouseWheel>', self._on_plot_wheel_zoom)
        canvas.bind('<Shift-MouseWheel>', self._on_plot_wheel_x)
        top.protocol('WM_DELETE_WINDOW', lambda: self._close_plot_popout(plot))
        # 等画布就绪后画一次
        top.after(50, lambda: self._draw_plot(canvas, plot))

    def _close_plot_popout(self, plot: dict):
        top = plot.pop('_popout_toplevel', None)
        plot.pop('_popout_canvas', None)
        if top is not None:
            try:
                top.destroy()
            except Exception:
                pass

    def _add_plot(self):
        if not self._vars:
            messagebox.showinfo('提示', '请先添加变量', parent=self)
            return
        dlg = _PlotDialog(self.winfo_toplevel(), all_vars=self._vars,
                          title='添加图表', theme=self._theme)
        self.winfo_toplevel().wait_window(dlg)
        if dlg.result:
            self._plots.append(dlg.result)
            self._rebuild_plots()
            self._notify_change()

    def _edit_plot(self):
        if not self._plots:
            messagebox.showinfo('提示', '没有可编辑的图表', parent=self)
            return
        names = [f'{i+1}. {p.get("title", "未命名")}' for i, p in enumerate(self._plots)]
        choice = self._pick_plot_dialog(names, title='编辑图表 - 选择')
        if choice is None or not (0 <= choice < len(self._plots)):
            return
        dlg = _PlotDialog(self.winfo_toplevel(), all_vars=self._vars,
                          initial=self._plots[choice], title='编辑图表',
                          theme=self._theme)
        self.winfo_toplevel().wait_window(dlg)
        if dlg.result:
            self._plots[choice] = dlg.result
            self._rebuild_plots()
            self._notify_change()

    def _del_plot(self):
        if not self._plots:
            return
        if len(self._plots) <= 1:
            messagebox.showinfo('提示', '至少保留一个图表', parent=self)
            return
        names = [f'{i+1}. {p.get("title", "未命名")}' for i, p in enumerate(self._plots)]
        choice = self._pick_plot_dialog(names, title='删除图表 - 选择')
        if choice is None or not (0 <= choice < len(self._plots)):
            return
        if not messagebox.askyesno('确认',
                f'删除图表「{self._plots[choice].get("title", "未命名")}」？',
                parent=self.winfo_toplevel()):
            return
        del self._plots[choice]
        self._rebuild_plots()
        self._notify_change()

    def _pick_plot_dialog(self, names: list[str], title: str = '选择图表') -> int | None:
        """弹出一个简单选择对话框，返回选中的索引或 None。"""
        t = self._theme
        dlg = tk.Toplevel(self.winfo_toplevel())
        dlg.title(title)
        dlg.transient(self.winfo_toplevel())
        dlg.resizable(False, False)
        dlg.configure(bg=t['bg'])
        result = {'idx': None}
        f = ttk.Frame(dlg, padding=12)
        f.pack(fill='both', expand=True)
        ttk.Label(f, text='选择图表：', font=_font('bold')).pack(
            anchor='w', pady=(0, 4))
        lb = tk.Listbox(f, selectmode='single', font=_font(),
                        height=min(len(names), 10),
                        bg=t['panel_bg'], fg=t['panel_fg'],
                        selectbackground=t['select_bg'],
                        selectforeground='#ffffff',
                        highlightthickness=0, borderwidth=0)
        lb.pack(fill='both', expand=True)
        for n in names:
            lb.insert('end', n)

        def _ok(_e=None):
            sel = lb.curselection()
            result['idx'] = sel[0] if sel else None
            dlg.destroy()

        def _cancel(_e=None):
            result['idx'] = None
            dlg.destroy()

        btns = ttk.Frame(f)
        btns.pack(pady=(8, 0))
        ttk.Button(btns, text='确定', command=_ok
                   ).pack(side='left', padx=4)
        ttk.Button(btns, text='取消', command=_cancel
                   ).pack(side='left', padx=4)
        lb.bind('<Double-1>', _ok)
        dlg.bind('<Return>', _ok)
        dlg.bind('<Escape>', _cancel)
        dlg.protocol('WM_DELETE_WINDOW', _cancel)
        self.winfo_toplevel().wait_window(dlg)
        return result['idx']

    def _rebuild_data_grid(self):
        for child in self._data_frame.winfo_children():
            child.destroy()
        self._data_labels = {}
        self._warn_indicators = {}
        if not self._vars:
            ttk.Label(self._data_frame, text='（无变量，请先添加）',
                      foreground=self._theme['muted']).pack(pady=40)
            return
        for i, v in enumerate(self._vars):
            row = ttk.Frame(self._data_frame, padding=8)
            row.grid(row=i, column=0, sticky='ew', padx=4, pady=3)
            row.columnconfigure(1, weight=3)
            row.columnconfigure(3, weight=1)

            color_canvas = tk.Canvas(row, width=14, height=14, bg=v['color'],
                      highlightthickness=0)
            color_canvas.grid(row=0, column=0, padx=(0, 6))
            self._warn_indicators[v['name']] = color_canvas

            ttk.Label(row, text=v['name'], font=_font('bold')
                      ).grid(row=0, column=1, sticky='w')
            ttk.Label(row, text=v['regex'],
                      foreground=self._theme['muted']
                      ).grid(row=0, column=2, sticky='w', padx=(8, 0))

            val_var = tk.StringVar(value='—')
            val_entry = ttk.Entry(row, textvariable=val_var, width=20,
                                  state='readonly', font=_mono(),
                                  justify='right')
            val_entry.grid(row=0, column=3, sticky='e', padx=(8, 0))
            self._data_labels[v['name']] = val_var

        self._data_frame.columnconfigure(0, weight=1)

    # ---- 数据喂入 ------------------------------------------------------
    def feed(self, line: str):
        """主线程从 _drain 调用：匹配 regex，提取数值入 buffer。

        时间戳取日志行首的毫秒时间戳；提取失败回退到 time.time()。
        """
        ts = _extract_log_ts(line) or time.time()
        for v in self._vars:
            name = v['name']
            pat = self._compiled.get(name)
            if pat is None:
                continue
            m = pat.search(line)
            if m:
                try:
                    val = float(m.group(1))
                    buf = self._buffers.get(name)
                    if buf is not None:
                        buf.append((ts, val))
                except (ValueError, IndexError):
                    pass

    # ---- 重绘定时器 ----------------------------------------------------
    def _schedule_redraw(self):
        if self._closed:
            return
        # 加载历史日志期间跳过定时重绘（避免对越来越大的 _history 反复重绘导致越来越慢）
        if self._loading:
            self._redraw_id = self.after(self.REDRAW_MS, self._schedule_redraw)
            return
        # 历史模式：数据固定不变，无需定时重绘（仅在数据变更时手动触发）
        # 用长间隔心跳保活，以便切回实时模式或关闭窗口时调度链不断
        if self._history is not None and self._mode == 'curve':
            self._redraw_id = self.after(1000, self._schedule_redraw)
            return
        if self._mode == 'curve':
            # 实时模式：无新数据时跳过重绘，串口空闲时多窗口 CPU 接近 0
            latest = self._latest_data_ts()
            if latest is not None and latest == self._last_drawn_ts:
                self._redraw_id = self.after(self.REDRAW_MS, self._schedule_redraw)
                return
            self._last_drawn_ts = latest if latest is not None else self._last_drawn_ts
            self._draw_curves()
            interval = self.REDRAW_MS
        else:
            self._update_data_text()
            interval = self.DATA_MS
        self._redraw_id = self.after(interval, self._schedule_redraw)

    def _latest_data_ts(self) -> float | None:
        """返回 _buffers 中所有变量的最新 ts（最后追加的点时间戳）。"""
        latest = None
        for buf in self._buffers.values():
            if buf:
                ts = buf[-1][0]
                if latest is None or ts > latest:
                    latest = ts
        return latest

    def _force_redraw(self):
        """外部触发强制重绘：让 _schedule_redraw 下一帧不跳过。"""
        self._last_drawn_ts = 0.0

    def _update_data_text(self):
        now = time.time()
        for name, var in self._data_labels.items():
            pts = self._get_plot_data(name)
            if not pts:
                var.set('—')
                self._set_warn_state(name, False)
                continue
            ts, val = pts[-1]
            # 历史模式（_history 非空）：不显示"旧"标记
            if self._history is not None:
                var.set(f'{val:g}')
            else:
                age = now - ts
                if age > 5:
                    var.set(f'{val:g}  (旧 {age:.1f}s)')
                else:
                    var.set(f'{val:g}')
            # 阈值告警检测
            self._set_warn_state(name, self._is_value_warning(name, val))

    def _is_value_warning(self, name: str, val: float) -> bool:
        """检查变量值是否超出 warn_min/warn_max。"""
        v = next((x for x in self._vars if x['name'] == name), None)
        if v is None:
            return False
        wmin = v.get('warn_min')
        wmax = v.get('warn_max')
        if wmin is not None and val < wmin:
            return True
        if wmax is not None and val > wmax:
            return True
        return False

    def _set_warn_state(self, name: str, warn: bool):
        """切换告警指示色块颜色：超限红，否则恢复变量原色。"""
        canvas = self._warn_indicators.get(name)
        if canvas is None:
            return
        try:
            if warn:
                canvas.configure(bg=self._theme['status_err'])
            else:
                v = next((x for x in self._vars if x['name'] == name), None)
                if v is not None:
                    canvas.configure(bg=v.get('color', self._theme['fg']))
        except Exception:
            pass

    def _draw_curves(self):
        """遍历 self._plots，每个 plot 在自己的 canvas 上绘制。

        分拆到多个 after(0) 任务：每个 plot 一帧，中间让出主线程响应 _drain，
        避免日志显示因 _draw_curves 阻塞而一卡一卡。
        """
        plots = list(self._plots)
        # 总点数 / 状态文本（在所有 plot 绘制完后更新）
        def _draw_one(i):
            if self._closed or i >= len(plots):
                # 全部 plot 画完，更新状态文本
                self._update_status_text()
                return
            plot = plots[i]
            canvas = plot.get('_canvas') or self._canvas
            self._draw_plot(canvas, plot)
            popout = plot.get('_popout_canvas')
            if popout is not None and popout.winfo_exists():
                self._draw_plot(popout, plot)
            # 让出主线程：下一个 plot 在下次事件循环 tick 绘制
            if i + 1 < len(plots):
                self.after(0, lambda: _draw_one(i + 1))
            else:
                self._update_status_text()
        _draw_one(0)

    def _update_status_text(self):
        """更新状态栏点数信息（缓存避免每帧 sum 几十万点）。"""
        if self._history is not None:
            total = sum(len(pts) for pts in self._history.values())
            total_str = f'{total} 点（历史）'
        else:
            total = sum(len(buf) for buf in self._buffers.values())
            total_str = f'{total} 点'
        if total != getattr(self, '_last_total', -1):
            self._last_total = total
            n_plots = len(self._plots)
            self._status_lbl.configure(
                text=f'{total_str} | {len(self._vars)} 变量 | {n_plots} 图表')

    def _get_plot_data(self, name: str) -> list[tuple[float, float]]:
        """返回变量对应的 (ts, val) 列表。
        历史模式优先用 _history（不限大小），否则用 _buffers（maxlen=600）。
        """
        if self._history is not None:
            return self._history.get(name, [])
        buf = self._buffers.get(name)
        return list(buf) if buf else []

    @staticmethod
    def _truncate_text(text: str, max_w: int,
                       font: tkfont.Font) -> str:
        """按像素宽度截断文字并加 '…' 后缀。"""
        if font.measure(text) <= max_w:
            return text
        # 二分查找最大字符数
        lo, hi = 1, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if font.measure(text[:mid] + '…') <= max_w:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo] + '…'

    def _draw_plot(self, c: tk.Canvas, plot: dict):
        c.delete('all')
        w = c.winfo_width()
        h = c.winfo_height()
        if w < 50 or h < 50:
            return

        t = self._theme
        fg = t['fg']
        muted = t['muted']
        border = t['border']

        # 取 plot 包含的变量字典列表
        var_names = plot.get('vars', [])
        var_map = {v['name']: v for v in self._vars}
        plot_vars = [var_map[n] for n in var_names
                     if n in var_map and n in self._buffers]

        title = plot.get('title', '')
        x_label = plot.get('x_label', '')
        y_label = plot.get('y_label', '')

        # 预先计算图例文字最大宽度，动态调整右侧留白
        legend_font = self._legend_font
        legend_pad_col = 12 + 4 + 16  # 色块 12 + 间隙 4 + 文字偏移 16
        legend_max_text = 0
        for v in plot_vars:
            pts = self._get_plot_data(v['name'])
            if pts:
                txt = f'{v["name"]} = {pts[-1][1]:g}'
            else:
                txt = v['name']
            legend_max_text = max(legend_max_text,
                                  legend_font.measure(txt))
        # 至少留 60px，最多不超过窗口一半
        legend_width = legend_pad_col + min(legend_max_text, max(60, w // 3))
        # 图例每行高度（行间距）：>= 18px 避免文字垂直方向挤压
        legend_line_h = max(18, legend_font.metrics('linespace') + 6)

        # 边距：上留 title，下留 x_label，左留 y_label
        pad_l, pad_r, pad_t, pad_b = 64, legend_width + 14, 26, 36
        if title:
            pad_t += 16
        if x_label:
            pad_b += 14
        if y_label:
            pad_l += 14
        plot_w = max(50, w - pad_l - pad_r)
        plot_h = max(30, h - pad_t - pad_b)

        # 时间范围：若用户框选缩放则用 zoom 范围，否则自动
        # 自动范围：t_max = buf 中最新 ts；t_min = max(t_max-WINDOW, 最老 ts)
        # 这样：数据稀疏时填满整个宽度；数据超过窗口时滚动显示最近 WINDOW 秒
        # 历史模式：不限时间窗口，显示全部数据范围
        zoom = plot.get('_zoom')  # (t_min, t_max, y_lo, y_hi) or None
        if zoom:
            t_min, t_max, y_lo, y_hi = zoom
            y_zoomed = True
        else:
            y_zoomed = False
            t_max = 0.0
            t_min_data = None
            for v in plot_vars:
                pts = self._get_plot_data(v['name'])
                if not pts:
                    continue
                if pts[-1][0] > t_max:
                    t_max = pts[-1][0]
                if t_min_data is None or pts[0][0] < t_min_data:
                    t_min_data = pts[0][0]
            if t_max == 0.0:
                t_max = time.time()
            # 历史模式或数据时间跨度 <= WINDOW：显示全部
            if (self._history is not None
                    or (t_min_data is not None
                        and t_max - t_min_data <= self.TIME_WINDOW_S)):
                t_min = t_min_data if t_min_data is not None else t_max - self.TIME_WINDOW_S
            else:
                t_min = t_max - self.TIME_WINDOW_S
            y_lo, y_hi = None, None

        if t_max <= t_min:
            t_max = t_min + 1.0

        # 收集每条曲线在 [t_min, t_max] 内的点 + 左侧锚点
        # 锚点让曲线从左边缘开始延伸，而不是从第一个窗口内点开始
        series: list[tuple[dict, list[tuple[float, float]]]] = []
        for v in plot_vars:
            buf = self._get_plot_data(v['name'])
            if not buf:
                continue
            pts: list[tuple[float, float]] = []
            anchor = None
            for ts, val in buf:
                if ts < t_min:
                    anchor = (ts, val)
                    continue
                if ts > t_max:
                    # zoom 模式下裁剪 t_max 以外的点
                    break
                if anchor is not None and not pts:
                    pts.append(anchor)
                    anchor = None
                pts.append((ts, val))
            # 若所有点都在 t_min 之前（数据过老），至少保留最近一个作锚点
            if not pts and anchor is not None:
                pts.append(anchor)
            # 若第一个点时间 > t_min（zoom t_min 在数据之前），插入虚拟锚点
            # 确保曲线从左边缘延伸，而非从第一个数据点开始留空白
            if pts and pts[0][0] > t_min:
                pts.insert(0, (t_min, pts[0][1]))
            # 降采样：点数超过 plot 像素宽度的 1.5 倍时按步长抽样
            # 减少 create_line 顶点数，降低 _draw_plot 主线程占用
            max_pts = max(200, int(plot_w * 1.5))
            if len(pts) > max_pts:
                step = len(pts) / max_pts
                sampled = [pts[0]]
                idx = step
                while idx < len(pts) - 1:
                    sampled.append(pts[int(idx)])
                    idx += step
                sampled.append(pts[-1])
                pts = sampled
            series.append((v, pts))

        # 自动 Y 范围：仅从窗口内（含锚点）数据计算
        if not y_zoomed:
            for _v, pts in series:
                for _ts, val in pts:
                    if y_lo is None or val < y_lo:
                        y_lo = val
                    if y_hi is None or val > y_hi:
                        y_hi = val
            for v in plot_vars:
                if v.get('y_min') is not None:
                    y_lo = v['y_min'] if y_lo is None else min(y_lo, v['y_min'])
                if v.get('y_max') is not None:
                    y_hi = v['y_max'] if y_hi is None else max(y_hi, v['y_max'])
            if y_lo is None:
                y_lo, y_hi = 0.0, 1.0
            if y_hi == y_lo:
                y_hi = y_lo + 1.0
            # 缓存自动 y 范围，供右键"重置缩放"使用
            plot['_auto_y'] = (y_lo, y_hi)

        # 标题
        if title:
            c.create_text(pad_l + plot_w / 2, 6, text=title,
                          fill=fg, anchor='n', font=_font('bold'))

        # Y 轴标签（左侧旋转用文字横向写在左侧上方）
        if y_label:
            c.create_text(pad_l - 28, pad_t + plot_h / 2, text=y_label,
                          fill=muted, anchor='n',
                          font=('DejaVu Sans', 9))

        n_grid_y = 5
        for i in range(n_grid_y + 1):
            y = pad_t + plot_h * i / n_grid_y
            c.create_line(pad_l, y, pad_l + plot_w, y,
                          fill=border, dash=(2, 3))
            val = y_hi - (y_hi - y_lo) * i / n_grid_y
            c.create_text(pad_l - 6, y, text=f'{val:g}',
                          fill=muted, anchor='e',
                          font=('DejaVu Sans', 9))

        # X 轴标签：日志时间戳 → 实际时间（自适应格式）
        n_grid_x = 6
        span = t_max - t_min
        # 格式选择：跨度 > 1 天 → 显示日期 + 时分；> 1 小时 → 时分秒；否则 → 秒.毫秒
        if span > 86400:
            fmt = '%m-%d %H:%M'
        elif span > 3600:
            fmt = '%H:%M:%S'
        elif span > 60:
            fmt = '%H:%M:%S'
        else:
            fmt = '%H:%M:%S'
        for i in range(n_grid_x + 1):
            x = pad_l + plot_w * i / n_grid_x
            t_at = t_min + span * i / n_grid_x
            try:
                label = _dt.datetime.fromtimestamp(t_at).strftime(fmt)
            except (ValueError, OSError):
                label = '--:--:--'
            c.create_text(x, pad_t + plot_h + 4, text=label,
                          fill=muted, anchor='n',
                          font=('DejaVu Sans', 9))

        c.create_rectangle(pad_l, pad_t, pad_l + plot_w, pad_t + plot_h,
                           outline=border, width=1)

        def _t2x(ts):
            return pad_l + plot_w * (ts - t_min) / (t_max - t_min)

        def _v2y(val):
            return pad_t + plot_h * (y_hi - val) / (y_hi - y_lo)

        for v, pts in series:
            if len(pts) >= 2:
                coords = []
                for ts, val in pts:
                    coords.extend([_t2x(ts), _v2y(val)])
                c.create_line(*coords, fill=v['color'], width=1.5)
            if pts:
                lx = _t2x(pts[-1][0])
                ly = _v2y(pts[-1][1])
                c.create_oval(lx - 3, ly - 3, lx + 3, ly + 3,
                              fill=v['color'], outline='')

        # X 轴标签（绘图区下方）
        if x_label:
            c.create_text(pad_l + plot_w / 2, pad_t + plot_h + 22,
                          text=x_label, fill=muted, anchor='n',
                          font=('DejaVu Sans', 9))

        # 图例
        legend_x = pad_l + plot_w + 10
        # 图例可用文字宽度（超过则字符级截断 + 省略号，避免文字延伸到画布外）
        legend_text_w = max(40, legend_width - legend_pad_col)
        # 行数过多时缩小行高（最多缩到 14px），保证所有行可见
        n_lines = len(plot_vars)
        line_h = legend_line_h
        if n_lines * line_h > plot_h:
            line_h = max(14, plot_h // max(1, n_lines))
        # 顶部居中起始 y，让图例在绘图区纵向居中
        if n_lines * line_h < plot_h:
            start_y = pad_t + (plot_h - n_lines * line_h) // 2
        else:
            start_y = pad_t + 2
        for i, v in enumerate(plot_vars):
            ly = start_y + i * line_h
            # 防止最后一行色块底部超出绘图区
            if ly + 12 > pad_t + plot_h:
                break
            c.create_rectangle(legend_x, ly, legend_x + 12, ly + 12,
                                fill=v['color'], outline='')
            label = v['name']
            buf = self._get_plot_data(v['name'])
            if buf:
                label = f'{v["name"]} = {buf[-1][1]:g}'
            # 按可用宽度截断文字，避免延伸到画布外被相邻图例覆盖
            if legend_font.measure(label) > legend_text_w:
                label = self._truncate_text(label, legend_text_w,
                                            legend_font)
            c.create_text(legend_x + 16, ly + 6, text=label,
                          fill=fg, anchor='w',
                          font=legend_font)

        # 缓存绘图区几何，供鼠标框选缩放时把像素坐标换算回数据坐标
        plot['_geom'] = (pad_l, pad_t, plot_w, plot_h, t_min, t_max,
                         y_lo, y_hi)
        # 拖动选框（如果存在）
        if plot.get('_zoom_rect'):
            x0, y0, x1, y1 = plot['_zoom_rect']
            c.create_rectangle(x0, y0, x1, y1,
                               outline=t.get('accent', fg),
                               dash=(3, 3), tag='zoom_rect')


    # ---- 主题 ----------------------------------------------------------
    def apply_theme(self, theme: dict):
        self._theme = theme
        try:
            self.configure(bg=theme['bg'])
            # 重主题所有 plot canvas
            for canvas in self._plot_canvases:
                try:
                    canvas.configure(bg=theme['panel_bg'])
                except Exception:
                    pass
            if hasattr(self, '_canvas') and self._canvas is not None:
                try:
                    self._canvas.configure(bg=theme['panel_bg'])
                except Exception:
                    pass
            self._rebuild_data_grid()
        except Exception:
            pass

    def update_theme(self):
        try:
            t = themes.THEMES[themes.resolve(themes.load_pref())]
            self.apply_theme(t)
        except Exception:
            pass

    # ---- 关闭 / 清理 ---------------------------------------------------
    def _stop(self):
        """停止定时器，清空 buffer。不销毁控件（由父容器决定）。"""
        self._closed = True
        if self._redraw_id is not None:
            try:
                self.after_cancel(self._redraw_id)
            except Exception:
                pass
            self._redraw_id = None
        # 关闭所有 plot 单独显示窗口
        for plot in self._plots:
            top = plot.pop('_popout_toplevel', None)
            plot.pop('_popout_canvas', None)
            if top is not None:
                try:
                    top.destroy()
                except Exception:
                    pass
        for buf in self._buffers.values():
            buf.clear()
        self._buffers.clear()
        self._compiled.clear()
        self._history = None

    def destroy(self):
        self._stop()
        super().destroy()

    @property
    def variables(self):
        return list(self._vars)

    @property
    def plots(self):
        return [self._normalize_plot(p) for p in self._plots]

    @staticmethod
    def _normalize_plot(p: dict) -> dict:
        return {
            'title': p.get('title', '数据曲线'),
            'x_label': p.get('x_label', '时间 (s)'),
            'y_label': p.get('y_label', '值'),
            'vars': list(p.get('vars', [])),
        }


class MonitorWindow(tk.Toplevel):
    """独立监视窗口：Toplevel 包装 MonitorPanel。

    子类覆盖 PANEL_CLS 提供特定 Panel（含 DEFAULT_VARS）。
    默认占半屏大小，工具栏有全屏/还原按钮。
    """

    PANEL_CLS = MonitorPanel
    TITLE = '数据监视'
    GEOMETRY = ''   # 空 = 动态计算半屏

    def __init__(self, parent, variables=None, on_change=None, plots=None):
        super().__init__(parent)
        self.title(self.TITLE)
        self._is_fullscreen = False
        # 动态计算半屏大小
        if not self.GEOMETRY:
            try:
                sw = parent.winfo_screenwidth()
                sh = parent.winfo_screenheight()
                half_w = sw // 2
                half_h = sh // 2
                self.geometry(f'{half_w}x{half_h}')
            except Exception:
                self.geometry('960x640')
        else:
            self.geometry(self.GEOMETRY)
        self.minsize(720, 480)
        self.resizable(True, True)
        # 不调用 transient：GNOME/Mutter 把 transient 窗口当对话框处理，
        # 会禁用最大化按钮和拖到顶部的最大化手势。改用 lift+focus 保持层级。
        try:
            self.lift(parent)
            self.focus_force()
        except Exception:
            pass

        self.panel = self.PANEL_CLS(self, variables=variables,
                                      on_change=on_change, plots=plots)
        self.panel.pack(fill='both', expand=True)

        self.protocol('WM_DELETE_WINDOW', self._on_close)
        # Escape 退出全屏
        self.bind('<Escape>', lambda _e: self._exit_fullscreen())

    @property
    def variables(self):
        return self.panel.variables

    @property
    def plots(self):
        return self.panel.plots

    def feed(self, line):
        self.panel.feed(line)

    def _exit_fullscreen(self):
        """退出全屏模式。"""
        if self._is_fullscreen:
            self.attributes('-fullscreen', False)
            self._is_fullscreen = False
            try:
                self.panel._fullscreen_btn.configure(text='全屏')
            except Exception:
                pass

    def update_theme(self):
        try:
            self.panel.update_theme()
            t = themes.THEMES[themes.resolve(themes.load_pref())]
            self.configure(bg=t['bg'])
        except Exception:
            pass

    def _on_close(self):
        self.panel._stop()
        self.destroy()


def _parse_int_tokens(tokens: list[str]) -> list[int]:
    """将字符串 token 列表解析为整数列表。

    先按十进制，失败再按十六进制（C 代码 faultCode 用 %x 打印）。
    跳过无法解析的 token。
    """
    vals = []
    for t in tokens:
        try:
            vals.append(int(t))
        except ValueError:
            try:
                vals.append(int(t, 16))
            except ValueError:
                continue
    return vals


def _parse_pipe_hash(rest: str) -> list[list[list[int]]] | None:
    """解析 '0 0 0|0 0 0#0 0 0' → [[0,0,0],[0,0,0],[0,0,0]]（1 个 block）。

    按 | 和 # 分割为组，每组按空格分割为整数。空组被丢弃。
    """
    rest = rest.split('\n')[0].split('\r')[0].strip()
    parts = re.split(r'[|#]', rest)
    groups = []
    for p in parts:
        vals = _parse_int_tokens(p.split())
        if vals:
            groups.append(vals)
    return [groups] if groups else None


class FillCheck(ttk.Frame):
    """扁平色块 Checkbutton：选中=填充色块，未选=空心方框（无对勾）。

    比 ttk.Checkbutton 视觉更接近 VSCode 复选框。
    颜色由 apply_theme 更新，默认浅色主题。
    """

    def __init__(self, parent, text='', variable=None, command=None,
                 **kw):
        super().__init__(parent)
        self._var = variable if variable is not None else tk.BooleanVar()
        self._cmd = command
        self._theme = _default_theme()
        self._canvas = tk.Canvas(self, width=14, height=14,
                                 highlightthickness=0, borderwidth=0,
                                 bg=self._theme['bg'])
        self._canvas.pack(side='left', padx=(0, 6))
        self._label = ttk.Label(self, text=text)
        self._label.pack(side='left')
        # 整个 frame 响应点击
        self.bind('<Button-1>', lambda _e: self._toggle())
        self._canvas.bind('<Button-1>', lambda _e: self._toggle())
        self._label.bind('<Button-1>', lambda _e: self._toggle())
        try:
            self._var.trace_add('write', lambda *_: self._redraw())
        except Exception:
            pass
        self._redraw()

    def _is_on(self) -> bool:
        try:
            return bool(self._var.get())
        except Exception:
            return False

    def _toggle(self):
        self._var.set(not self._is_on())
        if self._cmd:
            try:
                self._cmd()
            except Exception:
                pass

    def _redraw(self):
        c = self._canvas
        c.delete('all')
        t = self._theme
        if self._is_on():
            c.create_rectangle(1, 1, 13, 13, fill=t.get('accent', '#0e639c'),
                               outline='', width=0)
        else:
            c.create_rectangle(1, 1, 13, 13, fill=t.get('panel_bg', '#252526'),
                               outline=t.get('border', '#3c3c3c'), width=1)

    def apply_theme(self, theme: dict):
        self._theme = theme
        try:
            self._canvas.configure(bg=theme['bg'])
        except Exception:
            pass
        self._redraw()


def _build_motor_default_vars() -> list[dict]:
    """构造电机监视默认变量注册表（3wd1t_1 9 个 + 3wd1t_2 28 个 +
    STD_DEBUG MA/MB 各 14 个 = 65 个）。

    变量名直接取自 C 代码字段名，STD_DEBUG 加 MA_/MB_ 前缀区分块。
    """
    out: list[dict] = []
    palette = _PALETTE
    idx = [0]

    def next_color() -> str:
        c = palette[idx[0] % len(palette)]
        idx[0] += 1
        return c

    def add(name, label, log_tag, block_idx, group_idx, pos_in_group,
            y_min=None, y_max=None):
        out.append({
            'name': name, 'label': label, 'log_tag': log_tag,
            'block_idx': block_idx, 'group_idx': group_idx,
            'pos_in_group': pos_in_group, 'color': next_color(),
            'y_min': y_min, 'y_max': y_max,
        })

    # ---- 3wd1t_1：3 组 × 3 = 9 个 ----
    g3wd1t_1 = [
        ['Vx', 'Vx_Fbk', 'Vx_Msf'],
        ['Wx', 'Wx_Fbk', 'Wx_Imu'],
        ['ImuPidOut', 'Vx_Input', 'Wx_Input'],
    ]
    for gi, group in enumerate(g3wd1t_1):
        for pi, name in enumerate(group):
            add(name, name, '3wd1t_1', 0, gi, pi)

    # ---- 3wd1t_2：8 组 = 28 个 ----
    g3wd1t_2 = [
        ['M1_En', 'M2_En', 'M3_En', 'T1_En'],
        ['M1_Cmd_Spd', 'M1_Fbk_Spd', 'M1_Cmd_Iq', 'M1_Fbk_Iq'],
        ['M2_Cmd_Spd', 'M2_Fbk_Spd', 'M2_Cmd_Iq', 'M2_Fbk_Iq'],
        ['M3_Cmd_Spd', 'M3_Fbk_Spd', 'M3_Cmd_Iq', 'M3_Fbk_Iq'],
        ['T1_Cmd_Angle', 'T1_Fbk_Angle', 'T1_Fbk_Spd', 'T1_Fbk_Iq'],
        ['M3_Temp', 'T1_Temp'],
        ['BackMotorAssitFlag', 'ParkingFlag', 'ModelStatus'],
        ['TCS_Iq_Limit_M1', 'TCS_Iq_Limit_M2', 'TCS_Iq_Limit_M3'],
    ]
    for gi, group in enumerate(g3wd1t_2):
        for pi, name in enumerate(group):
            add(name, name, '3wd1t_2', 0, gi, pi)

    # ---- STD_DEBUG：L30~L39 各 2 块（MA/MB）× 9 组 × 14 个/块 ----
    # 日志格式：L30 STD_DEBUG:[...][...]  → block 0=MA, block 1=MB
    # 变量名格式：L30_MA_state / L30_MB_temp 等
    std_groups = [
        ['state'],                       # group 0
        ['faultCode'],                   # group 1 (hex)
        ['tgt_ang', 'out_ang'],          # group 2
        ['tgt_spd', 'outavg_spd'],       # group 3
        ['iq_ref', 'iq_fbk', 'id_fbk'],  # group 4
        ['uq', 'ud'],                    # group 5
        ['torque'],                       # group 6
        ['in_pwr', 'out_pwr'],           # group 7
        ['temp'],                         # group 8
    ]
    for vcu_i in range(10):  # L30~L39 → VCU1~VCU10
        l_num = 30 + vcu_i
        l_tag = f'L{l_num}_STD_DEBUG'
        for block_idx, prefix in enumerate(['MA', 'MB']):
            for gi, group in enumerate(std_groups):
                for pi, name in enumerate(group):
                    full = f'L{l_num}_{prefix}_{name}'
                    label = f'VCU{vcu_i+1} {prefix} {name}'
                    add(full, label, l_tag, block_idx, gi, pi)

    return out


class StructuredMonitorPanel(MonitorPanel):
    """结构化日志解析面板基类（BMS / 电机共用）。
    变量字段：name + label + log_tag + block_idx + group_idx +
              pos_in_group + color + y_min/y_max。
    用户只在对话框填变量名，log_tag / block_idx / group_idx / pos_in_group
    由子类的 _REGISTRY 注册表自动查表填入。

    解析返回 list[list[list[int]]]（blocks × groups × positions）：
      - BMS/BAT：1 个 block，每个 block 内若干方括号分组
      - 3wd1t_1/3wd1t_2：1 个 block，按 | 与 # 分组
      - STD_DEBUG：2 个 block（MA/MB），每个 block 内按 | 分组
    """

    # 子类覆盖：tag → 行内定位标记（含前后字符保证唯一）
    _TAG_MARKERS: dict[str, str] = {}
    # 子类覆盖：(tag, block_idx) → 数据网格分节标题
    _SECTION_LABELS: dict[tuple[str, int], str] = {}

    @classmethod
    def _parse_tagged_line(cls, line: str, tag: str):
        """子类实现：从 line 中按 tag 解析出 blocks × groups × positions。"""
        raise NotImplementedError

    @classmethod
    def _section_label(cls, tag: str, block_idx: int) -> str:
        key = (tag, block_idx)
        if key in cls._SECTION_LABELS:
            return cls._SECTION_LABELS[key]
        if block_idx == 0:
            return f'{tag} 数据'
        return f'{tag} 块{block_idx + 1}'

    @classmethod
    def _build_registry(cls) -> dict[str, dict]:
        return {v['name']: v for v in cls.DEFAULT_VARS}

    # ---- 重写父类方法 --------------------------------------------------
    def _rebuild_buffers(self):
        """结构化变量不用 regex，只重建 deque buffer。"""
        new_buf = {}
        for v in self._vars:
            name = v['name']
            if name in self._buffers:
                new_buf[name] = self._buffers[name]
            else:
                new_buf[name] = deque(maxlen=self.MAX_POINTS)
        self._buffers = new_buf
        self._compiled = {}

    def feed(self, line: str):
        """按 log_tag 解析 blocks × groups，按 block_idx/group_idx/pos_in_group 取值。

        时间戳取日志行首的毫秒时间戳；提取失败回退到 time.time()。
        """
        ts = _extract_log_ts(line) or time.time()
        parsed: dict[str, list | None] = {}
        for v in self._vars:
            val = self._extract_value_parsed(line, v, parsed)
            if val is not None:
                buf = self._buffers.get(v['name'])
                if buf is not None:
                    buf.append((ts, val))

    def _extract_value(self, line: str, v: dict) -> float | None:
        """覆盖：使用结构化解析（log_tag / block / group / pos）。"""
        tag = v.get('log_tag')
        if not tag:
            return None
        blocks = self._parse_tagged_line(line, tag)
        if not blocks:
            return None
        bi = v.get('block_idx', 0)
        gi = v.get('group_idx', 0)
        pi = v.get('pos_in_group', 0)
        try:
            if bi < len(blocks) and gi < len(blocks[bi]) and pi < len(blocks[bi][gi]):
                return float(blocks[bi][gi][pi])
        except (ValueError, IndexError):
            return None
        return None

    def _extract_value_parsed(self, line: str, v: dict,
                              parsed: dict) -> float | None:
        """feed 用：复用同一行的 parsed 缓存，避免每变量重解析。"""
        tag = v.get('log_tag')
        if not tag:
            return None
        if tag not in parsed:
            parsed[tag] = self._parse_tagged_line(line, tag)
        blocks = parsed.get(tag)
        if not blocks:
            return None
        bi = v.get('block_idx', 0)
        gi = v.get('group_idx', 0)
        pi = v.get('pos_in_group', 0)
        try:
            if bi < len(blocks) and gi < len(blocks[bi]) and pi < len(blocks[bi][gi]):
                return float(blocks[bi][gi][pi])
        except (ValueError, IndexError):
            return None
        return None

    def _refresh_var_list(self):
        """左侧 Treeview 显示：中文说明 / 标签 / 块.组.位。"""
        for iid in self._var_tree.get_children():
            self._var_tree.delete(iid)
        self._var_tree.configure(columns=('label', 'tag', 'pos'))
        self._var_tree.heading('label', text='说明')
        self._var_tree.heading('tag', text='标签')
        self._var_tree.heading('pos', text='位置')
        self._var_tree.column('label', width=180, stretch=True)
        self._var_tree.column('tag', width=70, anchor='center', stretch=False)
        self._var_tree.column('pos', width=80, anchor='center', stretch=False)

        for v in self._vars:
            label = v.get('label', '') or v['name']
            tag = v.get('log_tag', '')
            pos = f'{v.get("block_idx", 0)}.{v.get("group_idx", 0)}.{v.get("pos_in_group", 0)}'
            self._var_tree.insert('', 'end', iid=v['name'],
                                  values=(label, tag, pos))

    def _add_var(self):
        used = {v['name'] for v in self._vars}
        plot_names = [p.get('title', f'图表{i+1}')
                      for i, p in enumerate(self._plots)]
        dlg = _BmsVarDialog(self.winfo_toplevel(), used_names=used,
                            registry=self._REGISTRY or self._build_registry(),
                            plot_names=plot_names,
                            theme=self._theme)
        self.winfo_toplevel().wait_window(dlg)
        if dlg.result:
            self._vars.append(dlg.result)
            self._add_var_to_named_plot(
                dlg.result['name'], dlg.result.get('plot_name', ''))
            self._rebuild_buffers()
            self._refresh_var_list()
            self._rebuild_data_grid()
            self._rebuild_plots()
            self._notify_change()

    def _edit_var(self):
        sel = self._var_tree.selection()
        if not sel:
            return
        old_name = sel[0]
        idx = next((i for i, v in enumerate(self._vars)
                    if v['name'] == old_name), None)
        if idx is None:
            return
        used = {v['name'] for v in self._vars}
        used.discard(old_name)
        # 查变量当前所在图表，作为对话框初始值
        cur_plot = ''
        for p in self._plots:
            if old_name in p.get('vars', []):
                cur_plot = p.get('title', '')
                break
        plot_names = [p.get('title', f'图表{i+1}')
                      for i, p in enumerate(self._plots)]
        dlg = _BmsVarDialog(self.winfo_toplevel(), initial=self._vars[idx],
                           title='编辑变量', used_names=used,
                           registry=self._REGISTRY or self._build_registry(),
                           plot_names=plot_names, initial_plot=cur_plot,
                           theme=self._theme)
        self.winfo_toplevel().wait_window(dlg)
        if dlg.result:
            new_name = dlg.result['name']
            if new_name != old_name and old_name in self._buffers:
                self._buffers[new_name] = self._buffers.pop(old_name)
            self._vars[idx] = dlg.result
            if new_name != old_name:
                self._sync_plots_var_renamed(old_name, new_name)
            self._move_var_to_named_plot(
                new_name, dlg.result.get('plot_name', ''))
            self._rebuild_buffers()
            self._refresh_var_list()
            self._rebuild_data_grid()
            self._rebuild_plots()
            self._notify_change()

    def _rebuild_data_grid(self):
        """右侧数据网格：按 (log_tag, block_idx) 分节，每行=色块+说明+变量名+数值。"""
        for child in self._data_frame.winfo_children():
            child.destroy()
        self._data_labels = {}
        self._warn_indicators = {}
        if not self._vars:
            ttk.Label(self._data_frame, text='（无变量，请先添加）',
                      foreground=self._theme['muted']).pack(pady=40)
            return

        # 按 (log_tag, block_idx) 分组，保持插入顺序
        sections: dict[tuple, list[dict]] = {}
        order: list[tuple] = []
        for v in self._vars:
            key = (v.get('log_tag', ''), v.get('block_idx', 0))
            if key not in sections:
                sections[key] = []
                order.append(key)
            sections[key].append(v)

        row = 0
        for key in order:
            tag, block_idx = key
            header = self._section_label(tag, block_idx)
            ttk.Label(self._data_frame, text=header, font=_font('bold')
                      ).grid(row=row, column=0, sticky='w', padx=8,
                             pady=(10, 4))
            row += 1
            for v in sections[key]:
                r = ttk.Frame(self._data_frame, padding=6)
                r.grid(row=row, column=0, sticky='ew', padx=4)
                r.columnconfigure(1, weight=3)
                r.columnconfigure(3, weight=1)

                color_canvas = tk.Canvas(r, width=12, height=12,
                                          bg=v['color'],
                                          highlightthickness=0)
                color_canvas.grid(row=0, column=0, padx=(0, 6))
                self._warn_indicators[v['name']] = color_canvas

                ttk.Label(r, text=v.get('label', '') or v['name'],
                          font=_font('bold')
                          ).grid(row=0, column=1, sticky='w')
                ttk.Label(r, text=v['name'],
                          foreground=self._theme['muted']
                          ).grid(row=0, column=2, sticky='w', padx=(8, 0))

                val_var = tk.StringVar(value='—')
                ttk.Entry(r, textvariable=val_var, width=18,
                          state='readonly', font=_mono(),
                          justify='right'
                          ).grid(row=0, column=3, sticky='e', padx=(8, 0))
                self._data_labels[v['name']] = val_var
                row += 1

        self._data_frame.columnconfigure(0, weight=1)


class MotorMonitorPanel(StructuredMonitorPanel):
    """电机数据监视面板（可嵌入）。

    日志格式（设备 C 代码 LOG_TS/VCU_MON_LOG 宏）：
      1783996615491 I model 3wd1t_1 0 0 0|0 0 0#0 0 0
      1783995846145 I model 3wd1t_2 1 1 0 0|0 0 1000 0#...#0 0 0
      1783995845972 I L30 STD_DEBUG:[6|0|0 0|...|64][6|0|0 0|...|65]
      1784012130441 I L31 STD_DEBUG:[0|0|...][5|0|...]

    解析：
      - 3wd1t_1 / 3wd1t_2：1 个 block，按 | 与 # 分组
      - Lxx STD_DEBUG：2 个 block（MA/MB），每个 [...] 内按 | 分组
        L30~L39 分别对应 VCU1~VCU10
    faultCode 用 %x 打印（hex），解析时按十进制失败再按 hex。
    """

    _BRACKET_RE = re.compile(r'\[([^\]]*)\]')
    # 匹配 L30~L39 STD_DEBUG 行，log_tag 格式为 L30_STD_DEBUG
    _STD_DEBUG_MARKER_RE = re.compile(r'\b(L\d+)\s+STD_DEBUG:')
    _TAG_MARKERS = {
        '3wd1t_1': ' model 3wd1t_1 ',
        '3wd1t_2': ' model 3wd1t_2 ',
    }
    # STD_DEBUG 的 section label 动态生成（_section_label 方法覆盖）
    _SECTION_LABELS = {
        ('3wd1t_1', 0): '3wd1t_1 数据',
        ('3wd1t_2', 0): '3wd1t_2 数据',
    }

    DEFAULT_VARS = _build_motor_default_vars()
    _REGISTRY = {v['name']: v for v in DEFAULT_VARS}

    @classmethod
    def _section_label(cls, tag: str, block_idx: int) -> str:
        """Lxx_STD_DEBUG tag 的分节标题：VCUx(Lxx) MA/MB。"""
        if tag.endswith('_STD_DEBUG'):
            l_num = tag.split('_')[0]  # 如 'L30'
            # L30→VCU1, L31→VCU2, ... L39→VCU10
            vcu_num = int(l_num[1:]) - 30 + 1
            block_name = 'MA' if block_idx == 0 else 'MB'
            return f'VCU{vcu_num}({l_num}) {block_name}'
        return super()._section_label(tag, block_idx)

    @classmethod
    def _parse_tagged_line(cls, line: str, tag: str):
        if tag in ('3wd1t_1', '3wd1t_2'):
            marker = cls._TAG_MARKERS.get(tag)
            if not marker:
                return None
            idx = line.find(marker)
            if idx < 0:
                return None
            rest = line[idx + len(marker):]
            return _parse_pipe_hash(rest)
        # Lxx_STD_DEBUG tag：从行中匹配 Lxx STD_DEBUG: 提取数据
        # tag 格式如 'L30_STD_DEBUG'，需要从 tag 提取 Lxx 编号
        m = cls._STD_DEBUG_MARKER_RE.search(line)
        if not m:
            return None
        # 确认行中的 Lxx 编号与 tag 匹配
        l_num_in_line = m.group(1)  # 如 'L30'
        l_num_in_tag = tag.split('_')[0]  # 如 'L30'
        if l_num_in_line != l_num_in_tag:
            return None
        rest = line[m.end():]
        return cls._parse_std_debug(rest)

    # STD_DEBUG 每组的字段数，用于扁平→分组映射（兼容 9 组和 14 平铺格式）
    _STD_DEBUG_GROUP_SIZES = [1, 1, 2, 2, 3, 2, 1, 2, 1]

    @classmethod
    def _parse_std_debug(cls, rest: str):
        """解析 STD_DEBUG 数据块，兼容两种格式：
        - 9 组格式：[state|faultCode|tgt_ang out_ang|...]（| 分组，空格分值）
        - 14 平铺格式：[state|faultCode|tgt_ang|out_ang|...]（| 分隔每个字段）
        先扁平化再按 _STD_DEBUG_GROUP_SIZES 逻辑分组，两种格式结果一致。
        """
        blocks = []
        for m in cls._BRACKET_RE.finditer(rest):
            block_str = m.group(1)
            # 步骤 1：扁平化——所有 | 段的值拼成一个列表
            flat: list[int] = []
            for sp in block_str.split('|'):
                flat.extend(_parse_int_tokens(sp.split()))
            # 步骤 2：按逻辑分组重新切分
            groups: list[list[int]] = []
            offset = 0
            for size in cls._STD_DEBUG_GROUP_SIZES:
                groups.append(flat[offset:offset + size])
                offset += size
            if groups:
                blocks.append(groups)
        return blocks if blocks else None


class MotorMonitorWindow(MonitorWindow):
    """电机数据监视独立窗口。"""

    PANEL_CLS = MotorMonitorPanel
    TITLE = '电机数据监视'


class BmsMonitorPanel(StructuredMonitorPanel):
    """BMS 数据监视面板（可嵌入）。

    日志行格式（来自设备 C 代码 LOG_TS 宏）：
      1783993778690 F BMS [3 4 4][2500 0 24888 2663][2864 0 444][1360 0][1000 932 1000 1000][4523 4720]
      1783993778690 F BAT [24460 24750][2506][33 33 32][4144 4136 4136 4137 4139 4149][5 2]

    1 个 block（block_idx=0），按方括号分组。
    """

    _BRACKET_RE = re.compile(r'\[([^\]]*)\]')
    _TAG_MARKERS = {
        'BMS': ' BMS ',
        'BAT': ' BAT ',
    }
    _SECTION_LABELS = {
        ('BMS', 0): 'BMS 数据',
        ('BAT', 0): 'BAT 数据',
    }

    DEFAULT_VARS = [
        # ---- BMS line ----
        # Group 0：整车状态、充电检测、充电开关
        {'name': 'state', 'label': '整车状态', 'log_tag': 'BMS',
         'group_idx': 0, 'pos_in_group': 0, 'color': '#569cd6'},
        {'name': 'charge_detect', 'label': '充电状态检测', 'log_tag': 'BMS',
         'group_idx': 0, 'pos_in_group': 1, 'color': '#ce9178'},
        {'name': 'charge_sw', 'label': '充电开关', 'log_tag': 'BMS',
         'group_idx': 0, 'pos_in_group': 2, 'color': '#4ec9b0'},
        # Group 1：设置充电电流/电压、母线电压/电流
        {'name': 'charge_i_set', 'label': '设置充电电流', 'log_tag': 'BMS',
         'group_idx': 1, 'pos_in_group': 0, 'color': '#dcdcaa'},
        {'name': 'charge_v_set', 'label': '设置充电电压', 'log_tag': 'BMS',
         'group_idx': 1, 'pos_in_group': 1, 'color': '#c586c0'},
        {'name': 'board_v', 'label': '母线电压', 'log_tag': 'BMS',
         'group_idx': 1, 'pos_in_group': 2, 'color': '#f48771'},
        {'name': 'board_i', 'label': '母线电流', 'log_tag': 'BMS',
         'group_idx': 1, 'pos_in_group': 3, 'color': '#6a9955'},
        # Group 2：STU电压、母线放电电流、充电温度
        {'name': 'dc_in_voltage', 'label': 'STU电压', 'log_tag': 'BMS',
         'group_idx': 2, 'pos_in_group': 0, 'color': '#9cdcfe'},
        {'name': 'discharge_i', 'label': '母线放电电流', 'log_tag': 'BMS',
         'group_idx': 2, 'pos_in_group': 1, 'color': '#d16969'},
        {'name': 'dc_in_temp', 'label': '充电温度', 'log_tag': 'BMS',
         'group_idx': 2, 'pos_in_group': 2, 'color': '#b5cea8'},
        # Group 3：电流环/电压环占空比
        {'name': 'LK_pwm_set', 'label': '电流环占空比', 'log_tag': 'BMS',
         'group_idx': 3, 'pos_in_group': 0, 'color': '#569cd6'},
        {'name': 'LKV_pwm_set', 'label': '电压环占空比', 'log_tag': 'BMS',
         'group_idx': 3, 'pos_in_group': 1, 'color': '#ce9178'},
        # Group 4：SoC / SoH
        {'name': 'batSOC', 'label': '显示SoC', 'log_tag': 'BMS',
         'group_idx': 4, 'pos_in_group': 0, 'color': '#4ec9b0',
         'y_min': 0, 'y_max': 100},
        {'name': 'realSOC', 'label': '实际SoC', 'log_tag': 'BMS',
         'group_idx': 4, 'pos_in_group': 1, 'color': '#dcdcaa',
         'y_min': 0, 'y_max': 100},
        {'name': 'batSOH', 'label': '显示健康度', 'log_tag': 'BMS',
         'group_idx': 4, 'pos_in_group': 2, 'color': '#c586c0',
         'y_min': 0, 'y_max': 100},
        {'name': 'userSOH', 'label': '真实健康度', 'log_tag': 'BMS',
         'group_idx': 4, 'pos_in_group': 3, 'color': '#f48771',
         'y_min': 0, 'y_max': 100},
        # Group 5：剩余容量
        {'name': 'bat_cap_cc', 'label': '剩余容量Ah', 'log_tag': 'BMS',
         'group_idx': 5, 'pos_in_group': 0, 'color': '#6a9955'},
        {'name': 'bat_cap_vol', 'label': '剩余容量V', 'log_tag': 'BMS',
         'group_idx': 5, 'pos_in_group': 1, 'color': '#9cdcfe'},
        # ---- BAT line ----
        # Group 0：包电压、电压
        {'name': 'Pack_Voltage', 'label': '包电压', 'log_tag': 'BAT',
         'group_idx': 0, 'pos_in_group': 0, 'color': '#d16969'},
        {'name': 'Stack_Voltage', 'label': '电压', 'log_tag': 'BAT',
         'group_idx': 0, 'pos_in_group': 1, 'color': '#b5cea8'},
        # Group 1：包电流
        {'name': 'Pack_Current', 'label': '包电流', 'log_tag': 'BAT',
         'group_idx': 1, 'pos_in_group': 0, 'color': '#569cd6'},
        # Group 2：电芯温度1/2、高温温度
        {'name': 'TS1_Temperature', 'label': '电芯温度1', 'log_tag': 'BAT',
         'group_idx': 2, 'pos_in_group': 0, 'color': '#ce9178'},
        {'name': 'TS3_Temperature', 'label': '电芯温度2', 'log_tag': 'BAT',
         'group_idx': 2, 'pos_in_group': 1, 'color': '#4ec9b0'},
        {'name': 'HDQ_Temperature', 'label': '高温温度', 'log_tag': 'BAT',
         'group_idx': 2, 'pos_in_group': 2, 'color': '#dcdcaa'},
        # Group 3：电芯电压1~6
        {'name': 'Cell_Voltage_0', 'label': '电芯电压1', 'log_tag': 'BAT',
         'group_idx': 3, 'pos_in_group': 0, 'color': '#c586c0'},
        {'name': 'Cell_Voltage_1', 'label': '电芯电压2', 'log_tag': 'BAT',
         'group_idx': 3, 'pos_in_group': 1, 'color': '#f48771'},
        {'name': 'Cell_Voltage_2', 'label': '电芯电压3', 'log_tag': 'BAT',
         'group_idx': 3, 'pos_in_group': 2, 'color': '#6a9955'},
        {'name': 'Cell_Voltage_3', 'label': '电芯电压4', 'log_tag': 'BAT',
         'group_idx': 3, 'pos_in_group': 3, 'color': '#9cdcfe'},
        {'name': 'Cell_Voltage_4', 'label': '电芯电压5', 'log_tag': 'BAT',
         'group_idx': 3, 'pos_in_group': 4, 'color': '#d16969'},
        {'name': 'Cell_Voltage_5', 'label': '电芯电压6', 'log_tag': 'BAT',
         'group_idx': 3, 'pos_in_group': 5, 'color': '#b5cea8'},
        # Group 4：电芯类型、并数
        {'name': 'cell_type', 'label': '电芯类型', 'log_tag': 'BAT',
         'group_idx': 4, 'pos_in_group': 0, 'color': '#569cd6'},
        {'name': 'cell_parallel', 'label': '并数', 'log_tag': 'BAT',
         'group_idx': 4, 'pos_in_group': 1, 'color': '#ce9178'},
    ]

    # 变量注册表：用户只填变量名，UI 不暴露 block/group/pos
    _REGISTRY = {v['name']: v for v in DEFAULT_VARS}

    _BRACKET_RE = re.compile(r'\[([^\]]*)\]')

    @classmethod
    def _parse_tagged_line(cls, line: str, tag: str):
        """从 '... F BMS [...]...' 行中提取 tag 后的方括号分组。

        返回 [[group0, group1, ...]]（1 个 block，包含若干方括号分组）。
        没匹配返回 None。
        """
        marker = cls._TAG_MARKERS.get(tag) or f' {tag} '
        idx = line.find(marker)
        if idx < 0:
            return None
        rest = line[idx + len(marker):]
        groups = []
        for m in cls._BRACKET_RE.finditer(rest):
            parts = m.group(1).split()
            if not parts:
                continue
            try:
                groups.append([int(p) for p in parts])
            except ValueError:
                continue
        return [groups] if groups else None


class BmsMonitorWindow(MonitorWindow):
    """BMS 数据监视独立窗口。"""

    PANEL_CLS = BmsMonitorPanel
    TITLE = 'BMS 数据监视'


def _build_imu_default_vars() -> list[dict]:
    """构造 IMU 监视默认变量注册表（18 个，1 个 block × 7 组）。

    日志格式（C 代码）：
      LOG_TS(AM_LOG_LEVEL_INFO, "Imu_data",
             "%x %d %x#%d %d %d %d|%d %d %d# %d %d %d|%d|%d %d %d|%d\\n",
             imu->errCode, imu->init.state, imu->init.caliFlag,
             imu_angle.pitch_angle, imu_angle.roll_angle,
             imu_angle.slope_angle, imu->pos.swPitchAcc,
             fAcc[X/Y/Z]*1000,
             fGyro[X/Y/Z]*1000,
             imu->conver.fAcc[IMU_ACCEL_Z]*1000,
             imu->init.gyroOffset[X/Y/Z],
             imu->recali_t.var_gyro_norm)

    实际日志：1784000342739 I Imu_data 0 2 3#9153 -1567 9282 0|-157 -27 978# 0 0 0|977|20 -55 -24|72
    分组（按 # 和 | 分割）：
      g0(3): errCode / state / caliFlag
      g1(4): pitch / roll / slope / swPitchAcc
      g2(3): fAcc X / Y / Z
      g3(3): fGyro X / Y / Z
      g4(1): conver.fAcc Z
      g5(3): gyroOffset X / Y / Z
      g6(1): var_gyro_norm
    """
    out: list[dict] = []
    palette = _PALETTE
    idx = [0]

    def next_color() -> str:
        c = palette[idx[0] % len(palette)]
        idx[0] += 1
        return c

    def add(name, label, group_idx, pos_in_group,
            y_min=None, y_max=None):
        out.append({
            'name': name, 'label': label, 'log_tag': 'Imu_data',
            'block_idx': 0, 'group_idx': group_idx,
            'pos_in_group': pos_in_group, 'color': next_color(),
            'y_min': y_min, 'y_max': y_max,
        })

    imu_groups = [
        # g0: errCode / state / caliFlag
        ['ErrCode', 'InitState', 'CaliFlag'],
        # g1: pitch / roll / slope / swPitchAcc
        ['PitchAngle', 'RollAngle', 'SlopeAngle', 'SwPitchAcc'],
        # g2: fAcc X / Y / Z
        ['AccX', 'AccY', 'AccZ'],
        # g3: fGyro X / Y / Z
        ['GyroX', 'GyroY', 'GyroZ'],
        # g4: conver.fAcc Z
        ['ConvAccZ'],
        # g5: gyroOffset X / Y / Z
        ['GyroOffsetX', 'GyroOffsetY', 'GyroOffsetZ'],
        # g6: var_gyro_norm
        ['VarGyroNorm'],
    ]
    for gi, group in enumerate(imu_groups):
        for pi, name in enumerate(group):
            add(name, name, gi, pi)
    return out


class ImuMonitorPanel(StructuredMonitorPanel):
    """IMU 数据监视面板（可嵌入）。

    日志行格式（设备 C 代码 LOG_TS 宏）：
      1784000342739 I Imu_data 0 2 3#9153 -1567 9282 0|-157 -27 978# 0 0 0|977|20 -55 -24|72

    解析：1 个 block（block_idx=0），按 # 与 | 分隔成 7 组（3+4+3+3+1+3+1 = 18 个值）。
    faultCode/errCode/caliFlag 字段在 C 代码里用 %x 打印（hex），解析器按十进制失败再按 hex。
    """

    _TAG_MARKERS = {
        'Imu_data': ' Imu_data ',
    }
    _SECTION_LABELS = {
        ('Imu_data', 0): 'IMU 数据',
    }

    DEFAULT_VARS = _build_imu_default_vars()
    _REGISTRY = {v['name']: v for v in DEFAULT_VARS}

    @classmethod
    def _parse_tagged_line(cls, line: str, tag: str):
        """按 # 与 | 分组，1 个 block。"""
        marker = cls._TAG_MARKERS.get(tag)
        if not marker or marker not in line:
            return None
        rest = line.split(marker, 1)[1] if marker in line else ''
        return _parse_pipe_hash(rest)


class ImuMonitorWindow(MonitorWindow):
    """IMU 数据监视独立窗口。"""

    PANEL_CLS = ImuMonitorPanel
    TITLE = 'IMU 数据监视'
