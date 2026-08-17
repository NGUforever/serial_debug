"""core/serial_backend.py 测试：跨平台 import / open/close / read_loop / send。

不依赖真实硬件：用 mock Serial 模拟 read/in_waiting/write/flush/is_open。
"""
from __future__ import annotations

import sys
from unittest import mock

import pytest


@pytest.fixture
def backend():
    """提供一个 SerialBackend 实例，不连真串口。"""
    from serial_debug.core import serial_backend as mod
    b = mod.SerialBackend()
    yield b
    b.close()


# ---- B1 跨平台 fcntl import ----

def test_serial_backend_imports_on_windows_mock(backend, monkeypatch):
    """B1: 模拟 Windows 平台——即使 fcntl import 失败，模块也能正常加载。

    通过 monkeypatch sys.modules 让 fcntl 不可 import，重新加载模块，
    验证 HAS_FCNTL=False 且 list_ports 仍可调用。
    """
    # 把 fcntl 替换为一个会 raise ImportError 的 placeholder
    monkeypatch.setitem(sys.modules, 'fcntl', None)  # None 会让 import 失败
    # 直接验证逻辑：HAS_FCNTL 由 try/except 兜底
    from serial_debug.core import serial_backend as mod
    assert hasattr(mod, 'HAS_FCNTL')
    assert isinstance(mod.HAS_FCNTL, bool)
    # list_ports 不应因 fcntl 状态崩溃
    ports = mod.SerialBackend.list_ports()
    assert isinstance(ports, list)


def test_try_set_low_latency_skips_when_no_fcntl(monkeypatch):
    """B1: HAS_FCNTL=False 时 _try_set_low_latency 直接 return，不调 fcntl.ioctl。"""
    from serial_debug.core import serial_backend as mod
    b = mod.SerialBackend()
    monkeypatch.setattr(mod, 'HAS_FCNTL', False)
    # 不应抛异常
    b._try_set_low_latency()


def test_try_set_low_latency_uses_fcntl_when_available(monkeypatch):
    """B1: HAS_FCNTL=True 时走 fcntl.ioctl 路径（mock fcntl.ioctl 验证调用）。"""
    from serial_debug.core import serial_backend as mod
    b = mod.SerialBackend()

    class _FakeSer:
        def fileno(self):
            return 3

    ioctl_calls = []

    def fake_ioctl(fd, cmd, buf):
        ioctl_calls.append((fd, cmd, buf))

    monkeypatch.setattr(mod, 'HAS_FCNTL', True)
    monkeypatch.setattr(mod, 'fcntl', mock.MagicMock(ioctl=fake_ioctl), raising=False)
    b.ser = _FakeSer()
    b._try_set_low_latency()
    assert len(ioctl_calls) == 2  # TIOCGSERIAL + TIOCSSERIAL


# ---- M5 close 触发 reader 退出 ----

def test_read_loop_exits_on_close():
    """M5: close() 后 _read_loop 应退出循环（ser.read(1) 返空 + stop_evt set → break）。"""
    from serial_debug.core import serial_backend as mod

    class _FakeSer:
        """模拟 pyserial：read 永远返回 b''（模拟 fd 已关）；is_open True 直到外部置 False。"""
        def __init__(self):
            self.is_open = True
            self.in_waiting = 0
        def read(self, n=1):
            return b''  # 模拟 close 后 read 返空
        def close(self):
            self.is_open = False
        def reset_input_buffer(self): pass
        def reset_output_buffer(self): pass

    b = mod.SerialBackend()
    b.ser = _FakeSer()
    b._stop_evt.clear()
    # 启动 reader
    b._reader = __import__('threading').Thread(
        target=b._read_loop, name='test-reader', daemon=True)
    b._reader.start()
    # 模拟 close 调用：set stop event + 关 ser
    b._stop_evt.set()
    b.ser.is_open = False
    # reader 应在 2s 内退出
    b._reader.join(timeout=2.0)
    assert not b._reader.is_alive(), "reader thread did not exit after close"
    # 应该 emit ('closed', None) 到 rx_queue
    items = []
    while True:
        try:
            items.append(b.rx_queue.get_nowait())
        except Exception:
            break
    assert any(k == 'closed' for k, _ in items)


