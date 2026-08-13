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


def _smoke_parser_config_dialog() -> list[str]:
    """解析器配置对话框：构造 + 切 mode + 点测试按钮不崩。"""
    errors = []
    try:
        from PySide6.QtWidgets import QApplication
        if not QApplication.instance():
            QApplication([])
        from serial_debug.ui.qt_monitor_window import (
            QtMonitorWindow, _QtParserConfigDialog, _CUSTOM_PARSER_STATE,
            _PARSER_TEMPLATE_CODE,
        )
        mon = QtMonitorWindow()
        win = mon._create()
        win.show()
        QApplication.processEvents()
        # 构造 dialog
        dlg = _QtParserConfigDialog(win, callback_save=mon._save_custom_parser_state)
        built = dlg._build()
        # 切 mode：custom
        dlg._mode_custom.setChecked(True)
        # 代码区用模板 + 样本日志塞几行 motor 日志
        dlg._code_edit.setPlainText(_PARSER_TEMPLATE_CODE)
        dlg._sample_edit.setPlainText(
            '1783996615491 I model 3wd1t_1 1200 48.0 1.0 2.0 3.0\n'
            '1783996615492 I model 3wd1t_1 1205 48.2 1.1 2.1 3.1\n'
            '1783996615493 I BMS [48.1 1.2 0.5 25.0]\n')
        # 点测试按钮（不进 exec 事件循环，直接调槽）
        dlg._on_test()
        QApplication.processEvents()
        built.close()
        win.close()
    except Exception as e:
        import traceback
        errors.append(f'解析器 dialog: {traceback.format_exc().splitlines()[-1]}')
    return errors


def _smoke_validation_warning() -> list[str]:
    """格式校验：阈值改为 ≥ N 条成功解析。喂 200 行无关文本 → 0 条 → 弹警告。"""
    errors = []
    try:
        from PySide6.QtWidgets import QApplication
        if not QApplication.instance():
            QApplication([])
        from serial_debug.ui.qt_monitor_window import (
            QtMonitorWindow, _CUSTOM_PARSER_STATE,
        )
        mon = QtMonitorWindow()
        win = mon._create()
        win.show()
        QApplication.processEvents()
        # 启用校验 + 阈值 10 条 + 200 行
        _CUSTOM_PARSER_STATE['validation'] = {
            'enabled': True, 'min_parsed_count': 10, 'sample_lines': 200,
        }
        # 喂 200 行无关文本（无任何 tag）→ 0 条成功 → 报警
        bad_lines = ['this is not a valid log line'] * 200
        mon._validate_log_format(bad_lines, 'smoke-test')
        QApplication.processEvents()
        # 同样喂 motor 日志 → 200 条成功 → 通过
        good_lines = [
            '1783996615491 I model 3wd1t_1 1200 48.0 1.0 2.0 3.0'] * 200
        mon._validate_log_format(good_lines, 'smoke-test-good')
        QApplication.processEvents()
        win.close()
    except Exception as e:
        import traceback
        errors.append(f'校验: {traceback.format_exc().splitlines()[-1]}')
    return errors


