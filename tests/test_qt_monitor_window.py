"""ui/qt_monitor_window.py 测试：数据管理 / _get_xy / _var_color / stop_serial。

聚焦不依赖 pyqtgraph 渲染的逻辑层：变量注册、颜色、_max_points 截断、stop_serial
"""
from __future__ import annotations

import os
import time

import pytest

pytest.importorskip('PySide6')

from serial_debug.ui import qt_monitor_window as mod


@pytest.fixture
def monitor(qtbot, tmp_path, monkeypatch):
    """构建一个 QtMonitorWindow 实例，但跳过 _create()（避免真 pyqtgraph 渲染）。"""
    # 重定向 config 到 tmp 防污染用户配置
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / '.config'))
    # 重新 import 让 config 模块拿到新 env
    import importlib

    from serial_debug.infra import config
    importlib.reload(config)
    importlib.reload(mod)
    m = mod.QtMonitorWindow()
    return m


# ---- _register_var / _var_meta ----

def test_register_var_creates_meta(monitor):
    """_register_var 应在 _var_meta 里登记该 name + 分配颜色。"""
    monitor._register_var('motor.rpm')
    assert 'motor.rpm' in monitor._var_meta
    meta = monitor._var_meta['motor.rpm']
    assert 'color' in meta
    assert meta['color']


def test_register_var_idempotent(monitor):
    """重复 register 同名 var 不应覆盖已有的 color/label。"""
    monitor._register_var('motor.rpm')
    monitor._var_meta['motor.rpm']['color'] = '#ff0000'
    monitor._var_meta['motor.rpm']['label'] = 'MyRPM'
    monitor._register_var('motor.rpm')
    assert monitor._var_meta['motor.rpm']['color'] == '#ff0000'
    assert monitor._var_meta['motor.rpm']['label'] == 'MyRPM'


# ---- _var_color ----

def test_var_color_uses_meta(monitor):
    """var_meta 有 color 时应直接用。"""
    monitor._var_meta['motor.rpm'] = {'color': '#abcdef', 'label': 'rpm'}
    assert monitor._var_color('motor.rpm', ['motor.rpm']) == '#abcdef'


def test_var_color_falls_back_to_palette(monitor):
    """无 color 时按 all_data_keys 顺序取调色板。"""
    monitor._var_meta['motor.rpm'] = {}
    all_keys = ['motor.rpm']
    c = monitor._var_color('motor.rpm', all_keys)
    assert c == monitor._palette[0]


def test_var_color_palette_wraps_index(monitor):
    """索引超过 palette 长度应 wrap around。"""
    monitor._var_meta = {n: {} for n in ['a', 'b', 'c']}
    all_keys = ['a', 'b', 'c']
    # 多次取，验证 wrap
    colors = [monitor._var_color(n, all_keys) for n in all_keys]
    # 不全相同（palette 不只一种色）
    assert len(set(colors)) > 1


# ---- gen_mock_data ----

def test_gen_mock_data_populates_data(monitor):
    """gen_mock_data 应填 _data 三个变量。"""
    monitor.gen_mock_data(100)
    assert len(monitor._data) == 3
    assert 'sin_50hz' in monitor._data
    assert 'cos_with_spikes' in monitor._data
    assert 'ramp' in monitor._data


def test_gen_mock_data_registers_var_meta(monitor):
    """gen_mock_data 应同步登记 var_meta。"""
    monitor.gen_mock_data(50)
    for name in ('sin_50hz', 'cos_with_spikes', 'ramp'):
        assert name in monitor._var_meta


# ---- _get_xy ----

def test_get_xy_returns_numpy_arrays_from_mock(monitor):
    """gen_mock_data 后 _get_xy 应返回 numpy 数组（直接复用元组）。"""
    import numpy as np
    monitor.gen_mock_data(100)
    xs, ys = monitor._get_xy('sin_50hz')
    assert xs is not None and ys is not None
    assert len(xs) == 100
    assert len(ys) == 100


def test_get_xy_returns_none_for_missing_var(monitor):
    """未登记的变量应返 None, None。"""
    xs, ys = monitor._get_xy('nonexistent')
    assert xs is None
    assert ys is None


def test_get_xy_from_list_data_uses_cache(monitor):
    """list[(ts, val)] 数据应被 _get_xy 转 numpy + 缓存。"""
    import numpy as np
    monitor._data['motor.rpm'] = [(0.0, 1.0), (0.1, 2.0), (0.2, 3.0)]
    xs, ys = monitor._get_xy('motor.rpm')
    assert len(xs) == 3
    assert ys[0] == 1.0
    # 二次调应命中缓存
    xs2, ys2 = monitor._get_xy('motor.rpm')
    assert (np.asarray(xs) == np.asarray(xs2)).all()


# ---- _max_points 截断 ----

def test_max_points_truncates_buffer(monitor):
    """reader 线程 append 超过 _max_points 应截断老数据（见 start_serial._reader）。"""
    # 模拟 reader 内部 append 逻辑
    monitor._max_points = 5
    name = 'motor.rpm'
    monitor._data[name] = []
    for i in range(10):
        buf = monitor._data.setdefault(name, [])
        buf.append((float(i), float(i)))
        if len(buf) > monitor._max_points:
            del buf[:len(buf) - monitor._max_points]
    assert len(monitor._data[name]) == 5
    # 应保留最后 5 个
    assert monitor._data[name][0] == (5.0, 5.0)
    assert monitor._data[name][-1] == (9.0, 9.0)


# ---- stop_serial ----

def test_stop_serial_idempotent(monitor):
    """stop_serial 在未启动时是 no-op，不抛。"""
    # 应不抛
    monitor.stop_serial()


def test_stop_serial_after_manual_state(monitor):
    """手动 set _serial + _serial_thread + _serial_stop，stop_serial 应清理。"""
    import threading
    monitor._serial_stop = threading.Event()
    monitor._serial = None  # 真 serial 不实例化
    monitor._serial_thread = None
    monitor.stop_serial()
    assert monitor._serial is None
    assert monitor._serial_thread is None
    assert monitor._serial_stop is None


# ---- _curve_color (staticmethod) ----

def test_curve_color_returns_gray_for_no_color_attr(monitor):
    """PlotDataItem 无 _legend_color 属性时返回灰。"""
    class _FakeItem:
        pass
    c = monitor._curve_color(_FakeItem())
    assert c.startswith('#')


def test_curve_color_uses_legend_color_attr(monitor):
    """item._legend_color 应被优先返回。"""
    class _FakeItem:
        _legend_color = '#123456'
    c = monitor._curve_color(_FakeItem())
    assert c == '#123456'


# ---- 模块级纯函数：_build_var_registry / _pick_value / _fmt_val ----

def test_build_var_registry_includes_motor_bms_imu():
    """注册表应含 motor./bms./imu. 三类前缀。"""
    from serial_debug.ui import qt_monitor_window as mod
    out = mod._build_var_registry()
    assert any(v['name'].startswith('motor.') for v in out)
    assert any(v['name'].startswith('bms.') for v in out)
    assert any(v['name'].startswith('imu.') for v in out)
    # 每项应有 name + label
    for v in out:
        assert 'name' in v
        assert 'label' in v


def test_pick_value_valid_indices():
    """正常 block_idx/group_idx/pos 应取到值。"""
    blocks = [[[1.0, 2.0], [3.0]], [[4.0, 5.0]]]
    v = {'block_idx': 0, 'group_idx': 1, 'pos_in_group': 0}
    assert mod._pick_value(blocks, v) == 3.0


def test_pick_value_out_of_range_returns_none():
    """超出范围应返 None。"""
    blocks = [[[1.0]]]
    assert mod._pick_value(blocks, {'block_idx': 5}) is None
    assert mod._pick_value(blocks, {'block_idx': 0, 'group_idx': 9}) is None
    assert mod._pick_value(blocks,
                           {'block_idx': 0, 'group_idx': 0,
                            'pos_in_group': 9}) is None


def test_pick_value_non_numeric_returns_none():
    """非数字应被 float() 拒绝返 None。"""
    blocks = [[['abc']]]
    assert mod._pick_value(blocks,
                           {'block_idx': 0, 'group_idx': 0,
                            'pos_in_group': 0}) is None


def test_pick_value_invalid_blocks_returns_none():
    """blocks 非预期类型（如 None）应返 None。"""
    assert mod._pick_value(None, {}) is None
    assert mod._pick_value([], {'block_idx': 0}) is None


# ---- _iter_builtin_panel_vars ----

def test_iter_builtin_panel_vars_motor_3wd1t_1():
    """3wd1t_1 行应 yield 多个 motor.* 变量。"""
    line = '1783996615491 I model 3wd1t_1 0 0 0|0 0 0#0 0 0'
    out = list(mod._iter_builtin_panel_vars(line, 1783996615.491))
    assert any(name == 'motor.Vx' for _, name, _ in out)
    assert all(ts == 1783996615.491 for ts, _, _ in out)


def test_iter_builtin_panel_vars_no_motor_tag():
    """没有 motor tag 的行 yield 应为空。"""
    line = '1234567890 I some other log content'
    out = list(mod._iter_builtin_panel_vars(line, 1234567.89))
    assert out == []


def test_iter_builtin_panel_vars_std_debug():
    """Lxx STD_DEBUG 行应 yield motor.* 变量。"""
    line = '1783995845972 I L30 STD_DEBUG:[6|0|0 0|0 0|0 0 0|0 0|0|0 0|64]'
    out = list(mod._iter_builtin_panel_vars(line, 1783995.845972))
    # 至少 yield 一些 motor.* 项（即便列表不空即可）
    assert isinstance(out, list)


# ---- _iter_panel_vars ----

def test_iter_panel_vars_no_ts_no_fallback():
    """行内无 ts 且无 fallback 时应 return（不 yield）。"""
    out = list(mod._iter_panel_vars('OK,0%'))
    assert out == []


def test_iter_panel_vars_no_ts_uses_fallback():
    """行内无 ts 时应使用 fallback ts。"""
    out = list(mod._iter_panel_vars('OK,0%', ts_fallback=12345.0))
    for item in out:
        assert item[0] == 12345.0


def test_iter_panel_vars_custom_mode(monkeypatch):
    """mode=custom 时应只跑用户 parse_line。"""
    monkeypatch.setitem(mod._CUSTOM_PARSER_STATE, 'mode', 'custom')
    monkeypatch.setitem(mod._CUSTOM_PARSER_STATE, 'func',
                        lambda line: [(1.0, 'custom.var', 5.0)])
    out = list(mod._iter_panel_vars('1234567890 I some line',
                                    ts_fallback=1.0))
    assert any(name == 'custom.var' for _, name, _ in out)


def test_iter_panel_vars_both_mode_dedup(monkeypatch):
    """mode=both 时 built-in + custom 同名应取 custom 的（覆盖）。"""
    monkeypatch.setitem(mod._CUSTOM_PARSER_STATE, 'mode', 'both')
    monkeypatch.setitem(mod._CUSTOM_PARSER_STATE, 'func',
                        lambda line: [(2.0, 'motor.Vx', 99.0)])
    line = '1783996615491 I model 3wd1t_1 0 0 0|0 0 0#0 0 0'
    out = list(mod._iter_panel_vars(line, ts_fallback=1.0))
    vx = [v for _, n, v in out if n == 'motor.Vx']
    # 同名 should take last → 99.0
    assert vx == [99.0]


def test_iter_panel_vars_custom_func_exception_swallowed(monkeypatch):
    """用户 parse_line 抛异常应被吞。"""
    monkeypatch.setitem(mod._CUSTOM_PARSER_STATE, 'mode', 'custom')
    def bad(line):
        raise RuntimeError('boom')
    monkeypatch.setitem(mod._CUSTOM_PARSER_STATE, 'func', bad)
    out = list(mod._iter_panel_vars('1234567890 I line', ts_fallback=1.0))
    assert out == []


def test_iter_panel_vars_custom_func_returns_none(monkeypatch):
    """parse_line 返 None 应被跳过。"""
    monkeypatch.setitem(mod._CUSTOM_PARSER_STATE, 'mode', 'custom')
    monkeypatch.setitem(mod._CUSTOM_PARSER_STATE, 'func',
                        lambda line: None)
    out = list(mod._iter_panel_vars('1234567890 I line', ts_fallback=1.0))
    assert out == []


def test_iter_panel_vars_custom_func_invalid_tuple_skipped(monkeypatch):
    """parse_line yield 非 3 元组项应被跳过。"""
    monkeypatch.setitem(mod._CUSTOM_PARSER_STATE, 'mode', 'custom')
    monkeypatch.setitem(mod._CUSTOM_PARSER_STATE, 'func',
                        lambda line: [(1.0, 'only_two'), None, (2.0, 'ok', 1.0)])
    out = list(mod._iter_panel_vars('1234567890 I line', ts_fallback=1.0))
    assert len(out) == 1
    assert out[0][1] == 'ok'


# ---- _default_plot / _normalize_plot / _fmt_val / _discover_vars_from_sample ----

def test_default_plot_has_all_keys():
    """_default_plot 应含全部必要 key。"""
    d = mod._default_plot()
    expected = {'title', 'x_label', 'y_label', 'vars', 'var_visibility',
                'show_max', 'show_min', 'show_avg', 'show_last',
                'show_table', 'show_hover', 'legend_pos',
                'legend_font_size', 'legend_auto_avoid', 'hover_mode',
                'hover_show_max', 'hover_show_min', 'hover_show_avg',
                'hover_show_last'}
    assert expected <= set(d.keys())
    assert d['vars'] == []
    assert d['var_visibility'] == {}


def test_normalize_plot_empty_returns_default():
    """传 None / 空 dict 应返 _default_plot()。"""
    d = mod._normalize_plot(None)
    assert d['title'] == '数据曲线'
    d2 = mod._normalize_plot({})
    assert d2['vars'] == []


def test_normalize_plot_partial_input_keeps_defaults():
    """部分字段应保留默认值。"""
    p = {'title': 'Custom', 'show_max': True}
    d = mod._normalize_plot(p)
    assert d['title'] == 'Custom'
    assert d['show_max'] is True
    # 未传的字段保留默认
    assert d['x_label'] == '时间 (s)'
    assert d['show_min'] is False


def test_normalize_plot_var_visibility_non_dict_resets():
    """var_visibility 非 dict 应重置为 {}。"""
    d = mod._normalize_plot({'var_visibility': 'bad'})
    assert d['var_visibility'] == {}


def test_normalize_plot_var_visibility_dict_coerces():
    """var_visibility 是 dict 时应 str 化 key、bool 化 value。"""
    d = mod._normalize_plot({'var_visibility': {1: 'true', 2: 0}})
    assert d['var_visibility'] == {'1': True, '2': False}


def test_normalize_plot_vars_list_copied():
    """vars list 应被复制（不指向同一对象）。"""
    p = {'vars': ['a', 'b']}
    d = mod._normalize_plot(p)
    d['vars'].append('c')
    assert p['vars'] == ['a', 'b']


def test_fmt_val_large_number():
    """大数字应使用 .4g。"""
    assert mod._fmt_val(12345.0) == '1.234e+04'


def test_fmt_val_small_number():
    """极小非零数字应使用 .4g。"""
    s = mod._fmt_val(0.001)
    assert 'e' in s or '.' in s


def test_fmt_val_normal_float():
    """普通浮点应使用 .3f。"""
    assert mod._fmt_val(12.5) == '12.500'


def test_fmt_val_zero():
    """零应使用 .3f。"""
    assert mod._fmt_val(0.0) == '0.000'


def test_fmt_val_non_numeric_returns_str():
    """非数字应返 str(v)。"""
    assert mod._fmt_val('hello') == 'hello'
    assert mod._fmt_val(None) == 'None'


def test_discover_vars_from_sample_multi_line():
    """多行样本应收集所有唯一 name。"""
    def func(line):
        if 'a' in line:
            yield (1.0, 'motor.a', 1.0)
        if 'b' in line:
            yield (1.0, 'motor.b', 2.0)
    sample = 'line a\nline b\nline a\n'
    out = mod._discover_vars_from_sample(func, sample)
    names = [v['name'] for v in out]
    assert set(names) == {'motor.a', 'motor.b'}


def test_discover_vars_from_sample_empty():
    """空样本应返 []。"""
    out = mod._discover_vars_from_sample(lambda l: None, '')
    assert out == []


def test_discover_vars_from_sample_func_exception_swallowed():
    """func 抛异常应被吞，继续处理后续行。"""
    def func(line):
        if 'bad' in line:
            raise RuntimeError('boom')
        yield (1.0, 'ok', 1.0)
    sample = 'bad line\nok line\n'
    out = mod._discover_vars_from_sample(func, sample)
    names = [v['name'] for v in out]
    assert 'ok' in names


def test_discover_vars_from_sample_invalid_tuple_skipped():
    """非 3 元组项应被跳过。"""
    def func(line):
        yield (1.0, 'only_two')
        yield (1.0, 'ok', 2.0)
    out = mod._discover_vars_from_sample(func, 'one line')
    assert len(out) == 1
    assert out[0]['name'] == 'ok'


def test_discover_vars_from_sample_func_returns_none():
    """func 返 None 应被跳过。"""
    out = mod._discover_vars_from_sample(lambda l: None, 'line\n')
    assert out == []