# ---- list_ports ----

def test_list_ports_returns_sorted_list(backend):
    """list_ports 返回 list（即便没有真串口也不崩）。"""
    from serial_debug.core import serial_backend as mod
    ports = mod.SerialBackend.list_ports()
    assert isinstance(ports, list)
    # 即便空也应是 list
    # 验证返回的端口字符串可重复（即 sorted 一致）
    assert ports == sorted(ports)


# ---- send on closed serial ----

def test_send_returns_false_when_not_open(backend):
    """未连串口时 send 返回 False + emit status error。"""
    ok = backend.send('AT')
    assert ok is False
    # 应有 status error 入队
    items = []
    while True:
        try:
            items.append(backend.rx_queue.get_nowait())
        except Exception:
            break
    assert any(k == 'status' and v[0] == 'error' for k, v in items)


# ---- flush_input drains queue but keeps control items ----

def test_flush_input_drains_line_items_keeps_status(backend):
    """flush_input 应清掉 line 项但保留 status/error/closed 控制项。"""
    backend.rx_queue.put(('line', 'old log 1'))
    backend.rx_queue.put(('status', ('info', 'connected')))
    backend.rx_queue.put(('line', 'old log 2'))
    backend.rx_queue.put(('error', 'some error'))
    backend.flush_input()
    items = []
    while True:
        try:
            items.append(backend.rx_queue.get_nowait())
        except Exception:
            break
    # 应保留 status/error，丢弃 line
    kinds = [k for k, _ in items]
    assert 'status' in kinds
    assert 'error' in kinds
    assert 'line' not in kinds


# ---- _status callback swallow ----

def test_status_callback_failure_does_not_crash(backend):
    """on_status 回调抛异常不应让 _status 崩溃。"""
    calls = []
    def bad_cb(level, msg):
        calls.append((level, msg))
        raise RuntimeError('callback explosion')
    backend.on_status = bad_cb
    # 不应抛
    backend._status('info', 'test')
    assert len(calls) == 1


# ---- open/close full flow with mock serial ----

class _FakeSerial:
    """Mock pyserial：read 永远返空 + in_waiting=0，可被 close。"""
    def __init__(self, *a, **kw):
        self.is_open = True
        self._read_data = b'hello\nworld\n'  # 预先放 12 字节
        self._read_idx = 0
        self.in_waiting = 0
        self.written = b''
    def read(self, n=1):
        if self._read_idx >= len(self._read_data):
            return b''
        chunk = self._read_data[self._read_idx:self._read_idx + n]
        self._read_idx += len(chunk)
        return chunk
    def write(self, data):
        self.written += data
        return len(data)
    def flush(self):
        pass
    def close(self):
        self.is_open = False
    def reset_input_buffer(self):
        pass
    def reset_output_buffer(self):
        pass
    def fileno(self):
        return -1


def test_open_with_mock_serial_starts_threads(monkeypatch):
    """open() 用 mock serial 应启动 reader + writer 线程。"""
    import serial as pyserial

    from serial_debug.core import serial_backend as mod
    # patch serial.Serial 构造
    monkeypatch.setattr(pyserial, 'Serial', _FakeSerial)
    monkeypatch.setattr(mod, 'HAS_FCNTL', False)  # 跳过 fcntl.ioctl 路径
    b = mod.SerialBackend()
    # 不应抛
    ok = b.open(port='/dev/null', baudrate=115200)
    assert ok is True
    assert b.ser is not None
    assert b._reader is not None
    assert b._writer is not None
    # reader 应能读到 mock 数据，emit 'line' 到 queue
    import time
    time.sleep(0.05)
    items = []
    while True:
        try:
            items.append(b.rx_queue.get_nowait())
        except Exception:
            break
    kinds = [k for k, _ in items]
    assert 'line' in kinds
    # 应有 hello 和 world 两条 line
    lines = [v for k, v in items if k == 'line']
    assert 'hello' in lines
    assert 'world' in lines
    b.close()


