"""
测试用例 4：thinking/签名全程不被改写（修复 400 重试死循环）+ 流式还原行为

背景：出站自检默认档 remask 旧实现把整段 JSON 当字符串跑正则，会改掉 extended-thinking
的 signature（及 thinking 文本），导致上游验签失败 400、Claude Code 不断重试。
本组用例锁死核心不变量：
  · desensitize / remask / detect 绝不改 thinking 块与任意 signature；
  · 流式还原只动 text_delta / input_json_delta，跳过 thinking_delta / signature_delta，
    且能跨 SSE 事件拼回被劈开的标签、采集 usage；
  · 上游非 200 的流式请求回传【真实状态码】，不再用假 200 包错误体。
  · 脱敏注册表每请求隔离。
"""

import json
import pytest
import httpx
from unittest.mock import AsyncMock, MagicMock, patch

import desensitizer
from rules import BUILTIN_RULES


def _rules():
    return [{"name": r.name, "category": r.category,
             "pattern": r.pattern, "preserve_prefix": r.preserve_prefix}
            for r in BUILTIN_RULES]


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


# 一段会被旧 remask 误伤的签名：含 "护照号"(两字母+7数字) 和长数字片段
RAW_SIG = "ErcBCkgIdigits1234567abcDEF6225880212345678901xyz=="


def test_thinking_and_signature_byte_identical_through_pipeline():
    rules = _rules()
    raw_id = "110101199001011234"
    body = {
        "model": "claude-opus-4-6",
        "max_tokens": 1024,
        "messages": [
            {"role": "user", "content": "被告人张三 请分析"},
            {"role": "assistant", "content": [
                {"type": "thinking",
                 "thinking": f"用户提到当事人，手机13812345678 卡号6225880212345678901",
                 "signature": RAW_SIG},
                {"type": "tool_use", "id": "toolu_01ABC", "name": "Bash",
                 "input": {"command": "echo hi"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_01ABC",
                 "content": f"查询：当事人身份证 {raw_id}"}]},
        ],
    }

    masked, mapping = desensitizer.desensitize(body, rules)
    # 结构化脱敏不碰 thinking 块
    tb = masked["messages"][1]["content"][0]
    assert tb["thinking"] == body["messages"][1]["content"][0]["thinking"]
    assert tb["signature"] == RAW_SIG
    # tool_result 里的身份证应被脱敏
    assert raw_id not in json.dumps(masked, ensure_ascii=False)

    # 走默认 remask 全文兜底
    mb = json.dumps(masked, ensure_ascii=False).encode()
    out, extra, swept = desensitizer.remask_residual(mb, rules)
    mo = json.loads(out)

    tb2 = mo["messages"][1]["content"][0]
    assert tb2["signature"] == RAW_SIG, f"签名被改写了！-> {tb2['signature']}"
    assert tb2["thinking"] == body["messages"][1]["content"][0]["thinking"], "thinking 文本被改写了！"
    # tool_use 的 id / name 不能动（否则 tool_use/tool_result 配对断裂）
    assert mo["messages"][1]["content"][1]["id"] == "toolu_01ABC"
    assert mo["messages"][1]["content"][1]["name"] == "Bash"
    assert json.loads(out), "补脱后仍应是合法 JSON"
    print("\n[PASS] thinking / signature / tool id 全程逐字节不变")


def test_image_base64_not_corrupted_by_remask():
    """图片/二进制：image 块与 base64 数据源绝不能被脱敏改写——否则 base64 损坏、
    上游判图片非法（这正是「经代理后读图失败」的根因）。"""
    import base64
    rules = _rules()
    # 构造一段必然命中长数字/护照规则的 base64（数字串 + 双字母7数字）
    blob = base64.b64encode(b"0123456789012345678 GG1234567 " * 200).decode()
    body = {
        "model": "claude-sonnet-4-6",
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": blob}},
            {"type": "text", "text": "被告人李明，请描述这张图"},
        ]}],
    }
    masked, _ = desensitizer.desensitize(body, rules)
    assert masked["messages"][0]["content"][0]["source"]["data"] == blob, "desensitize 不应碰 base64"

    out, _, _ = desensitizer.remask_residual(json.dumps(masked, ensure_ascii=False).encode(), rules)
    mo = json.loads(out)
    assert mo["messages"][0]["content"][0]["source"]["data"] == blob, "remask 改坏了 base64 图片数据！"
    # 而同一请求里的文本 PII 仍被脱敏
    assert "李明" not in json.dumps(mo, ensure_ascii=False)
    print("\n[PASS] image/base64 数据完好，文本 PII 照常脱敏")


