"""Docker offscreen smoke test：验证源码改动是否满足要求。

被 scripts/setup.py verify 调用，也可单独跑：
    python scripts/smoke_test.py

跑方式：
1. 在 docker ubuntu:22.04 内装 PySide6 + pyqtgraph + numpy + PyOpenGL
2. QT_QPA_PLATFORM=offscreen 跑 QApplication
3. 验证主窗口 + AT 面板 + 日志查看器 + 数据监视窗口能构建、关键功能链路 OK

不触碰稳定版（~/.local/share/serial_debug/），纯验证源码。
"""
from __future__ import annotations

import os
import sys


_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC_DIR = os.path.join(_PROJECT_DIR, 'src')


def _setup_path():
    """把 src/ 加到 sys.path 让 import serial_debug 可用。"""
    if _SRC_DIR not in sys.path:
        sys.path.insert(0, _SRC_DIR)


def _smoke_main_window() -> list[str]:
    """主窗口构建 + 底栏 FlowLayout + 缩放验证。返回错误列表。"""
    errors = []
    try:
        from PySide6.QtWidgets import QApplication, QFrame
        if not QApplication.instance():
            QApplication([])
        from serial_debug.ui.qt_app import QtSerialDebugApp
        inst = QtSerialDebugApp()
        win = inst._create()
        win.resize(1200, 800)
        win.show()
        QApplication.processEvents()

        # 找到底栏 QFlowLayout
        fl = None
        for c in win.findChildren(QFrame):
            if c.layout() and c.layout().__class__.__name__ == 'QFlowLayout':
                fl = c.layout()
                break
        if fl is None:
            errors.append('底栏 QFlowLayout 未找到')
        else:
            h_full = fl.heightForWidth(1200)
            h_narrow = fl.heightForWidth(400)
            if not (h_narrow > h_full):
                errors.append(
                    f'窄屏换行失效：宽 1200 高 {h_full}, 宽 400 高 {h_narrow}')

        # 缩放不崩
        win.resize(400, 600)
        QApplication.processEvents()
        win.resize(1600, 900)
        QApplication.processEvents()
        win.close()
    except Exception as e:
        import traceback
        errors.append(f'主窗口: {traceback.format_exc().splitlines()[-1]}')
    return errors


def _smoke_at_panel() -> list[str]:
    """AT 面板构建 + 顶部 FlowLayout 验证。"""
    errors = []
    try:
        from PySide6.QtWidgets import QApplication
        if not QApplication.instance():
            QApplication([])
        from serial_debug.ui.qt_at_cmd_window import AtCmdPanelQt
        panel = AtCmdPanelQt(send_callback=lambda c: True)
        w = panel._build()
        w.show()
        QApplication.processEvents()
        top = w.layout().itemAt(0).layout()
        if top.__class__.__name__ != 'QFlowLayout':
            errors.append(f'AT 面板顶部不是 QFlowLayout: {top.__class__.__name__}')
        if w.minimumWidth() < 360:
            errors.append(f'AT 面板 minimumWidth < 360: {w.minimumWidth()}')
        w.close()
    except Exception as e:
        import traceback
        errors.append(f'AT 面板: {traceback.format_exc().splitlines()[-1]}')
    return errors


def _smoke_log_viewer() -> list[str]:
    """日志查看器构建验证。"""
    errors = []
    try:
        from PySide6.QtWidgets import QApplication
        if not QApplication.instance():
            QApplication([])
        from serial_debug.ui.qt_log_viewer_window import QtLogViewerWindow
        viewer = QtLogViewerWindow()
        viewer.show()
        QApplication.processEvents()
        viewer.close()
    except Exception as e:
        import traceback
        errors.append(f'日志查看器: {traceback.format_exc().splitlines()[-1]}')
    return errors


def _smoke_monitor_window() -> list[str]:
    """数据监视窗口构建验证。"""
    errors = []
    try:
        from PySide6.QtWidgets import QApplication
        if not QApplication.instance():
            QApplication([])
        from serial_debug.ui.qt_monitor_window import QtMonitorWindow
        mon = QtMonitorWindow()
        win = mon._create()
        win.show()
        QApplication.processEvents()
        win.close()
    except Exception as e:
        import traceback
        errors.append(f'数据监视: {traceback.format_exc().splitlines()[-1]}')
    return errors


def run_all() -> int:
    """跑所有 smoke test，返回 0=全过，非 0=有失败。"""
    _setup_path()
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

    print('==> 1. 主窗口 + 底栏 FlowLayout')
    errs = _smoke_main_window()
    if errs:
        for e in errs:
            print(f'    FAIL: {e}')
        return 1
    print('    OK')

    print('==> 2. AT 命令面板 + 顶部 FlowLayout')
    errs = _smoke_at_panel()
    if errs:
        for e in errs:
            print(f'    FAIL: {e}')
        return 1
    print('    OK')

    print('==> 3. 日志查看器')
    errs = _smoke_log_viewer()
    if errs:
        for e in errs:
            print(f'    FAIL: {e}')
        return 1
    print('    OK')

    print('==> 4. 数据监视窗口')
    errs = _smoke_monitor_window()
    if errs:
        for e in errs:
            print(f'    FAIL: {e}')
        return 1
    print('    OK')

    print('\n=== smoke test PASS ===')
    return 0


if __name__ == '__main__':
    sys.exit(run_all())
