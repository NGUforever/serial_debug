"""pip 装包工具：多镜像源依次重试，避免单源失败就死。

被 scripts/setup.py 和 packaging/build_*.sh 调用。
用法：
    python scripts/pip_install_with_fallback.py -r requirements.txt pyinstaller
    python scripts/pip_install_with_fallback.py PySide6 pyqtgraph numpy

镜像源顺序：官方 → 清华 → 阿里 → douban，第一个成功就停。

PEP 668（Ubuntu 24.04+ / Debian 12+）禁止系统级 pip install：
检测到 EXTERNALLY-MANAGED 标记时自动加 --break-system-packages（pip 23.0+），
打印警告说明此举会向系统 Python 装包，长期建议 venv 隔离。
"""
from __future__ import annotations

import os
import subprocess
import sys
import sysconfig


MIRRORS = [
    None,  # 官方 PyPI（不指定 -i）
    'https://pypi.tuna.tsinghua.edu.cn/simple',  # 清华
    'https://mirrors.aliyun.com/pypi/simple',     # 阿里
    'https://pypi.doubanio.com/simple',           # 豆瓣
]


def _is_externally_managed() -> bool:
    """检测当前 Python 是否启用 PEP 668 externally-managed。

    Python 3.11+ 在 stdlib 目录下放 EXTERNALLY-MANAGED 标记文件告诉 pip
    不要系统级安装。Ubuntu 24.04 / Debian 12 / Fedora 38+ 默认放。

    venv 内即使 stdlib 有这个文件也不算 externally-managed——
    venv 本来就是用户私有空间，pip install 总是允许。
    """
    # venv 内总是允许装包，不算 externally-managed
    if sys.prefix != sys.base_prefix:
        return False
    stdlib = sysconfig.get_path('stdlib')
    if not stdlib:
        return False
    return os.path.exists(os.path.join(stdlib, 'EXTERNALLY-MANAGED'))


def _supports_break_flag() -> bool:
    """检测当前 pip 是否支持 --break-system-packages（pip 23.0+）。"""
    try:
        r = subprocess.run(
            [sys.executable, '-m', 'pip', 'install', '--help'],
            capture_output=True, text=True, timeout=10, check=False)
        return '--break-system-packages' in r.stdout
    except Exception:
        return False


def install(packages: list[str], *, upgrade: bool = False) -> bool:
    """依次用各镜像源 pip install，成功返回 True。

    packages 是 pip 的参数列表，可包含 -r requirements.txt、包名、
    带版本约束的包名等。--no-input 避免交互卡死。

    PEP 668 环境自动加 --break-system-packages（pip 23.0+）。
    """
    base_args = [sys.executable, '-m', 'pip', 'install', '--no-input',
                 '--disable-pip-version-check']
    if upgrade:
        base_args.append('-U')

    # PEP 668 兼容：Ubuntu 24.04+ / Debian 12+ 系统级 pip install 被禁，
    # 自动加 --break-system-packages（pip 23.0+）绕过。这是 build 阶段
    # 的临时方案，长期应该用 venv 隔离。
    externally_managed = _is_externally_managed()
    supports_break = _supports_break_flag()
    if externally_managed and supports_break:
        print('==> 检测到 PEP 668 externally-managed-environment 限制')
        print('    (Ubuntu 24.04+ / Debian 12+ 禁止系统级 pip install)')
        print('    自动加 --break-system-packages 绕过')
        print('    警告：此操作会向系统 Python 装 PySide6/pyqtgraph/numpy 等')
        print('    长期建议：python3 -m venv .venv && source .venv/bin/activate')
        base_args.append('--break-system-packages')
    elif externally_managed and not supports_break:
        print('==> 警告：检测到 PEP 668 externally-managed-environment')
        print('    但 pip 不支持 --break-system-packages（pip < 23.0）')
        print('    请手动升级 pip：python3 -m pip install --user --upgrade pip')
        print('    或用 venv：python3 -m venv .venv && source .venv/bin/activate')

    last_err = ''
    for idx, mirror in enumerate(MIRRORS):
        args = list(base_args)
        if mirror:
            args += ['-i', mirror,
                     '--trusted-host', _host_of(mirror)]
        args += packages
        label = mirror or '官方 PyPI'
        print(f'==> pip install [{idx + 1}/{len(MIRRORS)}] {label}')
        try:
            subprocess.check_call(args)
            print(f'    成功（{label}）')
            return True
        except subprocess.CalledProcessError as e:
            last_err = f'{label}: returncode={e.returncode}'
            print(f'    失败（{last_err}），换源重试')
        except Exception as e:
            last_err = f'{label}: {e}'
            print(f'    异常（{last_err}），换源重试')

    print()
    print('全部镜像源都失败。诊断建议：')
    print('  1. 测试网络：curl -I https://pypi.org/simple/')
    print('  2. 检查代理：env | grep -i proxy')
    print('  3. 离线场景：把 wheels 放到本地目录，pip install --no-index '
          '--find-links=./wheels <pkgs>')
    print(f'  最后错误：{last_err}')
    return False


def _host_of(url: str) -> str:
    """从 https://x.y.z/simple 取 x.y.z（pip --trusted-host 要主机名）。"""
    if '://' in url:
        url = url.split('://', 1)[1]
    if '/' in url:
        url = url.split('/', 1)[0]
    return url


def main():
    args = sys.argv[1:]
    if not args:
        print('用法：pip_install_with_fallback.py [-r requirements.txt] '
              '<pkg1> [pkg2 ...]')
        sys.exit(2)
    upgrade = False
    if '-U' in args or '--upgrade' in args:
        upgrade = True
        args = [a for a in args if a not in ('-U', '--upgrade')]
    ok = install(args, upgrade=upgrade)
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
