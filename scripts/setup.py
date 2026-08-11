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
import shutil
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


def _has_uv() -> bool:
    """检测 uv 是否在 PATH（uv 比 venv + pip 快 10-100 倍）。"""
    return shutil.which('uv') is not None


def _create_venv(plat: str, venv_dir: str) -> str:
    """建 venv，优先用 uv，没装 fallback 到 python -m venv。

    返回 venv 内 python 的路径。
    """
    if _has_uv():
        print('    使用 uv 创建 venv（比 python -m venv 快 10-100 倍）')
        subprocess.check_call(['uv', 'venv', venv_dir])
    else:
        print('    uv 未安装，使用 python -m venv（可 pip install uv 加速）')
        subprocess.check_call([sys.executable, '-m', 'venv', venv_dir])
    return _venv_bin(plat, venv_dir, 'python')


def _install_deps(venv_python: str, requirements: str) -> int:
    """装依赖，优先用 uv pip（快得多），fallback 到 pip_install_with_fallback。"""
    if _has_uv():
        # uv pip install 直接用 venv 内 python，不需要 activate
        # --python 指定 venv 内 python，-i 指定镜像源
        print('    使用 uv pip install')
        # 用 uv 跑 pip_install_with_fallback 的镜像源 fallback 逻辑
        # 实际上 uv 自带重试，但没多镜像源。手动写一遍 fallback。
        mirrors = [
            None,  # 官方 PyPI
            'https://pypi.tuna.tsinghua.edu.cn/simple',
            'https://mirrors.aliyun.com/pypi/simple',
            'https://pypi.doubanio.com/simple',
        ]
        for idx, mirror in enumerate(mirrors):
            args = ['uv', 'pip', 'install', '--python', venv_python,
                    '-r', requirements]
            if mirror:
                args += ['--index-url', mirror]
            label = mirror or '官方 PyPI'
            print(f'==> uv pip install [{idx + 1}/{len(mirrors)}] {label}')
            try:
                subprocess.check_call(args)
                print(f'    成功（{label}）')
                return 0
            except subprocess.CalledProcessError as e:
                print(f'    失败（returncode={e.returncode}），换源重试')
        print('    全部镜像源失败')
        return 1
    else:
        # fallback 走 pip_install_with_fallback.py（多镜像源 + PEP 668）
        pip_script = os.path.join(_SCRIPTS_DIR, 'pip_install_with_fallback.py')
        return subprocess.check_call(
            [venv_python, pip_script, '-r', requirements])


def _create_run_script(plat: str) -> tuple:
    """在仓库根创建启动脚本 run.bat / run.sh + run_log.bat / run_log.sh。

    直接调 venv 内的 python + PYTHONPATH=src，不需要 activate venv，
    也不需要 pip install -e .。这样 venv 是自包含的，复制仓库到
    别的机器只要 venv 还能用就能跑。

    返回 (run_script, run_log_script) 两个路径。
    """
    if plat == 'win32':
        run_path = os.path.join(_PROJECT_DIR, 'run.bat')
        run_content = (
            '@echo off\r\n'
            'cd /d %~dp0\r\n'
            'set PYTHONPATH=src\r\n'
            '.venv\\Scripts\\python.exe -m serial_debug %*\r\n'
        )
        run_log_path = os.path.join(_PROJECT_DIR, 'run_log.bat')
        run_log_content = (
            '@echo off\r\n'
            'cd /d %~dp0\r\n'
            'set PYTHONPATH=src\r\n'
            '.venv\\Scripts\\python.exe -m serial_debug --view %*\r\n'
        )
        paths = [(run_path, run_content), (run_log_path, run_log_content)]
    else:
        run_path = os.path.join(_PROJECT_DIR, 'run.sh')
        run_content = (
            '#!/bin/bash\n'
            'cd "$(dirname "$0")"\n'
            'export PYTHONPATH=src\n'
            '.venv/bin/python -m serial_debug "$@"\n'
        )
        run_log_path = os.path.join(_PROJECT_DIR, 'run_log.sh')
        run_log_content = (
            '#!/bin/bash\n'
            'cd "$(dirname "$0")"\n'
            'export PYTHONPATH=src\n'
            '.venv/bin/python -m serial_debug --view "$@"\n'
        )
        paths = [(run_path, run_content), (run_log_path, run_log_content)]
    import stat
    for path, content in paths:
        with open(path, 'w', newline='') as f:
            f.write(content)
        if plat != 'win32':
            os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return run_path, run_log_path


