"""
测试用例 3：出站零泄漏自检（fail-closed）+ 加固后的案号规则

- 覆盖：tool_result / tool_use 等会上云的字段也要脱敏（而非直接拦截），
  脱敏后正常放行，原始 PII 不出本机。
- 自检收窄：只扫 messages 内容 + system，不扫 tools/model/metadata 等框架字段，
  避免框架里的邮箱/长数字示例造成假阳性 403。
- 兜底：若脱敏真有遗漏（内容区域仍残留 PII），自检仍拦截。
- 案号：加固后的正则应覆盖标准写法（如 （2024）京0108民初1234号）。
"""

import json
import pytest
import httpx
from unittest.mock import AsyncMock, MagicMock, patch


def _mock_ok_response():
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {"content-type": "application/json"}
    resp.content = json.dumps({
        "id": "msg_x", "type": "message", "role": "assistant",
        "content": [{"type": "text", "text": "ok"}],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }).encode()
    resp.json = MagicMock(return_value=json.loads(resp.content))
    return resp


@pytest.mark.asyncio
async def test_count_tokens_endpoint_is_desensitized():
    """/v1/messages/count_tokens 携带同样的对话内容，必须同样脱敏——
    否则 token 计数请求会把原始 PII 原文发往云端。"""
    from server import app
    import server

    captured = {}

    async def fake_request(method, url, headers=None, content=None, **kwargs):
        captured["url"] = str(url)
        captured["body"] = content.decode("utf-8") if isinstance(content, bytes) else content
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"content-type": "application/json"}
        resp.content = json.dumps({"input_tokens": 42}).encode()
        resp.json = MagicMock(return_value={"input_tokens": 42})
        return resp

    raw_id = "110101199001011234"
    payload = {
        "model": "claude-sonnet-4-6",
        "messages": [{"role": "user", "content": f"被告人李明 身份证 {raw_id}"}],
    }

    with patch.object(server, "_http_client") as mock_client:
        mock_client.request = AsyncMock(side_effect=fake_request)
        server._current_mode = "desensitize"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/v1/messages/count_tokens", json=payload,
                headers={"x-api-key": "sk", "content-type": "application/json"})

    assert resp.status_code == 200
    assert captured["url"].endswith("/v1/messages/count_tokens"), "应转发到 count_tokens 上游"
    assert raw_id not in captured["body"], "count_tokens 也绝不能让原始 PII 上云！"
    assert "[[SANITY_" in captured["body"], "count_tokens 内容应已脱敏为标签"
    print("\n[PASS] count_tokens 端点同样脱敏，PII 不外泄")


@pytest.mark.asyncio
async def test_tool_result_pii_now_masked():
    """
    PII 藏在 tool_result 块里（会上云）。desensitize 现已覆盖该字段：
    应【脱敏后放行】——返回 200、正常转发，且上游收到的 body 里
    只有 [[SANITY_*]] 标签、不含原始身份证号。
    """
    from server import app
    import server

    captured = {}

    async def fake_request(method, url, headers=None, content=None, **kwargs):
        captured["body"] = json.loads(content)
        return _mock_ok_response()

    raw_id = "110101199001011234"
    payload = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 100,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1",
                     "content": f"查询结果：当事人身份证 {raw_id}"},
                ],
            }
        ],
    }

    with patch.object(server, "_http_client") as mock_client:
        mock_client.request = AsyncMock(side_effect=fake_request)
        server._current_mode = "desensitize"

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/v1/messages", json=payload,
                headers={"x-api-key": "sk-test", "content-type": "application/json"},
            )

    assert resp.status_code == 200, f"tool_result PII 应被脱敏后放行，实际状态 {resp.status_code}"
    assert "body" in captured, "脱敏后的请求应被转发给上游"
    upstream_text = json.dumps(captured["body"], ensure_ascii=False)
    assert raw_id not in upstream_text, "原始身份证号绝不能上云！"
    assert "[[SANITY_" in upstream_text, "tool_result 里的身份证应替换为标签"
    print("\n[PASS] tool_result 里的 PII 被脱敏后放行，原始号码未外发")


def test_detect_residual_backstops_unmasked_pii():
    """兜底：内容区域若仍残留未脱敏 PII，detect_residual 必须命中（fail-closed 依据）。"""
    import desensitizer
    from rules import BUILTIN_RULES
    rules = [{"name": r.name, "category": r.category,
              "pattern": r.pattern, "preserve_prefix": r.preserve_prefix}
             for r in BUILTIN_RULES]

    body = {
        "model": "claude-sonnet-4-6",
        "messages": [{
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t",
                         "content": "身份证 110101199001011234"}],
        }],
    }
    leaks = desensitizer.detect_residual(body, rules)
    assert any(l["rule"] == "居民身份证" for l in leaks), f"残留 PII 应被兜底命中：{leaks}"
    print("\n[PASS] 残留 PII 仍被 detect_residual 兜底命中")


