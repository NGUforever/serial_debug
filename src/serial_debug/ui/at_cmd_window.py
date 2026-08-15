"""AT 命令列表：Treeview 表格 + 行内编辑 + 批量执行。

列宽可拖拽调整（Treeview 内置 heading sash）。
双击单元格弹出 Entry 覆盖层编辑。

列定义：
  cmd       — AT 命令字符串
  desc      — 功能描述
  seq       — 执行顺序（0=不参与批量，1/2/3...=顺序）
  period_ms — 批量执行时该命令发送后等待毫秒数

批量执行：按 seq 升序依次发送命令，每条间等待 period_ms。
"""
from __future__ import annotations

import json
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


def _norm(item: dict) -> dict:
    """确保命令字典包含所有字段（向后兼容旧格式）。"""
    return {
        'cmd': item.get('cmd', ''),
        'desc': item.get('desc', ''),
        'seq': int(item.get('seq', 0) or 0),
        'period_ms': int(item.get('period_ms', 0) or 0),
    }


_COLS = ('cmd', 'desc', 'seq', 'period_ms', 'op')
_EDITABLE_COLS = ('cmd', 'desc', 'seq', 'period_ms')  # 'op' 不可编辑
_COL_LABELS = {
    'cmd': '命令', 'desc': '功能描述',
    'seq': '顺序', 'period_ms': '间隔(ms)', 'op': '操作',
}
_COL_WIDTHS = {'cmd': 200, 'desc': 220, 'seq': 60, 'period_ms': 80, 'op': 60}
_COL_MINS = {'cmd': 80, 'desc': 60, 'seq': 40, 'period_ms': 50, 'op': 40}


