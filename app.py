"""串口调试助手 —— tkinter GUI。

布局：
  顶栏  串口 / 波特率 / 连接 / 状态灯
  中部  左「周期日志」 | 右「AT 交互」(→发送 / ←响应 成对)
  底栏  命令输入 + 发送 + 日志开/关快捷 + 自动滚动/追加换行/清空

行路由：命中响应正则 (^OK,) 的行进右栏并配对 pending_cmd，其余进左栏。
所有控件写入在主线程，经 root.after(30) 排空 rx_queue。
"""
from __future__ import annotations

import re
import time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from serial_backend import SerialBackend
import log_writer

RESP_RE = re.compile(r'^(OK,|ERROR|\+)')   # 响应行判定
MAX_LINES = 5000                            # 每栏最多保留行数
DRAIN_MS = 30                               # 刷新间隔
RESP_TIMEOUT = 2.0                          # 等待响应超时(秒)


class SerialDebugApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title('串口调试助手')
        root.geometry('1000x640')
        root.minsize(760, 460)

        self.backend = SerialBackend()
        self.pending_cmd: str | None = None
        self.pending_ts: float = 0.0
        self.auto_scroll = {'log': True, 'at': True}
        self.log_writer: log_writer.LogWriter | None = None
        self.log_path: str | None = None

        self._build_topbar()
        self._build_panels()
        self._build_bottombar()
        self._apply_tags()
        self._drain()
        root.protocol('WM_DELETE_WINDOW', self._on_close)

    # ---- 顶栏 ----------------------------------------------------------
    def _build_topbar(self):
        bar = ttk.Frame(self.root, padding=(8, 6))
        bar.pack(fill='x')

        ttk.Label(bar, text='串口').pack(side='left')
        self.port_var = tk.StringVar()
        self.port_box = ttk.Combobox(
            bar, textvariable=self.port_var, values=SerialBackend.list_ports(),
            width=18)
        self.port_box.pack(side='left', padx=(4, 12))
        self.port_box.bind('<FocusIn>', lambda _e: self.port_box.configure(
            values=SerialBackend.list_ports()))

        ttk.Label(bar, text='波特率').pack(side='left')
        self.baud_var = tk.IntVar(value=115200)
        self.baud_box = ttk.Combobox(
            bar, textvariable=self.baud_var, width=8,
            values=['9600', '19200', '38400', '57600', '115200', '230400',
                    '460800', '921600'])
        self.baud_box.pack(side='left', padx=(4, 12))

        self.connect_btn = ttk.Button(bar, text='连接', command=self._toggle)
        self.connect_btn.pack(side='left', padx=4)

        ttk.Button(bar, text='日志存储...', command=self._choose_log).pack(
            side='left', padx=4)
        self.log_lbl = tk.Label(bar, text='未启用', fg='#888', anchor='w')
        self.log_lbl.pack(side='left', padx=(8, 0))

        self.status_var = tk.StringVar(value='未连接')
        self.status_lbl = tk.Label(
            bar, textvariable=self.status_var, fg='#888', width=30, anchor='w')
        self.status_lbl.pack(side='left', padx=(12, 0))

    # ---- 中部分栏 ------------------------------------------------------
    def _build_panels(self):
        pane = ttk.PanedWindow(self.root, orient='horizontal')
        pane.pack(fill='both', expand=True, padx=8, pady=4)

        # 左：周期日志
        left = ttk.Frame(pane)
        ttk.Label(left, text='周期日志').pack(anchor='w')
        self.log_txt = tk.Text(left, wrap='none', undo=False,
                               bg='#101418', fg='#cdd6e0',
                               insertbackground='#cdd6e0',
                               font=('Courier', 10))
        self.log_txt.pack(fill='both', expand=True)
        self.log_txt.configure(state='disabled')
        self.log_scroll = ttk.Scrollbar(left, command=self.log_txt.yview)
        self.log_scroll.pack(side='right', fill='y')
        self.log_txt.configure(yscrollcommand=self.log_scroll.set)
        self.log_txt.bind('<Button-1>', lambda _e: self._check_scroll('log'))
        pane.add(left, weight=3)

        # 右：AT 交互
        right = ttk.Frame(pane)
        ttk.Label(right, text='AT 交互').pack(anchor='w')
        self.at_txt = tk.Text(right, wrap='none', undo=False,
                              bg='#0d1a12', fg='#cdd6e0',
                              insertbackground='#cdd6e0',
                              font=('Courier', 10))
        self.at_txt.pack(fill='both', expand=True)
        self.at_txt.configure(state='disabled')
        self.at_scroll = ttk.Scrollbar(right, command=self.at_txt.yview)
        self.at_scroll.pack(side='right', fill='y')
        self.at_txt.configure(yscrollcommand=self.at_scroll.set)
        self.at_txt.bind('<Button-1>', lambda _e: self._check_scroll('at'))
        pane.add(right, weight=2)

    def _apply_tags(self):
        self.log_txt.tag_config('log', foreground='#9fb0c8')
        self.log_txt.tag_config('ts', foreground='#5b6b80')
        self.at_txt.tag_config('send', foreground='#5aa9ff')
        self.at_txt.tag_config('resp', foreground='#5fe08a')
        self.at_txt.tag_config('ts', foreground='#4a6552')
        self.at_txt.tag_config('note', foreground='#e0c45f')

    # ---- 底栏 ----------------------------------------------------------
    def _build_bottombar(self):
        bar = ttk.Frame(self.root, padding=(8, 6))
        bar.pack(fill='x')

        ttk.Label(bar, text='AT 命令').pack(side='left')
        self.cmd_var = tk.StringVar()
        self.cmd_entry = ttk.Entry(bar, textvariable=self.cmd_var)
        self.cmd_entry.pack(side='left', fill='x', expand=True, padx=(4, 6))
        self.cmd_entry.bind('<Return>', lambda _e: self._send_entry())

        ttk.Button(bar, text='发送', command=self._send_entry).pack(side='left')
        ttk.Button(bar, text='日志关',
                   command=lambda: self._send_cmd('AT+ELOG=0%')).pack(
            side='left', padx=(8, 2))
        ttk.Button(bar, text='日志开',
                   command=lambda: self._send_cmd('AT+ELOG=1%')).pack(
            side='left')

        ttk.Separator(bar, orient='vertical').pack(side='left', fill='y',
                                                   padx=8)

        self.crlf_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text='追加 \\r\\n',
                        variable=self.crlf_var).pack(side='left')
        self.autoscroll_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text='自动滚动',
                        variable=self.autoscroll_var,
                        command=self._on_autoscroll_toggle).pack(side='left')
        ttk.Button(bar, text='清空日志', command=self._clear_log).pack(
            side='left', padx=(8, 2))
        ttk.Button(bar, text='清空AT', command=self._clear_at).pack(
            side='left')

    # ---- 行为 ----------------------------------------------------------
    def _on_autoscroll_toggle(self):
        v = self.autoscroll_var.get()
        self.auto_scroll['log'] = v
        self.auto_scroll['at'] = v

    def _check_scroll(self, which: str):
        """用户点滚动条/拖选时暂停跟随。"""
        txt = self.log_txt if which == 'log' else self.at_txt
        try:
            top, bottom = txt.yview()
            if bottom < 0.98:
                self.auto_scroll[which] = False
        except Exception:
            pass

    def _is_at_bottom(self, txt) -> bool:
        try:
            _top, bottom = txt.yview()
            return bottom >= 0.98
        except Exception:
            return True

    def _append(self, txt, text, tag):
        txt.configure(state='normal')
        txt.insert('end', text, tag)
        self._trim(txt)
        txt.configure(state='disabled')
        if self.auto_scroll['log'] if txt is self.log_txt else self.auto_scroll['at']:
            txt.see('end')
        # 日志写入：非阻塞入队，不拖慢显示
        if self.log_writer is not None:
            self.log_writer.put(text)

    def _trim(self, txt):
        line_cnt = int(txt.index('end-1c').split('.')[0])
        if line_cnt > MAX_LINES:
            drop = line_cnt - MAX_LINES
            txt.delete('1.0', f'{drop + 1}.0')

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
            messagebox.showerror('错误', f'无法创建日志文件: {w.error}')
            return
        self.log_writer = w
        self.log_path = path
        self.log_lbl.config(text=f'存储中: {path}', fg='#9fb0c8')

    # ---- 连接 ----------------------------------------------------------
    def _toggle(self):
        if self.backend.is_open:
            self.backend.close()
            self.connect_btn.config(text='连接')
            self.status_var.set('未连接')
            self.status_lbl.config(fg='#888')
        else:
            port = self.port_var.get().strip()
            if not port:
                messagebox.showwarning('提示', '请选择或输入串口')
                return
            try:
                baud = int(self.baud_var.get())
            except Exception:
                baud = 115200
            if self.backend.open(port, baud):
                self.connect_btn.config(text='断开')
                self.status_var.set(f'已连接 {port}@{baud}')
                self.status_lbl.config(fg='#5fe08a')

    # ---- 发送 ----------------------------------------------------------
    def _send_entry(self):
        cmd = self.cmd_var.get()
        if not cmd:
            return
        self._send_cmd(cmd)

    def _send_cmd(self, cmd: str):
        if not self.backend.is_open:
            self.status_var.set('未连接，无法发送')
            self.status_lbl.config(fg='#e0c45f')
            return
        ts = self._now()
        self._append(self.at_txt, f'[{ts}] → {cmd}\n', 'send')
        self.pending_cmd = cmd
        self.pending_ts = time.time()
        self.backend.send(cmd, append_crlf=self.crlf_var.get())

    # ---- 队列排空 + 路由 ----------------------------------------------
    def _drain(self):
        try:
            while True:
                kind, payload = self.backend.rx_queue.get_nowait()
                if kind == 'line':
                    self._route_line(payload)
                elif kind == 'status':
                    level, msg = payload
                    self.status_var.set(msg)
                    self.status_lbl.config(
                        fg='#5fe08a' if level == 'info' else '#e0554f')
                elif kind == 'error':
                    self.status_var.set(payload)
                    self.status_lbl.config(fg='#e0554f')
                elif kind == 'closed':
                    self.connect_btn.config(text='连接')
                    self.status_var.set('连接已断开')
                    self.status_lbl.config(fg='#888')
        except Exception:
            pass

        # 响应超时检查
        if self.pending_cmd and (time.time() - self.pending_ts) > RESP_TIMEOUT:
            self._append(self.at_txt, '         ← (无响应)\n', 'note')
            self.pending_cmd = None

        self.root.after(DRAIN_MS, self._drain)

    def _route_line(self, line: str):
        ts = self._now()
        if RESP_RE.match(line):
            # 响应：配对 pending_cmd
            if self.pending_cmd:
                self._append(self.at_txt, f'[{ts}] ← {line}\n', 'resp')
                self.pending_cmd = None
            else:
                self._append(self.at_txt, f'[{ts}] ← {line} (未配对)\n', 'resp')
        else:
            self._append(self.log_txt, f'[{ts}] {line}\n', 'log')

    # ---- 清空 / 关闭 ---------------------------------------------------
    def _clear_log(self):
        self.log_txt.configure(state='normal')
        self.log_txt.delete('1.0', 'end')
        self.log_txt.configure(state='disabled')

    def _clear_at(self):
        self.at_txt.configure(state='normal')
        self.at_txt.delete('1.0', 'end')
        self.at_txt.configure(state='disabled')

    def _on_close(self):
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
