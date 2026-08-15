"""跨平台异常日志辅助：在裸 except 块里调用 `log_exc`，避免
`except Exception: pass` 把故障现场吞掉。

设计原则（Agent-Head/memory/FACT.md 跨 Agent 协作教训 5）：
- 不能用裸 `except Exception: pass`——会让 NameError/AttributeError 静默失效
- 至少 `log_exc(level, msg, e)` 留故障现场
- 仅在确认不影响功能的 IO 边界（关 timer / 释放资源）才允许 pass，且必须注释

使用 stdlib `logging`：
- 跨平台无 import 风险
- 默认 NullHandler，不污染用户终端
- 应用启动时（app.py）调 `setup_basic_logging()` 配置 stderr sink
- 业务模块只 import `log_exc`，无需关心 sink 是否配置好
"""
from __future__ import annotations

import logging
import os
from typing import Any

_LOGGER = logging.getLogger('serial_debug')
if not _LOGGER.handlers:
    _LOGGER.addHandler(logging.NullHandler())


def log_exc(level: str, msg: str, exc: BaseException | None = None) -> None:
    """记录一条异常日志。

    level: 'debug' / 'info' / 'warning' / 'error'
    msg: 上下文描述（文件:行号 + 操作）
    exc: 异常对象（来自 `except X as e`），可 None
    """
    fn = {
        'debug': _LOGGER.debug,
        'info': _LOGGER.info,
        'warning': _LOGGER.warning,
        'error': _LOGGER.error,
    }.get(level, _LOGGER.warning)
    if exc is not None:
        fn('%s: %s', msg, exc, exc_info=False)
    else:
        fn('%s', msg)


def get_logger(name: str | None = None) -> logging.Logger:
    """业务模块取自己的 logger（带 serial_debug 前缀，便于统一配置）。"""
    if name:
        return logging.getLogger(f'serial_debug.{name}')
    return _LOGGER


def setup_basic_logging(level: str = 'INFO') -> None:
    """app.py 启动时调一次：配 stderr sink，让 log_exc 真正输出。

    设 DEBUG_CHUNK 或 SERIAL_DEBUG_LOG_LEVEL 环境变量可覆盖默认 level。
    """
    env_level = os.environ.get('SERIAL_DEBUG_LOG_LEVEL', '').upper()
    final_level = env_level or level.upper()
    logging.basicConfig(
        level=final_level,
        format='%(asctime)s %(name)s %(levelname)s %(message)s',
        force=False,  # 不覆盖用户已配的 root logger
    )


__all__: list[Any] = ['log_exc', 'get_logger', 'setup_basic_logging']
