import copy
import json
from typing import Any, Optional

try:
    import regex as re  # supports variable-length lookbehind
except ImportError:
    import re

# Canonical category key for tag naming
_CATEGORY_KEYS = {
    "个人身份": "PERSON",
    "联系方式": "CONTACT",
    "金融信息": "FINANCE",
    "机构信息": "ORG",
    "司法信息": "LEGAL",
    "其他": "OTHER",
}


def _new_registry() -> dict:
    """每请求一份脱敏注册表。

    刻意不再用模块级全局：① 全局表永不清空，长跑会无界增长且把【全部原始 PII】
    常驻进程内存——隐私代理的硬伤；② 并发请求共享同一张表会相互污染标签编号、串号。
    每请求一份既保证「同值同标签」的请求内一致性，又让原文随请求结束即可回收。
    还原走每请求的 session_mapping，不依赖此表，故隔离不影响还原。
    """
    return {"value_to_tag": {}, "tag_to_value": {}, "counters": {}}


# 公开别名：调用方（server）每请求建一份，贯穿 desensitize→remask 两趟，
# 既保持请求内「同值同标签、编号连续」，又随请求结束回收（隔离并发、不常驻 PII）。
new_registry = _new_registry


def _make_tag(category: str, value: str, reg: dict) -> str:
    value_to_tag = reg["value_to_tag"]
    if value in value_to_tag:
        return value_to_tag[value]
    key = _CATEGORY_KEYS.get(category, "DATA")
    reg["counters"][key] = reg["counters"].get(key, 0) + 1
    tag = f"[[SANITY_{key}_{reg['counters'][key]:03d}]]"
    value_to_tag[value] = tag
    reg["tag_to_value"][tag] = value
    return tag


def _desensitize_text(text: str, rules: list[dict], reg: dict) -> tuple[str, dict[str, str]]:
    """Apply all rules to text. Returns (masked_text, local_mapping{tag:value})."""
    local_mapping: dict[str, str] = {}
    result = text
    for rule in rules:
        preserve = rule.get("preserve_prefix", 0)
        pattern = rule["pattern"]
        category = rule["category"]
        try:
            def replacer(m: "re.Match", _preserve=preserve, _cat=category) -> str:
                original = m.group(0)
                if _preserve and len(original) > _preserve:
                    prefix = original[:_preserve]
                    rest = original[_preserve:]
                    tag = _make_tag(_cat, rest, reg)
                    local_mapping[tag] = rest
                    return prefix + tag
                else:
                    tag = _make_tag(_cat, original, reg)
                    local_mapping[tag] = original
                    return tag
            result = re.sub(pattern, replacer, result)
        except re.error:
            continue
    return result, local_mapping


def _walk_strings(obj: Any, base_path: list) -> list[tuple[list, str]]:
    """递归取出任意 JSON 结构里的所有字符串，返回 (path, text) 对。用于 tool_use 入参。"""
    out: list[tuple[list, str]] = []
    if isinstance(obj, str):
        out.append((base_path, obj))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            out.extend(_walk_strings(v, base_path + [k]))
    elif isinstance(obj, list):
        for idx, v in enumerate(obj):
            out.extend(_walk_strings(v, base_path + [idx]))
    return out


def _extract_text_from_content(content: Any) -> list[tuple[list, str]]:
    """
    取出一条消息 content 里【所有承载用户数据、且会上云】的字符串，返回 (path, text) 对。
    path 相对于 content 本身，可直接喂给 _set_nested 写回。

    覆盖：纯字符串 content、text 块、tool_result（字符串或嵌套 text 块）、
    tool_use 入参（input 内全部字符串）、document 文本源。
    只取内容字符串，绝不碰结构性键（type/tool_use_id/id/name 等），
    也绝不碰 thinking / signature（模型自有产物，改动即破坏签名）。
    """
    segments: list[tuple[list, str]] = []
    if isinstance(content, str):
        segments.append(([], content))
    elif isinstance(content, list):
        for i, block in enumerate(content):
            if not isinstance(block, dict):
                if isinstance(block, str):
                    segments.append(([i], block))
                continue
            btype = block.get("type")
            if btype == "text" and isinstance(block.get("text"), str):
                segments.append(([i, "text"], block["text"]))
            elif btype == "tool_result":
                inner = block.get("content")
                if isinstance(inner, str):
                    segments.append(([i, "content"], inner))
                elif isinstance(inner, list):
                    for j, b in enumerate(inner):
                        if isinstance(b, str):
                            segments.append(([i, "content", j], b))
                        elif isinstance(b, dict) and b.get("type") == "text" \
                                and isinstance(b.get("text"), str):
                            segments.append(([i, "content", j, "text"], b["text"]))
            elif btype == "tool_use" and isinstance(block.get("input"), (dict, list)):
                segments.extend(_walk_strings(block["input"], [i, "input"]))
            elif btype == "document":
                source = block.get("source")
                if isinstance(source, dict) and source.get("type") == "text" \
                        and isinstance(source.get("data"), str):
                    segments.append(([i, "source", "data"], source["data"]))
    return segments