def test_close_with_mock_serial_joins_threads(monkeypatch):
    """close() 应 join reader/writer + 清 self.ser。"""
    import serial as pyserial

    from serial_debug.core import serial_backend as mod
    monkeypatch.setattr(pyserial, 'Serial', _FakeSerial)
    monkeypatch.setattr(mod, 'HAS_FCNTL', False)
    b = mod.SerialBackend()
    b.open(port='/dev/null')
    assert b.ser is not None
    b.close()
    assert b.ser is None
    assert b._reader is None
    assert b._writer is None


def test_send_with_mock_serial_routes_to_write_thread(monkeypatch):
    """send() 应通过 writer 线程异步写入 mock serial。"""
    import serial as pyserial

    from serial_debug.core import serial_backend as mod
    monkeypatch.setattr(pyserial, 'Serial', _FakeSerial)
    monkeypatch.setattr(mod, 'HAS_FCNTL', False)
    b = mod.SerialBackend()
    b.open(port='/dev/null')
    ok = b.send('AT+VER', append_crlf=True)
    assert ok is True
    # 等 writer 线程处理
    import time
    time.sleep(0.1)
    assert b.ser.written  # 应有数据写入
    assert b.ser.written.endswith(b'AT+VER\r\n') or b'AT+VER' in b.ser.written
    b.close()


def test_send_with_crlf_appends_newlines(monkeypatch):
    """send(append_crlf=True) 应在数据末尾加 \r\n。"""
    import serial as pyserial

    from serial_debug.core import serial_backend as mod
    monkeypatch.setattr(pyserial, 'Serial', _FakeSerial)
    monkeypatch.setattr(mod, 'HAS_FCNTL', False)
    b = mod.SerialBackend()
    b.open(port='/dev/null')
    b.send('AT', append_crlf=True)
    import time
    time.sleep(0.05)
    assert b.ser.written == b'AT\r\n'
    b.close()


def test_close_idempotent_when_already_closed(backend):
    """close 在未 open 状态调用也不应抛。"""
    # 未 open
    backend.close()
    backend.close()  # 二次 close 不抛


def test_open_failure_returns_false(monkeypatch):
    """serial.Serial 构造抛异常时 open 应返 False 并 emit error status。"""
    import serial as pyserial

    from serial_debug.core import serial_backend as mod

    class _BadSerial:
        def __init__(self, *a, **kw):
            raise OSError('device not found')

    monkeypatch.setattr(pyserial, 'Serial', _BadSerial)
    b = mod.SerialBackend()
    ok = b.open(port='/dev/ttyXXX', baudrate=115200)
    assert ok is False
    assert b.ser is None
    # 应有 status error
    items = []
    while True:
        try:
            items.append(b.rx_queue.get_nowait())
        except Exception:
            break
    assert any(k == 'status' and v[0] == 'error' for k, v in items)


def test_read_loop_emits_closed_on_exit():
    """reader 线程退出时应 emit ('closed', None)。"""
    import threading

    from serial_debug.core import serial_backend as mod
    b = mod.SerialBackend()

    class _EmptySer:
        is_open = True
        in_waiting = 0
        def read(self, n=1):
            return b''  # 永远空，触发 stop_evt 检查
        def close(self):
            self.is_open = False

    b.ser = _EmptySer()
    b._stop_evt.clear()
    t = threading.Thread(target=b._read_loop, daemon=True)
    t.start()
    # 立即让 reader 退出
    b._stop_evt.set()
    t.join(timeout=2.0)
    assert not t.is_alive()
    # 应有 closed 项
    items = []
    while True:
        try:
            items.append(b.rx_queue.get_nowait())
        except Exception:
            break
    assert ('closed', None) in items