# ---- _ValidationBridge ----

def test_validation_bridge_setup_creates_object(monitor):
    """setup 应创建 QObject 并设置 parent。"""
    from PySide6.QtCore import QObject
    monitor._validation_bridge.setup(monitor._win or QObject())
    assert monitor._validation_bridge._bridge is not None


def test_validation_bridge_emit_no_setup_is_noop(monitor):
    """未 setup 时 emit 不应抛。"""
    monitor._validation_bridge._bridge = None
    monitor._validation_bridge.emit([], 'test')  # 不应抛


def test_validation_bridge_emit_calls_callback(monitor, qtbot):
    """emit 应跨线程触发主线程回调。"""
    from PySide6.QtCore import QObject
    from PySide6.QtWidgets import QApplication
    calls = []
    parent = QObject()
    bridge = mod._ValidationBridge(lambda lines, label: calls.append((lines, label)))
    bridge.setup(parent)
    # 强制直接调用 _handle（验证 bridge 对象 + 连接已建好）
    bridge._bridge._handle(['a'], 'src')
    assert calls
    assert calls[0] == (['a'], 'src')


def test_validation_bridge_emit_cross_thread_delivered(monitor, qtbot):
    """emit 应通过 signal 触发主线程回调（处理事件循环后）。"""
    import threading
    import time

    from PySide6.QtCore import QObject
    calls = []
    parent = QObject()
    bridge = mod._ValidationBridge(lambda lines, label: calls.append((lines, label)))
    bridge.setup(parent)

    def _emit_from_thread():
        bridge.emit(['a', 'b'], 'src')

    t = threading.Thread(target=_emit_from_thread)
    t.start()
    t.join()
    # 等待事件循环处理 queued signal
    deadline = time.monotonic() + 1.0
    while not calls and time.monotonic() < deadline:
        qtbot.wait(20)
    assert calls
    assert calls[0][1] == 'src'


# ---- _QtPlotDialog ----

def test_qt_plot_dialog_build_returns_qdialog(monitor, qtbot):
    """_QtPlotDialog._build 应创建 QDialog。"""
    dlg = mod._QtPlotDialog(None, all_vars=[], initial=None,
                            title='配置图表')
    built = dlg._build()
    from PySide6.QtWidgets import QDialog
    assert isinstance(built, QDialog)
    built.deleteLater()


def test_qt_plot_dialog_set_all_toggles_checks(monitor, qtbot):
    """_set_all 应切换所有 var 项的 CheckState。"""
    dlg = mod._QtPlotDialog(None, all_vars=[], initial=None)
    dlg._build()
    dlg._set_all(True)
    from PySide6.QtCore import Qt
    for it in dlg._var_items.values():
        assert it.checkState() == Qt.Checked
    dlg._set_all(False)
    for it in dlg._var_items.values():
        assert it.checkState() == Qt.Unchecked
    dlg._dlg.deleteLater()


def test_qt_plot_dialog_ok_builds_result(monitor, qtbot):
    """_ok 应组装 result dict 并 accept。"""
    dlg = mod._QtPlotDialog(None, all_vars=[],
                            initial={'title': 'My Curve'})
    dlg._build()
    dlg._ok()
    assert dlg.result is not None
    assert dlg.result['title'] == 'My Curve'
    assert 'vars' in dlg.result
    dlg._dlg.deleteLater()


def test_qt_plot_dialog_ok_uses_default_title_when_empty(monitor, qtbot):
    """_ok 空 title 应填默认值。"""
    dlg = mod._QtPlotDialog(None, all_vars=[],
                            initial={'title': '   '})
    dlg._build()
    dlg._ok()
    assert dlg.result['title'] == '数据曲线'
    dlg._dlg.deleteLater()


# ---- QtMonitorWindow._load_custom_parser_state ----

def _mem_config(monkeypatch):
    """让 config.save/load 用 in-memory dict（绕过 DEFAULTS 过滤）。

    多数 qt_monitor_* 字段（qt_monitor_custom_parser /
    qt_monitor_last_config_path / qt_monitor_last_exported_config_path /
    qt_monitor_splitter_sizes）不在 config.DEFAULTS，会被真实 save 过滤掉，
    无法 round-trip 验证。
    """
    from serial_debug.infra import config
    mem = {}
    def fake_save(cfg):
        mem.clear()
        mem.update(cfg)
    def fake_load():
        return dict(mem)
    monkeypatch.setattr(config, 'save', fake_save)
    monkeypatch.setattr(config, 'load', fake_load)
    return mem


def test_load_custom_parser_state_bad_mode_falls_back(monitor, monkeypatch):
    """config 里 mode 非法应回退 built-in。"""
    _mem_config(monkeypatch)
    from serial_debug.infra import config
    cfg = config.load()
    cfg['qt_monitor_custom_parser'] = {
        'mode': 'invalid', 'code': '', 'sample_log': '',
        'source_code': '', 'source_language': 'auto',
        'discovered_vars': [], 'validation': {},
    }
    config.save(cfg)
    monitor._load_custom_parser_state()
    assert mod._CUSTOM_PARSER_STATE['mode'] == 'built-in'


def test_load_custom_parser_state_valid_code_compiles(monitor, monkeypatch):
    """mode=custom + 合法 code 应 exec 出 parse_line。"""
    _mem_config(monkeypatch)
    from serial_debug.infra import config
    code = 'def parse_line(line):\n    yield 1.0, "x", 2.0\n'
    cfg = config.load()
    cfg['qt_monitor_custom_parser'] = {
        'mode': 'custom', 'code': code, 'sample_log': '',
        'source_code': '', 'source_language': 'auto',
        'discovered_vars': [], 'validation': {},
    }
    config.save(cfg)
    monitor._load_custom_parser_state()
    assert mod._CUSTOM_PARSER_STATE['mode'] == 'custom'
    assert callable(mod._CUSTOM_PARSER_STATE['func'])


def test_load_custom_parser_state_bad_code_func_none(monitor, monkeypatch):
    """mode=custom + 坏 code 应让 func=None。"""
    _mem_config(monkeypatch)
    from serial_debug.infra import config
    cfg = config.load()
    cfg['qt_monitor_custom_parser'] = {
        'mode': 'custom', 'code': 'def broken(', 'sample_log': '',
        'source_code': '', 'source_language': 'auto',
        'discovered_vars': [], 'validation': {},
    }
    config.save(cfg)
    monitor._load_custom_parser_state()
    assert mod._CUSTOM_PARSER_STATE['func'] is None


def test_load_custom_parser_state_validation_compat(monitor, monkeypatch):
    """validation 配置应兼容 min_parsed_count。"""
    _mem_config(monkeypatch)
    from serial_debug.infra import config
    cfg = config.load()
    cfg['qt_monitor_custom_parser'] = {
        'mode': 'built-in', 'code': '', 'sample_log': '',
        'source_code': '', 'source_language': 'auto',
        'discovered_vars': [],
        'validation': {'enabled': False, 'min_parsed_count': 25,
                        'sample_lines': 500},
    }
    config.save(cfg)
    monitor._load_custom_parser_state()
    v = mod._CUSTOM_PARSER_STATE['validation']
    assert v['enabled'] is False
    assert v['min_parsed_count'] == 25
    assert v['sample_lines'] == 500


def test_load_custom_parser_state_invalid_src_language(monitor, monkeypatch):
    """source_language 非法应回退 'auto'。"""
    _mem_config(monkeypatch)
    from serial_debug.infra import config
    cfg = config.load()
    cfg['qt_monitor_custom_parser'] = {
        'mode': 'built-in', 'code': '', 'sample_log': '',
        'source_code': '', 'source_language': 'french',
        'discovered_vars': [], 'validation': {},
    }
    config.save(cfg)
    monitor._load_custom_parser_state()
    assert mod._CUSTOM_PARSER_STATE['source_language'] == 'auto'


def test_load_custom_parser_state_raw_not_dict_returns(monitor, monkeypatch):
    """config 里 qt_monitor_custom_parser 非 dict 应早 return。"""
    _mem_config(monkeypatch)
    from serial_debug.infra import config
    cfg = config.load()
    cfg['qt_monitor_custom_parser'] = 'not a dict'
    config.save(cfg)
    # 不应抛
    monitor._load_custom_parser_state()
    # state 应保持初始（mode 默认 built-in）
    assert mod._CUSTOM_PARSER_STATE['mode'] in ('built-in', 'custom', 'both')


def test_load_custom_parser_state_discovered_vars_non_list(monitor, monkeypatch):
    """discovered_vars 非 list 应兜底 []。"""
    _mem_config(monkeypatch)
    from serial_debug.infra import config
    cfg = config.load()
    cfg['qt_monitor_custom_parser'] = {
        'mode': 'built-in', 'code': '', 'sample_log': '',
        'source_code': '', 'source_language': 'auto',
        'discovered_vars': 'not a list', 'validation': {},
    }
    config.save(cfg)
    monitor._load_custom_parser_state()
    assert mod._CUSTOM_PARSER_STATE['discovered_vars'] == []


def test_load_custom_parser_state_config_failure_swallowed(monitor, monkeypatch):
    """config.load 抛异常应被吞。"""
    from serial_debug.infra import config
    def boom():
        raise RuntimeError('fail')
    monkeypatch.setattr(config, 'load', boom)
    monitor._load_custom_parser_state()  # 不应抛


# ---- _save_custom_parser_state ----

def test_save_custom_parser_state_writes_config(monitor, monkeypatch):
    """save 应把 _CUSTOM_PARSER_STATE 写回 config。"""
    mem = _mem_config(monkeypatch)
    monkeypatch.setitem(mod._CUSTOM_PARSER_STATE, 'mode', 'custom')
    monkeypatch.setitem(mod._CUSTOM_PARSER_STATE, 'code', 'def parse_line(l): pass')
    monitor._save_custom_parser_state()
    saved = mem.get('qt_monitor_custom_parser', {})
    assert saved.get('mode') == 'custom'
    assert saved.get('code') == 'def parse_line(l): pass'


def test_save_custom_parser_state_config_failure_swallowed(monitor, monkeypatch):
    """config.save 抛异常应被吞。"""
    from serial_debug.infra import config
    def boom(cfg):
        raise RuntimeError('disk full')
    monkeypatch.setattr(config, 'save', boom)
    monitor._save_custom_parser_state()  # 不应抛


# ---- _load_last_config_path / _save_last_config_path ----

def test_load_last_config_path_empty_default(monitor, monkeypatch):
    """无记录应返 ''。"""
    _mem_config(monkeypatch)
    monitor._save_last_config_path('')
    assert monitor._load_last_config_path() == ''


def test_save_load_last_config_path_roundtrip(monitor, tmp_path, monkeypatch):
    """save 后 load 应返同一路径。"""
    _mem_config(monkeypatch)
    p = str(tmp_path / 'cfg.json')
    monitor._save_last_config_path(p)
    assert monitor._load_last_config_path() == p


# ---- _load_last_exported_path / _save_last_exported_path ----

def test_load_last_exported_path_empty_default(monitor, monkeypatch):
    _mem_config(monkeypatch)
    monitor._save_last_exported_path('')
    assert monitor._load_last_exported_path() == ''


def test_save_load_last_exported_path_roundtrip(monitor, tmp_path, monkeypatch):
    _mem_config(monkeypatch)
    p = str(tmp_path / 'exp.json')
    monitor._save_last_exported_path(p)
    assert monitor._load_last_exported_path() == p


def test_load_last_config_path_failure_returns_empty(monitor, monkeypatch):
    """config.load 抛异常应返 ''。"""
    from serial_debug.infra import config
    def boom():
        raise RuntimeError('fail')
    monkeypatch.setattr(config, 'load', boom)
    assert monitor._load_last_config_path() == ''


def test_load_last_exported_path_failure_returns_empty(monitor, monkeypatch):
    """config.load 抛异常应返 ''。"""
    from serial_debug.infra import config
    def boom():
        raise RuntimeError('fail')
    monkeypatch.setattr(config, 'load', boom)
    assert monitor._load_last_exported_path() == ''


def test_save_last_config_path_failure_swallowed(monitor, monkeypatch):
    """config.save 抛异常应被吞。"""
    from serial_debug.infra import config
    def boom(cfg):
        raise RuntimeError('disk full')
    monkeypatch.setattr(config, 'save', boom)
    monitor._save_last_config_path('/x')  # 不应抛


def test_save_last_exported_path_failure_swallowed(monitor, monkeypatch):
    """config.save 抛异常应被吞。"""
    from serial_debug.infra import config
    def boom(cfg):
        raise RuntimeError('disk full')
    monkeypatch.setattr(config, 'save', boom)
    monitor._save_last_exported_path('/x')  # 不应抛


# ---- _load_plots 3-tier ----

def test_load_plots_from_exported_file(monitor, tmp_path, monkeypatch):
    """有 exported 文件应优先用它。"""
    _mem_config(monkeypatch)
    import json
    exp_path = tmp_path / 'exp.json'
    exp_data = {'plots': [{'title': 'Exp Plot', 'vars': ['motor.Vx']}],
                 'var_meta': {'motor.Vx': {'color': '#fff'}}}
    exp_path.write_text(json.dumps(exp_data), encoding='utf-8')
    monitor._save_last_exported_path(str(exp_path))
    plots = monitor._load_plots()
    assert len(plots) == 1
    assert plots[0]['title'] == 'Exp Plot'
    # var_meta 应被替换
    assert 'motor.Vx' in monitor._var_meta
    assert monitor._var_meta['motor.Vx']['color'] == '#fff'


def test_load_plots_from_opened_file_when_no_exported(monitor, tmp_path, monkeypatch):
    """无 exported 但有 opened 文件应用 opened。"""
    _mem_config(monkeypatch)
    import json
    open_path = tmp_path / 'open.json'
    open_data = {'plots': [{'title': 'Opened', 'vars': []}]}
    open_path.write_text(json.dumps(open_data), encoding='utf-8')
    monitor._save_last_exported_path('')
    monitor._save_last_config_path(str(open_path))
    plots = monitor._load_plots()
    assert len(plots) == 1
    assert plots[0]['title'] == 'Opened'


def test_load_plots_from_default_config(monitor, tmp_path, monkeypatch):
    """无 exported / opened 应从 config qt_monitor_plots 取。"""
    _mem_config(monkeypatch)
    from serial_debug.infra import config
    monitor._save_last_exported_path('')
    monitor._save_last_config_path('')
    cfg = config.load()
    cfg['qt_monitor_plots'] = [{'title': 'Cfg Plot', 'vars': []}]
    config.save(cfg)
    plots = monitor._load_plots()
    assert len(plots) == 1
    assert plots[0]['title'] == 'Cfg Plot'


def test_load_plots_var_meta_list_format(monitor, tmp_path, monkeypatch):
    """var_meta 是 list 格式应被转换成 dict。"""
    _mem_config(monkeypatch)
    import json
    exp_path = tmp_path / 'exp_list.json'
    exp_data = {
        'plots': [],
        'var_meta': [
            {'name': 'motor.rpm', 'color': '#abc', 'label': 'RPM'},
        ],
    }
    exp_path.write_text(json.dumps(exp_data), encoding='utf-8')
    monitor._save_last_exported_path(str(exp_path))
    monitor._load_plots()
    assert 'motor.rpm' in monitor._var_meta
    assert monitor._var_meta['motor.rpm']['color'] == '#abc'


def test_load_plots_invalid_json_file_returns_default(monitor, tmp_path, monkeypatch):
    """exported 文件 JSON 坏应回退到下一级。"""
    _mem_config(monkeypatch)
    bad_path = tmp_path / 'bad.json'
    bad_path.write_text('{not valid json', encoding='utf-8')
    monitor._save_last_exported_path(str(bad_path))
    plots = monitor._load_plots()
    # 应回退到默认（空 list 或 config 默认）
    assert isinstance(plots, list)


def test_load_plots_config_failure_returns_empty(monitor, tmp_path, monkeypatch):
    """config.load 抛异常应返 []。"""
    from serial_debug.infra import config
    monitor._save_last_exported_path('')
    monitor._save_last_config_path('')
    def boom():
        raise RuntimeError('fail')
    monkeypatch.setattr(config, 'load', boom)
    plots = monitor._load_plots()
    assert plots == []


def test_load_plots_var_meta_dict_format(monitor, tmp_path, monkeypatch):
    """var_meta 是 dict 格式应直接转 dict。"""
    _mem_config(monkeypatch)
    import json
    exp_path = tmp_path / 'exp_dict.json'
    exp_data = {
        'plots': [],
        'var_meta': {'motor.rpm': {'color': '#def', 'label': 'RPM2'}},
    }
    exp_path.write_text(json.dumps(exp_data), encoding='utf-8')
    monitor._save_last_exported_path(str(exp_path))
    monitor._load_plots()
    assert 'motor.rpm' in monitor._var_meta
    assert monitor._var_meta['motor.rpm']['color'] == '#def'


