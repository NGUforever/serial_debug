"""qt_maximizable_dialog + qt_monitor_window 推导入口测试。

功能 1：MaximizableDialog 最大化/还原（offscreen 冒烟 + 状态切换）。
功能 2：infer_parse_line_from_source / replay_parse_line_draft——
源码 + 日志 → 生成的 parse_line 回放日志能提取正确字段（C / Python 各一）。
"""
from __future__ import annotations

import pytest

pytest.importorskip('PySide6')

from serial_debug.ui.qt_maximizable_dialog import MaximizableDialog

from serial_debug.ui.qt_monitor_window import (
    infer_parse_line_from_source,
    replay_parse_line_draft,
)


@pytest.fixture(scope='module')
def qapp():
    import os
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


# ---- 功能 1：MaximizableDialog ---------------------------------------

def test_maximizable_dialog_basic(qapp):
    """能构造、能 setWindowTitle，带最大/还原 hint。"""
    from PySide6.QtCore import Qt
    dlg = MaximizableDialog(None)
    dlg.setWindowTitle('测试对话框')
    assert dlg.windowTitle() == '测试对话框'
    flags = dlg.windowFlags()
    assert flags & Qt.WindowMaximizeButtonHint
    assert flags & Qt.WindowMinMaxButtonsHint


def test_maximizable_dialog_toggle_roundtrip(qapp):
    """toggle_maximize 最大化后能还原回原尺寸（记住窗口状态）。"""
    dlg = MaximizableDialog(None)
    dlg.resize(640, 480)
    dlg.show()
    normal_size = dlg.size()
    dlg.toggle_maximize()
    assert dlg.isMaximized()
    dlg.toggle_maximize()
    assert not dlg.isMaximized()
    # 还原后尺寸应回到 640x480（允许窗口管理器 offscreen 差异 ±2px）
    assert abs(dlg.width() - normal_size.width()) <= 2
    assert abs(dlg.height() - normal_size.height()) <= 2
    dlg.close()


def test_maximizable_dialog_double_click_no_crash(qapp):
    """双击（合成事件）切换不崩。"""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtCore import QEvent
    dlg = MaximizableDialog(None)
    dlg.resize(400, 300)
    dlg.show()
    pos = QPointF(10.0, 10.0)
    ev = QMouseEvent(QEvent.MouseButtonDblClick, pos, pos,
                     Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier)
    # 发给对话框自身（客户区空白）——应触发 toggle，不抛异常
    was_max = dlg.isMaximized()
    dlg.mouseDoubleClickEvent(ev)
    assert dlg.isMaximized() != was_max
    dlg.close()


def test_maximizable_dialog_save_restore_state(qapp):
    """save_state / restore_state 往返。"""
    from PySide6.QtCore import Qt
    dlg = MaximizableDialog(None)
    dlg.resize(500, 400)
    st = dlg.save_state()
    assert st['maximized'] is False
    assert st['geometry'][2] == 500
    # 还原坏数据不崩
    dlg.restore_state(None)
    dlg.restore_state({'geometry': [10, 20, 300, 200]})
    assert dlg.width() == 300
    # 最大化状态恢复
    dlg.restore_state({'maximized': True, 'geometry': [0, 0, 1, 1]})
    assert dlg.windowState() & Qt.WindowState.WindowMaximized
    dlg.close()


def test_maximizable_dialog_used_by_parser_config(qapp):
    """解析器配置对话框实际用 MaximizableDialog（功能 1 覆盖验证）。"""
    from PySide6.QtWidgets import QMainWindow
    from serial_debug.ui import qt_monitor_window as mod
    win = QMainWindow()
    dlg = mod._QtParserConfigDialog(win, callback_save=lambda: None)
    built = dlg._build()
    assert isinstance(built, MaximizableDialog)
    built.close()
    win.deleteLater()


# ---- 功能 2：源码 + 日志 → parse_line 推导 ---------------------------

C_SOURCE = '''#include <stdio.h>
void motor_report(long ts, int rpm, float vbus) {
    printf("ts=%ld rpm=%d vbus=%.2f\\n", ts, rpm, vbus);
}
'''

