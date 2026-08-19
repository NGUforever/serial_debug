"""core/raw_renderer.py 单元测试。

覆盖：
- decode_chunk：bytes 解码、错误兜底、非 bytes 输入
- format_raw_line：行尾归一化、控制字符替换、None/非 str 兜底
- RawBuffer：append_line / append_chunk / snapshot / recent / replace_all / clear / 上限丢弃
"""
from __future__ import annotations

import pytest

from serial_debug.core.raw_renderer import (
    DEFAULT_RAW_MAX_LINES,
    RawBuffer,
    decode_chunk,
    format_raw_line,
)

# ---- decode_chunk ----

def test_decode_chunk_bytes():
    assert decode_chunk(b'hello') == 'hello'


def test_decode_chunk_bytearray():
    assert decode_chunk(bytearray(b'abc')) == 'abc'


def test_decode_chunk_utf8_chinese():
    # 中文 utf-8 多字节
    assert decode_chunk('开机日志'.encode()) == '开机日志'


def test_decode_chunk_invalid_bytes_replaced():
    # 非法 utf-8 字节序列 → replacement char (\ufffd)，不抛异常
    result = decode_chunk(b'\xff\xfe')
    assert '\ufffd' in result
    assert isinstance(result, str)


def test_decode_chunk_empty():
    assert decode_chunk(b'') == ''
    assert decode_chunk(bytearray()) == ''


def test_decode_chunk_non_bytes_returns_str():
    # 调用方传 str → str() 兜底
    assert decode_chunk('abc') == 'abc'
    # 传 int → str()
    assert decode_chunk(42) == '42'


def test_decode_chunk_unknown_encoding_fallback():
    # 未知编码名 → fallback 到 utf-8（仍 errors='replace'）
    result = decode_chunk(b'hello', encoding='nonexistent-encoding')
    assert result == 'hello'


# ---- format_raw_line ----

def test_format_raw_line_plain():
    assert format_raw_line('hello') == 'hello\n'


def test_format_raw_line_strips_trailing_cr():
    # serial_backend 已按 \n 切，\r\n 的 \r 残留要清掉
    assert format_raw_line('hello\r') == 'hello\n'


def test_format_raw_line_strips_only_trailing_cr():
    # 行内 \r（非末尾）保留——某些协议用 \r 作分隔
    assert format_raw_line('a\rb') == 'a\rb\n'


def test_format_raw_line_empty():
    assert format_raw_line('') == '\n'


def test_format_raw_line_none():
    assert format_raw_line(None) == '\n'


def test_format_raw_line_bytes_input():
    # 防御：调用方误传 bytes → decode 后格式化
    assert format_raw_line(b'hello') == 'hello\n'


def test_format_raw_line_preserves_tab():
    assert format_raw_line('a\tb') == 'a\tb\n'


def test_format_raw_line_replaces_control_chars():
    # \x00 / \x01 / \x1b（ESC）/ \x07（BEL）等控制字符 → \ufffd
    line = 'a\x00b\x1bc\x07d'
    out = format_raw_line(line)
    assert '\x00' not in out
    assert '\x1b' not in out
    assert '\x07' not in out
    assert '\ufffd' in out
    # 可见字符保留
    assert 'a' in out and 'b' in out and 'c' in out and 'd' in out


def test_format_raw_line_chinese_preserved():
    assert format_raw_line('开机日志: hello') == '开机日志: hello\n'


def test_format_raw_line_non_str_non_bytes():
    # int → str() 兜底
    assert format_raw_line(42) == '42\n'


# ---- RawBuffer ----

def test_raw_buffer_init_default_max():
    buf = RawBuffer()
    assert buf.max_lines == DEFAULT_RAW_MAX_LINES
    assert len(buf) == 0


def test_raw_buffer_init_custom_max():
    buf = RawBuffer(max_lines=100)
    assert buf.max_lines == 100


def test_raw_buffer_init_invalid_max():
    with pytest.raises(ValueError):
        RawBuffer(max_lines=0)
    with pytest.raises(ValueError):
        RawBuffer(max_lines=-1)


def test_raw_buffer_append_line():
    buf = RawBuffer(max_lines=10)
    buf.append_line('hello')
    buf.append_line('world')
    assert len(buf) == 2
    assert buf.snapshot() == ['hello', 'world']


def test_raw_buffer_append_line_strips_trailing_cr():
    buf = RawBuffer(max_lines=10)
    buf.append_line('hello\r')
    assert buf.snapshot() == ['hello']


def test_raw_buffer_append_line_none_ignored():
    buf = RawBuffer(max_lines=10)
    buf.append_line(None)
    assert len(buf) == 0


