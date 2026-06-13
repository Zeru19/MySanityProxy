from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import AsyncGenerator, Optional

# 东八区（UTC+8），用于面板日志与快照时间显示
CST = timezone(timedelta(hours=8))

import httpx
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import StreamingResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import config
import desensitizer
import storage
from models import RuleCreate, RuleTest, ModeUpdate, SelfCheckUpdate, NameDetectionUpdate

app = FastAPI(title="SanityProxy")
app.mount("/static", StaticFiles(directory="static"), name="static")

_current_mode = config.MODE
# 出站自检策略：block=命中即拦截(fail-closed) | remask=补脱后放行 | off=仅告警放行
_current_selfcheck = "remask"
# 智能姓名识别（jieba 分词补召回裸姓名），默认开启；jieba 不可用时自动降级为纯 regex
_name_detection = True
_http_client: Optional[httpx.AsyncClient] = None


@app.on_event("startup")
async def startup():
    global _http_client
    _http_client = httpx.AsyncClient(timeout=120.0)
    await storage.get_db()  # init DB + seed built-in rules
    global _current_mode, _current_selfcheck, _name_detection
    _current_mode = await storage.get_setting("mode", config.MODE)
    _current_selfcheck = await storage.get_setting("selfcheck", _current_selfcheck)
    _name_detection = (await storage.get_setting("name_detection", "on")) == "on"
    # 预热 jieba 字典：把首次 ~1s 的加载从首个真实请求挪到启动期
    if _name_detection and desensitizer._HAS_JIEBA:
        await asyncio.to_thread(_prewarm_jieba)


def _prewarm_jieba():
    try:
        desensitizer.jieba.initialize()
        list(desensitizer._pseg.cut("张三"))  # 触发 HMM/词典加载
        print("jieba name detection: warmed up")
    except Exception as exc:  # pragma: no cover
        print(f"jieba warmup skipped: {exc}")


@app.on_event("shutdown")
async def shutdown():
    if _http_client:
        await _http_client.aclose()


# ── Anthropic API proxy ─────────────────────────────────────────────────────

@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(request: Request, path: str):
    start = time.monotonic()
    req_id = uuid.uuid4().hex[:8]

    # Forward headers (strip host, keep everything else including auth)
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length")
    }

    body_bytes = await request.body()
    masked_body_bytes = body_bytes
    session_mapping: dict[str, str] = {}
    hits = 0

    # 只有 /v1/messages（POST）会流式返回，需要逐事件还原。
    is_messages = path == "messages" and request.method == "POST"
    # 需要脱敏的端点：messages 与 count_tokens 都携带同样的对话内容上云，
    # 两者都必须脱敏，否则 token 计数请求会原文外泄 PII。
    should_mask = request.method == "POST" and path in ("messages", "messages/count_tokens")

    if should_mask and _current_mode == "desensitize":
        rules = await storage.get_enabled_rules()
        # 每请求一份注册表，贯穿 desensitize→remask 两趟（编号连续、同值同标签），
        # 请求结束即回收：既隔离并发、不常驻 PII，又避免两趟标签编号撞车。
        reg = desensitizer.new_registry()
        try:
            body_json = json.loads(body_bytes)
            # 脱敏含 jieba 分词时是 CPU 密集且同步，卸到线程，避免阻塞事件循环、拖慢并发。
            masked_body, session_mapping = await asyncio.to_thread(
                desensitizer.desensitize, body_json, rules, reg, _name_detection)
            hits = len(session_mapping)
            masked_body_bytes = json.dumps(masked_body, ensure_ascii=False).encode()
        except Exception:
            # fail-closed：脱敏模式下无法解析/脱敏请求体时，绝不放行原文
            _record_snapshot(req_id, path, body_bytes, 0,
                             status="blocked",
                             leaks=[{"rule": "desensitize_error", "category": "", "sample": ""}])
            _log(req_id, start, 403, 0, path)
            return JSONResponse(
                status_code=403,
                content={"error": {"type": "sanity_blocked",
                                   "message": "SanityProxy: 请求体无法脱敏，已拦截以防 PII 外泄（fail-closed）。"}},
            )

        # 出站自检：转发前再核对脱敏结果。策略可在面板切换：
        #   block  = 命中即拦截（fail-closed，最严，可能误拦）
        #   remask = 补脱后放行（发现残留就地脱敏再发，既不外泄也不拦）
        #   off    = 仅告警放行（命中只记审计，不拦不补）
        if _current_selfcheck == "remask":
            # 全文兜底补脱：把任何残留 PII 就地脱敏，再转发
            masked_body_bytes, extra, swept = desensitizer.remask_residual(masked_body_bytes, rules, reg)
            if extra:
                session_mapping.update(extra)
                hits = len(session_mapping)
            _record_snapshot(req_id, path, masked_body_bytes, hits,
                             status="remasked" if swept else "forwarded", leaks=swept)
        elif _current_selfcheck == "off":
            # 仅扫内容区域、记录告警，不拦截
            leaks = desensitizer.detect_residual(masked_body, rules)
            _record_snapshot(req_id, path, masked_body_bytes, hits,
                             status="warned" if leaks else "forwarded", leaks=leaks)
        else:
            # block：只扫会上云的用户内容（messages + system），不碰 tools/model/metadata
            leaks = desensitizer.detect_residual(masked_body, rules)
            if leaks:
                _record_snapshot(req_id, path, masked_body_bytes, hits, status="blocked", leaks=leaks)
                _log(req_id, start, 403, hits, path)
                names = "、".join(sorted({l["rule"] for l in leaks}))
                return JSONResponse(
                    status_code=403,
                    content={"error": {"type": "sanity_blocked",
                                       "message": f"SanityProxy 出站自检拦截：检测到未脱敏的 PII（命中规则：{names}）。"
                                                  f"请到面板「出站审计」查看快照，或改用「补脱后放行」策略/补充规则。"}},
                )
            _record_snapshot(req_id, path, masked_body_bytes, hits, status="forwarded", leaks=[])

    # Determine if streaming
    is_stream = False
    if is_messages:
        try:
            req_json = json.loads(body_bytes)
            is_stream = req_json.get("stream", False)
        except Exception:
            pass

    upstream_url = f"{config.UPSTREAM_URL}/v1/{path}"

    try:
        if is_stream:
            return await _handle_stream(
                request, upstream_url, headers, masked_body_bytes,
                session_mapping, start, req_id, hits
            )
        else:
            return await _handle_json(
                request, upstream_url, headers, masked_body_bytes,
                session_mapping, start, req_id, hits, path
            )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Upstream error: {exc}")