def _set_nested(obj: Any, path: list, value: Any):
    for key in path[:-1]:
        obj = obj[key]
    obj[path[-1]] = value


def desensitize(body: dict, rules: list[dict], reg: Optional[dict] = None) -> tuple[dict, dict[str, str]]:
    """
    Desensitize all message content in a Messages API request body.
    Returns (masked_body, combined_local_mapping).

    reg：每请求脱敏注册表；调用方可传入同一份给后续 remask_residual，确保两趟脱敏
    标签编号连续、同值同标签（不传则内部新建一份）。
    """
    masked = copy.deepcopy(body)
    if reg is None:
        reg = _new_registry()
    combined_mapping: dict[str, str] = {}

    messages = masked.get("messages", [])
    for msg in messages:
        content = msg.get("content")
        if content is None:
            continue
        segments = _extract_text_from_content(content)
        for path, text in segments:
            masked_text, local = _desensitize_text(text, rules, reg)
            combined_mapping.update(local)
            if path:
                _set_nested(msg["content"], path, masked_text)
            else:
                msg["content"] = masked_text

    # Also desensitize the system prompt if present
    system = masked.get("system")
    if isinstance(system, str):
        masked_system, local = _desensitize_text(system, rules, reg)
        combined_mapping.update(local)
        masked["system"] = masked_system
    elif isinstance(system, list):
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                masked_text, local = _desensitize_text(block["text"], rules, reg)
                combined_mapping.update(local)
                block["text"] = masked_text

    return masked, combined_mapping


_TAG_RE = re.compile(r"\[\[SANITY_[A-Z]+_\d+\]\]")


# 框架字段：合法地携带示例邮箱 / 长数字 / 模型名（工具 schema、metadata id、
# model 名称），不是用户 PII，扫它们只会假阳性。自检/补脱在顶层跳过这几个。
_FRAMEWORK_KEYS = frozenset({"tools", "model", "metadata"})

# 结构性字段：API 生成的标识或枚举，绝非用户 PII，且改动会破坏请求语义
# （签名失效、tool_use/tool_result 配对断裂等）。脱敏/自检/补脱/还原一律不碰。
_STRUCTURAL_KEYS = frozenset({"signature", "tool_use_id", "id", "type", "cache_control"})

# 模型自有产物：thinking / redacted_thinking 块连同其内部 signature，必须逐字节往返，
# 否则上游验签失败返回 400。整块跳过（既不脱敏、不自检，也不还原）。
_PROTECTED_BLOCK_TYPES = frozenset({"thinking", "redacted_thinking"})


def _walk_maskable(obj: Any, base_path: list, skip_top_keys: frozenset = frozenset()) -> list[tuple[list, str]]:
    """递归取出所有【可安全改写】的字符串 (path, text)，作为自检/补脱/还原的作用面。

    与 _walk_strings 的区别：跳过绝不能动的字段——
      · thinking / redacted_thinking 整块（含 signature 与 thinking 文本）
      · 任意位置的结构性键（signature / tool_use_id / id / type / cache_control）
      · tool_use 块的 name（工具名须与 tools 定义一致），但照常遍历其 input
      · 顶层的 skip_top_keys（请求侧传入框架字段 tools/model/metadata）

    采用「广覆盖 - 排除受保护字段」而非「只取 messages+system」：既能兜住结构化脱敏
    够不到的位置（未知 content 块、顶层非常规字段）里的漏网 PII，又不会改坏结构字段。
    """
    out: list[tuple[list, str]] = []
    if isinstance(obj, str):
        out.append((base_path, obj))
    elif isinstance(obj, dict):
        if obj.get("type") in _PROTECTED_BLOCK_TYPES:
            return out
        is_tool_use = obj.get("type") == "tool_use"
        for k, v in obj.items():
            if k in _STRUCTURAL_KEYS:
                continue
            if is_tool_use and k == "name":
                continue
            if not base_path and k in skip_top_keys:
                continue
            out.extend(_walk_maskable(v, base_path + [k], skip_top_keys))
    elif isinstance(obj, list):
        for idx, v in enumerate(obj):
            out.extend(_walk_maskable(v, base_path + [idx], skip_top_keys))
    return out


