"""Qt AT 命令面板 - QTableWidget + 行内编辑 + 批量执行。

迁移自 tkinter 版 at_cmd_window.py，复用同样的 JSON 格式（cmd/desc/seq/
period_ms）以兼容旧配置。

列定义：
  cmd       — AT 命令字符串
  desc      — 功能描述
  seq       — 执行顺序（0=不参与批量，1/2/3...=顺序）
  period_ms — 批量执行时该命令发送后等待毫秒数
  op        — 操作（发送 / 删除按钮，按行）

批量执行：按 seq 升序（忽略 seq=0）依次发送，每条间等待 period_ms。

集成方式：
  - 嵌入式：AtCmdPanelQt 作为 QWidget 挂到主窗口
  - 独立窗口：AtCmdWindowQt (QDialog) 包装 AtCmdPanelQt
  - send_callback: (cmd: str) -> None，由调用方提供
"""
from __future__ import annotations

import json
import os

from serial_debug.infra import config


def _norm(item: dict) -> dict:
    """确保命令字典包含所有字段（向后兼容旧格式）。"""
    return {
        'cmd': item.get('cmd', ''),
        'desc': item.get('desc', ''),
        'seq': int(item.get('seq', 0) or 0),
        'period_ms': int(item.get('period_ms', 0) or 0),
    }


_COLS = ('cmd', 'desc', 'seq', 'period_ms', 'op')
_COL_LABELS = {
    'cmd': '命令', 'desc': '功能描述',
    'seq': '顺序', 'period_ms': '间隔(ms)', 'op': '操作',
}
_COL_WIDTHS = {'cmd': 220, 'desc': 220, 'seq': 60,
               'period_ms': 80, 'op': 180}


