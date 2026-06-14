"""
测试用例 1：验证发往上游大模型的请求体已完全脱敏

确保原始 PII（姓名、身份证号、手机号、邮箱、案件编号）
在到达 api.anthropic.com 之前被替换为 [[SANITY_*]] 占位符，
原始数据不会离开本机。
"""

import json
import pytest
import httpx
from unittest.mock import AsyncMock, MagicMock, patch


# ── 测试数据：包含多类真实 PII 的法律文书片段 ─────────────────────────
LEGAL_DOCUMENT = (
    "被告人李明，身份证号 110101199001011234，"
    "手机 13812345678，邮箱 liming@lawcase.com，"
    "案件编号（2024）京民初第1234号，"
    "请对上述当事人信息进行法律风险评估。"
)

SENSITIVE_VALUES = [
    "李明",
    "110101199001011234",
    "13812345678",
    "liming@lawcase.com",
    "（2024）京民初第1234号",
]

REQUEST_PAYLOAD = {
    "model": "claude-sonnet-4-6",
    "max_tokens": 200,
    "messages": [{"role": "user", "content": LEGAL_DOCUMENT}],
}


@pytest.mark.asyncio
async def test_pii_stripped_from_upstream_request():
    """
    核心断言：上游收到的请求体中不包含任何原始 PII，
    且包含 [[SANITY_]] 占位符。
    """
    from server import app
    import server

    captured: dict = {}

    # 构造一个假的上游响应（不需要真实 Anthropic Key）
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"content-type": "application/json"}
    mock_resp.content = json.dumps({
        "id": "msg_test001",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "已收到脱敏数据，正在分析。"}],
        "usage": {"input_tokens": 80, "output_tokens": 15},
    }).encode()

    async def fake_request(method, url, headers=None, content=None, **kwargs):
        # 记录实际发往"上游"的请求体
        captured["method"] = method
        captured["url"] = str(url)
        captured["body"] = json.loads(content)
        return mock_resp

    with patch.object(server, "_http_client") as mock_client:
        mock_client.request = AsyncMock(side_effect=fake_request)
        server._current_mode = "desensitize"

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/v1/messages",
                json=REQUEST_PAYLOAD,
                headers={
                    "x-api-key": "sk-test-fake",
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
            )

    assert resp.status_code == 200, f"Proxy returned unexpected status: {resp.status_code}"
    assert "body" in captured, "Proxy never forwarded the request to upstream"

    upstream_text = captured["body"]["messages"][0]["content"]
    print(f"\n[上游收到] {upstream_text}")

    # ── 断言 1：所有 PII 均已从上游请求中移除 ──────────────────────────
    for value in SENSITIVE_VALUES:
        assert value not in upstream_text, (
            f"PII 泄露！上游请求中发现原始值: '{value}'\n"
            f"上游内容: {upstream_text}"
        )

    # ── 断言 2：存在脱敏标签，证明脱敏确实发生 ─────────────────────────
    assert "[[SANITY_" in upstream_text, (
        f"上游请求中未找到 [[SANITY_]] 标签，脱敏可能未生效\n"
        f"上游内容: {upstream_text}"
    )

    print("[PASS] 所有 PII 已脱敏，上游仅收到安全的标签化数据")


@pytest.mark.asyncio
async def test_system_prompt_also_desensitized():
    """
    system prompt 同样需要脱敏（常见于带背景资料的法律助手场景）。
    """
    from server import app
    import server

    captured: dict = {}

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"content-type": "application/json"}
    mock_resp.content = json.dumps({
        "id": "msg_test002",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "OK"}],
        "usage": {"input_tokens": 30, "output_tokens": 5},
    }).encode()

    async def fake_request(method, url, headers=None, content=None, **kwargs):
        captured["body"] = json.loads(content)
        return mock_resp

    payload_with_system = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 100,
        "system": "本案当事人：原告王芳，身份证 310101198505055678，联系电话 13900001234。",
        "messages": [{"role": "user", "content": "请总结案情。"}],
    }

    with patch.object(server, "_http_client") as mock_client:
        mock_client.request = AsyncMock(side_effect=fake_request)
        server._current_mode = "desensitize"

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/v1/messages",
                json=payload_with_system,
                headers={
                    "x-api-key": "sk-test-fake",
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
            )

    assert resp.status_code == 200
    upstream_system = captured["body"]["system"]
    print(f"\n[上游 system prompt] {upstream_system}")

    assert "310101198505055678" not in upstream_system, "身份证号泄露至 system prompt！"
    assert "13900001234" not in upstream_system, "手机号泄露至 system prompt！"
    assert "[[SANITY_" in upstream_system, "system prompt 未被脱敏"

    print("[PASS] system prompt 中的 PII 已正确脱敏")


@pytest.mark.asyncio
async def test_email_with_plus_tag_fully_masked():
    """
    回归测试（Issue #7）：含 "+tag" 本地部分的邮箱必须被完整脱敏。
    旧正则 [\\w.\\-]+@... 不含 '+'，会漏掉 "user+" 前缀导致 PII 泄露。
    """
    from server import app
    import server

    captured: dict = {}

    plus_email = "user" + "+" + "tag@example.com"
    payload = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": f"请联系 {plus_email} 获取资料。"}],
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"content-type": "application/json"}
    mock_resp.content = json.dumps({
        "id": "msg_test003",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "OK"}],
        "usage": {"input_tokens": 30, "output_tokens": 5},
    }).encode()

    async def fake_request(method, url, headers=None, content=None, **kwargs):
        captured["body"] = json.loads(content)
        return mock_resp

    with patch.object(server, "_http_client") as mock_client:
        mock_client.request = AsyncMock(side_effect=fake_request)
        server._current_mode = "desensitize"

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/v1/messages",
                json=payload,
                headers={
                    "x-api-key": "sk-test-fake",
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
            )

    assert resp.status_code == 200
    upstream_text = captured["body"]["messages"][0]["content"]
    print(f"\n[上游收到] {upstream_text}")

    # 断言 1：原始邮箱（含 "user+" 前缀）完全不出现在上游请求中
    assert plus_email not in upstream_text, (
        f"邮箱泄露！上游请求中发现原始值: '{plus_email}'\n上游内容: {upstream_text}"
    )
    assert "user+" not in upstream_text, (
        f"邮箱本地部分前缀 'user+' 泄露至上游\n上游内容: {upstream_text}"
    )

    # 断言 2：存在 CONTACT 类标签，证明邮箱已被识别并标签化
    assert "[[SANITY_CONTACT_" in upstream_text, (
        f"邮箱未被脱敏，未找到 [[SANITY_CONTACT_]] 标签\n上游内容: {upstream_text}"
    )

    print("[PASS] 含 '+tag' 的邮箱已完整脱敏")
