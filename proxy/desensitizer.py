import copy
import json
from typing import Any

try:
    import regex as re  # supports variable-length lookbehind
except ImportError:
    import re

# Global registry: original_value -> tag (ensures same value gets same tag across calls)
_value_to_tag: dict[str, str] = {}
_tag_to_value: dict[str, str] = {}
_category_counters: dict[str, int] = {}

# Canonical category key for tag naming
_CATEGORY_KEYS = {
    "个人身份": "PERSON",
    "联系方式": "CONTACT",
    "金融信息": "FINANCE",
    "机构信息": "ORG",
    "司法信息": "LEGAL",
    "其他": "OTHER",
}


def _make_tag(category: str, value: str) -> str:
    if value in _value_to_tag:
        return _value_to_tag[value]
    key = _CATEGORY_KEYS.get(category, "DATA")
    _category_counters[key] = _category_counters.get(key, 0) + 1
    tag = f"[[SANITY_{key}_{_category_counters[key]:03d}]]"
    _value_to_tag[value] = tag
    _tag_to_value[tag] = value
    return tag


def _desensitize_text(text: str, rules: list[dict]) -> tuple[str, dict[str, str]]:
    """Apply all rules to text. Returns (masked_text, local_mapping{tag:value})."""
    local_mapping: dict[str, str] = {}
    result = text
    for rule in rules:
        preserve = rule.get("preserve_prefix", 0)
        pattern = rule["pattern"]
        category = rule["category"]
        try:
            def replacer(m: re.Match, _preserve=preserve, _cat=category) -> str:
                original = m.group(0)
                if _preserve and len(original) > _preserve:
                    prefix = original[:_preserve]
                    rest = original[_preserve:]
                    tag = _make_tag(_cat, rest)
                    local_mapping[tag] = rest
                    return prefix + tag
                else:
                    tag = _make_tag(_cat, original)
                    local_mapping[tag] = original
                    return tag
            result = re.sub(pattern, replacer, result)
        except re.error:
            continue
    return result, local_mapping


def _extract_text_from_content(content: Any) -> list[tuple[list, str]]:
    """Extract all text segments from an Anthropic content block, returning (path, text) pairs."""
    segments = []
    if isinstance(content, str):
        segments.append(([], content))
    elif isinstance(content, list):
        for i, block in enumerate(content):
            if isinstance(block, dict) and block.get("type") == "text":
                segments.append(([i, "text"], block["text"]))
    return segments


def _set_nested(obj: Any, path: list, value: Any):
    for key in path[:-1]:
        obj = obj[key]
    obj[path[-1]] = value


def desensitize(body: dict, rules: list[dict]) -> tuple[dict, dict[str, str]]:
    """
    Desensitize all message content in a Messages API request body.
    Returns (masked_body, combined_local_mapping).
    """
    masked = copy.deepcopy(body)
    combined_mapping: dict[str, str] = {}

    messages = masked.get("messages", [])
    for msg in messages:
        content = msg.get("content")
        if content is None:
            continue
        segments = _extract_text_from_content(content)
        for path, text in segments:
            masked_text, local = _desensitize_text(text, rules)
            combined_mapping.update(local)
            if path:
                _set_nested(msg["content"], path, masked_text)
            else:
                msg["content"] = masked_text

    # Also desensitize the system prompt if present
    system = masked.get("system")
    if isinstance(system, str):
        masked_system, local = _desensitize_text(system, rules)
        combined_mapping.update(local)
        masked["system"] = masked_system
    elif isinstance(system, list):
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                masked_text, local = _desensitize_text(block["text"], rules)
                combined_mapping.update(local)
                block["text"] = masked_text

    return masked, combined_mapping


def restore(text: str, mapping: dict[str, str]) -> str:
    """Replace all [[SANITY_*]] tags back to original values using the session mapping."""
    result = text
    # Sort by tag length descending to avoid partial replacements
    for tag, original in sorted(mapping.items(), key=lambda x: -len(x[0])):
        result = result.replace(tag, original)
    return result


def restore_body(body: dict, mapping: dict[str, str]) -> dict:
    """Restore all content in a response body."""
    text = json.dumps(body, ensure_ascii=False)
    restored = restore(text, mapping)
    try:
        return json.loads(restored)
    except json.JSONDecodeError:
        return body


def clear_global_registry():
    """Reset global mappings (useful for testing)."""
    _value_to_tag.clear()
    _tag_to_value.clear()
    _category_counters.clear()


def test_rules(text: str, rules: list[dict]) -> dict:
    """Test desensitization on sample text, return masked text and mapping."""
    masked, mapping = _desensitize_text(text, rules)
    return {"masked": masked, "mapping": {tag: val for tag, val in mapping.items()}}
