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

# 延迟导入 PySide6/pyqtgraph：仅 Qt 窗口实际启动时才加载
# 让 tkinter 主程序不强制依赖 Qt（即便没装 Qt 也能跑 tkinter 路径）

# 复用 tkinter 监视窗口的日志解析器（时间戳提取 + motor/BMS/IMU 三类 Panel）
from serial_debug.ui.monitor_window import (
    MotorMonitorPanel, BmsMonitorPanel, ImuMonitorPanel,
    _extract_log_ts,
)


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


def _iter_panel_vars(line: str, ts_fallback: float | None = None):
    """从一行日志中提取所有 Panel 的 (var_name, value)。

    motor / bms / imu 三类 Panel 各自调 _parse_tagged_line + DEFAULT_VARS
    取值。变量名加 Panel 前缀避免冲突（motor. / bms. / imu.）。

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


def _default_plot() -> dict:
    return {
        'title': '数据曲线',
        'x_label': '时间 (s)',
        'y_label': '值',
        'vars': [],     # 空 = 显示全部当前已加载变量
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
                    out.append(_dt.datetime.fromtimestamp(v).strftime(fmt))
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
            QDialog, QDialogButtonBox, QFormLayout, QLineEdit,
            QCheckBox, QListWidget, QListWidgetItem, QVBoxLayout,
            QHBoxLayout, QPushButton, QGroupBox, QLabel, QComboBox,
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


class QtMonitorWindow:
    """Qt 数据监视窗口（Stage 1：多 plot 子图 + 配置持久化）。"""

    def __init__(self, log_path: str | None = None,
                 theme_name: str = 'auto'):
        self.log_path = log_path
        self.theme_name = theme_name
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
        self._plots: list[dict] = self._load_plots()
        # UI：PlotWidget 列表 + 每个对应的 curves dict（name -> PlotDataItem）
        self._plot_widgets: list = []
        self._curves: list[dict] = []
        self._plot_headers: list = []  # 每个 plot 的顶部条（title + legend）
        self._plots_layout = None
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

    # ---- 配置持久化 -------------------------------------------------
    def _load_plots(self) -> list[dict]:
        from serial_debug.infra import config
        try:
            cfg = config.load()
            raw = cfg.get('qt_monitor_plots', []) or []
            # 尊重配置值：[] 就返回 []，让用户删除所有 plot 后保持空白；
            # 首次启动 cfg 默认也是 []，用户用工具栏"添加图表"自己加。
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
        from serial_debug.infra import config
        try:
            cfg = config.load()
            cfg['qt_monitor_plots'] = self._plots
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
        """导出当前曲线图配置 + 变量元信息到 JSON 文件。

        导出内容：
          - plots: self._plots（title / vars / show_* / hover_* / legend_*）
          - var_meta: self._var_meta（颜色 / label / y_min/y_max / warn_*
            等显示参数；导入时合并到当前 var_meta）
          - version: 格式版本号（向后兼容）

        不导出 self._data（实际曲线数据）。导入后曲线显示需要先加载
        日志或连串口，按导出时的 vars 配置重新绘制。
        """
        import json
        data = {
            'version': 1,
            'plots': [_normalize_plot(p) for p in self._plots],
            'var_meta': {k: dict(v) for k, v in self._var_meta.items()},
        }
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def import_plots_config(self, file_path: str) -> int:
        """从 JSON 文件导入曲线图配置，替换当前 plots + 合并 var_meta。

        返回导入的 plot 数量。导入后立即 _rebuild_plots_ui + _refresh_sidebar
        + _refresh_plot + _save_plots，按导出时的 vars 配置显示曲线
        （若已加载数据则曲线立刻画出）。
        """
        import json
        with open(file_path, 'r', encoding='utf-8') as f:
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
        self._rebuild_plots_ui()
        self._refresh_sidebar()
        self._refresh_sidebar_visibility()
        self._refresh_plot()
        self._save_plots()
        return len(new_plots)

    def _save_geometry(self) -> None:
        from serial_debug.infra import config
        try:
            if self._win is None:
                return
            cfg = config.load()
            cfg['qt_monitor_geometry'] = self._win.saveGeometry().data().hex()
            config.save(cfg)
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
        避免冲突。返回解析命中的行数。
        """
        n = 0
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                for line in f:
                    hit = False
                    for ts, name, val in _iter_panel_vars(line):
                        self._data.setdefault(name, []).append((ts, val))
                        self._register_var(name)
                        hit = True
                    if hit:
                        n += 1
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
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QAction
        from PySide6.QtWidgets import (
            QApplication, QMainWindow, QToolBar, QPushButton, QStatusBar,
            QVBoxLayout, QSplitter, QWidget, QScrollArea, QSizePolicy, QLabel,
        )
        import pyqtgraph as pg

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
        win.setWindowTitle('[Qt 试点] 数据监视窗口 - PySide6 + pyqtgraph')
        win.resize(1400, 800)

        # 中央：QSplitter 横向分栏，左侧变量列表，右侧 plot 滚动区
        from PySide6.QtWidgets import QSplitter
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
                self._plots.append(_normalize_plot(res))
                self._rebuild_plots_ui()
                self._save_plots()
                self._refresh_plot()
                win.statusBar().showMessage(
                    f'已添加图表：{res["title"]}（{len(res["vars"])} 变量）')

        def _edit_plot():
            idx = self._select_plot_idx(win, '编辑图表')
            if idx is None:
                return
            dlg = _QtPlotDialog(
                win, self._loaded_var_meta(),
                initial=self._plots[idx], title='编辑图表')
            res = dlg.exec()
            if res:
                self._plots[idx] = _normalize_plot(res)
                self._rebuild_plots_ui()
                self._save_plots()
                self._refresh_plot()
                win.statusBar().showMessage(f'已更新图表：{res["title"]}')

        def _del_plot():
            from PySide6.QtWidgets import QMessageBox
            idx = self._select_plot_idx(win, '删除图表')
            if idx is None:
                return
            title = self._plots[idx].get('title', '数据曲线')
            if QMessageBox.question(
                    win, '删除图表',
                    f'确认删除图表 "{title}" ？'
            ) != QMessageBox.Yes:
                return
            del self._plots[idx]
            self._rebuild_plots_ui()
            self._save_plots()
            self._refresh_plot()
            win.statusBar().showMessage('已删除图表')

        def _open_log():
            from PySide6.QtWidgets import QFileDialog
            path, _ = QFileDialog.getOpenFileName(
                win, '打开日志文件', os.path.expanduser('~'),
                '日志 (*.txt *.log);;所有文件 (*)')
            if path:
                n = self.load_log(path)
                self._refresh_plot()
                win.statusBar().showMessage(
                    f'加载 {n} 行，{len(self._data)} 变量')

        def _import_plots_cfg():
            from PySide6.QtWidgets import QFileDialog, QMessageBox
            path, _ = QFileDialog.getOpenFileName(
                win, '导入曲线图配置', os.path.expanduser('~'),
                '曲线图配置 (*.json);;所有文件 (*)')
            if not path:
                return
            try:
                n = self.import_plots_config(path)
                win.statusBar().showMessage(
                    f'已导入 {n} 个曲线图配置：{path}')
            except Exception as e:
                QMessageBox.critical(win, '导入失败', str(e))

        def _export_plots_cfg():
            from PySide6.QtWidgets import QFileDialog, QMessageBox
            if not self._plots:
                QMessageBox.information(win, '导出', '当前没有曲线图配置')
                return
            path, _ = QFileDialog.getSaveFileName(
                win, '导出曲线图配置', os.path.expanduser('~/plots_config.json'),
                '曲线图配置 (*.json);;所有文件 (*)')
            if not path:
                return
            try:
                self.export_plots_config(path)
                win.statusBar().showMessage(
                    f'已导出 {len(self._plots)} 个曲线图配置：{path}')
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
            cur = qt_theme.palette(self.theme_name).get('bg', '#1e1e1e')
            if cur == '#1e1e1e':
                self.theme_name = 'light'
            else:
                self.theme_name = 'dark'
            qt_theme.apply_qss(self._app, self.theme_name)
            t = qt_theme.palette(self.theme_name)
            try:
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
            self._curves = [{} for _ in self._plots]
            self._refresh_plot()

        def _toggle_sidebar():
            self._toggle_sidebar()

        def _show_stats():
            self._show_statistics(win)

        def _export_csv():
            from PySide6.QtWidgets import QFileDialog, QMessageBox
            if not self._data:
                QMessageBox.information(win, '导出 CSV', '没有可导出的数据')
                return
            path, _ = QFileDialog.getSaveFileName(
                win, '导出 CSV', os.path.expanduser('~'),
                'CSV (*.csv);;所有文件 (*)')
            if not path:
                return
            try:
                n_rows, step = self._export_csv(path)
                QMessageBox.information(
                    win, '导出完成',
                    f'已导出 {n_rows:,} 行（采样步长 {step}）\n{path}')
            except Exception as e:
                QMessageBox.critical(win, '导出失败', str(e))

        def _connect_serial():
            from PySide6.QtWidgets import (
                QDialog, QDialogButtonBox, QFormLayout, QLineEdit,
                QMessageBox, QComboBox)
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
            except Exception:
                pass

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
        tb.addSeparator()
        btn_open = QPushButton('打开日志…')
        btn_open.clicked.connect(_open_log)
        tb.addWidget(btn_open)
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
                            _import_plots_cfg, _export_plots_cfg)

        # 状态栏
        try:
            import PySide6
            import pyqtgraph
            tech_label = QLabel(
                f'PySide6 {PySide6.__version__} | pyqtgraph {pyqtgraph.__version__}')
        except Exception:
            tech_label = QLabel('Qt 数据监视窗口')
        win.statusBar().addPermanentWidget(tech_label)
        win.statusBar().showMessage('就绪')

        # 关闭时保存几何 + plots + var_meta
        def _close_event(event):
            self._save_geometry()
            self._save_plots()
            self._save_var_meta()
            try:
                self.stop_serial()
            except Exception:
                pass
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
        return win

    def _loaded_var_meta(self) -> list[dict]:
        """返回当前已加载变量的元数据，供对话框使用。"""
        out = []
        for name in self._data.keys():
            label = name
            # 在静态注册表里查中文 label
            for v in _ALL_VARS:
                if v['name'] == name:
                    label = v.get('label', name)
                    break
            out.append({'name': name, 'label': label})
        return out

    def _build_menubar(self, win, _add_plot, _edit_plot, _del_plot,
                        _open_log, _mock_10w, _mock_100w, _clear,
                        _toggle_theme, _toggle_sidebar,
                        _show_stats, _export_csv,
                        _connect_serial, _disconnect_serial,
                        _perf_test,
                        _import_plots_cfg=None, _export_plots_cfg=None):
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
            import PySide6
            import pyqtgraph
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
        from PySide6.QtGui import QColor, QAction
        from PySide6.QtWidgets import (
            QWidget, QVBoxLayout, QTreeWidget, QTreeWidgetItem,
            QHeaderView, QMenu, QLabel,
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
                vars_list = self._plots[idx].setdefault('vars', [])
                if name not in vars_list:
                    vars_list.append(name)
                self._save_plots()
                self._refresh_plot()
                self._refresh_sidebar_visibility()

            def _remove_from_current_plot():
                idx = self._select_plot_idx(tree, '从图表移除')
                if idx is None:
                    return
                vars_list = self._plots[idx].get('vars', [])
                while name in vars_list:
                    vars_list.remove(name)
                self._save_plots()
                self._refresh_plot()
                self._refresh_sidebar_visibility()

            def _add_to_all_plots():
                for p in self._plots:
                    if name not in p.get('vars', []):
                        p.setdefault('vars', []).append(name)
                self._save_plots()
                self._refresh_plot()
                self._refresh_sidebar_visibility()

            def _remove_var():
                # 删除变量：从 var_meta / data / 所有 plot.vars 清理
                self._var_meta.pop(name, None)
                self._data.pop(name, None)
                for p in self._plots:
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
        counts: dict[str, int] = {n: 0 for n in all_names}
        for p in self._plots:
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
            QDialog, QDialogButtonBox, QVBoxLayout, QTableWidget,
            QTableWidgetItem, QHeaderView, QLabel,
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
        import numpy as np
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
        """弹出对话框让用户选择一个图表，返回索引；取消返回 None。"""
        from PySide6.QtWidgets import (
            QDialog, QDialogButtonBox, QVBoxLayout, QComboBox, QLabel,
        )
        if not self._plots:
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
        for i, p in enumerate(self._plots):
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
        """根据 self._plots 重建 PlotWidget 列表（增删改 plot 后调用）。

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

        n = len(self._plots)
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
                p = self._plots[idx]
                container, pw, header = self._make_plot_widget(p, t)
                self._plot_widgets.append(pw)
                self._curves.append({})
                self._plot_headers.append(header)
                h_splitter.addWidget(container)
            v_splitter.addWidget(h_splitter)

        layout.addWidget(v_splitter)

    # ---- plot 容器构建（顶部标题+图例条 + PlotWidget）---------------
    def _make_plot_widget(self, p: dict, t: dict):
        """创建 plot 容器：顶部标题+图例条（同一行）+ PlotWidget。

        返回 (container, plot_widget, header)。header 存 title_label /
        legend_box / legend_flow / legend_items 引用，便于刷新。
        """
        import pyqtgraph as pg
        from PySide6.QtWidgets import (QFrame, QVBoxLayout, QHBoxLayout,
                                       QLabel, QSizePolicy)
        from PySide6.QtGui import QFont
        from PySide6.QtCore import Qt
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
        }
        return container, pw, header

    def _refresh_header_legend(self, header, curves: dict, p: dict):
        """刷新顶部条：标题文本 + 重建图例项（color square + label）。

        legend_pos == 'hidden' 时隐藏整个图例条。其他值（top-left /
        top-right 等）在重构后统一放标题右侧同一行，位置参数被忽略。
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
        from serial_debug.ui import qt_theme
        from PySide6.QtWidgets import QWidget, QHBoxLayout, QLabel
        t = qt_theme.palette(self.theme_name)
        # 移除不再存在的曲线对应的图例项
        current = set(curves.keys())
        existing = set(header['legend_items'].keys())
        for name in list(existing - current):
            chip = header['legend_items'].pop(name)
            try:
                header['legend_flow'].removeWidget(chip)
                chip.setParent(None)
                chip.deleteLater()
            except Exception:
                pass
        # 添加新的 / 更新已有的（色块 + 文字作为一个整体，避免换行拆开）
        for name, item in curves.items():
            color = self._curve_color(item)
            meta = self._var_meta.get(name, {})
            label = meta.get('label') or name
            if name in header['legend_items']:
                chip = header['legend_items'][name]
                sq = chip.findChild(QLabel)
                if sq is not None:
                    sq.setStyleSheet(
                        f"background-color: {color};"
                        f"border: 1px solid {t.get('muted', '#888888')};")
                # 更新文字（chip 内第二个 QLabel）
                labels = chip.findChildren(QLabel)
                if len(labels) >= 2 and labels[1].text() != label:
                    labels[1].setText(label)
                try:
                    font = labels[1].font() if len(labels) >= 2 else None
                    if font is not None:
                        font.setPointSize(font_size)
                        labels[1].setFont(font)
                except Exception:
                    pass
            else:
                chip = QWidget()
                chip.setStyleSheet("background: transparent; border: none;")
                cl = QHBoxLayout(chip)
                cl.setContentsMargins(0, 0, 0, 0)
                cl.setSpacing(4)
                sq = QLabel()
                sq.setFixedSize(12, 12)
                sq.setStyleSheet(
                    f"background-color: {color};"
                    f"border: 1px solid {t.get('muted', '#888888')};")
                txt = QLabel(label)
                txt.setStyleSheet(
                    f"color: {t.get('fg', '#cccccc')};"
                    f"font-size: {font_size}pt;"
                    "background: transparent; border: none;")
                cl.addWidget(sq)
                cl.addWidget(txt)
                header['legend_flow'].addWidget(chip)
                header['legend_items'][name] = chip

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
        """启动后台线程读串口，按 \\n 切行喂给解析器，QTimer 节流刷新。"""
        import threading
        import serial
        self.stop_serial()
        ser = serial.Serial(port, baudrate, timeout=0.1)
        self._serial = ser
        self._serial_stop = threading.Event()
        self._serial_buf = ''

        def _reader():
            import time
            while not self._serial_stop.is_set() and ser.is_open:
                try:
                    chunk = ser.read(ser.in_waiting or 1024)
                except Exception:
                    break
                if not chunk:
                    continue
                try:
                    text = chunk.decode('utf-8', errors='replace')
                except Exception:
                    continue
                self._serial_buf += text
                while '\n' in self._serial_buf:
                    line, self._serial_buf = self._serial_buf.split('\n', 1)
                    line = line.rstrip('\r')
                    if not line:
                        continue
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
                    except Exception:
                        pass
            try:
                ser.close()
            except Exception:
                pass

        t = threading.Thread(target=_reader, daemon=True)
        self._serial_thread = t
        t.start()
        from PySide6.QtCore import QTimer
        if self._refresh_timer is None:
            self._refresh_timer = QTimer()
            self._refresh_timer.timeout.connect(self._refresh_plot)
        self._refresh_timer.start(40)

    def stop_serial(self):
        if self._serial_stop is not None:
            self._serial_stop.set()
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
        if self._serial_thread is not None:
            try:
                self._serial_thread.join(timeout=1.0)
            except Exception:
                pass
        if self._refresh_timer is not None:
            try:
                self._refresh_timer.stop()
            except Exception:
                pass
        self._serial = None
        self._serial_thread = None
        self._serial_stop = None

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
            except Exception:
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
        import numpy as np
        import pyqtgraph as pg
        from PySide6.QtCore import Qt

        with self._data_lock:
            all_data_keys = list(self._data.keys())
        for plot_idx, p in enumerate(self._plots):
            if plot_idx >= len(self._plot_widgets):
                continue
            pw = self._plot_widgets[plot_idx]
            curves = self._curves[plot_idx]
            vars_to_show = p.get('vars') or all_data_keys
            # 清理不再属于该 plot 的曲线
            for name in list(curves.keys()):
                if name not in vars_to_show:
                    try:
                        pw.removeItem(curves[name])
                    except Exception:
                        pass
                    del curves[name]
            # 创建/更新曲线
            for name in vars_to_show:
                if name not in self._data:
                    continue
                pts = self._data[name]
                if not pts:
                    continue
                # 始终通过 _get_xy 获取 numpy 数组，不覆盖 self._data
                xs, ys = self._get_xy(name)
                if xs is None or len(xs) == 0:
                    continue
                # 颜色：优先 var_meta，缺省回退到调色板
                meta = self._var_meta.get(name, {})
                color = meta.get('color') or self._palette[
                    (all_data_keys.index(name)
                     if name in all_data_keys else 0)
                    % len(self._palette)]
                # 图例用中文 label
                label = meta.get('label') or name
                if name in curves:
                    item = curves[name]
                    try:
                        item.setData(xs, ys, pen=pg.mkPen(color, width=1.5))
                    except Exception:
                        pass
                else:
                    item = pw.plot(xs, ys,
                                   pen=pg.mkPen(color, width=1.5),
                                   name=label)
                    curves[name] = item
                # 记录颜色字符串供图例使用（pen 反推不可靠）
                item._legend_color = color
            pw.enableAutoRange()
            # 顶部条图例刷新（标题 + 颜色块 + 中文 label，同一行）
            if plot_idx < len(self._plot_headers):
                self._refresh_header_legend(self._plot_headers[plot_idx], curves, p)
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
        import pyqtgraph as pg
        legend = getattr(pw, '_legend_obj', None)
        if legend is None:
            return
        try:
            vb = pw.getViewBox()
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
                        import numpy as np
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
            view_rect = vb.viewRect()
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

        只在曲线上画 max/min（点位明确，argmax/argmin 给出具体坐标）；
        avg/last 不在曲线上画——avg 是统计值无对应点，last 由悬停 tooltip
        显示。avg/last 开关只影响悬停 tooltip 显示，不影响曲线标注。
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
                pts = []  # ScatterPlot 点
                txts = []  # TextItem 数值
                if show_max:
                    i = int(np.argmax(yd))
                    pts.append((xd[i], yd[i]))
                    txts.append((xd[i], yd[i], f'max={_fmt(float(yd[i]))}'))
                if show_min:
                    i = int(np.argmin(yd))
                    pts.append((xd[i], yd[i]))
                    txts.append((xd[i], yd[i], f'min={_fmt(float(yd[i]))}'))
                if pts:
                    xs = np.array([p[0] for p in pts])
                    ys = np.array([p[1] for p in pts])
                    sc = pg.ScatterPlotItem(
                        xs=xs, ys=ys,
                        symbol='star', size=12,
                        pen=pg.mkPen(color, width=1),
                        brush=pg.mkBrush(color))
                    pw.addItem(sc, ignoreBounds=True)
                    pw._stat_markers.append(sc)
                for tx, ty, text in txts:
                    t = pg.TextItem(text, color=color,
                                    fill='#1e1e1ecc', anchor=(0, 1))
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
        import pyqtgraph as pg
        from serial_debug.ui import qt_theme
        t = qt_theme.palette(self.theme_name)
        if not self._plots:
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
        p = self._plots[idx]
        dlg = QDialog(self._win if hasattr(self, '_win') else None)
        dlg.setWindowTitle(f'{p.get("title", "数据曲线")} - 单独显示')
        dlg.resize(900, 560)
        v = QVBoxLayout(dlg)
        v.setContentsMargins(0, 0, 0, 0)
        container, pw, header = self._make_plot_widget(p, t)
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
        if plot_idx >= len(self._plots):
            return
        import numpy as np
        import pyqtgraph as pg
        from PySide6.QtCore import Qt as _Qt
        p = self._plots[plot_idx]
        with self._data_lock:
            all_data_keys = list(self._data.keys())
        vars_to_show = p.get('vars') or all_data_keys
        # 清理不再属于该 plot 的曲线
        for name in list(curves.keys()):
            if name not in vars_to_show:
                try:
                    pw.removeItem(curves[name])
                except Exception:
                    pass
                del curves[name]
        # 创建/更新曲线
        for name in vars_to_show:
            if name not in self._data:
                continue
            pts = self._data[name]
            if not pts:
                continue
            xs, ys = self._get_xy(name)
            if xs is None or len(xs) == 0:
                continue
            meta = self._var_meta.get(name, {})
            color = meta.get('color') or self._palette[
                (all_data_keys.index(name)
                 if name in all_data_keys else 0) % len(self._palette)]
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
        # 顶部条图例刷新
        if header is not None:
            self._refresh_header_legend(header, curves, p)
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
        from PySide6.QtCore import Qt
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
                    t_str = _dt.datetime.fromtimestamp(snap_ts).strftime(
                        '%H:%M:%S')
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
