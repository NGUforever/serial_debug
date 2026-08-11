"""流式布局：子部件按顺序排列，宽度不够时自动换行。

QHBoxLayout 在窗口变窄时会把子部件压缩到 minimumSizeHint 以下，
导致按钮文字截断。QFlowLayout 改为：当一行排不下时自动换到下一行，
保证每个子部件显示完整。

用于底栏和 AT 面板顶部的按钮组——窗口够宽时一行显示，不够时
自动分到 2 行或 3 行，所有按钮文字都看得见。

每个 addItem 进来的子部件当作"不可分割的单元"，整个换行不会
从单元中间断开。用法：把每个功能组的按钮先包成一个 QWidget
（内部 QHBoxLayout），再把整个组 add 进 QFlowLayout。

参考 Qt 守方 example: examples/layouts/flowlayout.
"""
from __future__ import annotations

from PySide6.QtCore import QMargins, QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import QLayout, QSizePolicy, QWidgetItem


class QFlowLayout(QLayout):
    """子部件按顺序流式排列，一行不够自动换行。"""

    def __init__(self, parent=None, margin=6, h_spacing=8, v_spacing=6):
        super().__init__(parent)
        self._h_spacing = h_spacing
        self._v_spacing = v_spacing
        self._items: list = []
        self.setContentsMargins(QMargins(margin, margin, margin, margin))

    # ---- 必须实现的 QLayout 接口 ----------------------------------
    def addItem(self, item):
        # QLayout 用 addItem 接收 QLayoutItem（widget 或 sublayout）。
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index):
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):
        # 流式布局不主动占满空间，让外部 layout 决定宽度。
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        # 宽度变化时高度也变（换行多了高度增加），必须返回 True
        # 才能让父 layout 调用 heightForWidth 算高度。
        return True

    def heightForWidth(self, width):
        # 给定宽度时算需要多少高度。testOnly=True 表示只算不摆。
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        size += QSize(m.left() + m.right(), m.top() + m.bottom())
        return size

    # ---- 摆放逻辑 --------------------------------------------------
    def _do_layout(self, rect, test_only):
        """核心算法：贪心地从左往右摆，摆不下就换行。

        返回该 rect 宽度下布局所需的总高度。
        """
        x = rect.x()
        y = rect.y()
        line_height = 0
        spacing = self._h_spacing
        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + spacing
            # 当前行放不下且不是行首：换行
            if next_x - spacing > rect.right() and line_height > 0:
                x = rect.x()
                y = y + line_height + self._v_spacing
                line_height = 0
                next_x = x + hint.width() + spacing
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y()
