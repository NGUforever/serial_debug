"""Qt + pyqtgraph 大数据曲线试点窗口。

独立进程启动，与 tkinter 主应用隔离，避免双事件循环冲突。
入口：python -m serial_debug.ui.qt_monitor_window [日志路径]
      或主程序设环境变量 SERIAL_DEBUG_QT_PILOT=1 启动子进程

试点目的：
  1. 验证 pyqtgraph 在 10w / 100w 点下平移/缩放的流畅度
  2. 验证 VSCode Dark+ QSS 主题视觉效果
  3. 验证 PyInstaller 打包 Qt + pyqtgraph 的体积与可运行性
  4. 通过后再逐步把 tkinter 监视窗口迁过来

未来：若决定全量切到 Qt，本文件会扩展为完整的 Qt 监视窗口，
     tkinter 版本废弃。
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

# 行内 tag → Panel 类映射，按出现顺序检测
_PANEL_REGISTRY = (
    (MotorMonitorPanel, ('3wd1t_1', '3wd1t_2')),
    (MotorMonitorPanel, None),  # STD_DEBUG 走 _STD_DEBUG_MARKER_RE，tag 动态生成
    (BmsMonitorPanel, ('BMS', 'BAT')),
    (ImuMonitorPanel, ('Imu_data',)),
)


def _iter_panel_vars(line: str):
    """从一行日志中提取所有 Panel 的 (var_name, value)。

    motor / bms / imu 三类 Panel 各自调 _parse_tagged_line + DEFAULT_VARS
    取值。变量名加 Panel 前缀避免冲突（motor. / bms. / imu.）。
    """
    ts = _extract_log_ts(line)
    if ts is None:
        return
    # motor: 3wd1t_1 / 3wd1t_2 / Lxx_STD_DEBUG
    for panel_cls, tag in (('motor', '3wd1t_1'), ('motor', '3wd1t_2')):
        blocks = panel_cls._parse_tagged_line(line, tag)
        if not blocks:
            continue
        for v in panel_cls.DEFAULT_VARS:
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


class QtMonitorWindow:
    """Qt 监视窗口（包装类，便于后续扩展）。

    真正的 QWidget 在 _create() 里延迟创建，避免模块导入就 init Qt。
    """

    def __init__(self, log_path: str | None = None,
                 theme_name: str = 'auto'):
        self.log_path = log_path
        self.theme_name = theme_name
        self._app = None
        self._win = None
        self._plot = None
        # 变量数据：{var_name: list[(ts, val)]}
        self._data: dict[str, list[tuple[float, float]]] = {}
        # 每变量一个颜色（与 tkinter 色板一致）
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
        # 实时模式下每变量保留最近 N 点（避免 list 无限增长）
        self._max_points = 60_000

    # ---- 数据加载 ----------------------------------------------------
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
                        hit = True
                    if hit:
                        n += 1
        except Exception:
            pass
        return n

    def gen_mock_data(self, n_points: int) -> None:
        """生成 n_points 模拟数据：正弦 + 随机尖峰，用于性能验证。

        numpy 向量化 + 直接存 (xs, ys) array 元组，不做 zip/tolist 转换。
        100w 点从 740ms → < 30ms。
        """
        import numpy as np
        t = np.arange(n_points, dtype=np.float64) * 0.001  # 1ms 一点 = 1000Hz
        data1 = np.sin(2 * np.pi * 50 * t) * 50 + 50
        data2 = np.cos(2 * np.pi * 30 * t) * 30 + 20
        rng = np.random.default_rng(42)
        spikes = rng.random(n_points) < 0.001
        data2 = np.where(spikes, data2 + 80, data2)
        data3 = t + rng.uniform(-0.5, 0.5, n_points)
        # 直接存 (xs, ys) numpy array 元组，零拷贝传给 pyqtgraph
        self._data = {
            'sin_50hz': (t, data1),
            'cos_with_spikes': (t.copy(), data2),
            'ramp': (t.copy(), data3),
        }

    # ---- UI ----------------------------------------------------------
    def _create(self):
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QAction, QColor
        from PySide6.QtWidgets import (QApplication, QMainWindow, QToolBar,
                                       QPushButton, QStatusBar, QVBoxLayout,
                                       QWidget)
        import pyqtgraph as pg

        from serial_debug.ui import qt_theme

        # QApplication 单例
        app = QApplication.instance() or QApplication(sys.argv)
        qt_theme.apply_qss(app, self.theme_name)
        self._app = app

        # pyqtgraph 全局配置：抗锯齿、平滑曲线
        # 背景色取自主题（与 QSS 同源），后建 PlotWidget 会沿用此默认
        t_init = qt_theme.palette(self.theme_name)
        pg.setConfigOption('background', t_init.get('panel_bg', '#1e1e1e'))
        pg.setConfigOption('foreground', t_init.get('fg', '#cccccc'))
        pg.setConfigOption('antialias', True)
        # 启用 OpenGL 加速（大数据下显著提升）—失败回退到默认
        try:
            pg.setConfigOption('useOpenGL', True)
            pg.setConfigOption('enableExperimental', True)
        except Exception:
            pass

        win = QMainWindow()
        win.setWindowTitle('[Qt 试点] 大数据曲线窗口 - PySide6 + pyqtgraph')
        win.resize(1400, 800)

        # 中央 PlotWidget
        plot_widget = pg.PlotWidget()
        plot_widget.showGrid(x=True, y=True, alpha=0.2)
        plot_widget.setLabel('left', '值')
        plot_widget.setLabel('bottom', '时间', units='s')
        self._plot = plot_widget
        win.setCentralWidget(plot_widget)

        # 工具栏
        tb = QToolBar('工具')
        win.addToolBar(tb)
        self._toolbar = tb

        def _add_curve():
            from PySide6.QtWidgets import QFileDialog
            path, _ = QFileDialog.getOpenFileName(
                win, '打开日志文件', os.path.expanduser('~'),
                '日志 (*.txt *.log);;所有文件 (*)')
            if path:
                n = self.load_log(path)
                self._refresh_plot()
                win.statusBar().showMessage(f'加载 {n} 行，{len(self._data)} 变量')

        btn_open = QPushButton('打开日志…')
        btn_open.clicked.connect(_add_curve)
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
            # pg.setConfigOption 只影响后建的 PlotWidget；已建的需手动 setBackground
            # 同时刷新轴/网格色，让 PlotItem 跟随新主题
            if self._plot is not None:
                try:
                    self._plot.setBackground(t.get('panel_bg', t.get('bg', '#1e1e1e')))
                    # 轴文字颜色
                    for axis_name in ('left', 'bottom'):
                        axis = self._plot.getAxis(axis_name)
                        if axis is not None:
                            axis.setPen(t.get('muted', '#cccccc'))
                            axis.setTextPen(t.get('fg', '#cccccc'))
                except Exception:
                    pass
                # 强制重画所有曲线：清空 PlotDataItem 缓存 + 重新绘制
                # 确保主题切换瞬时生效（背景色、轴色、曲线 pen 都更新）
                self._curves = None
                self._refresh_plot()

        btn_theme = QPushButton('切换主题')
        btn_theme.clicked.connect(_toggle_theme)
        tb.addWidget(btn_theme)

        # 状态栏：永久显示技术栈信息（让用户一眼确认是 Qt 试点窗口）
        from PySide6.QtWidgets import QLabel
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

        # F1: 关于本窗口（明确告诉用户这是 Qt 试点窗口）
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
                '这是 Qt 试点窗口（非 tkinter）\n\n'
                '技术栈:\n' + versions + '\n\n'
                '用途:\n'
                '  - 验证 pyqtgraph 大数据曲线性能\n'
                '  - 验证 VSCode Dark+ QSS 主题\n'
                '  - 通过后逐步把 tkinter 监视窗口迁到 Qt\n\n'
                '对比基准:\n'
                '  - tkinter Canvas 10w 点开始卡\n'
                '  - pyqtgraph + OpenGL 100w 点流畅\n\n'
                '点击 "性能基准" 按钮可一键测量渲染耗时。')

        act_about = QAction('关于本窗口', win)
        act_about.setShortcut('F1')
        act_about.triggered.connect(_show_about)
        win.addAction(act_about)

        # 性能基准按钮：一键生成 100w 点 + 测量渲染耗时
        def _perf_test():
            import time
            import math
            import random
            random.seed(42)
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
                   f'对比参考（同样 100w 点 × 3 条）:\n'
                   f'  tkinter Canvas 直接画 100w 点: 10+ 秒（不实用）\n'
                   f'  tkinter + LTTB 降采样到 2000 点: ~20 ms（视觉无损）\n'
                   f'  pyqtgraph + numpy + OpenGL: 预期 < 50 ms\n\n'
                   f'用鼠标拖动/滚轮缩放测试交互流畅度。')
            win.statusBar().showMessage(
                f'渲染 {n} 点完成: 数据 {t_gen:.0f}ms + 绘制 {t_draw:.0f}ms')
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(win, '性能基准', msg)

        btn_perf = QPushButton('性能基准（100w 点）')
        btn_perf.clicked.connect(_perf_test)
        tb.addWidget(btn_perf)

        # 实时串口 feed：弹出端口/波特率对话框，后台线程读串口
        def _connect_serial():
            from PySide6.QtWidgets import (QDialog, QDialogButtonBox,
                                            QFormLayout, QLineEdit,
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

        self._win = win
        return win

    # ---- 实时串口 feed ----------------------------------------------
    def start_serial(self, port: str, baudrate: int = 921600):
        """启动后台线程读串口，按 \\n 切行喂给解析器，QTimer 节流刷新。

        线程只读写 self._data；UI 线程定时 _refresh_plot。不直接调 Qt API，
        避免跨线程 GUI 操作（Qt 严格要求 GUI 调用在主线程）。
        """
        import threading
        import serial  # pyserial
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
                # 按 \n 切行：剩余残行留 buf 等下次
                while '\n' in self._serial_buf:
                    line, self._serial_buf = self._serial_buf.split('\n', 1)
                    line = line.rstrip('\r')
                    if not line:
                        continue
                    try:
                        for ts, name, val in _iter_panel_vars(line + '\n'):
                            buf = self._data.setdefault(name, [])
                            buf.append((ts, val))
                            # 实时模式限长，避免 list 无限增长
                            if len(buf) > self._max_points:
                                # 切片比 deque 快（一次性丢一半）
                                del buf[:len(buf) - self._max_points]
                    except Exception:
                        pass
            try:
                ser.close()
            except Exception:
                pass

        t = threading.Thread(target=_reader, daemon=True)
        self._serial_thread = t
        t.start()
        # QTimer 25 FPS 节流刷新 plot
        from PySide6.QtCore import QTimer
        if self._refresh_timer is None:
            self._refresh_timer = QTimer()
            self._refresh_timer.timeout.connect(self._refresh_plot)
        self._refresh_timer.start(40)  # 40ms = 25 FPS

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

    def _refresh_plot(self):
        if self._plot is None:
            return
        import numpy as np
        import pyqtgraph as pg
        # 复用 PlotDataItem：setData() 比每次 plot() + clear() 快 5-10 倍
        # 首次调用时为每条曲线创建 PlotDataItem 并缓存；后续只更新数据
        if not hasattr(self, '_curves') or self._curves is None:
            self._curves = {}  # name -> PlotDataItem
        cur_names = set(self._data.keys())
        # 清理不再存在的曲线 item
        for name in list(self._curves.keys()):
            if name not in cur_names:
                try:
                    self._plot.removeItem(self._curves[name])
                except Exception:
                    pass
                del self._curves[name]
        for i, (name, pts) in enumerate(self._data.items()):
            color = self._palette[i % len(self._palette)]
            if not pts:
                continue
            # 双路径：
            # (a) numpy array 元组 (xs, ys)：直接传给 pyqtgraph，零拷贝
            # (b) list[tuple]：批量转 numpy（np.array C 实现比 fromiter 快）
            if (isinstance(pts, tuple) and len(pts) == 2
                    and hasattr(pts[0], '__array_interface__')):
                xs, ys = pts
            else:
                n = len(pts)
                if n == 0:
                    continue
                try:
                    # np.array(list[tuple]) 是 C 实现，比 fromiter + generator 快
                    arr = np.asarray(pts, dtype=np.float64)
                    xs = arr[:, 0]
                    ys = arr[:, 1]
                    # 缓存回 numpy，下次 _refresh_plot 直接走快路径
                    self._data[name] = (xs, ys)
                except Exception:
                    xs = np.array([p[0] for p in pts], dtype=float)
                    ys = np.array([p[1] for p in pts], dtype=float)
            if name in self._curves:
                item = self._curves[name]
                try:
                    item.setData(xs, ys)
                except Exception:
                    pass
            else:
                item = self._plot.plot(xs, ys,
                                       pen=pg.mkPen(color, width=1.5),
                                       name=name)
                self._curves[name] = item
        # 自动范围（仅首次或数据集变化时）
        self._plot.enableAutoRange()

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
        description='Qt + pyqtgraph 大数据曲线试点窗口')
    parser.add_argument('log', nargs='?', default=None,
                        help='日志文件路径（可选）')
    parser.add_argument('--theme', default='auto',
                        help='主题：dark / light / auto')
    args = parser.parse_args()

    w = QtMonitorWindow(log_path=args.log, theme_name=args.theme)
    sys.exit(w.run())


if __name__ == '__main__':
    main()
