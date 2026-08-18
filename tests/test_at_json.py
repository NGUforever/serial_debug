"""resources/common_at.json 加载与字段校验测试。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def common_at_path() -> Path:
    here = Path(__file__).resolve().parent
    return here.parent / 'src' / 'serial_debug' / 'resources' / 'common_at.json'


def test_common_at_json_loads(common_at_path):
    assert common_at_path.is_file()
    with open(common_at_path, encoding='utf-8') as f:
        data = json.load(f)
    assert isinstance(data, list)
    assert len(data) > 0


def test_common_at_entry_schema(common_at_path):
    """每条命令必含 cmd/desc/seq/period_ms 四字段，类型符合契约。"""
    with open(common_at_path, encoding='utf-8') as f:
        data = json.load(f)
    for i, entry in enumerate(data):
        assert 'cmd' in entry, f'entry {i} missing cmd'
        assert 'desc' in entry, f'entry {i} missing desc'
        assert 'seq' in entry, f'entry {i} missing seq'
        assert 'period_ms' in entry, f'entry {i} missing period_ms'
        assert isinstance(entry['cmd'], str) and entry['cmd']
        assert isinstance(entry['desc'], str)
        assert isinstance(entry['seq'], int)
        assert isinstance(entry['period_ms'], int)
        # cmd 必须以 AT 开头
        assert entry['cmd'].startswith('AT'), f'entry {i} cmd not AT: {entry["cmd"]}'


def test_common_at_seq_values_are_consistent(common_at_path):
    """seq 字段语义：> 0 表示参与批量执行；所有 seq==0 项被批量执行跳过。
    这里只验证 seq 是非负整数。"""
    with open(common_at_path, encoding='utf-8') as f:
        data = json.load(f)
    for entry in data:
        assert entry['seq'] >= 0


def test_common_at_json_round_trips(common_at_path):
    """序列化往返不丢失字段。"""
    with open(common_at_path, encoding='utf-8') as f:
        data = json.load(f)
    s = json.dumps(data, ensure_ascii=False)
    back = json.loads(s)
    assert back == data
