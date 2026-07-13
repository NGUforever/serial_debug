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

RESP_RE = re.compile(r'^(OK,|ERROR|\+)')   # 响应行判定
MAX_LINES = 5000                            # 最多保留行数
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
        self.auto_scroll = True
        self.log_writer: log_writer.LogWriter | None = None
        self.log_path: str | None = None

        self._build_topbar()
        self._build_panel()
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

    # ---- 中部单窗口 ----------------------------------------------------
    def _build_panel(self):
        frame = ttk.Frame(self.root)
        frame.pack(fill='both', expand=True, padx=8, pady=4)
        ttk.Label(frame, text='日志窗口').pack(anchor='w')
        self.log_txt = tk.Text(frame, wrap='none', undo=False,
                               bg='#101418', fg='#cdd6e0',
                               insertbackground='#cdd6e0',
                               font=('Courier', 10))
        self.log_txt.pack(side='left', fill='both', expand=True)
        self.log_txt.configure(state='disabled')
        self.log_scroll = ttk.Scrollbar(frame, command=self.log_txt.yview)
        self.log_scroll.pack(side='right', fill='y')
        self.log_txt.configure(yscrollcommand=self.log_scroll.set)
        self.log_txt.bind('<Button-1>', lambda _e: self._check_scroll())

    def _apply_tags(self):
        self.log_txt.tag_config('log', foreground='#9fb0c8')
        self.log_txt.tag_config('ts', foreground='#5b6b80')
        self.log_txt.tag_config('send', foreground='#5aa9ff')
        self.log_txt.tag_config('resp', foreground='#5fe08a')
        self.log_txt.tag_config('note', foreground='#e0c45f')

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
        ttk.Button(bar, text='清空', command=self._clear_log).pack(
            side='left', padx=(8, 2))

    # ---- 行为 ----------------------------------------------------------
    def _on_autoscroll_toggle(self):
        self.auto_scroll = self.autoscroll_var.get()

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
                # 连接成功后清空显示，从头开始
                self._clear_log()
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
        # 发送前清空接收缓冲和队列中的旧日志行，确保响应不被淹没
        self.backend.flush_input()
        ts = self._now()
        self._append(f'[{ts}] → {cmd}\n', 'send')
        self.pending_cmd = cmd
        self.pending_ts = time.time()
        self.backend.send(cmd, append_crlf=self.crlf_var.get())

    # ---- 队列排空 + 路由 ----------------------------------------------
    def _drain(self):
        batch: list[tuple[str, str]] = []
        status_update = None
        error_msg = None
        closed = False
        try:
            while True:
                kind, payload = self.backend.rx_queue.get_nowait()
                if kind == 'line':
                    batch.append(self._route_line(payload))
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
            self.status_lbl.config(fg='#e0554f')
        elif status_update is not None:
            level, msg = status_update
            self.status_var.set(msg)
            self.status_lbl.config(
                fg='#5fe08a' if level == 'info' else '#e0554f')
        if closed:
            self.connect_btn.config(text='连接')
            self.status_var.set('连接已断开')
            self.status_lbl.config(fg='#888')

        # 响应超时检查
        if self.pending_cmd and (time.time() - self.pending_ts) > RESP_TIMEOUT:
            self._append_batch([('         ← (无响应)\n', 'note')])
            self.pending_cmd = None

        self.root.after(DRAIN_MS, self._drain)

    def _route_line(self, line: str) -> tuple[str, str]:
        """返回 (text, tag) 供批量插入；同时处理 pending_cmd 配对。"""
        ts = self._now()
        if RESP_RE.match(line):
            if self.pending_cmd:
                self.pending_cmd = None
                return (f'[{ts}] ← {line}\n', 'resp')
            return (f'[{ts}] ← {line} (未配对)\n', 'resp')
        return (f'[{ts}] {line}\n', 'log')

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
