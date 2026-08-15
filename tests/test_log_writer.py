"""core/log_writer.py 测试：落盘 / 线程安全 / 异常容错。

不依赖真磁盘行为——用 tmp_path 写真实文件验证。
"""
from __future__ import annotations

import os
import time

import pytest


@pytest.fixture
def log_path(tmp_path):
    return str(tmp_path / 'test.log')


def test_log_writer_start_writes_header(log_path):
    """start 后应立刻写入 === log start === 头。"""
    from serial_debug.core.log_writer import LogWriter
    lw = LogWriter(log_path)
    assert lw.start() is True
    # 等头写入
    time.sleep(0.05)
    lw.stop()
    with open(log_path, encoding='utf-8') as f:
        content = f.read()
    assert '=== log start' in content
    assert '=== log stop' in content


def test_log_writer_put_flushes_to_disk(log_path):
    """put 后内容应在 2s 内 flush 到磁盘。"""
    from serial_debug.core.log_writer import LogWriter
    lw = LogWriter(log_path)
    assert lw.start() is True
    lw.put('hello world\n')
    # 等线程 flush（writer 每 0.2s flush 一次）
    time.sleep(0.3)
    lw.stop()
    with open(log_path, encoding='utf-8') as f:
        content = f.read()
    assert 'hello world' in content


def test_log_writer_start_fails_on_unwritable_path():
    """start 失败应记 error 并返回 False，不创建线程。"""
    from serial_debug.core.log_writer import LogWriter
    # 不可写目录（root 拥有的 /proc 之类）——用不存在路径模拟
    bad_path = '/nonexistent_dir_xyz/log.txt'
    lw = LogWriter(bad_path)
    ok = lw.start()
    assert ok is False
    assert lw.error is not None
    assert lw._thread is None  # 启动失败不应创建线程


def test_log_writer_stop_joins_thread(log_path):
    """stop 应在 2s 内 join 线程。"""
    from serial_debug.core.log_writer import LogWriter
    lw = LogWriter(log_path)
    lw.start()
    # 写点数据让线程干活
    for i in range(10):
        lw.put(f'line {i}\n')
    t0 = time.monotonic()
    lw.stop()
    elapsed = time.monotonic() - t0
    assert elapsed < 2.0
    assert lw._thread is None


def test_log_writer_put_after_file_closed_is_noop(log_path):
    """文件未开时 put 是 no-op，不抛异常。"""
    from serial_debug.core.log_writer import LogWriter
    lw = LogWriter(log_path)
    # 未 start
    lw.put('should be dropped')
    # 不应抛
    lw.stop()  # 未启动时 stop 是 no-op


def test_log_writer_stop_idempotent(log_path):
    """多次 stop 不抛异常。"""
    from serial_debug.core.log_writer import LogWriter
    lw = LogWriter(log_path)
    lw.start()
    lw.stop()
    lw.stop()
    lw.stop()
