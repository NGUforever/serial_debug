"""ui/qt_app.py 测试：路由 / 时间戳前缀 / 资源路径。

聚焦不依赖完整 UI 构建的逻辑层：_route_line / _ts_prefix_if_needed /
_resource_path。完整 _create() 涉及大量 Qt widget 构造，留给集成测试覆盖。
"""
from __future__ import annotations

import os
import re
import sys

import pytest

pytest.importorskip('PySide6')

from serial_debug.ui import qt_app as mod


@pytest.fixture
def app(qtbot, tmp_path, monkeypatch):
    """构建一个 QtSerialDebugApp，但跳过 _create()（避免完整 UI 构建）。"""
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / '.config'))
    import importlib

    from serial_debug.infra import config
    importlib.reload(config)
    importlib.reload(mod)
    a = mod.QtSerialDebugApp()
    return a


# ---- _resource_path ----
def test_resource_path_uses_meipass(monkeypatch):
    monkeypatch.setattr(sys, '_MEIPASS', '/tmp/fake', raising=False)
    p = mod._resource_path('assets/icon.png')
    assert p == os.path.join('/tmp/fake', 'serial_debug', 'resources',
                             'assets', 'icon.png')


def test_resource_path_falls_back_to_importlib(monkeypatch):
    if hasattr(sys, '_MEIPASS'):
        monkeypatch.delattr(sys, '_MEIPASS', raising=False)
    p = mod._resource_path('common_at.json')
    assert p.endswith('common_at.json')


# ---- _make_status_dot_class ----
def test_make_status_dot_class_returns_qwidget_subclass(qtbot):
    cls = mod._make_status_dot_class()
    from PySide6.QtWidgets import QWidget
    assert issubclass(cls, QWidget)
    inst = cls()
    assert inst._state == 'idle'


# ---- _route_line ----
def test_route_line_empty_returns_none(app):
    assert app._route_line('') is None
    assert app._route_line('\n') is None


def test_route_line_send_arrow_tag(app):
    text, tag = app._route_line('→ AT+ELOG=1%')
    assert tag == 'send'
    assert text.startswith('→ AT+ELOG=1%')


def test_route_line_response_matches_resp_re(app):
    text, tag = app._route_line('OK,0%')
    assert tag == 'resp'
    # pending_cmd 应被清空
    app.pending_cmd = 'AT+ELOG=0%'
    app._route_line('OK,1%')
    assert app.pending_cmd is None


def test_route_line_response_error_tag(app):
    text, tag = app._route_line('ERROR')
    assert tag == 'resp'


def test_route_line_response_plus_cmd_tag(app):
    text, tag = app._route_line('+CME ERROR: 100')
    assert tag == 'resp'


def test_route_line_log_tag_for_periodic(app):
    text, tag = app._route_line('1783996615491 I model 3wd1t_1 0 0 0')
    assert tag == 'log'
    assert '1783996615491' in text


def test_route_line_log_filtered_out_when_pattern_doesnt_match(app):
    app._filter_enabled = True
    app._filter_compiled = re.compile('^BMS')
    # 一行 BMS 数据应通过；一行 IMU 数据应被过滤
    assert app._route_line('BMS [3 4 4]') is not None
    assert app._route_line('Imu_data 0 2 3') is None


def test_route_line_send_and_resp_bypass_filter(app):
    """发送 / 响应行不应被筛选过滤。"""
    app._filter_enabled = True
    app._filter_compiled = re.compile('^BMS')
    assert app._route_line('→ AT') is not None   # send
    assert app._route_line('OK,0%') is not None  # resp


def test_route_line_strips_crlf(app):
    text, tag = app._route_line('OK,0%\r\n')
    assert tag == 'resp'
    assert 'OK,0%' in text
    assert '\r' not in text


# ---- _ts_prefix_if_needed ----
def test_ts_prefix_disabled_returns_empty(app):
    app.ts_convert = False
    assert app._ts_prefix_if_needed('1783996615491 hello') == ''


