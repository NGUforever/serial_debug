"""Qt 主程序 - 串口调试助手。

迁移自原 tkinter 版 app.py。功能：
  - 串口连接 / 断开（端口 + 波特率 + 数据位/校验/停止位/流控）
  - 单一日志窗口（发送 / 响应 / 周期日志，按时间顺序，颜色区分）
  - 命令输入 + 发送 + 追加 CRLF / 自动滚动 / 清空
  - 响应检测 + 超时提示 + pending 配对
  - 主题切换（VSCode Dark+ / 浅色，复用 qt_theme）
  - 状态栏：状态点（灰/绿/黄/红）+ 状态文本
  - 菜单栏：工具（数据监视窗口 / AT 命令面板 / 日志查看器 / 刷新端口）
  - 筛选：正则表达式 + 启用开关，仅对周期日志行生效
  - 日志落盘：独立线程写入，不影响收发显示
  - 日志查看器：子进程启动 QtLogViewerWindow

入口：默认即 Qt 主程序（v1.1.0 起 tkinter 兼容路径已移除）
"""
from __future__ import annotations

import os
import queue
import re
import sys
import time

from serial_debug.core.serial_backend import SerialBackend
from serial_debug.infra import config, themes

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


def _make_status_dot_class():
    """返回 _StatusDot 类（延迟构造，避免模块导入即 init Qt）。"""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QPainter
    from PySide6.QtWidgets import QWidget

    from serial_debug.infra import themes

    class _StatusDot(QWidget):
        """自绘圆形状态点：idle 灰 / ok 绿 / wait 黄 / err 红。"""

        def __init__(self, parent=None):
            super().__init__(parent)
            self._state = 'idle'
            self._theme_resolved = 'dark'
            self.setFixedSize(18, 18)

        def set_state(self, state: str, theme_resolved: str):
            self._state = state
            self._theme_resolved = theme_resolved
            self.update()

        def paintEvent(self, event):
            t = themes.THEMES.get(self._theme_resolved, themes.THEMES['dark'])
            color_map = {
                'idle': t.get('status_idle', t.get('muted', '#888')),
                'ok': t.get('status_ok', '#4ec9b0'),
                'wait': t.get('status_wait', '#dcdcaa'),
                'err': t.get('status_err', '#f48771'),
            }
            color = color_map.get(self._state, color_map['idle'])
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            # 外圈柔和光晕（更淡的同色半透明）
            halo = QColor(color)
            halo.setAlpha(60)
            p.setBrush(halo)
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(0, 0, 18, 18)
            # 内圈实心圆点
            p.setBrush(QColor(color))
            p.drawEllipse(4, 4, 10, 10)

    return _StatusDot


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
        self._scroll_suppress: bool = False
        self.append_crlf: bool = bool(self.cfg.get('append_crlf', False))
        self.ts_convert: bool = bool(self.cfg.get('ts_convert_enabled', False))
        # 原始数据显示模式：串口收到的行原样追加，不解析 LOG_TS / 不识别响应 / 不过滤
        # 场景：开机阶段固件用 printf 打印纯文本日志，无 13 位 ms 时间戳，
        # 解析模式会把这种行误判成普通 log 但无时间戳前缀，可读性差。
        # 开启 raw 模式后 _drain 把 rx_queue 的 ('line', text) 直接 _append_log。
        self.raw_display: bool = bool(self.cfg.get('raw_display_enabled', False))
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
        # 子窗口引用（避免被 GC）
        self._at_window_qt = None
        # 连接按钮图标缓存（_create 内填充）
        self._connect_icon = None
        self._disconnect_icon = None
        # 工具栏引用（视图菜单显隐用）
        self._toolbar = None
        # AT 命令面板嵌入/独立/隐藏
        self._at_mode: str = self.cfg.get('at_window_mode', 'detached')
        self._at_panel_qt = None     # AtCmdPanelQt 实例（嵌入或独立）
        self._at_panel_widget = None # 嵌入时的 QWidget
        self._main_splitter = None   # QSplitter（日志 | AT 面板）
        # 筛选（Stage 4.4）
        self._filter_pattern: str = self.cfg.get('filter_pattern', '') or ''
        self._filter_enabled: bool = bool(self.cfg.get('filter_enabled', False))
        self._filter_compiled: re.Pattern | None = None
        try:
            if self._filter_pattern:
                self._filter_compiled = re.compile(self._filter_pattern)
        except re.error:
            self._filter_compiled = None
        self._filter_edit = None     # QLineEdit
        self._filter_cb = None       # QCheckBox
        # 日志落盘（Stage 4.4）
        self.log_writer = None       # LogWriter 实例
        self.log_path: str = self.cfg.get('log_path', '') or ''
        self._log_btn = None         # QPushButton（切换落盘 / 停止）
        self._log_status = None     # QLabel

    # ---- 后端回调（在工作线程触发）----------------------------------
    def _on_backend_status(self, level: str, msg: str):
        """SerialBackend 在工作线程调用，无需特殊处理（drain 会拉队列）。"""
        pass

    # ---- UI 构建 -----------------------------------------------------
    def _create(self):
        from PySide6.QtCore import QTimer
        from PySide6.QtGui import QIcon
        from PySide6.QtWidgets import (
            QApplication,
            QCheckBox,
            QComboBox,
            QFrame,
            QHBoxLayout,
            QLabel,
            QLineEdit,
            QMainWindow,
            QPlainTextEdit,
            QPushButton,
            QSizePolicy,
            QToolBar,
            QVBoxLayout,
            QWidget,
        )

        from serial_debug.ui import qt_theme

        app = QApplication.instance() or QApplication(sys.argv)
        # 显式设置 applicationName/displayName/desktopFileName，配合 desktop
        # file 让 GNOME dock 正确归类为"串口调试助手"。
        app.setApplicationName('serial_debug')
        app.setApplicationDisplayName('串口调试助手')
        try:
            app.setDesktopFileName('serial_debug')
        except Exception:
            pass
        qt_theme.apply_qss(app, self.theme_name)
        self._app = app

        win = QMainWindow()
        win.setWindowTitle('串口调试助手')
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

        # 顶栏：端口 / 波特率 / 连接 / 状态点 / 主题切换
        from PySide6.QtCore import QSize
        tb = QToolBar('串口')
        tb.setMovable(False)
        tb.setIconSize(QSize(16, 16))
        win.addToolBar(tb)
        self._toolbar = tb

        # 组1：连接组
        tb.addWidget(QLabel('端口'))
        self._port_combo = QComboBox()
        self._port_combo.setEditable(True)
        self._port_combo.setMinimumWidth(180)
        for p in SerialBackend.list_ports():
            self._port_combo.addItem(p)
        cur_port = self.cfg.get('port', '')
        if cur_port:
            self._port_combo.setCurrentText(cur_port)
        tb.addWidget(self._port_combo)

        tb.addWidget(QLabel('波特率'))
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
        # 连接按钮图标：用 SP_MediaPlay（连接）/ SP_MediaStop（断开）
        try:
            from PySide6.QtWidgets import QStyle
            self._connect_icon = win.style().standardIcon(
                QStyle.SP_MediaPlay)
            self._disconnect_icon = win.style().standardIcon(
                QStyle.SP_MediaStop)
            self._connect_btn.setIcon(self._connect_icon)
        except Exception:
            self._connect_icon = None
            self._disconnect_icon = None
        self._connect_btn.clicked.connect(_toggle_connect)
        tb.addWidget(self._connect_btn)

        # 分组2：状态点 + 文本
        tb.addSeparator()
        StatusDot = _make_status_dot_class()
        self._status_dot = StatusDot()
        self._status_dot.setToolTip('连接状态')
        tb.addWidget(self._status_dot)

        self._status_lbl = QLabel('未连接')
        self._status_lbl.setMinimumWidth(120)
        tb.addWidget(self._status_lbl)

        # 弹簧把主题按钮推到最右
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)

        # 组3：主题切换
        def _toggle_theme():
            cur = themes.resolve(self.theme_name)
            self.theme_name = 'light' if cur == 'dark' else 'dark'
            qt_theme.apply_qss(self._app, self.theme_name)
            self._theme_name_resolved = themes.resolve(self.theme_name)
            self._apply_theme_to_log()
            self._save_cfg_debounced()
        btn_theme = QPushButton('切换主题')
        try:
            from PySide6.QtWidgets import QStyle
            btn_theme.setIcon(win.style().standardIcon(
                QStyle.SP_DialogResetButton))
        except Exception:
            pass
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

        # 欢迎提示
        self._append_log('串口调试助手 (Qt) 就绪\n', tag='note')
        self._append_log('请选择端口并点击「连接」开始调试\n\n', tag='note')

        # 底栏：卡片容器 + 流式布局（功能组按需换行）
        # 同一功能组（命令/选项/筛选/落盘/清空）作为一个不可分割的
        # QWidget，QFlowLayout 在一行排不下时整组换行，保证按钮
        # 文字不被截断、同功能不被切到多行。
        bottom = QFrame()
        bottom.setProperty('card', True)
        from PySide6.QtWidgets import QStyle

        from .qt_flow_layout import QFlowLayout
        bl = QFlowLayout(bottom, margin=10, h_spacing=12, v_spacing=6)

        # 组 1：命令 + 发送（命令输入框要装较多字符，给足 minimumWidth）
        g1 = QWidget()
        g1l = QHBoxLayout(g1)
        g1l.setContentsMargins(0, 0, 0, 0)
        g1l.setSpacing(6)
        g1l.addWidget(QLabel('命令:'))
        self._cmd_edit = QLineEdit()
        self._cmd_edit.setPlaceholderText('输入命令，回车发送')
        self._cmd_edit.setMinimumWidth(180)
        self._cmd_edit.returnPressed.connect(self._on_send_clicked)
        g1l.addWidget(self._cmd_edit, 1)
        btn_send = self._make_btn('发送', icon_std=QStyle.SP_ArrowRight,
                                  primary=True)
        btn_send.clicked.connect(self._on_send_clicked)
        g1l.addWidget(btn_send)
        bl.addWidget(g1)

        bl.addWidget(self._make_divider())

        # 组 2：选项（CRLF / 自动滚动 / 时间戳 / 原始模式）四个 checkbox 一起
        g2 = QWidget()
        g2l = QHBoxLayout(g2)
        g2l.setContentsMargins(0, 0, 0, 0)
        g2l.setSpacing(8)
        cb_crlf = QCheckBox('追加 CRLF')
        cb_crlf.setChecked(self.append_crlf)
        cb_crlf.stateChanged.connect(self._on_crlf_toggled)
        g2l.addWidget(cb_crlf)
        cb_autoscroll = QCheckBox('自动滚动')
        cb_autoscroll.setChecked(self.auto_scroll)
        cb_autoscroll.stateChanged.connect(self._on_autoscroll_toggled)
        g2l.addWidget(cb_autoscroll)
        cb_ts = QCheckBox('时间戳转换')
        cb_ts.setChecked(self.ts_convert)
        cb_ts.stateChanged.connect(self._on_ts_convert_toggled)
        g2l.addWidget(cb_ts)
        # 原始数据显示模式：开机 printf 日志等无 LOG_TS 的纯文本，
        # 串口收到什么就显示什么，不解析时间戳/不识别响应/不过滤。
        self._cb_raw = QCheckBox('原始模式')
        self._cb_raw.setChecked(self.raw_display)
        self._cb_raw.setToolTip(
            '原始模式：串口收到的行原样显示，不解析时间戳/不识别响应/不过滤。\n'
            '适用场景：开机阶段固件 printf 打印的纯文本日志，无 13 位 ms 时间戳。')
        self._cb_raw.stateChanged.connect(self._on_raw_display_toggled)
        g2l.addWidget(self._cb_raw)
        bl.addWidget(g2)

        bl.addWidget(self._make_divider())

        # 组 3：筛选（输入框可能装多个关键字，给足 minimumWidth）
        g3 = QWidget()
        g3l = QHBoxLayout(g3)
        g3l.setContentsMargins(0, 0, 0, 0)
        g3l.setSpacing(6)
        g3l.addWidget(QLabel('筛选:'))
        self._filter_edit = QLineEdit()
        self._filter_edit.setPlaceholderText('正则表达式')
        self._filter_edit.setText(self._filter_pattern)
        self._filter_edit.setMinimumWidth(180)
        self._filter_edit.returnPressed.connect(self._on_filter_committed)
        self._filter_edit.editingFinished.connect(self._on_filter_committed)
        g3l.addWidget(self._filter_edit, 1)
        self._filter_cb = QCheckBox('启用')
        self._filter_cb.setChecked(self._filter_enabled)
        self._filter_cb.stateChanged.connect(self._on_filter_enable_toggled)
        g3l.addWidget(self._filter_cb)
        bl.addWidget(g3)

        bl.addWidget(self._make_divider())

        # 组 4：落盘 + 状态
        g4 = QWidget()
        g4l = QHBoxLayout(g4)
        g4l.setContentsMargins(0, 0, 0, 0)
        g4l.setSpacing(6)
        self._log_btn = self._make_btn('日志落盘…',
                                       icon_std=QStyle.SP_DialogSaveButton)
        self._log_btn.clicked.connect(self._on_log_btn_clicked)
        g4l.addWidget(self._log_btn)
        self._log_status = QLabel('未落盘')
        self._log_status.setMinimumWidth(100)
        g4l.addWidget(self._log_status)
        bl.addWidget(g4)

        bl.addWidget(self._make_divider())

        # 组 5：清空（靠右独立）
        g5 = QWidget()
        g5l = QHBoxLayout(g5)
        g5l.setContentsMargins(0, 0, 0, 0)
        g5l.setSpacing(0)
        btn_clear = self._make_btn('清空',
                                   icon_std=QStyle.SP_DialogDiscardButton)
        btn_clear.clicked.connect(self._on_clear_clicked)
        g5l.addWidget(btn_clear)
        bl.addWidget(g5)

        # 把底栏挂到主窗口下方
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(6, 6, 6, 6)
        lv.setSpacing(6)
        lv.addWidget(self._log_edit, 1)
        lv.addWidget(bottom)

        # 主 Splitter：左侧日志 + 右侧 AT 面板（嵌入时）
        from PySide6.QtWidgets import QSplitter
        self._main_splitter = QSplitter()
        # 禁止折叠到 0：拖动时子窗口至少保留 minimumWidth，
        # 避免 QTableWidget 动态 minimumSizeHint 导致宽度跳变。
        self._main_splitter.setChildrenCollapsible(False)
        self._main_splitter.addWidget(left)
        win.setCentralWidget(self._main_splitter)

        # 按配置恢复 AT 面板模式
        self._apply_at_mode(self._at_mode)

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
                if self.log_writer is not None:
                    self.log_writer.stop()
                    self.log_writer = None
            except Exception:
                pass
            try:
                self.backend.close()
            except Exception:
                pass
            event.accept()
        win.closeEvent = _close_event

        self._win = win
        # 恢复窗口几何
        try:
            from PySide6.QtCore import QByteArray
            geo_hex = self.cfg.get('qt_main_geometry', '')
            if geo_hex:
                ba = QByteArray.fromHex(bytes(geo_hex, 'ascii'))
                win.restoreGeometry(ba)
        except Exception:
            pass
        # 应用主题色到日志区
        self._apply_theme_to_log()
        # 启动 drain timer
        self._drain_timer = QTimer()
        self._drain_timer.timeout.connect(self._drain)
        self._drain_timer.start(DRAIN_MS)
        return win

    def _build_menubar(self, win):
        from PySide6.QtGui import QAction, QIcon
        from PySide6.QtWidgets import QStyle
        mb = win.menuBar()
        style = win.style()

        def _icon(sp):
            try:
                return style.standardIcon(sp)
            except Exception:
                return QIcon()

        # 工具菜单
        m_tools = mb.addMenu('工具')
        act_qt_monitor = QAction(_icon(QStyle.SP_ComputerIcon),
                                  '数据监视窗口…', win)
        act_qt_monitor.setShortcut('Ctrl+M')
        act_qt_monitor.triggered.connect(self._open_qt_monitor)
        m_tools.addAction(act_qt_monitor)
        act_at = QAction(_icon(QStyle.SP_FileDialogListView),
                         'AT 命令面板…', win)
        act_at.setShortcut('Ctrl+T')
        act_at.triggered.connect(self._open_at_window)
        m_tools.addAction(act_at)
        act_logview = QAction(_icon(QStyle.SP_DialogOpenButton),
                               '打开日志查看器…', win)
        act_logview.setShortcut('Ctrl+L')
        act_logview.triggered.connect(self._open_log_viewer)
        m_tools.addAction(act_logview)
        m_tools.addSeparator()
        act_refresh_ports = QAction(_icon(QStyle.SP_BrowserReload),
                                     '刷新端口列表', win)
        act_refresh_ports.triggered.connect(self._refresh_ports)
        m_tools.addAction(act_refresh_ports)

        # 视图菜单
        m_view = mb.addMenu('视图')
        # AT 命令面板模式
        from PySide6.QtWidgets import QMenu
        at_mode_menu = QMenu('AT 命令面板', win)
        self._act_at_embedded = at_mode_menu.addAction('嵌入侧栏')
        self._act_at_embedded.setCheckable(True)
        self._act_at_embedded.triggered.connect(lambda: self._apply_at_mode('embedded'))
        self._act_at_detached = at_mode_menu.addAction('独立窗口')
        self._act_at_detached.setCheckable(True)
        self._act_at_detached.triggered.connect(lambda: self._apply_at_mode('detached'))
        self._act_at_hidden = at_mode_menu.addAction('隐藏')
        self._act_at_hidden.setCheckable(True)
        self._act_at_hidden.triggered.connect(lambda: self._apply_at_mode('hidden'))
        m_view.addMenu(at_mode_menu)
        m_view.addSeparator()
        # 监视窗口
        act_monitor = QAction('数据监视窗口…', win)
        act_monitor.setShortcut('Ctrl+M')
        act_monitor.triggered.connect(self._open_qt_monitor)
        m_view.addAction(act_monitor)
        # 日志查看器
        act_logview = QAction('日志查看器…', win)
        act_logview.setShortcut('Ctrl+L')
        act_logview.triggered.connect(self._open_log_viewer)
        m_view.addAction(act_logview)
        m_view.addSeparator()
        # 工具栏显隐
        self._act_toolbar = QAction('工具栏', win)
        self._act_toolbar.setCheckable(True)
        self._act_toolbar.setChecked(True)
        self._act_toolbar.triggered.connect(self._toggle_toolbar)
        m_view.addAction(self._act_toolbar)
        # 状态栏显隐
        self._act_statusbar = QAction('状态栏', win)
        self._act_statusbar.setCheckable(True)
        self._act_statusbar.setChecked(True)
        self._act_statusbar.triggered.connect(self._toggle_statusbar)
        m_view.addAction(self._act_statusbar)

        # 帮助菜单
        m_help = mb.addMenu('帮助')
        act_about = QAction(_icon(QStyle.SP_MessageBoxInformation),
                             '关于…', win)
        act_about.triggered.connect(self._show_about)
        m_help.addAction(act_about)

    def _serial_params_str(self) -> str:
        bs = {8: '8', 7: '7', 6: '6', 5: '5'}.get(self._bytesize, '8')
        par = self._parity.upper()[0] if self._parity else 'N'
        sb = {1: '1', 2: '2', 1.5: '1.5'}.get(self._stopbits, '1')
        fc = 'RTS/CTS' if self._flow_control == 'rtscts' else 'None'
        return f'{bs}{par}{sb}  流控: {fc}'

    @staticmethod
    def _make_divider():
        """底栏分组用的竖直分隔条。"""
        from PySide6.QtWidgets import QFrame
        line = QFrame()
        line.setProperty('divider', True)
        line.setFixedWidth(1)
        return line

    def _make_btn(self, text, icon_std=None, primary=False):
        """创建带 minimumWidth 的 QPushButton。

        底栏空间不够时 layout 会压缩 widget 到 minimumSizeHint，
        QPushButton 默认 minimumSizeHint 不含完整文字宽度，会被截断。
        这里按 文字宽度 + 图标宽度 + padding 算出 minimumWidth，
        让 layout 优先压缩 stretch=1 的 QLineEdit 而不是按钮。
        """
        from PySide6.QtWidgets import QApplication, QPushButton
        btn = QPushButton(text)
        if primary:
            btn.setProperty('primary', True)
        if icon_std is not None:
            try:
                style = (self._win.style() if self._win
                         else QApplication.style())
                btn.setIcon(style.standardIcon(icon_std))
            except Exception:
                pass
        fm = btn.fontMetrics()
        text_w = fm.horizontalAdvance(text)
        icon_w = 22 if icon_std is not None else 0
        # padding：按钮左右内边距 + 留一点呼吸空间
        btn.setMinimumWidth(text_w + icon_w + 24)
        return btn

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
            if self._disconnect_icon is not None:
                self._connect_btn.setIcon(self._disconnect_icon)
            self._connected_label = f'已连接 {port} @ {baud}'
            self._dot_state = 'ok'
            self._update_status()
        else:
            self._dot_state = 'err'
            self._update_status()

    def _do_disconnect(self):
        self.backend.close()
        self._connect_btn.setText('连接')
        if self._connect_icon is not None:
            self._connect_btn.setIcon(self._connect_icon)
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

    def _toggle_toolbar(self, checked):
        if self._toolbar is not None:
            self._toolbar.setVisible(checked)

    def _toggle_statusbar(self, checked):
        if self._win is not None:
            self._win.statusBar().setVisible(checked)

    # ---- AT 面板模式切换（嵌入 / 独立 / 隐藏）-----------------------
    def _apply_at_mode(self, mode: str):
        """切换 AT 命令面板呈现模式：embedded / detached / hidden。"""
        self._at_mode = mode
        # 更新菜单勾选
        if hasattr(self, '_act_at_embedded') and self._act_at_embedded is not None:
            self._act_at_embedded.setChecked(mode == 'embedded')
            self._act_at_detached.setChecked(mode == 'detached')
            self._act_at_hidden.setChecked(mode == 'hidden')
        # 关闭旧独立窗口
        if mode != 'detached' and self._at_window_qt is not None:
            try:
                dlg = self._at_window_qt._dlg
                if dlg is not None:
                    dlg.close()
                self._at_window_qt.panel._widget = None
            except Exception:
                pass
            self._at_window_qt = None
        # 移除旧嵌入面板
        if self._at_panel_widget is not None:
            try:
                self._at_panel_widget.setVisible(False)
                self._main_splitter.removeWidget(self._at_panel_widget)
                self._at_panel_widget.setParent(None)
                self._at_panel_widget.deleteLater()
            except Exception:
                pass
            self._at_panel_widget = None
            self._at_panel_qt = None

        if mode == 'embedded':
            self._build_at_embedded()
        elif mode == 'detached':
            self._open_at_window()
        # hidden: 什么都不做

        self._save_cfg_debounced()

    def _build_at_embedded(self):
        """在主 Splitter 右侧嵌入 AT 命令面板。"""
        if self._main_splitter is None:
            return
        from serial_debug.ui.qt_at_cmd_window import AtCmdPanelQt
        initial = self.cfg.get('at_commands', [])

        def _on_change(cmds: list):
            try:
                self.cfg['at_commands'] = cmds
                config.save(self.cfg)
            except Exception:
                pass

        self._at_panel_qt = AtCmdPanelQt(
            send_callback=self._send_cmd_external,
            initial_cmds=initial,
            on_change=_on_change)
        self._at_panel_widget = self._at_panel_qt._build(parent=self._main_splitter)
        self._main_splitter.addWidget(self._at_panel_widget)
        # 设置 splitter 初始比例：日志 70% : AT 30%
        total = self._main_splitter.width() or 1000
        self._main_splitter.setSizes([int(total * 0.7), int(total * 0.3)])

    # ---- 发送 / 日志显示 ---------------------------------------------
    def _on_send_clicked(self):
        if self._cmd_edit is None:
            return
        cmd = self._cmd_edit.text()
        if not cmd:
            return
        if not self._send_cmd(cmd):
            return
        self._cmd_edit.clear()

    def _send_cmd(self, cmd: str) -> bool:
        """发送一条命令到串口。返回 True=已发送，False=未连接。

        同时把命令回显到日志（带 → 前缀和时间戳转换），设置 pending_cmd
        等待响应。供命令输入框和 AT 命令面板共用。
        """
        if not cmd:
            return False
        if not self.backend.is_open:
            if self._status_lbl is not None:
                self._status_lbl.setText('未连接，无法发送')
            self._dot_state = 'wait'
            self._update_status()
            return False
        self._append_log(f'{self._ts_prefix_if_needed(cmd)}→ {cmd}\n',
                         tag='send')
        self.pending_cmd = cmd
        self.pending_ts = time.time()
        self.backend.send(cmd, append_crlf=self.append_crlf)
        return True

    # 别名供 AT 命令面板调用（语义更明确）
    _send_cmd_external = _send_cmd

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

    def _on_raw_display_toggled(self, state):
        """切换原始数据显示模式。

        开启：_drain 把 rx_queue 的 ('line', text) 直接 _append_log(text, 'log')，
        不走 _route_line（不解析 LOG_TS、不识别响应、不过滤）。
        关闭：恢复解析模式，所有行重走 _route_line。
        落盘 log_writer 在两种模式下都正常工作。
        """
        prev = self.raw_display
        self.raw_display = bool(state)
        self._save_cfg_debounced()
        # 给用户一条提示，避免误以为没生效
        if prev != self.raw_display and self._log_edit is not None:
            tag = 'note'
            msg = ('[原始模式开启] 串口收到的行原样显示，不解析时间戳/不识别响应\n'
                   if self.raw_display
                   else '[原始模式关闭] 恢复解析模式（时间戳转换 + 响应识别 + 筛选）\n')
            try:
                self._append_log(msg, tag=tag)
            except Exception:
                # 提示失败不影响功能，记一下
                from serial_debug.infra.error_log import log_exc
                log_exc('debug', f'{__file__}:_on_raw_display_toggled append tip failed', None)

    def _on_scroll(self, val):
        """用户上滚时关闭自动滚动（参照 tkinter 版 _check_scroll）。

        注意：只在用户上滚时关闭 auto_scroll，不在滚到底时自动开启。
        开启 auto_scroll 由用户 checkbox 或 _append_log 末尾的 setValue 触发。
        """
        if self._log_edit is None:
            return
        if self._scroll_suppress:
            return
        sb = self._log_edit.verticalScrollBar()
        # 用户上滚（距底部远）→关闭自动滚动
        if sb.maximum() - val > sb.singleStep() * 3:
            self.auto_scroll = False

    # ---- drain（QTimer 替代 root.after）------------------------------
    def _drain(self):
        batch: list[tuple[str, str]] = []
        raw_batch: list[str] = []  # 原始模式：纯文本，不走 _route_line
        status_update = None
        error_msg = None
        closed = False
        had_pending = self.pending_cmd is not None
        raw_mode = self.raw_display
        try:
            while True:
                kind, payload = self.backend.rx_queue.get_nowait()
                if kind == 'line':
                    if raw_mode:
                        # 原始模式：串口收到的行原样追加，不解析 / 不识别响应 / 不过滤
                        # 仍走 log_writer 落盘，确保开机日志可追溯
                        raw_batch.append(payload)
                    else:
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
        except Exception as e:
            # 教训 5：裸 except Exception: pass 会吞 NameError 等故障现场
            from serial_debug.infra.error_log import log_exc
            log_exc('warning', f'{__file__}:_drain loop failed', e)

        if batch:
            self._append_batch(batch)
        if raw_batch:
            self._append_raw_batch(raw_batch)

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
            if self._connect_icon is not None:
                self._connect_btn.setIcon(self._connect_icon)
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
            # 筛选：只对周期日志行生效；发送/响应行始终显示
            if (self._filter_enabled
                    and self._filter_compiled is not None
                    and not self._filter_compiled.search(line)):
                return None
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

    def _append_raw_batch(self, raw_batch: list[str]) -> None:
        """原始模式：把串口收到的行原样追加到日志区。

        - 不走 _route_line：无时间戳前缀、无响应分类、无筛选
        - 仍走 log_writer 落盘（_append_log 内部已处理）
        - 用 core.raw_renderer.format_raw_line 归一化行尾与控制字符
        - tag 一律 'log'：原始模式下响应/日志同色，保持"收到什么显示什么"
        """
        from serial_debug.core.raw_renderer import format_raw_line
        for line in raw_batch:
            try:
                text = format_raw_line(line)
            except Exception as e:
                # format_raw_line 不会主动抛，但调用方传非 str 时防御
                from serial_debug.infra.error_log import log_exc
                log_exc('debug', f'{__file__}:_append_raw_batch format failed line={line!r}', e)
                text = (line if isinstance(line, str) else str(line)) + '\n'
            self._append_log(text, tag='log')

    def _append_log(self, text: str, tag: str = 'log'):
        """追加一段日志，按 tag 着色。

        参照 tkinter 版 _append 的逻辑：
          1. insert 到文档末尾
          2. _trim() 从顶部删除旧行
          3. auto_scroll 时滚到底部
        """
        if self.log_writer is not None:
            try:
                self.log_writer.put(text)
            except Exception:
                pass
        if self._log_edit is None:
            return
        from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor
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
        doc = self._log_edit.document()
        # 1. 在文档末尾追加文本（用独立 cursor，不依赖 widget 的 cursor 位置）
        cursor = QTextCursor(doc)
        try:
            cursor.movePosition(QTextCursor.MoveOperation.End)
        except AttributeError:
            cursor.movePosition(QTextCursor.End)
        cursor.insertText(text, fmt)
        # 2. 限制行数：从顶部一次性删除旧行（参照 tkinter _trim）
        if doc.blockCount() > MAX_LINES:
            excess = doc.blockCount() - MAX_LINES
            self._scroll_suppress = True
            try:
                trim_cursor = QTextCursor(doc)
                # 选中从文档开头到第 excess 行末尾
                block = doc.firstBlock()
                for _ in range(excess - 1):
                    block = block.next()
                trim_cursor.setPosition(doc.firstBlock().position())
                trim_cursor.setPosition(block.position() + block.length(),
                                        QTextCursor.MoveMode.KeepAnchor)
                trim_cursor.removeSelectedText()
            finally:
                self._scroll_suppress = False
        # 3. auto_scroll 时滚到底部
        if self.auto_scroll:
            self._scroll_suppress = True
            try:
                self._log_edit.verticalScrollBar().setValue(
                    self._log_edit.verticalScrollBar().maximum())
            finally:
                self._scroll_suppress = False

    def _update_status(self):
        if self._status_dot is None:
            return
        try:
            self._status_dot.set_state(self._dot_state,
                                      self._theme_name_resolved)
        except Exception:
            pass

    def _apply_theme_to_log(self):
        """主题切换后重新着色日志区背景。"""
        if self._log_edit is None:
            return
        t = themes.THEMES.get(self._theme_name_resolved,
                              themes.THEMES['dark'])
        self._log_edit.setStyleSheet(
            f'background-color: {t.get("panel_bg", t.get("bg", "#1e1e1e"))};'
            f'color: {t.get("fg", "#cccccc")};')
        self._update_status()

    # ---- 菜单动作 -----------------------------------------------------
    def _open_qt_monitor(self):
        """启动数据监视窗口子进程（避免双事件循环冲突）。

        用 sys.executable + 环境变量 SERIAL_DEBUG_QT_PILOT=1 走 main()
        里的 QtMonitorWindow 路径，兼容 PyInstaller 打包后 -m 不识别。
        """
        import subprocess
        env = dict(os.environ)
        env['SERIAL_DEBUG_QT_PILOT'] = '1'
        try:
            subprocess.Popen([sys.executable], env=env)
        except Exception as e:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.critical(self._win, '启动失败', str(e))

    def _open_at_window(self):
        """打开 AT 命令面板（独立非模态窗口）。"""
        # 如果已有独立窗口，直接激活
        if self._at_window_qt is not None:
            try:
                dlg = self._at_window_qt._dlg
                if dlg is not None and dlg.isVisible():
                    dlg.activateWindow()
                    dlg.raise_()
                    return
            except Exception:
                pass
        from serial_debug.ui.qt_at_cmd_window import AtCmdWindowQt
        initial = self.cfg.get('at_commands', [])

        def _on_change(cmds: list):
            try:
                self.cfg['at_commands'] = cmds
                config.save(self.cfg)
            except Exception:
                pass

        self._at_window_qt = AtCmdWindowQt(
            send_callback=self._send_cmd_external,
            initial_cmds=initial,
            on_change=_on_change)
        dlg = self._at_window_qt._build(parent=self._win)
        # 恢复几何
        try:
            from PySide6.QtCore import QByteArray
            geo_hex = self.cfg.get('qt_at_geometry', '')
            if geo_hex:
                ba = QByteArray.fromHex(bytes(geo_hex, 'ascii'))
                dlg.restoreGeometry(ba)
        except Exception:
            pass
        # 关闭时保存几何
        def _on_close(event):
            try:
                self.cfg['qt_at_geometry'] = dlg.saveGeometry().data().hex()
                config.save(self.cfg)
            except Exception:
                pass
            event.accept()
        dlg.closeEvent = _on_close
        dlg.show()

    # ---- 筛选 / 日志落盘 / 日志查看器（Stage 4.3-4.4）----------------
    def _on_filter_enable_toggled(self, state):
        self._filter_enabled = bool(state)
        self._save_cfg_debounced()

    def _on_filter_committed(self):
        """用户在筛选框回车或失焦：重新编译正则。"""
        if self._filter_edit is None:
            return
        pat = self._filter_edit.text().strip()
        self._filter_pattern = pat
        if pat:
            try:
                self._filter_compiled = re.compile(pat)
            except re.error as e:
                self._filter_compiled = None
                if self._status_lbl is not None:
                    self._status_lbl.setText(f'筛选正则错误: {e}')
        else:
            self._filter_compiled = None
        self._save_cfg_debounced()

    def _on_log_btn_clicked(self):
        if self.log_writer is not None:
            self._stop_log_writer()
        else:
            self._start_log_writer()

    def _start_log_writer(self):
        from PySide6.QtWidgets import QFileDialog, QMessageBox

        from serial_debug.core.log_writer import LogWriter
        # 默认目录：上次保存路径所在目录，否则用户家目录
        if self.log_path:
            default_path = self.log_path
        else:
            default_path = os.path.expanduser('~/serial_debug.log')
        path, _ = QFileDialog.getSaveFileName(
            self._win, '选择日志落盘文件', default_path,
            '日志 (*.log);;文本 (*.txt);;所有文件 (*)')
        if not path:
            return
        lw = LogWriter(path)
        if not lw.start():
            QMessageBox.critical(self._win, '日志落盘失败',
                                 lw.error or '无法打开文件')
            return
        self.log_writer = lw
        self.log_path = path
        if self._log_btn is not None:
            self._log_btn.setText('停止落盘')
        if self._log_status is not None:
            self._log_status.setText(f'落盘中: {os.path.basename(path)}')
        self._save_cfg_debounced()

    def _stop_log_writer(self):
        if self.log_writer is None:
            return
        try:
            self.log_writer.stop()
        except Exception:
            pass
        self.log_writer = None
        if self._log_btn is not None:
            self._log_btn.setText('日志落盘…')
        if self._log_status is not None:
            self._log_status.setText('已停止')

    def _open_log_viewer(self):
        """子进程启动 Qt 日志查看器窗口。

        用 sys.executable + 环境变量 SERIAL_DEBUG_LOG_VIEWER=1 走 main()
        里的 _run_log_viewer_only([]) 路径，子进程内启动 QtLogViewerWindow
        （PySide6 + QPlainTextEdit + QTabWidget），与主程序 Qt 路径一致；
        兼容 PyInstaller 打包后 -m 不识别的情况。
        """
        import subprocess
        env = dict(os.environ)
        env['SERIAL_DEBUG_LOG_VIEWER'] = '1'
        try:
            subprocess.Popen([sys.executable], env=env)
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
            '串口调试助手\n\n'
            '技术栈:\n' + versions + '\n\n'
            '功能:\n'
            '  - 串口连接 / 断开（端口/波特率/数据位/校验/停止位/流控）\n'
            '  - 单一日志窗口（发送/响应/日志，按时间顺序，颜色区分）\n'
            '  - 命令输入 + 发送（Enter 或按钮）\n'
            '  - 追加 CRLF / 自动滚动 / 时间戳转换 / 原始模式 / 清空\n'
            '  - 原始模式：串口收到的行原样显示（开机 printf 日志无 LOG_TS）\n'
            '  - 响应检测 + 超时提示 + pending 配对\n'
            '  - 主题切换（VSCode Dark+ / 浅色）\n'
            '  - 工具菜单：数据监视窗口 / AT 命令面板 / 日志查看器\n'
            '  - 筛选（正则）+ 日志落盘（独立线程）\n'
            '  - 默认入口即 Qt（v1.1.0 起无 tkinter 兼容路径）')

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
            cfg['raw_display_enabled'] = self.raw_display
            cfg['theme_pref'] = self.theme_name
            cfg['bytesize'] = self._bytesize
            cfg['parity'] = self._parity
            cfg['stopbits'] = self._stopbits
            cfg['flow_control'] = self._flow_control
            cfg['filter_pattern'] = self._filter_pattern
            cfg['filter_enabled'] = self._filter_enabled
            cfg['log_path'] = self.log_path
            cfg['at_window_mode'] = self._at_mode
            if self._win is not None:
                cfg['geometry'] = f'{self._win.width()}x{self._win.height()}'
                try:
                    cfg['qt_main_geometry'] = self._win.saveGeometry().data().hex()
                except Exception:
                    pass
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
