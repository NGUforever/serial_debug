"""串口调试助手 跨平台一键 build/install 入口。

用法：
    python scripts/setup.py build      # 构建 dist/ 二进制（PyInstaller）
    python scripts/setup.py install    # 安装二进制到本机（含运行时依赖检测）
    python scripts/setup.py all        # build + install
    python scripts/setup.py uninstall  # 卸载
    python scripts/setup.py check      # 仅自检，不执行 build/install
    python scripts/setup.py install_source  # 源码模式安装（不走 PyInstaller）

跨平台：Linux / macOS / Windows。内部调对应 packaging/build_*.{sh,bat}
和 packaging/install_*.{sh,bat}，不重复实现平台特定逻辑。

依赖自动解决：执行前调 scripts/check_env.py 自检，自动修复
（pip 镜像源 fallback / sudo apt install 系统库 / VC++ Redist 下载），
修不了的问题给明确指令让用户跑。

源码模式（install_source）适合公司安全机制拦截 exe 的环境：
建 venv + 装依赖 + 创建桌面快捷方式，不走 PyInstaller，不下载 exe。
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


# ---- 源码模式安装（不走 PyInstaller，适合 exe 被拦的环境）------------
def _venv_bin(plat: str, venv_dir: str, name: str) -> str:
    """venv 内的可执行文件路径：Windows 在 Scripts/，Linux/Mac 在 bin/。"""
    sub = 'Scripts' if plat == 'win32' else 'bin'
    ext = '.exe' if plat == 'win32' else ''
    return os.path.join(venv_dir, sub, name + ext)


def _create_run_script(plat: str) -> str:
    """在仓库根创建启动脚本 run.bat / run.sh，返回脚本路径。

    直接调 venv 内的 python + PYTHONPATH=src，不需要 activate venv，
    也不需要 pip install -e .。这样 venv 是自包含的，复制仓库到
    别的机器只要 venv 还能用就能跑。
    """
    if plat == 'win32':
        path = os.path.join(_PROJECT_DIR, 'run.bat')
        content = (
            '@echo off\r\n'
            'cd /d %~dp0\r\n'
            'set PYTHONPATH=src\r\n'
            '.venv\\Scripts\\python.exe -m serial_debug %*\r\n'
        )
    else:
        path = os.path.join(_PROJECT_DIR, 'run.sh')
        content = (
            '#!/bin/bash\n'
            'cd "$(dirname "$0")"\n'
            'export PYTHONPATH=src\n'
            '.venv/bin/python -m serial_debug "$@"\n'
        )
    with open(path, 'w', newline='') as f:
        f.write(content)
    if plat != 'win32':
        import stat
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _create_desktop_shortcut(plat: str, run_script: str) -> None:
    """创建桌面快捷方式，指向 run.bat / run.sh。"""
    desktop = os.path.expanduser('~/Desktop')
    assets = os.path.join(_PROJECT_DIR, 'src', 'serial_debug', 'resources', 'assets')
    if plat == 'win32':
        # Windows：PowerShell 创建 .lnk
        icon = os.path.join(assets, 'icon_debug.ico')
        lnk = os.path.join(desktop, '串口调试助手.lnk')
        ps = (
            "$ws = New-Object -ComObject WScript.Shell; "
            f"$lnk = $ws.CreateShortcut('{lnk}'); "
            f"$lnk.TargetPath = '{run_script}'; "
            f"$lnk.WorkingDirectory = '{_PROJECT_DIR}'; "
            f"$lnk.IconLocation = '{icon},0'; "
            "$lnk.Save()"
        )
        subprocess.run(['powershell', '-NoProfile', '-Command', ps], check=False)
    elif plat == 'linux':
        # Linux：写 .desktop 文件
        icon = os.path.join(assets, 'icon_debug.svg')
        desktop_dir = os.path.expanduser('~/.local/share/applications')
        os.makedirs(desktop_dir, exist_ok=True)
        desk_file = os.path.join(desktop_dir, 'serial_debug_src.desktop')
        content = (
            '[Desktop Entry]\n'
            'Type=Application\n'
            'Version=1.0\n'
            'Name=串口调试助手（源码）\n'
            f'Exec=bash "{run_script}"\n'
            f'Path={_PROJECT_DIR}\n'
            f'Icon={icon}\n'
            'Terminal=false\n'
            'Categories=Development;Utility;\n'
            'StartupNotify=false\n'
            'StartupWMClass=serial_debug\n'
        )
        with open(desk_file, 'w') as f:
            f.write(content)
        import stat
        os.chmod(desk_file, os.stat(desk_file).st_mode | stat.S_IEXEC)
    elif plat == 'darwin':
        # Mac：放到 /Applications 不现实，提示用户手动拖
        print(f'    Mac 用户请手动创建快捷方式，启动脚本：{run_script}')


def cmd_install_source() -> int:
    """源码模式安装：建 venv + 装依赖 + 创建桌面快捷方式。

    不走 PyInstaller，避免 exe 被公司安全机制拦截。直接用
    venv 内的 python 跑 -m serial_debug，桌面快捷方式指向
    run.bat / run.sh。

    适合场景：
    - 公司安全机制拦截 PyInstaller 打的 exe
    - 不想下载 vc_redist.x64.exe 静默安装
    - 想用最新源码，不重新打包
    """
    plat = _platform_name()
    print(f'==> 平台：{plat}')

    # 1. 自检（Python 版本 + pip + Git）
    if not _self_check():
        return 1

    # 2. 建 venv
    venv_dir = os.path.join(_PROJECT_DIR, '.venv')
    if os.path.exists(venv_dir) and os.path.exists(_venv_bin(plat, venv_dir, 'python')):
        print('\n==> 2. venv 已存在，复用')
    else:
        print('\n==> 2. 创建 venv（隔离依赖，不污染系统 Python）')
        try:
            subprocess.check_call([sys.executable, '-m', 'venv', venv_dir])
        except subprocess.CalledProcessError as e:
            print(f'    venv 创建失败：{e}')
            return 1

    # 3. 装依赖（走 pip_install_with_fallback，多镜像源 + PEP 668 兼容）
    print('\n==> 3. 安装 Python 依赖（pip 镜像源 fallback）')
    venv_python = _venv_bin(plat, venv_dir, 'python')
    pip_script = os.path.join(_SCRIPTS_DIR, 'pip_install_with_fallback.py')
    requirements = os.path.join(_PROJECT_DIR, 'requirements.txt')
    try:
        subprocess.check_call(
            [venv_python, pip_script, '-r', requirements])
    except subprocess.CalledProcessError as e:
        print(f'    依赖安装失败：{e}')
        print('    请手动执行：')
        print(f'    {venv_python} -m pip install -r requirements.txt')
        return 1

    # 4. 创建启动脚本
    print('\n==> 4. 创建启动脚本')
    run_script = _create_run_script(plat)
    print(f'    启动脚本：{run_script}')

    # 5. 创建桌面快捷方式
    print('\n==> 5. 创建桌面快捷方式')
    _create_desktop_shortcut(plat, run_script)

    # 6. Windows/Mac 还需要 VC++ Redist（PySide6 运行时依赖）
    if plat == 'win32':
        print('\n==> 6. VC++ Redistributable 检测')
        print('    PySide6 运行时需要 VC++ Redistributable 2015-2022 x64')
        print('    如已装会自动跳过，缺则首次启动 GUI 会崩 "缺 VCRuntime140.dll"')
        print('    下载：https://aka.ms/vs/17/release/vc_redist.x64.exe')

    print('\n==> 源码模式安装完成！')
    print('\n使用方式：')
    if plat == 'win32':
        print('  1. 双击桌面快捷方式「串口调试助手」')
        print(f'  2. 双击 {run_script}')
    else:
        print(f'  1. 应用菜单搜「串口调试助手（源码）」')
        print(f'  2. 命令行：bash {run_script}')
    return 0


def cmd_uninstall_source() -> int:
    """卸载源码模式：删 venv + 删桌面快捷方式 + 删 run 脚本。"""
    plat = _platform_name()
    print(f'==> 平台：{plat}')

    # 1. 删 venv
    venv_dir = os.path.join(_PROJECT_DIR, '.venv')
    if os.path.exists(venv_dir):
        print(f'==> 删除 venv: {venv_dir}')
        import shutil
        shutil.rmtree(venv_dir, ignore_errors=True)

    # 2. 删 run 脚本
    run_name = 'run.bat' if plat == 'win32' else 'run.sh'
    run_path = os.path.join(_PROJECT_DIR, run_name)
    if os.path.exists(run_path):
        print(f'==> 删除启动脚本: {run_path}')
        os.remove(run_path)

    # 3. 删桌面快捷方式
    if plat == 'win32':
        lnk = os.path.expanduser('~/Desktop/串口调试助手.lnk')
        if os.path.exists(lnk):
            os.remove(lnk)
    elif plat == 'linux':
        desk = os.path.expanduser('~/.local/share/applications/serial_debug_src.desktop')
        if os.path.exists(desk):
            os.remove(desk)

    print('==> 卸载完成')
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
    elif cmd == 'install_source':
        sys.exit(cmd_install_source())
    elif cmd == 'uninstall_source':
        sys.exit(cmd_uninstall_source())
    else:
        print(f'未知命令：{cmd}')
        print(__doc__)
        sys.exit(2)


if __name__ == '__main__':
    main()
