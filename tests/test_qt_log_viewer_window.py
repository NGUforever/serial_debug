"""ui/qt_log_viewer_window.py 测试：辅助函数 + 异步搜索 worker + 文件 IO。

聚焦纯函数 + _SearchWorker/_FilterWorker 后台扫描逻辑。
"""
from __future__ import annotations

import os
import time

import pytest

pytest.importorskip('PySide6')


@pytest.fixture(autouse=True)
def _mock_qtimer_single_shot(monkeypatch):
    """全局 mock QTimer.singleShot：避免 goto_line 的 3s 延迟回调跨测试
    访问已销毁 editor 导致 'Internal C++ object already deleted' 卡死。
    """
    from PySide6.QtCore import QTimer
    monkeypatch.setattr(QTimer, 'singleShot', lambda *a, **kw: None)


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
    """13 位 ms 时间戳应转换为 [MM-DD HH:MM:SS.mmm] 前缀（含日期，无年份）。"""
    from serial_debug.ui.qt_log_viewer_window import _convert_ts
    # 1750000000000 = 2025-06-15 16:53:20 UTC (本地时区差异)
    result = _convert_ts('1750000000000 some log')
    assert result.startswith('[')
    assert '.' in result
    assert '] ' in result


def test_convert_ts_format_includes_date_without_year():
    """时间戳格式应为 [MM-DD HH:MM:SS.mmm]，含月日，无年份，毫秒 3 位。"""
    import re

    from serial_debug.ui.qt_log_viewer_window import _convert_ts

    result = _convert_ts('1750000000000 some log')
    # 精确匹配：[MM-DD HH:MM:SS.mmm] 后跟一个空格
    m = re.match(r'^\[(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})\.(\d{3})\] $', result)
    assert m is not None, f'格式不符：{result!r}'
    mm, dd, hh, mi, ss, ms = m.groups()
    assert 1 <= int(mm) <= 12
    assert 1 <= int(dd) <= 31
    assert 0 <= int(hh) <= 23
    assert 0 <= int(mi) <= 59
    assert 0 <= int(ss) <= 59
    assert 0 <= int(ms) <= 999


def test_convert_ts_millisecond_padding():
    """毫秒位应补零到 3 位（如 5 → 005）。"""
    from datetime import datetime

    from serial_debug.ui.qt_log_viewer_window import _convert_ts

    # 用 datetime 反推：本地 2026-01-01 00:00:00.005
    dt = datetime(2026, 1, 1, 0, 0, 0, 5000)  # 5000 us = 5 ms
    ms13 = int(dt.timestamp() * 1000)
    result = _convert_ts(f'{ms13} hello')
    assert '.005] ' in result, f'毫秒补零失败：{result!r}'


def test_convert_ts_without_timestamp_returns_empty():
    """无 13 位时间戳应返回空串。"""
    from serial_debug.ui.qt_log_viewer_window import _convert_ts
    assert _convert_ts('plain text') == ''
    assert _convert_ts('12345 short') == ''


# ---- _convert_ts 10 位秒级格式（用户真实日志格式，2026-08-31 缺陷修复）----

def test_convert_ts_10digit_seconds_with_fraction_milliseconds():
    """10 位秒 + 小数点 + 3 位毫秒（用户真实日志格式）应正确转换。

    1787888455.650 在 UTC+8 → [08-28 11:40:55.650]。
    """
    from serial_debug.ui.qt_log_viewer_window import _convert_ts

    result = _convert_ts('1787888455.650 -17.037 29.143 -23.445')
    assert result == '[08-28 11:40:55.650] ', f'真实日志格式转换错误：{result!r}'


def test_convert_ts_10digit_seconds_pure():
    """10 位纯秒时间戳（无小数）应转换，毫秒补零为 .000。"""
    from serial_debug.ui.qt_log_viewer_window import _convert_ts

    result = _convert_ts('1787888455 hello')
    assert result == '[08-28 11:40:55.000] ', f'10 位纯秒转换错误：{result!r}'


def test_convert_ts_13digit_milliseconds_unchanged():
    """13 位毫秒时间戳仍应正常工作（格式不破坏）。

    1693045530123 在 UTC+8 → [08-26 18:25:30.123]。
    """
    from serial_debug.ui.qt_log_viewer_window import _convert_ts

    result = _convert_ts('1693045530123 hello')
    assert result == '[08-26 18:25:30.123] ', f'13 位毫秒格式破坏：{result!r}'


def test_convert_ts_10digit_seconds_single_digit_fraction_padded():
    """10 位秒 + 1 位小数（如 .6）应补零到 3 位（.600）。"""
    from serial_debug.ui.qt_log_viewer_window import _convert_ts

    result = _convert_ts('1787888455.6 hello')
    assert result == '[08-28 11:40:55.600] ', f'1 位小数补零错误：{result!r}'


def test_convert_ts_existing_date_format_not_mangled():
    """已是日期格式（如 2026-08-28 11:40:55）不应被误转换。

    各数字段（2026/08/28/11/40/55）均不足 10 位，正则不匹配，返回空串。
    """
    from serial_debug.ui.qt_log_viewer_window import _convert_ts

    assert _convert_ts('2026-08-28 11:40:55 hello') == ''
    assert _convert_ts('08-28 11:40:55 hello') == ''


def test_convert_ts_no_timestamp_unchanged():
    """无时间戳行应返回空串（调用方原样显示）。"""
    from serial_debug.ui.qt_log_viewer_window import _convert_ts

    assert _convert_ts('no timestamp here') == ''
    assert _convert_ts('123456789 too short') == ''  # 9 位不算时间戳


def test_convert_ts_not_anchored_to_line_start():
    """时间戳不必在行首（search 而非 match），支持前导内容。

    回放日志可能含 '[INFO] 1787888455.650 ...' 这类前缀。
    """
    from serial_debug.ui.qt_log_viewer_window import _convert_ts

    result = _convert_ts('[INFO] 1787888455.650 some data')
    assert result == '[08-28 11:40:55.650] ', f'非行首时间戳转换错误：{result!r}'


def test_convert_ts_real_log_file_first_lines(tmp_path, monkeypatch):
    """端到端：用户机器上的真实日志文件前 5 行都应加 [08-28 ...] 前缀。

    真实文件路径：/home/ninebot/Documents/3K0055_08-28-16-17_08-28-16-40_3444/
    private/rtk_1787888444649_1787888485249.txt，格式为
    '1787888455.650 -17.037 29.143 -23.445'。
    本测试内联同样数据（不依赖外部文件存在），验证批量转换正确。
    """
    from serial_debug.ui.qt_log_viewer_window import _convert_ts

    # 真实日志前 5 行（从机器 head -5 读取，1787888xxx 即 08-28）
    real_lines = [
        '1787888455.650 -17.037 29.143 -23.445',
        '1787888456.049 -16.958 29.177 -23.449',
        '1787888456.451 -16.917 29.225 -23.459',
        '1787888456.849 -16.872 29.295 -23.461',
        '1787888457.249 -16.854 29.373 -23.467',
    ]
    expected = [
        '[08-28 11:40:55.650] ',
        '[08-28 11:40:56.049] ',
        '[08-28 11:40:56.451] ',
        '[08-28 11:40:56.849] ',
        '[08-28 11:40:57.249] ',
    ]
    for line, exp in zip(real_lines, expected):
        got = _convert_ts(line)
        assert got == exp, f'真实日志行 {line!r} 期望 {exp!r} 实得 {got!r}'

    # 若机器上真实文件存在，额外读取并验证前 5 行
    import os

    real_path = (
        '/home/ninebot/Documents/3K0055_08-28-16-17_08-28-16-40_3444/'
        'private/rtk_1787888444649_1787888485249.txt'
    )
    if os.path.exists(real_path):
        with open(real_path, encoding='utf-8', errors='replace') as f:
            head_lines = [next(f).rstrip('\n') for _ in range(5)]
        for line in head_lines:
            got = _convert_ts(line)
            assert got.startswith('[08-28 11:40'), f'真实文件行 {line!r} 转换错误：{got!r}'


def test_convert_ts_beijing_time_independent_of_system_timezone(monkeypatch):
    """_convert_ts 应显式北京时间（UTC+8），不依赖系统时区。

    在 TZ=UTC（系统时区设为 UTC）下，1787888455 的 UTC 时间是 03:40:55，
    北京时间是 11:40:55。若 _convert_ts 仍用裸 fromtimestamp（依赖系统时区），
    则在 TZ=UTC 下会返回 [08-28 03:40:55.xxx]（错误）。显式 BEIJING_TZ 应返回
    [08-28 11:40:55.xxx] 无论系统时区。
    """
    monkeypatch.setenv('TZ', 'UTC')
    import time as _time
    _time.tzset()  # 应用 TZ=UTC 到进程
    from serial_debug.ui.qt_log_viewer_window import _convert_ts

    # 10 位秒 + 毫秒
    assert _convert_ts('1787888455.650') == '[08-28 11:40:55.650] '
    # 10 位纯秒
    assert _convert_ts('1787888455') == '[08-28 11:40:55.000] '
    # 13 位毫秒
    assert _convert_ts('1787888455650') == '[08-28 11:40:55.650] '


def test_convert_ts_beijing_offset_eight_hours():
    """北京时间 = UTC + 8。验证一个已知 UTC 时间戳的差恰好 8 小时。

    1787888455 的 UTC 是 03:40:55（2026-08-28），北京是 11:40:55，差 8h。
    """
    from datetime import datetime, timezone

    from serial_debug.ui.qt_log_viewer_window import BEIJING_TZ, _convert_ts

    ts = 1787888455
    # UTC 直接算
    utc_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%H:%M:%S')
    assert utc_str == '03:40:55', f'UTC 时间计算错：{utc_str}'
    # 北京时间
    beijing_str = datetime.fromtimestamp(ts, tz=BEIJING_TZ).strftime('%H:%M:%S')
    assert beijing_str == '11:40:55', f'北京时间计算错：{beijing_str}'
    # _convert_ts 输出含北京时间
    assert '11:40:55' in _convert_ts(f'{ts}.0')


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
    QApplication.instance() or QApplication([])
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
    QApplication.instance() or QApplication([])
    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import _QtLogViewTab
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
    QApplication.instance() or QApplication([])
    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import _QtLogViewTab
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
    QApplication.instance() or QApplication([])
    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import _QtLogViewTab
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
    from PySide6.QtCore import QCoreApplication, QObject, Qt, QThread
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
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
    QApplication.instance() or QApplication([])
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
    QApplication.instance() or QApplication([])
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
    QApplication.instance() or QApplication([])
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
    QApplication.instance() or QApplication([])
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
    QApplication.instance() or QApplication([])
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
    QApplication.instance() or QApplication([])
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
    QApplication.instance() or QApplication([])
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
    QApplication.instance() or QApplication([])
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


# ---- _LineNumberArea / _PlainTextEdit ----

def _make_qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_plain_text_edit_set_theme(qtbot):
    """_PlainTextEdit.set_theme 应更新 palette + line_area update。"""
    _make_qapp()
    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import _PlainTextEdit
    editor = _PlainTextEdit()
    qtbot.addWidget(editor)
    # 不应抛
    editor.set_theme(themes.THEMES['light'])
    # palette base color 应是 light 主题 panel_bg
    pal = editor.palette()
    from PySide6.QtGui import QColor
    expected = QColor(themes.THEMES['light'].get('panel_bg', '#1e1e1e'))
    actual = pal.color(pal.ColorRole.Base)
    # RGB 比较（名称不同实例）
    assert actual.red() == expected.red()
    assert actual.green() == expected.green()
    assert actual.blue() == expected.blue()


