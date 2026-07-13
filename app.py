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

import re
import time
import queue
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from serial_backend import SerialBackend
import log_writer
from at_cmd_window import AtCmdWindow
import themes
import config

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
        self._connected_label: str = '未连接'
        self._dot_state: str = 'idle'
        self._save_after_id: str | None = None
        # 筛选状态
        self._filter_var: tk.StringVar | None = None
        self._filter_enabled_var: tk.BooleanVar | None = None
        self._filter_compiled: re.Pattern | None = None
        self._ts_convert_var: tk.BooleanVar | None = None
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
        # 启动时恢复日志写入器
        self._restore_log_writer()
        self._drain()
        root.protocol('WM_DELETE_WINDOW', self._on_close)
        root.bind('<Configure>', self._on_root_configure, add='+')

    # ---- 图标 ----------------------------------------------------------
    def _set_icon(self):
        """加载 assets/icon.png 作为窗口图标。"""
        try:
            import os
            icon_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                'assets', 'icon.png')
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
        menubar.add_cascade(label='文件(F)', menu=m_file, underline=0)
        m_file.add_command(label='导入 AT 命令 JSON…', command=self._menu_import_at)
        m_file.add_command(label='导出 AT 命令 JSON…', command=self._menu_export_at)
        m_file.add_separator()
        m_file.add_command(label='选择日志存储路径…', command=self._choose_log)
        m_file.add_separator()
        m_file.add_command(label='退出', command=self._on_close, accelerator='Ctrl+Q')

        # 视图
        m_view = tk.Menu(menubar, tearoff=False)
        menubar.add_cascade(label='视图(V)', menu=m_view, underline=0)

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
        # 时间戳转换
        self._ts_convert_var = tk.BooleanVar(
            value=self.cfg.get('ts_convert_enabled', False))
        m_view.add_checkbutton(label='设备时间戳→可读时间',
                               variable=self._ts_convert_var,
                               command=self._on_ts_convert_toggle)
        # 自动滚动
        self._auto_scroll_menu_var = tk.BooleanVar(
            value=self.cfg.get('auto_scroll', True))
        m_view.add_checkbutton(label='自动滚动',
                               variable=self._auto_scroll_menu_var,
                               command=self._on_menu_autoscroll_toggle)

        # 工具
        m_tools = tk.Menu(menubar, tearoff=False)
        menubar.add_cascade(label='工具(T)', menu=m_tools, underline=0)
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
        m_tools.add_command(label='关闭周期日志 (AT+ELOG=0%)',
                            command=lambda: self._send_cmd('AT+ELOG=0%'))
        m_tools.add_command(label='开启周期日志 (AT+ELOG=1%)',
                            command=lambda: self._send_cmd('AT+ELOG=1%'))
        m_tools.add_separator()
        m_tools.add_command(label='AT 命令列表…',
                            command=self._open_at_window)

        # 帮助
        m_help = tk.Menu(menubar, tearoff=False)
        menubar.add_cascade(label='帮助(H)', menu=m_help, underline=0)
        m_help.add_command(label='关于', command=self._show_about)

        # 快捷键
        self.root.bind('<Control-q>', lambda _e: self._on_close())
        self.root.bind('<Control-d>', lambda _e: self._toggle())
        self.root.bind('<Control-l>', lambda _e: self._clear_log())

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
        self.port_box = ttk.Combobox(
            bar, textvariable=self.port_var, values=SerialBackend.list_ports(),
            width=22)
        self.port_box.pack(side='left', padx=(0, 8))
        self.port_box.bind('<FocusIn>', lambda _e: self.port_box.configure(
            values=SerialBackend.list_ports()))
        self.port_var.trace_add('write', lambda *_: self._schedule_save())

        ttk.Label(bar, text='波特率').pack(side='left', padx=(0, 4))
        try:
            baud_init = int(self.cfg.get('baudrate', 115200))
        except Exception:
            baud_init = 115200
        self.baud_var = tk.IntVar(value=baud_init)
        self.baud_box = ttk.Combobox(
            bar, textvariable=self.baud_var, width=8,
            values=['9600', '19200', '38400', '57600', '115200', '230400',
                    '460800', '921600'])
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
        ttk.Checkbutton(filter_bar, text='启用（正则）',
                        variable=self._filter_enabled_var,
                        command=self._on_filter_change
                        ).pack(side='left', padx=(0, 6))
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
        # 嵌入式 AT 面板 Canvas 跟随
        if self._at_embedded is not None:
            try:
                self._at_embedded.apply_theme(t)
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

        # 选项组
        self.crlf_var = tk.BooleanVar(value=self.cfg.get('append_crlf', False))
        ttk.Checkbutton(bar, text='追加 \\r\\n',
                        variable=self.crlf_var,
                        command=lambda: self._schedule_save()
                        ).pack(side='left', padx=4)
        self.autoscroll_var = tk.BooleanVar(
            value=self.cfg.get('auto_scroll', True))
        ttk.Checkbutton(bar, text='自动滚动',
                        variable=self.autoscroll_var,
                        command=self._on_autoscroll_toggle).pack(
            side='left', padx=4)
        ttk.Button(bar, text='清空',
                   command=self._clear_log).pack(side='left', padx=4)

    def _build_serial_bar(self):
        """最下方串口参数栏：数据位 / 校验 / 停止位 / 流控。"""
        bar = ttk.Frame(self.root, padding=(10, 4))
        bar.pack(fill='x')

        ttk.Label(bar, text='数据位').pack(side='left', padx=(0, 4))
        self.bytesize_var = tk.IntVar(value=self._bytesize)
        self.bytesize_box = ttk.Combobox(
            bar, textvariable=self.bytesize_var, width=4, state='readonly',
            values=['5', '6', '7', '8'])
        self.bytesize_box.pack(side='left', padx=(0, 12))
        self.bytesize_var.trace_add('write', lambda *_: self._on_serial_param_change())

        ttk.Label(bar, text='校验位').pack(side='left', padx=(0, 4))
        parity_map = {'无': 'none', '偶校验': 'even', '奇校验': 'odd'}
        self._parity_label = next((k for k, v in parity_map.items()
                                   if v == self._parity), '无')
        self.parity_var = tk.StringVar(value=self._parity_label)
        self.parity_box = ttk.Combobox(
            bar, textvariable=self.parity_var, width=8, state='readonly',
            values=list(parity_map.keys()))
        self.parity_box.pack(side='left', padx=(0, 12))
        self._parity_map = parity_map
        self.parity_var.trace_add('write', lambda *_: self._on_serial_param_change())

        ttk.Label(bar, text='停止位').pack(side='left', padx=(0, 4))
        self.stopbits_var = tk.IntVar(value=self._stopbits)
        self.stopbits_box = ttk.Combobox(
            bar, textvariable=self.stopbits_var, width=4, state='readonly',
            values=['1', '2'])
        self.stopbits_box.pack(side='left', padx=(0, 12))
        self.stopbits_var.trace_add('write', lambda *_: self._on_serial_param_change())

        ttk.Label(bar, text='流控').pack(side='left', padx=(0, 4))
        fc_map = {'无': 'none', 'RTS/CTS': 'rtscts'}
        self._fc_label = next((k for k, v in fc_map.items()
                               if v == self._flow_control), '无')
        self.flow_var = tk.StringVar(value=self._fc_label)
        self.flow_box = ttk.Combobox(
            bar, textvariable=self.flow_var, width=10, state='readonly',
            values=list(fc_map.keys()))
        self.flow_box.pack(side='left', padx=(0, 12))
        self._fc_map = fc_map
        self.flow_var.trace_add('write', lambda *_: self._on_serial_param_change())

        ttk.Separator(bar, orient='vertical').pack(
            side='left', fill='y', padx=8)

        # 统计信息（占位，运行时更新）
        self._stats_var = tk.StringVar(value='RX: 0B  TX: 0B')
        ttk.Label(bar, textvariable=self._stats_var,
                  foreground=self._theme['muted']).pack(side='right', padx=4)

    def _on_serial_param_change(self):
        """串口参数变化：更新内部状态，持久化。已连接则提示重连生效。"""
        try:
            self._bytesize = int(self.bytesize_var.get())
        except Exception:
            pass
        self._parity = self._parity_map.get(self.parity_var.get(), 'none')
        try:
            self._stopbits = int(self.stopbits_var.get())
        except Exception:
            pass
        self._flow_control = self._fc_map.get(self.flow_var.get(), 'none')
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
        from at_cmd_window import AtCmdPanel
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
        self._append(f'→ {cmd}\n', 'send')
        self.pending_cmd = cmd
        self.pending_ts = time.time()
        self.backend.send(cmd, append_crlf=self.crlf_var.get())

    # ---- 队列排空 + 路由 ----------------------------------------------
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

        # 批量插入：一次 state 切换 + 一次 insert，避免逐行开销
        if batch:
            self._append_batch(batch)

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
        if is_resp:
            if self.pending_cmd:
                self.pending_cmd = None
                return (f'← {line}\n', 'resp')
            return (f'← {line} (未配对)\n', 'resp')
        return (f'{line}\n', 'log')

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
        if self.log_writer is not None:
            for text, _tag in batch:
                self.log_writer.put(text)
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
    root = tk.Tk()
    SerialDebugApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
