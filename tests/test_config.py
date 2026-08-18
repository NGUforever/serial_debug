"""infra/config.py 读写测试。

用 XDG_CONFIG_HOME 重定向到 tmp_path，避免污染用户配置。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


def _reload_config_with_xdg(xdg: str):
    """config 在 import 时算出 CONFIG_DIR，需 reload 让新 XDG 生效。"""
    import importlib
    os.environ['XDG_CONFIG_HOME'] = xdg
    from serial_debug.infra import config as config_mod
    importlib.reload(config_mod)
    return config_mod


@pytest.fixture
def cfg_module(tmp_path, monkeypatch):
    xdg = tmp_path / 'xdg'
    xdg.mkdir()
    monkeypatch.setenv('XDG_CONFIG_HOME', str(xdg))
    return _reload_config_with_xdg(str(xdg))


def test_config_dir_under_xdg(cfg_module, tmp_path):
    assert cfg_module.CONFIG_DIR.startswith(str(tmp_path / 'xdg'))
    assert cfg_module.CONFIG_DIR.endswith('serial_debug')


def test_load_returns_defaults_when_missing(cfg_module):
    cfg = cfg_module.load()
    # 抽查几个 DEFAULTS 字段
    assert cfg['baudrate'] == 115200
    assert cfg['append_crlf'] is False
    assert cfg['auto_scroll'] is True
    assert cfg['theme_pref'] == 'auto'
    assert cfg['at_commands'] == []
    assert cfg['at_window_mode'] == 'detached'


def test_save_then_load_roundtrip(cfg_module):
    cfg = cfg_module.load()
    cfg['baudrate'] = 9600
    cfg['append_crlf'] = True
    cfg['theme_pref'] = 'dark'
    cfg['at_commands'] = [
        {'cmd': 'AT+RST', 'desc': 'reset', 'seq': 1, 'period_ms': 100},
    ]
    cfg_module.save(cfg)

    # 文件确实生成
    assert os.path.isfile(cfg_module.CONFIG_FILE)

    # reload 读回
    cfg2 = cfg_module.load()
    assert cfg2['baudrate'] == 9600
    assert cfg2['append_crlf'] is True
    assert cfg2['theme_pref'] == 'dark'
    assert cfg2['at_commands'] == [
        {'cmd': 'AT+RST', 'desc': 'reset', 'seq': 1, 'period_ms': 100},
    ]


def test_save_filters_unknown_keys(cfg_module):
    """save() 只写 DEFAULTS 里已有的字段，脏字段被丢弃。"""
    cfg = cfg_module.load()
    cfg['baudrate'] = 115200
    cfg['__secret__'] = 'should-not-persist'
    cfg_module.save(cfg)

    with open(cfg_module.CONFIG_FILE, 'r', encoding='utf-8') as f:
        raw = json.load(f)
    assert '__secret__' not in raw
    assert 'baudrate' in raw


def test_save_is_atomic_no_tmp_leftover(cfg_module):
    """save() 用 tmp + rename，不应残留 .tmp 文件。"""
    cfg_module.save(cfg_module.load())
    assert not os.path.isfile(cfg_module.CONFIG_FILE + '.tmp')


def test_load_tolerates_corrupt_file(cfg_module):
    """读失败时静默兜底 DEFAULTS。"""
    os.makedirs(cfg_module.CONFIG_DIR, exist_ok=True)
    with open(cfg_module.CONFIG_FILE, 'w', encoding='utf-8') as f:
        f.write('{not valid json')
    cfg = cfg_module.load()
    assert cfg['baudrate'] == 115200  # 退回 DEFAULTS


def test_load_skips_non_dict_payload(cfg_module):
    """读到的 JSON 不是 dict（如 list/str/int）时整体退回 DEFAULTS。"""
    os.makedirs(cfg_module.CONFIG_DIR, exist_ok=True)
    with open(cfg_module.CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(['not', 'a', 'dict'], f)
    cfg = cfg_module.load()
    assert cfg['baudrate'] == 115200


def test_load_skips_unknown_keys(cfg_module):
    """读到 dict 但含 DEFAULTS 未登记字段时，未知字段被丢弃，已知字段保留。"""
    os.makedirs(cfg_module.CONFIG_DIR, exist_ok=True)
    with open(cfg_module.CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump({'baudrate': 19200, '__unknown__': 'x'}, f)
    cfg = cfg_module.load()
    assert cfg['baudrate'] == 19200
    assert '__unknown__' not in cfg


def test_config_dir_windows(tmp_path, monkeypatch):
    """Windows 平台应走 %LOCALAPPDATA%/serial_debug。"""
    local = tmp_path / 'Local'
    monkeypatch.setenv('LOCALAPPDATA', str(local))
    monkeypatch.setattr('sys.platform', 'win32')
    import importlib
    from serial_debug.infra import config as config_mod
    importlib.reload(config_mod)
    expected = str(local / 'serial_debug')
    assert config_mod.CONFIG_DIR == expected


def test_config_dir_darwin(tmp_path, monkeypatch):
    """macOS 平台应走 ~/Library/Application Support/serial_debug。"""
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setattr('sys.platform', 'darwin')
    import importlib
    from serial_debug.infra import config as config_mod
    importlib.reload(config_mod)
    expected = str(tmp_path / 'Library' / 'Application Support' / 'serial_debug')
    assert config_mod.CONFIG_DIR == expected


def test_save_silently_ignores_failure(cfg_module, monkeypatch):
    """save() 内部异常（如 makedirs 权限拒）应被吞掉，不抛给调用方。"""
    def _raise(*a, **k):
        raise OSError('mocked permission denied')
    monkeypatch.setattr('os.makedirs', _raise)
    # 不应抛
    cfg_module.save({'baudrate': 115200})


def test_config_dir_windows_falls_back_to_appdata_local(tmp_path, monkeypatch):
    """Windows 无 LOCALAPPDATA 时回退到 ~/AppData/Local。"""
    monkeypatch.delenv('LOCALAPPDATA', raising=False)
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setattr('sys.platform', 'win32')
    import importlib
    from serial_debug.infra import config as config_mod
    importlib.reload(config_mod)
    expected = str(tmp_path / 'AppData' / 'Local' / 'serial_debug')
    assert config_mod.CONFIG_DIR == expected
