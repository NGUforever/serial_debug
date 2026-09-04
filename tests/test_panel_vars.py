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


# ---- 纯文本兜底解析器（2026-09-04 新增）----
# 验证 _expand_header_names / parse_plain_text_header / iter_plain_text_vars
# 两类格式：无头纯数值 + #注释头（纯头行 / 内联头）

# ---- _expand_header_names ----
def test_expand_header_names_single_dim():
    """单维列名保持原样。"""
    names = ['timestamp', 'seq', 'val']
    out = pv._expand_header_names(names)
    assert out == ['timestamp', 'seq', 'val']


def test_expand_header_names_xyz_to_three():
    """.xyz 后缀展开为 3 列（.0/.1/.2）。"""
    out = pv._expand_header_names(['position.xyz'])
    assert out == ['position.xyz.0', 'position.xyz.1', 'position.xyz.2']


def test_expand_header_names_xyzw_to_four():
    """.xyzw 后缀展开为 4 列。"""
    out = pv._expand_header_names(['q.xyzw'])
    assert out == ['q.xyzw.0', 'q.xyzw.1', 'q.xyzw.2', 'q.xyzw.3']


def test_expand_header_names_mixed():
    """混合单维+多维列名。"""
    out = pv._expand_header_names(
        ['#timestamp', '#seq', 'position.xyz', 'orientation.xyzw'])
    assert out == ['timestamp', 'seq',
                   'position.xyz.0', 'position.xyz.1', 'position.xyz.2',
                   'orientation.xyzw.0', 'orientation.xyzw.1',
                   'orientation.xyzw.2', 'orientation.xyzw.3']


def test_expand_header_names_strips_hash_prefix():
    """列名前导 # 被剥离。"""
    out = pv._expand_header_names(['#currtime', '#time'])
    assert out == ['currtime', 'time']


def test_expand_header_names_empty_list():
    assert pv._expand_header_names([]) == []


def test_expand_header_names_skips_empty_tokens():
    """空 token（连续分隔符产生）被跳过。"""
    out = pv._expand_header_names(['', 'val', '  '])
    assert out == ['val']


def test_expand_header_names_xyzw_takes_precedence_over_xyz():
    """.xyzw 比 .xyz 长，优先匹配（不会被 .xyz 截断）。"""
    out = pv._expand_header_names(['q.xyzw'])
    assert len(out) == 4


# ---- parse_plain_text_header ----
def test_parse_header_plain_header_line():
    """纯 # 头行（无内联数据）→ (names, None)。"""
    line = '#timestamp #seq #position.xyz #orientation.xyzw\n'
    names, data = pv.parse_plain_text_header(line)
    assert data is None
    assert 'timestamp' in names
    assert 'seq' in names
    assert 'position.xyz' in names
    assert 'orientation.xyzw' in names


def test_parse_header_inline_header_data():
    """#header#data 内联格式 → (names, data_text)。"""
    line = ('#currtime,#time,p.xyz,q.xyzw,#mah_dist,#outlier#'
            '0 0.122568 -0.0140473 0.273287\n')
    names, data = pv.parse_plain_text_header(line)
    assert data is not None
    assert data.startswith('0 0.122568')
    assert 'currtime' in names
    assert 'time' in names
    assert 'p.xyz' in names
    assert 'q.xyzw' in names
    assert 'mah_dist' in names
    assert 'outlier' in names


def test_parse_header_non_comment_line():
    """非 # 行 → ([], None)。"""
    names, data = pv.parse_plain_text_header('1788418270935 0 27 0 83\n')
    assert names == []
    assert data is None


def test_parse_header_pure_comment_no_numeric_tail():
    """# 后全是文字（无数值）→ (names, None)。"""
    line = '# This is just a comment with no data\n'
    names, data = pv.parse_plain_text_header(line)
    assert data is None
    # 名字是 'This', 'is', 'just', ... （非标准但符合 split 逻辑）
    assert len(names) > 0


# ---- iter_plain_text_vars：无头纯数值（类型①）----
def test_iter_plain_text_no_header_uses_col_names():
    """无头纯数值行：首列时间戳跳过，其余列用 col2/col3... 命名。"""
    line = '1788418270935 0 27 0 83 800 -2147483648\n'
    ts = 1788418270.935
    out = list(pv.iter_plain_text_vars(line, ts))
    # 7 列，首列跳过 → 6 个变量
    names = [n for _, n, _ in out]
    assert names == ['col2', 'col3', 'col4', 'col5', 'col6', 'col7']
    vals = [v for _, _, v in out]
    assert vals == [0.0, 27.0, 0.0, 83.0, 800.0, -2147483648.0]
    assert all(t == ts for t, _, _ in out)


