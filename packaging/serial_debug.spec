# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec 文件 - 串口调试助手跨平台打包。

构建命令（在仓库根目录执行）：
  pyinstaller --clean packaging/serial_debug.spec

输出：
  Linux:   dist/serial_debug/        (onedir，后续用 build_linux.sh 转 AppImage)
  Windows: dist/serial_debug/       (onedir，可用 Inno Setup 打包成安装器)
  macOS:   dist/serial_debug.app     (app bundle)

图标：
  Linux:   .desktop 文件引用 serial_debug 资源目录的 PNG
  Windows: 嵌入 src/serial_debug/resources/assets/icon.ico
  macOS:   嵌入 src/serial_debug/resources/assets/icon.icns
"""
import os
import sys
from PyInstaller.utils.hooks import collect_submodules

# pyserial 延迟导入 list_ports 平台子模块，必须显式收集
hiddenimports = collect_submodules('serial')
# PySide6 + pyqtgraph：Qt 试点曲线窗口依赖（延迟导入，主程序不强制）
# 只收集核心子模块，避免 PySide6 全量打包膨胀体积（仍 ~80MB，可接受）
try:
    hiddenimports += collect_submodules('PySide6.QtCore')
    hiddenimports += collect_submodules('PySide6.QtGui')
    hiddenimports += collect_submodules('PySide6.QtWidgets')
except Exception:
    pass
hiddenimports += ['pyqtgraph', 'pyqtgraph.functions',
                  'pyqtgraph.graphicsItems', 'pyqtgraph.graphicsItems.PlotItem',
                  'pyqtgraph.graphicsItems.PlotDataItem',
                  'pyqtgraph.graphicsItems.ViewBox',
                  'pyqtgraph.graphicsItems.AxisItem',
                  'numpy', 'numpy.core', 'numpy.core._multiarray_umath',
                  # OpenGL 加速：pyqtgraph useOpenGL=True 需要 PyOpenGL
                  'OpenGL', 'OpenGL.GL', 'OpenGL_accelerate']

# 仓库根目录（spec 在 packaging/ 下，向上一层）
_here = os.path.dirname(os.path.abspath(SPEC))
_root = os.path.dirname(_here)
_resources = os.path.join(_root, 'src', 'serial_debug', 'resources')

# 图标路径：按平台选择
_icon_path = None
if sys.platform == 'win32':
    _icon_path = os.path.join(_resources, 'assets', 'icon_debug.ico')
    if not os.path.exists(_icon_path):
        _icon_path = os.path.join(_resources, 'assets', 'icon_debug.png')
elif sys.platform == 'darwin':
    _icon_path = os.path.join(_resources, 'assets', 'icon_debug.icns')
    if not os.path.exists(_icon_path):
        _icon_path = os.path.join(_resources, 'assets', 'icon_debug.png')
else:
    _icon_path = os.path.join(_resources, 'assets', 'icon_debug.png')

a = Analysis(
    [os.path.join(_root, 'src', 'serial_debug', 'app.py')],
    pathex=[os.path.join(_root, 'src')],
    binaries=[],
    datas=[
        (os.path.join(_resources, 'assets'), 'serial_debug/resources/assets'),
        (os.path.join(_resources, 'common_at.json'), 'serial_debug/resources'),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # 通用不用的库
        'matplotlib', 'pytest', 'IPython',
        'setuptools', 'pip', 'distutils',
        # Qt 不用的模块（保留 Core/Gui/Widgets/OpenGL/Svg/Network/DBus/Wayland）
        # QML / Quick：不用 Qt 声明式 UI，省 ~22MB
        'PySide6.QtQml', 'PySide6.QtQuick', 'PySide6.QtQuick3D',
        'PySide6.QtQuickWidgets', 'PySide6.QtQuickControls2',
        'PySide6.QtQuickParticles', 'PySide6.QtQuickTemplates2',
        # PDF：不渲染 PDF，省 ~5MB
        'PySide6.QtPdf', 'PySide6.QtPdfWidgets',
        # 虚拟键盘：桌面应用不用，省 ~0.6MB
        'PySide6.QtVirtualKeyboard',
        # 其他不用的 Qt 模块
        'PySide6.QtTest',              # 单元测试框架
        'PySide6.QtMultimedia',        # 多媒体
        'PySide6.QtMultimediaWidgets',
        'PySide6.QtSql',               # SQL 数据库
        'PySide6.QtWebEngine',         # WebEngine（巨大）
        'PySide6.QtWebEngineCore',
        'PySide6.QtWebEngineWidgets',
        'PySide6.QtWebSockets',
        'PySide6.QtWebChannel',
        'PySide6.QtCharts',
        'PySide6.QtDataVisualization',
        'PySide6.QtBluetooth',
        'PySide6.QtNfc',
        'PySide6.QtPositioning',
        'PySide6.QtLocation',
        'PySide6.QtSensors',
        'PySide6.QtSerialPort',        # 我们用 pyserial，不用 Qt 串口
        'PySide6.QtSerialBus',
        'PySide6.QtRemoteObjects',
        'PySide6.QtScxml',
        'PySide6.QtStateMachines',
        'PySide6.QtHelp',
        'PySide6.QtPrintSupport',
        'PySide6.QtUiTools',
        'PySide6.QtDesigner',
        'PySide6.QtUiPlugin',
        'PySide6.QtLinguistTools',
        'PySide6.QtQmlDevTools',
        'PySide6.QtAxContainer',
        # 旧版 Qt bindings（不混用）
        'PyQt5', 'PyQt6', 'PySide2',
    ],
    noarchive=False,
)

# 手动从 binaries 移除 PySide6 hook 强制拉入但我们不用的 Qt 库
# （PyInstaller excludes 只对 Python 模块生效，对 .so 库无效）
_qt_strip_libs = (
    'libQt6Qml', 'libQt6Quick', 'libQt6Quick3D', 'libQt6QuickParticles',
    'libQt6QuickTemplates2', 'libQt6QuickControls2', 'libQt6QuickShapes',
    'libQt6QuickEffects', 'libQt6QuickDialogs2', 'libQt6QuickLayouts',
    'libQt6QmlMeta', 'libQt6QmlModels', 'libQt6QmlWorkerScript',
    'libQt6Pdf', 'libQt6PdfWidgets',
    'libQt6VirtualKeyboard', 'libQt6VirtualKeyboardQml',
    'libQt6Test',
    'libQt6Multimedia', 'libQt6MultimediaWidgets',
    'libQt6Sql',
    'libQt6WebEngine', 'libQt6WebEngineCore', 'libQt6WebEngineWidgets',
    'libQt6WebSockets', 'libQt6WebChannel',
    'libQt6Charts', 'libQt6DataVisualization',
    'libQt6Bluetooth', 'libQt6Nfc',
    'libQt6Positioning', 'libQt6Location',
    'libQt6Sensors', 'libQt6SerialPort', 'libQt6SerialBus',
    'libQt6RemoteObjects', 'libQt6Scxml',
    'libQt6Help', 'libQt6PrintSupport',
    'libQt6UiTools', 'libQt6Designer',
    # 注意：ICU（libicudata/libicui18n/libicuuc）不能去，Qt 字符串处理基础依赖
    # 去掉会 ImportError: libicui18n.so.73 无法启动
)


def _strip_binaries(binaries):
    """过滤掉 PySide6 hook 强制拉入但不用的 Qt 库。"""
    filtered = []
    for item in binaries:
        # item 是 (dest_name, source_path) 元组
        name = item[0] if isinstance(item, tuple) else str(item)
        # 取文件名（去路径）
        base = name.rsplit('/', 1)[-1].rsplit('\\', 1)[-1]
        # 去掉版本号前缀，如 libQt6Qml.so.6 → libQt6Qml
        stem = base.split('.so')[0].split('.dylib')[0]
        if stem.endswith('.dll'):
            stem = stem[:-4]
        # 检查是否在剥离列表中
        if any(stem == lib or stem.startswith(lib + '.') for lib in _qt_strip_libs):
            continue
        filtered.append(item)
    return filtered


a.binaries = _strip_binaries(a.binaries)

pyz = PYZ(a.pure, a.zipped_data)

# 平台条件：构建时 sys.platform 为当前构建平台
_platform = sys.platform

if _platform == 'darwin':
    # macOS: .app bundle
    exe = EXE(
        pyz, a.scripts, [],
        exclude_binaries=True,
        name='serial_debug',
        debug=False,
        strip=False,
        upx=False,
        console=False,
        icon=_icon_path,
        target_arch='universal2',
        codesign_identity=None,
        entitlements_file=None,
    )
    coll = COLLECT(
        exe, a.binaries, a.zipfiles, a.datas,
        name='serial_debug',
    )
    app = BUNDLE(
        coll,
        name='serial_debug.app',
        icon=_icon_path,
        bundle_identifier='com.serial-debug.app',
        info_plist={
            'CFBundleShortVersionString': '1.0.0',
            'CFBundleName': '串口调试助手',
            'NSHighResolutionCapable': True,
        },
    )
elif _platform == 'win32':
    # Windows: onedir + windowed
    exe = EXE(
        pyz, a.scripts, [],
        exclude_binaries=True,
        name='serial_debug',
        debug=False,
        strip=False,
        upx=True,
        console=False,
        icon=_icon_path,
        disable_windowed_traceback=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
    coll = COLLECT(
        exe, a.binaries, a.zipfiles, a.datas,
        name='serial_debug',
    )
else:
    # Linux: onedir（后续用 build_linux.sh 转 AppImage）
    exe = EXE(
        pyz, a.scripts, [],
        exclude_binaries=True,
        name='serial_debug',
        debug=False,
        strip=False,
        upx=True,
        console=False,
        icon=_icon_path,
    )
    coll = COLLECT(
        exe, a.binaries, a.zipfiles, a.datas,
        name='serial_debug',
    )
