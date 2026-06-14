# SanityProxy — Agent 使用手册

本项目包含 **SanityProxy**，一个完全本地运行的隐私脱敏反向代理，专为法律文书等高隐私场景设计。它拦截 Claude Code 发往 Anthropic 的 API 请求，在本地完成脱敏后再转发；云端模型的响应返回时自动还原，Claude Code 侧无感知。

```
Claude Code ──► SanityProxy (localhost:8080) ──► api.anthropic.com
                     │ 脱敏/还原                        │ 仅接触标签化数据
                     └─── 原始 PII 全程不出本机 ──────────┘
```

---

## 在开始之前：需要向用户确认的事项

进入项目目录后，**在启动代理或修改任何配置之前**，请先与用户确认以下内容：

1. **端口冲突**：代理默认监听 `localhost:8080`。如果该端口已被占用，需修改 `proxy/config.py` 中的 `LISTEN_PORT`，并同步更新 `ANTHROPIC_BASE_URL`。

2. **已有的 `ANTHROPIC_BASE_URL` 设置**：如果用户的 shell 或 Claude Code 配置中已有该环境变量，启动代理后会覆盖原来的设置。确认用户了解这一变化。

3. **脱敏规则是否满足需求**：内置规则覆盖身份证、护照、姓名、手机号、固话、邮箱、银行卡、统一社会信用代码、案件编号、车牌号，共 10 类。如果业务有额外需求（如合同编号、员工工号等），需在 Web 面板中添加自定义规则。

4. **代理关闭后的状态**：代理停止时，`ANTHROPIC_BASE_URL` 如果仍指向 `localhost:8080`，Claude Code 将无法连接。提醒用户停止代理后还原该变量，或始终通过代理使用。

5. **上游选择（原生 Anthropic / 第三方）**：**先执行下面〔首次配置：扫描环境、自动选择上游〕**——扫描环境变量判定该走原生 Anthropic 还是 DeepSeek / GLM 等第三方，给出默认配置并与用户确认。

---

## 首次配置：扫描环境、自动选择上游（原生 Anthropic / 第三方）

当用户要求「配置 / 接入 DeepSeek / 接入 GLM / 帮我跑起来」时，Agent **第一步先扫描当前环境变量**，据此判定走原生还是第三方上游，给出建议并**默认配置好**——全程只读环境变量的「是否设置」，**绝不打印 key 的值**。

### 第一步：扫描环境（不回显密钥值）

```bash
# 密钥类：只报「是否设置」，不回显值
for v in ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN DEEPSEEK_API_KEY GLM_API_KEY; do
  eval "val=\${$v}"; [ -n "$val" ] && echo "$v = ✅ 已设置" || echo "$v = ⬜ 未设置"
done
# 非密钥：可直接显示当前值
for v in ANTHROPIC_BASE_URL ANTHROPIC_MODEL DEEPSEEK_BASE_URL GLM_BASE_URL DEFAULT_UPSTREAM; do
  eval "val=\${$v}"; echo "$v = ${val:-(未设置)}"
done
```

> 这些 key 是给**运行 `python main.py` 的那个进程**用的（代理用 `os.getenv` 读上游的 `token_env`）。务必确认 key 在**启动代理的 shell** 里可见——写进 `~/.zshrc` / `~/.bashrc` 后 `source`，或在启动命令前 `export`。代理**不从 `sanity.db` 读 key、也绝不存 key**。

### 第二步：按扫描结果判定上游

| 扫描结果 | 判定 | 默认动作 |
|----------|------|----------|
| 仅 `ANTHROPIC_*`，或都没有（用 Claude 订阅 OAuth 登录） | **原生 Anthropic** | 默认上游 `anthropic`，无需改动；鉴权透传客户端原头 |
| 设了 `DEEPSEEK_API_KEY` | **DeepSeek** | 把模型设为 `deepseek-*`（如 `deepseek-v4-flash`）→ 命中内置 `deepseek*` 路由 |
| 设了 `GLM_API_KEY` | **GLM / 智谱** | 把模型设为 `glm-*`（如 `glm-4.6`）→ 命中内置 `glm*` 路由 |
| 设了多个第三方 key | **多上游并存** | 路由按 model 前缀已能区分；**问用户默认走哪个**，据此设 `DEFAULT_UPSTREAM` |
| 设了非内置第三方（如 Kimi/Moonshot） | **自定义上游** | 在面板「上游路由」或 `config.py` 加一条上游（`base_url` + `token_env` + 鉴权）和一条路由 |

