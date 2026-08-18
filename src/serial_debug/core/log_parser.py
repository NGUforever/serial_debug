"""日志行时间戳提取。

从 tkinter 监视窗口解析器抽出，供 Qt 监视窗口 / 日志查看器 / 其他模块共用。
不依赖任何 GUI 框架（tkinter / PySide6）。
"""
from __future__ import annotations

import re

__all__ = ["_LOG_TS_RE", "extract_log_ts"]


# 匹配日志行中的 10~13 位数字时间戳（毫秒或秒）。
# 前后必须非数字，避免从长串 ID 中误匹配中段。
_LOG_TS_RE = re.compile(r'(?<!\d)(\d{10,13})(?!\d)')


def extract_log_ts(line: str) -> float | None:
    """从日志行中提取时间戳，返回秒（float）。提取失败返回 None。

    13 位 → 毫秒，除以 1000 转秒；10 位及以下 → 秒，直接 float。
    """
    m = _LOG_TS_RE.search(line)
    if not m:
        return None
    try:
        ms = int(m.group(1))
        if len(m.group(1)) <= 10:
            return float(ms)
        return ms / 1000.0
    except ValueError:
        return None


# 向后兼容：旧调用方使用 `_extract_log_ts` 私有名
_extract_log_ts = extract_log_ts
