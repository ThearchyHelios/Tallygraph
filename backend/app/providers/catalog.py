from __future__ import annotations

import re
from collections import OrderedDict
from typing import Any

# --------------------------------------------------------------------------
# 模型目录
#
# pricing 单位是「美元 / 百万 token」，用于 run 的成本估算。
# 新模型的价格会变，这份表只是开箱即用的默认值，用户可以在设置页改。
# --------------------------------------------------------------------------

ANTHROPIC_MODELS: list[dict[str, Any]] = [
    {"id": "claude-opus-5", "label": "Claude Opus 5", "context": 1_000_000,
     "pricing": {"input": 5.0, "output": 25.0}, "thinking": "adaptive"},
    {"id": "claude-fable-5-1", "label": "Claude Fable 5.1", "context": 1_000_000,
     "pricing": {"input": 10.0, "output": 50.0}, "thinking": "always"},
    {"id": "claude-opus-4-8", "label": "Claude Opus 4.8", "context": 1_000_000,
     "pricing": {"input": 5.0, "output": 25.0}, "thinking": "adaptive"},
    {"id": "claude-sonnet-5", "label": "Claude Sonnet 5", "context": 1_000_000,
     "pricing": {"input": 2.0, "output": 10.0}, "thinking": "adaptive"},
    {"id": "claude-haiku-4-5", "label": "Claude Haiku 4.5", "context": 200_000,
     "pricing": {"input": 1.0, "output": 5.0}, "thinking": "budget"},
]

OPENAI_MODELS: list[dict[str, Any]] = [
    {"id": "gpt-5.2", "label": "GPT-5.2", "context": 400_000,
     "pricing": {"input": 1.25, "output": 10.0}},
    {"id": "gpt-5.2-mini", "label": "GPT-5.2 mini", "context": 400_000,
     "pricing": {"input": 0.25, "output": 2.0}},
    {"id": "o4", "label": "o4", "context": 200_000,
     "pricing": {"input": 3.0, "output": 12.0}},
]

# 常见的 OpenAI 兼容服务，一键添加用
COMPATIBLE_PRESETS: list[dict[str, Any]] = [
    {
        "key": "deepseek",
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "models": [
            {"id": "deepseek-chat", "label": "DeepSeek Chat", "context": 128_000,
             "pricing": {"input": 0.27, "output": 1.1}},
            {"id": "deepseek-reasoner", "label": "DeepSeek Reasoner", "context": 128_000,
             "pricing": {"input": 0.55, "output": 2.19}},
        ],
    },
    {
        "key": "moonshot",
        "label": "Moonshot / Kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "models": [
            {"id": "kimi-k2-turbo-preview", "label": "Kimi K2 Turbo", "context": 256_000,
             "pricing": {"input": 0.6, "output": 2.5}},
            {"id": "moonshot-v1-128k", "label": "Moonshot v1 128k", "context": 128_000,
             "pricing": {"input": 0.8, "output": 3.0}},
        ],
    },
    {
        "key": "dashscope",
        "label": "阿里云百炼 / 通义千问",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": [
            {"id": "qwen-max", "label": "Qwen Max", "context": 32_000,
             "pricing": {"input": 0.34, "output": 1.37}},
            {"id": "qwen-plus", "label": "Qwen Plus", "context": 131_000,
             "pricing": {"input": 0.11, "output": 0.28}},
        ],
    },
    {
        "key": "zhipu",
        "label": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": [
            {"id": "glm-4.6", "label": "GLM-4.6", "context": 200_000,
             "pricing": {"input": 0.6, "output": 2.2}},
        ],
    },
    {
        "key": "siliconflow",
        "label": "硅基流动",
        "base_url": "https://api.siliconflow.cn/v1",
        "models": [
            {"id": "deepseek-ai/DeepSeek-V3", "label": "DeepSeek V3", "context": 128_000,
             "pricing": {"input": 0.27, "output": 1.1}},
        ],
    },
    {
        "key": "openrouter",
        "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "models": [
            {"id": "anthropic/claude-opus-5", "label": "Claude Opus 5", "context": 1_000_000,
             "pricing": {"input": 5.0, "output": 25.0}},
        ],
    },
    {
        "key": "ollama",
        "label": "Ollama（本地）",
        "base_url": "http://localhost:11434/v1",
        "api_key_optional": True,
        "models": [
            {"id": "qwen3:8b", "label": "Qwen3 8B（本地）", "context": 32_000,
             "pricing": {"input": 0.0, "output": 0.0}},
            {"id": "llama3.2", "label": "Llama 3.2（本地）", "context": 128_000,
             "pricing": {"input": 0.0, "output": 0.0}},
        ],
    },
]