判定原则：**设了第三方 key 通常就是想用它 → 默认接第三方**；只有 Anthropic / 无 key → 原生。**拿不准就问用户，别擅自假设。**

### 第三步：默认配置好并给出启动命令

1. **代理侧**：默认沿用 `proxy/config.py` 内置的 anthropic / deepseek / glm 三个上游与路由；改默认上游用面板「上游路由」或 `POST /dashboard/api/default-upstream {"name":"deepseek"}`。
2. **Claude Code 侧**：写项目级 `.claude/settings.local.json`（默认做法，见〔安装与启动·第三步〕）；**接第三方时同时设 `ANTHROPIC_MODEL`**，代理据此路由：

   ```json
   {
     "env": {
       "ANTHROPIC_BASE_URL": "http://localhost:8080",
       "ANTHROPIC_MODEL": "deepseek-v4-flash"
     }
   }
   ```
   > **切勿把 API key 写进 `settings.local.json`**——key 只走环境变量；该文件会随项目，写进 key 等于落盘泄密。
3. **核对 key 就绪**：起代理后打开面板「上游路由」，确认目标上游的 **Key 列为 ✓ 就绪**（✗ 表示对应 `token_env` 没在代理进程的环境里）。

### 第四步：第三方上游的额外确认项

- **count_tokens**：DeepSeek / GLM 未文档化该端点，代理对「不支持」的上游做**本地粗略估算**兜底（避免 404，但 token 数不精确）。
- **扩展思考签名**：`thinking` / `signature` 仍逐字节透传，但 Anthropic 的加密签名语义在第三方上**不成立**，别指望第三方支持。
- **能力差异**：DeepSeek 不支持图片/文档块、GLM-4.6 纯文本；超出能力的内容块由上游决定，代理不拦。
- **协议**：仅支持 **Anthropic 兼容**端点；OpenAI Chat Completions 格式的服务需协议转换（前面串 claude-code-router / LiteLLM），不在本工具范围内。

---

## 环境要求

| 依赖 | 最低版本 | 说明 |
|------|---------|------|
| Python | 3.9+ | 系统自带或 Miniconda |
| pip | 任意 | 安装 Python 包 |
| Claude Code | 任意 | 配合 `ANTHROPIC_BASE_URL` 使用 |

---

## 安装与启动

### 第一步：安装依赖

```bash
cd proxy
pip install -r requirements.txt
```

依赖包括：`fastapi`、`uvicorn`、`httpx`、`aiosqlite`、`pydantic`、`python-dotenv`、`regex`。

### 第二步：启动代理

```bash
# 在 proxy/ 目录下运行
python main.py
```

成功后输出：
```
SanityProxy starting on http://127.0.0.1:8080
Dashboard: http://127.0.0.1:8080/dashboard
Mode: desensitize
INFO: Uvicorn running on http://127.0.0.1:8080
```

> **保持此终端开启**，代理在前台运行。如需后台运行：`nohup python main.py &`

### 第三步：配置 Claude Code 使用代理

