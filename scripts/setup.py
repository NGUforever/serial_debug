"""串口调试助手 跨平台一键 build/install 入口。

用法：
    python scripts/setup.py build      # 构建 dist/ 二进制
    python scripts/setup.py install    # 安装到本机（含运行时依赖检测）
    python scripts/setup.py all        # build + install
    python scripts/setup.py uninstall  # 卸载
    python scripts/setup.py check      # 仅自检，不执行 build/install

跨平台：Linux / macOS / Windows。内部调对应 packaging/build_*.{sh,bat}
和 packaging/install_*.{sh,bat}，不重复实现平台特定逻辑。

依赖自动解决：执行前调 scripts/check_env.py 自检，自动修复
（pip 镜像源 fallback / sudo apt install 系统库 / VC++ Redist 下载），
修不了的问题给明确指令让用户跑。
"""
from __future__ import annotations

import os
import subprocess
import sys


_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS_DIR = os.path.join(_PROJECT_DIR, 'scripts')
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from check_env import check_all, fix_auto, check_runtime  # noqa: E402


def _platform_name() -> str:
    if sys.platform == 'linux':
        return 'linux'
    if sys.platform == 'darwin':
        return 'darwin'
    if sys.platform == 'win32':
        return 'win32'
    return sys.platform


def _runner(plat: str) -> list[str]:
    """平台对应的脚本执行器：linux/mac 用 bash，win 用 cmd。"""
    if plat == 'win32':
        return ['cmd', '/c']
    return ['bash']


def _build_script(plat: str) -> str:
    return {
        'linux': 'packaging/build_linux.sh',
        'darwin': 'packaging/build_mac.sh',
        'win32': 'packaging/build_win.bat',
    }[plat]


def _install_script(plat: str) -> str:
    return {
        'linux': 'packaging/install_linux.sh',
        'darwin': 'packaging/install_mac.sh',
        'win32': 'packaging/install_win.bat',
    }[plat]


def _run_script(script_rel: str, args: list[str] = None) -> int:
    """跑平台脚本，工作目录在仓库根。"""
    plat = _platform_name()
    cmd = _runner(plat) + [os.path.join(_PROJECT_DIR, script_rel)] + (args or [])
    print(f'==> 执行：{" ".join(cmd)}')
    try:
        return subprocess.check_call(cmd, cwd=_PROJECT_DIR)
    except subprocess.CalledProcessError as e:
        print(f'    脚本失败：returncode={e.returncode}')
        return e.returncode


def _self_check() -> bool:
    """build/install 前自检 + 自动修复。返回 True 表示可继续。"""
    print('==> 1. 依赖自检')
    problems = check_all(quiet=True)
    if not problems:
        print('    自检通过')
        return True
    print(f'    发现 {len(problems)} 个问题：')
    for p in problems:
        print(f'      {p}')
    # 自动修复
    print('\n==> 2. 自动修复')
    remaining = fix_auto(problems)
    if remaining:
        print(f'\n    剩余 {len(remaining)} 个问题需手动修复：')
        for p in remaining:
            print(f'  [{p.severity}] {p.name}')
            print(f'    执行：{p.fix_cmd}')
        if sys.stdin.isatty():
            ans = input('\n已手动修复，继续？[y/N]: ').strip().lower()
            if ans != 'y':
                return False
        else:
            print('    非交互环境，终止。请手动修复后重试。')
            return False
    return True


def _runtime_check() -> None:
    """install 后跑运行时检测，提示用户缺什么。"""
    print('\n==> 4. 运行时依赖自检')
    problems = check_runtime(quiet=True)
    if not problems:
        print('    运行时依赖齐全')
        return
    print(f'    发现 {len(problems)} 个运行时问题：')
    for p in problems:
        print(f'  [{p.severity}] {p.name}')
        print(f'    {p.detail}')
        if p.fix_cmd:
            print(f'    修复：{p.fix_cmd}')


def cmd_build() -> int:
    plat = _platform_name()
    print(f'==> 平台：{plat}')
    if not _self_check():
        return 1
    print('\n==> 3. 执行构建')
    return _run_script(_build_script(plat))


def cmd_install() -> int:
    plat = _platform_name()
    print(f'==> 平台：{plat}')
    # install 脚本内部已自带 libxcb/glibc/vcredist/Gatekeeper 检测+自动装，
    # setup.py 不重复跑 check_runtime + fix_auto（避免 apt 装两次）。
    print('==> 1. 执行安装（脚本内含运行时依赖自检+自动修复）')
    rc = _run_script(_install_script(plat))
    if rc != 0:
        return rc
    # install 完成后再跑一次运行时自检，提示剩余问题
    print('\n==> 2. 运行时依赖复查')
    problems = check_runtime(quiet=True)
    if not problems:
        print('    运行时依赖齐全')
        return 0
    print(f'    发现 {len(problems)} 个运行时问题：')
    for p in problems:
        print(f'  [{p.severity}] {p.name}')
        print(f'    {p.detail}')
        if p.fix_cmd:
            print(f'    修复：{p.fix_cmd}')
    return 0


def cmd_all() -> int:
    plat = _platform_name()
    print(f'==> 平台：{plat}')
    if not _self_check():
        return 1
    print('\n==> 3. 执行构建')
    rc = _run_script(_build_script(plat))
    if rc != 0:
        return rc
    print('\n==> 4. 执行安装')
    rc = _run_script(_install_script(plat))
    if rc != 0:
        return rc
    _runtime_check()
    return 0


def cmd_uninstall() -> int:
    plat = _platform_name()
    return _run_script(_install_script(plat), ['--uninstall'])


def cmd_check() -> int:
    problems = check_all(quiet=False)
    fatal = [p for p in problems if p.severity == 'fatal']
    if fatal:
        print(f'\n致命问题 {len(fatal)} 个')
        return 1
    print('\n自检通过')
    return 0


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help', 'help'):
        print(__doc__)
        sys.exit(0)
    cmd = sys.argv[1]
    if cmd == 'build':
        sys.exit(cmd_build())
    elif cmd == 'install':
        sys.exit(cmd_install())
    elif cmd == 'all':
        sys.exit(cmd_all())
    elif cmd == 'uninstall':
        sys.exit(cmd_uninstall())
    elif cmd == 'check':
        sys.exit(cmd_check())
    else:
        print(f'未知命令：{cmd}')
        print(__doc__)
        sys.exit(2)


if __name__ == '__main__':
    main()
