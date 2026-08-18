"""Motor / BMS / IMU 日志解析器与默认变量注册表。

从原 tkinter `ui/monitor_window.py` 抽出，供 Qt 监视窗口
(`ui/qt_monitor_window.py`) 复用。**不依赖任何 GUI 框架**——
本模块只有解析逻辑 + 类常量 + classmethod，可在无 tkinter / 无 PySide6 环境运行。
"""
from __future__ import annotations

import re

__all__ = [
    "MotorMonitorParser",
    "BmsMonitorParser",
    "ImuMonitorParser",
    # 向后兼容别名（原 tkinter Panel 类名）
    "MotorMonitorPanel",
    "BmsMonitorPanel",
    "ImuMonitorPanel",
    "_parse_int_tokens",
    "_parse_pipe_hash",
]


# ---- 共用色板 --------------------------------------------------------------
_PALETTE = [
    '#569cd6', '#ce9178', '#4ec9b0', '#dcdcaa',
    '#c586c0', '#f48771', '#6a9955', '#9cdcfe',
    '#d16969', '#b5cea8',
]


# ---- 通用 token / 行解析 --------------------------------------------------
def _parse_int_tokens(tokens: list[str]) -> list[int]:
    """将字符串 token 列表解析为整数列表。

    先按十进制，失败再按十六进制（C 代码 faultCode 用 %x 打印）。
    跳过无法解析的 token。
    """
    vals = []
    for t in tokens:
        try:
            vals.append(int(t))
        except ValueError:
            try:
                vals.append(int(t, 16))
            except ValueError:
                continue
    return vals


def _parse_pipe_hash(rest: str) -> list[list[list[int]]] | None:
    """解析 '0 0 0|0 0 0#0 0 0' → [[0,0,0],[0,0,0],[0,0,0]]（1 个 block）。

    按 | 和 # 分割为组，每组按空格分割为整数。空组被丢弃。
    """
    rest = rest.split('\n')[0].split('\r')[0].strip()
    parts = re.split(r'[|#]', rest)
    groups = []
    for p in parts:
        vals = _parse_int_tokens(p.split())
        if vals:
            groups.append(vals)
    return [groups] if groups else None