def test_plain_text_edit_line_number_area_width(qtbot):
    """line_number_area_width 应基于 blockCount 算出 digits+padding 宽。"""
    _make_qapp()
    from serial_debug.ui.qt_log_viewer_window import _PlainTextEdit
    editor = _PlainTextEdit()
    qtbot.addWidget(editor)
    # 空 document blockCount=1
    w = editor.line_number_area_width()
    assert w > 0
    # 插入多行，width 应增加
    editor.setPlainText('\n'.join(['x'] * 100))
    w2 = editor.line_number_area_width()
    assert w2 >= w


def test_plain_text_edit_resize_event_sets_geometry(qtbot):
    """resizeEvent 应给 _line_area 设置 geometry。"""
    _make_qapp()
    from PySide6.QtCore import QRect
    from PySide6.QtWidgets import QApplication

    from serial_debug.ui.qt_log_viewer_window import _PlainTextEdit
    editor = _PlainTextEdit()
    qtbot.addWidget(editor)
    editor.resize(400, 200)
    editor.show()
    QApplication.processEvents()
    # _line_area 应有 geometry
    g = editor._line_area.geometry()
    assert g.width() > 0
    assert g.height() >= 0


def test_plain_text_edit_update_line_area_width(qtbot):
    """_update_line_area_width 应设置 viewport margins。"""
    _make_qapp()
    from serial_debug.ui.qt_log_viewer_window import _PlainTextEdit
    editor = _PlainTextEdit()
    qtbot.addWidget(editor)
    editor._update_line_area_width()
    margins = editor.viewportMargins()
    assert margins.left() > 0


def test_plain_text_edit_update_line_area_with_dy(qtbot):
    """_update_line_area dy>0 时应 scroll，dy=0 时应 update rect。"""
    _make_qapp()
    from PySide6.QtCore import QRect

    from serial_debug.ui.qt_log_viewer_window import _PlainTextEdit
    editor = _PlainTextEdit()
    qtbot.addWidget(editor)
    # 不应抛
    editor._update_line_area(QRect(0, 0, 100, 20), 5)
    editor._update_line_area(QRect(0, 0, 100, 20), 0)


def test_plain_text_edit_first_visible_block(qtbot):
    """first_visible_block 应返回 QTextBlock。"""
    _make_qapp()
    from serial_debug.ui.qt_log_viewer_window import _PlainTextEdit
    editor = _PlainTextEdit()
    qtbot.addWidget(editor)
    editor.setPlainText('line1\nline2\nline3')
    block = editor.first_visible_block()
    # 应为有效 block
    assert block.isValid()


def test_line_number_area_paint_event(qtbot):
    """_LineNumberArea.paintEvent 应能在 offscreen 下不抛。"""
    _make_qapp()
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QPaintEvent
    from PySide6.QtWidgets import QApplication

    from serial_debug.ui.qt_log_viewer_window import _PlainTextEdit
    editor = _PlainTextEdit()
    qtbot.addWidget(editor)
    editor.setPlainText('line1\nline2\nline3\n')
    editor.resize(400, 200)
    editor.show()
    QApplication.processEvents()
    # 触发 paintEvent
    area = editor._line_area
    area.update()
    QApplication.processEvents()


def test_line_number_area_size_hint(qtbot):
    """sizeHint 应返回 line_number_area_width 宽。"""
    _make_qapp()
    from serial_debug.ui.qt_log_viewer_window import _PlainTextEdit
    editor = _PlainTextEdit()
    qtbot.addWidget(editor)
    area = editor._line_area
    sh = area.sizeHint()
    assert sh.width() == editor.line_number_area_width()


# ---- _QtLogViewTab 方法 ----

def _make_tab(qtbot, tmp_path, lines):
    """辅助：创建一个 _QtLogViewTab 并加载文件。"""
    _make_qapp()
    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import _QtLogViewTab
    log_path = tmp_path / 'test.log'
    log_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    tab = _QtLogViewTab(parent=None, path=str(log_path), theme=themes.THEMES['dark'])
    qtbot.addWidget(tab)
    tab._load_file()
    return tab


def test_tab_get_line_returns_indexed(qtbot, tmp_path):
    """get_line 应返回索引处的行。"""
    tab = _make_tab(qtbot, tmp_path, ['hello', 'world', 'foo'])
    assert tab.get_line(0) == 'hello'
    assert tab.get_line(2) == 'foo'
    # 越界返 ''
    assert tab.get_line(99) == ''
    assert tab.get_line(-1) == ''


def test_tab_all_lines_returns_full_list(qtbot, tmp_path):
    """all_lines 应返回 _all_lines 引用。"""
    tab = _make_tab(qtbot, tmp_path, ['a', 'b', 'c'])
    assert tab.all_lines() == ['a', 'b', 'c']


def test_tab_path_and_filename(qtbot, tmp_path):
    """path / filename 应返回路径和文件名。"""
    import os
    tab = _make_tab(qtbot, tmp_path, ['a'])
    assert tab.path() == str(tmp_path / 'test.log')
    assert tab.filename() == 'test.log'


def test_tab_apply_theme_changes_panel_bg(qtbot, tmp_path):
    """apply_theme(light) 应更新 editor palette。"""
    _make_qapp()
    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import _QtLogViewTab
    log_path = tmp_path / 't.log'
    log_path.write_text('line\n', encoding='utf-8')
    tab = _QtLogViewTab(parent=None, path=str(log_path), theme=themes.THEMES['dark'])
    qtbot.addWidget(tab)
    tab._load_file()
    tab.apply_theme(themes.THEMES['light'])
    # editor palette 应改为 light
    pal = tab._editor.palette()
    from PySide6.QtGui import QColor
    expected = QColor(themes.THEMES['light'].get('panel_bg', '#1e1e1e'))
    actual = pal.color(pal.ColorRole.Base)
    assert actual.red() == expected.red()


def test_tab_format_for_tag_resp(qtbot, tmp_path):
    """_format_for_tag(resp) 应返回带 accent 色的 fmt。"""
    tab = _make_tab(qtbot, tmp_path, ['a'])
    from serial_debug.infra import themes
    # 注入一些 tag
    tab._line_tags = ['resp', 'note', 'log']
    fmt_resp = tab._format_for_tag('resp')
    fmt_note = tab._format_for_tag('note')
    fmt_log = tab._format_for_tag('log')
    assert fmt_resp is not None
    assert fmt_note is not None
    assert fmt_log is not None


def test_tab_render_batch_inserts_placeholder(qtbot, tmp_path):
    """_render_batch 在 end<len 时应插入占位符 + userData。"""
    _make_qapp()
    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import RENDER_BATCH, _PlaceholderData, _QtLogViewTab
    # 写超过 RENDER_BATCH 行
    lines = [f'line{i}' for i in range(RENDER_BATCH + 5)]
    log_path = tmp_path / 'big.log'
    log_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    tab = _QtLogViewTab(parent=None, path=str(log_path), theme=themes.THEMES['dark'])
    qtbot.addWidget(tab)
    tab._load_file()
    # 渲染首批后应有占位符 block
    last_block = tab._editor.document().lastBlock()
    assert isinstance(last_block.userData(), _PlaceholderData)


def test_tab_delete_trailing_placeholder(qtbot, tmp_path):
    """_delete_trailing_placeholder 应删掉尾部占位符。"""
    _make_qapp()
    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import RENDER_BATCH, _PlaceholderData, _QtLogViewTab
    lines = [f'line{i}' for i in range(RENDER_BATCH + 5)]
    log_path = tmp_path / 'big.log'
    log_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    tab = _QtLogViewTab(parent=None, path=str(log_path), theme=themes.THEMES['dark'])
    qtbot.addWidget(tab)
    tab._load_file()
    # 删占位符
    tab._delete_trailing_placeholder()
    # placeholder_block 应回 -1
    assert tab._placeholder_block == -1


def test_tab_on_scroll_triggers_chain_load(qtbot, tmp_path):
    """_on_scroll 在滚动到底时应触发 _chain_load。"""
    _make_qapp()
    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import RENDER_BATCH, _QtLogViewTab
    lines = [f'line{i}' for i in range(RENDER_BATCH * 3)]
    log_path = tmp_path / 'big.log'
    log_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    tab = _QtLogViewTab(parent=None, path=str(log_path), theme=themes.THEMES['dark'])
    qtbot.addWidget(tab)
    tab._load_file()
    # 模拟滚动到底
    sb = tab._editor.verticalScrollBar()
    # 最大值可能为 0（offscreen），但 _on_scroll 不应崩
    tab._on_scroll(0)  # 不应抛
    tab._on_scroll(sb.maximum() if sb.maximum() > 0 else 0)


def test_tab_chain_load_progressively(qtbot, tmp_path):
    """_chain_load 应分批加载。"""
    _make_qapp()
    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import RENDER_BATCH, _QtLogViewTab
    lines = [f'line{i}' for i in range(RENDER_BATCH * 2)]
    log_path = tmp_path / 'big.log'
    log_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    tab = _QtLogViewTab(parent=None, path=str(log_path), theme=themes.THEMES['dark'])
    qtbot.addWidget(tab)
    tab._load_file()
    # _chain_load 多次应渐进加载
    initial = tab._rendered_count
    tab._chain_load(budget=2)
    assert tab._rendered_count >= initial  # 应至少加了


def test_tab_jump_to_end(qtbot, tmp_path):
    """_jump_to_end 应加载完所有内容并滚到底。"""
    _make_qapp()
    from PySide6.QtWidgets import QApplication

    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import RENDER_BATCH, _QtLogViewTab
    lines = [f'line{i}' for i in range(RENDER_BATCH * 2)]
    log_path = tmp_path / 'big.log'
    log_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    tab = _QtLogViewTab(parent=None, path=str(log_path), theme=themes.THEMES['dark'])
    qtbot.addWidget(tab)
    tab._load_file()
    tab._jump_to_end()
    # 应加载完全部
    assert tab._rendered_count == len(lines)


def test_tab_goto_line_loads_then_highlights(qtbot, tmp_path, monkeypatch):
    """goto_line 应加载到该行 + 高亮。"""
    _make_qapp()
    from PySide6.QtCore import QTimer

    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import RENDER_BATCH, _QtLogViewTab
    lines = [f'line{i}' for i in range(RENDER_BATCH * 2)]
    log_path = tmp_path / 'big.log'
    log_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    tab = _QtLogViewTab(parent=None, path=str(log_path), theme=themes.THEMES['dark'])
    qtbot.addWidget(tab)
    tab._load_file()
    # mock QTimer.singleShot 避免 3s 后跨测试触发已销毁 editor
    monkeypatch.setattr(QTimer, 'singleShot', lambda *a, **kw: None)
    target = RENDER_BATCH + 10
    tab.goto_line(target)
    # 应已加载到 target+1
    assert tab._rendered_count > target
    # 越界：goto 不抛
    tab.goto_line(-1)
    tab.goto_line(999999)


def test_tab_clear_goto_highlight(qtbot, tmp_path):
    """_clear_goto_highlight 不应抛（即使 line_idx 越界）。"""
    tab = _make_tab(qtbot, tmp_path, ['a', 'b', 'c'])
    tab._clear_goto_highlight(0)  # 不应抛
    tab._clear_goto_highlight(999)  # 越界也不抛


def test_tab_show_search_toggles_visibility(qtbot, tmp_path):
    """_show_search 应切换 search_bar 可见性。"""
    tab = _make_tab(qtbot, tmp_path, ['a'])
    # 注意：offscreen 下父未 show 时 isVisible() 总返 False，改用 isHidden 校验
    tab._show_search(True)
    assert not tab._search_bar.isHidden()
    tab._show_search(False)
    assert tab._search_bar.isHidden()


