<!-- Language switch -->
[English](README.md) · **简体中文**

# SanityProxy

一个完全本地运行的反向代理：在敏感数据到达 Claude（或任何 Anthropic 兼容的大模型 API）之前先脱敏，再在响应里还原。专为法律文书、病历，以及任何「原始 PII 绝不能离开本机」的场景设计。

```
Claude Code ──► SanityProxy (localhost:8080) ──► api.anthropic.com
                     │ 脱敏 / 还原                      │ 仅接触标签
                     └─── 原始 PII 全程不出本机 ────────┘
```

## 工作原理

1. Claude Code 发出包含敏感文本的 API 请求
2. SanityProxy 拦截请求，把 PII 替换为 `[[SANITY_CATEGORY_NNN]]` 占位符
3. 脱敏后的请求转发给上游大模型
4. 大模型用同样的占位符作答
5. SanityProxy 在返回 Claude Code 之前，把所有标签还原为原始值

`POST /v1/messages`（含流式）与 `POST /v1/messages/count_tokens` 都会脱敏——两者携带同样的对话内容，因此 token 计数请求也不会泄漏原始 PII。

Claude Code 收到的是一段完整、自然的响应——完全感知不到代理的存在。

### 兼容扩展思考（extended thinking）

模型自有的产物——`thinking` 块及其加密 `signature`——在整条链路（脱敏 / 自检 / 补脱 / 还原）中**逐字节往返**，绝不被改写，因此上游验签恒通过。（响应里的 `thinking` 内容**刻意不做标签还原**，让它在整段会话里保持标签态、签名始终有效——你会在思考面板看到标签，这是有意为之。）流式还原是「按 SSE 事件」感知的：只还原回答正文与工具入参，能拼回被流式事件劈开的标签，并记录 token 用量。

## 内置规则（面向法律文书）

| 规则 | 分类 | 示例 |
|------|------|------|
| 居民身份证 | 个人身份 | `110101199001011234` |
| 护照号 | 个人身份 | `E12345678` |
| 姓名 | 个人身份 | 被告人 / 原告人 / 委托人 等后面的姓名 |
| 手机号 | 联系方式 | `13812345678`（保留前 3 位） |
| 固定电话 | 联系方式 | `010-12345678` |
| 电子邮箱 | 联系方式 | `user@example.com` |
| 银行卡号 | 金融信息 | 16–19 位数字 |
| 统一社会信用代码 | 机构信息 | 18 位字母数字 |
| 案件编号 | 司法信息 | `（2024）京民初第1234号` |
| 车牌号 | 其他 | `京A12345` |

可在 Web 面板或 API 中添加自定义规则。

## 快速开始

**环境要求：** Python 3.9+

```bash
# 1. 安装依赖
cd proxy
pip install -r requirements.txt

# 2. 启动代理
python main.py
# → http://127.0.0.1:8080

# 3. 打开面板
open http://localhost:8080/dashboard

# 4. 让 Claude Code 走代理
ANTHROPIC_BASE_URL=http://localhost:8080 claude
```

## 验证是否正常工作

```bash
cd proxy
python -m pytest tests/ -v
```

全部用例须通过，覆盖四个方面：
- **出站脱敏** —— 原始 PII 绝不到达大模型；system prompt 与 `count_tokens` 同样脱敏
- **入站还原** —— 标签在到达调用方前被还原；透明（直通）模式正常
- **零泄漏自检（fail-closed）** —— `tool_result`/`tool_use` 字段也脱敏；`messages` 之外的漏网 PII 被兜底；支持 block/remask/off 策略
- **思考签名与流式** —— `thinking`/`signature` 逐字节不变；流式还原正文但不还原 thinking、能拼回被劈开的标签、记录用量；上游非 200 回传真实状态码

## 监控面板

`http://localhost:8080/dashboard` 提供：
- 实时请求日志 —— 定高可滚动、表头吸顶、带实时条数标签的面板（流式请求的 token 现已正常显示）
- 出站审计 —— 实际发往上游内容（脱敏后）的快照；保留最近 20 / 100 / 200 / 500 / 全部
- 出站自检策略切换 —— 补脱后放行（默认）/ 拦截（fail-closed）/ 仅告警
- 规则管理 —— 启用/停用规则、添加自定义模式、导入/导出 JSON
- 规则测试 —— 粘贴文本即时预览脱敏结果
- 模式切换 —— 在脱敏与透明模式之间切换

## 配置

编辑 `proxy/config.py`：

```python
UPSTREAM_URL = "https://api.anthropic.com"  # 或任意兼容端点
LISTEN_HOST  = "127.0.0.1"
LISTEN_PORT  = 8080
MODE         = "desensitize"                # 或 "transparent"
```

## 项目结构

```
proxy/
├── main.py           # 启动入口
├── server.py         # FastAPI 路由 + 代理核心
├── desensitizer.py   # 脱敏 / 还原引擎（核心逻辑）
├── rules.py          # 内置规则定义
├── storage.py        # SQLite 规则存储 + 内存日志
├── models.py         # Pydantic 数据模型
├── config.py         # 配置
├── static/           # Web 面板（HTML / JS / CSS，无需构建）
└── tests/            # 自动化测试（出站、入站、fail-closed、思考/流式）
```

## 资料文件管理

当你把敏感原文（法律文书、记录、合同）放进项目供 Claude Code 阅读时，要把它们当作「被保护对象」本身——**绝不入库提交**。`.gitignore` 已预留 `materials/`（以及 `workspace/`、`data/`、`*.private/`）：

```
sanity_claude/
├── proxy/          # 工具代码（入库）
├── materials/      # 原始敏感资料（已 gitignore，绝不提交）
│   └── 2026-案件A/...
└── workspace/      # Claude 生成的分析/草稿（已 gitignore）
```

读取本地文件本身不会外发；内容只有在被放进请求时才会离开本机——**且必须处于脱敏模式**，代理会把其中 PII 换成标签（可在面板「出站审计」核对）。对超出内置规则的专有标识（员工号、内部单号等），先添加自定义规则再喂给模型。请自行为 `materials/` 做**加密备份**——它永远不在 git 里。完整约定见 AGENTS.md →「资料文件管理」。

## 安全说明

- 代理默认绑定 `127.0.0.1`，不暴露到网络
- `sanity.db` 只存规则与设置（不存原文）；仅本地、且不入库
- 值↔标签映射是**每请求隔离**的：每个请求新建、请求结束即回收——原始 PII 既不进程内常驻，也不落盘
- 描述「原始字节」的响应头（`content-encoding` / `content-length` / `transfer-encoding`）会被剥除，因为代理已解压并改写了响应体
- 你的原始资料存放在 gitignore 的目录里（见〔资料文件管理〕），永不提交