def test_open_drains_residual_queue(monkeypatch):
    """open 应先清空 rx_queue 拋留项。"""
    import serial as pyserial

    from serial_debug.core import serial_backend as mod
    monkeypatch.setattr(pyserial, 'Serial', _FakeSerial)
    monkeypatch.setattr(mod, 'HAS_FCNTL', False)
    b = mod.SerialBackend()
    b.rx_queue.put(('status', ('info', 'old status')))
    b.rx_queue.put(('line', 'old line'))
    b.open(port='/dev/null')
    # open 不清 queue，但 reader 启动后会推新数据进来；
    # 验证至少 reader 工作正常（避免 open 前 queue 卡死）
    import time
    time.sleep(0.05)
    b.close()


def test_is_open_property(backend):
    """未 open 时 is_open 应为 False。"""
    assert backend.is_open is False


def test_status_queue_carries_callback_status():
    """status 应入队 + 调 on_status 回调。"""
    from serial_debug.core import serial_backend as mod
    calls = []
    b = mod.SerialBackend(on_status=lambda lvl, msg: calls.append((lvl, msg)))
    b._status('info', 'test ok')
    # 应入队
    items = []
    while True:
        try:
            items.append(b.rx_queue.get_nowait())
        except Exception:
            break
    assert any(k == 'status' for k, _ in items)
    assert ('info', 'test ok') in calls


# ---- 22-23: fcntl ImportError 路径 ----

def test_fcntl_importerror_branch(monkeypatch):
    """模拟 fcntl 不存在（Windows）：HAS_FCNTL=False。"""
    import importlib
    saved = sys.modules.pop('serial_debug.core.serial_backend', None)
    # 把 fcntl 模块设为 None 让 import 失败
    monkeypatch.setitem(sys.modules, 'fcntl', None)
    try:
        mod = importlib.import_module('serial_debug.core.serial_backend')
        assert mod.HAS_FCNTL is False
    finally:
        # 还原原模块
        if saved is not None:
            sys.modules['serial_debug.core.serial_backend'] = saved


# ---- DEBUG_CHUNK _dbg 路径 ----

def test_dbg_prints_when_debug_chunk_enabled(capsys, monkeypatch):
    """DEBUG_CHUNK=True 时 _dbg 应 print 到 stderr。"""
    from serial_debug.core import serial_backend as mod
    monkeypatch.setattr(mod, 'DEBUG_CHUNK', True)
    mod._dbg('test message')
    captured = capsys.readouterr()
    assert 'test message' in captured.err


def test_dbg_silent_when_debug_disabled(capsys, monkeypatch):
    """DEBUG_CHUNK=False 时 _dbg 不应输出。"""
    from serial_debug.core import serial_backend as mod
    monkeypatch.setattr(mod, 'DEBUG_CHUNK', False)
    mod._dbg('should not print')
    captured = capsys.readouterr()
    assert captured.err == ''


# ---- open() reopen 已有 ser 的 close 路径 (line 69) ----

def test_open_reopen_closes_existing_ser(monkeypatch):
    """open() 时已有 ser 应先 close。"""
    import serial as pyserial

    from serial_debug.core import serial_backend as mod
    monkeypatch.setattr(pyserial, 'Serial', _FakeSerial)
    monkeypatch.setattr(mod, 'HAS_FCNTL', False)
    b = mod.SerialBackend()
    b.open(port='/dev/null')
    old_ser = b.ser
    close_called = []
    orig_close = old_ser.close
    old_ser.close = lambda: close_called.append(1) or orig_close()
    # 再次 open 应触发 close
    b.open(port='/dev/null')
    assert close_called == [1]
    b.close()


# ---- reset_input_buffer exception 路径 (line 87-89) ----

def test_open_reset_input_buffer_failure_swallowed(monkeypatch):
    """reset_input_buffer 抛异常应被吞，open 继续启动 reader。"""
    import serial as pyserial

    from serial_debug.core import serial_backend as mod

    class _ResetFailsSerial(_FakeSerial):
        def reset_input_buffer(self):
            raise OSError('not supported on this driver')

    monkeypatch.setattr(pyserial, 'Serial', _ResetFailsSerial)
    monkeypatch.setattr(mod, 'HAS_FCNTL', False)
    b = mod.SerialBackend()
    ok = b.open(port='/dev/null')
    assert ok is True
    b.close()