def _smoke_parser_inference() -> list[str]:
    """多语言日志格式推导：贴 C printf 源码 → detect/extract/generate
    → 执行生成的 parse_line → 喂日志行能产出变量。"""
    errors = []
    try:
        from serial_debug.ui.qt_parser_inference import (
            detect_language, extract_print_statements, generate_parse_line)
        # 7 语言样例
        cases = [
            ('c', 'printf("ts=%ld rpm=%d vbus=%.2f\\n", ts, rpm, vbus);'),
            ('cpp', 'std::cout << "ts=" << ts << " rpm=" << rpm << "\\n";'),
            ('python', 'print(f"ts={ts} rpm={rpm} vbus={vbus}")'),
            ('java', 'System.out.printf("ts=%d rpm=%d%n", ts, rpm);'),
            ('rust', 'println!("ts={} rpm={}", ts, rpm);'),
            ('js', 'console.log(`ts=${ts} rpm=${rpm}`);'),
        ]
        for lang_expected, src in cases:
            lang = detect_language(src)
            if lang != lang_expected:
                errors.append(
                    f'detect_language 误判：源 {src!r} 期望 {lang_expected}'
                    f' 实际 {lang}')
                continue
            stmts = extract_print_statements(src, lang)
            if not stmts:
                errors.append(
                    f'extract 返回空（语言={lang}）：{src!r}')
                continue
            gen = generate_parse_line(stmts)
            ns: dict = {}
            exec(compile(gen, '<inferred>', 'exec'), ns)
            if not callable(ns.get('parse_line')):
                errors.append(f'生成代码未定义 parse_line（语言={lang}）')
                continue
            line = ('1783996615491 ts=1783996615491 rpm=1200 vbus=48.0'
                    if lang != 'java'
                    else '1783996615491 ts=1783996615491 rpm=1200')
            got = list(ns['parse_line'](line))
            if not got:
                errors.append(
                    f'parse_line 未产出变量（语言={lang}）')
                continue
            # 检查至少有 rpm
            names = {item[1] for item in got if len(item) == 3}
            if 'rpm' not in names:
                errors.append(
                    f'parse_line 未产出 rpm（语言={lang}，names={names}）')
        # HTML 不应推导
        lang = detect_language('<html><body>foo</body></html>')
        if lang != 'html':
            errors.append(f'detect_language HTML 误判：{lang}')

        # LOG_TS 自定义日志宏推导（用户场景）
        bms_src = (
            'LOG_TS(g_bms_log_level, "BMS", "[%d %d %d][%d %d %d %d]'
            '[%d %d %d][%d %d][%d %d %d %d][%d %d]\\n", \\\n'
            '  BMS_Data.state, BMS_Data.charge_detect, BMS_Data.charge_sw, \\\n'
            '  BMS_Data.bat_cap_cc, BMS_Data.bat_cap_vol);')
        lang = detect_language(bms_src)
        if lang != 'c':
            errors.append(f'LOG_TS 源 detect_language 期望 c，实际 {lang}')
        stmts = extract_print_statements(bms_src, 'c')
        if not stmts:
            errors.append('LOG_TS extract 返回空')
        else:
            gen = generate_parse_line(stmts)
            ns = {}
            exec(compile(gen, '<bms>', 'exec'), ns)
            log = ('1786530975976 F BMS [1 1 0][0 0 23940 0]'
                   '[0 277 421][0 0][1000 943 1000 1000][4248 4230]')
            got = list(ns['parse_line'](log))
            if not got:
                errors.append('LOG_TS 推导后 parse_line 未产出变量')
            else:
                names = {item[1] for item in got if len(item) == 3}
                if 'BMS_Data.bat_cap_cc' not in names:
                    errors.append(
                        f'LOG_TS 推导未产出 BMS_Data.bat_cap_cc，'
                        f'实际产出 names={names}')
                if 'BMS_Data.bat_cap_vol' not in names:
                    errors.append(
                        f'LOG_TS 推导未产出 BMS_Data.bat_cap_vol')
                # level 变量 g_bms_log_level 不应被产出（'F' 不能 float）
                if 'g_bms_log_level' in names:
                    errors.append(
                        'LOG_TS 推导不应产出 level 变量 g_bms_log_level')
    except Exception as e:
        import traceback
        errors.append(f'推导: {traceback.format_exc().splitlines()[-1]}')
    return errors


def _smoke_validation_min_count() -> list[str]:
    """新阈值：5 条成功 < 10 → 报警；50 条成功 ≥ 10 → 通过。"""
    errors = []
    try:
        from PySide6.QtWidgets import QApplication
        if not QApplication.instance():
            QApplication([])
        from serial_debug.ui.qt_monitor_window import (
            QtMonitorWindow, _CUSTOM_PARSER_STATE,
        )
        mon = QtMonitorWindow()
        win = mon._create()
        win.show()
        QApplication.processEvents()
        _CUSTOM_PARSER_STATE['validation'] = {
            'enabled': True, 'min_parsed_count': 10, 'sample_lines': 200,
        }
        # 200 行中只有 5 条 motor → 5 < 10 → 报警
        mixed_5 = (
            ['this is not a valid log line'] * 195 +
            ['1783996615491 I model 3wd1t_1 1200 48.0 1.0 2.0 3.0'] * 5)
        mon._validate_log_format(mixed_5, 'smoke-5')
        QApplication.processEvents()
        # 200 行中 50 条 motor → 50 ≥ 10 → 通过
        mixed_50 = (
            ['this is not a valid log line'] * 150 +
            ['1783996615491 I model 3wd1t_1 1200 48.0 1.0 2.0 3.0'] * 50)
        mon._validate_log_format(mixed_50, 'smoke-50')
        QApplication.processEvents()
        win.close()
    except Exception as e:
        import traceback
        errors.append(f'阈值: {traceback.format_exc().splitlines()[-1]}')
    return errors


