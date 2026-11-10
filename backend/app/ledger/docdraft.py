"""业务文档 → 口径草稿的定义抽取，以及草稿共用的引用核对、对齐和合并（docs/design/next-round/02-口径账本.md 做法 8 的文档一侧）。

**这条抽取路径只产出定义。** 主路径（定义 + 取数 SQL 一次到位）在 ledger/draftplan.py：那边读数据目录、由模型写 SQL、
服务端建图并试跑。这里的 extract 仍然只抽定义，留给「已经有跑对的答案，让它认这份定义」的辅助路径（docattach）；引用核对、
对齐、合并定义这些规则两边共用，所以留在这个模块。

**为什么这个模块不写成图。** 它只负责把原文里的业务说法抽成定义卡需要的那几项：不碰取数、不碰数据库，纯逻辑加一次模型
调用，调用方拿到结果自己决定存哪里。建图（取数 SQL → start / fetch / card / write / out）在 draftplan.build_graph，
docattach 接上运行的那一路用的是 certify.build_graph。

**为什么引用要逐字核对。** 「文档里说过」是这份草稿唯一的事实来源。模型转述得再像，也要能在原文里按归一化空白
（换行、制表、全角空格）定位到那一句；定位不到就不算引用。span 是原文下标，界面据此高亮，也是之后复核的依据。

**为什么错误只说给人看。** dropped 里的 reason 直接进界面，写清楚哪一条为什么不算；机器要判别的只有 STATUSES。
"""
from __future__ import annotations

import copy
import json
import logging
import re
from collections.abc import Iterable, Mapping
from typing import Any

logger = logging.getLogger(__name__)

#: 草稿的状态（机器判别的落点，接口按它返回）。界面上只看得到四种阶段（STAGES）：草稿在写、待审、已丢弃、失败。
#: ready 是主路径的常态：图已生成（含取数 SQL）、没有未决问题、可试跑可签字发布；questions_open 有图也有数，只是问题
#: 还没清完（不阻断试跑）；path_pending 留给这次改版之前起草、还没有取数路径的老草稿；attached 是经辅助入口接上一次
#: 真实运行之后的。这些在界面上都归到「待审」。failed 是起草这一步没跑成（没有可用模型、模型回复读不懂、SQL 不合格），
#: rejected 是人判它不是口径——两者不是一回事。
STATUSES = ("drafting", "ready", "questions_open", "path_pending", "attached", "rejected", "failed")
#: 界面上的四种阶段。状态仍按 STATUSES 存（接口兼容、画布和试跑按细状态走），页面按阶段画、按阶段筛。
STAGES = ("drafting", "review", "discarded", "failed")


def stage_of(status: str) -> str:
    """细状态 → 界面阶段：ready / questions_open / path_pending / attached 都是「待审」，rejected 是「已丢弃」。"""
    if status == "drafting":
        return "drafting"
    if status == "rejected":
        return "discarded"
    if status == "failed":
        return "failed"
    return "review"

#: 一次发给模型的总字数上限（按 chunk 边界截断，见 extract）
MAX_INPUT_CHARS = 40000
#: 引用归一化之后少于这么多字，说明它指不出一句完整的话，丢掉
MIN_QUOTE_CHARS = 8

#: 来源快照的工件种类与格式版本（存进 app/core/artifact_store 的内容寻址工件）。名字不带 source_snapshots：
#: 那是数据源上传快照的表（app/db/models.SourceSnapshot），和「一次抽取读到的文档材料」不是一回事。
SNAPSHOT_KIND = "caliber_source_snapshot"
SNAPSHOT_VERSION = 1
#: 抽取提示词的版本。改了 _SYSTEM（怎么抽、要求怎么写引用）就 +1：草稿里记下这一版，历史结果才知道是按哪一版抽的。
PROMPT_VERSION = 1
#: 解析与归一化的版本。改了 parse_reply / normalize_metrics（引用核对、字段清洗的规则）就 +1。
PARSE_VERSION = 1

#: 指标对象里固定出现的字段（提示词和归一化都按这一份）
METRIC_FIELDS = ("name", "synonyms", "unit", "grain", "time_field", "filters", "numerator", "denominator",
                 "dimensions", "definition", "open_questions")