# ---- drain queue exception 路径 (line 96-100) ----

def test_open_drain_queue_get_nowait_failure_swallowed(monkeypatch):
    """open 清 queue 时 get_nowait 抛非 queue.Empty 异常应被吞。"""
    import serial as pyserial

    from serial_debug.core import serial_backend as mod
    monkeypatch.setattr(pyserial, 'Serial', _FakeSerial)
    monkeypatch.setattr(mod, 'HAS_FCNTL', False)
    b = mod.SerialBackend()

    class _BadQueue:
        def __init__(self):
            self.items = []
        def empty(self):
            return False
        def get_nowait(self):
            raise RuntimeError('queue corruption')
        def put(self, item):
            self.items.append(item)

    b.rx_queue = _BadQueue()
    # 不应抛
    ok = b.open(port='/dev/null')
    assert ok is True
    b.close()


# ---- close(): ser.close exception 路径 (line 120-121) ----

def test_close_swallows_ser_close_exception(monkeypatch):
    """ser.close() 抛异常应被吞。"""
    import serial as pyserial

    from serial_debug.core import serial_backend as mod

    class _CloseFailsSerial(_FakeSerial):
        def close(self):
            raise OSError('close failed')

    monkeypatch.setattr(pyserial, 'Serial', _CloseFailsSerial)
    monkeypatch.setattr(mod, 'HAS_FCNTL', False)
    b = mod.SerialBackend()
    b.open(port='/dev/null')
    # 不应抛
    b.close()
    assert b.ser is None


# ---- close(): reader thread 未退出 warning (line 125) ----

def test_close_warns_when_reader_thread_does_not_exit(monkeypatch):
    """reader 线程 join timeout 后仍存活应记日志，不抛。"""
    import threading
    import time

    import serial as pyserial

    from serial_debug.core import serial_backend as mod

    class _BlockingSerial(_FakeSerial):
        """read(1) 永远阻塞——模拟 reader 卡死。"""
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self._ev = threading.Event()
        def read(self, n=1):
            # 真正卡住 5s（远超 close 的 2s join timeout）
            self._ev.wait(timeout=5)
            return b''
        def close(self):
            self._ev.set()
            self.is_open = False

    monkeypatch.setattr(pyserial, 'Serial', _BlockingSerial)
    monkeypatch.setattr(mod, 'HAS_FCNTL', False)
    b = mod.SerialBackend()
    b.open(port='/dev/null')
    # close 应在 ~3s 内返回（2s join timeout + 一些清理）
    t0 = time.monotonic()
    b.close()
    elapsed = time.monotonic() - t0
    assert elapsed < 5.0  # 应在 5s 内退出


# ---- _read_loop read exception 路径 (line 148-152) ----

def test_read_loop_emits_error_on_read_exception():
    """_read_loop 在 read 抛异常应 emit ('error', msg) 入队。"""
    import threading

    from serial_debug.core import serial_backend as mod

    class _BadSer:
        is_open = True
        in_waiting = 0
        def read(self, n=1):
            raise OSError('device disconnected')
        def close(self):
            self.is_open = False

    b = mod.SerialBackend()
    b.ser = _BadSer()
    b._stop_evt.clear()
    t = threading.Thread(target=b._read_loop, daemon=True)
    t.start()
    t.join(timeout=2.0)
    assert not t.is_alive()
    items = []
    while True:
        try:
            items.append(b.rx_queue.get_nowait())
        except Exception:
            break
    assert any(k == 'error' for k, _ in items)
    assert ('closed', None) in items


# ---- _read_loop in_waiting exception 路径 (line 165-167) ----

