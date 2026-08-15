"""ui/qt_log_viewer_window.py 测试：辅助函数 + 异步搜索 worker + 文件 IO。

聚焦纯函数 + _SearchWorker/_FilterWorker 后台扫描逻辑。
"""
from __future__ import annotations

import os
import time

import pytest

pytest.importorskip('PySide6')


# ---- _classify ----

def test_classify_resp():
    """OK/ERROR/+ 开头行应分类为 'resp'。"""
    from serial_debug.ui.qt_log_viewer_window import _classify
    assert _classify('OK,0%') == 'resp'
    assert _classify('ERROR') == 'resp'
    assert _classify('+CME: 0') == 'resp'


def test_classify_note():
    """warning/error 等关键词应分类为 'note'。"""
    from serial_debug.ui.qt_log_viewer_window import _classify
    assert _classify('W warning text') == 'note'
    assert _classify('E error occurred') == 'note'
    assert _classify('something failed to') == 'note'


def test_classify_log():
    """默认周期日志应分类为 'log'。"""
    from serial_debug.ui.qt_log_viewer_window import _classify
    assert _classify('783587 I model 3wd1t_1 0 0 0') == 'log'
    assert _classify('plain text line') == 'log'


# ---- _convert_ts ----

def test_convert_ts_with_13_digit_timestamp():
    """13 位 ms 时间戳应转换为 [HH:MM:SS.mmm] 前缀。"""
    from serial_debug.ui.qt_log_viewer_window import _convert_ts
    # 1750000000000 = 2025-06-15 16:53:20 UTC (本地时区差异)
    result = _convert_ts('1750000000000 some log')
    assert result.startswith('[')
    assert '.' in result
    assert '] ' in result


def test_convert_ts_without_timestamp_returns_empty():
    """无 13 位时间戳应返回空串。"""
    from serial_debug.ui.qt_log_viewer_window import _convert_ts
    assert _convert_ts('plain text') == ''
    assert _convert_ts('12345 short') == ''


# ---- _is_dangerous_regex ----

def test_is_dangerous_regex_detects_nested_quantifiers():
    """嵌套量词 (a+)+ 应被识别为危险。"""
    from serial_debug.ui.qt_log_viewer_window import _is_dangerous_regex
    assert _is_dangerous_regex(r'(a+)+')
    assert _is_dangerous_regex(r'(.+)*')
    assert _is_dangerous_regex(r'(a*)*')


def test_is_dangerous_regex_detects_double_quantifiers():
    """a++ a*+ a?+ 应被识别为危险。"""
    from serial_debug.ui.qt_log_viewer_window import _is_dangerous_regex
    assert _is_dangerous_regex('a++')
    assert _is_dangerous_regex('a*+')
    assert _is_dangerous_regex('a?+')


def test_is_dangerous_regex_allows_normal_patterns():
    """正常正则应通过。"""
    from serial_debug.ui.qt_log_viewer_window import _is_dangerous_regex
    assert not _is_dangerous_regex('hello')
    assert not _is_dangerous_regex(r'\d+ ms')
    assert not _is_dangerous_regex(r'OK|ERROR')
    assert not _is_dangerous_regex('')  # 空串安全


# ---- _level_filter_static ----

def test_level_filter_static_all():
    """level=0 表示全部通过。"""
    from serial_debug.ui.qt_log_viewer_window import _level_filter_static
    assert _level_filter_static(0, 'any', 'log') is True
    assert _level_filter_static(0, 'any', 'resp') is True


def test_level_filter_static_log_only():
    """level=1 只过 log 类。"""
    from serial_debug.ui.qt_log_viewer_window import _level_filter_static
    assert _level_filter_static(1, 'log line', 'log') is True
    assert _level_filter_static(1, 'OK', 'resp') is False


def test_level_filter_static_resp_only():
    """level=2 只过 resp 类。"""
    from serial_debug.ui.qt_log_viewer_window import _level_filter_static
    assert _level_filter_static(2, 'OK', 'resp') is True
    assert _level_filter_static(2, 'log', 'log') is False


def test_level_filter_static_note_only():
    """level=3 只过 note 类。"""
    from serial_debug.ui.qt_log_viewer_window import _level_filter_static
    assert _level_filter_static(3, 'warn', 'note') is True
    assert _level_filter_static(3, 'log', 'log') is False


# ---- _QtLogViewTab 文件加载 ----