#: definition 里固定出现的字段
DEFINITION_FIELDS = ("text", "scope", "exclusions", "owner", "effective_from", "citations")
#: 这些字段本身就是在描述取数；草稿里出现一律删掉（含嵌套）
FORBIDDEN_FIELDS = ("table", "column", "sql", "tables")

#: 「select … from …」和模板占位符都是取数的痕迹，不是业务定义
_SQL_TRACE = re.compile(r"\bselect\b[\s\S]{0,4000}?\bfrom\b", re.IGNORECASE)
_TEMPLATE_TRACE = "{{"
#: 空白归一化：换行、制表、全角空格都算空白
_SPACE = re.compile(r"\s+")
_TRAILING_COMMA = re.compile(r",\s*([}\]])")
_FENCE = re.compile(r"```[A-Za-z0-9_-]*\s*")

_SYSTEM = """你是业务口径整理助手。你只读给定的业务文档，把其中的指标整理成「口径草稿」。

硬性约束：
1. 只输出一个 JSON 对象，不要写解释、不要写 Markdown 代码块之外的话。
2. 每条断言都必须带 citations：引用必须逐字抄原文，chunk 写材料里方括号内的 id。核对不上、或原文里没有依据的，
   不要写进指标，写进 open_questions。
3. 绝不输出 SQL、表名、列名、库名，也不要猜它们。你写的是业务定义，不是取数。
4. 拿不准的不要猜，写进 open_questions，用一句问得清的话。
5. 不要编造文档里没有的数字、单位、生效日期。

输出格式：
{
  "metrics": [
    {
      "name": "指标名称",
      "synonyms": ["同义词"],
      "unit": "单位（文档没写就留空）",
      "grain": "粒度（如 日 / 月 / 订单行）",
      "time_field": "业务时间字段的业务叫法",
      "filters": ["过滤条件的业务描述"],
      "numerator": "分子的业务描述",
      "denominator": "分母的业务描述",
      "dimensions": ["可拆分的业务维度"],
      "definition": {
        "text": "口径说明",
        "scope": "适用范围",
        "exclusions": ["不包含的情况"],
        "owner": "文档写明的负责人或部门",
        "effective_from": "生效日期（文档原文写法）",
        "citations": [{"chunk": "c1", "quote": "逐字原文"}]
      },
      "open_questions": ["还没说清、需要业务确认的问题"]
    }
  ],
  "open_questions": ["跨指标的未决问题"]
}
"""


class DraftUnavailable(Exception):
    """没有可用的起草模型（没有接入、只有演示模型、或模型不可用）。message 给人看。"""


def _collapse(text: str) -> str:
    return _SPACE.sub(" ", text).strip()


def _normalized(text: str) -> tuple[str, list[int]]:
    """归一化空白，并记下每个归一化字符来自原文的哪个下标。

    连续空白合成一个空格，映射到这段空白的第一个字符；开头结尾的空白不参与定位（引用本来就不该以空白开头）。
    结尾的映射不靠「空格 + 1」算，返回的 span 用最后一个非空白字符的下标 + 1，跨行时才不会把换行算丢。
    """
    out: list[str] = []
    index: list[int] = []
    for i, ch in enumerate(text):
        if ch.isspace():
            if not out or out[-1] == " ":
                continue
            out.append(" ")
            index.append(i)
        else:
            out.append(ch)
            index.append(i)
    while out and out[-1] == " ":
        out.pop()
        index.pop()
    return "".join(out), index


def verify_quote(text: str, quote: str) -> tuple[int, int] | None:
    """把引用定位回原文，返回原文的 [start, end)；找不到返回 None。

    两侧都先归一化空白再逐字比：换行、制表、全角空格、连续空格都不该让一句原文对不上。
    """
    if not isinstance(text, str) or not isinstance(quote, str):
        return None
    normalized_text, index = _normalized(text)
    normalized_quote, _ = _normalized(quote)
    if not normalized_quote:
        return None
    at = normalized_text.find(normalized_quote)
    if at < 0:
        return None
    return index[at], index[at + len(normalized_quote) - 1] + 1