# ---- Motor ------------------------------------------------------------------
def _build_motor_default_vars() -> list[dict]:
    """构造电机监视默认变量注册表（3wd1t_1 9 个 + 3wd1t_2 28 个 +
    STD_DEBUG MA/MB 各 14 个 = 65 个）。

    变量名直接取自 C 代码字段名，STD_DEBUG 加 MA_/MB_ 前缀区分块。
    """
    out: list[dict] = []
    palette = _PALETTE
    idx = [0]

    def next_color() -> str:
        c = palette[idx[0] % len(palette)]
        idx[0] += 1
        return c

    def add(name, label, log_tag, block_idx, group_idx, pos_in_group,
            y_min=None, y_max=None):
        out.append({
            'name': name, 'label': label, 'log_tag': log_tag,
            'block_idx': block_idx, 'group_idx': group_idx,
            'pos_in_group': pos_in_group, 'color': next_color(),
            'y_min': y_min, 'y_max': y_max,
        })

    # ---- 3wd1t_1：3 组 × 3 = 9 个 ----
    g3wd1t_1 = [
        ['Vx', 'Vx_Fbk', 'Vx_Msf'],
        ['Wx', 'Wx_Fbk', 'Wx_Imu'],
        ['ImuPidOut', 'Vx_Input', 'Wx_Input'],
    ]
    for gi, group in enumerate(g3wd1t_1):
        for pi, name in enumerate(group):
            add(name, name, '3wd1t_1', 0, gi, pi)

    # ---- 3wd1t_2：8 组 = 28 个 ----
    g3wd1t_2 = [
        ['M1_En', 'M2_En', 'M3_En', 'T1_En'],
        ['M1_Cmd_Spd', 'M1_Fbk_Spd', 'M1_Cmd_Iq', 'M1_Fbk_Iq'],
        ['M2_Cmd_Spd', 'M2_Fbk_Spd', 'M2_Cmd_Iq', 'M2_Fbk_Iq'],
        ['M3_Cmd_Spd', 'M3_Fbk_Spd', 'M3_Cmd_Iq', 'M3_Fbk_Iq'],
        ['T1_Cmd_Angle', 'T1_Fbk_Angle', 'T1_Fbk_Spd', 'T1_Fbk_Iq'],
        ['M3_Temp', 'T1_Temp'],
        ['BackMotorAssitFlag', 'ParkingFlag', 'ModelStatus'],
        ['TCS_Iq_Limit_M1', 'TCS_Iq_Limit_M2', 'TCS_Iq_Limit_M3'],
    ]
    for gi, group in enumerate(g3wd1t_2):
        for pi, name in enumerate(group):
            add(name, name, '3wd1t_2', 0, gi, pi)

    # ---- STD_DEBUG：L30~L39 各 2 块（MA/MB）× 9 组 × 14 个/块 ----
    # 日志格式：L30 STD_DEBUG:[...][...]  → block 0=MA, block 1=MB
    # 变量名格式：L30_MA_state / L30_MB_temp 等
    std_groups = [
        ['state'],                       # group 0
        ['faultCode'],                   # group 1 (hex)
        ['tgt_ang', 'out_ang'],          # group 2
        ['tgt_spd', 'outavg_spd'],       # group 3
        ['iq_ref', 'iq_fbk', 'id_fbk'],  # group 4
        ['uq', 'ud'],                    # group 5
        ['torque'],                      # group 6
        ['in_pwr', 'out_pwr'],           # group 7
        ['temp'],                        # group 8
    ]
    for vcu_i in range(10):  # L30~L39 → VCU1~VCU10
        l_num = 30 + vcu_i
        l_tag = f'L{l_num}_STD_DEBUG'
        for block_idx, prefix in enumerate(['MA', 'MB']):
            for gi, group in enumerate(std_groups):
                for pi, name in enumerate(group):
                    full = f'L{l_num}_{prefix}_{name}'
                    label = f'VCU{vcu_i+1} {prefix} {name}'
                    add(full, label, l_tag, block_idx, gi, pi)

    return out


