# 商业增强版（闭源）

> 本目录存放 serial_debug 的商业增强版代码与文档。
> 与仓库根目录的 MIT 开源核心隔离，受私有协议约束，详见下方条款。

## 1. 双轨策略

| 版本 | 位置 | 协议 | 目标用户 |
|---|---|---|---|
| **开源核心版** | 仓库根目录（`src/`） | [MIT](../LICENSE) | 个人开发者、爱好者、教学 |
| **商业增强版** | `commercial/`（本目录） | 私有协议（见下） | 企业、量产调试、商业化产品 |

开源核心版永远是 MIT，可独立编译运行；商业增强版在开源核心之上叠加扩展，必须持有合法 License 才能编译为闭源发行包。

## 2. v1.0.0 状态：占位骨架

v1.0.0 阶段 `commercial/` 仅为目录骨架 + 协议占位，**尚未实现任何闭源模块**。规划如下：

```
commercial/
├── COMMERCIAL.md               # 本文件（协议 + 路线图）
├── LICENSE-COMMERCIAL.md       # 私有许可全文（占位）
├── README.md                   # 商业版用户文档（占位）
├── licensing/                  # License 校验模块
│   └── license_guard.py        # 机器指纹绑定 + 离线激活（计划）
├── at_templates/               # 高级 AT 命令模板库（按厂商分组）
│   └── esp_4g_ble.json         # 占位
├── log_analysis/               # 增强日志分析
│   ├── boolean_search.py       # AND/OR/NOT 组合搜索
│   └── session_recorder.py     # SQLite 会话录制 + 回放
├── remote_api/                 # 远程控制
│   ├── http_api.py             # TCP/HTTP 控制接口
│   └── mcp_bridge.py           # MCP bridge 让 AI Agent 控制工具
└── plugins/                    # 插件机制
    └── user_script_host.py     # 用户 .py/.lua 脚本加载
```

## 3. 路线图（按 docs/COMMERCIAL_BENCHMARK.md 第三波）

| 阶段 | 模块 | 优先级 | 计划版本 |
|---|---|---|---|
| 1 | License 校验 + 机器绑定 + 离线激活 | P0 | v1.1.0 |
| 2 | 高级 AT 模板库（ESP/4G/蓝牙按厂商分组） | P1 | v1.2.0 |
| 3 | 增强日志分析（布尔组合搜索 + SQLite 录制 + 回放） | P1 | v1.3.0 |
| 4 | TCP/HTTP API 远程控制 | P1 | v1.4.0 |
| 5 | MCP bridge（AI Agent 接管） | P2 | v1.5.0 |
| 6 | 插件机制（用户脚本） | P2 | v1.6.0 |

## 4. 私有许可（摘要）

**完整文本见 [LICENSE-COMMERCIAL.md](LICENSE-COMMERCIAL.md)。摘要：**

1. 授权主体：经 Ninebot 签发的企业 License（含机器绑定指纹）。
2. 授权范围：内部调试、量产工具链集成、二次开发集成到自有产品。
3. 禁止：再分发 `commercial/` 源码、向第三方转让 License、绕过机器绑定。
4. 期限：永久（除非违反条款）。
5. 担保：按"现状"提供，详见全文免责条款。
6. 升级：含 12 个月免费维护期，过期后可继续使用旧版本但不获得新版本。

## 5. 编译发行

商业增强版发行包独立编译，**不混入开源版 PyInstaller spec**：

```bash
# 开源版（MIT，公开下载）
bash packaging/build_linux.sh

# 商业版（私有，License 持有者专发）
python3 -m commercial.build.commercial_build --license <path-to-license> --platform linux
```

商业版发行包会嵌入 License 校验模块，启动时验证机器指纹。

## 6. 联系

商业授权、定制开发、批量 License：联系 Ninebot 嵌入式工具团队。
