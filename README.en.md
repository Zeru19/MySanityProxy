<!-- Language switch -->
[简体中文](README.md) · **English**

# SanityProxy

A local reverse proxy that desensitizes sensitive data before it reaches Claude or any Anthropic-compatible LLM API — and restores it in the response. Designed for legal documents, medical records, and any workflow where raw PII must never leave your machine.

```
Claude Code ──► SanityProxy (localhost:8080) ──► api.anthropic.com
                     │ desensitize / restore            │ sees only tags
                     └─── raw PII never leaves host ────┘
```

## How it works

1. Claude Code sends an API request containing sensitive text
2. SanityProxy intercepts it and replaces PII with `[[SANITY_CATEGORY_NNN]]` placeholders
3. The sanitized request is forwarded to the upstream LLM
4. The LLM responds using the same placeholders
5. SanityProxy restores all tags to original values before returning to Claude Code

Both `POST /v1/messages` (including streaming) and `POST /v1/messages/count_tokens` are sanitized — they carry the same conversation content, so token-counting requests can't leak raw PII either.

Claude Code receives a complete, natural response — with no awareness of the proxy.

### Safe with extended thinking

The model's own artifacts — `thinking` blocks and their cryptographic `signature` — round-trip **byte-for-byte** through the whole pipeline (desensitize / self-check / re-mask / restore). They are never rewritten, so upstream signature validation always passes. (`thinking` content is intentionally **not** tag-restored in responses, so it stays in tag-space across turns and signatures stay valid — you'll see tags in the reasoning panel, which is by design.) Streaming restoration is SSE-event-aware: it restores only the answer text and tool inputs, reassembles tags split across stream events, and records token usage.

## Built-in rules (legal document focus)

| Rule | Category | Example |
|------|----------|---------|
| National ID | Personal | `110101199001011234` |
| Passport | Personal | `E12345678` |
| Name | Personal | Names after 被告人 / 原告人 / 委托人 etc. |
| Mobile | Contact | `13812345678` (first 3 digits preserved) |
| Landline | Contact | `010-12345678` |
| Email | Contact | `user@example.com` |
| Bank card | Finance | 16–19 digit numbers |
| Business registration | Org | 18-char unified social credit code |
| Case number | Legal | `（2024）京民初第1234号` |
| License plate | Other | `京A12345` |

Custom rules can be added via the web dashboard or API.

## Quick start

**Requirements:** Python 3.9+

```bash
# 1. Install dependencies
cd proxy
pip install -r requirements.txt

# 2. Start the proxy
python main.py
# → http://127.0.0.1:8080

# 3. Open the dashboard
open http://localhost:8080/dashboard

# 4. Point Claude Code at the proxy
ANTHROPIC_BASE_URL=http://localhost:8080 claude
```

## What it looks like

On a successful start the terminal prints the listen address, dashboard URL, and current mode, followed by Uvicorn's startup log:

```text
SanityProxy starting on http://127.0.0.1:8080
Dashboard: http://127.0.0.1:8080/dashboard
Mode: desensitize
INFO:     Started server process [12345]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8080 (Press CTRL+C to quit)
```

Open `http://localhost:8080/dashboard` for the live request log, outbound audit, rule manager, and rule tester (with one-click light/dark theme):

<p align="center">
  <img src="docs/images/dashboard.png" alt="SanityProxy dashboard" width="860" />
</p>

## Verify it's working

```bash
cd proxy
python -m pytest tests/ -v
```

All tests must pass, covering four areas:
- **Outbound desensitization** — raw PII never reaches the LLM; system prompts and `count_tokens` are sanitized too
- **Inbound restoration** — tags are restored before reaching the caller; transparent (bypass) mode works
- **Fail-closed self-check** — `tool_result`/`tool_use` fields are masked; PII outside `messages` is backstopped; block/remask/off policies
- **Thinking signatures & streaming** — `thinking`/`signature` stay byte-identical; streaming restores text but not thinking, reassembles split tags, logs usage; non-200 upstream returns its real status

## Dashboard

The web dashboard at `http://localhost:8080/dashboard` provides:
- Real-time request log — fixed-height, scrollable panel with a sticky header and live row counter (token counts now populate for streaming requests)
- Outbound audit — snapshots of what was actually sent upstream (after masking); retain the last 20 / 100 / 200 / 500 / all
- Self-check policy switch — remask (default) / block (fail-closed) / off
- Rule manager — enable/disable rules, add custom patterns, import/export JSON
- Rule tester — paste text and preview desensitization output instantly
- Mode toggle — switch between desensitize and transparent mode

## Configuration

Edit `proxy/config.py`:

```python
UPSTREAM_URL = "https://api.anthropic.com"  # or any compatible endpoint
LISTEN_HOST  = "127.0.0.1"
LISTEN_PORT  = 8080
MODE         = "desensitize"                # or "transparent"
```

## Project structure

```
proxy/
├── main.py           # entry point
├── server.py         # FastAPI routes + proxy logic
├── desensitizer.py   # core desensitize / restore engine
├── rules.py          # built-in rule definitions
├── storage.py        # SQLite rule persistence + in-memory log buffer
├── models.py         # Pydantic models
├── config.py         # configuration
├── static/           # web dashboard (HTML / JS / CSS, no build step)
└── tests/            # automated tests (outbound, inbound, fail-closed, thinking/streaming)
```

## Managing your documents

When you place sensitive source files (legal docs, records, contracts) in the project for Claude Code to read, treat them as the data to be protected — **never commit them**. A `materials/` directory (plus `workspace/`, `data/`, `*.private/`) is pre-ignored in `.gitignore`:

```
sanity_claude/
├── proxy/          # the tool (tracked)
├── materials/      # raw sensitive source files (gitignored — never committed)
│   └── 2026-case-A/...
└── workspace/      # Claude's generated analysis/drafts (gitignored)
```

Reading a local file doesn't send it anywhere; content only leaves the machine when it's put into a request — and then **only in desensitize mode**, where the proxy swaps PII for tags (verify in the dashboard's Outbound audit). For identifiers beyond the built-in rules (employee IDs, internal ticket numbers), add a custom rule first. Keep your own encrypted backup of `materials/` — it's never in git. See AGENTS.md → "资料文件管理" for the full convention.

## Security notes

- The proxy binds to `127.0.0.1` by default — not exposed to the network
- `sanity.db` stores only rules and settings (no original text); local-only and excluded from version control
- The tag↔value mapping is **per-request**: created fresh for each request and discarded when it finishes — raw PII is never retained process-wide or written to disk
- Response headers that describe the original bytes (`content-encoding` / `content-length` / `transfer-encoding`) are stripped, since the proxy decompresses and rewrites the body
- Your source documents live in gitignored directories (see *Managing your documents*) and are never committed
