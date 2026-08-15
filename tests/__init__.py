"""Smoke tests for serial_debug — config 读写、AT JSON 加载、parser 推断。

跑测试：
    cd /home/ninebot/tools/Personal-AI-Zone/Agent-Members/create_tools_set
    python3 -m pytest tests/ -v

跑覆盖率：
    python3 -m pytest tests/ --cov=serial_debug --cov-report=term-missing

PySide6/pyqtgraph 不在 smoke 测试依赖里（GUI 测试需 Docker + offscreen），
本文件只覆盖纯 Python 逻辑：config、JSON 资源、parser_inference。
"""