class AtCmdPanel(ttk.Frame):
    """AT 命令面板：工具栏 + Treeview + 行内编辑 + 批量执行。"""

    def __init__(self, parent, send_callback, initial_cmds=None,
                 on_change=None, *, show_close_btn=False):
        super().__init__(parent)
        self.send_callback = send_callback
        self.on_change = on_change
        self.commands: list[dict] = [_norm(c) for c in (initial_cmds or [])]
        self._show_close_btn = show_close_btn
        self.close_callback = None
        self._edit_entry: ttk.Entry | None = None
        self._batch_running = False
        self._batch_after_id: str | None = None
        self._batch_queue: list = []
        self._batch_idx = 0

        self._build_toolbar()
        self._build_treeview()
        if not self.commands:
            self._load_default()
        else:
            self._refresh_tree()

    # ---- 工具栏 --------------------------------------------------------
    def _build_toolbar(self):
        bar = ttk.Frame(self, padding=(8, 6))
        bar.pack(fill='x')

        ttk.Button(bar, text='导入 JSON',
                   command=self._import_json).pack(side='left')
        ttk.Button(bar, text='导出 JSON',
                   command=self._export_json).pack(side='left', padx=4)
        ttk.Separator(bar, orient='vertical').pack(
            side='left', fill='y', padx=6)
        ttk.Button(bar, text='添加', command=self._add_row).pack(side='left')
        ttk.Button(bar, text='删除选中',
                   command=self._delete_selected).pack(side='left', padx=4)
        ttk.Button(bar, text='清空',
                   command=self._clear_all).pack(side='left')
        ttk.Separator(bar, orient='vertical').pack(
            side='left', fill='y', padx=6)
        self._batch_btn = ttk.Button(bar, text='批量执行',
                                     command=self._start_batch)
        self._batch_btn.pack(side='left', padx=4)
        self._stop_btn = ttk.Button(bar, text='停止',
                                    command=self._stop_batch,
                                    state='disabled')
        self._stop_btn.pack(side='left', padx=4)
        self._batch_status = ttk.Label(bar, text='空闲', width=18,
                                        anchor='w')
        self._batch_status.pack(side='left', padx=8)

    # ---- Treeview -------------------------------------------------------
    def _build_treeview(self):
        outer = ttk.Frame(self, padding=(8, 0))
        outer.pack(fill='both', expand=True)

        self.tree = ttk.Treeview(
            outer, columns=_COLS, show='headings',
            selectmode='browse', height=12)
        for col in _COLS:
            self.tree.heading(col, text=_COL_LABELS[col])
            self.tree.column(col, width=_COL_WIDTHS[col],
                            minwidth=_COL_MINS[col], stretch=True)
        self.tree.column('seq', anchor='center')
        self.tree.column('period_ms', anchor='center')
        self.tree.column('op', anchor='center', stretch=False)

        vsb = ttk.Scrollbar(outer, orient='vertical',
                            command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side='left', fill='both', expand=True)
        vsb.pack(side='right', fill='y')

        self.tree.bind('<Double-1>', self._on_double_click)
        self.tree.bind('<Button-1>', self._on_tree_click)
        self.tree.bind('<Button-3>', self._on_context_menu)

        self._ctx_menu = tk.Menu(self.tree, tearoff=False)
        self._ctx_menu.add_command(label='编辑单元格',
                                   command=self._edit_focused_cell)
        self._ctx_menu.add_separator()
        self._ctx_menu.add_command(label='删除此行',
                                   command=self._delete_selected)

    def _on_tree_click(self, event):
        """点击 Treeview：如果点的是'操作'列，发送该行命令。"""
        row_id = self.tree.identify_row(event.y)
        col = self.tree.identify_column(event.x)
        if not row_id or not col:
            return
        col_idx = int(col.replace('#', '')) - 1
        if col_idx < 0 or col_idx >= len(_COLS):
            return
        if _COLS[col_idx] == 'op':
            cmd = self.tree.set(row_id, 'cmd')
            cmd = (cmd or '').strip()
            if cmd:
                self.send_callback(cmd)

    def _refresh_tree(self):
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        for idx, cmd in enumerate(self.commands):
            # 奇偶行交替色 tag（VSCode 风格）
            tag = 'evenrow' if idx % 2 == 0 else 'oddrow'
            self.tree.insert('', 'end', values=(
                cmd['cmd'], cmd['desc'], cmd['seq'], cmd['period_ms'], '发送'),
                tags=(tag,))
        # 配置交替行配色（首次调用时绑定）
        try:
            from serial_debug.infra import themes
            t = themes.THEMES.get(themes.resolve(themes.load_pref())) or \
                themes.THEMES['dark']
            self.tree.tag_configure('evenrow', background=t['panel_bg'])
            self.tree.tag_configure('oddrow', background=t['bg'])
        except Exception:
            pass

    def _notify_change(self):
        if self.on_change:
            try:
                self.on_change(list(self.commands))
            except Exception:
                pass

    # ---- 行内编辑 --------------------------------------------------------
    def _on_double_click(self, event):
        row_id = self.tree.identify_row(event.y)
        col = self.tree.identify_column(event.x)
        if not row_id or not col:
            return
        col_idx = int(col.replace('#', '')) - 1
        if col_idx < 0 or col_idx >= len(_EDITABLE_COLS):
            return  # 'op' 列不可编辑
        self._start_edit(row_id, _EDITABLE_COLS[col_idx])

    def _edit_focused_cell(self):
        sel = self.tree.selection()
        if not sel:
            return
        self._start_edit(sel[0], 'cmd')

    def _start_edit(self, row_id: str, col_name: str):
        if self._edit_entry is not None:
            try:
                self._edit_entry.destroy()
            except Exception:
                pass
            self._edit_entry = None
        bbox = self.tree.bbox(row_id, col_name)
        if not bbox:
            return
        x, y, w, h = bbox
        value = self.tree.set(row_id, col_name)
        entry = ttk.Entry(self.tree, font=('DejaVu Sans Mono', 11))
        entry.place(x=x, y=y, width=max(w, 60), height=h)
        entry.insert(0, str(value))
        entry.select_range(0, 'end')
        entry.focus_set()
        self._edit_entry = entry

        def _commit(_e=None):
            if self._edit_entry is None:
                return
            new_val = entry.get()
            entry.destroy()
            self._edit_entry = None
            # seq / period_ms 强制转 int
            if col_name in ('seq', 'period_ms'):
                try:
                    new_val = str(int(new_val))
                except ValueError:
                    new_val = '0'
            self.tree.set(row_id, col_name, new_val)
            self._sync_from_tree()
            self._notify_change()

        def _cancel(_e=None):
            if self._edit_entry is not None:
                entry.destroy()
                self._edit_entry = None

        entry.bind('<Return>', _commit)
        entry.bind('<Escape>', _cancel)
        entry.bind('<FocusOut>', _commit)

    def _sync_from_tree(self):
        self.commands = []
        for iid in self.tree.get_children():
            vals = self.tree.set(iid)
            self.commands.append(_norm(vals))

    # ---- 右键菜单 --------------------------------------------------------
    def _on_context_menu(self, event):
        row_id = self.tree.identify_row(event.y)
        if row_id:
            self.tree.selection_set(row_id)
            self._ctx_menu.tk_popup(event.x_root, event.y_root)

    # ---- 默认命令 -------------------------------------------------------
    def _load_default(self):
        self.commands = [
            _norm({'cmd': 'AT+ELOG=0%', 'desc': '关闭周期日志',
                   'seq': 1, 'period_ms': 500}),
            _norm({'cmd': 'AT+ELOG=1%', 'desc': '开启周期日志',
                   'seq': 2, 'period_ms': 500}),
        ]
        self._refresh_tree()
        self._notify_change()

    # ---- 操作 ----------------------------------------------------------
    def _add_row(self):
        self.commands.append(_norm({'cmd': '', 'desc': '',
                                   'seq': 0, 'period_ms': 0}))
        self._refresh_tree()
        self._notify_change()
        children = self.tree.get_children()
        if children:
            self.tree.see(children[-1])
            self.tree.selection_set(children[-1])

    def _delete_selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo('提示', '请先选中要删除的行',
                                parent=self.winfo_toplevel())
            return
        for iid in sel:
            idx = self.tree.index(iid)
            del self.commands[idx]
            self.tree.delete(iid)
        self._notify_change()

    def _clear_all(self):
        if not messagebox.askyesno('确认', '清空所有命令？',
                                   parent=self.winfo_toplevel()):
            return
        self.commands = []
        self._refresh_tree()
        self._notify_change()

    def _send_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        cmd = self.tree.set(sel[0], 'cmd')
        cmd = (cmd or '').strip()
        if not cmd:
            return
        self.send_callback(cmd)

    # ---- 批量执行（周期循环）---------------------------------------------
    def _toggle_batch(self):
        if self._batch_running:
            self._stop_batch()
        else:
            self._start_batch()

    def _start_batch(self):
        """按 seq 升序循环执行：跑完一轮从第一条重新开始，直到点停止。

        间隔规则：发送本条后等待该条的 period_ms 再发下一条。
        period_ms<=0 时按 500ms 兜底。
        """
        batch = [(c['seq'], c['period_ms'], c['cmd'])
                 for c in self.commands
                 if c.get('seq', 0) > 0 and c.get('cmd', '').strip()]
        batch.sort(key=lambda x: x[0])
        if not batch:
            messagebox.showinfo('提示',
                                '没有标记执行顺序（seq > 0）的命令',
                                parent=self.winfo_toplevel())
            return
        self._batch_queue = batch
        self._batch_idx = 0
        self._batch_running = True
        self._batch_btn.configure(state='disabled')
        self._stop_btn.configure(state='normal')
        self._update_batch_status('启动周期批量执行')
        self._exec_next()

    def _stop_batch(self):
        if not self._batch_running and self._batch_after_id is None:
            return
        self._batch_running = False
        if self._batch_after_id is not None:
            try:
                self.after_cancel(self._batch_after_id)
            except Exception:
                pass
            self._batch_after_id = None
        self._batch_btn.configure(state='normal')
        self._stop_btn.configure(state='disabled')
        self._update_batch_status('已停止')

    def _exec_next(self):
        if not self._batch_running:
            return
        if not self._batch_queue:
            self._stop_batch()
            return
        # 循环：索引到达末尾后回到 0
        if self._batch_idx >= len(self._batch_queue):
            self._batch_idx = 0
            self._update_batch_status('开始新一轮')
        seq, period_ms, cmd = self._batch_queue[self._batch_idx]
        self.send_callback(cmd)
        self._update_batch_status(
            f'执行 {self._batch_idx + 1}/{len(self._batch_queue)}: {cmd}')
        self._batch_idx += 1
        delay = period_ms if period_ms and period_ms > 0 else 500
        self._batch_after_id = self.after(delay, self._exec_next)

    def _update_batch_status(self, text: str):
        try:
            self._batch_status.configure(text=text)
        except Exception:
            pass

    # ---- 导入/导出 ------------------------------------------------------
    def _import_json(self):
        path = filedialog.askopenfilename(
            title='导入 AT 命令 JSON', parent=self.winfo_toplevel(),
            filetypes=[('JSON', '*.json'), ('所有文件', '*.*')])
        if not path:
            return
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, list):
                raise ValueError('JSON 顶层必须是列表')
            cmds = [_norm(item) for item in data if isinstance(item, dict)]
            self.commands = cmds
            self._refresh_tree()
            self._notify_change()
        except Exception as e:
            messagebox.showerror('导入失败', f'{e}',
                                 parent=self.winfo_toplevel())

    def _export_json(self):
        path = filedialog.asksaveasfilename(
            title='导出 AT 命令 JSON', parent=self.winfo_toplevel(),
            defaultextension='.json',
            filetypes=[('JSON', '*.json'), ('所有文件', '*.*')])
        if not path:
            return
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(self.commands, f, ensure_ascii=False, indent=2)
        except Exception as e:
            messagebox.showerror('导出失败', f'{e}',
                                 parent=self.winfo_toplevel())

    # ---- 主题 ----------------------------------------------------------
    def apply_theme(self, theme: dict):
        """ttk.Treeview 由全局 Style 接管；只更新右键菜单配色 + 交替行 tag。"""
        # 更新交替行 tag 配色（跟随主题深/浅切换）
        try:
            self.tree.tag_configure('evenrow', background=theme['panel_bg'])
            self.tree.tag_configure('oddrow', background=theme['bg'])
        except Exception:
            pass
        try:
            self._ctx_menu.configure(
                bg=theme['bg'], fg=theme['fg'],
                activebackground=theme['select_bg'],
                activeforeground='#ffffff',
                activeborderwidth=0, borderwidth=0,
                disabledforeground=theme['muted'],
                relief='flat', tearoff=False,
                font=('DejaVu Sans', 10))
        except Exception:
            for k, v in [('bg', theme['bg']), ('fg', theme['fg']),
                         ('activebackground', theme['select_bg']),
                         ('activeforeground', '#ffffff'),
                         ('activeborderwidth', 0), ('borderwidth', 0),
                         ('disabledforeground', theme['muted']),
                         ('relief', 'flat'), ('tearoff', False),
                         ('font', ('DejaVu Sans', 10))]:
                try:
                    self._ctx_menu.configure(**{k: v})
                except Exception:
                    pass

    # ---- 清理 ----------------------------------------------------------
    def destroy(self):
        self._stop_batch()
        super().destroy()


class AtCmdWindow(tk.Toplevel):
    """独立 AT 命令窗口：包装 AtCmdPanel。"""

    def __init__(self, parent, send_callback, initial_cmds=None,
                 on_change=None):
        super().__init__(parent)
        self.title('AT 命令列表')
        self.geometry('720x520')
        self.minsize(560, 360)
        self.resizable(True, True)
        self.send_callback = send_callback

        self.panel = AtCmdPanel(self, send_callback, initial_cmds, on_change)
        self.panel.pack(fill='both', expand=True)

        self.protocol('WM_DELETE_WINDOW', self._on_close)
        # 不调用 transient：GNOME/Mutter 把 transient 窗口当对话框，
        # 禁用最大化。改用 lift+focus 保持层级。
        try:
            self.lift(parent)
            self.focus_force()
        except Exception:
            pass

    @property
    def commands(self):
        return self.panel.commands

    def _on_close(self):
        self.panel._stop_batch()
        self.destroy()

    def update_theme(self):
        try:
            from serial_debug.infra import themes
            t = themes.THEMES[themes.resolve(themes.load_pref())]
            self.configure(bg=t['bg'])
            self.panel.apply_theme(t)
        except Exception:
            pass
