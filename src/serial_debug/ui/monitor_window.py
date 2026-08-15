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

import datetime as _dt
import math
import re
import time
import tkinter as tk
from array import array as _farray
from collections import deque
from tkinter import colorchooser, filedialog, messagebox, ttk
from tkinter import font as tkfont

try:
    from serial_debug.infra import config, themes
except Exception:
    try:
        import config
        import themes
    except Exception:
        themes = None
        config = None


# 日志时间戳（毫秒，10~13 位数字）。例：1783996615491
# 不限定行首：用户的日志格式可能是 "[HH:MM:SS.mmm] 1783996615491 ..."，
# 时间戳跟在 [time] 前缀后面，所以用 search 在整行里找第一个 10-13 位数字。
_LOG_TS_RE = re.compile(r'(?<!\d)(\d{10,13})(?!\d)')


def _downsample_lttb(data, threshold: int):
    """LTTB (Largest Triangle Three Buckets) 保形状降采样。

    输入 data: list[(x, y)]，按 x 升序。
    返回不超过 threshold 个点的子集，保留尖峰/突变等形状特征。

    原理：把数据分 threshold 个 bucket，每 bucket 选一个让"前一个选中点
    → 当前 bucket 内候选点 → 下一 bucket 平均点"三角形面积最大的点。
    面积大 = 该点对形状贡献大 = 保留它最不损失视觉特征。

    相比等距抽样：
    - 平稳段不丢点（连续相同值不会丢一个导致视觉断点）
    - 尖峰段能保留极值（即使尖峰只占 1 个点也大概率被选）
    - 复杂度 O(n)，纯 Python 实现，10w 点 ~30ms。

    threshold < 3 或 data 长度本身 <= threshold 时直接返回原数据。
    """
    n = len(data)
    if n <= threshold or threshold < 3 or n < 3:
        return data
    # 桶大小（浮点，避免整数除法丢精度）。首尾各保留 1 个点，中间分 threshold-2 桶
    step = (n - 2) / (threshold - 2)
    sampled = [data[0]]
    a = 0  # 上一个被选中点的索引
    n_minus_1 = n - 1
    for i in range(threshold - 2):
        # 当前 bucket 的范围 [bucket_start, bucket_end)
        bucket_start = int((i + 1) * step) + 1
        bucket_end = int((i + 2) * step) + 1
        if bucket_end > n:
            bucket_end = n
        if bucket_start >= bucket_end:
            bucket_start = bucket_end - 1
        # 下一 bucket 的平均点（用于三角形面积计算）
        next_start = bucket_end
        next_end = int((i + 3) * step) + 1
        if next_end > n:
            next_end = n
        # 最后一个 bucket 后面没数据了：用最后一点作为"下一 bucket 平均"
        if next_start >= n_minus_1:
            avg_x, avg_y = data[n_minus_1]
        else:
            if next_end <= next_start:
                next_end = next_start + 1
            avg_x = 0.0
            avg_y = 0.0
            for j in range(next_start, next_end):
                avg_x += data[j][0]
                avg_y += data[j][1]
            cnt = next_end - next_start
            if cnt > 0:
                avg_x /= cnt
                avg_y /= cnt
        # 在当前 bucket 内找三角形面积最大的点
        max_area = -1.0
        max_idx = bucket_start
        ax, ay = data[a]
        # 三角形面积 = |(x-ax)*(avg_y-ay) - (y-ay)*(avg_x-ax)|
        # 不乘 0.5，比较只需要相对大小
        for j in range(bucket_start, bucket_end):
            p = data[j]
            area = abs((p[0] - ax) * (avg_y - ay)
                       - (p[1] - ay) * (avg_x - ax))
            if area > max_area:
                max_area = area
                max_idx = j
        sampled.append(data[max_idx])
        a = max_idx
    sampled.append(data[n_minus_1])
    return sampled



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
    # 兜底主题：补全所有代码访问的 key，避免 KeyError 导致弹窗只显示部分内容
    return {
        'bg': '#1e1e1e', 'panel_bg': '#252526', 'panel_fg': '#cccccc',
        'fg': '#cccccc', 'muted': '#858585', 'border': '#3c3c3c',
        'select_bg': '#264f78',
        'btn_bg': '#3a3d41', 'btn_hover': '#4c5052', 'btn_active': '#2d2d30',
        'btn_primary': '#0e639c', 'btn_primary_hover': '#1177bb',
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


def _size_dialog_to_content(dlg, min_w=320, min_h=200,
                            parent=None):
    """让对话框按内容自适应初始尺寸，并居中到父窗口。

    问题背景：很多 Toplevel 弹窗默认尺寸由 Tk 内部布局自然撑开，
    在不同 DPI/字体下会出现内容被裁剪、按钮看不见、Listbox 只有 1 行高
    等问题。本函数：
    1. update_idletasks 触发布局计算
    2. 用 winfo_reqwidth/reqheight 拿到内容需要的尺寸
    3. 取 max(需要尺寸, min_w/min_h) 作为初始 geometry
    4. 设 minsize 防止用户拖小后内容被裁
    5. resizable(True, True) 让用户可以放大查看更多内容
    6. 居中到父窗口（若提供）
    """
    dlg.update_idletasks()
    need_w = dlg.winfo_reqwidth()
    need_h = dlg.winfo_reqheight()
    w = max(need_w, min_w)
    h = max(need_h, min_h)
    try:
        dlg.geometry(f'{w}x{h}')
    except Exception:
        pass
    try:
        dlg.minsize(min_w, min_h)
    except Exception:
        pass
    try:
        dlg.resizable(True, True)
    except Exception:
        pass
    if parent is not None:
        try:
            parent.update_idletasks()
            px = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
            py = parent.winfo_rooty() + (parent.winfo_height() - h) // 2
            px = max(px, 0)
            py = max(py, 0)
            dlg.geometry(f'+{px}+{py}')
        except Exception:
            pass


def _enable_tree_column_resize(tree, min_width=40, edge_tol=6):
    """让 ttk.Treeview 支持拖拽列分隔线调整列宽。

    ttk.Treeview 默认 heading 分隔线只有 1px，用户很难抓取。
    这里在 heading 区域监听鼠标：靠近列右边缘 edge_tol 像素时启动拖拽，
    拖动期间用 tree.column(col, width=...) 实时更新。
    所有列强制 stretch=False，避免自动伸缩覆盖手动拖拽结果。
    """
    state = {'col': None, 'x0': 0, 'w0': 0}

    def col_list():
        cols = tree.cget('columns') or ()
        if isinstance(cols, str):
            cols = tuple(c for c in cols.split() if c)
        return list(cols)

    def col_right_x(name):
        x = 0
        for c in col_list():
            try:
                w = tree.column(c, 'width')
            except Exception:
                w = 0
            if c == name:
                return x + w
            x += w
        return None

    def on_press(e):
        # Tk 不同版本对 heading 与 separator 的 region 命名略不同，统一接受
        region = tree.identify_region(e.x, e.y)
        if region not in ('heading', 'separator', 'separator1', 'cell'):
            # cell 用于在 heading 行的非边缘位置仍允许判断（会被 edge_tol 拦截）
            return
        col_id = tree.identify_column(e.x)  # '#1', '#2' ...
        if not col_id or col_id == '#0':
            return
        try:
            idx = int(col_id.lstrip('#'))
        except ValueError:
            return
        cols = col_list()
        if idx < 1 or idx > len(cols):
            return
        # 拖拽触发点：当前列右边缘 OR 下一列的左边缘（更宽容）
        name = cols[idx - 1]
        rx = col_right_x(name)
        if rx is None:
            return
        if abs(e.x - rx) <= edge_tol:
            state['col'] = name
            state['x0'] = e.x
            try:
                state['w0'] = tree.column(name, 'width')
            except Exception:
                state['w0'] = 0

    def on_motion(e):
        if not state['col']:
            return
        new_w = max(min_width, state['w0'] + (e.x - state['x0']))
        try:
            # 保留原 stretch 设置：若上层用 stretch=True 想自动分配，
            # 拖拽后不能强制 False 覆盖掉
            cur_stretch = tree.column(state['col'], 'stretch')
            tree.column(state['col'], width=new_w, stretch=cur_stretch)
        except Exception:
            pass

    def on_release(_e):
        state['col'] = None

    tree.bind('<Button-1>', on_press, '+')
    tree.bind('<B1-Motion>', on_motion, '+')
    tree.bind('<ButtonRelease-1>', on_release, '+')

    # 鼠标在 heading 列边缘切换光标形状，提示可拖拽
    def on_motion_cursor(e):
        region = tree.identify_region(e.x, e.y)
        if region not in ('heading', 'separator', 'separator1', 'cell'):
            tree.configure(cursor='')
            return
        col_id = tree.identify_column(e.x)
        if not col_id or col_id == '#0':
            tree.configure(cursor='')
            return
        try:
            idx = int(col_id.lstrip('#'))
        except ValueError:
            tree.configure(cursor='')
            return
        cols = col_list()
        if idx < 1 or idx > len(cols):
            tree.configure(cursor='')
            return
        rx = col_right_x(cols[idx - 1])
        if rx is not None and abs(e.x - rx) <= edge_tol:
            tree.configure(cursor='sb_h_double_arrow')
        else:
            tree.configure(cursor='')

    tree.bind('<Motion>', on_motion_cursor, '+')
    tree.bind('<Leave>', lambda _e: tree.configure(cursor=''), '+')


def _enable_tree_column_stretch(tree, ratios, min_widths=None):
    """绑定 <Configure>：当 treeview 宽度变化时按比例自动分配各列宽度。

    ratios: dict[col_name, float] —— 每列占比（可不等比），如
        {'label': 3, 'tag': 1, 'pos': 1}
    min_widths: 可选每列最小宽度，避免压缩到不可读。
    用户手动拖拽后的新宽度会作为下次分配的基准比例。
    """
    if not ratios:
        return
    state = {'last_w': 0}

    def col_list():
        cols = tree.cget('columns') or ()
        if isinstance(cols, str):
            cols = tuple(c for c in cols.split() if c)
        return list(cols)

    def apply_dist():
        cols = col_list()
        try:
            total_w = tree.winfo_width()
        except Exception:
            return
        if total_w <= 1:
            return
        # 用当前各列宽度作为权重（用户拖拽过就以新比例为准），
        # 缺失列回退到 ratios
        cur = {}
        for c in cols:
            try:
                w = tree.column(c, 'width')
            except Exception:
                w = 0
            cur[c] = max(w, 1)
        # 归一化权重
        sum_cur = sum(cur.get(c, 0) for c in cols)
        sum_ratio = sum(ratios.get(c, 1) for c in cols)
        if sum_cur <= 0 or sum_ratio <= 0:
            return
        # 用当前宽度比例作为分配权重，若比例稳定则与 ratios 等效
        for c in cols:
            w_cur = cur.get(c, 0)
            r = w_cur / sum_cur if sum_cur else (ratios.get(c, 1) / sum_ratio)
            new_w = int(total_w * r)
            if min_widths and c in min_widths:
                new_w = max(new_w, min_widths[c])
            try:
                tree.column(c, width=new_w, stretch=False)
            except Exception:
                pass

    def on_configure(_e):
        try:
            cur_w = tree.winfo_width()
        except Exception:
            return
        if cur_w != state['last_w']:
            state['last_w'] = cur_w
            apply_dist()

    # 初次布局后做一次分配
    tree.after_idle(apply_dist)
    tree.bind('<Configure>', on_configure, '+')


class _VarDialog(tk.Toplevel):
    """添加/编辑监视变量的对话框。"""

    def __init__(self, parent, initial=None, title='添加监视变量',
                 used_names=(), theme=None):
        super().__init__(parent)
        self.title(title)
        # 不调用 transient：GNOME/Mutter 把 transient 窗口当对话框处理，
        # 会移除最大化按钮、禁用边框拖拽缩放，导致弹窗只有最小化/关闭按钮
        # 且尺寸被锁死。改用 lift+focus 保持层级。
        try:
            self.lift(parent)
            self.focus_force()
        except Exception:
            pass
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
        # 内容完整可见 + 可调整大小（原 resizable(False, False) 会让用户
        # 在不同字体/DPI 下无法看到全部 Entry/按钮）
        _size_dialog_to_content(self, min_w=480, min_h=320,
                                parent=parent)

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
        # 不调用 transient：GNOME/Mutter 把 transient 窗口当对话框处理，
        # 会移除最大化按钮、禁用边框拖拽缩放。改用 lift+focus 保持层级。
        try:
            self.lift(parent)
            self.focus_force()
        except Exception:
            pass
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
        # 显式初始尺寸 + 最小尺寸，避免 BMS 变量对话框在不同字体/DPI 下
        # 默认尺寸过小导致分组下拉、变量名、说明、Y 范围、告警、显示图表等
        # 多行 Entry 互相挤压或被裁切
        _size_dialog_to_content(self, min_w=520, min_h=420,
                                parent=parent)

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
        # 不调用 transient：GNOME/Mutter 把 transient 窗口当对话框处理，
        # 会移除最大化按钮、禁用边框拖拽缩放。改用 lift+focus 保持层级。
        try:
            self.lift(parent)
            self.focus_force()
        except Exception:
            pass
        self.resizable(True, True)
        # 显式初始尺寸 + 最小尺寸，避免窗口被默认撑小或被变量数量撑到屏外
        self.geometry('760x560')
        self.minsize(560, 420)
        # 居中到父窗口
        try:
            parent.update_idletasks()
            px = parent.winfo_rootx() + (parent.winfo_width() - 760) // 2
            py = parent.winfo_rooty() + (parent.winfo_height() - 560) // 2
            self.geometry(f'+{max(px, 0)}+{max(py, 0)}')
        except Exception:
            pass
        self.result = None
        if theme:
            self.configure(bg=theme['bg'])

        f = ttk.Frame(self, padding=12)
        f.pack(fill='both', expand=True)

        ttk.Label(f, text='标题').grid(row=0, column=0, sticky='w', pady=4)
        self.title_var = tk.StringVar(
            value=initial.get('title', '数据曲线') if initial else '数据曲线')
        ttk.Entry(f, textvariable=self.title_var, width=36
                  ).grid(row=0, column=1, pady=4, padx=8, columnspan=2)

        ttk.Label(f, text='X 轴标签').grid(row=1, column=0, sticky='w', pady=4)
        self.x_var = tk.StringVar(
            value=initial.get('x_label', '时间 (s)') if initial else '时间 (s)')
        ttk.Entry(f, textvariable=self.x_var, width=36
                  ).grid(row=1, column=1, pady=4, padx=8, columnspan=2)

        ttk.Label(f, text='Y 轴标签').grid(row=2, column=0, sticky='w', pady=4)
        self.y_var = tk.StringVar(
            value=initial.get('y_label', '值') if initial else '值')
        ttk.Entry(f, textvariable=self.y_var, width=36
                  ).grid(row=2, column=1, pady=4, padx=8, columnspan=2)

        # 显示选项：在曲线上叠加最值标注 / 数据表格；鼠标悬停查看点详情
        ttk.Label(f, text='显示选项', font=_font('bold')
                  ).grid(row=3, column=0, columnspan=3, sticky='w',
                         pady=(8, 2))
        opt_frame = ttk.Frame(f)
        opt_frame.grid(row=3, column=0, columnspan=3, sticky='w',
                        padx=(0, 4), pady=(0, 2))
        # NOTE: 上面覆盖了 row 3 的 label，下面用单独的 row 容纳 checkboxes
        opt_frame.grid_rowconfigure(0, weight=0)
        self.show_max_var = tk.BooleanVar(
            value=initial.get('show_max', False) if initial else False)
        self.show_min_var = tk.BooleanVar(
            value=initial.get('show_min', False) if initial else False)
        self.show_avg_var = tk.BooleanVar(
            value=initial.get('show_avg', False) if initial else False)
        self.show_last_var = tk.BooleanVar(
            value=initial.get('show_last', False) if initial else False)
        self.show_table_var = tk.BooleanVar(
            value=initial.get('show_table', False) if initial else False)
        self.show_hover_var = tk.BooleanVar(
            value=initial.get('show_hover', True) if initial else True)
        ttk.Checkbutton(opt_frame, text='最大值',
                        variable=self.show_max_var
                        ).pack(side='left', padx=4)
        ttk.Checkbutton(opt_frame, text='最小值',
                        variable=self.show_min_var
                        ).pack(side='left', padx=4)
        ttk.Checkbutton(opt_frame, text='均值线',
                        variable=self.show_avg_var
                        ).pack(side='left', padx=4)
        ttk.Checkbutton(opt_frame, text='末值点',
                        variable=self.show_last_var
                        ).pack(side='left', padx=4)
        ttk.Checkbutton(opt_frame, text='数据表格',
                        variable=self.show_table_var
                        ).pack(side='left', padx=4)
        ttk.Checkbutton(opt_frame, text='悬停提示',
                        variable=self.show_hover_var
                        ).pack(side='left', padx=4)

        ttk.Label(f, text='包含变量（多选）', font=_font('bold')
                  ).grid(row=4, column=0, columnspan=3, sticky='w', pady=(8, 2))
        # 变量列表放进可滚动容器：变量多时不再把对话框撑高到屏幕外，
        # 同时显示完整 label（不再截断为 10 字符）
        vars_outer = ttk.Frame(f)
        vars_outer.grid(row=5, column=0, columnspan=3, sticky='nsew',
                        padx=(0, 4), pady=2)
        f.rowconfigure(5, weight=1)
        f.columnconfigure(1, weight=1)
        f.columnconfigure(2, weight=1)

        vars_canvas = tk.Canvas(vars_outer, highlightthickness=0,
                                borderwidth=0,
                                bg=theme['bg'] if theme else '#ffffff')
        vars_canvas.pack(side='left', fill='both', expand=True)
        vars_sb = ttk.Scrollbar(vars_outer, orient='vertical',
                                command=vars_canvas.yview)
        vars_sb.pack(side='right', fill='y')
        vars_canvas.configure(yscrollcommand=vars_sb.set)
        vars_inner = ttk.Frame(vars_canvas)
        self._vars_inner = vars_inner
        vars_window = vars_canvas.create_window((0, 0), window=vars_inner,
                                                anchor='nw')
        # 让 inner 跟随 canvas 宽度（避免水平方向留白或溢出）
        def _resize_inner(_e=None):
            vars_canvas.itemconfigure(vars_window,
                                       width=vars_canvas.winfo_width())
        vars_canvas.bind('<Configure>', _resize_inner, '+')
        # 鼠标滚轮滚动变量列表
        def _wheel(e):
            vars_canvas.yview_scroll(int(-e.delta / 120), 'units')
        vars_canvas.bind('<Enter>',
                        lambda _e: vars_canvas.bind_all('<MouseWheel>', _wheel))
        vars_canvas.bind('<Leave>',
                        lambda _e: vars_canvas.unbind_all('<MouseWheel>'))

        initial_vars = set(initial.get('vars', [])) if initial else set()
        self._var_checks: dict[str, tk.BooleanVar] = {}
        n_cols = 3
        for i, v in enumerate(all_vars):
            name = v['name']
            bv = tk.BooleanVar(value=name in initial_vars)
            label = v.get('label', '') or name
            # 完整 label，让 ttk.Label 自带省略由布局控制
            cb = FillCheck(vars_inner, text=f'{label} ({name})', variable=bv)
            cb.grid(row=i // n_cols, column=i % n_cols,
                    sticky='ew', padx=4, pady=1)
            self._var_checks[name] = bv
        for c in range(n_cols):
            vars_inner.columnconfigure(c, weight=1, minsize=160)
        # 让 inner 更新后才能正确设置 scrollregion
        vars_inner.update_idletasks()
        vars_canvas.configure(scrollregion=vars_canvas.bbox('all'))

        # 全选/全不选
        sel_btns = ttk.Frame(f)
        sel_btns.grid(row=6, column=0, columnspan=3, sticky='w', pady=(6, 0))
        ttk.Button(sel_btns, text='全选',
                   command=lambda: self._set_all(True)
                   ).pack(side='left', padx=4)
        ttk.Button(sel_btns, text='全不选',
                   command=lambda: self._set_all(False)
                   ).pack(side='left', padx=4)

        btns = ttk.Frame(f)
        btns.grid(row=7, column=0, columnspan=3, pady=(10, 0))
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
            'show_max': self.show_max_var.get(),
            'show_min': self.show_min_var.get(),
            'show_avg': self.show_avg_var.get(),
            'show_last': self.show_last_var.get(),
            'show_table': self.show_table_var.get(),
            'show_hover': self.show_hover_var.get(),
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
        # 鼠标悬停 tooltip：Toplevel 单例，跟随鼠标显示点详情
        self._hover_tip: tk.Toplevel | None = None
        self._hover_canvas: tk.Canvas | None = None
        # 多图表配置：每个图表 dict = {title, x_label, y_label, vars: [names]}
        # 若传入 plots 则用之；否则默认一个空图表（vars=[]），
        # 让用户在编辑图表时再勾选变量。这样初次打开时图例为空、绘制轻量，
        # 打开速度快；用户编辑后的配置通过 plots 参数传入即生效。
        if plots:
            self._plots: list[dict] = [self._normalize_plot(p)
                                       for p in plots]
        else:
            self._plots = [{
                'title': '数据曲线',
                'x_label': '时间 (s)',
                'y_label': '值',
                'vars': [],
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
        self._axis_font = tkfont.Font(font=('DejaVu Sans', 9))
        self._title_font = tkfont.Font(font=('DejaVu Sans', 10, 'bold'))
        self._small_font = tkfont.Font(font=('DejaVu Sans', 8))
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
        self._toolbar = bar

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
        # 配置菜单：悬停提示模式（仅当前曲线 / 显示全部曲线）
        self._hover_mode = tk.StringVar(value='current')
        cfg_btn = ttk.Menubutton(bar, text='配置', direction='below')
        cfg_menu = tk.Menu(cfg_btn, tearoff=False)
        cfg_btn['menu'] = cfg_menu
        cfg_btn.pack(side='left')
        hover_sub = tk.Menu(cfg_menu, tearoff=False)
        cfg_menu.add_cascade(label='悬停提示', menu=hover_sub)
        hover_sub.add_radiobutton(label='仅当前曲线',
                                    variable=self._hover_mode,
                                    value='current')
        hover_sub.add_radiobutton(label='显示全部曲线',
                                    variable=self._hover_mode,
                                    value='all')

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
        # 用自定义 style 让 sash 更显眼，避免 BMS 主题下 1px 分隔线
        # 用户抓不到，导致"无法调节各窗口宽高"
        style = ttk.Style()
        try:
            style.configure('MonitorSash.TPanedwindow',
                             background=self._theme.get('border', '#3c3c3c'))
            style.configure('MonitorSash.TPanedwindow.Sash',
                             sashthickness=8, gripcount=0)
        except Exception:
            pass
        try:
            pane = ttk.PanedWindow(self, orient='horizontal',
                                   style='MonitorSash.TPanedwindow')
        except Exception:
            pane = ttk.PanedWindow(self, orient='horizontal')
        pane.pack(fill='both', expand=True)
        self._content_pane = pane

        left = ttk.Frame(pane, width=320)
        pane.add(left, weight=0)
        # 保存左侧变量列表 frame 引用，视图菜单可隐藏/显示
        self._var_list_frame = left
        # 通过 paneconfigure 设 minsize：某些 Tk 版本（含 docker python3.12
        # 打包出的 binary）的 add 子命令不接受 -minsize 选项，会直接
        # 抛 TclError 导致整个 panel 构建失败 → Toplevel 空白（白屏）
        try:
            pane.paneconfigure(left, minsize=280)
        except Exception:
            pass
        ttk.Label(left, text='变量列表', font=_font('bold')).pack(
            anchor='w', padx=8, pady=(8, 4))
        self._var_tree = ttk.Treeview(
            left, columns=('name', 'regex'), show='headings',
            height=12, selectmode='browse')
        self._var_tree.heading('name', text='名称')
        self._var_tree.heading('regex', text='正则')
        self._var_tree.column('name', width=90, stretch=False,
                              anchor='w', minwidth=60)
        self._var_tree.column('regex', width=180, stretch=False,
                              anchor='w', minwidth=60)
        # 水平滚动条：列总宽超过可见宽度时可横向滚动查看完整内容
        hsb = ttk.Scrollbar(left, orient='horizontal',
                            command=self._var_tree.xview)
        self._var_tree.configure(xscrollcommand=hsb.set)
        self._var_tree.pack(fill='both', expand=True, padx=4, pady=(4, 0))
        hsb.pack(fill='x', padx=4, pady=(0, 4))
        self._var_tree.bind('<Double-1>', lambda _e: self._edit_var())
        # 启用列宽拖拽（ttk.Treeview 默认分隔线只有 1px，体验差）
        _enable_tree_column_resize(self._var_tree)

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
            with open(path, encoding='utf-8', errors='replace') as f:
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
        else:
            data = {n: list(buf) for n, buf in self._buffers.items()}
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
                cursor = dict.fromkeys(names, 0)
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
        dlg.minsize(520, 320)
        dlg.resizable(True, True)
        # 不调用 transient：避免 GNOME 把它当对话框锁死尺寸
        try:
            dlg.lift(self.winfo_toplevel())
            dlg.focus_force()
        except Exception:
            pass
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
        _size_dialog_to_content(dlg, min_w=520, min_h=320,
                                parent=self.winfo_toplevel())
        try:
            dlg.geometry('680x480')
        except Exception:
            pass

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
        # 清除旧的 grid 配置：grid_size() 返回 (cols, rows)
        # 必须按对应轴重置：rows → rowconfigure, cols → columnconfigure
        # 否则从 2 plot 删到 1 plot 时，col 1 的 weight=1 不会清零，
        # 导致剩余 1 个 plot 只占一半宽度，另一半是空白
        n_cols, n_rows = container.grid_size()
        for r in range(n_rows):
            container.rowconfigure(r, weight=0, minsize=0)
        for c in range(n_cols):
            container.columnconfigure(c, weight=0, minsize=0)
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
            # 鼠标悬停：显示最近点详情（plot 配置 show_hover 控制开关）
            canvas.bind('<Motion>', self._on_plot_hover)
            canvas.bind('<Leave>', self._on_plot_leave)
            # 窗口/Canvas 大小变化时强制重绘（避免空闲跳过导致 resize 后画面不刷新）
            canvas.bind('<Configure>',
                        lambda _e: self._force_redraw())
            self._plot_frames.append(frame)
            self._plot_canvases.append(canvas)
        if self._plot_canvases:
            self._canvas = self._plot_canvases[0]
        # 重建后必须强制下一帧重绘：否则实时模式无新数据时会跳过，
        # 导致编辑/删除图表后新画布空白，曲线不显示
        self._force_redraw()

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

    # ---- 鼠标悬停 tooltip：显示最近点详情 ------------------------------
    def _on_plot_hover(self, event):
        """鼠标在画布上移动：找最近点，弹出 tooltip 显示数据详情。

        模式由 self._hover_mode 决定：
        - 'current': 找像素距离最近的单个点，显示单条曲线信息
        - 'all':     按鼠标 x 对应的时间，显示每条曲线在该时间附近的值
        """
        canvas: tk.Canvas = event.widget
        plot = self._canvas_plot(canvas)
        if plot is None:
            return
        # 默认开启；plot 配置可关
        if plot.get('show_hover', True) is False:
            self._hide_hover()
            return
        geom = plot.get('_geom')
        stats = plot.get('_stats_cache')
        if not geom or not stats:
            self._hide_hover()
            return
        pad_l, pad_t, plot_w, plot_h, t_min, t_max, y_lo, y_hi = geom
        if plot_w <= 0 or plot_h <= 0:
            return
        # 表格 overlay 区域：在表格内时不显示点提示
        tbl_rect = plot.get('_table_rect')
        if tbl_rect and tbl_rect[0] <= event.x <= tbl_rect[2] \
                and tbl_rect[1] <= event.y <= tbl_rect[3]:
            self._hide_hover()
            return
        if t_max <= t_min:
            self._hide_hover()
            return
        # 鼠标 x 转数据时间
        t_at = t_min + (event.x - pad_l) / plot_w * (t_max - t_min)

        mode = self._hover_mode.get() or 'current'
        if mode == 'all':
            self._hover_show_all(plot, stats, t_at, pad_l, pad_t,
                                  plot_w, plot_h, t_min, t_max,
                                  y_lo, y_hi, canvas, event)
        else:
            self._hover_show_current(stats, t_at, pad_l, pad_t,
                                       plot_w, plot_h, t_min, t_max,
                                       y_lo, y_hi, canvas, event)

    def _hover_show_current(self, stats, t_at, pad_l, pad_t,
                              plot_w, plot_h, t_min, t_max,
                              y_lo, y_hi, canvas, event):
        """仅当前曲线模式：找像素距离最近的点。"""
        SNAP_PX = 15  # 抓取半径：15px 内才算"在曲线上"
        best = None  # (px_dist, name, ts, val, color, mx, my)
        for st in stats:
            if not st or not st.get('pts'):
                continue
            pts = st['pts']
            # pts 按 ts 升序，二分定位 ts 最近候选
            lo, hi = 0, len(pts) - 1
            while lo < hi:
                mid = (lo + hi) // 2
                if pts[mid][0] < t_at:
                    lo = mid + 1
                else:
                    hi = mid
            cand = []
            if lo < len(pts):
                cand.append(lo)
            if lo > 0:
                cand.append(lo - 1)
            for idx in cand:
                p_ts, p_val = pts[idx]
                mx = pad_l + plot_w * (p_ts - t_min) / (t_max - t_min) \
                    if (t_max > t_min) else pad_l
                my = pad_t + plot_h * (y_hi - p_val) / (y_hi - y_lo) \
                    if (y_hi > y_lo) else pad_t
                dx = event.x - mx
                dy = event.y - my
                dist = (dx * dx + dy * dy) ** 0.5
                if best is None or dist < best[0]:
                    best = (dist, st['name'], p_ts, p_val,
                            st['color'], mx, my)
        if best is None or best[0] > SNAP_PX:
            self._hide_hover()
            return
        _, name, p_ts, p_val, color, mx, my = best
        try:
            t_str = _dt.datetime.fromtimestamp(p_ts).strftime(
                '%Y-%m-%d %H:%M:%S')
        except (ValueError, OSError):
            t_str = f't={p_ts:g}'
        # 第一行：时间（无图标），其余数据行带变量色图标
        rows = [
            (f'时间: {t_str}', None),
            (f'{name}: {p_val:g}', color),
        ]
        # 附加统计
        st = next((s for s in stats
                    if s and s['name'] == name), None)
        if st:
            rows.append(
                (f'范围: {st["min_v"]:g} ~ {st["max_v"]:g} '
                 f'均值 {st["avg_v"]:g}', None))
        self._show_hover(canvas, rows, event.x_root,
                          event.y_root, mx, my)

    def _hover_show_all(self, plot, stats, t_at, pad_l, pad_t,
                         plot_w, plot_h, t_min, t_max,
                         y_lo, y_hi, canvas, event):
        """显示全部曲线模式：按鼠标 x 时间显示每条曲线的值。"""
        per_series = []
        snap_ts = None
        for st in stats:
            if not st or not st.get('pts'):
                continue
            pts = st['pts']
            lo, hi = 0, len(pts) - 1
            while lo < hi:
                mid = (lo + hi) // 2
                if pts[mid][0] < t_at:
                    lo = mid + 1
                else:
                    hi = mid
            cand = []
            if lo < len(pts):
                cand.append(lo)
            if lo > 0:
                cand.append(lo - 1)
            if not cand:
                continue
            best_idx = min(cand, key=lambda i: abs(pts[i][0] - t_at))
            p_ts, p_val = pts[best_idx]
            per_series.append((st, p_ts, p_val))
            if snap_ts is None or abs(p_ts - t_at) < abs(snap_ts - t_at):
                snap_ts = p_ts
        if not per_series:
            self._hide_hover()
            return
        try:
            t_str = _dt.datetime.fromtimestamp(snap_ts).strftime(
                '%Y-%m-%d %H:%M:%S')
        except (ValueError, OSError):
            t_str = f't={snap_ts:g}'
        # 第一行：时间（无图标），其余每行带变量色图标
        rows = [(f'时间: {t_str}', None)]
        for st, p_ts, p_val in per_series:
            rows.append(
                (f'{st["name"]}: {p_val:g}  '
                 f'(范围 {st["min_v"]:g} ~ {st["max_v"]:g}, '
                 f'均值 {st["avg_v"]:g})',
                 st['color']))
        # 颜色用第一条曲线的色，锚点用 snap_ts 对应的 x + 鼠标 y
        mx = pad_l + plot_w * (snap_ts - t_min) / (t_max - t_min) \
            if t_max > t_min else pad_l
        my = event.y
        self._show_hover(canvas, rows, event.x_root,
                          event.y_root, mx, my)

    def _on_plot_leave(self, _event):
        self._hide_hover()

    def _show_hover(self, canvas, rows, screen_x, screen_y,
                     plot_x, plot_y):
        """在鼠标位置旁显示 tooltip Toplevel。

        rows: list[(text, color_or_None)]。color 非 None 时在该行左侧
        画一个小色块（与文字同高度），用于区分变量/曲线。
        """
        if self._hover_tip is None or not self._hover_tip.winfo_exists():
            tip = tk.Toplevel(canvas)
            tip.wm_overrideredirect(True)  # 无边框
            tip.configure(bg=self._theme.get('panel_bg', '#252526'))
            self._hover_tip = tip
            # 用 Canvas 画图标 + 文字（Label 嵌入色块难做）
            self._hover_cv = tk.Canvas(
                tip, bg=self._theme.get('panel_bg', '#252526'),
                highlightthickness=0, borderwidth=0)
            self._hover_cv.pack(fill='both', expand=True)
        else:
            tip = self._hover_tip
        cv = self._hover_cv
        cv.delete('all')
        font = self._small_font
        linespace = font.metrics('linespace')
        # 色块尺寸：与文字行高匹配（小一点显得协调）
        icon_size = max(8, linespace - 3)
        pad = 6
        gap = 5
        row_h = linespace + 2

        # 计算文字最大宽度，确定画布宽度
        max_text_w = 0
        for text, _color in rows:
            w = font.measure(text)
            if w > max_text_w:
                max_text_w = w
        tip_w = pad + icon_size + gap + max_text_w + pad
        tip_h = pad + row_h * len(rows) + pad

        y = pad
        fg_color = self._theme.get('fg', '#cccccc')
        for text, color in rows:
            if color is not None:
                cx = pad
                cy = y + (row_h - icon_size) // 2
                # 圆形小图标：比方形更柔和，与文字协调
                cv.create_oval(cx, cy, cx + icon_size, cy + icon_size,
                               fill=color, outline='')
                text_x = pad + icon_size + gap
            else:
                text_x = pad
            cv.create_text(text_x, y + row_h / 2, text=text,
                           fill=fg_color, anchor='w', font=font)
            y += row_h

        cv.configure(width=tip_w, height=tip_h)
        # 计算尺寸并定位：鼠标右下方，避免贴边
        x = screen_x + 14
        y_pos = screen_y + 14
        # 屏幕右/下溢出则翻到左/上方
        try:
            sw = tip.winfo_screenwidth()
            sh = tip.winfo_screenheight()
            if x + tip_w > sw - 4:
                x = screen_x - tip_w - 14
            if y_pos + tip_h > sh - 4:
                y_pos = screen_y - tip_h - 14
        except Exception:
            pass
        tip.geometry(f'+{int(x)}+{int(y_pos)}')
        # 若之前 withdraw 过，需 deiconify 才能重新显示
        try:
            tip.wm_deiconify()
        except Exception:
            pass
        tip.lift()
        self._hover_canvas = canvas

    def _hide_hover(self):
        if self._hover_tip is not None:
            try:
                if self._hover_tip.winfo_exists():
                    self._hover_tip.withdraw()
            except Exception:
                pass
        self._hover_canvas = None

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
        # 不调用 transient：避免 GNOME 把它当对话框锁死尺寸
        try:
            dlg.lift(self.winfo_toplevel())
            dlg.focus_force()
        except Exception:
            pass
        dlg.configure(bg=t['bg'])
        result = {'idx': None}
        f = ttk.Frame(dlg, padding=12)
        f.pack(fill='both', expand=True)
        ttk.Label(f, text='选择图表：', font=_font('bold')).pack(
            anchor='w', pady=(0, 4))
        # Listbox 高度按内容自适应：每行 22px，至少 4 行可见，最多 14 行
        n_items = max(1, len(names))
        list_h = max(4, min(n_items, 14))
        lb = tk.Listbox(f, selectmode='single', font=_font(),
                        height=list_h,
                        bg=t['panel_bg'], fg=t['panel_fg'],
                        selectbackground=t['select_bg'],
                        selectforeground='#ffffff',
                        highlightthickness=0, borderwidth=0)
        lb.pack(fill='both', expand=True)
        for n in names:
            lb.insert('end', n)
        if names:
            lb.selection_set(0)

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
        # 按 Listbox 行数动态计算初始高度，并允许用户调整大小
        # 每行约 22px + 标题/按钮/内边距 ~120px
        init_h = max(220, 120 + 22 * list_h)
        _size_dialog_to_content(dlg, min_w=360, min_h=220,
                                parent=self.winfo_toplevel())
        try:
            dlg.geometry(f'{max(360, dlg.winfo_reqwidth())}x{init_h}')
        except Exception:
            pass
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
        w = c.winfo_width()
        h = c.winfo_height()
        if w < 30 or h < 30:
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

        # 一次性获取本 plot 所有变量数据，避免重复 list(buf)
        _data: dict[str, list] = {}
        for v in plot_vars:
            _data[v['name']] = self._get_plot_data(v['name'])

        title = plot.get('title', '')
        x_label = plot.get('x_label', '')
        y_label = plot.get('y_label', '')

        # 缓存 Font 对象
        legend_font = self._legend_font
        axis_font = self._axis_font
        title_font = self._title_font
        small_font = self._small_font

        # 时间范围：若用户框选缩放则用 zoom 范围，否则自动
        zoom = plot.get('_zoom')  # (t_min, t_max, y_lo, y_hi) or None
        if zoom:
            t_min, t_max, y_lo, y_hi = zoom
            y_zoomed = True
        else:
            y_zoomed = False
            t_max = 0.0
            t_min_data = None
            for v in plot_vars:
                pts = _data[v['name']]
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

        # 收集每条曲线在 [t_min, t_max] 内的点 + 左侧锚点（暂不降采样，
        # 降采样依赖 plot_w，而 plot_w 依赖 padding，padding 依赖 y 刻度文字宽度，
        # y 刻度文字宽度依赖 y_lo/y_hi，所以先收集算出 y 范围）
        series: list[tuple[dict, list[tuple[float, float]]]] = []
        for v in plot_vars:
            buf = _data[v['name']]
            if not buf:
                continue
            pts: list[tuple[float, float]] = []
            anchor = None
            for ts, val in buf:
                if ts < t_min:
                    anchor = (ts, val)
                    continue
                if ts > t_max:
                    break
                if anchor is not None and not pts:
                    pts.append(anchor)
                    anchor = None
                pts.append((ts, val))
            if not pts and anchor is not None:
                pts.append(anchor)
            if pts and pts[0][0] > t_min:
                pts.insert(0, (t_min, pts[0][1]))
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
            plot['_auto_y'] = (y_lo, y_hi)

        # ---- 自适应布局：测量文字宽度，渐进式压缩 padding ----
        # Y 轴刻度文字最大宽度（用 6 档刻度先估算）
        y_axis_w = 0
        for i in range(6):
            val = y_hi - (y_hi - y_lo) * i / 5
            y_axis_w = max(y_axis_w, axis_font.measure(f'{val:g}'))
        y_axis_w = min(y_axis_w, 72)  # 极端数值占位过多时截断

        # X 轴刻度文字宽度（用 6 档估算）
        span = t_max - t_min
        if span > 86400:
            fmt = '%m-%d %H:%M'
        elif span > 3600:
            fmt = '%H:%M:%S'
        elif span > 60:
            fmt = '%H:%M:%S'
        else:
            fmt = '%H:%M:%S'
        x_label_w = 0
        for i in range(7):
            t_at = t_min + span * i / 6
            try:
                txt = _dt.datetime.fromtimestamp(t_at).strftime(fmt)
            except (ValueError, OSError):
                txt = '--:--:--'
            x_label_w = max(x_label_w, axis_font.measure(txt))

        # 图例文字最大宽度
        legend_pad_col = 12 + 4 + 16  # 色块 12 + 间隙 4 + 文字偏移 16
        legend_max_text = 0
        for v in plot_vars:
            pts = _data[v['name']]
            if pts:
                txt = f'{v["name"]} = {pts[-1][1]:g}'
            else:
                txt = v['name']
            legend_max_text = max(legend_max_text,
                                  legend_font.measure(txt))

        axis_h = axis_font.metrics('linespace')
        title_h = title_font.metrics('linespace')
        legend_line_h = max(18, legend_font.metrics('linespace') + 6)

        # 渐进式显示开关：默认全显示，宽度不足时按优先级剥离
        show_title = bool(title)
        # X 轴标题不显示：用户反馈与刻度文字混着不方便，去掉
        show_x_label = False
        show_y_label = bool(y_label)
        show_legend = bool(plot_vars)

        # 基础 padding（最紧凑）
        pad_l = y_axis_w + 8
        if show_y_label:
            pad_l += 12
        pad_r = 8
        pad_t = 4
        if show_title:
            pad_t += title_h + 4
        # Y 轴标签放顶端时需要一行空间（与 title 共存时复用同一行）
        if show_y_label and not show_title:
            pad_t += axis_h + 4
        pad_b = axis_h + 6

        # 估算绘图区宽度，决定图例占多大
        est_plot_w = w - pad_l - pad_r
        if show_legend:
            # 图例文字宽度：最多占绘图区 40%，至少 80px
            legend_text_w = min(legend_max_text,
                                max(80, int(est_plot_w * 0.4)))
            pad_r = legend_pad_col + legend_text_w + 14
            est_plot_w = w - pad_l - pad_r

        # 宽度不足时按优先级剥离（legend > y_label > x_label > title）
        if est_plot_w < 60 and show_legend:
            show_legend = False
            legend_text_w = 0
            pad_r = 8
            est_plot_w = w - pad_l - pad_r
        if est_plot_w < 40 and show_y_label:
            show_y_label = False
            pad_l -= 12
            est_plot_w = w - pad_l - pad_r
        if est_plot_w < 30:
            c.delete('all')
            c.create_text(w / 2, h / 2, text='窗口过小',
                          fill=muted, font=small_font)
            return

        # 高度不足时剥离 title/x_label
        est_plot_h = h - pad_t - pad_b
        if est_plot_h < 60 and show_title:
            show_title = False
            pad_t -= title_h + 4
            est_plot_h = h - pad_t - pad_b
        if est_plot_h < 40 and show_x_label:
            show_x_label = False
            pad_b -= 14
            est_plot_h = h - pad_t - pad_b
        if est_plot_h < 20:
            c.delete('all')
            c.create_text(w / 2, h / 2, text='窗口过小',
                          fill=muted, font=small_font)
            return

        plot_w = max(20, w - pad_l - pad_r)
        plot_h = max(15, h - pad_t - pad_b)

        # 自适应刻度数量：
        # X 网格数：每档至少留 2.2 倍文字宽度间隔，确保不重叠
        if x_label_w > 0:
            desired = int(plot_w / (x_label_w * 2.2))
            n_grid_x = min(6, max(2, desired))
        else:
            n_grid_x = 6
        # Y 网格数：每档至少留 1.8 倍文字高度间隔，确保不重叠
        if axis_h > 0:
            n_grid_y = min(5, max(2, int(plot_h / (axis_h * 1.8))))
        else:
            n_grid_y = 5

        # 注：不在 series 层面降采样。统计/hover 需要全量极值，降采样只用于画线。
        # 画线处的 LTTB 降采样见下方 c.create_line 之前。

        # 跳过完全相同的重绘：几何/范围/数据均未变 → 直接返回。
        sig = (w, h, pad_l, pad_r, pad_t, pad_b, t_min, t_max, y_lo, y_hi,
               bool(zoom), plot.get('_zoom_rect'),
               show_title, show_x_label, show_y_label, show_legend,
               n_grid_x, n_grid_y,
               tuple((v['name'], len(p), p[-1][0] if p else 0.0)
                     for v, p in series))
        cache = plot.setdefault('_draw_cache', {})
        cur = cache.get(id(c))
        if cur and cur.get('sig') == sig:
            # 数据未变，跳过整次重绘（line item 等不重建）
            return
        cache[id(c)] = {'sig': sig}
        c.delete('all')

        # 标题：限制在绘图区内（小窗口下若仍显示则居中绘制）
        if show_title:
            title_text = title
            max_w = plot_w
            if title_font.measure(title_text) > max_w:
                title_text = self._truncate_text(title_text, max_w,
                                                  title_font)
            c.create_text(pad_l + plot_w / 2, 4, text=title_text,
                          fill=fg, anchor='n', font=title_font)

        # Y 轴标签：放画布顶端（绘图区上方左侧），避免与曲线混在一起
        if show_y_label:
            ylbl = y_label
            # 限定宽度：Y 轴刻度文字宽度 + 12 余量
            max_w = y_axis_w + 12
            if axis_font.measure(ylbl) > max_w:
                ylbl = self._truncate_text(ylbl, max_w, axis_font)
            # 放在顶端紧贴 Y 刻度顶部，靠左对齐
            c.create_text(pad_l - 6, pad_t - 2, text=ylbl,
                          fill=muted, anchor='sw',
                          font=axis_font)

        # Y 刻度 + 水平网格线
        # 先按等距生成 n_grid_y+1 个刻度值，若 0 在范围内则把最接近 0
        # 的刻度替换为 0，让用户能直观看到 0 基线
        y_tick_vals = [y_hi - (y_hi - y_lo) * i / n_grid_y
                       for i in range(n_grid_y + 1)]
        if y_lo < 0 < y_hi:
            idx0 = min(range(len(y_tick_vals)),
                        key=lambda i: abs(y_tick_vals[i]))
            if abs(y_tick_vals[idx0]) > 1e-9:
                y_tick_vals[idx0] = 0.0
        for i, val in enumerate(y_tick_vals):
            y = pad_t + plot_h * i / n_grid_y
            c.create_line(pad_l, y, pad_l + plot_w, y,
                          fill=border, dash=(2, 3))
            c.create_text(pad_l - 6, y, text=f'{val:g}',
                          fill=muted, anchor='e',
                          font=axis_font)

        # X 刻度 + 垂直网格线 + 时间标签（自适应格式）
        # 边缘刻度用 nw/ne 锚点，避免长标签溢出画布左右边界
        # 中间刻度用 n 居中；间隔由 n_grid_x 自适应保证不重叠
        # 极窄画布下按槽位宽度截断，防止边缘标签延伸出画布
        n_xticks = n_grid_x + 1
        slot_w = plot_w / max(1, n_grid_x)
        # 留 4px 余量，避免标签贴边
        max_label_w = max(20, int(slot_w - 4))
        for i in range(n_xticks):
            x = pad_l + plot_w * i / n_grid_x
            t_at = t_min + span * i / n_grid_x
            try:
                label = _dt.datetime.fromtimestamp(t_at).strftime(fmt)
            except (ValueError, OSError):
                label = '--:--:--'
            if axis_font.measure(label) > max_label_w:
                label = self._truncate_text(label, max_label_w, axis_font)
            if i == 0:
                anc = 'nw'
            elif i == n_grid_x:
                anc = 'ne'
            else:
                anc = 'n'
            c.create_text(x, pad_t + plot_h + 4, text=label,
                          fill=muted, anchor=anc,
                          font=axis_font)

        c.create_rectangle(pad_l, pad_t, pad_l + plot_w, pad_t + plot_h,
                           outline=border, width=1)

        # 内联坐标变换：避免每点函数调用开销（原 _t2x/_v2y 闭包）
        t_span = t_max - t_min
        y_span = y_hi - y_lo
        inv_t = 1.0 / t_span if t_span else 0.0
        inv_y = 1.0 / y_span if y_span else 0.0
        _pl = pad_l
        _pt = pad_t
        _pw = plot_w
        _ph = plot_h
        _tmn = t_min
        _yhi = y_hi

        # 画线前对每条 series 降采样：阈值 = 像素宽 × 2（每像素 2 个点足够平滑）
        # LTTB 保形状，10w 点降到 ~2*plot_w 个点，绘制 create_line 性能从 O(n) 降到 O(plot_w)
        max_pts = max(200, int(plot_w * 2))

        for v, pts in series:
            n = len(pts)
            if n >= 2:
                draw_pts = _downsample_lttb(pts, max_pts) if n > max_pts else pts
                # array.array 批量构建坐标，比 list.extend 在大列表上更快
                arr = _farray('d')
                ap = arr.append
                for ts, val in draw_pts:
                    ap(_pl + _pw * (ts - _tmn) * inv_t)
                    ap(_pt + _ph * (_yhi - val) * inv_y)
                c.create_line(*arr, fill=v['color'], width=1.5)
            if pts:
                lx = _pl + _pw * (pts[-1][0] - _tmn) * inv_t
                ly = _pt + _ph * (_yhi - pts[-1][1]) * inv_y
                # 端点圆点限制在绘图区内（小窗口下不溢出）
                lx = max(_pl, min(_pl + _pw, lx))
                ly = max(_pt, min(_pt + _ph, ly))
                c.create_oval(lx - 3, ly - 3, lx + 3, ly + 3,
                              fill=v['color'], outline='')

        # X 轴标签（绘图区下方，紧贴 X 刻度文字之下）
        if show_x_label:
            xlbl = x_label
            if axis_font.measure(xlbl) > plot_w:
                xlbl = self._truncate_text(xlbl, plot_w, axis_font)
            c.create_text(pad_l + plot_w / 2, pad_t + plot_h + axis_h + 4,
                          text=xlbl, fill=muted, anchor='n',
                          font=axis_font)

        # 图例：show_legend=False 时完全跳过
        if show_legend:
            legend_x = pad_l + plot_w + 10
            # 图例可用文字宽度（基于实际 legend_text_w，超过则字符级截断）
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
                buf = _data[v['name']]
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

        # ---- 显示选项：最值标注 / 末值点 / 均值线 / 数据表格 ----
        # 缓存每条 series 的统计值供 hover 也复用
        stats_cache = []
        for v, pts in series:
            if not pts:
                stats_cache.append(None)
                continue
            vals = [p[1] for p in pts]
            stats_cache.append({
                'name': v['name'], 'color': v['color'],
                'pts': pts,
                'min_v': min(vals), 'max_v': max(vals),
                'last_v': vals[-1], 'last_ts': pts[-1][0],
                'avg_v': sum(vals) / len(vals),
                'min_pt': pts[vals.index(min(vals))],
                'max_pt': pts[vals.index(max(vals))],
            })
        plot['_stats_cache'] = stats_cache

        # 均值水平线：在绘图区内画一条虚线 + 右侧标注
        if plot.get('show_avg') and stats_cache:
            for st in stats_cache:
                if not st:
                    continue
                y_avg = _pt + _ph * (_yhi - st['avg_v']) * inv_y
                y_avg = max(_pt, min(_pt + _ph, y_avg))
                c.create_line(_pl, y_avg, _pl + _pw, y_avg,
                              fill=st['color'], dash=(4, 4))
                lbl = f'均值 {st["name"]}: {st["avg_v"]:g}'
                if legend_font.measure(lbl) > _pw - 4:
                    lbl = self._truncate_text(lbl, _pw - 4, legend_font)
                c.create_text(_pl + 4, y_avg - 2, text=lbl,
                              fill=st['color'], anchor='nw',
                              font=small_font)

        # 最大值/最小值/末值 标注点
        def _draw_marker(pt, text, color, anchor_side='ne'):
            if not pt:
                return
            mx = _pl + _pw * (pt[0] - _tmn) * inv_t
            my = _pt + _ph * (_yhi - pt[1]) * inv_y
            mx = max(_pl, min(_pl + _pw, mx))
            my = max(_pt, min(_pt + _ph, my))
            c.create_oval(mx - 4, my - 4, mx + 4, my + 4,
                          fill=color, outline=t.get('bg', ''))
            c.create_text(mx + 6, my - 6, text=text,
                          fill=color, anchor=anchor_side,
                          font=small_font)
        for st in stats_cache:
            if not st:
                continue
            if plot.get('show_max'):
                _draw_marker(st['max_pt'],
                             f'最大 {st["name"]}: {st["max_v"]:g}',
                             st['color'])
            if plot.get('show_min'):
                _draw_marker(st['min_pt'],
                             f'最小 {st["name"]}: {st["min_v"]:g}',
                             st['color'], anchor_side='sw')
            if plot.get('show_last'):
                _draw_marker((st['last_ts'], st['last_v']),
                             f'{st["name"]}: {st["last_v"]:g}',
                             st['color'], anchor_side='n')

        # 数据表格 overlay：右上角紧凑表格显示 每变量的 当前/最小/最大/均值
        if plot.get('show_table') and stats_cache:
            cols = ['变量', '当前', '最小', '最大', '均值']
            col_w = [legend_font.measure(c) + 16 for c in cols]
            rows_data = []
            for st in stats_cache:
                if not st:
                    continue
                row = [st['name'],
                       f'{st["last_v"]:g}',
                       f'{st["min_v"]:g}',
                       f'{st["max_v"]:g}',
                       f'{st["avg_v"]:g}']
                rows_data.append((st['color'], row))
                for i, cell in enumerate(row):
                    col_w[i] = max(col_w[i],
                                    legend_font.measure(cell) + 8)
            if rows_data:
                cell_h = max(16, legend_font.metrics('linespace') + 4)
                tbl_w = sum(col_w) + 4
                tbl_h = cell_h * (len(rows_data) + 1) + 4
                # 右上角，留 4px 内边距，超出绘图区时缩到画布右上角
                tx0 = _pl + _pw - tbl_w - 4
                ty0 = _pt + 4
                if tx0 < _pl + 4:
                    tx0 = _pl + 4
                if ty0 < _pt:
                    ty0 = _pt
                # 半透明背景：用 panel_bg 实色覆盖（tk canvas 无 alpha）
                c.create_rectangle(tx0, ty0, tx0 + tbl_w, ty0 + tbl_h,
                                   fill=t.get('panel_bg', '#252526'),
                                   stipple='gray50', outline=border)
                # 表头
                cx = tx0 + 2
                for i, col_text in enumerate(cols):
                    c.create_text(cx + col_w[i] / 2,
                                  ty0 + cell_h / 2 + 2,
                                  text=col_text, fill=muted,
                                  anchor='center', font=small_font)
                    cx += col_w[i]
                # 数据行
                for ri, (color, row) in enumerate(rows_data):
                    ry = ty0 + cell_h * (ri + 1)
                    cx = tx0 + 2
                    # 色块标识
                    c.create_rectangle(cx, ry + 2, cx + 8,
                                       ry + cell_h - 2,
                                       fill=color, outline='')
                    for i, cell in enumerate(row):
                        anc = 'w' if i == 0 else 'e'
                        c.create_text(cx + (8 if i == 0 else col_w[i] - 4),
                                       ry + cell_h / 2 + 2,
                                       text=cell, fill=fg,
                                       anchor=anc, font=small_font)
                        cx += col_w[i]
                # 记录表格区域，悬停时跳过
                plot['_table_rect'] = (tx0, ty0, tx0 + tbl_w,
                                        ty0 + tbl_h)
            else:
                plot['_table_rect'] = None
        else:
            plot['_table_rect'] = None


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
            # 曲线/坐标轴/网格/标签是 create_line/create_text 一次性 baked 的颜色，
            # 不会跟随主题自动更新。必须清掉每个 plot 的 _draw_cache（否则下次
            # _draw_curves 因 sig 命中提前 return，画面停在旧主题），并强制下一帧重绘。
            for plot in self._plots:
                cache = plot.get('_draw_cache')
                if isinstance(cache, dict):
                    cache.clear()
            self._force_redraw()
            try:
                self.after(0, self._draw_curves)
            except Exception:
                pass
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
        # 销毁悬停 tooltip Toplevel
        if self._hover_tip is not None:
            try:
                self._hover_tip.destroy()
            except Exception:
                pass
            self._hover_tip = None
            self._hover_canvas = None
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
            'show_max': p.get('show_max', False),
            'show_min': p.get('show_min', False),
            'show_avg': p.get('show_avg', False),
            'show_last': p.get('show_last', False),
            'show_table': p.get('show_table', False),
            'show_hover': p.get('show_hover', True),
        }


class MonitorWindow(tk.Toplevel):
    """独立监视窗口：Toplevel 包装 MonitorPanel。

    子类覆盖 PANEL_CLS 提供特定 Panel（含 DEFAULT_VARS）。
    默认占半屏大小，工具栏有全屏/还原按钮。
    """

    PANEL_CLS = MonitorPanel
    TITLE = '数据监视'
    GEOMETRY = ''   # 空 = 动态计算半屏
    # 配置前缀：子类覆盖为 'motor'/'bms'/'imu'，用于持久化视图状态/几何/plots
    # 为 None 时不读写 config（避免无关子类污染配置）
    CONFIG_PREFIX = None

    def __init__(self, parent, variables=None, on_change=None, plots=None):
        # 先 withdraw 隐藏，避免 Toplevel 默认白背景在内容就绪前闪现
        # （父类 __init__ 之后立刻 withdraw，等到 panel+menu 全部就绪再 deiconify）
        super().__init__(parent)
        try:
            self.withdraw()
        except Exception:
            pass
        # 提前设置深色背景，避免哪怕 withdraw 失败时仍是白底
        try:
            t_init = _default_theme()
            self.configure(bg=t_init.get('bg', '#1e1e1e'))
        except Exception:
            pass

        self.title(self.TITLE)
        self._is_fullscreen = False
        # 优先用持久化的几何，没有再动态计算半屏
        saved_geo = ''
        if config is not None and self.CONFIG_PREFIX:
            try:
                saved_geo = config.load().get(
                    f'{self.CONFIG_PREFIX}_monitor_geometry', '')
            except Exception:
                saved_geo = ''
        if saved_geo:
            try:
                self.geometry(saved_geo)
            except Exception:
                pass
        elif not self.GEOMETRY:
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
        # 会禁用最大化按钮和拖到顶部的最大化手势。改用 lift+focus 保持层级
        # （在内容就绪后 lift+focus，见下方 deiconify 之后）

        self.panel = self.PANEL_CLS(self, variables=variables,
                                      on_change=on_change, plots=plots)
        self.panel.pack(fill='both', expand=True)
        self._build_menubar()
        # 应用持久化的视图状态（工具栏/状态/变量列表显隐、模式、悬停）
        self._load_view_state()

        # 内容全部就绪后再显示窗口，避免初始白屏闪烁
        try:
            self.update_idletasks()
            self.deiconify()
            self.lift(parent)
            self.focus_force()
        except Exception:
            pass

        self.protocol('WM_DELETE_WINDOW', self._on_close)
        # Escape 退出全屏
        self.bind('<Escape>', lambda _e: self._exit_fullscreen())

    def _build_menubar(self):
        """构建菜单栏：打开/视图/工具/帮助。

        菜单项直接转发到 self.panel 的对应方法，避免与工具栏按钮重复实现。
        工具菜单覆盖工具栏全部功能：变量 / 图表 / 模式 / 悬停提示 / 清空 /
        加载日志 / 统计 / 导出 CSV / 全屏。
        """
        menubar = tk.Menu(self)
        # File / 打开
        m_file = tk.Menu(menubar, tearoff=False)
        m_file.add_command(label='打开日志文件…', accelerator='Ctrl+O',
                           command=self.panel._load_history_log)
        m_file.add_separator()
        m_file.add_command(label='导出 CSV…', accelerator='Ctrl+S',
                           command=self.panel._export_csv)
        m_file.add_separator()
        m_file.add_command(label='关闭窗口', accelerator='Ctrl+W',
                           command=self._on_close)
        menubar.add_cascade(label='打开', menu=m_file)

        # View / 视图
        m_view = tk.Menu(menubar, tearoff=False)
        self._view_toolbar_var = tk.BooleanVar(value=True)
        self._view_status_var = tk.BooleanVar(value=True)
        self._view_varlist_var = tk.BooleanVar(value=True)
        m_view.add_checkbutton(
            label='工具栏', variable=self._view_toolbar_var,
            command=self._toggle_toolbar)
        m_view.add_checkbutton(
            label='状态文本', variable=self._view_status_var,
            command=self._toggle_status)
        m_view.add_checkbutton(
            label='变量列表', variable=self._view_varlist_var,
            command=self._toggle_varlist)
        m_view.add_separator()
        # 显示模式：与工具栏 combobox 共用 panel._mode_var，菜单切换同步工具栏
        m_mode_sub = tk.Menu(m_view, tearoff=False)
        m_mode_sub.add_radiobutton(
            label='曲线', variable=self.panel._mode_var, value='曲线',
            command=self.panel._on_mode_change)
        m_mode_sub.add_radiobutton(
            label='数据', variable=self.panel._mode_var, value='数据',
            command=self.panel._on_mode_change)
        m_view.add_cascade(label='显示模式', menu=m_mode_sub)
        # 悬停提示模式：与工具栏配置菜单共用 panel._hover_mode
        m_hover_sub = tk.Menu(m_view, tearoff=False)
        m_hover_sub.add_radiobutton(
            label='仅当前曲线',
            variable=self.panel._hover_mode, value='current')
        m_hover_sub.add_radiobutton(
            label='显示全部曲线',
            variable=self.panel._hover_mode, value='all')
        m_view.add_cascade(label='悬停提示', menu=m_hover_sub)
        m_view.add_separator()
        m_view.add_command(label='全屏切换', accelerator='F11',
                           command=self.panel._toggle_fullscreen)
        m_view.add_command(label='清空数据', command=self.panel._clear_data)
        menubar.add_cascade(label='视图', menu=m_view)

        # Tools / 工具（覆盖工具栏全部功能）
        m_tools = tk.Menu(menubar, tearoff=False)
        m_tools.add_command(label='添加变量…', accelerator='Ctrl+N',
                            command=self.panel._add_var)
        m_tools.add_command(label='编辑选中变量…',
                            command=self.panel._edit_var)
        m_tools.add_command(label='删除选中变量',
                            command=self.panel._del_var)
        m_tools.add_separator()
        m_tools.add_command(label='添加图表…', command=self.panel._add_plot)
        m_tools.add_command(label='编辑图表…', command=self.panel._edit_plot)
        m_tools.add_command(label='删除图表…', command=self.panel._del_plot)
        m_tools.add_separator()
        m_tools.add_command(label='清空数据', command=self.panel._clear_data)
        m_tools.add_command(label='加载日志…',
                            command=self.panel._load_history_log)
        m_tools.add_command(label='统计信息…',
                            command=self.panel._show_statistics)
        m_tools.add_command(label='导出 CSV…', accelerator='Ctrl+S',
                            command=self.panel._export_csv)
        m_tools.add_separator()
        m_tools.add_command(label='全屏切换', accelerator='F11',
                            command=self.panel._toggle_fullscreen)
        menubar.add_cascade(label='工具', menu=m_tools)

        # Help / 帮助
        m_help = tk.Menu(menubar, tearoff=False)
        m_help.add_command(label='关于', command=self._show_about)
        menubar.add_cascade(label='帮助', menu=m_help)

        try:
            self.configure(menu=menubar)
        except Exception:
            pass
        self._menubar = menubar
        # 应用菜单主题（深色 / 浅色）
        try:
            if themes is not None:
                t = themes.THEMES[themes.resolve(themes.load_pref())]
                themes.apply_menu_theme(menubar, t)
        except Exception:
            pass
        # 快捷键
        self.bind('<Control-o>', lambda _e: self.panel._load_history_log())
        self.bind('<Control-s>', lambda _e: self.panel._export_csv())
        self.bind('<Control-w>', lambda _e: self._on_close())
        self.bind('<Control-n>', lambda _e: self.panel._add_var())
        self.bind('<F11>', lambda _e: self.panel._toggle_fullscreen())

    def _toggle_toolbar(self):
        try:
            if self._view_toolbar_var.get():
                self.panel._toolbar.pack(fill='x',
                                          before=self.panel._content_pane)
            else:
                self.panel._toolbar.pack_forget()
        except Exception:
            pass

    def _toggle_status(self):
        try:
            if self._view_status_var.get():
                self.panel._status_lbl.pack(side='right')
            else:
                self.panel._status_lbl.pack_forget()
        except Exception:
            pass

    def _toggle_varlist(self):
        """显示/隐藏左侧变量列表 frame。"""
        try:
            pane = self.panel._content_pane
            left = self.panel._var_list_frame
            if self._view_varlist_var.get():
                # 重新加入 PanedWindow（保留原 weight=0 + minsize=280）
                if left not in pane.winfo_children():
                    pane.add(left, weight=0)
                    try:
                        pane.paneconfigure(left, minsize=280)
                    except Exception:
                        pass
            else:
                pane.forget(left)
        except Exception:
            pass

    def _load_view_state(self):
        """从 config 读取视图状态并应用。

        读 monitor_view_*（toolbar/status/varlist 显隐）+ monitor_mode +
        monitor_hover_mode。所有 motor/bms/imu 共用一组视图键——同一用户
        在电机里调的视图，切到 BMS 也保持一致，符合"上次设置"的预期。
        """
        if config is None:
            return
        try:
            cfg = config.load()
        except Exception:
            return
        try:
            if cfg.get('monitor_view_toolbar', True):
                self._view_toolbar_var.set(True)
            else:
                self._view_toolbar_var.set(False)
                self._toggle_toolbar()
            if cfg.get('monitor_view_status', True):
                self._view_status_var.set(True)
            else:
                self._view_status_var.set(False)
                self._toggle_status()
            if cfg.get('monitor_view_varlist', True):
                self._view_varlist_var.set(True)
            else:
                self._view_varlist_var.set(False)
                self._toggle_varlist()
            mode = cfg.get('monitor_mode', '曲线')
            if mode in ('曲线', '数据'):
                self.panel._mode_var.set(mode)
                try:
                    self.panel._on_mode_change()
                except Exception:
                    pass
            hover = cfg.get('monitor_hover_mode', 'current')
            if hover in ('current', 'all'):
                self.panel._hover_mode.set(hover)
        except Exception:
            pass

    def _save_state(self):
        """关闭前持久化视图状态 + plots + variables + geometry。

        视图键共用一组（monitor_view_*），按 CONFIG_PREFIX 存几何/plots/vars。
        """
        if config is None:
            return
        try:
            cfg = config.load()
        except Exception:
            cfg = {}
        try:
            cfg['monitor_view_toolbar'] = bool(
                self._view_toolbar_var.get())
            cfg['monitor_view_status'] = bool(
                self._view_status_var.get())
            cfg['monitor_view_varlist'] = bool(
                self._view_varlist_var.get())
            cfg['monitor_mode'] = self.panel._mode_var.get() or '曲线'
            cfg['monitor_hover_mode'] = (
                self.panel._hover_mode.get() or 'current')
            if self.CONFIG_PREFIX:
                try:
                    cfg[f'{self.CONFIG_PREFIX}_monitor_geometry'] = (
                        self.geometry())
                except Exception:
                    pass
                try:
                    cfg[f'{self.CONFIG_PREFIX}_vars'] = (
                        list(self.panel.variables))
                except Exception:
                    pass
                try:
                    cfg[f'{self.CONFIG_PREFIX}_plots'] = (
                        list(self.panel.plots))
                except Exception:
                    pass
        except Exception:
            pass
        try:
            config.save(cfg)
        except Exception:
            pass

    def _show_about(self):
        dlg = tk.Toplevel(self)
        dlg.title('关于')
        try:
            dlg.lift(self)
            dlg.focus_force()
        except Exception:
            pass
        dlg.resizable(True, True)
        f = ttk.Frame(dlg, padding=20)
        f.pack(fill='both', expand=True)
        ttk.Label(f, text='串口调试助手 - 数据监视',
                  font=_font('bold')).pack(pady=(0, 6))
        ttk.Label(f, text='CH343 实时收发 / AT 命令管理 / 日志存储').pack()
        ttk.Label(f, text='曲线绘制 + 变量监视 + 历史日志回放').pack(pady=4)
        ttk.Button(f, text='关闭', command=dlg.destroy).pack(pady=(10, 0))
        _size_dialog_to_content(dlg, min_w=360, min_h=180, parent=self)

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
            if getattr(self, '_menubar', None) is not None:
                themes.apply_menu_theme(self._menubar, t)
        except Exception:
            pass

    def _on_close(self):
        try:
            self._save_state()
        except Exception:
            pass
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
        # 列宽随 treeview 宽度自动按比例分配（label:tag:pos = 3:1:1）
        # 用户拖拽后新比例会作为下次分配的权重
        self._var_tree.column('label', width=180, stretch=False,
                              anchor='w', minwidth=80)
        self._var_tree.column('tag', width=60, anchor='center',
                              stretch=False, minwidth=40)
        self._var_tree.column('pos', width=60, anchor='center',
                              stretch=False, minwidth=40)
        _enable_tree_column_stretch(
            self._var_tree,
            ratios={'label': 3, 'tag': 1, 'pos': 1},
            min_widths={'label': 80, 'tag': 40, 'pos': 40})

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
    CONFIG_PREFIX = 'motor'


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
    CONFIG_PREFIX = 'bms'


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
    CONFIG_PREFIX = 'imu'
