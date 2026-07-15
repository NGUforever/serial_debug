"""应用配置持久化。

存到平台适配的配置目录：
  Linux:   ~/.config/serial_debug/config.json
  macOS:   ~/Library/Application Support/serial_debug/config.json
  Windows: %LOCALAPPDATA%/serial_debug/config.json

包含：
  port, baudrate, append_crlf, auto_scroll, log_path, theme_pref,
  at_commands, filter_pattern, filter_enabled, ts_convert_enabled,
  at_window_mode, geometry, at_geometry
"""
from __future__ import annotations

import json
import os
import sys
import threading


def _config_dir() -> str:
    """返回平台适配的配置目录。"""
    home = os.path.expanduser('~')
    if sys.platform == 'win32':
        base = os.environ.get('LOCALAPPDATA',
                              os.path.join(home, 'AppData', 'Local'))
        return os.path.join(base, 'serial_debug')
    if sys.platform == 'darwin':
        return os.path.join(home, 'Library', 'Application Support',
                            'serial_debug')
    xdg = os.environ.get('XDG_CONFIG_HOME',
                         os.path.join(home, '.config'))
    return os.path.join(xdg, 'serial_debug')


CONFIG_DIR = _config_dir()
CONFIG_FILE = os.path.join(CONFIG_DIR, 'config.json')

# 默认值，新装机或首次启动用
DEFAULTS = {
    'port': '',
    'baudrate': 115200,
    'append_crlf': False,
    'auto_scroll': True,
    'log_path': '',
    'theme_pref': 'auto',
    'at_commands': [],
    'filter_pattern': '',
    'filter_enabled': False,
    'ts_convert_enabled': False,
    'at_window_mode': 'detached',     # detached / embedded / hidden
    'geometry': '1100x680',
    'at_geometry': '720x520',
    'font_size': 10,
    # 串口参数
    'bytesize': 8,
    'parity': 'none',     # none / even / odd
    'stopbits': 1,        # 1 / 2
    'flow_control': 'none',   # none / rtscts
    # 监视窗口变量（电机 / BMS / IMU）
    'motor_vars': [],
    'bms_vars': [],
    'imu_vars': [],
    'motor_plots': [],
    'bms_plots': [],
    'imu_plots': [],
    'motor_monitor_geometry': '',
    'bms_monitor_geometry': '',
    'imu_monitor_geometry': '',
    'motor_monitor_mode': 'detached',   # detached / embedded / hidden
    'bms_monitor_mode': 'detached',
    'imu_monitor_mode': 'detached',
}


def load() -> dict:
    """读配置，合并默认值；读失败返回 DEFAULTS 副本。"""
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            for k, v in data.items():
                if k in cfg:
                    cfg[k] = v
    except Exception:
        pass
    return cfg


_save_lock = threading.Lock()


def save(cfg: dict) -> None:
    """原子写配置（临时文件 + rename）。非阻塞，失败静默。"""
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        # 只存 DEFAULTS 里已有的字段，避免脏数据
        cleaned = {k: cfg.get(k, DEFAULTS[k]) for k in DEFAULTS}
        tmp = CONFIG_FILE + '.tmp'
        with _save_lock:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(cleaned, f, ensure_ascii=False, indent=2)
            os.replace(tmp, CONFIG_FILE)
    except Exception:
        pass