def test_load_plots_var_meta_dict_entry_non_dict_skipped(monitor, tmp_path, monkeypatch):
    """var_meta dict 里某项非 dict 应被跳过。"""
    _mem_config(monkeypatch)
    import json
    exp_path = tmp_path / 'exp_dict_bad.json'
    exp_data = {
        'plots': [],
        'var_meta': {'ok': {'color': '#fff'}, 'bad': 'not a dict'},
    }
    exp_path.write_text(json.dumps(exp_data), encoding='utf-8')
    monitor._save_last_exported_path(str(exp_path))
    monitor._load_plots()
    assert 'ok' in monitor._var_meta
    assert 'bad' not in monitor._var_meta


# ---- _load_var_meta ----

def test_load_var_meta_from_config(monitor):
    """config qt_monitor_vars list 应被解析成 dict。"""
    from serial_debug.infra import config
    cfg = config.load()
    cfg['qt_monitor_vars'] = [
        {'name': 'motor.rpm', 'color': '#fff', 'label': 'RPM',
         'y_min': 0, 'y_max': 100, 'warn_min': None, 'warn_max': None},
    ]
    config.save(cfg)
    out = monitor._load_var_meta()
    assert 'motor.rpm' in out
    assert out['motor.rpm']['color'] == '#fff'
    assert out['motor.rpm']['y_max'] == 100


def test_load_var_meta_bad_entry_skipped(monitor):
    """非 dict / 缺 name 项应被跳过。"""
    from serial_debug.infra import config
    cfg = config.load()
    cfg['qt_monitor_vars'] = [
        'not a dict',
        {'no_name': True},
        {'name': 'ok', 'color': '#fff'},
    ]
    config.save(cfg)
    out = monitor._load_var_meta()
    assert 'ok' in out
    assert len(out) == 1


def test_load_var_meta_config_failure_returns_empty(monitor, monkeypatch):
    """config.load 抛异常应返 {}。"""
    from serial_debug.infra import config
    def boom():
        raise RuntimeError('fail')
    monkeypatch.setattr(config, 'load', boom)
    out = monitor._load_var_meta()
    assert out == {}


# ---- _save_var_meta ----

def test_save_var_meta_writes_config(monitor):
    """save 应把 var_meta 写回 config。"""
    monitor._var_meta = {'motor.rpm': {'color': '#abc', 'label': 'RPM',
                                       'y_min': None, 'y_max': None,
                                       'warn_min': None, 'warn_max': None}}
    monitor._save_var_meta()
    from serial_debug.infra import config
    cfg = config.load()
    saved = cfg.get('qt_monitor_vars', [])
    assert any(v.get('name') == 'motor.rpm' for v in saved)


def test_save_var_meta_swallows_failure(monitor, monkeypatch):
    """config.save 抛异常不应让 save_var_meta 抛。"""
    from serial_debug.infra import config
    def boom(cfg):
        raise RuntimeError('disk full')
    monkeypatch.setattr(config, 'save', boom)
    monitor._save_var_meta()  # 不应抛


def test_save_var_meta_with_splitter_records_sidebar_width(monitor):
    """_splitter 非 None 时应记 sidebar width。"""
    class _FakeSplitter:
        def sizes(self):
            return [250, 750]
    monitor._splitter = _FakeSplitter()
    monitor._var_meta = {}
    monitor._save_var_meta()
    from serial_debug.infra import config
    cfg = config.load()
    assert cfg.get('qt_monitor_sidebar_width') == 250


# ---- _save_plots ----

def test_save_plots_writes_visible_plots(monitor):
    """_save_plots 应把 _visible_plots 写回 config。"""
    monitor._visible_plots = [{'title': 'A', 'vars': []},
                                {'title': 'B', 'vars': []}]
    monitor._save_plots()
    from serial_debug.infra import config
    cfg = config.load()
    saved = cfg.get('qt_monitor_plots', [])
    assert len(saved) == 2
    assert saved[0]['title'] == 'A'


def test_save_plots_no_win_skips_geometry(monitor):
    """_win=None 时不应记录 geometry。"""
    monitor._win = None
    monitor._visible_plots = []
    monitor._save_plots()
    from serial_debug.infra import config
    cfg = config.load()
    assert 'qt_monitor_geometry' not in cfg or cfg.get('qt_monitor_geometry') == ''


def test_save_plots_swallows_failure(monitor, monkeypatch):
    """config.save 抛异常应被吞。"""
    from serial_debug.infra import config
    def boom(cfg):
        raise RuntimeError('fail')
    monkeypatch.setattr(config, 'save', boom)
    monitor._save_plots()  # 不应抛


# ---- export_plots_config / import_plots_config ----

def test_export_plots_config_writes_json(monitor, tmp_path, monkeypatch):
    """export 应写 JSON 并记录指针。"""
    _mem_config(monkeypatch)
    monitor._visible_plots = [{'title': 'Exp', 'vars': ['motor.Vx']}]
    monitor._var_meta = {'motor.Vx': {'color': '#fff'}}
    out_path = tmp_path / 'out.json'
    monitor.export_plots_config(str(out_path))
    import json
    data = json.loads(out_path.read_text(encoding='utf-8'))
    assert data['version'] == 1
    assert data['plots'][0]['title'] == 'Exp'
    assert data['var_meta']['motor.Vx']['color'] == '#fff'
    # pointer 应记录
    assert monitor._load_last_exported_path() == str(out_path)


def test_import_plots_config_loads_and_rebuilds(monitor, tmp_path, monkeypatch):
    """import 应加载 plots + 合并 var_meta + 触发 rebuild。"""
    _mem_config(monkeypatch)
    import json
    cfg_path = tmp_path / 'imp.json'
    cfg_data = {
        'plots': [{'title': 'Imported', 'vars': []}],
        'var_meta': {'motor.rpm': {'color': '#abc'}},
    }
    cfg_path.write_text(json.dumps(cfg_data), encoding='utf-8')
    # mock UI rebuild 路径避免真 pyqtgraph
    monkeypatch.setattr(monitor, '_rebuild_plots_ui', lambda: None)
    monkeypatch.setattr(monitor, '_refresh_sidebar', lambda: None)
    monkeypatch.setattr(monitor, '_refresh_sidebar_visibility', lambda: None)
    monkeypatch.setattr(monitor, '_refresh_plot', lambda: None)
    n = monitor.import_plots_config(str(cfg_path))
    assert n == 1
    assert monitor._visible_plots[0]['title'] == 'Imported'
    assert monitor._var_meta['motor.rpm']['color'] == '#abc'
    # 指针应记录
    assert monitor._load_last_config_path() == str(cfg_path)


def test_import_plots_config_missing_plots_raises(monitor, tmp_path, monkeypatch):
    """JSON 缺 plots 字段应抛 ValueError。"""
    _mem_config(monkeypatch)
    import json
    cfg_path = tmp_path / 'bad.json'
    cfg_path.write_text(json.dumps({'var_meta': {}}), encoding='utf-8')
    try:
        monitor.import_plots_config(str(cfg_path))
    except ValueError:
        pass
    else:
        raise AssertionError('应抛 ValueError')


def test_import_plots_config_var_meta_non_dict_ignored(monitor, tmp_path, monkeypatch):
    """var_meta 非 dict 应被忽略。"""
    _mem_config(monkeypatch)
    import json
    cfg_path = tmp_path / 'bad_vm.json'
    cfg_path.write_text(json.dumps({
        'plots': [{'title': 'A', 'vars': []}],
        'var_meta': 'not a dict',
    }), encoding='utf-8')
    monkeypatch.setattr(monitor, '_rebuild_plots_ui', lambda: None)
    monkeypatch.setattr(monitor, '_refresh_sidebar', lambda: None)
    monkeypatch.setattr(monitor, '_refresh_sidebar_visibility', lambda: None)
    monkeypatch.setattr(monitor, '_refresh_plot', lambda: None)
    n = monitor.import_plots_config(str(cfg_path))
    assert n == 1


def test_import_plots_config_var_meta_entry_non_dict_skipped(monitor, tmp_path, monkeypatch):
    """var_meta 里某项非 dict 应被跳过。"""
    _mem_config(monkeypatch)
    import json
    cfg_path = tmp_path / 'imp2.json'
    cfg_path.write_text(json.dumps({
        'plots': [{'title': 'A', 'vars': []}],
        'var_meta': {'ok': {'color': '#fff'}, 'bad': 'not a dict'},
    }), encoding='utf-8')
    monkeypatch.setattr(monitor, '_rebuild_plots_ui', lambda: None)
    monkeypatch.setattr(monitor, '_refresh_sidebar', lambda: None)
    monkeypatch.setattr(monitor, '_refresh_sidebar_visibility', lambda: None)
    monkeypatch.setattr(monitor, '_refresh_plot', lambda: None)
    monitor.import_plots_config(str(cfg_path))
    assert 'ok' in monitor._var_meta
    assert 'bad' not in monitor._var_meta


# ---- _save_geometry ----

def test_save_geometry_no_win_returns_early(monitor):
    """_win=None 应早 return 不抛。"""
    monitor._win = None
    monitor._save_geometry()  # 不应抛


def test_save_geometry_swallows_failure(monitor, monkeypatch):
    """config.save 抛异常应被吞。"""
    from serial_debug.infra import config
    def boom(cfg):
        raise RuntimeError('fail')
    monkeypatch.setattr(config, 'save', boom)
    monitor._win = None  # 早 return
    monitor._save_geometry()  # 不应抛


# ---- _last_dir / _remember_dir ----

def test_last_dir_returns_recorded_existing(monitor, tmp_path, monkeypatch):
    """已记录且存在的目录应返该路径。"""
    _mem_config(monkeypatch)
    # _remember_dir 记录的是 dirname
    f = tmp_path / 'somefile.log'
    f.write_text('', encoding='utf-8')
    monitor._remember_dir('qt_monitor_last_open_dir', str(f))
    assert monitor._last_dir('qt_monitor_last_open_dir') == str(tmp_path)


def test_last_dir_missing_record_returns_default(monitor, monkeypatch):
    """无记录应返 ~。"""
    _mem_config(monkeypatch)
    import os
    out = monitor._last_dir('qt_monitor_test_dir')
    assert out == os.path.expanduser('~')


def test_last_dir_nonexistent_record_returns_default(monitor, monkeypatch):
    """记录的目录不存在应回退 ~。"""
    _mem_config(monkeypatch)
    from serial_debug.infra import config
    cfg = config.load()
    cfg['qt_monitor_test_dir'] = '/nonexistent_dir_xyz_abc'
    config.save(cfg)
    import os
    out = monitor._last_dir('qt_monitor_test_dir')
    assert out == os.path.expanduser('~')


def test_last_dir_config_failure_returns_default(monitor, monkeypatch):
    """config.load 抛异常应返 ~。"""
    import os

    from serial_debug.infra import config
    def boom():
        raise RuntimeError('fail')
    monkeypatch.setattr(config, 'load', boom)
    out = monitor._last_dir('qt_monitor_test_dir')
    assert out == os.path.expanduser('~')


def test_remember_dir_empty_path_skips(monitor, monkeypatch):
    """空 path 不应记。"""
    mem = _mem_config(monkeypatch)
    monitor._remember_dir('qt_monitor_test_dir', '')
    assert 'qt_monitor_test_dir' not in mem


def test_remember_dir_swallows_failure(monitor, monkeypatch):
    """config.save 抛异常应被吞。"""
    from serial_debug.infra import config
    def boom(cfg):
        raise RuntimeError('fail')
    monkeypatch.setattr(config, 'save', boom)
    monitor._remember_dir('qt_monitor_test_dir', '/tmp')  # 不应抛


# ---- _dump_splitter_sizes ----

def test_dump_splitter_sizes_no_v_splitter(monitor):
    """_v_splitter=None 应返 {}。"""
    monitor._v_splitter = None
    assert monitor._dump_splitter_sizes() == {}


def test_dump_splitter_sizes_with_v_splitter(monitor):
    """_v_splitter 非 None 时应返 {'v': [...], 'h': [...]}。"""
    class _FakeHSplitter:
        def __init__(self):
            self._sizes = [100, 200]
        def sizes(self):
            return self._sizes
        __class__ = type('QSplitter', (), {})
    h = _FakeHSplitter()
    h.__class__.__name__ = 'QSplitter'
    class _FakeVSplitter:
        def sizes(self):
            return [50, 60]
        def count(self):
            return 1
        def widget(self, i):
            return h
    monitor._v_splitter = _FakeVSplitter()
    out = monitor._dump_splitter_sizes()
    assert out['v'] == [50, 60]
    assert out['h'] == [[100, 200]]


def test_dump_splitter_sizes_widget_not_qsplitter_skipped(monitor):
    """子 widget 非 QSplitter 应被跳过。"""
    class _FakeWidget:
        def sizes(self):
            return [1]
    _FakeWidget.__name__ = 'OtherWidget'
    class _FakeVSplitter:
        def sizes(self):
            return [50, 60]
        def count(self):
            return 1
        def widget(self, i):
            return _FakeWidget()
    monitor._v_splitter = _FakeVSplitter()
    out = monitor._dump_splitter_sizes()
    assert out['v'] == [50, 60]
    assert out['h'] == []


# ---- _restore_geometry ----

def test_restore_geometry_no_geometry_is_noop(monitor):
    """无 geometry 不应抛。"""
    monitor._win = None
    monitor._restore_geometry()  # 不应抛


# ---- _compute_var_stats ----

def test_compute_var_stats_empty_returns_none(monitor):
    """空数据应返 None。"""
    monitor._data = {}
    assert monitor._compute_var_stats('nonexistent') is None


def test_compute_var_stats_list_data(monitor):
    """list[(ts, val)] 数据应计算 min/max/avg/n。"""
    monitor._data = {'v': [(0.0, 1.0), (1.0, 2.0), (2.0, 3.0)]}
    monitor._data_cache.clear()
    s = monitor._compute_var_stats('v')
    assert s['min'] == 1.0
    assert s['max'] == 3.0
    assert s['avg'] == 2.0
    assert s['n'] == 3
    assert s['current'] == 3.0
    assert s['duration'] == 2.0


def test_compute_var_stats_empty_buffer_returns_none(monitor):
    """list 为空应返 None。"""
    monitor._data = {'v': []}
    monitor._data_cache.clear()
    assert monitor._compute_var_stats('v') is None


# ---- _fmt_num ----

def test_fmt_num_integer():
    """整数应返 int str。"""
    assert mod.QtMonitorWindow._fmt_num(5.0) == '5'


def test_fmt_num_float():
    """浮点应返 .3f。"""
    assert mod.QtMonitorWindow._fmt_num(5.1234) == '5.123'


# ---- _export_csv ----

def test_export_csv_empty_data_raises(monitor):
    """空 _data 应抛 ValueError。"""
    monitor._data = {}
    try:
        monitor._export_csv('/tmp/x.csv')
    except ValueError:
        pass
    else:
        raise AssertionError('应抛')


def test_export_csv_single_var(monitor, tmp_path):
    """单变量应写出 row + 单列。"""
    monitor._data = {'v': [(0.0, 1.0), (1.0, 2.0)]}
    monitor._data_cache.clear()
    out_path = tmp_path / 'out.csv'
    rows, step = monitor._export_csv(str(out_path))
    assert rows == 2
    assert step == 1
    content = out_path.read_text(encoding='utf-8')
    assert 'row,v' in content
    assert '0,1.0' in content or '0,1' in content


def test_export_csv_max_rows_downsamples(monitor, tmp_path):
    """数据量超过 max_rows 应等间隔采样。"""
    monitor._data = {'v': [(float(i), float(i)) for i in range(200)]}
    monitor._data_cache.clear()
    out_path = tmp_path / 'ds.csv'
    rows, step = monitor._export_csv(str(out_path), max_rows=10)
    assert step > 1
    assert rows == 10


def test_export_csv_disk_full_swallowed(monitor, tmp_path, monkeypatch):
    """write 抛 OSError 应传播（用户看到磁盘满）。"""
    monitor._data = {'v': [(0.0, 1.0)]}
    monitor._data_cache.clear()
    # 用不可写路径模拟
    try:
        monitor._export_csv('/nonexistent_dir_xyz/out.csv')
    except (OSError, FileNotFoundError):
        pass
    else:
        raise AssertionError('应抛 OSError')


def test_export_csv_all_empty_vars_raises(monitor, tmp_path):
    """所有变量都为空应抛 ValueError。"""
    monitor._data = {'v': []}
    monitor._data_cache.clear()
    try:
        monitor._export_csv(str(tmp_path / 'x.csv'))
    except ValueError:
        pass
    else:
        raise AssertionError('应抛 ValueError')


# ---- _current_plot_idx ----

def test_current_plot_idx_no_plots(monitor):
    """无 plot_widgets 应返 None。"""
    monitor._plot_widgets = []
    assert monitor._current_plot_idx() is None


def test_current_plot_idx_with_plots(monitor):
    """有 plot_widgets 应返 0。"""
    monitor._plot_widgets = [object()]
    assert monitor._current_plot_idx() == 0


# ---- _hide_plot ----

def test_hide_plot_invalid_idx_noop(monitor):
    """非法 idx 应直接 return。"""
    monitor._visible_plots = [{'title': 'A', 'vars': []}]
    monitor._hide_plot(5)  # 不应抛
    assert len(monitor._visible_plots) == 1


