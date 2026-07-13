"""串口后端：开/关串口、读线程按行切分入队、发送。

读线程只负责把字节拼成完整行丢进 rx_queue；所有 GUI 写入由主线程
经 after() 轮询队列完成（tkinter 非线程安全）。"""
from __future__ import annotations

import threading
import queue
import serial
import serial.tools.list_ports
import sys
import time
import fcntl
import struct
import glob

# Debug 开关：把 reader 每次读到的 chunk 时间戳打到 stderr
# 用法：从终端启动 `python3 app.py 2>debug.log`，复现后看 debug.log
# 定位用：对比 usbmon 时间戳，确定数据卡在哪一层
DEBUG_CHUNK = False


def _dbg(msg: str):
    if DEBUG_CHUNK:
        print(f'[{time.time():.3f}] {msg}', file=sys.stderr, flush=True)


class SerialBackend:
    def __init__(self, on_status=None):
        self.ser: serial.Serial | None = None
        self._reader: threading.Thread | None = None
        self._stop_evt = threading.Event()
        self._write_lock = threading.Lock()
        self._buf_lock = threading.Lock()
        self._buf = bytearray()
        self.rx_queue: queue.Queue = queue.Queue()
        self.on_status = on_status  # 回调: (level, msg)  level in {'info','error'}

    # ---- 端口枚举 -------------------------------------------------------
    @staticmethod
    def list_ports():
        """枚举可用串口。

        pyserial 的 list_ports_linux 不扫非标准命名的 tty（如 WCH ch343
        vendor 驱动生成的 /dev/ttyCH343USBx），手动 glob 兜底补齐。
        """
        seen = {p.device for p in serial.tools.list_ports.comports()}
        for pat in ('/dev/ttyCH343USB*', '/dev/ttyUSB*',
                    '/dev/ttyACM[0-9]*', '/dev/ttyXRUSB*'):
            for path in glob.glob(pat):
                seen.add(path)
        return sorted(seen)

    # ---- 连接 / 断开 ----------------------------------------------------
    def open(self, port: str, baudrate: int = 115200,
             bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
             stopbits=serial.STOPBITS_ONE, rtscts: bool = False) -> bool:
        if self.ser is not None:
            self.close()
        try:
            # timeout=None → VMIN=1, VTIME=0：阻塞读到第一个字节就返回
            # 与 minicom/picocom 一致，避免 cdc_acm 驱动在小数据包上延迟交付
            self.ser = serial.Serial(
                port=port, baudrate=baudrate,
                bytesize=bytesize, parity=parity, stopbits=stopbits,
                rtscts=rtscts,
                timeout=None, write_timeout=1.0,
            )
        except Exception as e:
            self._status('error', f'打开串口失败: {e}')
            self.ser = None
            return False
        # 打开后立刻清空硬件缓冲，避免上次残留数据或启动时的积压数据涌入界面
        try:
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()
        except Exception:
            pass
        # 设置 ASYNC_LOW_LATENCY（CH343 可能不支持，失败也无所谓）
        self._try_set_low_latency()
        # 清空上次会话残留的队列项
        while not self.rx_queue.empty():
            try:
                self.rx_queue.get_nowait()
            except Exception:
                break
        self._stop_evt.clear()
        self._reader = threading.Thread(
            target=self._read_loop, name='serial-reader', daemon=True)
        self._reader.start()
        self._status('info', f'已连接 {port} @ {baudrate}')
        return True

    def close(self):
        self._stop_evt.set()
        # 先关 fd，解除 reader 阻塞在 read(1) 上的等待
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
        if self._reader is not None:
            self._reader.join(timeout=1.0)
            self._reader = None
        if self.ser is not None:
            self.ser = None
        self._status('info', '已断开')

    @property
    def is_open(self) -> bool:
        return self.ser is not None and self.ser.is_open

    # ---- 读线程 ---------------------------------------------------------
    def _read_loop(self):
        ser = self.ser
        while not self._stop_evt.is_set():
            try:
                if ser is None or not ser.is_open:
                    break
                # timeout=None → VMIN=1 VTIME=0：阻塞到第一个字节就返回
                # 与 minicom/picocom 一致；close() 关 fd 解除阻塞
                first = ser.read(1)
            except Exception as e:
                if self._stop_evt.is_set():
                    break
                self.rx_queue.put(('error', f'读取异常: {e}'))
                break
            if not first:
                # read 返回空：fd 被关（close 调用）或异常
                if self._stop_evt.is_set():
                    break
                continue
            # 读完 OS buf 剩余字节
            try:
                rest = b''
                n = ser.in_waiting
                while n:
                    rest += ser.read(n)
                    n = ser.in_waiting
            except Exception:
                rest = b''
            chunk = first + rest
            _dbg(f'reader chunk {len(chunk)}B in_waiting_was={n if "n" in dir() else "?"} '
                 f'preview={chunk[:60]!r}')
            # 累积到共享 buf，按 \n 切完整行发出
            with self._buf_lock:
                self._buf.extend(chunk)
                while True:
                    idx = self._buf.find(b'\n')
                    if idx < 0:
                        break
                    line = bytes(self._buf[:idx])
                    del self._buf[:idx + 1]
                    line = line.rstrip(b'\r')
                    if not line:
                        continue  # 跳过纯空行
                    try:
                        text = line.decode('utf-8', errors='replace')
                    except Exception:
                        text = repr(line)
                    _dbg(f'reader emit line: {text[:80]!r}')
                    self.rx_queue.put(('line', text))
        # 线程退出：清空 buf
        with self._buf_lock:
            self._buf.clear()
        self.rx_queue.put(('closed', None))

    # ---- 发送 -----------------------------------------------------------
    def flush_input(self):
        """清空 OS 接收缓冲、reader 线程的 buf、rx_queue 残留行。

        发送 AT 命令前调用，确保响应不被旧周期日志淹没。
        响应是命令发出后才到，不会误清。
        """
        _dbg(f'flush_input start, in_waiting={self.ser.in_waiting if self.ser and self.ser.is_open else "closed"}')
        if self.ser is not None and self.ser.is_open:
            try:
                self.ser.reset_input_buffer()
            except Exception:
                pass
        # 清掉 reader 线程累积的半行 buf，防止 OK,0% 被拼到旧日志行末尾
        with self._buf_lock:
            self._buf.clear()
        # 清 queue 里的 line 项；保留 status/error/closed 控制消息
        # 注意：不能「取出非 line 再 put 回」，否则 queue 永不空，死循环
        dropped = 0
        keep = []
        while True:
            try:
                item = self.rx_queue.get_nowait()
            except queue.Empty:
                break
            except Exception:
                break
            if item[0] != 'line':
                keep.append(item)
            else:
                dropped += 1
        for item in keep:
            self.rx_queue.put(item)
        _dbg(f'flush_input done, dropped {dropped} line items')

    def send(self, text: str, append_crlf: bool = False) -> bool:
        _dbg(f'send: {text!r} append_crlf={append_crlf}')
        if self.ser is None or not self.ser.is_open:
            self._status('error', '串口未连接，无法发送')
            return False
        data = text.encode('utf-8')
        if append_crlf:
            data += b'\r\n'
        try:
            with self._write_lock:
                n = self.ser.write(data)
                try:
                    self.ser.flush()
                except Exception:
                    pass
        except Exception as e:
            self._status('error', f'发送失败: {e}')
            return False
        _dbg(f'send wrote {n} bytes, in_waiting={self.ser.in_waiting}')
        self._status('info', f'已发送 {n} 字节')
        return True

    def _try_set_low_latency(self):
        """尝试设置 ASYNC_LOW_LATENCY，让内核低延迟处理 USB 数据。

        usbmon 证据：CH343 的 bulk IN FIFO 需要数据达到阈值才主动发，
        OK 响应（几字节）卡在 FIFO 直到周期日志涌入才被推出。
        ASYNC_LOW_LATENCY 让内核更主动从 USB 拷贝 short packet。
        cdc_acm 驱动可能不支持 TIOCSSERIAL，失败也无所谓。
        """
        if self.ser is None:
            return
        TIOCGSERIAL = 0x541E
        TIOCSSERIAL = 0x5421
        ASYNC_LOW_LATENCY = 0x2000
        try:
            fd = self.ser.fileno()
            # serial_struct 大小约 80 字节，用 256B 缓冲足够
            buf = bytearray(256)
            fcntl.ioctl(fd, TIOCGSERIAL, buf)
            # flags 在 offset 16（type, line, port, irq 之后）
            old_flags = struct.unpack_from('i', buf, 16)[0]
            new_flags = old_flags | ASYNC_LOW_LATENCY
            struct.pack_into('i', buf, 16, new_flags)
            fcntl.ioctl(fd, TIOCSSERIAL, buf)
            _dbg(f'set ASYNC_LOW_LATENCY ok, flags {old_flags:#x} -> {new_flags:#x}')
        except Exception as e:
            _dbg(f'set ASYNC_LOW_LATENCY failed: {e}')

    def _status(self, level: str, msg: str):
        self.rx_queue.put(('status', (level, msg)))
        if self.on_status:
            try:
                self.on_status(level, msg)
            except Exception:
                pass
