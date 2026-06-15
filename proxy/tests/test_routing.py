"""
测试用例：多上游 model 路由

覆盖：
- routing.resolve 的内置匹配 / 通配 / 回退 / 自定义覆盖 / 禁用内置 / 缺 key 不注入
- 集成：按 model 路由到 DeepSeek(x-api-key) / GLM(Bearer)，原 PII 仍被脱敏
- model 改写、count_tokens 本地兜底、Anthropic 无 key 时鉴权透传、透明模式下仍路由
"""
import json
import pytest
import httpx
from unittest.mock import AsyncMock, MagicMock, patch


# ── helpers ──────────────────────────────────────────────────────────────────

def _header(headers: dict, name: str):
    for k, v in headers.items():
        if k.lower() == name.lower():
            return v
    return None


def _make_resp():
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {"content-type": "application/json"}
    payload = {
        "id": "msg_x", "type": "message", "role": "assistant",
        "content": [{"type": "text", "text": "ok"}],
        "usage": {"input_tokens": 5, "output_tokens": 2},
    }
    resp.content = json.dumps(payload).encode()
    resp.json = MagicMock(return_value=payload)
    return resp


async def _post(path: str, payload: dict, capture: dict, extra_headers=None):
    """发一条请求，捕获实际发往上游的 method/url/headers/body。"""
    from server import app
    import server

    async def fake_request(method, url, headers=None, content=None, **kwargs):
        capture["method"] = method
        capture["url"] = str(url)
        capture["headers"] = dict(headers or {})
        capture["body"] = json.loads(content) if content else None
        return _make_resp()

    with patch.object(server, "_http_client") as mc:
        mc.request = AsyncMock(side_effect=fake_request)
        server._current_mode = "desensitize"  # 全局会被其它测试改成 transparent，这里显式复位
        server._current_selfcheck = "remask"
        server._name_detection = False  # 测试里关掉 jieba，快且确定
        headers = {"x-api-key": "client-key", "anthropic-version": "2023-06-01"}
        if extra_headers:
            headers.update(extra_headers)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(path, json=payload, headers=headers)
    return resp


PII_MSG = {"role": "user", "content": "被告人[[SANITY_PERSON_028]]，手机 13812345678，身份证 110101199001011234"}


# ── routing.resolve 单元测试 ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resolve_builtin_deepseek(monkeypatch):
    import storage, routing
    await storage.get_db()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-secret")
    r = await routing.resolve("deepseek-v4-flash")
    assert r.name == "deepseek"
    assert r.base_url == "https://api.deepseek.com/anthropic"
    assert r.auth_scheme == "x-api-key"
    assert r.token == "ds-secret"
    assert r.supports_count_tokens is False


@pytest.mark.asyncio
async def test_resolve_builtin_glm_bearer(monkeypatch):
    import storage, routing
    await storage.get_db()
    monkeypatch.setenv("GLM_API_KEY", "glm-secret")
    r = await routing.resolve("glm-4.6")
    assert r.name == "glm"
    assert r.auth_scheme == "bearer"
    assert r.token == "glm-secret"


@pytest.mark.asyncio
async def test_resolve_claude_and_fallback(monkeypatch):
    import storage, routing
    await storage.get_db()
    assert (await routing.resolve("claude-opus-4-8")).name == "anthropic"
    # 未知模型 / None → 回退默认上游（anthropic）
    assert (await routing.resolve("totally-unknown")).name == "anthropic"
    assert (await routing.resolve(None)).name == "anthropic"


@pytest.mark.asyncio
async def test_resolve_missing_key_means_no_token(monkeypatch):
    import storage, routing
    await storage.get_db()
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    r = await routing.resolve("deepseek-v4-flash")
    assert r.name == "deepseek"
    assert r.token is None  # 缺 key → 不注入，由 server 决定透传


@pytest.mark.asyncio
async def test_resolve_custom_overrides_builtin(monkeypatch):
    import storage, routing
    await storage.get_db()
    monkeypatch.setenv("KIMI_API_KEY", "kimi-secret")
    await storage.create_upstream("kimi", "https://api.moonshot.ai/anthropic", "bearer", "KIMI_API_KEY", 0)
    await storage.create_route("kimi", "kimi*", "kimi", None, 0)
    r = await routing.resolve("kimi-k2")
    assert r.name == "kimi"
    assert r.base_url == "https://api.moonshot.ai/anthropic"
    assert r.token == "kimi-secret"


@pytest.mark.asyncio
async def test_resolve_disabled_builtin_route_falls_through(monkeypatch):
    import storage, routing
    await storage.get_db()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds")
    await routing.set_builtin_enabled("route", "deepseek", False)
    # deepseek 路由被禁用 → deepseek-v4 不再命中 → 回退默认 anthropic
    r = await routing.resolve("deepseek-v4-flash")
    assert r.name == "anthropic"


@pytest.mark.asyncio
async def test_resolve_model_rewrite(monkeypatch):
    import storage, routing
    await storage.get_db()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds")
    await storage.create_route("ds-rewrite", "claude-haiku*", "deepseek", "deepseek-v4-flash", 0)
    r = await routing.resolve("claude-haiku-4-5")
    assert r.name == "deepseek"
    assert r.model_rewrite == "deepseek-v4-flash"


# ── 集成测试：实际路由 + 鉴权注入 + 脱敏仍生效 ────────────────────────────────

