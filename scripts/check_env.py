"""跨平台依赖自检 + 自动修复。

被 scripts/setup.py 调用，也可单独跑：python scripts/check_env.py

检测项：
  - Python >= 3.9
  - pip 可用（缺失则尝试 ensurepip / get-pip.py）
  - Python tkinter（Linux 缺则 sudo apt install python3-tk）
  - Linux build 系统包：tk-dev binutils python3-dev
  - Linux 运行时系统库：libxcb-* / libGL / libEGL（PySide6 必需）
  - glibc >= 2.35（Ubuntu 22.04 LTS 是构建基线，更老系统可能跑不起来）
  - Mac Xcode CLT
  - Windows VC++ Redistributable 2015-2022 x64
  - Mac Gatekeeper quarantine（install 后检测 .app 是否被 quarantine）
  - PySide6 能否 import

返回 Problem 列表，每个 Problem 有：
  - name: 问题名
  - severity: 'fatal' / 'warning'
  - auto_fix: 是否可自动修复
  - fix_cmd: 修复命令（手动跑或自动跑）
  - detail: 详细说明
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import sysconfig
from dataclasses import dataclass, field
from typing import Optional


# PySide6 在 Linux 上必需的共享库（apt 包名）
# 来源：PySide6 文档 + Qt 官方 Linux 要求
_LINUX_QT_LIBS = [
    'libxcb-xinerama0',
    'libxcb-cursor0',
    'libxcb-icccm4',
    'libxcb-image0',
    'libxcb-keysyms1',
    'libxcb-randr0',
    'libxcb-render-util0',
    'libxcb-shape0',
    'libxcb-xkb1',
    'libxkbcommon-x11-0',
    'libgl1',
    'libegl1',
    'libfontconfig1',
    'libdbus-1-3',
]

# Linux build 系统包
_LINUX_BUILD_PKGS = ['tk-dev', 'binutils', 'python3-dev', 'python3-tk']

# glibc 最低版本（Ubuntu 22.04 LTS = 2.35）
_GLIBC_MIN = (2, 35)


@dataclass
class Problem:
    name: str
    severity: str = 'warning'   # 'fatal' / 'warning'
    auto_fix: bool = False
    fix_cmd: str = ''
    detail: str = ''
    platform_filter: str = ''  # 'linux' / 'darwin' / 'win32' / '' = all

    def __str__(self):
        return f'[{self.severity}] {self.name}: {self.detail}'


def _run(cmd: list[str], timeout: int = 10) -> tuple[int, str, str]:
    """跑命令返回 (returncode, stdout, stderr)。"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, check=False)
        return r.returncode, r.stdout, r.stderr
    except FileNotFoundError:
        return 127, '', f'{cmd[0]} not found'
    except Exception as e:
        return 1, '', str(e)


def _check_python() -> list[Problem]:
    """检测 Python 版本 >= 3.9。"""
    out = []
    if sys.version_info < (3, 9):
        out.append(Problem(
            name='python_version',
            severity='fatal',
            auto_fix=False,
            fix_cmd='请安装 Python 3.9+',
            detail=f'当前 {sys.version.split()[0]}，需要 >=3.9',
        ))
    return out


def _check_pip() -> list[Problem]:
    """检测 pip 可用。"""
    out = []
    rc, _, _ = _run([sys.executable, '-m', 'pip', '--version'])
    if rc != 0:
        # 尝试 ensurepip
        rc2, _, _ = _run([sys.executable, '-m', 'ensurepip', '--upgrade'])
        if rc2 == 0:
            return []
        out.append(Problem(
            name='pip_missing',
            severity='fatal',
            auto_fix=False,
            fix_cmd='curl https://bootstrap.pypa.io/get-pip.py | python3',
            detail='pip 不可用，ensurepip 也失败，需手动装 get-pip.py',
        ))
    return out


def _check_tkinter() -> list[Problem]:
    """检测 Python tkinter 模块（tkinter 主程序用）。"""
    out = []
    try:
        import tkinter  # noqa
    except ImportError:
        if sys.platform == 'linux':
            out.append(Problem(
                name='tkinter_missing',
                severity='fatal',
                auto_fix=True,
                fix_cmd='sudo apt install python3-tk',
                detail='tkinter 模块缺失，Linux 上需装 python3-tk',
                platform_filter='linux',
            ))
        else:
            out.append(Problem(
                name='tkinter_missing',
                severity='fatal',
                auto_fix=False,
                fix_cmd='重装 Python 时勾选 tcl/tk 选项',
                detail='tkinter 模块缺失',
            ))
    return out


def _linux_pkg_installed(pkg: str) -> bool:
    """检测 apt 包是否已装。"""
    rc, _, _ = _run(['dpkg', '-s', pkg])
    return rc == 0


