"""infra/themes.py 测试：颜色解析 / detect / load / save / 字体管理。

不依赖真实 X server——themes 模块只在 apply_theme 时才用 tk.Tk()。
"""
from __future__ import annotations

import os
import sys
from unittest import mock

import pytest


@pytest.fixture
def themes_mod():
    from serial_debug.infra import themes
    # 重置全局字体大小（其他测试可能改过）
    themes._font_size = themes.DEFAULT_FONT_SIZE
    return themes


# ---- THEMES 字典 ----

def test_themes_have_dark_and_light(themes_mod):
    """应有 dark + light 两套主题。"""
    assert 'dark' in themes_mod.THEMES
    assert 'light' in themes_mod.THEMES


def test_dark_theme_keys_complete(themes_mod):
    """dark 主题应含全部必要 key（bg/panel_bg/fg/cursor/select_bg/btn_*/status_*/...）。"""
    keys = themes_mod.THEMES['dark']
    expected = {'bg', 'panel_bg', 'panel_fg', 'fg', 'cursor', 'select_bg',
                'btn_bg', 'btn_hover', 'btn_active', 'btn_primary',
                'btn_primary_hover', 'status_ok', 'status_wait',
                'status_err', 'status_idle', 'ts', 'send', 'resp', 'note',
                'log', 'muted', 'accent', 'border'}
    assert expected <= set(keys.keys())


def test_light_theme_keys_complete(themes_mod):
    """light 主题同样应含全部必要 key。"""
    keys = themes_mod.THEMES['light']
    expected = {'bg', 'panel_bg', 'panel_fg', 'fg', 'cursor', 'select_bg',
                'btn_bg', 'btn_hover', 'btn_active', 'btn_primary',
                'status_ok', 'status_wait', 'status_err', 'status_idle',
                'ts', 'send', 'resp', 'note', 'log', 'muted', 'accent', 'border'}
    assert expected <= set(keys.keys())


def test_dark_and_light_differ(themes_mod):
    """dark / light 配色不同（至少 bg 不同）。"""
    assert themes_mod.THEMES['dark']['bg'] != themes_mod.THEMES['light']['bg']


# ---- resolve(pref) ----

def test_resolve_light_returns_light(themes_mod):
    assert themes_mod.resolve('light') == 'light'


def test_resolve_dark_returns_dark(themes_mod):
    assert themes_mod.resolve('dark') == 'dark'


def test_resolve_auto_uses_system_theme(themes_mod, monkeypatch):
    """resolve('auto') 调 detect_system_theme 兜底。"""
    monkeypatch.setattr(themes_mod, 'detect_system_theme', lambda: 'light')
    assert themes_mod.resolve('auto') == 'light'


def test_resolve_unknown_pref_falls_back_to_system(themes_mod, monkeypatch):
    """未知 pref 应兜底到 detect_system_theme。"""
    monkeypatch.setattr(themes_mod, 'detect_system_theme', lambda: 'dark')
    assert themes_mod.resolve('xyz') == 'dark'


# ---- detect_system_theme ----

def test_detect_system_theme_returns_dark_or_light(themes_mod):
    """detect 应返回 'dark' 或 'light'（不抛异常）。"""
    r = themes_mod.detect_system_theme()
    assert r in ('dark', 'light')


def test_detect_system_theme_subprocess_failure_returns_dark(themes_mod, monkeypatch):
    """subprocess 调用失败应兜底 dark。"""
    def fake_run(*a, **kw):
        raise FileNotFoundError('no gsettings/defaults/reg')
    monkeypatch.setattr(themes_mod.subprocess, 'run', fake_run)
    r = themes_mod.detect_system_theme()
    assert r == 'dark'


# ---- 字体管理 ----

def test_font_size_get_set(themes_mod):
    """get/set 字体大小，set 应 clamp 到 [8, 20]。"""
    orig = themes_mod.get_font_size()
    themes_mod.set_font_size(14)
    assert themes_mod.get_font_size() == 14
    # 太小 → 8
    themes_mod.set_font_size(1)
    assert themes_mod.get_font_size() == 8
    # 太大 → 20
    themes_mod.set_font_size(999)
    assert themes_mod.get_font_size() == 20
    # 还原
    themes_mod.set_font_size(orig)


def test_font_returns_tuple(themes_mod):
    """_font() / _mono() 返回 (family, size[, weight]) tuple。"""
    f = themes_mod._font()
    assert isinstance(f, tuple)
    assert len(f) >= 2
    m = themes_mod._mono()
    assert isinstance(m, tuple)


def test_platform_specific_font_family(themes_mod):
    """不同平台应有不同 font family 常量。"""
    if sys.platform == 'win32':
        assert themes_mod.FONT_FAMILY == 'Segoe UI'
    elif sys.platform == 'darwin':
        assert themes_mod.FONT_FAMILY == 'Helvetica Neue'
    else:
        assert themes_mod.FONT_FAMILY == 'DejaVu Sans'


