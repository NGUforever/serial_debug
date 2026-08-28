"""Qt 主题系统：VSCode Dark+ 配色 + QSS 样式表。

复用 infra/themes.py 的色板（dark/light），生成 QSS 供 PySide6 应用。
tkinter 和 Qt 共存期间，主题切换时两边同步。

设计原则：
  - 颜色与 tkinter 完全一致（同一份色板）
  - 控件圆角 4px（QSS constexpr，比 tkinter 直角更精致）
  - 按钮 hover/pressed 三态，primary 用 accent 蓝色
  - 输入框 focus 边框用 accent 蓝
  - 字体：DejaVu Sans 9（控件）/ DejaVu Sans Mono 10（数据/日志）
"""
from __future__ import annotations

from serial_debug.infra import themes

# Qt 字体（与 tkinter 保持一致）
QT_FONT_FAMILY = 'DejaVu Sans'
QT_MONO_FAMILY = 'DejaVu Sans Mono'
QT_FONT_SIZE = 9
QT_MONO_SIZE = 10


def _qss_color(c: str) -> str:
    """把 #rrggbb 转成 QSS 颜色（直接用即可，QSS 接受 #rgb / #rrggbb）。"""
    return c


def build_qss(theme_name: str) -> str:
    """根据主题名生成全局 QSS 样式表。

    :param theme_name: 具体主题名（dark/light/monokai/solarized_dark/
        solarized_light/github_light）。若传入模式名（auto/light/dark），
        会经 themes.resolve 解析为具体主题。
    :return: QSS 字符串，用 QApplication.setStyleSheet() 应用
    """
    t = themes.THEMES.get(theme_name)
    if t is None:
        # 兼容旧调用：传入 auto/light/dark 模式名 → 解析为具体主题
        try:
            resolved = themes.resolve(theme_name)
            t = themes.THEMES[resolved]
        except Exception:
            t = themes.THEMES['dark']

    bg = _qss_color(t['bg'])
    panel_bg = _qss_color(t['panel_bg'])
    fg = _qss_color(t['fg'])
    muted = _qss_color(t['muted'])
    border = _qss_color(t['border'])
    select_bg = _qss_color(t['select_bg'])
    btn_bg = _qss_color(t['btn_bg'])
    btn_hover = _qss_color(t['btn_hover'])
    btn_active = _qss_color(t['btn_active'])
    btn_primary = _qss_color(t['btn_primary'])
    btn_primary_hover = _qss_color(t['btn_primary_hover'])
    accent = _qss_color(t['accent'])

    # QSS 模板：通用控件样式
    qss = f"""
    /* 全局 */
    QWidget {{
        background-color: {bg};
        color: {fg};
        font-family: '{QT_FONT_FAMILY}';
        font-size: {QT_FONT_SIZE}pt;
    }}

    /* 窗口背景 */
    QMainWindow, QDialog {{
        background-color: {bg};
    }}

    /* 面板/Frame */
    QFrame#Panel, QFrame[panel="true"] {{
        background-color: {panel_bg};
        border: 1px solid {border};
        border-radius: 4px;
    }}

    /* 卡片（底栏等需要明显层级感的容器） */
    QFrame[card="true"] {{
        background-color: {panel_bg};
        border: 1px solid {border};
        border-radius: 6px;
    }}

    /* 竖直分隔条 */
    QFrame[divider="true"] {{
        background-color: {border};
        max-width: 1px;
        min-width: 1px;
        margin: 4px 8px;
    }}

    /* 工具栏（顶栏） */
    QToolBar {{
        background-color: {panel_bg};
        border: none;
        border-bottom: 1px solid {border};
        spacing: 4px;
        padding: 4px 6px;
        font-family: '{QT_FONT_FAMILY}';
        font-size: {QT_FONT_SIZE}pt;
    }}
    QToolBar QLabel {{
        background: transparent;
        color: {muted};
        padding: 0 4px 0 8px;
    }}
    QToolBar::separator {{
        width: 1px;
        height: 20px;
        background-color: {border};
        margin: 0 6px;
    }}
    QToolBar QToolButton {{
        background: transparent;
        border: none;
        padding: 4px 8px;
        border-radius: 4px;
    }}
    QToolBar QToolButton:hover {{
        background-color: {btn_hover};
    }}
    QToolBar QToolButton:pressed {{
        background-color: {btn_active};
    }}

    /* 按钮（普通） */
    QPushButton {{
        background-color: {btn_bg};
        color: {fg};
        border: 1px solid {border};
        border-radius: 4px;
        padding: 6px 14px;
        font-family: '{QT_FONT_FAMILY}';
        font-size: {QT_FONT_SIZE}pt;
    }}
    QPushButton:hover {{
        background-color: {btn_hover};
        border: 1px solid {accent};
    }}
    QPushButton:pressed {{
        background-color: {btn_active};
    }}
    QPushButton:disabled {{
        color: {muted};
        background-color: {btn_bg};
    }}

    /* Primary 按钮（发送/连接等主操作） */
    QPushButton[primary="true"] {{
        background-color: {btn_primary};
        color: #ffffff;
        border: 1px solid {btn_primary};
    }}
    QPushButton[primary="true"]:hover {{
        background-color: {btn_primary_hover};
        border: 1px solid {btn_primary_hover};
    }}
    QPushButton[primary="true"]:pressed {{
        background-color: {btn_active};
    }}

    /* 输入框 */
    QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox,
    QComboBox, QComboBox QLineEdit {{
        background-color: {panel_bg};
        color: {fg};
        border: 1px solid {border};
        border-radius: 4px;
        padding: 5px 8px;
        selection-background-color: {select_bg};
        selection-color: {fg};
        font-family: '{QT_MONO_FAMILY}';
        font-size: {QT_MONO_SIZE}pt;
    }}
    QPlainTextEdit {{
        padding: 6px 8px 6px 0;
        background-color: {panel_bg};
    }}
    QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus,
    QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
        border: 1px solid {accent};
    }}

    /* CheckBox / RadioButton：indicator 紧凑，文字留白 */
    QCheckBox, QRadioButton {{
        spacing: 6px;
        background: transparent;
        color: {fg};
        padding: 2px 4px;
    }}
    QCheckBox::indicator, QRadioButton::indicator {{
        width: 14px;
        height: 14px;
        border: 1px solid {border};
        background-color: {panel_bg};
        border-radius: 3px;
    }}
    QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
        background-color: {accent};
        border: 1px solid {accent};
    }}

    /* ComboBox 下拉 */
    QComboBox::drop-down {{
        border: none;
        width: 18px;
    }}
    QComboBox::down-arrow {{
        width: 8px;
        height: 8px;
        background: transparent;
    }}
    QComboBox QAbstractItemView {{
        background-color: {panel_bg};
        color: {fg};
        border: 1px solid {border};
        selection-background-color: {select_bg};
        selection-color: {fg};
        outline: 0;
    }}

    /* 标签 */
    QLabel {{
        background: transparent;
        color: {fg};
    }}
    QLabel[muted="true"] {{
        color: {muted};
    }}

    /* 列表/Tree */
    QTreeWidget, QListView, QTableView, QTableWidget {{
        background-color: {panel_bg};
        color: {fg};
        border: 1px solid {border};
        border-radius: 4px;
        selection-background-color: {select_bg};
        selection-color: {fg};
        alternate-background-color: {bg};
        outline: 0;
        font-family: '{QT_MONO_FAMILY}';
        font-size: {QT_MONO_SIZE}pt;
    }}
    QTreeWidget::item, QListView::item, QTableView::item {{
        padding: 2px 4px;
    }}
    QHeaderView::section {{
        background-color: {btn_bg};
        color: {fg};
        border: none;
        border-right: 1px solid {border};
        padding: 4px 8px;
        font-family: '{QT_FONT_FAMILY}';
    }}

    /* 滚动条 */
    QScrollBar:vertical {{
        background: transparent;
        width: 12px;
        margin: 0;
    }}
    QScrollBar::handle:vertical {{
        background: {btn_bg};
        border-radius: 4px;
        min-height: 24px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: {btn_hover};
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0;
    }}
    QScrollBar:horizontal {{
        background: transparent;
        height: 12px;
        margin: 0;
    }}
    QScrollBar::handle:horizontal {{
        background: {btn_bg};
        border-radius: 4px;
        min-width: 24px;
    }}
    QScrollBar::handle:horizontal:hover {{
        background: {btn_hover};
    }}

    /* 菜单栏 */
    QMenuBar {{
        background-color: {bg};
        color: {fg};
        border-bottom: 1px solid {border};
        padding: 2px;
        spacing: 2px;
    }}
    QMenuBar::item {{
        background: transparent;
        padding: 6px 12px;
        border-radius: 4px;
    }}
    QMenuBar::item:selected {{
        background-color: {btn_bg};
    }}
    QMenuBar::item:pressed {{
        background-color: {btn_active};
    }}
    QMenu {{
        background-color: {panel_bg};
        color: {fg};
        border: 1px solid {border};
        padding: 4px;
    }}
    QMenu::item {{
        padding: 6px 28px 6px 20px;
        border-radius: 4px;
    }}
    QMenu::item:selected {{
        background-color: {select_bg};
        color: #ffffff;
    }}
    QMenu::item:disabled {{
        color: {muted};
    }}
    /* shortcut 右侧灰色显示（Qt 自动布局，无需手动 column） */
    QMenu::right-arrow {{
        width: 8px;
        height: 8px;
    }}
    QMenu::separator {{
        height: 1px;
        background: {border};
        margin: 4px 8px;
    }}

    /* 状态栏 */
    QStatusBar {{
        background-color: {bg};
        color: {muted};
        border-top: 1px solid {border};
        padding: 2px 6px;
    }}
    QStatusBar::item {{ border: none; }}

    /* Tab */
    QTabWidget::pane {{
        border: 1px solid {border};
        border-radius: 4px;
        top: -1px;
    }}
    QTabBar::tab {{
        background: {btn_bg};
        color: {fg};
        padding: 6px 14px;
        border: 1px solid {border};
        border-bottom: none;
        border-top-left-radius: 4px;
        border-top-right-radius: 4px;
        margin-right: 1px;
    }}
    QTabBar::tab:selected {{
        background: {panel_bg};
        border-bottom: 2px solid {accent};
    }}

    /* GroupBox */
    QGroupBox {{
        background-color: {panel_bg};
        border: 1px solid {border};
        border-radius: 4px;
        margin-top: 8px;
        padding: 8px;
        font-weight: bold;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        subcontrol-position: top left;
        padding: 0 6px;
        color: {muted};
    }}

    /* ToolTip */
    QToolTip {{
        background-color: {panel_bg};
        color: {fg};
        border: 1px solid {border};
        border-radius: 4px;
        padding: 4px 8px;
        font-family: '{QT_MONO_FAMILY}';
        font-size: {QT_MONO_SIZE}pt;
    }}
    """
    return qss


