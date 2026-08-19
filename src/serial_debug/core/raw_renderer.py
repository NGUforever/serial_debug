"""原始数据缓冲与渲染辅助。

为 qt_app.py 的"原始数据显示模式"提供：
- `RawBuffer`：有界累积缓冲，按 \\n 切行，超过 `max_lines` 自动丢弃最旧行
- `format_raw_line`：把单行文本转成"可安全插入 QPlainTextEdit"的字符串
- `decode_chunk`：把串口收到的 bytes 按 utf-8 解码（errors='replace' 兜底）

设计原则：
- 不依赖任何 GUI 框架（PySide6/tkinter），可在测试中纯函数调用
- 跨平台安全：只用 stdlib，无 fcntl/termios/msvcrt 等平台特定 import
- 行尾统一化：把 \\r\\n / \\r 残留归一化为 \\n，但保留行内可见字符
- 不丢字符：decode 失败用 replacement char（\\ufffd）代替，不抛异常

参考 Agent-Head/memory/FACT.md 教训 5：不裸 except Exception: pass，
decode 失败时调用方仍能拿到字符串而非异常。
"""
from __future__ import annotations

from collections import deque
from collections.abc import Iterable

__all__ = [
    'RawBuffer',
    'decode_chunk',
    'format_raw_line',
    'DEFAULT_RAW_MAX_LINES',
]

# 开机日志可能很长，但 UI 不能无限堆积。默认保留最后 50000 行，
# 与 qt_app.MAX_LINES 同量级，避免长时间开机刷屏吃光内存。
DEFAULT_RAW_MAX_LINES = 50_000


def decode_chunk(data: bytes, encoding: str = 'utf-8') -> str:
    """把串口收到的 bytes 解码成字符串，失败用 replacement char 兜底。

    与 serial_backend._read_loop 的 decode 策略一致：utf-8 + errors='replace'，
    任何解码错误都不抛异常，避免 reader 线程崩。
    """
    if not isinstance(data, (bytes, bytearray)):
        # 防御：调用方传 str 或其他类型，直接 str() 兜底
        return str(data)
    try:
        return bytes(data).decode(encoding, errors='replace')
    except LookupError:
        # 未知编码名：fallback 到 utf-8
        return bytes(data).decode('utf-8', errors='replace')
    except Exception:
        # 理论上不会到这（errors='replace' 已兜底），防御性返回空
        return ''


def format_raw_line(line: str) -> str:
    """把单行文本（不带 \\n）归一化成"可插入 QPlainTextEdit"的显示行。

    - 去掉行尾残留的 \\r（serial_backend 已按 \\n 切行，但 \\r\\n 的 \\r 残留要清掉）
    - 末尾补 \\n：QPlainTextEdit 按 \\n 切块，不补 \\n 会拼到下一行前
    - 不做时间戳转换、不做响应分类：原始模式的核心特征
    - 控制字符（除 \\t）：用 \\ufffd 替代避免破坏 QPlainTextEdit 渲染
      （开机 printf 可能含 \\x00 / \\x1b 等）
    """
    if line is None:
        return '\n'
    s = line
    if not isinstance(s, str):
        s = decode_chunk(s) if isinstance(s, (bytes, bytearray)) else str(s)
    # 去尾 \r（serial_backend 按 \n 切，\r\n 的 \r 残留要清掉）
    # 注意：只去末尾 \r，行内 \r 保留——某些协议用 \r 作行内分隔
    if s.endswith('\r'):
        s = s[:-1]
    # 替换破坏性控制字符（0x00-0x08, 0x0b, 0x0c, 0x0e-0x1f）为 replacement char
    # 保留 \t (0x09) / \n (0x0a) / \r (0x0d)：
    #   - \r 在原始模式下可能承载协议信息，保留可读性更友好
    out_chars: list[str] = []
    for ch in s:
        o = ord(ch)
        if o < 0x20 and ch not in '\t\r':
            out_chars.append('\ufffd')
        else:
            out_chars.append(ch)
    return ''.join(out_chars) + '\n'


class RawBuffer:
    """有界原始文本缓冲。

    - 串口行陆续到达，按 \\n 切分后 append
    - 超过 max_lines 时丢弃最旧行（FIFO，deque 自动处理）
    - 提供 snapshot() 返回当前所有行的列表（不带 \\n）
    - 提供 recent(n) 返回最后 n 行

    线程安全：内部用 list/deque，调用方（qt_app._drain）在主线程调用，
    与 reader 线程不直接共享本对象（reader 把 line 经 rx_queue 传过来）。
    本对象本身不持锁——调用方需保证单线程访问。
    """

    def __init__(self, max_lines: int = DEFAULT_RAW_MAX_LINES):
        if max_lines <= 0:
            raise ValueError(f'max_lines must be > 0, got {max_lines}')
        self._max_lines = max_lines
        self._lines: deque[str] = deque(maxlen=max_lines)
        # 半行缓冲：串口可能在行中间断流（虽然 serial_backend 已按 \\n 切，
        # 但 RawBuffer 也支持 chunk 拼接用法）
        self._partial = ''

    @property
    def max_lines(self) -> int:
        return self._max_lines

    def __len__(self) -> int:
        return len(self._lines)

    def clear(self) -> None:
        """清空所有行与半行缓冲。"""
        self._lines.clear()
        self._partial = ''

    def append_line(self, line: str) -> None:
        """追加一行（不带 \\n）。超过上限自动丢最旧。"""
        if line is None:
            return
        # 去尾 \r 残留
        if line.endswith('\r'):
            line = line[:-1]
        self._lines.append(line)

    def append_chunk(self, chunk: str) -> list[str]:
        """追加一段可能跨多行的文本，返回本次切出的完整行列表。

        - 末尾不完整的行留 _partial 等下次拼接
        - 返回的行不带 \\n，调用方可用 format_raw_line 转 QPlainTextEdit 文本
        """
        if not chunk:
            return []
        self._partial += chunk
        if '\n' not in self._partial:
            return []
        parts = self._partial.split('\n')
        # 最后一段是不完整行，留 _partial
        self._partial = parts[-1]
        complete = parts[:-1]
        # 去尾 \r 残留（\r\n 切完后 \r 残留），与 append_line 行为一致
        # 但返回值保留切出的原始行（不返含 \r），便于调用方直接 format_raw_line
        cleaned: list[str] = []
        for ln in complete:
            if ln.endswith('\r'):
                ln = ln[:-1]
            self._lines.append(ln)
            cleaned.append(ln)
        return cleaned

    def snapshot(self) -> list[str]:
        """返回当前所有行（不带 \\n）。"""
        return list(self._lines)

    def recent(self, n: int) -> list[str]:
        """返回最后 n 行（不带 \\n）。n <= 0 返回空列表。"""
        if n <= 0:
            return []
        if n >= len(self._lines):
            return list(self._lines)
        # deque 切片：转 list 取尾
        return list(self._lines)[-n:]

    def replace_all(self, lines: Iterable[str]) -> None:
        """清空后用 lines 重新填充（不触发上限检查，按顺序保留最后 max_lines 行）。"""
        self._lines.clear()
        self._partial = ''
        for ln in lines:
            if ln is None:
                continue
            if ln.endswith('\r'):
                ln = ln[:-1]
            self._lines.append(ln)