**默认做法**：写入项目级配置 `.claude/settings.local.json`，作用范围限定在本项目，不污染全局环境，也便于随项目管理。

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://localhost:8080"
  }
}
```

> Agent 在执行时应**默认采用上述项目级配置**，但**最终以用户选择为准**——若用户希望临时生效或全局生效，按下面两种方式处理。

临时生效（仅本次启动，不落盘）：

```bash
ANTHROPIC_BASE_URL=http://localhost:8080 claude
```

全局永久生效（写入 shell 配置 `~/.zshrc` / `~/.bashrc`）：

```bash
export ANTHROPIC_BASE_URL=http://localhost:8080
```

---

## 验证代理正常工作

### 方法 A：Web 面板（推荐）

打开 [http://localhost:8080/dashboard](http://localhost:8080/dashboard)，使用**规则测试**功能：

1. 在测试文本框输入：
   ```
   被告人李明，身份证号 110101199001011234，手机 13812345678
   ```
2. 点击「测试脱敏」
3. 验证结果：
   - **脱敏结果**应类似：`被告人[[SANITY_PERSON_002]]，身份证号 [[SANITY_PERSON_001]]，手机 138[[SANITY_CONTACT_001]]`
   - **映射表**应显示 tag ↔ 原始值的对应关系

### 方法 B：运行自动化测试

```bash
cd proxy
pip install pytest pytest-asyncio   # 首次需要
python -m pytest tests/ -v
```

全部用例通过即代表代理工作正常，覆盖四组场景：
- **出站脱敏**（`test_desensitize_outbound.py`）— 发往上游的请求不含原始 PII，system prompt 同样被脱敏
- **入站还原**（`test_restore_inbound.py`）— 响应中的标签被正确还原；透明模式不修改数据
- **出站自检 / fail-closed**（`test_failclosed.py`）— tool_result/tool_use 等字段也脱敏；漏网 PII 被兜底；案号加固；block/remask/off 三档行为
- **thinking 签名与流式**（`test_thinking_signature.py`）— thinking 块与 signature 全程逐字节不变；流式只还原正文不还原 thinking、能拼回被劈开的标签、采集 usage；上游非 200 回传真实状态码

> **⚠️ 测试未全部通过时，禁止继续后续步骤。** Agent 必须回头定位失败原因、检查并修改相关代码（`desensitizer.py` / `rules.py` / `server.py` 等），然后重新运行 `python -m pytest tests/ -v`，如此循环直到**全部通过**为止。只有测试全绿，才进入「测试通过后的操作」环节。

### 方法 C：在 Claude Code 中直接测试

启动 Claude Code（已设置 `ANTHROPIC_BASE_URL`）后，发送一条包含身份证号的消息。同时在 Web 面板日志栏中观察：
- 状态码 `200`
- `脱敏命中` 列的数字 > 0
- 模式列显示「脱敏」

---

## 测试通过后的操作（Agent 执行流程）

当且仅当 4 个自动化测试**全部通过**后，Agent 按以下顺序操作：

1. **向用户确认**。明确告知「测试已全部通过，准备后台启动代理并新建一个脱敏会话」，并征得用户同意。**未获同意不得继续**。

2. **后台启动代理**。用户同意后，在 `proxy/` 目录后台运行代理（避免绑定在当前会话上）：

   ```bash
   cd proxy && nohup python main.py > /tmp/sanityproxy.log 2>&1 &
   ```

   随后确认端口已监听：`lsof -nP -iTCP:8080 -sTCP:LISTEN`。

3. **新建一个能脱敏的 Claude 会话**。按「第三步」的默认做法（项目级 `.claude/settings.local.json`，或以用户选择为准）配置好 `ANTHROPIC_BASE_URL` 后，**启动一个全新的 Claude Code 会话**：

   ```bash
   ANTHROPIC_BASE_URL=http://localhost:8080 claude
   ```

   > **关键**：`ANTHROPIC_BASE_URL` 只在 Claude Code 启动那一刻读取。当前正在运行的会话**无法中途切换为脱敏状态**，必须新开会话才会经过代理。脱敏对话请在这个**新会话**中进行。

4. **提示用户验证**。引导用户打开 [http://localhost:8080/dashboard](http://localhost:8080/dashboard)，在新会话发送一条含 PII 的消息，观察「脱敏命中」列数字 > 0。

---

## 项目文件结构

```
sanity_claude/
├── AGENTS.md           ← 本文件
├── harness/            ← Harness Engineering 教程内容（只读）
└── proxy/
    ├── main.py         ← 启动入口：python main.py
    ├── server.py       ← FastAPI 路由 + 代理核心
    ├── desensitizer.py ← 脱敏/还原引擎（核心逻辑）
    ├── routing.py      ← 多上游 model 路由（按 model 选上游、注入鉴权）
    ├── rules.py        ← 内置规则定义
    ├── storage.py      ← SQLite 规则/上游/路由存储 + 内存日志（不存任何密钥）
    ├── models.py       ← Pydantic 数据模型
    ├── config.py       ← 配置（端口、模式、内置上游 UPSTREAMS / 路由 ROUTES）
    ├── requirements.txt
    ├── static/
    │   ├── index.html  ← Web 面板
    │   ├── app.js      ← 面板交互逻辑
    │   └── style.css
    └── tests/
        ├── conftest.py
        ├── test_desensitize_outbound.py  ← 验证出站脱敏
        ├── test_restore_inbound.py       ← 验证入站还原
        ├── test_failclosed.py            ← 出站自检 / fail-closed / 案号加固
        ├── test_thinking_signature.py    ← thinking 签名不变 + 流式还原
        └── test_routing.py               ← 多上游路由 / 鉴权注入 / 兜底 / 向后兼容
```

---

## 常用操作

### 切换透明模式（临时绕过脱敏）

在 Web 面板右上角点击「切换透明模式」，或调用 API：

```bash
curl -X POST http://localhost:8080/dashboard/api/mode \
  -H "Content-Type: application/json" \
  -d '{"mode": "transparent"}'