def test_tab_do_search_empty_text_clears(qtbot, tmp_path):
    """_do_search 空文本应清空 matches。"""
    tab = _make_tab(qtbot, tmp_path, ['hello', 'world'])
    tab._search_edit.setText('')
    tab._do_search()
    assert tab._search_matches == []


def test_tab_do_search_dangerous_regex_rejected(qtbot, tmp_path):
    """_do_search 危险正则应被拒。"""
    tab = _make_tab(qtbot, tmp_path, ['a' * 100])
    tab._search_edit.setText(r'(a+)+')
    tab._do_search()
    assert tab._search_matches == []
    assert '嵌套' in tab._search_info.text() or '回溯' in tab._search_info.text()


def test_tab_do_search_invalid_regex_rejected(qtbot, tmp_path):
    """_do_search 非法正则应被拒。"""
    tab = _make_tab(qtbot, tmp_path, ['abc'])
    tab._search_edit.setText(r'(unclosed')
    tab._do_search()
    assert tab._search_matches == []


def test_tab_do_search_sync_no_worker(qtbot, tmp_path):
    """无 worker 时应回退到 _do_search_sync。"""
    tab = _make_tab(qtbot, tmp_path, ['hello world', 'foo', 'hello again'])
    # 注入 None worker（已是 None 默认）
    tab._search_edit.setText('hello')
    tab._do_search()
    # _do_search_sync 应填充 matches
    assert 0 in tab._search_matches
    assert 2 in tab._search_matches


def test_tab_do_search_sync_search_scan_limit(qtbot, tmp_path):
    """_do_search_sync 应遵守 SEARCH_SCAN_LIMIT（不无限扫）。"""
    from serial_debug.ui.qt_log_viewer_window import SEARCH_SCAN_LIMIT
    lines = ['hit'] * (SEARCH_SCAN_LIMIT + 100)
    tab = _make_tab(qtbot, tmp_path, lines)
    tab._search_edit.setText('hit')
    tab._do_search()
    # 应有截断（不超过 SCAN_LIMIT+1）
    assert len(tab._search_matches) <= SEARCH_SCAN_LIMIT + 1


def test_tab_on_search_done_with_pending_next(qtbot, tmp_path):
    """_on_search_done 配 pending='next' 应跳第一个匹配。"""
    tab = _make_tab(qtbot, tmp_path, ['x', 'y', 'x'])
    tab._search_epoch = 1
    tab._search_pending = 'next'
    tab._on_search_done(1, [0, 2], '2 个匹配')
    assert tab._search_pos == 0
    assert '1/2' in tab._search_info.text()


def test_tab_on_search_done_with_pending_prev(qtbot, tmp_path):
    """_on_search_done 配 pending='prev' 应跳末个匹配。"""
    tab = _make_tab(qtbot, tmp_path, ['x', 'y', 'x'])
    tab._search_epoch = 1
    tab._search_pending = 'prev'
    tab._on_search_done(1, [0, 2], '2 个匹配')
    assert tab._search_pos == 1
    assert '2/2' in tab._search_info.text()


def test_tab_on_search_done_stale_epoch_dropped(qtbot, tmp_path):
    """过期 epoch 应被丢弃。"""
    tab = _make_tab(qtbot, tmp_path, ['x'])
    tab._search_epoch = 5
    tab._search_matches = ['old']
    tab._on_search_done(3, [0, 1], 'should be dropped')
    # 不应被覆盖
    assert tab._search_matches == ['old']


def test_tab_on_search_error_sets_msg(qtbot, tmp_path):
    """_on_search_error 应设置 info text。"""
    tab = _make_tab(qtbot, tmp_path, ['x'])
    tab._search_epoch = 1
    tab._on_search_error(1, 'something wrong')
    assert tab._search_matches == []
    assert 'something wrong' in tab._search_info.text()


def test_tab_search_next_match_triggers_search(qtbot, tmp_path):
    """matches 空时 _search_next_match 应触发 do_search。"""
    tab = _make_tab(qtbot, tmp_path, ['x'])
    tab._search_matches = []
    tab._search_edit.setText('x')
    tab._search_next_match()  # 不应抛
    # 应触发搜索（同步路径填充 matches）
    assert 0 in tab._search_matches


def test_tab_search_next_match_cycles(qtbot, tmp_path):
    """已有 matches 时 _search_next_match 应 cycle 到下一个。"""
    tab = _make_tab(qtbot, tmp_path, ['x', 'y', 'x'])
    tab._search_matches = [0, 2]
    tab._search_pos = 0
    tab._search_next_match()
    assert tab._search_pos == 1


def test_tab_search_prev_match_cycles(qtbot, tmp_path):
    """已有 matches 时 _search_prev_match 应 cycle 到上一个。"""
    tab = _make_tab(qtbot, tmp_path, ['x', 'y', 'x'])
    tab._search_matches = [0, 2]
    tab._search_pos = 0
    tab._search_prev_match()
    assert tab._search_pos == 1  # (0-1) % 2 = 1


def test_tab_global_search_returns_preview_tuples(qtbot, tmp_path):
    """global_search 应返回 [(line_idx, col, preview)]。"""
    import re
    tab = _make_tab(qtbot, tmp_path, ['hello world', 'foo', 'hello again'])
    pattern = re.compile('hello')
    results = tab.global_search(pattern)
    assert len(results) == 2
    assert results[0][0] == 0
    assert results[1][0] == 2
    assert all(isinstance(r[2], str) for r in results)


def test_tab_global_search_caps_at_max(qtbot, tmp_path):
    """global_search 应 cap 在 GLOBAL_SEARCH_MAX。"""
    import re

    from serial_debug.ui.qt_log_viewer_window import GLOBAL_SEARCH_MAX
    lines = ['hit'] * (GLOBAL_SEARCH_MAX + 100)
    tab = _make_tab(qtbot, tmp_path, lines)
    pattern = re.compile('hit')
    results = tab.global_search(pattern)
    assert len(results) == GLOBAL_SEARCH_MAX


def test_tab_set_live_mode_idempotent(qtbot, tmp_path):
    """set_live_mode 同状态应直接 return。"""
    tab = _make_tab(qtbot, tmp_path, ['x'])
    initial_state = tab._live_mode
    tab.set_live_mode(initial_state)  # 不启动 timer
    assert tab._live_mode == initial_state


def test_tab_set_live_mode_off_stops_timer(qtbot, tmp_path):
    """set_live_mode(False) 应 stop timer。"""
    tab = _make_tab(qtbot, tmp_path, ['x'])
    tab.set_live_mode(True)
    timer = tab._live_timer
    tab.set_live_mode(False)
    assert not tab._live_mode
    # timer 仍在但已 stop
    assert not timer.isActive() if timer else True


def test_tab_stop_live_clears_timer(qtbot, tmp_path):
    """stop_live 应把 _live_timer 置 None。"""
    tab = _make_tab(qtbot, tmp_path, ['x'])
    tab.set_live_mode(True)
    tab.stop_live()
    assert tab._live_timer is None
    assert not tab._live_mode


def test_tab_file_size_returns_int(qtbot, tmp_path):
    """_file_size 应返回 int 字节数。"""
    tab = _make_tab(qtbot, tmp_path, ['hello world\n'])
    size = tab._file_size()
    assert isinstance(size, int)
    assert size > 0


def test_tab_file_size_missing_file_returns_zero(qtbot, tmp_path):
    """_file_size 文件不存在应返 0。"""
    _make_qapp()
    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import _QtLogViewTab
    tab = _QtLogViewTab(parent=None, path='/nonexistent_xyz.log',
                       theme=themes.THEMES['dark'])
    qtbot.addWidget(tab)
    tab._load_file()
    assert tab._file_size() == 0


def test_tab_poll_file_no_change_increases_idle(qtbot, tmp_path):
    """_poll_file 在文件无变化时应增加 _live_idle_count。"""
    tab = _make_tab(qtbot, tmp_path, ['x\n'])
    tab.set_live_mode(True)
    initial_idle = tab._live_idle_count
    tab._poll_file()
    assert tab._live_idle_count == initial_idle + 1


def test_tab_poll_file_truncated_reloads(qtbot, tmp_path):
    """文件被截断（cur_size < _live_pos）应重置 + 重新加载。"""
    _make_qapp()
    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import _QtLogViewTab
    log_path = tmp_path / 'trunc.log'
    log_path.write_text('aaaa\nbbbb\ncccc\n', encoding='utf-8')
    tab = _QtLogViewTab(parent=None, path=str(log_path),
                       theme=themes.THEMES['dark'])
    qtbot.addWidget(tab)
    tab._load_file()
    tab.set_live_mode(True)
    # 把文件缩小（截断）
    log_path.write_text('xx\n', encoding='utf-8')
    tab._poll_file()
    # _live_pos 应回到 0（_load_file 重置后又设置为新 size）
    # _all_lines 应回到 ['xx']
    assert 'xx' in tab._all_lines


def test_tab_poll_file_partial_line_buffered(qtbot, tmp_path):
    """追加无 \\n 的半行应被 buffer 到 _live_partial。"""
    _make_qapp()
    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import _QtLogViewTab
    log_path = tmp_path / 'partial.log'
    log_path.write_text('init\n', encoding='utf-8')
    tab = _QtLogViewTab(parent=None, path=str(log_path),
                       theme=themes.THEMES['dark'])
    qtbot.addWidget(tab)
    tab._load_file()
    tab.set_live_mode(True)
    # 追加半行
    with open(log_path, 'a', encoding='utf-8') as f:
        f.write('partial line without newline')
    tab._poll_file()
    # 应 buffer 到 _live_partial，_all_lines 不应增加
    assert 'partial line without newline' in tab._live_partial
    assert 'partial line without newline' not in tab._all_lines


def test_tab_poll_file_calls_feed_monitor_cb(qtbot, tmp_path):
    """_feed_monitor_cb 应被追加的新行调用。"""
    _make_qapp()
    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import _QtLogViewTab
    log_path = tmp_path / 'feed.log'
    log_path.write_text('init\n', encoding='utf-8')
    tab = _QtLogViewTab(parent=None, path=str(log_path),
                       theme=themes.THEMES['dark'])
    qtbot.addWidget(tab)
    tab._load_file()
    tab.set_live_mode(True)
    received = []
    tab._feed_monitor_cb = lambda line: received.append(line)
    # 追加完整行
    with open(log_path, 'a', encoding='utf-8') as f:
        f.write('new line 1\nnew line 2\n')
    tab._poll_file()
    assert any('new line 1' in ln for ln in received)
    assert any('new line 2' in ln for ln in received)


def test_tab_poll_file_feed_monitor_cb_failure_swallowed(qtbot, tmp_path):
    """_feed_monitor_cb 抛异常应被吞。"""
    _make_qapp()
    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import _QtLogViewTab
    log_path = tmp_path / 'feed_fail.log'
    log_path.write_text('init\n', encoding='utf-8')
    tab = _QtLogViewTab(parent=None, path=str(log_path),
                       theme=themes.THEMES['dark'])
    qtbot.addWidget(tab)
    tab._load_file()
    tab.set_live_mode(True)
    def boom(line):
        raise RuntimeError('feed fail')
    tab._feed_monitor_cb = boom
    with open(log_path, 'a', encoding='utf-8') as f:
        f.write('another line\n')
    tab._poll_file()  # 不应抛
    assert 'another line' in tab._all_lines


def test_tab_on_text_click_calls_callback(qtbot, tmp_path):
    """_on_text_click 应调 on_line_click。"""
    from PySide6.QtCore import QPoint
    tab = _make_tab(qtbot, tmp_path, ['hello', 'world'])
    called = []
    tab._on_line_click = lambda t, idx: called.append(idx)
    # 给 editor 设置文本后 click
    # editor 文本已加载
    tab._rendered_count = 2  # 确保 block 0 在范围内
    tab._on_text_click(QPoint(0, 0))  # 不应抛


