"""Qt 主程序 - 串口调试助手（Stage 4.1：骨架）。

迁移自 tkinter 版 app.py。本阶段聚焦核心串口调试功能：
  - 串口连接 / 断开（端口 + 波特率 + 数据位/校验/停止位/流控）
  - 单一日志窗口（发送 / 响应 / 周期日志，按时间顺序，颜色区分）
  - 命令输入 + 发送 + 追加 CRLF / 自动滚动 / 清空
  - 响应检测 + 超时提示 + pending 配对
  - 主题切换（VSCode Dark+ / 浅色，复用 qt_theme）
  - 状态栏：状态点（灰/绿/黄/红）+ 状态文本
  - 菜单栏：工具（打开 Qt 监视窗口）/ 帮助

后续 Stage：
  4.2  AT 命令面板（嵌入式 + 独立窗口）
  4.3  嵌入式 motor/BMS/IMU 监视
  4.4  日志查看器入口 / 筛选 / 日志落盘
  4.5  拆 tkinter

入口：环境变量 SERIAL_DEBUG_QT_MAIN=1 启动 Qt 主程序（默认仍走 tkinter）
"""
from __future__ import annotations

import os
import re
import sys
import time
import queue

from serial_debug.core.serial_backend import SerialBackend
from serial_debug.infra import config
from serial_debug.infra import themes


RESP_RE = re.compile(r'^(OK[%,]|ERROR|\+)')   # 响应行判定
TS_RE = re.compile(r'^(\d{13})\b')             # 设备时间戳：13 位毫秒
MAX_LINES = 5000
DRAIN_MS = 30
RESP_TIMEOUT = 2.0
SAVE_DEBOUNCE_MS = 400


def _resource_path(relative_path: str) -> str:
    """返回包内资源文件绝对路径，兼容 PyInstaller _MEIPASS。"""
    try:
        base_path = sys._MEIPASS  # type: ignore[attr-defined]
        return os.path.join(base_path, 'serial_debug', 'resources',
                           relative_path)
    except AttributeError:
        from importlib.resources import files
        base = files('serial_debug.resources')
        return str(base / relative_path)