@pytest.mark.asyncio
async def test_deepseek_routed_with_xapikey_and_masked(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-secret")
    cap = {}
    payload = {"model": "deepseek-v4-flash", "max_tokens": 50, "messages": [PII_MSG]}
    resp = await _post("/v1/messages", payload, cap)
    assert resp.status_code == 200
    assert cap["url"].startswith("https://api.deepseek.com/anthropic/v1/messages")
    assert _header(cap["headers"], "x-api-key") == "ds-secret"
    # 原 PII 不得出现在发往上游的请求体里，且含占位标签
    sent = json.dumps(cap["body"], ensure_ascii=False)
    assert "110101199001011234" not in sent and "13812345678" not in sent
    assert "[[SANITY_" in sent


@pytest.mark.asyncio
async def test_glm_routed_with_bearer(monkeypatch):
    monkeypatch.setenv("GLM_API_KEY", "glm-secret")
    cap = {}
    payload = {"model": "glm-4.6", "max_tokens": 50, "messages": [PII_MSG]}
    resp = await _post("/v1/messages", payload, cap)
    assert resp.status_code == 200
    assert cap["url"].startswith("https://api.z.ai/api/anthropic/v1/messages")
    assert _header(cap["headers"], "authorization") == "Bearer glm-secret"
    # 注入后客户端原 x-api-key 应被移除
    assert _header(cap["headers"], "x-api-key") is None


@pytest.mark.asyncio
async def test_anthropic_passthrough_when_no_env_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cap = {}
    payload = {"model": "claude-opus-4-8", "max_tokens": 50, "messages": [PII_MSG]}
    resp = await _post("/v1/messages", payload, cap)
    assert resp.status_code == 200
    # 无 env key → 不注入，透传客户端原鉴权头（兼容 Claude 订阅登录）
    assert _header(cap["headers"], "x-api-key") == "client-key"


@pytest.mark.asyncio
async def test_deepseek_passthrough_when_no_env_key(monkeypatch):
    """复用现有配置：未设 DEEPSEEK_API_KEY 时，deepseek 路由仍生效，客户端自带的鉴权头
    原样透传到 DeepSeek，PII 仍被脱敏——即"接入只需把 ANTHROPIC_BASE_URL 指向代理"。"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    cap = {}
    payload = {"model": "deepseek-chat", "max_tokens": 50, "messages": [PII_MSG]}
    # 模拟用户原本用 ANTHROPIC_AUTH_TOKEN（Bearer）跑通 DeepSeek 的情形
    resp = await _post("/v1/messages", payload, cap,
                       extra_headers={"authorization": "Bearer client-ds"})
    assert resp.status_code == 200
    # ① 仍路由到 deepseek 端点（destination 由内置上游/路由提供，无需用户配）
    assert cap["url"].startswith("https://api.deepseek.com/anthropic/v1/messages")
    # ② 客户端原鉴权头原样透传（既不删除、也不改写）——无需另设 DEEPSEEK_API_KEY
    assert _header(cap["headers"], "authorization") == "Bearer client-ds"
    # ③ PII 仍被脱敏
    sent = json.dumps(cap["body"], ensure_ascii=False)
    assert "110101199001011234" not in sent and "[[SANITY_" in sent


@pytest.mark.asyncio
async def test_model_rewrite_applied_to_body(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds")
    import storage
    await storage.get_db()
    await storage.create_route("rw", "claude-haiku*", "deepseek", "deepseek-v4-flash", 0)
    cap = {}
    payload = {"model": "claude-haiku-4-5", "max_tokens": 50, "messages": [PII_MSG]}
    resp = await _post("/v1/messages", payload, cap)
    assert resp.status_code == 200
    assert cap["url"].startswith("https://api.deepseek.com/anthropic/")
    assert cap["body"]["model"] == "deepseek-v4-flash"


@pytest.mark.asyncio
async def test_count_tokens_local_fallback_no_upstream_call(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds")
    from server import app
    import server

    called = {"n": 0}

    async def fake_request(*a, **k):
        called["n"] += 1
        return _make_resp()

    with patch.object(server, "_http_client") as mc:
        mc.request = AsyncMock(side_effect=fake_request)
        server._current_mode = "desensitize"
        server._name_detection = False
        payload = {"model": "deepseek-v4-flash", "messages": [PII_MSG]}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/v1/messages/count_tokens", json=payload,
                                     headers={"x-api-key": "client-key"})
    assert resp.status_code == 200
    assert isinstance(resp.json().get("input_tokens"), int)
    # DeepSeek 不支持 count_tokens → 本地兜底，不应打到上游
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_transparent_mode_still_routes(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-secret")
    from server import app
    import server

    cap = {}

    async def fake_request(method, url, headers=None, content=None, **kwargs):
        cap["url"] = str(url)
        cap["headers"] = dict(headers or {})
        cap["body"] = json.loads(content) if content else None
        return _make_resp()

    with patch.object(server, "_http_client") as mc:
        mc.request = AsyncMock(side_effect=fake_request)
        server._current_mode = "transparent"
        try:
            payload = {"model": "deepseek-v4-flash", "max_tokens": 50, "messages": [PII_MSG]}
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post("/v1/messages", json=payload,
                                         headers={"x-api-key": "client-key"})
        finally:
            server._current_mode = "desensitize"
    assert resp.status_code == 200
    # 透明模式不脱敏，但仍要路由到正确上游并注入鉴权
    assert cap["url"].startswith("https://api.deepseek.com/anthropic/v1/messages")
    assert _header(cap["headers"], "x-api-key") == "ds-secret"