def _smoke_legend_checkbox() -> list[str]:
    """图例 checkbox：默认勾选有曲线；取消勾选后曲线移除；持久化 var_visibility。"""
    errors = []
    try:
        from PySide6.QtWidgets import QApplication, QCheckBox
        if not QApplication.instance():
            QApplication([])
        from serial_debug.ui.qt_monitor_window import (
            QtMonitorWindow, _normalize_plot,
        )
        mon = QtMonitorWindow()
        win = mon._create()
        win.show()
        QApplication.processEvents()
        # 准备 mock data：两个变量 motor.rpm / motor.vbus
        mon._data = {
            'motor.rpm': [(1700000000.0 + i, float(i)) for i in range(5)],
            'motor.vbus': [(1700000000.0 + i, 48.0 + 0.1 * i) for i in range(5)],
        }
        mon._register_var('motor.rpm')
        mon._register_var('motor.vbus')
        # 添加一个 plot 含两个变量
        p = _normalize_plot({
            'title': 't', 'vars': ['motor.rpm', 'motor.vbus'],
        })
        mon._visible_plots = [p]
        mon._plots = [p]
        mon._rebuild_plots_ui()
        mon._refresh_plot()
        QApplication.processEvents()
        # 检查：两个变量都有曲线
        if len(mon._curves[0]) != 2:
            errors.append(
                f'默认勾选时曲线数 != 2: {len(mon._curves[0])}')
        # 检查：图例有两个 checkbox
        header = mon._plot_headers[0]
        if len(header['legend_items']) != 2:
            errors.append(
                f'图例 chip 数 != 2: {len(header["legend_items"])}')
        else:
            # 检查：两个 checkbox 默认勾选
            for name in ('motor.rpm', 'motor.vbus'):
                chip = header['legend_items'].get(name)
                if chip is None:
                    errors.append(f'图例缺 {name}')
                    continue
                cb = chip.findChild(QCheckBox)
                if cb is None:
                    errors.append(f'{name} 的 chip 里没找到 QCheckBox')
                    continue
                if not cb.isChecked():
                    errors.append(f'{name} 的 checkbox 默认未勾选')
        # 模拟取消勾选 motor.rpm：直接改 plot 配置 + 调 _refresh_single_plot
        p['var_visibility'] = {'motor.rpm': False}
        mon._refresh_single_plot(0)
        QApplication.processEvents()
        # 检查：motor.rpm 的曲线已移除，motor.vbus 还在
        if 'motor.rpm' in mon._curves[0]:
            errors.append('取消勾选后 motor.rpm 曲线仍在')
        if 'motor.vbus' not in mon._curves[0]:
            errors.append('取消勾选 motor.rpm 后 motor.vbus 也消失了')
        # 检查：图例两个 chip 都还在（chip 不随曲线消失）
        if len(header['legend_items']) != 2:
            errors.append(
                f'取消勾选后图例 chip 数 != 2: {len(header["legend_items"])}')
        # 检查：var_visibility 序列化保留
        norm = _normalize_plot(p)
        if norm.get('var_visibility', {}).get('motor.rpm') is not False:
            errors.append('_normalize_plot 未保留 var_visibility.motor.rpm=False')
        win.close()
    except Exception as e:
        import traceback
        errors.append(f'图例 checkbox: {traceback.format_exc().splitlines()[-1]}')
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

    print('==> 5. 解析器配置对话框')
    errs = _smoke_parser_config_dialog()
    if errs:
        for e in errs:
            print(f'    FAIL: {e}')
        return 1
    print('    OK')

    print('==> 6. 格式校验')
    errs = _smoke_validation_warning()
    if errs:
        for e in errs:
            print(f'    FAIL: {e}')
        return 1
    print('    OK')

    print('==> 7. 多语言日志格式推导')
    errs = _smoke_parser_inference()
    if errs:
        for e in errs:
            print(f'    FAIL: {e}')
        return 1
    print('    OK')

    print('==> 8. 校验阈值（≥ N 条）')
    errs = _smoke_validation_min_count()
    if errs:
        for e in errs:
            print(f'    FAIL: {e}')
        return 1
    print('    OK')

    print('==> 9. 图例 checkbox 可见性')
    errs = _smoke_legend_checkbox()
    if errs:
        for e in errs:
            print(f'    FAIL: {e}')
        return 1
    print('    OK')

    print('\n=== smoke test PASS ===')
    return 0


if __name__ == '__main__':
    sys.exit(run_all())
