"""向后兼容导出：实现已下沉至 app.capabilities.check_state，消除 tools -> api 反向依赖。"""
from __future__ import annotations

from app.capabilities.check_state import (
    _DRAFT_MAX,
    _DRAFT_TTL,
    _drafts,
    draft_result,
    fields,
    fingerprint,
    record,
    remember_draft,
)

__all__ = [
    "_DRAFT_MAX",
    "_DRAFT_TTL",
    "_drafts",
    "draft_result",
    "fields",
    "fingerprint",
    "record",
    "remember_draft",
]