def test_remask_still_masks_residual_pii_outside_known_fields():
    """广覆盖不能丢：藏在顶层非常规字段的漏网 PII 仍要被补脱。"""
    rules = _rules()
    raw_id = "110101199001011234"
    mb = json.dumps({
        "model": "claude-sonnet-4-6",
        "messages": [{"role": "user", "content": "已脱敏 [[SANITY_PERSON_001]]"}],
        "weird_field": f"漏网身份证 {raw_id}",
    }, ensure_ascii=False).encode()

    out, mapping, swept = desensitizer.remask_residual(mb, rules)
    out_text = out.decode("utf-8")
    assert raw_id not in out_text, "残留身份证应被补脱"
    assert "[[SANITY_PERSON_001]]" in out_text, "既有标签不应被破坏"
    assert json.loads(out_text), "补脱后仍是合法 JSON"
    assert any(l["rule"] == "居民身份证" for l in swept)
    print("\n[PASS] remask 广覆盖能力保留（顶层字段漏网 PII 仍补脱）")


def test_detect_skips_thinking_and_signature():
    """detect_residual 不应把 thinking/签名当成泄漏（既无法处理，也不该误拦）。"""
    rules = _rules()
    body = {
        "model": "claude-sonnet-4-6",
        "messages": [{"role": "assistant", "content": [
            {"type": "thinking", "thinking": "手机13812345678", "signature": RAW_SIG},
        ]}],
    }
    leaks = desensitizer.detect_residual(body, rules)
    assert leaks == [], f"thinking/签名不应触发自检：{leaks}"
    print("\n[PASS] 自检跳过 thinking / signature")


def test_per_request_registry_isolation():
    """每请求注册表独立：两次独立脱敏各自从 001 计数，互不串号。"""
    rules = _rules()
    a, ma = desensitizer.desensitize(
        {"messages": [{"role": "user", "content": "被告人李明"}]}, rules)
    b, mb = desensitizer.desensitize(
        {"messages": [{"role": "user", "content": "被告人王芳"}]}, rules)
    assert "[[SANITY_PERSON_001]]" in json.dumps(a, ensure_ascii=False)
    assert "[[SANITY_PERSON_001]]" in json.dumps(b, ensure_ascii=False)
    assert ma["[[SANITY_PERSON_001]]"] == "李明"
    assert mb["[[SANITY_PERSON_001]]"] == "王芳"
    print("\n[PASS] 脱敏注册表每请求隔离")


# ── 流式还原 ────────────────────────────────────────────────────────────────

class _FakeStream:
    def __init__(self, status, chunks, headers=None):
        self.status_code = status
        self.headers = headers or {"content-type": "text/event-stream"}
        self._chunks = chunks

    async def aiter_bytes(self):
        for c in self._chunks:
            yield c

    async def aread(self):
        return b"".join(self._chunks)

    async def aclose(self):
        pass


def _sse(events: list[tuple[str, dict]]) -> str:
    return "".join(f"event: {name}\ndata: {json.dumps(d, ensure_ascii=False)}\n\n"
                   for name, d in events)


async def _run_stream(payload, chunks, status=200):
    from server import app
    import server

    fake = _FakeStream(status, chunks)
    with patch.object(server, "_http_client") as mc:
        mc.build_request = MagicMock(return_value="REQ")
        mc.send = AsyncMock(return_value=fake)
        server._current_mode = "desensitize"
        server._current_selfcheck = "remask"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/v1/messages", json=payload,
                headers={"x-api-key": "sk", "content-type": "application/json"})
    return resp


