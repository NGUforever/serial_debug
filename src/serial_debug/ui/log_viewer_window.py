"""LogViewerWindow：多文件日志查看 + 独立筛选面板 + 双向定位。

架构：
  - LogViewerWindow 顶层 ttk.PanedWindow（水平）：
    * 左：Notebook 多 Tab，每个 _LogViewTab 显示原始日志（不筛选）
    * 右：FilterPanel 筛选面板，对当前 Tab 生效
  - 筛选面板可"分离为独立窗口"：ttk.Frame reparent 到 Toplevel
  - 双向定位：
    * 主 Text 点击行 → 筛选面板 Treeview 选中最近匹配行并滚动到
    * 筛选面板 Treeview 双击行 → 主 Text 跳转到对应行

文件内搜索 Ctrl+F 和全局搜索 Ctrl+Shift+F 仍保留。
"""
from __future__ import annotations

import os
import re
import time as _time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from serial_debug.infra import config, themes
from serial_debug.ui.monitor_window import BmsMonitorWindow, ImuMonitorWindow, MotorMonitorWindow

RESP_RE = re.compile(r'^(OK[%,]|ERROR|\+)')
TS_RE = re.compile(r'^(\d{13})\b')

RENDER_BATCH = 50000
SEARCH_SCAN_LIMIT = 2_000_000
GLOBAL_SEARCH_MAX = 500
FILTER_RESULT_MAX = 50000
FILTER_INSERT_BATCH = 5000
LIVE_POLL_MS = 500  # 实时刷新轮询间隔（文件持续变化时）
LIVE_IDLE_POLL_MS = 2000  # 文件持续无新增时拉长间隔，节省 CPU
LIVE_IDLE_THRESHOLD = 6  # 连续 N 次无新增后切到 idle 间隔


