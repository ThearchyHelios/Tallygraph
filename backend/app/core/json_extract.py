"""核心 JSON 提取工具与 Copilot 设置键定义。

将 `_extract_json` 与 `COPILOT_SETTING_KEY` 下沉至 `app.core`，解除 `engine/` 对 `api/copilot.py`
的反向依赖，并用 `json.JSONDecoder().raw_decode` 前向扫描取代原先 O(N^2) 反向切片解析。
"""
from __future__ import annotations

import json
import re
from typing import Any

COPILOT_SETTING_KEY = "copilot"

_DECODER = json.JSONDecoder()
_FENCE_BLOCK_RE = re.compile(r"```(?:[a-zA-Z0-9_-]+)?\s*\n(.*?)```", re.DOTALL)
_OPEN_BRACKET_RE = re.compile(r"[\{\[]")


def _next_non_ws(text: str, pos: int) -> str:
    n = len(text)
    while pos < n and text[pos] in " \t\r\n":
        pos += 1
    return text[pos] if pos < n else ""


def _scan_json_value(candidate: str) -> Any:
    """在单段候选文本中按 `{` / `[` 位置前向使用 `raw_decode` 提取首个合法 JSON 对象或数组。"""
    stripped = candidate.strip()
    if not stripped:
        return None
    if stripped[0] in ("{", "["):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, (dict, list)):
                return parsed
        except (json.JSONDecodeError, RecursionError):
            pass

    fallback_list: list[Any] | None = None
    for match in _OPEN_BRACKET_RE.finditer(stripped):
        idx = match.start()
        ch = stripped[idx]
        if ch == "{":
            # 合法 JSON 对象 `{` 后的首个非空白字符只能是 `"` 或 `}`，借此 O(1) 跳过 `{{ ... }}` 模板与畸形花括号串
            nxt = _next_non_ws(stripped, idx + 1)
            if nxt not in ('"', "}"):
                continue
        try:
            obj, _ = _DECODER.raw_decode(stripped, idx)
        except (json.JSONDecodeError, RecursionError):
            continue
        if isinstance(obj, dict):
            return obj
        if isinstance(obj, list):
            if idx == 0 or any(isinstance(item, dict) for item in obj):
                return obj
            if fallback_list is None:
                fallback_list = obj
    return fallback_list


def _extract_json(text: str) -> Any:
    """从模型输出文本中 O(N) 提取 JSON 对象（或数组），未找到时抛出 ValueError。"""
    if not isinstance(text, str):
        raise ValueError("模型输出里找不到 JSON")
    stripped = text.strip()
    if not stripped:
        raise ValueError("模型输出里找不到 JSON")

    # 1. 外层整体由 ``` 包裹时先剥离围栏
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        inner = "\n".join(lines[1 : -1 if len(lines) > 1 and lines[-1].strip().startswith("```") else None])
        found = _scan_json_value(inner)
        if found is not None:
            return found

    # 2. 正文夹带 ```json ... ``` 代码块时优先扫描代码块内部
    for block in _FENCE_BLOCK_RE.finditer(stripped):
        found = _scan_json_value(block.group(1))
        if found is not None:
            return found

    # 3. 全文前向扫描 `{` / `[` 起点
    found = _scan_json_value(stripped)
    if found is not None:
        return found
    raise ValueError("模型输出里找不到 JSON")


__all__ = ["COPILOT_SETTING_KEY", "_extract_json"]
