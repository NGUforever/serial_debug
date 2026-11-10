"""项目级对话框基类：统一支持最大化 / 还原切换。

所有自定义内容对话框（解析器配置、编辑图表、添加图表、曲线配置、
监视窗口内各类 QDialog 等）都应使用本基类，而不是直接用 QDialog：

    from serial_debug.ui.qt_maximizable_dialog import MaximizableDialog
    dlg = MaximizableDialog(parent)   # 构造签名与 QDialog 完全一致
    # 或
    class MyDialog(MaximizableDialog):
        ...

能力：
  - 标题栏带最大化 / 还原按钮（WindowMinMaxButtonsHint +
    WindowMaximizeButtonHint），仅在不破坏既有窗口标志的前提下追加。
  - 双击标题栏在最大化 / 还原之间切换。
  - 记住窗口尺寸状态：最大化前保存 normalGeometry，还原时恢复；
    另提供 save_state / restore_state 供 config 层持久化（可选）。

不改变布局与业务逻辑——只是 QDialog 的替换品。
QMessageBox 等系统标准弹窗不需要改。
"""
from __future__ import annotations

# PySide6 顶层导入：本模块只被 Qt UI 代码使用；包 import 失败时
# 置 None 让下方类定义退化为普通 mixin（供纯逻辑测试 import 不崩）
try:
    from PySide6.QtWidgets import QDialog as _QDialogBase
except Exception:  # pragma: no cover - 无 GUI 环境
    _QDialogBase = object


def make_maximizable(dlg):
    """给已构造好的 QDialog 追加最大化 / 还原能力（实例级换类）。

    用于不方便改继承关系的位置。优先直接用 MaximizableDialog。
    """
    if isinstance(dlg, MaximizableDialog):
        return dlg
    if _QDialogBase is object or not isinstance(dlg, _QDialogBase):
        return dlg

    class _Max(_QDialogBase, _MaximizeMixin):
        pass

    _Max.__name__ = dlg.__class__.__name__
    dlg.__class__ = _Max
    dlg._saved_normal_geometry = None
    _MaximizeMixin._apply_hints(dlg)
    return dlg


class _MaximizeMixin:
    """最大化 / 还原逻辑（供基类与 make_maximizable 共用）。"""

    @staticmethod
    def _apply_hints(dlg):
        """追加最大/还原按钮 hint，不动既有窗口标志。"""
        try:
            from PySide6.QtCore import Qt
            flags = dlg.windowFlags()
            flags |= Qt.WindowMaximizeButtonHint | Qt.WindowMinMaxButtonsHint
            dlg.setWindowFlags(flags)
        except Exception:
            pass

    def _save_normal_geometry(self):
        """最大化前记录 normal 状态的几何，供还原时恢复。"""
        try:
            self._saved_normal_geometry = self.normalGeometry()
        except Exception:
            try:
                self._saved_normal_geometry = self.geometry()
            except Exception:
                self._saved_normal_geometry = None

    def toggle_maximize(self):
        """在最大化 / 还原之间切换，并记住窗口尺寸状态。"""
        try:
            from PySide6.QtCore import Qt
            if self.windowState() & Qt.WindowState.WindowMaximized:
                self.showNormal()
                saved = getattr(self, '_saved_normal_geometry', None)
                if saved is not None:
                    try:
                        self.setGeometry(saved)
                    except Exception:
                        pass
            else:
                self._save_normal_geometry()
                self.showMaximized()
        except Exception:
            pass  # 任何异常都不允许拖崩对话框


class MaximizableDialog(_QDialogBase, _MaximizeMixin):
    """QDialog 替代品：标题栏带最大/还原按钮 + 双击标题栏切换。"""

    def __init__(self, *args, **kwargs):
        self._saved_normal_geometry = None
        _QDialogBase.__init__(self, *args, **kwargs)
        self._apply_hints(self)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        """双击标题栏 / 客户区空隙切换最大化 / 还原。

        子控件（表格、画布、输入框）上的双击由子控件自己消费，
        不会传播到这里；只有点到对话框自身空白区才触发切换。
        """
        try:
            from PySide6.QtCore import Qt
            if event.button() == Qt.MouseButton.LeftButton:
                self.toggle_maximize()
                event.accept()
                return
        except Exception:
            pass
        try:
            from PySide6.QtWidgets import QDialog
            QDialog.mouseDoubleClickEvent(self, event)
        except Exception:
            pass

    # ---- 状态持久化（可选接入 config 层）----------------------------
    def save_state(self) -> dict:
        """导出窗口几何状态（可 json 持久化）。"""
        from PySide6.QtCore import Qt
        st = self.windowState()
        return {
            'maximized': bool(st & Qt.WindowState.WindowMaximized),
            'geometry': list(self.geometry().getRect()),
        }

    def restore_state(self, state) -> None:
        """恢复 save_state 导出的状态；坏数据静默忽略。"""
        if not isinstance(state, dict):
            return
        try:
            from PySide6.QtCore import Qt
            geo = state.get('geometry')
            if geo and not state.get('maximized'):
                self.setGeometry(*[int(x) for x in geo])
            if state.get('maximized'):
                self.setWindowState(Qt.WindowState.WindowMaximized)
        except Exception:
            pass
