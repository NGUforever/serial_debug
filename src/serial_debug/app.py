"""串口调试助手 —— 启动入口路由。

tkinter 兼容路径已在 v1.1.0 移除，仅保留 Qt（PySide6）入口：
  - 默认：Qt 主程序（qt_app.QtSerialDebugApp）
  - ``--view F1 [F2 ...]``：只启动 Qt 日志查看器加载给定文件
  - 环境变量 ``SERIAL_DEBUG_QT_PILOT=1``：Qt 监视窗口独立子进程
  - 环境变量 ``SERIAL_DEBUG_LOG_VIEWER=1``：Qt 日志查看器独立子进程
  - 环境变量 ``SERIAL_DEBUG_QT_MAIN=1``：与默认等价（保留兼容）

PyInstaller 打包后 ``sys.executable -m <module>`` 不识别，改用环境
变量区分子口；详见 ``qt_app._open_log_viewer`` / ``_open_qt_monitor``。
"""
from __future__ import annotations

import os
import sys


def resource_path(relative_path: str) -> str:
    """返回包内资源文件绝对路径，兼容 PyInstaller 打包和开发模式。

    PyInstaller 打包后资源解压到 sys._MEIPASS/serial_debug/resources；
    开发模式用 importlib.resources 解析包路径。
    """
    try:
        base_path = sys._MEIPASS  # type: ignore[attr-defined]
        return os.path.join(base_path, 'serial_debug', 'resources', relative_path)
    except AttributeError:
        from importlib.resources import files
        base = files('serial_debug.resources')
        return str(base / relative_path)


def main():
    """启动入口。

    用法：
      serial_debug                    # 启动 Qt 主程序（默认）
      serial_debug --view F1 [F2 ...]  # 只启动日志查看器加载给定文件

    环境变量：
      SERIAL_DEBUG_QT_PILOT=1        启动 Qt 监视窗口（曲线）
      SERIAL_DEBUG_QT_MAIN=1         强制 Qt 主程序（与默认等价，保留兼容）
      SERIAL_DEBUG_LOG_VIEWER=1      启动独立 Qt 日志查看器窗口
    （PyInstaller 打包后 sys.executable -m <module> 不工作，
     改用环境变量区分入口，子进程看到此变量走对应路径）。
    """
    if os.environ.get('SERIAL_DEBUG_QT_PILOT') == '1':
        # Qt 试点窗口：独立子进程运行，避免与主事件循环冲突
        try:
            from serial_debug.ui.qt_monitor_window import QtMonitorWindow
            sys.exit(QtMonitorWindow().run())
        except Exception as e:
            import traceback
            traceback.print_exc()
            try:
                from PySide6.QtWidgets import QApplication, QMessageBox
                QApplication.instance() or QApplication(sys.argv)
                QMessageBox.critical(None, '启动失败',
                                     f'Qt 监视窗口启动失败:\n{e}\n\n{traceback.format_exc()}')
            except Exception:
                pass
            sys.exit(1)
    if os.environ.get('SERIAL_DEBUG_LOG_VIEWER') == '1':
        # 独立日志查看器子进程（Qt 主程序的工具菜单启动）
        return _run_log_viewer_only([])
    # 默认：Qt 主程序
    args = sys.argv[1:]
    if args and args[0] == '--view':
        # --view 走 Qt 日志查看器
        files = [f for f in args[1:] if not f.startswith('-')]
        return _run_log_viewer_only(files)
    from serial_debug.ui.qt_app import QtSerialDebugApp
    sys.exit(QtSerialDebugApp().run())


def _run_log_viewer_only(files: list[str]):
    """独立模式：只启动 Qt 日志查看器窗口。

    桌面图标启动时（无文件参数）直接打开空窗口，用户在工具栏里点
    "打开文件…" 选择日志文件；避免出现"先弹文件对话框、选完才开主窗口"
    的反直觉流程。命令行带文件参数（如 ``serial_debug --view f1.txt``）
    仍按原方式自动加载这些文件。

    走 Qt 路径（PySide6 QPlainTextEdit + QTabWidget），与 Qt 主程序
    qt_app.QtSerialDebugApp 一致；环境变量 SERIAL_DEBUG_LOG_VIEWER=1
    由 qt_app._open_log_viewer 子进程注入，PyInstaller 打包后
    sys.executable -m 不识别，改用环境变量区分入口。

    Qt 不可用时打印 stderr 退出（不再回退 tkinter，v1.1.0 起无 tk 路径）。
    """
    from serial_debug.infra import themes
    pref = themes.load_pref()
    resolved = themes.resolve(pref)
    try:
        from PySide6.QtWidgets import QApplication

        from serial_debug.ui.qt_log_viewer_window import QtLogViewerWindow
    except Exception as e:
        import traceback
        traceback.print_exc()
        sys.stderr.write(
            f'Qt 不可用：{e}\n请确认已安装 PySide6（pip install PySide6）\n')
        sys.exit(1)
    app = QApplication.instance() or QApplication(sys.argv)
    # 显式设置 applicationName + displayName + desktopFileName，
    # 让 GNOME dock（X11 WM_CLASS / Wayland app_id）识别为"日志查看器"
    # 而不是主程序"串口调试助手"。
    # - X11：WM_CLASS 由 applicationName 决定，匹配 desktop file StartupWMClass
    # - Wayland：app_id 由 applicationName/desktopFileName 决定，匹配 desktop
    #   file basename。若不设 desktopFileName，Wayland 可能用 /proc/PID/exe
    #   basename（symlink resolve 后 = serial_debug）匹配到主程序 desktop file。
    app.setApplicationName('serial_debug_log')
    app.setApplicationDisplayName('日志查看器')
    try:
        app.setDesktopFileName('serial_debug_log')
    except Exception:
        pass
    from serial_debug.ui import qt_theme
    qt_theme.apply_qss(app, resolved)
    win = QtLogViewerWindow(theme_name=resolved)
    # 独立图标
    try:
        icon_path = resource_path(os.path.join('assets', 'icon_view.png'))
        if os.path.exists(icon_path):
            from PySide6.QtGui import QIcon
            app.setWindowIcon(QIcon(icon_path))
    except Exception:
        pass
    for p in files:
        win.open_file(p)
    win.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