def test_qt_log_view_tab_loads_file(qtbot, tmp_path):
    """_QtLogViewTab 加载日志文件，应填 _all_lines + 渲染首批。"""
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from serial_debug.ui.qt_log_viewer_window import _QtLogViewTab
    # 写 100 行测试日志
    log_path = tmp_path / 'test.log'
    log_path.write_text('\n'.join(f'{i:013d} line {i}' for i in range(100)) + '\n',
                        encoding='utf-8')
    # 不需要真主题：用 dark
    from serial_debug.infra import themes
    theme = themes.THEMES['dark']
    tab = _QtLogViewTab(parent=None, path=str(log_path), theme=theme)
    qtbot.addWidget(tab)
    # _load_file 在 __init__ 用 after(50) 调度，这里直接调一次
    tab._load_file()
    assert len(tab._all_lines) == 100
    # 应已渲染首批
    assert tab._rendered_count > 0


def test_qt_log_view_tab_load_missing_file(qtbot, tmp_path):
    """加载不存在的文件应走 except 分支，不抛。"""
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from serial_debug.ui.qt_log_viewer_window import _QtLogViewTab
    from serial_debug.infra import themes
    theme = themes.THEMES['dark']
    tab = _QtLogViewTab(parent=None, path=str(tmp_path / 'nope.log'),
                        theme=theme)
    qtbot.addWidget(tab)
    # 不应抛
    tab._load_file()
    # 空 all_lines
    assert tab._all_lines == []


def test_qt_log_view_tab_live_mode_toggle(qtbot, tmp_path):
    """set_live_mode 切换 + stop_live 关闭。"""
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from serial_debug.ui.qt_log_viewer_window import _QtLogViewTab
    from serial_debug.infra import themes
    theme = themes.THEMES['dark']
    # 写一个新文件
    log_path = tmp_path / 'live.log'
    log_path.write_text('initial\n', encoding='utf-8')
    tab = _QtLogViewTab(parent=None, path=str(log_path), theme=theme)
    qtbot.addWidget(tab)
    tab._load_file()
    # _load_file 自动检测 mtime<60s 应自动开 live；这里不假设状态
    # 强制关闭再开
    tab.set_live_mode(False)
    assert not tab.is_live_mode()
    tab.set_live_mode(True)
    assert tab.is_live_mode()
    tab.set_live_mode(False)
    assert not tab.is_live_mode()
    tab.stop_live()


def test_qt_log_view_tab_poll_file_picks_up_appended_lines(qtbot, tmp_path):
    """live 模式下文件追加内容应被 _poll_file 读到 _all_lines。"""
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from serial_debug.ui.qt_log_viewer_window import _QtLogViewTab
    from serial_debug.infra import themes
    theme = themes.THEMES['dark']
    log_path = tmp_path / 'live.log'
    log_path.write_text('line0\n', encoding='utf-8')
    tab = _QtLogViewTab(parent=None, path=str(log_path), theme=theme)
    qtbot.addWidget(tab)
    tab._load_file()
    tab.set_live_mode(True)
    # 追加内容
    with open(log_path, 'a', encoding='utf-8') as f:
        f.write('line1\nline2\n')
    # 调 _poll_file 模拟定时器触发
    tab._poll_file()
    assert 'line1' in tab._all_lines
    assert 'line2' in tab._all_lines
    tab.stop_live()


# ---- _SearchWorker ----

def test_search_worker_run_single_finds_matches(qtbot):
    """_SearchWorker.run_single 应在行列表里找匹配。"""
    from PySide6.QtCore import QCoreApplication, QThread, QObject, Qt
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from serial_debug.ui.qt_log_viewer_window import _SearchWorker
    w = _SearchWorker()
    results = {}

    def on_done(epoch, matches, info):
        results['matches'] = matches
        results['info'] = info

    def on_error(epoch, msg):
        results['error'] = msg

    w.done_single.connect(on_done, Qt.QueuedConnection)
    w.error.connect(on_error, Qt.QueuedConnection)
    lines = ['hello world', 'foo bar', 'hello again']
    w.start_run_single.emit(1, lines, 'hello')
    # 等 QueuedConnection 派发
    qtbot.wait(50)
    assert 'matches' in results
    assert results['matches'] == [0, 2]


def test_search_worker_rejects_dangerous_regex(qtbot):
    """_SearchWorker 应拒绝嵌套量词正则。"""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from serial_debug.ui.qt_log_viewer_window import _SearchWorker
    w = _SearchWorker()
    errors = {}

    def on_error(epoch, msg):
        errors['msg'] = msg

    w.error.connect(on_error, Qt.QueuedConnection)
    w.start_run_single.emit(1, ['a' * 100], r'(a+)+')
    qtbot.wait(50)
    assert 'msg' in errors
    assert '回溯' in errors['msg'] or '危险' in errors['msg'] or '嵌套' in errors['msg']