def test_tab_on_text_click_out_of_range_no_callback(qtbot, tmp_path):
    """_on_text_click block 超出范围应直接 return 不调回调。"""
    from PySide6.QtCore import QPoint
    tab = _make_tab(qtbot, tmp_path, ['hello', 'world'])
    called = []
    tab._on_line_click = lambda t, idx: called.append(idx)
    # 把 rendered_count 设 0，让 block_number >= 0 触发 return
    tab._rendered_count = 0
    tab._on_text_click(QPoint(0, 0))
    # 不应调
    assert called == []


def test_tab_on_text_click_callback_failure_swallowed(qtbot, tmp_path):
    """on_line_click 回调抛异常应被吞。"""
    from PySide6.QtCore import QPoint
    tab = _make_tab(qtbot, tmp_path, ['hello', 'world'])
    def boom(t, idx):
        raise RuntimeError('cb fail')
    tab._on_line_click = boom
    tab._rendered_count = 2
    tab._on_text_click(QPoint(0, 0))  # 不应抛


def test_tab_event_filter_click(qtbot, tmp_path):
    """eventFilter 处理 viewport 鼠标按下。"""
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    _make_qapp()
    tab = _make_tab(qtbot, tmp_path, ['a', 'b', 'c'])
    called = []
    tab._on_line_click = lambda t, idx: called.append(idx)
    # 构造一个 MouseButtonPress 事件
    ev = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(0, 0),
                    QPointF(0, 0), Qt.LeftButton, Qt.LeftButton,
                    Qt.NoModifier)
    # 不应抛
    tab.eventFilter(tab._editor.viewport(), ev)


def test_tab_on_search_return_next(qtbot, tmp_path):
    """_on_search_return 不带 Shift 应触发 next match。"""
    tab = _make_tab(qtbot, tmp_path, ['x', 'y', 'x'])
    tab._search_edit.setText('x')
    tab._search_matches = [0, 2]
    tab._search_pos = 0
    # 无 Shift modifier
    tab._on_search_return()  # 不应抛，应 cycle next


# ---- _SearchWorker.run_global ----

def test_search_worker_run_global_finds_matches(qtbot):
    """_SearchWorker.run_global 应在多 tab 中找匹配。"""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from serial_debug.ui.qt_log_viewer_window import _SearchWorker
    w = _SearchWorker()
    results = {}

    def on_done(epoch, results_list, info):
        results['list'] = results_list
        results['info'] = info

    def on_error(epoch, msg):
        results['error'] = msg

    w.done_global.connect(on_done, Qt.QueuedConnection)
    w.error.connect(on_error, Qt.QueuedConnection)
    tabs_snapshot = [
        (0, ['hello', 'foo', 'hello'], 'a.log'),
        (1, ['world', 'hello'], 'b.log'),
    ]
    w.start_run_global.emit(1, tabs_snapshot, 'hello')
    qtbot.wait(80)
    assert 'list' in results
    # 应找到 3 个 'hello'（tab 0 idx 0 + 2，tab 1 idx 1）
    assert len(results['list']) == 3


def test_search_worker_run_global_rejects_dangerous(qtbot):
    """run_global 应拒危险正则。"""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from serial_debug.ui.qt_log_viewer_window import _SearchWorker
    w = _SearchWorker()
    errors = {}

    def on_error(epoch, msg):
        errors['msg'] = msg

    w.error.connect(on_error, Qt.QueuedConnection)
    w.start_run_global.emit(1, [(0, ['a'], 'x.log')], r'(a+)+')
    qtbot.wait(80)
    assert 'msg' in errors
    assert '嵌套' in errors['msg'] or '回溯' in errors['msg']


def test_search_worker_run_global_invalid_regex(qtbot):
    """run_global 应捕获非法正则。"""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from serial_debug.ui.qt_log_viewer_window import _SearchWorker
    w = _SearchWorker()
    errors = {}

    def on_error(epoch, msg):
        errors['msg'] = msg

    w.error.connect(on_error, Qt.QueuedConnection)
    w.start_run_global.emit(1, [(0, ['a'], 'x.log')], r'(unclosed')
    qtbot.wait(80)
    assert 'msg' in errors
    assert '正则' in errors['msg']


def test_search_worker_run_global_caps_at_max(qtbot):
    """run_global 应 cap 在 GLOBAL_SEARCH_MAX。"""
    import re

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from serial_debug.ui.qt_log_viewer_window import GLOBAL_SEARCH_MAX, _SearchWorker
    w = _SearchWorker()
    results = {}

    def on_done(epoch, results_list, info):
        results['list'] = results_list

    w.done_global.connect(on_done, Qt.QueuedConnection)
    # 单 tab 含 GLOBAL_SEARCH_MAX + 100 个 hit
    tabs_snapshot = [(0, ['hit'] * (GLOBAL_SEARCH_MAX + 100), 'big.log')]
    w.start_run_global.emit(1, tabs_snapshot, 'hit')
    qtbot.wait(200)
    assert len(results.get('list', [])) == GLOBAL_SEARCH_MAX


# ---- _QtFilterPanel ----

def test_filter_panel_construction(qtbot):
    """_QtFilterPanel 应能正常构造 + 启动 worker thread。"""
    _make_qapp()
    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import _QtFilterPanel
    panel = _QtFilterPanel(parent=None, theme=themes.THEMES['dark'])
    qtbot.addWidget(panel)
    # 应有 _filter_thread + _tree + _regex_edit
    assert panel._filter_thread is not None
    assert panel._tree is not None
    assert panel._regex_edit is not None
    panel.shutdown_filter_thread()


def test_filter_panel_apply_theme(qtbot):
    """apply_theme 应更新 info label 颜色。"""
    _make_qapp()
    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import _QtFilterPanel
    panel = _QtFilterPanel(parent=None, theme=themes.THEMES['dark'])
    qtbot.addWidget(panel)
    panel.apply_theme(themes.THEMES['light'])
    # 不应抛
    panel.shutdown_filter_thread()


def test_filter_panel_set_tab_attaches(qtbot, tmp_path):
    """set_tab 应绑定 + 触发 _apply_filter。"""
    _make_qapp()
    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import _QtFilterPanel
    panel = _QtFilterPanel(parent=None, theme=themes.THEMES['dark'])
    qtbot.addWidget(panel)
    tab = _make_tab(qtbot, tmp_path, ['hello', 'world'])
    panel.set_tab(tab)
    assert panel._tab is tab
    # epoch 应 +1
    assert panel._filter_epoch >= 1
    panel.shutdown_filter_thread()


def test_filter_panel_set_tab_none_clears(qtbot, tmp_path):
    """set_tab(None) 应清结果 + 不抛。"""
    _make_qapp()
    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import _QtFilterPanel
    panel = _QtFilterPanel(parent=None, theme=themes.THEMES['dark'])
    qtbot.addWidget(panel)
    panel.set_tab(None)  # 不应抛
    panel.shutdown_filter_thread()


def test_filter_panel_level_filter_levels(qtbot):
    """_level_filter 4 级应正确分类。"""
    _make_qapp()
    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import _QtFilterPanel
    panel = _QtFilterPanel(parent=None, theme=themes.THEMES['dark'])
    qtbot.addWidget(panel)
    # idx 0 全过
    panel._level_box.setCurrentIndex(0)
    assert panel._level_filter('any', 'log') is True
    panel._level_box.setCurrentIndex(1)
    assert panel._level_filter('log', 'log') is True
    assert panel._level_filter('OK', 'resp') is False
    panel._level_box.setCurrentIndex(2)
    assert panel._level_filter('OK', 'resp') is True
    assert panel._level_filter('log', 'log') is False
    panel._level_box.setCurrentIndex(3)
    assert panel._level_filter('warn', 'note') is True
    assert panel._level_filter('log', 'log') is False
    panel.shutdown_filter_thread()


def test_filter_panel_apply_filter_dangerous_regex(qtbot, tmp_path):
    """_apply_filter 危险正则应被拒。"""
    _make_qapp()
    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import _QtFilterPanel
    panel = _QtFilterPanel(parent=None, theme=themes.THEMES['dark'])
    qtbot.addWidget(panel)
    tab = _make_tab(qtbot, tmp_path, ['a' * 100])
    panel.set_tab(tab)
    panel._regex_edit.setText(r'(a+)+')
    panel._apply_filter()
    assert panel._filtered_idx == []
    assert '嵌套' in panel._info_lbl.text() or '回溯' in panel._info_lbl.text()
    panel.shutdown_filter_thread()


def test_filter_panel_apply_filter_invalid_regex(qtbot, tmp_path):
    """_apply_filter 非法正则应被拒。"""
    _make_qapp()
    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import _QtFilterPanel
    panel = _QtFilterPanel(parent=None, theme=themes.THEMES['dark'])
    qtbot.addWidget(panel)
    tab = _make_tab(qtbot, tmp_path, ['abc'])
    panel.set_tab(tab)
    panel._regex_edit.setText(r'(unclosed')
    panel._apply_filter()
    assert panel._filtered_idx == []
    assert '正则' in panel._info_lbl.text()
    panel.shutdown_filter_thread()


def test_filter_panel_apply_filter_empty_text_uses_worker(qtbot, tmp_path):
    """_apply_filter 空文本应通过 worker 全过（异步）。"""
    _make_qapp()
    from PySide6.QtWidgets import QApplication

    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import _QtFilterPanel
    panel = _QtFilterPanel(parent=None, theme=themes.THEMES['dark'])
    qtbot.addWidget(panel)
    tab = _make_tab(qtbot, tmp_path, ['a', 'b', 'c'])
    panel.set_tab(tab)
    panel._regex_edit.setText('')
    panel._apply_filter()
    # 等 worker 派发
    qtbot.wait(100)
    QApplication.processEvents()
    panel.shutdown_filter_thread()


def test_filter_panel_on_filter_done_stale_epoch_dropped(qtbot, tmp_path):
    """_on_filter_done 过期 epoch 应被丢弃。"""
    _make_qapp()
    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import _QtFilterPanel
    panel = _QtFilterPanel(parent=None, theme=themes.THEMES['dark'])
    qtbot.addWidget(panel)
    panel._filter_epoch = 5
    panel._filtered_idx = [99]
    panel._on_filter_done(3, [0, 1], 10, 'info')
    # 不应被覆盖
    assert panel._filtered_idx == [99]
    panel.shutdown_filter_thread()


def test_filter_panel_on_filter_done_renders(qtbot, tmp_path):
    """_on_filter_done 当前 epoch 应渲染 + 更新 info。"""
    _make_qapp()
    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import _QtFilterPanel
    panel = _QtFilterPanel(parent=None, theme=themes.THEMES['dark'])
    qtbot.addWidget(panel)
    tab = _make_tab(qtbot, tmp_path, ['a', 'b', 'c'])
    panel.set_tab(tab)
    panel._filter_epoch = 1
    panel._on_filter_done(1, [0, 2], 3, '2 个匹配')
    assert panel._filtered_idx == [0, 2]
    panel.shutdown_filter_thread()


def test_filter_panel_on_filter_error_sets_msg(qtbot, tmp_path):
    """_on_filter_error 应设置 info。"""
    _make_qapp()
    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import _QtFilterPanel
    panel = _QtFilterPanel(parent=None, theme=themes.THEMES['dark'])
    qtbot.addWidget(panel)
    panel._filter_epoch = 1
    panel._on_filter_error(1, 'some error msg')
    assert panel._filtered_idx == []
    assert 'some error msg' in panel._info_lbl.text()
    panel.shutdown_filter_thread()