class MotorMonitorParser:
    """电机数据解析器（无 GUI 依赖）。

    日志格式（设备 C 代码 LOG_TS/VCU_MON_LOG 宏）：
      1783996615491 I model 3wd1t_1 0 0 0|0 0 0#0 0 0
      1783995846145 I model 3wd1t_2 1 1 0 0|0 0 1000 0#...#0 0 0
      1783995845972 I L30 STD_DEBUG:[6|0|0 0|...|64][6|0|0 0|...|65]
      1784012130441 I L31 STD_DEBUG:[0|0|...][5|0|...]

    解析：
      - 3wd1t_1 / 3wd1t_2：1 个 block，按 | 与 # 分组
      - Lxx STD_DEBUG：2 个 block（MA/MB），每个 [...] 内按 | 分组
        L30~L39 分别对应 VCU1~VCU10
    faultCode 用 %x 打印（hex），解析时按十进制失败再按 hex。
    """

    _BRACKET_RE = re.compile(r'\[([^\]]*)\]')
    # 匹配 L30~L39 STD_DEBUG 行，log_tag 格式为 L30_STD_DEBUG
    _STD_DEBUG_MARKER_RE = re.compile(r'\b(L\d+)\s+STD_DEBUG:')
    _TAG_MARKERS = {
        '3wd1t_1': ' model 3wd1t_1 ',
        '3wd1t_2': ' model 3wd1t_2 ',
    }
    # STD_DEBUG 的 section label 动态生成（_section_label 方法覆盖）
    _SECTION_LABELS = {
        ('3wd1t_1', 0): '3wd1t_1 数据',
        ('3wd1t_2', 0): '3wd1t_2 数据',
    }

    DEFAULT_VARS = _build_motor_default_vars()
    _REGISTRY = {v['name']: v for v in DEFAULT_VARS}

    # STD_DEBUG 每组的字段数，用于扁平→分组映射（兼容 9 组和 14 平铺格式）
    _STD_DEBUG_GROUP_SIZES = [1, 1, 2, 2, 3, 2, 1, 2, 1]

    @classmethod
    def _section_label(cls, tag: str, block_idx: int) -> str:
        """Lxx_STD_DEBUG tag 的分节标题：VCUx(Lxx) MA/MB。"""
        if tag.endswith('_STD_DEBUG'):
            l_num = tag.split('_')[0]  # 如 'L30'
            # L30→VCU1, L31→VCU2, ... L39→VCU10
            vcu_num = int(l_num[1:]) - 30 + 1
            block_name = 'MA' if block_idx == 0 else 'MB'
            return f'VCU{vcu_num}({l_num}) {block_name}'
        if (tag, block_idx) in cls._SECTION_LABELS:
            return cls._SECTION_LABELS[(tag, block_idx)]
        if block_idx == 0:
            return f'{tag} 数据'
        return f'{tag} 块{block_idx + 1}'

    @classmethod
    def _parse_tagged_line(cls, line: str, tag: str):
        if tag in ('3wd1t_1', '3wd1t_2'):
            marker = cls._TAG_MARKERS.get(tag)
            if not marker:
                return None
            idx = line.find(marker)
            if idx < 0:
                return None
            rest = line[idx + len(marker):]
            return _parse_pipe_hash(rest)
        # Lxx_STD_DEBUG tag：从行中匹配 Lxx STD_DEBUG: 提取数据
        # tag 格式如 'L30_STD_DEBUG'，需要从 tag 提取 Lxx 编号
        m = cls._STD_DEBUG_MARKER_RE.search(line)
        if not m:
            return None
        # 确认行中的 Lxx 编号与 tag 匹配
        l_num_in_line = m.group(1)  # 如 'L30'
        l_num_in_tag = tag.split('_')[0]  # 如 'L30'
        if l_num_in_line != l_num_in_tag:
            return None
        rest = line[m.end():]
        return cls._parse_std_debug(rest)

    @classmethod
    def _parse_std_debug(cls, rest: str):
        """解析 STD_DEBUG 数据块，兼容两种格式：
        - 9 组格式：[state|faultCode|tgt_ang out_ang|...]（| 分组，空格分值）
        - 14 平铺格式：[state|faultCode|tgt_ang|out_ang|...]（| 分隔每个字段）
        先扁平化再按 _STD_DEBUG_GROUP_SIZES 逻辑分组，两种格式结果一致。
        """
        blocks = []
        for m in cls._BRACKET_RE.finditer(rest):
            block_str = m.group(1)
            # 步骤 1：扁平化——所有 | 段的值拼成一个列表
            flat: list[int] = []
            for sp in block_str.split('|'):
                flat.extend(_parse_int_tokens(sp.split()))
            # 步骤 2：按逻辑分组重新切分
            groups: list[list[int]] = []
            offset = 0
            for size in cls._STD_DEBUG_GROUP_SIZES:
                groups.append(flat[offset:offset + size])
                offset += size
            if groups:
                blocks.append(groups)
        return blocks if blocks else None