def test_ts_prefix_converts_13_digit_ms(app):
    app.ts_convert = True
    p = app._ts_prefix_if_needed('1783996615491 hello')
    assert p.endswith(' ')
    # 形如 HH:MM:SS.mmm
    parts = p.strip().split(':')
    assert len(parts) == 3


def test_ts_prefix_no_match_returns_empty(app):
    app.ts_convert = True
    assert app._ts_prefix_if_needed('hello world') == ''


def test_ts_prefix_non_13_digit_no_match(app):
    """TS_RE 只匹配 13 位，10 位秒级时间戳不匹配。"""
    app.ts_convert = True
    assert app._ts_prefix_if_needed('1234567890 hello') == ''


def test_ts_prefix_combined_with_route_line(app):
    app.ts_convert = True
    text, tag = app._route_line('1783996615491 I model 3wd1t_1 0 0 0')
    assert tag == 'log'
    # 应有时间戳前缀（HH:MM:SS.mmm + 空格）
    assert ':' in text.split(' ')[0]


# ---- _make_status_dot set_state ----
def test_status_dot_set_state_updates_attr(qtbot):
    cls = mod._make_status_dot_class()
    inst = cls()
    inst.set_state('ok', 'dark')
    assert inst._state == 'ok'
    assert inst._theme_resolved == 'dark'


def test_status_dot_default_state_idle(qtbot):
    cls = mod._make_status_dot_class()
    inst = cls()
    assert inst._state == 'idle'
    inst.set_state('err', 'light')
    assert inst._state == 'err'


# ---- _make_divider / _make_btn ----
def test_make_divider_returns_frame_with_divider_property(app, qtbot):
    from PySide6.QtWidgets import QFrame
    d = app._make_divider()
    assert isinstance(d, QFrame)
    assert d.property('divider') is True
    # minimumWidth 应被设为 1
    assert d.minimumWidth() == 1 or d.minimumSize().width() == 1


def test_make_btn_basic(app, qtbot):
    from PySide6.QtWidgets import QPushButton
    btn = app._make_btn('连接')
    assert isinstance(btn, QPushButton)
    assert btn.minimumWidth() > 0


def test_make_btn_primary_sets_property(app, qtbot):
    btn = app._make_btn('发送', primary=True)
    assert btn.property('primary') is True


def test_make_btn_with_icon_std_does_not_crash(app, qtbot):
    """传入 standard icon enum 应不崩（找不到也吞掉异常）。"""
    from PySide6.QtWidgets import QStyle
    btn = app._make_btn('刷新', icon_std=QStyle.StandardPixmap.SP_BrowserReload)
    assert btn is not None


# ---- _on_crlf_toggled / _on_autoscroll_toggled / _on_ts_convert_toggled ----
def test_on_crlf_toggled_sets_append_crlf(app):
    app._on_crlf_toggled(True)
    assert app.append_crlf is True
    app._on_crlf_toggled(False)
    assert app.append_crlf is False


def test_on_autoscroll_toggled_sets_state(app):
    app._on_autoscroll_toggled(True)
    assert app.auto_scroll is True
    app._on_autoscroll_toggled(False)
    assert app.auto_scroll is False


def test_on_ts_convert_toggled_sets_state(app):
    app._on_ts_convert_toggled(True)
    assert app.ts_convert is True
    app._on_ts_convert_toggled(False)
    assert app.ts_convert is False


def test_on_clear_clicked_clears_log_edit(app, qtbot):
    """_on_clear_clicked 应调 QPlainTextEdit.clear()。"""
    from PySide6.QtWidgets import QPlainTextEdit
    edit = QPlainTextEdit()
    edit.setPlainText('hello\nworld')
    app._log_edit = edit
    app._on_clear_clicked()
    assert edit.toPlainText() == ''


# ---- _send_cmd ----
def test_send_cmd_empty_returns_false(app):
    assert app._send_cmd('') is False


def test_send_cmd_not_connected_returns_false(app):
    """未连接时应返回 False 并设置 _dot_state='wait'。"""
    app.backend = type('B', (), {'is_open': False})()
    app._status_lbl = type('L', (), {'setText': lambda self_, t: None})()
    assert app._send_cmd('AT') is False
    assert app._dot_state == 'wait'


