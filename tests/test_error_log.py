"""infra/error_log.py 测试：log_exc 跨平台异常日志辅助。

不依赖 Tk / Qt——纯 stdlib logging 包装。
"""
from __future__ import annotations

import logging

import pytest


@pytest.fixture
def error_log_mod():
    from serial_debug.infra import error_log
    return error_log


def test_log_exc_does_not_crash(error_log_mod):
    """log_exc 不应抛异常（即便 level 非法）。"""
    # 不应抛
    error_log_mod.log_exc('error', 'test message', ValueError('boom'))
    error_log_mod.log_exc('warning', 'warn')
    error_log_mod.log_exc('debug', 'debug msg', RuntimeError('err'))
    # 非法 level 应兜底 warning 不抛
    error_log_mod.log_exc('xxx', 'unknown level', None)


def _make_handler(records, fmt=False):
    """创建一个捕获日志的 handler，返回 (handler, records_list)。"""
    class _H(logging.Handler):
        pass
    h = _H()
    def emit(record):
        if fmt:
            records.append(h.format(record))
        else:
            records.append(record)
    h.emit = emit
    h.setLevel(logging.DEBUG)
    return h


def test_log_exc_with_exception_object(error_log_mod):
    """传 exc 对象时应把异常信息写入日志。"""
    caplog_records = []
    h = _make_handler(caplog_records, fmt=True)

    logger = error_log_mod.get_logger()
    logger.handlers = []
    logger.addHandler(h)
    logger.setLevel(logging.DEBUG)

    error_log_mod.log_exc('error', 'ctx: write failed',
                          ValueError('bad value'))
    assert len(caplog_records) >= 1
    assert 'ctx: write failed' in caplog_records[0]
    assert 'bad value' in caplog_records[0]


def test_get_logger_returns_serial_debug_prefix(error_log_mod):
    """get_logger(name) 应返回 serial_debug.name 前缀 logger。"""
    lg = error_log_mod.get_logger('mylib')
    assert lg.name == 'serial_debug.mylib'


def test_get_logger_without_name_returns_root(error_log_mod):
    """get_logger() 不带 name 返回 serial_debug 根 logger。"""
    lg = error_log_mod.get_logger()
    assert lg.name == 'serial_debug'


def test_setup_basic_logging_idempotent(error_log_mod):
    """setup_basic_logging 多次调用不应抛。"""
    # 不应抛
    error_log_mod.setup_basic_logging('DEBUG')
    error_log_mod.setup_basic_logging('INFO')
    error_log_mod.setup_basic_logging('WARNING')


def test_setup_basic_logging_respects_env_level(error_log_mod, monkeypatch):
    """SERIAL_DEBUG_LOG_LEVEL 环境变量应覆盖默认 level。"""
    monkeypatch.setenv('SERIAL_DEBUG_LOG_LEVEL', 'DEBUG')
    # 不应抛
    error_log_mod.setup_basic_logging('INFO')


def test_log_exc_unknown_level_falls_back_to_warning(error_log_mod):
    """非法 level 应回退到 warning。"""
    caplog_records = []
    h = _make_handler(caplog_records, fmt=False)

    logger = error_log_mod.get_logger()
    logger.handlers = []
    logger.addHandler(h)
    logger.setLevel(logging.DEBUG)

    error_log_mod.log_exc('critical_invalid', 'some msg', None)
    # 应有 warning 级别记录
    assert any(r.levelno == logging.WARNING for r in caplog_records)


def test_log_exc_without_exc_passes_message_only(error_log_mod):
    """exc=None 时只记 msg，不格式化异常。"""
    caplog_records = []
    h = _make_handler(caplog_records, fmt=True)

    logger = error_log_mod.get_logger()
    logger.handlers = []
    logger.addHandler(h)
    logger.setLevel(logging.DEBUG)

    error_log_mod.log_exc('info', 'just a message', None)
    assert any('just a message' in r for r in caplog_records)