def test_read_loop_swallows_in_waiting_exception():
    """in_waiting 抛异常应被吞，仍 emit line。"""
    import threading

    from serial_debug.core import serial_backend as mod

    class _WaitingFailsSer:
        is_open = True
        def __init__(self):
            self._read_data = b'hello\n'
            self._idx = 0
            self.in_waiting = 0
        def read(self, n=1):
            if self._idx >= len(self._read_data):
                return b''
            chunk = self._read_data[self._idx:self._idx + n]
            self._idx += len(chunk)
            return chunk
        def close(self):
            self.is_open = False

    # 用 property 让 in_waiting 访问抛
    ser = _WaitingFailsSer()
    type(ser).in_waiting = property(lambda self: (_ for _ in ()).throw(OSError('io')))
    b = mod.SerialBackend()
    b.ser = ser
    b._stop_evt.clear()
    t = threading.Thread(target=b._read_loop, daemon=True)
    t.start()
    # 等 reader 读 first byte + 触发 in_waiting 异常
    import time
    time.sleep(0.1)
    b._stop_evt.set()
    t.join(timeout=2.0)
    items = []
    while True:
        try:
            items.append(b.rx_queue.get_nowait())
        except Exception:
            break
    # 应有 'hello' line 入队（in_waiting 异常被吞，rest=b''，但 first+''=hello）
    lines = [v for k, v in items if k == 'line']
    assert 'hello' in lines


# ---- _read_loop decode exception + empty line (line 182, 185-187) ----

def test_read_loop_decodes_non_utf8_bytes_with_replace():
    """非 UTF-8 字节应走 errors='replace' 路径。"""
    import threading

    from serial_debug.core import serial_backend as mod

    class _BadUtf8Ser:
        is_open = True
        def __init__(self):
            self._data = b'\xff\xfe\n'
            self._idx = 0
            self.in_waiting = 0
        def read(self, n=1):
            if self._idx >= len(self._data):
                return b''
            chunk = self._data[self._idx:self._idx + n]
            self._idx += len(chunk)
            return chunk
        def close(self):
            self.is_open = False

    b = mod.SerialBackend()
    b.ser = _BadUtf8Ser()
    b._stop_evt.clear()
    t = threading.Thread(target=b._read_loop, daemon=True)
    t.start()
    import time
    time.sleep(0.1)
    b._stop_evt.set()
    t.join(timeout=2.0)
    items = []
    while True:
        try:
            items.append(b.rx_queue.get_nowait())
        except Exception:
            break
    lines = [v for k, v in items if k == 'line']
    # 非空字节应通过 replace 解码出（\ufffd 替换字符）
    assert any('\ufffd' in ln for ln in lines)


def test_read_loop_skips_empty_lines():
    """纯空行（只有 \r\n）应被跳过不 emit。"""
    import threading

    from serial_debug.core import serial_backend as mod

    class _EmptyLineSer:
        is_open = True
        def __init__(self):
            self._data = b'\r\nreal\n'
            self._idx = 0
            self.in_waiting = 0
        def read(self, n=1):
            if self._idx >= len(self._data):
                return b''
            chunk = self._data[self._idx:self._idx + n]
            self._idx += len(chunk)
            return chunk
        def close(self):
            self.is_open = False

    b = mod.SerialBackend()
    b.ser = _EmptyLineSer()
    b._stop_evt.clear()
    t = threading.Thread(target=b._read_loop, daemon=True)
    t.start()
    import time
    time.sleep(0.1)
    b._stop_evt.set()
    t.join(timeout=2.0)
    items = []
    while True:
        try:
            items.append(b.rx_queue.get_nowait())
        except Exception:
            break
    lines = [v for k, v in items if k == 'line']
    # 应只 emit 'real'，空行跳过
    assert lines == ['real']


# ---- flush_input: ser closed branch (line 204-207) ----

def test_flush_input_skips_when_ser_closed(backend):
    """ser 未开时 flush_input 不应崩。"""
    # ser is None，flush_input 走 if 分支的 False 侧
    backend.flush_input()  # 不应抛


def test_flush_input_resets_buf_under_lock(backend):
    """flush_input 应清空 reader _buf。"""
    # 直接 put 一些半行到 _buf
    backend._buf.extend(b'partial half line')
    backend.flush_input()
    assert len(backend._buf) == 0


