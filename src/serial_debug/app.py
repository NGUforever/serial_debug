"""串口调试助手 —— tkinter GUI。

布局：
  顶栏  串口 / 波特率 / 连接 / 状态灯
  中部  单一日志窗口（发送 / 响应 / 周期日志，按时间顺序，颜色区分）
  底栏  命令输入 + 发送 + 日志开/关快捷 + 自动滚动/追加换行/清空

行路由：命中响应正则 (^OK,) 的行配对 pending_cmd，其余为周期日志。
所有内容显示在同一 Text 控件，用 tag 颜色区分类型。
所有控件写入在主线程，经 root.after(30) 排空 rx_queue。
"""
from __future__ import annotations

import os
import re
import sys
import time
import queue
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from serial_debug.core.serial_backend import SerialBackend
from serial_debug.core import log_writer
from serial_debug.ui.at_cmd_window import AtCmdWindow
from serial_debug.ui.log_viewer_window import LogViewerWindow
from serial_debug.ui.monitor_window import (MotorMonitorWindow, BmsMonitorWindow,
                            ImuMonitorWindow,
                            MotorMonitorPanel, BmsMonitorPanel,
                            ImuMonitorPanel, FillCheck)
from serial_debug.widgets.auto_combobox import AutoCombobox
from serial_debug.infra import themes
from serial_debug.infra import config


def resource_path(relative_path: str) -> str:
    """返回包内资源文件绝对路径，兼容 PyInstaller 打包和开发模式。

    PyInstaller 打包后资源解压到 sys._MEIPASS/serial_debug/resources；
    开发模式用 importlib.resources 解析包路径。
    """
    try:
        base_path = sys._MEIPASS  # type: ignore[attr-defined]
        return os.path.join(base_path, 'serial_debug', 'resources', relative_path)
    except AttributeError:
        from importlib.resources import files
        base = files('serial_debug.resources')
        return str(base / relative_path)

RESP_RE = re.compile(r'^(OK[%,]|ERROR|\+)')   # 响应行判定：OK, / OK% / ERROR / +CMD
TS_RE = re.compile(r'^(\d{13})\b')              # 设备时间戳：13 位毫秒
MAX_LINES = 5000                            # 最多保留行数
DRAIN_MS = 30                               # 刷新间隔
RESP_TIMEOUT = 2.0                          # 等待响应超时(秒)
SAVE_DEBOUNCE_MS = 400                      # 配置保存去抖延时


class SerialDebugApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title('串口调试助手')
        self.cfg = config.load()
        try:
            root.geometry(self.cfg.get('geometry') or '1100x680')
        except Exception:
            root.geometry('1100x680')
        root.minsize(820, 500)
        self._set_icon()

        self.backend = SerialBackend()
        self.pending_cmd: str | None = None
        self.pending_ts: float = 0.0
        self.auto_scroll: bool = True
        self.log_writer: log_writer.LogWriter | None = None
        self.log_path: str | None = None
        self.at_window: AtCmdWindow | None = None
        self._at_embedded = None   # AtCmdPanel 嵌入式实例
        self._at_window_mode: str = 'detached'
        self._motor_monitor: MotorMonitorWindow | None = None
        self._bms_monitor: BmsMonitorWindow | None = None
        self._imu_monitor: ImuMonitorWindow | None = None
        self._log_viewer = None  # LogViewerWindow 单例
        self._motor_embedded: MotorMonitorPanel | None = None
        self._bms_embedded: BmsMonitorPanel | None = None
        self._imu_embedded: ImuMonitorPanel | None = None
        self._motor_frame: ttk.Frame | None = None
        self._bms_frame: ttk.Frame | None = None
        self._imu_frame: ttk.Frame | None = None
        self._motor_mode: str = 'detached'
        self._bms_mode: str = 'detached'
        self._imu_mode: str = 'detached'
        self._connected_label: str = '未连接'
        self._dot_state: str = 'idle'
        self._save_after_id: str | None = None
        # 筛选状态
        self._filter_var: tk.StringVar | None = None
        self._filter_enabled_var: tk.BooleanVar | None = None
        self._filter_compiled: re.Pattern | None = None
        self._ts_convert_var: tk.BooleanVar | None = None
        self._show_at_var: tk.BooleanVar | None = None
        self.theme_pref = self.cfg.get('theme_pref', 'auto')
        self._theme_name = themes.resolve(self.theme_pref)
        self._theme = themes.THEMES[self._theme_name]
        # 字体大小从配置恢复
        themes.set_font_size(self.cfg.get('font_size', 10))
        # 串口参数（默认值由 cfg 给）
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
        # 嵌入式 AT 面板
        self.at_panel: ttk.Frame | None = None
        self.at_panel_idx: str | None = None

        self._build_menubar()
        self._build_topbar()
        self._build_panel()
        self._build_bottombar()
        self._build_serial_bar()
        self._apply_theme()
        # 初始化筛选正则（从配置恢复）
        self._on_filter_change()
        # 嵌入式 AT 面板按上次状态加载
        self._apply_at_window_mode(self.cfg.get('at_window_mode', 'detached'))
        # 电机/BMS/IMU 监视按上次模式加载（只恢复 embedded，detached 等用户点开）
        self._motor_mode = self.cfg.get('motor_monitor_mode', 'detached')
        self._bms_mode = self.cfg.get('bms_monitor_mode', 'detached')
        self._imu_mode = self.cfg.get('imu_monitor_mode', 'detached')
        if self._motor_mode == 'embedded':
            self._apply_motor_mode('embedded')
        if self._bms_mode == 'embedded':
            self._apply_bms_mode('embedded')
        if self._imu_mode == 'embedded':
            self._apply_imu_mode('embedded')
        # 启动时恢复日志写入器
        self._restore_log_writer()
        self._drain()
        root.protocol('WM_DELETE_WINDOW', self._on_close)
        root.bind('<Configure>', self._on_root_configure, add='+')

    # ---- 图标 ----------------------------------------------------------
    def _set_icon(self):
        """加载 assets/icon_debug.png 作为串口助手窗口图标。"""
        try:
            icon_path = resource_path(
                os.path.join('assets', 'icon_debug.png'))
            if os.path.exists(icon_path):
                img = tk.PhotoImage(file=icon_path)
                self.root.iconphoto(True, img)
                self._icon_img = img   # 防 GC
        except Exception:
            pass

    # ---- 菜单栏 --------------------------------------------------------
    def _build_menubar(self):
        menubar = tk.Menu(self.root)
        self.root.configure(menu=menubar)
        self._menubar = menubar

        # 文件
        m_file = tk.Menu(menubar, tearoff=False)
        menubar.add_cascade(label='文件(F)', menu=m_file)
        m_file.add_command(label='打开日志文件…',
                           command=self._open_log_viewer,
                           accelerator='Ctrl+O')
        m_file.add_separator()
        m_file.add_command(label='导入 AT 命令 JSON…', command=self._menu_import_at)
        m_file.add_command(label='导出 AT 命令 JSON…', command=self._menu_export_at)
        m_file.add_separator()
        m_file.add_command(label='选择日志存储路径…', command=self._choose_log)
        m_file.add_separator()
        m_file.add_command(label='退出', command=self._on_close, accelerator='Ctrl+Q')

        # 视图
        m_view = tk.Menu(menubar, tearoff=False)
        menubar.add_cascade(label='视图(V)', menu=m_view)

        # 主题子菜单
        m_theme = tk.Menu(m_view, tearoff=False)
        m_view.add_cascade(label='主题', menu=m_theme)
        self._theme_var_menu = tk.StringVar(value=self._pref_label(self.theme_pref))
        for label, pref in [('跟随系统', 'auto'), ('浅色', 'light'), ('深色', 'dark')]:
            m_theme.add_radiobutton(label=label, value=pref,
                                     variable=self._theme_var_menu,
                                     command=self._on_theme_change_menu)
        m_view.add_separator()
        # AT 命令面板模式
        self._at_mode_var = tk.StringVar(
            value={'detached': '独立窗口', 'embedded': '嵌入侧栏',
                   'hidden': '隐藏'}.get(self.cfg.get('at_window_mode', 'detached'),
                                          '独立窗口'))
        m_view.add_radiobutton(label='AT 面板：独立窗口', value='独立窗口',
                               variable=self._at_mode_var,
                               command=lambda: self._set_at_mode('detached'))
        m_view.add_radiobutton(label='AT 面板：嵌入侧栏', value='嵌入侧栏',
                               variable=self._at_mode_var,
                               command=lambda: self._set_at_mode('embedded'))
        m_view.add_radiobutton(label='AT 面板：隐藏', value='隐藏',
                               variable=self._at_mode_var,
                               command=lambda: self._set_at_mode('hidden'))
        m_view.add_separator()
        # 电机监视模式
        _mode_label = {'detached': '独立窗口', 'embedded': '嵌入侧栏',
                       'hidden': '隐藏'}
        self._motor_mode_var = tk.StringVar(
            value=_mode_label.get(self.cfg.get('motor_monitor_mode', 'detached'),
                                   '独立窗口'))
        m_view.add_radiobutton(label='电机监视：独立窗口', value='独立窗口',
                               variable=self._motor_mode_var,
                               command=lambda: self._set_motor_mode('detached'))
        m_view.add_radiobutton(label='电机监视：嵌入侧栏', value='嵌入侧栏',
                               variable=self._motor_mode_var,
                               command=lambda: self._set_motor_mode('embedded'))
        m_view.add_radiobutton(label='电机监视：隐藏', value='隐藏',
                               variable=self._motor_mode_var,
                               command=lambda: self._set_motor_mode('hidden'))
        m_view.add_separator()
        # BMS 监视模式
        self._bms_mode_var = tk.StringVar(
            value=_mode_label.get(self.cfg.get('bms_monitor_mode', 'detached'),
                                   '独立窗口'))
        m_view.add_radiobutton(label='BMS 监视：独立窗口', value='独立窗口',
                               variable=self._bms_mode_var,
                               command=lambda: self._set_bms_mode('detached'))
        m_view.add_radiobutton(label='BMS 监视：嵌入侧栏', value='嵌入侧栏',
                               variable=self._bms_mode_var,
                               command=lambda: self._set_bms_mode('embedded'))
        m_view.add_radiobutton(label='BMS 监视：隐藏', value='隐藏',
                               variable=self._bms_mode_var,
                               command=lambda: self._set_bms_mode('hidden'))
        m_view.add_separator()
        # IMU 监视模式
        self._imu_mode_var = tk.StringVar(
            value=_mode_label.get(self.cfg.get('imu_monitor_mode', 'detached'),
                                   '独立窗口'))
        m_view.add_radiobutton(label='IMU 监视：独立窗口', value='独立窗口',
                               variable=self._imu_mode_var,
                               command=lambda: self._set_imu_mode('detached'))
        m_view.add_radiobutton(label='IMU 监视：嵌入侧栏', value='嵌入侧栏',
                               variable=self._imu_mode_var,
                               command=lambda: self._set_imu_mode('embedded'))
        m_view.add_radiobutton(label='IMU 监视：隐藏', value='隐藏',
                               variable=self._imu_mode_var,
                               command=lambda: self._set_imu_mode('hidden'))
        m_view.add_separator()
        # 时间戳转换
        self._ts_convert_var = tk.BooleanVar(
            value=self.cfg.get('ts_convert_enabled', False))
        m_view.add_checkbutton(label='设备时间戳→可读时间',
                               variable=self._ts_convert_var,
                               command=self._on_ts_convert_toggle)
        # 显示 AT 命令交互（发送/响应）
        self._show_at_var = tk.BooleanVar(
            value=self.cfg.get('show_at_interactive', True))
        m_view.add_checkbutton(label='显示 AT 命令交互',
                               variable=self._show_at_var,
                               command=lambda: self._schedule_save())
        # 自动滚动
        self._auto_scroll_menu_var = tk.BooleanVar(
            value=self.cfg.get('auto_scroll', True))
        m_view.add_checkbutton(label='自动滚动',
                               variable=self._auto_scroll_menu_var,
                               command=self._on_menu_autoscroll_toggle)

        # 工具
        m_tools = tk.Menu(menubar, tearoff=False)
        menubar.add_cascade(label='工具(T)', menu=m_tools)
        m_tools.add_command(label='连接/断开', command=self._toggle,
                            accelerator='Ctrl+D')
        m_tools.add_separator()
        # 字体大小子菜单
        m_font = tk.Menu(m_tools, tearoff=False)
        m_tools.add_cascade(label='字体大小', menu=m_font)
        self._font_size_var = tk.IntVar(
            value=self.cfg.get('font_size', 10))
        for size in (9, 10, 11, 12, 14, 16):
            m_font.add_radiobutton(label=str(size), value=size,
                                    variable=self._font_size_var,
                                    command=self._on_font_size_change)
        m_tools.add_separator()
        m_tools.add_command(label='清空日志窗口',
                            command=self._clear_log, accelerator='Ctrl+L')
        m_tools.add_separator()
        m_tools.add_command(label='AT 命令列表…',
                            command=self._open_at_window)
        m_tools.add_separator()
        m_tools.add_command(label='电机数据监视…',
                            command=self._open_motor_monitor)
        m_tools.add_command(label='BMS 数据监视…',
                            command=self._open_bms_monitor)
        m_tools.add_command(label='IMU 数据监视…',
                            command=self._open_imu_monitor)

        # 帮助
        m_help = tk.Menu(menubar, tearoff=False)
        menubar.add_cascade(label='帮助(H)', menu=m_help)
        m_help.add_command(label='Qt 试点曲线窗口…',
                           command=self._open_qt_monitor_pilot)
        m_help.add_command(label='Qt 主程序（Stage 4 试点）…',
                           command=self._open_qt_main_pilot)
        m_help.add_separator()
        m_help.add_command(label='关于', command=self._show_about)

        # 快捷键
        self.root.bind('<Control-q>', lambda _e: self._on_close())
        self.root.bind('<Control-d>', lambda _e: self._toggle())
        self.root.bind('<Control-l>', lambda _e: self._clear_log())
        self.root.bind('<Control-o>', lambda _e: self._open_log_viewer())

    # ---- 菜单回调 ------------------------------------------------------
    def _menu_import_at(self):
        if self.at_window is not None and self.at_window.winfo_exists():
            self.at_window.panel._import_json()
        elif self._at_embedded is not None:
            self._at_embedded._import_json()
        else:
            self._open_at_window()
            if self.at_window:
                self.at_window.panel._import_json()

    def _menu_export_at(self):
        if self.at_window is not None and self.at_window.winfo_exists():
            self.at_window.panel._export_json()
        elif self._at_embedded is not None:
            self._at_embedded._export_json()
        else:
            self._open_at_window()
            if self.at_window:
                self.at_window.panel._export_json()

    def _on_theme_change_menu(self):
        pref = self._pref_from_label(self._theme_var_menu.get())
        self.theme_var.set(self._pref_label(pref))
        self._on_theme_change()

    def _set_at_mode(self, mode: str):
        self._apply_at_window_mode(mode)
        self._schedule_save()
        if mode == 'detached':
            self._open_at_window()

    def _on_ts_convert_toggle(self):
        self._schedule_save()
        # 不立即重渲染历史行（成本高），只对新行生效

    def _on_font_size_change(self):
        size = self._font_size_var.get()
        themes.set_font_size(size)
        self.cfg['font_size'] = size
        self._apply_theme()
        self._schedule_save()

    def _on_menu_autoscroll_toggle(self):
        self.autoscroll_var.set(self._auto_scroll_menu_var.get())
        self._on_autoscroll_toggle()

    def _open_qt_monitor_pilot(self):
        """启动 Qt + pyqtgraph 试点窗口（独立进程）。

        与 tkinter 主应用隔离，避免双事件循环冲突。

        实现方式：用环境变量 SERIAL_DEBUG_QT_PILOT=1 启动 sys.executable
        子进程。PyInstaller 打包后 sys.executable 是 bootloader，不识别
        `-m <module>` 参数，所以不能用 `python -m` 方式启动。改用环境变量
        让主入口 main() 在子进程里走 Qt 试点路径。
        """
        import subprocess
        env = os.environ.copy()
        env['SERIAL_DEBUG_QT_PILOT'] = '1'
        try:
            subprocess.Popen(
                [sys.executable],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True)
        except Exception as e:
            try:
                messagebox.showerror(
                    'Qt 试点启动失败',
                    f'启动 Qt 试点窗口失败：{e}\n'
                    '请确认已安装 PySide6 + pyqtgraph（pip install PySide6 pyqtgraph）')
            except Exception:
                pass

    def _open_qt_main_pilot(self):
        """启动 Qt 主程序试点（独立进程）。

        环境变量 SERIAL_DEBUG_QT_MAIN=1 让 main() 走 qt_app.QtSerialDebugApp
        路径，与 tkinter 主事件循环隔离。
        """
        import subprocess
        env = os.environ.copy()
        env['SERIAL_DEBUG_QT_MAIN'] = '1'
        try:
            subprocess.Popen(
                [sys.executable],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True)
        except Exception as e:
            try:
                messagebox.showerror(
                    'Qt 主程序启动失败',
                    f'启动 Qt 主程序失败：{e}\n'
                    '请确认已安装 PySide6（pip install PySide6）')
            except Exception:
                pass

    def _show_about(self):
        messagebox.showinfo(
            '关于', '串口调试助手',
            detail='Linux 本地串口调试工具\n'
            '支持 CH343 vendor 驱动 / 实时日志存储 / AT 命令管理\n\n'
            '快捷键：Ctrl+Q 退出  Ctrl+D 连接  Ctrl+L 清空',
            parent=self.root)

    # ---- 顶栏 ----------------------------------------------------------
    def _build_topbar(self):
        bar = ttk.Frame(self.root, padding=(10, 6))
        bar.pack(fill='x')

        # 连接组：串口 + 波特率 + 连接按钮
        ttk.Label(bar, text='串口').pack(side='left', padx=(0, 4))
        self.port_var = tk.StringVar(value=self.cfg.get('port', ''))
        self.port_box = AutoCombobox(
            bar, values=SerialBackend.list_ports(), state='normal',
            width=22, font=themes._font(), textvariable=self.port_var)
        self.port_box.pack(side='left', padx=(0, 8))
        # FocusIn 时刷新串口列表
        self.port_box.bind_entry('<FocusIn>',
            lambda _e: self.port_box.configure(values=SerialBackend.list_ports()))
        self.port_var.trace_add('write', lambda *_: self._schedule_save())

        ttk.Label(bar, text='波特率').pack(side='left', padx=(0, 4))
        try:
            baud_init = int(self.cfg.get('baudrate', 115200))
        except Exception:
            baud_init = 115200
        self.baud_var = tk.StringVar(value=str(baud_init))
        self.baud_box = AutoCombobox(
            bar, values=['9600', '19200', '38400', '57600', '115200', '230400',
                        '460800', '921600', '1000000', '2000000', '3000000'],
            state='normal', width=10, font=themes._font(),
            textvariable=self.baud_var)
        self.baud_box.set(str(baud_init))
        self.baud_box.pack(side='left', padx=(0, 8))
        self.baud_var.trace_add('write', lambda *_: self._schedule_save())

        self.connect_btn = ttk.Button(
            bar, text='连接', style='Primary.TButton', command=self._toggle)
        self.connect_btn.pack(side='left', padx=(0, 10))

        ttk.Separator(bar, orient='vertical').pack(
            side='left', fill='y', padx=6)

        # 工具组：日志存储 + AT 列表
        ttk.Button(bar, text='日志存储', command=self._choose_log
                   ).pack(side='left', padx=2)
        self.log_lbl = tk.Label(bar, text='未启用', fg='#888', anchor='w',
                                font=themes._font())
        self.log_lbl.pack(side='left', padx=(8, 8))

        ttk.Button(bar, text='AT 命令列表',
                   command=self._open_at_window).pack(side='left', padx=2)

        ttk.Separator(bar, orient='vertical').pack(
            side='left', fill='y', padx=6)

        # 主题选择
        ttk.Label(bar, text='主题').pack(side='left', padx=(0, 4))
        self.theme_var = tk.StringVar(value=self._pref_label(self.theme_pref))
        self.theme_box = ttk.Combobox(
            bar, textvariable=self.theme_var, width=8, state='readonly',
            values=['跟随系统', '浅色', '深色'])
        self.theme_box.pack(side='left')
        self.theme_box.bind('<<ComboboxSelected>>',
                            lambda _e: self._on_theme_change())

        # 右侧状态点 + 状态文本
        right = ttk.Frame(bar)
        right.pack(side='right')
        self.status_dot = tk.Canvas(right, width=12, height=12,
                                    highlightthickness=0, bg=self._theme['bg'])
        self.status_dot.pack(side='left', padx=(0, 6))
        self.status_var = tk.StringVar(value='未连接')
        self.status_lbl = tk.Label(
            right, textvariable=self.status_var, fg='#888', anchor='w',
            font=themes._font())
        self.status_lbl.pack(side='left')
        self._draw_status_dot('idle')

    # ---- 中部单窗口 ----------------------------------------------------
    def _build_panel(self):
        outer = ttk.Frame(self.root)
        outer.pack(fill='both', expand=True, padx=10, pady=(4, 4))

        # 筛选条：放在日志窗口上方
        filter_bar = ttk.Frame(outer, padding=(2, 4, 2, 4))
        filter_bar.pack(fill='x')
        ttk.Label(filter_bar, text='筛选').pack(side='left', padx=(0, 4))
        self._filter_var = tk.StringVar(
            value=self.cfg.get('filter_pattern', ''))
        filter_entry = ttk.Entry(filter_bar, textvariable=self._filter_var,
                                 width=40)
        filter_entry.pack(side='left', fill='x', expand=True, padx=(0, 6))
        self._filter_entry = filter_entry
        self._filter_var.trace_add('write', lambda *_: self._on_filter_change())
        self._filter_enabled_var = tk.BooleanVar(
            value=self.cfg.get('filter_enabled', False))
        # FillCheck 取代 ttk.Checkbutton（填充色块代替对勾）
        self._filter_check = FillCheck(filter_bar, text='启用（正则）',
                                       variable=self._filter_enabled_var,
                                       command=self._on_filter_change)
        self._filter_check.pack(side='left', padx=(0, 6))
        ttk.Label(filter_bar, text='（不匹配行隐藏）',
                  foreground=self._theme['muted']
                  ).pack(side='left', padx=(0, 6))

        # 日志窗口：始终放进一个 PanedWindow，AT 面板按需 add/forget 第二 pane
        self._main_pane = ttk.PanedWindow(outer, orient='horizontal')
        self._main_pane.pack(fill='both', expand=True)

        frame = ttk.Frame(self._main_pane)
        self._main_pane.add(frame, weight=3)
        self.log_txt = tk.Text(
            frame, wrap='none', undo=False,
            bg=self._theme['panel_bg'], fg=self._theme['panel_fg'],
            insertbackground=self._theme['cursor'],
            selectbackground=self._theme['select_bg'],
            font=themes._mono(),
            padx=8, pady=6, spacing2=2, spacing3=2,
            borderwidth=0, highlightthickness=0)
        self.log_txt.pack(side='left', fill='both', expand=True)
        self.log_txt.configure(state='disabled')
        self.log_scroll = ttk.Scrollbar(frame, command=self.log_txt.yview)
        self.log_scroll.pack(side='right', fill='y')
        self.log_txt.configure(yscrollcommand=self.log_scroll.set)
        self.log_txt.bind('<Button-1>', lambda _e: self._check_scroll())
        # 记住日志 frame 的父，方便 _apply_at_window_mode 切换
        self._log_frame_parent = frame

    def _draw_status_dot(self, state: str):
        """绘制顶栏右侧状态点。
        state: idle(灰) / ok(绿) / wait(黄) / err(红)
        """
        colors = {
            'idle': self._theme['status_idle'],
            'ok': self._theme['status_ok'],
            'wait': self._theme['status_wait'],
            'err': self._theme['status_err'],
        }
        fill = colors.get(state, colors['idle'])
        bg = self._theme['bg']
        self.status_dot.configure(bg=bg)
        self.status_dot.delete('all')
        self.status_dot.create_oval(2, 2, 10, 10, fill=fill, outline='')

    def _apply_theme(self):
        """应用当前主题到所有 tk 控件 + Text tag。ttk 控件由 themes.apply_theme 统一配置。"""
        t = themes.apply_theme(self.root, self._theme_name)
        self._theme = t
        # Text 控件
        self.log_txt.configure(bg=t['panel_bg'], fg=t['panel_fg'],
                               insertbackground=t['cursor'],
                               selectbackground=t['select_bg'],
                               font=themes._mono())
        # tk.Label（ttk.Label 由 Style 接管，tk.Label 需手动）
        self.status_lbl.configure(bg=t['bg'], fg=t['muted'],
                                   font=themes._font())
        self.log_lbl.configure(bg=t['bg'], fg=t['muted'],
                                font=themes._font())
        # Text tag 配色
        self.log_txt.tag_config('log', foreground=t['log'])
        self.log_txt.tag_config('ts', foreground=t['ts'])
        self.log_txt.tag_config('send', foreground=t['send'])
        self.log_txt.tag_config('resp', foreground=t['resp'])
        self.log_txt.tag_config('note', foreground=t['note'])
        # 筛选条 Entry 字体
        try:
            self._filter_entry.configure(font=themes._font())
        except Exception:
            pass
        # 菜单栏深色（VSCode 风格）
        try:
            themes.apply_menu_theme(self._menubar, t)
        except Exception:
            pass
        # FillCheck 跟随主题
        for attr in ('_filter_check', '_crlf_check', '_autoscroll_check'):
            w = getattr(self, attr, None)
            if w is not None:
                try:
                    w.apply_theme(t)
                except Exception:
                    pass
        # AutoCombobox 跟随主题（弹出下拉配色）
        for attr in ('port_box', 'baud_box', 'bytesize_box',
                     'parity_box', 'stopbits_box', 'flow_box'):
            w = getattr(self, attr, None)
            if w is not None:
                try:
                    w.apply_theme(t)
                except Exception:
                    pass
        # 嵌入式 AT 面板 Canvas 跟随
        if self._at_embedded is not None:
            try:
                self._at_embedded.apply_theme(t)
            except Exception:
                pass
        # 日志查看窗口跟随主题
        if (self._log_viewer is not None
                and self._log_viewer.winfo_exists()):
            try:
                self._log_viewer.update_theme()
            except Exception:
                pass
        # 状态点重画
        self._draw_status_dot(self._dot_state)

    @staticmethod
    def _pref_label(pref: str) -> str:
        return {'auto': '跟随系统', 'light': '浅色', 'dark': '深色'}.get(pref, '跟随系统')

    @staticmethod
    def _pref_from_label(label: str) -> str:
        return {'跟随系统': 'auto', '浅色': 'light', '深色': 'dark'}.get(label, 'auto')

    def _on_theme_change(self):
        self.theme_pref = self._pref_from_label(self.theme_var.get())
        self._theme_var_menu.set(self._pref_label(self.theme_pref))
        self.cfg['theme_pref'] = self.theme_pref
        config.save(self.cfg)
        self._theme_name = themes.resolve(self.theme_pref)
        self._apply_theme()
        # AT 命令窗口（如果开着）也跟随主题
        if self.at_window is not None and self.at_window.winfo_exists():
            try:
                self.at_window.update_theme()
            except Exception:
                pass
        if self._at_embedded is not None:
            try:
                self._at_embedded.apply_theme(self._theme)
            except Exception:
                pass
        # 监视窗口跟随主题
        if self._motor_monitor is not None and self._motor_monitor.winfo_exists():
            try:
                self._motor_monitor.update_theme()
            except Exception:
                pass
        if self._bms_monitor is not None and self._bms_monitor.winfo_exists():
            try:
                self._bms_monitor.update_theme()
            except Exception:
                pass
        if self._imu_monitor is not None and self._imu_monitor.winfo_exists():
            try:
                self._imu_monitor.update_theme()
            except Exception:
                pass
        if self._motor_embedded is not None:
            try:
                self._motor_embedded.apply_theme(self._theme)
            except Exception:
                pass
        if self._bms_embedded is not None:
            try:
                self._bms_embedded.apply_theme(self._theme)
            except Exception:
                pass
        if self._imu_embedded is not None:
            try:
                self._imu_embedded.apply_theme(self._theme)
            except Exception:
                pass

    # ---- 底栏 ----------------------------------------------------------
    def _build_bottombar(self):
        bar = ttk.Frame(self.root, padding=(10, 6))
        bar.pack(fill='x')

        # 命令输入组
        ttk.Label(bar, text='AT 命令').pack(side='left', padx=(0, 6))
        self.cmd_var = tk.StringVar()
        self.cmd_entry = ttk.Entry(bar, textvariable=self.cmd_var)
        self.cmd_entry.pack(side='left', fill='x', expand=True, padx=(0, 8))
        self.cmd_entry.bind('<Return>', lambda _e: self._send_entry())

        ttk.Button(bar, text='发送', style='Primary.TButton',
                   command=self._send_entry).pack(side='left', padx=(0, 8))
        ttk.Button(bar, text='清空',
                   command=self._clear_cmd_entry).pack(side='left', padx=(0, 8))

        # 快捷命令组
        ttk.Button(bar, text='日志关',
                   command=lambda: self._send_cmd('AT+ELOG=0%')
                   ).pack(side='left', padx=2)
        ttk.Button(bar, text='日志开',
                   command=lambda: self._send_cmd('AT+ELOG=1%')
                   ).pack(side='left', padx=2)

        ttk.Separator(bar, orient='vertical').pack(
            side='left', fill='y', padx=8)

        # 选项组（FillCheck 填充色块复选框）
        self.crlf_var = tk.BooleanVar(value=self.cfg.get('append_crlf', False))
        self._crlf_check = FillCheck(bar, text='追加 \\r\\n',
                                      variable=self.crlf_var,
                                      command=lambda: self._schedule_save())
        self._crlf_check.pack(side='left', padx=4)
        self.autoscroll_var = tk.BooleanVar(
            value=self.cfg.get('auto_scroll', True))
        self._autoscroll_check = FillCheck(
            bar, text='自动滚动',
            variable=self.autoscroll_var,
            command=self._on_autoscroll_toggle)
        self._autoscroll_check.pack(side='left', padx=4)
        self._show_at_check = FillCheck(
            bar, text='显示 AT',
            variable=self._show_at_var,
            command=lambda: self._schedule_save())
        self._show_at_check.pack(side='left', padx=4)
        ttk.Button(bar, text='清空',
                   command=self._clear_log).pack(side='left', padx=4)

    def _build_serial_bar(self):
        """最下方串口参数栏：数据位 / 校验 / 停止位 / 流控。

        用 AutoCombobox：下拉方向自适应，最大化窗口时下方空间不足会向上弹出。
        """
        bar = ttk.Frame(self.root, padding=(10, 4))
        bar.pack(fill='x')

        ttk.Label(bar, text='数据位').pack(side='left', padx=(0, 4))
        self.bytesize_box = AutoCombobox(
            bar, values=['5', '6', '7', '8'], state='normal',
            width=4, font=themes._font())
        self.bytesize_box.set(str(self._bytesize))
        self.bytesize_box.pack(side='left', padx=(0, 12))
        self.bytesize_box.trace_add('write', lambda *_: self._on_serial_param_change())

        ttk.Label(bar, text='校验位').pack(side='left', padx=(0, 4))
        parity_map = {'无': 'none', '偶校验': 'even', '奇校验': 'odd'}
        self._parity_label = next((k for k, v in parity_map.items()
                                   if v == self._parity), '无')
        self.parity_box = AutoCombobox(
            bar, values=list(parity_map.keys()), state='normal',
            width=8, font=themes._font())
        self.parity_box.set(self._parity_label)
        self.parity_box.pack(side='left', padx=(0, 12))
        self._parity_map = parity_map
        self.parity_box.trace_add('write', lambda *_: self._on_serial_param_change())

        ttk.Label(bar, text='停止位').pack(side='left', padx=(0, 4))
        self.stopbits_box = AutoCombobox(
            bar, values=['1', '2'], state='normal',
            width=4, font=themes._font())
        self.stopbits_box.set(str(self._stopbits))
        self.stopbits_box.pack(side='left', padx=(0, 12))
        self.stopbits_box.trace_add('write', lambda *_: self._on_serial_param_change())

        ttk.Label(bar, text='流控').pack(side='left', padx=(0, 4))
        fc_map = {'无': 'none', 'RTS/CTS': 'rtscts'}
        self._fc_label = next((k for k, v in fc_map.items()
                               if v == self._flow_control), '无')
        self.flow_box = AutoCombobox(
            bar, values=list(fc_map.keys()), state='normal',
            width=10, font=themes._font())
        self.flow_box.set(self._fc_label)
        self.flow_box.pack(side='left', padx=(0, 12))
        self._fc_map = fc_map
        self.flow_box.trace_add('write', lambda *_: self._on_serial_param_change())

        ttk.Separator(bar, orient='vertical').pack(
            side='left', fill='y', padx=8)

        # 统计信息（占位，运行时更新）
        self._stats_var = tk.StringVar(value='RX: 0B  TX: 0B')
        ttk.Label(bar, textvariable=self._stats_var,
                  foreground=self._theme['muted']).pack(side='right', padx=4)

    def _on_serial_param_change(self):
        """串口参数变化：更新内部状态，持久化。已连接则提示重连生效。"""
        try:
            self._bytesize = int(self.bytesize_box.get())
        except Exception:
            pass
        self._parity = self._parity_map.get(self.parity_box.get(), 'none')
        try:
            self._stopbits = int(self.stopbits_box.get())
        except Exception:
            pass
        self._flow_control = self._fc_map.get(self.flow_box.get(), 'none')
        self.cfg['bytesize'] = self._bytesize
        self.cfg['parity'] = self._parity
        self.cfg['stopbits'] = self._stopbits
        self.cfg['flow_control'] = self._flow_control
        self._schedule_save()

    def _bytesize_ser(self):
        import serial
        bs_map = {5: serial.FIVEBITS, 6: serial.SIXBITS,
                  7: serial.SEVENBITS, 8: serial.EIGHTBITS}
        return bs_map.get(self._bytesize, serial.EIGHTBITS)

    def _parity_ser(self):
        import serial
        p_map = {'none': serial.PARITY_NONE, 'even': serial.PARITY_EVEN,
                 'odd': serial.PARITY_ODD}
        return p_map.get(self._parity, serial.PARITY_NONE)

    def _stopbits_ser(self):
        import serial
        sb_map = {1: serial.STOPBITS_ONE, 2: serial.STOPBITS_TWO}
        return sb_map.get(self._stopbits, serial.STOPBITS_ONE)

    # ---- 行为 ----------------------------------------------------------
    def _on_autoscroll_toggle(self):
        self.auto_scroll = self.autoscroll_var.get()
        self._schedule_save()

    # ---- 配置保存 ------------------------------------------------------
    def _schedule_save(self):
        """去抖保存：合并多次状态变化，SAVE_DEBOUNCE_MS 后写盘。"""
        if self._save_after_id is not None:
            try:
                self.root.after_cancel(self._save_after_id)
            except Exception:
                pass
        self._save_after_id = self.root.after(SAVE_DEBOUNCE_MS, self._save_cfg)

    def _save_cfg(self):
        """把当前状态写入 cfg 并落盘。"""
        self._save_after_id = None
        try:
            self.cfg['port'] = self.port_var.get()
            try:
                self.cfg['baudrate'] = int(self.baud_var.get())
            except Exception:
                pass
            self.cfg['append_crlf'] = bool(self.crlf_var.get())
            self.cfg['auto_scroll'] = bool(self.autoscroll_var.get())
            self.cfg['log_path'] = self.log_path or ''
            self.cfg['theme_pref'] = self.theme_pref
            self.cfg['filter_pattern'] = self._filter_var.get() if self._filter_var else ''
            self.cfg['filter_enabled'] = bool(self._filter_enabled_var.get()) if self._filter_enabled_var else False
            self.cfg['ts_convert_enabled'] = bool(self._ts_convert_var.get()) if self._ts_convert_var else False
            self.cfg['show_at_interactive'] = bool(self._show_at_var.get()) if self._show_at_var else True
            try:
                self.cfg['geometry'] = self.root.geometry()
            except Exception:
                pass
            # AT 命令列表 + 嵌入模式 + 独立窗口大小
            self.cfg['at_window_mode'] = self._at_window_mode
            if self.at_window is not None and self.at_window.winfo_exists():
                try:
                    self.cfg['at_geometry'] = self.at_window.geometry()
                    if hasattr(self.at_window, 'commands'):
                        self.cfg['at_commands'] = self.at_window.commands
                except Exception:
                    pass
            # 监视窗口几何 + 模式
            self.cfg['motor_monitor_mode'] = self._motor_mode
            self.cfg['bms_monitor_mode'] = self._bms_mode
            self.cfg['imu_monitor_mode'] = self._imu_mode
            if self._motor_monitor is not None and self._motor_monitor.winfo_exists():
                try:
                    self.cfg['motor_monitor_geometry'] = self._motor_monitor.geometry()
                    self.cfg['motor_vars'] = self._motor_monitor.variables
                    self.cfg['motor_plots'] = self._motor_monitor.plots
                except Exception:
                    pass
            elif self._motor_embedded is not None:
                try:
                    self.cfg['motor_vars'] = self._motor_embedded.variables
                    self.cfg['motor_plots'] = self._motor_embedded.plots
                except Exception:
                    pass
            if self._bms_monitor is not None and self._bms_monitor.winfo_exists():
                try:
                    self.cfg['bms_monitor_geometry'] = self._bms_monitor.geometry()
                    self.cfg['bms_vars'] = self._bms_monitor.variables
                    self.cfg['bms_plots'] = self._bms_monitor.plots
                except Exception:
                    pass
            elif self._bms_embedded is not None:
                try:
                    self.cfg['bms_vars'] = self._bms_embedded.variables
                    self.cfg['bms_plots'] = self._bms_embedded.plots
                except Exception:
                    pass
            if self._imu_monitor is not None and self._imu_monitor.winfo_exists():
                try:
                    self.cfg['imu_monitor_geometry'] = self._imu_monitor.geometry()
                    self.cfg['imu_vars'] = self._imu_monitor.variables
                    self.cfg['imu_plots'] = self._imu_monitor.plots
                except Exception:
                    pass
            elif self._imu_embedded is not None:
                try:
                    self.cfg['imu_vars'] = self._imu_embedded.variables
                    self.cfg['imu_plots'] = self._imu_embedded.plots
                except Exception:
                    pass
            config.save(self.cfg)
        except Exception:
            pass

    def _on_root_configure(self, _event):
        """窗口大小变化时去抖保存。"""
        self._schedule_save()

    def _restore_log_writer(self):
        """启动时按上次日志路径恢复写入器。"""
        path = self.cfg.get('log_path', '')
        if not path:
            return
        try:
            w = log_writer.LogWriter(path)
            if w.start():
                self.log_writer = w
                self.log_path = path
                self.log_lbl.config(text=f'存储中: {path}',
                                    fg=self._theme['accent'])
        except Exception:
            pass


    def _check_scroll(self):
        """用户点滚动条/拖选时暂停跟随。"""
        try:
            _top, bottom = self.log_txt.yview()
            if bottom < 0.98:
                self.auto_scroll = False
        except Exception:
            pass

    def _is_at_bottom(self) -> bool:
        try:
            _top, bottom = self.log_txt.yview()
            return bottom >= 0.98
        except Exception:
            return True

    def _append(self, text, tag):
        self.log_txt.configure(state='normal')
        self.log_txt.insert('end', text, tag)
        self._trim()
        self.log_txt.configure(state='disabled')
        if self.auto_scroll:
            self.log_txt.see('end')
        # 日志写入：非阻塞入队，不拖慢显示
        if self.log_writer is not None:
            self.log_writer.put(text)

    def _trim(self):
        line_cnt = int(self.log_txt.index('end-1c').split('.')[0])
        if line_cnt > MAX_LINES:
            drop = line_cnt - MAX_LINES
            self.log_txt.delete('1.0', f'{drop + 1}.0')

    @staticmethod
    def _now() -> str:
        t = time.localtime()
        return time.strftime('%H:%M:%S', t) + f'.{int((time.time() % 1)*1000):03d}'

    def _ts_prefix_if_needed(self, line: str) -> str:
        """时间戳转换开关打开、且行未以 [ 开头时，返回本地可读时间前缀。
        设备周期日志行首有 13 位毫秒时间戳，已被 _convert_ts 转成 [HH:MM:SS.mmm]
        前缀（保留原 ts）；响应/发送行没有设备 ts，用本地当前时间补前缀。
        """
        if (self._ts_convert_var and self._ts_convert_var.get()
                and not line.startswith('[')):
            return f'[{self._now()}] '
        return ''

    # ---- 日志存储 -------------------------------------------------------
    def _choose_log(self):
        path = filedialog.asksaveasfilename(
            title='选择日志存储路径',
            defaultextension='.txt',
            filetypes=[('文本文件', '*.txt'), ('所有文件', '*.*')],
            parent=self.root)
        if not path:
            return
        # 若已有写入器，先停旧
        if self.log_writer is not None:
            self.log_writer.stop()
            self.log_writer = None
        w = log_writer.LogWriter(path)
        if not w.start():
            messagebox.showerror('错误', '无法创建日志文件',
                                 detail=str(w.error), parent=self.root)
            return
        self.log_writer = w
        self.log_path = path
        self.log_lbl.config(text=f'存储中: {path}', fg=self._theme['accent'])
        self._schedule_save()

    # ---- AT 命令列表窗口 -----------------------------------------------
    def _open_at_window(self):
        """打开独立 AT 命令窗口（保留旧 API，菜单栏 / 顶栏按钮调用）。"""
        if self.at_window is not None and self.at_window.winfo_exists():
            self.at_window.lift()
            self.at_window.focus_force()
            return
        self.at_window = AtCmdWindow(self.root, self._send_cmd,
                                     initial_cmds=self.cfg.get('at_commands', []),
                                     on_change=self._on_at_commands_changed)
        try:
            self.at_window.geometry(self.cfg.get('at_geometry', '720x520'))
        except Exception:
            pass
        self.at_window.update_theme()

    def _on_at_commands_changed(self, cmds: list):
        """AT 命令窗口内容变化回调，持久化。"""
        self.cfg['at_commands'] = cmds
        self._schedule_save()

    # ---- 监视窗口（电机 / BMS） ----------------------------------------
    # 独立/嵌入/隐藏三态：detached → Toplevel，embedded → PanedWindow 中的 Frame，
    # hidden → 不创建。切换模式时变量列表迁移。
    def _set_motor_mode(self, mode: str):
        self._apply_motor_mode(mode)
        self._schedule_save()
        if mode == 'detached':
            self._open_motor_monitor()

    def _set_bms_mode(self, mode: str):
        self._apply_bms_mode(mode)
        self._schedule_save()
        if mode == 'detached':
            self._open_bms_monitor()

    def _set_imu_mode(self, mode: str):
        self._apply_imu_mode(mode)
        self._schedule_save()
        if mode == 'detached':
            self._open_imu_monitor()

    def _apply_motor_mode(self, mode: str):
        """切换电机监视呈现模式：detached / embedded / hidden。"""
        self._motor_mode = mode
        self._motor_mode_var.set(
            {'detached': '独立窗口', 'embedded': '嵌入侧栏',
             'hidden': '隐藏'}.get(mode, '独立窗口'))
        # 非 detached 模式下关闭独立窗口
        if mode != 'detached' and self._motor_monitor is not None:
            try:
                self._motor_monitor.destroy()
            except Exception:
                pass
            self._motor_monitor = None
        # 销毁旧嵌入式面板
        if self._motor_embedded is not None:
            try:
                self._main_pane.forget(self._motor_frame)
                self._motor_frame.destroy()
            except Exception:
                pass
            self._motor_embedded = None
            self._motor_frame = None
        if mode == 'embedded':
            self._build_motor_embedded()
        # hidden: 不创建

    def _apply_bms_mode(self, mode: str):
        """切换 BMS 监视呈现模式。"""
        self._bms_mode = mode
        self._bms_mode_var.set(
            {'detached': '独立窗口', 'embedded': '嵌入侧栏',
             'hidden': '隐藏'}.get(mode, '独立窗口'))
        if mode != 'detached' and self._bms_monitor is not None:
            try:
                self._bms_monitor.destroy()
            except Exception:
                pass
            self._bms_monitor = None
        if self._bms_embedded is not None:
            try:
                self._main_pane.forget(self._bms_frame)
                self._bms_frame.destroy()
            except Exception:
                pass
            self._bms_embedded = None
            self._bms_frame = None
        if mode == 'embedded':
            self._build_bms_embedded()

    def _apply_imu_mode(self, mode: str):
        """切换 IMU 监视呈现模式。"""
        self._imu_mode = mode
        self._imu_mode_var.set(
            {'detached': '独立窗口', 'embedded': '嵌入侧栏',
             'hidden': '隐藏'}.get(mode, '独立窗口'))
        if mode != 'detached' and self._imu_monitor is not None:
            try:
                self._imu_monitor.destroy()
            except Exception:
                pass
            self._imu_monitor = None
        if self._imu_embedded is not None:
            try:
                self._main_pane.forget(self._imu_frame)
                self._imu_frame.destroy()
            except Exception:
                pass
            self._imu_embedded = None
            self._imu_frame = None
        if mode == 'embedded':
            self._build_imu_embedded()

    def _build_motor_embedded(self):
        """在 PanedWindow 加入电机监视嵌入式面板。"""
        if self._motor_embedded is not None:
            return
        self._motor_frame = ttk.Frame(self._main_pane, padding=2)
        self._main_pane.add(self._motor_frame, weight=1)
        self._motor_embedded = MotorMonitorPanel(
            self._motor_frame,
            variables=self.cfg.get('motor_vars', []),
            plots=self.cfg.get('motor_plots', []),
            on_change=lambda vs, plots=None: self._on_monitor_vars_changed(
                'motor', vs, plots))
        self._motor_embedded.pack(fill='both', expand=True)
        self._motor_embedded.apply_theme(self._theme)

    def _build_bms_embedded(self):
        """在 PanedWindow 加入 BMS 监视嵌入式面板。"""
        if self._bms_embedded is not None:
            return
        self._bms_frame = ttk.Frame(self._main_pane, padding=2)
        self._main_pane.add(self._bms_frame, weight=1)
        self._bms_embedded = BmsMonitorPanel(
            self._bms_frame,
            variables=self.cfg.get('bms_vars', []),
            plots=self.cfg.get('bms_plots', []),
            on_change=lambda vs, plots=None: self._on_monitor_vars_changed(
                'bms', vs, plots))
        self._bms_embedded.pack(fill='both', expand=True)
        self._bms_embedded.apply_theme(self._theme)

    def _build_imu_embedded(self):
        """在 PanedWindow 加入 IMU 监视嵌入式面板。"""
        if self._imu_embedded is not None:
            return
        self._imu_frame = ttk.Frame(self._main_pane, padding=2)
        self._main_pane.add(self._imu_frame, weight=1)
        self._imu_embedded = ImuMonitorPanel(
            self._imu_frame,
            variables=self.cfg.get('imu_vars', []),
            plots=self.cfg.get('imu_plots', []),
            on_change=lambda vs, plots=None: self._on_monitor_vars_changed(
                'imu', vs, plots))
        self._imu_embedded.pack(fill='both', expand=True)
        self._imu_embedded.apply_theme(self._theme)

    def _open_motor_monitor(self):
        """打开电机监视独立窗口。当前若为嵌入/隐藏模式，先切到 detached。"""
        if self._motor_mode != 'detached':
            self._apply_motor_mode('detached')
            self._schedule_save()
        if self._motor_monitor is not None and self._motor_monitor.winfo_exists():
            self._motor_monitor.lift()
            self._motor_monitor.focus_force()
            return
        self._motor_monitor = MotorMonitorWindow(
            self.root,
            variables=self.cfg.get('motor_vars', []),
            plots=self.cfg.get('motor_plots', []),
            on_change=lambda vs, plots=None: self._on_monitor_vars_changed(
                'motor', vs, plots))
        try:
            saved_geo = self.cfg.get('motor_monitor_geometry', '')
            if saved_geo:
                self._motor_monitor.geometry(saved_geo)
        except Exception:
            pass
        self._motor_monitor.update_theme()

    def _open_bms_monitor(self):
        """打开 BMS 监视独立窗口。当前若为嵌入/隐藏模式，先切到 detached。"""
        if self._bms_mode != 'detached':
            self._apply_bms_mode('detached')
            self._schedule_save()
        if self._bms_monitor is not None and self._bms_monitor.winfo_exists():
            self._bms_monitor.lift()
            self._bms_monitor.focus_force()
            return
        self._bms_monitor = BmsMonitorWindow(
            self.root,
            variables=self.cfg.get('bms_vars', []),
            plots=self.cfg.get('bms_plots', []),
            on_change=lambda vs, plots=None: self._on_monitor_vars_changed(
                'bms', vs, plots))
        try:
            saved_geo = self.cfg.get('bms_monitor_geometry', '')
            if saved_geo:
                self._bms_monitor.geometry(saved_geo)
        except Exception:
            pass
        self._bms_monitor.update_theme()

    def _open_imu_monitor(self):
        """打开 IMU 监视独立窗口。当前若为嵌入/隐藏模式，先切到 detached。"""
        if self._imu_mode != 'detached':
            self._apply_imu_mode('detached')
            self._schedule_save()
        if self._imu_monitor is not None and self._imu_monitor.winfo_exists():
            self._imu_monitor.lift()
            self._imu_monitor.focus_force()
            return
        self._imu_monitor = ImuMonitorWindow(
            self.root,
            variables=self.cfg.get('imu_vars', []),
            plots=self.cfg.get('imu_plots', []),
            on_change=lambda vs, plots=None: self._on_monitor_vars_changed(
                'imu', vs, plots))
        try:
            saved_geo = self.cfg.get('imu_monitor_geometry', '')
            if saved_geo:
                self._imu_monitor.geometry(saved_geo)
        except Exception:
            pass
        self._imu_monitor.update_theme()

    def _open_log_viewer(self):
        """打开日志文件查看窗口（不实时刷新，只读查看）。"""
        path = filedialog.askopenfilename(
            title='打开日志文件', parent=self.root,
            filetypes=[('文本文件', '*.txt'), ('日志', '*.log'),
                       ('所有文件', '*.*')])
        if not path:
            return
        # 已有窗口则复用（单例）
        if (hasattr(self, '_log_viewer')
                and self._log_viewer is not None
                and self._log_viewer.winfo_exists()):
            self._log_viewer.destroy()
        try:
            self._log_viewer = LogViewerWindow(
                self.root, path, theme=self._theme)
            self._log_viewer.update_theme()
        except Exception as e:
            messagebox.showerror('打开失败', f'{e}', parent=self.root)

    def _on_monitor_vars_changed(self, kind: str, variables: list,
                                   plots: list | None = None):
        """监视窗口变量/图表变化回调，持久化。kind: motor/bms/imu。"""
        if kind == 'motor':
            self.cfg['motor_vars'] = variables
            if plots is not None:
                self.cfg['motor_plots'] = plots
        elif kind == 'bms':
            self.cfg['bms_vars'] = variables
            if plots is not None:
                self.cfg['bms_plots'] = plots
        else:  # imu
            self.cfg['imu_vars'] = variables
            if plots is not None:
                self.cfg['imu_plots'] = plots
        self._schedule_save()

    def _feed_monitors(self, line: str):
        """把日志行喂给所有打开的监视窗口（独立 + 嵌入）提取数据。"""
        if self._motor_monitor is not None and self._motor_monitor.winfo_exists():
            try:
                self._motor_monitor.feed(line)
            except Exception:
                pass
        if self._bms_monitor is not None and self._bms_monitor.winfo_exists():
            try:
                self._bms_monitor.feed(line)
            except Exception:
                pass
        if self._imu_monitor is not None and self._imu_monitor.winfo_exists():
            try:
                self._imu_monitor.feed(line)
            except Exception:
                pass
        if self._motor_embedded is not None:
            try:
                self._motor_embedded.feed(line)
            except Exception:
                pass
        if self._bms_embedded is not None:
            try:
                self._bms_embedded.feed(line)
            except Exception:
                pass
        if self._imu_embedded is not None:
            try:
                self._imu_embedded.feed(line)
            except Exception:
                pass

    def _feed_monitors_batch(self, lines: list[str]):
        """批量喂多行给监视窗口（_drain 末尾 after_idle 触发）。"""
        for ln in lines:
            self._feed_monitors(ln)

    def _apply_at_window_mode(self, mode: str):
        """根据模式呈现 AT 命令面板：embedded / detached / hidden。
        日志始终在 PanedWindow 第 0 pane；AT 面板按 add/forget 切换。
        """
        self._at_window_mode = mode
        # 关闭旧独立窗口
        if mode != 'detached' and self.at_window is not None:
            try:
                self.at_window.destroy()
            except Exception:
                pass
            self.at_window = None
        # 销毁旧嵌入面板
        if self._at_embedded is not None:
            try:
                self._main_pane.forget(self.at_panel)
                self.at_panel.destroy()
            except Exception:
                pass
            self._at_embedded = None
            self.at_panel = None
        if mode == 'embedded':
            self._build_at_panel()
        elif mode == 'detached':
            # 不主动打开独立窗口，等用户从菜单/顶栏按钮触发
            pass
        # hidden: 仅日志，不创建 AT 面板

    def _build_at_panel(self):
        """在 PanedWindow 右侧加入 AT 命令面板。"""
        if self._at_embedded is not None:
            return
        self.at_panel = ttk.Frame(self._main_pane, padding=6)
        self._main_pane.add(self.at_panel, weight=1)
        from serial_debug.ui.at_cmd_window import AtCmdPanel
        self._at_embedded = AtCmdPanel(
            self.at_panel, self._send_cmd,
            self.cfg.get('at_commands', []),
            self._on_at_commands_changed)
        self._at_embedded.pack(fill='both', expand=True)
        self._at_embedded.apply_theme(self._theme)
        # 让左侧日志 pane 占大头
        try:
            self._main_pane.sashpos(0, 700)
        except Exception:
            pass

    # ---- 连接 ----------------------------------------------------------
    def _toggle(self):
        if self.backend.is_open:
            self.backend.close()
            self.connect_btn.config(text='连接')
            self._connected_label = '未连接'
            self.status_var.set(self._connected_label)
            self.status_lbl.config(fg=self._theme['status_idle'])
            self._dot_state = 'idle'
            self._draw_status_dot('idle')
        else:
            port = self.port_var.get().strip()
            if not port:
                messagebox.showwarning('提示', '请选择或输入串口',
                                       parent=self.root)
                return
            try:
                baud = int(self.baud_var.get())
            except Exception:
                baud = 115200
            if self.backend.open(port, baud,
                                 bytesize=self._bytesize_ser(),
                                 parity=self._parity_ser(),
                                 stopbits=self._stopbits_ser(),
                                 rtscts=self._flow_control == 'rtscts'):
                # 连接成功后清空显示，从头开始
                self._clear_log()
                self.connect_btn.config(text='断开')
                self._connected_label = f'已连接 {port}@{baud}'
                self.status_var.set(self._connected_label)
                self.status_lbl.config(fg=self._theme['status_ok'])
                self._dot_state = 'ok'
                self._draw_status_dot('ok')

    # ---- 发送 ----------------------------------------------------------
    def _send_entry(self):
        cmd = self.cmd_var.get()
        if not cmd:
            return
        self._send_cmd(cmd)

    def _clear_cmd_entry(self):
        self.cmd_var.set('')

    def _on_filter_change(self):
        """筛选输入或开关变化：重新编译正则，持久化。"""
        pat = self._filter_var.get() if self._filter_var else ''
        try:
            self._filter_compiled = re.compile(pat) if pat else None
            try:
                self._filter_entry.configure(foreground=self._theme['fg'])
            except Exception:
                pass
        except re.error:
            self._filter_compiled = None
            try:
                self._filter_entry.configure(foreground=self._theme['status_err'])
            except Exception:
                pass
        self._schedule_save()

    def _send_cmd(self, cmd: str):
        if not self.backend.is_open:
            self.status_var.set('未连接，无法发送')
            self.status_lbl.config(fg=self._theme['status_wait'])
            self._dot_state = 'wait'
            self._draw_status_dot('wait')
            return
        if self._show_at_var and self._show_at_var.get():
            self._append(f'{self._ts_prefix_if_needed(cmd)}→ {cmd}\n', 'send')
        self.pending_cmd = cmd
        self.pending_ts = time.time()
        self.backend.send(cmd, append_crlf=self.crlf_var.get())

    # ---- 队列排空 + 路由 ----------------------------------------------
    def _drain(self):
        batch: list[tuple[str, str]] = []
        pending_feeds: list[str] = []
        status_update = None
        error_msg = None
        closed = False
        had_pending = self.pending_cmd is not None
        try:
            while True:
                kind, payload = self.backend.rx_queue.get_nowait()
                if kind == 'line':
                    # 收集到待喂列表，_drain 末尾 after_idle 批量喂
                    # （让 log_txt 先显示，监视窗口喂料不阻塞日志显示）
                    pending_feeds.append(payload)
                    # 日志落盘：原始行直接写，不受正则筛选影响
                    if self.log_writer is not None:
                        try:
                            self.log_writer.put(payload + '\n')
                        except Exception:
                            pass
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
            # 无数据时拉长下次轮询间隔，减少空闲 CPU 占用
            if not batch:
                self.root.after(60, self._drain)
                return
        except Exception:
            pass

        # 批量插入：一次 state 切换 + 一次 insert，避免逐行开销
        if batch:
            self._append_batch(batch)

        # 监视窗口喂料延后到 idle，让日志显示先完成（避免 _feed_monitors 累积开销拖慢显示）
        if pending_feeds:
            self.root.after_idle(self._feed_monitors_batch, pending_feeds)

        if error_msg is not None:
            self.status_var.set(error_msg)
            self.status_lbl.config(fg=self._theme['status_err'])
            self._dot_state = 'err'
        elif status_update is not None:
            level, msg = status_update
            self.status_var.set(msg)
            self.status_lbl.config(
                fg=self._theme['status_ok'] if level == 'info'
                else self._theme['status_err'])
            self._dot_state = 'ok' if level == 'info' else 'err'
        if closed:
            self.connect_btn.config(text='连接')
            self._connected_label = '连接已断开'
            self.status_var.set(self._connected_label)
            self.status_lbl.config(fg=self._theme['status_idle'])
            self._dot_state = 'idle'

        # 响应超时检查
        if self.pending_cmd and (time.time() - self.pending_ts) > RESP_TIMEOUT:
            self._append_batch([('         ← (无响应)\n', 'note')])
            self.pending_cmd = None

        # 等待响应指示：pending 期间显示"等待响应中..."并计时；
        # pending 被响应或超时清除后，恢复连接状态。
        if self.pending_cmd and not closed:
            elapsed_ms = int((time.time() - self.pending_ts) * 1000)
            self.status_var.set(f'等待响应中... {elapsed_ms}ms')
            self.status_lbl.config(fg=self._theme['status_wait'])
            self._dot_state = 'wait'
        elif had_pending and not self.pending_cmd and not closed:
            self.status_var.set(self._connected_label)
            self.status_lbl.config(fg=self._theme['status_ok'])
            self._dot_state = 'ok'

        self._draw_status_dot(self._dot_state)

        self.root.after(DRAIN_MS, self._drain)

    def _route_line(self, line: str) -> tuple[str, str] | None:
        """返回 (text, tag) 供批量插入；同时处理 pending_cmd 配对。
        时间戳转换开启时，行首 13 位毫秒数 → [HH:MM:SS.mmm] 前缀。
        筛选开启时，不匹配正则的日志行被丢弃（响应/发送行不参与筛选）。
        返回 None 表示该行被筛选过滤掉。
        """
        is_resp = bool(RESP_RE.match(line))
        # 时间戳转换：设备时间戳 1783939259030 → [HH:MM:SS.mmm]
        if self._ts_convert_var and self._ts_convert_var.get():
            line = self._convert_ts(line)
        # 筛选：只对周期日志行生效；发送/响应行始终显示
        if (not is_resp and self._filter_enabled_var
                and self._filter_enabled_var.get()
                and self._filter_compiled is not None):
            if not self._filter_compiled.search(line):
                return None
        prefix = self._ts_prefix_if_needed(line)
        show_at = self._show_at_var and self._show_at_var.get()
        if is_resp:
            if self.pending_cmd:
                self.pending_cmd = None
                if show_at:
                    return (f'{prefix}← {line}\n', 'resp')
                return None
            if show_at:
                return (f'{prefix}← {line} (未配对)\n', 'resp')
            return None
        return (f'{prefix}{line}\n', 'log')

    @staticmethod
    def _convert_ts(line: str) -> str:
        """行首 13 位毫秒设备时间戳 → [HH:MM:SS.mmm] 前缀，原时间戳保留。"""
        m = TS_RE.match(line)
        if not m:
            return line
        ts_ms = int(m.group(1))
        sec, ms = divmod(ts_ms, 1000)
        try:
            t = time.localtime(sec)
            readable = time.strftime('%H:%M:%S', t) + f'.{ms:03d}'
            return f'[{readable}] {line}'
        except Exception:
            return line

    def _append_batch(self, batch: list[tuple[str, str]]):
        """批量插入多行，单次 state 切换 + 单次多段 insert，降低逐行开销。"""
        if not batch:
            return
        txt = self.log_txt
        txt.configure(state='normal')
        # Tk insert 支持 (index, chars, tag, chars, tag, ...) 多段一次插入
        args = []
        for text, tag in batch:
            args.append(text)
            args.append(tag)
        txt.insert('end', *args)
        self._trim()
        txt.configure(state='disabled')
        if self.auto_scroll:
            txt.see('end')

    # ---- 清空 / 关闭 ---------------------------------------------------
    def _clear_log(self):
        self.log_txt.configure(state='normal')
        self.log_txt.delete('1.0', 'end')
        self.log_txt.configure(state='disabled')

    def _on_close(self):
        try:
            self._save_cfg()
        except Exception:
            pass
        try:
            self.backend.close()
        finally:
            if self.log_writer is not None:
                self.log_writer.stop()
            self.root.destroy()


