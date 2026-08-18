# 串口调试助手 (Serial Debug)

商用级串口调试与日志查看工具，面向嵌入式开发与硬件调试场景。

## 功能

- **实时收发**：CH343 / CH340 / CDC-ACM 全支持，VMIN=1 VTIME=0 阻塞读模式与 minicom 一致
- **AT 命令管理**：表格化命令列表 + 行内编辑 + 批量周期执行 + JSON 导入导出
- **日志存储**：独立写入线程消费队列，磁盘 I/O 不阻塞收发
- **日志查看器**：多文件 Tab + 筛选面板 + 双向定位 + Ctrl+F 文件内搜索 + Ctrl+Shift+F 全局搜索
- **实时数据监视**：变量级 regex 提取 + 曲线绘制 + 多变量同图 + 数据模式
- **VSCode Dark+ / Breakers Light 主题**：跟随系统 + 手动覆盖
- **桌面集成**：文件管理器右键 `.txt/.log` → 打开方式 → 日志查看器

## 目录结构

```
serial_debug/
├── src/serial_debug/         # Python 包（src layout）
│   ├── app.py                # 主入口（Qt GUI + CLI: --view <file...>）
│   ├── core/                 # 串口 I/O 与日志写入
│   ├── ui/                   # 主窗口、AT 命令、日志查看、监视窗口
│   ├── widgets/              # 通用 UI 组件（AutoCombobox 等）
│   ├── infra/                # 配置、主题
│   └── resources/            # 图标 + 预置 AT 命令 JSON
├── tools/                    # 开发辅助：图标生成、mock 设备、自检
├── packaging/               # PyInstaller spec + 跨平台 build/install 脚本
├── desktop/                  # .desktop 桌面入口（Linux）
├── scripts/                  # 系统脚本（CH343 驱动绑定）
├── docs/                     # 项目文档
├── tests/                    # 测试占位
├── pyproject.toml            # 包元数据 + 入口点
├── requirements.txt
└── run.sh                    # 开发模式一键启动（虚拟串口 + mock + GUI）
```

## 开发运行

```bash
# 源码模式（无需安装）
cd src && PYTHONPATH=. python3 -m serial_debug

# 一键端到端测试（虚拟串口 + mock 设备 + GUI）
bash run.sh

# editable 安装
pip install -e .
serial_debug
```

## 打包与安装

```bash
# Linux
bash packaging/build_linux.sh            # 生成 dist/serial_debug-linux-*.tar.gz
bash packaging/install_linux.sh           # 安装到 ~/.local/share/serial_debug
bash packaging/install_linux.sh --desktop # 同时创建桌面快捷方式

# Windows
package\build_win.bat
packaging\install_win.bat

# macOS
bash packaging/build_mac.sh
bash packaging/install_mac.sh
```

## 系统要求

- Python 3.9+
- pyserial >= 3.5
- PySide6 >= 6.5（Qt GUI）
- pyqtgraph >= 0.14（实时曲线）
- numpy（LTTB 降采样）
- PyOpenGL（可选，开启 OpenGL 加速曲线渲染）
- Linux 桌面集成可选：`update-desktop-database` / `update-mime-database`

## License

MIT
