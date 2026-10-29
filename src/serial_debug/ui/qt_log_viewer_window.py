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
from datetime import datetime, timedelta, timezone

from PySide6.QtCore import QObject, QRect, QSize, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QKeySequence,
    QPainter,
    QShortcut,
    QTextBlockUserData,
    QTextCharFormat,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QStatusBar,
    QStyle,
    QTabWidget,
    QToolBar,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from serial_debug.core.log_parser import _LOG_TS_RE
from serial_debug.infra import config, themes
from serial_debug.infra.error_log import log_exc
from serial_debug.ui import qt_theme
from serial_debug.ui.qt_monitor_window import _iter_panel_vars

# ---- 常量（与 tkinter 版一致）----------------------------------------
RESP_RE = re.compile(r'^(OK[%,]|ERROR|\+)')
RENDER_BATCH = 5000   # 单批插入行数：批内主线程忙 ~几十ms，批间让出事件循环保持响应
SEARCH_SCAN_LIMIT = 2_000_000
GLOBAL_SEARCH_MAX = 500
SEARCH_SCAN_TIMEOUT_MS = 5000   # 单次搜索墙钟超时
SEARCH_MAX_LINE_LEN = 8000      # 单行搜索截断上限：避免超长行 + 灾难性正则回溯
FILTER_RESULT_MAX = 50000       # 旧默认值，保留作为兜底常量；运行时实际
                                # 上限读 config['log_view_filter_result_max']
                                # （默认 500000），见 _QtFilterPanel._get_filter_max
FILTER_INSERT_BATCH = 200   # 分批插入 QTreeWidgetItem 的批次大小（小批次 + 事件循环呼吸）
FILTER_MAX_LINE_LEN = 8000  # 单行搜索时截断上限：避免超长行 + 灾难性正则回溯
FILTER_SCAN_TIMEOUT_MS = 800  # 单次筛选扫描超时（墙钟）：超过就中断 + 报警
# 显示懒加载：筛选命中总数可达数十万行（无限制模式），但 QTreeWidget 一次性
# 插入全部会让每个 QTreeWidgetItem 占内存 + 渲染卡死。_filtered_idx 仍存全部命中
# （int 列表，50万行约 4MB，无压力），但树只插入前 FILTER_DISPLAY_MAX 行，状态栏
# 提示"命中 X 行，仅显示前 N 行"，并提供"加载更多"按钮与 locate_line 按需加载窗口。
# 见 _render_results / _load_more / locate_line。
FILTER_DISPLAY_MAX = 5000    # 树初始显示行数上限（降低以避免主线程创建过多 QTreeWidgetItem 卡死）
FILTER_LOAD_MORE_BATCH = 50000  # "加载更多" / locate_line 按需加载时每批行数
LIVE_POLL_MS = 500
LIVE_IDLE_POLL_MS = 2000
LIVE_IDLE_THRESHOLD = 6

# 日志时间戳统一按北京时间（UTC+8）显示。不依赖 tzdata/系统时区数据库——
# 日志时间戳是 Unix 秒（绝对时间），转北京时间即 +8 偏移。这样 PyInstaller
# 打包版即使无 tzdata（无 Asia/Shanghai 字符串）也能正确显示北京时间，
# 不会 fallback 到 UTC。见 _convert_ts / qt_monitor_window 时间轴/曲线。
BEIJING_TZ = timezone(timedelta(hours=8))