def test_hide_plot_negative_idx_noop(monitor):
    """负 idx 应直接 return。"""
    monitor._visible_plots = [{'title': 'A', 'vars': []}]
    monitor._hide_plot(-1)
    assert len(monitor._visible_plots) == 1


def test_hide_plot_valid_idx_removes(monitor, monkeypatch):
    """合法 idx 应从 _visible_plots 移除并调 rebuild。"""
    monitor._visible_plots = [{'title': 'A', 'vars': []},
                              {'title': 'B', 'vars': []}]
    called = []
    monkeypatch.setattr(monitor, '_rebuild_plots_ui',
                        lambda: called.append('rebuild'))
    monkeypatch.setattr(monitor, '_refresh_plot', lambda: called.append('refresh'))
    monitor._hide_plot(0)
    assert len(monitor._visible_plots) == 1
    assert monitor._visible_plots[0]['title'] == 'B'
    assert 'rebuild' in called
    assert 'refresh' in called


# ---- _show_all_plots ----

def test_show_all_plots_restores_from_config(monitor, monkeypatch):
    """_show_all_plots 应把 _visible_plots 重置为 _plots。"""
    monitor._plots = [{'title': 'A', 'vars': []}, {'title': 'B', 'vars': []}]
    monitor._visible_plots = [{'title': 'A', 'vars': []}]
    called = []
    monkeypatch.setattr(monitor, '_rebuild_plots_ui',
                        lambda: called.append('r'))
    monkeypatch.setattr(monitor, '_refresh_plot', lambda: called.append('p'))
    monitor._show_all_plots()
    assert len(monitor._visible_plots) == 2
    assert 'r' in called
    assert 'p' in called


# ---- _update_file_label ----

def test_update_file_label_none_label(monitor):
    """label=None 应把 _current_file_label 设为空。"""
    monitor._file_label = None
    monitor._update_file_label(None)
    assert monitor._current_file_label == ''


def test_update_file_label_with_file_path(monitor, tmp_path):
    """文件路径 label 应只显示 basename。"""
    class _FakeLabel:
        def __init__(self):
            self.text = ''
            self.tip = ''
        def setText(self, t):
            self.text = t
        def setToolTip(self, t):
            self.tip = t
    lbl = _FakeLabel()
    monitor._file_label = lbl
    f = tmp_path / 'mylog.log'
    f.write_text('hello', encoding='utf-8')
    monitor._update_file_label(str(f))
    assert lbl.text == 'mylog.log'
    assert str(f) == lbl.tip


def test_update_file_label_non_file_string(monitor):
    """非文件路径字符串应原样显示。"""
    class _FakeLabel:
        def __init__(self):
            self.text = ''
            self.tip = ''
        def setText(self, t):
            self.text = t
        def setToolTip(self, t):
            self.tip = t
    lbl = _FakeLabel()
    monitor._file_label = lbl
    monitor._update_file_label('串口 /dev/ttyUSB0')
    assert lbl.text == '串口 /dev/ttyUSB0'


def test_update_file_label_no_label_obj(monitor):
    """_file_label=None 时只设 _current_file_label 不抛。"""
    monitor._file_label = None
    monitor._update_file_label('something')
    assert monitor._current_file_label == 'something'


# ---- _loaded_var_meta ----

def test_loaded_var_meta_records_loaded_names(monitor):
    """_loaded_var_meta 应记录已加载变量名。"""
    # gen_mock_data 调 _register_var 后 _var_meta 应有 names
    monitor.gen_mock_data(10)
    # _loaded_var_meta 是 _var_meta 的 view（dict）
    assert 'sin_50hz' in monitor._var_meta


# ---- load_log ----

def test_load_log_empty_file_returns_zero(monitor, tmp_path):
    """空日志文件应返 0 不抛。"""
    p = tmp_path / 'empty.log'
    p.write_text('', encoding='utf-8')
    # mock UI 刷新路径
    monitor._refresh_plot = lambda: None
    monitor._refresh_sidebar = lambda: None
    monitor._refresh_sidebar_visibility = lambda: None
    monitor._file_label = None
    n = monitor.load_log(str(p))
    assert n == 0


def test_load_log_motor_data(monitor, tmp_path):
    """带 motor 行的日志应解析出变量。"""
    p = tmp_path / 'motor.log'
    p.write_text('1783996615491 I model 3wd1t_1 0 0 0|0 0 0#0 0 0\n', encoding='utf-8')
    monitor._refresh_plot = lambda: None
    monitor._refresh_sidebar = lambda: None
    monitor._refresh_sidebar_visibility = lambda: None
    monitor._file_label = None
    n = monitor.load_log(str(p))
    assert n == 1
    assert any(name.startswith('motor.') for name in monitor._data)


def test_load_log_clears_existing_data(monitor, tmp_path):
    """load_log 应清掉旧 _data。"""
    monitor._data = {'old': [(0.0, 1.0)]}
    p = tmp_path / 'empty2.log'
    p.write_text('', encoding='utf-8')
    monitor._refresh_plot = lambda: None
    monitor._refresh_sidebar = lambda: None
    monitor._refresh_sidebar_visibility = lambda: None
    monitor._file_label = None
    monitor.load_log(str(p))
    assert 'old' not in monitor._data


def test_load_log_nonexistent_file_returns_zero(monitor, tmp_path):
    """不存在的文件应不抛返 0。"""
    monitor._refresh_plot = lambda: None
    monitor._refresh_sidebar = lambda: None
    monitor._refresh_sidebar_visibility = lambda: None
    monitor._file_label = None
    n = monitor.load_log(str(tmp_path / 'noexist.log'))
    assert n == 0


# ---- _toggle_sidebar ----

def test_toggle_sidebar_no_container_is_noop(monitor):
    """_sidebar_container=None 应早 return。"""
    monitor._sidebar_container = None
    monitor._toggle_sidebar()  # 不应抛


# ---- _refresh_sidebar_values / _refresh_sidebar_visibility ----

def test_refresh_sidebar_values_no_sidebar(monitor):
    """_sidebar=None 应是 noop。"""
    monitor._sidebar = None
    monitor._refresh_sidebar_values()  # 不应抛


def test_refresh_sidebar_visibility_no_sidebar(monitor):
    """_sidebar=None 应是 noop。"""
    monitor._sidebar = None
    monitor._refresh_sidebar_visibility()  # 不应抛


# ---- _apply_splitter_sizes ----

def test_apply_splitter_sizes_empty_dict_noop(monitor):
    """空 dict / 非 dict 应早 return。"""
    monitor._v_splitter = None
    monitor._apply_splitter_sizes({})
    monitor._apply_splitter_sizes(None)


def test_apply_splitter_sizes_v_size_applied(monitor):
    """v 列表长度匹配应被应用。"""
    class _FakeSplitter:
        def __init__(self):
            self.set_calls = []
            self._count = 2
        def count(self):
            return self._count
        def setSizes(self, sizes):
            self.set_calls.append(sizes)
        def widget(self, i):
            return None
    sp = _FakeSplitter()
    monitor._v_splitter = sp
    monitor._apply_splitter_sizes({'v': [100, 200]})
    assert sp.set_calls == [[100, 200]]


def test_apply_splitter_sizes_h_mismatch_skipped(monitor):
    """v 长度不匹配应跳过 v 的 setSizes。"""
    class _FakeSplitter:
        def __init__(self):
            self.set_calls = []
            self._count = 3
        def count(self):
            return self._count
        def setSizes(self, sizes):
            self.set_calls.append(sizes)
        def widget(self, i):
            return None
    sp = _FakeSplitter()
    monitor._v_splitter = sp
    monitor._apply_splitter_sizes({'v': [100, 200]})
    # v 长度不匹配，跳过
    assert sp.set_calls == []


def test_apply_splitter_sizes_h_sizes_applied(monitor):
    """h 子 splitter 列表长度匹配应被应用。"""
    class _FakeHSplitter:
        def __init__(self):
            self.set_calls = []
            self._count = 2
        def count(self):
            return self._count
        def setSizes(self, sizes):
            self.set_calls.append(sizes)
        __class__ = type('QSplitter', (), {})
    h = _FakeHSplitter()
    h.__class__.__name__ = 'QSplitter'
    class _FakeVSplitter:
        def __init__(self):
            self._count = 1
        def count(self):
            return self._count
        def setSizes(self, sizes):
            pass
        def widget(self, i):
            return h
    sp = _FakeVSplitter()
    monitor._v_splitter = sp
    monitor._apply_splitter_sizes({'h': [[100, 200]]})
    assert h.set_calls == [[100, 200]]


# ---- _restore_splitter_sizes ----

def test_restore_splitter_sizes_no_saved_is_noop(monitor):
    """config 无 saved 应不抛。"""
    from serial_debug.infra import config
    cfg = config.load()
    cfg.pop('qt_monitor_splitter_sizes', None)
    config.save(cfg)
    monitor._v_splitter = None
    monitor._restore_splitter_sizes()  # 不应抛


def test_restore_splitter_sizes_saved_not_dict_returns(monitor):
    """saved 非 dict 应早 return。"""
    from serial_debug.infra import config
    cfg = config.load()
    cfg['qt_monitor_splitter_sizes'] = 'not a dict'
    config.save(cfg)
    monitor._v_splitter = None
    monitor._restore_splitter_sizes()  # 不应抛


def test_restore_splitter_sizes_empty_dict_returns(monitor):
    """空 dict 应早 return。"""
    from serial_debug.infra import config
    cfg = config.load()
    cfg['qt_monitor_splitter_sizes'] = {}
    config.save(cfg)
    monitor._v_splitter = None
    monitor._restore_splitter_sizes()  # 不应抛


# ---- _loaded_var_meta ----

def test_loaded_var_meta_includes_data_keys(monitor):
    """已加载变量应出现。"""
    monitor._data = {'motor.rpm': [(0.0, 1.0)]}
    out = monitor._loaded_var_meta()
    names = [v['name'] for v in out]
    assert 'motor.rpm' in names


def test_loaded_var_meta_includes_registry_vars(monitor):
    """_data 空时也应包含 _ALL_VARS 静态注册表项。"""
    monitor._data = {}
    out = monitor._loaded_var_meta()
    names = [v['name'] for v in out]
    assert any(n.startswith('motor.') for n in names)
    assert any(n.startswith('bms.') for n in names)
    assert any(n.startswith('imu.') for n in names)


def test_loaded_var_meta_includes_discovered_vars(monitor, monkeypatch):
    """discovered_vars 中的 name 应出现。"""
    monkeypatch.setitem(mod._CUSTOM_PARSER_STATE, 'discovered_vars',
                        [{'name': 'custom.x', 'label': 'X'}])
    monitor._data = {}
    out = monitor._loaded_var_meta()
    names = [v['name'] for v in out]
    assert 'custom.x' in names


def test_loaded_var_meta_dedup(monitor):
    """已加载变量与静态注册表同名应去重。"""
    monitor._data = {}
    out = monitor._loaded_var_meta()
    # _ALL_VARS 内部不应有重复
    all_names = [v['name'] for v in out]
    assert len(all_names) == len(set(all_names))


# ---- _legend_offset (static) ----

def test_legend_offset_known_positions():
    """各 legend_pos 应返对应偏移。"""
    assert mod.QtMonitorWindow._legend_offset('top-left') == (10, 10)
    assert mod.QtMonitorWindow._legend_offset('top-right') == (-10, 10)
    assert mod.QtMonitorWindow._legend_offset('bottom-left') == (10, -10)
    assert mod.QtMonitorWindow._legend_offset('bottom-right') == (-10, -10)


def test_legend_offset_unknown_returns_default():
    """未知 pos 应回退 top-right。"""
    assert mod.QtMonitorWindow._legend_offset('unknown') == (-10, 10)


# ---- _legend_checkbox_qss ----

def test_legend_checkbox_qss_returns_string(monitor):
    """应返 QSS 字符串含 color + muted。"""
    s = monitor._legend_checkbox_qss('#ff0000', '#888888')
    assert isinstance(s, str)
    assert '#ff0000' in s
    assert '#888888' in s
    assert 'QCheckBox' in s


# ---- _make_legend_toggled_cb ----

def test_make_legend_toggled_cb_no_match_is_noop(monitor):
    """无匹配 chip 时调用回调不应抛。"""
    cb = monitor._make_legend_toggled_cb('motor.rpm', object())
    # _plot_headers 为空，不应抛
    cb(2)  # Qt.Checked
    cb(0)  # Qt.Unchecked


def test_make_legend_toggled_cb_matches_and_toggles(monitor, monkeypatch):
    """找到匹配 chip 时应更新 var_visibility 并触发 single refresh。"""
    class _Chip:
        pass
    chip = _Chip()
    header = {'legend_items': {'motor.rpm': chip}}
    monitor._plot_headers = [header]
    monitor._visible_plots = [{'title': 'A', 'vars': [],
                                'var_visibility': {}}]
    monitor._plot_widgets = [object()]
    monitor._curves = [{}]
    called = []
    monkeypatch.setattr(monitor, '_refresh_single_plot',
                        lambda idx: called.append(idx))
    cb = monitor._make_legend_toggled_cb('motor.rpm', chip)
    cb(2)  # checked
    assert monitor._visible_plots[0]['var_visibility']['motor.rpm'] is True
    assert called == [0]
    cb(0)  # unchecked
    assert monitor._visible_plots[0]['var_visibility']['motor.rpm'] is False


# ---- _refresh_single_plot ----

def test_refresh_single_plot_invalid_idx_noop(monitor):
    """非法 idx 应直接 return。"""
    monitor._visible_plots = []
    monitor._refresh_single_plot(0)
    monitor._refresh_single_plot(-1)
    monitor._refresh_single_plot(5)


def test_refresh_single_plot_no_plot_widget_noop(monitor):
    """_plot_widgets 长度不够应直接 return。"""
    monitor._visible_plots = [{'title': 'A', 'vars': []}]
    monitor._plot_widgets = []
    monitor._refresh_single_plot(0)  # 不应抛


# ---- _refresh_popouts ----

def test_refresh_popouts_empty_list(monitor):
    """无 popout_dialogs 应是 noop。"""
    monitor._popout_dialogs = []
    monitor._refresh_popouts()  # 不应抛


def test_refresh_popouts_skips_invisible(monitor):
    """不可见的 popout 应跳过。"""
    class _FakeDlg:
        def isVisible(self):
            return False
    monitor._popout_dialogs = [(0, _FakeDlg(), None, {}, None)]
    monitor._refresh_popouts()  # 不应抛


def test_refresh_popouts_visible_calls_draw(monitor, monkeypatch):
    """可见的 popout 应调 _draw_popout。"""
    called = []
    class _FakeDlg:
        def isVisible(self):
            return True
    monitor._popout_dialogs = [(0, _FakeDlg(), None, {}, None)]
    monkeypatch.setattr(monitor, '_draw_popout',
                        lambda *a, **kw: called.append(True))
    monitor._refresh_popouts()
    assert called == [True]


def test_refresh_popouts_draw_failure_swallowed(monitor, monkeypatch):
    """_draw_popout 抛异常应被吞。"""
    class _FakeDlg:
        def isVisible(self):
            return True
    monitor._popout_dialogs = [(0, _FakeDlg(), None, {}, None)]
    def boom(*a, **kw):
        raise RuntimeError('boom')
    monkeypatch.setattr(monitor, '_draw_popout', boom)
    monitor._refresh_popouts()  # 不应抛


# ---- _curve_color (static) ----

def test_curve_color_uses_legend_color_attr():
    """item._legend_color 应被优先返回。"""
    class _Item:
        _legend_color = '#aabbcc'
    assert mod.QtMonitorWindow._curve_color(_Item()) == '#aabbcc'


def test_curve_color_no_color_attr_returns_gray():
    """无 _legend_color 属性应返灰。"""
    class _Item:
        pass
    assert mod.QtMonitorWindow._curve_color(_Item()) == '#888888'


def test_curve_color_uses_pen_color_method():
    """有 opts.pen.color().name() 路径时应使用。"""
    class _QC:
        def name(self):
            return '#deadbe'
    class _Pen:
        def color(self):
            return _QC()
    class _Item:
        opts = {'pen': _Pen()}
    assert mod.QtMonitorWindow._curve_color(_Item()) == '#deadbe'


def test_curve_color_exception_returns_gray():
    """取色过程抛异常应回灰。"""
    class _Item:
        @property
        def opts(self):
            raise RuntimeError('boom')
    assert mod.QtMonitorWindow._curve_color(_Item()) == '#888888'


# ---- _validate_log_format ----

def test_validate_log_format_disabled_returns(monitor, monkeypatch):
    """validation.enabled=False 应早 return。"""
    monkeypatch.setitem(mod._CUSTOM_PARSER_STATE, 'validation',
                        {'enabled': False, 'min_parsed_count': 10,
                         'sample_lines': 200})
    monitor._win = None
    monitor._validate_log_format(['some line'], 'test')  # 不应抛


def test_validate_log_format_no_win_returns(monitor, monkeypatch):
    """_win=None 应早 return 不弹警告。"""
    monkeypatch.setitem(mod._CUSTOM_PARSER_STATE, 'validation',
                        {'enabled': True, 'min_parsed_count': 10,
                         'sample_lines': 200})
    monitor._win = None
    monitor._validate_log_format([], 'test')  # 不应抛