def _salvage_metrics(text: str) -> list[dict]:
    """从被截断的回复里捞出已经完整的指标对象（数组里逐个平衡的 {...}）。

    截断常常发生在最后一条指标中间：前面那些完整的仍然能核对引用，不必整份回复作废。
    """
    at = text.find('"metrics"')
    if at < 0:
        return []
    start = text.find("[", at + len('"metrics"'))
    if start < 0:
        return []
    out: list[dict] = []
    depth = 0
    begin: int | None = None
    in_string = False
    escaped = False
    for i in range(start + 1, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                begin = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and begin is not None:
                try:
                    item = json.loads(text[begin:i + 1])
                except (json.JSONDecodeError, RecursionError):
                    item = None
                if isinstance(item, dict):
                    out.append(item)
                begin = None
            elif depth < 0:
                break
    return out


def _raw_metrics(raw: Any) -> list[Any]:
    """raw 可能是 dict、指标列表，或带围栏 / 前后废话 / 截断的 JSON 文本。"""
    if isinstance(raw, Mapping):
        metrics = raw.get("metrics")
        return metrics if isinstance(metrics, list) else []
    if isinstance(raw, list):
        return raw
    if not isinstance(raw, str):
        return []
    try:
        data = parse_reply(raw)
    except ValueError:
        salvaged = _salvage_metrics(raw)
        if not salvaged:
            raise
        return salvaged
    metrics = data.get("metrics")
    return metrics if isinstance(metrics, list) else []


def _strip_forbidden(value: Any) -> Any:
    """递归删掉取数字段。文档里可能出现「按 table 分」这种话，但它不是指标定义的一部分。"""
    if isinstance(value, dict):
        return {k: _strip_forbidden(v) for k, v in value.items() if k not in FORBIDDEN_FIELDS}
    if isinstance(value, list):
        return [_strip_forbidden(v) for v in value]
    return value


def _citation_list(metric: dict) -> list[Any]:
    top = metric.get("citations")
    if isinstance(top, list):
        return top
    definition = metric.get("definition")
    if isinstance(definition, Mapping) and isinstance(definition.get("citations"), list):
        return definition["citations"]
    return []


def _check_citation(citation: Any, chunks: Mapping[str, dict]) -> dict | None:
    """一条引用：chunk 存在、quote 至少 8 字、能在原文里逐字定位，才补上 document_id 和 span。"""
    if not isinstance(citation, Mapping):
        return None
    chunk_id = citation.get("chunk") or citation.get("chunk_id") or citation.get("chunkId")
    quote = citation.get("quote")
    if not isinstance(chunk_id, str) or chunk_id not in chunks or not isinstance(quote, str):
        return None
    if len(_collapse(quote)) < MIN_QUOTE_CHARS:
        return None
    chunk = chunks[chunk_id] if isinstance(chunks[chunk_id], Mapping) else {}
    span = verify_quote(str(chunk.get("content") or ""), quote)
    if span is None:
        return None
    out = dict(citation)
    out.pop("chunk_id", None)
    out.pop("chunkId", None)
    out["chunk"] = chunk_id
    out["document_id"] = str(chunk.get("document_id") or "")
    out["span"] = [span[0], span[1]]
    return out


def _has_sql_trace(metric: Any) -> bool:
    try:
        body = json.dumps(metric, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        body = str(metric)
    return bool(_SQL_TRACE.search(body)) or _TEMPLATE_TRACE in body


def normalize_metrics(raw: Any, chunks: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    """校验模型交回的指标。返回 (合格指标, 丢弃原因)。

    丢弃原因的形状是 {"name": …, "reason": …}：name 是能认出是哪一条的名字，认不出就按序号说。
    引用是逐条丢的（chunk 不存在、太短、对不上原文），一条都不剩时整条指标丢弃——没有出处的定义不进草稿。
    """
    items = _raw_metrics(raw)
    good: list[dict] = []
    dropped: list[dict] = []
    for position, item in enumerate(items, start=1):
        fallback = f"第 {position} 条"
        if not isinstance(item, Mapping):
            dropped.append({"name": fallback, "reason": "不是一个对象"})
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            dropped.append({"name": fallback, "reason": "缺少指标名称"})
            continue
        name = name.strip()
        # 先看取数痕迹再删字段：SQL 就写在 sql 字段里的，删掉字段就看不出来了
        if _has_sql_trace(item):
            dropped.append({"name": name, "reason": "含有 SQL、表名或模板占位符的痕迹：草稿只产出定义，不产出取数"})
            continue
        cleaned = _strip_forbidden(dict(item))
        cleaned["name"] = name
        citations = [kept for kept in (_check_citation(c, chunks) for c in _citation_list(item)) if kept]
        if not citations:
            dropped.append({"name": name, "reason": "没有一条引用能在原文里逐字核对上（或引用不足 8 字）"})
            continue
        cleaned["citations"] = citations
        definition = cleaned.get("definition")
        if not isinstance(definition, dict):
            definition = {}
        definition = dict(definition)
        definition["citations"] = copy.deepcopy(citations)
        cleaned["definition"] = definition
        synonyms = cleaned.get("synonyms")
        cleaned["synonyms"] = [s.strip() for s in synonyms if isinstance(s, str) and s.strip()] \
            if isinstance(synonyms, list) else []
        questions = cleaned.get("open_questions")
        cleaned["open_questions"] = [q for q in questions if isinstance(q, str) and q.strip()] \
            if isinstance(questions, list) else []
        good.append(cleaned)
    return good, dropped


def parse_reply(text: str) -> dict:
    """从模型回复里取出 JSON 对象：容忍围栏、前后废话、尾随逗号；取不出来抛 ValueError。

    截断的回复这里一律当失败：只捞得回一半的 JSON 不是可核对的结果，由 normalize_metrics 决定要不要按指标对象抢救。
    """
    if not isinstance(text, str):
        raise ValueError("模型回复不是文字")
    body = text.strip()
    if body.startswith("```"):
        body = _FENCE.sub("", body, count=1)
        if body.rstrip().endswith("```"):
            body = body.rstrip()[:-3].rstrip()
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("回复里没有 JSON 对象")
    body = body[start:end + 1]
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, RecursionError):
        # 只在第一次失败后试尾随逗号，免得把引用里的 ",}" 也改掉
        try:
            data = json.loads(_TRAILING_COMMA.sub(r"\1", body))
        except (json.JSONDecodeError, RecursionError):
            raise ValueError("回复里的 JSON 不完整或格式不对") from None
    if not isinstance(data, dict):
        raise ValueError("回复里的 JSON 不是对象")
    return data


def _order(chunks: Mapping[str, dict]) -> list[str]:
    def key(chunk_id: str) -> tuple[int, str]:
        chunk = chunks[chunk_id] if isinstance(chunks[chunk_id], Mapping) else {}
        ordinal = chunk.get("ordinal")
        return (ordinal if isinstance(ordinal, int) else 0, chunk_id)

    return sorted(chunks, key=key)


def _doc_line(doc: Any) -> str:
    if not isinstance(doc, Mapping):
        return str(doc)
    doc_id = doc.get("document_id") or doc.get("id") or ""
    title = doc.get("title") or doc.get("filename") or doc.get("name") or doc_id
    return f"- {doc_id}：{title}"


def _chunk_block(chunk_id: str, chunk: Any) -> str:
    chunk = chunk if isinstance(chunk, Mapping) else {}
    ordinal = chunk.get("ordinal")
    where = f" · 第 {ordinal} 段" if isinstance(ordinal, int) else ""
    document = chunk.get("document_id") or ""
    return f"[{chunk_id}]（文档 {document}{where}）\n{chunk.get('content') or ''}"


def _human(docs: list[dict], chunks: Mapping[str, dict], guidance: str, datasource: dict | None) -> str:
    parts = ["文档清单：", *(_doc_line(doc) for doc in docs), "", "逐段原文（引用时 chunk 写方括号里的 id，quote 逐字抄）："]
    parts.extend(_chunk_block(chunk_id, chunks[chunk_id]) for chunk_id in _order(chunks))
    parts.append("")
    if datasource:
        bits = [str(datasource.get(key)) for key in ("name", "type", "dialect", "description")
                if datasource.get(key)]
        parts.append("数据源：{}（只用来写清楚适用范围，不要据此猜表名或列名）".format(" · ".join(bits)))
    parts.append("用户补充要求：{}".format(guidance.strip() if guidance and guidance.strip() else "（无）"))
    return "\n".join(parts)


def prompt(docs: list[dict], chunks: dict[str, dict], guidance: str = "",
           datasource: dict | None = None) -> list[Any]:
    """构造 system + human。材料只放文档与逐段原文；数据源只给名字和类型，不给表结构。"""
    from langchain_core.messages import HumanMessage, SystemMessage

    return [SystemMessage(content=_SYSTEM), HumanMessage(content=_human(docs, chunks, guidance, datasource))]


def _chars(messages: Iterable[Any]) -> int:
    return sum(len(str(getattr(message, "content", "") or "")) for message in messages)


def _fit(docs: list[dict], chunks: dict[str, dict], guidance: str,
         datasource: dict | None) -> tuple[dict[str, dict], bool]:
    """按 chunk 边界把输入压到 MAX_INPUT_CHARS 以内；只剩一段还超就截这一段的内容。

    从后往前丢（ordinal 大的先丢），保留文档开头的事实；被丢掉的段落不会出现在提示里，模型也就不会引用它。
    """
    kept = dict(chunks)
    order = _order(kept)
    truncated = False
    while _chars(prompt(docs, kept, guidance, datasource)) > MAX_INPUT_CHARS and len(order) > 1:
        kept.pop(order.pop())
        truncated = True
    if _chars(prompt(docs, kept, guidance, datasource)) > MAX_INPUT_CHARS and order:
        chunk_id = order[0]
        chunk = dict(kept[chunk_id]) if isinstance(kept[chunk_id], Mapping) else {}
        content = str(chunk.get("content") or "")
        excess = _chars(prompt(docs, kept, guidance, datasource)) - MAX_INPUT_CHARS
        chunk["content"] = content[:max(0, len(content) - excess)]
        kept[chunk_id] = chunk
        truncated = True
    return kept, truncated


async def extract(session: Any, *, docs: list[dict], chunks: dict[str, dict], guidance: str = "",
                  datasource: dict | None = None, model: Any = None) -> dict:
    """一次模型调用，返回 {"metrics", "dropped", "open_questions", "truncated", "model_id",
    "visible_chunk_ids", "prompt_version", "parse_version"}。

    不落库。model 可注入（测试用假对象：有 async ainvoke(messages)，回复有 .content）；不给就按设置解析，
    模型不可用时抛 DraftUnavailable，reason 原样给人看。输入超限按 chunk 截断，truncated 交调用方在界面上说清楚。
    visible_chunk_ids 是**实际进了这次提示**的段落（按 ordinal 排）：被 _fit 丢掉的段落不在里面，引用它也核不上——
    引用核对只认实际可见的材料，核对用的正是这里交出去的这一份。
    """
    from app.engine.state import message_text

    if model is None:
        from app.data.recipe_ai import resolve_draft_model

        model, model_id, availability = await resolve_draft_model(session)
        if model is None or not getattr(availability, "available", False):
            raise DraftUnavailable(getattr(availability, "reason", "") or "AI 起草不可用")
    else:
        named = getattr(model, "model_name", None) or getattr(model, "model", None)
        model_id = named if isinstance(named, str) else ""

    kept, truncated = _fit(docs, chunks, guidance, datasource)
    reply = await model.ainvoke(prompt(docs, kept, guidance, datasource))
    text = message_text(reply)
    try:
        data = parse_reply(text)
    except ValueError:
        # 截断的回复：能捞回几条完整的指标就交几条，其余交给调用方说明
        data = {"metrics": _salvage_metrics(text)}
        if not data["metrics"]:
            raise
    metrics, dropped = normalize_metrics(data, kept)
    questions = data.get("open_questions")
    return {
        "metrics": metrics,
        "dropped": dropped,
        "open_questions": questions if isinstance(questions, list) else [],
        "truncated": truncated,
        "model_id": model_id,
        "visible_chunk_ids": _order(kept),
        "prompt_version": PROMPT_VERSION,
        "parse_version": PARSE_VERSION,
    }


def _key(value: Any) -> str:
    """同名判定：去掉所有空白（含全角空格）再小写。"""
    return re.sub(r"\s+", "", value).casefold() if isinstance(value, str) else ""


def _words(metric: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    name = metric.get("name")
    if isinstance(name, str) and name.strip():
        out.append(name)
    synonyms = metric.get("synonyms")
    if isinstance(synonyms, list):
        out.extend(s for s in synonyms if isinstance(s, str) and s.strip())
    keys: list[str] = []
    for word in out:
        key = _key(word)
        if key and key not in keys:
            keys.append(key)
    return keys


def _unit(value: Any) -> str:
    return _key(value)


def _definition_text(metric: Mapping[str, Any]) -> str:
    definition = metric.get("definition")
    text = definition.get("text") if isinstance(definition, Mapping) else None
    if not isinstance(text, str):
        # 现有口径那一侧交上来的是 definition_text（见模块说明的入参形状）
        text = metric.get("definition_text")
    return _collapse(text) if isinstance(text, str) else ""


def _reason(draft: Mapping[str, Any], existing: Mapping[str, Any]) -> str:
    parts: list[str] = []
    if _unit(draft.get("unit")) != _unit(existing.get("unit")):
        parts.append("单位不同（草稿「{}」，现有「{}」）".format(draft.get("unit") or "未写", existing.get("unit") or "未写"))
    if _definition_text(draft) != _definition_text(existing):
        parts.append("定义说明不同")
    return "；".join(parts) or "定义不一致"


def alignment(metrics: list[dict], existing: list[dict]) -> dict:
    """草稿和现有口径对齐：new / duplicate / conflict。

    同名按 name 与 synonyms 去空白、转小写后相等算；一个草稿指标可能撞上多条现有指标，只要有一条完全相同就算
    duplicate（说明这一条已经有出处），否则按第一条不同的写 conflict 和原因。
    """
    index: dict[str, list[dict]] = {}
    for entry in existing:
        if not isinstance(entry, Mapping):
            continue
        for key in _words(entry):
            index.setdefault(key, []).append(dict(entry))

    result: dict[str, list[Any]] = {"new": [], "duplicate": [], "conflict": []}
    for metric in metrics:
        if not isinstance(metric, Mapping):
            continue
        name = metric.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        name = name.strip()
        matched: list[dict] = []
        seen: set[int] = set()
        for key in _words(metric):
            for entry in index.get(key, ()):
                if id(entry) not in seen:
                    seen.add(id(entry))
                    matched.append(entry)
        if not matched:
            result["new"].append(name)
            continue
        same = [e for e in matched
                if _unit(e.get("unit")) == _unit(metric.get("unit"))
                and _definition_text(e) == _definition_text(metric)]
        if same:
            entry = same[0]
            result["duplicate"].append({"name": name, "workflow_id": entry.get("workflow_id"),
                                        "version": entry.get("version"), "node_id": entry.get("node_id")})
        else:
            entry = matched[0]
            result["conflict"].append({"name": name, "workflow_id": entry.get("workflow_id"),
                                       "version": entry.get("version"), "reason": _reason(metric, entry)})
    return result


def _find_card(card_metrics: list[dict], draft: Mapping[str, Any]) -> dict | None:
    keys = set(_words(draft))
    for card in card_metrics:
        if isinstance(card, dict) and keys.intersection(_words(card)):
            return card
    return None


def merge_definition(card_metrics: list[dict], draft_metrics: list[dict]) -> tuple[list[dict], list[str]]:
    """把草稿的 definition 并进同名 / 同义词的口径卡指标；不改 expression / id。

    只并 definition（和卡上还没有的同义词）：草稿不知道取数，改写式子或 id 会破坏已有口径的算法。匹配不上的进
    warnings，由人决定新建还是忽略。同义词要带下去——文档里的叫法就是以后按名字找这份口径时的另一种说法，丢了它，
    「月活」和「月度活跃买家」就永远对不上。
    """
    merged = copy.deepcopy(list(card_metrics))
    warnings: list[str] = []
    for draft in draft_metrics:
        if not isinstance(draft, Mapping):
            continue
        name = draft.get("name")
        name = name.strip() if isinstance(name, str) and name.strip() else ""
        target = _find_card(merged, draft)
        if target is None:
            warnings.append(f"草稿指标「{name or '（未命名）'}」在口径卡上找不到同名或同义词指标，定义未合并")
            continue
        synonyms = draft.get("synonyms")
        if isinstance(synonyms, list):
            kept = [s.strip() for s in synonyms if isinstance(s, str) and s.strip()]
            if kept:
                have = target.get("synonyms")
                have = [s for s in have if isinstance(s, str)] if isinstance(have, list) else []
                # 大小写和空白不算新说法：卡上已有「GMV」时不再加一条「gmv」
                known = {s.strip().casefold() for s in have}
                added = [s for s in kept if s.casefold() not in known]
                for s in added:
                    known.add(s.casefold())
                if added:
                    target["synonyms"] = have + added
        definition = draft.get("definition")
        if not isinstance(definition, Mapping) or not definition:
            warnings.append(f"草稿指标「{name}」没有可合并的定义说明")
            continue
        base = target.get("definition")
        base = dict(base) if isinstance(base, Mapping) else {}
        for key, value in definition.items():
            if value is not None:
                base[key] = copy.deepcopy(value)
        target["definition"] = base
    return merged, warnings


#: 被截断的回复里抢回已经完整的指标对象（draftplan 生成取数草稿时也要用）
salvage_metrics = _salvage_metrics


# ==========================================================================
# 来源快照（K05：提取那一刻就把原文和材料存下来，草稿只记引用）
# ==========================================================================


def source_snapshot(*, documents: list[Any], chunks: Mapping[str, Any], datasource: Any = None,
                    catalog: str = "", guidance: str = "", fixed_filters: str = "") -> dict:
    """把这次抽取实际读到的来源材料收成一份**可还原**的快照：逐份原文、逐段原文，以及会改变模型看到什么的那几样
    （数据目录文本、用户补充要求、固定条件、数据源）。调用方把它存进 app/core/artifact_store，草稿里只记返回的 id。

    **为什么不能只存哈希。** 哈希只说明「还是那一份」，说不出内容是什么；重导入要比对的是「上次被接受的导入值」
    （任务书 B），只有哈希就还原不了 B。所以原文整份进快照，chunk 的可见单位就是段。

    **为什么按内容寻址。** 同一份材料只存一份，工件 id 就是规范化 JSON 的 sha256；快照因此不可变，也不会因为草稿
    后来被改而跟着变。文档被重新切块、chunk id 换了一批，这份快照照旧读得回来（它按 id 认内容，不按名字）。

    **这不是字段基线。** 快照只保证「当时的材料还原得出来」，字段级的 B 要等接受重导入时按字段推进（见 K11）。
    """
    from app.core.artifact_store import content_hash

    def text(value: Any) -> str:
        return value if isinstance(value, str) else ""

    docs: list[dict[str, Any]] = []
    for doc in documents if isinstance(documents, list) else []:
        item = doc if isinstance(doc, Mapping) else {}
        content = text(item.get("content"))
        docs.append({"document_id": str(item.get("document_id") or item.get("id") or ""),
                     "title": text(item.get("title")), "source": text(item.get("source")),
                     "collection": text(item.get("collection")),
                     "content": content, "content_hash": content_hash(content)})
    kept: dict[str, dict[str, Any]] = {}
    for chunk_id, chunk in (chunks if isinstance(chunks, Mapping) else {}).items():
        item = chunk if isinstance(chunk, Mapping) else {}
        content = text(item.get("content"))
        ordinal = item.get("ordinal")
        kept[str(chunk_id)] = {"document_id": str(item.get("document_id") or ""),
                               "ordinal": ordinal if isinstance(ordinal, int) else None,
                               "content": content, "content_hash": content_hash(content)}
    return {"kind": SNAPSHOT_KIND, "version": SNAPSHOT_VERSION, "documents": docs, "chunks": kept,
            "datasource": dict(datasource) if isinstance(datasource, Mapping) else None,
            "catalog": text(catalog), "guidance": text(guidance), "fixed_filters": text(fixed_filters)}


def snapshot_ref(artifact_id: str, snapshot: Mapping[str, Any]) -> dict:
    """草稿 payload 里放的那一份引用：工件 id（= 内容的 sha256）、格式版本、这份快照收了哪些文档和段落。

    原文不在 payload 里再抄一遍——草稿页按这份引用回工件库读，读到的是当初那一份，不会随草稿改动。
    """
    return {"artifact_id": str(artifact_id or ""), "kind": snapshot.get("kind") or SNAPSHOT_KIND,
            "version": snapshot.get("version") or SNAPSHOT_VERSION, "content_hash": str(artifact_id or ""),
            "documents": [str(d.get("document_id")) for d in snapshot.get("documents") or []
                          if isinstance(d, Mapping)],
            "chunk_ids": sorted(str(c) for c in (snapshot.get("chunks") or {}))}


def source_baseline(payload: Any) -> dict[str, Any]:
    """这份草稿的来源基线是什么状态：known（有**读得回来**的来源快照）/ baseline_unknown（没有，或读不回来）。

    **旧草稿一律 baseline_unknown。** 保存来源快照之前起草的草稿没有这份引用，它当时的导入内容是什么已经无法
    还原；不能拿它现在的内容冒充历史基线（B 是「上次被接受的导入值」，不是「现在长什么样」）。这份引用也不是
    字段基线——字段级 B 要等接受重导入时按字段落（K11）。

    **有引用不等于有基线。** 这里会把工件真的读一遍（load 会复验哈希）：工件不在、内容被改过、或种类不对，都照实
    说 baseline_unknown 并把 readable 标成 false——「快照坏了」不能报成 known，也不能在读草稿时抛出去把页面弄崩。
    读的时候现算，不拿 payload 里存过的那份状态：改了口径内容不改变来源快照，但工件被删被改必须看得出来。
    """
    ref = payload.get("source_snapshot") if isinstance(payload, Mapping) else None
    artifact_id = ref.get("artifact_id") if isinstance(ref, Mapping) else None
    if not (isinstance(artifact_id, str) and artifact_id):
        return {"status": "baseline_unknown", "readable": False,
                "reason": "这份草稿没有可还原的来源快照（保存快照之前起草的，或这次落库失败）：当时的导入内容无法还原，"
                          "不能拿当前内容当作历史基线"}
    try:
        from app.core.artifact_store import load

        snapshot = load(artifact_id)
    except Exception as e:  # noqa: BLE001 - 工件被改过（哈希不符）也算读不出来，照实说，不让读草稿失败
        logger.warning("来源快照工件读不出来（%s）：%s", artifact_id, e)
        snapshot = None
    if not isinstance(snapshot, Mapping) or snapshot.get("kind") != SNAPSHOT_KIND:
        return {"status": "baseline_unknown", "readable": False, "artifact_id": artifact_id,
                "version": ref.get("version") or SNAPSHOT_VERSION,
                "reason": "来源快照工件读不回来（不在、内容与哈希不符，或不是来源快照）：当时的导入内容无法还原，"
                          "不能拿当前内容当作历史基线"}
    return {"status": "known", "readable": True, "artifact_id": artifact_id,
            "version": ref.get("version") or SNAPSHOT_VERSION,
            "chunk_ids": sorted(str(c) for c in (snapshot.get("chunks") or {}))}


__all__ = [
    "DEFINITION_FIELDS", "DraftUnavailable", "FORBIDDEN_FIELDS", "MAX_INPUT_CHARS", "METRIC_FIELDS",
    "MIN_QUOTE_CHARS", "PARSE_VERSION", "PROMPT_VERSION", "SNAPSHOT_KIND", "SNAPSHOT_VERSION", "STAGES",
    "STATUSES", "alignment", "extract", "merge_definition", "normalize_metrics", "parse_reply", "prompt",
    "salvage_metrics", "snapshot_ref", "source_baseline", "source_snapshot", "stage_of", "verify_quote",
]