MOCK_MODELS: list[dict[str, Any]] = [
    {"id": "mock-fast", "label": "演示模型（无需 API Key）", "context": 128_000,
     "pricing": {"input": 0.0, "output": 0.0}},
]

PROVIDER_KINDS = [
    {
        "kind": "anthropic",
        "label": "Anthropic",
        "base_url_hint": "留空使用官方地址；填写后使用自定义网关（默认读取 ANTHROPIC_BASE_URL）",
        "models": ANTHROPIC_MODELS,
        "default_model": "claude-opus-5",
    },
    {
        "kind": "openai",
        "label": "OpenAI",
        "base_url_hint": "留空使用官方地址",
        "models": OPENAI_MODELS,
        "default_model": "gpt-5.2",
    },
    {
        "kind": "openai_compatible",
        "label": "OpenAI 兼容（DeepSeek / Kimi / 通义 / Ollama…）",
        "base_url_hint": "必填，例如 https://api.deepseek.com/v1",
        "models": [],
        "presets": COMPATIBLE_PRESETS,
        "default_model": "",
    },
    {
        "kind": "mock",
        "label": "演示模型（无需 API Key）",
        "base_url_hint": "",
        "models": MOCK_MODELS,
        "default_model": "mock-fast",
    },
]


# --------------------------------------------------------------------------
# 模型能力
# --------------------------------------------------------------------------

# Claude 4.6 及以后的模型移除了采样参数，传 temperature/top_p 会直接 400。
_NO_SAMPLING_PREFIXES = (
    "claude-fable-5",
    "claude-mythos-5",
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
)

# 支持 adaptive thinking（而不是老的 budget_tokens）的模型
_ADAPTIVE_THINKING_PREFIXES = _NO_SAMPLING_PREFIXES

# 支持 output_config.effort
_EFFORT_PREFIXES = _NO_SAMPLING_PREFIXES + ("claude-opus-4-5",)

# 交错思考（工具结果回来之后再想一段，而不是一轮开头想完就只剩执行）。
#
# 4.6 及以后走 adaptive thinking，交错是自带的，不需要也不该再挂 beta header
# ——在 Opus 4.6 上手动模式压根没有交错思考，只有 adaptive 有。上面那张
# _ADAPTIVE_THINKING_PREFIXES 覆盖的都属于这一档。
#
# 需要显式开的是 Claude 4 / 4.5 这一代。目录里没有列它们，但模型名是放行的
# （用户可以手填），所以这条路径要留着。Haiku 4.5 不支持，前缀也就不包含它。
_INTERLEAVED_BETA_PREFIXES = ("claude-opus-4", "claude-sonnet-4")
INTERLEAVED_BETA = "interleaved-thinking-2025-05-14"


def needs_interleaved_beta(model: str) -> bool:
    """这个模型要不要靠 beta header 才会在工具之间思考。"""
    name = _norm(model)
    if supports_adaptive_thinking(name):
        return False   # adaptive 自带，多挂一个 header 只会是噪音
    return name.startswith(_INTERLEAVED_BETA_PREFIXES)