class QtSerialDebugApp:
    """Qt 主程序（包装类，UI 在 _create() 延迟构建）。"""

    def __init__(self, theme_name: str = 'auto'):
        self.theme_name = theme_name
        self._app = None
        self._win = None
        # 配置
        self.cfg = config.load()
        self._theme_name_resolved = themes.resolve(self.theme_name)
        # 串口后端
        self.backend = SerialBackend(on_status=self._on_backend_status)
        # 状态
        self.pending_cmd: str | None = None
        self.pending_ts: float = 0.0
        self.auto_scroll: bool = True
        self.append_crlf: bool = bool(self.cfg.get('append_crlf', False))
        self.ts_convert: bool = bool(self.cfg.get('ts_convert_enabled', False))
        # 串口参数
        try:
            self._bytesize = int(self.cfg.get('bytesize', 8))
        except Exception:
            self._bytesize = 8
        self._parity = self.cfg.get('parity', 'none')
        try:
            self._stopbits = int(self.cfg.get('stopbits', 1))
        except Exception:
            self._stopbits = 1
        self._flow_control = self.cfg.get('flow_control', 'none')
        # UI 元素
        self._log_edit = None        # QPlainTextEdit
        self._cmd_edit = None        # QLineEdit
        self._port_combo = None
        self._baud_combo = None
        self._status_lbl = None
        self._status_dot = None      # QLabel 显示圆点
        self._connect_btn = None
        self._drain_timer = None
        self._save_timer = None
        # 状态点状态：idle / ok / wait / err
        self._dot_state: str = 'idle'
        self._connected_label: str = '未连接'

    # ---- 后端回调（在工作线程触发）----------------------------------
    def _on_backend_status(self, level: str, msg: str):
        """SerialBackend 在工作线程调用，无需特殊处理（drain 会拉队列）。"""
        pass

    # ---- UI 构建 -----------------------------------------------------
    def _create(self):
        from PySide6.QtCore import Qt, QTimer
        from PySide6.QtGui import QAction, QIcon
        from PySide6.QtWidgets import (
            QApplication, QMainWindow, QToolBar, QPlainTextEdit, QLineEdit,
            QPushButton, QComboBox, QLabel, QWidget, QHBoxLayout, QCheckBox,
            QFrame, QSizePolicy, QMessageBox, QVBoxLayout,
        )

        from serial_debug.ui import qt_theme

        app = QApplication.instance() or QApplication(sys.argv)
        qt_theme.apply_qss(app, self.theme_name)
        self._app = app

        win = QMainWindow()
        win.setWindowTitle('串口调试助手 (Qt)')
        try:
            geo = self.cfg.get('geometry') or '1100x680'
            parts = geo.split('x')
            if len(parts) == 2:
                win.resize(int(parts[0]), int(parts[1]))
            else:
                win.resize(1100, 680)
        except Exception:
            win.resize(1100, 680)
        win.setMinimumSize(820, 500)
        # 图标
        try:
            icon_path = _resource_path(
                os.path.join('assets', 'icon_debug.png'))
            if os.path.exists(icon_path):
                win.setWindowIcon(QIcon(icon_path))
        except Exception:
            pass

        # 顶栏：串口 / 波特率 / 数据位 / 校验 / 停止位 / 流控 / 连接 / 状态点
        tb = QToolBar('串口')
        tb.setMovable(False)
        win.addToolBar(tb)

        self._port_combo = QComboBox()
        self._port_combo.setEditable(True)
        self._port_combo.setMinimumWidth(180)
        for p in SerialBackend.list_ports():
            self._port_combo.addItem(p)
        cur_port = self.cfg.get('port', '')
        if cur_port:
            self._port_combo.setCurrentText(cur_port)
        tb.addWidget(self._port_combo)
        tb.addWidget(QLabel('  波特率: '))

        self._baud_combo = QComboBox()
        self._baud_combo.setEditable(True)
        for b in ('9600', '19200', '38400', '57600', '115200', '230400',
                  '460800', '921600'):
            self._baud_combo.addItem(b)
        try:
            cur_baud = str(self.cfg.get('baudrate', 115200))
        except Exception:
            cur_baud = '115200'
        self._baud_combo.setCurrentText(cur_baud)
        tb.addWidget(self._baud_combo)

        def _toggle_connect():
            if self.backend.is_open:
                self._do_disconnect()
            else:
                self._do_connect()
        self._connect_btn = QPushButton('连接')
        self._connect_btn.setProperty('primary', True)
        self._connect_btn.clicked.connect(_toggle_connect)
        tb.addWidget(self._connect_btn)

        # 分隔
        tb.addSeparator()

        # 状态点（用 QLabel + 圆形 stylesheet）
        self._status_dot = QLabel('●')
        self._status_dot.setStyleSheet(
            'color: #888; font-size: 16pt; padding: 0 8px 0 12px;')
        tb.addWidget(self._status_dot)

        self._status_lbl = QLabel('未连接')
        self._status_lbl.setMinimumWidth(120)
        tb.addWidget(self._status_lbl)

        tb.addSeparator()

        # 主题切换
        def _toggle_theme():
            cur = themes.resolve(self.theme_name)
            self.theme_name = 'light' if cur == 'dark' else 'dark'
            qt_theme.apply_qss(self._app, self.theme_name)
            self._theme_name_resolved = themes.resolve(self.theme_name)
            self._apply_theme_to_log()
            self._save_cfg_debounced()
        btn_theme = QPushButton('切换主题')
        btn_theme.clicked.connect(_toggle_theme)
        tb.addWidget(btn_theme)

        # 中央：日志显示
        self._log_edit = QPlainTextEdit()
        self._log_edit.setReadOnly(True)
        # 等宽字体 + 行高 + 不换行（横向滚动）
        from PySide6.QtGui import QFont
        mono = QFont(qt_theme.QT_MONO_FAMILY, qt_theme.QT_MONO_SIZE)
        self._log_edit.setFont(mono)
        self._log_edit.setLineWrapMode(QPlainTextEdit.NoWrap)
        # 滚动条跟随
        sb = self._log_edit.verticalScrollBar()
        sb.valueChanged.connect(self._on_scroll)
        win.setCentralWidget(self._log_edit)

        # 底栏：命令输入 + 发送 + 选项
        bottom = QFrame()
        bottom.setProperty('panel', True)
        bl = QHBoxLayout(bottom)
        bl.setContentsMargins(8, 6, 8, 6)
        bl.setSpacing(6)

        bl.addWidget(QLabel('命令:'))
        self._cmd_edit = QLineEdit()
        self._cmd_edit.returnPressed.connect(self._on_send_clicked)
        bl.addWidget(self._cmd_edit, 1)

        btn_send = QPushButton('发送')
        btn_send.setProperty('primary', True)
        btn_send.clicked.connect(self._on_send_clicked)
        bl.addWidget(btn_send)

        bl.addSpacing(12)
        cb_crlf = QCheckBox('追加 CRLF')
        cb_crlf.setChecked(self.append_crlf)
        cb_crlf.stateChanged.connect(self._on_crlf_toggled)
        bl.addWidget(cb_crlf)

        cb_autoscroll = QCheckBox('自动滚动')
        cb_autoscroll.setChecked(self.auto_scroll)
        cb_autoscroll.stateChanged.connect(self._on_autoscroll_toggled)
        bl.addWidget(cb_autoscroll)

        cb_ts = QCheckBox('时间戳转换')
        cb_ts.setChecked(self.ts_convert)
        cb_ts.stateChanged.connect(self._on_ts_convert_toggled)
        bl.addWidget(cb_ts)

        btn_clear = QPushButton('清空')
        btn_clear.clicked.connect(self._on_clear_clicked)
        bl.addWidget(btn_clear)

        # 把底栏挂到主窗口下方（QMainWindow 的 central 只能一个，用 dock 风格）
        # 用 QFrame 作为 QWidget 添加到 QMainWindow 底部
        # 简化：把 centralWidget 替换为包含 log + bottom 的容器
        central = QWidget()
        cv = QVBoxLayout(central)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(0)
        cv.addWidget(self._log_edit, 1)
        cv.addWidget(bottom)
        win.setCentralWidget(central)

        # 菜单栏
        self._build_menubar(win)

        # 状态栏：永久显示串口参数
        sb_widget = win.statusBar()
        sb_widget.showMessage('就绪')
        self._sb_params_lbl = QLabel(self._serial_params_str())
        sb_widget.addPermanentWidget(self._sb_params_lbl)

        # 关闭事件
        def _close_event(event):
            self._save_cfg_now()
            try:
                self.backend.close()
            except Exception:
                pass
            event.accept()
        win.closeEvent = _close_event

        self._win = win
        # 应用主题色到日志区
        self._apply_theme_to_log()
        # 启动 drain timer
        self._drain_timer = QTimer()
        self._drain_timer.timeout.connect(self._drain)
        self._drain_timer.start(DRAIN_MS)
        return win

    def _build_menubar(self, win):
        from PySide6.QtGui import QAction
        from PySide6.QtWidgets import QMenuBar
        mb = win.menuBar()

        # 工具菜单
        m_tools = mb.addMenu('工具')
        act_qt_monitor = QAction('Qt 监视窗口…', win)
        act_qt_monitor.setShortcut('Ctrl+M')
        act_qt_monitor.triggered.connect(self._open_qt_monitor)
        m_tools.addAction(act_qt_monitor)
        m_tools.addSeparator()
        act_refresh_ports = QAction('刷新端口列表', win)
        act_refresh_ports.triggered.connect(self._refresh_ports)
        m_tools.addAction(act_refresh_ports)

        # 帮助菜单
        m_help = mb.addMenu('帮助')
        act_about = QAction('关于…', win)
        act_about.triggered.connect(self._show_about)
        m_help.addAction(act_about)

    def _serial_params_str(self) -> str:
        bs = {8: '8', 7: '7', 6: '6', 5: '5'}.get(self._bytesize, '8')
        par = self._parity.upper()[0] if self._parity else 'N'
        sb = {1: '1', 2: '2', 1.5: '1.5'}.get(self._stopbits, '1')
        fc = 'RTS/CTS' if self._flow_control == 'rtscts' else 'None'
        return f'{bs}{par}{sb}  流控: {fc}'

    # ---- 串口连接 -----------------------------------------------------
    def _do_connect(self):
        port = self._port_combo.currentText().strip()
        baud = int(self._baud_combo.currentText() or '115200')
        if not port:
            self._status_lbl.setText('请选择端口')
            return
        # 串口参数
        import serial as pyserial
        bs_map = {8: pyserial.EIGHTBITS, 7: pyserial.SEVENBITS,
                  6: pyserial.SIXBITS, 5: pyserial.FIVEBITS}
        par_map = {'none': pyserial.PARITY_NONE, 'even': pyserial.PARITY_EVEN,
                   'odd': pyserial.PARITY_ODD, 'mark': pyserial.PARITY_MARK,
                   'space': pyserial.PARITY_SPACE}
        sb_map = {1: pyserial.STOPBITS_ONE, 2: pyserial.STOPBITS_TWO,
                  1.5: pyserial.STOPBITS_ONE_POINT_FIVE}
        ok = self.backend.open(
            port=port, baudrate=baud,
            bytesize=bs_map.get(self._bytesize, pyserial.EIGHTBITS),
            parity=par_map.get(self._parity, pyserial.PARITY_NONE),
            stopbits=sb_map.get(self._stopbits, pyserial.STOPBITS_ONE),
            rtscts=(self._flow_control == 'rtscts'))
        if ok:
            self._connect_btn.setText('断开')
            self._connected_label = f'已连接 {port} @ {baud}'
            self._dot_state = 'ok'
            self._update_status()
        else:
            self._dot_state = 'err'
            self._update_status()

    def _do_disconnect(self):
        self.backend.close()
        self._connect_btn.setText('连接')
        self._connected_label = '已断开'
        self._dot_state = 'idle'
        self._update_status()

    def _refresh_ports(self):
        if self._port_combo is None:
            return
        cur = self._port_combo.currentText()
        self._port_combo.clear()
        for p in SerialBackend.list_ports():
            self._port_combo.addItem(p)
        if cur:
            self._port_combo.setCurrentText(cur)

    # ---- 发送 / 日志显示 ---------------------------------------------
    def _on_send_clicked(self):
        if self._cmd_edit is None:
            return
        cmd = self._cmd_edit.text()
        if not cmd:
            return
        if not self.backend.is_open:
            self._status_lbl.setText('未连接，无法发送')
            self._dot_state = 'wait'
            self._update_status()
            return
        # 显示发送命令（带时间戳前缀）
        self._append_log(f'{self._ts_prefix_if_needed(cmd)}→ {cmd}\n',
                         tag='send')
        self.pending_cmd = cmd
        self.pending_ts = time.time()
        self.backend.send(cmd, append_crlf=self.append_crlf)
        self._cmd_edit.clear()

    def _on_clear_clicked(self):
        if self._log_edit is not None:
            self._log_edit.clear()

    def _on_crlf_toggled(self, state):
        self.append_crlf = bool(state)
        self._save_cfg_debounced()

    def _on_autoscroll_toggled(self, state):
        self.auto_scroll = bool(state)

    def _on_ts_convert_toggled(self, state):
        self.ts_convert = bool(state)
        self._save_cfg_debounced()

    def _on_scroll(self, val):
        # 用户主动滚动时关闭自动滚动（滚到底再开启）
        if self._log_edit is None:
            return
        sb = self._log_edit.verticalScrollBar()
        if sb.maximum() - val <= 2:
            self.auto_scroll = True

    # ---- drain（QTimer 替代 root.after）------------------------------
    def _drain(self):
        batch: list[tuple[str, str]] = []
        status_update = None
        error_msg = None
        closed = False
        had_pending = self.pending_cmd is not None
        try:
            while True:
                kind, payload = self.backend.rx_queue.get_nowait()
                if kind == 'line':
                    r = self._route_line(payload)
                    if r is not None:
                        batch.append(r)
                elif kind == 'status':
                    status_update = payload
                elif kind == 'error':
                    error_msg = payload
                elif kind == 'closed':
                    closed = True
        except queue.Empty:
            pass
        except Exception:
            pass

        if batch:
            self._append_batch(batch)

        if error_msg is not None:
            self._status_lbl.setText(error_msg)
            self._dot_state = 'err'
        elif status_update is not None:
            level, msg = status_update
            self._status_lbl.setText(msg)
            self._dot_state = 'ok' if level == 'info' else 'err'
            self._connected_label = msg
        if closed:
            self._connect_btn.setText('连接')
            self._connected_label = '连接已断开'
            self._status_lbl.setText(self._connected_label)
            self._dot_state = 'idle'

        # 响应超时检查
        if self.pending_cmd and (time.time() - self.pending_ts) > RESP_TIMEOUT:
            self._append_log('         ← (无响应)\n', tag='note')
            self.pending_cmd = None

        # 等待响应状态
        if self.pending_cmd and not closed:
            elapsed_ms = int((time.time() - self.pending_ts) * 1000)
            self._status_lbl.setText(f'等待响应中... {elapsed_ms}ms')
            self._dot_state = 'wait'
        elif had_pending and not self.pending_cmd and not closed:
            self._status_lbl.setText(self._connected_label)
            self._dot_state = 'ok' if self.backend.is_open else 'idle'

        self._update_status()

    def _route_line(self, line: str) -> tuple[str, str] | None:
        """行路由：判定响应/日志，配对 pending_cmd，加时间戳前缀。"""
        line = line.rstrip('\r\n')
        if not line:
            return None
        # 时间戳前缀
        prefix = self._ts_prefix_if_needed(line)
        # 响应判定
        if RESP_RE.match(line):
            tag = 'resp'
            self.pending_cmd = None
        elif line.startswith('→'):
            tag = 'send'
        else:
            tag = 'log'
        return (f'{prefix}{line}\n', tag)

    def _ts_prefix_if_needed(self, line: str) -> str:
        """13 位毫秒时间戳转可读时间（如果开启 ts_convert 且匹配）。"""
        if not self.ts_convert:
            return ''
        m = TS_RE.match(line)
        if not m:
            return ''
        try:
            ts_ms = int(m.group(1))
            secs = ts_ms / 1000.0
            # 时:分:秒.毫秒
            h = int(secs // 3600) % 24
            mi = int(secs // 60) % 60
            s = int(secs) % 60
            ms = ts_ms % 1000
            return f'{h:02d}:{mi:02d}:{s:02d}.{ms:03d} '
        except Exception:
            return ''

    def _append_batch(self, batch: list[tuple[str, str]]):
        for text, tag in batch:
            self._append_log(text, tag)

    def _append_log(self, text: str, tag: str = 'log'):
        """追加一段日志，按 tag 着色。"""
        if self._log_edit is None:
            return
        from PySide6.QtGui import QTextCharFormat, QColor, QTextCursor
        t = themes.THEMES.get(self._theme_name_resolved,
                              themes.THEMES['dark'])
        color_map = {
            'send': t.get('accent', '#569cd6'),
            'resp': t.get('log_resp', '#4ec9b0'),
            'log': t.get('fg', '#cccccc'),
            'note': t.get('muted', '#888888'),
            'error': t.get('status_err', '#f48771'),
        }
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color_map.get(tag, color_map['log'])))
        cursor = self._log_edit.textCursor()
        # PySide6 6.6+ 枚举作用域化：MoveOperation.End / SelectionType.BlockUnderCursor
        try:
            cursor.movePosition(QTextCursor.MoveOperation.End)
        except AttributeError:
            cursor.movePosition(QTextCursor.End)
        cursor.insertText(text, fmt)
        # 限制行数
        if self._log_edit.blockCount() > MAX_LINES:
            excess = self._log_edit.blockCount() - MAX_LINES
            cursor = self._log_edit.textCursor()
            try:
                cursor.movePosition(QTextCursor.MoveOperation.Start)
            except AttributeError:
                cursor.movePosition(QTextCursor.Start)
            for _ in range(excess):
                cursor.select(QTextCursor.SelectionType.BlockUnderCursor
                              if hasattr(QTextCursor, 'SelectionType')
                              else QTextCursor.BlockUnderCursor)
                cursor.removeSelectedText()
                cursor.deleteChar()
        if self.auto_scroll:
            self._log_edit.verticalScrollBar().setValue(
                self._log_edit.verticalScrollBar().maximum())

    def _update_status(self):
        if self._status_dot is None:
            return
        t = themes.THEMES.get(self._theme_name_resolved,
                              themes.THEMES['dark'])
        color_map = {
            'idle': t.get('muted', '#888'),
            'ok': t.get('status_ok', '#4ec9b0'),
            'wait': t.get('status_wait', '#dcdcaa'),
            'err': t.get('status_err', '#f48771'),
        }
        color = color_map.get(self._dot_state, color_map['idle'])
        self._status_dot.setStyleSheet(
            f'color: {color}; font-size: 16pt; padding: 0 8px 0 12px;')

    def _apply_theme_to_log(self):
        """主题切换后重新着色日志区背景。"""
        if self._log_edit is None:
            return
        t = themes.THEMES.get(self._theme_name_resolved,
                              themes.THEMES['dark'])
        from PySide6.QtGui import QColor
        self._log_edit.setStyleSheet(
            f'background-color: {t.get("panel_bg", t.get("bg", "#1e1e1e"))};'
            f'color: {t.get("fg", "#cccccc")};')
        self._update_status()

    # ---- 菜单动作 -----------------------------------------------------
    def _open_qt_monitor(self):
        """启动 Qt 监视窗口子进程（避免双事件循环冲突）。"""
        import subprocess
        env = dict(os.environ)
        env['SERIAL_DEBUG_QT_PILOT'] = '1'
        try:
            subprocess.Popen([sys.executable, '-m',
                              'serial_debug.ui.qt_monitor_window'],
                             env=env)
        except Exception as e:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.critical(self._win, '启动失败', str(e))

    def _show_about(self):
        from PySide6.QtWidgets import QMessageBox
        try:
            import PySide6
            versions = f'PySide6: {PySide6.__version__}\nPython: {sys.version.split()[0]}'
        except Exception as e:
            versions = f'版本获取失败: {e}'
        QMessageBox.information(
            self._win, '关于',
            '串口调试助手 (Qt) - Stage 4.1 骨架\n\n'
            '技术栈:\n' + versions + '\n\n'
            '功能:\n'
            '  - 串口连接 / 断开（端口/波特率/数据位/校验/停止位/流控）\n'
            '  - 单一日志窗口（发送/响应/日志，按时间顺序，颜色区分）\n'
            '  - 命令输入 + 发送（Enter 或按钮）\n'
            '  - 追加 CRLF / 自动滚动 / 时间戳转换 / 清空\n'
            '  - 响应检测 + 超时提示 + pending 配对\n'
            '  - 主题切换（VSCode Dark+ / 浅色）\n'
            '  - 工具菜单打开 Qt 监视窗口\n\n'
            '后续 Stage 4.2-4.5:\n'
            '  4.2  AT 命令面板\n'
            '  4.3  嵌入式 motor/BMS/IMU 监视\n'
            '  4.4  日志查看器入口 / 筛选 / 日志落盘\n'
            '  4.5  拆 tkinter')

    # ---- 配置持久化 ---------------------------------------------------
    def _save_cfg_debounced(self):
        from PySide6.QtCore import QTimer
        if self._save_timer is None:
            self._save_timer = QTimer()
            self._save_timer.setSingleShot(True)
            self._save_timer.timeout.connect(self._save_cfg_now)
        self._save_timer.start(SAVE_DEBOUNCE_MS)

    def _save_cfg_now(self):
        try:
            cfg = config.load()
            if self._port_combo is not None:
                cfg['port'] = self._port_combo.currentText()
            if self._baud_combo is not None:
                try:
                    cfg['baudrate'] = int(self._baud_combo.currentText())
                except Exception:
                    pass
            cfg['append_crlf'] = self.append_crlf
            cfg['ts_convert_enabled'] = self.ts_convert
            cfg['theme_pref'] = self.theme_name
            cfg['bytesize'] = self._bytesize
            cfg['parity'] = self._parity
            cfg['stopbits'] = self._stopbits
            cfg['flow_control'] = self._flow_control
            if self._win is not None:
                cfg['geometry'] = f'{self._win.width()}x{self._win.height()}'
            config.save(cfg)
        except Exception:
            pass

    def run(self):
        win = self._create()
        win.show()
        return self._app.exec()


def main():
    app = QtSerialDebugApp()
    sys.exit(app.run())


if __name__ == '__main__':
    main()
