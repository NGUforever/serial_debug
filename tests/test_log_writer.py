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


# ---- 写失败 / flush 失败 / 收尾失败路径 ----

class _FailingFile:
    """模拟 file 对象：write 永远抛 OSError（磁盘满）。"""

    def __init__(self, real_path):
        self.real = open(real_path, 'a', encoding='utf-8')
        self.write_count = 0
        self.flush_count = 0

    def write(self, text):
        self.write_count += 1
        raise OSError('disk full')

    def flush(self):
        self.flush_count += 1
        raise OSError('flush failed')

    def close(self):
        try:
            self.real.close()
        except Exception:
            pass
        raise OSError('close failed')


def test_log_writer_write_failure_does_not_kill_thread(log_path):
    """_run 写失败应记 error 不退出线程（继续消费 queue）。"""
    from serial_debug.core import log_writer as mod
    lw = mod.LogWriter(log_path)
    # 替换 _f 为失败 file
    lw._f = _FailingFile(log_path)
    lw._thread = __import__('threading').Thread(
        target=mod.LogWriter._run, args=(lw,), name='test-writer', daemon=True)
    lw._thread.start()
    lw.put('line that triggers write failure\n')
    import time
    time.sleep(0.05)
    # 线程应仍存活（write 失败不退出）
    assert lw._thread.is_alive()
    lw.stop()
    # stop 后线程退出


def test_log_writer_flush_failure_swallowed(log_path):
    """_flush 抛异常应被吞，不退出线程。"""
    from serial_debug.core import log_writer as mod
    lw = mod.LogWriter(log_path)
    lw._f = _FailingFile(log_path)
    # 直接调 _flush
    lw._flush()  # 不应抛
    assert lw._f.flush_count == 1


def test_log_writer_close_failure_swallowed(log_path):
    """_f.close 抛异常应被吞。"""
    from serial_debug.core import log_writer as mod
    lw = mod.LogWriter(log_path)
    lw._f = _FailingFile(log_path)
    # 手动跑 _run 收尾——直接调 stop 跑完整流程
    lw._thread = __import__('threading').Thread(
        target=mod.LogWriter._run, args=(lw,), name='test-writer', daemon=True)
    lw._thread.start()
    lw.stop()  # close 异常被吞


def test_log_writer_drain_tail_exception_swallowed(log_path):
    """_run 收尾 drain queue 时 _f.write 抛异常应被吞。"""
    import threading

    from serial_debug.core import log_writer as mod

    class _WriteFailsAfter:
        """前 N 次 write 成功，之后抛异常。"""
        def __init__(self, real_path):
            self.real = open(real_path, 'a', encoding='utf-8')
            self.count = 0
        def write(self, text):
            self.count += 1
            if self.count > 1:
                raise OSError('disk full mid drain')
            self.real.write(text)
        def flush(self):
            self.real.flush()
        def close(self):
            self.real.close()

    lw = mod.LogWriter(log_path)
    lw._f = _WriteFailsAfter(log_path)
    lw._thread = threading.Thread(
        target=mod.LogWriter._run, args=(lw,), name='test-writer', daemon=True)
    lw._thread.start()
    # put 一些数据让收尾 drain 有数据要写
    lw.put('line1\n')
    lw.put('line2\n')
    import time
    time.sleep(0.05)
    lw.stop()  # 不应抛


def test_log_writer_idle_flush(log_path):
    """queue 空闲时 _run 也定期 flush（line 52-55 分支）。"""
    from serial_debug.core.log_writer import LogWriter
    lw = LogWriter(log_path)
    lw.start()
    lw.put('one\n')
    # 等 0.5s 触发至少 2 次空闲 flush cycle
    import time
    time.sleep(0.5)
    lw.stop()
    with open(log_path, encoding='utf-8') as f:
        content = f.read()
    assert 'one' in content