_OPENAI_REASONING_RE = re.compile(r"^o[134](?:$|[-_])")
_MAX_CUSTOM_MODELS = 512
#: 用户在「设置 → 模型接入」里填了自定义价格或上下文窗口的模型（由 list_providers 同步，带容量上限与跨 worker 缓存同步）
_CUSTOM_MODELS: OrderedDict[str, dict[str, Any]] = OrderedDict()


def _put_custom_model(target: OrderedDict[str, dict[str, Any]], key: str, info: dict[str, Any]) -> None:
    if not key:
        return
    target[key] = info
    target.move_to_end(key)
    while len(target) > _MAX_CUSTOM_MODELS:
        target.popitem(last=False)


def sync_provider_models(providers: list[Any], *, broadcast: bool = True) -> None:
    """把当前启用的模型接入里带了自定义 pricing / context 的模型同步进有界内存表，并通知集群其他 worker。"""
    import asyncio

    custom: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for p in providers or ():
        if not getattr(p, "enabled", True):
            continue
        prov_name = str(getattr(p, "name", "") or "").strip().lower()
        for entry in getattr(p, "models", None) or ():
            if not isinstance(entry, dict) or not entry.get("id"):
                continue
            raw_id = str(entry["id"]).strip().lower()
            key = _norm(raw_id)
            if not key:
                continue
            info: dict[str, Any] = {}
            pricing = entry.get("pricing")
            if isinstance(pricing, dict) and ("input" in pricing or "output" in pricing):
                info["pricing"] = {
                    "input": float(pricing.get("input") or 0.0),
                    "output": float(pricing.get("output") or 0.0),
                    **({"cache_read": float(pricing["cache_read"])} if "cache_read" in pricing and pricing["cache_read"] is not None else {}),
                    **({"cache_creation": float(pricing["cache_creation"])} if "cache_creation" in pricing and pricing["cache_creation"] is not None else {}),
                }
            ctx_val = entry.get("context")
            if isinstance(ctx_val, (int, float)) and not isinstance(ctx_val, bool) and ctx_val > 0:
                info["context"] = int(ctx_val)
            if info:
                if prov_name:
                    _put_custom_model(custom, f"{prov_name}:{key}", info)
                if "/" in raw_id and raw_id != key:
                    _put_custom_model(custom, raw_id, info)
                _put_custom_model(custom, key, info)
    _CUSTOM_MODELS.clear()
    _CUSTOM_MODELS.update(custom)
    if broadcast:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and loop.is_running():
            from app.cluster import caches

            task = loop.create_task(caches.broadcast("custom_models"))
            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)


async def _reload_custom_models_from_db() -> None:
    try:
        from sqlalchemy import select

        from app.db.base import SessionLocal
        from app.db.models import Provider

        async with SessionLocal() as session:
            rows = list((await session.execute(select(Provider).where(Provider.enabled.is_(True)))).scalars())
        sync_provider_models(rows, broadcast=False)
    except Exception:  # noqa: BLE001
        pass


def _on_custom_models_cache_event(_key: str | None = None) -> None:
    import asyncio

    _CUSTOM_MODELS.clear()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None and loop.is_running():
        task = loop.create_task(_reload_custom_models_from_db())
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)


from app.cluster import caches as _cluster_caches  # noqa: E402

_cluster_caches.on("custom_models", _on_custom_models_cache_event)


def _norm(model: str) -> str:
    """去掉 openrouter 之类的前缀并统一小写，只看模型名本身。"""
    return (model or "").split("/")[-1].strip().lower()


def supports_sampling(model: str) -> bool:
    """这个模型还能不能传 temperature / top_p。"""
    name = _norm(model)
    if name.startswith(_NO_SAMPLING_PREFIXES) or _OPENAI_REASONING_RE.match(name):
        return False
    return True


def supports_adaptive_thinking(model: str) -> bool:
    return _norm(model).startswith(_ADAPTIVE_THINKING_PREFIXES)