def test_send_cmd_connected_sends_and_sets_pending(app):
    """已连接时应调 backend.send + 设置 pending_cmd + 回显到日志。"""
    sent = {'cmd': None, 'append_crlf': None}
    app.backend = type('B', (), {
        'is_open': True,
        'send': lambda self_, cmd, append_crlf: sent.update(cmd=cmd, append_crlf=append_crlf),
    })()
    # 用 list 收集 _append_log 调用
    logged = []
    app._append_log = lambda text, tag='log': logged.append((text, tag))
    assert app._send_cmd('AT+ELOG=1%') is True
    assert sent['cmd'] == 'AT+ELOG=1%'
    assert sent['append_crlf'] is False  # append_crlf 默认 False
    assert app.pending_cmd == 'AT+ELOG=1%'
    assert logged[0][1] == 'send'
    assert '→ AT+ELOG=1%' in logged[0][0]


def test_send_cmd_external_alias(app):
    """_send_cmd_external 应与 _send_cmd 等价（同源函数对象）。"""
    assert app._send_cmd_external.__func__ is app._send_cmd.__func__


# ---- _on_send_clicked ----
def test_on_send_clicked_no_cmd_edit_returns(app):
    """_cmd_edit=None 时不应崩。"""
    app._cmd_edit = None
    app._on_send_clicked()  # 不应抛


def test_on_send_clicked_empty_cmd_skips(app):
    """空命令应跳过。"""
    app._cmd_edit = type('E', (), {
        'text': lambda self_: '',
        'clear': lambda self_: None,
    })()
    app._on_send_clicked()


def test_on_send_clicked_sends_and_clears_input(app):
    """有命令时应发送并清空输入框。"""
    cleared = {'n': 0}
    sent = {'cmd': None}
    app.backend = type('B', (), {
        'is_open': True,
        'send': lambda self_, cmd, append_crlf: sent.update(cmd=cmd),
    })()
    app._append_log = lambda text, tag='log': None
    app._cmd_edit = type('E', (), {
        'text': lambda self_: 'AT',
        'clear': lambda self_: cleared.__setitem__('n', cleared['n'] + 1),
    })()
    app._on_send_clicked()
    assert sent['cmd'] == 'AT'
    assert cleared['n'] == 1


# ---- _save_cfg_now ----
def test_save_cfg_now_writes_config(app, tmp_path):
    """_save_cfg_now 应把当前状态写入 config。"""
    app.append_crlf = True
    app.ts_convert = True
    app.theme_name = 'dark'
    app._bytesize = 7
    app._parity = 'even'
    app._stopbits = 2
    app._flow_control = 'xon'
    app._filter_pattern = 'BMS'
    app._filter_enabled = True
    app.log_path = '/tmp/test.log'
    app._at_mode = 'embedded'
    app._save_cfg_now()
    from serial_debug.infra import config
    cfg = config.load()
    assert cfg['append_crlf'] is True
    assert cfg['ts_convert_enabled'] is True
    assert cfg['theme_pref'] == 'dark'
    assert cfg['bytesize'] == 7
    assert cfg['parity'] == 'even'
    assert cfg['stopbits'] == 2
    assert cfg['flow_control'] == 'xon'
    assert cfg['filter_pattern'] == 'BMS'
    assert cfg['filter_enabled'] is True
    assert cfg['log_path'] == '/tmp/test.log'
    assert cfg['at_window_mode'] == 'embedded'


def test_save_cfg_now_swallows_config_failure(app, monkeypatch):
    """config.save 抛异常应被吞掉。"""
    from serial_debug.infra import config
    def boom(_):
        raise OSError('disk full')
    monkeypatch.setattr(config, 'save', boom)
    # 不应抛
    app._save_cfg_now()


# ---- _refresh_ports ----
def test_refresh_ports_no_combo_returns(app):
    """无 _port_combo 时不应崩。"""
    app._port_combo = None
    app._refresh_ports()


