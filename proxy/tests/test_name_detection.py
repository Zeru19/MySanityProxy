"""
测试用例 5：jieba 智能姓名识别（与 regex 协同）

锁定:
  · 裸姓名(无角色词引导)只有开启 jieba 时才被召回;
  · token 粒度 → "高强度" 不被切碎(即便"高强"是已确认姓名);
  · 开关关闭 == 纯 regex 行为;
  · jieba 不可用时优雅降级、不崩;
  · 结构化 PII 不受影响、姓名识别正常叠加;还原可逆;
  · 服务器把 name_detection 开关正确透传到 desensitize。

注：本组用例只用「裸姓名」(无角色词，运行环境不会改写)或「运行时拼接的 PII」作输入，
刻意避开「角色词+姓名」和占位符字面量——它们会被运行环境的文件级脱敏改写而让断言失真。
"""

import json
import pytest
import httpx
from unittest.mock import AsyncMock, MagicMock, patch

import desensitizer
from rules import BUILTIN_RULES

needs_jieba = pytest.mark.skipif(not desensitizer._HAS_JIEBA, reason="jieba 未安装")


def _rules():
    return [{"name": r.name, "category": r.category,
             "pattern": r.pattern, "preserve_prefix": r.preserve_prefix}
            for r in BUILTIN_RULES]


def _mask(text, name_detect):
    reg = desensitizer.new_registry()
    return desensitizer._desensitize_text(text, _rules(), reg, name_detect=name_detect)


@needs_jieba
def test_bare_name_recalled_only_with_jieba():
    """裸姓名「张三喝了一瓶啤酒」：纯 regex 漏(无角色词)，jieba 召回。"""
    text = "张三喝了一瓶啤酒"
    plain, _ = _mask(text, False)
    jb, _ = _mask(text, True)
    assert plain == text, f"纯 regex 不应脱裸姓名：{plain!r}"
    assert "张三" not in jb and "[[SANITY_PERSON_" in jb, f"jieba 应召回裸姓名：{jb!r}"
    print("\n[PASS] 裸姓名仅在开启 jieba 时被召回")


@needs_jieba
def test_token_precision_keeps_compound_word_intact():
    """token 粒度（核心）：已确认姓名"高强"不应切碎独立词"高强度"。

    直接验证 _jieba_name_pass：name 运行时拼接以规避文件级脱敏；预置一个"高强→标签"
    的已确认映射，再对含"高强度"的文本兜底——"高强度"是单独 token，不能被替换。
    """
    reg = desensitizer.new_registry()
    name = "高" + "强"
    tag = desensitizer._make_tag("个人身份", name, reg)
    out = desensitizer._jieba_name_pass("使用高强度合金很贵", reg, {tag: name})
    assert "高强度合金" in out, f"'高强度'(独立词)不应被切碎：{out!r}"
    print("\n[PASS] token 级：已确认姓名'高强'不切碎独立词'高强度'")


@needs_jieba
def test_repeated_bare_name_swept():
    """没有角色词引导的裸姓名（即用户原始疑问里"张三喝了一瓶啤酒"那种）被 jieba 召回。"""
    text = "事后张三喝了一瓶啤酒，张三随即离开"
    plain, _ = _mask(text, False)
    jb, _ = _mask(text, True)
    assert "张三" in plain, f"纯 regex 漏掉裸姓名：{plain!r}"
    assert "张三" not in jb, f"开启 jieba 后裸姓名都应脱：{jb!r}"
    print("\n[PASS] 重复出现的裸姓名被 jieba 脱净")


@needs_jieba
def test_jieba_adds_recall_beyond_role_words():
    """'原告李四'：'原告'不是触发词('原告人'才是)，regex 漏；jieba 召回 李四。"""
    text = "原告李四到庭"
    plain, _ = _mask(text, False)
    jb, _ = _mask(text, True)
    assert plain == text
    assert "李四" not in jb and "[[SANITY_PERSON_" in jb
    print("\n[PASS] jieba 补召回角色词覆盖不到的姓名")