def test_iter_plain_text_no_header_single_data_col():
    """仅 2 列（时间戳 + 1 值）→ 1 个变量 col2。"""
    out = list(pv.iter_plain_text_vars('1234567890 42\n', 1234567.89))
    assert len(out) == 1
    assert out[0][1] == 'col2'
    assert out[0][2] == 42.0


def test_iter_plain_text_skips_non_numeric_cols():
    """非数值列被跳过（不 yield）。"""
    line = '1234567890 hello 42 world\n'
    out = list(pv.iter_plain_text_vars(line, 1234567.89))
    # 只有 '42' 可解析
    names = [n for _, n, _ in out]
    # col2=hello 跳过，col3=42 yield，col4=world 跳过
    assert names == ['col3']
    assert out[0][2] == 42.0


def test_iter_plain_text_too_few_cols():
    """仅 1 列（时间戳）→ 不 yield（需 ≥ 2 列）。"""
    out = list(pv.iter_plain_text_vars('1234567890\n', 1234567.89))
    assert out == []


# ---- iter_plain_text_vars：有头（类型②）----
def test_iter_plain_text_with_header_uses_header_names():
    """有头时：其余列用头名（多维展开）。"""
    header = ['timestamp', 'seq', 'position.xyz', 'orientation.xyzw']
    line = '1234567890 0 0.122568 -0.0140473 0.273287 0.527782\n'
    out = list(pv.iter_plain_text_vars(line, 1234567.89, header))
    # 首列 timestamp 跳过；seq=0, position.xyz.0/1/2, orientation.xyzw.0
    names = [n for _, n, _ in out]
    assert names == ['seq', 'position.xyz.0', 'position.xyz.1',
                     'position.xyz.2', 'orientation.xyzw.0']


def test_iter_plain_text_header_fewer_names_than_cols():
    """头名少于数据列时：多余列用 colN 兜底。"""
    header = ['timestamp', 'seq']
    line = '1234567890 0 0.1 0.2 0.3\n'
    out = list(pv.iter_plain_text_vars(line, 1234567.89, header))
    names = [n for _, n, _ in out]
    # seq=0, col3=0.1, col4=0.2, col5=0.3
    assert names == ['seq', 'col3', 'col4', 'col5']


def test_iter_plain_text_inline_header_data_format():
    """#header#data 内联格式：行首 # 取最后一个 # 后数据。"""
    header = ['currtime', 'time', 'p.xyz', 'q.xyzw', 'mah_dist', 'outlier']
    line = ('#currtime,#time,p.xyz,q.xyzw,#mah_dist,#outlier#'
            '0 0.122568 -0.0140473 0.273287 0.527782 -0.525768 -0.472039'
            ' 0.471374 0 0 0 0 0 0 1\n')
    out = list(pv.iter_plain_text_vars(line, 0.0, header))
    # 首列 currtime=0 跳过，其余 14 列 yield
    assert len(out) == 14
    names = [n for _, n, _ in out]
    # 第二列 time → 'time'
    assert names[0] == 'time'
    # p.xyz 展开为 3 列
    assert names[1:4] == ['p.xyz.0', 'p.xyz.1', 'p.xyz.2']
    # q.xyzw 展开为 4 列
    assert names[4:8] == ['q.xyzw.0', 'q.xyzw.1', 'q.xyzw.2', 'q.xyzw.3']
    # mah_dist, outlier
    assert names[8] == 'mah_dist'
    assert names[9] == 'outlier'
    # 多余列用 colN
    assert names[10:] == ['col12', 'col13', 'col14', 'col15']


def test_iter_plain_text_pure_header_line_no_yield():
    """纯 # 头行（最后一个 # 后无数值）→ 不 yield。"""
    line = '#timestamp #seq #position.xyz #orientation.xyzw\n'
    out = list(pv.iter_plain_text_vars(line, 1234567.89))
    assert out == []


def test_iter_plain_text_header_none_uses_col_names():
    """header_cols=None 时用 colN 兜底命名。"""
    line = '1234567890 1.5 2.5\n'
    out = list(pv.iter_plain_text_vars(line, 1234567.89, None))
    names = [n for _, n, _ in out]
    assert names == ['col2', 'col3']