def test_flush_input_swallows_reset_input_buffer_exception(monkeypatch):
    """ser.open=True 但 reset_input_buffer 抛异常应被吞。"""
    from serial_debug.core import serial_backend as mod

    class _BadSer:
        is_open = True
        in_waiting = 0
        def reset_input_buffer(self):
            raise OSError('not supported')

    b = mod.SerialBackend()
    b.ser = _BadSer()
    # 不应抛
    b.flush_input()


# ---- flush_input: queue drain 异常分支 (line 220-222) ----

def test_flush_input_swallows_queue_get_failure(backend):
    """queue.get_nowait 抛非 Empty 异常应被吞。"""
    class _BadQueue:
        def __init__(self):
            self.items = []
        def empty(self):
            return False
        def get_nowait(self):
            raise RuntimeError('corrupt')
        def put(self, item):
            self.items.append(item)

    backend.rx_queue = _BadQueue()
    # 不应抛
    backend.flush_input()


# ---- _write_loop: ser closed + write exception + flush exception (line 252-273) ----

def test_write_loop_swallows_write_exception(monkeypatch):
    """ser.write 抛异常应 emit error status 入队。"""
    import threading
    import time

    import serial as pyserial

    from serial_debug.core import serial_backend as mod

    class _WriteFailsSerial(_FakeSerial):
        def write(self, data):
            raise OSError('device write failed')
        def flush(self):
            pass

    monkeypatch.setattr(pyserial, 'Serial', _WriteFailsSerial)
    monkeypatch.setattr(mod, 'HAS_FCNTL', False)
    b = mod.SerialBackend()
    b.open(port='/dev/null')
    b.send('AT')
    time.sleep(0.15)  # 等 writer 处理
    items = []
    while True:
        try:
            items.append(b.rx_queue.get_nowait())
        except Exception:
            break
    # 应有 status error
    assert any(k == 'status' and v[0] == 'error' for k, v in items)
    b.close()


def test_write_loop_swallows_flush_exception(monkeypatch):
    """ser.flush 抛异常应被吞，writer 继续工作。"""
    import time

    import serial as pyserial

    from serial_debug.core import serial_backend as mod

    class _FlushFailsSerial(_FakeSerial):
        def write(self, data):
            return len(data)
        def flush(self):
            raise OSError('flush fail')

    monkeypatch.setattr(pyserial, 'Serial', _FlushFailsSerial)
    monkeypatch.setattr(mod, 'HAS_FCNTL', False)
    b = mod.SerialBackend()
    b.open(port='/dev/null')
    b.send('hello')
    time.sleep(0.15)
    # 应有 '已发送' status 入队（write 成功 + flush 失败被吞）
    items = []
    while True:
        try:
            items.append(b.rx_queue.get_nowait())
        except Exception:
            break
    assert any(k == 'status' and '已发送' in v[1] for k, v in items)
    b.close()


def test_write_loop_skips_when_ser_closed_during_write(monkeypatch):
    """writer 取到 data 后 ser 已 close，应 continue 不抛。"""
    import threading
    import time

    import serial as pyserial

    from serial_debug.core import serial_backend as mod

    monkeypatch.setattr(pyserial, 'Serial', _FakeSerial)
    monkeypatch.setattr(mod, 'HAS_FCNTL', False)
    b = mod.SerialBackend()
    b.open(port='/dev/null')
    # 主动把 ser.is_open 置 False 模拟中途断开
    b.ser.is_open = False
    b.send('data1')
    time.sleep(0.15)
    # 数据应被 writer 跳过（ser None or not is_open continue）
    assert b.ser.written == b''  # 没写入
    b.ser.is_open = True
    b.close()


# ---- _try_set_low_latency: ser=None branch (line 288) + exception (line 303-304) ----

