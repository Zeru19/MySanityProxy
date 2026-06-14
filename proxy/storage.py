from __future__ import annotations

import asyncio
import aiosqlite
import json
from collections import deque
from typing import Any, Optional

from config import DB_PATH, LOG_CAPACITY
from rules import BUILTIN_RULES


_db: Optional[aiosqlite.Connection] = None
_log_buffer: deque = deque(maxlen=LOG_CAPACITY)
_log_subscribers: list[asyncio.Queue] = []

# 出站审计快照缓冲。容量可在面板配置：20 / 100 / 200 / 500 / all（不限）
_SNAPSHOT_CHOICES = {"20": 20, "100": 100, "200": 200, "500": 500, "all": None}
_snapshot_capacity_label = "100"
_snapshot_buffer: deque = deque(maxlen=100)


async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is None:
        _db = await aiosqlite.connect(DB_PATH)
        _db.row_factory = aiosqlite.Row
        await _init_schema()
    return _db


async def _init_schema():
    db = await get_db()
    await db.execute("""
        CREATE TABLE IF NOT EXISTS rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            pattern TEXT NOT NULL,
            preserve_prefix INTEGER DEFAULT 0,
            enabled INTEGER DEFAULT 1,
            builtin INTEGER DEFAULT 0
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    # 多上游路由：仅存「自定义」上游/路由（面板新增或对内置的覆盖）。
    # 注意：绝不存 API key 本身——只存 token_env（环境变量名），key 运行时从 env 读。
    await db.execute("""
        CREATE TABLE IF NOT EXISTS upstreams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            base_url TEXT NOT NULL,
            auth_scheme TEXT NOT NULL DEFAULT 'x-api-key',
            token_env TEXT NOT NULL DEFAULT '',
            supports_count_tokens INTEGER DEFAULT 0,
            enabled INTEGER DEFAULT 1
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS routes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            match TEXT NOT NULL,
            upstream TEXT NOT NULL,
            model_rewrite TEXT,
            priority INTEGER DEFAULT 0,
            enabled INTEGER DEFAULT 1
        )
    """)
    count = await db.execute_fetchall("SELECT COUNT(*) as c FROM rules WHERE builtin=1")
    if count[0]["c"] == 0:
        await db.executemany(
            "INSERT INTO rules (name, category, pattern, preserve_prefix, enabled, builtin) VALUES (?,?,?,?,1,1)",
            [(r.name, r.category, r.pattern, r.preserve_prefix) for r in BUILTIN_RULES],
        )
    else:
        # 内置规则已存在：把代码里的最新正则同步进库（如案号规则加固），
        # 但保留用户对启用/停用状态的设置。
        for r in BUILTIN_RULES:
            await db.execute(
                "UPDATE rules SET pattern=?, category=?, preserve_prefix=? WHERE builtin=1 AND name=?",
                (r.pattern, r.category, r.preserve_prefix, r.name),
            )
    await db.commit()

    # 载入已保存的快照容量设置
    global _snapshot_capacity_label, _snapshot_buffer
    saved = await get_setting("snapshot_capacity", _snapshot_capacity_label)
    if saved in _SNAPSHOT_CHOICES:
        _snapshot_capacity_label = saved
        _snapshot_buffer = deque(_snapshot_buffer, maxlen=_SNAPSHOT_CHOICES[saved])


async def get_all_rules() -> list[dict]:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id, name, category, pattern, preserve_prefix, enabled, builtin FROM rules ORDER BY builtin DESC, id"
    )
    return [dict(r) for r in rows]


async def get_enabled_rules() -> list[dict]:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id, name, category, pattern, preserve_prefix FROM rules WHERE enabled=1 ORDER BY builtin DESC, id"
    )
    return [dict(r) for r in rows]


async def create_rule(name: str, category: str, pattern: str, preserve_prefix: int) -> dict:
    db = await get_db()
    cur = await db.execute(
        "INSERT INTO rules (name, category, pattern, preserve_prefix, enabled, builtin) VALUES (?,?,?,?,1,0)",
        (name, category, pattern, preserve_prefix),
    )
    await db.commit()
    row = await db.execute_fetchall("SELECT * FROM rules WHERE id=?", (cur.lastrowid,))
    return dict(row[0])


async def update_rule(rule_id: int, **kwargs) -> Optional[dict]:
    db = await get_db()
    allowed = {"name", "category", "pattern", "preserve_prefix", "enabled"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return None
    set_clause = ", ".join(f"{k}=?" for k in fields)
    await db.execute(
        f"UPDATE rules SET {set_clause} WHERE id=?",
        (*fields.values(), rule_id),
    )
    await db.commit()
    rows = await db.execute_fetchall("SELECT * FROM rules WHERE id=?", (rule_id,))
    return dict(rows[0]) if rows else None


async def delete_rule(rule_id: int) -> bool:
    db = await get_db()
    await db.execute("DELETE FROM rules WHERE id=? AND builtin=0", (rule_id,))
    await db.commit()
    return True


async def get_setting(key: str, default: str = "") -> str:
    db = await get_db()
    rows = await db.execute_fetchall("SELECT value FROM settings WHERE key=?", (key,))
    return rows[0]["value"] if rows else default


async def set_setting(key: str, value: str):
    db = await get_db()
    await db.execute(
        "INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    await db.commit()


# ── 多上游路由（自定义上游 / 路由）─────────────────────────────────────────────
# 仅存自定义条目；内置默认来自 config.UPSTREAMS / config.ROUTES。token 一律不入库。

async def get_custom_upstreams() -> list[dict]:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id, name, base_url, auth_scheme, token_env, supports_count_tokens, enabled "
        "FROM upstreams ORDER BY id"
    )
    return [dict(r) for r in rows]


async def create_upstream(name: str, base_url: str, auth_scheme: str,
                          token_env: str, supports_count_tokens: int = 0) -> dict:
    db = await get_db()
    cur = await db.execute(
        "INSERT INTO upstreams (name, base_url, auth_scheme, token_env, supports_count_tokens, enabled) "
        "VALUES (?,?,?,?,?,1)",
        (name, base_url, auth_scheme, token_env, int(bool(supports_count_tokens))),
    )
    await db.commit()
    rows = await db.execute_fetchall("SELECT * FROM upstreams WHERE id=?", (cur.lastrowid,))
    return dict(rows[0])


async def update_upstream(upstream_id: int, **kwargs) -> Optional[dict]:
    db = await get_db()
    allowed = {"name", "base_url", "auth_scheme", "token_env", "supports_count_tokens", "enabled"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return None
    set_clause = ", ".join(f"{k}=?" for k in fields)
    await db.execute(
        f"UPDATE upstreams SET {set_clause} WHERE id=?",
        (*fields.values(), upstream_id),
    )
    await db.commit()
    rows = await db.execute_fetchall("SELECT * FROM upstreams WHERE id=?", (upstream_id,))
    return dict(rows[0]) if rows else None


async def delete_upstream(upstream_id: int) -> bool:
    db = await get_db()
    await db.execute("DELETE FROM upstreams WHERE id=?", (upstream_id,))
    await db.commit()
    return True


async def get_custom_routes() -> list[dict]:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id, name, match, upstream, model_rewrite, priority, enabled "
        "FROM routes ORDER BY priority, id"
    )
    return [dict(r) for r in rows]


async def create_route(name: str, match: str, upstream: str,
                       model_rewrite: Optional[str] = None, priority: int = 0) -> dict:
    db = await get_db()
    cur = await db.execute(
        "INSERT INTO routes (name, match, upstream, model_rewrite, priority, enabled) "
        "VALUES (?,?,?,?,?,1)",
        (name, match, upstream, model_rewrite or None, priority),
    )
    await db.commit()
    rows = await db.execute_fetchall("SELECT * FROM routes WHERE id=?", (cur.lastrowid,))
    return dict(rows[0])


async def update_route(route_id: int, **kwargs) -> Optional[dict]:
    db = await get_db()
    allowed = {"name", "match", "upstream", "model_rewrite", "priority", "enabled"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return None
    set_clause = ", ".join(f"{k}=?" for k in fields)
    await db.execute(
        f"UPDATE routes SET {set_clause} WHERE id=?",
        (*fields.values(), route_id),
    )
    await db.commit()
    rows = await db.execute_fetchall("SELECT * FROM routes WHERE id=?", (route_id,))
    return dict(rows[0]) if rows else None


async def delete_route(route_id: int) -> bool:
    db = await get_db()
    await db.execute("DELETE FROM routes WHERE id=?", (route_id,))
    await db.commit()
    return True


def add_log(entry: dict):
    _log_buffer.append(entry)
    for q in _log_subscribers:
        try:
            q.put_nowait(entry)
        except asyncio.QueueFull:
            pass


def get_logs() -> list[dict]:
    return list(_log_buffer)


def subscribe_logs() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=50)
    _log_subscribers.append(q)
    return q


def unsubscribe_logs(q: asyncio.Queue):
    try:
        _log_subscribers.remove(q)
    except ValueError:
        pass


# ── 出站审计快照 ──────────────────────────────────────────────────────────────

def add_snapshot(entry: dict):
    _snapshot_buffer.append(entry)


def get_snapshots() -> list[dict]:
    # 最新的在前
    return list(reversed(_snapshot_buffer))


def get_snapshot_capacity() -> str:
    return _snapshot_capacity_label


async def set_snapshot_capacity(label: str) -> str:
    """更新快照容量（100/200/500/all），并按新容量重建缓冲，保留已有快照。"""
    global _snapshot_capacity_label, _snapshot_buffer
    if label not in _SNAPSHOT_CHOICES:
        raise ValueError("invalid capacity")
    _snapshot_capacity_label = label
    _snapshot_buffer = deque(_snapshot_buffer, maxlen=_SNAPSHOT_CHOICES[label])
    await set_setting("snapshot_capacity", label)
    return label