def _linux_so_available(soname: str) -> bool:
    """检测 .so 是否在 ld 搜索路径里能找到。"""
    rc, out, _ = _run(['ldconfig', '-p'])
    if rc != 0:
        # 退化用 ldconfig -p | grep，找不到也认为有
        return True
    return soname in out


def _check_linux_build() -> list[Problem]:
    """Linux build 阶段必需的系统包。"""
    if sys.platform != 'linux':
        return []
    out = []
    missing = [p for p in _LINUX_BUILD_PKGS if not _linux_pkg_installed(p)]
    if missing:
        out.append(Problem(
            name='linux_build_pkgs',
            severity='warning',
            auto_fix=True,
            fix_cmd=f'sudo apt install {" ".join(missing)}',
            detail=f'缺构建系统包：{", ".join(missing)}（PyInstaller strip / tkinter 编译需要）',
            platform_filter='linux',
        ))
    return out


def _check_linux_runtime() -> list[Problem]:
    """Linux 运行时 PySide6 必需的共享库。"""
    if sys.platform != 'linux':
        return []
    out = []
    missing_pkgs = []
    for pkg in _LINUX_QT_LIBS:
        # apt 包名 → soname 推断（粗略）：libxcb-xinerama0 → libxcb-xinerama.so.0
        if not _linux_pkg_installed(pkg):
            missing_pkgs.append(pkg)
    if missing_pkgs:
        out.append(Problem(
            name='linux_qt_libs',
            severity='fatal',
            auto_fix=True,
            fix_cmd=f'sudo apt install {" ".join(missing_pkgs)}',
            detail=f'缺 PySide6/Qt 运行时库：{", ".join(missing_pkgs)}（不装会崩 '
                   f'"Could not load Qt platform plugin xcb"）',
            platform_filter='linux',
        ))
    return out


def _check_glibc() -> list[Problem]:
    """检测 glibc >= 2.35（Ubuntu 22.04 LTS 基线）。"""
    if sys.platform != 'linux':
        return []
    rc, out, _ = _run(['ldd', '--version'])
    if rc != 0:
        return [Problem(
            name='glibc_unknown',
            severity='warning',
            detail='无法检测 glibc 版本（ldd 不可用）',
        )]
    # 输出第一行是 "ldd (GNU libc) 2.35"，取版本号
    first = out.split('\n', 1)[0]
    try:
        ver_str = first.split()[-1]
        parts = tuple(int(x) for x in ver_str.split('.'))
        if parts < _GLIBC_MIN:
            return [Problem(
                name='glibc_too_old',
                severity='warning',
                auto_fix=False,
                fix_cmd='升级系统到 Ubuntu 22.04+ 或用 docker ubuntu:22.04 构建',
                detail=f'glibc {ver_str} < {_GLIBC_MIN[0]}.{_GLIBC_MIN[1]}，'
                       f'二进制可能跑不起来',
            )]
    except Exception:
        return [Problem(
            name='glibc_parse_fail',
            severity='warning',
            detail=f'glibc 版本解析失败：{first}',
        )]
    return []


def _check_mac_xcode_clt() -> list[Problem]:
    """Mac 检测 Xcode Command Line Tools。"""
    if sys.platform != 'darwin':
        return []
    out = []
    rc, _, _ = _run(['xcode-select', '-p'])
    if rc != 0:
        out.append(Problem(
            name='xcode_clt_missing',
            severity='warning',
            auto_fix=False,
            fix_cmd='xcode-select --install',
            detail='Xcode CLT 缺失，PyInstaller 可能拿不到 .dylib 系统库',
            platform_filter='darwin',
        ))
    return out


def _check_vc_redist() -> list[Problem]:
    """Windows 检测 VC++ Redistributable 2015-2022 x64（PySide6 必需）。"""
    if sys.platform != 'win32':
        return []
    out = []
    # 查注册表：HKLM\SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x64
    # Installed=1 表示已装
    rc, out_txt, _ = _run([
        'reg', 'query',
        r'HKLM\SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x64',
        '/v', 'Installed', '/reg:64',
    ])
    if rc != 0 or '0x1' not in out_txt:
        out.append(Problem(
            name='vc_redist_missing',
            severity='fatal',
            auto_fix=True,
            fix_cmd='下载 https://aka.ms/vs/17/release/vc_redist.x64.exe 静默安装 '
                    '（/install /quiet /norestart）',
            detail='VC++ Redistributable 2015-2022 x64 缺失，PySide6 启动会崩 '
                   '缺 VCRuntime140.dll',
            platform_filter='win32',
        ))
    return out