# 我们会读取（解压）并改写上游响应体，故这些描述「原始字节」的头不能原样回传，
# 否则客户端会按错误的长度/编码去解码改写后的 body。
_DROP_RESP_HEADERS = frozenset({"content-encoding", "content-length", "transfer-encoding"})


def _clean_response_headers(headers) -> dict:
    return {k: v for k, v in headers.items() if k.lower() not in _DROP_RESP_HEADERS}


def _usage_from(data: dict) -> tuple[int, int]:
    usage = data.get("usage", {}) if isinstance(data, dict) else {}
    return usage.get("input_tokens", 0) or 0, usage.get("output_tokens", 0) or 0


async def _handle_json(
    request: Request, url: str, headers: dict, body: bytes,
    mapping: dict, start: float, req_id: str, hits: int, path: str
) -> Response:
    resp = await _http_client.request(
        method=request.method,
        url=url,
        headers=headers,
        content=body,
    )

    resp_content = resp.content
    input_tokens = output_tokens = 0
    if resp.headers.get("content-type", "").startswith("application/json"):
        try:
            resp_json = resp.json()
            if mapping:
                resp_json = desensitizer.restore_body(resp_json, mapping)
            input_tokens, output_tokens = _usage_from(resp_json)
            resp_content = json.dumps(resp_json, ensure_ascii=False).encode()
        except Exception:
            pass

    _log(req_id, start, resp.status_code, hits, path,
         input_tokens=input_tokens, output_tokens=output_tokens)

    return Response(
        content=resp_content,
        status_code=resp.status_code,
        headers=_clean_response_headers(resp.headers),
        media_type=resp.headers.get("content-type"),
    )


def _split_safe_tag(text: str) -> tuple[str, str]:
    """切出可安全输出的前缀，暂留可能是半个 [[SANITY_*]] 标签的尾部。

    标签可能被模型的 token 流劈成多个 SSE 增量。若逐增量盲还原，跨增量的标签会漏还原
    （用户看到残留标签——不泄漏 PII，但难看）。这里把任何未闭合的 `[[...` 尾巴留到
    下一增量再处理，闭合后即可整体还原。
    """
    idx = text.rfind("[[")
    if idx != -1 and "]]" not in text[idx:]:
        return text[:idx], text[idx:]
    if text.endswith("["):
        return text[:-1], text[-1:]
    return text, ""