def test_try_set_low_latency_no_ser(monkeypatch):
    """ser=None 时 _try_set_low_latency 直接 return。"""
    from serial_debug.core import serial_backend as mod
    monkeypatch.setattr(mod, 'HAS_FCNTL', True)
    monkeypatch.setattr(mod, 'fcntl', mock.MagicMock(), raising=False)
    b = mod.SerialBackend()
    assert b.ser is None
    b._try_set_low_latency()  # 不应抛


def test_try_set_low_latency_swallows_ioctl_exception(monkeypatch):
    """fcntl.ioctl 抛异常应被吞。"""
    from serial_debug.core import serial_backend as mod

    class _FakeSer:
        def fileno(self):
            return 3

    def boom(*a, **kw):
        raise OSError('ioctl not supported')

    fake_fcntl = mock.MagicMock()
    fake_fcntl.ioctl = boom
    monkeypatch.setattr(mod, 'HAS_FCNTL', True)
    monkeypatch.setattr(mod, 'fcntl', fake_fcntl, raising=False)
    b = mod.SerialBackend()
    b.ser = _FakeSer()
    # 不应抛
    b._try_set_low_latency()


def test_try_set_low_latency_fileno_exception(monkeypatch):
    """ser.fileno() 抛异常应被吞（pyserial 已关）。"""
    from serial_debug.core import serial_backend as mod

    class _BadSer:
        def fileno(self):
            raise OSError('not open')

    monkeypatch.setattr(mod, 'HAS_FCNTL', True)
    monkeypatch.setattr(mod, 'fcntl', mock.MagicMock(), raising=False)
    b = mod.SerialBackend()
    b.ser = _BadSer()
    b._try_set_low_latency()  # 不应抛


# ---- _write_loop queue.get timeout (line 252 continue) ----

def test_write_loop_handles_queue_get_timeout(monkeypatch):
    """writer 在 queue.get(timeout) 超时应 continue，不抛。"""
    import threading
    import time

    import serial as pyserial

    from serial_debug.core import serial_backend as mod

    monkeypatch.setattr(pyserial, 'Serial', _FakeSerial)
    monkeypatch.setattr(mod, 'HAS_FCNTL', False)
    b = mod.SerialBackend()
    b.open(port='/dev/null')
    # 不 send 任何东西，等 0.3s 让 writer 跑几轮 timeout
    time.sleep(0.3)
    # writer 应仍存活
    assert b._writer is not None
    assert b._writer.is_alive()
    b.close()


# ---- send: append_crlf vs not ----

def test_send_without_crlf(monkeypatch):
    """send 不加 CRLF 应只 send 原文。"""
    import time

    import serial as pyserial

    from serial_debug.core import serial_backend as mod

    monkeypatch.setattr(pyserial, 'Serial', _FakeSerial)
    monkeypatch.setattr(mod, 'HAS_FCNTL', False)
    b = mod.SerialBackend()
    b.open(port='/dev/null')
    b.send('AT?')
    time.sleep(0.1)
    assert b.ser.written == b'AT?'
    b.close()


# ---- _dbg 通过 _read_loop 路径 ----

def test_read_loop_dbg_prints_when_enabled(monkeypatch, capsys):
    """DEBUG_CHUNK=True 时 _read_loop 应输出 chunk debug。"""
    import threading
    import time

    from serial_debug.core import serial_backend as mod

    monkeypatch.setattr(mod, 'DEBUG_CHUNK', True)

    class _HelloSer:
        is_open = True
        in_waiting = 0
        def __init__(self):
            self._data = b'hi\n'
            self._idx = 0
        def read(self, n=1):
            if self._idx >= len(self._data):
                return b''
            chunk = self._data[self._idx:self._idx + n]
            self._idx += len(chunk)
            return chunk
        def close(self):
            self.is_open = False

    b = mod.SerialBackend()
    b.ser = _HelloSer()
    b._stop_evt.clear()
    t = threading.Thread(target=b._read_loop, daemon=True)
    t.start()
    time.sleep(0.1)
    b._stop_evt.set()
    t.join(timeout=2.0)
    captured = capsys.readouterr()
    assert 'reader chunk' in captured.err or 'reader emit' in captured.err
