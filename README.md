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

Claude Code receives a complete, natural response — with no awareness of the proxy.

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

## Verify it's working

```bash
cd proxy
python -m pytest tests/ -v
```

All 4 tests must pass:
- `test_pii_stripped_from_upstream_request` — raw PII never reaches the LLM
- `test_system_prompt_also_desensitized` — system prompts are sanitized too
- `test_full_roundtrip_pii_restored_in_response` — tags are restored before reaching the caller
- `test_transparent_mode_passes_raw_data` — transparent (bypass) mode works correctly

## Dashboard

The web dashboard at `http://localhost:8080/dashboard` provides:
- Real-time request log (timestamp, latency, token counts, hit count, status)
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
└── tests/            # 4 automated tests
```

## Security notes

- The proxy binds to `127.0.0.1` by default — not exposed to the network
- `sanity.db` (rule storage) is local only and excluded from version control
- The tag-to-value mapping lives in memory and is cleared on restart
- No data is written to disk beyond rule configuration