def main():
    """启动入口。

    用法：
      serial_debug                    # 启动 Qt 主程序（Stage 4.5 起默认）
      serial_debug --view F1 [F2 ...] # 只启动日志查看器加载给定文件

    环境变量：
      SERIAL_DEBUG_TK=1              回退到 tkinter 主程序
      SERIAL_DEBUG_QT_PILOT=1        启动 Qt 监视窗口（曲线）
      SERIAL_DEBUG_QT_MAIN=1         强制 Qt 主程序（与默认等价，保留兼容）
      SERIAL_DEBUG_LOG_VIEWER=1      启动独立 Qt 日志查看器窗口
    （PyInstaller 打包后 sys.executable -m <module> 不工作，
     改用环境变量区分入口，子进程看到此变量走对应路径）。
    """
    import sys
    import os
    if os.environ.get('SERIAL_DEBUG_QT_PILOT') == '1':
        # Qt 试点窗口：独立子进程运行，避免与主事件循环冲突
        try:
            from serial_debug.ui.qt_monitor_window import QtMonitorWindow
            sys.exit(QtMonitorWindow().run())
        except Exception as e:
            import traceback
            traceback.print_exc()
            try:
                from PySide6.QtWidgets import QMessageBox, QApplication
                app = QApplication.instance() or QApplication(sys.argv)
                QMessageBox.critical(None, '启动失败',
                                     f'Qt 监视窗口启动失败:\n{e}\n\n{traceback.format_exc()}')
            except Exception:
                pass
            sys.exit(1)
    if os.environ.get('SERIAL_DEBUG_LOG_VIEWER') == '1':
        # 独立日志查看器子进程（Qt 主程序的工具菜单启动）
        return _run_log_viewer_only([])
    if os.environ.get('SERIAL_DEBUG_TK') == '1':
        # tkinter 主程序（回退路径，兼容老用法）
        args = sys.argv[1:]
        if args and args[0] == '--view':
            files = [f for f in args[1:] if not f.startswith('-')]
            return _run_log_viewer_only(files)
        try:
            root = tk.Tk()
            app = SerialDebugApp(root)
            if '--debug-monitor' in args:
                root.after(800, app._open_motor_monitor)
            root.mainloop()
        except Exception as e:
            import traceback
            try:
                from tkinter import messagebox as _mb
                _mb.showerror('串口调试助手 - 启动错误',
                              f'{e}\n\n{traceback.format_exc()}')
            except Exception:
                traceback.print_exc()
        return
    # 默认：Qt 主程序
    args = sys.argv[1:]
    if args and args[0] == '--view':
        # --view 走 Qt 日志查看器
        files = [f for f in args[1:] if not f.startswith('-')]
        return _run_log_viewer_only(files)
    from serial_debug.ui.qt_app import QtSerialDebugApp
    sys.exit(QtSerialDebugApp().run())


