"""Qt + pyqtgraph 数据监视窗口（Stage 1：多 plot 子图 + 配置持久化）。

独立进程启动，与 tkinter 主应用隔离，避免双事件循环冲突。
入口：python -m serial_debug.ui.qt_monitor_window [日志路径]
      或主程序设环境变量 SERIAL_DEBUG_QT_PILOT=1 启动子进程

Stage 1（2026-07-31 启动）：
  - 多 plot 子图架构（QScrollArea + QVBoxLayout，每 plot 一个 PlotWidget）
  - 添加/编辑/删除 plot 对话框（title / x_label / y_label / vars 多选 /
    show_max / show_min / show_avg / show_last / show_table / show_hover）
  - 配置持久化（plots + 窗口几何，存 infra/config：qt_monitor_* 前缀）

后续 Stage：
  2. 变量列表侧栏 / 显示模式（曲线/数据）
  3. 统计对话框 / CSV 导出
  4. 主程序迁 Qt
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
from datetime import timedelta, timezone

# 延迟导入 PySide6/pyqtgraph：仅 Qt 窗口实际启动时才加载
# 让旧 tkinter 主程序不强制依赖 Qt（即便没装 Qt 也能跑 tkinter 路径）
# 复用日志解析器（时间戳提取 + motor/BMS/IMU 三类 Parser）。
# 这些纯逻辑从原 tkinter `ui/monitor_window.py` 抽出到 `core/` 下，
# 不依赖 tkinter / PySide6，便于在无 GUI 环境运行。
from serial_debug.core.log_parser import extract_log_ts as _extract_log_ts
from serial_debug.core.panel_vars import (
    BmsMonitorPanel,
    ImuMonitorPanel,
    MotorMonitorPanel,
)
from serial_debug.infra import themes
from serial_debug.infra.error_log import log_exc

# 日志时间戳统一按北京时间（UTC+8）显示：不依赖 tzdata/系统时区数据库，
# 避免 PyInstaller 打包版无 tzdata 时 fromtimestamp fallback 到 UTC。
# 见 _make_time_axis / _draw_hover_label 等 fromtimestamp 调用。
BEIJING_TZ = timezone(timedelta(hours=8))


# ---- 静态变量注册表（从 Panel.DEFAULT_VARS 派生，加前缀避免冲突）---------
def _build_var_registry():
    """构建静态变量注册表，供 plot 对话框的"包含变量"多选列表使用。

    即便用户没加载日志，也能预先配置 plot 的变量。
    """
    out = []
    for prefix, cls in (('motor', MotorMonitorPanel),
                       ('bms', BmsMonitorPanel),
                       ('imu', ImuMonitorPanel)):
        for v in cls.DEFAULT_VARS:
            out.append({
                'name': f'{prefix}.{v["name"]}',
                'label': v.get('label', v['name']),
            })
    return out


_ALL_VARS = _build_var_registry()


# ---- 自定义解析器状态（用户在"解析器配置"对话框贴 Python 代码）---------
_CUSTOM_PARSER_STATE = {
    'mode': 'built-in',   # built-in / custom / both
    'code': '',           # 用户原始 Python 源码（用于 dialog 回填 + 持久化）
    'sample_log': '',     # 用户贴的样本日志（dialog 回填 + 持久化）
    'source_code': '',    # 设备源码（C/C++/Python/Java/Rust/JS）用于自动推导
    'source_language': 'auto',  # auto / c / cpp / python / java / rust / html / js
    'func': None,         # exec 后的 parse_line(line) 函数（运行时用，不持久化）
    'discovered_vars': [],
    'validation': {       # 加载/连串口时格式校验配置
        'enabled': True,
        'min_parsed_count': 10,
        'sample_lines': 200,
    },
}

# 内置解析器模板（dialog 首次打开时预填）
_PARSER_TEMPLATE_CODE = """\
# 解析一行日志，yield (ts, name, value) 三元组
# ts: float 秒（毫秒时间戳 / 1000）
# name: 变量名（字符串，建议带前缀如 motor.rpm）
# value: float
# 无匹配行可不 yield 或 return None
#
# 本模板示例：解析 motor 3wd1t_1 行的 rpm / vbus 字段
def parse_line(line):
    if '3wd1t_1' not in line:
        return  # 非目标行
    # 提取时间戳（毫秒 → 秒）
    ts_part = line.split()[0]
    try:
        ts = int(ts_part) / 1000.0
    except ValueError:
        return
    # 按 | 切组、# 切子组
    try:
        main = line.split('model ')[1]
    except IndexError:
        return
    blocks = []
    for blk_str in main.split('|'):
        blocks.append([g.split() for g in blk_str.split('#')])
    # 从 blocks 取值（block_idx=0, group_idx=0, pos=0/1）
    try:
        yield ts, 'motor.rpm', float(blocks[0][0][0])
        yield ts, 'motor.vbus', float(blocks[0][0][1])
    except (IndexError, ValueError):
        pass
