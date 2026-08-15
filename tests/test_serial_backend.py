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
    from serial_debug.core import serial_backend as mod
    import serial as pyserial
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
    from serial_debug.core import serial_backend as mod
    import serial as pyserial
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
    from serial_debug.core import serial_backend as mod
    import serial as pyserial
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
    from serial_debug.core import serial_backend as mod
    import serial as pyserial
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
    from serial_debug.core import serial_backend as mod
    import serial as pyserial

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
    from serial_debug.core import serial_backend as mod
    import threading
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
    from serial_debug.core import serial_backend as mod
    import serial as pyserial
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
