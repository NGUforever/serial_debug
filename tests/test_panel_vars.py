"""core/panel_vars.py 测试：Motor/BMS/IMU 解析器 + DEFAULT_VARS。

验证从原 tkinter ui/monitor_window.py 抽出的解析逻辑：
  - DEFAULT_VARS 长度合理（motor 65 / imu 18）
  - _parse_tagged_line 对真实日志行解析正确
  - _parse_pipe_hash / _parse_int_tokens 边界
  - _section_label 命名规则
"""
from __future__ import annotations

import pytest

from serial_debug.core import panel_vars as pv


# ---- DEFAULT_VARS 长度 ----
def test_motor_default_vars_count():
    """motor DEFAULT_VARS = 9 (3wd1t_1) + 28 (3wd1t_2) + 10*2*9*1 (STD_DEBUG, 但 size 不一)"""
    # 实际：3wd1t_1=9 + 3wd1t_2=28 + L30~L39 每个有 MA/MB 2 块 × 9 组 × 多少字段
    # STD_DEBUG groups sizes [1,1,2,2,3,2,1,2,1] = 15 个字段 × 2 块 × 10 Lxx = 300
    # 总 = 9 + 28 + 300 = 337，但 std_groups 是 9 组每组 size 之和 = 15
    assert len(pv.MotorMonitorPanel.DEFAULT_VARS) == 337


def test_imu_default_vars_count():
    """imu DEFAULT_VARS = 3+4+3+3+1+3+1 = 18"""
    assert len(pv.ImuMonitorPanel.DEFAULT_VARS) == 18


def test_bms_default_vars_count():
    """bms DEFAULT_VARS = 15 (BMS) + 15 (BAT) = 30"""
    # Group 0 BMS=3, Group1=4, Group2=3, Group3=2, Group4=4, Group5=2 = 18 BMS
    # Group 0 BAT=2, Group1=1, Group2=3, Group3=6, Group4=2 = 14 BAT
    assert len(pv.BmsMonitorPanel.DEFAULT_VARS) == 32


def test_motor_var_has_required_keys():
    v = pv.MotorMonitorPanel.DEFAULT_VARS[0]
    for k in ('name', 'label', 'log_tag', 'block_idx',
              'group_idx', 'pos_in_group', 'color'):
        assert k in v, f'missing key {k}'


def test_imu_var_has_required_keys():
    v = pv.ImuMonitorPanel.DEFAULT_VARS[0]
    for k in ('name', 'label', 'log_tag', 'block_idx',
              'group_idx', 'pos_in_group', 'color'):
        assert k in v


def test_bms_var_has_required_keys():
    v = pv.BmsMonitorPanel.DEFAULT_VARS[0]
    for k in ('name', 'label', 'log_tag', 'group_idx',
              'pos_in_group', 'color'):
        assert k in v


# ---- Motor _parse_tagged_line ----
def test_motor_parse_3wd1t_1_line():
    line = '1783996615491 I model 3wd1t_1 0 0 0|0 0 0#0 0 0'
    blocks = pv.MotorMonitorPanel._parse_tagged_line(line, '3wd1t_1')
    assert blocks == [[[0, 0, 0], [0, 0, 0], [0, 0, 0]]]


def test_motor_parse_3wd1t_2_line():
    line = '1783995846145 I model 3wd1t_2 1 1 0 0|0 0 1000 0#0 0 0#0 0 0'
    blocks = pv.MotorMonitorPanel._parse_tagged_line(line, '3wd1t_2')
    assert blocks is not None
    # 按 | 与 # 分割：4 组
    assert len(blocks[0]) == 4


def test_motor_parse_std_debug_line():
    line = '1783995845972 I L30 STD_DEBUG:[6|0|0 0|...|64][6|0|0 0|...|65]'
    blocks = pv.MotorMonitorPanel._parse_tagged_line(line, 'L30_STD_DEBUG')
    assert blocks is not None
    assert len(blocks) == 2  # MA / MB 两个 block


def test_motor_parse_wrong_std_debug_tag_returns_none():
    """行中 Lxx 与 tag 不匹配应返回 None。"""
    line = '1783995845972 I L31 STD_DEBUG:[6|0|0]'
    blocks = pv.MotorMonitorPanel._parse_tagged_line(line, 'L30_STD_DEBUG')
    assert blocks is None


def test_motor_parse_unknown_tag_returns_none():
    blocks = pv.MotorMonitorPanel._parse_tagged_line('hello world', 'unknown_tag')
    assert blocks is None


def test_motor_parse_missing_marker_returns_none():
    """行中无 'model 3wd1t_1' marker 应返回 None。"""
    blocks = pv.MotorMonitorPanel._parse_tagged_line(
        '1783996615491 I model unknown 0 0 0', '3wd1t_1')
    assert blocks is None


# ---- Motor._parse_std_debug ----
def test_motor_parse_std_debug_flat_format():
    """14 平铺格式（无空格分值，全靠 |）也应能解析。"""
    rest = '[6|0|0 0|0 0|64]'  # 6 个值扁平化
    blocks = pv.MotorMonitorPanel._parse_std_debug(rest)
    assert blocks is not None
    assert len(blocks) == 1