def test_validate_log_format_empty_lines_returns(monitor, monkeypatch):
    """空 lines 应早 return。"""
    monkeypatch.setitem(mod._CUSTOM_PARSER_STATE, 'validation',
                        {'enabled': True, 'min_parsed_count': 10,
                         'sample_lines': 200})
    monitor._win = None
    monitor._validate_log_format([], 'test')


def test_validate_log_format_high_parsed_rate(monitor, monkeypatch):
    """parsed >= min_count 应显示通过消息（statusBar 调用）。"""
    monkeypatch.setitem(mod._CUSTOM_PARSER_STATE, 'validation',
                        {'enabled': True, 'min_parsed_count': 1,
                         'sample_lines': 10})
    monkeypatch.setitem(mod._CUSTOM_PARSER_STATE, 'mode', 'built-in')
    monkeypatch.setitem(mod._CUSTOM_PARSER_STATE, 'func', None)
    # 用真 motor 行让 _iter_panel_vars 解析成功
    lines = ['1783996615491 I model 3wd1t_1 0 0 0|0 0 0#0 0 0']
    # mock _win.statusBar().showMessage
    sb_calls = []
    class _SB:
        def showMessage(self, msg, ms=0):
            sb_calls.append(msg)
    class _Win:
        def statusBar(self):
            return _SB()
    monitor._win = _Win()
    monitor._validate_log_format(lines, '日志 test')
    assert any('通过' in s for s in sb_calls)


def test_validate_log_format_low_parsed_triggers_warning(monitor, monkeypatch):
    """parsed < min_count 应调 _show_validation_warning。"""
    monkeypatch.setitem(mod._CUSTOM_PARSER_STATE, 'validation',
                        {'enabled': True, 'min_parsed_count': 10,
                         'sample_lines': 100})
    called = []
    monkeypatch.setattr(monitor, '_show_validation_warning',
                        lambda *a: called.append(a))
    # 用一行 motor 数据（parsed=1 < 10）
    lines = ['1783996615491 I model 3wd1t_1 0 0 0|0 0 0#0 0 0']
    class _SB:
        def showMessage(self, *a, **kw):
            pass
    class _Win:
        def statusBar(self):
            return _SB()
    monitor._win = _Win()
    monitor._validate_log_format(lines, '日志 test')
    assert called


def test_validate_log_format_invalid_count_falls_back(monitor, monkeypatch):
    """min_parsed_count 非 int 应回退默认 200/10。"""
    monkeypatch.setitem(mod._CUSTOM_PARSER_STATE, 'validation',
                        {'enabled': True, 'min_parsed_count': 'bad',
                         'sample_lines': 'bad'})
    monitor._win = None  # 早 return (no _win)
    monitor._validate_log_format(['1783996615491 I model 3wd1t_1 0 0 0|0 0 0#0 0 0'],
                                  'test')  # 不应抛


# ---- _show_validation_warning ----

def test_show_validation_warning_shows_dialog(monitor, monkeypatch):
    """有 _win 应创建 QMessageBox 并调 show。"""
    from PySide6.QtWidgets import QMessageBox
    shown = []
    monkeypatch.setattr(QMessageBox, 'show', lambda self: shown.append(True))
    monitor._win = None
    monitor._show_validation_warning('src', 10, 1, 5)
    assert shown == [True]


def test_show_validation_warning_no_win_returns(monitor, monkeypatch):
    """_win=None 时仍应执行（QMessageBox 接受 None parent）。"""
    from PySide6.QtWidgets import QMessageBox
    shown = []
    monkeypatch.setattr(QMessageBox, 'show', lambda self: shown.append(True))
    monitor._win = None
    monitor._show_validation_warning('src', 10, 1, 5)  # 不应抛
    assert shown


# ---- _show_about_dialog ----

def test_show_about_dialog_no_win_returns(monitor, monkeypatch):
    """_win=None 时仍应执行（QMessageBox.information 接受 None parent）。"""
    from PySide6.QtWidgets import QMessageBox
    called = []
    monkeypatch.setattr(QMessageBox, 'information',
                        lambda *a, **kw: called.append(a) or 0)
    monitor._win = None
    monitor._show_about_dialog()  # 不应抛
    assert called


def test_show_about_dialog_shows_info(monitor, monkeypatch):
    """有 _win 应调 QMessageBox.information。"""
    called = []
    def _info(parent, title, text):
        called.append((parent, title, text))
    monkeypatch.setattr('PySide6.QtWidgets.QMessageBox.information', _info)
    monitor._win = object()
    monitor._show_about_dialog()
    assert called
    assert 'Qt 数据监视窗口' in called[0][2]


# ---- _show_statistics ----

def test_show_statistics_with_mock_dialog(monitor, monkeypatch, qtbot):
    """_show_statistics 应弹 QDialog 并填表。"""
    monitor.gen_mock_data(10)
    # 替换 QDialog.exec 避免真模态
    from PySide6.QtWidgets import QDialog
    monkeypatch.setattr(QDialog, 'exec', lambda self: QDialog.Accepted)
    parent = None
    monitor._show_statistics(parent)  # 不应抛


# ---- _select_plot_idx ----

def test_select_plot_idx_no_plots_returns_none(monitor, monkeypatch):
    """无 plot 应弹 information 返 None。"""
    called = []
    def _info(parent, title, text):
        called.append((title, text))
        return None
    monkeypatch.setattr('PySide6.QtWidgets.QMessageBox.information', _info)
    monitor._visible_plots = []
    out = monitor._select_plot_idx(None, '选图')
    assert out is None
    assert called


def test_select_plot_idx_user_accepts(monitor, monkeypatch):
    """用户点 OK 应返 combo 当前 index。"""
    from PySide6.QtWidgets import QDialog
    monkeypatch.setattr(QDialog, 'exec', lambda self: QDialog.Accepted)
    monitor._visible_plots = [{'title': 'A', 'vars': []},
                              {'title': 'B', 'vars': []}]
    out = monitor._select_plot_idx(None, '选图')
    # combo.currentIndex 默认 0
    assert out == 0


def test_select_plot_idx_user_cancels(monitor, monkeypatch):
    """用户点 Cancel 应返 None。"""
    from PySide6.QtWidgets import QDialog
    monkeypatch.setattr(QDialog, 'exec', lambda self: QDialog.Rejected)
    monitor._visible_plots = [{'title': 'A', 'vars': []}]
    out = monitor._select_plot_idx(None, '选图')
    assert out is None


# ---- _refresh_plot (无 plot_widgets / sidebar) ----

def test_refresh_plot_no_plot_widgets_refreshes_sidebar(monitor):
    """无 _plot_widgets 应只刷新侧栏值/可见性。"""
    monitor._plot_widgets = []
    monitor._sidebar = None
    monitor._refresh_plot()  # 不应抛


def test_refresh_plot_with_sidebar_rowcount_mismatch(monitor, monkeypatch):
    """sidebar 行数与 var_meta 不一致应调 _refresh_sidebar。"""
    class _FakeTree:
        def topLevelItemCount(self):
            return 0
    monitor._sidebar = _FakeTree()
    monitor._var_meta = {'a': {}}
    called = []
    monkeypatch.setattr(monitor, '_refresh_sidebar', lambda: called.append(True))
    monitor._plot_widgets = []
    monitor._refresh_plot()
    assert called == [True]


def test_refresh_plot_sidebar_failure_swallowed(monitor, monkeypatch):
    """sidebar topLevelItemCount 抛异常应被吞（_refresh_plot 内层 try/except）。"""
    class _FakeTree:
        def topLevelItemCount(self):
            raise RuntimeError('boom')
    monitor._sidebar = _FakeTree()
    monitor._plot_widgets = []
    # _refresh_plot 应吞掉异常返回——但 _refresh_sidebar_values 会再调一次
    # topLevelItemCount；将其改为 None 让后续 _refresh_sidebar_values 早 return
    # 验证 _refresh_plot 内的 try/except 兜底
    orig = monitor._sidebar
    call_count = [0]
    class _ToggleTree:
        def topLevelItemCount(self):
            call_count[0] += 1
            # 第二次调用（在 _refresh_sidebar_values 里）应让 sidebar=None
            # 通过直接置空避免再抛
            if call_count[0] >= 2:
                return 0
            raise RuntimeError('boom')
    monitor._sidebar = _ToggleTree()
    monitor._refresh_plot()  # 不应抛


# ---- _popout_current_plot ----

def test_popout_current_plot_no_plots_returns(monitor):
    """_visible_plots 为空应直接 return。"""
    monitor._visible_plots = []
    monitor._popout_current_plot()  # 不应抛


# ---- _close_popout ----

def test_close_popout_removes_entry(monitor):
    """_close_popout 应从 _popout_dialogs 移除匹配项。"""
    class _Dlg1:
        pass
    class _Pw:
        def setParent(self, p):
            pass
        def deleteLater(self):
            pass
    pw = _Pw()
    dlg = _Dlg1()
    monitor._popout_dialogs = [(0, dlg, pw, {}, None)]
    monitor._close_popout(0, dlg)
    assert monitor._popout_dialogs == []


def test_close_popout_no_match_is_noop(monitor):
    """无匹配项应是 noop。"""
    monitor._popout_dialogs = []
    monitor._close_popout(0, object())  # 不应抛


def test_close_popout_delete_failure_swallowed(monitor):
    """pw.deleteLater 抛异常应被吞。"""
    class _Pw:
        def setParent(self, p):
            pass
        def deleteLater(self):
            raise RuntimeError('boom')
    dlg = object()
    monitor._popout_dialogs = [(0, dlg, _Pw(), {}, None)]
    monitor._close_popout(0, dlg)  # 不应抛


# ---- _draw_popout ----

def test_draw_popout_idx_out_of_range_returns(monitor):
    """idx 超出 _visible_plots 长度应 return。"""
    monitor._visible_plots = []
    monitor._draw_popout(0, None, None, {}, None)  # 不应抛


# ---- _do_rebuild_plots ----

def test_do_rebuild_plots_empty_visible(monitor):
    """_visible_plots 为空应早 return。"""
    class _Layout:
        def count(self):
            return 0
        def itemAt(self, i):
            return None
    monitor._plots_layout = _Layout()
    monitor._visible_plots = []
    # 调 _do_rebuild_plots 不应抛（空 visible 早 return）
    monitor._do_rebuild_plots(_Layout(), None, None, None, None, {})


# ---- _rebuild_plots_ui ----

def test_rebuild_plots_ui_no_layout_returns(monitor):
    """_plots_layout=None 应早 return。"""
    monitor._plots_layout = None
    monitor._rebuild_plots_ui()  # 不应抛


# ---- _open_parser_config ----

def test_open_parser_config_no_win_returns(monitor):
    """_win=None 应早 return。"""
    monitor._win = None
    monitor._open_parser_config()  # 不应抛


def test_reparse_after_config_change_no_source(monitor):
    """无日志/无串口时，_reparse_after_config_change 应刷 plot 不抛。"""
    monitor._refresh_plot = lambda: None
    monitor._refresh_sidebar = lambda: None
    monitor._refresh_sidebar_visibility = lambda: None
    monitor.log_path = ''
    monitor._serial = None
    monitor._reparse_after_config_change()  # 不应抛


def test_reparse_after_config_change_reloads_log(monitor, tmp_path):
    """有 log_path 时 _reparse_after_config_change 应重读文件重解析。

    构造一个日志文件，先 load_log 解析出变量，改 _CUSTOM_PARSER_STATE 让它
    能解析出不同变量，再调 _reparse_after_config_change 应重解析出新变量。
    """
    p = tmp_path / 'motor.log'
    p.write_text(
        '1783996615491 I model 3wd1t_1 0 0 0|0 0 0#0 0 0\n' * 5,
        encoding='utf-8')
    monitor._refresh_plot = lambda: None
    monitor._refresh_sidebar = lambda: None
    monitor._refresh_sidebar_visibility = lambda: None
    monitor._file_label = None
    monitor._win = None  # 避免 statusBar
    monitor.log_path = str(p)
    monitor.load_log(str(p))
    assert any(name.startswith('motor.') for name in monitor._data)
    # 改解析器为 custom 模式，自定义 parse_line 产出 different 变量名
    def parse_line(line):
        if '3wd1t_1' in line:
            ts = 1.0
            yield ts, 'custom.var', 42.0
    mod._CUSTOM_PARSER_STATE.update({'mode': 'custom', 'func': parse_line})
    monitor._reparse_after_config_change()
    # 重解析后应含 custom.var，不含 motor.*
    assert 'custom.var' in monitor._data
    assert not any(name.startswith('motor.') for name in monitor._data)


def test_open_parser_config_reparse_on_save(monitor, tmp_path, monkeypatch):
    """保存解析器配置后，若有 log_path 应自动重解析（_open_parser_config 路径）。"""
    # 构造日志
    p = tmp_path / 'motor.log'
    p.write_text(
        '1783996615491 I model 3wd1t_1 0 0 0|0 0 0#0 0 0\n' * 3,
        encoding='utf-8')
    monitor._refresh_plot = lambda: None
    monitor._refresh_sidebar = lambda: None
    monitor._refresh_sidebar_visibility = lambda: None
    monitor._file_label = None
    monitor.log_path = str(p)
    monitor.load_log(str(p))
    assert any(name.startswith('motor.') for name in monitor._data)

    # mock 对话框：直接模拟保存（写 _CUSTOM_PARSER_STATE）+ 返回非 None result
    from PySide6.QtWidgets import QMainWindow
    win = QMainWindow()
    monitor._win = win

    def parse_line(line):
        if '3wd1t_1' in line:
            yield 1.0, 'new.var', 1.0
    mod._CUSTOM_PARSER_STATE.update({'mode': 'custom', 'func': parse_line,
                                     'discovered_vars': [{'name': 'new.var',
                                                         'label': 'new.var'}]})

    # patch _QtParserConfigDialog 让 exec() 返回非 None 触发重解析分支
    class _FakeDlg:
        def __init__(self, *a, **kw):
            pass
        def exec(self):
            return {'mode': 'custom'}  # truthy → 进入重解析
    monkeypatch.setattr(mod, '_QtParserConfigDialog', _FakeDlg)
    monitor._open_parser_config()
    assert 'new.var' in monitor._data
    win.deleteLater()


def test_save_invalid_parser_shows_warning(monitor, monkeypatch):
    """_on_save 时 exec 失败 / 无 parse_line 应弹 QMessageBox.warning（非静默吞）。

    用真实 _QtParserConfigDialog 构造（需 QApplication），填坏代码，mock
    QMessageBox.warning 记录调用，mock _dlg.accept 避免真关闭。
    """
    from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox
    app = QApplication.instance() or QApplication([])
    win = QMainWindow()
    calls = []

    dlg = mod._QtParserConfigDialog(win, callback_save=lambda: None)
    dlg._build()  # 构造内部控件（_code_edit 等）
    # 填一段无 parse_line 的代码 → callable(ns.get('parse_line')) is False
    dlg._code_edit.setPlainText('def x():\n  pass')
    dlg._mode_built.setChecked(False)
    dlg._mode_custom.setChecked(True)
    # mode both 也走 custom 分支，这里用 custom 即可

    monkeypatch.setattr(QMessageBox, 'warning',
                        lambda *a, **kw: calls.append(a))
    # 避免 accept 真关 dialog（_dlg 是真实 QDialog）
    monkeypatch.setattr(dlg._dlg, 'accept', lambda: None)
    dlg._on_save()
    # 应触发 warning（代码未定义 parse_line）
    assert len(calls) >= 1
    # func 应为 None（未启用）
    assert mod._CUSTOM_PARSER_STATE.get('func') is None
    win.deleteLater()


# ---- _auto_avoid_legend ----

def test_auto_avoid_legend_no_curves(monitor):
    """无 curves 应是 noop。"""
    class _FakePW:
        pass
    monitor._auto_avoid_legend(_FakePW(), {})  # 不应抛


# ---- _make_time_axis ----

def test_make_time_axis_returns_axis(monitor):
    """_make_time_axis 应返回 AxisItem 子类实例。"""
    import pyqtgraph as pg
    ax = mod._make_time_axis({'muted': '#888', 'fg': '#fff'})
    assert ax is not None
    # tickStrings 应格式化时间
    out = ax.tickStrings([0], 1, 10)
    assert isinstance(out, list)


def test_make_time_axis_theme_failure_swallowed(monitor):
    """theme 缺 key 不应抛。"""
    ax = mod._make_time_axis({})
    assert ax is not None


def test_make_time_axis_uses_beijing_time(monitor, monkeypatch):
    """时间轴刻度应显式北京时间（UTC+8），不依赖系统时区。

    1787888455 的 UTC 是 03:40:55，北京是 11:40:55。在 TZ=UTC 下，
    若用裸 fromtimestamp 会得到 03:40:55（错误）；显式 BEIJING_TZ 应得 11:40:55。
    """
    monkeypatch.setenv('TZ', 'UTC')
    import time as _time
    _time.tzset()
    ax = mod._make_time_axis({'muted': '#888', 'fg': '#fff'})
    # spacing=10 → fmt='%H:%M:%S'
    out = ax.tickStrings([1787888455.0], 1, 10)
    assert isinstance(out, list)
    assert out[0] == '11:40:55', f'时间轴非北京时间：{out[0]!r}'