class _LogViewTab(ttk.Frame):
    """单文件视图：行号 + Text + 滚动条 + 内嵌搜索条。
    只显示原始日志（不筛选），筛选由 FilterPanel 处理。
    """

    def __init__(self, parent, path: str, theme: dict,
                 on_line_click=None):
        super().__init__(parent)
        self._path = path
        self._theme = theme
        self._all_lines: list[str] = []
        self._line_tags: list[str] = []  # 预计算的行分类，供筛选快速访问
        self._rendered_count = 0
        self._search_re: re.Pattern | None = None
        self._search_matches: list[int] = []
        self._search_pos = -1
        self._auto_load = True
        self._loading = False
        # 实时刷新：监控文件新增内容，自动追加渲染
        self._live_mode = False
        self._live_poll_id: str | None = None
        self._live_pos = 0  # 文件字节偏移，从上次读取位置继续
        self._live_partial = ''  # 上次读取结尾的不完整行
        self._on_lines_appended = None  # 回调：(new_count)，通知 FilterPanel 重新筛选
        self._live_idle_count = 0  # 文件无新增计数，达阈值后拉长轮询间隔
        self._on_line_click = on_line_click  # 回调：(tab, line_idx)

        self._build_widgets()
        self.apply_theme(theme)
        self.after(50, self._load_file)

    # ---- UI 构建 -------------------------------------------------------
    def _build_widgets(self):
        # 文件信息条
        bar = ttk.Frame(self, padding=(8, 4))
        bar.pack(fill='x')
        self._info_lbl = ttk.Label(bar, text='')
        self._info_lbl.pack(side='left')

        body = ttk.Frame(self, padding=(8, 0))
        body.pack(fill='both', expand=True)

        self._line_no = tk.Text(body, width=7, wrap='none', undo=False,
                                bg=self._theme['bg'],
                                fg=self._theme['muted'],
                                takefocus=False, cursor='arrow',
                                borderwidth=0, highlightthickness=0,
                                font=themes._mono())
        self._line_no.pack(side='left', fill='y', padx=(0, 2))
        self._line_no.configure(state='disabled')

        self._text = tk.Text(body, wrap='none', undo=False,
                             bg=self._theme['panel_bg'],
                             fg=self._theme['panel_fg'],
                             insertbackground=self._theme['cursor'],
                             selectbackground=self._theme['select_bg'],
                             font=themes._mono(),
                             borderwidth=0, highlightthickness=0)
        vsb = ttk.Scrollbar(body, orient='vertical',
                            command=self._text.yview)
        self._text.pack(side='left', fill='both', expand=True)
        vsb.pack(side='right', fill='y')

        def _on_yscroll(*args):
            try:
                vsb.set(*args)
            except Exception:
                pass
            try:
                self._line_no.yview_moveto(args[0])
            except Exception:
                pass
            try:
                last = float(args[1])
            except (IndexError, ValueError, TypeError):
                last = 0.0
            if last > 0.95 and self._auto_load and not self._loading:
                self._load_more()

        self._text.configure(yscrollcommand=_on_yscroll)
        self._text.configure(state='disabled')

        def _check_bottom_and_load():
            if self._loading or not self._auto_load:
                return
            try:
                first, last = self._text.yview()
            except Exception:
                return
            if last > 0.95:
                self._load_more()
                self.after(30, self._chain_load)

        def _on_wheel(e):
            self._text.yview_scroll(int(-e.delta / 120), 'units')
            _check_bottom_and_load()

        def _jump_end(_e=None):
            self._text.yview_moveto(1.0)
            self.update_idletasks()
            # Ctrl+End 链式加载直到文件末尾
            self._chain_load()

        def _page_down(_e=None):
            self._text.yview_scroll(1, 'pages')
            self.update_idletasks()
            _check_bottom_and_load()

        def _line_down(_e=None):
            self._text.yview_scroll(3, 'units')
            self.update_idletasks()
            _check_bottom_and_load()

        self._text.bind('<MouseWheel>', _on_wheel, add='+')
        self._line_no.bind('<MouseWheel>', _on_wheel, add='+')
        # 键盘导航也触发加载
        self._text.bind('<Control-End>', _jump_end, add='+')
        self._text.bind('<End>', _jump_end, add='+')
        self._text.bind('<Next>', _page_down, add='+')
        self._text.bind('<Prior>', lambda e: self._text.yview_scroll(-1, 'pages'), add='+')
        self._text.bind('<Down>', _line_down, add='+')
        self._text.bind('<Up>', lambda e: self._text.yview_scroll(-3, 'units'), add='+')

        # 点击 Text 行 → 触发回调（用于双向定位）
        self._text.bind('<Button-1>', self._on_text_click, add='+')

        # 内嵌搜索条（默认隐藏）
        self._search_bar = ttk.Frame(self, padding=(8, 4))
        ttk.Label(self._search_bar, text='文件内搜索:').pack(side='left')
        self._search_var = tk.StringVar()
        entry = ttk.Entry(self._search_bar, textvariable=self._search_var,
                          width=40)
        entry.pack(side='left', padx=4)
        entry.bind('<Return>', lambda _e: self._search_from_start())
        entry.bind('<Shift-Return>', lambda _e: self._search_prev())
        ttk.Button(self._search_bar, text='上一个',
                   command=self._search_prev).pack(side='left', padx=2)
        ttk.Button(self._search_bar, text='下一个',
                   command=self._search_next).pack(side='left', padx=2)
        ttk.Button(self._search_bar, text='关闭',
                   command=lambda: self._show_search(False)
                   ).pack(side='left', padx=2)
        self._search_info = ttk.Label(self._search_bar, text='')
        self._search_info.pack(side='left', padx=8)

        self.bind('<Control-f>', lambda _e: self._toggle_search())
        self.bind('<Control-g>', lambda _e: self._search_next())
        self.bind('<Control-G>', lambda _e: self._search_prev())

    def _show_search(self, show: bool):
        if show:
            self._search_bar.pack(side='bottom', fill='x',
                                  before=self._text.master)
            try:
                for w in self._search_bar.winfo_children():
                    if isinstance(w, ttk.Entry):
                        w.focus_set()
                        break
            except Exception:
                pass
        else:
            self._search_bar.pack_forget()
            self._clear_search_highlight()

    def _toggle_search(self):
        if self._search_bar.winfo_ismapped():
            self._show_search(False)
        else:
            self._show_search(True)

    # ---- 文件加载 ------------------------------------------------------
    def _load_file(self):
        try:
            with open(self._path, encoding='utf-8',
                      errors='replace') as f:
                self._all_lines = f.readlines()
                self._live_pos = f.tell()  # 记录 EOF 位置，供实时刷新用
        except Exception as e:
            self._info_lbl.configure(text=f'打开失败：{e}')
            return
        self._all_lines = [ln.rstrip('\r\n') for ln in self._all_lines]
        # 预计算每行分类，避免筛选时重复 _classify
        self._line_tags = [self._classify(ln) for ln in self._all_lines]
        # 如果文件结尾不是 \n，最后一行可能不完整，移到 partial 缓存
        try:
            with open(self._path, 'rb') as fb:
                fb.seek(0, 2)
                size = fb.tell()
                if size > 0:
                    fb.seek(size - 1)
                    last_byte = fb.read(1)
                    if last_byte != b'\n' and self._all_lines:
                        self._live_partial = self._all_lines.pop()
                        self._line_tags.pop()
        except Exception:
            pass
        self._info_lbl.configure(
            text=f'{len(self._all_lines)} 行 | '
                 f'{os.path.getsize(self._path)/1024:.1f} KB')
        self._render_batch(0, RENDER_BATCH)
        # 智能检测：文件 mtime 距今 < 60 秒（说明正在被写入），自动开启实时刷新
        try:
            mtime = os.path.getmtime(self._path)
            if _time.time() - mtime < 60:
                self.set_live_mode(True)
        except Exception:
            pass

    def _render_batch(self, start: int, count: int):
        total = len(self._all_lines)
        end = min(start + count, total)
        if start >= end:
            return
        text = self._text
        line_no = self._line_no
        tags = self._line_tags
        all_lines = self._all_lines
        text.configure(state='normal')
        line_no.configure(state='normal')
        # 批量构造文本，减少 Tk 调用次数
        for line_idx in range(start, end):
            raw_line = all_lines[line_idx]
            tag = tags[line_idx] if line_idx < len(tags) else self._classify(raw_line)
            line_no.insert('end', f'{line_idx + 1:>6}\n', 'muted')
            line_disp = self._convert_ts(raw_line)
            text.insert('end', line_disp + '\n', tag)
        text.configure(state='disabled')
        line_no.configure(state='disabled')
        self._rendered_count = end
        if total - end > 0:
            text.configure(state='normal')
            line_no.configure(state='normal')
            text.insert('end',
                        f'… 还有 {total - end} 行未加载，滚动到底自动加载 '
                        f'(已渲染 {self._rendered_count})\n',
                        ('note', 'placeholder'))
            line_no.insert('end', '\n', 'muted')
            line_no.configure(state='disabled')
            text.configure(state='disabled')

    def _delete_trailing_placeholder(self):
        """删除末尾占位符行（… 还有 N 行未加载...）。

        Tk Text 末尾总保留一个空行；旧实现用 `end-1c` 计算行号会落到空行
        而非占位符行，导致 delete 只删空行、占位符残留，后续渲染把新行
        追加到占位符之后，造成行号错位。改用 placeholder tag 范围删除：
        只删 tag 覆盖的占位符行本身，保留末尾空行，避免 insert('end')
        把新内容拼到上一行末尾。
        """
        text = self._text
        line_no = self._line_no
        text.configure(state='normal')
        line_no.configure(state='normal')
        try:
            ranges = text.tag_ranges('placeholder')
            if ranges and len(ranges) >= 2:
                start_idx, end_idx = str(ranges[0]), str(ranges[1])
                text.delete(start_idx, end_idx)
                # line_no 对应行：占位符那行是空（line_no 插的是 '\n'）
                line_no.delete(start_idx, end_idx)
        except Exception:
            pass
        text.configure(state='disabled')
        line_no.configure(state='disabled')

    def _load_more(self):
        if not self._auto_load or self._loading:
            return
        total = len(self._all_lines)
        if self._rendered_count >= total:
            return
        self._loading = True
        try:
            self._delete_trailing_placeholder()
            self._render_batch(self._rendered_count, RENDER_BATCH)
            # 加载后跳到新底部，让用户看到新内容 + 新占位符
            self._text.yview_moveto(1.0)
            self.update_idletasks()
            self._text.yview_moveto(1.0)
        finally:
            self._loading = False

    def _chain_load(self):
        """连续加载多批，直到用户离开底部或全部加载完。"""
        if self._loading or not self._auto_load:
            return
        total = len(self._all_lines)
        if self._rendered_count >= total:
            return
        try:
            first, last = self._text.yview()
        except Exception:
            return
        if last < 0.95:
            return  # 用户已离开底部，停止
        # 直接连续加载多批，用 update_idletasks 让 UI 不卡死
        # 每批加载后检查是否还在底部，不在就停
        batch_budget = 20  # 每次回调最多加载 20 批，避免长时间占用主线程
        for _ in range(batch_budget):
            if self._rendered_count >= total:
                break
            try:
                first, last = self._text.yview()
            except Exception:
                break
            if last < 0.95:
                break
            self._load_more()
            self.update_idletasks()
        # 还有剩余且仍在底部 → 通过 after 让出主线程后继续
        if self._rendered_count < total:
            try:
                first, last = self._text.yview()
            except Exception:
                return
            if last >= 0.95:
                self.after(1, self._chain_load)

    # ---- 实时刷新 ------------------------------------------------------
    def set_live_mode(self, enabled: bool):
        """开启/关闭实时刷新。开启后会定期轮询文件新增内容。"""
        if enabled == self._live_mode:
            return
        self._live_mode = enabled
        if enabled:
            # 开启时立即轮询一次，然后定时
            self._poll_file()
        else:
            if self._live_poll_id is not None:
                try:
                    self.after_cancel(self._live_poll_id)
                except Exception:
                    pass
            self._live_poll_id = None

    def is_live_mode(self) -> bool:
        return self._live_mode

    def _poll_file(self):
        """轮询文件新增内容，追加到 _all_lines 并渲染（如果用户在底部）。"""
        if not self._live_mode:
            self._live_poll_id = None
            return
        new_count = 0
        try:
            with open(self._path, encoding='utf-8',
                      errors='replace') as f:
                f.seek(self._live_pos)
                new_text = f.read()
                self._live_pos = f.tell()
        except Exception:
            self._schedule_next_poll()
            return
        if new_text:
            # 拼接上次留下的 partial 行
            full = self._live_partial + new_text
            self._live_partial = ''
            lines = full.split('\n')
            # 处理 \r（Windows 行尾 \r\n 已经被 split 后 \r 留在每行尾）
            if not new_text.endswith('\n'):
                # 最后一段是不完整行，缓存起来
                self._live_partial = lines[-1]
                lines = lines[:-1]
            # 追加完整行
            for ln in lines:
                if ln.endswith('\r'):
                    ln = ln[:-1]
                self._all_lines.append(ln)
                self._line_tags.append(self._classify(ln))
                new_count += 1
                # 同步喂给已打开的数据监视窗口（电机/BMS/IMU），
                # 让曲线/数值随实时日志一起更新
                try:
                    parent = self.winfo_toplevel()
                    feed = getattr(parent, 'feed_monitor_from_line', None)
                    if feed is not None:
                        feed(ln)
                except Exception:
                    pass
        if new_count > 0:
            # 更新信息条
            self._info_lbl.configure(
                text=f'{len(self._all_lines)} 行 | '
                     f'{os.path.getsize(self._path)/1024:.1f} KB'
                     + (' [实时]' if self._live_mode else ''))
            # 通知 FilterPanel 重新筛选
            if self._on_lines_appended:
                try:
                    self._on_lines_appended(new_count)
                except Exception:
                    pass
            # 渲染新行（仅当用户在底部且已全部渲染完旧内容）
            try:
                first, last = self._text.yview()
                at_bottom = last > 0.95
            except Exception:
                at_bottom = False
            # all_old_rendered: 在新增之前，旧内容是否已全部渲染
            old_total = len(self._all_lines) - new_count
            all_old_rendered = self._rendered_count >= old_total
            if all_old_rendered and at_bottom and not self._loading:
                # 删除旧占位符（如有），追加新行
                self._delete_trailing_placeholder()
                # 渲染新增的行（可能 total - _rendered_count > new_count，因为之前可能有占位符）
                self._render_batch(self._rendered_count,
                                   len(self._all_lines) - self._rendered_count)
                # 自动滚到底
                self._text.yview_moveto(1.0)
                self.update_idletasks()
                self._text.yview_moveto(1.0)
        # 空闲计数：有新增则重置，无新增则累计，达到阈值后拉长轮询间隔
        if new_count > 0:
            self._live_idle_count = 0
        else:
            self._live_idle_count += 1
        self._schedule_next_poll()

    def _schedule_next_poll(self):
        if not self._live_mode:
            self._live_poll_id = None
            return
        interval = (LIVE_IDLE_POLL_MS
                    if self._live_idle_count >= LIVE_IDLE_THRESHOLD
                    else LIVE_POLL_MS)
        self._live_poll_id = self.after(interval, self._poll_file)

    def stop_live(self):
        """销毁前清理轮询定时器。"""
        self._live_mode = False
        if self._live_poll_id is not None:
            try:
                self.after_cancel(self._live_poll_id)
            except Exception:
                pass
            self._live_poll_id = None

    # ---- 分类 / 转换 ---------------------------------------------------
    @staticmethod
    def _classify(line: str) -> str:
        if RESP_RE.match(line):
            return 'resp'
        low = line.lower()
        for kw in ('error', 'fail', 'warn', 'exception', 'critical'):
            if kw in low:
                return 'note'
        return 'log'

    @staticmethod
    def _convert_ts(line: str) -> str:
        m = TS_RE.match(line)
        if not m:
            return line
        ts_ms = int(m.group(1))
        sec, ms = divmod(ts_ms, 1000)
        try:
            t = _time.localtime(sec)
            readable = _time.strftime('%H:%M:%S', t) + f'.{ms:03d}'
            return f'[{readable}] {line}'
        except Exception:
            return line

    # ---- 点击定位 ------------------------------------------------------
    def _on_text_click(self, event):
        """点击 Text → 计算行号 → 通知回调。"""
        try:
            index = self._text.index(f'@{event.x},{event.y}')
            line_no = int(index.split('.')[0])
            # Text 行号 = 文件行号 + 1（因为文件第一行渲染到 Text 第 1 行）
            line_idx = line_no - 1
            if 0 <= line_idx < len(self._all_lines) and self._on_line_click:
                self._on_line_click(self, line_idx)
        except Exception:
            pass

    def get_line(self, idx: int) -> str:
        if 0 <= idx < len(self._all_lines):
            return self._all_lines[idx]
        return ''

    @property
    def all_lines(self) -> list[str]:
        return self._all_lines

    # ---- 跳转 ----------------------------------------------------------
    def goto_line(self, line_idx: int):
        if not (0 <= line_idx < len(self._all_lines)):
            return
        # 若未渲染，加载到包含该行的批次
        if line_idx >= self._rendered_count:
            self._delete_trailing_placeholder()
            batch_end = ((line_idx // RENDER_BATCH) + 1) * RENDER_BATCH
            self._render_batch(self._rendered_count,
                               batch_end - self._rendered_count)
        text_line = line_idx + 1
        self._text.see(f'{text_line}.0')
        # 短暂高亮
        self._text.tag_remove('goto_hit', '1.0', 'end')
        self._text.tag_add('goto_hit', f'{text_line}.0',
                           f'{text_line}.end')
        # 3 秒后清除高亮
        try:
            self.after(3000, lambda: self._text.tag_remove(
                'goto_hit', '1.0', 'end'))
        except Exception:
            pass

    # ---- 文件内搜索 ----------------------------------------------------
    def _search_from_start(self):
        kw = self._search_var.get()
        if not kw:
            return
        try:
            self._search_re = re.compile(kw)
        except re.error as e:
            messagebox.showerror('正则错误', f'{e}', parent=self)
            return
        self._do_search()
        self._search_next()

    def _do_search(self):
        if self._search_re is None:
            return
        matches = []
        lines = self._all_lines
        limit = min(len(lines), SEARCH_SCAN_LIMIT)
        for i in range(limit):
            if self._search_re.search(lines[i]):
                matches.append(i)
                if len(matches) >= GLOBAL_SEARCH_MAX * 4:
                    break
        self._search_matches = matches
        self._search_pos = -1
        self._clear_search_highlight()

    def _search_next(self):
        if not self._search_matches:
            self._do_search()
        if not self._search_matches:
            self._search_info.configure(text='无匹配')
            return
        self._search_pos = (self._search_pos + 1) % len(self._search_matches)
        self._goto_match(self._search_pos)

    def _search_prev(self):
        if not self._search_matches:
            self._do_search()
        if not self._search_matches:
            self._search_info.configure(text='无匹配')
            return
        self._search_pos = (self._search_pos - 1) % len(self._search_matches)
        self._goto_match(self._search_pos)

    def _goto_match(self, pos: int):
        line_idx = self._search_matches[pos]
        self.goto_line(line_idx)
        # 在 goto_line 高亮基础上再加子串高亮
        text_line = line_idx + 1
        line_str = self._all_lines[line_idx]
        if self._search_re is not None:
            for m in self._search_re.finditer(line_str):
                self._text.tag_add('search_sub',
                                   f'{text_line}.{m.start()}',
                                   f'{text_line}.{m.end()}')
        self._search_info.configure(
            text=f'{pos + 1} / {len(self._search_matches)}')

    def _clear_search_highlight(self):
        self._text.tag_remove('search_hit', '1.0', 'end')
        self._text.tag_remove('search_sub', '1.0', 'end')
        self._text.tag_remove('goto_hit', '1.0', 'end')

    # ---- 全局搜索 ------------------------------------------------------
    def global_search(self, pattern: re.Pattern) -> list[tuple[int, int, str]]:
        results = []
        limit = min(len(self._all_lines), SEARCH_SCAN_LIMIT)
        for i in range(limit):
            m = pattern.search(self._all_lines[i])
            if m:
                preview = self._all_lines[i][:100]
                results.append((i, m.start(), preview))
                if len(results) >= GLOBAL_SEARCH_MAX:
                    break
        return results

    @property
    def path(self) -> str:
        return self._path

    @property
    def filename(self) -> str:
        return os.path.basename(self._path)

    # ---- 主题 ----------------------------------------------------------
    def apply_theme(self, theme: dict):
        self._theme = theme
        self._text.configure(bg=theme['panel_bg'],
                             fg=theme['panel_fg'],
                             insertbackground=theme['cursor'],
                             selectbackground=theme['select_bg'],
                             font=themes._mono())
        self._line_no.configure(bg=theme['bg'], fg=theme['muted'],
                                font=themes._mono())
        self._text.tag_config('log', foreground=theme['log'])
        self._text.tag_config('ts', foreground=theme['ts'])
        self._text.tag_config('resp', foreground=theme['resp'])
        self._text.tag_config('note', foreground=theme['note'])
        self._text.tag_config('search_hit',
                             background=theme['select_bg'],
                             foreground='#ffffff')
        self._text.tag_config('search_sub',
                             background=theme['status_wait'],
                             foreground='#000000')
        self._text.tag_config('goto_hit',
                             background=theme['status_wait'],
                             foreground='#000000')
        self._line_no.tag_config('muted', foreground=theme['muted'])


class _FilterPanel(ttk.Frame):
    """筛选面板：过滤条件 + 结果 Treeview，可双向定位主 Text。

    数据源：当前 Tab 的 _LogViewTab.all_lines
    on_locate(line_idx) 回调：双击 Treeview 行时通知主窗口跳转
    """

    def __init__(self, parent, theme: dict, on_locate=None):
        super().__init__(parent)
        self._theme = theme
        self._tab: _LogViewTab | None = None
        self._filtered_idx: list[int] = []
        self._last_scanned_idx = 0   # 增量筛选游标：已扫描到哪一行
        self._on_locate = on_locate
        self._build_widgets()
        self.apply_theme(theme)

    def _build_widgets(self):
        bar = ttk.Frame(self, padding=(8, 6))
        bar.pack(fill='x')

        ttk.Label(bar, text='过滤').pack(side='left', padx=(0, 4))
        self._filter_var = tk.StringVar(value='全部')
        fb = ttk.Combobox(bar, textvariable=self._filter_var, width=12,
                          values=['全部', '日志', 'AT 响应', '警告/错误'],
                          state='readonly')
        fb.pack(side='left')
        fb.bind('<<ComboboxSelected>>', lambda _e: self._apply_filter())

        ttk.Label(bar, text='正则').pack(side='left', padx=(8, 4))
        self._regex_var = tk.StringVar()
        entry = ttk.Entry(bar, textvariable=self._regex_var, width=20)
        entry.pack(side='left')
        entry.bind('<Return>', lambda _e: self._apply_filter())
        ttk.Button(bar, text='应用', command=self._apply_filter
                   ).pack(side='left', padx=2)
        ttk.Button(bar, text='清除', command=self._clear_filter
                   ).pack(side='left')

        self._info_lbl = ttk.Label(bar, text='')
        self._info_lbl.pack(side='right')

        # 结果 Treeview
        tree_frame = ttk.Frame(self, padding=(4, 0))
        tree_frame.pack(fill='both', expand=True)
        self._tree = ttk.Treeview(
            tree_frame, columns=('line', 'preview', 'tag'),
            show='tree headings', height=15)
        self._tree.heading('#0', text='行号')
        self._tree.heading('line', text='文件行')
        self._tree.heading('preview', text='内容预览')
        self._tree.heading('tag', text='类型')
        self._tree.column('#0', width=50, stretch=False)
        self._tree.column('line', width=70, stretch=False, anchor='e')
        self._tree.column('preview', width=400)
        self._tree.column('tag', width=80, stretch=False)
        vsb = ttk.Scrollbar(tree_frame, orient='vertical',
                            command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        self._tree.pack(side='left', fill='both', expand=True)
        vsb.pack(side='right', fill='y')
        self._tree.bind('<Double-1>', self._on_tree_double_click)

    # ---- 数据绑定 ------------------------------------------------------
    def set_tab(self, tab: _LogViewTab | None):
        self._tab = tab
        if tab is None:
            self._clear_results()
            self._info_lbl.configure(text='无打开的文件')
            return
        # 绑定实时刷新回调：新行到来时自动增量筛选
        tab._on_lines_appended = self._on_lines_appended
        self._apply_filter()

    def _on_lines_appended(self, _new_count: int):
        """实时模式下新行追加后触发：增量筛选只扫描新增行，避免全量 O(N)。"""
        self._apply_filter_incremental()

    def _clear_results(self):
        for iid in self._tree.get_children():
            self._tree.delete(iid)
        self._filtered_idx = []
        self._last_scanned_idx = 0

    def _apply_filter(self):
        """全量筛选：扫描所有行，用于初次加载、用户切换过滤类型/正则时。"""
        if self._tab is None:
            return
        ftype = self._filter_var.get()
        regex = self._regex_var.get().strip()
        try:
            pat = re.compile(regex) if regex else None
        except re.error as e:
            messagebox.showerror('正则错误', f'{e}', parent=self)
            return
        lines = self._tab.all_lines
        tags = self._tab._line_tags if hasattr(self._tab, '_line_tags') else None
        filtered = []
        # 类型预筛
        if ftype == 'AT 响应':
            type_check = lambda i: RESP_RE.match(lines[i])
        elif ftype == '警告/错误':
            type_check = lambda i: (tags[i] if tags else _LogViewTab._classify(lines[i])) == 'note'
        elif ftype == '日志':
            type_check = lambda i: (tags[i] if tags else _LogViewTab._classify(lines[i])) == 'log'
        else:
            type_check = lambda i: True
        # 主循环：类型 + 正则
        for i in range(len(lines)):
            if not type_check(i):
                continue
            if pat is not None and not pat.search(lines[i]):
                continue
            filtered.append(i)
            if len(filtered) >= FILTER_RESULT_MAX:
                break
        self._render_results(filtered, len(lines))
        self._last_scanned_idx = len(lines)
        suffix = '' if len(filtered) < FILTER_RESULT_MAX else f'（仅显示前 {FILTER_RESULT_MAX} 条）'
        self._info_lbl.configure(
            text=f'{len(filtered)} / {len(lines)} 行 {suffix}')

    def _apply_filter_incremental(self):
        """增量筛选：实时模式新行到来时，只扫描 [last_scanned, end) 区间，
        匹配的行追加到 Treeview，避免全量 O(N) 重扫几十万行。"""
        if self._tab is None:
            return
        ftype = self._filter_var.get()
        regex = self._regex_var.get().strip()
        try:
            pat = re.compile(regex) if regex else None
        except re.error:
            return  # 正则错误时不增量，下次全量重试会弹错
        lines = self._tab.all_lines
        tags = self._tab._line_tags if hasattr(self._tab, '_line_tags') else None
        start = self._last_scanned_idx
        if start >= len(lines):
            return
        # 类型预筛
        if ftype == 'AT 响应':
            type_check = lambda i: RESP_RE.match(lines[i])
        elif ftype == '警告/错误':
            type_check = lambda i: (tags[i] if (tags and i < len(tags)) else _LogViewTab._classify(lines[i])) == 'note'
        elif ftype == '日志':
            type_check = lambda i: (tags[i] if (tags and i < len(tags)) else _LogViewTab._classify(lines[i])) == 'log'
        else:
            type_check = lambda i: True
        new_filtered = []
        cap = FILTER_RESULT_MAX - len(self._filtered_idx)
        for i in range(start, len(lines)):
            if not type_check(i):
                continue
            if pat is not None and not pat.search(lines[i]):
                continue
            new_filtered.append(i)
            if len(new_filtered) >= cap:
                break
        self._last_scanned_idx = len(lines)
        if new_filtered:
            self._filtered_idx.extend(new_filtered)
            self._insert_into_tree(new_filtered, lines, tags)
        suffix = '' if len(self._filtered_idx) < FILTER_RESULT_MAX else f'（仅显示前 {FILTER_RESULT_MAX} 条）'
        self._info_lbl.configure(
            text=f'{len(self._filtered_idx)} / {len(lines)} 行 {suffix}')

    def _insert_into_tree(self, idx_list: list[int],
                          lines: list[str], tags):
        """把指定行索引批量插入 Treeview（增量追加，不先 clear）。"""
        def _insert_batch(start: int):
            end = min(start + FILTER_INSERT_BATCH, len(idx_list))
            for k in range(start, end):
                i = idx_list[k]
                ln = lines[i] if i < len(lines) else ''
                tag = tags[i] if (tags and i < len(tags)) else _LogViewTab._classify(ln)
                preview = ln.replace('\t', ' ')[:120]
                try:
                    self._tree.insert(
                        '', 'end', iid=f'L{i}',
                        text=f'{i+1}',
                        values=(i + 1, preview, tag), tags=(tag,))
                except tk.TclError:
                    pass  # iid 已存在
            if end < len(idx_list):
                self.after(10, lambda: _insert_batch(end))
        _insert_batch(0)

    def _render_results(self, filtered: list[int], total: int = 0):
        self._clear_results()
        self._filtered_idx = filtered
        lines = self._tab.all_lines if self._tab else []
        tags = self._tab._line_tags if (self._tab and hasattr(self._tab, '_line_tags')) else None
        # 分批插入，避免一次性卡死 UI
        def _insert_batch(start: int):
            end = min(start + FILTER_INSERT_BATCH, len(filtered))
            for k in range(start, end):
                i = filtered[k]
                ln = lines[i] if i < len(lines) else ''
                tag = tags[i] if (tags and i < len(tags)) else _LogViewTab._classify(ln)
                preview = ln.replace('\t', ' ')[:120]
                try:
                    self._tree.insert(
                        '', 'end', iid=f'L{i}',
                        text=f'{i+1}',
                        values=(i + 1, preview, tag), tags=(tag,))
                except tk.TclError:
                    pass  # iid 已存在
            if end < len(filtered):
                self.after(10, lambda: _insert_batch(end))
        _insert_batch(0)

    def _clear_filter(self):
        self._filter_var.set('全部')
        self._regex_var.set('')
        self._apply_filter()

    # ---- 双向定位 ------------------------------------------------------
    def _on_tree_double_click(self, _event):
        sel = self._tree.selection()
        if not sel:
            return
        iid = sel[0]
        # iid 格式 'L{idx}'，idx 为文件行号（0-based）
        try:
            line_idx = int(iid[1:])
        except ValueError:
            return
        if self._on_locate:
            self._on_locate(line_idx)

    def locate_line(self, line_idx: int):
        """主 Text 点击时调用：在结果 Treeview 中选中最近的匹配行并滚动。"""
        if not self._filtered_idx:
            return
        # 找 filtered_idx 中 <= line_idx 的最大值
        import bisect
        pos = bisect.bisect_right(self._filtered_idx, line_idx) - 1
        if pos < 0:
            pos = 0
        target_idx = self._filtered_idx[pos]
        iid = f'L{target_idx}'
        try:
            self._tree.selection_set(iid)
            self._tree.see(iid)
            self._tree.focus(iid)
        except Exception:
            pass

    # ---- 主题 ----------------------------------------------------------
    def apply_theme(self, theme: dict):
        self._theme = theme
        try:
            self._tree.tag_configure('log',
                                     background=theme['panel_bg'],
                                     foreground=theme['log'])
            self._tree.tag_configure('resp',
                                     background=theme['panel_bg'],
                                     foreground=theme['resp'])
            self._tree.tag_configure('note',
                                     background=theme['panel_bg'],
                                     foreground=theme['note'])
        except Exception:
            pass


class LogViewerWindow(tk.Toplevel):
    """多文件日志查看窗口：Notebook + FilterPanel（可分离为独立窗口）。"""

    def __init__(self, parent, path: str | None = None, theme=None):
        super().__init__(parent)
        self.title('日志查看')
        # 优先用持久化的几何
        try:
            saved_geo = config.load().get('log_view_geometry', '')
        except Exception:
            saved_geo = ''
        if saved_geo:
            try:
                self.geometry(saved_geo)
            except Exception:
                self.geometry('1400x800')
        else:
            self.geometry('1400x800')
        self.minsize(900, 540)

        self._theme = theme or themes.THEMES['dark']
        self._tabs: list[_LogViewTab] = []
        # 分离的 FilterPanel Toplevel（None 表示嵌入式）
        self._filter_popout: tk.Toplevel | None = None
        self._filter_panel: _FilterPanel | None = None
        # 数据监视独立窗口单例（电机 / BMS / IMU）
        self._motor_monitor: MotorMonitorWindow | None = None
        self._bms_monitor: BmsMonitorWindow | None = None
        self._imu_monitor: ImuMonitorWindow | None = None

        self._build_toolbar()
        self._build_menubar()
        self._build_paned()
        self._build_global_search_bar()
        # 应用持久化的视图状态（工具栏显隐）
        self._load_view_state()

        self.protocol('WM_DELETE_WINDOW', self._on_close)
        try:
            self.lift(parent)
            self.focus_force()
        except Exception:
            pass

        if path:
            self.open_file(path)

        self.bind('<Control-f>', lambda _e: self._focus_current_search())
        self.bind('<Control-o>', lambda _e: self._open_file_dialog())
        self.bind('<Control-Shift-F>',
                  lambda _e: self._show_global_search(True))
        self.bind('<Escape>', lambda _e: self._show_global_search(False))
        # 跳到末尾的快捷键在 Toplevel 层绑定，不依赖 text 焦点
        self.bind('<Control-End>', lambda _e: self._jump_to_end())
        self.bind('<Control-Key-End>', lambda _e: self._jump_to_end())
        self.bind('<Control-w>', lambda _e: self._on_close())

    # ---- UI 构建 -------------------------------------------------------
    def _build_toolbar(self):
        bar = ttk.Frame(self, padding=(8, 6))
        bar.pack(fill='x')
        self._toolbar = bar
        ttk.Button(bar, text='打开文件…',
                   command=self._open_file_dialog).pack(side='left')
        ttk.Button(bar, text='关闭当前 Tab',
                   command=self._close_current_tab).pack(side='left', padx=4)
        ttk.Separator(bar, orient='vertical').pack(
            side='left', fill='y', padx=6)
        ttk.Button(bar, text='分离筛选窗口',
                   command=self._popout_filter).pack(side='left')
        ttk.Button(bar, text='嵌入筛选窗口',
                   command=self._embed_filter).pack(side='left', padx=4)
        ttk.Separator(bar, orient='vertical').pack(
            side='left', fill='y', padx=6)
        ttk.Button(bar, text='全局搜索',
                   command=lambda: self._show_global_search(True)
                   ).pack(side='left')
        ttk.Separator(bar, orient='vertical').pack(
            side='left', fill='y', padx=6)
        ttk.Button(bar, text='跳到末尾',
                   command=self._jump_to_end).pack(side='left')
        ttk.Separator(bar, orient='vertical').pack(
            side='left', fill='y', padx=6)
        self._live_btn = ttk.Button(bar, text='实时刷新',
                                    command=self._toggle_live_mode)
        self._live_btn.pack(side='left')
        ttk.Separator(bar, orient='vertical').pack(
            side='left', fill='y', padx=6)
        # 数据监视窗口入口（与串口调试助手工具栏一致）
        ttk.Button(bar, text='电机监视',
                   command=self._open_motor_monitor).pack(side='left')
        ttk.Button(bar, text='BMS 监视',
                   command=self._open_bms_monitor).pack(side='left', padx=4)
        ttk.Button(bar, text='IMU 监视',
                   command=self._open_imu_monitor).pack(side='left')
        ttk.Label(bar, text='（Ctrl+Shift+F 全局，Ctrl+F 文件内搜索）',
                 foreground=self._theme['muted']
                 ).pack(side='left', padx=8)
        self._info_lbl = ttk.Label(bar, text='')
        self._info_lbl.pack(side='right')

    def _toggle_live_mode(self):
        """切换当前 Tab 的实时刷新模式。"""
        tab = self._current_tab()
        if tab is None:
            return
        new_mode = not tab.is_live_mode()
        tab.set_live_mode(new_mode)
        self._update_live_btn()

    def _update_live_btn(self):
        """根据当前 Tab 实时模式更新按钮文字。"""
        tab = self._current_tab()
        if tab is None:
            return
        try:
            if tab.is_live_mode():
                self._live_btn.configure(text='停止刷新')
            else:
                self._live_btn.configure(text='实时刷新')
        except Exception:
            pass

    def _jump_to_end(self):
        """跳到当前 Tab 的文件末尾，链式加载直到全部加载完。"""
        tab = self._current_tab()
        if tab is None:
            return
        try:
            tab._text.yview_moveto(1.0)
            self.update_idletasks()
            tab._chain_load()
        except Exception:
            pass

    def _build_menubar(self):
        """菜单栏：文件 / 视图 / 工具 / 帮助。

        工具菜单覆盖工具栏全部功能：搜索 / 跳转 / 实时刷新 / 筛选窗口分离嵌入 /
        数据监视三窗口（电机 / BMS / IMU）。
        """
        menubar = tk.Menu(self)
        self._menubar = menubar
        # 文件
        m_file = tk.Menu(menubar, tearoff=False)
        m_file.add_command(label='打开文件…', accelerator='Ctrl+O',
                           command=self._open_file_dialog)
        m_file.add_command(label='关闭当前 Tab',
                           command=self._close_current_tab)
        m_file.add_separator()
        m_file.add_command(label='关闭窗口', accelerator='Ctrl+W',
                           command=self._on_close)
        menubar.add_cascade(label='文件', menu=m_file)

        # 视图
        m_view = tk.Menu(menubar, tearoff=False)
        self._view_toolbar_var = tk.BooleanVar(value=True)
        m_view.add_checkbutton(
            label='工具栏', variable=self._view_toolbar_var,
            command=self._toggle_toolbar)
        m_view.add_separator()
        m_view.add_command(label='分离筛选窗口',
                           command=self._popout_filter)
        m_view.add_command(label='嵌入筛选窗口',
                           command=self._embed_filter)
        m_view.add_separator()
        m_view.add_command(label='切换实时刷新',
                           command=self._toggle_live_mode)
        m_view.add_command(label='跳到末尾',
                           command=self._jump_to_end)
        menubar.add_cascade(label='视图', menu=m_view)

        # 工具
        m_tools = tk.Menu(menubar, tearoff=False)
        m_tools.add_command(label='全局搜索',
                            accelerator='Ctrl+Shift+F',
                            command=lambda: self._show_global_search(True))
        m_tools.add_command(label='文件内搜索',
                            accelerator='Ctrl+F',
                            command=self._focus_current_search)
        m_tools.add_separator()
        m_tools.add_command(label='分离筛选窗口',
                            command=self._popout_filter)
        m_tools.add_command(label='嵌入筛选窗口',
                            command=self._embed_filter)
        m_tools.add_separator()
        m_tools.add_command(label='切换实时刷新',
                            command=self._toggle_live_mode)
        m_tools.add_command(label='跳到末尾',
                            command=self._jump_to_end)
        m_tools.add_separator()
        # 数据监视窗口（与串口调试助手工具栏一致）
        m_tools.add_command(label='电机监视',
                            command=self._open_motor_monitor)
        m_tools.add_command(label='BMS 监视',
                            command=self._open_bms_monitor)
        m_tools.add_command(label='IMU 监视',
                            command=self._open_imu_monitor)
        menubar.add_cascade(label='工具', menu=m_tools)

        # 帮助
        m_help = tk.Menu(menubar, tearoff=False)
        m_help.add_command(label='关于', command=self._show_about)
        menubar.add_cascade(label='帮助', menu=m_help)

        try:
            self.configure(menu=menubar)
        except Exception:
            pass
        # 菜单主题
        try:
            t = self._theme
            themes.apply_menu_theme(menubar, t)
        except Exception:
            pass

    def _toggle_toolbar(self):
        """显示/隐藏工具栏。"""
        try:
            if self._view_toolbar_var.get():
                self._toolbar.pack(fill='x', before=self._paned)
            else:
                self._toolbar.pack_forget()
        except Exception:
            pass

    def _load_view_state(self):
        """从 config 读取工具栏显隐并应用。"""
        try:
            cfg = config.load()
        except Exception:
            return
        try:
            show_tb = cfg.get('log_view_show_toolbar', True)
            self._view_toolbar_var.set(bool(show_tb))
            if not show_tb:
                self._toolbar.pack_forget()
        except Exception:
            pass

    def _save_state(self):
        """关闭前持久化视图状态 + 几何。"""
        try:
            cfg = config.load()
        except Exception:
            cfg = {}
        try:
            cfg['log_view_show_toolbar'] = bool(
                self._view_toolbar_var.get())
            try:
                cfg['log_view_geometry'] = self.geometry()
            except Exception:
                pass
        except Exception:
            pass
        try:
            config.save(cfg)
        except Exception:
            pass

    def _show_about(self):
        dlg = tk.Toplevel(self)
        dlg.title('关于')
        try:
            dlg.lift(self)
            dlg.focus_force()
        except Exception:
            pass
        dlg.resizable(True, True)
        f = ttk.Frame(dlg, padding=20)
        f.pack(fill='both', expand=True)
        ttk.Label(f, text='日志查看器', font=('', 11, 'bold')
                  ).pack(pady=(0, 6))
        ttk.Label(f, text='多文件 Tab + 筛选面板 + 全局搜索 + 数据监视'
                  ).pack()
        ttk.Button(f, text='关闭', command=dlg.destroy
                   ).pack(pady=(10, 0))

    def _build_paned(self):
        # 外层 PanedWindow：左 Notebook + 右 FilterPanel
        self._paned = ttk.PanedWindow(self, orient='horizontal')
        self._paned.pack(fill='both', expand=True)

        # 左：Notebook
        self._notebook = ttk.Notebook(self._paned)
        self._paned.add(self._notebook, weight=3)
        self._notebook.bind('<<NotebookTabChanged>>',
                            lambda _e: self._on_tab_changed())

        # 右：FilterPanel（嵌入式）
        self._filter_panel = _FilterPanel(
            self._paned, self._theme,
            on_locate=self._on_filter_locate)
        self._paned.add(self._filter_panel, weight=1)

    def _build_global_search_bar(self):
        bar = ttk.Frame(self, padding=(8, 4))
        self._gs_bar = bar
        ttk.Label(bar, text='全局搜索:').pack(side='left')
        self._gs_var = tk.StringVar()
        entry = ttk.Entry(bar, textvariable=self._gs_var, width=40)
        entry.pack(side='left', padx=4)
        entry.bind('<Return>', lambda _e: self._do_global_search())
        ttk.Button(bar, text='搜索',
                   command=self._do_global_search).pack(side='left', padx=2)
        ttk.Button(bar, text='关闭',
                   command=lambda: self._show_global_search(False)
                   ).pack(side='left', padx=2)
        self._gs_info = ttk.Label(bar, text='')
        self._gs_info.pack(side='left', padx=8)
        list_frame = ttk.Frame(bar)
        list_frame.pack(fill='x', side='top', pady=(4, 0))
        self._gs_list = ttk.Treeview(list_frame,
                                     columns=('file', 'line', 'preview'),
                                     show='headings', height=8)
        self._gs_list.heading('file', text='文件')
        self._gs_list.heading('line', text='行号')
        self._gs_list.heading('preview', text='预览')
        self._gs_list.column('file', width=160, stretch=False)
        self._gs_list.column('line', width=70, stretch=False, anchor='e')
        self._gs_list.column('preview', width=700)
        self._gs_list.pack(fill='x')
        self._gs_list.bind('<Double-1>', self._on_gs_double_click)
        bar.pack_forget()

    # ---- 文件 / Tab 管理 ------------------------------------------------
    def open_file(self, path: str):
        for tab in self._tabs:
            if tab.path == path:
                self._notebook.select(tab)
                return
        tab = _LogViewTab(self._notebook, path, self._theme,
                          on_line_click=self._on_text_click)
        self._notebook.add(tab, text=tab.filename)
        self._tabs.append(tab)
        self._notebook.select(tab)
        self._info_lbl.configure(text=f'已打开 {len(self._tabs)} 个文件')
        # 当前 Tab 变化 → FilterPanel 切换数据源
        self._on_tab_changed()

    def _open_file_dialog(self):
        cfg = config.load()
        init_dir = cfg.get('log_view_last_dir', '') or os.path.expanduser('~')
        paths = filedialog.askopenfilenames(
            title='打开日志文件（可多选）', parent=self,
            initialdir=init_dir,
            filetypes=[('文本文件', '*.txt'), ('日志', '*.log'),
                       ('所有文件', '*.*')])
        if paths:
            new_dir = os.path.dirname(paths[0])
            if new_dir and new_dir != cfg.get('log_view_last_dir'):
                cfg['log_view_last_dir'] = new_dir
                config.save(cfg)
        for p in paths:
            self.open_file(p)

    def _close_current_tab(self):
        current = self._notebook.select()
        if not current:
            return
        for tab in self._tabs[:]:
            if str(tab) == current:
                try:
                    tab.stop_live()
                except Exception:
                    pass
                self._notebook.forget(tab)
                self._tabs.remove(tab)
                tab.destroy()
                break
        self._info_lbl.configure(text=f'已打开 {len(self._tabs)} 个文件')
        self._on_tab_changed()

    def _current_tab(self) -> _LogViewTab | None:
        current = self._notebook.select()
        for tab in self._tabs:
            if str(tab) == current:
                return tab
        return None

    def _on_tab_changed(self):
        tab = self._current_tab()
        if self._filter_panel is not None:
            self._filter_panel.set_tab(tab)
        if self._filter_popout is not None and self._filter_panel is not None:
            # 分离模式下也同步（filter_panel 已经在 popout 里）
            self._filter_panel.set_tab(tab)
        self._update_live_btn()

    # ---- 双向定位 ------------------------------------------------------
    def _on_text_click(self, tab: _LogViewTab, line_idx: int):
        """主 Text 点击行 → FilterPanel 定位到最近匹配行。"""
        if self._filter_panel is not None:
            self._filter_panel.locate_line(line_idx)

    def _on_filter_locate(self, line_idx: int):
        """FilterPanel 双击 → 主 Text 跳转到对应行。"""
        tab = self._current_tab()
        if tab is None:
            return
        tab.goto_line(line_idx)

    # ---- 分离 / 嵌入筛选窗口 -------------------------------------------
    def _popout_filter(self):
        """把 FilterPanel 从 PanedWindow 中取出，放到独立 Toplevel。"""
        if self._filter_popout is not None or self._filter_panel is None:
            return  # 已分离
        # 销毁嵌入式 FilterPanel
        self._paned.forget(self._filter_panel)
        self._filter_panel.destroy()
        self._filter_panel = None
        # 创建独立 Toplevel
        top = tk.Toplevel(self)
        top.title('日志筛选')
        top.geometry('700x600')
        top.minsize(400, 300)
        try:
            top.configure(bg=self._theme['bg'])
        except Exception:
            pass
        top.transient(self)
        try:
            top.lift(self)
        except Exception:
            pass
        panel = _FilterPanel(top, self._theme,
                             on_locate=self._on_filter_locate)
        panel.pack(fill='both', expand=True)
        # 同步当前 Tab 数据源
        tab = self._current_tab()
        panel.set_tab(tab)
        panel.apply_theme(self._theme)
        # 关闭时回到嵌入
        def _on_top_close():
            self._filter_popout = None
            try:
                top.destroy()
            except Exception:
                pass
            # 重建嵌入式 FilterPanel
            self._filter_panel = _FilterPanel(
                self._paned, self._theme,
                on_locate=self._on_filter_locate)
            self._paned.add(self._filter_panel, weight=1)
            tab2 = self._current_tab()
            self._filter_panel.set_tab(tab2)
            self._filter_panel.apply_theme(self._theme)
        top.protocol('WM_DELETE_WINDOW', _on_top_close)
        self._filter_popout = top
        self._filter_panel = panel

    def _embed_filter(self):
        """手动从分离模式回到嵌入。"""
        if self._filter_popout is None:
            return
        # 触发 Toplevel 关闭回调（会自动重建嵌入式 panel）
        try:
            self._filter_popout.destroy()
        except Exception:
            pass
        # _on_top_close 在 WM_DELETE_WINDOW 时已调用，这里手动补一遍
        if self._filter_panel is None or self._filter_panel.winfo_ismapped():
            # 重建嵌入式
            if self._filter_panel is None:
                self._filter_panel = _FilterPanel(
                    self._paned, self._theme,
                    on_locate=self._on_filter_locate)
                self._paned.add(self._filter_panel, weight=1)
                self._filter_panel.set_tab(self._current_tab())
                self._filter_panel.apply_theme(self._theme)
        self._filter_popout = None

    # ---- 全局搜索 ------------------------------------------------------
    def _show_global_search(self, show: bool):
        if show:
            self._gs_bar.pack(fill='x', side='top',
                              before=self._paned)
            try:
                for w in self._gs_bar.winfo_children():
                    if isinstance(w, ttk.Entry):
                        w.focus_set()
                        break
            except Exception:
                pass
        else:
            self._gs_bar.pack_forget()

    def _do_global_search(self):
        kw = self._gs_var.get()
        if not kw:
            return
        try:
            pat = re.compile(kw)
        except re.error as e:
            messagebox.showerror('正则错误', f'{e}', parent=self)
            return
        for iid in self._gs_list.get_children():
            self._gs_list.delete(iid)
        total = 0
        for tab in self._tabs:
            results = tab.global_search(pat)
            for line_idx, col, preview in results:
                p = preview.replace('\n', ' ').replace('\t', ' ')[:120]
                self._gs_list.insert(
                    'end', values=(tab.filename, line_idx + 1, p))
                total += 1
                if total >= GLOBAL_SEARCH_MAX:
                    break
            if total >= GLOBAL_SEARCH_MAX:
                break
        self._gs_info.configure(
            text=f'{total} 个结果（最多 {GLOBAL_SEARCH_MAX}）')

    def _on_gs_double_click(self, _event):
        sel = self._gs_list.selection()
        if not sel:
            return
        vals = self._gs_list.set(sel[0])
        fname = vals.get('file', '')
        try:
            line_no = int(vals.get('line', '1')) - 1
        except ValueError:
            line_no = 0
        for tab in self._tabs:
            if tab.filename == fname or tab.path.endswith(fname):
                self._notebook.select(tab)
                tab.goto_line(line_no)
                return

    # ---- 文件内搜索聚焦 ------------------------------------------------
    def _focus_current_search(self):
        tab = self._current_tab()
        if tab is not None:
            tab._show_search(True)

    # ---- 主题 ----------------------------------------------------------
    def apply_theme(self, theme: dict):
        self._theme = theme
        self.configure(bg=theme['bg'])
        for tab in self._tabs:
            try:
                tab.apply_theme(theme)
            except Exception:
                pass
        if self._filter_panel is not None:
            try:
                self._filter_panel.apply_theme(theme)
            except Exception:
                pass

    def update_theme(self):
        try:
            t = themes.THEMES[themes.resolve(themes.load_pref())]
            self.apply_theme(t)
        except Exception:
            pass

    # ---- 关闭 ----------------------------------------------------------
    def _on_close(self):
        try:
            self._save_state()
        except Exception:
            pass
        if self._filter_popout is not None:
            try:
                self._filter_popout.destroy()
            except Exception:
                pass
            self._filter_popout = None
        for win in (self._motor_monitor, self._bms_monitor,
                    self._imu_monitor):
            if win is not None:
                try:
                    win._on_close()
                except Exception:
                    try:
                        win.destroy()
                    except Exception:
                        pass
        self._motor_monitor = None
        self._bms_monitor = None
        self._imu_monitor = None
        for tab in self._tabs:
            tab._all_lines = []
            tab._search_matches = []
        self._tabs = []
        self.destroy()

    # ---- 数据监视窗口（电机 / BMS / IMU）------------------------------
    def _open_motor_monitor(self):
        """打开电机数据监视独立窗口（单例）。"""
        if (self._motor_monitor is not None
                and self._motor_monitor.winfo_exists()):
            self._motor_monitor.lift()
            self._motor_monitor.focus_force()
            return
        try:
            cfg = config.load()
        except Exception:
            cfg = {}
        self._motor_monitor = MotorMonitorWindow(
            self,
            variables=cfg.get('motor_vars', []),
            plots=cfg.get('motor_plots', []))
        self._motor_monitor.update_theme()
        # 先让窗口立刻渲染出来，再用 after 在下一事件循环 tick 重放日志，
        # 避免大日志回放阻塞窗口显示（用户感觉"打开特别慢"）
        try:
            self._motor_monitor.update_idletasks()
        except Exception:
            pass
        self._motor_monitor.after(
            0, lambda: self._replay_current_tab_to_monitor(
                self._motor_monitor))

    def _open_bms_monitor(self):
        if (self._bms_monitor is not None
                and self._bms_monitor.winfo_exists()):
            self._bms_monitor.lift()
            self._bms_monitor.focus_force()
            return
        try:
            cfg = config.load()
        except Exception:
            cfg = {}
        self._bms_monitor = BmsMonitorWindow(
            self,
            variables=cfg.get('bms_vars', []),
            plots=cfg.get('bms_plots', []))
        self._bms_monitor.update_theme()
        try:
            self._bms_monitor.update_idletasks()
        except Exception:
            pass
        self._bms_monitor.after(
            0, lambda: self._replay_current_tab_to_monitor(
                self._bms_monitor))

    def _open_imu_monitor(self):
        if (self._imu_monitor is not None
                and self._imu_monitor.winfo_exists()):
            self._imu_monitor.lift()
            self._imu_monitor.focus_force()
            return
        try:
            cfg = config.load()
        except Exception:
            cfg = {}
        self._imu_monitor = ImuMonitorWindow(
            self,
            variables=cfg.get('imu_vars', []),
            plots=cfg.get('imu_plots', []))
        self._imu_monitor.update_theme()
        try:
            self._imu_monitor.update_idletasks()
        except Exception:
            pass
        self._imu_monitor.after(
            0, lambda: self._replay_current_tab_to_monitor(
                self._imu_monitor))

    def _replay_current_tab_to_monitor(self, win):
        """把当前 Tab 已加载的全部日志行重放给新打开的监视窗口，
        让用户在打开窗口后立刻看到曲线/数值，无需等新日志产生。

        分块重放：每 tick 处理 CHUNK 行，让出主线程响应窗口绘制与
        用户操作。一次性 feed 完整 lines 会阻塞 UI 数十秒（大日志），
        用户感觉"打开特别慢"。
        """
        tab = self._current_tab()
        if tab is None or win is None:
            return
        try:
            lines = tab._all_lines
            feed = win.feed
        except Exception:
            return
        # 每块 5000 行：在中等机器上约 30~50ms，UI 仍能响应
        CHUNK = 5000
        state = {'i': 0}

        def _tick():
            # 窗口被关则停止
            if not win.winfo_exists():
                return
            end = min(state['i'] + CHUNK, len(lines))
            try:
                for line in lines[state['i']:end]:
                    feed(line)
            except Exception:
                pass
            state['i'] = end
            # 主动让出主线程：下一 tick 在 1ms 后继续，期间可处理 expose
            if state['i'] < len(lines):
                win.after(1, _tick)
        _tick()

    def feed_monitor_from_line(self, line: str):
        """把一行日志同步喂给所有已打开的监视窗口。

        由 _LogViewTab 在实时刷新追加新行时调用，让监视窗口跟着
        实时日志一起更新曲线。
        """
        for win in (self._motor_monitor, self._bms_monitor,
                    self._imu_monitor):
            if win is not None and win.winfo_exists():
                try:
                    win.feed(line)
                except Exception:
                    pass


if __name__ == '__main__':
    import sys
    root = tk.Tk()
    root.withdraw()
    cfg = config.load()
    init_dir = cfg.get('log_view_last_dir', '') or os.path.expanduser('~')
    paths = filedialog.askopenfilenames(
        title='打开日志文件（可多选）',
        initialdir=init_dir,
        filetypes=[('文本文件', '*.txt'), ('日志', '*.log'),
                   ('所有文件', '*.*')])
    if not paths:
        sys.exit(0)
    new_dir = os.path.dirname(paths[0])
    if new_dir and new_dir != cfg.get('log_view_last_dir'):
        cfg['log_view_last_dir'] = new_dir
        config.save(cfg)
    pref = themes.load_pref()
    t = themes.THEMES[themes.resolve(pref)]
    themes.apply_theme(root, themes.resolve(pref))
    win = LogViewerWindow(root, path=None, theme=t)
    for p in paths:
        win.open_file(p)
    win.protocol('WM_DELETE_WINDOW',
                 lambda: (win._on_close(), root.destroy()))
    root.mainloop()