@needs_jieba
def test_restore_roundtrip_with_jieba():
    reg = desensitizer.new_registry()
    masked, mapping = desensitizer._desensitize_text("原告李四到庭", _rules(), reg, name_detect=True)
    assert "李四" not in masked
    assert desensitizer.restore(masked, mapping) == "原告李四到庭", "还原应逐字回到原文"
    print("\n[PASS] jieba 脱敏可被 restore 还原")


def test_toggle_off_is_pure_regex():
    """开关关闭时行为与纯 regex 完全一致（不依赖 jieba 是否安装）。"""
    text = "张三喝了一瓶啤酒"  # 无角色词
    masked, mapping = _mask(text, False)
    assert masked == text and mapping == {}
    print("\n[PASS] 关闭开关 == 纯 regex")


def test_graceful_degrade_when_jieba_missing(monkeypatch):
    """jieba 不可用时，name_detect=True 也走纯 regex、不报错。"""
    monkeypatch.setattr(desensitizer, "_HAS_JIEBA", False)
    text = "张三喝了一瓶啤酒"
    masked, mapping = _mask(text, True)
    assert masked == text and mapping == {}, "缺 jieba 应静默降级为纯 regex"
    print("\n[PASS] 缺 jieba 时优雅降级，不崩")


@needs_jieba
def test_structured_pii_unaffected_and_name_added():
    """手机号在开/关两种模式下都脱(结构化 PII 不受影响)，姓名识别只在开启时叠加。"""
    phone = "139" + "1234" + "5678"  # 运行时拼出 11 位，规避文件级脱敏
    text = phone + "，张三到场"
    a, _ = _mask(text, False)
    b, _ = _mask(text, True)
    assert phone not in a and phone not in b, "手机号在两种模式下都应脱敏"
    assert "张三" in a, "纯 regex 不脱裸姓名"
    assert "张三" not in b, "开启 jieba 后裸姓名被脱"
    print("\n[PASS] 结构化 PII 不受姓名识别影响，姓名识别正常叠加")


# ── 服务器层：开关透传到 desensitize ─────────────────────────────────────────

def _mock_ok_response():
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {"content-type": "application/json"}
    resp.content = json.dumps({"id": "m", "type": "message", "role": "assistant",
                               "content": [{"type": "text", "text": "ok"}],
                               "usage": {"input_tokens": 1, "output_tokens": 1}}).encode()
    resp.json = MagicMock(return_value=json.loads(resp.content))
    return resp


async def _post_through_proxy(name_detection: bool):
    from server import app
    import server

    captured = {}

    async def fake_request(method, url, headers=None, content=None, **kwargs):
        captured["body"] = content.decode("utf-8") if isinstance(content, bytes) else content
        return _mock_ok_response()

    payload = {"model": "claude-sonnet-4-6", "max_tokens": 50,
               "messages": [{"role": "user", "content": "张三喝了一瓶啤酒"}]}

    with patch.object(server, "_http_client") as mc:
        mc.request = AsyncMock(side_effect=fake_request)
        server._current_mode = "desensitize"
        server._current_selfcheck = "off"   # 隔离 remask，只看主脱敏
        server._name_detection = name_detection
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post("/v1/messages", json=payload,
                                         headers={"x-api-key": "sk", "content-type": "application/json"})
        finally:
            server._current_selfcheck = "remask"
            server._name_detection = True
    return resp, captured


@needs_jieba
@pytest.mark.asyncio
async def test_server_threads_name_detection_on():
    """端到端：开启时裸姓名经整个 proxy() 路径被脱。"""
    resp, captured = await _post_through_proxy(True)
    assert resp.status_code == 200
    assert "张三" not in captured["body"], "开启姓名识别后裸姓名不应上云"
    assert "[[SANITY_PERSON_" in captured["body"]
    print("\n[PASS] 服务器把姓名识别开关透传到脱敏，裸姓名未外发")


@pytest.mark.asyncio
async def test_server_name_detection_off_keeps_bare_name():
    """对照：关闭时裸姓名按纯 regex 不脱(原样转发)。"""
    resp, captured = await _post_through_proxy(False)
    assert resp.status_code == 200
    assert "张三" in captured["body"], "关闭姓名识别时裸姓名按纯 regex 不脱"
    print("\n[PASS] 关闭开关时裸姓名按纯 regex 处理（对照）")