def _collect_deltas(sse_text: str):
    texts, thinks = [], []
    for line in sse_text.split("\n"):
        if not line.startswith("data:"):
            continue
        try:
            obj = json.loads(line[5:].lstrip())
        except Exception:
            continue
        if obj.get("type") == "content_block_delta":
            d = obj.get("delta", {})
            if d.get("type") == "text_delta":
                texts.append(d.get("text", ""))
            elif d.get("type") == "thinking_delta":
                thinks.append(d.get("thinking", ""))
    return "".join(texts), "".join(thinks)


@pytest.mark.asyncio
async def test_stream_restores_text_not_thinking_and_logs_usage():
    import storage
    payload = {"model": "claude-opus-4-6", "max_tokens": 100, "stream": True,
               "messages": [{"role": "user", "content": "被告人李明 请分析"}]}
    sse = _sse([
        ("message_start", {"type": "message_start",
                           "message": {"id": "m", "usage": {"input_tokens": 50, "output_tokens": 1}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": "你好 [[SANITY_PERSON_001]] 同志"}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("content_block_start", {"type": "content_block_start", "index": 1,
                                 "content_block": {"type": "thinking", "thinking": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 1,
                                 "delta": {"type": "thinking_delta", "thinking": "想到了[[SANITY_PERSON_001]]"}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 1,
                                 "delta": {"type": "signature_delta", "signature": "sig=="}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 1}),
        ("message_delta", {"type": "message_delta", "delta": {}, "usage": {"output_tokens": 20}}),
        ("message_stop", {"type": "message_stop"}),
    ])
    resp = await _run_stream(payload, [sse.encode("utf-8")])
    assert resp.status_code == 200
    text, think = _collect_deltas(resp.text)
    assert "李明" in text and "[[SANITY_" not in text, f"text_delta 应被还原：{text!r}"
    assert "[[SANITY_PERSON_001]]" in think, f"thinking_delta 应保持标签态：{think!r}"

    entry = storage.get_logs()[-1]
    assert entry["input_tokens"] == 50 and entry["output_tokens"] == 20, f"usage 未记录：{entry}"
    print("\n[PASS] 流式：text 还原、thinking 保持标签、usage 记录")


@pytest.mark.asyncio
async def test_stream_restores_tag_split_across_events():
    payload = {"model": "claude-opus-4-6", "max_tokens": 100, "stream": True,
               "messages": [{"role": "user", "content": "被告人李明"}]}
    sse = _sse([
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": "甲[[SANITY_PER"}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": "SON_001]]乙"}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_stop", {"type": "message_stop"}),
    ])
    # 故意把字节流切碎，模拟网络分块
    raw = sse.encode("utf-8")
    chunks = [raw[i:i + 7] for i in range(0, len(raw), 7)]
    resp = await _run_stream(payload, chunks)
    text, _ = _collect_deltas(resp.text)
    assert "甲李明乙" in text, f"跨事件拆分的标签应被拼回还原：{text!r}"
    assert "[[SANITY_" not in text
    print("\n[PASS] 流式：跨事件被劈开的标签能拼回还原")


@pytest.mark.asyncio
async def test_stream_non_200_returns_real_status():
    payload = {"model": "claude-opus-4-6", "max_tokens": 100, "stream": True,
               "messages": [{"role": "user", "content": "被告人李明"}]}
    err = json.dumps({"type": "error", "error": {"type": "invalid_request_error",
                                                  "message": "bad"}}).encode()
    fake_headers = {"content-type": "application/json"}
    from server import app
    import server
    fake = _FakeStream(400, [err], headers=fake_headers)
    with patch.object(server, "_http_client") as mc:
        mc.build_request = MagicMock(return_value="REQ")
        mc.send = AsyncMock(return_value=fake)
        server._current_mode = "desensitize"
        server._current_selfcheck = "remask"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/v1/messages", json=payload,
                headers={"x-api-key": "sk", "content-type": "application/json"})
    assert resp.status_code == 400, f"流式非 200 应回传真实状态码，实际 {resp.status_code}"
    print("\n[PASS] 流式上游 400 回传真实状态码（不再假 200 包错误体）")


# ── 端到端：图片 / base64 经整个代理路径必须原样转发 ──────────────────────────
# data 串里特意塞入会命中规则的【裸子串】（不用占位标签，免得被还原）：
#   19 位数字→银行卡、18 位大写数字→统一社会信用代码、双字母+7数字→护照。
# 证明即便 base64 里凑巧含这些，代理也不会改动 image 的 data。
_IMG_DATA = "iVBORw0KGgoAAAANSUhEUgo" + "1234567890123456789" + "ABCDEFGH012345678Y" + "Gd1234567" + "tail=="


def _image_payload(stream=False):
    return {
        "model": "claude-opus-4-6", "max_tokens": 64, **({"stream": True} if stream else {}),
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _IMG_DATA}},
            {"type": "text", "text": "请描述这张图片"},
        ]}],
    }