def _check_pyside6() -> list[Problem]:
    """检测 PySide6 能否 import（验证 pip 装包成功）。"""
    out = []
    try:
        import PySide6  # noqa
        import pyqtgraph  # noqa
    except ImportError:
        out.append(Problem(
            name='pyside6_missing',
            severity='fatal',
            auto_fix=True,
            fix_cmd=f'{sys.executable} scripts/pip_install_with_fallback.py '
                    'PySide6 pyqtgraph numpy PyOpenGL PyOpenGL-accelerate',
            detail='PySide6/pyqtgraph 未装，监视窗口无法启动',
        ))
    return out


def check_all(quiet: bool = False) -> list[Problem]:
    """跑 build 前自检：Python/pip/tkinter/系统构建包/glibc/Xcode CLT/VC++ Redist。

    不检 PySide6 import 和 linux_qt_libs —— 这俩由 build 脚本
    （pip install）和 install 脚本（apt install libxcb-*）负责，
    在 check_env 阶段触发会跟 build 脚本重复装包浪费时间。
    """
    problems: list[Problem] = []
    problems += _check_python()
    problems += _check_pip()
    problems += _check_tkinter()
    if sys.platform == 'linux':
        problems += _check_linux_build()
        problems += _check_glibc()
    elif sys.platform == 'darwin':
        problems += _check_mac_xcode_clt()
    elif sys.platform == 'win32':
        problems += _check_vc_redist()
    if not quiet and problems:
        print(f'==> 自检发现 {len(problems)} 个问题：')
        for p in problems:
            print(f'  {p}')
    return problems


def fix_auto(problems: list[Problem]) -> list[Problem]:
    """对 auto_fix=True 的问题自动执行 fix_cmd。返回剩余需手动处理的问题。

    用 shlex.split 处理带引号的复杂命令。若当前是 root（uid=0），
    自动去掉 fix_cmd 的 sudo 前缀（容器内常见）。pip install 类命令
    给 30 分钟超时（PySide6 + numpy 等大包下载慢）。
    """
    remaining = []
    is_root = hasattr(os, 'geteuid') and os.geteuid() == 0
    for p in problems:
        if not p.auto_fix:
            remaining.append(p)
            continue
        print(f'==> 自动修复：{p.name}')
        # root 用户去掉 sudo 前缀（容器内 sudo 可能不存在）
        cmd_str = p.fix_cmd
        if is_root and cmd_str.startswith('sudo '):
            cmd_str = cmd_str[5:]
        print(f'    执行：{cmd_str}')
        try:
            args = shlex.split(cmd_str)
        except ValueError as e:
            print(f'    命令解析失败：{e}')
            p.auto_fix = False
            remaining.append(p)
            continue
        # pip install 类命令给长超时；apt install 给中等超时
        timeout = 1800 if 'pip' in cmd_str or 'apt' in cmd_str else 600
        try:
            r = subprocess.run(args, timeout=timeout, check=False)
            if r.returncode == 0:
                print(f'    成功')
            else:
                print(f'    失败（returncode={r.returncode}）')
                p.auto_fix = False
                remaining.append(p)
        except subprocess.TimeoutExpired:
            print(f'    超时（{timeout}s）')
            p.auto_fix = False
            remaining.append(p)
        except FileNotFoundError as e:
            print(f'    命令不存在：{e}')
            p.auto_fix = False
            remaining.append(p)
        except Exception as e:
            print(f'    异常：{e}')
            p.auto_fix = False
            remaining.append(p)
    return remaining


def check_runtime(quiet: bool = False) -> list[Problem]:
    """install 后跑：检测终端用户运行时缺什么（libxcb/glibc/vcredist/quarantine）。

    与 check_all 的区别：check_runtime 不检 Python/pip/PySide6 import
    （PyInstaller 二进制不需要 Python 环境），只检系统库和权限。
    """
    problems: list[Problem] = []
    if sys.platform == 'linux':
        problems += _check_linux_runtime()
        problems += _check_glibc()
    elif sys.platform == 'darwin':
        # Gatekeeper quarantine 检测
        app = '/Applications/serial_debug.app'
        if os.path.exists(app):
            rc, out, _ = _run(['xattr', app])
            if 'com.apple.quarantine' in out:
                problems.append(Problem(
                    name='gatekeeper_quarantine',
                    severity='warning',
                    auto_fix=True,
                    fix_cmd=f'xattr -dr com.apple.quarantine "{app}"',
                    detail='.app 被 quarantine 标记，首次打开会弹"无法打开"',
                ))
    elif sys.platform == 'win32':
        problems += _check_vc_redist()
    if not quiet and problems:
        print(f'==> 运行时自检发现 {len(problems)} 个问题：')
        for p in problems:
            print(f'  {p}')
    return problems


def main():
    problems = check_all(quiet=False)
    fatal = [p for p in problems if p.severity == 'fatal']
    if fatal:
        print(f'\n致命问题 {len(fatal)} 个，需修复后重试')
        sys.exit(1)
    print('\n自检通过')
    sys.exit(0)


if __name__ == '__main__':
    main()
