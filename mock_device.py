"""模拟设备：用于无真机时端到端测试。

打开一个串口端口，周期打印日志行；解析收到的 AT 命令并回响应：
  AT+ELOG=0%  -> 停止周期日志，回 OK,0%
  AT+ELOG=1%  -> 恢复周期日志，回 OK,1%
  AT+<X>=<V>% -> 回 OK,<V>%
  其他        -> 回 ERROR,unknown

用法:
  python3 mock_device.py /dev/pts/X
"""
from __future__ import annotations

import sys
import time
import re
import threading
import serial

CMD_RE = re.compile(r'^AT\+([A-Z]+)(?:=(.+))?%$')


class MockDevice:
    def __init__(self, port: str, baudrate: int = 115200):
        self.ser = serial.Serial(port, baudrate, timeout=0.2)
        self.elog_on = True
        self._stop = threading.Event()
        self._seq = 0

    def run(self):
        log_thread = threading.Thread(target=self._log_loop, daemon=True)
        log_thread.start()
        buf = bytearray()
        while not self._stop.is_set():
            try:
                chunk = self.ser.read(4096)
            except Exception as e:
                print(f'[mock] read err: {e}', file=sys.stderr)
                break
            if not chunk:
                continue
            buf.extend(chunk)
            while b'\n' in buf or b'%' in buf:
                # 以换行或 % 为一条命令边界
                nidx = buf.find(b'\n')
                pidx = buf.find(b'%')
                if nidx < 0 and pidx < 0:
                    break
                if nidx >= 0 and (pidx < 0 or nidx <= pidx):
                    end = nidx
                else:
                    end = pidx
                raw = bytes(buf[:end + 1])
                del buf[:end + 1]
                raw = raw.strip().rstrip(b'\r\n')
                cmd = raw.decode('utf-8', errors='replace').strip()
                if cmd:
                    self._handle(cmd)
        self._stop.set()

    def _handle(self, cmd: str):
        m = CMD_RE.match(cmd)
        if not m:
            self._reply(f'ERROR,unknown')
            return
        name = m.group(1)
        val = m.group(2) or ''
        if name == 'ELOG':
            if val == '0':
                self.elog_on = False
                self._reply('OK,0%')
            elif val == '1':
                self.elog_on = True
                self._reply('OK,1%')
            else:
                self._reply(f'OK,{val}%')
        else:
            self._reply(f'OK,{val}%')

    def _reply(self, text: str):
        self.ser.write((text + '\n').encode('utf-8'))
        try:
            self.ser.flush()
        except Exception:
            pass

    def _log_loop(self):
        while not self._stop.is_set():
            if self.elog_on:
                self._seq += 1
                line = f'temp=36.{(self._seq % 5)+5}, bat=3.{70 + (self._seq % 3)}, cnt={self._seq}'
                try:
                    self.ser.write((line + '\n').encode('utf-8'))
                    self.ser.flush()
                except Exception:
                    pass
            time.sleep(1.0)


def main():
    if len(sys.argv) < 2:
        print('用法: python3 mock_device.py <port> [baud]', file=sys.stderr)
        sys.exit(1)
    port = sys.argv[1]
    baud = int(sys.argv[2]) if len(sys.argv) > 2 else 115200
    print(f'[mock] open {port}@{baud}')
    MockDevice(port, baud).run()


if __name__ == '__main__':
    main()