@pytest.mark.asyncio
async def test_image_passthrough_nonstream_server():
    """非流式：含 image 的请求经整个 proxy() 路径，转发给上游的 base64 必须逐字节不变。"""
    from server import app
    import server

    captured = {}

    async def fake_request(method, url, headers=None, content=None, **kwargs):
        captured["body"] = json.loads(content)
        return _mock_ok_response()

    with patch.object(server, "_http_client") as mc:
        mc.request = AsyncMock(side_effect=fake_request)
        server._current_mode = "desensitize"
        server._current_selfcheck = "remask"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/v1/messages", json=_image_payload(),
                                     headers={"x-api-key": "sk", "content-type": "application/json"})

    assert resp.status_code == 200
    fwd = captured["body"]["messages"][0]["content"][0]["source"]["data"]
    assert fwd == _IMG_DATA, "image base64 被代理改动了！"
    print("\n[PASS] 非流式：image base64 原样转发")


@pytest.mark.asyncio
async def test_image_passthrough_stream_server():
    """流式：含 image 的请求经 proxy() + _handle_stream，转发的 base64 必须逐字节不变。"""
    from server import app
    import server

    captured = {}

    def fake_build(method=None, url=None, headers=None, content=None, **kw):
        captured["body"] = json.loads(content)
        return "REQ"

    sse = _sse([("message_start", {"type": "message_start",
                                   "message": {"id": "m", "usage": {"input_tokens": 5, "output_tokens": 1}}}),
                ("message_stop", {"type": "message_stop"})])
    fake = _FakeStream(200, [sse.encode("utf-8")])
    with patch.object(server, "_http_client") as mc:
        mc.build_request = MagicMock(side_effect=fake_build)
        mc.send = AsyncMock(return_value=fake)
        server._current_mode = "desensitize"
        server._current_selfcheck = "remask"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/v1/messages", json=_image_payload(stream=True),
                                     headers={"x-api-key": "sk", "content-type": "application/json"})

    assert resp.status_code == 200
    fwd = captured["body"]["messages"][0]["content"][0]["source"]["data"]
    assert fwd == _IMG_DATA, "流式路径下 image base64 被改动了！"
    print("\n[PASS] 流式：image base64 原样转发")


@pytest.mark.asyncio
async def test_image_not_blocked_by_block_policy():
    """block 策略下，image 的 base64 绝不能被自检误判为泄漏而 403——必须照常转发。"""
    from server import app
    import server

    captured = {}

    async def fake_request(method, url, headers=None, content=None, **kwargs):
        captured["body"] = content
        return _mock_ok_response()

    with patch.object(server, "_http_client") as mc:
        mc.request = AsyncMock(side_effect=fake_request)
        server._current_mode = "desensitize"
        server._current_selfcheck = "block"
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post("/v1/messages", json=_image_payload(),
                                         headers={"x-api-key": "sk", "content-type": "application/json"})
        finally:
            server._current_selfcheck = "remask"

    assert resp.status_code == 200, f"block 策略误拦了 image，状态 {resp.status_code}"
    assert "body" in captured, "image 请求应被转发"
    print("\n[PASS] block 策略不再把 image base64 误判为泄漏")