def _scan_hits(texts: list[str], rules: list[dict]) -> list[dict]:
    """在【去标签后】的文本集合上用全部规则扫一遍，返回命中列表 [{rule,category,sample}]。

    用空格替换占位符（而非删除）：既避免标签里的数字被误判，又切断
    "当事人[[TAG]]法律风险" 去标签后关键词与后文相邻、再次触发姓名等
    lookbehind 规则的假阳性。真正漏脱的 PII 不含标签，不受影响。
    每个规则至多记一条（样本部分遮挡，避免审计再次泄露完整原文）。
    """
    scan_text = _TAG_RE.sub(" ", "\n".join(texts))
    leaks: list[dict] = []
    for rule in rules:
        pattern = rule.get("pattern", "")
        try:
            m = re.search(pattern, scan_text)
        except re.error:
            continue
        if m:
            sample = m.group(0)
            if len(sample) > 4:
                sample = sample[:2] + "***" + sample[-2:]
            leaks.append({
                "rule": rule.get("name", "?"),
                "category": rule.get("category", ""),
                "sample": sample,
            })
    return leaks


def detect_residual(masked_body: dict, rules: list[dict]) -> list[dict]:
    """
    出站零泄漏自检（fail-closed 的判定依据）。

    对【脱敏后】请求体里所有【可改写】的字符串（见 _walk_maskable：广覆盖但跳过
    thinking/签名/结构字段与框架字段），去掉所有 [[SANITY_*]] 占位符后用全部规则再扫。
    任何命中都意味着 desensitize() 漏脱了某段 PII——此时拦截（fail-closed 兜底）。

    返回残留命中列表 [{rule, category, sample}]；为空表示自检通过。
    """
    if not isinstance(masked_body, dict):
        # 无法判定安全性，按"有泄漏"处理（fail-closed）
        return [{"rule": "decode_error", "category": "", "sample": ""}]
    texts = [text for _path, text in _walk_maskable(masked_body, [], _FRAMEWORK_KEYS)]
    return _scan_hits(texts, rules)


def remask_residual(
    masked_body_bytes: bytes, rules: list[dict], reg: Optional[dict] = None
) -> tuple[bytes, dict[str, str], list[dict]]:
    """补脱（出站自检的「补脱后放行」档，detect_residual 的可选替代）。

    对【已结构化脱敏】的请求体做一次广覆盖兜底：遍历所有【可改写】字符串
    （_walk_maskable：跳过 thinking/签名/结构字段与框架字段），任何仍命中规则的残留
    PII 都就地替换为 [[SANITY_*]] 标签，再照常转发——既保证残留不出本机，又不拦截请求。

    关键：采用结构化遍历 + 逐字段写回，而非把整段 JSON 当字符串替换。后者会把
    thinking 签名、tool_use_id 等结构串里形似 PII 的片段也改掉，导致上游 400（本次修复点）。

    返回 (补脱后的 body bytes, 新增映射 {tag:value}, 补脱命中 [{rule,category,sample}])。
    """
    try:
        body = json.loads(masked_body_bytes)
    except Exception:
        # 解析失败：无法结构化补脱，原样返回 + 一条说明，由调用方决定是否记录告警
        return masked_body_bytes, {}, [{"rule": "decode_error", "category": "", "sample": ""}]
    if not isinstance(body, dict):
        return masked_body_bytes, {}, []

    if reg is None:
        reg = _new_registry()
    mapping: dict[str, str] = {}
    segments = _walk_maskable(body, [], _FRAMEWORK_KEYS)

    # 先在去标签文本上「识别」命中（带规则名，供审计展示）
    swept = _scan_hits([text for _p, text in segments], rules)

    # 再逐字段就地补脱（保留既有标签，规则不会匹配标签本身）
    for path, text in segments:
        if not text:
            continue
        masked_text, local = _desensitize_text(text, rules, reg)
        if masked_text != text:
            mapping.update(local)
            _set_nested(body, path, masked_text)

    out = json.dumps(body, ensure_ascii=False).encode("utf-8")
    return out, mapping, swept


def restore(text: str, mapping: dict[str, str]) -> str:
    """Replace all [[SANITY_*]] tags back to original values using the session mapping."""
    result = text
    # Sort by tag length descending to avoid partial replacements
    for tag, original in sorted(mapping.items(), key=lambda x: -len(x[0])):
        result = result.replace(tag, original)
    return result


def restore_body(body: Any, mapping: dict[str, str]) -> Any:
    """还原响应体里【用户可见内容】的标签（结构化遍历）。

    刻意跳过 thinking/redacted_thinking 块与 signature——这些是模型自有产物，
    若把其中标签还原成真实 PII，下一轮被原样发回时与（基于标签版计算的）签名不符，
    会触发上游 400。让 thinking 在整条会话里保持标签态，签名因此恒有效。
    """
    if not mapping or not isinstance(body, (dict, list)):
        return body
    for path, text in _walk_maskable(body, []):
        if "[[SANITY_" in text:
            restored = restore(text, mapping)
            if restored != text:
                _set_nested(body, path, restored)
    return body


def test_rules(text: str, rules: list[dict]) -> dict:
    """Test desensitization on sample text, return masked text and mapping."""
    reg = _new_registry()
    masked, mapping = _desensitize_text(text, rules, reg)
    return {"masked": masked, "mapping": {tag: val for tag, val in mapping.items()}}