def _process_sse_event(block: str, state: dict, mapping: dict) -> tuple[str, list[str]]:
    """处理一个完整 SSE 事件块：仅还原 text_delta/input_json_delta，跳过 thinking/signature，
    顺带采集 usage。返回 (改写后的事件块, 需在其【之前】补发的事件列表)。"""
    lines = block.split("\n")
    data_idx = next((i for i, l in enumerate(lines) if l.startswith("data:")), None)
    if data_idx is None:
        return block, []
    try:
        obj = json.loads(lines[data_idx][5:].lstrip())
    except Exception:
        return block, []

    extra: list[str] = []
    t = obj.get("type")
    if t == "message_start":
        i, o = _usage_from(obj.get("message", {}))
        state["input_tokens"] = i or state["input_tokens"]
        state["output_tokens"] = o or state["output_tokens"]
    elif t == "message_delta":
        _i, o = _usage_from(obj)
        if o:
            state["output_tokens"] = o
    elif t == "content_block_start":
        state["block_type"][obj.get("index")] = obj.get("content_block", {}).get("type")
    elif t == "content_block_delta" and mapping:
        idx = obj.get("index")
        delta = obj.get("delta", {})
        dt = delta.get("type")
        field = {"text_delta": "text", "input_json_delta": "partial_json"}.get(dt)
        if field is not None:
            pend = state["pending"].get(idx, "") + (delta.get(field) or "")
            flush, hold = _split_safe_tag(pend)
            state["pending"][idx] = hold
            delta[field] = desensitizer.restore(flush, mapping)
        # thinking_delta / signature_delta：原样保留，绝不还原
    elif t == "content_block_stop" and mapping:
        idx = obj.get("index")
        hold = state["pending"].pop(idx, "")
        if hold:
            jf = "partial_json" if state["block_type"].get(idx) == "tool_use" else "text"
            dtype = "input_json_delta" if jf == "partial_json" else "text_delta"
            flush_delta = {"type": "content_block_delta", "index": idx,
                           "delta": {"type": dtype, jf: desensitizer.restore(hold, mapping)}}
            extra.append("event: content_block_delta\ndata: " + json.dumps(flush_delta, ensure_ascii=False))

    lines[data_idx] = "data: " + json.dumps(obj, ensure_ascii=False)
    return "\n".join(lines), extra


async def _handle_stream(
    request: Request, url: str, headers: dict, body: bytes,
    mapping: dict, start: float, req_id: str, hits: int
) -> Response:
    # 先建立连接拿到状态码。上游若返回非 200（如 400 坏请求），它给的是一段 JSON 错误体、
    # 而非 SSE 流；此时必须把【真实状态码】回传给 Claude Code，否则 StreamingResponse 会以
    # 假 200 包住错误体，客户端解析不到有效事件、误判为可重试 → 死循环。
    upstream = await _http_client.send(
        _http_client.build_request(
            method=request.method, url=url, headers=headers, content=body),
        stream=True,
    )

    if upstream.status_code != 200:
        try:
            raw = await upstream.aread()
        finally:
            await upstream.aclose()
        _log(req_id, start, upstream.status_code, hits, "messages")
        return Response(
            content=raw,
            status_code=upstream.status_code,
            headers=_clean_response_headers(upstream.headers),
            media_type=upstream.headers.get("content-type"),
        )

    async def generate() -> AsyncGenerator[bytes, None]:
        state = {"pending": {}, "block_type": {}, "input_tokens": 0, "output_tokens": 0}
        try:
            # 按字节缓冲、按 SSE 事件边界(\n\n)切分，避免多字节字符/标签被网络分块劈开
            buf = b""
            async for chunk in upstream.aiter_bytes():
                buf += chunk
                while b"\n\n" in buf:
                    raw_event, buf = buf.split(b"\n\n", 1)
                    processed, extra = _process_sse_event(
                        raw_event.decode("utf-8", errors="replace"), state, mapping)
                    for e in extra:
                        yield (e + "\n\n").encode("utf-8")
                    yield (processed + "\n\n").encode("utf-8")
            if buf:
                processed, extra = _process_sse_event(
                    buf.decode("utf-8", errors="replace"), state, mapping)
                for e in extra:
                    yield (e + "\n\n").encode("utf-8")
                yield processed.encode("utf-8")
        finally:
            await upstream.aclose()
            _log(req_id, start, upstream.status_code, hits, "messages",
                 input_tokens=state["input_tokens"], output_tokens=state["output_tokens"])

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"cache-control": "no-cache"},
    )