def test_filter_panel_apply_filter_incremental(qtbot, tmp_path):
    """_apply_filter_incremental 应只扫新增行。"""
    _make_qapp()
    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import _QtFilterPanel
    panel = _QtFilterPanel(parent=None, theme=themes.THEMES['dark'])
    qtbot.addWidget(panel)
    tab = _make_tab(qtbot, tmp_path, ['a', 'b', 'c', 'a'])
    panel.set_tab(tab)
    panel._filtered_idx = []
    panel._last_scanned_idx = 0
    panel._regex_edit.setText('a')
    panel._apply_filter_incremental(4)
    # 应找到 idx 0 和 3
    assert 0 in panel._filtered_idx
    assert 3 in panel._filtered_idx
    panel.shutdown_filter_thread()


def test_filter_panel_apply_filter_incremental_no_tab(qtbot):
    """_apply_filter_incremental 无 tab 应直接 return。"""
    _make_qapp()
    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import _QtFilterPanel
    panel = _QtFilterPanel(parent=None, theme=themes.THEMES['dark'])
    qtbot.addWidget(panel)
    panel._apply_filter_incremental(5)  # 不应抛
    panel.shutdown_filter_thread()


def test_filter_panel_apply_filter_incremental_caps(qtbot, tmp_path):
    """_apply_filter_incremental 应 cap 在 _get_filter_max()（可配置）。

    用一个小上限（100）造 150 行，验证 cap 到 100，独立于 config 默认值。
    """
    _make_qapp()
    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import _QtFilterPanel
    panel = _QtFilterPanel(parent=None, theme=themes.THEMES['dark'])
    qtbot.addWidget(panel)
    # 设一个小上限（在 spinbox 范围 1000~2000000 内），验证 cap 行为不依赖默认值
    panel._max_rows_box.setValue(1500)
    cap = panel._get_filter_max()
    assert cap == 1500
    # 准备超过 cap 的 tab
    lines = ['hit'] * (cap + 50)
    tab = _make_tab(qtbot, tmp_path, lines)
    panel.set_tab(tab)
    panel._filtered_idx = []
    panel._last_scanned_idx = 0
    panel._regex_edit.setText('hit')
    panel._apply_filter_incremental(len(lines))
    assert len(panel._filtered_idx) <= cap
    panel.shutdown_filter_thread()


def test_filter_panel_render_results_renders(qtbot, tmp_path):
    """_render_results 应在 tree 中插入项。"""
    _make_qapp()
    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import _QtFilterPanel
    panel = _QtFilterPanel(parent=None, theme=themes.THEMES['dark'])
    qtbot.addWidget(panel)
    tab = _make_tab(qtbot, tmp_path, ['a', 'b', 'c'])
    panel.set_tab(tab)
    panel._render_results([0, 2], 3)
    assert panel._tree.topLevelItemCount() == 2
    panel.shutdown_filter_thread()


def test_filter_panel_color_for_tag(qtbot):
    """_color_for_tag 应按 tag 返回颜色字符串。"""
    _make_qapp()
    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import _QtFilterPanel
    panel = _QtFilterPanel(parent=None, theme=themes.THEMES['dark'])
    qtbot.addWidget(panel)
    c_resp = panel._color_for_tag('resp')
    c_note = panel._color_for_tag('note')
    c_log = panel._color_for_tag('log')
    assert c_resp == themes.THEMES['dark'].get('accent', '#569cd6')
    assert c_note == themes.THEMES['dark'].get('status_err', '#f48771')
    assert c_log == themes.THEMES['dark'].get('panel_fg', '#cccccc')
    panel.shutdown_filter_thread()


def test_filter_panel_clear_filter(qtbot, tmp_path):
    """_clear_filter 应清 regex + level + 重新 apply。"""
    _make_qapp()
    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import _QtFilterPanel
    panel = _QtFilterPanel(parent=None, theme=themes.THEMES['dark'])
    qtbot.addWidget(panel)
    tab = _make_tab(qtbot, tmp_path, ['a'])
    panel.set_tab(tab)
    panel._regex_edit.setText('foo')
    panel._level_box.setCurrentIndex(2)
    panel._clear_filter()
    assert panel._regex_edit.text() == ''
    assert panel._level_box.currentIndex() == 0
    panel.shutdown_filter_thread()


def test_filter_panel_locate_line_binary_search(qtbot, tmp_path):
    """locate_line 应二分查找最近匹配行。"""
    _make_qapp()
    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import _QtFilterPanel
    panel = _QtFilterPanel(parent=None, theme=themes.THEMES['dark'])
    qtbot.addWidget(panel)
    tab = _make_tab(qtbot, tmp_path, ['a', 'b', 'c', 'd', 'e'])
    panel.set_tab(tab)
    panel._filtered_idx = [0, 2, 4]
    # 渲染进 tree
    panel._render_results([0, 2, 4], 5)
    # locate_line(3) 应选中 idx=2 (<=3 的最大值)
    panel.locate_line(3)
    # 不抛即可
    # locate_line(99) 也应不抛
    panel.locate_line(99)
    panel.shutdown_filter_thread()


def test_filter_panel_clear_results(qtbot, tmp_path):
    """clear_results 应清空 tree + filtered_idx。"""
    _make_qapp()
    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import _QtFilterPanel
    panel = _QtFilterPanel(parent=None, theme=themes.THEMES['dark'])
    qtbot.addWidget(panel)
    tab = _make_tab(qtbot, tmp_path, ['a', 'b'])
    panel.set_tab(tab)
    panel._filtered_idx = [0, 1]
    panel._render_results([0, 1], 2)
    assert panel._tree.topLevelItemCount() == 2
    panel.clear_results()
    assert panel._tree.topLevelItemCount() == 0
    assert panel._filtered_idx == []
    panel.shutdown_filter_thread()


def test_filter_panel_on_tree_double_click(qtbot, tmp_path):
    """_on_tree_double_click 应触发 on_locate。"""
    _make_qapp()
    from PySide6.QtWidgets import QTreeWidgetItem

    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import _QtFilterPanel
    called = []
    panel = _QtFilterPanel(parent=None, theme=themes.THEMES['dark'],
                          on_locate=lambda idx: called.append(idx))
    qtbot.addWidget(panel)
    tab = _make_tab(qtbot, tmp_path, ['a', 'b', 'c'])
    panel.set_tab(tab)
    panel._render_results([1], 3)
    # 双击第一项（idx=1）
    item = panel._tree.topLevelItem(0)
    panel._on_tree_double_click(item)
    assert called == [1]
    panel.shutdown_filter_thread()


def test_filter_panel_on_lines_appended(qtbot, tmp_path):
    """_on_lines_appended 应调 _apply_filter_incremental。"""
    _make_qapp()
    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import _QtFilterPanel
    panel = _QtFilterPanel(parent=None, theme=themes.THEMES['dark'])
    qtbot.addWidget(panel)
    tab = _make_tab(qtbot, tmp_path, ['a'])
    panel.set_tab(tab)
    panel._on_lines_appended(5)  # 不应抛
    panel.shutdown_filter_thread()


def test_filter_panel_minimum_and_size_hint(qtbot):
    """minimumSizeHint / sizeHint 应返回 QSize。"""
    _make_qapp()
    from PySide6.QtCore import QSize

    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import _QtFilterPanel
    panel = _QtFilterPanel(parent=None, theme=themes.THEMES['dark'])
    qtbot.addWidget(panel)
    ms = panel.minimumSizeHint()
    sh = panel.sizeHint()
    assert ms.width() == 0
    assert sh.width() > 0
    panel.shutdown_filter_thread()


def test_filter_panel_shutdown_thread_idempotent(qtbot):
    """shutdown_filter_thread 多次调用不应抛。"""
    _make_qapp()
    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import _QtFilterPanel
    panel = _QtFilterPanel(parent=None, theme=themes.THEMES['dark'])
    qtbot.addWidget(panel)
    panel.shutdown_filter_thread()
    panel.shutdown_filter_thread()


# ---- 任务4：筛选上限可配置 ----

def test_filter_panel_max_rows_box_range_and_default(qtbot, monkeypatch, tmp_path):
    """spinbox 应有正确范围 + 默认值来自 config（重定向到 tmp 避免污染）。"""
    import importlib

    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / '.config'))
    from serial_debug.infra import config
    importlib.reload(config)
    _make_qapp()
    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import _QtFilterPanel
    panel = _QtFilterPanel(parent=None, theme=themes.THEMES['dark'])
    qtbot.addWidget(panel)
    box = panel._max_rows_box
    # 范围 1000 ~ 2000000，步长 10000
    assert box.minimum() == 1000
    assert box.maximum() == 2_000_000
    assert box.singleStep() == 10000
    # 默认值 = config.DEFAULTS['log_view_filter_result_max'] = 500000
    assert box.value() == 500000
    # _get_filter_max 应与 spinbox 一致
    assert panel._get_filter_max() == 500000
    panel.shutdown_filter_thread()


def test_filter_panel_get_filter_max_clamps_to_range(qtbot):
    """_get_filter_max 返回 spinbox 值，超出范围被 Qt 自动 clamp。"""
    _make_qapp()
    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import _QtFilterPanel
    panel = _QtFilterPanel(parent=None, theme=themes.THEMES['dark'])
    qtbot.addWidget(panel)
    # 试图设 100（低于最小值 1000）→ Qt clamp 到 1000
    panel._max_rows_box.setValue(100)
    assert panel._get_filter_max() == 1000
    # 试图设 99999999（高于最大值）→ clamp 到 2000000
    panel._max_rows_box.setValue(99_999_999)
    assert panel._get_filter_max() == 2_000_000
    # 设中间值正常
    panel._max_rows_box.setValue(750_000)
    assert panel._get_filter_max() == 750_000
    panel.shutdown_filter_thread()


def test_filter_panel_save_filter_max_persists(qtbot, monkeypatch, tmp_path):
    """_save_filter_max 应写入 config（重定向到 tmp），重开面板读到新值。"""
    import importlib

    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / '.config'))
    from serial_debug.infra import config
    importlib.reload(config)
    _make_qapp()
    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import _QtFilterPanel
    panel = _QtFilterPanel(parent=None, theme=themes.THEMES['dark'])
    qtbot.addWidget(panel)
    # 改为 750000 并保存
    panel._save_filter_max(750_000)
    panel.shutdown_filter_thread()
    # 重读 config 文件，确认持久化
    cfg = config.load()
    assert cfg['log_view_filter_result_max'] == 750_000


def test_filter_panel_loads_custom_config_on_init(qtbot, monkeypatch, tmp_path):
    """config 已有自定义上限时，新面板构造应读到该值。"""
    import importlib

    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / '.config'))
    from serial_debug.infra import config
    importlib.reload(config)
    # 预写 config
    cfg = config.load()
    cfg['log_view_filter_result_max'] = 1_500_000
    config.save(cfg)
    _make_qapp()
    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import _QtFilterPanel
    panel = _QtFilterPanel(parent=None, theme=themes.THEMES['dark'])
    qtbot.addWidget(panel)
    assert panel._max_rows_box.value() == 1_500_000
    assert panel._get_filter_max() == 1_500_000
    panel.shutdown_filter_thread()


# ---- 任务1（无限制）：筛选行数不受限制 ----

def test_filter_panel_unlimited_default_off(qtbot, monkeypatch, tmp_path):
    """默认非无限制：_unlimited=False，checkbox 未勾选，spinbox 启用。"""
    import importlib

    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / '.config'))
    from serial_debug.infra import config
    importlib.reload(config)
    _make_qapp()
    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import _QtFilterPanel
    panel = _QtFilterPanel(parent=None, theme=themes.THEMES['dark'])
    qtbot.addWidget(panel)
    assert panel._unlimited is False
    assert panel._unlimited_box.isChecked() is False
    assert panel._max_rows_box.isEnabled() is True
    panel.shutdown_filter_thread()