"""


def _pick_value(blocks, v: dict):
    """从 blocks 中按 block_idx / group_idx / pos_in_group 取一个值。"""
    try:
        bi = v.get('block_idx', 0)
        gi = v.get('group_idx', 0)
        pi = v.get('pos_in_group', 0)
        if bi < len(blocks) and gi < len(blocks[bi]) and pi < len(blocks[bi][gi]):
            return float(blocks[bi][gi][pi])
    except (ValueError, IndexError, TypeError):
        return None
    return None


def _iter_builtin_panel_vars(line: str, ts: float):
    """内置解析器：motor (3wd1t / STD_DEBUG) + BMS / BAT + IMU。

    从 `monitor_window.py` 的三个 Panel 类调 `_parse_tagged_line` +
    `DEFAULT_VARS` 取值，加 `motor.` / `bms.` / `imu.` 前缀 yield。
    """
    # motor: 3wd1t_1 / 3wd1t_2 / Lxx_STD_DEBUG
    for tag in ('3wd1t_1', '3wd1t_2'):
        blocks = MotorMonitorPanel._parse_tagged_line(line, tag)
        if not blocks:
            continue
        for v in MotorMonitorPanel.DEFAULT_VARS:
            if v.get('log_tag') != tag:
                continue
            val = _pick_value(blocks, v)
            if val is not None:
                yield ts, f'motor.{v["name"]}', val
    # STD_DEBUG：tag 形如 L30_STD_DEBUG，正则匹配行中 Lxx
    m = MotorMonitorPanel._STD_DEBUG_MARKER_RE.search(line)
    if m:
        l_num = m.group(1)
        tag = f'{l_num}_STD_DEBUG'
        blocks = MotorMonitorPanel._parse_tagged_line(line, tag)
        if blocks:
            for v in MotorMonitorPanel.DEFAULT_VARS:
                if v.get('log_tag') != tag:
                    continue
                val = _pick_value(blocks, v)
                if val is not None:
                    yield ts, f'motor.{v["name"]}', val
    # BMS / BAT
    for tag in ('BMS', 'BAT'):
        blocks = BmsMonitorPanel._parse_tagged_line(line, tag)
        if not blocks:
            continue
        for v in BmsMonitorPanel.DEFAULT_VARS:
            if v.get('log_tag') != tag:
                continue
            val = _pick_value(blocks, v)
            if val is not None:
                yield ts, f'bms.{v["name"]}', val
    # IMU
    blocks = ImuMonitorPanel._parse_tagged_line(line, 'Imu_data')
    if blocks:
        for v in ImuMonitorPanel.DEFAULT_VARS:
            val = _pick_value(blocks, v)
            if val is not None:
                yield ts, f'imu.{v["name"]}', val


def _iter_panel_vars(line: str, ts_fallback: float | None = None):
    """从一行日志中提取所有 (ts, name, value) 三元组。

    按 `_CUSTOM_PARSER_STATE['mode']` 分发：
      - ``built-in``：仅内置 motor/BMS/IMU 解析器
      - ``custom``：仅用户自定义 `parse_line(line)` 函数
      - ``both``：两者都跑，yield 项取并集（同 name 重复时取最后一次）

    :param ts_fallback: 行内提取不到时间戳时用此值。实时串口 feed 的
        设备原始行（如 ``783587 I model ...`` 只有 6 位、``OK,0%`` 无时间戳）
        提取不到 ts，若不传 fallback 会直接 return 不 yield，曲线不生成。
        日志回放路径不传 fallback（行内必有时间戳，缺失说明格式异常）。
    """
    ts = _extract_log_ts(line)
    if ts is None:
        if ts_fallback is None:
            return
        ts = ts_fallback

    mode = _CUSTOM_PARSER_STATE.get('mode', 'built-in')
    seen_names: dict[str, tuple] = {}  # name -> (ts, name, val)（dedup 用）

    if mode in ('built-in', 'both'):
        for item in _iter_builtin_panel_vars(line, ts):
            if len(item) == 3:
                seen_names[item[1]] = tuple(item)
    if mode in ('custom', 'both') and _CUSTOM_PARSER_STATE.get('func') is not None:
        try:
            result = _CUSTOM_PARSER_STATE['func'](line)
            if result is not None:
                for item in result:
                    if item and len(item) == 3:
                        seen_names[item[1]] = tuple(item)
        except Exception:
            pass  # 解析器异常吞掉，不让一行坏数据拖崩

    for _, item in seen_names.items():
        yield item


def _default_plot() -> dict:
    return {
        'title': '数据曲线',
        'x_label': '时间 (s)',
        'y_label': '值',
        'vars': [],     # 空 = 显示全部当前已加载变量
        # 图例 checkbox 可见性开关：{var_name: bool}。缺失键 = True（可见）
        # 用户在 plot 图例前的 checkbox 上勾选/取消来快速控制每条曲线
        'var_visibility': {},
        'show_max': False,
        'show_min': False,
        'show_avg': False,
        'show_last': False,
        'show_table': False,
        'show_hover': True,
        'legend_pos': 'top-right',    # top-left / top-right / bottom-left / bottom-right / hidden
        'legend_font_size': 9,        # 图例字号（9=默认，调大调小）
        'legend_auto_avoid': True,    # 自动避让曲线密集区
        'hover_mode': 'current',      # current / all / stats / all_stats
        'hover_show_max': True,       # 统计：最大值
        'hover_show_min': True,       # 统计：最小值
        'hover_show_avg': True,       # 统计：均值
        'hover_show_last': True,      # 统计：末值
    }


def _normalize_plot(p: dict) -> dict:
    d = _default_plot()
    if not p:
        return d
    d.update({k: p[k] for k in d if k in p})
    d['vars'] = list(d.get('vars', []))
    # var_visibility 必须是 dict（兼容旧 config 缺失此字段）
    vv = d.get('var_visibility')
    if not isinstance(vv, dict):
        d['var_visibility'] = {}
    else:
        d['var_visibility'] = {str(k): bool(v) for k, v in vv.items()}
    return d


def _make_time_axis(theme):
    """返回自定义 AxisItem：x 轴刻度格式化为 HH:MM:SS 时间。

    x 数据是 unix epoch 秒；按可见范围自动选精度：
    < 5s 显示 ms；< 1h 显示 HH:MM:SS；> 1h 显示 MM-DD HH:MM。
    """
    import datetime as _dt

    import pyqtgraph as pg

    class _TimeAxis(pg.AxisItem):
        def tickStrings(self, values, scale, spacing):
            out = []
            # 根据相邻 tick 差值（spacing 秒）选精度
            if spacing < 5:
                fmt = '%H:%M:%S'
            elif spacing < 3600:
                fmt = '%H:%M:%S'
            elif spacing < 86400:
                fmt = '%m-%d %H:%M'
            else:
                fmt = '%m-%d'
            for v in values:
                try:
                    out.append(_dt.datetime.fromtimestamp(
                        v, tz=BEIJING_TZ).strftime(fmt))
                except (OSError, ValueError, OverflowError):
                    out.append(f'{v:.0f}')
            return out

    ax = _TimeAxis(orientation='bottom')
    try:
        ax.setPen(theme.get('muted', '#cccccc'))
        ax.setTextPen(theme.get('fg', '#cccccc'))
    except Exception:
        pass
    return ax


class _QtPlotDialog:
    """配置图表对话框（延迟创建真正的 QDialog，避免模块导入即 init Qt）。

    字段与 tkinter _PlotDialog 一一对应，方便配置互通。
    """

    def __init__(self, parent, all_vars, initial=None, title='配置图表'):
        self.parent = parent
        self.all_vars = all_vars
        self.initial = initial or {}
        self.title = title
        self.result = None
        self._dlg = None
        self._var_items: dict[str, object] = {}
        self._checks: dict[str, object] = {}

    def _build(self):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import (
            QCheckBox,
            QComboBox,
            QDialog,
            QDialogButtonBox,
            QFormLayout,
            QGroupBox,
            QHBoxLayout,
            QLabel,
            QLineEdit,
            QListWidget,
            QListWidgetItem,
            QPushButton,
            QVBoxLayout,
        )

        dlg = QDialog(self.parent)
        dlg.setWindowTitle(self.title)
        dlg.resize(640, 560)
        dlg.setMinimumSize(480, 360)

        v = QVBoxLayout(dlg)
        v.setContentsMargins(12, 12, 12, 12)
        v.setSpacing(8)

        # 表单区：标题 / X 轴 / Y 轴
        form = QFormLayout()
        form.setSpacing(6)
        self._title_edit = QLineEdit(self.initial.get('title', '数据曲线'))
        self._x_edit = QLineEdit(self.initial.get('x_label', '时间 (s)'))
        self._y_edit = QLineEdit(self.initial.get('y_label', '值'))
        form.addRow('标题', self._title_edit)
        form.addRow('X 轴标签', self._x_edit)
        form.addRow('Y 轴标签', self._y_edit)
        v.addLayout(form)

        # 显示选项
        opts_box = QGroupBox('显示选项')
        opts = QHBoxLayout(opts_box)
        opts.setContentsMargins(8, 8, 8, 8)
        self._show_checks = {}
        init = self.initial
        for key, label, default in (
            ('show_max', '最大值', False),
            ('show_min', '最小值', False),
            ('show_avg', '均值线', False),
            ('show_last', '末值点', False),
            ('show_table', '数据表格', False),
            ('show_hover', '悬停提示', True),
        ):
            cb = QCheckBox(label)
            cb.setChecked(bool(init.get(key, default)))
            opts.addWidget(cb)
            self._show_checks[key] = cb
        opts.addStretch(1)
        v.addWidget(opts_box)

        # 图例位置 + 悬停模式
        extra_box = QGroupBox('图例与悬停')
        extra_l = QVBoxLayout(extra_box)
        extra_l.setContentsMargins(8, 8, 8, 8)
        extra_l.setSpacing(6)
        # 第一行：图例位置 + 字号 + 自动避让
        row1 = QHBoxLayout()
        row1.addWidget(QLabel('图例位置:'))
        self._legend_combo = QComboBox()
        legend_items = [('top-right', '右上角'), ('top-left', '左上角'),
                        ('bottom-right', '右下角'), ('bottom-left', '左下角'),
                        ('hidden', '隐藏')]
        for val, label in legend_items:
            self._legend_combo.addItem(label, val)
        cur_legend = init.get('legend_pos', 'top-right')
        for i, (val, _) in enumerate(legend_items):
            if val == cur_legend:
                self._legend_combo.setCurrentIndex(i)
                break
        row1.addWidget(self._legend_combo)
        row1.addSpacing(12)
        row1.addWidget(QLabel('字号:'))
        self._legend_font = QComboBox()
        for sz in (7, 8, 9, 10, 11, 12, 14):
            self._legend_font.addItem(str(sz), sz)
        cur_font = int(init.get('legend_font_size', 9))
        for i in range(self._legend_font.count()):
            if self._legend_font.itemData(i) == cur_font:
                self._legend_font.setCurrentIndex(i)
                break
        row1.addWidget(self._legend_font)
        row1.addSpacing(12)
        self._legend_avoid_cb = QCheckBox('自动避让曲线')
        self._legend_avoid_cb.setChecked(bool(init.get('legend_auto_avoid', True)))
        row1.addWidget(self._legend_avoid_cb)
        row1.addStretch(1)
        extra_l.addLayout(row1)
        # 第二行：悬停模式 + 统计项
        row2 = QHBoxLayout()
        row2.addWidget(QLabel('悬停显示:'))
        self._hover_combo = QComboBox()
        self._hover_combo.addItem('仅当前曲线', 'current')
        self._hover_combo.addItem('全部曲线', 'all')
        self._hover_combo.addItem('当前曲线 + 统计', 'stats')
        self._hover_combo.addItem('全部曲线 + 统计', 'all_stats')
        cur_hover = init.get('hover_mode', 'current')
        for i in range(self._hover_combo.count()):
            if self._hover_combo.itemData(i) == cur_hover:
                self._hover_combo.setCurrentIndex(i)
                break
        row2.addWidget(self._hover_combo)
        row2.addSpacing(12)
        row2.addWidget(QLabel('统计项:'))
        self._hover_stat_checks = {}
        for key, label, default in (
            ('hover_show_max', '最大值', True),
            ('hover_show_min', '最小值', True),
            ('hover_show_avg', '均值', True),
            ('hover_show_last', '末值', True),
        ):
            cb = QCheckBox(label)
            cb.setChecked(bool(init.get(key, default)))
            row2.addWidget(cb)
            self._hover_stat_checks[key] = cb
        row2.addStretch(1)
        extra_l.addLayout(row2)
        # 提示文字
        tip = QLabel('图例可在画布内拖拽到任意位置；自动避让检测曲线密集区，'
                     '若图例覆盖曲线则翻转到对角。')
        tip.setWordWrap(True)
        tip.setStyleSheet('color: #888; font-size: 9pt;')
        extra_l.addWidget(tip)
        v.addWidget(extra_box)

        # 包含变量（多选）
        vars_box = QGroupBox('包含变量（多选，空 = 显示全部已加载变量）')
        vb = QVBoxLayout(vars_box)
        vb.setContentsMargins(8, 8, 8, 8)

        # 已加载变量 + 静态注册表（去重，已加载的优先）
        # NOTE: 循环变量不能取 v（会遮蔽外层 QVBoxLayout 的 v）
        init_vars = set(self.initial.get('vars', []))
        loaded_names = [var['name'] for var in self.all_vars]
        seen = set(loaded_names)
        all_names = list(loaded_names) + [
            var['name'] for var in _ALL_VARS if var['name'] not in seen
        ]
        labels = {var['name']: var.get('label', var['name'])
                  for var in self.all_vars}
        for var in _ALL_VARS:
            labels.setdefault(var['name'], var.get('label', var['name']))

        lw = QListWidget()
        for name in all_names:
            item = QListWidgetItem(f'{labels.get(name, name)} ({name})')
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if name in init_vars else Qt.Unchecked)
            item.setData(Qt.UserRole, name)
            lw.addItem(item)
            self._var_items[name] = item
        vb.addWidget(lw)
        self._var_list = lw

        # 全选 / 全不选
        sel_row = QHBoxLayout()
        btn_all = QPushButton('全选')
        btn_none = QPushButton('全不选')
        btn_all.clicked.connect(lambda: self._set_all(True))
        btn_none.clicked.connect(lambda: self._set_all(False))
        sel_row.addWidget(btn_all)
        sel_row.addWidget(btn_none)
        sel_row.addStretch(1)
        vb.addLayout(sel_row)
        v.addWidget(vars_box, 1)

        # OK / Cancel
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self._ok)
        btns.rejected.connect(dlg.reject)
        v.addWidget(btns)

        self._dlg = dlg
        return dlg

    def _set_all(self, val: bool):
        from PySide6.QtCore import Qt
        for it in self._var_items.values():
            it.setCheckState(Qt.Checked if val else Qt.Unchecked)

    def _ok(self):
        from PySide6.QtCore import Qt
        chosen = []
        for name, it in self._var_items.items():
            if it.checkState() == Qt.Checked:
                chosen.append(name)
        self.result = {
            'title': self._title_edit.text().strip() or '数据曲线',
            'x_label': self._x_edit.text().strip() or '时间 (s)',
            'y_label': self._y_edit.text().strip() or '值',
            'vars': chosen,
            'show_max': self._show_checks['show_max'].isChecked(),
            'show_min': self._show_checks['show_min'].isChecked(),
            'show_avg': self._show_checks['show_avg'].isChecked(),
            'show_last': self._show_checks['show_last'].isChecked(),
            'show_table': self._show_checks['show_table'].isChecked(),
            'show_hover': self._show_checks['show_hover'].isChecked(),
            'legend_pos': self._legend_combo.currentData() or 'top-right',
            'legend_font_size': int(self._legend_font.currentData() or 9),
            'legend_auto_avoid': self._legend_avoid_cb.isChecked(),
            'hover_mode': self._hover_combo.currentData() or 'current',
            'hover_show_max': self._hover_stat_checks['hover_show_max'].isChecked(),
            'hover_show_min': self._hover_stat_checks['hover_show_min'].isChecked(),
            'hover_show_avg': self._hover_stat_checks['hover_show_avg'].isChecked(),
            'hover_show_last': self._hover_stat_checks['hover_show_last'].isChecked(),
        }
        if self._dlg is not None:
            self._dlg.accept()

    def exec(self):
        dlg = self._build()
        dlg.exec()
        return self.result


class _ValidationBridge:
    """跨线程触发主线程校验。

    reader 线程不能直接动 UI（QMessageBox 等 PySide6 限制在主线程）。
    本类在主线程创建一个 QObject + Signal，reader 调 emit 即可。
    """
    def __init__(self, callback):
        self._callback = callback
        self._bridge = None

    def setup(self, parent_qobj):
        """在主线程调用：创建 QObject + 连接信号。"""
        from PySide6.QtCore import QObject, Signal, Slot
        outer = self

        class _Impl(QObject):
            requested = Signal(list, str)

            def __init__(self):
                super().__init__()
                self.requested.connect(self._handle)

            @Slot(list, str)
            def _handle(self, lines, label):
                outer._callback(lines, label)

        self._bridge = _Impl()
        self._bridge.setParent(parent_qobj)

    def emit(self, lines, label):
        """从任意线程调用：跨线程触发主线程回调。"""
        if self._bridge is not None:
            try:
                self._bridge.requested.emit(list(lines), str(label))
            except Exception:
                pass


class _QtParserConfigDialog:
    """解析器配置对话框：贴 Python 代码 + 样本日志 → 测试 → 保存。

    用户写的 `parse_line(line)` 函数 yield `(ts, name, value)` 三元组。
    保存时自动跑解析器扫样本日志，收集所有唯一 name 写入
    `discovered_vars`，让"添加曲线"对话框提前显示这些变量。
    """

    def __init__(self, parent, callback_save):
        self.parent = parent
        self.callback_save = callback_save  # 保存回调：传 dict -> None
        self.result = None
        self._dlg = None

    def _build(self):
        from PySide6.QtGui import QFont
        from PySide6.QtWidgets import (
            QButtonGroup,
            QCheckBox,
            QComboBox,
            QDialog,
            QDialogButtonBox,
            QFormLayout,
            QGroupBox,
            QHBoxLayout,
            QHeaderView,
            QLabel,
            QPlainTextEdit,
            QPushButton,
            QRadioButton,
            QSpinBox,
            QTableWidget,
            QVBoxLayout,
        )

        dlg = QDialog(self.parent)
        dlg.setWindowTitle('解析器配置')
        dlg.resize(800, 720)
        dlg.setMinimumSize(600, 480)
        self._dlg = dlg

        v = QVBoxLayout(dlg)
        v.setContentsMargins(12, 12, 12, 12)
        v.setSpacing(8)

        # 模式选择
        mode_box = QGroupBox('解析模式')
        mode_l = QHBoxLayout(mode_box)
        mode_l.setContentsMargins(8, 8, 8, 8)
        self._mode_built = QRadioButton('内置（motor/BMS/IMU）')
        self._mode_custom = QRadioButton('仅自定义代码')
        self._mode_both = QRadioButton('同时（两者都跑，并集）')
        cur_mode = _CUSTOM_PARSER_STATE.get('mode', 'built-in')
        if cur_mode == 'custom':
            self._mode_custom.setChecked(True)
        elif cur_mode == 'both':
            self._mode_both.setChecked(True)
        else:
            self._mode_built.setChecked(True)
        self._mode_group = QButtonGroup(mode_box)
        self._mode_group.addButton(self._mode_built)
        self._mode_group.addButton(self._mode_custom)
        self._mode_group.addButton(self._mode_both)
        mode_l.addWidget(self._mode_built)
        mode_l.addWidget(self._mode_custom)
        mode_l.addWidget(self._mode_both)
        mode_l.addStretch(1)
        v.addWidget(mode_box)

        # 设备源码区 + 语言下拉 + 推导按钮
        src_box = QGroupBox('设备源码（贴实际打印日志的源码——C/C++/Python/Java/Rust/JS，'
                            '自动推导日志格式生成下方 parse_line）')
        src_l_v = QVBoxLayout(src_box)
        src_l_v.setContentsMargins(8, 8, 8, 8)
        # 第一行：语言下拉 + 推导按钮 + 清空按钮
        lang_row = QHBoxLayout()
        lang_row.addWidget(QLabel('语言:'))
        self._lang_combo = QComboBox()
        self._lang_combo.addItem('自动识别', 'auto')
        for code, label in (
                ('c', 'C'), ('cpp', 'C++'), ('python', 'Python'),
                ('java', 'Java'), ('rust', 'Rust'), ('html', 'HTML'),
                ('js', 'JavaScript')):
            self._lang_combo.addItem(label, code)
        # 回填上次选择的语言
        cur_lang = _CUSTOM_PARSER_STATE.get('source_language', 'auto')
        for i in range(self._lang_combo.count()):
            if self._lang_combo.itemData(i) == cur_lang:
                self._lang_combo.setCurrentIndex(i)
                break
        self._lang_combo.currentIndexChanged.connect(self._on_lang_changed)
        lang_row.addWidget(self._lang_combo)
        self._btn_infer = QPushButton('推导解析器')
        self._btn_infer.setToolTip('从源码中提取 print 语句 → 生成 parse_line')
        self._btn_infer.clicked.connect(self._on_infer)
        lang_row.addWidget(self._btn_infer)
        self._btn_load_src = QPushButton('从文件加载源码…')
        self._btn_load_src.setToolTip('从 .c/.py/.rs/.java/.js/.md 等文件读源码')
        self._btn_load_src.clicked.connect(self._on_load_source_file)
        lang_row.addWidget(self._btn_load_src)
        lang_row.addStretch(1)
        src_l_v.addLayout(lang_row)
        self._src_edit = QPlainTextEdit()
        src_mono = QFont('DejaVu Sans Mono')
        src_mono.setStyleHint(QFont.Monospace)
        self._src_edit.setFont(src_mono)
        self._src_edit.setPlainText(
            _CUSTOM_PARSER_STATE.get('source_code', ''))
        self._src_edit.setPlaceholderText(
            '贴设备/固件里实际打印日志的代码，例如：\n'
            '    printf("ts=%ld rpm=%d vbus=%.2f\\n", ts, rpm, vbus);')
        src_l_v.addWidget(self._src_edit)
        v.addWidget(src_box, 2)

        # 解析器代码（Python，由推导生成或手动编写）
        code_box = QGroupBox(
            '解析器代码（Python，本机执行——仅可信来源粘贴）')
        code_box.setToolTip(
            '本区代码就是实际执行的 parse_line(line) 函数——\n'
            '加载日志或连串口时，每行被喂给 parse_line，\n'
            '其 yield 的 (ts, name, value) 三元组成为曲线数据。\n\n'
            '使用流程：\n'
            '  1. 上方源码区贴入设备/固件源码（C/Python/Rust...）\n'
            '  2. 点"推导解析器"→ 自动生成 Python 代码填入本区\n'
            '  3. 点"测试解析"→ 用下方样本日志跑一遍，看变量表\n'
            '  4. 保存 → 下次加载日志/连串口时按本配置解析\n\n'
            '也可直接在本区手写或微调 parse_line（无需上方源码）。')
        code_l = QVBoxLayout(code_box)
        code_l.setContentsMargins(8, 8, 8, 8)
        code_hint = QLabel(
            '↑ 这里是实际执行的逻辑：每行日志喂给 parse_line，'
            'yield (ts, name, value) 即可成为曲线。点"推导解析器"自动填充；'
            '也可手动编辑 / 直接写。')
        code_hint.setWordWrap(True)
        code_hint.setStyleSheet('color: #888; font-size: 9pt;')
        code_l.addWidget(code_hint)
        self._code_edit = QPlainTextEdit()
        mono = QFont('DejaVu Sans Mono')
        mono.setStyleHint(QFont.Monospace)
        self._code_edit.setFont(mono)
        cur_code = _CUSTOM_PARSER_STATE.get('code', '')
        self._code_edit.setPlainText(cur_code if cur_code else _PARSER_TEMPLATE_CODE)
        self._code_edit.setPlaceholderText(
            'def parse_line(line):\n'
            '    # 每行日志喂进来，yield (ts, name, value) 三元组\n'
            '    # ts: float 秒 / name: 变量名 / value: float\n'
            '    if "3wd1t_1" in line:\n'
            '        ...\n'
            '        yield ts, "motor.rpm", float(parts[4])')
        code_l.addWidget(self._code_edit)
        v.addWidget(code_box, 2)

        # 样本日志
        sample_box = QGroupBox('样本日志（贴几行真实日志用于发现变量）')
        sample_l = QVBoxLayout(sample_box)
        sample_l.setContentsMargins(8, 8, 8, 8)
        sample_btn_row = QHBoxLayout()
        self._btn_load_sample = QPushButton('从文件加载…')
        self._btn_load_sample.setToolTip('从 .txt / .log / .md 文件读样本日志')
        self._btn_load_sample.clicked.connect(self._on_load_sample_file)
        sample_btn_row.addWidget(self._btn_load_sample)
        sample_btn_row.addStretch(1)
        sample_l.addLayout(sample_btn_row)
        self._sample_edit = QPlainTextEdit()
        self._sample_edit.setFont(mono)
        self._sample_edit.setPlainText(
            _CUSTOM_PARSER_STATE.get('sample_log', ''))
        self._sample_edit.setPlaceholderText(
            '1783996615491 I model 3wd1t_1 ...')
        sample_l.addWidget(self._sample_edit)
        v.addWidget(sample_box, 1)

        # 校验设置：≥ N 条成功解析即视为规则正确
        val_box = QGroupBox('加载/连串口时格式校验')
        val_form = QFormLayout(val_box)
        val_form.setContentsMargins(8, 8, 8, 8)
        val_cfg = _CUSTOM_PARSER_STATE.get('validation', {})
        self._val_enabled = QCheckBox('启用（打开日志 / 连串口时扫前 N 行，'
                                       '成功解析 ≥ M 条即视为规则正确）')
        self._val_enabled.setChecked(bool(val_cfg.get('enabled', True)))
        val_form.addRow('', self._val_enabled)
        self._val_min_count = QSpinBox()
        self._val_min_count.setRange(1, 1000)
        self._val_min_count.setValue(int(val_cfg.get('min_parsed_count', 10)))
        self._val_min_count.setToolTip(
            '只要扫描的前 N 行里成功解析出 ≥ M 条，就视为规则正确。'
            '默认 10 条——比成功率阈值更宽松，容忍日志中混入非数据行')
        val_form.addRow('最低成功解析条数', self._val_min_count)
        self._val_lines = QSpinBox()
        self._val_lines.setRange(50, 5000)
        self._val_lines.setSingleStep(50)
        self._val_lines.setValue(int(val_cfg.get('sample_lines', 200)))
        val_form.addRow('扫描行数', self._val_lines)
        v.addWidget(val_box)

        # 按钮区
        btn_row = QHBoxLayout()
        self._btn_test = QPushButton('测试解析')
        self._btn_test.clicked.connect(self._on_test)
        btn_row.addWidget(self._btn_test)
        btn_row.addStretch(1)
        bb = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Save).setText('保存')
        bb.button(QDialogButtonBox.Cancel).setText('取消')
        bb.accepted.connect(self._on_save)
        bb.rejected.connect(dlg.reject)
        btn_row.addWidget(bb)
        v.addLayout(btn_row)

        # 测试结果表
        self._result_label = QLabel('（点"测试解析"查看结果）')
        v.addWidget(self._result_label)
        self._result_table = QTableWidget(0, 4)
        self._result_table.setHorizontalHeaderLabels(
            ['变量名', '首次值', '末次值', '出现次数'])
        self._result_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.Stretch)
        self._result_table.setVisible(False)
        v.addWidget(self._result_table, 1)

        return dlg

    def _on_lang_changed(self, _idx):
        """语言下拉变化：选"自动识别"时尝试 detect_language 回填。"""
        lang_data = self._lang_combo.currentData()
        if lang_data != 'auto':
            return
        src = self._src_edit.toPlainText()
        if not src.strip():
            return
        try:
            from serial_debug.ui.qt_parser_inference import detect_language
            lang = detect_language(src)
        except Exception:
            return
        if lang not in ('c', 'cpp', 'python', 'java', 'rust', 'html', 'js'):
            return
        # 不强制切下拉（保留"自动识别"项），只在状态栏提示
        try:
            self._result_label.setText(f'（自动识别语言：{lang}）')
        except Exception:
            pass

    def _on_infer(self):
        """从源码提取 print 语句 → 生成 parse_line → 填入代码区。"""
        try:
            from serial_debug.ui.qt_parser_inference import (
                detect_language,
                extract_print_statements,
                generate_parse_line,
            )
        except Exception as e:
            self._result_label.setText(f'❌ 推导模块不可用：{e!r}')
            return
        src = self._src_edit.toPlainText()
        if not src.strip():
            self._result_label.setText('⚠ 源码为空')
            return
        lang_data = self._lang_combo.currentData()
        if lang_data == 'auto':
            lang = detect_language(src)
            # 找到下拉里的对应项并切换（保留用户能再次切回 auto）
            for i in range(self._lang_combo.count()):
                if self._lang_combo.itemData(i) == lang:
                    self._lang_combo.blockSignals(True)
                    self._lang_combo.setCurrentIndex(i)
                    self._lang_combo.blockSignals(False)
                    break
        else:
            lang = lang_data
        if lang == 'html':
            self._result_label.setText(
                '⚠ HTML 不产生日志，不支持推导。请贴 C/C++/Python/Java/Rust/JS 源码')
            return
        try:
            stmts = extract_print_statements(src, lang)
        except Exception as e:
            self._result_label.setText(f'❌ 提取失败：{e!r}')
            return
        if not stmts:
            self._result_label.setText(
                f'⚠ 未从源码识别到 print 语句（语言={lang}）')
            return
        try:
            gen = generate_parse_line(stmts)
        except Exception as e:
            self._result_label.setText(f'❌ 生成失败：{e!r}')
            return
        self._code_edit.setPlainText(gen)
        self._result_label.setText(
            f'✓ 推导完成：识别 {len(stmts)} 条 print 语句（语言={lang}）'
            '，已填入"解析器代码"区，可点"测试解析"验证')

    def _on_load_source_file(self):
        """从 .c/.cpp/.py/.java/.rs/.js/.md/.txt 文件读源码。"""
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            self._dlg, '选择源码文件', '',
            '源码 (*.c *.cpp *.h *.hpp *.py *.java *.rs *.js *.ts *.md *.txt);;'
            '所有文件 (*)')
        if not path:
            return
        try:
            with open(path, encoding='utf-8', errors='replace') as f:
                data = f.read(10 * 1024 * 1024)  # 限 10MB
        except Exception as e:
            self._result_label.setText(f'❌ 读文件失败：{e!r}')
            return
        if path.lower().endswith('.md'):
            # 取第一个 ``` 代码块
            import re as _re
            m = _re.search(r'```(?:\w+)?\s*\n(.*?)```', data, _re.DOTALL)
            if m:
                data = m.group(1)
        self._src_edit.setPlainText(data)
        self._result_label.setText(f'✓ 已加载源码：{path}')

    def _on_load_sample_file(self):
        """从 .txt/.log/.md 文件读样本日志。"""
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            self._dlg, '选择样本日志', '',
            '日志 (*.txt *.log *.md);;所有文件 (*)')
        if not path:
            return
        try:
            with open(path, encoding='utf-8', errors='replace') as f:
                data = f.read(10 * 1024 * 1024)
        except Exception as e:
            self._result_label.setText(f'❌ 读文件失败：{e!r}')
            return
        if path.lower().endswith('.md'):
            import re as _re
            m = _re.search(r'```(?:\w+)?\s*\n(.*?)```', data, _re.DOTALL)
            if m:
                data = m.group(1)
        self._sample_edit.setPlainText(data)
        self._result_label.setText(f'✓ 已加载样本日志：{path}')

    def _on_test(self):
        """跑当前代码 + 样本日志，结果填进表格。"""
        from PySide6.QtWidgets import QTableWidgetItem
        code = self._code_edit.toPlainText()
        sample = self._sample_edit.toPlainText()
        try:
            ns: dict = {}
            exec(compile(code, '<custom_parser_test>', 'exec'), ns)
        except SyntaxError as e:
            self._result_label.setText(
                f'❌ 语法错误（行 {e.lineno}）：{e.msg}')
            self._result_table.setVisible(False)
            return
        except Exception as e:
            self._result_label.setText(f'❌ exec 失败：{e!r}')
            self._result_table.setVisible(False)
            return
        func = ns.get('parse_line')
        if not callable(func):
            self._result_label.setText('❌ 代码未定义 parse_line(line) 函数')
            self._result_table.setVisible(False)
            return

        lines = [l for l in sample.splitlines() if l.strip()]
        if not lines:
            self._result_label.setText('⚠ 样本日志为空')
            self._result_table.setVisible(False)
            return

        # name -> {first, last, count}
        found: dict[str, dict] = {}
        err_count = 0
        for line in lines:
            try:
                result = func(line)
                if result is None:
                    continue
                for item in result:
                    if not item or len(item) != 3:
                        continue
                    _, name, val = item
                    if name not in found:
                        found[name] = {'first': val, 'last': val, 'count': 0}
                    found[name]['last'] = val
                    found[name]['count'] += 1
            except Exception:
                err_count += 1

        self._result_table.setRowCount(len(found))
        for i, (name, info) in enumerate(sorted(found.items())):
            self._result_table.setItem(i, 0, QTableWidgetItem(str(name)))
            self._result_table.setItem(i, 1, QTableWidgetItem(
                _fmt_val(info['first'])))
            self._result_table.setItem(i, 2, QTableWidgetItem(
                _fmt_val(info['last'])))
            self._result_table.setItem(i, 3, QTableWidgetItem(
                str(info['count'])))
        self._result_table.setVisible(True)
        msg = (f'✓ 解析成功：{len(found)} 个变量，{len(lines)} 行'
               f'（{err_count} 行解析失败）')
        self._result_label.setText(msg)

    def _on_save(self):
        """保存：写 _CUSTOM_PARSER_STATE + 持久化 + 跑变量发现。"""
        mode = ('built-in' if self._mode_built.isChecked()
                else 'custom' if self._mode_custom.isChecked()
                else 'both')
        code = self._code_edit.toPlainText()
        sample = self._sample_edit.toPlainText()
        src_code = self._src_edit.toPlainText()
        src_lang = self._lang_combo.currentData()
        val_cfg = {
            'enabled': self._val_enabled.isChecked(),
            'min_parsed_count': int(self._val_min_count.value()),
            'sample_lines': int(self._val_lines.value()),
        }

        # exec + 发现变量
        func = None
        discovered: list = []
        exec_error: str = ''
        if code and mode in ('custom', 'both'):
            try:
                ns: dict = {}
                exec(compile(code, '<custom_parser>', 'exec'), ns)
                if callable(ns.get('parse_line')):
                    func = ns['parse_line']
                else:
                    exec_error = '代码未定义 parse_line(line) 函数'
            except SyntaxError as e:
                exec_error = f'语法错误（行 {e.lineno}）：{e.msg}'
            except Exception as e:
                exec_error = f'exec 失败：{e!r}'
            if func is not None:
                discovered = _discover_vars_from_sample(func, sample)
            else:
                # 代码无效：保存配置但提示用户，避免静默接受坏解析器。
                # 仍写入 code/mode（用户意图保留），func=None 让 custom 模式不
                # 运行坏代码，用户应回头改。
                from PySide6.QtWidgets import QMessageBox
                QMessageBox.warning(
                    self._dlg, '解析器代码无效',
                    f'代码无法执行：{exec_error}\n\n'
                    '配置已保存（代码 + 模式），但解析器未启用。'
                    '请修正代码后再次保存，或先点"测试解析"验证。')

        new_state = {
            'mode': mode,
            'code': code,
            'sample_log': sample,
            'source_code': src_code,
            'source_language': src_lang,
            'func': func,
            'discovered_vars': discovered,
            'validation': val_cfg,
        }
        _CUSTOM_PARSER_STATE.update(new_state)
        if callable(self.callback_save):
            self.callback_save()
        self.result = new_state
        self._dlg.accept()

    def exec(self):
        dlg = self._build()
        dlg.exec()
        return self.result


def _fmt_val(v) -> str:
    """格式化解析出的值用于表格显示。"""
    try:
        f = float(v)
        if abs(f) >= 1000 or (abs(f) < 0.01 and f != 0):
            return f'{f:.4g}'
        return f'{f:.3f}'
    except Exception:
        return str(v)


def _discover_vars_from_sample(func, sample_log: str) -> list:
    """跑 func 扫 sample_log 每行 → 收集所有唯一 name → 返回
    `[{name, label}]` 列表。label 暂取 name 自身（用户后续可在侧栏改）。"""
    out: dict[str, dict] = {}
    for line in (sample_log or '').splitlines():
        if not line.strip():
            continue
        try:
            result = func(line)
            if result is None:
                continue
            for item in result:
                if not item or len(item) != 3:
                    continue
                name = str(item[1])
                if name not in out:
                    out[name] = {'name': name, 'label': name}
        except Exception:
            pass
    return list(out.values())


class QtMonitorWindow:
    """Qt 数据监视窗口（Stage 1：多 plot 子图 + 配置持久化）。"""

    def __init__(self, log_path: str | None = None,
                 theme_name: str = 'auto'):
        self.log_path = log_path
        # theme_name 可以是模式（auto/light/dark）或具体主题名
        self.theme_pref = (theme_name if theme_name in ('auto', 'light', 'dark')
                          else themes.load_pref())
        if theme_name in themes.THEMES:
            self.theme_name = theme_name
        else:
            self.theme_name = themes.resolve(self.theme_pref)
        self._app = None
        self._win = None
        # 变量数据：{var_name: list[(ts, val)]}——始终是 list，实时 feed 依赖 append
        self._data: dict = {}
        # numpy 缓存：{var_name: (xs_array, ys_array, list_len)}
        # list_len 记录缓存时的 list 长度，新数据 append 后长度变化→缓存失效
        self._data_cache: dict = {}
        # 变量元数据：{name: {color, label, y_min, y_max, warn_min, warn_max}}
        # 自动从 self._data 派生（load_log / gen_mock_data 加新 var 时同步登记）
        # 用户可改 color/label，持久化到 qt_monitor_vars
        self._var_meta: dict[str, dict] = self._load_var_meta()
        # 配置：plots list（每项 = {title, x_label, y_label, vars, show_*}）
        # 这是"原始配置"，导入/导出用；运行时显示用 _visible_plots
        self._plots: list[dict] = self._load_plots()
        # 运行时显示列表：用户在本会话里的 X 关闭 / 添加 都改这里，不动 _plots。
        # 初始化 = _plots 的浅拷贝（独立列表，元素仍指向同一 dict）
        self._visible_plots: list[dict] = list(self._plots)
        # UI：PlotWidget 列表 + 每个对应的 curves dict（name -> PlotDataItem）
        self._plot_widgets: list = []
        self._curves: list[dict] = []
        self._plot_headers: list = []  # 每个 plot 的顶部条（title + legend）
        self._plot_containers: list = []  # 每个 plot 的容器（用于显隐）
        self._plots_layout = None
        # 工具栏文件名显示（load_log / 串口连接时更新）
        self._file_label = None
        self._current_file_label: str = ''
        # 嵌套 splitter（每行一个 h_splitter，外层 v_splitter），用户拖动
        # 分隔条可调节每个 PlotWidget 的宽高，默认等分（视觉同 QGridLayout）
        self._v_splitter = None
        # popout 单图独立窗口列表：[(plot_idx, QDialog, PlotWidget, curves_dict)]
        self._popout_dialogs: list = []
        # 侧栏 UI
        self._sidebar = None        # QTreeWidget
        self._sidebar_container = None  # QWidget（splitter 左侧）
        self._splitter = None
        # 侧栏显隐状态（独立于 Qt 实际 isVisible，避免 win 未 show 时误存 False）
        self._sidebar_visible: bool = True
        self._palette = [
            '#569cd6', '#ce9178', '#4ec9b0', '#dcdcaa',
            '#c586c0', '#f48771', '#6a9955', '#9cdcfe',
            '#d16969', '#b5cea8',
        ]
        # 实时串口 feed 状态
        self._serial = None
        self._serial_thread = None
        self._serial_stop = None
        self._serial_buf = ''
        self._refresh_timer = None
        self._max_points = 60_000
        self._data_lock = threading.Lock()  # 保护 _data 的线程安全
        # 自定义解析器状态（模块级 _CUSTOM_PARSER_STATE 在启动时加载一次）
        self._load_custom_parser_state()
        # 串口校验缓冲（reader 线程攒够 sample_lines 或 2 秒后触发一次校验）
        self._serial_validated: bool = False
        self._serial_validation_buf: list[str] = []
        self._serial_start_ts: float = 0.0
        # 跨线程触发校验（reader 线程不能直接动 UI）
        self._validation_bridge = _ValidationBridge(self._validate_log_format)

    # ---- 配置持久化 -------------------------------------------------
    def _load_custom_parser_state(self) -> None:
        """启动时读 config → exec 代码拿 parse_line → 写入 _CUSTOM_PARSER_STATE。"""
        global _CUSTOM_PARSER_STATE
        from serial_debug.infra import config
        try:
            cfg = config.load()
            raw = cfg.get('qt_monitor_custom_parser', {})
            if not isinstance(raw, dict):
                return
            mode = raw.get('mode', 'built-in')
            if mode not in ('built-in', 'custom', 'both'):
                mode = 'built-in'
            code = raw.get('code', '') or ''
            sample_log = raw.get('sample_log', '') or ''
            func = None
            if code and mode in ('custom', 'both'):
                try:
                    ns: dict = {}
                    exec(compile(code, '<custom_parser>', 'exec'), ns)
                    if callable(ns.get('parse_line')):
                        func = ns['parse_line']
                except Exception:
                    func = None  # 代码坏掉时退回 built-in，下次 dialog 修
            disc = raw.get('discovered_vars', []) or []
            if not isinstance(disc, list):
                disc = []
            val_cfg = raw.get('validation', {}) or {}
            if not isinstance(val_cfg, dict):
                val_cfg = {}
            # 兼容旧 config：min_success_rate → min_parsed_count（10 默认）
            if 'min_parsed_count' in val_cfg:
                min_count = int(val_cfg['min_parsed_count'])
            else:
                min_count = 10
            validation = {
                'enabled': bool(val_cfg.get('enabled', True)),
                'min_parsed_count': min_count,
                'sample_lines': int(val_cfg.get('sample_lines', 200)),
            }
            src_code = raw.get('source_code', '') or ''
            src_lang = raw.get('source_language', 'auto') or 'auto'
            if src_lang not in ('auto', 'c', 'cpp', 'python', 'java',
                                 'rust', 'html', 'js'):
                src_lang = 'auto'
            _CUSTOM_PARSER_STATE.update({
                'mode': mode,
                'code': code,
                'sample_log': sample_log,
                'source_code': src_code,
                'source_language': src_lang,
                'func': func,
                'discovered_vars': disc,
                'validation': validation,
            })
        except Exception:
            pass

    def _save_custom_parser_state(self) -> None:
        """把 _CUSTOM_PARSER_STATE 持久化到 config。"""
        from serial_debug.infra import config
        try:
            cfg = config.load()
            cfg['qt_monitor_custom_parser'] = {
                'mode': _CUSTOM_PARSER_STATE.get('mode', 'built-in'),
                'code': _CUSTOM_PARSER_STATE.get('code', ''),
                'sample_log': _CUSTOM_PARSER_STATE.get('sample_log', ''),
                'source_code': _CUSTOM_PARSER_STATE.get('source_code', ''),
                'source_language': _CUSTOM_PARSER_STATE.get('source_language', 'auto'),
                'discovered_vars': _CUSTOM_PARSER_STATE.get('discovered_vars', []),
                'validation': _CUSTOM_PARSER_STATE.get('validation', {
                    'enabled': True, 'min_parsed_count': 10,
                    'sample_lines': 200,
                }),
            }
            config.save(cfg)
        except Exception:
            pass

    def _load_last_config_path(self) -> str:
        """读上次"打开过"的配置文件路径（指针，仅路径不存配置）。

        历史命名保留——仅指"用户主动从'导入配置'选过的文件"。
        启动时优先用 _load_last_exported_path（用户导出过的最新文件），
        无导出记录才回退到本字段。
        """
        from serial_debug.infra import config
        try:
            cfg = config.load()
            p = cfg.get('qt_monitor_last_config_path', '') or ''
            return str(p) if p else ''
        except Exception:
            return ''

    def _save_last_config_path(self, path: str) -> None:
        """记录上次"打开过"的配置文件路径（导入时调）。仅指针。"""
        from serial_debug.infra import config
        try:
            cfg = config.load()
            cfg['qt_monitor_last_config_path'] = path or ''
            config.save(cfg)
        except Exception:
            pass

    def _load_last_exported_path(self) -> str:
        """读上次"导出过"的配置文件路径（用户在 dialog 里点导出后记录）。

        启动时优先用此路径加载曲线配置（包含用户调整后的布局）。
        """
        from serial_debug.infra import config
        try:
            cfg = config.load()
            p = cfg.get('qt_monitor_last_exported_config_path', '') or ''
            return str(p) if p else ''
        except Exception:
            return ''

    def _save_last_exported_path(self, path: str) -> None:
        """记录上次"导出过"的配置文件路径。仅指针。"""
        from serial_debug.infra import config
        try:
            cfg = config.load()
            cfg['qt_monitor_last_exported_config_path'] = path or ''
            config.save(cfg)
        except Exception:
            pass

    def _load_plots(self) -> list[dict]:
        """加载 plots 配置。

        优先级：
          1. 上次导出过的配置文件（用户调整满意后保存的布局）
          2. 上次打开过的配置文件（用户主动"导入配置"选过的文件）
          3. 默认 config 内的 qt_monitor_plots key（兼容旧用户）
        """
        import json
        import os

        from serial_debug.infra import config

        def _try_load(path: str):
            """从给定 JSON 文件加载 plots + var_meta；返回 (plots, ok)。"""
            if not path or not os.path.isfile(path):
                return None, False
            try:
                with open(path, encoding='utf-8') as f:
                    data = json.load(f)
            except Exception:
                return None, False
            raw = data.get('plots', []) or []
            vm = data.get('var_meta', []) or data.get('var_meta_dict', {})
            # 兼容两种存储格式：list（旧）或 dict（新）
            new_meta: dict[str, dict] = {}
            if isinstance(vm, list):
                for v in vm:
                    if isinstance(v, dict) and 'name' in v:
                        new_meta[v['name']] = {
                            'color': v.get('color', ''),
                            'label': v.get('label', ''),
                            'y_min': v.get('y_min'),
                            'y_max': v.get('y_max'),
                            'warn_min': v.get('warn_min'),
                            'warn_max': v.get('warn_max'),
                        }
            elif isinstance(vm, dict):
                for name, m in vm.items():
                    if isinstance(m, dict):
                        new_meta[name] = {
                            'color': m.get('color', ''),
                            'label': m.get('label', ''),
                            'y_min': m.get('y_min'),
                            'y_max': m.get('y_max'),
                            'warn_min': m.get('warn_min'),
                            'warn_max': m.get('warn_max'),
                        }
            if new_meta:
                self._var_meta = new_meta
            return [_normalize_plot(p) for p in raw], True

        try:
            # 1. 上次导出过的配置文件
            exported = self._load_last_exported_path()
            plots, ok = _try_load(exported)
            if ok and plots is not None:
                return plots
            # 2. 上次打开过的配置文件
            opened = self._load_last_config_path()
            plots, ok = _try_load(opened)
            if ok and plots is not None:
                return plots
            # 3. 默认 config 内的 qt_monitor_plots key
            cfg = config.load()
            raw = cfg.get('qt_monitor_plots', []) or []
            return [_normalize_plot(p) for p in raw]
        except Exception:
            return []

    def _load_var_meta(self) -> dict[str, dict]:
        from serial_debug.infra import config
        out: dict[str, dict] = {}
        try:
            cfg = config.load()
            for v in cfg.get('qt_monitor_vars', []):
                if isinstance(v, dict) and 'name' in v:
                    out[v['name']] = {
                        'color': v.get('color', ''),
                        'label': v.get('label', ''),
                        'y_min': v.get('y_min'),
                        'y_max': v.get('y_max'),
                        'warn_min': v.get('warn_min'),
                        'warn_max': v.get('warn_max'),
                    }
        except Exception:
            pass
        return out

    def _save_var_meta(self) -> None:
        from serial_debug.infra import config
        try:
            cfg = config.load()
            cfg['qt_monitor_vars'] = [
                {'name': n, **m} for n, m in self._var_meta.items()
            ]
            if self._splitter is not None:
                try:
                    sizes = self._splitter.sizes()
                    if sizes:
                        cfg['qt_monitor_sidebar_width'] = int(sizes[0])
                except Exception:
                    pass
            cfg['qt_monitor_sidebar_visible'] = self._sidebar_visible
            config.save(cfg)
        except Exception:
            pass

    def _save_plots(self) -> None:
        """保存当前显示的 plots 到 config 的 qt_monitor_plots。

        保存 _visible_plots（运行时显示列表，包含本会话里的增删改 + X 关闭），
        下次启动 _load_plots 第 3 级会读到——这样关闭时用户调整后的布局
        自动恢复，不强制用户"导出配置"。
        """
        from serial_debug.infra import config
        try:
            cfg = config.load()
            # 同步 _plots = _visible_plots，避免下次启动 _plots 还指向旧基准
            self._plots = [dict(p) for p in self._visible_plots]
            cfg['qt_monitor_plots'] = [_normalize_plot(p) for p in self._visible_plots]
            if self._win is not None:
                try:
                    cfg['qt_monitor_geometry'] = self._win.saveGeometry().data().hex()
                except Exception:
                    pass
            config.save(cfg)
        except Exception:
            pass

    # ---- 配置导入 / 导出 -------------------------------------------------
    def export_plots_config(self, file_path: str) -> None:
        """导出当前 _visible_plots + var_meta 到 JSON 文件。

        导出 _visible_plots（含本会话里的增删改），让用户能"调整满意后
        保存为新配置"。_plots（原始配置）不导出——它只是初始基准。

        导出内容：
          - plots: self._visible_plots（title / vars / show_* / hover_* / legend_*）
          - var_meta: self._var_meta（颜色 / label / y_min/y_max / warn_*
            等显示参数；导入时合并到当前 var_meta）
          - version: 格式版本号（向后兼容）

        不导出 self._data（实际曲线数据）。导入后曲线显示需要先加载
        日志或连串口，按导出时的 vars 配置重新绘制。
        """
        import json
        data = {
            'version': 1,
            'plots': [_normalize_plot(p) for p in self._visible_plots],
            'var_meta': {k: dict(v) for k, v in self._var_meta.items()},
        }
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        # 记录指针：导出的文件作为下次启动的默认配置
        self._save_last_exported_path(file_path)
        # 同步：导出后 _plots 也对齐 _visible_plots（视为用户确认保存）
        self._plots = list(self._visible_plots)

    def import_plots_config(self, file_path: str) -> int:
        """从 JSON 文件导入曲线图配置，替换当前 plots + 合并 var_meta。

        返回导入的 plot 数量。导入后 _plots 和 _visible_plots 都设为新
        配置（视为新的基准），调 _rebuild_plots_ui + _refresh_sidebar +
        _refresh_plot。不自动写配置——但会记录 file_path 指针，下次启动
        自动加载该文件。
        """
        import json
        with open(file_path, encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict) or 'plots' not in data:
            raise ValueError('JSON 格式不对：缺少 plots 字段')
        new_plots = [_normalize_plot(p) for p in data['plots']]
        # 合并 var_meta（导入文件里的覆盖当前同名字段，不全量替换
        # 防止丢失当前已加载但配置里没的变量元信息）
        new_meta = data.get('var_meta', {})
        if isinstance(new_meta, dict):
            for name, meta in new_meta.items():
                if not isinstance(meta, dict):
                    continue
                cur = self._var_meta.setdefault(name, {})
                cur.update(meta)
                # 触发 _register_var 注册到 _all_var_names
                self._register_var(name)
        self._plots = new_plots
        self._visible_plots = list(new_plots)
        self._rebuild_plots_ui()
        self._refresh_sidebar()
        self._refresh_sidebar_visibility()
        self._refresh_plot()
        # 记录指针，下次启动自动从此文件加载
        self._save_last_config_path(file_path)
        return len(new_plots)

    def _save_geometry(self) -> None:
        from serial_debug.infra import config
        try:
            if self._win is None:
                return
            cfg = config.load()
            cfg['qt_monitor_geometry'] = self._win.saveGeometry().data().hex()
            # 保存曲线图布局：v_splitter 各行高度 + 每个 h_splitter 各列宽度
            cfg['qt_monitor_splitter_sizes'] = self._dump_splitter_sizes()
            config.save(cfg)
        except Exception:
            pass

    def _last_dir(self, key: str, default: str = '') -> str:
        """读上次 file dialog 记的目录（key 为 config 字段名）。

        无记录或目录不存在时回退到 ~。
        """
        import os

        from serial_debug.infra import config
        try:
            d = config.load().get(key, '') or ''
            if d and os.path.isdir(d):
                return d
        except Exception:
            pass
        return os.path.expanduser(default or '~')

    def _remember_dir(self, key: str, file_path: str) -> None:
        """把 file_path 所在目录记到 config[key]——下次打开同类型文件时默认此目录。"""
        import os

        from serial_debug.infra import config
        try:
            d = os.path.dirname(os.path.abspath(file_path)) if file_path else ''
            if not d:
                return
            cfg = config.load()
            cfg[key] = d
            config.save(cfg)
        except Exception:
            pass


    def _dump_splitter_sizes(self) -> dict:
        """导出 v_splitter + 子 h_splitter 的 sizes（便于启动恢复布局）。"""
        out = {}
        try:
            if self._v_splitter is None:
                return out
            out['v'] = list(self._v_splitter.sizes())
            # 找到所有 h_splitter（v_splitter 的子）
            hs = []
            for i in range(self._v_splitter.count()):
                w = self._v_splitter.widget(i)
                if w is not None and hasattr(w, 'sizes') and \
                        w.__class__.__name__ == 'QSplitter':
                    hs.append(list(w.sizes()))
            out['h'] = hs
        except Exception:
            pass
        return out

    def _restore_splitter_sizes(self) -> None:
        """启动时按保存的 sizes 恢复 v_splitter + h_splitter 布局。"""
        from serial_debug.infra import config
        try:
            cfg = config.load()
            saved = cfg.get('qt_monitor_splitter_sizes', {})
            if not isinstance(saved, dict) or not saved:
                return
            v_sizes = saved.get('v')
            if v_sizes and self._v_splitter is not None:
                try:
                    self._v_splitter.setSizes([int(x) for x in v_sizes])
                except Exception:
                    pass
            h_sizes_list = saved.get('h', [])
            if not isinstance(h_sizes_list, list):
                return
            h_idx = 0
            for i in range(self._v_splitter.count()):
                w = self._v_splitter.widget(i)
                if w is None or w.__class__.__name__ != 'QSplitter':
                    continue
                if h_idx >= len(h_sizes_list):
                    break
                sizes = h_sizes_list[h_idx]
                h_idx += 1
                try:
                    w.setSizes([int(x) for x in sizes])
                except Exception:
                    pass
        except Exception:
            pass

    def _restore_geometry(self) -> None:
        from serial_debug.infra import config
        try:
            cfg = config.load()
            geo_hex = cfg.get('qt_monitor_geometry', '')
            if geo_hex and self._win is not None:
                from PySide6.QtCore import QByteArray
                ba = QByteArray.fromHex(bytes(geo_hex, 'ascii'))
                self._win.restoreGeometry(ba)
            # splitter sizes 在 _rebuild_plots_ui 之后调用
            # （_make_plot_widget 已建出 _v_splitter + 子 h_splitter）
            self._restore_splitter_sizes()
        except Exception:
            pass

    # ---- 数据加载 ----------------------------------------------------
    def _register_var(self, name: str) -> None:
        """新变量首次出现时登记到 self._var_meta（自动分配调色板下一色）。"""
        if name in self._var_meta:
            return
        idx = len(self._var_meta)
        # 中文 label：从静态注册表查
        label = name
        for v in _ALL_VARS:
            if v['name'] == name:
                label = v.get('label', name)
                break
        self._var_meta[name] = {
            'color': self._palette[idx % len(self._palette)],
            'label': label,
            'y_min': None,
            'y_max': None,
            'warn_min': None,
            'warn_max': None,
        }

    def load_log(self, path: str) -> int:
        """加载日志文件，复用 monitor_window 的 motor/BMS/IMU 解析器。

        自动按行内 tag 检测数据类型，变量名加 Panel 前缀（motor./bms./imu.）
        避免冲突。返回解析命中的行数。加载完成后跑一次格式校验（成功率
        低于阈值则弹非模态警告 + 状态栏提示）。

        加载完毕后刷新 plot + sidebar：从日志查看器入口的 _replay_to_monitor
        会主动调 _refresh_plot，但 monitor 自身"打开日志"按钮走这条路径，
        必须自刷新，否则曲线显示陈旧 / 错位。

        先清 _data + _data_cache：避免旧日志/mock 数据残留导致时间戳跳跃，
        曲线错位（与 _replay_to_monitor 走新实例 _data={} 路径对齐）。
        var_meta 不清——颜色/label 等用户配置应跨日志保留。
        """
        # 清数据点（保留 var_meta 配置）
        self._data.clear()
        self._data_cache.clear()
        n = 0
        sample_lines: list[str] = []
        sample_n = max(10, int(_CUSTOM_PARSER_STATE.get(
            'validation', {}).get('sample_lines', 200)))
        try:
            with open(path, encoding='utf-8', errors='replace') as f:
                for line in f:
                    if len(sample_lines) < sample_n:
                        sample_lines.append(line)
                    hit = False
                    for ts, name, val in _iter_panel_vars(line):
                        self._data.setdefault(name, []).append((ts, val))
                        self._register_var(name)
                        hit = True
                    if hit:
                        n += 1
        except Exception:
            pass
        # 加载完成后跑格式校验（前 sample_lines 行）
        self._validate_log_format(sample_lines, f'日志 {path}')
        # 更新工具栏文件名显示
        self._update_file_label(path)
        # 刷新 plot + sidebar：与 _replay_to_monitor 路径对齐
        try:
            self._refresh_plot()
            self._refresh_sidebar()
            self._refresh_sidebar_visibility()
        except Exception:
            pass
        return n

    def gen_mock_data(self, n_points: int) -> None:
        """生成 n_points 模拟数据：正弦 + 随机尖峰，用于性能验证。

        numpy 向量化 + 直接存 (xs, ys) array 元组，不做 zip/tolist 转换。
        """
        import numpy as np
        t = np.arange(n_points, dtype=np.float64) * 0.001
        data1 = np.sin(2 * np.pi * 50 * t) * 50 + 50
        data2 = np.cos(2 * np.pi * 30 * t) * 30 + 20
        rng = np.random.default_rng(42)
        spikes = rng.random(n_points) < 0.001
        data2 = np.where(spikes, data2 + 80, data2)
        data3 = t + rng.uniform(-0.5, 0.5, n_points)
        self._data_cache.clear()
        self._data = {
            'sin_50hz': (t, data1),
            'cos_with_spikes': (t.copy(), data2),
            'ramp': (t.copy(), data3),
        }
        # 同步登记到 var_meta
        for name in self._data:
            self._register_var(name)

    # ---- UI ----------------------------------------------------------
    def _create(self):
        import pyqtgraph as pg
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import (
            QApplication,
            QLabel,
            QMainWindow,
            QPushButton,
            QScrollArea,
            QSplitter,
            QToolBar,
            QVBoxLayout,
            QWidget,
        )

        from serial_debug.ui import qt_theme

        app = QApplication.instance() or QApplication(sys.argv)
        qt_theme.apply_qss(app, self.theme_name)
        self._app = app

        # pyqtgraph 全局配置
        t_init = qt_theme.palette(self.theme_name)
        pg.setConfigOption('background', t_init.get('panel_bg', '#1e1e1e'))
        pg.setConfigOption('foreground', t_init.get('fg', '#cccccc'))
        pg.setConfigOption('antialias', True)
        try:
            pg.setConfigOption('useOpenGL', True)
            pg.setConfigOption('enableExperimental', True)
        except Exception:
            pass

        win = QMainWindow()
        win.setWindowTitle('数据监视窗口')
        win.resize(1400, 800)

        # 跨线程校验桥（在主线程 setup，避免 reader 线程直接动 UI）
        self._validation_bridge.setup(win)

        # 中央：QSplitter 横向分栏，左侧变量列表，右侧 plot 滚动区
        central = QSplitter(Qt.Horizontal)
        central.setHandleWidth(4)
        central.setChildrenCollapsible(False)

        # 左侧：变量列表侧栏
        self._sidebar_container = self._build_sidebar()
        central.addWidget(self._sidebar_container)

        # 右侧：可滚动容器 + 网格布局放多个 PlotWidget（自动排版）
        right_pane = QWidget()
        outer = QVBoxLayout(right_pane)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)

        plots_container = QWidget()
        # plots_container 内只放一个垂直 QSplitter，每行一个水平 splitter
        # 装 cols 个 PlotWidget。默认等分（视觉等于网格布局），用户可拖
        # 分隔条调节每个 PlotWidget 的宽高。sizePolicy 仍 Expanding，
        # 最小尺寸由 PlotWidget.setMinimumHeight/Width 兜底。
        self._plots_layout = QVBoxLayout(plots_container)
        self._plots_layout.setContentsMargins(0, 0, 0, 0)
        self._plots_layout.setSpacing(0)

        scroll.setWidget(plots_container)
        outer.addWidget(scroll)
        central.addWidget(right_pane)

        # 让右侧 plot 区占大头，侧栏按持久化宽度
        from serial_debug.infra import config as _cfg
        try:
            sb_w = int(_cfg.load().get('qt_monitor_sidebar_width', 260))
        except Exception:
            sb_w = 260
        central.setSizes([sb_w, 1400 - sb_w])
        # 持久化侧栏可见状态
        try:
            sb_visible = bool(_cfg.load().get('qt_monitor_sidebar_visible', True))
        except Exception:
            sb_visible = True
        self._sidebar_visible = sb_visible
        self._sidebar_container.setVisible(sb_visible)

        win.setCentralWidget(central)
        self._splitter = central

        # ---- 回调定义（工具栏 + 菜单栏共用）----
        def _add_plot():
            dlg = _QtPlotDialog(
                win, self._loaded_var_meta(),
                initial=None, title='添加图表')
            res = dlg.exec()
            if res:
                p = _normalize_plot(res)
                self._plots.append(p)         # 配置侧（导出时用）
                self._visible_plots.append(p)  # 运行时显示侧
                self._rebuild_plots_ui()
                self._refresh_plot()
                win.statusBar().showMessage(
                    f'已添加图表：{res["title"]}（{len(res["vars"])} 变量）'
                    '——变更未保存，需手动"导出配置"')

        def _edit_plot():
            idx = self._select_plot_idx(win, '编辑图表')
            if idx is None:
                return
            # 编辑 _visible_plots 中第 idx 个（idx 是显示序）
            dlg = _QtPlotDialog(
                win, self._loaded_var_meta(),
                initial=self._visible_plots[idx], title='编辑图表')
            res = dlg.exec()
            if res:
                # 合并而非替换：res 只含 dialog 收集的字段（title/vars/
                # show_*/hover_*/legend_*），不含 var_visibility（图例
                # checkbox 勾选状态）等运行时字段。直接 _normalize_plot(res)
                # 会让 var_visibility 丢失 → 用户编辑图例位置后，原本取消
                # 勾选的曲线又重新可见（"还原到全部显示"）。
                # 修复：把旧 plot 的字段作底，让 res 覆盖 dialog 改的部分。
                old = self._visible_plots[idx] or {}
                merged = {**old, **res}
                p = _normalize_plot(merged)
                self._visible_plots[idx] = p
                # 同步到 _plots：找原始 idx（_plots 中可能含有已被 X 关闭的项，
                # 这里简单策略——重设 _plots 为 visible 的拷贝）
                self._plots = list(self._visible_plots)
                self._rebuild_plots_ui()
                self._refresh_plot()
                win.statusBar().showMessage(
                    f'已更新图表：{res["title"]}——变更未保存，需手动"导出配置"')

        def _del_plot():
            from PySide6.QtWidgets import QMessageBox
            idx = self._select_plot_idx(win, '删除图表')
            if idx is None:
                return
            title = self._visible_plots[idx].get('title', '数据曲线')
            if QMessageBox.question(
                    win, '删除图表',
                    f'确认删除图表 "{title}" ？'
            ) != QMessageBox.Yes:
                return
            del self._visible_plots[idx]
            self._plots = list(self._visible_plots)
            self._rebuild_plots_ui()
            self._refresh_plot()
            win.statusBar().showMessage(
                '已删除图表——变更未保存，需手动"导出配置"')

        def _open_log():
            from PySide6.QtWidgets import QFileDialog
            start_dir = self._last_dir('qt_monitor_last_log_dir')
            path, _ = QFileDialog.getOpenFileName(
                win, '打开日志文件', start_dir,
                '日志 (*.txt *.log);;所有文件 (*)')
            if path:
                self._remember_dir('qt_monitor_last_log_dir', path)
                n = self.load_log(path)
                self._refresh_plot()
                win.statusBar().showMessage(
                    f'加载 {n} 行，{len(self._data)} 变量')

        def _import_plots_cfg():
            from PySide6.QtWidgets import QFileDialog, QMessageBox
            start_dir = self._last_dir('qt_monitor_last_import_cfg_dir')
            path, _ = QFileDialog.getOpenFileName(
                win, '导入曲线图配置', start_dir,
                '曲线图配置 (*.json);;所有文件 (*)')
            if not path:
                return
            self._remember_dir('qt_monitor_last_import_cfg_dir', path)
            try:
                n = self.import_plots_config(path)
                win.statusBar().showMessage(
                    f'已导入 {n} 个曲线图配置：{path}')
            except Exception as e:
                QMessageBox.critical(win, '导入失败', str(e))

        def _export_plots_cfg():
            from PySide6.QtWidgets import QFileDialog, QMessageBox
            if not self._visible_plots:
                QMessageBox.information(win, '导出', '当前没有曲线图配置')
                return
            start_dir = self._last_dir('qt_monitor_last_export_cfg_dir')
            default_path = os.path.join(start_dir, 'plots_config.json')
            path, _ = QFileDialog.getSaveFileName(
                win, '导出曲线图配置', default_path,
                '曲线图配置 (*.json);;所有文件 (*)')
            if not path:
                return
            self._remember_dir('qt_monitor_last_export_cfg_dir', path)
            try:
                self.export_plots_config(path)
                win.statusBar().showMessage(
                    f'已导出 {len(self._visible_plots)} 个曲线图配置：{path}')
            except Exception as e:
                QMessageBox.critical(win, '导出失败', str(e))

        def _mock_10w():
            self.gen_mock_data(100_000)
            self._refresh_plot()
            win.statusBar().showMessage('已生成 10w 模拟数据')

        def _mock_100w():
            self.gen_mock_data(1_000_000)
            self._refresh_plot()
            win.statusBar().showMessage('已生成 100w 模拟数据')

        def _clear():
            self._data.clear()
            self._data_cache.clear()
            self._refresh_plot()
            win.statusBar().showMessage('已清空')

        def _toggle_theme():
            # 工具栏按钮：在当前深浅的两个默认主题间切换
            cur_kind = themes.THEME_KIND.get(self.theme_name, 'dark')
            new_kind = 'light' if cur_kind == 'dark' else 'dark'
            new_theme = themes.load_specific(new_kind)
            self._apply_theme_with_pref(new_theme, pref=new_kind)

        def _toggle_sidebar():
            self._toggle_sidebar()

        def _show_stats():
            self._show_statistics(win)

        def _export_csv():
            from PySide6.QtWidgets import QFileDialog, QMessageBox
            if not self._data:
                QMessageBox.information(win, '导出 CSV', '没有可导出的数据')
                return
            start_dir = self._last_dir('qt_monitor_last_csv_dir')
            path, _ = QFileDialog.getSaveFileName(
                win, '导出 CSV', start_dir,
                'CSV (*.csv);;所有文件 (*)')
            if not path:
                return
            self._remember_dir('qt_monitor_last_csv_dir', path)
            try:
                n_rows, step = self._export_csv(path)
                QMessageBox.information(
                    win, '导出完成',
                    f'已导出 {n_rows:,} 行（采样步长 {step}）\n{path}')
            except Exception as e:
                QMessageBox.critical(win, '导出失败', str(e))

        def _connect_serial():
            from PySide6.QtWidgets import QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLineEdit, QMessageBox
            dlg = QDialog(win)
            dlg.setWindowTitle('连接串口（实时 feed）')
            form = QFormLayout(dlg)
            port_edit = QLineEdit('/dev/ttyCH343USB0')
            baud_box = QComboBox()
            for b in ('115200', '230400', '460800', '921600', '9600', '57600'):
                baud_box.addItem(b)
            baud_box.setCurrentText('921600')
            form.addRow('端口', port_edit)
            form.addRow('波特率', baud_box)
            btns = QDialogButtonBox(
                QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            form.addRow(btns)
            btns.accepted.connect(dlg.accept)
            btns.rejected.connect(dlg.reject)
            if dlg.exec() != QDialog.Accepted:
                return
            port = port_edit.text().strip()
            baud = int(baud_box.currentText())
            try:
                self.start_serial(port, baud)
                win.statusBar().showMessage(
                    f'已连接 {port} @ {baud}，实时数据流入')
            except Exception as e:
                QMessageBox.critical(win, '连接失败', str(e))

        def _disconnect_serial():
            try:
                self.stop_serial()
                win.statusBar().showMessage('已断开串口')
            except Exception as e:
                log_exc('warning', f'{__file__}:_disconnect_serial stop_serial failed', e)
                win.statusBar().showMessage(f'断开失败: {e}')

        def _perf_test():
            import time
            n = 1_000_000
            win.statusBar().showMessage(f'生成 {n} 点...')
            self._app.processEvents()
            t0 = time.perf_counter()
            self.gen_mock_data(n)
            t_gen = (time.perf_counter() - t0) * 1000
            win.statusBar().showMessage(f'渲染 {n} 点...')
            self._app.processEvents()
            t0 = time.perf_counter()
            self._refresh_plot()
            self._app.processEvents()
            t_draw = (time.perf_counter() - t0) * 1000
            msg = (f'性能基准（{n} 点 × 3 条曲线）:\n'
                   f'  数据生成: {t_gen:.1f} ms\n'
                   f'  渲染绘制: {t_draw:.1f} ms\n'
                   f'  总耗时:   {t_gen + t_draw:.1f} ms\n\n'
                   f'用鼠标拖动/滚轮缩放测试交互流畅度。')
            win.statusBar().showMessage(
                f'渲染 {n} 点完成: 数据 {t_gen:.0f}ms + 绘制 {t_draw:.0f}ms')
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(win, '性能基准', msg)

        # ---- 工具栏（精简：只保留最常用操作）----
        tb = QToolBar('工具')
        win.addToolBar(tb)
        self._toolbar = tb

        btn_add = QPushButton('添加图表…')
        btn_add.clicked.connect(_add_plot)
        tb.addWidget(btn_add)
        btn_edit = QPushButton('编辑图表…')
        btn_edit.clicked.connect(_edit_plot)
        tb.addWidget(btn_edit)
        btn_del = QPushButton('删除图表')
        btn_del.clicked.connect(_del_plot)
        tb.addWidget(btn_del)
        btn_popout = QPushButton('放大当前图…')
        btn_popout.clicked.connect(self._popout_current_plot)
        tb.addWidget(btn_popout)
        btn_show_all = QPushButton('显示全部')
        btn_show_all.clicked.connect(self._show_all_plots)
        btn_show_all.setToolTip('恢复所有被隐藏的曲线图')
        tb.addWidget(btn_show_all)
        tb.addSeparator()
        btn_parser_cfg = QPushButton('解析器配置…')
        btn_parser_cfg.clicked.connect(self._open_parser_config)
        btn_parser_cfg.setToolTip('配置 Python 解析代码 + 样本日志 + 格式校验')
        tb.addWidget(btn_parser_cfg)
        tb.addSeparator()
        btn_open = QPushButton('打开日志…')
        btn_open.clicked.connect(_open_log)
        tb.addWidget(btn_open)
        # 当前打开的文件名显示（load_log / 串口连接时更新）
        self._file_label = QLabel('（未加载）')
        self._file_label.setStyleSheet(
            'QLabel { color: #88aa88; padding: 0 6px; }')
        self._file_label.setToolTip('当前打开的日志文件 / 串口')
        tb.addWidget(self._file_label)
        tb.addSeparator()
        btn_import_cfg = QPushButton('导入配置…')
        btn_import_cfg.clicked.connect(_import_plots_cfg)
        tb.addWidget(btn_import_cfg)
        btn_export_cfg = QPushButton('导出配置…')
        btn_export_cfg.clicked.connect(_export_plots_cfg)
        tb.addWidget(btn_export_cfg)
        tb.addSeparator()
        btn_theme = QPushButton('切换主题')
        btn_theme.clicked.connect(_toggle_theme)
        tb.addWidget(btn_theme)
        btn_sidebar = QPushButton('切换侧栏')
        btn_sidebar.clicked.connect(_toggle_sidebar)
        tb.addWidget(btn_sidebar)
        tb.addSeparator()
        btn_serial = QPushButton('连串口实时 feed')
        btn_serial.clicked.connect(_connect_serial)
        tb.addWidget(btn_serial)
        btn_disc = QPushButton('断开串口')
        btn_disc.clicked.connect(_disconnect_serial)
        tb.addWidget(btn_disc)

        # ---- 菜单栏 ----
        self._build_menubar(win, _add_plot, _edit_plot, _del_plot,
                            _open_log, _mock_10w, _mock_100w, _clear,
                            _toggle_theme, _toggle_sidebar,
                            _show_stats, _export_csv,
                            _connect_serial, _disconnect_serial,
                            _perf_test,
                            _import_plots_cfg, _export_plots_cfg,
                            self._open_parser_config)

        # 状态栏
        try:
            import pyqtgraph
            import PySide6
            tech_label = QLabel(
                f'PySide6 {PySide6.__version__} | pyqtgraph {pyqtgraph.__version__}')
        except Exception:
            tech_label = QLabel('Qt 数据监视窗口')
        win.statusBar().addPermanentWidget(tech_label)
        win.statusBar().showMessage('就绪')

        # 关闭时保存几何 + plots + var_meta——让下次启动自动恢复
        def _close_event(event):
            # M2: closeEvent 必须显式释放 reader 线程 + QTimer
            # 不能裸 except 吞所有错（参见 Agent-Head/memory/FACT.md 教训 5）
            try:
                self.stop_serial()
            except Exception as e:
                log_exc('warning', f'{__file__}:_close_event stop_serial failed', e)
            # 显式停 _refresh_timer 并 deleteLater，避免 QTimer 无 parent 时泄露
            if getattr(self, '_refresh_timer', None) is not None:
                try:
                    self._refresh_timer.stop()
                    self._refresh_timer.deleteLater()
                except Exception as e:
                    log_exc('debug', f'{__file__}:_close_event timer cleanup failed', e)
                self._refresh_timer = None
            try:
                self._save_plots()
                self._save_var_meta()
                self._save_geometry()
            except Exception as e:
                log_exc('warning', f'{__file__}:_close_event persist state failed', e)
            event.accept()
        win.closeEvent = _close_event

        self._win = win

        # 构建初始 plots UI
        self._rebuild_plots_ui()
        # 填充侧栏（var_meta 已在 __init__ 加载）
        self._refresh_sidebar()
        self._refresh_sidebar_visibility()
        # 恢复持久化的窗口几何
        self._restore_geometry()
        # 任务3：运行中主题联动——轮询 config 主题变化（主程序切主题时子进程跟随）
        self._start_theme_watch()
        return win

    def _start_theme_watch(self):
        """启动 5 秒轮询：检查 config 主题是否与本进程不一致，是则应用。

        跨进程联动：主程序切主题写 config，子进程此处发现变化并跟随。
        跟随系统实时跟随：pref='auto' 时额外检测 detect_system_theme() 变化，
        系统切深浅即重新 resolve+apply（QSS 不会自动跟随系统，需主动 apply）。
        低频（5s）不影响性能，关闭窗口时 timer 随 win 释放。
        """
        from PySide6.QtCore import QTimer
        # 记住上次检测到的系统深浅，避免无变化时重复 apply
        try:
            self._last_system_kind = themes.detect_system_theme()
        except Exception:
            self._last_system_kind = None
        self._theme_watch_timer = QTimer(self._win)
        self._theme_watch_timer.setInterval(5000)
        self._theme_watch_timer.timeout.connect(self._check_theme_changed)
        self._theme_watch_timer.start()

    def _check_theme_changed(self):
        """检查 config 主题 + 系统深浅变化，必要时 apply。

        - 检测 config.theme_pref 变化（跨进程联动：主程序切主题写 config）
        - pref='auto' 时额外检测 detect_system_theme() 变化（跟随系统实时跟随）
        """
        from serial_debug.infra import config
        from serial_debug.infra.error_log import log_exc
        try:
            cfg = config.load()
            pref = cfg.get('theme_pref', 'auto')
            # 按当前 pref 解析应有主题
            expected = themes.resolve(pref)
            # 1) config/pref 变化（跨进程联动）
            if expected != self.theme_name:
                self.theme_pref = pref
                self._apply_theme_to_window(expected)
                # apply 后刷新系统深浅记忆，避免下一轮误判
                try:
                    self._last_system_kind = themes.detect_system_theme()
                except Exception as e:
                    log_exc('debug', 'qt_monitor: refresh _last_system_kind failed', e)
                return
            # 2) 跟随系统模式：系统深浅变化时重新 resolve+apply
            if pref == 'auto':
                try:
                    sys_kind = themes.detect_system_theme()
                except Exception as e:
                    log_exc('debug', 'qt_monitor: detect_system_theme failed', e)
                    sys_kind = None
                if sys_kind is not None and sys_kind != self._last_system_kind:
                    self._last_system_kind = sys_kind
                    new_theme = themes.resolve('auto')
                    if new_theme != self.theme_name:
                        self._apply_theme_to_window(new_theme)
        except Exception as e:
            log_exc('warning', 'qt_monitor: _check_theme_changed failed', e)


    def _loaded_var_meta(self) -> list[dict]:
        """返回当前已加载变量的元数据 + 静态注册表 + 自定义解析器发现的变量。"""
        out: list[dict] = []
        seen: set[str] = set()
        # 1. 已加载变量（self._data）
        for name in self._data.keys():
            label = name
            for v in _ALL_VARS:
                if v['name'] == name:
                    label = v.get('label', name)
                    break
            out.append({'name': name, 'label': label})
            seen.add(name)
        # 2. 静态注册表（用户预先配置 plot 时可用）
        for v in _ALL_VARS:
            if v['name'] not in seen:
                out.append({'name': v['name'], 'label': v.get('label', v['name'])})
                seen.add(v['name'])
        # 3. 自定义解析器发现的变量（在样本日志里出现过但还没加载）
        for v in _CUSTOM_PARSER_STATE.get('discovered_vars', []):
            name = v.get('name')
            if name and name not in seen:
                out.append({'name': name, 'label': v.get('label', name)})
                seen.add(name)
        return out

    def _build_menubar(self, win, _add_plot, _edit_plot, _del_plot,
                        _open_log, _mock_10w, _mock_100w, _clear,
                        _toggle_theme, _toggle_sidebar,
                        _show_stats, _export_csv,
                        _connect_serial, _disconnect_serial,
                        _perf_test,
                        _import_plots_cfg=None, _export_plots_cfg=None,
                        _open_parser_cfg=None):
        from PySide6.QtGui import QAction
        from PySide6.QtWidgets import QStyle
        mb = win.menuBar()
        style = win.style()

        def _icon(sp):
            try:
                return style.standardIcon(sp)
            except Exception:
                from PySide6.QtGui import QIcon
                return QIcon()

        # 文件菜单
        m_file = mb.addMenu('文件')
        act_open = QAction(_icon(QStyle.SP_DialogOpenButton), '打开日志…', win)
        act_open.setShortcut('Ctrl+O')
        act_open.triggered.connect(_open_log)
        m_file.addAction(act_open)
        m_file.addSeparator()
        if _import_plots_cfg is not None:
            act_import_cfg = QAction(
                _icon(QStyle.SP_DialogOpenButton), '导入曲线图配置…', win)
            act_import_cfg.triggered.connect(_import_plots_cfg)
            m_file.addAction(act_import_cfg)
        if _export_plots_cfg is not None:
            act_export_cfg = QAction(
                _icon(QStyle.SP_DialogSaveButton), '导出曲线图配置…', win)
            act_export_cfg.triggered.connect(_export_plots_cfg)
            m_file.addAction(act_export_cfg)
        m_file.addSeparator()
        act_csv = QAction(_icon(QStyle.SP_DialogSaveButton), '导出 CSV…', win)
        act_csv.triggered.connect(_export_csv)
        m_file.addAction(act_csv)

        # 视图菜单
        m_view = mb.addMenu('视图')
        self._act_sidebar = QAction('变量列表侧栏', win)
        self._act_sidebar.setCheckable(True)
        self._act_sidebar.setChecked(self._sidebar_visible)
        self._act_sidebar.triggered.connect(_toggle_sidebar)
        m_view.addAction(self._act_sidebar)
        self._act_toolbar = QAction('工具栏', win)
        self._act_toolbar.setCheckable(True)
        self._act_toolbar.setChecked(True)
        self._act_toolbar.triggered.connect(
            lambda checked: self._toolbar.setVisible(checked) if self._toolbar else None)
        m_view.addAction(self._act_toolbar)
        m_view.addSeparator()
        act_theme = QAction('切换主题', win)
        act_theme.setShortcut('Ctrl+T')
        act_theme.triggered.connect(_toggle_theme)
        m_view.addAction(act_theme)
        m_view.addSeparator()
        # 主题子菜单（任务3：模式组 + 主题组）
        self._build_theme_menu(m_view, win)

        # 工具菜单
        m_tools = mb.addMenu('工具')
        act_add = QAction('添加图表…', win)
        act_add.setShortcut('Ctrl+N')
        act_add.triggered.connect(_add_plot)
        m_tools.addAction(act_add)
        act_edit = QAction('编辑图表…', win)
        act_edit.triggered.connect(_edit_plot)
        m_tools.addAction(act_edit)
        act_del = QAction('删除图表…', win)
        act_del.triggered.connect(_del_plot)
        m_tools.addAction(act_del)
        m_tools.addSeparator()
        act_stats = QAction('统计…', win)
        act_stats.triggered.connect(_show_stats)
        m_tools.addAction(act_stats)
        m_tools.addSeparator()
        # 日志格式配置：菜单栏入口（与工具栏按钮同入口，命名更直观）
        if _open_parser_cfg is not None:
            act_parser_cfg = QAction('日志格式配置…', win)
            act_parser_cfg.setShortcut('Ctrl+Shift+P')
            act_parser_cfg.setToolTip(
                '配置 Python 解析代码 + 样本日志 + 格式校验')
            act_parser_cfg.triggered.connect(_open_parser_cfg)
            m_tools.addAction(act_parser_cfg)
            m_tools.addSeparator()
        act_mock10 = QAction('模拟 10w 点', win)
        act_mock10.triggered.connect(_mock_10w)
        m_tools.addAction(act_mock10)
        act_mock100 = QAction('模拟 100w 点', win)
        act_mock100.triggered.connect(_mock_100w)
        m_tools.addAction(act_mock100)
        act_perf = QAction('性能基准（100w 点）', win)
        act_perf.triggered.connect(_perf_test)
        m_tools.addAction(act_perf)
        m_tools.addSeparator()
        act_serial = QAction('连接串口实时 feed…', win)
        act_serial.triggered.connect(_connect_serial)
        m_tools.addAction(act_serial)
        act_disc = QAction('断开串口', win)
        act_disc.triggered.connect(_disconnect_serial)
        m_tools.addAction(act_disc)
        m_tools.addSeparator()
        act_clear = QAction('清空数据', win)
        act_clear.triggered.connect(_clear)
        m_tools.addAction(act_clear)

        # 帮助菜单
        m_help = mb.addMenu('帮助')
        act_about = QAction(_icon(QStyle.SP_MessageBoxInformation), '关于…', win)
        act_about.triggered.connect(self._show_about_dialog)
        m_help.addAction(act_about)

    def _show_about_dialog(self):
        from PySide6.QtWidgets import QMessageBox
        try:
            import pyqtgraph
            import PySide6
            versions = (f'PySide6: {PySide6.__version__}\n'
                        f'pyqtgraph: {pyqtgraph.__version__}\n'
                        f'Python: {sys.version.split()[0]}')
        except Exception as e:
            versions = f'版本信息获取失败: {e}'
        QMessageBox.information(
            self._win, '关于',
            'Qt 数据监视窗口\n\n'
            '技术栈:\n' + versions + '\n\n'
            '功能:\n'
            '  - 多 plot 子图（自动排版，左右分栏优先）\n'
            '  - 添加/编辑/删除 plot 对话框\n'
            '  - 配置持久化 / 加载日志 / 模拟数据\n'
            '  - 串口实时 feed / 主题切换 / 统计 / CSV 导出')

    def _build_sidebar(self):
        """构建左侧变量列表侧栏：颜色 | 名称 | 当前值 | 在图。

        - 颜色格点击 → QColorDialog 改色
        - 右键菜单：添加到当前 plot / 从当前 plot 移除 / 删除变量
        - 自动从 self._var_meta 派生行
        """
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QAction, QColor
        from PySide6.QtWidgets import (
            QHeaderView,
            QLabel,
            QMenu,
            QTreeWidget,
            QVBoxLayout,
            QWidget,
        )

        container = QWidget()
        v = QVBoxLayout(container)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        title = QLabel('变量列表')
        title.setStyleSheet('padding: 6px 10px; font-weight: bold;')
        v.addWidget(title)

        tree = QTreeWidget()
        tree.setColumnCount(4)
        tree.setHeaderLabels(['颜色', '名称', '当前值', '在图'])
        tree.setRootIsDecorated(False)
        tree.setUniformRowHeights(True)
        tree.setAllColumnsShowFocus(True)
        # 列宽策略：颜色窄、名称宽、当前值中、在图窄
        hdr = tree.header()
        hdr.setSectionResizeMode(0, QHeaderView.Fixed)
        hdr.resizeSection(0, 36)
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.Fixed)
        hdr.resizeSection(3, 28)
        # 整行选中
        tree.setSelectionMode(QTreeWidget.SingleSelection)
        tree.setSelectionBehavior(QTreeWidget.SelectRows)

        tree.setContextMenuPolicy(Qt.CustomContextMenu)

        def _ctx_menu(pos):
            item = tree.itemAt(pos)
            if item is None:
                return
            name = item.data(0, Qt.UserRole)
            if not name:
                return
            menu = QMenu(tree)

            def _add_to_current_plot():
                idx = self._select_plot_idx(tree, '添加到图表')
                if idx is None:
                    return
                # idx 是 _visible_plots 中的下标（_select_plot_idx 基于显示列表）
                vars_list = self._visible_plots[idx].setdefault('vars', [])
                if name not in vars_list:
                    vars_list.append(name)
                self._save_plots()
                self._refresh_plot()
                self._refresh_sidebar_visibility()

            def _remove_from_current_plot():
                idx = self._select_plot_idx(tree, '从图表移除')
                if idx is None:
                    return
                vars_list = self._visible_plots[idx].get('vars', [])
                while name in vars_list:
                    vars_list.remove(name)
                self._save_plots()
                self._refresh_plot()
                self._refresh_sidebar_visibility()

            def _add_to_all_plots():
                for p in self._visible_plots:
                    if name not in p.get('vars', []):
                        p.setdefault('vars', []).append(name)
                self._save_plots()
                self._refresh_plot()
                self._refresh_sidebar_visibility()

            def _remove_var():
                # 删除变量：从 var_meta / data / 所有 plot.vars 清理
                self._var_meta.pop(name, None)
                self._data.pop(name, None)
                for p in self._visible_plots:
                    while name in p.get('vars', []):
                        p['vars'].remove(name)
                self._save_plots()
                self._save_var_meta()
                self._refresh_plot()
                self._refresh_sidebar()

            act_add = QAction('添加到当前图表', menu)
            act_add.triggered.connect(_add_to_current_plot)
            menu.addAction(act_add)
            act_rm = QAction('从当前图表移除', menu)
            act_rm.triggered.connect(_remove_from_current_plot)
            menu.addAction(act_rm)
            act_all = QAction('添加到全部图表', menu)
            act_all.triggered.connect(_add_to_all_plots)
            menu.addAction(act_all)
            menu.addSeparator()
            act_del = QAction('删除变量', menu)
            act_del.triggered.connect(_remove_var)
            menu.addAction(act_del)
            menu.exec(tree.viewport().mapToGlobal(pos))

        tree.customContextMenuRequested.connect(_ctx_menu)

        # 颜色格点击 → 改色
        def _item_clicked(item, column):
            if column != 0:
                return
            name = item.data(0, Qt.UserRole)
            if not name:
                return
            from PySide6.QtWidgets import QColorDialog
            cur = self._var_meta.get(name, {}).get('color', '#ffffff')
            c = QColorDialog.getColor(QColor(cur), tree, '选择颜色')
            if c.isValid():
                self._var_meta.setdefault(name, {}).update(
                    {'color': c.name()})
                self._save_var_meta()
                self._refresh_sidebar()
                self._refresh_plot()

        tree.itemClicked.connect(_item_clicked)

        v.addWidget(tree, 1)
        self._sidebar = tree
        self._sidebar_container = container
        return container

    def _refresh_sidebar(self):
        """根据 self._var_meta 重建侧栏行。"""
        if self._sidebar is None:
            return
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QColor
        from PySide6.QtWidgets import QTreeWidgetItem
        tree = self._sidebar
        # 记住选中行
        sel_name = None
        cur = tree.currentItem()
        if cur is not None:
            sel_name = cur.data(0, Qt.UserRole)
        tree.clear()
        for name, meta in self._var_meta.items():
            it = QTreeWidgetItem()
            color = meta.get('color', '#569cd6') or '#569cd6'
            it.setData(0, Qt.UserRole, name)
            it.setText(0, '   ')
            it.setBackground(0, QColor(color))
            it.setText(1, meta.get('label') or name)
            it.setText(2, '—')   # 当前值，_refresh_sidebar_values 更新
            it.setTextAlignment(2, Qt.AlignRight | Qt.AlignVCenter)
            it.setText(3, '')    # 在图标记，_refresh_sidebar_visibility 更新
            it.setToolTip(1, name)
            tree.addTopLevelItem(it)
        # 恢复选中
        if sel_name is not None:
            for i in range(tree.topLevelItemCount()):
                it = tree.topLevelItem(i)
                if it.data(0, Qt.UserRole) == sel_name:
                    tree.setCurrentItem(it)
                    break

    def _refresh_sidebar_values(self):
        """更新侧栏各行的"当前值"列。"""
        if self._sidebar is None:
            return
        from PySide6.QtCore import Qt
        tree = self._sidebar
        for i in range(tree.topLevelItemCount()):
            it = tree.topLevelItem(i)
            name = it.data(0, Qt.UserRole)
            if not name or name not in self._data:
                continue
            pts = self._data[name]
            try:
                if (isinstance(pts, tuple) and len(pts) >= 2
                        and hasattr(pts[0], '__array_interface__')):
                    ys = pts[1]
                    if len(ys) == 0:
                        continue
                    val = float(ys[-1])
                else:
                    if not pts:
                        continue
                    val = float(pts[-1][1])
                # 数值格式：整数显示整数，浮点保留 3 位
                if val == int(val):
                    s = str(int(val))
                else:
                    s = f'{val:.3f}'
                it.setText(2, s)
            except Exception:
                pass

    def _refresh_sidebar_visibility(self):
        """更新侧栏"在图"列的勾选标记。

        plot.vars 为空 = 显示全部已加载变量，故所有 var 都算"在该 plot"。
        """
        if self._sidebar is None:
            return
        tree = self._sidebar
        # 把所有 plot 的 vars 并集，标记在哪些图出现
        # （空 vars 计为"全部 var 都在该 plot"）
        all_names = set(self._data.keys())
        counts: dict[str, int] = dict.fromkeys(all_names, 0)
        for p in self._visible_plots:
            vs = p.get('vars', [])
            if not vs:
                # 显示全部：每个已加载变量 +1
                for n in all_names:
                    counts[n] = counts.get(n, 0) + 1
            else:
                for name in vs:
                    if name in all_names:
                        counts[name] = counts.get(name, 0) + 1
        from PySide6.QtCore import Qt
        for i in range(tree.topLevelItemCount()):
            it = tree.topLevelItem(i)
            name = it.data(0, Qt.UserRole)
            if not name:
                continue
            c = counts.get(name, 0)
            it.setText(3, '✓' if c > 0 else '')
            it.setToolTip(3, f'在 {c} 个图表中')

    def _toggle_sidebar(self):
        """切换侧栏显隐。"""
        from serial_debug.infra import config
        if self._sidebar_container is None:
            return
        self._sidebar_visible = not self._sidebar_visible
        self._sidebar_container.setVisible(self._sidebar_visible)
        if hasattr(self, '_act_sidebar') and self._act_sidebar is not None:
            self._act_sidebar.setChecked(self._sidebar_visible)
        try:
            cfg = config.load()
            cfg['qt_monitor_sidebar_visible'] = self._sidebar_visible
            config.save(cfg)
        except Exception:
            pass

    # ---- 统计 & CSV 导出 --------------------------------------------
    def _get_xy(self, name: str):
        """取一个变量的 (xs, ys) numpy 数组。

        优先从 _data_cache 取；缓存为空或 list 长度变化则重新转换。
        _data 可以是 list[(ts, val)] 或 numpy 元组（gen_mock_data）。
        """
        import numpy as np
        with self._data_lock:
            pts = self._data.get(name)
            if pts is None:
                return None, None
            # numpy 元组直接用（gen_mock_data 路径）
            if (isinstance(pts, tuple) and len(pts) >= 2
                    and hasattr(pts[0], '__array_interface__')):
                return pts[0], pts[1]
            # list 路径：优先用缓存（检查 list 长度是否变化）
            cached = self._data_cache.get(name)
            if cached is not None and len(pts) == cached[2]:
                return cached[0], cached[1]
            if not pts:
                return None, None
            try:
                arr = np.asarray(pts, dtype=np.float64)
                xs = arr[:, 0]
                ys = arr[:, 1]
                self._data_cache[name] = (xs, ys, len(pts))
                return xs, ys
            except Exception:
                return None, None

    def _compute_var_stats(self, name: str) -> dict | None:
        """计算单变量统计：current/min/max/avg/n/first_ts/last_ts/duration。"""
        import numpy as np
        xs, ys = self._get_xy(name)
        if xs is None or len(ys) == 0:
            return None
        return {
            'current': float(ys[-1]),
            'min': float(np.min(ys)),
            'max': float(np.max(ys)),
            'avg': float(np.mean(ys)),
            'n': int(len(ys)),
            'first_ts': float(xs[0]),
            'last_ts': float(xs[-1]),
            'duration': float(xs[-1] - xs[0]),
        }

    def _show_statistics(self, parent_win):
        """弹窗显示每变量统计信息。"""
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import (
            QDialog,
            QDialogButtonBox,
            QHeaderView,
            QLabel,
            QTableWidget,
            QTableWidgetItem,
            QVBoxLayout,
        )
        dlg = QDialog(parent_win)
        dlg.setWindowTitle('统计信息')
        dlg.resize(720, 480)
        v = QVBoxLayout(dlg)
        v.setContentsMargins(8, 8, 8, 8)
        info = QLabel(f'共 {len(self._data)} 个变量，统计基于当前已加载数据')
        info.setStyleSheet('padding: 2px 4px; color: #888;')
        v.addWidget(info)

        cols = ['变量名', '说明', '当前值', '最小', '最大', '均值',
                '点数', '时长(s)']
        tw = QTableWidget(len(self._data), len(cols), dlg)
        tw.setHorizontalHeaderLabels(cols)
        tw.setEditTriggers(QTableWidget.NoEditTriggers)
        tw.verticalHeader().setVisible(False)
        tw.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        tw.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        tw.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        for row, name in enumerate(self._data.keys()):
            meta = self._var_meta.get(name, {})
            stats = self._compute_var_stats(name)
            label = meta.get('label') or name
            if stats is None:
                vals = [name, label, '—', '—', '—', '—', 0, '—']
            else:
                vals = [
                    name,
                    label,
                    self._fmt_num(stats['current']),
                    self._fmt_num(stats['min']),
                    self._fmt_num(stats['max']),
                    self._fmt_num(stats['avg']),
                    f"{stats['n']:,}",
                    f"{stats['duration']:.2f}",
                ]
            for col, text in enumerate(vals):
                item = QTableWidgetItem(str(text))
                if col == 0:
                    item.setToolTip(name)
                # 数值列右对齐
                if col >= 2:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                tw.setItem(row, col, item)
        v.addWidget(tw, 1)

        btns = QDialogButtonBox(QDialogButtonBox.Close)
        btns.rejected.connect(dlg.reject)
        btns.accepted.connect(dlg.accept)
        v.addWidget(btns)
        dlg.exec()

    @staticmethod
    def _fmt_num(v: float) -> str:
        if v == int(v):
            return str(int(v))
        return f'{v:.3f}'

    def _export_csv(self, path: str, max_rows: int = 100_000) -> tuple[int, int]:
        """导出当前 self._data 到 CSV（wide format，按行号对齐）。

        每变量一列，行号对齐（不同变量长度不同时短列补空）。
        数据量超过 max_rows 时按等间隔采样降采样到 max_rows，避免巨型 CSV。
        返回 (实际行数, 采样步长)。
        """
        names = list(self._data.keys())
        if not names:
            raise ValueError('没有可导出的数据')
        # 取所有变量的 ys（不合并时间戳，按行号对齐）
        ys_list = []
        max_n = 0
        for name in names:
            _, ys = self._get_xy(name)
            if ys is None:
                ys_list.append([])
                continue
            ys_list.append(ys)
            if len(ys) > max_n:
                max_n = len(ys)
        if max_n == 0:
            raise ValueError('所有变量均为空')
        # 采样步长（>=1）
        step = (max_n + max_rows - 1) // max_rows if max_n > max_rows else 1
        out_rows = (max_n + step - 1) // step
        with open(path, 'w', encoding='utf-8') as f:
            f.write('row,' + ','.join(names) + '\n')
            for i in range(0, max_n, step):
                row_idx = i // step
                row = [str(row_idx)]
                for j, ys in enumerate(ys_list):
                    if i < len(ys):
                        v = float(ys[i])
                        # 整数显示整数，浮点保留 6 位有效
                        if v == int(v):
                            row.append(str(int(v)))
                        else:
                            row.append(f'{v:.6g}')
                    else:
                        row.append('')
                f.write(','.join(row) + '\n')
        return out_rows, step

    def _select_plot_idx(self, parent, title: str) -> int | None:
        """弹出对话框让用户选择一个图表，返回索引；取消返回 None。

        基于 _visible_plots（当前视图），不是 _plots（原始配置）。
        """
        from PySide6.QtWidgets import (
            QComboBox,
            QDialog,
            QDialogButtonBox,
            QLabel,
            QVBoxLayout,
        )
        if not self._visible_plots:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(parent, title, '没有可操作的图表')
            return None
        dlg = QDialog(parent)
        dlg.setWindowTitle(title)
        dlg.setMinimumWidth(320)
        v = QVBoxLayout(dlg)
        v.setContentsMargins(12, 12, 12, 12)
        v.setSpacing(8)
        v.addWidget(QLabel('选择图表：'))
        combo = QComboBox()
        for i, p in enumerate(self._visible_plots):
            vars_info = ''
            pv = p.get('vars', [])
            if pv:
                vars_info = f'（{len(pv)} 变量）'
            else:
                vars_info = '（全部变量）'
            combo.addItem(f'{i + 1}. {p.get("title", "数据曲线")}{vars_info}')
        v.addWidget(combo)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        v.addWidget(btns)
        if dlg.exec() != QDialog.Accepted:
            return None
        return combo.currentIndex()

    def _current_plot_idx(self) -> int | None:
        """返回当前聚焦/选中的 plot 索引。

        简化实现：如果有 plot，返回第一个（暂无选中态）。
        后续 Stage 加 focus 跟踪后再改。
        """
        if not self._plot_widgets:
            return None
        return 0

    def _rebuild_plots_ui(self):
        """根据 self._visible_plots 重建 PlotWidget 列表（增删改 plot 后调用）。

        使用 _visible_plots（运行时显示列表）而非 _plots（原始配置）——
        用户在本会话里 X 关闭的 plot 不会在添加新 plot 时复活。

        布局：嵌套 QSplitter。外层 vertical splitter 每行一个 horizontal
        splitter，水平 splitter 装 cols 个 PlotWidget。默认等分（视觉
        等于原来的 QGridLayout 网格），用户拖动分隔条可调节每个曲线图
        的宽高。setChildrenCollapsible(False) 防止拖到 0，最小尺寸由
        PlotWidget.setMinimumHeight/Width 兜底。

        列数规则与原 QGridLayout 版本一致：优先左右分栏。
        1 个图 → 1 列，2 个图 → 2 列，3 个图 → 2 列 + 1 行，
        4 个图 → 2×2，5 个图 → 3 列 + 2 行，以此类推。
        """
        import pyqtgraph as pg
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QSizePolicy, QSplitter

        from serial_debug.ui import qt_theme
        t = qt_theme.palette(self.theme_name)

        layout = self._plots_layout
        if layout is None:
            return
        # 防闪烁：增删 plot 时关闭窗口级重绘，rebuild 完成后一次性恢复
        win = self._win
        host = win if win is not None else layout.parent()
        if host is not None:
            try:
                host.setUpdatesEnabled(False)
            except Exception:
                host = None
        try:
            self._do_rebuild_plots(layout, pg, Qt, QSizePolicy, QSplitter, t)
        finally:
            if host is not None:
                try:
                    host.setUpdatesEnabled(True)
                    host.update()
                except Exception:
                    pass

    def _do_rebuild_plots(self, layout, pg, Qt, QSizePolicy, QSplitter, t):
        """实际 rebuild 逻辑（_rebuild_plots_ui 包了防闪烁后调用）。"""
        # 先保存旧布局（splitter sizes），rebuild 后尝试恢复——避免每次增删
        # plot 都重置成等分布局
        old_sizes = self._dump_splitter_sizes()
        # 清空旧内容（之前的 v_splitter 或残留 widget）
        for i in reversed(range(layout.count())):
            item = layout.itemAt(i)
            if item is None:
                continue
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._plot_widgets = []
        self._curves = []
        self._plot_headers = []
        self._plot_containers = []

        visible = self._visible_plots
        n = len(visible)
        if n == 0:
            return
        # 计算列数：优先左右分栏（与原 QGridLayout 一致）
        if n <= 1:
            cols = 1
        elif n <= 2:
            cols = 2
        elif n <= 4:
            cols = 2
        elif n <= 6:
            cols = 3
        else:
            cols = max(3, (n + 1) // 2)

        # 垂直 splitter（每行一个 horizontal splitter）
        v_splitter = QSplitter(Qt.Vertical)
        v_splitter.setHandleWidth(6)
        v_splitter.setChildrenCollapsible(False)
        self._v_splitter = v_splitter

        rows = (n + cols - 1) // cols
        for row in range(rows):
            h_splitter = QSplitter(Qt.Horizontal)
            h_splitter.setHandleWidth(6)
            h_splitter.setChildrenCollapsible(False)
            for col in range(cols):
                idx = row * cols + col
                if idx >= n:
                    break
                p = visible[idx]
                container, pw, header = self._make_plot_widget(p, t, idx)
                self._plot_widgets.append(pw)
                self._curves.append({})
                self._plot_headers.append(header)
                self._plot_containers.append(container)
                h_splitter.addWidget(container)
            v_splitter.addWidget(h_splitter)

        layout.addWidget(v_splitter)
        # 尝试恢复旧布局（增删 plot 时若行列数相同则保留用户拖动过的尺寸）
        self._apply_splitter_sizes(old_sizes)

    def _apply_splitter_sizes(self, sizes: dict):
        """把保存的 sizes 应用到当前 _v_splitter + 子 h_splitter。

        行列数不匹配时跳过对应部分（Qt setSizes 不匹配长度会忽略，
        但我们更明确地按子 splitter 数量做对齐）。
        """
        if not isinstance(sizes, dict) or not sizes:
            return
        try:
            v = sizes.get('v')
            if v and self._v_splitter is not None and \
                    len(v) == self._v_splitter.count():
                self._v_splitter.setSizes([int(x) for x in v])
        except Exception:
            pass
        try:
            h_list = sizes.get('h', [])
            if not isinstance(h_list, list):
                return
            h_idx = 0
            for i in range(self._v_splitter.count()):
                w = self._v_splitter.widget(i)
                if w is None or w.__class__.__name__ != 'QSplitter':
                    continue
                if h_idx >= len(h_list):
                    break
                cur = h_list[h_idx]
                h_idx += 1
                if isinstance(cur, list) and len(cur) == w.count():
                    try:
                        w.setSizes([int(x) for x in cur])
                    except Exception:
                        pass
        except Exception:
            pass

    # ---- plot 容器构建（顶部标题+图例条 + PlotWidget）---------------
    def _make_plot_widget(self, p: dict, t: dict, idx: int | None = None):
        """创建 plot 容器：顶部标题+图例条（同一行）+ PlotWidget。

        返回 (container, plot_widget, header)。header 存 title_label /
        legend_box / legend_flow / legend_items / close_btn 引用，便于刷新。

        idx 为本 plot 在 self._plots 中的下标；为 None 表示 popout 单窗口，
        不渲染 X 关闭按钮（QDialog 自带原生关闭）。
        """
        import pyqtgraph as pg
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QFont
        from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout

        from serial_debug.ui.qt_flow_layout import QFlowLayout

        pw = pg.PlotWidget(axisItems={'bottom': _make_time_axis(t)})
        pw.showGrid(x=True, y=True, alpha=0.2)
        pw.setLabel('left', p.get('y_label', '值'))
        pw.setLabel('bottom', '时间')
        pw.setBackground(t.get('panel_bg', t.get('bg', '#1e1e1e')))
        for axis_name in ('left', 'bottom'):
            axis = pw.getAxis(axis_name)
            if axis is not None:
                axis.setPen(t.get('muted', '#cccccc'))
                axis.setTextPen(t.get('fg', '#cccccc'))
        pw.setMinimumHeight(120)
        pw.setMinimumWidth(150)
        sp = pw.sizePolicy()
        sp.setVerticalPolicy(QSizePolicy.Expanding)
        sp.setHorizontalPolicy(QSizePolicy.Expanding)
        pw.setSizePolicy(sp)
        # 首次创建时启用 autoRange 让曲线立即可见；之后用户手动 zoom 会
        # 自动 disable autoRange，增删曲线 / checkbox 切换不再重置视图。
        pw.enableAutoRange(x=True, y=True)
        # 标记：后续 _refresh_plot 不再调 enableAutoRange（依赖此初始 auto 状态）
        pw._auto_ranged_init = True

        header_frame = QFrame()
        header_frame.setObjectName('plotHeader')
        header_frame.setStyleSheet(
            f"background-color: {t.get('panel_bg', t.get('bg', '#1e1e1e'))};"
            f"border-bottom: 1px solid {t.get('muted', '#3a3a3a')};")
        header_layout = QHBoxLayout(header_frame)
        header_layout.setContentsMargins(10, 4, 10, 4)
        header_layout.setSpacing(10)

        title_label = QLabel(p.get('title', '数据曲线'))
        title_font = QFont()
        title_font.setBold(True)
        title_font.setPointSize(int(p.get('legend_font_size', 9)) + 1)
        title_label.setFont(title_font)
        title_label.setStyleSheet(f"color: {t.get('fg', '#cccccc')};"
                                  "background: transparent; border: none;")
        header_layout.addWidget(title_label)

        legend_box = QFrame()
        legend_box.setObjectName('plotLegendBox')
        legend_box.setStyleSheet("background: transparent; border: none;")
        legend_flow = QFlowLayout()
        legend_flow.setContentsMargins(0, 0, 0, 0)
        legend_flow.setSpacing(8)
        legend_box.setLayout(legend_flow)
        header_layout.addWidget(legend_box, 1)

        close_btn = None
        if idx is not None:
            close_btn = QPushButton('×')
            close_btn.setObjectName('plotCloseBtn')
            close_btn.setFixedSize(20, 20)
            close_btn.setCursor(Qt.PointingHandCursor)
            close_btn.setToolTip('隐藏本曲线图（仅本次视图，不修改配置；'
                                  '可通过工具栏"显示全部"恢复）')
            close_btn.setStyleSheet(
                "QPushButton { border: none; background: transparent;"
                f" color: {t.get('muted', '#cccccc')};"
                " font-size: 16px; font-weight: bold; padding: 0; }"
                "QPushButton:hover {"
                f" color: {t.get('accent', '#ff5555')}; }}")
            close_btn.clicked.connect(
                lambda _=False, i=idx: self._hide_plot(i))
            header_layout.addWidget(close_btn)

        container = QFrame()
        container.setObjectName('plotContainer')
        container.setStyleSheet(
            f"background-color: {t.get('panel_bg', t.get('bg', '#1e1e1e'))};"
            "border: none;")
        cv = QVBoxLayout(container)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(0)
        cv.addWidget(header_frame)
        cv.addWidget(pw, 1)

        header = {
            'frame': header_frame,
            'title_label': title_label,
            'legend_box': legend_box,
            'legend_flow': legend_flow,
            'legend_items': {},  # name -> (square_label, text_label)
            'close_btn': close_btn,
        }
        return container, pw, header

    def _hide_plot(self, idx: int):
        """从当前视图移除指定 plot（仅运行时显示，不写配置）。

        不调 setVisible(False)——直接从 _visible_plots 移除 + 重建布局，
        这样后续添加新 plot 时不会复活本图，剩余 plot 自动填空（QSplitter
        原生行为）。配置 _plots 不动，"显示全部"按钮可恢复。
        """
        if idx < 0 or idx >= len(self._visible_plots):
            return
        removed = self._visible_plots.pop(idx)
        self._rebuild_plots_ui()
        self._refresh_plot()
        if self._win is not None:
            try:
                self._win.statusBar().showMessage(
                    f'已从视图移除：{removed.get("title", "曲线")}（'
                    '工具栏点"显示全部"可从配置恢复）', 4000)
            except Exception:
                pass

    def _show_all_plots(self):
        """从配置 _plots 恢复所有曲线（撤销本会话里所有 X 关闭）。"""
        self._visible_plots = list(self._plots)
        self._rebuild_plots_ui()
        self._refresh_plot()
        if self._win is not None:
            try:
                self._win.statusBar().showMessage(
                    f'已从配置恢复 {len(self._visible_plots)} 个曲线图', 3000)
            except Exception:
                pass

    def _update_file_label(self, label: str) -> None:
        """更新工具栏上的文件名显示。load_log / 串口连接时调。"""
        self._current_file_label = label or ''
        if self._file_label is not None:
            try:
                # 仅显示文件名（避免过长），完整路径在 tooltip
                import os
                short = label
                if label and os.path.isfile(label):
                    short = os.path.basename(label)
                self._file_label.setText(short or '（未加载）')
                self._file_label.setToolTip(label or '当前打开的日志文件 / 串口')
            except Exception:
                pass

    def _open_parser_config(self):
        """打开"日志格式配置"对话框：贴代码 + 样本日志 → 测试 → 保存。

        保存成功后，若当前已加载日志文件，自动重解析全量数据（用新解析器
        重跑 _iter_panel_vars），让用户改完立即看到曲线变化；若串口在连，
        清旧解析数据点（无原始行缓存，无法重解析历史，新行自动用新解析器），
        状态栏提示用户后续行会按新配置解析。
        """
        win = self._win
        if win is None:
            return
        dlg = _QtParserConfigDialog(win, callback_save=self._save_custom_parser_state)
        res = dlg.exec()
        if res:
            n = len(_CUSTOM_PARSER_STATE.get('discovered_vars', []))
            try:
                win.statusBar().showMessage(
                    f'解析器配置已保存：mode={_CUSTOM_PARSER_STATE["mode"]}'
                    f'，{n} 个发现变量', 5000)
            except Exception:
                pass
            # 重解析：让用户改完立即看到曲线变化
            self._reparse_after_config_change()

    def _reparse_after_config_change(self):
        """解析器配置变更后重解析当前数据源。

        - 日志文件场景：重读文件重跑 _iter_panel_vars，曲线立即按新规则刷新。
        - 串口场景：清旧解析数据点（无原始行缓存无法重解析历史），后续新行
          自动用新解析器，状态栏提示。
        - 无数据源：仅刷新侧栏/plot 预览。
        """
        win = self._win
        log_path = getattr(self, 'log_path', '') or ''
        if log_path:
            try:
                n = self.load_log(log_path)
                if win is not None:
                    win.statusBar().showMessage(
                        f'已用新解析器重新解析：{n} 行，{len(self._data)} 变量',
                        5000)
            except Exception as e:
                log_exc('warning',
                        f'{__file__}:_reparse_after_config_change reload failed', e)
                try:
                    self._refresh_plot()
                    self._refresh_sidebar()
                    self._refresh_sidebar_visibility()
                except Exception:
                    pass
        elif getattr(self, '_serial', None) is not None and self._serial.is_open:
            # 串口在连：清旧解析数据，新行自动用新解析器
            with self._data_lock:
                self._data.clear()
                self._data_cache.clear()
            try:
                self._refresh_plot()
                self._refresh_sidebar()
                self._refresh_sidebar_visibility()
            except Exception:
                pass
            if win is not None:
                win.statusBar().showMessage(
                    '已切换解析器，历史数据已清空；后续新行按新配置解析', 5000)
        else:
            try:
                self._refresh_plot()
                self._refresh_sidebar()
                self._refresh_sidebar_visibility()
            except Exception:
                pass

    def _validate_log_format(self, lines: list, source_label: str) -> None:
        """扫前 sample_lines 行，成功解析 ≥ min_count 条即视为规则正确；
        否则弹非模态警告。

        :param lines: 已读到的日志行（日志文件场景）或串口缓冲行
            （feed 场景）。仅取前 N 行做样本
        :param source_label: '日志 /tmp/xxx.log' 或 '串口 /dev/ttyUSB0@921600'
        """
        cfg = _CUSTOM_PARSER_STATE.get('validation', {})
        if not cfg.get('enabled', True):
            return
        try:
            sample_n = max(10, int(cfg.get('sample_lines', 200)))
            min_count = max(1, int(cfg.get('min_parsed_count', 10)))
        except (TypeError, ValueError):
            sample_n, min_count = 200, 10
        scan = list(lines)[:sample_n]
        if not scan:
            return
        total = len(scan)
        parsed = 0
        for line in scan:
            try:
                got = list(_iter_panel_vars(line))
            except Exception:
                got = []
            if got:
                parsed += 1
        win = self._win
        if win is None:
            return
        if parsed >= min_count:
            try:
                rate = parsed / total if total else 0.0
                win.statusBar().showMessage(
                    f'{source_label} 格式校验通过：成功解析 {parsed}/{total} 行'
                    f'（≥ 阈值 {min_count}，命中率 {rate:.0%}）', 4000)
            except Exception:
                pass
            return
        self._show_validation_warning(source_label, total, parsed, min_count)

    def _show_validation_warning(self, source_label, total, parsed, min_count):
        """非模态警告对话框：提示+提供"打开解析器配置"/"忽略"。"""
        from PySide6.QtWidgets import QMessageBox
        msg = (
            f'解析成功条数过低！\n\n'
            f'数据源：{source_label}\n'
            f'扫描 {total} 行，成功解析 {parsed} 行'
            f'（< 阈值 {min_count}）\n\n'
            f'可能原因：\n'
            f'• 日志关键字 / tag 名变了，与解析器代码不匹配\n'
            f'• 字段位置变了（位数不匹配）\n'
            f'• 用了新的日志格式但解析器配置还是旧的\n'
            f'• 日志中混入大量非数据行（启动 banner / 注释等）\n\n'
            f'建议打开"解析器配置"对话框修改代码 / 样本日志 / 重新发现变量，'
            f'或调低阈值 / 关闭校验。'
        )
        dlg = QMessageBox(self._win)
        dlg.setIcon(QMessageBox.Warning)
        dlg.setWindowTitle('解析器配置与数据不匹配')
        dlg.setText(msg)
        btn_cfg = dlg.addButton('打开解析器配置…', QMessageBox.AcceptRole)
        dlg.addButton('忽略并继续', QMessageBox.RejectRole)
        dlg.setModal(False)

        def _on_finished(_result):
            try:
                if dlg.clickedButton() is btn_cfg:
                    self._open_parser_config()
            except Exception:
                pass
        dlg.finished.connect(_on_finished)
        dlg.show()

    def _refresh_header_legend(self, header, vars_to_show, p: dict):
        """刷新顶部条：标题文本 + 重建图例项（checkbox + label）。

        每个图例项是一个 QCheckBox（无文字）+ QLabel（变量名）：
          - checkbox 勾选 → 曲线显示，indicator 填充曲线颜色
          - checkbox 未勾选 → 曲线隐藏，indicator 透明

        基于 vars_to_show（plot 配置的变量列表，含被取消勾选的），
        而非 curves（实际绘制的曲线）——否则取消勾选后 chip 消失，
        用户无法重新勾选。
        """
        if header is None:
            return
        title = p.get('title', '数据曲线')
        if header['title_label'].text() != title:
            header['title_label'].setText(title)
        legend_pos = p.get('legend_pos', 'top-right')
        if legend_pos == 'hidden':
            header['legend_box'].setVisible(False)
            return
        header['legend_box'].setVisible(True)
        font_size = int(p.get('legend_font_size', 9))
        from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QWidget

        from serial_debug.ui import qt_theme
        t = qt_theme.palette(self.theme_name)
        vis = p.get('var_visibility') or {}
        with self._data_lock:
            all_keys = list(self._data.keys())
        # 移除不再属于该 plot 的图例项
        current = set(vars_to_show)
        existing = set(header['legend_items'].keys())
        for name in list(existing - current):
            chip = header['legend_items'].pop(name)
            try:
                header['legend_flow'].removeWidget(chip)
                chip.setParent(None)
                chip.deleteLater()
            except Exception:
                pass
        muted = t.get('muted', '#888888')
        fg = t.get('fg', '#cccccc')
        for name in vars_to_show:
            color = self._var_color(name, all_keys)
            meta = self._var_meta.get(name, {})
            label = meta.get('label') or name
            checked = bool(vis.get(name, True))
            if name in header['legend_items']:
                chip = header['legend_items'][name]
                cb = chip.findChild(QCheckBox)
                # checkbox 是 chip 的第一个子控件（findChildren 顺序：先 cb 后 label）
                if cb is not None:
                    # 阻塞信号避免 setChecked 触发 _on_legend_toggled 死循环
                    try:
                        cb.blockSignals(True)
                        cb.setChecked(checked)
                    finally:
                        cb.blockSignals(False)
                    cb.setStyleSheet(self._legend_checkbox_qss(color, muted))
                # 更新文字 label
                labels = chip.findChildren(QLabel)
                if labels and labels[0].text() != label:
                    labels[0].setText(label)
                try:
                    f = labels[0].font() if labels else None
                    if f is not None:
                        f.setPointSize(font_size)
                        labels[0].setFont(f)
                except Exception:
                    pass
            else:
                chip = QWidget()
                chip.setStyleSheet("background: transparent; border: none;")
                cl = QHBoxLayout(chip)
                cl.setContentsMargins(0, 0, 0, 0)
                cl.setSpacing(4)
                cb = QCheckBox()
                cb.setChecked(checked)
                cb.setStyleSheet(self._legend_checkbox_qss(color, muted))
                # 关联 toggled 信号到 plot 索引（_visible_plots 中的下标）
                # 不能直接闭包 plot_idx——_visible_plots 后续可能增删导致
                # idx 漂移。改用 chip 引用 + 实时查 idx。
                cb.stateChanged.connect(
                    self._make_legend_toggled_cb(name, chip))
                txt = QLabel(label)
                txt.setStyleSheet(
                    f"color: {fg};"
                    f"font-size: {font_size}pt;"
                    "background: transparent; border: none;")
                cl.addWidget(cb)
                cl.addWidget(txt)
                header['legend_flow'].addWidget(chip)
                header['legend_items'][name] = chip

    def _legend_checkbox_qss(self, color: str, muted: str) -> str:
        """生成 QCheckBox 样式表：勾选时 indicator 填充曲线颜色，
        未勾选时透明；边框统一用 muted。"""
        return (
            f"QCheckBox::indicator {{ width: 12px; height: 12px;"
            f" border: 1px solid {muted}; }}"
            f"QCheckBox::indicator:checked {{ background-color: {color}; }}"
            "QCheckBox::indicator:unchecked { background-color: transparent; }"
            "QCheckBox { background: transparent; border: none; padding: 0; }"
        )

    def _make_legend_toggled_cb(self, var_name: str, chip):
        """构造 checkbox stateChanged 回调：更新 plot 配置 + 仅重画本 plot。

        通过 chip 反查 header/plot_idx（避免 plot_idx 闭包在 _visible_plots
        增删后失效）。
        """
        def _cb(_state):
            # 找到包含此 chip 的 header
            for plot_idx, header in enumerate(self._plot_headers):
                if header is None:
                    continue
                if header['legend_items'].get(var_name) is chip:
                    p = self._visible_plots[plot_idx]
                    vv = p.setdefault('var_visibility', {})
                    vv[var_name] = bool(_state == 2)  # Qt.Checked=2
                    # 只重画本 plot，不刷全部
                    self._refresh_single_plot(plot_idx)
                    return
        return _cb

    def _refresh_single_plot(self, plot_idx: int):
        """只刷新指定 plot 的曲线 + 图例（不重画所有 plot）。

        legend checkbox 切换时调——避免全量 _refresh_plot 抖动其他 plot。
        """
        if plot_idx < 0 or plot_idx >= len(self._visible_plots):
            return
        if plot_idx >= len(self._plot_widgets):
            return
        import pyqtgraph as pg
        p = self._visible_plots[plot_idx]
        pw = self._plot_widgets[plot_idx]
        curves = self._curves[plot_idx]
        with self._data_lock:
            all_data_keys = list(self._data.keys())
        vars_to_show = p.get('vars') or all_data_keys
        vis = p.get('var_visibility') or {}
        for name in list(curves.keys()):
            if name not in vars_to_show or not vis.get(name, True):
                try:
                    pw.removeItem(curves[name])
                except Exception:
                    pass
                del curves[name]
        for name in vars_to_show:
            if name not in self._data:
                continue
            if not vis.get(name, True):
                continue
            pts = self._data[name]
            if not pts:
                continue
            xs, ys = self._get_xy(name)
            if xs is None or len(xs) == 0:
                continue
            color = self._var_color(name, all_data_keys)
            meta = self._var_meta.get(name, {})
            label = meta.get('label') or name
            if name in curves:
                item = curves[name]
                try:
                    item.setData(xs, ys, pen=pg.mkPen(color, width=1.5))
                except Exception:
                    pass
            else:
                item = pw.plot(xs, ys, pen=pg.mkPen(color, width=1.5),
                               name=label)
                curves[name] = item
            item._legend_color = color
        # checkbox 切换时不重置视图——保留用户手动 zoom 状态。
        # 仅 _make_plot_widget 创建 pw 时调一次 enableAutoRange 让曲线首屏可见。
        if plot_idx < len(self._plot_headers):
            self._refresh_header_legend(
                self._plot_headers[plot_idx], vars_to_show, p)
        self._draw_stat_markers(pw, curves, p)
        self._refresh_sidebar_visibility()

    def _var_color(self, name: str, all_data_keys: list) -> str:
        """取变量颜色：优先 var_meta.color，缺省回退到调色板（按数据顺序）。"""
        meta = self._var_meta.get(name, {})
        c = meta.get('color')
        if c:
            return c
        try:
            idx = (all_data_keys.index(name) if name in all_data_keys else 0)
        except ValueError:
            idx = 0
        return self._palette[idx % len(self._palette)]

    @staticmethod
    def _curve_color(item) -> str:
        """从 PlotDataItem 取颜色 hex 字符串，失败回退灰色。

        颜色在 _refresh_plot 创建曲线时存到 item._legend_color（pen
        反推不可靠：QPen.color() 是方法不是属性，getattr 行为容易错）。
        """
        try:
            c = getattr(item, '_legend_color', None)
            if isinstance(c, str) and c:
                return c
            # 兜底：尝试从 pen.color() 取（部分 pyqtgraph 版本有效）
            pen = item.opts.get('pen') if hasattr(item, 'opts') else None
            if pen is not None and hasattr(pen, 'color'):
                qc = pen.color()
                if hasattr(qc, 'name'):
                    return qc.name()
            return '#888888'
        except Exception:
            return '#888888'

    # ---- 实时串口 feed ----------------------------------------------
    def start_serial(self, port: str, baudrate: int = 921600):
        """启动后台线程读串口，按 \\n 切行喂给解析器，QTimer 节流刷新。

        清空 _data + _data_cache：与 load_log 对齐，避免之前日志/mock 数据
        残留导致时间戳跳跃、曲线错位（串口实时数据应从空白开始）。
        var_meta 不清——颜色/label 跨会话保留。
        """
        import threading
        import time

        import serial
        self.stop_serial()
        # 清数据点（保留 var_meta 配置）
        self._data.clear()
        self._data_cache.clear()
        ser = serial.Serial(port, baudrate, timeout=0.1)
        self._serial = ser
        self._serial_stop = threading.Event()
        self._serial_buf = ''
        # 重置一次性校验状态（reader 攒够 N 行或 2 秒后触发）
        self._serial_validated = False
        self._serial_validation_buf = []
        self._serial_start_ts = time.time()
        port_label = f'串口 {port}@{baudrate}'
        # 工具栏显示当前串口
        self._update_file_label(port_label)

        def _reader():
            import time
            while not self._serial_stop.is_set() and ser.is_open:
                try:
                    chunk = ser.read(ser.in_waiting or 1024)
                except Exception as e:
                    # reader 读失败：记日志，退出 reader 循环
                    log_exc('warning', f'{__file__}:start_serial._reader read failed', e)
                    break
                if not chunk:
                    continue
                try:
                    text = chunk.decode('utf-8', errors='replace')
                except Exception as e:
                    log_exc('debug', f'{__file__}:start_serial._reader decode failed', e)
                    continue
                self._serial_buf += text
                while '\n' in self._serial_buf:
                    line, self._serial_buf = self._serial_buf.split('\n', 1)
                    line = line.rstrip('\r')
                    if not line:
                        continue
                    # 攒缓冲用于一次性格式校验（前 N 行 或 2 秒到）
                    if not self._serial_validated:
                        self._serial_validation_buf.append(line)
                        cfg_n = max(10, int(_CUSTOM_PARSER_STATE.get(
                            'validation', {}).get('sample_lines', 200)))
                        elapsed = time.time() - self._serial_start_ts
                        if (len(self._serial_validation_buf) >= cfg_n or
                                elapsed > 2.0):
                            self._serial_validated = True
                            buf_snapshot = list(self._serial_validation_buf)
                            self._validation_bridge.emit(buf_snapshot, port_label)
                    try:
                        for ts, name, val in _iter_panel_vars(
                                line + '\n', ts_fallback=time.time()):
                            with self._data_lock:
                                buf = self._data.setdefault(name, [])
                                if isinstance(buf, tuple):
                                    buf = list(zip(buf[0], buf[1]))
                                    self._data[name] = buf
                                buf.append((ts, val))
                                if len(buf) > self._max_points:
                                    del buf[:len(buf) - self._max_points]
                            self._register_var(name)
                    except Exception as e:
                        # 解析失败：记日志，不退出（继续读后续行）
                        log_exc('debug', f'{__file__}:start_serial._reader parse failed line={line[:80]!r}', e)
            try:
                ser.close()
            except Exception as e:
                log_exc('debug', f'{__file__}:start_serial._reader final ser.close failed', e)

        t = threading.Thread(target=_reader, daemon=True)
        self._serial_thread = t
        t.start()
        from PySide6.QtCore import QTimer
        if self._refresh_timer is None:
            # M2: QTimer 必须给 parent，让 Qt 自动随窗口释放，避免无主对象泄露
            self._refresh_timer = QTimer(self._win if hasattr(self, '_win') and self._win is not None else None)
            self._refresh_timer.timeout.connect(self._refresh_plot)
        self._refresh_timer.start(40)

    def stop_serial(self):
        if self._serial_stop is not None:
            self._serial_stop.set()
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception as e:
                log_exc('warning', f'{__file__}:stop_serial ser.close failed', e)
        if self._serial_thread is not None:
            try:
                # M2: join timeout 提升到 2s（原 1s 在 read 卡等数据时不一定够）
                self._serial_thread.join(timeout=2.0)
            except Exception as e:
                log_exc('warning', f'{__file__}:stop_serial thread join failed', e)
        if self._refresh_timer is not None:
            try:
                self._refresh_timer.stop()
            except Exception as e:
                log_exc('debug', f'{__file__}:stop_serial timer stop failed', e)
        self._serial = None
        self._serial_thread = None
        self._serial_stop = None

    # ---- 主题（任务3）------------------------------------------------
    def _apply_theme_to_window(self, theme_name: str):
        """应用具体主题到监视窗口（QSS + pyqtgraph 背景/坐标轴）。"""
        self.theme_name = theme_name
        from serial_debug.ui import qt_theme
        qt_theme.apply_qss(self._app, theme_name)
        t = qt_theme.palette(theme_name)
        try:
            import pyqtgraph as pg
            pg.setConfigOption('background',
                               t.get('panel_bg', t.get('bg', '#1e1e1e')))
            pg.setConfigOption('foreground', t.get('fg', '#cccccc'))
        except Exception:
            pass
        for pw in self._plot_widgets:
            try:
                pw.setBackground(t.get('panel_bg', t.get('bg', '#1e1e1e')))
                for axis_name in ('left', 'bottom'):
                    axis = pw.getAxis(axis_name)
                    if axis is not None:
                        axis.setPen(t.get('muted', '#cccccc'))
                        axis.setTextPen(t.get('fg', '#cccccc'))
            except Exception:
                pass
        self._curves = [{} for _ in self._visible_plots]
        self._refresh_plot()
        self._sync_theme_menu()

    def _apply_theme_with_pref(self, theme_name: str, pref: str | None = None):
        """应用主题 + 更新模式 + 持久化（任务3）。"""
        if pref is not None:
            self.theme_pref = pref
        try:
            themes.save_pref(self.theme_pref)
            kind = themes.THEME_KIND.get(theme_name)
            if kind:
                themes.save_specific(kind, theme_name)
        except Exception:
            pass
        self._apply_theme_to_window(theme_name)

    def _on_theme_pref_selected(self, pref: str):
        specific = themes.resolve(pref)
        self._apply_theme_with_pref(specific, pref=pref)

    def _on_theme_specific_selected(self, theme_name: str):
        kind = themes.THEME_KIND.get(theme_name)
        if kind is None:
            return
        new_pref = self.theme_pref
        if self.theme_pref != 'auto' and self.theme_pref != kind:
            new_pref = kind
        self._apply_theme_with_pref(theme_name, pref=new_pref)

    def _build_theme_menu(self, parent_menu, parent_widget):
        from PySide6.QtGui import QActionGroup
        m_theme = parent_menu.addMenu('主题')
        mode_grp = QActionGroup(m_theme)
        mode_grp.setExclusive(True)
        self._theme_mode_actions = {}
        for pref, label in (('auto', '跟随系统'),
                            ('light', '浅色'),
                            ('dark', '深色')):
            act = m_theme.addAction(label)
            act.setCheckable(True)
            act.setActionGroup(mode_grp)
            act.triggered.connect(lambda _c=False, p=pref:
                                   self._on_theme_pref_selected(p))
            self._theme_mode_actions[pref] = act
        m_theme.addSeparator()
        theme_grp = QActionGroup(m_theme)
        theme_grp.setExclusive(True)
        self._theme_specific_actions = {}
        for name, label in themes.THEME_DISPLAY:
            act = m_theme.addAction(label)
            act.setCheckable(True)
            act.setActionGroup(theme_grp)
            act.triggered.connect(lambda _c=False, n=name:
                                   self._on_theme_specific_selected(n))
            self._theme_specific_actions[name] = act
        self._sync_theme_menu()
        return m_theme

    def _sync_theme_menu(self):
        pref = getattr(self, 'theme_pref', 'auto')
        name = getattr(self, 'theme_name', 'dark')
        for p, act in getattr(self, '_theme_mode_actions', {}).items():
            act.setChecked(p == pref)
        for n, act in getattr(self, '_theme_specific_actions', {}).items():
            act.setChecked(n == name)

    # ---- 绘制 -------------------------------------------------------
    def _refresh_plot(self):
        """刷新所有 plot 的曲线 + 侧栏当前值/在图标记。

        对每个 plot：
          - plot.vars 为空 = 显示 self._data 里全部变量
          - plot.vars 非空 = 只显示指定变量
        复用 PlotDataItem：setData() 比每次 plot()+clear() 快 5-10 倍。
        """
        # 若侧栏行数与 var_meta 不一致（load_log/gen_mock_data 新增了变量）
        # 则先重建侧栏行
        if self._sidebar is not None:
            try:
                cur_rows = self._sidebar.topLevelItemCount()
            except Exception as e:
                log_exc('debug', f'{__file__}:_refresh_plot topLevelItemCount failed', e)
                cur_rows = 0
            if cur_rows != len(self._var_meta):
                self._refresh_sidebar()
        # 用户显式删除所有 plot 后 self._plots=[]，_rebuild_plots_ui
        # 已清空 _plot_widgets，这里不再自动补默认 plot，让窗口保持空白。
        # 用户可用工具栏"添加图表"按钮自行添加。
        if not self._plot_widgets:
            self._refresh_sidebar_values()
            self._refresh_sidebar_visibility()
            return
        import pyqtgraph as pg
        from PySide6.QtCore import Qt

        with self._data_lock:
            all_data_keys = list(self._data.keys())
        # 迭代 _visible_plots（运行时显示列表）而非 _plots（原始配置），
        # 否则 X 关闭 plot 4 + 新加 plot 5 后，_visible_plots=[1,2,3,5] 而
        # _plots=[1,2,3,4,5]，_refresh_plot 会拿 _plots[3]=plot4 的配置
        # 去刷新 widget[3]（其实显示的是 plot5）→ 用户看到 1,2,3,4
        for plot_idx, p in enumerate(self._visible_plots):
            if plot_idx >= len(self._plot_widgets):
                continue
            pw = self._plot_widgets[plot_idx]
            curves = self._curves[plot_idx]
            vars_to_show = p.get('vars') or all_data_keys
            # 图例 checkbox 可见性：缺失键 = True（默认可见）
            vis = p.get('var_visibility') or {}
            # 清理不再属于该 plot 或被 checkbox 取消勾选的曲线
            for name in list(curves.keys()):
                if name not in vars_to_show or not vis.get(name, True):
                    try:
                        pw.removeItem(curves[name])
                    except Exception as e:
                        log_exc('debug', f'{__file__}:_refresh_plot removeItem failed name={name!r}', e)
                    del curves[name]
            # 创建/更新曲线（跳过被取消勾选的变量）
            for name in vars_to_show:
                if not vis.get(name, True):
                    continue  # checkbox 未勾选：不画曲线
                # 始终通过 _get_xy 获取 numpy 数组（锁内取 + asarray 拷贝，
                # 避免与 reader 线程的 append/del 竞争 self._data[name]）。
                # 不在锁外读 self._data[name]：_get_xy 对不存在或空返回 None。
                xs, ys = self._get_xy(name)
                if xs is None or len(xs) == 0:
                    continue
                # 颜色：优先 var_meta，缺省回退到调色板
                color = self._var_color(name, all_data_keys)
                # 图例用中文 label
                meta = self._var_meta.get(name, {})
                label = meta.get('label') or name
                if name in curves:
                    item = curves[name]
                    try:
                        item.setData(xs, ys, pen=pg.mkPen(color, width=1.5))
                    except Exception as e:
                        log_exc('warning', f'{__file__}:_refresh_plot setData failed name={name!r}', e)
                else:
                    item = pw.plot(xs, ys,
                                   pen=pg.mkPen(color, width=1.5),
                                   name=label)
                    curves[name] = item
                # 记录颜色字符串供图例使用（pen 反推不可靠）
                item._legend_color = color
            # 不在每次 refresh 时调 enableAutoRange——会让用户手动 zoom 后
            # 增删图例（曲线 / checkbox 切换）时自动重置视图范围。
            # autoRange 已在 _make_plot_widget 创建 pw 时调用一次（首次进入
            # 时让曲线可见）；之后由用户手动操作触发（右键 View All / 双击）。
            # 顶部条图例刷新：基于 vars_to_show（含被取消勾选的变量，
            # chip 保留 + checkbox 显示未选中状态，便于用户重新勾选）
            if plot_idx < len(self._plot_headers):
                self._refresh_header_legend(
                    self._plot_headers[plot_idx], vars_to_show, p)
            # 在曲线上标注 max/min/avg/last
            self._draw_stat_markers(pw, curves, p)
            # 悬停提示：延迟绑定（只绑一次）
            if p.get('show_hover', True) and not hasattr(pw, '_hover_bound'):
                pw._hover_bound = True
                pw._hover_vline = pg.InfiniteLine(
                    angle=90, pen=pg.mkPen('#888888', width=1, style=Qt.PenStyle.DashLine))
                pw.addItem(pw._hover_vline, ignoreBounds=True)
                pw._hover_vline.setVisible(False)
                pw._hover_label = None
                pw._plot_cfg = p
                pw._monitor_ref = self
                pw._plot_idx = plot_idx
                pw.scene().sigMouseMoved.connect(
                    lambda pos, _pw=pw: self._on_plot_hover(_pw, pos))
        # 同步侧栏当前值 + 在图标记
        self._refresh_sidebar_values()
        self._refresh_sidebar_visibility()
        # 同步刷新 popout 单图独立窗口
        self._refresh_popouts()

    @staticmethod
    def _legend_offset(pos: str) -> tuple[int, int]:
        """返回图例偏移量（像素）。pyqtgraph addLegend 的 offset 参数。"""
        return {'top-left': (10, 10), 'top-right': (-10, 10),
                'bottom-left': (10, -10), 'bottom-right': (-10, -10)
                }.get(pos, (-10, 10))

    def _auto_avoid_legend(self, pw, curves: dict):
        """检测图例是否覆盖曲线密集区，若是则翻转到对角。

        策略：采样曲线在图例 x 范围内的点，若有点落入图例 y 范围，
        说明重合；尝试 4 个角，选重合最少的。保留用户拖拽后的位置
        （若用户已拖离默认角落，不强制挪回）。
        """
        legend = getattr(pw, '_legend_obj', None)
        if legend is None:
            return
        try:
            pw.getViewBox()
            # 图例在 view 坐标系中的矩形
            leg_rect = legend.mapRectToParent(legend.boundingRect())
            if leg_rect.width() <= 0 or leg_rect.height() <= 0:
                return
            # 统计图例区域内曲线点数
            def _count_overlap(rect):
                cnt = 0
                for name, item in curves.items():
                    try:
                        xd = item.xData
                        yd = item.yData
                        if xd is None or len(xd) == 0:
                            continue
                        # 筛选 x 在 rect 范围的点
                        mask = (xd >= rect.left()) & (xd <= rect.right())
                        ys_in = yd[mask]
                        if len(ys_in) == 0:
                            continue
                        cnt += int(((ys_in >= rect.top()) & (ys_in <= rect.bottom())).sum())
                    except Exception:
                        continue
                return cnt
            cur_overlap = _count_overlap(leg_rect)
            # 若当前重合点数少（< 5），不挪动
            if cur_overlap < 5:
                return
            # 尝试 4 个角，找重合最少的位置
            best_pos = None
            best_overlap = cur_overlap
            # 4 个角的锚点（itemPos, parentPos）
            corners = [
                ((0, 0), (0, 0), 'top-left'),
                ((1, 0), (1, 0), 'top-right'),
                ((0, 1), (0, 1), 'bottom-left'),
                ((1, 1), (1, 1), 'bottom-right'),
            ]
            for item_pos, parent_pos, name in corners:
                try:
                    legend.anchor(itemPos=item_pos, parentPos=parent_pos)
                    legend.updateSize()
                    new_rect = legend.mapRectToParent(legend.boundingRect())
                    ov = _count_overlap(new_rect)
                    if ov < best_overlap:
                        best_overlap = ov
                        best_pos = (item_pos, parent_pos)
                except Exception:
                    continue
            # 若找到更优位置，应用
            if best_pos is not None:
                try:
                    legend.anchor(itemPos=best_pos[0], parentPos=best_pos[1])
                    legend.updateSize()
                except Exception:
                    pass
            else:
                # 没找到更优，恢复原位置（用 autoAnchor 回到最近角落）
                try:
                    legend.autoAnchor(legend.pos())
                except Exception:
                    pass
        except Exception:
            pass

    def _draw_stat_markers(self, pw, curves: dict, p_cfg: dict):
        """在曲线上标注 max/min 点位 + 数值标签。

        借鉴 PlotJuggler QwtPlotMarker 的做法：标记用小圆点（6-8px）+
        文本固定方向偏移——max 文本放在点上方、min 文本放在点下方，
        避免直接覆盖曲线本体。y 偏移按当前视图 y 范围的 4% 动态计算，
        跟随用户 zoom 适配（不写死像素，因为 pyqtgraph TextItem 用
        数据坐标系）。
        """
        import numpy as np
        import pyqtgraph as pg
        # 清理上一轮标注
        old = getattr(pw, '_stat_markers', None)
        if old:
            for it in old:
                try:
                    pw.removeItem(it)
                except Exception:
                    pass
        pw._stat_markers = []
        show_max = p_cfg.get('hover_show_max', True)
        show_min = p_cfg.get('hover_show_min', True)
        if not (show_max or show_min):
            return
        if not curves:
            return

        # y 偏移：取当前视图 y 范围的 4%（所有曲线共用，保证视觉一致）
        try:
            vr = pw.getViewBox().viewRange()
            y_span = vr[1][1] - vr[1][0]
            if not np.isfinite(y_span) or y_span <= 0:
                y_span = 1.0
        except Exception:
            y_span = 1.0
        y_off = y_span * 0.04

        def _fmt(v):
            try:
                if v == int(v):
                    return str(int(v))
            except Exception:
                pass
            return f'{v:.3f}'

        for name, item in curves.items():
            try:
                xd = item.xData
                yd = item.yData
                if xd is None or len(xd) == 0:
                    continue
                meta = self._var_meta.get(name, {})
                color = meta.get('color', '#cccccc')
                pts = []  # ScatterPlot 点（小圆，不遮挡曲线）
                txts = []  # (x, y, text, anchor)
                if show_max:
                    i = int(np.argmax(yd))
                    pts.append((xd[i], yd[i]))
                    # 文本在 max 点上方：anchor=(0.5, 1) = 文本底边对齐到 y
                    txts.append((xd[i], yd[i] + y_off,
                                  f'max={_fmt(float(yd[i]))}', (0.5, 1)))
                if show_min:
                    i = int(np.argmin(yd))
                    pts.append((xd[i], yd[i]))
                    # 文本在 min 点下方：anchor=(0.5, 0) = 文本顶边对齐到 y
                    txts.append((xd[i], yd[i] - y_off,
                                  f'min={_fmt(float(yd[i]))}', (0.5, 0)))
                if pts:
                    xs = np.array([p[0] for p in pts])
                    ys = np.array([p[1] for p in pts])
                    # 小圆点 size=7：标记点位但不遮挡曲线
                    sc = pg.ScatterPlotItem(
                        xs=xs, ys=ys,
                        symbol='o', size=7,
                        pen=pg.mkPen(color, width=1),
                        brush=pg.mkBrush(color))
                    pw.addItem(sc, ignoreBounds=True)
                    pw._stat_markers.append(sc)
                for tx, ty, text, anchor in txts:
                    t = pg.TextItem(text, color=color,
                                    fill='#1e1e1ed9', anchor=anchor)
                    t.setPos(tx, ty)
                    pw.addItem(t, ignoreBounds=True)
                    pw._stat_markers.append(t)
            except Exception:
                continue

    # ---- popout 单图独立窗口 ----------------------------------------
    def _popout_current_plot(self):
        """把当前 plot 弹到独立 QDialog 窗口放大显示。

        QDialog 内放 pyqtgraph PlotWidget，画一遍当前 plot 的曲线；
        _refresh_plot 末尾会同步刷新所有 popout 窗口；关闭时清理。
        """
        from PySide6.QtWidgets import QDialog, QVBoxLayout

        from serial_debug.ui import qt_theme
        t = qt_theme.palette(self.theme_name)
        if not self._visible_plots:
            return
        idx = self._current_plot_idx()
        if idx is None:
            idx = 0
        # 已弹出的同 plot 复用
        for pi, dlg, pw, _, _ in self._popout_dialogs:
            if pi == idx and dlg.isVisible():
                dlg.raise_()
                dlg.activateWindow()
                return
        p = self._visible_plots[idx]
        dlg = QDialog(self._win if hasattr(self, '_win') else None)
        dlg.setWindowTitle(f'{p.get("title", "数据曲线")} - 单独显示')
        dlg.resize(900, 560)
        v = QVBoxLayout(dlg)
        v.setContentsMargins(0, 0, 0, 0)
        container, pw, header = self._make_plot_widget(p, t, None)
        v.addWidget(container)
        curves: dict = {}
        self._popout_dialogs.append((idx, dlg, pw, curves, header))
        dlg.show()
        # 立即画一次
        self._draw_popout(idx, dlg, pw, curves, header)
        # 关闭时清理
        dlg.finished.connect(lambda *_: self._close_popout(idx, dlg))

    def _close_popout(self, plot_idx, dlg):
        """关闭 popout 窗口时从列表移除。"""
        for i, (pi, d, pw, _, header) in enumerate(self._popout_dialogs):
            if pi == plot_idx and d is dlg:
                try:
                    pw.setParent(None)
                    pw.deleteLater()
                except Exception:
                    pass
                self._popout_dialogs.pop(i)
                break

    def _draw_popout(self, plot_idx, dlg, pw, curves, header=None):
        """在 popout PlotWidget 上画一遍 plot_idx 的曲线。"""
        if plot_idx >= len(self._visible_plots):
            return
        import pyqtgraph as pg
        p = self._visible_plots[plot_idx]
        with self._data_lock:
            all_data_keys = list(self._data.keys())
        vars_to_show = p.get('vars') or all_data_keys
        vis = p.get('var_visibility') or {}
        # 清理不再属于该 plot 的曲线
        for name in list(curves.keys()):
            if name not in vars_to_show or not vis.get(name, True):
                try:
                    pw.removeItem(curves[name])
                except Exception:
                    pass
                del curves[name]
        # 创建/更新曲线（跳过被取消勾选的变量）
        for name in vars_to_show:
            if not vis.get(name, True):
                continue
            # 锁内取 + asarray 拷贝，避免与 reader 线程竞争 self._data[name]
            xs, ys = self._get_xy(name)
            if xs is None or len(xs) == 0:
                continue
            color = self._var_color(name, all_data_keys)
            meta = self._var_meta.get(name, {})
            label = meta.get('label') or name
            if name in curves:
                try:
                    curves[name].setData(xs, ys, pen=pg.mkPen(color, width=1.5))
                except Exception:
                    pass
            else:
                item = pw.plot(xs, ys, pen=pg.mkPen(color, width=1.5),
                               name=label)
                curves[name] = item
            curves[name]._legend_color = color
        pw.enableAutoRange()
        # 顶部条图例刷新（popout 单图也用 checkbox 图例）
        if header is not None:
            self._refresh_header_legend(header, vars_to_show, p)
        # 在曲线上标注 max/min/avg/last
        self._draw_stat_markers(pw, curves, p)

    def _refresh_popouts(self):
        """同步刷新所有 popout 窗口的曲线（_refresh_plot 末尾调）。"""
        for pi, dlg, pw, curves, header in list(self._popout_dialogs):
            if not dlg.isVisible():
                continue
            try:
                self._draw_popout(pi, dlg, pw, curves, header)
            except Exception:
                pass

    def _on_plot_hover(self, pw, pos):
        """鼠标在 plot 上移动时显示竖线 + 数据标签。"""
        import numpy as np
        import pyqtgraph as pg
        # 检查鼠标是否在 plot 区域内
        if pw.sceneBoundingRect().contains(pos):
            mouse_point = pw.getViewBox().mapSceneToView(pos)
            x_val = mouse_point.x()
            # 更新竖线位置
            vline = getattr(pw, '_hover_vline', None)
            if vline is not None:
                vline.setPos(x_val)
                vline.setVisible(True)
            # 获取数据
            p_cfg = getattr(pw, '_plot_cfg', {})
            hover_mode = p_cfg.get('hover_mode', 'current')
            plot_idx = getattr(pw, '_plot_idx', 0)
            curves = self._curves[plot_idx] if plot_idx < len(self._curves) else {}
            if not curves:
                return
            # 构建标签文本
            # hover_mode: current / all / stats / all_stats
            # current = 鼠标最近曲线；all = 全部曲线；stats = 当前 + 统计；
            # all_stats = 全部曲线 + 各自统计
            rows = []  # (label, val, color, ts) 当前值行
            stat_rows = []  # (label, max, min, avg, last, color) 统计行
            want_stats = hover_mode in ('stats', 'all_stats')
            if hover_mode in ('all', 'all_stats'):
                # 全部曲线：按鼠标 x 时间找最近点
                for name, item in curves.items():
                    try:
                        x_data = item.xData
                        y_data = item.yData
                        if x_data is None or len(x_data) == 0:
                            continue
                        idx = int(np.searchsorted(x_data, x_val))
                        idx = max(0, min(idx, len(x_data) - 1))
                        if idx > 0 and abs(x_data[idx - 1] - x_val) < abs(x_data[idx] - x_val):
                            idx = idx - 1
                        ts = x_data[idx]
                        val = y_data[idx]
                        meta = self._var_meta.get(name, {})
                        label = meta.get('label') or name
                        color = meta.get('color', '#cccccc')
                        rows.append((label, float(val), color, ts))
                        if want_stats:
                            stat_rows.append((label, float(np.max(y_data)),
                                              float(np.min(y_data)),
                                              float(np.mean(y_data)),
                                              float(y_data[-1]), color))
                    except Exception:
                        continue
            else:
                # 仅当前曲线：找像素距离最近的点
                best_dist = float('inf')
                best_name = None
                for name, item in curves.items():
                    try:
                        x_data = item.xData
                        y_data = item.yData
                        if x_data is None or len(x_data) == 0:
                            continue
                        idx = int(np.searchsorted(x_data, x_val))
                        idx = max(0, min(idx, len(x_data) - 1))
                        if idx > 0 and abs(x_data[idx - 1] - x_val) < abs(x_data[idx] - x_val):
                            idx = idx - 1
                        ts = x_data[idx]
                        val = y_data[idx]
                        # 转为像素距离
                        px = pw.getViewBox().mapViewToScene(pg.Point(ts, val))
                        dist = (pos - px).manhattanLength()
                        if dist < best_dist:
                            best_dist = dist
                            best_name = name
                    except Exception:
                        continue
                if best_name is not None:
                    item = curves[best_name]
                    x_data = item.xData
                    y_data = item.yData
                    idx = int(np.searchsorted(x_data, x_val))
                    idx = max(0, min(idx, len(x_data) - 1))
                    if idx > 0 and abs(x_data[idx - 1] - x_val) < abs(x_data[idx] - x_val):
                        idx = idx - 1
                    meta = self._var_meta.get(best_name, {})
                    label = meta.get('label') or best_name
                    color = meta.get('color', '#cccccc')
                    rows.append((label, float(y_data[idx]), color, x_data[idx]))
                    if want_stats:
                        stat_rows.append((label, float(np.max(y_data)),
                                          float(np.min(y_data)),
                                          float(np.mean(y_data)),
                                          float(y_data[-1]), color))
            # 显示标签
            if rows:
                snap_ts = rows[0][3]
                try:
                    import datetime as _dt
                    t_str = _dt.datetime.fromtimestamp(
                        snap_ts, tz=BEIJING_TZ).strftime('%H:%M:%S')
                except Exception:
                    t_str = f't={snap_ts:.3f}'
                # 用 HTML 拼：每行前面带一个该曲线颜色的圆点
                # 圆点用 ●（U+25CF），fontSize 略小避免行高过大
                lines_html = [
                    f'<div style="color:#cccccc;">t={t_str}</div>']
                for label, val, color, _ts in rows:
                    try:
                        if val == int(val):
                            val_s = str(int(val))
                        else:
                            val_s = f'{val:.3f}'
                    except Exception:
                        val_s = f'{val:.3f}'
                    lines_html.append(
                        f'<div style="color:{color};">'
                        f'<span style="color:{color};font-size:10pt;">●</span> '
                        f'<span style="color:#cccccc;">{label}:</span> '
                        f'<span style="color:{color};">{val_s}</span>'
                        f'</div>')
                # 统计行
                show_max = p_cfg.get('hover_show_max', True)
                show_min = p_cfg.get('hover_show_min', True)
                show_avg = p_cfg.get('hover_show_avg', True)
                show_last = p_cfg.get('hover_show_last', True)

                def _fmt_num(v):
                    if v == int(v):
                        return str(int(v))
                    return f'{v:.3f}'

                for slabel, smax, smin, savg, slast, scolor in stat_rows:
                    parts = [f'<span style="color:#cccccc;">{slabel}</span>']
                    if show_max:
                        parts.append(f'max={_fmt_num(smax)}')
                    if show_min:
                        parts.append(f'min={_fmt_num(smin)}')
                    if show_avg:
                        parts.append(f'avg={_fmt_num(savg)}')
                    if show_last:
                        parts.append(f'last={_fmt_num(slast)}')
                    inner = ' '.join(parts)
                    lines_html.append(
                        f'<div style="color:{scolor};">'
                        f'<span style="color:{scolor};font-size:10pt;">●</span> '
                        f'{inner}'
                        f'</div>')
                html = '<br>'.join(lines_html)
                # 使用 TextItem 或更新已有
                # 注意：pyqtgraph TextItem.setText 走 setPlainText（HTML 会被
                # 当作字面文本显示为 <div> 等），必须用 setHtml 才能渲染 HTML
                if getattr(pw, '_hover_label', None) is None:
                    pw._hover_label = pg.TextItem(html, color='#cccccc',
                                                  fill='#1e1e1e99',
                                                  anchor=(0, 1))
                    pw.addItem(pw._hover_label, ignoreBounds=True)
                pw._hover_label.setHtml(html)
                pw._hover_label.setPos(mouse_point.x(), mouse_point.y())
                pw._hover_label.setVisible(True)
            else:
                label = getattr(pw, '_hover_label', None)
                if label is not None:
                    label.setVisible(False)
        else:
            # 鼠标离开 plot 区域
            vline = getattr(pw, '_hover_vline', None)
            if vline is not None:
                vline.setVisible(False)
            label = getattr(pw, '_hover_label', None)
            if label is not None:
                label.setVisible(False)

    def run(self):
        win = self._create()
        if self.log_path:
            n = self.load_log(self.log_path)
            self._refresh_plot()
            win.statusBar().showMessage(
                f'加载 {n} 行，{len(self._data)} 变量')
        win.show()
        return self._app.exec()


def main():
    parser = argparse.ArgumentParser(
        description='Qt + pyqtgraph 数据监视窗口')
    parser.add_argument('log', nargs='?', default=None,
                        help='日志文件路径（可选）')
    parser.add_argument('--theme', default='auto',
                        help='主题：dark / light / auto')
    args = parser.parse_args()

    w = QtMonitorWindow(log_path=args.log, theme_name=args.theme)
    sys.exit(w.run())


if __name__ == '__main__':
    main()