class AtCmdPanelQt:
    """Qt AT 命令面板（包装类，UI 在 _build() 延迟创建）。

    真正的 QWidget 在 _build() 里延迟创建，避免模块导入即 init Qt。
    """

    def __init__(self, send_callback, initial_cmds=None,
                 on_change=None, *, show_close_btn=False):
        self.send_callback = send_callback
        self.on_change = on_change
        self.commands: list[dict] = [_norm(c) for c in (initial_cmds or [])]
        self._show_close_btn = show_close_btn
        self.close_callback = None
        self._batch_running = False
        self._batch_queue: list = []
        self._batch_idx = 0
        self._widget = None      # QWidget
        self._table = None       # QTableWidget
        self._batch_btn = None
        self._stop_btn = None
        self._batch_status = None
        self._batch_timer = None

    # ---- UI 构建 -----------------------------------------------------
    def _build(self, parent=None):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import (
            QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QTableWidget,
            QTableWidgetItem, QHeaderView, QFrame, QLabel,
        )

        w = QFrame(parent)
        w.setProperty('panel', True)
        v = QVBoxLayout(w)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(6)

        # 工具栏
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(4)

        def _import_json():
            self._import_json(parent=w)
        btn = QPushButton('导入 JSON')
        btn.clicked.connect(_import_json)
        bar.addWidget(btn)

        def _export_json():
            self._export_json(parent=w)
        btn = QPushButton('导出 JSON')
        btn.clicked.connect(_export_json)
        bar.addWidget(btn)

        bar.addSpacing(8)

        btn = QPushButton('添加')
        btn.clicked.connect(self._add_row)
        bar.addWidget(btn)

        btn = QPushButton('删除选中')
        btn.clicked.connect(self._delete_selected)
        bar.addWidget(btn)

        btn = QPushButton('清空')
        btn.clicked.connect(self._clear_all)
        bar.addWidget(btn)

        bar.addSpacing(8)

        self._batch_btn = QPushButton('批量执行')
        self._batch_btn.setProperty('primary', True)
        self._batch_btn.clicked.connect(self._start_batch)
        bar.addWidget(self._batch_btn)

        self._stop_btn = QPushButton('停止')
        self._stop_btn.clicked.connect(self._stop_batch)
        self._stop_btn.setEnabled(False)
        bar.addWidget(self._stop_btn)

        self._batch_status = QLabel('空闲')
        self._batch_status.setMinimumWidth(120)
        bar.addWidget(self._batch_status)

        bar.addStretch(1)

        if self._show_close_btn:
            btn_close = QPushButton('关闭')
            btn_close.clicked.connect(self._on_close_clicked)
            bar.addWidget(btn_close)

        v.addLayout(bar)

        # 表格
        self._table = QTableWidget(0, len(_COLS), parent=w)
        self._table.setHorizontalHeaderLabels(
            [_COL_LABELS[c] for c in _COLS])
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setSelectionMode(QTableWidget.SingleSelection)
        self._table.setEditTriggers(
            QTableWidget.DoubleClicked | QTableWidget.EditKeyPressed)
        # 列宽
        hh = self._table.horizontalHeader()
        for i, c in enumerate(_COLS):
            hh.resizeSection(i, _COL_WIDTHS[c])
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.Interactive)
        # 行高紧凑
        vh = self._table.verticalHeader()
        vh.setVisible(False)
        vh.setDefaultSectionSize(32)

        v.addWidget(self._table, 1)

        self._widget = w

        if not self.commands:
            self._load_default()
        else:
            self._refresh_table()
        return w

    def _refresh_table(self):
        """根据 self.commands 重建表格行。"""
        if self._table is None:
            return
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QTableWidgetItem, QPushButton, QWidget, QHBoxLayout
        # 同步编辑中的内容到 commands（避免用户正在编辑的格子被覆盖）
        self._sync_from_table()
        self._table.setRowCount(len(self.commands))
        for row, c in enumerate(self.commands):
            # cmd
            it = QTableWidgetItem(c.get('cmd', ''))
            it.setToolTip(c.get('cmd', ''))
            self._table.setItem(row, 0, it)
            # desc
            it = QTableWidgetItem(c.get('desc', ''))
            self._table.setItem(row, 1, it)
            # seq
            it = QTableWidgetItem(str(c.get('seq', 0)))
            it.setTextAlignment(Qt.AlignCenter)
            self._table.setItem(row, 2, it)
            # period_ms
            it = QTableWidgetItem(str(c.get('period_ms', 0)))
            it.setTextAlignment(Qt.AlignCenter)
            self._table.setItem(row, 3, it)
            # op：发送 + 删除 按钮
            op_w = QWidget()
            op_l = QHBoxLayout(op_w)
            op_l.setContentsMargins(2, 2, 2, 2)
            op_l.setSpacing(4)
            btn_send = QPushButton('发送')
            btn_send.setProperty('primary', True)
            btn_send.clicked.connect(lambda _=False, r=row: self._send_row(r))
            btn_del = QPushButton('删除')
            btn_del.clicked.connect(lambda _=False, r=row: self._delete_row(r))
            op_l.addWidget(btn_send)
            op_l.addWidget(btn_del)
            op_l.addStretch(1)
            self._table.setCellWidget(row, 4, op_w)
            self._table.setRowHeight(row, 32)

    def _sync_from_table(self):
        """把表格当前编辑的内容同步回 self.commands（批量重建前调用）。"""
        if self._table is None:
            return
        n = self._table.rowCount()
        for r in range(n):
            if r >= len(self.commands):
                break
            try:
                cmd = self._table.item(r, 0).text() if self._table.item(r, 0) else ''
                desc = self._table.item(r, 1).text() if self._table.item(r, 1) else ''
                seq_text = self._table.item(r, 2).text() if self._table.item(r, 2) else '0'
                per_text = self._table.item(r, 3).text() if self._table.item(r, 3) else '0'
                self.commands[r] = _norm({
                    'cmd': cmd, 'desc': desc,
                    'seq': int(seq_text or 0),
                    'period_ms': int(per_text or 0),
                })
            except Exception:
                pass

    def _notify_change(self):
        if self.on_change is not None:
            try:
                self.on_change(list(self.commands))
            except Exception:
                pass

    # ---- 行操作 ------------------------------------------------------
    def _send_row(self, row: int):
        # 必须先把表格当前编辑的内容同步回 self.commands，否则用户编辑
        # 后点发送读到的是旧值（新增行读到空串被 if not cmd 拦截不发）。
        self._sync_from_table()
        if row >= len(self.commands):
            return
        cmd = self.commands[row].get('cmd', '').strip()
        if not cmd:
            return
        try:
            self.send_callback(cmd)
        except Exception:
            pass

    def _delete_row(self, row: int):
        if row >= len(self.commands):
            return
        del self.commands[row]
        self._refresh_table()
        self._notify_change()

    def _add_row(self):
        self._sync_from_table()
        self.commands.append(_norm({'cmd': '', 'desc': '', 'seq': 0,
                                     'period_ms': 0}))
        self._refresh_table()
        self._notify_change()
        # 聚焦新行第一格进入编辑
        if self._table is not None:
            r = self._table.rowCount() - 1
            if r >= 0:
                it = self._table.item(r, 0)
                if it is not None:
                    self._table.editItem(it)

    def _delete_selected(self):
        if self._table is None:
            return
        rows = sorted({idx.row() for idx in self._table.selectedIndexes()},
                      reverse=True)
        if not rows:
            return
        self._sync_from_table()
        for r in rows:
            if 0 <= r < len(self.commands):
                del self.commands[r]
        self._refresh_table()
        self._notify_change()

    def _clear_all(self):
        self.commands.clear()
        self._refresh_table()
        self._notify_change()

    def _load_default(self):
        """加载默认 AT 命令集（与 tkinter 版一致）。"""
        try:
            from serial_debug.resources import common_at
        except Exception:
            # fallback：硬编码几条
            self.commands = [
                _norm({'cmd': 'AT', 'desc': '测试', 'seq': 1, 'period_ms': 100}),
                _norm({'cmd': 'AT+VER', 'desc': '版本', 'seq': 2, 'period_ms': 200}),
                _norm({'cmd': 'AT+RESET', 'desc': '复位', 'seq': 0, 'period_ms': 0}),
            ]
            self._refresh_table()
            return
        try:
            path = os.path.join(os.path.dirname(common_at.__file__),
                               'common_at.json')
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, list):
                self.commands = [_norm(c) for c in data]
            self._refresh_table()
        except Exception:
            self.commands = []
            self._refresh_table()

    # ---- 导入 / 导出 -------------------------------------------------
    def _import_json(self, parent=None):
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        path, _ = QFileDialog.getOpenFileName(
            parent, '导入 AT 命令 JSON', os.path.expanduser('~'),
            'JSON (*.json);;所有文件 (*)')
        if not path:
            return
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, list):
                raise ValueError('JSON 必须是命令数组')
            self._sync_from_table()
            self.commands = [_norm(c) for c in data]
            self._refresh_table()
            self._notify_change()
        except Exception as e:
            QMessageBox.critical(parent, '导入失败', str(e))

    def _export_json(self, parent=None):
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        path, _ = QFileDialog.getSaveFileName(
            parent, '导出 AT 命令 JSON', os.path.expanduser('~'),
            'JSON (*.json);;所有文件 (*)')
        if not path:
            return
        try:
            self._sync_from_table()
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(self.commands, f, ensure_ascii=False, indent=2)
            QMessageBox.information(parent, '导出完成', path)
        except Exception as e:
            QMessageBox.critical(parent, '导出失败', str(e))

    # ---- 批量执行 ----------------------------------------------------
    def _start_batch(self):
        if self._batch_running:
            return
        self._sync_from_table()
        # 按 seq 升序，seq=0 跳过
        ordered = sorted(
            [c for c in self.commands if c.get('seq', 0) > 0 and c.get('cmd', '').strip()],
            key=lambda c: c.get('seq', 0))
        if not ordered:
            if self._batch_status is not None:
                self._batch_status.setText('无批量命令（seq=0 跳过）')
            return
        self._batch_queue = ordered
        self._batch_idx = 0
        self._batch_running = True
        self._batch_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._update_batch_status(f'批量 {len(ordered)} 条，准备发送')
        self._exec_next()

    def _exec_next(self):
        from PySide6.QtCore import QTimer
        if not self._batch_running:
            return
        if self._batch_idx >= len(self._batch_queue):
            self._stop_batch()
            self._update_batch_status('批量完成')
            return
        c = self._batch_queue[self._batch_idx]
        cmd = c.get('cmd', '').strip()
        try:
            self.send_callback(cmd)
        except Exception:
            pass
        self._batch_idx += 1
        per = c.get('period_ms', 0)
        if self._batch_idx < len(self._batch_queue):
            self._update_batch_status(
                f'{self._batch_idx}/{len(self._batch_queue)} 已发送，等待 {per}ms')
            self._batch_timer = QTimer()
            self._batch_timer.setSingleShot(True)
            self._batch_timer.timeout.connect(self._exec_next)
            self._batch_timer.start(max(per, 1))
        else:
            # 最后一条：等响应后完成（简化：直接完成）
            self._update_batch_status(
                f'{self._batch_idx}/{len(self._batch_queue)} 已发送')
            self._batch_timer = QTimer()
            self._batch_timer.setSingleShot(True)
            self._batch_timer.timeout.connect(self._stop_batch)
            self._batch_timer.start(max(per, 1))

    def _stop_batch(self):
        self._batch_running = False
        if self._batch_timer is not None:
            try:
                self._batch_timer.stop()
            except Exception:
                pass
            self._batch_timer = None
        if self._batch_btn is not None:
            self._batch_btn.setEnabled(True)
        if self._stop_btn is not None:
            self._stop_btn.setEnabled(False)

    def _update_batch_status(self, text: str):
        if self._batch_status is not None:
            self._batch_status.setText(text)

    def _on_close_clicked(self):
        if self.close_callback is not None:
            try:
                self.close_callback()
            except Exception:
                pass

    # ---- 对外 -------------------------------------------------------
    def get_widget(self):
        return self._widget


class AtCmdWindowQt:
    """独立 AT 命令窗口（QDialog 包装 AtCmdPanelQt）。"""

    TITLE = 'AT 命令面板'

    def __init__(self, send_callback, initial_cmds=None, on_change=None):
        self.panel = AtCmdPanelQt(send_callback, initial_cmds, on_change)
        self._dlg = None

    def _build(self, parent=None):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QDialog, QVBoxLayout
        dlg = QDialog(parent)
        dlg.setWindowTitle(self.TITLE)
        dlg.resize(800, 500)
        dlg.setMinimumSize(600, 400)
        v = QVBoxLayout(dlg)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        w = self.panel._build(parent=dlg)
        v.addWidget(w)
        self._dlg = dlg
        return dlg

    def exec(self):
        dlg = self._build()
        return dlg.exec()

    def show(self):
        dlg = self._build()
        dlg.show()
        return dlg