# ---- BMS / BAT --------------------------------------------------------------
class BmsMonitorParser:
    """BMS / BAT 数据解析器（无 GUI 依赖）。

    日志行格式（来自设备 C 代码 LOG_TS 宏）：
      1783993778690 F BMS [3 4 4][2500 0 24888 2663][2864 0 444][1360 0][1000 932 1000 1000][4523 4720]
      1783993778690 F BAT [24460 24750][2506][33 33 32][4144 4136 4136 4137 4139 4149][5 2]

    1 个 block（block_idx=0），按方括号分组。
    """

    _BRACKET_RE = re.compile(r'\[([^\]]*)\]')
    _TAG_MARKERS = {
        'BMS': ' BMS ',
        'BAT': ' BAT ',
    }
    _SECTION_LABELS = {
        ('BMS', 0): 'BMS 数据',
        ('BAT', 0): 'BAT 数据',
    }

    DEFAULT_VARS = [
        # ---- BMS line ----
        # Group 0：整车状态、充电检测、充电开关
        {'name': 'state', 'label': '整车状态', 'log_tag': 'BMS',
         'group_idx': 0, 'pos_in_group': 0, 'color': '#569cd6'},
        {'name': 'charge_detect', 'label': '充电状态检测', 'log_tag': 'BMS',
         'group_idx': 0, 'pos_in_group': 1, 'color': '#ce9178'},
        {'name': 'charge_sw', 'label': '充电开关', 'log_tag': 'BMS',
         'group_idx': 0, 'pos_in_group': 2, 'color': '#4ec9b0'},
        # Group 1：设置充电电流/电压、母线电压/电流
        {'name': 'charge_i_set', 'label': '设置充电电流', 'log_tag': 'BMS',
         'group_idx': 1, 'pos_in_group': 0, 'color': '#dcdcaa'},
        {'name': 'charge_v_set', 'label': '设置充电电压', 'log_tag': 'BMS',
         'group_idx': 1, 'pos_in_group': 1, 'color': '#c586c0'},
        {'name': 'board_v', 'label': '母线电压', 'log_tag': 'BMS',
         'group_idx': 1, 'pos_in_group': 2, 'color': '#f48771'},
        {'name': 'board_i', 'label': '母线电流', 'log_tag': 'BMS',
         'group_idx': 1, 'pos_in_group': 3, 'color': '#6a9955'},
        # Group 2：STU电压、母线放电电流、充电温度
        {'name': 'dc_in_voltage', 'label': 'STU电压', 'log_tag': 'BMS',
         'group_idx': 2, 'pos_in_group': 0, 'color': '#9cdcfe'},
        {'name': 'discharge_i', 'label': '母线放电电流', 'log_tag': 'BMS',
         'group_idx': 2, 'pos_in_group': 1, 'color': '#d16969'},
        {'name': 'dc_in_temp', 'label': '充电温度', 'log_tag': 'BMS',
         'group_idx': 2, 'pos_in_group': 2, 'color': '#b5cea8'},
        # Group 3：电流环/电压环占空比
        {'name': 'LK_pwm_set', 'label': '电流环占空比', 'log_tag': 'BMS',
         'group_idx': 3, 'pos_in_group': 0, 'color': '#569cd6'},
        {'name': 'LKV_pwm_set', 'label': '电压环占空比', 'log_tag': 'BMS',
         'group_idx': 3, 'pos_in_group': 1, 'color': '#ce9178'},
        # Group 4：SoC / SoH
        {'name': 'batSOC', 'label': '显示SoC', 'log_tag': 'BMS',
         'group_idx': 4, 'pos_in_group': 0, 'color': '#4ec9b0',
         'y_min': 0, 'y_max': 100},
        {'name': 'realSOC', 'label': '实际SoC', 'log_tag': 'BMS',
         'group_idx': 4, 'pos_in_group': 1, 'color': '#dcdcaa',
         'y_min': 0, 'y_max': 100},
        {'name': 'batSOH', 'label': '显示健康度', 'log_tag': 'BMS',
         'group_idx': 4, 'pos_in_group': 2, 'color': '#c586c0',
         'y_min': 0, 'y_max': 100},
        {'name': 'userSOH', 'label': '真实健康度', 'log_tag': 'BMS',
         'group_idx': 4, 'pos_in_group': 3, 'color': '#f48771',
         'y_min': 0, 'y_max': 100},
        # Group 5：剩余容量
        {'name': 'bat_cap_cc', 'label': '剩余容量Ah', 'log_tag': 'BMS',
         'group_idx': 5, 'pos_in_group': 0, 'color': '#6a9955'},
        {'name': 'bat_cap_vol', 'label': '剩余容量V', 'log_tag': 'BMS',
         'group_idx': 5, 'pos_in_group': 1, 'color': '#9cdcfe'},
        # ---- BAT line ----
        # Group 0：包电压、电压
        {'name': 'Pack_Voltage', 'label': '包电压', 'log_tag': 'BAT',
         'group_idx': 0, 'pos_in_group': 0, 'color': '#d16969'},
        {'name': 'Stack_Voltage', 'label': '电压', 'log_tag': 'BAT',
         'group_idx': 0, 'pos_in_group': 1, 'color': '#b5cea8'},
        # Group 1：包电流
        {'name': 'Pack_Current', 'label': '包电流', 'log_tag': 'BAT',
         'group_idx': 1, 'pos_in_group': 0, 'color': '#569cd6'},
        # Group 2：电芯温度1/2、高温温度
        {'name': 'TS1_Temperature', 'label': '电芯温度1', 'log_tag': 'BAT',
         'group_idx': 2, 'pos_in_group': 0, 'color': '#ce9178'},
        {'name': 'TS3_Temperature', 'label': '电芯温度2', 'log_tag': 'BAT',
         'group_idx': 2, 'pos_in_group': 1, 'color': '#4ec9b0'},
        {'name': 'HDQ_Temperature', 'label': '高温温度', 'log_tag': 'BAT',
         'group_idx': 2, 'pos_in_group': 2, 'color': '#dcdcaa'},
        # Group 3：电芯电压1~6
        {'name': 'Cell_Voltage_0', 'label': '电芯电压1', 'log_tag': 'BAT',
         'group_idx': 3, 'pos_in_group': 0, 'color': '#c586c0'},
        {'name': 'Cell_Voltage_1', 'label': '电芯电压2', 'log_tag': 'BAT',
         'group_idx': 3, 'pos_in_group': 1, 'color': '#f48771'},
        {'name': 'Cell_Voltage_2', 'label': '电芯电压3', 'log_tag': 'BAT',
         'group_idx': 3, 'pos_in_group': 2, 'color': '#6a9955'},
        {'name': 'Cell_Voltage_3', 'label': '电芯电压4', 'log_tag': 'BAT',
         'group_idx': 3, 'pos_in_group': 3, 'color': '#9cdcfe'},
        {'name': 'Cell_Voltage_4', 'label': '电芯电压5', 'log_tag': 'BAT',
         'group_idx': 3, 'pos_in_group': 4, 'color': '#d16969'},
        {'name': 'Cell_Voltage_5', 'label': '电芯电压6', 'log_tag': 'BAT',
         'group_idx': 3, 'pos_in_group': 5, 'color': '#b5cea8'},
        # Group 4：电芯类型、并数
        {'name': 'cell_type', 'label': '电芯类型', 'log_tag': 'BAT',
         'group_idx': 4, 'pos_in_group': 0, 'color': '#569cd6'},
        {'name': 'cell_parallel', 'label': '并数', 'log_tag': 'BAT',
         'group_idx': 4, 'pos_in_group': 1, 'color': '#ce9178'},
    ]

    # 变量注册表：用户只填变量名，UI 不暴露 block/group/pos
    _REGISTRY = {v['name']: v for v in DEFAULT_VARS}

    @classmethod
    def _parse_tagged_line(cls, line: str, tag: str):
        """从 '... F BMS [...]...' 行中提取 tag 后的方括号分组。

        返回 [[group0, group1, ...]]（1 个 block，包含若干方括号分组）。
        没匹配返回 None。
        """
        marker = cls._TAG_MARKERS.get(tag) or f' {tag} '
        idx = line.find(marker)
        if idx < 0:
            return None
        rest = line[idx + len(marker):]
        groups = []
        for m in cls._BRACKET_RE.finditer(rest):
            parts = m.group(1).split()
            if not parts:
                continue
            try:
                groups.append([int(p) for p in parts])
            except ValueError:
                continue
        return [groups] if groups else None


