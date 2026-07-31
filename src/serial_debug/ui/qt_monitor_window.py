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


def _iter_panel_vars(line: str):
    """从一行日志中提取所有 Panel 的 (var_name, value)。

    motor / bms / imu 三类 Panel 各自调 _parse_tagged_line + DEFAULT_VARS
    取值。变量名加 Panel 前缀避免冲突（motor. / bms. / imu.）。
    """
    ts = _extract_log_ts(line)
    if ts is None:
        return
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
    }


def _normalize_plot(p: dict) -> dict:
    d = _default_plot()
    if not p:
        return d
    d.update({k: p[k] for k in d if k in p})
    d['vars'] = list(d.get('vars', []))
    return d


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
            QHBoxLayout, QPushButton, QGroupBox, QLabel,
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
        # 变量数据：{var_name: (xs_array, ys_array) | list[(ts, val)]}
        self._data: dict = {}
        # 变量元数据：{name: {color, label, y_min, y_max, warn_min, warn_max}}
        # 自动从 self._data 派生（load_log / gen_mock_data 加新 var 时同步登记）
        # 用户可改 color/label，持久化到 qt_monitor_vars
        self._var_meta: dict[str, dict] = self._load_var_meta()
        # 配置：plots list（每项 = {title, x_label, y_label, vars, show_*}）
        self._plots: list[dict] = self._load_plots()
        # UI：PlotWidget 列表 + 每个对应的 curves dict（name -> PlotDataItem）
        self._plot_widgets: list = []
        self._curves: list[dict] = []
        self._plots_layout = None
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

    # ---- 配置持久化 -------------------------------------------------
    def _load_plots(self) -> list[dict]:
        from serial_debug.infra import config
        try:
            cfg = config.load()
            raw = cfg.get('qt_monitor_plots', [])
            if not raw:
                return [_default_plot()]
            return [_normalize_plot(p) for p in raw]
        except Exception:
            return [_default_plot()]

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
            QVBoxLayout, QWidget, QScrollArea, QSizePolicy, QLabel,
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

        # 右侧：可滚动容器 + 垂直布局放多个 PlotWidget
        right_pane = QWidget()
        outer = QVBoxLayout(right_pane)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(8)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)

        plots_container = QWidget()
        self._plots_layout = QVBoxLayout(plots_container)
        self._plots_layout.setContentsMargins(0, 0, 0, 0)
        self._plots_layout.setSpacing(8)
        self._plots_layout.addStretch(1)

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

        # 工具栏
        tb = QToolBar('工具')
        win.addToolBar(tb)
        self._toolbar = tb

        # 图表管理：添加 / 编辑 / 删除
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

        btn_add = QPushButton('添加图表…')
        btn_add.clicked.connect(_add_plot)
        tb.addWidget(btn_add)

        def _edit_plot():
            idx = self._current_plot_idx()
            if idx is None:
                from PySide6.QtWidgets import QMessageBox
                QMessageBox.information(win, '编辑图表', '请先选中一个图表')
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

        btn_edit = QPushButton('编辑图表…')
        btn_edit.clicked.connect(_edit_plot)
        tb.addWidget(btn_edit)

        def _del_plot():
            from PySide6.QtWidgets import QMessageBox
            idx = self._current_plot_idx()
            if idx is None:
                QMessageBox.information(win, '删除图表', '请先选中一个图表')
                return
            title = self._plots[idx].get('title', '数据曲线')
            if QMessageBox.question(
                    win, '删除图表',
                    f'确认删除图表 "{title}" ？'
            ) != QMessageBox.Yes:
                return
            del self._plots[idx]
            if not self._plots:
                self._plots.append(_default_plot())
            self._rebuild_plots_ui()
            self._save_plots()
            self._refresh_plot()
            win.statusBar().showMessage('已删除图表')

        btn_del = QPushButton('删除图表')
        btn_del.clicked.connect(_del_plot)
        tb.addWidget(btn_del)

        tb.addSeparator()

        # 打开日志
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

        btn_open = QPushButton('打开日志…')
        btn_open.clicked.connect(_open_log)
        tb.addWidget(btn_open)

        def _mock_10w():
            self.gen_mock_data(100_000)
            self._refresh_plot()
            win.statusBar().showMessage('已生成 10w 模拟数据')

        btn_mock10 = QPushButton('模拟 10w 点')
        btn_mock10.clicked.connect(_mock_10w)
        tb.addWidget(btn_mock10)

        def _mock_100w():
            self.gen_mock_data(1_000_000)
            self._refresh_plot()
            win.statusBar().showMessage('已生成 100w 模拟数据')

        btn_mock100 = QPushButton('模拟 100w 点')
        btn_mock100.clicked.connect(_mock_100w)
        tb.addWidget(btn_mock100)

        def _clear():
            self._data.clear()
            self._refresh_plot()
            win.statusBar().showMessage('已清空')

        btn_clear = QPushButton('清空')
        btn_clear.clicked.connect(_clear)
        tb.addWidget(btn_clear)

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
            # 主题切换后：重设每个 PlotWidget 的背景/轴色，并重画曲线
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
            # 清缓存 PlotDataItem，重画曲线（pen 颜色需重设）
            self._curves = [{} for _ in self._plots]
            self._refresh_plot()

        btn_theme = QPushButton('切换主题')
        btn_theme.clicked.connect(_toggle_theme)
        tb.addWidget(btn_theme)

        def _toggle_sidebar():
            self._toggle_sidebar()
        btn_sidebar = QPushButton('切换侧栏')
        btn_sidebar.clicked.connect(_toggle_sidebar)
        tb.addWidget(btn_sidebar)

        tb.addSeparator()

        # 统计对话框：每变量 当前/最小/最大/均值/点数
        def _show_stats():
            self._show_statistics(win)
        btn_stats = QPushButton('统计…')
        btn_stats.clicked.connect(_show_stats)
        tb.addWidget(btn_stats)

        # CSV 导出
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
        btn_csv = QPushButton('导出 CSV…')
        btn_csv.clicked.connect(_export_csv)
        tb.addWidget(btn_csv)

        # 状态栏：永久显示技术栈信息
        try:
            import PySide6
            import pyqtgraph
            tech_label = QLabel(
                f'PySide6 {PySide6.__version__} | pyqtgraph {pyqtgraph.__version__} | '
                f'Qt 试点窗口（非 tkinter）')
        except Exception:
            tech_label = QLabel('Qt 试点窗口')
        win.statusBar().addPermanentWidget(tech_label)
        win.statusBar().showMessage('就绪 - 按 F1 查看窗口信息')

        # F1：关于本窗口
        def _show_about():
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
                win, '关于本窗口',
                'Qt 数据监视窗口（Stage 1：多 plot + 配置持久化）\n\n'
                '技术栈:\n' + versions + '\n\n'
                '功能:\n'
                '  - 多 plot 子图（QScrollArea + QVBoxLayout）\n'
                '  - 添加/编辑/删除 plot 对话框\n'
                '  - 配置持久化（qt_monitor_plots / qt_monitor_geometry）\n'
                '  - 加载日志 / 模拟数据 / 串口实时 feed / 主题切换\n\n'
                '后续 Stage:\n'
                '  2. 变量列表侧栏 / 显示模式（曲线/数据）\n'
                '  3. 统计对话框 / CSV 导出\n'
                '  4. 主程序迁 Qt / 日志查看器迁 Qt / 拆 tkinter')

        act_about = QAction('关于本窗口', win)
        act_about.setShortcut('F1')
        act_about.triggered.connect(_show_about)
        win.addAction(act_about)

        # 性能基准按钮
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

        btn_perf = QPushButton('性能基准（100w 点）')
        btn_perf.clicked.connect(_perf_test)
        tb.addWidget(btn_perf)

        # 实时串口 feed
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

        btn_serial = QPushButton('连串口实时 feed')
        btn_serial.clicked.connect(_connect_serial)
        tb.addWidget(btn_serial)

        def _disconnect_serial():
            try:
                self.stop_serial()
                win.statusBar().showMessage('已断开串口')
            except Exception:
                pass

        btn_disc = QPushButton('断开串口')
        btn_disc.clicked.connect(_disconnect_serial)
        tb.addWidget(btn_disc)

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
                idx = self._current_plot_idx()
                if idx is None:
                    return
                vars_list = self._plots[idx].setdefault('vars', [])
                if name not in vars_list:
                    vars_list.append(name)
                self._save_plots()
                self._refresh_plot()
                self._refresh_sidebar_visibility()

            def _remove_from_current_plot():
                idx = self._current_plot_idx()
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
                if (isinstance(pts, tuple) and len(pts) == 2):
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
        try:
            cfg = config.load()
            cfg['qt_monitor_sidebar_visible'] = self._sidebar_visible
            config.save(cfg)
        except Exception:
            pass

    # ---- 统计 & CSV 导出 --------------------------------------------
    def _get_xy(self, name: str):
        """取一个变量的 (xs, ys) numpy 数组（list 路径会缓存回 self._data）。"""
        import numpy as np
        pts = self._data.get(name)
        if pts is None:
            return None, None
        if (isinstance(pts, tuple) and len(pts) == 2
                and hasattr(pts[0], '__array_interface__')):
            return pts[0], pts[1]
        if not pts:
            return None, None
        try:
            arr = np.asarray(pts, dtype=np.float64)
            xs = arr[:, 0]
            ys = arr[:, 1]
            self._data[name] = (xs, ys)
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

    def _current_plot_idx(self) -> int | None:
        """返回当前聚焦/选中的 plot 索引。

        简化实现：如果有 plot，返回第一个（暂无选中态）。
        后续 Stage 加 focus 跟踪后再改。
        """
        if not self._plot_widgets:
            return None
        return 0

    def _rebuild_plots_ui(self):
        """根据 self._plots 重建 PlotWidget 列表（增删改 plot 后调用）。"""
        import pyqtgraph as pg
        from PySide6.QtWidgets import QSizePolicy

        from serial_debug.ui import qt_theme
        t = qt_theme.palette(self.theme_name)

        # 清空旧的 PlotWidget
        layout = self._plots_layout
        if layout is None:
            return
        # 先移除末尾的 stretch
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

        # 重建
        for p in self._plots:
            pw = pg.PlotWidget()
            pw.showGrid(x=True, y=True, alpha=0.2)
            pw.setLabel('left', p.get('y_label', '值'))
            pw.setLabel('bottom', p.get('x_label', '时间 (s)'))
            pw.setTitle(p.get('title', '数据曲线'))
            pw.setBackground(t.get('panel_bg', t.get('bg', '#1e1e1e')))
            for axis_name in ('left', 'bottom'):
                axis = pw.getAxis(axis_name)
                if axis is not None:
                    axis.setPen(t.get('muted', '#cccccc'))
                    axis.setTextPen(t.get('fg', '#cccccc'))
            # 每个 plot 给一个合理最小高度，避免被压缩到 0
            pw.setMinimumHeight(220)
            sp = pw.sizePolicy()
            sp.setVerticalPolicy(QSizePolicy.Preferred)
            pw.setSizePolicy(sp)
            self._plot_widgets.append(pw)
            self._curves.append({})
            layout.addWidget(pw)
        # 末尾 stretch
        layout.addStretch(1)

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
                        for ts, name, val in _iter_panel_vars(line + '\n'):
                            buf = self._data.setdefault(name, [])
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
        if not self._plot_widgets:
            self._refresh_sidebar_values()
            self._refresh_sidebar_visibility()
            return
        import numpy as np
        import pyqtgraph as pg

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
            for name in vars_to_show:
                if name not in self._data:
                    continue
                pts = self._data[name]
                if not pts:
                    continue
                # 双路径：numpy 元组直接用；list 批量转 numpy
                if (isinstance(pts, tuple) and len(pts) == 2
                        and hasattr(pts[0], '__array_interface__')):
                    xs, ys = pts
                else:
                    n = len(pts)
                    if n == 0:
                        continue
                    try:
                        arr = np.asarray(pts, dtype=np.float64)
                        xs = arr[:, 0]
                        ys = arr[:, 1]
                        self._data[name] = (xs, ys)
                    except Exception:
                        xs = np.array([p[0] for p in pts], dtype=float)
                        ys = np.array([p[1] for p in pts], dtype=float)
                # 颜色：优先 var_meta，缺省回退到调色板
                meta = self._var_meta.get(name, {})
                color = meta.get('color') or self._palette[
                    (all_data_keys.index(name)
                     if name in all_data_keys else 0)
                    % len(self._palette)]
                if name in curves:
                    item = curves[name]
                    try:
                        item.setData(xs, ys, pen=pg.mkPen(color, width=1.5))
                    except Exception:
                        pass
                else:
                    item = pw.plot(xs, ys,
                                   pen=pg.mkPen(color, width=1.5),
                                   name=name)
                    curves[name] = item
            pw.enableAutoRange()
        # 同步侧栏当前值 + 在图标记
        self._refresh_sidebar_values()
        self._refresh_sidebar_visibility()

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
