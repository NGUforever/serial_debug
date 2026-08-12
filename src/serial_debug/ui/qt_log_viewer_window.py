"""Qt 日志查看器（PySide6 版），独立子进程启动。

与 tkinter 版 log_viewer_window.py 功能等价：多 Tab + 行号 + 大文件按批
渲染 + 滚动加载更多 + 文件内搜索 + 全局搜索 + 实时刷新 + 筛选面板
（嵌入/分离）+ 双向定位 + 视图状态持久化 + 主题切换 + 电机/BMS/IMU
监视入口 + 菜单/工具栏/快捷键。

独立子进程启动：环境变量 SERIAL_DEBUG_LOG_VIEWER=1 让 main() 走此路径，
避免与 tkinter 主程序双事件循环冲突。
"""
from __future__ import annotations

import os
import re
import sys
import time
from bisect import bisect_right
from datetime import datetime

from PySide6.QtCore import Qt, QTimer, QRect, QSize, QPoint
from PySide6.QtGui import (QColor, QFont, QTextCharFormat, QTextCursor,
                            QAction, QKeySequence, QPainter, QShortcut,
                            QTextBlockUserData)
from PySide6.QtWidgets import (QApplication, QFileDialog, QMainWindow,
                                QPlainTextEdit, QWidget, QHBoxLayout,
                                QVBoxLayout, QToolBar, QLabel, QLineEdit,
                                QPushButton, QStatusBar, QMessageBox,
                                QTabWidget, QComboBox,
                                QTreeWidget, QTreeWidgetItem,
                                QDockWidget, QSplitter, QCheckBox,
                                QHeaderView, QStyle, QStyleOptionViewItem,
                                QToolButton)

from serial_debug.infra import themes, config
from serial_debug.ui import qt_theme
from serial_debug.ui.qt_monitor_window import _iter_panel_vars


# ---- 常量（与 tkinter 版一致）----------------------------------------
RESP_RE = re.compile(r'^(OK[%,]|ERROR|\+)')
TS_RE = re.compile(r'^(\d{13})\b')
RENDER_BATCH = 50000
SEARCH_SCAN_LIMIT = 2_000_000
GLOBAL_SEARCH_MAX = 500
FILTER_RESULT_MAX = 50000
FILTER_INSERT_BATCH = 5000
LIVE_POLL_MS = 500
LIVE_IDLE_POLL_MS = 2000
LIVE_IDLE_THRESHOLD = 6

_NOTE_KEYWORDS = ('error', 'fail', 'warn', 'exception', 'critical')


def _classify(line: str) -> str:
    if RESP_RE.match(line):
        return 'resp'
    low = line.lower()
    for kw in _NOTE_KEYWORDS:
        if kw in low:
            return 'note'
    return 'log'


def _convert_ts(line: str) -> str:
    """13 位 ms 时间戳 → [HH:MM:SS.mmm] 前缀。失败返回空串。"""
    m = TS_RE.match(line)
    if not m:
        return ''
    try:
        ms = int(m.group(1))
    except ValueError:
        return ''
    dt = datetime.fromtimestamp(ms / 1000.0)
    return dt.strftime('[%H:%M:%S.') + f'{ms % 1000:03d}] '


# ---- 行号区 ----------------------------------------------------------
class _LineNumberArea(QWidget):
    """QPlainTextEdit 左侧行号自绘区。

    paintEvent 遍历可见 QTextBlock，按 blockBoundingRect 算 y，画行号文字。
    跟随 QPlainTextEdit 滚动同步（updateRequest 信号触发 update）。
    """

    def __init__(self, editor):
        super().__init__(editor)
        self._editor = editor
        self._font = None

    def sizeHint(self):
        return QSize(self._editor.line_number_area_width(), 0)

    def paintEvent(self, event):
        # QPainter(self) 才能绑到 _LineNumberArea 这个 QWidget 上画
        # 之前误写 QPainter(event.rect()) → PySide6 6.11 直接抛 TypeError
        # 导致行号区从来没画过 → 左侧出现一列空白
        painter = QPainter(self)
        t = self._editor._theme
        painter.fillRect(event.rect(), QColor(t.get('bg', '#1e1e1e')))
        painter.setPen(QColor(t.get('muted', '#858585')))
        if self._font is None:
            self._font = QFont(qt_theme.QT_MONO_FAMILY, qt_theme.QT_MONO_SIZE)
        painter.setFont(self._font)
        block = self._editor.firstVisibleBlock()
        block_number = block.blockNumber() if block.isValid() else 0
        top = int(self._editor.blockBoundingGeometry(block).top()
                  + self._editor.contentOffset().y())
        bottom = top + int(self._editor.blockBoundingRect(block).height())
        width = self.width()
        viewport_height = self._editor.viewport().height()
        while block.isValid() and top <= viewport_height:
            if block.isVisible() and bottom >= 0:
                painter.drawText(
                    QRect(0, top, width - 6, bottom - top),
                    Qt.AlignRight | Qt.AlignVCenter,
                    str(block_number + 1))
            block = block.next()
            top = bottom
            bottom = top + int(self._editor.blockBoundingRect(block).height())
            block_number += 1