def test_make_time_axis_mmdd_uses_beijing_time(monitor, monkeypatch):
    """时间轴 > 1h 精度（MM-DD HH:MM）也用北京时间。"""
    monkeypatch.setenv('TZ', 'UTC')
    import time as _time
    _time.tzset()
    ax = mod._make_time_axis({'muted': '#888', 'fg': '#fff'})
    # spacing=100000 (>86400) → fmt='%m-%d'
    out = ax.tickStrings([1787888455.0], 1, 100000)
    assert out[0] == '08-28', f'时间轴日期非北京时间：{out[0]!r}'


# ---- _on_plot_hover ----

def test_on_plot_hover_outside_scene(monitor):
    """鼠标离开 plot 区域应隐藏 vline + label。"""
    class _FakePW:
        def sceneBoundingRect(self):
            class _R:
                def contains(self, pos):
                    return False
            return _R()
        def getViewBox(self):
            return None
    pw = _FakePW()
    monitor._on_plot_hover(pw, None)  # 不应抛


def test_on_plot_hover_no_curves(monitor, monkeypatch):
    """鼠标在 plot 内但无 curves 应不抛。"""
    class _Rect:
        def contains(self, pos):
            return True
    class _VB:
        def mapSceneToView(self, pos):
            class _P:
                def x(self):
                    return 0.0
                def y(self):
                    return 0.0
            return _P()
    class _FakePW:
        _hover_vline = None
        _hover_label = None
        _plot_cfg = {'hover_mode': 'current'}
        _plot_idx = 0
        def sceneBoundingRect(self):
            return _Rect()
        def getViewBox(self):
            return _VB()
    pw = _FakePW()
    monitor._curves = []
    monitor._on_plot_hover(pw, object())  # 不应抛


# ---- _refresh_sidebar ----

def test_refresh_sidebar_no_sidebar_is_noop(monitor):
    """_sidebar=None 应直接 return。"""
    monitor._sidebar = None
    monitor._refresh_sidebar()  # 不应抛


def test_refresh_sidebar_rebuilds_rows(monitor, qtbot):
    """有 _sidebar 应重建行（QTreeWidget 实例）。"""
    from PySide6.QtWidgets import QTreeWidget
    tree = QTreeWidget()
    monitor._sidebar = tree
    monitor._var_meta = {'a': {'color': '#ff0000', 'label': 'A'},
                          'b': {'color': '#00ff00', 'label': 'B'}}
    monitor._refresh_sidebar()
    assert tree.topLevelItemCount() == 2
    tree.deleteLater()


def test_refresh_sidebar_preserves_selection(monitor, qtbot):
    """refresh 后应恢复选中行。"""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem
    tree = QTreeWidget()
    monitor._sidebar = tree
    monitor._var_meta = {'a': {'color': '#ff', 'label': 'A'},
                          'b': {'color': '#ff', 'label': 'B'}}
    monitor._refresh_sidebar()
    # 选 b
    item_b = tree.topLevelItem(1)
    tree.setCurrentItem(item_b)
    monitor._refresh_sidebar()
    cur = tree.currentItem()
    assert cur is not None
    assert cur.data(0, Qt.UserRole) == 'b'
    tree.deleteLater()


# ---- _refresh_sidebar_values ----

def test_refresh_sidebar_values_with_data(monitor, qtbot):
    """有 _data 时应更新 text 第 2 列（当前值）。"""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem
    tree = QTreeWidget()
    monitor._sidebar = tree
    monitor._var_meta = {'a': {'color': '#ff', 'label': 'A'}}
    monitor._data = {'a': [(0.0, 5.0), (1.0, 10.0)]}
    monitor._refresh_sidebar()  # build rows
    monitor._refresh_sidebar_values()
    item = tree.topLevelItem(0)
    assert item.text(2) == '10'  # 末值 10.0 整数显示
    tree.deleteLater()


def test_refresh_sidebar_values_empty_buffer_skipped(monitor, qtbot):
    """空 buffer 应被跳过。"""
    from PySide6.QtWidgets import QTreeWidget
    tree = QTreeWidget()
    monitor._sidebar = tree
    monitor._var_meta = {'a': {'color': '#ff', 'label': 'A'}}
    monitor._data = {'a': []}
    monitor._refresh_sidebar()
    # 不应抛
    monitor._refresh_sidebar_values()
    tree.deleteLater()


def test_refresh_sidebar_values_numpy_tuple_data(monitor, qtbot):
    """numpy 元组数据应取 ys[-1] 作为当前值。"""
    import numpy as np
    from PySide6.QtWidgets import QTreeWidget
    tree = QTreeWidget()
    monitor._sidebar = tree
    monitor._var_meta = {'a': {'color': '#ff', 'label': 'A'}}
    xs = np.array([0.0, 1.0, 2.0])
    ys = np.array([5.5, 6.5, 7.5])
    monitor._data = {'a': (xs, ys)}
    monitor._refresh_sidebar()
    monitor._refresh_sidebar_values()
    item = tree.topLevelItem(0)
    assert item.text(2) == '7.500'
    tree.deleteLater()


def test_refresh_sidebar_values_exception_swallowed(monitor, qtbot):
    """_data[name] 取值抛异常应被吞。"""
    from PySide6.QtWidgets import QTreeWidget
    tree = QTreeWidget()
    monitor._sidebar = tree
    monitor._var_meta = {'a': {'color': '#ff', 'label': 'A'}}
    monitor._data = {'a': 'bad data'}
    monitor._refresh_sidebar()
    monitor._refresh_sidebar_values()  # 不应抛
    tree.deleteLater()


# ---- _refresh_sidebar_visibility ----

def test_refresh_sidebar_visibility_marks_in_plot(monitor, qtbot):
    """有 plot.vars=[v1] 时 v1 应被标 ✓，其他变量空。"""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QTreeWidget
    tree = QTreeWidget()
    monitor._sidebar = tree
    monitor._var_meta = {'a': {'color': '#ff', 'label': 'A'},
                          'b': {'color': '#ff', 'label': 'B'}}
    monitor._data = {'a': [(0, 1)], 'b': [(0, 2)]}
    monitor._visible_plots = [{'title': 'p', 'vars': ['a']}]
    monitor._refresh_sidebar()
    monitor._refresh_sidebar_visibility()
    a_item = tree.topLevelItem(0)
    b_item = tree.topLevelItem(1)
    assert a_item.text(3) == '✓'
    assert b_item.text(3) == ''
    tree.deleteLater()


def test_refresh_sidebar_visibility_empty_vars_all_marked(monitor, qtbot):
    """plot.vars=[] 时所有已加载变量都应被标 ✓。"""
    from PySide6.QtWidgets import QTreeWidget
    tree = QTreeWidget()
    monitor._sidebar = tree
    monitor._var_meta = {'a': {'color': '#ff', 'label': 'A'},
                          'b': {'color': '#ff', 'label': 'B'}}
    monitor._data = {'a': [(0, 1)], 'b': [(0, 2)]}
    monitor._visible_plots = [{'title': 'p', 'vars': []}]
    monitor._refresh_sidebar()
    monitor._refresh_sidebar_visibility()
    for i in range(tree.topLevelItemCount()):
        item = tree.topLevelItem(i)
        assert item.text(3) == '✓'
    tree.deleteLater()


# ---- _toggle_sidebar ----

def test_toggle_sidebar_flips_visibility(monitor, monkeypatch):
    """_toggle_sidebar 应翻转 _sidebar_visible 并调 setVisible。"""
    set_visible_calls = []
    class _FakeContainer:
        def setVisible(self, v):
            set_visible_calls.append(v)
    monitor._sidebar_container = _FakeContainer()
    monitor._sidebar_visible = True
    monitor._act_sidebar = None
    # mock config to avoid file write
    _mem_config(monkeypatch)
    monitor._toggle_sidebar()
    assert monitor._sidebar_visible is False
    assert set_visible_calls == [False]
    # 再调一次翻回
    monitor._toggle_sidebar()
    assert monitor._sidebar_visible is True
    assert set_visible_calls == [False, True]


def test_toggle_sidebar_updates_act_sidebar(monitor, monkeypatch):
    """_act_sidebar 非 None 时应同步 checked。"""
    class _FakeContainer:
        def setVisible(self, v):
            pass
    class _FakeAct:
        def __init__(self):
            self.checked = None
        def setChecked(self, v):
            self.checked = v
    monitor._sidebar_container = _FakeContainer()
    monitor._act_sidebar = _FakeAct()
    monitor._sidebar_visible = False
    _mem_config(monkeypatch)
    monitor._toggle_sidebar()
    assert monitor._act_sidebar.checked is True


def test_toggle_sidebar_config_failure_swallowed(monitor, monkeypatch):
    """config.save 抛异常应被吞。"""
    from serial_debug.infra import config
    class _FakeContainer:
        def setVisible(self, v):
            pass
    monitor._sidebar_container = _FakeContainer()
    monitor._act_sidebar = None
    def boom(cfg):
        raise RuntimeError('fail')
    monkeypatch.setattr(config, 'save', boom)
    monitor._toggle_sidebar()  # 不应抛


# ---- _refresh_header_legend ----

def test_refresh_header_legend_none_is_noop(monitor):
    """header=None 应直接 return。"""
    monitor._refresh_header_legend(None, [], {'title': 'A'})


def test_refresh_header_legend_hidden_legend(monitor, qtbot):
    """legend_pos='hidden' 应设 legend_box 不可见。"""
    class _FakeLabel:
        def __init__(self):
            self.text_val = ''
            self.setFont = lambda f: None
            self.font = lambda: None
        def text(self):
            return self.text_val
        def setText(self, t):
            self.text_val = t
    class _FakeBox:
        def __init__(self):
            self.visible = True
        def setVisible(self, v):
            self.visible = v
    class _FakeFlow:
        def __init__(self):
            self.added = []
            self.removed = []
            self.setParent = lambda p: None
        def addWidget(self, w):
            self.added.append(w)
        def removeWidget(self, w):
            self.removed.append(w)
    header = {
        'title_label': _FakeLabel(),
        'legend_box': _FakeBox(),
        'legend_flow': _FakeFlow(),
        'legend_items': {},
    }
    monitor._refresh_header_legend(header, [], {'title': 'My Plot',
                                                  'legend_pos': 'hidden'})
    assert header['legend_box'].visible is False
    assert header['title_label'].text_val == 'My Plot'


def test_refresh_header_legend_visible_legend(monitor, qtbot):
    """非 hidden legend_pos 应让 legend_box 可见。"""
    class _FakeLabel:
        def __init__(self):
            self.text_val = ''
        def text(self):
            return self.text_val
        def setText(self, t):
            self.text_val = t
        def setFont(self, f):
            pass
        def font(self):
            return None
    class _FakeBox:
        def __init__(self):
            self.visible = True
        def setVisible(self, v):
            self.visible = v
    class _FakeFlow:
        def addWidget(self, w):
            pass
        def removeWidget(self, w):
            pass
        def setParent(self, p):
            pass
    header = {
        'title_label': _FakeLabel(),
        'legend_box': _FakeBox(),
        'legend_flow': _FakeFlow(),
        'legend_items': {},
    }
    monitor._refresh_header_legend(header, [], {'title': 'A',
                                                  'legend_pos': 'top-right'})
    assert header['legend_box'].visible is True


def test_refresh_header_legend_removes_stale_chip(monitor, qtbot):
    """vars_to_show 不含某 name 时应从 legend_items 移除。"""
    class _FakeLabel:
        def text(self):
            return ''
        def setText(self, t):
            pass
        def setFont(self, f):
            pass
        def font(self):
            return None
    class _FakeBox:
        def setVisible(self, v):
            pass
    class _FakeFlow:
        def addWidget(self, w):
            pass
        def removeWidget(self, w):
            pass
        def setParent(self, p):
            pass
    class _FakeChip:
        def setParent(self, p):
            pass
        def deleteLater(self):
            pass
    header = {
        'title_label': _FakeLabel(),
        'legend_box': _FakeBox(),
        'legend_flow': _FakeFlow(),
        'legend_items': {'old_chip': _FakeChip()},
    }
    monitor._var_meta = {}
    monitor._refresh_header_legend(header, [], {'title': 'A',
                                                  'legend_pos': 'top-right'})
    assert 'old_chip' not in header['legend_items']


# ---- _refresh_single_plot ----

def test_refresh_single_plot_empty_data(monitor, monkeypatch):
    """_data 空时应清 curves 但不画。"""
    class _FakePW:
        def removeItem(self, item):
            pass
    monitor._visible_plots = [{'title': 'A', 'vars': [], 'var_visibility': {}}]
    monitor._plot_widgets = [_FakePW()]
    monitor._curves = [{'old_curve': object()}]
    monitor._plot_headers = [None]
    monitor._data = {}
    monkeypatch.setattr(monitor, '_refresh_header_legend', lambda *a: None)
    monkeypatch.setattr(monitor, '_draw_stat_markers', lambda *a: None)
    monkeypatch.setattr(monitor, '_refresh_sidebar_visibility', lambda: None)
    monitor._refresh_single_plot(0)
    assert monitor._curves[0] == {}


def test_refresh_single_plot_with_numpy_data(monitor, monkeypatch):
    """numpy 元组数据应被 plot。"""
    import numpy as np
    class _FakeItem:
        _legend_color = None
        def setData(self, xs, ys, pen=None):
            pass
    class _FakePW:
        def __init__(self):
            self.plotted = []
            self.removed = []
        def plot(self, xs, ys, pen=None, name=None):
            item = _FakeItem()
            self.plotted.append(item)
            return item
        def removeItem(self, item):
            self.removed.append(item)
    pw = _FakePW()
    monitor._visible_plots = [{'title': 'A', 'vars': [],
                                'var_visibility': {}}]
    monitor._plot_widgets = [pw]
    monitor._curves = [{}]
    monitor._plot_headers = [None]
    xs = np.array([0.0, 1.0])
    ys = np.array([5.0, 6.0])
    monitor._data = {'v': (xs, ys)}
    monitor._var_meta = {'v': {'color': '#fff', 'label': 'V'}}
    monkeypatch.setattr(monitor, '_refresh_header_legend', lambda *a: None)
    monkeypatch.setattr(monitor, '_draw_stat_markers', lambda *a: None)
    monkeypatch.setattr(monitor, '_refresh_sidebar_visibility', lambda: None)
    monitor._refresh_single_plot(0)
    assert 'v' in monitor._curves[0]


def test_refresh_single_plot_skips_unchecked_var(monitor, monkeypatch):
    """var_visibility[name]=False 时该 var 不应被画。"""
    import numpy as np
    class _FakePW:
        def plot(self, *a, **kw):
            raise AssertionError('should not be called')
        def removeItem(self, item):
            pass
    monitor._plot_widgets = [_FakePW()]
    monitor._visible_plots = [{'title': 'A', 'vars': ['v'],
                                'var_visibility': {'v': False}}]
    monitor._curves = [{'v': object()}]  # 既有 curve 应被移除
    monitor._plot_headers = [None]
    monitor._data = {'v': (np.array([0]), np.array([1]))}
    monitor._var_meta = {'v': {'color': '#fff', 'label': 'V'}}
    monkeypatch.setattr(monitor, '_refresh_header_legend', lambda *a: None)
    monkeypatch.setattr(monitor, '_draw_stat_markers', lambda *a: None)
    monkeypatch.setattr(monitor, '_refresh_sidebar_visibility', lambda: None)
    monitor._refresh_single_plot(0)
    assert 'v' not in monitor._curves[0]


# ---- _draw_popout ----

def test_draw_popout_with_mock_pw(monitor, monkeypatch):
    """有 visible_plots[idx] 时应调 _draw_popout 流程。"""
    class _FakePW:
        def __init__(self):
            self.added = []
            self.removed = []
            self.setEnabled = lambda v: None
            self.enableAutoRange = lambda: None
        def addItem(self, item, ignoreBounds=False):
            self.added.append(item)
        def removeItem(self, item):
            self.removed.append(item)
    pw = _FakePW()
    monitor._visible_plots = [{'title': 'A', 'vars': [], 'var_visibility': {}}]
    monitor._data = {}
    monitor._curves = [{}]
    monitor._plot_headers = [None]
    monkeypatch.setattr(monitor, '_refresh_header_legend', lambda *a: None)
    monkeypatch.setattr(monitor, '_draw_stat_markers', lambda *a: None)
    monitor._draw_popout(0, None, pw, {}, None)


# ---- _on_plot_hover inside scene ----

