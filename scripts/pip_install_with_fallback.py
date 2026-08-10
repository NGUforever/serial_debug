"""pip 装包工具：多镜像源依次重试，避免单源失败就死。

被 scripts/setup.py 和 packaging/build_*.sh 调用。
用法：
    python scripts/pip_install_with_fallback.py -r requirements.txt pyinstaller
    python scripts/pip_install_with_fallback.py PySide6 pyqtgraph numpy

镜像源顺序：官方 → 清华 → 阿里 → douban，第一个成功就停。
"""
from __future__ import annotations

import subprocess
import sys


MIRRORS = [
    None,  # 官方 PyPI（不指定 -i）
    'https://pypi.tuna.tsinghua.edu.cn/simple',  # 清华
    'https://mirrors.aliyun.com/pypi/simple',     # 阿里
    'https://pypi.doubanio.com/simple',           # 豆瓣
]


def install(packages: list[str], *, upgrade: bool = False) -> bool:
    """依次用各镜像源 pip install，成功返回 True。

    packages 是 pip 的参数列表，可包含 -r requirements.txt、包名、
    带版本约束的包名等。--no-input 避免交互卡死。
    """
    base_args = [sys.executable, '-m', 'pip', 'install', '--no-input',
                 '--disable-pip-version-check']
    if upgrade:
        base_args.append('-U')

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
