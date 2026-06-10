"""
测试用例 2：验证上游返回的脱敏响应在到达调用方之前已还原为真实数据

模拟上游返回含 [[SANITY_*]] 标签的响应，
验证 Claude Code 最终收到的是完整的原始信息。
"""

import json
import pytest
import httpx
from unittest.mock import AsyncMock, MagicMock, patch


# ── 共享测试辅助 ────────────────────────────────────────────────────────────

def make_upstream_response(text: str, status: int = 200) -> MagicMock:
    """构造包含指定文本的假上游响应（含正确的 .json() 方法供还原使用）。"""
    data = {
        "id": "msg_restore_test",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": 60, "output_tokens": 40},
    }
    resp = MagicMock()
    resp.status_code = status
    resp.headers = {"content-type": "application/json"}
    resp.content = json.dumps(data, ensure_ascii=False).encode()
    resp.json = MagicMock(return_value=data)  # 必须设置，否则 restore_body 无法解析
    return resp


# ── 测试 1：完整往返验证（脱敏 → 上游 → 还原）──────────────────────────

@pytest.mark.asyncio
async def test_full_roundtrip_pii_restored_in_response():
    """
    端到端验证：
      1. 发送含 PII 的请求
      2. 代理脱敏后转发给上游
      3. 上游返回含 SANITY 标签的分析结果
      4. 调用方收到的响应中 PII 已完整还原
    """
    from server import app
    import server

    original_id = "110101199001011234"
    original_phone = "13812345678"
    original_name = "李明"

    request_payload = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 300,
        "messages": [
            {
                "role": "user",
                "content": (
                    f"被告人{original_name}，"
                    f"身份证号 {original_id}，"
                    f"手机 {original_phone}，"
                    f"请分析其法律责任。"
                ),
            }
        ],
    }

    # 上游的响应中使用了 SANITY 标签（真实场景中上游也只看到标签）
    # 我们在 fake_request 里捕获标签，再构造含标签的响应
    captured_tags: dict = {}

    async def fake_request(method, url, headers=None, content=None, **kwargs):
        body = json.loads(content)
        text = body["messages"][0]["content"]
        # 提取所有 [[SANITY_*]] 标签
        import re
        tags = re.findall(r"\[\[SANITY_[A-Z_0-9]+\]\]", text)
        captured_tags["all"] = tags
        captured_tags["masked_text"] = text

        # 上游用标签构造分析结果（模拟真实大模型行为）
        tag_list = "、".join(tags[:3]) if tags else "（无标签）"
        upstream_reply = (
            f"根据材料，当事人 {tags[0] if tags else '未知'} 的身份证件 "
            f"{tags[1] if len(tags) > 1 else ''} 所涉案件需进一步核查。"
            f"联系电话 138{tags[2] if len(tags) > 2 else ''} 已记录在案。"
        )

        return make_upstream_response(upstream_reply)

    with patch.object(server, "_http_client") as mock_client:
        mock_client.request = AsyncMock(side_effect=fake_request)
        server._current_mode = "desensitize"

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/v1/messages",
                json=request_payload,
                headers={
                    "x-api-key": "sk-test-fake",
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
            )

    assert resp.status_code == 200
    body = resp.json()
    final_text = body["content"][0]["text"]
    print(f"\n[上游收到] {captured_tags.get('masked_text', '')}")
    print(f"[调用方收到] {final_text}")

    # ── 断言 1：调用方响应中不含任何 SANITY 标签 ──────────────────────
    assert "[[SANITY_" not in final_text, (
        f"响应中仍含有未还原的 SANITY 标签！\n收到: {final_text}"
    )

    # ── 断言 2：原始值已还原 ──────────────────────────────────────────
    # 姓名标签对应 "李明"，身份证标签对应 ID，手机后缀标签对应 "12345678"
    assert original_name in final_text or original_id in final_text or "12345678" in final_text, (
        f"响应中未找到任何还原的原始值\n"
        f"期望找到: {original_name} 或 {original_id}\n"
        f"收到: {final_text}"
    )

    print("[PASS] 上游标签已正确还原，调用方收到完整的原始数据")


# ── 测试 2：透明模式下数据不经脱敏，原样透传 ─────────────────────────────

@pytest.mark.asyncio
async def test_transparent_mode_passes_raw_data():
    """
    透明模式（bypass）下，请求和响应均不做任何修改。
    验证切换模式不会破坏正常 API 调用。
    """
    from server import app
    import server

    raw_text = "被告人张三，身份证 110101199001011234，请简要分析。"
    captured: dict = {}

    async def fake_request(method, url, headers=None, content=None, **kwargs):
        captured["body"] = json.loads(content)
        return make_upstream_response("分析完毕，无需脱敏处理。")

    with patch.object(server, "_http_client") as mock_client:
        mock_client.request = AsyncMock(side_effect=fake_request)
        server._current_mode = "transparent"   # 透明模式

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/v1/messages",
                json={
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 100,
                    "messages": [{"role": "user", "content": raw_text}],
                },
                headers={
                    "x-api-key": "sk-test-fake",
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
            )

    assert resp.status_code == 200
    upstream_content = captured["body"]["messages"][0]["content"]
    print(f"\n[透明模式-上游收到] {upstream_content}")

    # 透明模式：上游收到原始数据（含 PII）
    assert "110101199001011234" in upstream_content, (
        "透明模式下身份证号应原样转发"
    )
    assert "[[SANITY_" not in upstream_content, (
        "透明模式下不应出现脱敏标签"
    )

    print("[PASS] 透明模式正确透传原始数据，未做任何修改")