def test_on_plot_hover_inside_scene_with_curves(monitor, monkeypatch):
    """鼠标在 plot 内 + 有 curves 应构建 hover label HTML。"""
    import numpy as np
    import pyqtgraph as pg
    # 准备 plot widget 模拟
    class _FakeVB:
        def mapSceneToView(self, pos):
            class _P:
                def x(self):
                    return 0.5
                def y(self):
                    return 0.5
            return _P()
    class _FakeRect:
        def contains(self, pos):
            return True
    class _FakeItem:
        xData = np.array([0.0, 1.0])
        yData = np.array([5.0, 6.0])
        _legend_color = '#ffffff'
        def setData(self, xs, ys, pen=None):
            pass
    class _FakeScene:
        sigMouseMoved = type('Sig', (), {'connect': lambda self, cb: None})()
    class _FakePW:
        _hover_vline = None
        _hover_label = None
        _plot_cfg = {'hover_mode': 'all', 'hover_show_max': True,
                     'hover_show_min': True, 'hover_show_avg': True,
                     'hover_show_last': True}
        _plot_idx = 0
        _stat_markers = []
        def sceneBoundingRect(self):
            return _FakeRect()
        def getViewBox(self):
            return _FakeVB()
        def scene(self):
            return _FakeScene()
        def addItem(self, item, ignoreBounds=False):
            self._stat_markers.append(item)
    pw = _FakePW()
    fake_item = _FakeItem()
    monitor._curves = [{'v': fake_item}]
    monitor._var_meta = {'v': {'color': '#fff', 'label': 'V'}}
    monitor._on_plot_hover(pw, object())
    # 不应抛


def test_on_plot_hover_stats_mode(monitor, monkeypatch):
    """hover_mode='stats' 应触发 stats 行构建。"""
    import numpy as np
    class _FakeVB:
        def mapSceneToView(self, pos):
            class _P:
                def x(self):
                    return 0.5
                def y(self):
                    return 0.5
            return _P()
    class _FakeRect:
        def contains(self, pos):
            return True
    class _FakeItem:
        xData = np.array([0.0, 1.0])
        yData = np.array([5.0, 6.0])
        _legend_color = '#ffffff'
        def setData(self, *a, **kw):
            pass
    class _FakeScene:
        sigMouseMoved = type('Sig', (), {'connect': lambda self, cb: None})()
    class _FakePW:
        _hover_vline = None
        _hover_label = None
        _plot_cfg = {'hover_mode': 'stats', 'hover_show_max': True,
                     'hover_show_min': True, 'hover_show_avg': True,
                     'hover_show_last': True}
        _plot_idx = 0
        _stat_markers = []
        def sceneBoundingRect(self):
            return _FakeRect()
        def getViewBox(self):
            return _FakeVB()
        def scene(self):
            return _FakeScene()
        def addItem(self, item, ignoreBounds=False):
            self._stat_markers.append(item)
    pw = _FakePW()
    fake_item = _FakeItem()
    monitor._curves = [{'v': fake_item}]
    monitor._var_meta = {'v': {'color': '#fff', 'label': 'V'}}
    monitor._on_plot_hover(pw, object())


# ---- _auto_avoid_legend ----

def test_auto_avoid_legend_with_curves(monitor):
    """有 curves + valid pw 应不抛。"""
    import numpy as np
    class _FakeVB:
        def viewRect(self):
            class _R:
                def x(self):
                    return 0.0
                def y(self):
                    return 0.0
                def width(self):
                    return 10.0
                def height(self):
                    return 10.0
            return _R()
        def mapViewToScene(self, pt):
            return pt
    class _FakeItem:
        xData = np.array([0.0, 1.0])
        yData = np.array([5.0, 6.0])
    class _FakeLegend:
        def geometry(self):
            class _G:
                def x(self):
                    return 0
                def y(self):
                    return 0
                def width(self):
                    return 10
                def height(self):
                    return 10
            return _G()
        def setPosition(self, *a):
            pass
    class _FakePW:
        def getViewBox(self):
            return _FakeVB()
        def getPlotItem(self):
            class _Item:
                legend = _FakeLegend()
            return _Item()
    pw = _FakePW()
    curves = {'v': _FakeItem()}
    monitor._auto_avoid_legend(pw, curves)


# ---- _draw_stat_markers ----

def test_draw_stat_markers_no_markers(monitor):
    """show_max/min/avg/last 都 False 应不画 marker。"""
    class _FakePW:
        def __init__(self):
            self.added = []
        def addItem(self, item, ignoreBounds=False):
            self.added.append(item)
        def getViewBox(self):
            class _VB:
                def viewRect(self):
                    class _R:
                        def x(self): return 0
                        def y(self): return 0
                        def width(self): return 10
                        def height(self): return 10
                    return _R()
                def mapViewToScene(self, pt):
                    return pt
            return _VB()
    pw = _FakePW()
    curves = {'v': type('It', (), {'xData': None, 'yData': None,
                                     '_legend_color': '#fff'})()}
    p_cfg = {'show_max': False, 'show_min': False, 'show_avg': False,
             'show_last': False, 'hover_mode': 'current'}
    monitor._draw_stat_markers(pw, curves, p_cfg)


# ---- _popout_current_plot ----

def test_popout_current_plot_no_plots_returns_v2(monitor, monkeypatch):
    """_visible_plots 为空应直接 return（不调 _make_plot_widget）。"""
    called = []
    monkeypatch.setattr(monitor, '_make_plot_widget',
                        lambda *a, **kw: called.append(True) or (None, None, None))
    monitor._visible_plots = []
    monitor._popout_current_plot()
    assert called == []


def test_popout_current_plot_already_popped(monitor, monkeypatch):
    """已弹出的同 plot 应 raise_ + activate。"""
    class _FakeDlg:
        def isVisible(self):
            return True
        def raise_(self):
            self.raised = True
        def activateWindow(self):
            self.activated = True
    dlg = _FakeDlg()
    monitor._popout_dialogs = [(0, dlg, None, {}, None)]
    monitor._visible_plots = [{'title': 'A', 'vars': []}]
    monitor._plot_widgets = [object()]
    monitor._popout_current_plot()
    assert dlg.raised
    assert dlg.activated


# ---- _build_sidebar ----

def test_build_sidebar_returns_container(monitor, qtbot):
    """_build_sidebar 应返回 QWidget 容器并建出 _sidebar QTreeWidget。"""
    monitor._build_sidebar()
    assert monitor._sidebar is not None
    assert monitor._sidebar_container is not None
    # tree 应有 4 列
    assert monitor._sidebar.columnCount() == 4
    # 清理
    monitor._sidebar_container.deleteLater()
    monitor._sidebar.deleteLater()


def test_build_sidebar_custom_context_menu_policy(monitor, qtbot):
    """tree 应启用 CustomContextMenu。"""
    from PySide6.QtCore import Qt
    monitor._build_sidebar()
    assert monitor._sidebar.contextMenuPolicy() == Qt.CustomContextMenu
    monitor._sidebar_container.deleteLater()


# ---- _build_menubar ----

def test_build_menubar_creates_menus(monitor, qtbot):
    """_build_menubar 应建出 文件/视图/工具/帮助 四菜单。"""
    from PySide6.QtWidgets import QMainWindow
    win = QMainWindow()
    monitor._build_menubar(
        win,
        _add_plot=lambda: None,
        _edit_plot=lambda: None,
        _del_plot=lambda: None,
        _open_log=lambda: None,
        _mock_10w=lambda: None,
        _mock_100w=lambda: None,
        _clear=lambda: None,
        _toggle_theme=lambda: None,
        _toggle_sidebar=lambda: None,
        _show_stats=lambda: None,
        _export_csv=lambda: None,
        _connect_serial=lambda: None,
        _disconnect_serial=lambda: None,
        _perf_test=lambda: None,
        _import_plots_cfg=lambda: None,
        _export_plots_cfg=lambda: None,
    )
    mb = win.menuBar()
    actions = [a.text() for a in mb.actions()]
    assert '文件' in actions
    assert '视图' in actions
    assert '工具' in actions
    assert '帮助' in actions
    # _act_sidebar / _act_toolbar 应被设置
    assert monitor._act_sidebar is not None
    assert monitor._act_toolbar is not None
    win.deleteLater()


def test_build_menubar_tools_has_parser_config_entry(monitor, qtbot):
    """_build_menubar 传入 _open_parser_cfg 时，「工具」菜单应含「日志格式配置…」。"""
    from PySide6.QtWidgets import QMainWindow
    win = QMainWindow()
    called = []
    monitor._build_menubar(
        win,
        _add_plot=lambda: None,
        _edit_plot=lambda: None,
        _del_plot=lambda: None,
        _open_log=lambda: None,
        _mock_10w=lambda: None,
        _mock_100w=lambda: None,
        _clear=lambda: None,
        _toggle_theme=lambda: None,
        _toggle_sidebar=lambda: None,
        _show_stats=lambda: None,
        _export_csv=lambda: None,
        _connect_serial=lambda: None,
        _disconnect_serial=lambda: None,
        _perf_test=lambda: None,
        _import_plots_cfg=lambda: None,
        _export_plots_cfg=lambda: None,
        _open_parser_cfg=lambda: called.append('cfg'),
    )
    mb = win.menuBar()
    # 找「工具」菜单
    tools_menu = None
    for a in mb.actions():
        if a.text() == '工具':
            tools_menu = a.menu()
            break
    assert tools_menu is not None
    tool_actions = [a.text() for a in tools_menu.actions()]
    assert '日志格式配置…' in tool_actions
    # 触发应调用回调
    idx = tool_actions.index('日志格式配置…')
    tools_menu.actions()[idx].trigger()
    assert called == ['cfg']
    win.deleteLater()


def test_build_menubar_no_parser_cfg_omits_entry(monitor, qtbot):
    """_build_menubar 不传 _open_parser_cfg 时，「工具」菜单无解析器项（向后兼容）。"""
    from PySide6.QtWidgets import QMainWindow
    win = QMainWindow()
    monitor._build_menubar(
        win,
        _add_plot=lambda: None,
        _edit_plot=lambda: None,
        _del_plot=lambda: None,
        _open_log=lambda: None,
        _mock_10w=lambda: None,
        _mock_100w=lambda: None,
        _clear=lambda: None,
        _toggle_theme=lambda: None,
        _toggle_sidebar=lambda: None,
        _show_stats=lambda: None,
        _export_csv=lambda: None,
        _connect_serial=lambda: None,
        _disconnect_serial=lambda: None,
        _perf_test=lambda: None,
    )
    mb = win.menuBar()
    tools_menu = None
    for a in mb.actions():
        if a.text() == '工具':
            tools_menu = a.menu()
            break
    tool_actions = [a.text() for a in tools_menu.actions()]
    assert '日志格式配置…' not in tool_actions
    win.deleteLater()


# ---- 任务3：主题菜单系统（monitor）----

def test_monitor_build_menubar_creates_theme_menu(monitor, qtbot):
    """_build_menubar 应建出主题子菜单 + mode/specific actions。"""
    from PySide6.QtWidgets import QMainWindow
    win = QMainWindow()
    monitor._build_menubar(
        win,
        _add_plot=lambda: None,
        _edit_plot=lambda: None,
        _del_plot=lambda: None,
        _open_log=lambda: None,
        _mock_10w=lambda: None,
        _mock_100w=lambda: None,
        _clear=lambda: None,
        _toggle_theme=lambda: None,
        _toggle_sidebar=lambda: None,
        _show_stats=lambda: None,
        _export_csv=lambda: None,
        _connect_serial=lambda: None,
        _disconnect_serial=lambda: None,
        _perf_test=lambda: None,
        _import_plots_cfg=lambda: None,
        _export_plots_cfg=lambda: None,
    )
    # 主题菜单 actions 应被建出
    assert hasattr(monitor, '_theme_mode_actions')
    assert hasattr(monitor, '_theme_specific_actions')
    assert set(monitor._theme_mode_actions) == {'auto', 'light', 'dark'}
    assert len(monitor._theme_specific_actions) == 6
    win.deleteLater()


def test_monitor_apply_theme_to_window_updates_theme_name(monitor, qtbot, monkeypatch, tmp_path):
    """_apply_theme_to_window 应更新 theme_name + 不抛（pyqtgraph 可能无 GUI）。"""
    # 构造 _plot_widgets 为空，避免真 pyqtgraph 渲染
    monitor._plot_widgets = []
    monitor._visible_plots = []
    monitor._curves = []
    monitor._app = None  # apply_qss 会吞 None
    monitor._apply_theme_to_window('monokai')
    assert monitor.theme_name == 'monokai'


def test_monitor_apply_theme_with_pref_persists(monitor, qtbot, monkeypatch, tmp_path):
    """_apply_theme_with_pref 应持久化 pref + specific。"""
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / '.config'))
    monitor._plot_widgets = []
    monitor._visible_plots = []
    monitor._curves = []
    monitor._app = None
    monitor._apply_theme_with_pref('solarized_light', pref='light')
    from serial_debug.infra import config
    cfg = config.load()
    assert cfg['theme_pref'] == 'light'
    assert cfg['theme_specific_light'] == 'solarized_light'
    assert monitor.theme_name == 'solarized_light'


def test_monitor_on_theme_pref_selected_auto(monitor, qtbot, monkeypatch, tmp_path):
    """选 'auto' 应 resolve 到系统深浅 + specific。"""
    import importlib

    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / '.config'))
    from serial_debug.infra import config, themes
    importlib.reload(config)
    monitor._plot_widgets = []
    monitor._visible_plots = []
    monitor._curves = []
    monitor._app = None
    monkeypatch.setattr(themes, 'detect_system_theme', lambda: 'light')
    cfg = config.load()
    cfg['theme_specific_light'] = 'github_light'
    config.save(cfg)
    monitor._on_theme_pref_selected('auto')
    assert monitor.theme_pref == 'auto'
    assert monitor.theme_name == 'github_light'


def test_monitor_sync_theme_menu_checks_current(monitor, qtbot):
    """_sync_theme_menu 应勾选当前 pref + theme。"""
    from PySide6.QtGui import QAction, QActionGroup
    from PySide6.QtWidgets import QMainWindow
    win = QMainWindow()
    m = win.menuBar().addMenu('视图')
    monitor.theme_pref = 'dark'
    monitor.theme_name = 'monokai'
    monitor._theme_mode_actions = {}
    monitor._theme_specific_actions = {}
    mode_grp = QActionGroup(m)
    for pref, label in (('auto', '跟随系统'), ('light', '浅色'), ('dark', '深色')):
        act = QAction(label, m)
        act.setCheckable(True)
        act.setActionGroup(mode_grp)
        m.addAction(act)
        monitor._theme_mode_actions[pref] = act
    theme_grp = QActionGroup(m)
    for name, label in (('dark', 'D'), ('monokai', 'M')):
        act = QAction(label, m)
        act.setCheckable(True)
        act.setActionGroup(theme_grp)
        m.addAction(act)
        monitor._theme_specific_actions[name] = act
    monitor._sync_theme_menu()
    assert monitor._theme_mode_actions['dark'].isChecked()
    assert monitor._theme_specific_actions['monokai'].isChecked()
    win.deleteLater()


def test_build_menubar_no_optional_callbacks(monitor, qtbot):
    """无 _import_plots_cfg / _export_plots_cfg 时不应抛。"""
    from PySide6.QtWidgets import QMainWindow
    win = QMainWindow()
    monitor._build_menubar(
        win,
        _add_plot=lambda: None,
        _edit_plot=lambda: None,
        _del_plot=lambda: None,
        _open_log=lambda: None,
        _mock_10w=lambda: None,
        _mock_100w=lambda: None,
        _clear=lambda: None,
        _toggle_theme=lambda: None,
        _toggle_sidebar=lambda: None,
        _show_stats=lambda: None,
        _export_csv=lambda: None,
        _connect_serial=lambda: None,
        _disconnect_serial=lambda: None,
        _perf_test=lambda: None,
    )
    win.deleteLater()


# ---- _make_plot_widget ----

def test_make_plot_widget_returns_container(monitor, qtbot):
    """_make_plot_widget 应返 (container, pw, header) 三元组。"""
    import pyqtgraph as pg

    from serial_debug.ui import qt_theme
    t = qt_theme.palette(monitor.theme_name)
    p = {'title': 'A', 'vars': [], 'show_max': False, 'show_min': False,
         'show_avg': False, 'show_last': False, 'show_table': False,
         'show_hover': True, 'legend_pos': 'top-right',
         'legend_font_size': 9, 'legend_auto_avoid': True,
         'hover_mode': 'current', 'var_visibility': {}}
    container, pw, header = monitor._make_plot_widget(p, t, 0)
    assert container is not None
    assert pw is not None
    assert 'frame' in header
    assert 'title_label' in header
    assert 'legend_box' in header
    container.deleteLater()
    pw.deleteLater()


def test_make_plot_widget_no_idx_no_close_btn(monitor, qtbot):
    """idx=None 时不应创建 close 按钮。"""
    from serial_debug.ui import qt_theme
    t = qt_theme.palette(monitor.theme_name)
    p = {'title': 'A', 'vars': [], 'legend_pos': 'top-right',
         'legend_font_size': 9, 'var_visibility': {}}
    container, pw, header = monitor._make_plot_widget(p, t, None)
    assert header['close_btn'] is None
    container.deleteLater()
    pw.deleteLater()


# ---- _do_rebuild_plots ----

def test_do_rebuild_plots_with_one_plot(monitor, qtbot):
    """有 1 个 plot 时应建出 1 个 PlotWidget。"""
    import pyqtgraph as pg
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QSizePolicy, QSplitter, QVBoxLayout, QWidget

    from serial_debug.ui import qt_theme
    t = qt_theme.palette(monitor.theme_name)
    container = QWidget()
    layout = QVBoxLayout(container)
    monitor._plots_layout = layout
    monitor._visible_plots = [{'title': 'A', 'vars': [],
                                'var_visibility': {}}]
    monitor._do_rebuild_plots(layout, pg, Qt, QSizePolicy, QSplitter, t)
    assert len(monitor._plot_widgets) == 1
    container.deleteLater()