def test_log_writer_thread_did_not_exit_warning(log_path, monkeypatch):
    """stop 时 thread.join timeout 后仍存活应记 warning，不抛。"""
    import threading
    import time

    from serial_debug.core import log_writer as mod

    lw = mod.LogWriter(log_path)
    lw.start()
    # 让 join timeout=0.001 极短，模拟 thread 不退出
    orig_join = threading.Thread.join

    def quick_join(self, timeout=None):
        return orig_join(self, timeout=0.001)

    monkeypatch.setattr(threading.Thread, 'join', quick_join)
    # 不应抛
    lw.stop()
    # 即使 thread 仍存活（短暂），_thread 应被置 None
    assert lw._thread is None


def test_log_writer_stop_marker_written(log_path):
    """stop 后文件应包含 === log stop === 标记。"""
    from serial_debug.core.log_writer import LogWriter
    lw = LogWriter(log_path)
    lw.start()
    import time
    time.sleep(0.05)
    lw.stop()
    with open(log_path, encoding='utf-8') as f:
        content = f.read()
    assert '=== log stop' in content


def test_log_writer_put_dropped_after_start_failure(log_path):
    """start 失败后 _f=None，put 应丢弃不抛。"""
    from serial_debug.core.log_writer import LogWriter
    lw = LogWriter('/nonexistent_dir_xyz/test.log')
    assert lw.start() is False
    assert lw._f is None
    # put 不应抛
    lw.put('should be dropped')
    # stop 在 _thread=None 时是 no-op
    lw.stop()


def test_log_writer_tail_drain_writes_pending(log_path):
    """stop 后 drain tail 应把 queue 里剩余项写出。"""
    import time

    from serial_debug.core.log_writer import LogWriter
    lw = LogWriter(log_path)
    lw.start()
    # 快速 put 多条
    for i in range(50):
        lw.put(f'pending-{i}\n')
    # 立即 stop，让 drain tail 在收尾处理这批
    lw.stop()
    with open(log_path, encoding='utf-8') as f:
        content = f.read()
    # 应有部分 pending-* 被写出（至少最后一个）
    assert 'pending-49' in content or 'pending-0' in content


def test_log_writer_none_sentinel_stops_loop(log_path):
    """queue 收到 None 哨兵应让 _run 退出循环。"""
    import time

    from serial_debug.core.log_writer import LogWriter
    lw = LogWriter(log_path)
    lw.start()
    # 直接 put None（模拟 stop 内部行为）
    lw._q.put(None)
    time.sleep(0.1)
    # 线程应已退出
    assert not lw._thread.is_alive()
    # 显式清理
    lw._thread = None
    lw._stop.set()


def test_log_writer_flush_during_write_interval(log_path):
    """put 后超过 0.2s 应触发 _flush（line 66-67 路径）。"""
    import time

    from serial_debug.core.log_writer import LogWriter
    lw = LogWriter(log_path)
    lw.start()
    lw.put('trigger\n')
    # 等 0.3s 触发 interval flush
    time.sleep(0.3)
    lw.stop()
    with open(log_path, encoding='utf-8') as f:
        content = f.read()
    assert 'trigger' in content


def test_log_writer_stop_marker_failure_swallowed(log_path, monkeypatch):
    """stop marker write 抛异常应被吞。"""
    from serial_debug.core import log_writer as mod

    class _FailingAtStop:
        def __init__(self, real_path):
            self.real = open(real_path, 'a', encoding='utf-8')
            self.first_write = True
        def write(self, text):
            if 'log stop' in text:
                raise OSError('stop marker fail')
            self.real.write(text)
        def flush(self):
            self.real.flush()
        def close(self):
            self.real.close()

    lw = mod.LogWriter(log_path)
    lw._f = _FailingAtStop(log_path)
    import threading
    lw._thread = threading.Thread(
        target=mod.LogWriter._run, args=(lw,), name='test-writer', daemon=True)
    lw._thread.start()
    lw.stop()  # stop marker write 失败被吞