```

### 添加自定义规则

在 Web 面板 → 「规则管理」→ 填写名称、分类、正则，点击「添加规则」。

或通过 API：

```bash
curl -X POST http://localhost:8080/dashboard/api/rules \
  -H "Content-Type: application/json" \
  -d '{
    "name": "合同编号",
    "category": "司法信息",
    "pattern": "合同[编号]{2}[：:]\\s*[A-Z0-9\\-]{6,20}",
    "preserve_prefix": 0
  }'
```

### 修改监听端口

编辑 `proxy/config.py`：

```python
LISTEN_PORT = 9090   # 改为其他端口
```

同步更新 `ANTHROPIC_BASE_URL=http://localhost:9090`。

### 多上游 / 切换上游（DeepSeek / GLM 等第三方）

代理按请求体的 `model` 字段路由到不同上游。内置 anthropic / deepseek / glm 与对应路由在 `proxy/config.py`（`UPSTREAMS` / `ROUTES` / `DEFAULT_UPSTREAM`），凭证只从环境变量读。完整流程见〔首次配置：扫描环境、自动选择上游〕。

面板「上游路由」可增删自定义上游/路由、禁用内置项、设默认上游、查看各上游 key 是否就绪；也可走 API：

```bash
# 查看生效上游/路由（key 只显示 key_present，不含 key 值）
curl -s http://localhost:8080/dashboard/api/routing

# 加一条自定义上游（如 Kimi/Moonshot）——只存 token 环境变量名，不存 key
curl -X POST http://localhost:8080/dashboard/api/upstreams \
  -H "Content-Type: application/json" \
  -d '{"name":"kimi","base_url":"https://api.moonshot.ai/anthropic","auth_scheme":"bearer","token_env":"KIMI_API_KEY"}'

# 加一条路由：kimi* → kimi
curl -X POST http://localhost:8080/dashboard/api/routes \
  -H "Content-Type: application/json" \
  -d '{"name":"kimi","match":"kimi*","upstream":"kimi"}'
```

> 单上游回退仍可用：只想整体指向另一个 Anthropic 兼容服务时，改 `proxy/config.py` 的 `UPSTREAM_URL`（它是 anthropic 默认 base 兼回退）。

---

## 出站安全：零泄漏自检与审计

### 出站自检（转发前的兜底，策略可选）

脱敏覆盖的字段：`messages` 里的 **text 块、`tool_result` 内容、`tool_use` 入参、`document` 文本**，以及 `system`——凡是承载用户数据、会上云的内容都脱。`tools` 定义、`model`、`metadata` 等框架字段不脱（不是用户隐私）。

脱敏作用于两个端点：`POST /v1/messages`（含流式）与 `POST /v1/messages/count_tokens`——两者携带同样的对话内容，**count_tokens 也必须脱敏**，否则 token 计数请求会把原文 PII 直接外发。

### 智能姓名识别（jieba，默认开启）

角色词规则（`被告人X` 等）只脱「角色词紧邻」的姓名，**没有角色词引导的裸姓名会漏**（如「…，张三喝了一瓶啤酒」里的张三）。开启「智能姓名识别」后，在 regex 之后追加一趟 `jieba.posseg` 词性标注：把判定为人名（`nr/nrfg/nrt`）且形如姓名（2–4 中文字）的 **token** 也脱，并对本请求内已确认的姓名做 **token 级**全文兜底。

- **token 粒度**是关键：`高强度` 是一个 token（≠`高强`），即便`高强`是已确认姓名也不会被切碎——从根上避免子串过脱。
- **概率性增强、非 fail-closed**：jieba 会漏判生僻/三字/复姓/音译名，也会误判（误判属安全偏向，`restore` 可逆）；角色词 regex 仍是确定性补充。
- 面板右上角「姓名识别」开关，持久化在 `sanity.db`（key `name_detection`）；API：`POST /dashboard/api/name-detection` body `{"enabled":bool}`。
- 依赖 `jieba`（软依赖）：未安装时**自动降级为纯 regex**、不报错；面板开关会置灰。jieba 含 CPU 密集分词，已在 `server.py` 用 `asyncio.to_thread` 卸出事件循环，并在启动时预热字典。
- **切勿把每请求识别出的姓名写入 jieba 全局词典**（`jieba.add_word`）——会跨请求驻留 PII。用户自维护的「已知姓名表」属未来项。