class _PlainTextEdit(QPlainTextEdit):
    """带行号区的 QPlainTextEdit。

    line_number_area_width 根据总行数位数算宽度；resizeEvent 调整行号区几何；
    updateRequest 信号触发行号区重绘（滚动 + 内容变更）。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._theme = themes.THEMES['dark']
        self._line_area = _LineNumberArea(self)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        # 不限块数（大文件按需渲染，setMaximumBlockCount 会截断旧内容）
        self.setMaximumBlockCount(0)
        self.setCenterOnScroll(False)
        self.blockCountChanged.connect(self._update_line_area_width)
        self.updateRequest.connect(self._update_line_area)
        self._update_line_area_width()

    def set_theme(self, theme):
        self._theme = theme
        # 文字/背景色
        pal = self.palette()
        pal.setColor(pal.ColorRole.Base, QColor(theme.get('panel_bg', '#1e1e1e')))
        pal.setColor(pal.ColorRole.Text, QColor(theme.get('panel_fg', '#cccccc')))
        pal.setColor(pal.ColorRole.Highlight,
                     QColor(theme.get('select_bg', '#264f78')))
        pal.setColor(pal.ColorRole.HighlightedText, QColor('#ffffff'))
        self.setPalette(pal)
        font = QFont(qt_theme.QT_MONO_FAMILY, qt_theme.QT_MONO_SIZE)
        self.setFont(font)
        self._line_area.update()

    def line_number_area_width(self):
        n = max(1, self.blockCount())
        digits = max(3, len(str(n)))
        # 字符宽度 ~7px @ 10pt mono，左 padding 4 + 右 padding 6
        return 8 * digits + 12

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cr = self.contentsRect()
        self._line_area.setGeometry(
            QRect(cr.left(), cr.top(),
                  self.line_number_area_width(), cr.height()))

    def _update_line_area_width(self):
        self.setViewportMargins(self.line_number_area_width(), 0, 0, 0)

    def _update_line_area(self, rect, dy):
        if dy:
            self._line_area.scroll(0, dy)
        else:
            self._line_area.update(
                0, rect.y(), self._line_area.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_line_area_width()

    def first_visible_block(self):
        return self.firstVisibleBlock()

    def _editor(self):
        return self


# ---- 占位符 userData -------------------------------------------------
class _PlaceholderData(QTextBlockUserData):
    """QTextBlock userData 标记"未加载更多"占位行。"""
    pass


# ---- 单 Tab 视图 -----------------------------------------------------
class _QtLogViewTab(QWidget):
    """单文件视图：行号区 + QPlainTextEdit + 内嵌搜索条。

    按批渲染 RENDER_BATCH 行，滚动到底部自动加载下一批。
    Ctrl+F 文件内搜索 + 高亮 + 上一个/下一个。
    """

    def __init__(self, parent, path, theme, on_line_click=None):
        super().__init__(parent)
        self._path = path
        self._theme = theme
        self._all_lines: list[str] = []
        self._line_tags: list[str] = []
        self._rendered_count = 0
        self._search_re: re.Pattern | None = None
        self._search_matches: list[int] = []
        self._search_pos = -1
        self._auto_load = True
        self._loading = False
        self._placeholder_block: int = -1  # 占位符 block number
        # 实时刷新
        self._live_mode = False
        self._live_timer: QTimer | None = None
        self._live_pos = 0
        self._live_partial = ''
        self._live_idle_count = 0
        self._live_last_mtime = 0
        self._on_lines_appended = None
        self._on_line_click = on_line_click
        # 监视窗口 feed 回调（由父窗口注入）
        self._feed_monitor_cb = None

        self._build_widgets()
        self.apply_theme(theme)
        QTimer.singleShot(50, self._load_file)

    # ---- UI 构建 ------------------------------------------------------
    def _build_widgets(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(8, 4, 8, 4)
        v.setSpacing(4)
        self._info_lbl = QLabel('')
        self._info_lbl.setStyleSheet(
            f'color: {self._theme.get("muted", "#858585")};')
        v.addWidget(self._info_lbl)

        self._editor = _PlainTextEdit()
        self._editor.set_theme(self._theme)
        # 点击行 → 回调
        self._editor.viewport().installEventFilter(self)
        v.addWidget(self._editor, stretch=1)

        # 内嵌搜索条
        self._search_bar = QWidget()
        sl = QHBoxLayout(self._search_bar)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(4)
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText('搜索（回车下一个，Shift+回车上一个）')
        self._search_edit.returnPressed.connect(self._on_search_return)
        sl.addWidget(self._search_edit, stretch=1)
        self._search_prev = QPushButton('上一个')
        self._search_prev.clicked.connect(self._search_prev_match)
        sl.addWidget(self._search_prev)
        self._search_next = QPushButton('下一个')
        self._search_next.clicked.connect(self._search_next_match)
        sl.addWidget(self._search_next)
        self._search_close = QPushButton('×')
        self._search_close.clicked.connect(lambda: self._show_search(False))
        sl.addWidget(self._search_close)
        self._search_info = QLabel('')
        self._search_info.setStyleSheet(
            f'color: {self._theme.get("muted", "#858585")};')
        sl.addWidget(self._search_info)
        self._search_bar.setVisible(False)
        v.addWidget(self._search_bar)

        # 快捷键
        self._shortcut_find = QShortcut(QKeySequence('Ctrl+F'), self)
        self._shortcut_find.activated.connect(lambda: self._show_search(True))
        self._shortcut_find_esc = QShortcut(QKeySequence('Escape'), self)
        self._shortcut_find_esc.activated.connect(
            lambda: self._show_search(False))
        self._shortcut_next = QShortcut(QKeySequence('Ctrl+G'), self)
        self._shortcut_next.activated.connect(self._search_next_match)
        self._shortcut_prev = QShortcut(
            QKeySequence('Shift+Ctrl+G'), self)
        self._shortcut_prev.activated.connect(self._search_prev_match)
        self._shortcut_end = QShortcut(QKeySequence('Ctrl+End'), self)
        self._shortcut_end.activated.connect(self._jump_to_end)

        # 滚动到底自动加载
        sb = self._editor.verticalScrollBar()
        sb.valueChanged.connect(self._on_scroll)

    def eventFilter(self, obj, event):
        from PySide6.QtCore import QEvent
        if obj is self._editor.viewport() and event.type() == QEvent.MouseButtonPress:
            pos = event.position().toPoint() if hasattr(event, 'position') else event.pos()
            self._on_text_click(pos)
        return super().eventFilter(obj, event)

    # ---- 主题 ---------------------------------------------------------
    def apply_theme(self, theme):
        self._theme = theme
        self._editor.set_theme(theme)
        self._info_lbl.setStyleSheet(
            f'color: {theme.get("muted", "#858585")}; background: transparent;')
        self._search_info.setStyleSheet(
            f'color: {theme.get("muted", "#858585")}; background: transparent;')
        # 重建已渲染文本的 tag 颜色
        self._apply_tag_formats()

    def _apply_tag_formats(self):
        """对已渲染文本按 _line_tags 重新应用 QTextCharFormat。"""
        cursor = self._editor.textCursor()
        cursor.beginEditBlock()
        try:
            cursor.select(QTextCursor.Document)
            cursor.setCharFormat(self._default_fmt())
            for i in range(min(self._rendered_count, len(self._line_tags))):
                tag = self._line_tags[i]
                fmt = self._format_for_tag(tag)
                block = self._editor.document().findBlockByNumber(i)
                if block.isValid():
                    cur = QTextCursor(block)
                    cur.select(QTextCursor.LineUnderCursor)
                    cur.setCharFormat(fmt)
        finally:
            cursor.endEditBlock()

    def _default_fmt(self):
        t = self._theme
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(t.get('panel_fg', '#cccccc')))
        return fmt

    def _format_for_tag(self, tag):
        t = self._theme
        fmt = QTextCharFormat()
        if tag == 'resp':
            fmt.setForeground(QColor(t.get('accent', '#569cd6')))
        elif tag == 'note':
            fmt.setForeground(QColor(t.get('status_err', '#f48771')))
        else:
            fmt.setForeground(QColor(t.get('panel_fg', '#cccccc')))
        return fmt

    # ---- 文件加载 -----------------------------------------------------
    def _load_file(self):
        try:
            with open(self._path, 'r', encoding='utf-8',
                       errors='replace') as f:
                lines = f.readlines()
        except Exception as e:
            self._info_lbl.setText(f'加载失败：{e}')
            return
        # 处理末尾不完整行（live 模式可能继续追加）
        if lines and not lines[-1].endswith('\n'):
            self._live_partial = lines[-1].rstrip('\n')
            lines = lines[:-1]
        else:
            self._live_partial = ''
        self._all_lines = [ln.rstrip('\r\n') for ln in lines]
        self._line_tags = [_classify(ln) for ln in self._all_lines]
        try:
            size = os.path.getsize(self._path)
        except Exception:
            size = 0
        self._info_lbl.setText(
            f'{self._path} | {len(self._all_lines)} 行 | {size/1024:.1f} KB')
        # 渲染首批
        self._render_batch(0, RENDER_BATCH)
        # mtime < 60s 自动开 live
        try:
            mtime = os.path.getmtime(self._path)
            if time.time() - mtime < 60:
                self.set_live_mode(True)
        except Exception:
            pass

    def _render_batch(self, start, count):
        """批量插入 [start, start+count) 行，末尾插占位符标记未加载部分。"""
        end = min(start + count, len(self._all_lines))
        if start >= end and self._rendered_count >= len(self._all_lines):
            return
        self._loading = True
        # 先删尾占位符
        self._delete_trailing_placeholder()
        cursor = QTextCursor(self._editor.document())
        cursor.movePosition(QTextCursor.End)
        # 批量 appendPlainText：每行带 tag
        lines_to_insert = self._all_lines[start:end]
        tags_to_insert = self._line_tags[start:end]
        cursor.beginEditBlock()
        for i, ln in enumerate(lines_to_insert):
            tag = tags_to_insert[i]
            fmt = self._format_for_tag(tag)
            # 时间戳前缀转换：原始行替换行首 13 位 ts → [HH:MM:SS.mmm]
            display = _convert_ts(ln) + ln if _convert_ts(ln) else ln
            cursor.insertText(display + '\n', fmt)
        # 末尾占位符（如果还有未加载）
        if end < len(self._all_lines):
            placeholder_text = f'--- 还有 {len(self._all_lines) - end} 行未加载，滚到底部自动加载 ---'
            # 不加 '\n'：避免 lastBlock() 返回 '\n' 创建的空尾块，导致
            # setUserData 设到空块上、_delete_trailing_placeholder 删不掉
            # 真正的占位文本，残留行让 findBlockByNumber 后续全部偏移 +1
            cursor.insertText(placeholder_text)
            cur_block = self._editor.document().lastBlock()
            cur_block.setUserData(_PlaceholderData())
            self._placeholder_block = cur_block.blockNumber()
        cursor.endEditBlock()
        self._rendered_count = end
        self._loading = False

    def _delete_trailing_placeholder(self):
        """删除尾部占位符块（用 userData 标记定位）。"""
        doc = self._editor.document()
        if self._placeholder_block < 0:
            # 兜底：扫最后几块
            block = doc.lastBlock()
            while block.isValid():
                if isinstance(block.userData(), _PlaceholderData):
                    self._placeholder_block = block.blockNumber()
                    break
                block = block.previous()
                if block.blockNumber() < max(0, doc.blockCount() - 3):
                    break
        if self._placeholder_block < 0:
            return
        block = doc.findBlockByNumber(self._placeholder_block)
        if not block.isValid():
            self._placeholder_block = -1
            return
        if not isinstance(block.userData(), _PlaceholderData):
            return
        cursor = QTextCursor(block)
        cursor.select(QTextCursor.LineUnderCursor)
        cursor.removeSelectedText()
        # 删尾部换行
        cursor.deleteChar()
        # 清掉 stale userData：占位文本已删，块可能被后续 insertText 复用，
        # 不清的话 _delete_trailing_placeholder 兜底扫描会误删新内容
        block.setUserData(None)
        self._placeholder_block = -1

    # ---- 滚动 + 加载更多 ---------------------------------------------
    def _on_scroll(self, value):
        if self._loading:
            return
        sb = self._editor.verticalScrollBar()
        if sb.maximum() == 0:
            return
        if value >= sb.maximum() * 0.95:
            if self._rendered_count < len(self._all_lines):
                self._chain_load()

    def _chain_load(self, budget=20):
        """连续加载最多 budget 批，每批 RENDER_BATCH 行；QTimer.singleShot(0) 让出主线程。"""
        if self._loading or budget <= 0:
            return
        if self._rendered_count >= len(self._all_lines):
            return
        self._render_batch(self._rendered_count, RENDER_BATCH)
        if self._rendered_count < len(self._all_lines):
            QTimer.singleShot(0, lambda: self._chain_load(budget - 1))

    def _jump_to_end(self):
        """跳到末尾：先加载完所有内容，再滚到底。"""
        if self._rendered_count < len(self._all_lines):
            self._chain_load(budget=9999)
        sb = self._editor.verticalScrollBar()
        sb.setValue(sb.maximum())
        self._editor.ensureCursorVisible()

    # ---- 行点击 -------------------------------------------------------
    def _on_text_click(self, pos):
        """点击行 → 回调 (tab, line_idx)。"""
        cursor = self._editor.cursorForPosition(pos)
        block_number = cursor.blockNumber()
        if block_number < 0 or block_number >= self._rendered_count:
            return
        if self._on_line_click:
            try:
                self._on_line_click(self, block_number)
            except Exception:
                pass

    def get_line(self, idx: int) -> str:
        if 0 <= idx < len(self._all_lines):
            return self._all_lines[idx]
        return ''

    def all_lines(self) -> list[str]:
        return self._all_lines

    def path(self) -> str:
        return self._path

    def filename(self) -> str:
        return os.path.basename(self._path)

    def goto_line(self, line_idx: int):
        """跳转到指定行：未渲染则按 RENDER_BATCH 块加载到包含该行，see + 高亮。"""
        if line_idx < 0 or line_idx >= len(self._all_lines):
            return
        if line_idx >= self._rendered_count:
            # 加载到包含该行
            while self._rendered_count <= line_idx:
                self._render_batch(self._rendered_count, RENDER_BATCH)
                if self._rendered_count >= len(self._all_lines):
                    break
        block = self._editor.document().findBlockByNumber(line_idx)
        if not block.isValid():
            return
        cursor = QTextCursor(block)
        self._editor.setTextCursor(cursor)
        self._editor.ensureCursorVisible()
        # goto_hit 高亮 3s
        fmt = QTextCharFormat()
        fmt.setBackground(QColor(self._theme.get('select_bg', '#264f78')))
        cursor.select(QTextCursor.LineUnderCursor)
        cursor.setCharFormat(fmt)
        QTimer.singleShot(3000, lambda: self._clear_goto_highlight(line_idx))

    def _clear_goto_highlight(self, line_idx):
        block = self._editor.document().findBlockByNumber(line_idx)
        if not block.isValid():
            return
        cursor = QTextCursor(block)
        cursor.select(QTextCursor.LineUnderCursor)
        cursor.setCharFormat(self._format_for_tag(self._line_tags[line_idx]))

    # ---- 文件内搜索 ---------------------------------------------------
    def _show_search(self, show: bool):
        self._search_bar.setVisible(show)
        if show:
            self._search_edit.setFocus()
            self._search_edit.selectAll()

    def _on_search_return(self):
        mods = QApplication.keyboardModifiers()
        if mods & Qt.ShiftModifier:
            self._search_prev_match()
        else:
            self._search_next_match()

    def _search_next_match(self):
        if not self._search_matches:
            self._do_search()
        if not self._search_matches:
            self._search_info.setText('无匹配')
            return
        self._search_pos = (self._search_pos + 1) % len(self._search_matches)
        self._goto_match(self._search_matches[self._search_pos])
        self._search_info.setText(
            f'{self._search_pos + 1}/{len(self._search_matches)}')

    def _search_prev_match(self):
        if not self._search_matches:
            self._do_search()
        if not self._search_matches:
            self._search_info.setText('无匹配')
            return
        self._search_pos = (self._search_pos - 1) % len(self._search_matches)
        self._goto_match(self._search_matches[self._search_pos])
        self._search_info.setText(
            f'{self._search_pos + 1}/{len(self._search_matches)}')

    def _do_search(self):
        text = self._search_edit.text()
        if not text:
            self._search_matches = []
            self._search_info.setText('')
            return
        try:
            self._search_re = re.compile(text)
        except re.error:
            self._search_info.setText('正则错误')
            self._search_matches = []
            return
        matches: list[int] = []
        scanned = 0
        for i, ln in enumerate(self._all_lines):
            if scanned > SEARCH_SCAN_LIMIT:
                break
            scanned += 1
            if self._search_re.search(ln):
                matches.append(i)
                if len(matches) >= GLOBAL_SEARCH_MAX * 4:
                    break
        self._search_matches = matches
        self._search_pos = -1

    def _goto_match(self, line_idx: int):
        self.goto_line(line_idx)
        # 子串高亮
        if self._search_re is None:
            return
        block = self._editor.document().findBlockByNumber(line_idx)
        if not block.isValid():
            return
        text = block.text()
        fmt = QTextCharFormat()
        fmt.setBackground(QColor(self._theme.get('accent', '#0e639c')))
        fmt.setForeground(QColor('#ffffff'))
        cursor = QTextCursor(block)
        cursor.select(QTextCursor.LineUnderCursor)
        cursor.setCharFormat(self._format_for_tag(self._line_tags[line_idx]))
        # 在行内搜子串
        for m in self._search_re.finditer(text):
            start = m.start()
            end = m.end()
            cur = QTextCursor(block)
            cur.setPosition(block.position() + start)
            cur.setPosition(block.position() + end, QTextCursor.KeepAnchor)
            cur.setCharFormat(fmt)

    def global_search(self, pattern: re.Pattern) -> list[tuple[int, int, str]]:
        """返回 [(line_idx, col, preview)]，cap GLOBAL_SEARCH_MAX。"""
        out: list[tuple[int, int, str]] = []
        scanned = 0
        for i, ln in enumerate(self._all_lines):
            if scanned > SEARCH_SCAN_LIMIT:
                break
            scanned += 1
            m = pattern.search(ln)
            if m:
                preview = ln[:100]
                out.append((i, m.start(), preview))
                if len(out) >= GLOBAL_SEARCH_MAX:
                    break
        return out

    # ---- 实时刷新 -----------------------------------------------------
    def set_live_mode(self, enabled: bool):
        if enabled == self._live_mode:
            return
        self._live_mode = enabled
        if enabled:
            self._live_pos = self._file_size()
            try:
                self._live_last_mtime = os.path.getmtime(self._path)
            except Exception:
                self._live_last_mtime = 0
            self._live_idle_count = 0
            if self._live_timer is None:
                self._live_timer = QTimer(self)
                self._live_timer.timeout.connect(self._poll_file)
            self._live_timer.start(LIVE_POLL_MS)
        else:
            if self._live_timer is not None:
                self._live_timer.stop()

    def is_live_mode(self) -> bool:
        return self._live_mode

    def stop_live(self):
        if self._live_timer is not None:
            self._live_timer.stop()
            self._live_timer = None
        self._live_mode = False

    def _file_size(self):
        try:
            return os.path.getsize(self._path)
        except Exception:
            return 0

    def _poll_file(self):
        if self._live_pos == 0 and not self._all_lines:
            # 还没加载过，先全量加载
            self._load_file()
            return
        try:
            cur_size = self._file_size()
            cur_mtime = os.path.getmtime(self._path)
        except Exception:
            return
        if cur_size < self._live_pos:
            # 文件被截断或轮转，重置
            self._live_pos = 0
            self._all_lines = []
            self._line_tags = []
            self._rendered_count = 0
            self._editor.clear()
            self._load_file()
            return
        if cur_size == self._live_pos:
            self._live_idle_count += 1
            if self._live_idle_count >= LIVE_IDLE_THRESHOLD:
                self._live_timer.start(LIVE_IDLE_POLL_MS)
            return
        self._live_idle_count = 0
        self._live_timer.start(LIVE_POLL_MS)
        try:
            with open(self._path, 'r', encoding='utf-8',
                       errors='replace') as f:
                f.seek(self._live_pos)
                chunk = f.read(cur_size - self._live_pos)
        except Exception:
            return
        self._live_pos = cur_size
        self._live_last_mtime = cur_mtime
        self._live_partial += chunk
        if '\n' not in self._live_partial:
            return
        parts = self._live_partial.split('\n')
        self._live_partial = parts[-1]
        new_lines = parts[:-1]
        new_count = 0
        for ln in new_lines:
            ln = ln.rstrip('\r')
            if not ln:
                continue
            self._all_lines.append(ln)
            self._line_tags.append(_classify(ln))
            new_count += 1
            # 监视窗口 feed
            if self._feed_monitor_cb:
                try:
                    self._feed_monitor_cb(ln + '\n')
                except Exception:
                    pass
        if new_count == 0:
            return
        # 追加渲染（若用户在底部，则追加；否则只更新数据）
        sb = self._editor.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() * 0.95
        # 先删尾占位符
        self._delete_trailing_placeholder()
        cursor = QTextCursor(self._editor.document())
        cursor.movePosition(QTextCursor.End)
        cursor.beginEditBlock()
        start = self._rendered_count
        end = min(len(self._all_lines), self._rendered_count + new_count)
        for i in range(start, end):
            ln = self._all_lines[i]
            tag = self._line_tags[i]
            fmt = self._format_for_tag(tag)
            display = _convert_ts(ln) + ln if _convert_ts(ln) else ln
            cursor.insertText(display + '\n', fmt)
        if end < len(self._all_lines):
            placeholder_text = f'--- 还有 {len(self._all_lines) - end} 行未加载，滚到底部自动加载 ---'
            cursor.insertText(placeholder_text + '\n')
            block = self._editor.document().lastBlock()
            block.setUserData(_PlaceholderData())
            self._placeholder_block = block.blockNumber()
        cursor.endEditBlock()
        self._rendered_count = end
        # 更新 info
        try:
            size = os.path.getsize(self._path)
        except Exception:
            size = 0
        self._info_lbl.setText(
            f'{self._path} | {len(self._all_lines)} 行 | {size/1024:.1f} KB')
        if at_bottom:
            sb.setValue(sb.maximum())
        # 通知 FilterPanel 增量筛选
        if self._on_lines_appended:
            try:
                self._on_lines_appended(new_count)
            except Exception:
                pass


# ---- 全局搜索栏（Phase 3）------------------------------------------
class _QtGlobalSearchBar(QDockWidget):
    """全局搜索栏：QLineEdit + 结果 QTreeWidget（file/line/preview）。

    Ctrl+Shift+F 触发；扫所有 Tab 的 global_search(pattern)，
    cap GLOBAL_SEARCH_MAX；双击结果 → 跳转对应 Tab + 行。
    """

    def __init__(self, parent, theme, on_locate=None,
                 tab_widget=None, tabs_list_fn=None):
        super().__init__('全局搜索', parent)
        self._theme = theme
        self._on_locate = on_locate
        self._tab_widget = tab_widget
        self._tabs_list_fn = tabs_list_fn
        self.setFeatures(QDockWidget.DockWidgetMovable
                         | QDockWidget.DockWidgetFloatable
                         | QDockWidget.DockWidgetClosable)
        self._build_widgets()
        self.apply_theme(theme)

    def _build_widgets(self):
        container = QWidget()
        v = QVBoxLayout(container)
        v.setContentsMargins(6, 6, 6, 6)
        v.setSpacing(4)
        # 搜索条
        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(4)
        self._edit = QLineEdit()
        self._edit.setPlaceholderText('输入正则，回车搜索（Ctrl+Shift+F）')
        self._edit.returnPressed.connect(self._do_search)
        rl.addWidget(self._edit, stretch=1)
        self._search_btn = QPushButton('搜索')
        self._search_btn.clicked.connect(self._do_search)
        rl.addWidget(self._search_btn)
        self._close_btn = QPushButton('关闭')
        self._close_btn.clicked.connect(lambda: self.setVisible(False))
        rl.addWidget(self._close_btn)
        v.addWidget(row)
        # 结果 Tree
        self._tree = QTreeWidget()
        self._tree.setColumnCount(3)
        self._tree.setHeaderLabels(['文件', '行号', '预览'])
        self._tree.setRootIsDecorated(False)
        self._tree.setAlternatingRowColors(True)
        self._tree.itemDoubleClicked.connect(self._on_double_click)
        hdr = self._tree.header()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.Stretch)
        v.addWidget(self._tree, stretch=1)
        # info
        self._info_lbl = QLabel('就绪')
        v.addWidget(self._info_lbl)
        self.setWidget(container)
        self.setVisible(False)

    def apply_theme(self, theme):
        self._theme = theme
        self._info_lbl.setStyleSheet(
            f'color: {theme.get("muted", "#858585")}; background: transparent;')

    def show(self):
        self.setVisible(True)
        self._edit.setFocus()
        self._edit.selectAll()

    def _do_search(self):
        text = self._edit.text()
        if not text:
            self._info_lbl.setText('请输入搜索词')
            return
        try:
            pattern = re.compile(text)
        except re.error:
            self._info_lbl.setText('正则错误')
            return
        self._tree.clear()
        if self._tabs_list_fn is None:
            return
        total = 0
        for i, tab in enumerate(self._tabs_list_fn()):
            if not isinstance(tab, _QtLogViewTab):
                continue
            results = tab.global_search(pattern)
            for line_idx, col, preview in results:
                item = QTreeWidgetItem([
                    tab.filename(), str(line_idx + 1), preview])
                item.setData(0, Qt.UserRole, (i, line_idx))
                self._tree.addTopLevelItem(item)
                total += 1
                if total >= GLOBAL_SEARCH_MAX:
                    self._info_lbl.setText(
                        f'已截断 {GLOBAL_SEARCH_MAX} 个结果（共更多）')
                    return
        self._info_lbl.setText(f'{total} 个结果')

    def _on_double_click(self, item):
        data = item.data(0, Qt.UserRole)
        if data is None or self._on_locate is None:
            return
        tab_idx, line_idx = data
        try:
            self._on_locate(tab_idx, line_idx)
        except Exception:
            pass


# ---- 主窗口（Phase 1 骨架）-------------------------------------------
class QtLogViewerWindow(QMainWindow):
    """Qt 日志查看器主窗口：Tab + 工具栏 + 状态栏 + 主题。

    Phase 1：骨架，Tab/工具栏/状态栏/主题/打开文件/关闭 Tab/快捷键。
    Phase 2 加 FilterPanel；Phase 3 加全局搜索/实时/菜单/持久化。
    """

    def __init__(self, path: str | None = None,
                 theme_name: str = 'auto'):
        super().__init__()
        self._theme_name = theme_name
        self._theme = themes.THEMES[themes.resolve(theme_name)]
        self._tabs: list[_QtLogViewTab] = []
        self._filter_panel: _QtFilterPanel | None = None  # Phase 2
        # 监视窗口单例（Phase 5）
        self._motor_monitor = None
        self._bms_monitor = None
        self._imu_monitor = None

        # 应用 QSS
        app = QApplication.instance() or QApplication(sys.argv)
        self._app = app
        qt_theme.apply_qss(app, theme_name)

        # 窗口
        self.setWindowTitle('日志查看')
        self.resize(1400, 800)
        self.setMinimumSize(900, 540)
        # 持久化几何
        try:
            saved = config.load().get('log_view_geometry', '')
            if saved:
                self.restoreGeometry(bytes.fromhex(saved))
        except Exception:
            pass

        # 中央 QTabWidget
        self._tab_widget = QTabWidget()
        self._tab_widget.setTabsClosable(True)
        self._tab_widget.tabCloseRequested.connect(self._on_tab_close)
        self._tab_widget.currentChanged.connect(self._on_tab_changed)
        self.setCentralWidget(self._tab_widget)

        # 筛选面板 DockWidget（右侧）
        self._filter_panel = _QtFilterPanel(
            self, self._theme, on_locate=self._on_filter_locate)
        self.addDockWidget(Qt.RightDockWidgetArea, self._filter_panel)
        # 允许向左拖窄：DockWidget 自己 setMinimumWidth(0)，外加 minimumSizeHint
        # 覆盖（见 _QtFilterPanel.minimumSizeHint）才能突破 layout 自然下限。
        # 不设上限，允许用户拖到任意宽度。
        self._filter_panel.setMinimumWidth(0)
        # X 按钮关闭后同步菜单 checkable 状态
        self._filter_panel.visibilityChanged.connect(self._on_filter_visibility)
        self._filter_panel.topLevelChanged.connect(self._on_filter_floating_changed)

        # 全局搜索 DockWidget（顶部，默认隐藏）
        self._global_search = _QtGlobalSearchBar(
            self, self._theme,
            on_locate=self._on_global_search_locate,
            tabs_list_fn=lambda: self._tabs)
        self.addDockWidget(Qt.TopDockWidgetArea, self._global_search)
        self._global_search.hide()
        self._global_search.visibilityChanged.connect(self._on_global_search_visibility)

        # 工具栏 + 菜单栏 + 状态栏
        self._build_toolbar()
        self._build_menubar()
        sb = QStatusBar()
        self.setStatusBar(sb)
        sb.showMessage('就绪')

        # 视图状态：工具栏显隐
        try:
            show_tb = config.load().get('log_view_show_toolbar', True)
        except Exception:
            show_tb = True
        self._toolbar.setVisible(show_tb)

        # 快捷键
        QShortcut(QKeySequence('Ctrl+O'), self,
                  activated=self._open_file_dialog)
        QShortcut(QKeySequence('Ctrl+W'), self,
                  activated=self._on_close)
        QShortcut(QKeySequence('Ctrl+End'), self,
                  activated=self._jump_to_end)
        QShortcut(QKeySequence('Ctrl+F'), self,
                  activated=self._focus_current_search)
        QShortcut(QKeySequence('Ctrl+Shift+F'), self,
                  activated=lambda: self._toggle_global_search(
                      not self._global_search.isVisible()))
        QShortcut(QKeySequence('Escape'), self,
                  activated=lambda: self._toggle_global_search(False))

        if path:
            self.open_file(path)

    # ---- 工具栏 -------------------------------------------------------
    def _build_toolbar(self):
        tb = QToolBar('主工具栏')
        tb.setMovable(False)
        self.addToolBar(tb)
        self._toolbar = tb

        a = QAction('打开文件…', self)
        a.triggered.connect(self._open_file_dialog)
        tb.addAction(a)
        a = QAction('关闭当前 Tab', self)
        a.triggered.connect(self._close_current_tab)
        tb.addAction(a)
        tb.addSeparator()
        # 分离/嵌入筛选 DockWidget（保留快捷按钮，但状态由菜单 checkable 项同步）
        a = QAction('分离筛选窗口', self)
        a.triggered.connect(self._float_filter)
        tb.addAction(a)
        a = QAction('嵌入筛选窗口', self)
        a.triggered.connect(self._embed_filter)
        tb.addAction(a)
        tb.addSeparator()
        # 全局搜索（toggle）
        a = QAction('全局搜索', self, checkable=True)
        a.setChecked(False)
        a.toggled.connect(self._toggle_global_search)
        tb.addAction(a)
        tb.addSeparator()
        a = QAction('跳到末尾', self)
        a.triggered.connect(self._jump_to_end)
        tb.addAction(a)
        tb.addSeparator()
        # 实时刷新 toggle
        self._live_action = QAction('实时刷新', self, checkable=True)
        self._live_action.triggered.connect(self._toggle_live_mode)
        tb.addAction(self._live_action)
        tb.addSeparator()
        a = QAction('切换主题', self)
        a.triggered.connect(self._toggle_theme)
        tb.addAction(a)
        tb.addSeparator()
        self._info_lbl = QLabel('')
        tb.addWidget(self._info_lbl)

    def _build_menubar(self):
        mb = self.menuBar()
        # 文件菜单
        m_file = mb.addMenu('文件(F)')
        a = QAction('打开文件…', self)
        a.setShortcut('Ctrl+O')
        a.triggered.connect(self._open_file_dialog)
        m_file.addAction(a)
        a = QAction('关闭当前 Tab', self)
        a.triggered.connect(self._close_current_tab)
        m_file.addAction(a)
        m_file.addSeparator()
        a = QAction('关闭窗口', self)
        a.setShortcut('Ctrl+W')
        a.triggered.connect(self._on_close)
        m_file.addAction(a)
        # 视图菜单
        m_view = mb.addMenu('视图(V)')
        self._toolbar_action = QAction('主工具栏', self, checkable=True)
        self._toolbar_action.setChecked(self._toolbar.isVisible())
        self._toolbar_action.toggled.connect(self._toggle_toolbar)
        m_view.addAction(self._toolbar_action)
        m_view.addSeparator()
        # 筛选面板（checkable）：开关双向同步，X 关闭即取消勾选
        self._filter_action = QAction('筛选面板', self, checkable=True)
        self._filter_action.setChecked(True)
        self._filter_action.toggled.connect(self._toggle_filter_panel)
        m_view.addAction(self._filter_action)
        a = QAction('分离筛选窗口', self)
        a.triggered.connect(self._float_filter)
        m_view.addAction(a)
        a = QAction('嵌入筛选窗口', self)
        a.triggered.connect(self._embed_filter)
        m_view.addAction(a)
        m_view.addSeparator()
        # 全局搜索（checkable）
        self._global_search_action = QAction('全局搜索', self, checkable=True)
        self._global_search_action.setChecked(False)
        self._global_search_action.setShortcut('Ctrl+Shift+F')
        self._global_search_action.toggled.connect(self._toggle_global_search)
        m_view.addAction(self._global_search_action)
        a = QAction('文件内搜索', self)
        a.setShortcut('Ctrl+F')
        a.triggered.connect(self._focus_current_search)
        m_view.addAction(a)
        m_view.addSeparator()
        self._live_action_menu = QAction('实时刷新', self, checkable=True)
        self._live_action_menu.toggled.connect(self._toggle_live_mode)
        m_view.addAction(self._live_action_menu)
        a = QAction('跳到末尾', self)
        a.setShortcut('Ctrl+End')
        a.triggered.connect(self._jump_to_end)
        m_view.addAction(a)
        # 工具菜单
        m_tools = mb.addMenu('工具(T)')
        a = QAction('全局搜索…', self)
        a.setShortcut('Ctrl+Shift+F')
        a.triggered.connect(lambda: self._toggle_global_search(True))
        m_tools.addAction(a)
        a = QAction('文件内搜索', self)
        a.setShortcut('Ctrl+F')
        a.triggered.connect(self._focus_current_search)
        m_tools.addAction(a)
        m_tools.addSeparator()
        a = QAction('电机数据监视…', self)
        a.triggered.connect(lambda: self._open_monitor('motor'))
        m_tools.addAction(a)
        a = QAction('BMS 数据监视…', self)
        a.triggered.connect(lambda: self._open_monitor('bms'))
        m_tools.addAction(a)
        a = QAction('IMU 数据监视…', self)
        a.triggered.connect(lambda: self._open_monitor('imu'))
        m_tools.addAction(a)
        # 帮助菜单
        m_help = mb.addMenu('帮助(H)')
        a = QAction('关于', self)
        a.triggered.connect(self._show_about)
        m_help.addAction(a)

    # ---- Tab 管理 -----------------------------------------------------
    def open_file(self, path):
        # 去重：已开则聚焦
        for i, tab in enumerate(self._tabs):
            if tab.path() == path:
                self._tab_widget.setCurrentIndex(i)
                return
        tab = _QtLogViewTab(self, path, self._theme,
                            on_line_click=self._on_text_click)
        tab._feed_monitor_cb = self._feed_monitor_from_line
        idx = self._tab_widget.addTab(tab, tab.filename())
        self._tabs.append(tab)
        self._tab_widget.setCurrentIndex(idx)
        # addTab 可能已自动设 currentIndex，currentChanged 不触发，手动同步
        if self._filter_panel is not None:
            self._filter_panel.set_tab(tab)
        self._info_lbl.setText(f'{len(self._tabs)} 个文件打开')

    def _open_file_dialog(self):
        last_dir = config.load().get('log_view_last_dir', '')
        paths, _ = QFileDialog.getOpenFileNames(
            self, '打开日志文件', last_dir,
            '日志 (*.txt *.log);;所有文件 (*)')
        if paths:
            for p in paths:
                self.open_file(p)
            try:
                cfg = config.load()
                cfg['log_view_last_dir'] = os.path.dirname(paths[0])
                config.save(cfg)
            except Exception:
                pass

    def _close_current_tab(self):
        idx = self._tab_widget.currentIndex()
        if idx < 0:
            return
        self._on_tab_close(idx)

    def _on_tab_close(self, idx):
        if idx < 0 or idx >= len(self._tabs):
            return
        tab = self._tabs[idx]
        tab.stop_live()
        self._tab_widget.removeTab(idx)
        self._tabs.pop(idx)
        self._info_lbl.setText(f'{len(self._tabs)} 个文件打开')

    def _current_tab(self) -> _QtLogViewTab | None:
        idx = self._tab_widget.currentIndex()
        if idx < 0 or idx >= len(self._tabs):
            return None
        return self._tabs[idx]

    def _on_tab_changed(self, idx):
        if idx < 0:
            return
        tab = self._current_tab()
        if tab is not None and self._filter_panel is not None:
            self._filter_panel.set_tab(tab)
        # 同步实时刷新按钮状态
        if tab is not None:
            live = tab.is_live_mode()
            self._live_action.setChecked(live)
            self._live_action_menu.setChecked(live)

    # ---- 视图控制 ---------------------------------------------------
    def _toggle_filter_panel(self, checked: bool):
        """菜单 checkable 项切换筛选面板可见性。

        与 _on_filter_visibility 形成双向同步：
        - 用户点菜单 → setVisible(checked) → visibilityChanged → 菜单 check 同步
        - 用户点 X 按钮 → visibilityChanged(False) → 菜单自动取消勾选
        """
        if self._filter_panel is not None:
            self._filter_panel.setVisible(checked)

    def _on_filter_visibility(self, visible: bool):
        """X 按钮关闭 / dock 显隐变化 → 同步菜单 checkable。"""
        if self._filter_action is not None:
            self._filter_action.setChecked(visible)

    def _on_filter_floating_changed(self, floating: bool):
        """分离/嵌入状态变化时，菜单只是反映当前态，无需特殊处理。
        但需要确保面板可见（用户拖出 dock 时 Qt 自动 setVisible）。"""
        pass

    def _float_filter(self):
        """分离筛选：让 DockWidget 浮动为独立窗口。"""
        if self._filter_panel is not None:
            self._filter_panel.setVisible(True)
            self._filter_panel.setFloating(True)
            self._filter_panel.raise_()
            if self._filter_action is not None:
                self._filter_action.setChecked(True)

    def _embed_filter(self):
        """嵌入筛选：DockWidget 回到主窗口 dock 区。"""
        if self._filter_panel is not None:
            self._filter_panel.setFloating(False)
            self.addDockWidget(Qt.RightDockWidgetArea, self._filter_panel)
            self._filter_panel.setVisible(True)
            self._filter_panel.raise_()
            if self._filter_action is not None:
                self._filter_action.setChecked(True)

    def _toggle_global_search(self, checked: bool):
        """菜单/工具栏 checkable 项切换全局搜索面板可见性。"""
        if checked:
            self._global_search.show()
        else:
            self._global_search.hide()

    def _on_global_search_visibility(self, visible: bool):
        """全局搜索 X 按钮关闭 → 同步菜单/工具栏。"""
        if self._global_search_action is not None:
            self._global_search_action.setChecked(visible)

    def _toggle_toolbar(self, checked: bool):
        self._toolbar.setVisible(checked)

    def _toggle_live_mode(self, checked: bool):
        tab = self._current_tab()
        if tab is not None:
            tab.set_live_mode(checked)
        self._live_action.setChecked(checked)
        self._live_action_menu.setChecked(checked)

    def _show_about(self):
        from PySide6.QtWidgets import QMessageBox
        try:
            import PySide6
            versions = (f'PySide6: {PySide6.__version__}\n'
                        f'Python: {sys.version.split()[0]}')
        except Exception as e:
            versions = f'版本信息获取失败: {e}'
        QMessageBox.information(
            self, '关于', '日志查看 (Qt)\n\n技术栈:\n' + versions + '\n\n'
            '功能:\n'
            '  - 多 Tab 多文件 + 行号 + 大文件按批渲染\n'
            '  - 滚动到底自动加载更多（RENDER_BATCH=50000）\n'
            '  - 文件内搜索 Ctrl+F + 高亮 + 上一个/下一个\n'
            '  - 全局搜索 Ctrl+Shift+F 跨 Tab\n'
            '  - 筛选面板（级别 + 正则，DockWidget 拖出/嵌入）\n'
            '  - 双向定位（主 Text 点击 ↔ 筛选面板选中）\n'
            '  - 实时刷新（文件变化追加，polling + mtime 兜底）\n'
            '  - 视图状态持久化（几何/工具栏/最近目录）\n'
            '  - 主题切换（VSCode Dark+ / 浅色）\n'
            '  - 电机/BMS/IMU 监视入口（Phase 5）')

    # ---- 回调 ---------------------------------------------------------
    def _on_text_click(self, tab, line_idx):
        # 主 Text 点击 → FilterPanel 选中最近匹配
        if self._filter_panel is not None:
            self._filter_panel.locate_line(line_idx)

    def _on_filter_locate(self, line_idx: int):
        # 筛选面板双击 → 主 Text 跳转
        tab = self._current_tab()
        if tab is not None:
            tab.goto_line(line_idx)

    def _feed_monitor_from_line(self, line: str):
        # Phase 5：实时新行喂给监视窗口
        for monitor in (self._motor_monitor, self._bms_monitor,
                        self._imu_monitor):
            if monitor is not None:
                try:
                    monitor.feed(line)
                except Exception:
                    pass

    def _jump_to_end(self):
        tab = self._current_tab()
        if tab is not None:
            tab._jump_to_end()

    def _focus_current_search(self):
        tab = self._current_tab()
        if tab is not None:
            tab._show_search(True)

    # ---- 全局搜索 ---------------------------------------------------
    def _on_global_search_locate(self, tab_idx: int, line_idx: int):
        """全局搜索双击 → 切换到对应 Tab + 跳行。"""
        if 0 <= tab_idx < len(self._tabs):
            self._tab_widget.setCurrentIndex(tab_idx)
            self._tabs[tab_idx].goto_line(line_idx)
            # 关闭全局搜索面板（setVisible(False) 会触发 visibilityChanged
            # → _on_global_search_visibility 同步菜单勾选状态）
            self._global_search.setVisible(False)

    # ---- 监视窗口入口（Phase 5）-------------------------------------
    def _open_monitor(self, kind: str):
        """电机/BMS/IMU 监视入口 → 同进程打开 QtMonitorWindow。

        单例：第一次创建，之后 lift/focus。复用 config 持久化字段
        motor_vars/motor_plots 等。打开时 replay 当前 Tab 全量日志
        （分块 CHUNK=5000，QTimer.singleShot 让出主线程避免卡顿）。
        """
        attr = f'_{kind}_monitor'
        monitor = getattr(self, attr, None)
        if monitor is not None and monitor._win is not None and monitor._win.isVisible():
            monitor._win.raise_()
            monitor._win.activateWindow()
            return
        from serial_debug.ui.qt_monitor_window import QtMonitorWindow
        monitor = QtMonitorWindow(theme_name=self._theme_name)
        monitor._create()
        # 应用主题 + 持久化字段读取
        try:
            monitor._win.show()
        except Exception:
            pass
        setattr(self, attr, monitor)
        # replay 当前 Tab 全量日志
        tab = self._current_tab()
        if tab is not None:
            self._replay_to_monitor(tab, monitor)

    def _replay_to_monitor(self, tab: _QtLogViewTab, monitor):
        """分块重放当前 Tab 全量日志到 Qt 监视窗口。

        CHUNK=5000 行/批，QTimer.singleShot(0) 让出主线程，避免一次性
        喂数十万行卡 UI。日志查看器实时新行也会通过 _feed_monitor_from_line
        喂给监视窗口（Phase 1 已接）。
        """
        CHUNK = 5000
        lines = tab.all_lines()
        total = len(lines)

        def _replay_batch(start):
            if start >= total:
                return
            end = min(start + CHUNK, total)
            for i in range(start, end):
                try:
                    for ts, name, val in _iter_panel_vars(lines[i] + '\n'):
                        monitor._register_var(name)
                        monitor._data.setdefault(name, []).append((ts, val))
                except Exception:
                    pass
            monitor._refresh_plot()
            if end < total:
                QTimer.singleShot(0, lambda: _replay_batch(end))

        _replay_batch(0)

    # ---- 主题 + 关闭 -------------------------------------------------
    def apply_theme(self, theme_name):
        self._theme_name = theme_name
        self._theme = themes.THEMES[themes.resolve(theme_name)]
        qt_theme.apply_qss(self._app, theme_name)
        for tab in self._tabs:
            tab.apply_theme(self._theme)
        if self._filter_panel is not None:
            self._filter_panel.apply_theme(self._theme)
        self._global_search.apply_theme(self._theme)

    def _toggle_theme(self):
        cur = themes.resolve(self._theme_name)
        new = 'light' if cur == 'dark' else 'dark'
        self.apply_theme(new)

    def _on_close(self):
        try:
            cfg = config.load()
            cfg['log_view_geometry'] = (
                self.saveGeometry().toHex().data().decode())
            cfg['log_view_show_toolbar'] = self._toolbar.isVisible()
            config.save(cfg)
        except Exception:
            pass
        for tab in self._tabs:
            tab.stop_live()

    def closeEvent(self, event):
        self._on_close()
        super().closeEvent(event)

    def run(self):
        self.show()
        return self._app.exec()


# ---- 筛选面板（Phase 2）---------------------------------------------
class _QtFilterPanel(QDockWidget):
    """筛选面板（QDockWidget，自动支持拖出/嵌入）。

    级别 Combobox + 正则 Entry + 结果 QTreeWidget。
    双击 Tree → on_locate(line_idx) → tab.goto_line。
    Text 点击 → locate_line 选中最近匹配。
    增量筛选：_on_lines_appended(new_count) 只扫新增行，cap FILTER_RESULT_MAX。
    """

    def __init__(self, parent, theme, on_locate=None):
        super().__init__('筛选', parent)
        self._theme = theme
        self._tab: _QtLogViewTab | None = None
        self._filtered_idx: list[int] = []
        self._last_scanned_idx = 0
        self._on_locate = on_locate
        # DockWidget 设置：可移动 / 可拖出 / 可关闭
        self.setFeatures(QDockWidget.DockWidgetMovable
                         | QDockWidget.DockWidgetFloatable
                         | QDockWidget.DockWidgetClosable)
        self.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea
                             | Qt.BottomDockWidgetArea
                             | Qt.TopDockWidgetArea)
        # 自定义标题栏：默认标题栏有关闭/浮动按钮，自然最小宽度 ~49px，
        # 让 DockWidget 拖不到更窄。覆盖为可缩到 0 的自定义标题栏。
        # 用 QToolButton + QStyle 标准图标，避免字体缺字渲染成方框。
        # 关闭按钮的语义保留在 visibilityChanged 信号 + 主菜单 checkable 项。
        from PySide6.QtWidgets import QHBoxLayout, QLabel, QToolButton
        style = self.style()

        class _SlimTitleBar(QWidget):
            def __init__(self, host):
                super().__init__(host)
                self._host = host
                lay = QHBoxLayout(self)
                lay.setContentsMargins(6, 2, 2, 2)
                lay.setSpacing(2)
                self._title = QLabel('筛选')
                self._title.setStyleSheet('font-weight: 600;')
                lay.addWidget(self._title)
                lay.addStretch(1)
                b_float = QToolButton()
                b_float.setAutoRaise(True)
                b_float.setIcon(style.standardIcon(QStyle.SP_TitleBarNormalButton))
                b_float.setToolTip('分离 / 嵌入')
                b_float.clicked.connect(lambda: host.setFloating(not host.isFloating()))
                lay.addWidget(b_float)
                b_close = QToolButton()
                b_close.setAutoRaise(True)
                b_close.setIcon(style.standardIcon(QStyle.SP_DockWidgetCloseButton))
                b_close.setToolTip('关闭')
                b_close.clicked.connect(lambda: host.setVisible(False))
                lay.addWidget(b_close)
            def minimumSizeHint(self):
                return QSize(0, 0)
            def sizeHint(self):
                return QSize(80, 24)
        self._title_bar = _SlimTitleBar(self)
        self.setTitleBarWidget(self._title_bar)
        self._build_widgets()
        self.apply_theme(theme)

    def _build_widgets(self):
        # 自定义 container：覆盖 minimumSizeHint 让 DockWidget 能拖到任意窄
        class _ShrinkableContainer(QWidget):
            def minimumSizeHint(self):
                return QSize(0, 0)
            def sizeHint(self):
                return QSize(260, 400)
        container = _ShrinkableContainer()
        v = QVBoxLayout(container)
        v.setContentsMargins(6, 6, 6, 6)
        v.setSpacing(4)
        # 级别 Combobox + Label 同行
        self._level_box = QComboBox()
        for label in ('全部', '日志', 'AT 响应', '警告/错误'):
            self._level_box.addItem(label)
        self._level_box.currentIndexChanged.connect(self._apply_filter)
        level_row = QWidget()
        ll = QHBoxLayout(level_row)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(6)
        ll.addWidget(QLabel('级别'))
        ll.addWidget(self._level_box, 1)
        v.addWidget(level_row)
        # 正则 Entry + Label 同行
        self._regex_edit = QLineEdit()
        self._regex_edit.setPlaceholderText('正则筛选（回车应用）')
        self._regex_edit.returnPressed.connect(self._apply_filter)
        regex_row = QWidget()
        rl = QHBoxLayout(regex_row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(6)
        rl.addWidget(QLabel('正则'))
        rl.addWidget(self._regex_edit, 1)
        v.addWidget(regex_row)
        # 按钮
        btn_row = QWidget()
        bl = QHBoxLayout(btn_row)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(4)
        self._apply_btn = QPushButton('应用')
        self._apply_btn.clicked.connect(self._apply_filter)
        self._clear_btn = QPushButton('清除')
        self._clear_btn.clicked.connect(self._clear_filter)
        bl.addWidget(self._apply_btn)
        bl.addWidget(self._clear_btn)
        v.addWidget(btn_row)
        # 结果 Tree
        self._tree = QTreeWidget()
        self._tree.setColumnCount(2)
        self._tree.setHeaderLabels(['行号', '内容'])
        self._tree.setRootIsDecorated(False)
        self._tree.setAlternatingRowColors(True)
        self._tree.setSortingEnabled(False)
        self._tree.itemDoubleClicked.connect(self._on_tree_double_click)
        # 列宽：行号按内容自适应，内容列 stretch 跟随可用宽度
        hdr = self._tree.header()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)
        v.addWidget(self._tree, stretch=1)
        # info Label
        self._info_lbl = QLabel('就绪')
        v.addWidget(self._info_lbl)
        self.setWidget(container)
        # 内部控件不卡最小宽度，允许 DockWidget 拖到任意窄
        for w in (self._level_box, self._regex_edit, self._tree,
                  self._apply_btn, self._clear_btn, self._info_lbl,
                  btn_row, container, level_row, regex_row):
            try:
                w.setMinimumWidth(0)
                w.setMinimumSize(0, 0)
            except Exception:
                pass
        try:
            v.setSizeConstraint(QVBoxLayout.SetNoConstraint)
        except Exception:
            pass

    def minimumSizeHint(self):
        # 覆盖默认 minimumSizeHint（= layout 自然最小），允许 DockWidget
        # 拖到任意窄。Qt 会在内部裁剪/隐藏控件，不影响 DockWidget 自己。
        from PySide6.QtCore import QSize
        return QSize(0, 0)

    def sizeHint(self):
        # 提示默认宽度，但不强求
        from PySide6.QtCore import QSize
        return QSize(300, 400)

    # ---- 主题 --------------------------------------------------------
    def apply_theme(self, theme):
        self._theme = theme
        # DockWidget 内控件由 QSS 处理；只设 info label 颜色
        self._info_lbl.setStyleSheet(
            f'color: {theme.get("muted", "#858585")}; background: transparent;')
        # Tree 行 tag 配色（由 QSS alternate/background 处理，无需手动）
        # 重新渲染已显示的行：QTreeWidget 自动跟随 QSS

    # ---- Tab 绑定 ----------------------------------------------------
    def set_tab(self, tab: _QtLogViewTab | None):
        self._tab = tab
        if tab is not None:
            tab._on_lines_appended = self._on_lines_appended
        self._apply_filter()

    def _on_lines_appended(self, new_count: int):
        """实时模式新增行回调 → 增量筛选。"""
        self._apply_filter_incremental(new_count)

    # ---- 筛选 --------------------------------------------------------
    def _level_filter(self, line: str, tag: str) -> bool:
        """级别筛选：返回 True 表示该行通过级别筛。"""
        idx = self._level_box.currentIndex()
        if idx == 0:
            return True  # 全部
        if idx == 1:
            return tag == 'log'
        if idx == 2:
            return tag == 'resp'
        if idx == 3:
            return tag == 'note'
        return True

    def _apply_filter(self):
        """全量筛选：扫所有行，cap FILTER_RESULT_MAX。"""
        if self._tab is None:
            return
        lines = self._tab.all_lines()
        tags = self._tab._line_tags
        try:
            text = self._regex_edit.text()
            regex = re.compile(text) if text else None
        except re.error:
            self._info_lbl.setText('正则错误')
            return
        filtered: list[int] = []
        for i, ln in enumerate(lines):
            if len(filtered) >= FILTER_RESULT_MAX:
                break
            if not self._level_filter(ln, tags[i] if i < len(tags) else 'log'):
                continue
            if regex is not None and not regex.search(ln):
                continue
            filtered.append(i)
        self._filtered_idx = filtered
        self._last_scanned_idx = len(lines)
        self._render_results(filtered, len(lines))

    def _apply_filter_incremental(self, new_count: int):
        """增量筛选：扫 [last, last+new_count)，cap = FILTER_RESULT_MAX - len(already)。"""
        if self._tab is None:
            return
        lines = self._tab.all_lines()
        tags = self._tab._line_tags
        try:
            text = self._regex_edit.text()
            regex = re.compile(text) if text else None
        except re.error:
            return
        if len(self._filtered_idx) >= FILTER_RESULT_MAX:
            self._last_scanned_idx = len(lines)
            return
        start = self._last_scanned_idx
        end = min(len(lines), start + new_count)
        new_filtered: list[int] = []
        for i in range(start, end):
            if len(self._filtered_idx) + len(new_filtered) >= FILTER_RESULT_MAX:
                break
            ln = lines[i]
            tag = tags[i] if i < len(tags) else 'log'
            if not self._level_filter(ln, tag):
                continue
            if regex is not None and not regex.search(ln):
                continue
            new_filtered.append(i)
        if new_filtered:
            self._filtered_idx.extend(new_filtered)
            self._insert_into_tree(new_filtered)
        self._last_scanned_idx = end
        self._info_lbl.setText(
            f'{len(self._filtered_idx)} / {len(lines)} 行（增量 +{len(new_filtered)}）')

    def _render_results(self, filtered: list[int], total: int = 0):
        """全量渲染：清 Tree + 分批插。"""
        self._tree.clear()
        self._insert_into_tree(filtered)
        if total == 0:
            total = len(self._tab.all_lines()) if self._tab else 0
        self._info_lbl.setText(f'{len(filtered)} / {total} 行')

    def _insert_into_tree(self, idx_list: list[int]):
        """分批插入 QTreeWidgetItem（FILTER_INSERT_BATCH=5000）。"""
        if not idx_list:
            return
        # 简化：直接 addTopLevelItems 批插（QTreeWidget 比 tkinter Treeview 快）
        lines = self._tab.all_lines() if self._tab else []
        tags = self._tab._line_tags if self._tab else []
        start = 0
        while start < len(idx_list):
            end = min(start + FILTER_INSERT_BATCH, len(idx_list))
            items = []
            for i in range(start, end):
                line_idx = idx_list[i]
                ln = lines[line_idx] if line_idx < len(lines) else ''
                tag = tags[line_idx] if line_idx < len(tags) else 'log'
                item = QTreeWidgetItem([str(line_idx + 1), ln])
                item.setData(0, Qt.UserRole, line_idx)
                # tag 颜色
                color = self._color_for_tag(tag)
                item.setForeground(0, QColor(color))
                item.setForeground(1, QColor(color))
                items.append(item)
            self._tree.addTopLevelItems(items)
            start = end
        # 跳到顶部
        if self._tree.topLevelItemCount() > 0:
            self._tree.setCurrentItem(self._tree.topLevelItem(0))

    def _color_for_tag(self, tag: str) -> str:
        t = self._theme
        if tag == 'resp':
            return t.get('accent', '#569cd6')
        if tag == 'note':
            return t.get('status_err', '#f48771')
        return t.get('panel_fg', '#cccccc')

    def _clear_filter(self):
        self._regex_edit.clear()
        self._level_box.setCurrentIndex(0)
        self._apply_filter()

    # ---- 双向定位 ----------------------------------------------------
    def _on_tree_double_click(self, item):
        line_idx = item.data(0, Qt.UserRole)
        if line_idx is None:
            return
        if self._on_locate:
            try:
                self._on_locate(int(line_idx))
            except Exception:
                pass

    def locate_line(self, line_idx: int):
        """主 Text 点击 → 选中 <= line_idx 的最近匹配行。"""
        # 二分查找 filtered_idx 中 <= line_idx 的最大值
        pos = bisect_right(self._filtered_idx, line_idx) - 1
        if pos < 0:
            return
        item = self._tree.topLevelItem(pos)
        if item is None:
            return
        self._tree.setCurrentItem(item)
        self._tree.scrollToItem(item, QTreeWidget.PositionAtCenter)
        self._tree.setFocus()

    def clear_results(self):
        self._tree.clear()
        self._filtered_idx = []
        self._last_scanned_idx = 0
        self._info_lbl.setText('就绪')


def main():
    """子进程入口：环境变量 SERIAL_DEBUG_LOG_VIEWER=1 启动 Qt 日志查看器。"""
    import argparse
    parser = argparse.ArgumentParser(description='Qt 日志查看器')
    parser.add_argument('files', nargs='*', help='日志文件路径（可选）')
    parser.add_argument('--theme', default='auto',
                        help='主题：dark / light / auto')
    args = parser.parse_args()
    win = QtLogViewerWindow(theme_name=args.theme)
    for f in args.files:
        win.open_file(f)
    sys.exit(win.run())


if __name__ == '__main__':
    main()
