# 测试

serial_debug smoke 单元测试骨架，覆盖纯 Python 逻辑（config / AT JSON / parser_inference）。
GUI / Qt 测试需 Docker + offscreen，本目录暂不覆盖。

## 跑测试

### 方式 A：宿主机（开发态，仅 smoke）

```bash
cd /home/ninebot/tools/Personal-AI-Zone/Agent-Members/create_tools_set
pip install -e .[dev]        # 或 pip install pytest pytest-cov
python3 -m pytest tests/ -v
```

### 方式 B：Docker 内（CI 态，推荐）

```bash
docker run --rm -v "$PWD:/work" -w /work python:3.11-slim bash -c '
  pip install -e . pytest pytest-cov && pytest tests/ -v --cov=serial_debug
'
```

## 覆盖率

```bash
python3 -m pytest tests/ --cov=serial_debug --cov-report=term-missing --cov-report=html
# HTML 报告在 htmlcov/index.html
```

## 配置

- `pyproject.toml` 的 `[tool.pytest.ini_options]`：testpaths / markers
- `[tool.coverage.run]`：source = src/serial_debug，omit 掉 UI 层（Qt 窗口需 GUI 环境）
- `[tool.coverage.report]`：show_missing + 排除 `if __name__ == "__main__"`

## 测试矩阵

| 文件 | 覆盖目标 | 用例数 |
|---|---|---|
| `test_config.py` | infra/config.py 读写 / 原子写 / 默认值 / 损坏文件兜底 | 7 |
| `test_at_json.py` | resources/common_at.json 加载 + schema + 往返 | 4 |
| `test_parser_inference.py` | qt_parser_inference.py detect_language / printf/brace 解析 / extract / generate_parse_line | 14 |

## 已知限制

- 仅 smoke 层，未覆盖串口 reader / writer、Qt 窗口构造、CSV 导出、AT 批量执行调度
- 覆盖率初值偏低（约 < 10%），下一步按 COMMERCIAL_BENCHMARK.md "测试要求" 节扩充到解析器 / 串口封装 / CSV 导出

## 复审

代码质量复审由 **Agent-Senior-TestDev** 负责。本目录测试通过仅代表 smoke 层绿灯，不替代复审。
