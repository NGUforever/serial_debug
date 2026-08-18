"""Smoke tests for serial_debug.app entry routing.

v1.1.0 移除 tkinter 兼容路径后，app.py 收缩为纯入口路由：
  - SERIAL_DEBUG_QT_PILOT=1   → QtMonitorWindow.run()
  - SERIAL_DEBUG_LOG_VIEWER=1 → _run_log_viewer_only([])
  - --view F1 [...]           → _run_log_viewer_only([F1, ...])
  - 默认                       → QtSerialDebugApp.run()

测试用 monkeypatch 替换目标模块避免真实 Qt 初始化。
"""
from __future__ import annotations

import os
import sys
from unittest import mock

import pytest


@pytest.fixture
def app_mod(monkeypatch):
    """Reload serial_debug.app 以保证环境变量改动生效。"""
    import importlib

    import serial_debug.app as mod
    importlib.reload(mod)
    return mod


def _clear_env(monkeypatch):
    """清空所有 SERIAL_DEBUG_* 环境变量。"""
    for k in ('SERIAL_DEBUG_QT_PILOT', 'SERIAL_DEBUG_LOG_VIEWER',
              'SERIAL_DEBUG_QT_MAIN', 'SERIAL_DEBUG_TK'):
        monkeypatch.delenv(k, raising=False)


def test_main_routes_to_qt_pilot_when_env_set(app_mod, monkeypatch):
    """SERIAL_DEBUG_QT_PILOT=1 应走 QtMonitorWindow.run()。"""
    _clear_env(monkeypatch)
    monkeypatch.setenv('SERIAL_DEBUG_QT_PILOT', '1')
    fake_win = mock.MagicMock()
    fake_win.run.return_value = 0
    fake_cls = mock.MagicMock(return_value=fake_win)
    monkeypatch.setitem(sys.modules, 'serial_debug.ui.qt_monitor_window',
                        mock.MagicMock(QtMonitorWindow=fake_cls))
    with pytest.raises(SystemExit) as exc:
        app_mod.main()
    assert exc.value.code == 0
    fake_cls.assert_called_once()
    fake_win.run.assert_called_once()


def test_main_routes_to_log_viewer_when_env_set(app_mod, monkeypatch):
    """SERIAL_DEBUG_LOG_VIEWER=1 应走 _run_log_viewer_only([])。"""
    _clear_env(monkeypatch)
    monkeypatch.setenv('SERIAL_DEBUG_LOG_VIEWER', '1')
    called = {'n': 0, 'files': None}

    def fake_run(files):
        called['n'] += 1
        called['files'] = list(files)
        return None

    monkeypatch.setattr(app_mod, '_run_log_viewer_only', fake_run)
    app_mod.main()
    assert called['n'] == 1
    assert called['files'] == []


def test_main_view_flag_routes_to_log_viewer_with_files(app_mod, monkeypatch):
    """`--view F1 F2` 应走 _run_log_viewer_only(['F1', 'F2'])。"""
    _clear_env(monkeypatch)
    monkeypatch.setattr(sys, 'argv', ['serial_debug', '--view', 'a.log', 'b.log'])
    called = {'files': None}

    def fake_run(files):
        called['files'] = list(files)
        return None

    monkeypatch.setattr(app_mod, '_run_log_viewer_only', fake_run)
    app_mod.main()
    assert called['files'] == ['a.log', 'b.log']


def test_main_default_routes_to_qt_app(app_mod, monkeypatch):
    """无环境变量且无 --view，默认走 QtSerialDebugApp.run()。"""
    _clear_env(monkeypatch)
    monkeypatch.setattr(sys, 'argv', ['serial_debug'])
    fake_app = mock.MagicMock()
    fake_app.run.return_value = 0
    fake_cls = mock.MagicMock(return_value=fake_app)
    monkeypatch.setitem(sys.modules, 'serial_debug.ui.qt_app',
                        mock.MagicMock(QtSerialDebugApp=fake_cls))
    with pytest.raises(SystemExit) as exc:
        app_mod.main()
    assert exc.value.code == 0
    fake_cls.assert_called_once()
    fake_app.run.assert_called_once()


def test_main_pilot_failure_exits_nonzero(app_mod, monkeypatch):
    """Qt pilot 路径启动抛异常应 sys.exit(1)。"""
    _clear_env(monkeypatch)
    monkeypatch.setenv('SERIAL_DEBUG_QT_PILOT', '1')
    fake_cls = mock.MagicMock(side_effect=RuntimeError('boom'))
    monkeypatch.setitem(sys.modules, 'serial_debug.ui.qt_monitor_window',
                        mock.MagicMock(QtMonitorWindow=fake_cls))
    # QApplication 也不可用——走双 except，最终 sys.exit(1)
    monkeypatch.setitem(sys.modules, 'PySide6.QtWidgets',
                        mock.MagicMock(QApplication=mock.MagicMock(side_effect=ImportError),
                                       QMessageBox=mock.MagicMock()))
    with pytest.raises(SystemExit) as exc:
        app_mod.main()
    assert exc.value.code == 1


def test_resource_path_uses_meipass_when_frozen(app_mod, monkeypatch):
    """PyInstaller 打包后 sys._MEIPASS 指向解压目录，resource_path 应优先用它。"""
    monkeypatch.setattr(sys, '_MEIPASS', '/tmp/fake_meipass', raising=False)
    p = app_mod.resource_path('assets/icon.png')
    assert p == os.path.join('/tmp/fake_meipass', 'serial_debug', 'resources',
                             'assets', 'icon.png')


def test_resource_path_falls_back_to_importlib(app_mod, monkeypatch):
    """非冻结模式（无 _MEIPASS）走 importlib.resources 解析。"""
    if hasattr(sys, '_MEIPASS'):
        monkeypatch.delattr(sys, '_MEIPASS', raising=False)
    p = app_mod.resource_path('common_at.json')
    assert p.endswith('common_at.json')
