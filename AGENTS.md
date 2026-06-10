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

全部 4 个测试通过即代表代理工作正常：
- `test_pii_stripped_from_upstream_request` — 发往上游的请求不含原始 PII
- `test_system_prompt_also_desensitized` — system prompt 同样被脱敏
- `test_full_roundtrip_pii_restored_in_response` — 响应中的标签被正确还原
- `test_transparent_mode_passes_raw_data` — 透明模式不修改数据

> **⚠️ 测试未全部通过时，禁止继续后续步骤。** Agent 必须回头定位失败原因、检查并修改相关代码（`desensitizer.py` / `rules.py` / `server.py` 等），然后重新运行 `python -m pytest tests/ -v`，如此循环直到 **4/4 全部通过**为止。只有测试全绿，才进入「测试通过后的操作」环节。

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
    ├── rules.py        ← 内置规则定义
    ├── storage.py      ← SQLite 规则存储 + 内存日志
    ├── models.py       ← Pydantic 数据模型
    ├── config.py       ← 配置（端口、上游 URL、模式）
    ├── requirements.txt
    ├── static/
    │   ├── index.html  ← Web 面板
    │   ├── app.js      ← 面板交互逻辑
    │   └── style.css
    └── tests/
        ├── conftest.py
        ├── test_desensitize_outbound.py  ← 验证出站脱敏
        └── test_restore_inbound.py       ← 验证入站还原
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

### 修改上游 API（如使用其他 Anthropic 兼容服务）

编辑 `proxy/config.py`：

```python
UPSTREAM_URL = "https://your-compatible-api.example.com"
```

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

- **改完核心逻辑后必须跑测试**：`python -m pytest tests/ -v`，4/4 全绿才算完成
- 脱敏标签格式固定为 `[[SANITY_CATEGORY_NNN]]`，不要轻易变更，会破坏还原逻辑
- `sanity.db` 是运行时产物，不进 git；内置规则在首次启动时由 `storage.py` 写入
- Python 最低版本 3.9，类型注解用 `Optional[X]` 而非 `X | None`（3.10+ 语法）
- 姓名规则依赖 `regex` 模块（支持变长 lookbehind），不是标准库 `re`