# ---- load_pref / save_pref ----

def test_load_pref_returns_auto_when_config_missing(themes_mod, monkeypatch):
    """config 模块 import 失败时应返回 'auto'。"""
    # 让 config 模块不可 import
    import sys
    monkeypatch.setitem(sys.modules, 'config', None)
    r = themes_mod.load_pref()
    assert r == 'auto'


def test_save_pref_swallows_config_failure(themes_mod, monkeypatch):
    """save_pref 在 config 不可用时不应抛。"""
    import sys
    monkeypatch.setitem(sys.modules, 'config', None)
    # 不应抛
    themes_mod.save_pref('dark')


# ---- _config_dir 跨平台 ----

def test_config_dir_is_absolute_path(themes_mod):
    """_config_dir 应返回绝对路径。"""
    # CONFIG_DIR 在 import 时已固定，验证它是字符串绝对路径
    assert isinstance(themes_mod.CONFIG_DIR, str)
    assert os.path.isabs(themes_mod.CONFIG_DIR) or themes_mod.CONFIG_DIR.startswith('~')


# ---- _config_dir 平台分支 ----

def _reload_themes_with_platform(monkeypatch, platform):
    """用 monkeypatch sys.platform 后重载 themes 模块。"""
    import importlib
    monkeypatch.setattr('sys.platform', platform)
    # 必须清除已缓存模块强制重新执行 platform 分支
    import sys
    saved = sys.modules.pop('serial_debug.infra.themes', None)
    try:
        mod = importlib.import_module('serial_debug.infra.themes')
        return mod
    finally:
        # 不还原：测试间依赖也走同一份；下面 monkeypatch 会自动复原 sys.platform
        if saved is not None:
            sys.modules['serial_debug.infra.themes'] = saved


def test_config_dir_win32(monkeypatch):
    """win32 平台应取 LOCALAPPDATA/serial_debug。"""
    themes_mod = _reload_themes_with_platform(monkeypatch, 'win32')
    monkeypatch.setenv('LOCALAPPDATA', '/tmp/locapp')
    expected = os.path.join('/tmp/locapp', 'serial_debug')
    # 重新调用 _config_dir 因为 CONFIG_DIR 在 import 时已固定
    # 但 _config_dir 是模块级函数，可重新调
    actual = themes_mod._config_dir()
    assert actual == expected


def test_config_dir_darwin(monkeypatch):
    """darwin 平台应取 ~/Library/Application Support/serial_debug。"""
    themes_mod = _reload_themes_with_platform(monkeypatch, 'darwin')
    home = os.path.expanduser('~')
    expected = os.path.join(home, 'Library', 'Application Support', 'serial_debug')
    assert themes_mod._config_dir() == expected


def test_config_dir_linux(monkeypatch):
    """linux 平台应取 $XDG_CONFIG_HOME/serial_debug。"""
    themes_mod = _reload_themes_with_platform(monkeypatch, 'linux')
    monkeypatch.setenv('XDG_CONFIG_HOME', '/tmp/xdgcfg')
    assert themes_mod._config_dir() == os.path.join('/tmp/xdgcfg', 'serial_debug')


def test_config_dir_linux_default_xdg(monkeypatch):
    """linux 无 XDG_CONFIG_HOME 时回退 ~/.config/serial_debug。"""
    themes_mod = _reload_themes_with_platform(monkeypatch, 'linux')
    monkeypatch.delenv('XDG_CONFIG_HOME', raising=False)
    home = os.path.expanduser('~')
    assert themes_mod._config_dir() == os.path.join(home, '.config', 'serial_debug')


# ---- detect_system_theme 平台分支 ----

def test_detect_system_theme_darwin_dark(monkeypatch):
    """darwin: defaults 返 'Dark' 应判 dark。"""
    themes_mod = _reload_themes_with_platform(monkeypatch, 'darwin')

    class _R:
        stdout = 'Dark\n'

    monkeypatch.setattr(themes_mod.subprocess, 'run', lambda *a, **kw: _R())
    assert themes_mod.detect_system_theme() == 'dark'


def test_detect_system_theme_darwin_light(monkeypatch):
    """darwin: defaults 返非 'Dark' 应判 light。"""
    themes_mod = _reload_themes_with_platform(monkeypatch, 'darwin')

    class _R:
        stdout = 'Light\n'  # 'Light' 不含 'Dark' 大小写敏感

    monkeypatch.setattr(themes_mod.subprocess, 'run', lambda *a, **kw: _R())
    assert themes_mod.detect_system_theme() == 'light'