def _log(req_id: str, start: float, status: int, hits: int, path: str,
         input_tokens: int = 0, output_tokens: int = 0):
    latency = int((time.monotonic() - start) * 1000)
    entry = {
        "id": req_id,
        "timestamp": datetime.now(CST).strftime("%H:%M:%S"),
        "path": path,
        "latency_ms": latency,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "hits": hits,
        "status": status,
        "mode": _current_mode,
    }
    storage.add_log(entry)


def _record_snapshot(req_id: str, path: str, body: bytes, hits: int, status: str, leaks: list):
    """记录一份出站审计快照（脱敏后内容；blocked 时为被拦截内容）。"""
    try:
        text = body.decode("utf-8", errors="replace")
    except Exception:
        text = ""
    storage.add_snapshot({
        "id": req_id,
        "timestamp": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
        "path": path,
        "size": len(body),
        "hits": hits,
        "status": status,          # "forwarded" | "blocked"
        "leaks": leaks,            # 自检命中（fail-closed 时非空）
        "body": text,             # 实际出站的脱敏后请求体
    })


# ── Dashboard API ────────────────────────────────────────────────────────────

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    with open("static/index.html", encoding="utf-8") as f:
        return f.read()


@app.get("/dashboard/api/logs")
async def stream_logs(request: Request):
    async def event_stream():
        # Send existing logs first
        for entry in storage.get_logs():
            yield f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"

        q = storage.subscribe_logs()
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    entry = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            storage.unsubscribe_logs(q)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/dashboard/api/status")
async def get_status():
    return {
        "mode": _current_mode,
        "selfcheck": _current_selfcheck,
        "name_detection": _name_detection,
        "name_detection_available": desensitizer._HAS_JIEBA,
    }


@app.post("/dashboard/api/mode")
async def set_mode(body: ModeUpdate):
    global _current_mode
    if body.mode not in ("desensitize", "transparent"):
        raise HTTPException(status_code=400, detail="Invalid mode")
    _current_mode = body.mode
    await storage.set_setting("mode", body.mode)
    return {"mode": _current_mode}


@app.post("/dashboard/api/selfcheck")
async def set_selfcheck(body: SelfCheckUpdate):
    global _current_selfcheck
    if body.policy not in ("block", "remask", "off"):
        raise HTTPException(status_code=400, detail="Invalid policy")
    _current_selfcheck = body.policy
    await storage.set_setting("selfcheck", body.policy)
    return {"selfcheck": _current_selfcheck}


@app.post("/dashboard/api/name-detection")
async def set_name_detection(body: NameDetectionUpdate):
    global _name_detection
    _name_detection = bool(body.enabled)
    await storage.set_setting("name_detection", "on" if _name_detection else "off")
    # 首次开启时预热 jieba，避免下一个请求承担字典加载延迟
    if _name_detection and desensitizer._HAS_JIEBA:
        await asyncio.to_thread(_prewarm_jieba)
    return {"name_detection": _name_detection, "available": desensitizer._HAS_JIEBA}


@app.get("/dashboard/api/snapshots")
async def list_snapshots():
    return {
        "capacity": storage.get_snapshot_capacity(),
        "choices": ["20", "100", "200", "500", "all"],
        "items": storage.get_snapshots(),
    }


@app.post("/dashboard/api/snapshot-capacity")
async def update_snapshot_capacity(body: dict):
    try:
        label = await storage.set_snapshot_capacity(str(body.get("capacity")))
    except ValueError:
        raise HTTPException(status_code=400, detail="capacity 必须是 20/100/200/500/all 之一")
    return {"capacity": label}


@app.get("/dashboard/api/rules")
async def list_rules():
    return await storage.get_all_rules()


@app.post("/dashboard/api/rules", status_code=201)
async def create_rule(body: RuleCreate):
    return await storage.create_rule(body.name, body.category, body.pattern, body.preserve_prefix)


@app.put("/dashboard/api/rules/{rule_id}")
async def update_rule(rule_id: int, body: dict):
    result = await storage.update_rule(rule_id, **body)
    if result is None:
        raise HTTPException(status_code=404)
    return result


@app.delete("/dashboard/api/rules/{rule_id}", status_code=204)
async def delete_rule(rule_id: int):
    await storage.delete_rule(rule_id)


@app.post("/dashboard/api/rules/test")
async def test_rule(body: RuleTest):
    rules = await storage.get_enabled_rules()
    result = desensitizer.test_rules(body.text, rules)
    return result