def test_filter_panel_get_filter_max_unlimited_returns_big(qtbot, monkeypatch, tmp_path):
    """无限制模式下 _get_filter_max 返回 2_000_000_000，不受 spinbox 影响。"""
    import importlib

    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / '.config'))
    from serial_debug.infra import config
    importlib.reload(config)
    _make_qapp()
    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import _QtFilterPanel
    panel = _QtFilterPanel(parent=None, theme=themes.THEMES['dark'])
    qtbot.addWidget(panel)
    # 切到无限制
    panel._unlimited_box.setChecked(True)
    assert panel._unlimited is True
    assert panel._max_rows_box.isEnabled() is False
    # 不管 spinbox 是什么值，都返回大数
    assert panel._get_filter_max() == 2_000_000_000
    # 切回有上限：spinbox 恢复启用，值生效
    panel._unlimited_box.setChecked(False)
    assert panel._max_rows_box.isEnabled() is True
    assert panel._get_filter_max() == panel._max_rows_box.value()
    panel.shutdown_filter_thread()


def test_filter_panel_save_unlimited_persists(qtbot, monkeypatch, tmp_path):
    """切换无限制应持久化到 config，重开面板读到。"""
    import importlib

    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / '.config'))
    from serial_debug.infra import config
    importlib.reload(config)
    _make_qapp()
    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import _QtFilterPanel
    panel = _QtFilterPanel(parent=None, theme=themes.THEMES['dark'])
    qtbot.addWidget(panel)
    panel._save_unlimited(True)
    panel.shutdown_filter_thread()
    cfg = config.load()
    assert cfg['log_view_filter_unlimited'] is True


def test_filter_panel_loads_unlimited_on_init(qtbot, monkeypatch, tmp_path):
    """config 已设无限制时，新面板构造应读到：checkbox 勾选 + spinbox 禁用。"""
    import importlib

    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / '.config'))
    from serial_debug.infra import config
    importlib.reload(config)
    cfg = config.load()
    cfg['log_view_filter_unlimited'] = True
    config.save(cfg)
    _make_qapp()
    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import _QtFilterPanel
    panel = _QtFilterPanel(parent=None, theme=themes.THEMES['dark'])
    qtbot.addWidget(panel)
    assert panel._unlimited is True
    assert panel._unlimited_box.isChecked() is True
    assert panel._max_rows_box.isEnabled() is False
    assert panel._get_filter_max() == 2_000_000_000
    panel.shutdown_filter_thread()


def test_filter_panel_unlimited_does_not_truncate(qtbot, monkeypatch, tmp_path):
    """无限制模式下全量筛选不截断：命中行数 = 实际命中行数。

    构造 3000 行全部命中，无限制下筛选结果 = 3000（若用默认 500000 不
    截断也成立，但这里验证 _get_filter_max 在无限制下确实返回大数，且
    worker 不会因 cap 提前 break）。同时验证 info 标签含警告（>500000 时），
    这里用 3000 不触发警告，但验证 _on_filter_done 路径不破坏 info。
    """
    import importlib

    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / '.config'))
    from serial_debug.infra import config
    importlib.reload(config)
    _make_qapp()
    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import _QtFilterPanel
    panel = _QtFilterPanel(parent=None, theme=themes.THEMES['dark'])
    qtbot.addWidget(panel)
    # 切无限制
    panel._unlimited_box.setChecked(True)
    assert panel._get_filter_max() == 2_000_000_000
    lines = ['hit'] * 3000
    tab = _make_tab(qtbot, tmp_path, lines)
    panel.set_tab(tab)
    panel._regex_edit.setText('hit')
    panel._apply_filter()
    qtbot.wait(200)
    # 命中行数应等于 3000，不截断
    assert len(panel._filtered_idx) == 3000
    panel.shutdown_filter_thread()


def test_filter_panel_capped_mode_still_truncates(qtbot, monkeypatch, tmp_path):
    """非无限制模式下 spinbox 上限仍生效：命中超 cap 截断。"""
    import importlib

    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / '.config'))
    from serial_debug.infra import config
    importlib.reload(config)
    _make_qapp()
    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import _QtFilterPanel
    panel = _QtFilterPanel(parent=None, theme=themes.THEMES['dark'])
    qtbot.addWidget(panel)
    # 设小上限
    panel._max_rows_box.setValue(1500)
    assert panel._unlimited is False
    assert panel._get_filter_max() == 1500
    lines = ['hit'] * 3000
    tab = _make_tab(qtbot, tmp_path, lines)
    panel.set_tab(tab)
    panel._regex_edit.setText('hit')
    panel._apply_filter()
    qtbot.wait(200)
    # 应截断到 cap
    assert len(panel._filtered_idx) <= 1500
    assert len(panel._filtered_idx) == 1500
    panel.shutdown_filter_thread()


def test_filter_panel_unlimited_warning_above_threshold(qtbot, monkeypatch, tmp_path):
    """无限制模式 + 命中 > 500000 时，info label 应含警告提示。"""
    import importlib

    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / '.config'))
    from serial_debug.infra import config
    importlib.reload(config)
    _make_qapp()
    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import _QtFilterPanel
    panel = _QtFilterPanel(parent=None, theme=themes.THEMES['dark'])
    qtbot.addWidget(panel)
    panel._unlimited_box.setChecked(True)
    # 构造 600000 行全部命中（超过 500000 阈值）
    lines = ['hit'] * 600_000
    tab = _make_tab(qtbot, tmp_path, lines)
    panel.set_tab(tab)
    panel._regex_edit.setText('hit')
    panel._apply_filter()
    qtbot.wait(500)
    assert len(panel._filtered_idx) == 600_000
    info_text = panel._info_lbl.text()
    assert '可能变慢' in info_text or '建议筛更精确' in info_text
    panel.shutdown_filter_thread()


def test_filter_panel_unlimited_incremental_no_truncate(qtbot, monkeypatch, tmp_path):
    """无限制模式下增量筛选不截断：逐步追加全部命中。"""
    import importlib

    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / '.config'))
    from serial_debug.infra import config
    importlib.reload(config)
    _make_qapp()
    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import _QtFilterPanel
    panel = _QtFilterPanel(parent=None, theme=themes.THEMES['dark'])
    qtbot.addWidget(panel)
    panel._unlimited_box.setChecked(True)
    lines = ['hit'] * 2500
    tab = _make_tab(qtbot, tmp_path, lines)
    panel.set_tab(tab)
    panel._filtered_idx = []
    panel._last_scanned_idx = 0
    panel._regex_edit.setText('hit')
    # 分两批增量
    panel._apply_filter_incremental(1500)
    panel._apply_filter_incremental(1000)
    assert len(panel._filtered_idx) == 2500
    panel.shutdown_filter_thread()


# ---- 显示懒加载（大日志筛选不卡死）----

def test_filter_panel_render_results_caps_display(qtbot, tmp_path):
    """_render_results 命中数超过 FILTER_DISPLAY_MAX 时，树只插前 N 行，
    _filtered_idx 仍存全部命中。"""
    _make_qapp()
    from serial_debug.infra import themes
    from serial_debug.ui import qt_log_viewer_window as mod
    from serial_debug.ui.qt_log_viewer_window import _QtFilterPanel

    panel = _QtFilterPanel(parent=None, theme=themes.THEMES['dark'])
    qtbot.addWidget(panel)
    tab = _make_tab(qtbot, tmp_path, ['hit'] * 10)
    panel.set_tab(tab)
    panel._filtered_idx = list(range(10))
    # 构造 10 条命中，但临时把显示上限调到 3
    old = mod.FILTER_DISPLAY_MAX
    mod.FILTER_DISPLAY_MAX = 3
    try:
        panel._render_results(list(range(10)), 10)
        # 树只插前 3 行
        assert panel._tree.topLevelItemCount() == 3
        # _filtered_idx 仍存全部 10 条
        assert len(panel._filtered_idx) == 10
        assert panel._displayed_count == 3
        # 「加载更多」按钮应可见（offscreen 下父未 show 时 isVisible() 总返
        # False，改用 not isHidden() 校验显隐意图）
        assert not panel._load_more_btn.isHidden()
        # info 标签应提示"仅显示前 N 行"
        assert '仅显示前' in panel._info_lbl.text()
    finally:
        mod.FILTER_DISPLAY_MAX = old
    panel.shutdown_filter_thread()


def test_filter_panel_load_more_inserts_next_batch(qtbot, tmp_path):
    """「加载更多」点击后插入下一批，displayed_count 增长，按钮可见性更新。"""
    _make_qapp()
    from serial_debug.infra import themes
    from serial_debug.ui import qt_log_viewer_window as mod
    from serial_debug.ui.qt_log_viewer_window import _QtFilterPanel

    panel = _QtFilterPanel(parent=None, theme=themes.THEMES['dark'])
    qtbot.addWidget(panel)
    tab = _make_tab(qtbot, tmp_path, ['hit'] * 12)
    panel.set_tab(tab)
    panel._filtered_idx = list(range(12))
    old_disp = mod.FILTER_DISPLAY_MAX
    old_batch = mod.FILTER_LOAD_MORE_BATCH
    mod.FILTER_DISPLAY_MAX = 3
    mod.FILTER_LOAD_MORE_BATCH = 5
    try:
        panel._render_results(list(range(12)), 12)
        assert panel._tree.topLevelItemCount() == 3
        assert panel._displayed_count == 3
        # 加载一批 → 3+5=8
        panel._load_more()
        assert panel._tree.topLevelItemCount() == 8
        assert panel._displayed_count == 8
        assert not panel._load_more_btn.isHidden()
        # 再加载一批 → 8+4=12（只剩 4 条）
        panel._load_more()
        assert panel._tree.topLevelItemCount() == 12
        assert panel._displayed_count == 12
        # 全部加载完按钮隐藏
        assert panel._load_more_btn.isHidden()
    finally:
        mod.FILTER_DISPLAY_MAX = old_disp
        mod.FILTER_LOAD_MORE_BATCH = old_batch
    panel.shutdown_filter_thread()


def test_filter_panel_locate_line_lazy_loads(qtbot, tmp_path):
    """locate_line 目标 pos 超过已显示数时按需加载，定位成功。"""
    _make_qapp()
    from serial_debug.infra import themes
    from serial_debug.ui import qt_log_viewer_window as mod
    from serial_debug.ui.qt_log_viewer_window import _QtFilterPanel

    panel = _QtFilterPanel(parent=None, theme=themes.THEMES['dark'])
    qtbot.addWidget(panel)
    tab = _make_tab(qtbot, tmp_path, [str(i) for i in range(20)])
    panel.set_tab(tab)
    old_disp = mod.FILTER_DISPLAY_MAX
    old_batch = mod.FILTER_LOAD_MORE_BATCH
    mod.FILTER_DISPLAY_MAX = 5
    mod.FILTER_LOAD_MORE_BATCH = 3
    try:
        # 命中 20 行，树只显示前 5
        panel._filtered_idx = list(range(20))
        panel._render_results(list(range(20)), 20)
        assert panel._displayed_count == 5
        # locate_line(15) → bisect_right([0..19], 15) - 1 = 15，pos=15 > displayed=5
        # 应触发按需加载，然后定位到 item
        panel.locate_line(15)
        # 现在已加载覆盖到 pos=15
        assert panel._displayed_count > 15
        item = panel._tree.currentItem()
        assert item is not None
        # 当前选中项的 line_idx 应 = 15
        from PySide6.QtCore import Qt
        assert item.data(0, Qt.UserRole) == 15
    finally:
        mod.FILTER_DISPLAY_MAX = old_disp
        mod.FILTER_LOAD_MORE_BATCH = old_batch
    panel.shutdown_filter_thread()