def test_refresh_ports_populates_combo(app, monkeypatch):
    """有 _port_combo 时应填充端口列表。"""
    items = []
    cur_text = {'val': ''}

    class FakeCombo:
        def currentText(self):
            return cur_text['val']
        def clear(self):
            items.clear()
        def addItem(self, name):
            items.append(name)
        def setCurrentText(self, s):
            cur_text['val'] = s

    app._port_combo = FakeCombo()
    monkeypatch.setattr(mod.SerialBackend, 'list_ports',
                        staticmethod(lambda: ['/dev/ttyUSB0', '/dev/ttyUSB1']))
    app._refresh_ports()
    assert items == ['/dev/ttyUSB0', '/dev/ttyUSB1']


# ---- _toggle_toolbar / _toggle_statusbar ----
def test_toggle_toolbar_no_toolbar_is_noop(app):
    app._toolbar = None
    app._toggle_toolbar(True)  # 不应崩


def test_toggle_statusbar_no_win_is_noop(app):
    app._win = None
    app._toggle_statusbar(True)  # 不应崩


# ---- _apply_at_mode ----
def test_apply_at_mode_hidden_clears_state(app):
    """hidden 模式应清理独立窗口和嵌入面板引用（即使原本是 None）。"""
    app._at_mode = 'detached'
    app._at_window_qt = None
    app._at_panel_widget = None
    # 不应崩——_save_cfg_debounced 会创建 QTimer，但本测试不验证该副作用
    app._apply_at_mode('hidden')
    assert app._at_mode == 'hidden'


# ---- _ts_prefix_if_needed 边界 ----
def test_ts_prefix_13_digit_zero(app):
    app.ts_convert = True
    p = app._ts_prefix_if_needed('0000000000000 hello')
    assert p.startswith('00:00:00.000 ')


# ---- _drain ----
def _make_fake_queue(items):
    """构造一个能被 get_nowait 消费的假队列。"""
    class _Q:
        def __init__(self, items):
            self._items = list(items)
        def get_nowait(self):
            if not self._items:
                import queue
                raise queue.Empty()
            return self._items.pop(0)
    return _Q(items)


def test_drain_empty_queue_no_crash(app):
    """空队列 _drain 不应崩（无 _status_lbl / _connect_btn 时）。"""
    app.backend = type('B', (), {
        'is_open': False,
        'rx_queue': _make_fake_queue([]),
    })()
    app._status_lbl = None
    app._connect_btn = None
    app._update_status = lambda: None
    app._append_log = lambda text, tag='log': None
    app._drain()  # 不应抛


def test_drain_processes_line_kind(app):
    """line 类型应通过 _route_line + _append_batch。"""
    appended = []
    app.backend = type('B', (), {
        'is_open': True,
        'rx_queue': _make_fake_queue([('line', 'OK,0%')]),
    })()
    app._status_lbl = type('L', (), {'setText': lambda self_, t: None})()
    app._connect_btn = None
    app._update_status = lambda: None
    app._append_batch = lambda batch: appended.extend(batch)
    app._append_log = lambda text, tag='log': None
    app._drain()
    assert appended  # 至少一条
    assert appended[0][1] == 'resp'


def test_drain_status_update_sets_dot_state(app):
    """status 类型应更新 _dot_state。"""
    app.backend = type('B', (), {
        'is_open': True,
        'rx_queue': _make_fake_queue([('status', ('info', '已连接'))]),
    })()
    app._status_lbl = type('L', (), {'setText': lambda self_, t: None})()
    app._connect_btn = None
    app._update_status = lambda: None
    app._append_batch = lambda batch: None
    app._append_log = lambda text, tag='log': None
    app._drain()
    assert app._dot_state == 'ok'
    assert app._connected_label == '已连接'


def test_drain_error_kind_sets_err_state(app):
    app.backend = type('B', (), {
        'is_open': True,
        'rx_queue': _make_fake_queue([('error', 'IO error')]),
    })()
    app._status_lbl = type('L', (), {'setText': lambda self_, t: None})()
    app._connect_btn = None
    app._update_status = lambda: None
    app._append_batch = lambda batch: None
    app._append_log = lambda text, tag='log': None
    app._drain()
    assert app._dot_state == 'err'