在此之上，代理**转发前**再做一道自检兜底。自检核对**整段会上云的请求体**，仅跳过 `tools`/`model`/`metadata` 三个确知合法携带示例邮箱/长数字的框架字段（避免假阳性 403）。这样即便 PII 藏在结构化脱敏够不到的位置（顶层非常规字段、未知 content 块类型等），`block`/`off` 也能发现——避免"最严档反而漏"的盲区。处理策略可在面板右上角「出站自检」下拉切换，持久化在 `sanity.db`：

| 策略 | 行为 | 适用 |
|------|------|------|
| **补脱后放行**（`remask`，默认） | 对请求体做**结构化广覆盖**兜底（遍历可改写字符串，跳过 thinking/签名/结构字段与框架字段），把任何残留 PII **就地脱敏后再转发**——既不外泄也不拦截 | 既要安全又要顺滑，推荐 |
| **拦截**（`block`，fail-closed） | 自检命中即返回 `403`，绝不发往云端（"宁可拦错，不可放过"） | 高敏、宁可误拦 |
| **仅告警**（`off`） | 命中只在「出站审计」记一条告警，照常转发 | 只想观察、不被打断 |

- 请求体解析/脱敏失败时一律拦截（脱敏模式下不"出错即放行原文"）。
- 透明模式不做自检（本就不脱敏）。
- 切换也可走 API：`POST /dashboard/api/selfcheck`，body `{"policy":"remask|block|off"}`。

> `remask` 是广覆盖兜底，可能顺带把非常规字段里形似 PII 的内容（如工具 schema 的示例号码）也打上标签，属安全偏向的副作用，不影响脱敏正确性。
>
> **关键不变量**：`thinking` / `redacted_thinking` 块及任意位置的 `signature`、`tool_use_id`、`id` 等结构字段，在脱敏 / 自检 / 补脱 / 还原全链路中**绝不被改写，逐字节往返**。否则上游验签失败返回 400，Claude Code 会不断重试（历史 bug）。相应地，**响应里的 thinking 不做标签还原**——让它在整条会话里保持标签态，签名才能恒有效（代价：思考面板显示的是标签）。

### 出站审计快照

每次出站请求都会在面板「出站审计」中留一份**脱敏后实际发送内容**的快照（被拦截的请求也会记录，并标注命中详情，样本做部分遮挡）。可点「查看」核对到底发了什么。

**保留条数可在面板配置**：最近 `20 / 100 / 200 / 500 / 所有`。设置持久化在 `sanity.db`，重启保留。

> 面板「实时请求日志」与「出站审计」均为**定高可滚动**面板（表头吸顶），不会随请求增多把页面撑长；日志标题旁的计数标签显示当前缓冲条数（上限 200 行）。**流式请求的输入/输出 Token 现已从 SSE 的 `message_start`/`message_delta` 事件解析记录**（此前流式恒显示 0）。

> 注意：选「所有」时快照不设上限，长时间运行会占用较多内存；按需选择。

快照与容量也可通过 API 操作：

```bash
# 查看快照 + 当前容量
curl -s http://localhost:8080/dashboard/api/snapshots

# 设置容量（100/200/500/all）
curl -X POST http://localhost:8080/dashboard/api/snapshot-capacity \
  -H "Content-Type: application/json" -d '{"capacity":"500"}'
```

### 面板时间

实时日志与审计快照的时间均为 **UTC+8（东八区）**。

---

## 资料文件管理（用户放置敏感资料的建议）

用户常需在项目目录下放置大量敏感原文（法律文书、病历、合同等）供 Claude Code 阅读分析。**这些资料是脱敏对象本身，绝不能进 git、不能外发。** 建议如下：

**1. 固定一个被忽略的资料目录。** 已在 `.gitignore` 预留 `materials/`、`workspace/`、`data/`、`*.private/`。把原始资料放进 `materials/`，按案件/主题分子目录：

```
sanity_claude/
├── proxy/                  # 工具代码（入库）
├── materials/              # 原始敏感资料（已 gitignore，绝不入库）
│   ├── 2026-案件A/
│   │   ├── 起诉状.pdf
│   │   └── 笔录.txt
│   └── 2026-案件B/
└── workspace/              # Claude 生成的分析/草稿（已 gitignore）
```

**2. 命名与组织。** 用「日期/案号-主题」开头便于检索；同一案件的原文与产出物分开（`materials/` 放原文，`workspace/` 放生成结果），避免误把含 PII 的原文当成果提交。

