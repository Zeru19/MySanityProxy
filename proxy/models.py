from __future__ import annotations

from pydantic import BaseModel
from typing import Any, Optional


class Rule(BaseModel):
    id: Optional[int] = None
    name: str
    category: str
    pattern: str
    preserve_prefix: int = 0
    enabled: bool = True
    builtin: bool = False


class RuleCreate(BaseModel):
    name: str
    category: str
    pattern: str
    preserve_prefix: int = 0
    enabled: bool = True


class RuleTest(BaseModel):
    text: str


class ModeUpdate(BaseModel):
    mode: str  # "desensitize" | "transparent"


class SelfCheckUpdate(BaseModel):
    policy: str  # "block" | "remask" | "off"


class NameDetectionUpdate(BaseModel):
    enabled: bool  # 智能姓名识别（jieba）开关


class UpstreamCreate(BaseModel):
    name: str
    base_url: str
    auth_scheme: str = "x-api-key"   # "x-api-key" | "bearer"
    token_env: str = ""              # 环境变量名，绝不存 key 本身
    supports_count_tokens: bool = False


class RouteCreate(BaseModel):
    name: str
    match: str                       # fnmatch 通配，如 "deepseek*"
    upstream: str
    model_rewrite: Optional[str] = None
    priority: int = 0


class LogEntry(BaseModel):
    id: str
    timestamp: str
    latency_ms: int
    input_tokens: int
    output_tokens: int
    hits: int
    status: int
    masked_preview: str = ""
