"""多上游 model 路由。

把请求体里的 `model` 字段映射到某个「上游」（Anthropic 兼容端点），返回它的 base_url、
鉴权方式与（从环境变量读出的）token。生效配置 = config 默认 ∪ DB 自定义：
  - 内置上游/路由来自 config.UPSTREAMS / config.ROUTES（活的默认）；
  - 面板新增的自定义上游/路由存 SQLite（storage），同名自定义上游覆盖内置；
  - 内置的启用/禁用记在 settings 的 JSON 集合里。

凭证只来自环境变量（token_env 指向的变量名），绝不落库、不写日志/快照。
"""
from __future__ import annotations

import fnmatch
import json
import os
from dataclasses import dataclass
from typing import Optional

import config
import storage

_DISABLED_UPSTREAMS_KEY = "disabled_upstreams"  # settings: JSON list of builtin upstream names
_DISABLED_ROUTES_KEY = "disabled_routes"        # settings: JSON list of builtin route names
_DEFAULT_UPSTREAM_KEY = "default_upstream"


@dataclass
class ResolvedUpstream:
    name: str
    base_url: str
    auth_scheme: str            # "x-api-key" | "bearer"
    token: Optional[str]        # 仅在内存；None 表示透传客户端原鉴权头
    model_rewrite: Optional[str]
    supports_count_tokens: bool


async def _get_json_list(key: str) -> list:
    raw = await storage.get_setting(key, "")
    if not raw:
        return []
    try:
        val = json.loads(raw)
        return val if isinstance(val, list) else []
    except Exception:
        return []


async def get_default_upstream() -> str:
    return await storage.get_setting(_DEFAULT_UPSTREAM_KEY, config.DEFAULT_UPSTREAM)


async def set_default_upstream(name: str) -> str:
    await storage.set_setting(_DEFAULT_UPSTREAM_KEY, name)
    return name


async def set_builtin_enabled(kind: str, name: str, enabled: bool) -> None:
    """启用/禁用一个内置上游或路由（记录到 settings 的 disabled 集合）。kind: 'upstream'|'route'."""
    key = _DISABLED_UPSTREAMS_KEY if kind == "upstream" else _DISABLED_ROUTES_KEY
    disabled = set(await _get_json_list(key))
    if enabled:
        disabled.discard(name)
    else:
        disabled.add(name)
    await storage.set_setting(key, json.dumps(sorted(disabled)))


async def effective_upstreams() -> dict[str, dict]:
    """按 name 返回生效上游：内置（含禁用态）被同名自定义覆盖。"""
    disabled = set(await _get_json_list(_DISABLED_UPSTREAMS_KEY))
    result: dict[str, dict] = {}
    for u in config.UPSTREAMS:
        result[u["name"]] = {
            "name": u["name"],
            "base_url": u["base_url"],
            "auth_scheme": u["auth_scheme"],
            "token_env": u.get("token_env", ""),
            "supports_count_tokens": bool(u.get("supports_count_tokens", False)),
            "enabled": u["name"] not in disabled,
            "builtin": True,
            "id": None,
        }
    for u in await storage.get_custom_upstreams():
        result[u["name"]] = {
            "name": u["name"],
            "base_url": u["base_url"],
            "auth_scheme": u["auth_scheme"],
            "token_env": u["token_env"] or "",
            "supports_count_tokens": bool(u["supports_count_tokens"]),
            "enabled": bool(u["enabled"]),
            "builtin": False,
            "id": u["id"],
        }
    return result


async def effective_routes() -> list[dict]:
    """生效路由（有序）：自定义在前（可覆盖内置），内置在后。首条命中。"""
    disabled = set(await _get_json_list(_DISABLED_ROUTES_KEY))
    routes: list[dict] = []
    for r in await storage.get_custom_routes():
        routes.append({
            "name": r["name"], "match": r["match"], "upstream": r["upstream"],
            "model_rewrite": r["model_rewrite"], "priority": r["priority"],
            "enabled": bool(r["enabled"]), "builtin": False, "id": r["id"],
        })
    for r in config.ROUTES:
        routes.append({
            "name": r["name"], "match": r["match"], "upstream": r["upstream"],
            "model_rewrite": r.get("model_rewrite"), "priority": r.get("priority", 100),
            "enabled": r["name"] not in disabled, "builtin": True, "id": None,
        })
    return routes


def _resolve_token(upstream: dict) -> Optional[str]:
    env = upstream.get("token_env") or ""
    return (os.getenv(env) or None) if env else None


async def resolve(model_id: Optional[str]) -> ResolvedUpstream:
    """按 model 选上游。无命中则回退默认上游；连默认都不可用时回退 config.UPSTREAM_URL。"""
    ups = await effective_upstreams()
    routes = await effective_routes()
    model = model_id or ""

    chosen: Optional[dict] = None
    model_rewrite: Optional[str] = None
    for r in routes:
        if not r["enabled"]:
            continue
        if fnmatch.fnmatch(model, r["match"]):
            u = ups.get(r["upstream"])
            if u and u["enabled"]:
                chosen = u
                model_rewrite = r["model_rewrite"]
                break

    if chosen is None:
        default_name = await get_default_upstream()
        u = ups.get(default_name)
        if u and u["enabled"]:
            chosen = u

    if chosen is None:
        # 最后兜底：把 config.UPSTREAM_URL 当 anthropic 端点透传（保持历史行为）
        chosen = {
            "name": "anthropic", "base_url": config.UPSTREAM_URL,
            "auth_scheme": "x-api-key", "token_env": "ANTHROPIC_API_KEY",
            "supports_count_tokens": True,
        }

    return ResolvedUpstream(
        name=chosen["name"],
        base_url=chosen["base_url"].rstrip("/"),
        auth_scheme=chosen["auth_scheme"],
        token=_resolve_token(chosen),
        model_rewrite=model_rewrite,
        supports_count_tokens=bool(chosen.get("supports_count_tokens", False)),
    )