def requires_thinking(model: str) -> bool:
    """该模型是否强制始终开启思考（如 catalog 中标注 thinking='always' 的模型，传 disabled 会被服务方拒绝）。"""
    target = _norm(model)
    return any(_norm(entry["id"]) == target and entry.get("thinking") == "always" for entry in ANTHROPIC_MODELS)


def supports_effort(model: str) -> bool:
    return _norm(model).startswith(_EFFORT_PREFIXES)


def _lookup_custom(model: str, field: str) -> Any:
    raw = (model or "").strip().lower()
    if raw and raw in _CUSTOM_MODELS and field in _CUSTOM_MODELS[raw]:
        return _CUSTOM_MODELS[raw][field]
    target = _norm(model)
    if target in _CUSTOM_MODELS and field in _CUSTOM_MODELS[target]:
        return _CUSTOM_MODELS[target][field]
    return None


def find_pricing(model: str) -> dict[str, float] | None:
    custom = _lookup_custom(model, "pricing")
    if custom is not None:
        return custom
    target = _norm(model)
    pools = [ANTHROPIC_MODELS, OPENAI_MODELS, MOCK_MODELS]
    pools += [p["models"] for p in COMPATIBLE_PRESETS]
    for pool in pools:
        for entry in pool:
            if _norm(entry["id"]) == target:
                return entry.get("pricing")
    return None


def find_context(model: str) -> int | None:
    """目录里登记的上下文窗口（token）。没登记的返回 None，由调用方取保守值。"""
    custom = _lookup_custom(model, "context")
    if custom is not None:
        return custom
    target = _norm(model)
    pools = [ANTHROPIC_MODELS, OPENAI_MODELS, MOCK_MODELS]
    pools += [p["models"] for p in COMPATIBLE_PRESETS]
    for pool in pools:
        for entry in pool:
            if _norm(entry["id"]) == target:
                value = entry.get("context")
                return int(value) if isinstance(value, (int, float)) and value > 0 else None
    return None


def unpriced_call(data: Any) -> bool:
    """一次模型调用（llm.end 的数据）的金额估不出来：用了 token，模型却不在价格目录里，estimate_cost 只能给 0。

    这时写 $0 像是没花钱（真实运行里上百次调用、上百万 token，运行页写着 $0）。本地模型在目录里标的是 0 价，
    不算估不出来。事件自己标了 unpriced 的以它为准。"""
    if not isinstance(data, dict):
        return False
    if isinstance(data.get("unpriced"), bool):
        return data["unpriced"]
    tokens = sum(v for v in (data.get("input_tokens"), data.get("output_tokens"))
                 if isinstance(v, (int, float)) and not isinstance(v, bool))
    model = data.get("model")
    return tokens > 0 and isinstance(model, str) and bool(model) and not data.get("cost_usd") \
        and find_pricing(model) is None


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    *,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cache_read: int = 0,
    cache_write: int = 0,
) -> float:
    """按目录价估算一次调用的花费（美元）。带缓存读写明细时按缓存单价折算（默认命中 0.1x、建缓存 1.25x）。"""
    pricing = find_pricing(model)
    if not pricing:
        return 0.0
    inp_rate = pricing.get("input", 0.0)
    out_rate = pricing.get("output", 0.0)
    eff_read = max(0, int(cache_read_tokens or cache_read or 0))
    eff_write = max(0, int(cache_creation_tokens or cache_write or 0))
    if eff_read <= 0 and eff_write <= 0:
        return input_tokens / 1_000_000 * inp_rate + output_tokens / 1_000_000 * out_rate
    uncached = max(0, int(input_tokens) - eff_read - eff_write)
    read_rate = pricing.get("cache_read", inp_rate * 0.1)
    write_rate = pricing.get("cache_creation", inp_rate * 1.25)
    return (
        uncached * inp_rate
        + eff_read * read_rate
        + eff_write * write_rate
        + output_tokens * out_rate
    ) / 1_000_000