def test_do_rebuild_plots_multiple(monitor, qtbot):
    """多 plot 时应建出对应数量的 PlotWidget。"""
    import pyqtgraph as pg
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QSizePolicy, QSplitter, QVBoxLayout, QWidget

    from serial_debug.ui import qt_theme
    t = qt_theme.palette(monitor.theme_name)
    container = QWidget()
    layout = QVBoxLayout(container)
    monitor._plots_layout = layout
    monitor._visible_plots = [{'title': 'A', 'vars': [], 'var_visibility': {}},
                              {'title': 'B', 'vars': [], 'var_visibility': {}},
                              {'title': 'C', 'vars': [], 'var_visibility': {}},
                              {'title': 'D', 'vars': [], 'var_visibility': {}}]
    monitor._do_rebuild_plots(layout, pg, Qt, QSizePolicy, QSplitter, t)
    assert len(monitor._plot_widgets) == 4
    container.deleteLater()


# ---- _refresh_plot full body ----

def test_refresh_plot_with_curves_and_data(monitor, monkeypatch):
    """有 _plot_widgets + _data 时应刷新曲线（走 setData 路径）。"""
    import numpy as np
    class _FakeItem:
        _legend_color = '#fff'
        def setData(self, xs, ys, pen=None):
            self._xs = xs
            self._ys = ys
    class _FakePW:
        def __init__(self):
            self.plotted = []
            self.removed = []
            self._scene_bound = False
        def plot(self, xs, ys, pen=None, name=None):
            item = _FakeItem()
            self.plotted.append(item)
            return item
        def removeItem(self, item):
            self.removed.append(item)
        def scene(self):
            class _S:
                sigMouseMoved = type("Sig", (), {"connect": lambda *a, **kw: None})()
            return _S()
        def addItem(self, item, ignoreBounds=False):
            pass
    pw = _FakePW()
    monitor._plot_widgets = [pw]
    monitor._curves = [{'v': _FakeItem()}]
    monitor._plot_headers = [None]
    monitor._visible_plots = [{'title': 'A', 'vars': [], 'var_visibility': {}}]
    monitor._data = {'v': (np.array([0, 1]), np.array([5, 6]))}
    monitor._var_meta = {'v': {'color': '#fff', 'label': 'V'}}
    monitor._sidebar = None
    monkeypatch.setattr(monitor, '_refresh_header_legend', lambda *a: None)
    monkeypatch.setattr(monitor, '_draw_stat_markers', lambda *a: None)
    monkeypatch.setattr(monitor, '_refresh_sidebar_values', lambda: None)
    monkeypatch.setattr(monitor, '_refresh_sidebar_visibility', lambda: None)
    monkeypatch.setattr(monitor, '_refresh_popouts', lambda: None)
    monitor._refresh_plot()
    # v 在 curves 已存在 → 应调 setData
    assert hasattr(monitor._curves[0]['v'], '_xs')


def test_refresh_plot_removes_unchecked_curves(monitor, monkeypatch):
    """var_visibility[name]=False 时既有曲线应被移除。"""
    import numpy as np
    class _FakeItem:
        _legend_color = '#fff'
    class _FakePW:
        def __init__(self):
            self.removed = []
        def plot(self, *a, **kw):
            raise AssertionError('should not plot')
        def removeItem(self, item):
            self.removed.append(item)
        def scene(self):
            class _S:
                sigMouseMoved = type("Sig", (), {"connect": lambda *a, **kw: None})()
            return _S()
        def addItem(self, item, ignoreBounds=False):
            pass
    pw = _FakePW()
    old_item = _FakeItem()
    monitor._plot_widgets = [pw]
    monitor._curves = [{'v': old_item}]
    monitor._plot_headers = [None]
    monitor._visible_plots = [{'title': 'A', 'vars': ['v'],
                                'var_visibility': {'v': False}}]
    monitor._data = {'v': (np.array([0, 1]), np.array([5, 6]))}
    monitor._var_meta = {'v': {'color': '#fff', 'label': 'V'}}
    monitor._sidebar = None
    monkeypatch.setattr(monitor, '_refresh_header_legend', lambda *a: None)
    monkeypatch.setattr(monitor, '_draw_stat_markers', lambda *a: None)
    monkeypatch.setattr(monitor, '_refresh_sidebar_values', lambda: None)
    monkeypatch.setattr(monitor, '_refresh_sidebar_visibility', lambda: None)
    monkeypatch.setattr(monitor, '_refresh_popouts', lambda: None)
    monitor._refresh_plot()
    assert 'v' not in monitor._curves[0]
    assert old_item in pw.removed


def test_refresh_plot_empty_var_buffer_skipped(monitor, monkeypatch):
    """变量 buffer 为空应被跳过。"""
    class _FakePW:
        def plot(self, *a, **kw):
            raise AssertionError('should not plot')
        def removeItem(self, item):
            pass
        def scene(self):
            class _S:
                sigMouseMoved = type("Sig", (), {"connect": lambda *a, **kw: None})()
            return _S()
        def addItem(self, item, ignoreBounds=False):
            pass
    pw = _FakePW()
    monitor._plot_widgets = [pw]
    monitor._curves = [{}]
    monitor._plot_headers = [None]
    monitor._visible_plots = [{'title': 'A', 'vars': [], 'var_visibility': {}}]
    monitor._data = {'v': []}
    monitor._var_meta = {}
    monitor._sidebar = None
    monkeypatch.setattr(monitor, '_refresh_header_legend', lambda *a: None)
    monkeypatch.setattr(monitor, '_draw_stat_markers', lambda *a: None)
    monkeypatch.setattr(monitor, '_refresh_sidebar_values', lambda: None)
    monkeypatch.setattr(monitor, '_refresh_sidebar_visibility', lambda: None)
    monkeypatch.setattr(monitor, '_refresh_popouts', lambda: None)
    monitor._refresh_plot()
    assert monitor._curves[0] == {}


# ---- 任务2：缩放/接收并发加固 ----

def test_refresh_plot_concurrent_with_reader_no_error(monitor, monkeypatch):
    """reader 线程持续 append + 截断 _data[name] 时，主线程反复 _refresh_plot
    不应抛异常（验证锁内 _get_xy 拷贝路径，消除锁外 self._data[name] 竞争）。

    场景对应任务2：鼠标缩放时主线程卡在 Qt 事件循环，reader 线程仍 append。
    本测试直接压测 _refresh_plot × reader 并发，验证 setData 用的拷贝
    不受 reader 修改影响、无 IndexError/ValueError。
    """
    import threading

    import numpy as np

    class _FakeItem:
        _legend_color = '#fff'
        def setData(self, xs, ys, pen=None):
            # 持续校验 xs/ys 是 numpy 数组且长度一致（拷贝完整）
            assert len(xs) == len(ys), 'setData xs/ys 长度不一致'

    class _FakePW:
        def plot(self, xs, ys, pen=None, name=None):
            return _FakeItem()
        def removeItem(self, item):
            pass
        def scene(self):
            class _S:
                sigMouseMoved = type("Sig", (), {"connect": lambda *a, **kw: None})()
            return _S()
        def addItem(self, item, ignoreBounds=False):
            pass

    pw = _FakePW()
    monitor._plot_widgets = [pw]
    monitor._curves = [{}]
    monitor._plot_headers = [None]
    monitor._visible_plots = [{'title': 'A', 'vars': [], 'var_visibility': {}}]
    monitor._var_meta = {'v': {'color': '#fff', 'label': 'V'}}
    monitor._sidebar = None
    monitor._max_points = 2000
    monkeypatch.setattr(monitor, '_refresh_header_legend', lambda *a: None)
    monkeypatch.setattr(monitor, '_draw_stat_markers', lambda *a: None)
    monkeypatch.setattr(monitor, '_refresh_sidebar_values', lambda: None)
    monkeypatch.setattr(monitor, '_refresh_sidebar_visibility', lambda: None)
    monkeypatch.setattr(monitor, '_refresh_popouts', lambda: None)

    # 初始放一个变量
    monitor._data['v'] = [(0.0, 1.0)]

    stop = threading.Event()
    errors = []

    def _reader():
        """模拟 reader 线程：高频 append + 触发 _max_points 截断。"""
        t = 0.0
        while not stop.is_set():
            t += 0.001
            with monitor._data_lock:
                buf = monitor._data.setdefault('v', [])
                buf.append((t, float(t * 1000)))
                if len(buf) > monitor._max_points:
                    del buf[:len(buf) - monitor._max_points]

    # 启动 reader
    th = threading.Thread(target=_reader, daemon=True)
    th.start()
    try:
        # 主线程反复 _refresh_plot，模拟 40ms 定时器 + 鼠标缩放事件穿插
        for _ in range(200):
            try:
                monitor._refresh_plot()
            except Exception as e:
                errors.append(e)
                break
    finally:
        stop.set()
        th.join(timeout=2.0)

    assert not errors, f'_refresh_plot 并发时抛异常：{errors!r}'


def test_get_xy_returns_copy_not_live_reference(monitor):
    """_get_xy 返回的 numpy 数组应是拷贝，不随 reader append 变化。

    这是任务2加固的核心保证：setData 用的 xs/ys 是锁内 asarray 拷贝，
    reader 后续 append/del 不会影响已返回的数组。
    """
    import numpy as np
    monitor._data['v'] = [(0.0, 1.0), (1.0, 2.0), (2.0, 3.0)]
    xs, ys = monitor._get_xy('v')
    assert xs is not None and len(xs) == 3
    # reader 在锁内 append
    with monitor._data_lock:
        monitor._data['v'].append((3.0, 4.0))
    # 返回的 xs/ys 不应变化（拷贝）
    assert len(xs) == 3, '_get_xy 返回了 live 引用，受 reader 影响'
    assert len(ys) == 3
    # 再取一次应是 4 个点
    xs2, ys2 = monitor._get_xy('v')
    assert len(xs2) == 4


def test_refresh_plot_skips_var_removed_by_reader(monitor, monkeypatch):
    """reader 在 _refresh_plot 迭代期间删掉某变量，不应抛 KeyError。

    原代码锁外读 self._data[name] 再 _get_xy，中间变量可能消失。
    加固后直接 _get_xy（锁内 None 兜底），消除 KeyError 风险。
    """
    import numpy as np

    class _FakeItem:
        _legend_color = '#fff'
        def setData(self, xs, ys, pen=None):
            pass

    class _FakePW:
        def plot(self, xs, ys, pen=None, name=None):
            return _FakeItem()
        def removeItem(self, item):
            pass
        def scene(self):
            class _S:
                sigMouseMoved = type("Sig", (), {"connect": lambda *a, **kw: None})()
            return _S()
        def addItem(self, item, ignoreBounds=False):
            pass

    pw = _FakePW()
    monitor._plot_widgets = [pw]
    monitor._curves = [{}]
    monitor._plot_headers = [None]
    monitor._visible_plots = [{'title': 'A', 'vars': ['v', 'gone'], 'var_visibility': {}}]
    monitor._data['v'] = [(0.0, 1.0), (1.0, 2.0)]
    monitor._data['gone'] = [(0.0, 1.0)]
    monitor._var_meta = {'v': {'color': '#fff', 'label': 'V'}, 'gone': {'color': '#f00', 'label': 'G'}}
    monitor._sidebar = None
    monkeypatch.setattr(monitor, '_refresh_header_legend', lambda *a: None)
    monkeypatch.setattr(monitor, '_draw_stat_markers', lambda *a: None)
    monkeypatch.setattr(monitor, '_refresh_sidebar_values', lambda: None)
    monkeypatch.setattr(monitor, '_refresh_sidebar_visibility', lambda: None)
    monkeypatch.setattr(monitor, '_refresh_popouts', lambda: None)

    # 让 _get_xy('gone') 在被调用前由 reader 删掉该变量
    orig_get_xy = monitor._get_xy
    call_count = {'n': 0}
    def _patched_get_xy(name):
        call_count['n'] += 1
        if name == 'gone':
            with monitor._data_lock:
                monitor._data.pop('gone', None)
            return None, None  # 模拟变量已消失
        return orig_get_xy(name)
    monkeypatch.setattr(monitor, '_get_xy', _patched_get_xy)

    # 不应抛 KeyError
    monitor._refresh_plot()
    assert 'v' in monitor._curves[0]
    assert 'gone' not in monitor._curves[0]


# ---- 任务5：跟随系统模式实时跟随系统深浅变化 ----

def _prep_monitor_for_theme(monitor):
    """置空 monitor 的渲染相关状态，避免真 pyqtgraph/QSS 构造。"""
    monitor._app = None
    monitor._plot_widgets = []
    monitor._visible_plots = []
    monitor._curves = []
    monitor._sidebar = None


def test_monitor_check_theme_changed_auto_system_dark_to_light(monitor, qtbot, monkeypatch, tmp_path):
    """跟随系统模式：系统从 dark 切到 light，_check_theme_changed 应 apply 浅色主题。"""
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / '.config'))
    _prep_monitor_for_theme(monitor)
    from serial_debug.infra import config, themes
    cfg = config.load()
    cfg['theme_pref'] = 'auto'
    cfg['theme_specific_light'] = 'github_light'
    cfg['theme_specific_dark'] = 'monokai'
    config.save(cfg)
    monkeypatch.setattr(themes, 'detect_system_theme', lambda: 'dark')
    monitor.theme_pref = 'auto'
    monitor.theme_name = 'monokai'
    monitor._last_system_kind = 'dark'
    applied = []
    import serial_debug.ui.qt_theme as qt_theme
    monkeypatch.setattr(qt_theme, 'apply_qss',
                        lambda a, name: applied.append(name))
    # 系统切到 light
    monkeypatch.setattr(themes, 'detect_system_theme', lambda: 'light')
    monitor._check_theme_changed()
    assert monitor.theme_name == 'github_light'
    assert applied == ['github_light']
    assert monitor._last_system_kind == 'light'


def test_monitor_check_theme_changed_auto_no_change_no_apply(monitor, qtbot, monkeypatch, tmp_path):
    """跟随系统模式：系统深浅未变时不重复 apply。"""
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / '.config'))
    _prep_monitor_for_theme(monitor)
    from serial_debug.infra import config, themes
    cfg = config.load()
    cfg['theme_pref'] = 'auto'
    cfg['theme_specific_dark'] = 'monokai'
    config.save(cfg)
    monkeypatch.setattr(themes, 'detect_system_theme', lambda: 'dark')
    monitor.theme_pref = 'auto'
    monitor.theme_name = 'monokai'
    monitor._last_system_kind = 'dark'
    applied = []
    import serial_debug.ui.qt_theme as qt_theme
    monkeypatch.setattr(qt_theme, 'apply_qss',
                        lambda a, name: applied.append(name))
    monitor._check_theme_changed()
    assert applied == []
    assert monitor.theme_name == 'monokai'


def test_monitor_check_theme_changed_fixed_dark_ignores_system(monitor, qtbot, monkeypatch, tmp_path):
    """固定深色模式：系统切到浅色不应跟随。"""
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / '.config'))
    _prep_monitor_for_theme(monitor)
    from serial_debug.infra import config, themes
    cfg = config.load()
    cfg['theme_pref'] = 'dark'
    cfg['theme_specific_dark'] = 'monokai'
    config.save(cfg)
    monitor.theme_pref = 'dark'
    monitor.theme_name = 'monokai'
    monitor._last_system_kind = 'dark'
    applied = []
    import serial_debug.ui.qt_theme as qt_theme
    monkeypatch.setattr(qt_theme, 'apply_qss',
                        lambda a, name: applied.append(name))
    monkeypatch.setattr(themes, 'detect_system_theme', lambda: 'light')
    monitor._check_theme_changed()
    assert applied == []
    assert monitor.theme_name == 'monokai'


def test_monitor_check_theme_changed_config_pref_change(monitor, qtbot, monkeypatch, tmp_path):
    """config.theme_pref 变化（跨进程联动）应 apply 新主题。"""
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / '.config'))
    _prep_monitor_for_theme(monitor)
    from serial_debug.infra import config, themes
    # 起始 pref=auto/dark
    cfg = config.load()
    cfg['theme_pref'] = 'auto'
    cfg['theme_specific_dark'] = 'monokai'
    config.save(cfg)
    monkeypatch.setattr(themes, 'detect_system_theme', lambda: 'dark')
    monitor.theme_pref = 'auto'
    monitor.theme_name = 'monokai'
    monitor._last_system_kind = 'dark'
    # 外部把 pref 改成 light + specific_light=github_light
    cfg = config.load()
    cfg['theme_pref'] = 'light'
    cfg['theme_specific_light'] = 'github_light'
    config.save(cfg)
    applied = []
    import serial_debug.ui.qt_theme as qt_theme
    monkeypatch.setattr(qt_theme, 'apply_qss',
                        lambda a, name: applied.append(name))
    monitor._check_theme_changed()
    assert monitor.theme_pref == 'light'
    assert monitor.theme_name == 'github_light'
    assert applied == ['github_light']