C_LOG = '''1788418270935 ts=1788418270935 rpm=1500 vbus=48.20
1788418271935 ts=1788418271935 rpm=1520 vbus=48.10
noise line without fields
1788418272935 ts=1788418272935 rpm=1540 vbus=48.05
'''

PY_SOURCE = 'def report():\n    print(f"rpm={rpm} vbus={vbus}")\n'

PY_LOG = '''1788418270935 rpm=1500 vbus=48.2
1788418271935 rpm=1520 vbus=48.1
'''


def test_infer_c_source_replay_extracts_fields():
    """C 源码 + 日志 → 生成 parse_line 回放能提取 rpm/vbus。"""
    code, lang, n = infer_parse_line_from_source(C_SOURCE, 'auto')
    assert lang in ('c', 'cpp')
    assert n >= 1
    assert 'def parse_line(line):' in code
    rep = replay_parse_line_draft(code, C_LOG)
    assert rep['error'] is None
    found = rep['found']
    assert 'rpm' in found, f'rpm 缺失: {found}'
    assert 'vbus' in found
    assert found['rpm']['count'] == 3
    assert found['rpm']['first'] == 1500.0
    assert found['rpm']['last'] == 1540.0
    assert found['vbus']['count'] == 3
    # 命中 3 行（噪声行不命中）
    assert rep['hit_lines'] == 3
    assert rep['total_lines'] == 4


def test_infer_c_explicit_language():
    """显式指定语言 c 也能推导。"""
    code, lang, _ = infer_parse_line_from_source(C_SOURCE, 'c')
    assert lang == 'c'
    rep = replay_parse_line_draft(code, C_LOG)
    assert rep['error'] is None
    assert 'rpm' in rep['found']


def test_infer_python_source_replay_extracts_fields():
    """Python f-string 源码 + 日志 → 回放提取 rpm/vbus。"""
    code, lang, n = infer_parse_line_from_source(PY_SOURCE, 'auto')
    assert lang == 'python'
    assert n >= 1
    rep = replay_parse_line_draft(code, PY_LOG)
    assert rep['error'] is None
    found = rep['found']
    assert 'rpm' in found, f'rpm 缺失: {found}'
    assert 'vbus' in found
    assert found['rpm']['first'] == 1500.0
    assert found['rpm']['last'] == 1520.0
    assert rep['hit_lines'] == 2


def test_infer_empty_source_raises():
    with pytest.raises(ValueError):
        infer_parse_line_from_source('   ', 'auto')


def test_infer_html_rejected():
    with pytest.raises(ValueError):
        infer_parse_line_from_source('<!DOCTYPE html><html></html>', 'auto')


def test_infer_no_print_statement_raises():
    with pytest.raises(ValueError):
        infer_parse_line_from_source('int x = 1;\nint y = 2;\n', 'c')


def test_infer_error_message_readable():
    """推导失败错误信息可读（非静默空白）。"""
    try:
        infer_parse_line_from_source('int x = 1;\n', 'c')
    except ValueError as e:
        msg = str(e)
        assert 'print' in msg and 'c' in msg
    else:
        pytest.fail('应抛 ValueError')


def test_replay_bad_code_returns_error():
    """坏代码 → error 字段带可读原因，不抛异常。"""
    rep = replay_parse_line_draft('def not_parse_line(x):\n    pass\n', 'abc')
    assert rep['error'] is not None
    assert 'parse_line' in rep['error']


def test_replay_syntax_error_returns_error():
    rep = replay_parse_line_draft('def parse_line(line\n', 'abc')
    assert rep['error'] is not None
    assert '语法' in rep['error']


def test_replay_empty_log_returns_error():
    code, _, _ = infer_parse_line_from_source(C_SOURCE, 'c')
    rep = replay_parse_line_draft(code, '')
    assert rep['error'] is not None
    assert '空' in rep['error']


def test_replay_zero_hit_rate():
    """日志与源码不对应 → 命中率为 0，found 为空但不崩。"""
    code, _, _ = infer_parse_line_from_source(C_SOURCE, 'c')
    rep = replay_parse_line_draft(code, 'totally unrelated log lines\n' * 3)
    assert rep['error'] is None
    assert rep['found'] == {}
    assert rep['hit_lines'] == 0
    assert rep['total_lines'] == 3