def apply_qss(app, theme_name: str) -> None:
    """给 QApplication 应用 QSS 样式表。

    :param app: PySide6.QtWidgets.QApplication 实例
    :param theme_name: 具体主题名或模式名（auto/light/dark）。
        具体主题名直接用；模式名经 resolve 解析。
    """
    try:
        from PySide6.QtGui import QFont
    except Exception:
        return
    # 具体主题名直接用，否则 resolve 模式
    if theme_name in themes.THEMES:
        resolved = theme_name
    else:
        try:
            resolved = themes.resolve(theme_name)
        except Exception:
            resolved = 'dark'
    try:
        qss = build_qss(resolved)
        app.setStyleSheet(qss)
        # 默认字体
        f = QFont(QT_FONT_FAMILY, QT_FONT_SIZE)
        app.setFont(f)
    except Exception:
        pass


def palette(theme_name: str) -> dict:
    """返回 Qt 用的色板字典（与 tkinter 共用）。

    用于在 pyqtgraph / 自绘控件里直接取颜色（QColor 构造）。
    theme_name 可以是具体主题名或模式名。
    """
    t = themes.THEMES.get(theme_name)
    if t is None:
        try:
            resolved = themes.resolve(theme_name)
            t = themes.THEMES[resolved]
        except Exception:
            t = themes.THEMES['dark']
    return dict(t)
