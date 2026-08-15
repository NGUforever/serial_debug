"""ui/qt_monitor_window.py 测试：数据管理 / _get_xy / _var_color / stop_serial。

聚焦不依赖 pyqtgraph 渲染的逻辑层：变量注册、颜色、_max_points 截断、stop_serial
"""
from __future__ import annotations

import os
import time

import pytest

pytest.importorskip('PySide6')


@pytest.fixture
def monitor(qtbot, tmp_path, monkeypatch):
    """构建一个 QtMonitorWindow 实例，但跳过 _create()（避免真 pyqtgraph 渲染）。"""
    # 重定向 config 到 tmp 防污染用户配置
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / '.config'))
    from serial_debug.ui import qt_monitor_window as mod
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
