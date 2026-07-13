"""串口后端：开/关串口、读线程按行切分入队、发送。

读线程只负责把字节拼成完整行丢进 rx_queue；所有 GUI 写入由主线程
经 after() 轮询队列完成（tkinter 非线程安全）。"""
from __future__ import annotations

import threading
import queue
import serial
import serial.tools.list_ports


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
        return [p.device for p in serial.tools.list_ports.comports()]

    # ---- 连接 / 断开 ----------------------------------------------------
    def open(self, port: str, baudrate: int = 115200,
             bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
             stopbits=serial.STOPBITS_ONE) -> bool:
        if self.ser is not None:
            self.close()
        try:
            self.ser = serial.Serial(
                port=port, baudrate=baudrate,
                bytesize=bytesize, parity=parity, stopbits=stopbits,
                timeout=0.1, write_timeout=1.0,
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
        if self._reader is not None:
            self._reader.join(timeout=1.0)
            self._reader = None
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
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
                chunk = ser.read(4096)   # timeout=0.1
            except Exception as e:
                self.rx_queue.put(('error', f'读取异常: {e}'))
                break
            if not chunk:
                # 读超时：残行不再卡 buf，立即入队，避免设备停发后 OK 响应被拼到残行末尾
                with self._buf_lock:
                    if self._buf:
                        piece = bytes(self._buf).rstrip(b'\r')
                        self._buf.clear()
                        if piece:
                            try:
                                text = piece.decode('utf-8', errors='replace')
                            except Exception:
                                text = repr(piece)
                            self.rx_queue.put(('line', text))
                continue
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
                    self.rx_queue.put(('line', text))
        # 线程退出：残行入队，清空 buf
        with self._buf_lock:
            if self._buf:
                piece = bytes(self._buf).rstrip(b'\r')
                self._buf.clear()
                if piece:
                    try:
                        text = piece.decode('utf-8', errors='replace')
                    except Exception:
                        text = repr(piece)
                    self.rx_queue.put(('line', text))
        self.rx_queue.put(('closed', None))

    # ---- 发送 -----------------------------------------------------------
    def flush_input(self):
        """清空 OS 接收缓冲、reader 线程的 buf、rx_queue 残留行。

        发送 AT 命令前调用，确保响应不被旧周期日志淹没。
        响应是命令发出后才到，不会误清。
        """
        if self.ser is not None and self.ser.is_open:
            try:
                self.ser.reset_input_buffer()
            except Exception:
                pass
        # 清掉 reader 线程累积的半行 buf，防止 OK,0% 被拼到旧日志行末尾
        with self._buf_lock:
            self._buf.clear()
        while not self.rx_queue.empty():
            try:
                kind, _payload = self.rx_queue.get_nowait()
                # 保留 status/error/closed 控制消息，只丢弃 line
                if kind != 'line':
                    self.rx_queue.put((kind, _payload))
            except Exception:
                break

    def send(self, text: str, append_crlf: bool = False) -> bool:
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
        self._status('info', f'已发送 {n} 字节')
        return True

    def _status(self, level: str, msg: str):
        self.rx_queue.put(('status', (level, msg)))
        if self.on_status:
            try:
                self.on_status(level, msg)
            except Exception:
                pass