def test_raw_buffer_fifo_drop_oldest():
    buf = RawBuffer(max_lines=3)
    buf.append_line('a')
    buf.append_line('b')
    buf.append_line('c')
    buf.append_line('d')  # 触发上限，丢 a
    assert len(buf) == 3
    assert buf.snapshot() == ['b', 'c', 'd']


def test_raw_buffer_clear():
    buf = RawBuffer(max_lines=10)
    buf.append_line('a')
    buf.append_line('b')
    buf.clear()
    assert len(buf) == 0
    assert buf.snapshot() == []


def test_raw_buffer_snapshot_returns_copy():
    buf = RawBuffer(max_lines=10)
    buf.append_line('a')
    snap = buf.snapshot()
    snap.append('b')
    # 修改 snap 不影响 buffer
    assert buf.snapshot() == ['a']


def test_raw_buffer_recent_normal():
    buf = RawBuffer(max_lines=10)
    for i in range(5):
        buf.append_line(f'L{i}')
    assert buf.recent(3) == ['L2', 'L3', 'L4']


def test_raw_buffer_recent_n_exceeds_size():
    buf = RawBuffer(max_lines=10)
    buf.append_line('a')
    buf.append_line('b')
    # n > 实际行数 → 返回全部
    assert buf.recent(100) == ['a', 'b']


def test_raw_buffer_recent_zero_or_negative():
    buf = RawBuffer(max_lines=10)
    buf.append_line('a')
    assert buf.recent(0) == []
    assert buf.recent(-1) == []


def test_raw_buffer_append_chunk_single_line():
    buf = RawBuffer(max_lines=10)
    out = buf.append_chunk('hello\n')
    assert out == ['hello']
    assert buf.snapshot() == ['hello']


def test_raw_buffer_append_chunk_multi_line():
    buf = RawBuffer(max_lines=10)
    out = buf.append_chunk('a\nb\nc\n')
    assert out == ['a', 'b', 'c']
    assert buf.snapshot() == ['a', 'b', 'c']


def test_raw_buffer_append_chunk_partial_kept():
    buf = RawBuffer(max_lines=10)
    out = buf.append_chunk('abc')  # 无 \n，留 _partial
    assert out == []
    assert len(buf) == 0
    # 续上一段
    out = buf.append_chunk('def\n')
    assert out == ['abcdef']
    assert buf.snapshot() == ['abcdef']


def test_raw_buffer_append_chunk_multi_chunk_stitch():
    buf = RawBuffer(max_lines=10)
    buf.append_chunk('hel')
    buf.append_chunk('lo\nwor')
    out = buf.append_chunk('ld\n')
    assert out == ['world']
    assert buf.snapshot() == ['hello', 'world']


def test_raw_buffer_append_chunk_strips_trailing_cr():
    buf = RawBuffer(max_lines=10)
    out = buf.append_chunk('hello\r\nworld\r\n')
    assert out == ['hello', 'world']


def test_raw_buffer_append_chunk_empty():
    buf = RawBuffer(max_lines=10)
    assert buf.append_chunk('') == []
    assert buf.append_chunk('\n') == ['']


def test_raw_buffer_append_chunk_fifo_drop():
    buf = RawBuffer(max_lines=2)
    buf.append_chunk('a\nb\nc\n')  # 3 行，上限 2 → 丢 a
    assert buf.snapshot() == ['b', 'c']


def test_raw_buffer_replace_all():
    buf = RawBuffer(max_lines=10)
    buf.append_line('old1')
    buf.append_line('old2')
    buf.replace_all(['new1', 'new2', 'new3'])
    assert buf.snapshot() == ['new1', 'new2', 'new3']


def test_raw_buffer_replace_all_strips_trailing_cr():
    buf = RawBuffer(max_lines=10)
    buf.replace_all(['a\r', 'b\r'])
    assert buf.snapshot() == ['a', 'b']


def test_raw_buffer_replace_all_skips_none():
    buf = RawBuffer(max_lines=10)
    buf.replace_all(['a', None, 'b'])
    assert buf.snapshot() == ['a', 'b']


def test_raw_buffer_replace_all_truncates_to_max():
    # replace_all 后超过 max_lines 时，deque(maxlen=...) 自动丢最旧
    buf = RawBuffer(max_lines=3)
    buf.replace_all(['a', 'b', 'c', 'd', 'e'])  # 5 行入，上限 3
    assert buf.snapshot() == ['c', 'd', 'e']


def test_raw_buffer_partial_not_in_snapshot():
    buf = RawBuffer(max_lines=10)
    buf.append_chunk('partial_no_newline')
    # _partial 不算完整行，snapshot 不应包含
    assert buf.snapshot() == []
    assert len(buf) == 0
