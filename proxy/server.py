from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from typing import AsyncGenerator, Optional

import httpx
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import StreamingResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import config
import desensitizer
import storage
from models import RuleCreate, RuleTest, ModeUpdate

app = FastAPI(title="SanityProxy")
app.mount("/static", StaticFiles(directory="static"), name="static")

_current_mode = config.MODE
_http_client: Optional[httpx.AsyncClient] = None


@app.on_event("startup")
async def startup():
    global _http_client
    _http_client = httpx.AsyncClient(timeout=120.0)
    await storage.get_db()  # init DB + seed built-in rules
    global _current_mode
    _current_mode = await storage.get_setting("mode", config.MODE)


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

    is_messages = path == "messages" and request.method == "POST"

    if is_messages and _current_mode == "desensitize":
        try:
            body_json = json.loads(body_bytes)
            rules = await storage.get_enabled_rules()
            masked_body, session_mapping = desensitizer.desensitize(body_json, rules)
            hits = len(session_mapping)
            masked_body_bytes = json.dumps(masked_body, ensure_ascii=False).encode()
        except Exception as e:
            # On any parse error, fall through transparently
            pass

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
    if mapping and resp.headers.get("content-type", "").startswith("application/json"):
        try:
            resp_json = resp.json()
            restored = desensitizer.restore_body(resp_json, mapping)
            resp_content = json.dumps(restored, ensure_ascii=False).encode()
        except Exception:
            pass

    _log(req_id, start, resp.status_code, hits, resp_content, mapping, path)

    return Response(
        content=resp_content,
        status_code=resp.status_code,
        headers=dict(resp.headers),
        media_type=resp.headers.get("content-type"),
    )


async def _handle_stream(
    request: Request, url: str, headers: dict, body: bytes,
    mapping: dict, start: float, req_id: str, hits: int
) -> StreamingResponse:

    async def generate() -> AsyncGenerator[bytes, None]:
        async with _http_client.stream(
            method=request.method,
            url=url,
            headers=headers,
            content=body,
        ) as resp:
            status = resp.status_code
            async for chunk in resp.aiter_bytes():
                if mapping:
                    chunk_str = chunk.decode("utf-8", errors="replace")
                    chunk_str = desensitizer.restore(chunk_str, mapping)
                    chunk = chunk_str.encode("utf-8")
                yield chunk
            _log(req_id, start, status, hits, b"", mapping, "messages/stream")

    return StreamingResponse(generate(), media_type="text/event-stream")


def _log(req_id: str, start: float, status: int, hits: int, body: bytes, mapping: dict, path: str):
    latency = int((time.monotonic() - start) * 1000)
    input_tokens = output_tokens = 0
    try:
        data = json.loads(body)
        usage = data.get("usage", {})
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
    except Exception:
        pass

    entry = {
        "id": req_id,
        "timestamp": datetime.now(timezone.utc).strftime("%H:%M:%S"),
        "path": path,
        "latency_ms": latency,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "hits": hits,
        "status": status,
        "mode": _current_mode,
    }
    storage.add_log(entry)


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
    return {"mode": _current_mode}


@app.post("/dashboard/api/mode")
async def set_mode(body: ModeUpdate):
    global _current_mode
    if body.mode not in ("desensitize", "transparent"):
        raise HTTPException(status_code=400, detail="Invalid mode")
    _current_mode = body.mode
    await storage.set_setting("mode", body.mode)
    return {"mode": _current_mode}


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