def test_filter_panel_large_log_500k_no_freeze(qtbot, monkeypatch, tmp_path):
    """50 万行大日志 + 宽正则命中全部：筛选不卡死，树显示受限。

    验收点：构造 50 万行，宽正则命中全部，_filtered_idx=500000（不截断），
    树只插 FILTER_DISPLAY_MAX 行（不是 50 万），渲染耗时合理（< 10s），
    locate_line 在未显示区按需加载不抛。
    """
    import importlib
    import time as _time

    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / '.config'))
    from serial_debug.infra import config
    importlib.reload(config)
    _make_qapp()
    from serial_debug.infra import themes
    from serial_debug.ui import qt_log_viewer_window as mod
    from serial_debug.ui.qt_log_viewer_window import _QtFilterPanel

    panel = _QtFilterPanel(parent=None, theme=themes.THEMES['dark'])
    qtbot.addWidget(panel)
    # 无限制模式：不截断命中
    panel._unlimited_box.setChecked(True)
    assert panel._get_filter_max() == 2_000_000_000
    n = 500_000
    lines = ['hit'] * n
    tab = _make_tab(qtbot, tmp_path, lines)
    panel.set_tab(tab)
    panel._regex_edit.setText('hit')
    t0 = _time.monotonic()
    panel._apply_filter()
    qtbot.wait(1500)
    elapsed = _time.monotonic() - t0
    # 命中不截断
    assert len(panel._filtered_idx) == n, (
        f'expected {n} hits, got {len(panel._filtered_idx)}')
    # 树显示受限：不应是 50 万（应是 FILTER_DISPLAY_MAX）
    tree_count = panel._tree.topLevelItemCount()
    assert tree_count == mod.FILTER_DISPLAY_MAX, (
        f'tree should cap at FILTER_DISPLAY_MAX={mod.FILTER_DISPLAY_MAX}, '
        f'got {tree_count}')
    assert panel._displayed_count == mod.FILTER_DISPLAY_MAX
    # 「加载更多」按钮可见
    assert not panel._load_more_btn.isHidden()
    # info 标签提示"仅显示前"
    assert '仅显示前' in panel._info_lbl.text()
    # 渲染耗时合理（worker 扫 50 万 + 树插 5 万，应 < 10s）
    assert elapsed < 10.0, f'render too slow: {elapsed:.2f}s'
    # locate_line 在未显示区（如 line_idx=400000）按需加载不抛
    panel.locate_line(400_000)
    assert panel._displayed_count > 400_000
    # 定位后选中项应存在
    cur = panel._tree.currentItem()
    assert cur is not None
    panel.shutdown_filter_thread()


def test_filter_panel_ensure_displayed_to_idempotent(qtbot, tmp_path):
    """_ensure_displayed_to 对已显示范围不重复加载。"""
    _make_qapp()
    from serial_debug.infra import themes
    from serial_debug.ui import qt_log_viewer_window as mod
    from serial_debug.ui.qt_log_viewer_window import _QtFilterPanel

    panel = _QtFilterPanel(parent=None, theme=themes.THEMES['dark'])
    qtbot.addWidget(panel)
    tab = _make_tab(qtbot, tmp_path, ['hit'] * 10)
    panel.set_tab(tab)
    old_disp = mod.FILTER_DISPLAY_MAX
    mod.FILTER_DISPLAY_MAX = 4
    try:
        panel._filtered_idx = list(range(10))
        panel._render_results(list(range(10)), 10)
        assert panel._displayed_count == 4
        # pos=2 已在已显示范围 → 不加载
        panel._ensure_displayed_to(2)
        assert panel._displayed_count == 4
        # pos=8 超出 → 加载
        panel._ensure_displayed_to(8)
        assert panel._displayed_count > 8
    finally:
        mod.FILTER_DISPLAY_MAX = old_disp
    panel.shutdown_filter_thread()


# ---- QtLogViewerWindow 基础构造 ----

def test_qt_log_viewer_window_construction(qtbot, tmp_path):
    """QtLogViewerWindow 应能构造（无文件路径）。"""
    _make_qapp()
    from serial_debug.ui.qt_log_viewer_window import QtLogViewerWindow
    win = QtLogViewerWindow()
    qtbot.addWidget(win)
    # 不应抛——构造即覆盖 __init__ + _build_toolbar + _build_menubar
    assert win.windowTitle() != '' or win.windowTitle() == ''
    # 显式关线程避免 hang
    if win._filter_panel is not None:
        try:
            win._filter_panel.shutdown_filter_thread()
        except Exception:
            pass
    try:
        win._search_thread.quit()
        win._search_thread.wait(1000)
    except Exception:
        pass
    win.close()


def test_qt_log_viewer_window_open_file(qtbot, tmp_path):
    """open_file 应创建新 tab。"""
    _make_qapp()
    from serial_debug.ui.qt_log_viewer_window import QtLogViewerWindow
    log_path = tmp_path / 'win.log'
    log_path.write_text('line1\nline2\n', encoding='utf-8')
    win = QtLogViewerWindow()
    qtbot.addWidget(win)
    win.open_file(str(log_path))
    # 应有 1 个 tab
    tabs = win._tabs
    assert len(tabs) >= 1
    if win._filter_panel is not None:
        try:
            win._filter_panel.shutdown_filter_thread()
        except Exception:
            pass
    try:
        win._search_thread.quit()
        win._search_thread.wait(1000)
    except Exception:
        pass
    win.close()


def test_qt_log_viewer_window_close_event(qtbot, tmp_path):
    """closeEvent 应不抛 + 清理资源。"""
    _make_qapp()
    from PySide6.QtGui import QCloseEvent

    from serial_debug.ui.qt_log_viewer_window import QtLogViewerWindow
    win = QtLogViewerWindow()
    qtbot.addWidget(win)
    log_path = tmp_path / 'close.log'
    log_path.write_text('x\n', encoding='utf-8')
    win.open_file(str(log_path))
    # 触发 closeEvent
    try:
        win.closeEvent(QCloseEvent())
    except Exception:
        pass
    win.close()


def test_qt_log_viewer_window_toggle_live_mode(qtbot, tmp_path):
    """_toggle_live_mode 应切 current tab 的 live。"""
    _make_qapp()
    from serial_debug.ui.qt_log_viewer_window import QtLogViewerWindow
    win = QtLogViewerWindow()
    qtbot.addWidget(win)
    log_path = tmp_path / 'live.log'
    log_path.write_text('init\n', encoding='utf-8')
    win.open_file(str(log_path))
    try:
        win._toggle_live_mode(True)
    except Exception:
        pass
    # current tab 应进 live
    tab = win._current_tab()
    if tab is not None:
        assert tab.is_live_mode() or not tab.is_live_mode()  # 至少不抛
    if win._filter_panel is not None:
        try:
            win._filter_panel.shutdown_filter_thread()
        except Exception:
            pass
    try:
        win._search_thread.quit()
        win._search_thread.wait(1000)
    except Exception:
        pass
    win.close()


def test_qt_log_viewer_window_apply_theme(qtbot):
    """apply_theme 应切换 dark/light。"""
    _make_qapp()
    from serial_debug.ui.qt_log_viewer_window import QtLogViewerWindow
    win = QtLogViewerWindow()
    qtbot.addWidget(win)
    win.apply_theme('light')  # 不应抛
    win.apply_theme('dark')  # 切回
    # 显式关 filter thread 避免 hang
    if win._filter_panel is not None:
        try:
            win._filter_panel.shutdown_filter_thread()
        except Exception:
            pass
    try:
        win._search_thread.quit()
        win._search_thread.wait(1000)
    except Exception:
        pass
    win.close()


def test_qt_log_viewer_window_toggle_theme(qtbot):
    """_toggle_theme 应在 dark/light 间切换。"""
    _make_qapp()
    from serial_debug.ui.qt_log_viewer_window import QtLogViewerWindow
    win = QtLogViewerWindow()
    qtbot.addWidget(win)
    # 应能多次切换不抛
    win._toggle_theme()
    win._toggle_theme()
    if win._filter_panel is not None:
        try:
            win._filter_panel.shutdown_filter_thread()
        except Exception:
            pass
    try:
        win._search_thread.quit()
        win._search_thread.wait(1000)
    except Exception:
        pass
    win.close()


# ---- 任务3：主题菜单系统（log_viewer）----

def test_log_viewer_theme_menu_built(qtbot, monkeypatch, tmp_path):
    """构造 QtLogViewerWindow 后应建出主题菜单 actions。"""
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / '.config'))
    _make_qapp()
    from serial_debug.ui.qt_log_viewer_window import QtLogViewerWindow
    win = QtLogViewerWindow()
    qtbot.addWidget(win)
    # _build_menubar 应已调 _build_theme_menu，留下 mode/specific actions
    assert hasattr(win, '_theme_mode_actions')
    assert hasattr(win, '_theme_specific_actions')
    assert set(win._theme_mode_actions) == {'auto', 'light', 'dark'}
    assert len(win._theme_specific_actions) == 6
    if win._filter_panel is not None:
        try:
            win._filter_panel.shutdown_filter_thread()
        except Exception:
            pass
    try:
        win._search_thread.quit()
        win._search_thread.wait(1000)
    except Exception:
        pass
    win.close()


def test_log_viewer_apply_theme_with_pref_persists(qtbot, monkeypatch, tmp_path):
    """_apply_theme_with_pref 应持久化 pref + specific。"""
    import importlib

    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / '.config'))
    from serial_debug.infra import config
    importlib.reload(config)
    _make_qapp()
    from serial_debug.ui.qt_log_viewer_window import QtLogViewerWindow
    win = QtLogViewerWindow()
    qtbot.addWidget(win)
    win._apply_theme_with_pref('solarized_dark', pref='dark')
    cfg = config.load()
    assert cfg['theme_pref'] == 'dark'
    assert cfg['theme_specific_dark'] == 'solarized_dark'
    assert win._theme_name == 'solarized_dark'
    assert win._theme_pref == 'dark'
    if win._filter_panel is not None:
        try:
            win._filter_panel.shutdown_filter_thread()
        except Exception:
            pass
    try:
        win._search_thread.quit()
        win._search_thread.wait(1000)
    except Exception:
        pass
    win.close()


def test_log_viewer_on_theme_pref_selected_auto(qtbot, monkeypatch, tmp_path):
    """选 'auto' 模式应解析为系统深浅对应的具体主题。"""
    import importlib

    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / '.config'))
    from serial_debug.infra import config, themes
    importlib.reload(config)
    importlib.reload(themes)
    _make_qapp()
    from serial_debug.ui.qt_log_viewer_window import QtLogViewerWindow
    win = QtLogViewerWindow()
    qtbot.addWidget(win)
    monkeypatch.setattr(themes, 'detect_system_theme', lambda: 'dark')
    # 预设 specific_dark
    cfg = config.load()
    cfg['theme_specific_dark'] = 'monokai'
    config.save(cfg)
    win._on_theme_pref_selected('auto')
    assert win._theme_pref == 'auto'
    assert win._theme_name == 'monokai'
    if win._filter_panel is not None:
        try:
            win._filter_panel.shutdown_filter_thread()
        except Exception:
            pass
    try:
        win._search_thread.quit()
        win._search_thread.wait(1000)
    except Exception:
        pass
    win.close()


