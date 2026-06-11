<!-- Language switch -->
**简体中文** · [English](README.en.md)

# SanityProxy

一个跑在本地的反向代理：敏感数据发往 Claude（或任何 Anthropic 兼容的大模型 API）之前先就地脱敏，响应回来时再自动还原。专为法律文书、病历这类「原始 PII 绝不能离开本机」的场景而设计。

```
Claude Code ──► SanityProxy (localhost:8080) ──► api.anthropic.com
                     │ 脱敏 / 还原                      │ 只看得到标签
                     └─── 原始 PII 全程不出本机 ────────┘
```

## 工作原理

1. Claude Code 发出一条带有敏感文本的 API 请求；
2. SanityProxy 拦下请求，把其中的 PII 替换成 `[[SANITY_类别_编号]]` 形式的占位标签；
3. 脱敏后的请求再转发给上游大模型；
4. 大模型拿到的是标签，也用标签来作答；
5. 响应回到 Claude Code 之前，SanityProxy 把所有标签还原成原始值。

`POST /v1/messages`（含流式）和 `POST /v1/messages/count_tokens` 都会脱敏——这两个端点带的对话内容是一样的，所以连 token 计数请求也不会把原文漏出去。

整个过程对 Claude Code 完全透明：它收到的是一段完整、自然的回复，根本察觉不到代理的存在。

### 兼容「扩展思考」

模型自己产出的 `thinking` 块和它的加密 `signature`，在脱敏、自检、补脱、还原这一整条链路里**原样保留、逐字节往返**，从不被改动，因此上游验签永远能过。（响应里的 `thinking` 内容**特意不做标签还原**，让它在多轮对话中始终保持标签形态、签名持续有效——你会在思考面板里看到标签，这是预期行为，不是 bug。）流式还原是按 SSE 事件逐条处理的：只还原回答正文和工具入参，能把被拆进多个事件里的标签重新拼回来，并顺手记录 token 用量。

## 内置规则（侧重法律文书）

| 规则 | 类别 | 示例 |
|------|------|------|
| 居民身份证 | 个人身份 | `110101199001011234` |
| 护照号 | 个人身份 | `E12345678` |
| 姓名 | 个人身份 | 「被告人 / 原告人 / 委托人」等称谓后跟的姓名 |
| 手机号 | 联系方式 | `13812345678`（保留前 3 位） |
| 固定电话 | 联系方式 | `010-12345678` |
| 电子邮箱 | 联系方式 | `user@example.com` |
| 银行卡号 | 金融信息 | 16–19 位数字 |
| 统一社会信用代码 | 机构信息 | 18 位字母数字 |
| 案件编号 | 司法信息 | `（2024）京民初第1234号` |
| 车牌号 | 其他 | `京A12345` |

也可以在 Web 面板或通过 API 添加自定义规则。

## 快速开始

**环境要求：** Python 3.9+

```bash
# 1. 安装依赖
cd proxy
pip install -r requirements.txt

# 2. 启动代理
python main.py
# → http://127.0.0.1:8080

# 3. 打开监控面板
open http://localhost:8080/dashboard

# 4. 让 Claude Code 走代理
ANTHROPIC_BASE_URL=http://localhost:8080 claude
```

## 验证是否生效

```bash
cd proxy
python -m pytest tests/ -v
```

测试需全部通过，覆盖四个方面：
- **出站脱敏**——原始 PII 绝不会到达大模型；system prompt 和 `count_tokens` 也一并脱敏；
- **入站还原**——标签在送回调用方之前被还原；透明（直通）模式正常工作；
- **零泄漏自检（fail-closed）**——`tool_result`/`tool_use` 字段同样脱敏，`messages` 之外的漏网 PII 也会被兜底拦下，支持「拦截 / 补脱 / 仅告警」三档策略；
- **思考签名与流式**——`thinking`/`signature` 逐字节不变；流式只还原正文、不动思考内容，能拼回被拆开的标签并记录用量；上游返回非 200 时如实透传状态码。

## 监控面板

`http://localhost:8080/dashboard` 提供：
- **实时请求日志**——定高、可滚动、表头吸顶，标题旁实时显示条数；流式请求的 token 现已能正常统计；
- **出站审计**——保存实际发往上游内容（脱敏后）的快照，保留条数可选最近 20 / 100 / 200 / 500 / 全部；
- **出站自检策略**——补脱后放行（默认）/ 拦截（fail-closed）/ 仅告警，随时可切；
- **规则管理**——启停规则、添加自定义模式、导入导出 JSON；
- **规则测试**——贴一段文本，立刻预览脱敏效果；
- **模式切换**——脱敏与透明模式一键互换。

## 配置

编辑 `proxy/config.py`：

```python
UPSTREAM_URL = "https://api.anthropic.com"  # 也可指向其他兼容端点
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
├── static/           # Web 面板（HTML / JS / CSS，免构建）
└── tests/            # 自动化测试（出站、入站、fail-closed、思考/流式）
```

## 资料文件怎么放

把敏感原文（法律文书、病历、合同等）放进项目让 Claude Code 阅读时，要把它们看作「需要被保护的对象」本身——**千万别提交进 git**。`.gitignore` 已经预先忽略了 `materials/`（以及 `workspace/`、`data/`、`*.private/`）：

```
sanity_claude/
├── proxy/          # 工具代码（纳入版本管理）
├── materials/      # 原始敏感资料（已 gitignore，绝不提交）
│   └── 2026-案件A/...
└── workspace/      # Claude 产出的分析 / 草稿（已 gitignore）
```

读取本地文件本身不会把任何内容发出去；只有当文件内容被塞进请求时才会上云——**而且必须是在脱敏模式下**，代理会把其中的 PII 换成标签（具体发出去什么，可在面板「出站审计」里核对）。遇到内置规则覆盖不到的专有标识（工号、内部单号等），先加一条自定义规则再喂给模型。`materials/` 不进 git，记得自己做好**加密备份**。完整约定见 AGENTS.md 的「资料文件管理」一节。

## 安全说明

- 代理默认只监听 `127.0.0.1`，不对外网暴露；
- `sanity.db` 只存规则和设置（不含任何原文），仅在本地，也不纳入版本管理；
- 值↔标签的映射**按请求隔离**：每个请求现建、请求一结束就销毁——原始 PII 既不会在进程里长期驻留，也不会落盘；
- 描述「原始字节」的那几个响应头（`content-encoding`/`content-length`/`transfer-encoding`）会被剥掉，因为响应体已经被代理解压并改写过了；
- 你的原始资料都放在被 gitignore 的目录里（见〔资料文件怎么放〕），永远不会被提交。