def test_detect_residual_ignores_framework_fields():
    """收窄：tools/metadata 等框架字段里的邮箱/长数字不应被自检误判为泄漏。"""
    import desensitizer
    from rules import BUILTIN_RULES
    rules = [{"name": r.name, "category": r.category,
              "pattern": r.pattern, "preserve_prefix": r.preserve_prefix}
             for r in BUILTIN_RULES]

    body = {
        "model": "claude-sonnet-4-6",
        "metadata": {"user_id": "account_018ab12cd34ef567"},
        "tools": [{
            "name": "bash",
            "description": "Run cmd. hotline 010-12345678, ref AB1234567, mail dev@example.com",
            "input_schema": {"type": "object",
                             "properties": {"card": {"description": "e.g. 6225880212345678901"}}},
        }],
        "messages": [{"role": "user", "content": "普通问题，不含任何 PII。"}],
    }
    leaks = desensitizer.detect_residual(body, rules)
    assert leaks == [], f"框架字段不应触发自检假阳性：{leaks}"
    print("\n[PASS] 框架字段（tools/metadata）不再造成假阳性")


@pytest.mark.asyncio
async def test_failclosed_allows_clean_request():
    """正常脱敏后无残留的请求应放行（自检通过 → 转发）。"""
    from server import app
    import server

    captured = {}

    async def fake_request(method, url, headers=None, content=None, **kwargs):
        captured["body"] = json.loads(content)
        return _mock_ok_response()

    payload = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "被告人李明，手机 13812345678"}],
    }

    with patch.object(server, "_http_client") as mock_client:
        mock_client.request = AsyncMock(side_effect=fake_request)
        server._current_mode = "desensitize"

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/v1/messages", json=payload,
                headers={"x-api-key": "sk-test", "content-type": "application/json"},
            )

    assert resp.status_code == 200, "干净请求不应被拦截"
    assert "body" in captured, "干净请求应被转发"
    assert "[[SANITY_" in captured["body"]["messages"][0]["content"]
    print("\n[PASS] 自检通过的干净请求正常放行")


@pytest.mark.asyncio
async def test_hardened_case_number_masked():
    """加固后的案号规则应覆盖标准写法 （2024）京0108民初1234号。"""
    from server import app
    import server

    captured = {}

    async def fake_request(method, url, headers=None, content=None, **kwargs):
        captured["body"] = json.loads(content)
        return _mock_ok_response()

    case_no = "（2024）京0108民初1234号"
    payload = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": f"本案案号{case_no}，请分析。"}],
    }

    with patch.object(server, "_http_client") as mock_client:
        mock_client.request = AsyncMock(side_effect=fake_request)
        server._current_mode = "desensitize"

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/v1/messages", json=payload,
                headers={"x-api-key": "sk-test", "content-type": "application/json"},
            )

    assert resp.status_code == 200
    upstream_text = captured["body"]["messages"][0]["content"]
    print(f"\n[上游收到] {upstream_text}")
    assert case_no not in upstream_text, "加固后的案号未被脱敏！"
    assert "[[SANITY_LEGAL_" in upstream_text, "案号应替换为 LEGAL 标签"
    print("[PASS] 标准格式案号已正确脱敏")


def test_remask_residual_masks_anywhere():
    """补脱（remask 档）：全文兜底应把任何残留 PII 就地脱敏，即便它藏在
    结构化脱敏不覆盖的字段里，且不丢标签、不破坏 JSON。"""
    import desensitizer
    from rules import BUILTIN_RULES
    rules = [{"name": r.name, "category": r.category,
              "pattern": r.pattern, "preserve_prefix": r.preserve_prefix}
             for r in BUILTIN_RULES]

    raw_id = "110101199001011234"
    body_bytes = json.dumps({
        "model": "claude-sonnet-4-6",
        "messages": [{"role": "user", "content": "已脱敏的正常内容 [[SANITY_PERSON_001]]"}],
        # 残留 PII 藏在结构化脱敏不会走到的位置
        "weird_field": f"漏网身份证 {raw_id}",
    }, ensure_ascii=False).encode()

    out, mapping, swept = desensitizer.remask_residual(body_bytes, rules)
    out_text = out.decode("utf-8")

    assert raw_id not in out_text, "残留身份证应被补脱"
    assert "[[SANITY_" in out_text and "[[SANITY_PERSON_001]]" in out_text, "既有标签不应被破坏"
    assert json.loads(out_text), "补脱后仍应是合法 JSON"
    assert any(l["rule"] == "居民身份证" for l in swept), f"补脱命中应包含身份证：{swept}"
    print("\n[PASS] 残留 PII 被全文兜底补脱，标签与 JSON 完好")