# ---- IMU --------------------------------------------------------------------
def _build_imu_default_vars() -> list[dict]:
    """构造 IMU 监视默认变量注册表（18 个，1 个 block × 7 组）。

    日志格式（C 代码）：
      LOG_TS(AM_LOG_LEVEL_INFO, "Imu_data",
             "%x %d %x#%d %d %d %d|%d %d %d# %d %d %d|%d|%d %d %d|%d\\n",
             imu->errCode, imu->init.state, imu->init.caliFlag,
             imu_angle.pitch_angle, imu_angle.roll_angle,
             imu_angle.slope_angle, imu->pos.swPitchAcc,
             fAcc[X/Y/Z]*1000,
             fGyro[X/Y/Z]*1000,
             imu->conver.fAcc[IMU_ACCEL_Z]*1000,
             imu->init.gyroOffset[X/Y/Z],
             imu->recali_t.var_gyro_norm)

    实际日志：1784000342739 I Imu_data 0 2 3#9153 -1567 9282 0|-157 -27 978# 0 0 0|977|20 -55 -24|72
    分组（按 # 和 | 分割）：
      g0(3): errCode / state / caliFlag
      g1(4): pitch / roll / slope / swPitchAcc
      g2(3): fAcc X / Y / Z
      g3(3): fGyro X / Y / Z
      g4(1): conver.fAcc Z
      g5(3): gyroOffset X / Y / Z
      g6(1): var_gyro_norm
    """
    out: list[dict] = []
    palette = _PALETTE
    idx = [0]

    def next_color() -> str:
        c = palette[idx[0] % len(palette)]
        idx[0] += 1
        return c

    def add(name, label, group_idx, pos_in_group,
            y_min=None, y_max=None):
        out.append({
            'name': name, 'label': label, 'log_tag': 'Imu_data',
            'block_idx': 0, 'group_idx': group_idx,
            'pos_in_group': pos_in_group, 'color': next_color(),
            'y_min': y_min, 'y_max': y_max,
        })

    imu_groups = [
        # g0: errCode / state / caliFlag
        ['ErrCode', 'InitState', 'CaliFlag'],
        # g1: pitch / roll / slope / swPitchAcc
        ['PitchAngle', 'RollAngle', 'SlopeAngle', 'SwPitchAcc'],
        # g2: fAcc X / Y / Z
        ['AccX', 'AccY', 'AccZ'],
        # g3: fGyro X / Y / Z
        ['GyroX', 'GyroY', 'GyroZ'],
        # g4: conver.fAcc Z
        ['ConvAccZ'],
        # g5: gyroOffset X / Y / Z
        ['GyroOffsetX', 'GyroOffsetY', 'GyroOffsetZ'],
        # g6: var_gyro_norm
        ['VarGyroNorm'],
    ]
    for gi, group in enumerate(imu_groups):
        for pi, name in enumerate(group):
            add(name, name, gi, pi)
    return out


