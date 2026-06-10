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


class LogEntry(BaseModel):
    id: str
    timestamp: str
    latency_ms: int
    input_tokens: int
    output_tokens: int
    hits: int
    status: int
    masked_preview: str = ""