# 已转换的时间戳前缀：[MM-DD HH:MM:SS.mmm] 或 [MM-DD HH:MM:SS]
# 用于检测日志文件是否已含北京时间格式的时间戳，避免重复转换
_CONVERTED_TS_RE = re.compile(r'^\[\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d{3})?\]')

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
    """日志行时间戳 → [MM-DD HH:MM:SS.mmm] 前缀（不含年份）。失败返回空串。

    支持两种格式（复用 core.log_parser._LOG_TS_RE，DRY）：
    - 13 位毫秒时间戳（如 1693045530123）：整数即毫秒
    - 10~12 位秒级时间戳（如 1787888455 或 1787888455.650）：
      整数即秒，可选小数部分作毫秒（取前 3 位，不足补零）

    格式：月-日 时:分:秒.毫秒。毫秒手拼以避免 strftime %f 位数不可控
    和 Windows 上 strftime 对 %f 支持不一致的问题。

    如果行首已有 [MM-DD HH:MM:SS.mmm] 格式的前缀（说明日志文件已经
    被转换过），则不再重复转换，返回空串。
    """
    # 已有转换后的时间戳前缀则跳过，避免重复转换
    if _CONVERTED_TS_RE.match(line):
        return ''
    m = _LOG_TS_RE.search(line)
    if not m:
        return ''
    digits = m.group(1)
    frac = m.group(2)
    try:
        num = int(digits)
    except ValueError:
        return ''
    if len(digits) == 13:
        # 13 位毫秒时间戳：整数本身即毫秒，忽略可能的小数部分
        seconds = num // 1000
        ms = num % 1000
    else:
        # 10~12 位秒级时间戳：小数部分即毫秒（取前 3 位，不足补零）
        seconds = num
        ms = int(frac[:3].ljust(3, '0')) if frac else 0
    # 显式北京时间（UTC+8）：不依赖 tzdata/系统时区数据库，避免 PyInstaller
    # 打包版无 tzdata 时 fromtimestamp fallback 到 UTC 显示成 UTC 时间。
    dt = datetime.fromtimestamp(seconds, tz=BEIJING_TZ)
    return dt.strftime('[%m-%d %H:%M:%S.') + f'{ms:03d}] '


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
        # 日志查看器是只读工具：禁止编辑/拖拽移动文字，防止误改原始日志内容。
        # 用户误拖拽选中文字会移动/删除内容且无法撤销，设只读从根本杜绝。
        self.setReadOnly(True)
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
        self._search_epoch = 0  # 异步搜索序号：旧 epoch 结果丢弃
        self._search_pending: str | None = None  # 'next' / 'prev' / None
        self._search_worker = None  # 由主窗口注入
        self._auto_load = True
        self._loading = False
        self._placeholder_block: int = -1  # 占位符 block number
        self._pending_goto = -1  # goto_line 异步加载目标行（-1 = 无）
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
            with open(self._path, encoding='utf-8',
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
        except Exception as e:
            log_exc('debug', f'{__file__}:_load_file getsize failed path={self._path!r}', e)
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
        except Exception as e:
            log_exc('debug', f'{__file__}:_load_file getmtime failed path={self._path!r}', e)

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
            # 时间戳前缀转换：原始行替换行首 13 位 ts → [MM-DD HH:MM:SS.mmm]
            ts_prefix = _convert_ts(ln)
            display = ts_prefix + ln if ts_prefix else ln
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
        """跳到末尾：异步分批加载完所有内容后再滚到底（不冻结 UI）。"""
        target = len(self._all_lines) - 1
        if target < 0:
            return
        if self._rendered_count <= target:
            self._pending_goto = target
            if not self._loading:
                self._goto_step()
            # 滚到底在 _goto_step 完成后由 _goto_rendered 之前的最后一步处理：
            # 直接复用：加载完后 ensureCursorVisible
            return
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
            except Exception as e:
                log_exc('warning', f'{__file__}:_on_text_click on_line_click callback failed', e)

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
        """跳转到指定行：未渲染则异步分批加载到包含该行（不冻结 UI）。

        同步 while 循环一次插几十万行会冻结主线程（Ubuntu 弹"强制关闭/等待"），
        改为 QTimer.singleShot 分批推进，每批间让事件循环呼吸。
        """
        if line_idx < 0 or line_idx >= len(self._all_lines):
            return
        if line_idx >= self._rendered_count:
            self._load_until(line_idx)
            return
        self._goto_rendered(line_idx)

    def _load_until(self, target: int):
        """异步分批渲染到 target 行（含），完成后高亮跳转。"""
        self._pending_goto = target
        if self._loading:
            return
        self._goto_step()

    def _goto_step(self):
        """单步：渲染一批，未到目标则 singleShot(0) 继续。"""
        if self._pending_goto < 0:
            return
        if self._rendered_count > self._pending_goto:
            target = self._pending_goto
            self._pending_goto = -1
            self._goto_rendered(target)
            return
        self._render_batch(self._rendered_count, RENDER_BATCH)
        if self._rendered_count > self._pending_goto:
            target = self._pending_goto
            self._pending_goto = -1
            self._goto_rendered(target)
        else:
            QTimer.singleShot(0, self._goto_step)

    def _goto_rendered(self, line_idx: int):
        """目标行已渲染：光标定位 + 可见 + goto_hit 高亮 3s。"""
        block = self._editor.document().findBlockByNumber(line_idx)
        if not block.isValid():
            return
        cursor = QTextCursor(block)
        self._editor.setTextCursor(cursor)
        self._editor.ensureCursorVisible()
        if line_idx >= len(self._all_lines) - 1:
            sb = self._editor.verticalScrollBar()
            sb.setValue(sb.maximum())
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
            # matches 空时触发异步搜索 + 等 done 回调中跳第一个
            self._search_pending = 'next'
            self._do_search()
            return
        self._search_pos = (self._search_pos + 1) % len(self._search_matches)
        self._goto_match(self._search_matches[self._search_pos])
        self._search_info.setText(
            f'{self._search_pos + 1}/{len(self._search_matches)}')

    def _search_prev_match(self):
        if not self._search_matches:
            self._search_pending = 'prev'
            self._do_search()
            return
        self._search_pos = (self._search_pos - 1) % len(self._search_matches)
        self._goto_match(self._search_matches[self._search_pos])
        self._search_info.setText(
            f'{self._search_pos + 1}/{len(self._search_matches)}')

    def _do_search(self):
        """触发后台搜索：UI 线程不阻塞，反复回车时旧 epoch 结果丢弃。"""
        text = self._search_edit.text()
        if not text:
            self._search_matches = []
            self._search_info.setText('')
            return
        # 静态预检：危险正则 + 语法错误立即提示
        if _is_dangerous_regex(text):
            self._search_info.setText('正则存在嵌套量词，可能引发灾难性回溯')
            self._search_matches = []
            return
        try:
            self._search_re = re.compile(text)
        except re.error:
            self._search_info.setText('正则错误')
            self._search_matches = []
            return
        if self._search_worker is None:
            # 无 worker 回退到同步（不应出现，主窗口总会注入）
            self._do_search_sync()
            return
        self._search_epoch += 1
        ep = self._search_epoch
        self._search_info.setText('搜索中…')
        # 用 Signal 触发 worker（跨线程 QueuedConnection 自动派发）；
        # 不用 Q_ARG(list) —— PySide6 打包后 qArgDataFromPyType 崩
        self._search_worker.start_run_single.emit(
            ep, self._all_lines, text)

    def _do_search_sync(self):
        """回退路径：UI 线程同步搜索（无 worker 时）。"""
        matches: list[int] = []
        scanned = 0
        for i, ln in enumerate(self._all_lines):
            if scanned > SEARCH_SCAN_LIMIT:
                break
            scanned += 1
            scan_ln = ln if len(ln) <= SEARCH_MAX_LINE_LEN \
                else ln[:SEARCH_MAX_LINE_LEN]
            if self._search_re and self._search_re.search(scan_ln):
                matches.append(i)
                if len(matches) >= GLOBAL_SEARCH_MAX * 4:
                    break
        self._search_matches = matches
        self._search_pos = -1

    def _on_search_done(self, epoch: int, matches: list, info: str):
        """worker 完成回调：过期 epoch 丢弃。若有 pending 动作则跳第一个/末个。"""
        if epoch != self._search_epoch:
            return
        self._search_matches = matches
        self._search_pos = -1
        pending = self._search_pending
        self._search_pending = None
        if matches and pending == 'next':
            self._search_pos = 0
            self._goto_match(matches[0])
            self._search_info.setText(f'1/{len(matches)}')
            return
        if matches and pending == 'prev':
            self._search_pos = len(matches) - 1
            self._goto_match(matches[-1])
            self._search_info.setText(f'{len(matches)}/{len(matches)}')
            return
        self._search_info.setText(info if not matches
                                   else f'{len(matches)} 个匹配，按 ↓ 跳转')

    def _on_search_error(self, epoch: int, msg: str):
        if epoch != self._search_epoch:
            return
        self._search_matches = []
        self._search_pos = -1
        self._search_pending = None
        self._search_info.setText(msg)

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
            except Exception as e:
                log_exc('debug', f'{__file__}:set_live_mode getmtime failed', e)
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
        except Exception as e:
            log_exc('debug', f'{__file__}:_file_size getsize failed', e)
            return 0

    def _poll_file(self):
        if self._live_pos == 0 and not self._all_lines:
            # 还没加载过，先全量加载
            self._load_file()
            return
        try:
            cur_size = self._file_size()
            cur_mtime = os.path.getmtime(self._path)
        except Exception as e:
            log_exc('debug', f'{__file__}:_poll_file stat failed', e)
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
            with open(self._path, encoding='utf-8',
                       errors='replace') as f:
                f.seek(self._live_pos)
                chunk = f.read(cur_size - self._live_pos)
        except Exception as e:
            log_exc('warning', f'{__file__}:_poll_file read tail failed', e)
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
                except Exception as e:
                    log_exc('warning', f'{__file__}:_poll_file feed_monitor_cb failed', e)
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
            ts_prefix = _convert_ts(ln)
            display = ts_prefix + ln if ts_prefix else ln
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
                 tab_widget=None, tabs_list_fn=None, search_worker=None):
        super().__init__('全局搜索', parent)
        self._theme = theme
        self._on_locate = on_locate
        self._tab_widget = tab_widget
        self._tabs_list_fn = tabs_list_fn
        self._search_worker = search_worker
        self._search_epoch = 0
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
        """全局搜索：投递后台 worker，UI 线程不阻塞。"""
        text = self._edit.text()
        if not text:
            self._info_lbl.setText('请输入搜索词')
            return
        if _is_dangerous_regex(text):
            self._info_lbl.setText('正则存在嵌套量词，可能引发灾难性回溯，已拒绝')
            self._tree.clear()
            return
        try:
            re.compile(text)
        except re.error as e:
            self._info_lbl.setText(f'正则错误：{e}')
            return
        if self._search_worker is None or self._tabs_list_fn is None:
            self._info_lbl.setText('搜索未就绪')
            return
        # 取 tabs 快照（只传 lines 引用 + filename，避免 worker 线程访问 Qt 对象；
        # 引用共享不拷贝——list(...) 浅拷贝几十万行在主线程有明显停顿）
        tabs_snap = []
        for i, tab in enumerate(self._tabs_list_fn()):
            if not isinstance(tab, _QtLogViewTab):
                continue
            tabs_snap.append((i, tab.all_lines(), tab.filename()))
        self._search_epoch += 1
        ep = self._search_epoch
        self._tree.clear()
        self._info_lbl.setText('搜索中…')
        # 用 Signal 触发 worker（跨线程 QueuedConnection 自动派发）；
        # 不用 Q_ARG(list) —— PySide6 打包后 qArgDataFromPyType 崩
        self._search_worker.start_run_global.emit(ep, tabs_snap, text)

    def _on_search_done(self, epoch: int, results: list, info: str):
        """worker 完成回调：批量插入 + setUpdatesEnabled 包裹减少闪烁。"""
        if epoch != self._search_epoch:
            return
        self._tree.setUpdatesEnabled(False)
        try:
            self._tree.clear()
            items = []
            for tab_idx, line_idx, col, preview, fname in results:
                item = QTreeWidgetItem([fname, str(line_idx + 1), preview])
                item.setData(0, Qt.UserRole, (tab_idx, line_idx))
                items.append(item)
            if items:
                self._tree.addTopLevelItems(items)
        finally:
            self._tree.setUpdatesEnabled(True)
        self._info_lbl.setText(info)

    def _on_search_error(self, epoch: int, msg: str):
        if epoch != self._search_epoch:
            return
        self._tree.clear()
        self._info_lbl.setText(msg)

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
        # theme_name 可以是模式（auto/light/dark）或具体主题名
        self._theme_pref = (theme_name if theme_name in ('auto', 'light', 'dark')
                           else themes.load_pref())
        if theme_name in themes.THEMES:
            self._theme_name = theme_name
        else:
            self._theme_name = themes.resolve(self._theme_pref)
        self._theme = themes.THEMES[self._theme_name]
        self._tabs: list[_QtLogViewTab] = []
        self._filter_panel: _QtFilterPanel | None = None  # Phase 2
        # 后台共享搜索线程：当前文件搜索 + 全局搜索都丢这里跑，避免阻塞 UI
        self._search_thread = QThread(self)
        self._search_worker = _SearchWorker()
        self._search_worker.moveToThread(self._search_thread)
        self._search_worker.done_single.connect(self._on_search_done_single)
        self._search_worker.done_global.connect(self._on_search_done_global)
        self._search_worker.error.connect(self._on_search_error)
        self._search_thread.start()
        # 监视窗口单例（Phase 5）
        self._motor_monitor = None
        self._bms_monitor = None
        self._imu_monitor = None
        # 统一单例：不区分 motor/bms/imu，用户打开后通过导入配置文件
        # 决定显示哪些曲线（不同设备日志格式靠自定义解析器区分）
        self._monitor = None

        # 应用 QSS
        app = QApplication.instance() or QApplication(sys.argv)
        self._app = app
        qt_theme.apply_qss(app, theme_name)

        # 窗口
        self.setWindowTitle('日志查看器')
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
            tabs_list_fn=lambda: self._tabs,
            search_worker=self._search_worker)
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

        # 任务3：运行中主题联动——轮询 config 主题变化
        self._start_theme_watch()

        if path:
            self.open_file(path)

    def _start_theme_watch(self):
        """启动 5 秒轮询：主程序切主题写 config，本进程发现变化跟随。

        兼任"跟随系统"实时跟随：pref='auto' 时额外检测 detect_system_theme()
        返回值是否与上次不同，系统切深浅即重新 resolve+apply（修复标题栏变
        浅色但工具栏仍深色的 bug——QSS 必须程序主动 apply）。
        """
        from PySide6.QtCore import QTimer
        # 记住上次检测到的系统深浅，避免无变化时重复 apply
        try:
            self._last_system_kind = themes.detect_system_theme()
        except Exception:
            self._last_system_kind = None
        self._theme_watch_timer = QTimer(self)
        self._theme_watch_timer.setInterval(5000)
        self._theme_watch_timer.timeout.connect(self._check_theme_changed)
        self._theme_watch_timer.start()

    def _check_theme_changed(self):
        """检查 config 主题 + 系统深浅变化，必要时 apply。

        - 检测 config.theme_pref 变化（跨进程联动：主程序切主题写 config）
        - pref='auto' 时额外检测 detect_system_theme() 变化（跟随系统实时跟随）
        """
        from serial_debug.infra import config
        from serial_debug.infra.error_log import log_exc
        try:
            cfg = config.load()
            pref = cfg.get('theme_pref', 'auto')
            expected = themes.resolve(pref)
            # 1) config/pref 变化（跨进程联动）
            if expected != self._theme_name:
                self._theme_pref = pref
                self.apply_theme(expected)
                # apply 后刷新系统深浅记忆，避免下一轮误判
                try:
                    self._last_system_kind = themes.detect_system_theme()
                except Exception as e:
                    log_exc('debug', 'qt_log_viewer: refresh _last_system_kind failed', e)
                return
            # 2) 跟随系统模式：系统深浅变化时重新 resolve+apply
            if pref == 'auto':
                try:
                    sys_kind = themes.detect_system_theme()
                except Exception as e:
                    log_exc('debug', 'qt_log_viewer: detect_system_theme failed', e)
                    sys_kind = None
                if sys_kind is not None and sys_kind != self._last_system_kind:
                    self._last_system_kind = sys_kind
                    new_theme = themes.resolve('auto')
                    if new_theme != self._theme_name:
                        self.apply_theme(new_theme)
        except Exception as e:
            log_exc('warning', 'qt_log_viewer: _check_theme_changed failed', e)


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
        m_view.addSeparator()
        # 主题子菜单（任务3：模式组 + 主题组）
        self._build_theme_menu(m_view)
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
        a = QAction('数据监视…', self)
        a.triggered.connect(self._open_monitor)
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
        tab._search_worker = self._search_worker
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
            '所有文件 (*);;日志与文本 (*.txt *.log *.md *.csv *.json)')
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
        # removeTab 后若 currentIndex 数值未变（如关闭的就是当前 tab 且
        # 还有别的 tab 顶上来），Qt 不会发 currentChanged 信号——
        # 必须显式重绑 filter panel，否则 _tab 还指向已关闭的旧 tab，
        # 后续筛选会继续扫旧 tab 的行数据。
        new_tab = self._current_tab()
        if self._filter_panel is not None:
            self._filter_panel.set_tab(new_tab)
        # 实时刷新按钮状态同步
        if new_tab is not None:
            live = new_tab.is_live_mode()
            self._live_action.setChecked(live)
            self._live_action_menu.setChecked(live)

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
        # 统一单例：实时新行喂给监视窗口（不分 motor/bms/imu）
        for monitor in (self._monitor,):
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
    def _open_monitor(self):
        """统一数据监视入口 → 同进程打开 QtMonitorWindow 单例。

        不再区分 motor/bms/imu：用户通过"导入配置文件"决定显示什么曲线。
        单例：第一次创建，之后 lift/focus。打开时 replay 当前 Tab 全量日志
        （分块 CHUNK=5000，QTimer.singleShot 让出主线程避免卡顿）。
        """
        monitor = self._monitor
        if monitor is not None and monitor._win is not None and monitor._win.isVisible():
            monitor._win.raise_()
            monitor._win.activateWindow()
            return
        from serial_debug.ui.qt_monitor_window import QtMonitorWindow
        monitor = QtMonitorWindow(theme_name=self._theme_name)
        monitor._create()
        try:
            monitor._win.show()
        except Exception:
            pass
        self._monitor = monitor
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
        """应用具体主题名 + 持久化 + 同步菜单。"""
        self._theme_name = theme_name
        self._theme = themes.THEMES.get(theme_name, themes.THEMES['dark'])
        qt_theme.apply_qss(self._app, theme_name)
        for tab in self._tabs:
            tab.apply_theme(self._theme)
        if self._filter_panel is not None:
            self._filter_panel.apply_theme(self._theme)
        self._global_search.apply_theme(self._theme)
        self._sync_theme_menu()

    def _apply_theme_with_pref(self, theme_name, pref=None):
        """应用主题 + 更新模式 + 持久化（任务3）。"""
        if pref is not None:
            self._theme_pref = pref
        self._theme_name = theme_name
        try:
            themes.save_pref(self._theme_pref)
            kind = themes.THEME_KIND.get(theme_name)
            if kind:
                themes.save_specific(kind, theme_name)
        except Exception:
            pass
        self.apply_theme(theme_name)

    def _on_theme_pref_selected(self, pref: str):
        specific = themes.resolve(pref)
        self._apply_theme_with_pref(specific, pref=pref)

    def _on_theme_specific_selected(self, theme_name: str):
        kind = themes.THEME_KIND.get(theme_name)
        if kind is None:
            return
        new_pref = self._theme_pref
        if self._theme_pref != 'auto' and self._theme_pref != kind:
            new_pref = kind
        self._apply_theme_with_pref(theme_name, pref=new_pref)

    def _build_theme_menu(self, parent_menu):
        from PySide6.QtGui import QActionGroup
        m_theme = parent_menu.addMenu('主题')
        mode_grp = QActionGroup(m_theme)
        mode_grp.setExclusive(True)
        self._theme_mode_actions = {}
        for pref, label in (('auto', '跟随系统'),
                            ('light', '浅色'),
                            ('dark', '深色')):
            act = m_theme.addAction(label)
            act.setCheckable(True)
            act.setActionGroup(mode_grp)
            act.triggered.connect(lambda _c=False, p=pref:
                                   self._on_theme_pref_selected(p))
            self._theme_mode_actions[pref] = act
        m_theme.addSeparator()
        theme_grp = QActionGroup(m_theme)
        theme_grp.setExclusive(True)
        self._theme_specific_actions = {}
        for name, label in themes.THEME_DISPLAY:
            act = m_theme.addAction(label)
            act.setCheckable(True)
            act.setActionGroup(theme_grp)
            act.triggered.connect(lambda _c=False, n=name:
                                   self._on_theme_specific_selected(n))
            self._theme_specific_actions[name] = act
        self._sync_theme_menu()
        return m_theme

    def _sync_theme_menu(self):
        pref = getattr(self, '_theme_pref', 'auto')
        name = getattr(self, '_theme_name', 'dark')
        for p, act in getattr(self, '_theme_mode_actions', {}).items():
            act.setChecked(p == pref)
        for n, act in getattr(self, '_theme_specific_actions', {}).items():
            act.setChecked(n == name)

    def _toggle_theme(self):
        """工具栏按钮：在当前深浅的两个默认主题间切换。"""
        cur_kind = themes.THEME_KIND.get(self._theme_name, 'dark')
        new_kind = 'light' if cur_kind == 'dark' else 'dark'
        new_theme = themes.load_specific(new_kind)
        self._apply_theme_with_pref(new_theme, pref=new_kind)

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
        # 检查是否有 tab 的日志内容被修改（只读模式下正常不会修改，此为安全网）
        modified_tabs = []
        for i in range(self._tab_widget.count()):
            tab = self._tab_widget.widget(i)
            if isinstance(tab, _QtLogViewTab) and tab._editor.document().isModified():
                label = self._tab_widget.tabText(i)
                modified_tabs.append(label)
        if modified_tabs:
            from PySide6.QtWidgets import QMessageBox
            names = '\n'.join(modified_tabs[:5])
            if len(modified_tabs) > 5:
                names += f'\n...（共 {len(modified_tabs)} 个）'
            reply = QMessageBox.question(
                self, '内容已修改',
                f'以下日志内容已被修改：\n\n{names}\n\n'
                f'日志查看器是只读工具，修改不会写回原始文件。\n'
                f'是否放弃修改并关闭？（点"取消"返回不关闭）',
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply != QMessageBox.Yes:
                event.ignore()
                return
        if self._filter_panel is not None:
            try:
                self._filter_panel.shutdown_filter_thread()
            except Exception:
                pass
        try:
            self._search_thread.quit()
            self._search_thread.wait(2000)
        except Exception:
            pass
        self._on_close()
        super().closeEvent(event)

    # ---- 后台搜索回调 -------------------------------------------------
    def _on_search_done_single(self, epoch: int, matches: list, info: str):
        """当前文件搜索完成：路由到当前 tab。"""
        tab = self._tab_widget.currentWidget()
        if isinstance(tab, _QtLogViewTab):
            tab._on_search_done(epoch, matches, info)

    def _on_search_done_global(self, epoch: int, results: list, info: str):
        self._global_search._on_search_done(epoch, results, info)

    def _on_search_error(self, epoch: int, msg: str):
        # 当前 tab 单文件搜索 + 全局搜索共享 epoch 空间：两个面板都尝试接收
        tab = self._tab_widget.currentWidget()
        if isinstance(tab, _QtLogViewTab):
            tab._on_search_error(epoch, msg)
        self._global_search._on_search_error(epoch, msg)

    def run(self):
        self.show()
        return self._app.exec()


# ---- 筛选面板（Phase 2）---------------------------------------------
def _is_dangerous_regex(text: str) -> bool:
    """检测灾难性回溯正则模式：嵌套量词 + 同一分支重叠。

    例如 (a+)+, (a*)*, (a|a)*, (.+)* 等会让长行进入指数级回溯。
    仅做粗略静态检测——足够阻止用户粘贴的明显危险模式。
    """
    if not text:
        return False
    # 嵌套量词：(...)+ 内部又以 +/?/*/{} 结尾，且内部非锚定常量
    # 例：(\w+)+, (a+)*, (.*?)+
    if re.search(r'\([^()]*[+*?][^()]*\)[+*?]', text):
        return True
    # 重复量词本身嵌套：a++, a*+, a?+
    if re.search(r'[+*?]\s*[+*?]', text):
        return True
    # 大括号嵌套量词：a{2,}{3,}
    if re.search(r'\}\s*\{', text):
        return True
    return False


class _FilterWorker(QObject):
    """后台筛选 worker：扫描行 + 编译正则 + 截断超长行 + 超时中断。

    通过 done 信号把结果回传主线程；通过 epoch 序号让主线程丢弃过期结果。
    运行在独立 QThread 中，避免正则灾难性回溯冻结 UI。

    关键：lines/tags 不通过 signal 传递（跨线程 QueuedConnection 会深拷贝整个
    list，大文件几十万行直接卡死主线程）。改为 worker 持有 tab 引用，signal
    只传轻量参数（epoch + regex_text + level_idx + result_cap），worker 直接
    读 tab 的 lines/tags 引用——Python 对象跨线程共享引用不拷贝。
    """
    start_run = Signal(int, str, int, int, str)  # epoch, text, level_idx, cap, match_mode
    done = Signal(int, object, int, str)   # epoch, filtered_idx, total, info
    error = Signal(int, str)                # epoch, error_msg

    def __init__(self):
        super().__init__()
        self._epoch = 0
        self._tab = None  # 由主线程 set_tab() 注入，共享引用不拷贝
        self.start_run.connect(self.run, Qt.QueuedConnection)

    def set_tab(self, tab):
        """主线程切换文件时调用，注入当前 tab 引用（轻量，不拷贝数据）。"""
        self._tab = tab

    @Slot(int, str, int, int, str)
    def run(self, epoch: int, text: str, level_idx: int, result_cap: int,
            match_mode: str):
        """单次筛选扫描。主线程通过 epoch 序号取消过期结果。

        match_mode: 'fuzzy'=子串包含, 'exact'=整行精确相等, 'regex'=正则搜索
        """
        try:
            if self._tab is None:
                self.error.emit(epoch, '无活动日志')
                return
            lines = self._tab.all_lines()
            tags = self._tab._line_tags
            regex = None
            if text:
                if match_mode == 'regex':
                    if _is_dangerous_regex(text):
                        self.error.emit(epoch, '正则存在灾难性回溯风险（嵌套量词）'
                                        '，已拒绝执行。请简化正则。')
                        return
                    try:
                        regex = re.compile(text)
                    except re.error as e:
                        self.error.emit(epoch, f'正则错误：{e}')
                        return
                # fuzzy / exact 不需要编译正则，直接用字符串操作
            start_ts = time.monotonic()
            deadline = start_ts + FILTER_SCAN_TIMEOUT_MS / 1000.0
            total = len(lines)
            filtered: list[int] = []
            last_level = level_idx
            for i, ln in enumerate(lines):
                if (i & 0x3FF) == 0 and time.monotonic() > deadline:
                    # 超时：返回已扫到的部分 + 警告
                    self.done.emit(epoch, filtered, total,
                                   f'筛选超时，已扫 {i}/{total} 行'
                                   f'（{len(filtered)} 命中）。'
                                   f'可能正则过慢或行数过多。')
                    return
                if len(filtered) >= result_cap:
                    break
                if last_level != 0:
                    tag = tags[i] if i < len(tags) else 'log'
                    if not _level_filter_static(last_level, ln, tag):
                        continue
                if text:
                    scan_ln = ln if len(ln) <= FILTER_MAX_LINE_LEN \
                        else ln[:FILTER_MAX_LINE_LEN]
                    if match_mode == 'fuzzy':
                        if text not in scan_ln:
                            continue
                    elif match_mode == 'exact':
                        if scan_ln.strip() != text:
                            continue
                    else:  # regex
                        if not regex.search(scan_ln):
                            continue
                filtered.append(i)
            self.done.emit(epoch, filtered, total,
                            f'{len(filtered)} / {total} 行')
        except Exception as e:
            self.error.emit(epoch, f'筛选异常：{e}')


def _level_filter_static(level_idx: int, line: str, tag: str) -> bool:
    """级别筛选纯函数（worker 线程也能用，不依赖 self）。"""
    if level_idx == 0:
        return True
    if level_idx == 1:
        return tag == 'log'
    if level_idx == 2:
        return tag == 'resp'
    if level_idx == 3:
        return tag == 'note'
    return True


class _SearchWorker(QObject):
    """后台搜索 worker：单文件搜索 + 全局（多文件）搜索。

    单线程串行扫所有 tab：re 模块会释放 GIL，但 Python 字节码本身受 GIL
    约束。真正加速来自不阻塞 UI + 超时截断 + 批量结果回传。
    """
    # 触发信号：主线程 emit → worker 线程事件循环调槽（QueuedConnection）
    start_run_single = Signal(int, object, str)
    start_run_global = Signal(int, object, str)
    done_single = Signal(int, object, str)  # epoch, matches, info
    done_global = Signal(int, object, str)   # epoch, results, info
    error = Signal(int, str)

    def __init__(self):
        super().__init__()
        self.start_run_single.connect(self.run_single, Qt.QueuedConnection)
        self.start_run_global.connect(self.run_global, Qt.QueuedConnection)

    @Slot(int, object, str)
    def run_single(self, epoch: int, lines: list, regex_text: str):
        """单文件搜索：返回匹配行号 list[int]。"""
        try:
            if _is_dangerous_regex(regex_text):
                self.error.emit(epoch, '正则存在嵌套量词，可能引发灾难性回溯，已拒绝')
                return
            try:
                regex = re.compile(regex_text)
            except re.error as e:
                self.error.emit(epoch, f'正则错误：{e}')
                return
            start = time.monotonic()
            deadline = start + SEARCH_SCAN_TIMEOUT_MS / 1000.0
            matches: list[int] = []
            scanned = 0
            total = len(lines)
            for i, ln in enumerate(lines):
                if (i & 0x3FF) == 0 and time.monotonic() > deadline:
                    self.done_single.emit(epoch, matches,
                        f'搜索超时：已扫 {i}/{total} 行（{len(matches)} 命中）')
                    return
                if scanned > SEARCH_SCAN_LIMIT:
                    break
                scanned += 1
                scan_ln = ln if len(ln) <= SEARCH_MAX_LINE_LEN \
                    else ln[:SEARCH_MAX_LINE_LEN]
                if regex.search(scan_ln):
                    matches.append(i)
                    if len(matches) >= GLOBAL_SEARCH_MAX * 4:
                        break
            self.done_single.emit(epoch, matches,
                f'{len(matches)} 个匹配 / {total} 行')
        except Exception as e:
            self.error.emit(epoch, f'搜索异常：{e}')

    @Slot(int, object, str)
    def run_global(self, epoch: int, tabs_snapshot: list, regex_text: str):
        """全局搜索：扫所有 tab，cap GLOBAL_SEARCH_MAX。

        tabs_snapshot 是 [(tab_idx, lines_copy, filename)] 的快照，
        避免 worker 线程访问 _QtLogViewTab 实例对象（线程安全）。
        """
        try:
            if _is_dangerous_regex(regex_text):
                self.error.emit(epoch, '正则存在嵌套量词，可能引发灾难性回溯，已拒绝')
                return
            try:
                regex = re.compile(regex_text)
            except re.error as e:
                self.error.emit(epoch, f'正则错误：{e}')
                return
            start = time.monotonic()
            deadline = start + SEARCH_SCAN_TIMEOUT_MS / 1000.0
            results: list = []  # (tab_idx, line_idx, col, preview, filename)
            total = 0
            for tab_idx, lines, fname in tabs_snapshot:
                if time.monotonic() > deadline:
                    self.done_global.emit(epoch, results,
                        f'搜索超时：已扫 {len(results) and results[-1][4] or "?"}'
                        f'（{total} 命中）')
                    return
                for i, ln in enumerate(lines):
                    if (i & 0x3FF) == 0 and time.monotonic() > deadline:
                        self.done_global.emit(epoch, results,
                            f'搜索超时：文件 {fname} 已扫 {i}/{len(lines)}'
                            f'（{total} 命中）')
                        return
                    scan_ln = ln if len(ln) <= SEARCH_MAX_LINE_LEN \
                        else ln[:SEARCH_MAX_LINE_LEN]
                    m = regex.search(scan_ln)
                    if m:
                        preview = ln[:100]
                        results.append((tab_idx, i, m.start(), preview, fname))
                        total += 1
                        if total >= GLOBAL_SEARCH_MAX:
                            self.done_global.emit(epoch, results,
                                f'已截断 {GLOBAL_SEARCH_MAX} 个结果（共更多）')
                            return
            self.done_global.emit(epoch, results, f'{total} 个结果')
        except Exception as e:
            self.error.emit(epoch, f'搜索异常：{e}')


class _QtFilterPanel(QDockWidget):
    """筛选面板（QDockWidget，自动支持拖出/嵌入）。

    级别 Combobox + 正则 Entry + 结果 QTreeWidget。
    双击 Tree → on_locate(line_idx) → tab.goto_line。
    Text 点击 → locate_line 选中最近匹配。
    增量筛选：_on_lines_appended(new_count) 只扫新增行，cap = _get_filter_max()（可配置）。
    """

    def __init__(self, parent, theme, on_locate=None):
        super().__init__('筛选', parent)
        self._theme = theme
        self._tab: _QtLogViewTab | None = None
        self._filtered_idx: list[int] = []
        self._last_scanned_idx = 0
        self._on_locate = on_locate
        # 显示懒加载：_filtered_idx 存全部命中，但树只插前 N 行，避免 QTreeWidget
        # 一次性插入几十万 QTreeWidgetItem 导致内存爆 + 渲染卡死。
        # _displayed_count = 当前树中已插入的命中数（= _filtered_idx 的前缀）。
        self._displayed_count = 0
        # 后台筛选：worker 运行在独立 QThread，避免正则灾难性回溯冻结 UI
        self._filter_epoch = 0
        self._filter_thread = QThread(self)
        self._filter_worker = _FilterWorker()
        self._filter_worker.moveToThread(self._filter_thread)
        self._filter_worker.done.connect(self._on_filter_done)
        self._filter_worker.error.connect(self._on_filter_error)
        self._filter_thread.start()
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
        from PySide6.QtWidgets import QHBoxLayout, QLabel
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
        # 筛选结果行数上限：从 config 读（默认 500000），spinbox 可改
        cfg = config.load()
        self._filter_max_value = int(
            cfg.get('log_view_filter_result_max',
                    config.DEFAULTS['log_view_filter_result_max']))
        # 无限制模式：True 时忽略 spinbox 上限，_get_filter_max 返回大数（2e9）
        # 不截断；spinbox 仍保存值但禁用变灰。见 _save_unlimited / _get_filter_max。
        self._unlimited = bool(
            cfg.get('log_view_filter_unlimited',
                    config.DEFAULTS['log_view_filter_unlimited']))
        self._build_widgets()
        self.apply_theme(theme)

    def _get_filter_max(self) -> int:
        """当前筛选结果行数上限。

        无限制模式（self._unlimited=True）：返回 2_000_000_000（int 上限内，
        `>=` 比较正常不截断），spinbox 值保存但不用。
        有上限模式：取 spinbox 值，兜底内存值 / 常量。
        """
        if self._unlimited:
            return 2_000_000_000
        try:
            if hasattr(self, '_max_rows_box') and self._max_rows_box is not None:
                return int(self._max_rows_box.value())
        except Exception:
            pass
        try:
            return int(self._filter_max_value)
        except (TypeError, ValueError):
            return FILTER_RESULT_MAX

    def _save_filter_max(self, value: int):
        """spinbox 值变化：存内存 + 持久化到 config，并触发重筛。

        无限制模式下 spinbox 被禁用，不会触发 valueChanged，本方法不执行。
        """
        self._filter_max_value = int(value)
        try:
            cfg = config.load()
            cfg['log_view_filter_result_max'] = int(value)
            config.save(cfg)
        except Exception:
            pass
        self._apply_filter()

    def _save_unlimited(self, checked: bool):
        """切换无限制模式：持久化 + 启用/禁用 spinbox + 重筛。"""
        self._unlimited = bool(checked)
        try:
            self._max_rows_box.setEnabled(not self._unlimited)
        except Exception:
            pass
        try:
            cfg = config.load()
            cfg['log_view_filter_unlimited'] = self._unlimited
            config.save(cfg)
        except Exception:
            pass
        self._apply_filter()

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
        # 匹配模式 Combobox + Label 同行
        self._match_mode_box = QComboBox()
        self._match_mode_box.addItem('模糊匹配', 'fuzzy')
        self._match_mode_box.addItem('精确匹配', 'exact')
        self._match_mode_box.addItem('正则匹配', 'regex')
        self._match_mode_box.setCurrentIndex(0)
        self._match_mode_box.currentIndexChanged.connect(self._apply_filter)
        mode_row = QWidget()
        ml = QHBoxLayout(mode_row)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(6)
        ml.addWidget(QLabel('匹配'))
        ml.addWidget(self._match_mode_box, 1)
        v.addWidget(mode_row)
        # 关键字 Entry + Label 同行
        self._regex_edit = QLineEdit()
        self._regex_edit.setPlaceholderText('输入关键字（回车应用）')
        self._regex_edit.returnPressed.connect(self._apply_filter)
        regex_row = QWidget()
        rl = QHBoxLayout(regex_row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(6)
        rl.addWidget(QLabel('关键字'))
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
        # 筛选结果行数上限 spinbox（可配置，持久化到 config）
        self._max_rows_box = QSpinBox()
        self._max_rows_box.setRange(1000, 2_000_000)
        self._max_rows_box.setSingleStep(10000)
        self._max_rows_box.setValue(int(self._filter_max_value))
        self._max_rows_box.setGroupSeparatorShown(True)
        self._max_rows_box.setToolTip(
            '筛选结果最大行数（超过截断）。修改后自动重新筛选并保存设置')
        # 编辑延迟提交：避免输入途中反复触发重筛
        from PySide6.QtCore import QTimer
        self._max_rows_edit_timer = QTimer(self)
        self._max_rows_edit_timer.setSingleShot(True)
        self._max_rows_edit_timer.setInterval(400)
        self._max_rows_edit_timer.timeout.connect(
            lambda: self._save_filter_max(self._max_rows_box.value()))
        self._max_rows_box.valueChanged.connect(
            lambda _v: self._max_rows_edit_timer.start())
        max_row = QWidget()
        ml = QHBoxLayout(max_row)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(6)
        ml.addWidget(QLabel('上限'))
        ml.addWidget(self._max_rows_box, 1)
        # 无限制复选框：勾选后 spinbox 变灰，_get_filter_max 返回 2e9 不截断
        self._unlimited_box = QCheckBox('无限制')
        self._unlimited_box.setChecked(self._unlimited)
        self._unlimited_box.setToolTip(
            '勾选后不限制筛选结果行数（可能加载数十万行，操作变慢）')
        self._unlimited_box.toggled.connect(self._save_unlimited)
        ml.addWidget(self._unlimited_box)
        v.addWidget(max_row)
        # 初始禁用 spinbox 若已是无限制模式
        if self._unlimited:
            self._max_rows_box.setEnabled(False)
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
        # "加载更多"按钮：命中数超过树已显示数时可见，点击加载下一批。
        # 解决无限制模式下命中几十万行 QTreeWidget 一次性插入卡死的问题。
        self._load_more_btn = QPushButton('加载更多…')
        self._load_more_btn.setVisible(False)
        self._load_more_btn.clicked.connect(self._load_more)
        v.addWidget(self._load_more_btn)
        self.setWidget(container)
        # 内部控件不卡最小宽度，允许 DockWidget 拖到任意窄
        for w in (self._level_box, self._regex_edit, self._tree,
                  self._apply_btn, self._clear_btn, self._info_lbl,
                  self._max_rows_box, self._unlimited_box, btn_row, container,
                  level_row, regex_row, self._load_more_btn):
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
        # 立即清旧 tab 的结果显示 + 重置 epoch（旧 worker 回调被丢弃）；
        # 否则切 tab 时旧结果还会显示一会儿，且 _insert_into_tree 用
        # 当前 self._tab 的行数据但旧 line_idx 会跨文件错位。
        self._filter_epoch += 1
        self._filtered_idx = []
        self._last_scanned_idx = 0
        self._displayed_count = 0
        self._tree.clear()
        self._load_more_btn.setVisible(False)
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
        """全量筛选：投递给后台 worker，避免正则灾难性回溯冻结 UI。

        反复修改关键字时，前一次扫描还没完，又一次触发——
        主线程立即返回，worker 用新 epoch 启动新扫描，旧 epoch 的结果
        在 _on_filter_done 中被丢弃。这样 UI 线程永远不阻塞。

        关键优化：lines/tags 不通过 signal 传（跨线程深拷贝大 list 会卡死主线程），
        改为 set_tab 注入 tab 引用，worker 直接读 tab.all_lines()，共享引用零拷贝。
        """
        if self._tab is None:
            return
        text = self._regex_edit.text()
        match_mode = self._match_mode_box.currentData() or 'fuzzy'
        # 正则模式才做语法预检；模糊/精确匹配不需要编译正则
        if text and match_mode == 'regex':
            if _is_dangerous_regex(text):
                self._info_lbl.setText(
                    '正则存在嵌套量词，可能引发灾难性回溯，已拒绝执行')
                self._filtered_idx = []
                self._tree.clear()
                return
            try:
                re.compile(text)
            except re.error as e:
                self._info_lbl.setText(f'正则错误：{e}')
                self._filtered_idx = []
                self._tree.clear()
                return
        # 取新序号 + 通过 Signal 触发 worker（跨线程 QueuedConnection）
        self._filter_epoch += 1
        ep = self._filter_epoch
        level_idx = self._level_box.currentIndex()
        self._info_lbl.setText('筛选中…')
        # 注入当前 tab 引用（轻量，不拷贝数据），worker 直接读 lines/tags
        self._filter_worker.set_tab(self._tab)
        # Signal 只传轻量参数（epoch + text + level_idx + cap + match_mode），不传大 list
        self._filter_worker.start_run.emit(
            ep, text, level_idx, self._get_filter_max(), match_mode)

    @Slot(int, list, int, str)
    def _on_filter_done(self, epoch: int, filtered: list, total: int, info: str):
        """worker 完成回调：过期 epoch 丢弃，避免旧结果覆盖新状态。"""
        if epoch != self._filter_epoch:
            return
        self._filtered_idx = filtered
        self._last_scanned_idx = total
        self._render_results(filtered, total)
        # 信息：命中数 vs 已显示数，超显示上限时提示
        shown = self._displayed_count
        if len(filtered) > shown:
            info = (f'{info}（命中 {len(filtered)} 行，仅显示前 {shown} 行，'
                    '点击「加载更多」或精确筛选）')
        # 无限制模式下命中行数过多时追加警告，引导用户筛更精确
        if self._unlimited and len(filtered) > 500000:
            info = (f'{info}  ⚠ 已加载 {len(filtered)} 行，'
                    '继续操作可能变慢，建议筛更精确')
        self._info_lbl.setText(info)

    @Slot(int, str)
    def _on_filter_error(self, epoch: int, msg: str):
        if epoch != self._filter_epoch:
            return
        self._filtered_idx = []
        self._displayed_count = 0
        self._tree.clear()
        self._load_more_btn.setVisible(False)
        self._info_lbl.setText(msg)

    def _apply_filter_incremental(self, new_count: int):
        """增量筛选：扫 [last, last+new_count)，cap = _get_filter_max() - len(already)。"""
        if self._tab is None:
            return
        lines = self._tab.all_lines()
        tags = self._tab._line_tags
        text = self._regex_edit.text()
        match_mode = self._match_mode_box.currentData() or 'fuzzy'
        regex = None
        if text and match_mode == 'regex':
            try:
                regex = re.compile(text)
            except re.error:
                return
        result_cap = self._get_filter_max()
        if len(self._filtered_idx) >= result_cap:
            self._last_scanned_idx = len(lines)
            return
        start = self._last_scanned_idx
        end = min(len(lines), start + new_count)
        new_filtered: list[int] = []
        for i in range(start, end):
            if len(self._filtered_idx) + len(new_filtered) >= result_cap:
                break
            ln = lines[i]
            tag = tags[i] if i < len(tags) else 'log'
            if not self._level_filter(ln, tag):
                continue
            if text:
                scan_ln = ln if len(ln) <= FILTER_MAX_LINE_LEN \
                    else ln[:FILTER_MAX_LINE_LEN]
                if match_mode == 'fuzzy':
                    if text not in scan_ln:
                        continue
                elif match_mode == 'exact':
                    if scan_ln.strip() != text:
                        continue
                else:  # regex
                    if not regex.search(scan_ln):
                        continue
            new_filtered.append(i)
        if new_filtered:
            self._filtered_idx.extend(new_filtered)
            # 仅在未达显示上限时插入新命中到树（保持树 = _filtered_idx 前缀）
            if self._displayed_count < FILTER_DISPLAY_MAX:
                remain = FILTER_DISPLAY_MAX - self._displayed_count
                to_show = new_filtered[:remain]
                if to_show:
                    self._insert_into_tree(to_show)
                    self._displayed_count += len(to_show)
            self._update_load_more_btn()
        self._last_scanned_idx = end
        info = (f'{len(self._filtered_idx)} / {len(lines)} 行'
                f'（增量 +{len(new_filtered)}）')
        if len(self._filtered_idx) > self._displayed_count:
            info = (f'{info}（命中 {len(self._filtered_idx)} 行，'
                    f'仅显示前 {self._displayed_count} 行）')
        # 无限制模式下累计命中过多时追加警告
        if self._unlimited and len(self._filtered_idx) > 500000:
            info = (f'{info}  ⚠ 已加载 {len(self._filtered_idx)} 行，'
                    '继续操作可能变慢，建议筛更精确')
        self._info_lbl.setText(info)

    def _render_results(self, filtered: list[int], total: int = 0):
        """全量渲染：清 Tree + 异步分批插入命中行（显示懒加载）。

        _filtered_idx 存全部命中（int 列表），但 QTreeWidget 一次性插入大量
        QTreeWidgetItem 会冻 UI。改为 QTimer 分批插入（每批 FILTER_INSERT_BATCH
        行，批次间 processEvents 让 UI 呼吸），避免主线程长阻塞。
        """
        # 取消上一次异步插入（如果有）
        if hasattr(self, '_render_timer') and self._render_timer is not None:
            self._render_timer.stop()
        self._tree.clear()
        self._displayed_count = 0
        display_slice = filtered[:FILTER_DISPLAY_MAX]
        if not display_slice:
            if total == 0:
                total = len(self._tab.all_lines()) if self._tab else 0
            self._update_load_more_btn()
            self._update_info_text(total)
            return
        # 缓存渲染参数，供 _render_next_batch 使用
        self._render_pending = list(display_slice)
        self._render_done = False
        self._render_total = total
        # 用 QTimer 零延迟分批插入（每批回来让事件循环处理 UI 事件）
        if not hasattr(self, '_render_timer') or self._render_timer is None:
            self._render_timer = QTimer(self)
            self._render_timer.setSingleShot(True)
            self._render_timer.timeout.connect(self._render_next_batch)
        self._render_timer.start(0)

    def _render_next_batch(self):
        """QTimer 回调：插入下一批 QTreeWidgetItem，批次间让 UI 呼吸。"""
        if not hasattr(self, '_render_pending') or not self._render_pending:
            return
        lines = self._tab.all_lines() if self._tab else []
        tags = self._tab._line_tags if self._tab else []
        # 预取颜色，避免每行调 dict.get + 创建 QColor
        c_log = QColor(self._color_for_tag('log'))
        c_resp = QColor(self._color_for_tag('resp'))
        c_note = QColor(self._color_for_tag('note'))
        color_map = {'log': c_log, 'resp': c_resp, 'note': c_note}
        batch = self._render_pending[:FILTER_INSERT_BATCH]
        self._render_pending = self._render_pending[FILTER_INSERT_BATCH:]
        items = []
        for line_idx in batch:
            ln = lines[line_idx] if line_idx < len(lines) else ''
            tag = tags[line_idx] if line_idx < len(tags) else 'log'
            item = QTreeWidgetItem([str(line_idx + 1), ln])
            item.setData(0, Qt.UserRole, line_idx)
            c = color_map.get(tag, c_log)
            item.setForeground(0, c)
            item.setForeground(1, c)
            items.append(item)
        self._tree.addTopLevelItems(items)
        self._displayed_count += len(batch)
        if not self._render_pending:
            # 全部插入完成
            self._render_done = True
            if self._tree.topLevelItemCount() > 0:
                self._tree.setCurrentItem(self._tree.topLevelItem(0))
            self._update_load_more_btn()
            self._update_info_text(self._render_total)
        else:
            # 还有批次待插入，继续
            self._render_timer.start(0)

    def _insert_into_tree(self, idx_list: list[int]):
        """同步分批插入 QTreeWidgetItem（用于 load_more / locate_line）。

        idx_list 是要插入的 line_idx 列表（已截断到显示范围，由调用方负责）。
        批量小（FILTER_INSERT_BATCH=200），主线程每次插入不超过 200 行。
        """
        if not idx_list:
            return
        lines = self._tab.all_lines() if self._tab else []
        tags = self._tab._line_tags if self._tab else []
        c_log = QColor(self._color_for_tag('log'))
        c_resp = QColor(self._color_for_tag('resp'))
        c_note = QColor(self._color_for_tag('note'))
        color_map = {'log': c_log, 'resp': c_resp, 'note': c_note}
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
                c = color_map.get(tag, c_log)
                item.setForeground(0, c)
                item.setForeground(1, c)
                items.append(item)
            self._tree.addTopLevelItems(items)
            start = end
        # 跳到顶部（仅全量渲染后；增量/load_more 不跳）
        if self._tree.topLevelItemCount() > 0:
            self._tree.setCurrentItem(self._tree.topLevelItem(0))

    def _update_load_more_btn(self):
        """「加载更多」按钮可见性：命中数 > 已显示数 时显示。"""
        self._load_more_btn.setVisible(
            len(self._filtered_idx) > self._displayed_count)

    def _load_more(self):
        """「加载更多」按钮：插入下一批命中到树。"""
        start = self._displayed_count
        end = min(start + FILTER_LOAD_MORE_BATCH, len(self._filtered_idx))
        if start >= end:
            return
        self._tree.setUpdatesEnabled(False)
        try:
            self._insert_into_tree_no_jump(self._filtered_idx[start:end])
            self._displayed_count = end
        finally:
            self._tree.setUpdatesEnabled(True)
        self._update_load_more_btn()
        self._update_info_text()

    def _insert_into_tree_no_jump(self, idx_list: list[int]):
        """分批插入但不跳到顶部（用于 load_more / ensure_displayed）。"""
        if not idx_list:
            return
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
                color = self._color_for_tag(tag)
                item.setForeground(0, QColor(color))
                item.setForeground(1, QColor(color))
                items.append(item)
            self._tree.addTopLevelItems(items)
            start = end

    def _ensure_displayed_to(self, pos: int):
        """确保树已插入到覆盖 idx_list 位置 pos 的行（供 locate_line 用）。

        pos 是 _filtered_idx 中的下标。若 pos >= _displayed_count，按需加载
        到 pos + 1（向上取整到 FILTER_LOAD_MORE_BATCH 批，减少反复加载）。
        """
        if pos < self._displayed_count:
            return
        target = min(len(self._filtered_idx),
                      pos + 1 + FILTER_LOAD_MORE_BATCH)
        if target <= self._displayed_count:
            return
        self._tree.setUpdatesEnabled(False)
        try:
            self._insert_into_tree_no_jump(
                self._filtered_idx[self._displayed_count:target])
            self._displayed_count = target
        finally:
            self._tree.setUpdatesEnabled(True)
        self._update_load_more_btn()
        self._update_info_text()

    def _update_info_text(self, total: int | None = None):
        """刷新 info label（命中/已显示/总数），保留无限制警告。"""
        if total is None:
            total = (len(self._tab.all_lines()) if self._tab else 0)
        info = (f'{len(self._filtered_idx)} / {total} 行')
        if len(self._filtered_idx) > self._displayed_count:
            info = (f'{info}（命中 {len(self._filtered_idx)} 行，'
                    f'仅显示前 {self._displayed_count} 行）')
        if self._unlimited and len(self._filtered_idx) > 500000:
            info = (f'{info}  ⚠ 已加载 {len(self._filtered_idx)} 行，'
                    '继续操作可能变慢，建议筛更精确')
        self._info_lbl.setText(info)

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
        """主 Text 点击 → 选中 <= line_idx 的最近匹配行。

        显示懒加载：若 pos 超过树已显示数，先按需加载到 pos+1 再定位。
        """
        # 二分查找 filtered_idx 中 <= line_idx 的最大值
        pos = bisect_right(self._filtered_idx, line_idx) - 1
        if pos < 0:
            return
        # 懒加载：目标 pos 超过树已插入行数时，按需加载到覆盖 pos 的批次
        if pos >= self._displayed_count:
            self._ensure_displayed_to(pos)
        item = self._tree.topLevelItem(pos)
        if item is None:
            return
        self._tree.setCurrentItem(item)
        self._tree.scrollToItem(item, QTreeWidget.PositionAtCenter)
        self._tree.setFocus()

    def clear_results(self):
        self._tree.clear()
        self._filtered_idx = []
        self._displayed_count = 0
        self._last_scanned_idx = 0
        self._load_more_btn.setVisible(False)
        self._info_lbl.setText('就绪')

    def shutdown_filter_thread(self):
        """关闭 worker 线程：主窗口 closeEvent 时调用，避免 QThread 析构警告。"""
        try:
            # 推进 epoch 让 worker 即便完成也丢弃结果
            self._filter_epoch += 1
            self._filter_thread.quit()
            self._filter_thread.wait(2000)  # 最多等 2s
        except Exception:
            pass


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