def test_detect_system_theme_win32_dark(monkeypatch):
    """win32: reg query 返 '0x0' 应判 dark。"""
    themes_mod = _reload_themes_with_platform(monkeypatch, 'win32')

    class _R:
        stdout = '    AppsUseLightTheme    REG_DWORD    0x0\n'

    monkeypatch.setattr(themes_mod.subprocess, 'run', lambda *a, **kw: _R())
    assert themes_mod.detect_system_theme() == 'dark'


def test_detect_system_theme_win32_light(monkeypatch):
    """win32: reg query 返 '0x1' 应判 light。"""
    themes_mod = _reload_themes_with_platform(monkeypatch, 'win32')

    class _R:
        stdout = '    AppsUseLightTheme    REG_DWORD    0x1\n'

    monkeypatch.setattr(themes_mod.subprocess, 'run', lambda *a, **kw: _R())
    assert themes_mod.detect_system_theme() == 'light'


def test_detect_system_theme_darwin_subprocess_failure(monkeypatch):
    """darwin: subprocess 失败应回 dark。"""
    themes_mod = _reload_themes_with_platform(monkeypatch, 'darwin')
    monkeypatch.setattr(themes_mod.subprocess, 'run',
                        lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError()))
    assert themes_mod.detect_system_theme() == 'dark'


def test_detect_system_theme_win32_subprocess_failure(monkeypatch):
    """win32: subprocess 失败应回 dark。"""
    themes_mod = _reload_themes_with_platform(monkeypatch, 'win32')
    monkeypatch.setattr(themes_mod.subprocess, 'run',
                        lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError()))
    assert themes_mod.detect_system_theme() == 'dark'


def test_detect_system_theme_linux_dark(monkeypatch):
    """linux: gsettings 返 'prefer-dark' 应判 dark。"""
    themes_mod = _reload_themes_with_platform(monkeypatch, 'linux')

    class _R:
        stdout = "'prefer-dark'\n"

    monkeypatch.setattr(themes_mod.subprocess, 'run', lambda *a, **kw: _R())
    assert themes_mod.detect_system_theme() == 'dark'


def test_detect_system_theme_linux_light(monkeypatch):
    """linux: gsettings 返 'prefer-light' 应判 light。"""
    themes_mod = _reload_themes_with_platform(monkeypatch, 'linux')

    class _R:
        stdout = "'prefer-light'\n"

    monkeypatch.setattr(themes_mod.subprocess, 'run', lambda *a, **kw: _R())
    assert themes_mod.detect_system_theme() == 'light'


def test_detect_system_theme_linux_no_gsettings(monkeypatch):
    """linux: gsettings 不存在应回 dark。"""
    themes_mod = _reload_themes_with_platform(monkeypatch, 'linux')
    monkeypatch.setattr(themes_mod.subprocess, 'run',
                        lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError()))
    assert themes_mod.detect_system_theme() == 'dark'


# ---- load_pref / save_pref with mocked config ----

def test_load_pref_returns_stored_value(themes_mod, monkeypatch):
    """config 模块可用时应读 theme_pref。"""
    import types
    fake_config = types.ModuleType('config')
    fake_config.load = lambda: {'theme_pref': 'dark'}
    monkeypatch.setitem(sys.modules, 'config', fake_config)
    assert themes_mod.load_pref() == 'dark'


def test_load_pref_default_auto_when_missing(themes_mod, monkeypatch):
    """config.load 没设 theme_pref 时回 'auto'。"""
    import types
    fake_config = types.ModuleType('config')
    fake_config.load = lambda: {}
    monkeypatch.setitem(sys.modules, 'config', fake_config)
    assert themes_mod.load_pref() == 'auto'


def test_load_pref_swallows_exception(themes_mod, monkeypatch):
    """config.load 抛异常应回 'auto'。"""
    import types
    fake_config = types.ModuleType('config')

    def boom():
        raise RuntimeError('config read fail')

    fake_config.load = boom
    monkeypatch.setitem(sys.modules, 'config', fake_config)
    assert themes_mod.load_pref() == 'auto'


def test_save_pref_writes_to_config(themes_mod, monkeypatch):
    """save_pref 应合并 theme_pref 写回 config.save。"""
    import types
    saved = {}

    fake_config = types.ModuleType('config')
    fake_config.load = lambda: {'other_key': 1}
    fake_config.save = lambda cfg: saved.update(cfg)
    monkeypatch.setitem(sys.modules, 'config', fake_config)
    themes_mod.save_pref('light')
    assert saved.get('theme_pref') == 'light'
    assert saved.get('other_key') == 1


def test_save_pref_swallows_save_failure(themes_mod, monkeypatch):
    """config.save 抛异常不应让 save_pref 抛。"""
    import types
    fake_config = types.ModuleType('config')
    fake_config.load = lambda: {}

    def boom(cfg):
        raise RuntimeError('disk full')

    fake_config.save = boom
    monkeypatch.setitem(sys.modules, 'config', fake_config)
    # 不应抛
    themes_mod.save_pref('dark')


