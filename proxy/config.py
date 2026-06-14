import os
from dotenv import load_dotenv

load_dotenv()

UPSTREAM_URL = os.getenv("UPSTREAM_URL", "https://api.anthropic.com")
LISTEN_HOST = os.getenv("LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.getenv("LISTEN_PORT", "8080"))
MODE = os.getenv("SANITY_MODE", "desensitize")  # "desensitize" | "transparent"
DB_PATH = os.getenv("DB_PATH", "sanity.db")
LOG_CAPACITY = 1000

# ── 多上游路由 ────────────────────────────────────────────────────────────────
# Claude Code 只认一个 ANTHROPIC_BASE_URL，所有请求都到 SanityProxy；代理按请求体里的
# model 字段把它路由到不同的「上游」。这里是「活的默认配置」，面板可在其上新增/覆盖/禁用。
#
# 凭证只来自环境变量（token_env 指向的变量名），绝不写进 sanity.db / 日志 / 快照。
# auth_scheme: "x-api-key" → 头 `x-api-key: <key>`；"bearer" → 头 `Authorization: Bearer <key>`。
# 某上游若取不到 env key，则【不注入鉴权、原样透传客户端的鉴权头】——这样 Anthropic 在用
# Claude 订阅(OAuth)登录、无 API key 时仍照常工作。
UPSTREAMS = [
    {"name": "anthropic", "base_url": UPSTREAM_URL,
     "auth_scheme": "x-api-key", "token_env": "ANTHROPIC_API_KEY",
     "supports_count_tokens": True},
    {"name": "deepseek", "base_url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/anthropic"),
     "auth_scheme": "x-api-key", "token_env": "DEEPSEEK_API_KEY",
     "supports_count_tokens": False},
    {"name": "glm", "base_url": os.getenv("GLM_BASE_URL", "https://api.z.ai/api/anthropic"),
     "auth_scheme": "bearer", "token_env": "GLM_API_KEY",
     "supports_count_tokens": False},
]
DEFAULT_UPSTREAM = os.getenv("DEFAULT_UPSTREAM", "anthropic")
# 有序，首条命中。match 用 shell 通配（fnmatch）匹配请求体的 model 字段。
# model_rewrite 非空时，转发前把请求体的 model 改写成它（用于上游模型名与客户端不同的情况）。
ROUTES = [
    {"name": "deepseek", "match": "deepseek*", "upstream": "deepseek", "model_rewrite": None},
    {"name": "glm", "match": "glm*", "upstream": "glm", "model_rewrite": None},
    {"name": "anthropic", "match": "claude*", "upstream": "anthropic", "model_rewrite": None},
]