def test_search_worker_handles_invalid_regex(qtbot):
    """_SearchWorker 应捕获非法正则错误。"""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from serial_debug.ui.qt_log_viewer_window import _SearchWorker
    w = _SearchWorker()
    errors = {}

    def on_error(epoch, msg):
        errors['msg'] = msg

    w.error.connect(on_error, Qt.QueuedConnection)
    # 未闭合的 (
    w.start_run_single.emit(1, ['abc'], r'(unclosed')
    qtbot.wait(50)
    assert 'msg' in errors
    assert '正则' in errors['msg']


# ---- _FilterWorker ----

def test_filter_worker_filters_by_regex(qtbot):
    """_FilterWorker.run 应按正则筛选出匹配的行索引。"""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from serial_debug.ui.qt_log_viewer_window import _FilterWorker
    w = _FilterWorker()
    results = {}

    def on_done(epoch, matches, total, info):
        results['matches'] = matches
        results['total'] = total
        results['info'] = info

    def on_error(epoch, msg):
        results['error'] = msg

    w.done.connect(on_done, Qt.QueuedConnection)
    w.error.connect(on_error, Qt.QueuedConnection)
    lines = ['hello world', 'foo', 'hello again', 'bar']
    tags = ['log', 'log', 'log', 'log']
    w.start_run.emit(1, lines, tags, 'hello', 0, 100)
    qtbot.wait(80)
    assert 'matches' in results
    assert results['matches'] == [0, 2]
    assert results['total'] == 4


def test_filter_worker_without_regex_passes_all(qtbot):
    """无正则 + level=0 应放过所有行。"""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from serial_debug.ui.qt_log_viewer_window import _FilterWorker
    w = _FilterWorker()
    results = {}

    def on_done(epoch, matches, total, info):
        results['matches'] = matches

    w.done.connect(on_done, Qt.QueuedConnection)
    lines = ['a', 'b', 'c']
    tags = ['log', 'log', 'log']
    w.start_run.emit(1, lines, tags, '', 0, 100)
    qtbot.wait(80)
    assert results.get('matches') == [0, 1, 2]


def test_filter_worker_level_filter(qtbot):
    """level=1 + tags 应只过 log 类。"""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from serial_debug.ui.qt_log_viewer_window import _FilterWorker
    w = _FilterWorker()
    results = {}

    def on_done(epoch, matches, total, info):
        results['matches'] = matches

    w.done.connect(on_done, Qt.QueuedConnection)
    lines = ['log1', 'OK resp', 'log2', 'ERROR note']
    tags = ['log', 'resp', 'log', 'note']
    w.start_run.emit(1, lines, tags, '', 1, 100)
    qtbot.wait(80)
    # 只过 log 类的索引
    assert results.get('matches') == [0, 2]


def test_filter_worker_rejects_dangerous_regex(qtbot):
    """_FilterWorker 应拒绝嵌套量词正则。"""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from serial_debug.ui.qt_log_viewer_window import _FilterWorker
    w = _FilterWorker()
    errors = {}

    def on_error(epoch, msg):
        errors['msg'] = msg

    w.error.connect(on_error, Qt.QueuedConnection)
    lines = ['a' * 100]
    tags = ['log']
    w.start_run.emit(1, lines, tags, r'(a+)+', 0, 100)
    qtbot.wait(80)
    assert 'msg' in errors
    assert '回溯' in errors['msg'] or '嵌套' in errors['msg']


def test_filter_worker_handles_invalid_regex(qtbot):
    """_FilterWorker 应捕获非法正则。"""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from serial_debug.ui.qt_log_viewer_window import _FilterWorker
    w = _FilterWorker()
    errors = {}

    def on_error(epoch, msg):
        errors['msg'] = msg

    w.error.connect(on_error, Qt.QueuedConnection)
    lines = ['abc']
    tags = ['log']
    w.start_run.emit(1, lines, tags, r'(unclosed', 0, 100)
    qtbot.wait(80)
    assert 'msg' in errors
    assert '正则' in errors['msg']


def test_filter_worker_respects_result_cap(qtbot):
    """result_cap 应限制返回的匹配数。"""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from serial_debug.ui.qt_log_viewer_window import _FilterWorker
    w = _FilterWorker()
    results = {}

    def on_done(epoch, matches, total, info):
        results['matches'] = matches

    w.done.connect(on_done, Qt.QueuedConnection)
    lines = ['hit'] * 20
    tags = ['log'] * 20
    w.start_run.emit(1, lines, tags, 'hit', 0, 5)
    qtbot.wait(80)
    assert len(results.get('matches', [])) == 5