class ImuMonitorParser:
    """IMU 数据解析器（无 GUI 依赖）。

    日志行格式（设备 C 代码 LOG_TS 宏）：
      1784000342739 I Imu_data 0 2 3#9153 -1567 9282 0|-157 -27 978# 0 0 0|977|20 -55 -24|72

    解析：1 个 block（block_idx=0），按 # 与 | 分隔成 7 组（3+4+3+3+1+3+1 = 18 个值）。
    faultCode/errCode/caliFlag 字段在 C 代码里用 %x 打印（hex），解析器按十进制失败再按 hex。
    """

    _TAG_MARKERS = {
        'Imu_data': ' Imu_data ',
    }
    _SECTION_LABELS = {
        ('Imu_data', 0): 'IMU 数据',
    }

    DEFAULT_VARS = _build_imu_default_vars()
    _REGISTRY = {v['name']: v for v in DEFAULT_VARS}

    @classmethod
    def _parse_tagged_line(cls, line: str, tag: str):
        """按 # 与 | 分组，1 个 block。"""
        marker = cls._TAG_MARKERS.get(tag)
        if not marker or marker not in line:
            return None
        rest = line.split(marker, 1)[1] if marker in line else ''
        return _parse_pipe_hash(rest)


# ---- 向后兼容别名（原 tkinter Panel 类名） -------------------------------
# 让 qt_monitor_window.py 等老调用方不改代码即可 import：
#   from serial_debug.core.panel_vars import (
#       MotorMonitorPanel, BmsMonitorPanel, ImuMonitorPanel,
#   )
MotorMonitorPanel = MotorMonitorParser
BmsMonitorPanel = BmsMonitorParser
ImuMonitorPanel = ImuMonitorParser
