"""ui/qt_at_cmd_window.py 测试：seq 排序 / seq=0 跳过 / 异常 / 批量停止。

用 pytest-qt + QtBot 做事件级测试，pyqtgraph 渲染留集成测试覆盖。
"""
from __future__ import annotations

import pytest

pytest.importorskip('PySide6')


def test_start_batch_orders_by_seq(qtbot):
    """M3 关联：批量执行按 seq 升序（seq=0 跳过）。

    _exec_next 用 QTimer 调度后续行；测试需让事件循环跑起来。
    """
    from serial_debug.ui.qt_at_cmd_window import AtCmdPanelQt
    sent = []
    p = AtCmdPanelQt(send_callback=lambda c: sent.append(c), initial_cmds=[
        {'cmd': 'AT+B', 'desc': '', 'seq': 2, 'period_ms': 0},
        {'cmd': 'AT+A', 'desc': '', 'seq': 1, 'period_ms': 0},
        {'cmd': 'AT+SKIP', 'desc': '', 'seq': 0, 'period_ms': 0},
    ])
    p._build()
    qtbot.addWidget(p._widget)
    p._start_batch()
    # 等 QTimer 触发后续 _exec_next（period_ms=0 → start(1)，1ms 后 fire）
    qtbot.wait(50)
    # seq=1 先发，seq=2 后发，seq=0 跳过
    assert sent == ['AT+A', 'AT+B']


def test_start_batch_skips_seq_zero(qtbot):
    """seq=0 应被跳过，即便有命令也不发。"""
    from serial_debug.ui.qt_at_cmd_window import AtCmdPanelQt
    sent = []
    p = AtCmdPanelQt(send_callback=lambda c: sent.append(c), initial_cmds=[
        {'cmd': 'AT+Z', 'desc': '', 'seq': 0, 'period_ms': 0},
        {'cmd': 'AT+Y', 'desc': '', 'seq': 0, 'period_ms': 0},
    ])
    p._build()
    qtbot.addWidget(p._widget)
    p._start_batch()
    assert sent == []
    # 状态栏应显示「无批量命令」
    assert 'seq=0' in p._batch_status.text() or '无' in p._batch_status.text()


def test_send_callback_exception_stops_batch_and_reports(qtbot):
    """M3 核心：send_callback 抛异常 → _stop_batch + 状态栏报错。"""
    from serial_debug.ui.qt_at_cmd_window import AtCmdPanelQt
    sent = []

    def bad_cb(cmd):
        sent.append(cmd)
        raise RuntimeError('device offline')

    p = AtCmdPanelQt(send_callback=bad_cb, initial_cmds=[
        {'cmd': 'AT+A', 'desc': '', 'seq': 1, 'period_ms': 0},
        {'cmd': 'AT+B', 'desc': '', 'seq': 2, 'period_ms': 0},
    ])
    p._build()
    qtbot.addWidget(p._widget)
    p._start_batch()
    # 应该只发了第一条就停止，状态栏含「失败」
    assert sent == ['AT+A']
    assert p._batch_running is False
    status_text = p._batch_status.text()
    assert '失败' in status_text or 'error' in status_text.lower()


def test_send_row_callback_exception_reports(qtbot):
    """M3 单行发送：send_callback 抛异常 → status 报错，不崩。"""
    from serial_debug.ui.qt_at_cmd_window import AtCmdPanelQt

    def bad_cb(cmd):
        raise OSError('port closed')

    p = AtCmdPanelQt(send_callback=bad_cb, initial_cmds=[
        {'cmd': 'AT+X', 'desc': '', 'seq': 0, 'period_ms': 0},
    ])
    p._build()
    qtbot.addWidget(p._widget)
    # 不应抛
    p._send_row(0)
    # 状态栏应有失败提示
    assert p._batch_status.text()


def test_stop_batch_releases_timer(qtbot):
    """M2 关联：_stop_batch 应清掉 _batch_timer 并 deleteLater。"""
    from serial_debug.ui.qt_at_cmd_window import AtCmdPanelQt
    sent = []
    p = AtCmdPanelQt(send_callback=lambda c: sent.append(c), initial_cmds=[
        {'cmd': 'AT+A', 'desc': '', 'seq': 1, 'period_ms': 100},
        {'cmd': 'AT+B', 'desc': '', 'seq': 2, 'period_ms': 100},
    ])
    p._build()
    qtbot.addWidget(p._widget)
    p._start_batch()
    # 启动后应有一个 timer
    assert p._batch_timer is not None
    p._stop_batch()
    assert p._batch_timer is None
    assert p._batch_running is False


def test_add_row_extends_commands(qtbot):
    """添加行应让 self.commands 长度 +1。"""
    from serial_debug.ui.qt_at_cmd_window import AtCmdPanelQt
    p = AtCmdPanelQt(send_callback=lambda c: None, initial_cmds=[
        {'cmd': 'AT+OLD', 'desc': '', 'seq': 1, 'period_ms': 0},
    ])
    p._build()
    qtbot.addWidget(p._widget)
    n0 = len(p.commands)
    p._add_row()
    assert len(p.commands) == n0 + 1


def test_delete_selected_removes_rows(qtbot):
    """删除选中行应让 self.commands 减对应行数。"""
    from PySide6.QtWidgets import QTableWidgetSelectionRange

    from serial_debug.ui.qt_at_cmd_window import AtCmdPanelQt
    p = AtCmdPanelQt(send_callback=lambda c: None, initial_cmds=[
        {'cmd': 'AT+A', 'desc': '', 'seq': 1, 'period_ms': 0},
        {'cmd': 'AT+B', 'desc': '', 'seq': 2, 'period_ms': 0},
        {'cmd': 'AT+C', 'desc': '', 'seq': 3, 'period_ms': 0},
    ])
    p._build()
    qtbot.addWidget(p._widget)
    # 通过 setRangeSelected 让 selectedIndexes() 包含第 0 行所有列
    p._table.setRangeSelected(
        QTableWidgetSelectionRange(0, 0, 0, 4), True)
    p._delete_selected()
    assert len(p.commands) == 2
    # 第 0 行被删
    assert p.commands[0]['cmd'] == 'AT+B'


def test_sync_from_table_commits_edits(qtbot):
    """_sync_from_table 应把表格内编辑后的内容同步回 commands。"""
    from PySide6.QtWidgets import QTableWidgetItem

    from serial_debug.ui.qt_at_cmd_window import AtCmdPanelQt
    p = AtCmdPanelQt(send_callback=lambda c: None, initial_cmds=[
        {'cmd': 'AT+OLD', 'desc': '', 'seq': 1, 'period_ms': 0},
    ])
    p._build()
    qtbot.addWidget(p._widget)
    # 模拟编辑：直接改 item 文本
    p._table.setItem(0, 0, QTableWidgetItem('AT+NEW'))
    p._table.setItem(0, 2, QTableWidgetItem('5'))
    p._sync_from_table()
    assert p.commands[0]['cmd'] == 'AT+NEW'
    assert p.commands[0]['seq'] == 5