def test_drain_closed_kind_sets_idle_state(app):
    """closed 类型应把 _connect_btn 文本设为'连接'，状态置 idle。"""
    app.backend = type('B', (), {
        'is_open': False,
        'rx_queue': _make_fake_queue([('closed', None)]),
    })()
    app._status_lbl = type('L', (), {'setText': lambda self_, t: None})()
    class FakeBtn:
        def __init__(self):
            self.text = ''
            self.icon = None
        def setText(self, t):
            self.text = t
        def setIcon(self, i):
            self.icon = i
    btn = FakeBtn()
    app._connect_btn = btn
    app._connect_icon = None
    app._update_status = lambda: None
    app._append_batch = lambda batch: None
    app._append_log = lambda text, tag='log': None
    app._drain()
    assert btn.text == '连接'
    assert app._dot_state == 'idle'


def test_drain_response_timeout_appends_note(app):
    """pending_cmd 超过 RESP_TIMEOUT 应追加'无响应'提示并清空。"""
    import time as _time
    app.pending_cmd = 'AT+ELOG=0%'
    app.pending_ts = _time.time() - 5  # 远超 RESP_TIMEOUT=2s
    app.backend = type('B', (), {
        'is_open': True,
        'rx_queue': _make_fake_queue([]),
    })()
    app._status_lbl = type('L', (), {'setText': lambda self_, t: None})()
    app._connect_btn = None
    app._update_status = lambda: None
    app._append_batch = lambda batch: None
    notes = []
    app._append_log = lambda text, tag='log': notes.append((text, tag))
    app._drain()
    assert app.pending_cmd is None
    assert notes  # 有 note
    assert notes[0][1] == 'note'


# ---- _append_log ----
def test_append_log_with_log_edit(app, qtbot):
    """_append_log 应把文本写入 QPlainTextEdit。"""
    from PySide6.QtWidgets import QPlainTextEdit
    edit = QPlainTextEdit()
    app._log_edit = edit
    app._append_log('hello world\n', tag='log')
    assert 'hello world' in edit.toPlainText()


def test_append_log_multiple_tags(app, qtbot):
    """不同 tag 应都能写入文档（颜色由 QTextCharFormat 决定）。"""
    from PySide6.QtWidgets import QPlainTextEdit
    edit = QPlainTextEdit()
    app._log_edit = edit
    for tag in ('send', 'resp', 'log', 'note', 'error'):
        app._append_log(f'line-{tag}\n', tag=tag)
    text = edit.toPlainText()
    for tag in ('send', 'resp', 'log', 'note', 'error'):
        assert f'line-{tag}' in text


def test_append_log_with_writer(app, qtbot):
    """log_writer 不为 None 时应 put 文本。"""
    written = []
    class FakeWriter:
        def put(self, text):
            written.append(text)
    app.log_writer = FakeWriter()
    app._log_edit = None  # 不写 widget
    app._append_log('test line\n')
    assert written == ['test line\n']


def test_append_log_writer_failure_swallowed(app, qtbot):
    """log_writer.put 抛异常应被吞掉。"""
    class BadWriter:
        def put(self, text):
            raise RuntimeError('writer dead')
    app.log_writer = BadWriter()
    app._log_edit = None
    app._append_log('still ok\n')  # 不应抛


# ---- _append_batch ----
def test_append_batch_iterates_all(app):
    called = []
    app._append_log = lambda text, tag='log': called.append((text, tag))
    app._append_batch([('a\n', 'log'), ('b\n', 'resp')])
    assert called == [('a\n', 'log'), ('b\n', 'resp')]


# ---- _update_status ----
def test_update_status_no_dot_is_noop(app):
    app._status_dot = None
    app._update_status()  # 不应崩


def test_update_status_sets_dot_state(app, qtbot):
    """_status_dot.set_state 应被调用。"""
    cls = mod._make_status_dot_class()
    dot = cls()
    app._status_dot = dot
    app._dot_state = 'ok'
    app._theme_name_resolved = 'dark'
    app._update_status()
    assert dot._state == 'ok'