def test_detect_residual_catches_pii_outside_messages():
    """fail-closed 堵漏：结构化脱敏只覆盖 messages+system，藏在顶层非常规字段里的
    PII 既没被脱、旧版自检（只扫 messages+system）也扫不出——会以"最严"的 block
    策略静默上云。现自检扫描面已扩到「全身 - 框架字段」，必须命中。"""
    import desensitizer
    from rules import BUILTIN_RULES
    rules = [{"name": r.name, "category": r.category,
              "pattern": r.pattern, "preserve_prefix": r.preserve_prefix}
             for r in BUILTIN_RULES]

    raw_id = "110101199001011234"
    # desensitize 只脱 messages/system；weird_field 不在其覆盖面内
    masked, _ = desensitizer.desensitize({
        "model": "claude-sonnet-4-6",
        "messages": [{"role": "user", "content": "正常问题"}],
        "weird_field": f"漏网身份证 {raw_id}",
    }, rules)
    assert raw_id in json.dumps(masked, ensure_ascii=False), "前提：weird_field 未被结构化脱敏"

    leaks = desensitizer.detect_residual(masked, rules)
    assert any(l["rule"] == "居民身份证" for l in leaks), \
        f"block/off 自检必须发现 messages 之外的漏网 PII：{leaks}"
    print("\n[PASS] 顶层非常规字段里的漏网 PII 已被自检兜住（fail-closed 不再有盲区）")


def test_detect_residual_catches_unknown_content_block():
    """fail-closed 堵漏：未来/未知的 content 块类型若承载 PII，_extract_text_from_content
    认不出、desensitize 脱不到，但全身扫描的自检仍须命中。"""
    import desensitizer
    from rules import BUILTIN_RULES
    rules = [{"name": r.name, "category": r.category,
              "pattern": r.pattern, "preserve_prefix": r.preserve_prefix}
             for r in BUILTIN_RULES]

    raw_id = "110101199001011234"
    body = {
        "model": "claude-sonnet-4-6",
        "messages": [{"role": "user", "content": [
            {"type": "future_block_xyz", "text": f"身份证 {raw_id}"},
        ]}],
    }
    masked, _ = desensitizer.desensitize(body, rules)
    assert raw_id in json.dumps(masked, ensure_ascii=False), "前提：未知块类型未被脱敏"

    leaks = desensitizer.detect_residual(masked, rules)
    assert any(l["rule"] == "居民身份证" for l in leaks), \
        f"未知 content 块里的 PII 必须被自检命中：{leaks}"
    print("\n[PASS] 未知 content 块类型里的 PII 已被自检兜住")


@pytest.mark.asyncio
async def test_block_policy_blocks_pii_in_nonstandard_field():
    """端到端：block 策略下，藏在顶层非常规字段里的 PII 应触发 403，绝不转发。"""
    from server import app
    import server

    captured = {}

    async def fake_request(method, url, headers=None, content=None, **kwargs):
        captured["body"] = content
        return _mock_ok_response()

    raw_id = "110101199001011234"
    payload = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "正常问题"}],
        "weird_field": f"漏网身份证 {raw_id}",
    }

    with patch.object(server, "_http_client") as mock_client:
        mock_client.request = AsyncMock(side_effect=fake_request)
        server._current_mode = "desensitize"
        server._current_selfcheck = "block"
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/v1/messages", json=payload,
                    headers={"x-api-key": "sk-test", "content-type": "application/json"},
                )
        finally:
            server._current_selfcheck = "remask"  # restore default

    assert resp.status_code == 403, f"block 策略须拦截漏网 PII，实际 {resp.status_code}"
    assert "body" not in captured, "被拦截的请求绝不能转发给上游！"
    print("\n[PASS] block 策略拦住了 messages 之外的漏网 PII（最严档不再有盲区）")


@pytest.mark.asyncio
async def test_selfcheck_remask_policy_forwards_masked():
    """remask 策略下：含 PII 的 tool_result 应脱敏后放行（200 + 转发 + 无原文）。"""
    from server import app
    import server

    captured = {}

    async def fake_request(method, url, headers=None, content=None, **kwargs):
        captured["body"] = content.decode("utf-8") if isinstance(content, bytes) else content
        return _mock_ok_response()

    raw_id = "110101199001011234"
    payload = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1",
             "content": f"查询：当事人身份证 {raw_id}"}]}],
    }

    with patch.object(server, "_http_client") as mock_client:
        mock_client.request = AsyncMock(side_effect=fake_request)
        server._current_mode = "desensitize"
        server._current_selfcheck = "remask"
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/v1/messages", json=payload,
                    headers={"x-api-key": "sk-test", "content-type": "application/json"},
                )
        finally:
            server._current_selfcheck = "remask"  # restore default

    assert resp.status_code == 200, f"remask 策略不应拦截，实际 {resp.status_code}"
    assert "body" in captured, "应转发给上游"
    assert raw_id not in captured["body"], "原始身份证号绝不能上云！"
    assert "[[SANITY_" in captured["body"], "应已脱敏为标签"
    print("\n[PASS] remask 策略下 tool_result PII 脱敏后放行")
