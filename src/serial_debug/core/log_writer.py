"""实时日志写入器。

独立线程消费队列、批量落盘。主线程/读线程只做非阻塞 queue.put，
磁盘 I/O 完全隔离在写入线程——即便落盘很慢，也不会丢串口字节、
不会拖慢收发与显示（吸取之前"读线程里 flush 落盘丢 OK,0%"的教训）。
"""
from __future__ import annotations

import queue
import threading
import time

from serial_debug.infra.error_log import log_exc


class LogWriter:
    def __init__(self, path: str):
        self.path = path
        self._q: queue.Queue[str | None] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._f = None
        self.error: str | None = None

    def start(self) -> bool:
        try:
            self._f = open(self.path, 'a', encoding='utf-8')
            self._f.write(f"=== log start {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        except Exception as e:
            self.error = str(e)
            self._f = None
            log_exc('error', f'{__file__}:LogWriter.start open failed path={self.path}', e)
            return False
        self._thread = threading.Thread(target=self._run, name='log-writer',
                                        daemon=True)
        self._thread.start()
        return True

    def put(self, text: str):
        """主线程调用：非阻塞入队。文件未打开时丢弃。"""
        if self._f is not None:
            self._q.put(text)

    def _run(self):
        last_flush = time.monotonic()
        while not self._stop.is_set():
            try:
                text = self._q.get(timeout=0.2)
            except queue.Empty:
                # 空闲时也定期 flush，保证“实时”落盘
                now = time.monotonic()
                if now - last_flush >= 0.2:
                    self._flush()
                    last_flush = now
                continue
            if text is None:  # 停止哨兵
                break
            try:
                self._f.write(text)
            except Exception as e:
                # 磁盘满 / 设备拔出等：记日志，不退出线程（继续消费 queue
                # 避免上游 put 端 queue 无限增长）
                log_exc('error', f'{__file__}:LogWriter._run write failed', e)
            now = time.monotonic()
            if now - last_flush >= 0.2:
                self._flush()
                last_flush = now
        # 收尾：把队列里剩余的全写出去
        try:
            while True:
                text = self._q.get_nowait()
                if text is None:
                    break
                self._f.write(text)
        except queue.Empty:
            pass
        except Exception as e:
            log_exc('warning', f'{__file__}:LogWriter._run drain tail failed', e)
        try:
            self._f.write(f"=== log stop {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        except Exception as e:
            log_exc('warning', f'{__file__}:LogWriter._run stop marker failed', e)
        self._flush()
        try:
            self._f.close()
        except Exception as e:
            log_exc('warning', f'{__file__}:LogWriter._run close failed', e)

    def _flush(self):
        try:
            self._f.flush()
        except Exception as e:
            log_exc('warning', f'{__file__}:LogWriter._flush failed', e)

    def stop(self):
        if self._thread is None:
            return
        self._stop.set()
        self._q.put(None)
        self._thread.join(timeout=2.0)
        if self._thread.is_alive():
            log_exc('warning', f'{__file__}:LogWriter.stop thread did not exit in 2s', None)
        self._thread = None