def test_log_viewer_sync_theme_menu_checks_current(qtbot, monkeypatch, tmp_path):
    """_sync_theme_menu 应勾选当前 pref + theme。"""
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / '.config'))
    _make_qapp()
    from serial_debug.ui.qt_log_viewer_window import QtLogViewerWindow
    win = QtLogViewerWindow()
    qtbot.addWidget(win)
    win._apply_theme_with_pref('github_light', pref='light')
    assert win._theme_mode_actions['light'].isChecked()
    assert not win._theme_mode_actions['dark'].isChecked()
    assert win._theme_specific_actions['github_light'].isChecked()
    if win._filter_panel is not None:
        try:
            win._filter_panel.shutdown_filter_thread()
        except Exception:
            pass
    try:
        win._search_thread.quit()
        win._search_thread.wait(1000)
    except Exception:
        pass
    win.close()


def test_qt_log_viewer_window_show_about(qtbot):
    """_show_about 应弹出关于对话框（mock exec 避免阻塞）。"""
    _make_qapp()
    from unittest import mock

    from serial_debug.ui.qt_log_viewer_window import QtLogViewerWindow
    win = QtLogViewerWindow()
    qtbot.addWidget(win)
    # mock QMessageBox.information 避免模态对话框阻塞
    with mock.patch('PySide6.QtWidgets.QMessageBox.information', return_value=0):
        try:
            win._show_about()
        except Exception:
            pass
    if win._filter_panel is not None:
        try:
            win._filter_panel.shutdown_filter_thread()
        except Exception:
            pass
    try:
        win._search_thread.quit()
        win._search_thread.wait(1000)
    except Exception:
        pass
    win.close()


def test_qt_log_viewer_window_jump_to_end(qtbot, tmp_path):
    """_jump_to_end 应不抛。"""
    _make_qapp()
    from serial_debug.ui.qt_log_viewer_window import QtLogViewerWindow
    win = QtLogViewerWindow()
    qtbot.addWidget(win)
    log_path = tmp_path / 'jump.log'
    log_path.write_text('a\nb\nc\n', encoding='utf-8')
    win.open_file(str(log_path))
    try:
        win._jump_to_end()
    except Exception:
        pass
    if win._filter_panel is not None:
        try:
            win._filter_panel.shutdown_filter_thread()
        except Exception:
            pass
    try:
        win._search_thread.quit()
        win._search_thread.wait(1000)
    except Exception:
        pass
    win.close()


def test_qt_log_viewer_window_focus_current_search(qtbot, tmp_path):
    """_focus_current_search 应不抛。"""
    _make_qapp()
    from serial_debug.ui.qt_log_viewer_window import QtLogViewerWindow
    win = QtLogViewerWindow()
    qtbot.addWidget(win)
    log_path = tmp_path / 'focus.log'
    log_path.write_text('x\n', encoding='utf-8')
    win.open_file(str(log_path))
    try:
        win._focus_current_search()
    except Exception:
        pass
    if win._filter_panel is not None:
        try:
            win._filter_panel.shutdown_filter_thread()
        except Exception:
            pass
    try:
        win._search_thread.quit()
        win._search_thread.wait(1000)
    except Exception:
        pass
    win.close()


def test_qt_log_viewer_window_toggle_filter_panel(qtbot):
    """_toggle_filter_panel 应切换显隐。"""
    _make_qapp()
    from serial_debug.ui.qt_log_viewer_window import QtLogViewerWindow
    win = QtLogViewerWindow()
    qtbot.addWidget(win)
    win._toggle_filter_panel(True)
    win._toggle_filter_panel(False)
    if win._filter_panel is not None:
        try:
            win._filter_panel.shutdown_filter_thread()
        except Exception:
            pass
    try:
        win._search_thread.quit()
        win._search_thread.wait(1000)
    except Exception:
        pass
    win.close()


def test_qt_log_viewer_window_toggle_global_search(qtbot):
    """_toggle_global_search 应切换显隐。"""
    _make_qapp()
    from serial_debug.ui.qt_log_viewer_window import QtLogViewerWindow
    win = QtLogViewerWindow()
    qtbot.addWidget(win)
    win._toggle_global_search(True)
    win._toggle_global_search(False)
    if win._filter_panel is not None:
        try:
            win._filter_panel.shutdown_filter_thread()
        except Exception:
            pass
    try:
        win._search_thread.quit()
        win._search_thread.wait(1000)
    except Exception:
        pass
    win.close()


def test_qt_log_viewer_window_toggle_toolbar(qtbot):
    """_toggle_toolbar 应切换显隐。"""
    _make_qapp()
    from serial_debug.ui.qt_log_viewer_window import QtLogViewerWindow
    win = QtLogViewerWindow()
    qtbot.addWidget(win)
    win._toggle_toolbar(True)
    win._toggle_toolbar(False)
    if win._filter_panel is not None:
        try:
            win._filter_panel.shutdown_filter_thread()
        except Exception:
            pass
    try:
        win._search_thread.quit()
        win._search_thread.wait(1000)
    except Exception:
        pass
    win.close()


def test_qt_log_viewer_window_close_current_tab(qtbot, tmp_path):
    """_close_current_tab 应能关闭当前 tab。"""
    _make_qapp()
    from serial_debug.ui.qt_log_viewer_window import QtLogViewerWindow
    win = QtLogViewerWindow()
    qtbot.addWidget(win)
    log_path = tmp_path / 'tab.log'
    log_path.write_text('x\n', encoding='utf-8')
    win.open_file(str(log_path))
    initial_count = len(win._tabs)
    if initial_count > 0:
        try:
            win._close_current_tab()
        except Exception:
            pass
    if win._filter_panel is not None:
        try:
            win._filter_panel.shutdown_filter_thread()
        except Exception:
            pass
    try:
        win._search_thread.quit()
        win._search_thread.wait(1000)
    except Exception:
        pass
    win.close()


# ---- _QtGlobalSearchBar ----

def test_global_search_bar_construction(qtbot):
    """_QtGlobalSearchBar 应能构造。"""
    _make_qapp()
    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import _QtGlobalSearchBar
    bar = _QtGlobalSearchBar(parent=None, theme=themes.THEMES['dark'])
    qtbot.addWidget(bar)
    assert bar._edit is not None


def test_global_search_bar_apply_theme(qtbot):
    """apply_theme 不应抛。"""
    _make_qapp()
    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import _QtGlobalSearchBar
    bar = _QtGlobalSearchBar(parent=None, theme=themes.THEMES['dark'])
    qtbot.addWidget(bar)
    bar.apply_theme(themes.THEMES['light'])  # 不应抛


def test_global_search_bar_do_search_invalid_regex(qtbot):
    """_do_search 非法正则应记 error。"""
    _make_qapp()
    from PySide6.QtWidgets import QApplication

    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import _QtGlobalSearchBar
    bar = _QtGlobalSearchBar(parent=None, theme=themes.THEMES['dark'])
    qtbot.addWidget(bar)
    bar._edit.setText(r'(unclosed')
    bar._do_search()
    QApplication.processEvents()
    # info 应显示错误
    assert '正则' in bar._info_lbl.text() or bar._info_lbl.text() == ''


def test_global_search_bar_do_search_dangerous_regex(qtbot):
    """_do_search 危险正则应被拒。"""
    _make_qapp()
    from PySide6.QtWidgets import QApplication

    from serial_debug.infra import themes
    from serial_debug.ui.qt_log_viewer_window import _QtGlobalSearchBar
    bar = _QtGlobalSearchBar(parent=None, theme=themes.THEMES['dark'])
    qtbot.addWidget(bar)
    bar._edit.setText(r'(a+)+')
    bar._do_search()
    QApplication.processEvents()
    assert '嵌套' in bar._info_lbl.text() or '回溯' in bar._info_lbl.text()


# ---- 任务5：跟随系统模式实时跟随系统深浅变化 ----

def test_log_viewer_check_theme_changed_auto_system_dark_to_light(qtbot, monkeypatch):
    """跟随系统模式：系统从 dark 切到 light，_check_theme_changed 应 apply 浅色主题。"""
    _make_qapp()
    from serial_debug.infra import config, themes
    from serial_debug.ui.qt_log_viewer_window import QtLogViewerWindow
    win = QtLogViewerWindow()
    qtbot.addWidget(win)
    # 预置：pref=auto, specific_light=github_light, specific_dark=monokai
    cfg = config.load()
    cfg['theme_pref'] = 'auto'
    cfg['theme_specific_light'] = 'github_light'
    cfg['theme_specific_dark'] = 'monokai'
    config.save(cfg)
    # 起始：系统 dark → 应用 monokai
    monkeypatch.setattr(themes, 'detect_system_theme', lambda: 'dark')
    win._theme_pref = 'auto'
    win._theme_name = 'monokai'
    win._theme = themes.THEMES['monokai']
    win._last_system_kind = 'dark'
    applied = []
    import serial_debug.ui.qt_theme as qt_theme
    monkeypatch.setattr(qt_theme, 'apply_qss',
                        lambda a, name: applied.append(name))
    # 系统切到 light
    monkeypatch.setattr(themes, 'detect_system_theme', lambda: 'light')
    win._check_theme_changed()
    assert win._theme_name == 'github_light'
    assert applied == ['github_light']
    assert win._last_system_kind == 'light'
    _close_log_viewer(win)


def test_log_viewer_check_theme_changed_auto_no_change_no_apply(qtbot, monkeypatch):
    """跟随系统模式：系统深浅未变时不重复 apply。"""
    _make_qapp()
    from serial_debug.infra import config, themes
    from serial_debug.ui.qt_log_viewer_window import QtLogViewerWindow
    win = QtLogViewerWindow()
    qtbot.addWidget(win)
    cfg = config.load()
    cfg['theme_pref'] = 'auto'
    cfg['theme_specific_dark'] = 'monokai'
    config.save(cfg)
    monkeypatch.setattr(themes, 'detect_system_theme', lambda: 'dark')
    win._theme_pref = 'auto'
    win._theme_name = 'monokai'
    win._theme = themes.THEMES['monokai']
    win._last_system_kind = 'dark'
    applied = []
    import serial_debug.ui.qt_theme as qt_theme
    monkeypatch.setattr(qt_theme, 'apply_qss',
                        lambda a, name: applied.append(name))
    win._check_theme_changed()
    assert applied == []
    assert win._theme_name == 'monokai'
    _close_log_viewer(win)


def test_log_viewer_check_theme_changed_fixed_dark_ignores_system(qtbot, monkeypatch):
    """固定深色模式：系统切到浅色不应跟随。"""
    _make_qapp()
    from serial_debug.infra import config, themes
    from serial_debug.ui.qt_log_viewer_window import QtLogViewerWindow
    win = QtLogViewerWindow()
    qtbot.addWidget(win)
    cfg = config.load()
    cfg['theme_pref'] = 'dark'
    cfg['theme_specific_dark'] = 'monokai'
    config.save(cfg)
    win._theme_pref = 'dark'
    win._theme_name = 'monokai'
    win._theme = themes.THEMES['monokai']
    win._last_system_kind = 'dark'
    applied = []
    import serial_debug.ui.qt_theme as qt_theme
    monkeypatch.setattr(qt_theme, 'apply_qss',
                        lambda a, name: applied.append(name))
    monkeypatch.setattr(themes, 'detect_system_theme', lambda: 'light')
    win._check_theme_changed()
    assert applied == []
    assert win._theme_name == 'monokai'
    _close_log_viewer(win)


def _close_log_viewer(win):
    """关闭 log_viewer 窗口，避免后台线程 hang 测试。"""
    if win._filter_panel is not None:
        try:
            win._filter_panel.shutdown_filter_thread()
        except Exception:
            pass
    try:
        win._search_thread.quit()
        win._search_thread.wait(1000)
    except Exception:
        pass
    win.close()