def _create_desktop_shortcut(plat: str, run_script: str, run_log_script: str) -> None:
    """创建桌面快捷方式 + 日志查看器入口。

    主程序快捷方式指向 run.bat / run.sh。
    日志查看器另起 .desktop / .lnk，含 MimeType 注册，让文件管理器
    右键 .log/.txt 文件能选「日志查看器」打开。
    """
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
        # 日志查看器快捷方式
        log_icon = os.path.join(assets, 'icon_view.ico')
        log_lnk = os.path.join(desktop, '日志查看器.lnk')
        ps_log = (
            "$ws = New-Object -ComObject WScript.Shell; "
            f"$lnk = $ws.CreateShortcut('{log_lnk}'); "
            f"$lnk.TargetPath = '{run_log_script}'; "
            f"$lnk.WorkingDirectory = '{_PROJECT_DIR}'; "
            f"$lnk.IconLocation = '{log_icon},0'; "
            "$lnk.Save()"
        )
        subprocess.run(['powershell', '-NoProfile', '-Command', ps_log], check=False)
    elif plat == 'linux':
        # Linux：写 .desktop 文件
        icon = os.path.join(assets, 'icon_debug.svg')
        log_icon = os.path.join(assets, 'icon_view.svg')
        desktop_dir = os.path.expanduser('~/.local/share/applications')
        os.makedirs(desktop_dir, exist_ok=True)
        # 主程序
        desk_file = os.path.join(desktop_dir, 'serial_debug_src.desktop')
        content = (
            '[Desktop Entry]\n'
            'Type=Application\n'
            'Version=1.0\n'
            'Name=串口调试助手\n'
            f'Exec=bash "{run_script}"\n'
            f'Path={_PROJECT_DIR}\n'
            f'Icon={icon}\n'
            'Terminal=false\n'
            'Categories=Development;Utility;\n'
            'Keywords=serial;uart;AT;debug;ch343;\n'
            'StartupNotify=false\n'
            'StartupWMClass=serial_debug\n'
        )
        with open(desk_file, 'w') as f:
            f.write(content)
        # 日志查看器（含 MimeType，让文件管理器右键能用）
        log_desk_file = os.path.join(desktop_dir, 'serial_debug_log_src.desktop')
        log_content = (
            '[Desktop Entry]\n'
            'Type=Application\n'
            'Version=1.0\n'
            'Name=日志查看器\n'
            'GenericName=Log File Viewer\n'
            'Comment=日志文件查看工具：多文件 Tab + 筛选面板 + 全局搜索\n'
            f'Exec=bash "{run_log_script}" %F\n'
            f'Path={_PROJECT_DIR}\n'
            f'Icon={log_icon}\n'
            'Terminal=false\n'
            'Categories=Utility;TextTools;\n'
            'Keywords=log;viewer;text;grep;filter;\n'
            'StartupNotify=false\n'
            'StartupWMClass=serial_debug_log\n'
            'MimeType=text/plain;text/x-log;text/x-readme;application/octet-stream;text/*;\n'
        )
        with open(log_desk_file, 'w') as f:
            f.write(log_content)
        import stat
        os.chmod(desk_file, os.stat(desk_file).st_mode | stat.S_IEXEC)
        os.chmod(log_desk_file, os.stat(log_desk_file).st_mode | stat.S_IEXEC)
        # 刷新 mime-database（MimeType 改动后某些文件管理器需要）
        subprocess.run(['update-desktop-database', desktop_dir],
                       check=False, capture_output=True)
        subprocess.run(['update-mime-database',
                        os.path.expanduser('~/.local/share/mime')],
                       check=False, capture_output=True)
    elif plat == 'darwin':
        print(f'    Mac 用户请手动创建快捷方式，启动脚本：{run_script} / {run_log_script}')


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

    # 2. 建 venv（优先 uv，fallback 到 python -m venv）
    venv_dir = os.path.join(_PROJECT_DIR, '.venv')
    if os.path.exists(venv_dir) and os.path.exists(_venv_bin(plat, venv_dir, 'python')):
        print('\n==> 2. venv 已存在，复用')
    else:
        print('\n==> 2. 创建 venv（隔离依赖，不污染系统 Python）')
        try:
            _create_venv(plat, venv_dir)
        except subprocess.CalledProcessError as e:
            print(f'    venv 创建失败：{e}')
            return 1

    # 3. 装依赖（优先 uv pip，fallback 到 pip_install_with_fallback）
    print('\n==> 3. 安装 Python 依赖（pip 镜像源 fallback）')
    venv_python = _venv_bin(plat, venv_dir, 'python')
    requirements = os.path.join(_PROJECT_DIR, 'requirements.txt')
    try:
        _install_deps(venv_python, requirements)
    except subprocess.CalledProcessError as e:
        print(f'    依赖安装失败：{e}')
        print('    请手动执行：')
        print(f'    uv pip install --python {venv_python} -r requirements.txt')
        return 1

    # 4. 创建启动脚本
    print('\n==> 4. 创建启动脚本（主程序 + 日志查看器）')
    run_script, run_log_script = _create_run_script(plat)
    print(f'    主程序: {run_script}')
    print(f'    日志查看器: {run_log_script}')

    # 5. 创建桌面快捷方式
    print('\n==> 5. 创建桌面快捷方式 + 日志查看器入口（含 MimeType）')
    _create_desktop_shortcut(plat, run_script, run_log_script)

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
        print('  1. 应用菜单搜「串口调试助手」或「日志查看器」')
        print(f'  2. 命令行：bash {run_script}')
        print('  3. 文件管理器右键 .log/.txt 文件 → 打开方式 → 日志查看器')
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
    run_log_name = 'run_log.bat' if plat == 'win32' else 'run_log.sh'
    run_log_path = os.path.join(_PROJECT_DIR, run_log_name)
    if os.path.exists(run_log_path):
        print(f'==> 删除日志查看器脚本: {run_log_path}')
        os.remove(run_log_path)

    # 3. 删桌面快捷方式 + 日志查看器入口
    if plat == 'win32':
        for lnk_name in ('串口调试助手.lnk', '日志查看器.lnk'):
            lnk = os.path.expanduser(f'~/Desktop/{lnk_name}')
            if os.path.exists(lnk):
                os.remove(lnk)
    elif plat == 'linux':
        desk_dir = os.path.expanduser('~/.local/share/applications')
        for name in ('serial_debug_src.desktop', 'serial_debug_log_src.desktop'):
            p = os.path.join(desk_dir, name)
            if os.path.exists(p):
                os.remove(p)
        subprocess.run(['update-desktop-database', desk_dir],
                       check=False, capture_output=True)

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
