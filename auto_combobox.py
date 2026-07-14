"""AutoCombobox：下拉方向自适应 + 可手动输入的 Combobox。

ttk.Combobox 的下拉方向由 Tk 内部决定，无法控制，最大化窗口时
下拉列表可能被屏幕底边截断。AutoCombobox 用 Toplevel + Listbox
自绘下拉，根据屏幕剩余空间决定向下还是向上弹出，并保证内容完全显示。

支持两种模式：
  - state='readonly'（默认）：Entry 不可编辑，点击 Entry 或按钮弹出
  - state='normal' / 'editable'：Entry 可手动输入，按钮弹出，回车触发选中

对外 API 与 ttk.Combobox 兼容：
  get() / set() / configure(values=...)
  <<ComboboxSelected>> 虚拟事件
  trace_add('write', cb)
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk


class AutoCombobox(ttk.Frame):
    """下拉方向自适应 + 可手动输入的 Combobox。"""

    def __init__(self, parent, values=(), *, state='readonly', width=12,
                 font=None, textvariable=None, **kw):
        super().__init__(parent)
        self._values: list = list(values)
        self._readonly = (state == 'readonly')
        self._var = textvariable if textvariable is not None else tk.StringVar()
        self._font = font or ('DejaVu Sans', 10)
        self._popup: tk.Toplevel | None = None
        self._width = width

        entry_state = 'readonly' if self._readonly else 'normal'
        self._entry = ttk.Entry(self, textvariable=self._var,
                                state=entry_state, width=width,
                                font=self._font)
        self._entry.pack(side='left', fill='x', expand=True)
        # readonly 模式：Entry 点击即弹出（ttk.Entry readonly 不响应 Button-1 文本，
        # 需要绑定整个 widget）
        if self._readonly:
            self._entry.bind('<Button-1>', lambda _e: self._open_popup())
        else:
            # editable 模式：回车匹配 values 选中，否则触发 ComboboxSelected
            self._entry.bind('<Return>', self._on_enter)
            self._entry.bind('<Down>', lambda _e: self._open_popup())

        self._btn = ttk.Button(self, text='▾', width=2,
                              command=self._open_popup)
        self._btn.pack(side='right', padx=(2, 0))
        self._theme = None  # 由外部 apply_theme 设置

        # 对外 API
        self.get = self._var.get
        self.set = self._var.set

    # ---- editable 回车 ------------------------------------------------
    def _on_enter(self, _e):
        """editable 模式回车：匹配 values 选中，否则直接触发 ComboboxSelected。"""
        cur = self._var.get()
        for v in self._values:
            if str(v) == cur:
                break
        self.event_generate('<<ComboboxSelected>>')

    # ---- 对外兼容方法 --------------------------------------------------
    def configure(self, **kw):
        if 'values' in kw:
            self._values = list(kw.pop('values'))
        if 'state' in kw:
            s = kw.pop('state')
            self._readonly = (s == 'readonly')
            self._entry.configure(state='readonly' if self._readonly else 'normal')
            # 重新绑定
            if self._readonly:
                self._entry.unbind('<Return>')
                self._entry.bind('<Button-1>', lambda _e: self._open_popup())
            else:
                self._entry.unbind('<Button-1>')
                self._entry.bind('<Return>', self._on_enter)
                self._entry.bind('<Down>', lambda _e: self._open_popup())
        if 'font' in kw:
            self._font = kw.pop('font')
            self._entry.configure(font=self._font)
        if 'width' in kw:
            self._width = kw.pop('width')
            self._entry.configure(width=self._width)
        if kw:
            self._entry.configure(**kw)

    def cget(self, key):
        if key == 'values':
            return self._values
        return self._entry.cget(key)

    @property
    def values(self):
        return self._values

    @values.setter
    def values(self, v):
        self._values = list(v)

    # ---- trace 兼容 ----------------------------------------------------
    def trace_add(self, mode, cb):
        """模拟 StringVar.trace_add。mode 只支持 'write'。"""
        self._var.trace_add(mode, cb)

    def bind_entry(self, event, callback):
        """绑定事件到内部 Entry 控件（用于 FocusIn 等事件）。"""
        self._entry.bind(event, callback)

    # ---- 弹出下拉 -------------------------------------------------------
    def _open_popup(self):
        if self._popup is not None:
            try:
                self._popup.destroy()
            except Exception:
                pass
            self._popup = None
            return
        self.update_idletasks()
        ex = self._entry.winfo_rootx()
        ey = self._entry.winfo_rooty()
        ew = self._entry.winfo_width()
        eh = self._entry.winfo_height()
        if ew < 60:
            ew = 200

        screen_h = self.winfo_screenheight()
        screen_w = self.winfo_screenwidth()
        item_h = 22
        max_list_h = 280
        # 实际需要的高度：按 values 数量算，但不超过 max_list_h
        want_h = max(40, min(len(self._values) * item_h + 4, max_list_h))

        room_below = screen_h - (ey + eh)
        room_above = ey
        # 选能容纳 want_h 的方向；都不够就取较大的一边
        if room_below >= want_h:
            pop_y = ey + eh
            list_h = want_h
        elif room_above >= want_h:
            pop_y = ey - want_h
            list_h = want_h
        elif room_below >= room_above:
            # 下方空间多，向下弹，但高度限制为 room_below
            pop_y = ey + eh
            list_h = max(80, room_below - 2)
        else:
            # 上方空间多，向上弹，高度限制为 room_above
            pop_y = max(0, ey - room_above + 2)
            list_h = max(80, room_above - 2)

        # 水平：如果右边空间不够，向左偏移保证完全显示
        pop_x = ex
        pop_w = ew
        if pop_x + pop_w > screen_w - 4:
            pop_x = max(0, screen_w - pop_w - 4)

        top = tk.Toplevel(self)
        top.overrideredirect(True)
        top.geometry(f'{pop_w}x{list_h}+{pop_x}+{pop_y}')
        top.attributes('-topmost', True)
        # 主题色：弹出下拉背景跟随深色主题
        t = self._theme or {}
        lb_bg = t.get('panel_bg', '#ffffff')
        lb_fg = t.get('panel_fg', '#333333')
        lb_select = t.get('select_bg', '#cce5ff')
        top_bg = t.get('border', '#dcdcdc')  # 弹出框边框用 border 色
        panel_bg = t.get('panel_bg', '#ffffff')

        lb = tk.Listbox(top, selectmode='single', font=self._font,
                        activestyle='dotbox', borderwidth=0,
                        highlightthickness=0,
                        bg=lb_bg, fg=lb_fg,
                        selectbackground=lb_select,
                        selectforeground='#ffffff')
        lb.pack(fill='both', expand=True, padx=1, pady=1)
        top.configure(bg=top_bg)
        for v in self._values:
            lb.insert('end', v)
        # 当前值高亮
        cur = self._var.get()
        for i, v in enumerate(self._values):
            if str(v) == cur:
                lb.selection_set(i)
                lb.see(i)
                break

        def _pick(_e=None):
            sel = lb.curselection()
            if sel:
                self._var.set(self._values[sel[0]])
                self.event_generate('<<ComboboxSelected>>')
            self._close_popup()

        def _close(_e=None):
            self._close_popup()

        lb.bind('<Button-1>', _pick)
        lb.bind('<Return>', _pick)
        top.bind('<Escape>', _close)
        top.bind('<FocusOut>', _close)
        top.protocol('WM_DELETE_WINDOW', _close)
        # 滚轮
        lb.bind('<MouseWheel>',
                lambda e: lb.yview_scroll(int(-e.delta/120), 'units'))
        lb.bind('<Button-4>', lambda _e: lb.yview_scroll(-1, 'units'))
        lb.bind('<Button-5>', lambda _e: lb.yview_scroll(1, 'units'))

        self._popup = top
        top.focus_force()
        lb.focus_set()

    def _close_popup(self):
        if self._popup is not None:
            try:
                self._popup.destroy()
            except Exception:
                pass
            self._popup = None

    def apply_theme(self, theme: dict):
        """保存主题配色，弹出下拉时跟随。"""
        self._theme = theme