**3. 资料经代理才安全。** Claude Code 读取本地文件本身不外发；只有当文件内容被放进发往 Anthropic 的请求时才会上云——**此时务必处于脱敏模式**，代理会把其中 PII 换成标签。可在面板「出站审计」核对实际发出的内容。对超出内置规则的资料专有标识（员工号、内部单号等），先在「规则管理」加自定义规则再喂给模型。

**4. 大文件与体积。** 单次请求体积有限，超大文档应拆分或摘要后再喂；`materials/` 不入库，故不影响仓库体积，但请自行做**加密备份**（资料一旦丢失不可恢复）。

**5. 清理。** 注册表（值↔标签映射）每请求隔离、随请求结束回收，不落盘；`sanity.db` 只存规则与设置，不存原文。原始资料的留存与销毁由用户在 `materials/` 自行管理。

---

## 内置脱敏规则一览

| 名称 | 分类 | 示例 |
|------|------|------|
| 居民身份证 | 个人身份 | `110101199001011234` |
| 护照号 | 个人身份 | `E12345678` |
| 姓名 | 个人身份 | 被告人/原告人/委托人等后接的 2-4 字中文名 |
| 手机号 | 联系方式 | `13812345678`（保留前 3 位） |
| 固定电话 | 联系方式 | `010-12345678` |
| 电子邮箱 | 联系方式 | `user@example.com` |
| 银行卡号 | 金融信息 | 16-19 位数字 |
| 统一社会信用代码 | 机构信息 | 18 位字母数字 |
| 案件编号 | 司法信息 | `（2024）京民初第1234号` |
| 车牌号 | 其他 | `京A12345` |

---

## 故障排查

| 现象 | 可能原因 | 解决方法 |
|------|---------|---------|
| Claude Code 无法连接 | 代理未启动或端口不匹配 | 确认 `python main.py` 正在运行，端口与 `ANTHROPIC_BASE_URL` 一致 |
| 响应很慢 | 代理增加了一次本地处理 | 正常，通常 < 50ms 额外延迟 |
| 脱敏命中数为 0 | 规则未启用或文本不匹配 | 在面板「规则测试」中验证规则是否生效 |
| 面板无法打开 | 端口冲突 | 检查 `lsof -i:8080`，修改 `LISTEN_PORT` |
| 标签未还原 | 映射表未建立（透明模式发起的请求） | 确认发送请求时已处于脱敏模式 |
| 新会话连不上、报错无法连接 Anthropic | 代理已停，但项目配置仍指向 `localhost:8080` | 重启代理（`cd proxy && nohup python main.py > /tmp/sanityproxy.log 2>&1 &`），或临时删除 `.claude/settings.local.json` 中的 `env.ANTHROPIC_BASE_URL` 后再开会话 |

---

## 修改代码须知

- **改完核心逻辑后必须跑测试**：`python -m pytest tests/ -v`，全部用例（含出站自检、案号加固）全绿才算完成
- 脱敏标签格式固定为 `[[SANITY_CATEGORY_NNN]]`，不要轻易变更，会破坏还原与自检逻辑
- 需要脱敏的端点由 `server.py` 的 `should_mask` 判定（`messages` + `messages/count_tokens`）；新增会上云内容的端点时，记得一并纳入，否则原文会绕过脱敏
- `sanity.db` 是运行时产物，不进 git；内置规则首次启动时写入，**之后每次启动会用 `rules.py` 里的最新正则同步覆盖内置规则**（保留用户的启用/停用状态），所以改内置规则正则直接改 `rules.py` 即可
- 出站自检 `desensitizer.detect_residual` 去标签时用**空格**替换（非删除），以免关键词型规则（如姓名）在标签移除后与后文相邻而假阳性
- **绝不可改写 `thinking`/`signature`/`tool_use_id`/`id` 等字段**：自检、补脱、还原统一走 `desensitizer._walk_maskable`（跳过受保护字段）做结构化遍历，**严禁把整段 JSON 当字符串跑正则替换**——那会改坏签名导致上游 400 死循环
- 脱敏注册表（值→标签映射）为**每请求隔离**（`_new_registry()`），不再有模块级全局；还原走每请求 `session_mapping`，不依赖全局状态
- Python 最低版本 3.9，类型注解用 `Optional[X]` 而非 `X | None`（3.10+ 语法）
- 姓名规则依赖 `regex` 模块（支持变长 lookbehind），不是标准库 `re`