def test_motor_parse_std_debug_no_brackets_returns_none():
    blocks = pv.MotorMonitorPanel._parse_std_debug('no brackets here')
    assert blocks is None


# ---- Motor._section_label ----
def test_motor_section_label_for_3wd1t_1():
    assert pv.MotorMonitorPanel._section_label('3wd1t_1', 0) == '3wd1t_1 数据'


def test_motor_section_label_for_std_debug_ma():
    label = pv.MotorMonitorPanel._section_label('L30_STD_DEBUG', 0)
    assert 'VCU1' in label and 'MA' in label


def test_motor_section_label_for_std_debug_mb():
    label = pv.MotorMonitorPanel._section_label('L35_STD_DEBUG', 1)
    assert 'VCU6' in label and 'MB' in label


def test_motor_section_label_unknown_tag_fallback():
    assert pv.MotorMonitorPanel._section_label('???', 0) == '??? 数据'


# ---- BMS _parse_tagged_line ----
def test_bms_parse_bms_line():
    line = '1783993778690 F BMS [3 4 4][2500 0 24888 2663][2864 0 444]'
    blocks = pv.BmsMonitorPanel._parse_tagged_line(line, 'BMS')
    assert blocks is not None
    assert len(blocks[0]) == 3  # 3 个方括号分组
    assert blocks[0][0] == [3, 4, 4]


def test_bms_parse_bat_line():
    line = '1783993778690 F BAT [24460 24750][2506][33 33 32]'
    blocks = pv.BmsMonitorPanel._parse_tagged_line(line, 'BAT')
    assert blocks is not None
    assert len(blocks[0]) == 3


def test_bms_parse_no_marker_returns_none():
    blocks = pv.BmsMonitorPanel._parse_tagged_line('1783993778690 F unknown [3]', 'BMS')
    assert blocks is None


def test_bms_parse_empty_brackets_skipped():
    """空方括号 [] 应被跳过。"""
    line = '1783993778690 F BMS [3 4 4][][1 2]'
    blocks = pv.BmsMonitorPanel._parse_tagged_line(line, 'BMS')
    assert blocks is not None
    assert len(blocks[0]) == 2  # 空的 [] 被丢


def test_bms_section_label():
    """BmsMonitorParser 没有 _section_label classmethod（Qt 路径不用）。
    验证 _SECTION_LABELS dict 含必要 key。"""
    assert pv.BmsMonitorPanel._SECTION_LABELS[('BMS', 0)] == 'BMS 数据'
    assert pv.BmsMonitorPanel._SECTION_LABELS[('BAT', 0)] == 'BAT 数据'


# ---- IMU _parse_tagged_line ----
def test_imu_parse_line():
    line = ('1784000342739 I Imu_data 0 2 3#9153 -1567 9282 0|-157 -27 978'
            '# 0 0 0|977|20 -55 -24|72')
    blocks = pv.ImuMonitorPanel._parse_tagged_line(line, 'Imu_data')
    assert blocks is not None
    # 7 组：3+4+3+3+1+3+1 = 18 个值
    total = sum(len(g) for g in blocks[0])
    assert total == 18


def test_imu_parse_missing_marker_returns_none():
    blocks = pv.ImuMonitorPanel._parse_tagged_line('hello world', 'Imu_data')
    assert blocks is None


def test_imu_section_label():
    """ImuMonitorParser 没有 _section_label classmethod（Qt 路径不用）。
    验证 _SECTION_LABELS dict 含必要 key。"""
    assert pv.ImuMonitorPanel._SECTION_LABELS[('Imu_data', 0)] == 'IMU 数据'


# ---- _parse_int_tokens ----
def test_parse_int_tokens_decimal():
    assert pv._parse_int_tokens(['1', '2', '3']) == [1, 2, 3]


def test_parse_int_tokens_hex_fallback():
    """十六进制 token 应能 fallback 解析。"""
    # 'ff' 不是十进制，应 fallback 到 hex 255
    assert pv._parse_int_tokens(['ff', '10']) == [255, 10]


def test_parse_int_tokens_skips_unparseable():
    """非十进制也非十六进制的 token 应被跳过（如带小数点 / 非法字符）。"""
    # '12.5' 不是十进制 int，也不是合法 hex；'xyz' 中 'x' 不是 hex digit
    assert pv._parse_int_tokens(['1', '12.5', '!!', '2']) == [1, 2]


def test_parse_int_tokens_empty():
    assert pv._parse_int_tokens([]) == []


# ---- _parse_pipe_hash ----
def test_parse_pipe_hash_basic():
    rest = '0 0 0|0 0 0#0 0 0'
    result = pv._parse_pipe_hash(rest)
    assert result == [[[0, 0, 0], [0, 0, 0], [0, 0, 0]]]


def test_parse_pipe_hash_empty_returns_none():
    assert pv._parse_pipe_hash('') is None


def test_parse_pipe_hash_strips_newline():
    """行尾换行应被截断。"""
    rest = '1 2 3\n4 5 6'
    result = pv._parse_pipe_hash(rest)
    # 只取第一行
    assert result == [[[1, 2, 3]]]


def test_parse_pipe_hash_drops_empty_groups():
    rest = '0 0||0 0'  # 中间空组
    result = pv._parse_pipe_hash(rest)
    assert result == [[[0, 0], [0, 0]]]