def _run_log_viewer_only(files: list[str]):
    """独立模式：只启动 Qt 日志查看器窗口。

    桌面图标启动时（无文件参数）直接打开空窗口，用户在工具栏里点
    "打开文件…" 选择日志文件；避免出现"先弹文件对话框、选完才开主窗口"
    的反直觉流程。命令行带文件参数（如 `serial_debug --view f1.txt`）
    仍按原方式自动加载这些文件。

    走 Qt 路径（PySide6 QPlainTextEdit + QTabWidget），与 Qt 主程序
    qt_app.QtSerialDebugApp 一致；环境变量 SERIAL_DEBUG_LOG_VIEWER=1
    由 qt_app._open_log_viewer 子进程注入，PyInstaller 打包后
    sys.executable -m 不识别，改用环境变量区分入口。
    """
    import sys
    import os
    from serial_debug.infra import themes
    pref = themes.load_pref()
    resolved = themes.resolve(pref)
    try:
        from PySide6.QtWidgets import QApplication
        from serial_debug.ui.qt_log_viewer_window import QtLogViewerWindow
    except Exception as e:
        import traceback
        # Qt 不可用时回退到 tkinter
        try:
            import tkinter as _tk
            from tkinter import messagebox as _mb
            _tk.Tk().withdraw()
            _mb.showerror('日志查看 - Qt 启动失败',
                          f'Qt 不可用：{e}\n回退到 tkinter 版本。\n'
                          '请确认已安装 PySide6（pip install PySide6）')
        except Exception:
            pass
        return _run_log_viewer_only_tk(files)
    app = QApplication.instance() or QApplication(sys.argv)
    # 显式设置 applicationName + displayName + desktopFileName，
    # 让 GNOME dock（X11 WM_CLASS / Wayland app_id）识别为"日志查看器"
    # 而不是主程序"串口调试助手"。
    # - X11：WM_CLASS 由 applicationName 决定，匹配 desktop file StartupWMClass
    # - Wayland：app_id 由 applicationName/desktopFileName 决定，匹配 desktop
    #   file basename。若不设 desktopFileName，Wayland 可能用 /proc/PID/exe
    #   basename（symlink resolve 后 = serial_debug）匹配到主程序 desktop file。
    app.setApplicationName('serial_debug_log')
    app.setApplicationDisplayName('日志查看器')
    try:
        app.setDesktopFileName('serial_debug_log')
    except Exception:
        pass
    from serial_debug.ui import qt_theme
    qt_theme.apply_qss(app, resolved)
    win = QtLogViewerWindow(theme_name=resolved)
    # 独立图标
    try:
        icon_path = resource_path(os.path.join('assets', 'icon_view.png'))
        if os.path.exists(icon_path):
            from PySide6.QtGui import QIcon
            app.setWindowIcon(QIcon(icon_path))
    except Exception:
        pass
    for p in files:
        win.open_file(p)
    win.show()
    sys.exit(app.exec())


def _run_log_viewer_only_tk(files: list[str]):
    """tkinter 兜底路径（Qt 不可用时使用）。"""
    import sys
    import os
    from serial_debug.infra import config
    from serial_debug.infra import themes
    root = tk.Tk()
    root.withdraw()
    pref = themes.load_pref()
    resolved = themes.resolve(pref)
    t = themes.THEMES[resolved]
    themes.apply_theme(root, resolved)
    try:
        icon_path = resource_path(
            os.path.join('assets', 'icon_view.png'))
        if os.path.exists(icon_path):
            img = tk.PhotoImage(file=icon_path)
            root.iconphoto(True, img)
            root._icon_img = img  # 防 GC
    except Exception:
        pass
    try:
        win = LogViewerWindow(root, path=None, theme=t)
        for p in files:
            win.open_file(p)
    except Exception as e:
        import traceback
        from tkinter import messagebox as _mb
        _mb.showerror('日志查看 - 启动错误',
                      f'{e}\n\n{traceback.format_exc()}')
        sys.exit(1)
    win.protocol('WM_DELETE_WINDOW',
                 lambda: (win._on_close(), root.destroy()))
    root.mainloop()


if __name__ == '__main__':
    main()
