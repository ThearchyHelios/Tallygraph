"""Lossless first stage for Markdown metric documents: inventory before implementation.

No model, SQL, database probes or approval claims. Every row retains its original source.
Symbols and policy notes are supplied together to the implementation stage; unimplemented
definitions stay visible instead of disappearing because SQL could not be generated.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any


def inventory(documents: list[dict[str, Any]]) -> dict[str, Any]:
    items, rules, warnings = [], [], []
    hashes = {}
    occurrences = {}
    for document in documents:
        doc_id = str(document["document_id"])
        section, headers = "", []
        text = str(document.get("content") or "")
        hashes[doc_id] = hashlib.sha256(text.encode()).hexdigest()
        offset = 0
        for number, line in enumerate(text.splitlines(keepends=True), 1):
            clean = line.strip()
            start = offset
            offset += len(line)
            if clean.startswith("#"):
                section = clean.lstrip("# ")
                headers = []
                continue
            if not clean.startswith("|"):
                headers = []
                if clean:
                    rules.append({"document_id": doc_id, "section": section, "text": clean,
                                  "line": number, "span": [start, offset]})
                continue
            cells = [c.strip().replace("<br>", "\n") for c in re.split(r"(?<!\\)\|", clean.strip("|"))]
            if all(re.fullmatch(r":?-+:?", c.replace(" ", "")) for c in cells):
                continue
            if not headers:
                headers = cells
                continue
            if len(cells) != len(headers) or not cells[0]:
                warnings.append({"document_id": doc_id, "line": number, "quote": clean,
                                 "reason": "表格列数不一致或名称为空，未作为可实现指标导入"})
                continue
            fields = dict(zip(headers, cells))
            # Only recognized definition tables; unrelated Markdown tables aren't invented metrics.
            if headers[0] not in ("符号", "量", "指标", "指标名称", "名称"):
                continue
            name = cells[0].strip("` ")
            kind = "input" if headers[0] == "符号" else "metric"
            formula = fields.get("公式", fields.get("源字段/来源", ""))
            policy = fields.get("缺失/边界处理", fields.get("口径/说明", ""))
            unavailable = "无数据源" in formula
            identity = f"{doc_id}|{section}|{name}"
            occurrences[identity] = occurrences.get(identity, 0) + 1
            key = hashlib.sha256(f"{identity}|{occurrences[identity]}".encode()).hexdigest()[:20]
            items.append({"id": key, "name": name, "kind": kind, "section": section,
                          "formula": formula, "policy": policy, "fields": fields,
                          "status": "unavailable" if unavailable else "definition_only",
                          "source": {"document_id": doc_id, "line": number, "span": [start, offset],
                                     "quote": clean},
                          "implementation": fields.get("实现", "")})
    symbols = {item["name"] for item in items if item["kind"] == "input"}
    for item in items:
        mentioned = set(re.findall(r"\b[A-Za-z][A-Za-z0-9_]*\b", item["formula"]))
        item["dependencies"] = sorted((mentioned & symbols) - {item["name"]})
    return {"version": 1, "items": items, "rules": rules, "warnings": warnings, "document_hashes": hashes,
            "note": "这是原文定义清单，尚未证明字段映射、计算实现或当前生效配置正确"}


def source_matches(catalog: dict, documents: list[dict]) -> bool:
    expected = catalog.get("document_hashes") or {}
    actual = {str(d["document_id"]): hashlib.sha256(str(d.get("content") or "").encode()).hexdigest()
              for d in documents}
    return bool(expected) and expected == actual


def implementation_scope(catalog: dict, selected: list[str] | None = None) -> tuple[list[str], list[dict]]:
    """Default to all unambiguous, available definitions. Never guess between same-name definitions."""
    from collections import Counter

    metrics = [i for i in catalog.get("items", []) if i["kind"] == "metric"]
    eligible = {i["id"]: i for i in metrics if i["status"] != "unavailable"}
    if selected is not None:
        wanted = list(dict.fromkeys(selected))
        if not wanted or set(wanted) - eligible.keys():
            raise ValueError("只能选择有来源、可实现的指标定义；无数据源的定义不能自动推算")
        names = [eligible[key]["name"] for key in wanted]
        if len(names) != len(set(names)):
            raise ValueError("所选项包含同名的不同定义，请确认要采用的口径后再处理")
        return wanted, []
    counts = Counter(i["name"] for i in eligible.values())
    wanted, issues = [], []
    for item in metrics:
        reason = ("原文注明无数据源，待补充来源" if item["status"] == "unavailable" else
                  "存在同名的不同定义，需确认采用的口径" if counts[item["name"]] > 1 else "")
        if reason:
            issues.append({"definition_id": item["id"], "name": item["name"], "reason": reason})
        else:
            wanted.append(item["id"])
    return wanted, issues


def _compact_definition(item: dict[str, Any], chunk_hints: dict[str, str] | None = None) -> dict[str, Any]:
    redundant = {"符号", "量", "指标", "指标名称", "名称", "公式", "源字段/来源", "缺失/边界处理", "口径/说明", "实现"}
    extra = {k: v for k, v in (item.get("fields") or {}).items() if v and k not in redundant}
    out: dict[str, Any] = {
        "id": item["id"],
        "name": item["name"],
        "kind": item["kind"],
    }
    if item.get("section"):
        out["section"] = item["section"]
    if item.get("formula"):
        out["formula"] = item["formula"]
    if item.get("policy"):
        out["policy"] = item["policy"]
    if item.get("dependencies"):
        out["dependencies"] = item["dependencies"]
    if extra:
        out["extra_fields"] = extra
    if item.get("kind") == "metric":
        quote = (item.get("source") or {}).get("quote") or ""
        if quote:
            out["quote"] = quote
        chunk_id = (chunk_hints or {}).get(item["id"])
        if chunk_id:
            out["chunk"] = chunk_id
    return out


def implementation_guidance(catalog: dict, selected: list[str],
                            chunk_hints: dict[str, str] | None = None) -> str:
    """Carry source policies and all symbolic definitions into implementation, including overrides.
    Selected IDs control scope; formula dependencies don't become user-facing metrics by accident.
    """
    import json

    wanted = set(selected)
    documents = {item["source"]["document_id"] for item in catalog.get("items", []) if item["id"] in wanted}
    items = [_compact_definition(item, chunk_hints) for item in catalog.get("items", [])
             if item["id"] in wanted or (item["kind"] == "input" and item["source"]["document_id"] in documents)]
    rules = [{"section": r["section"], "text": r["text"]} if r.get("section") else r["text"]
             for r in catalog.get("rules", []) if r["document_id"] in documents and r.get("text")]
    return ("先按以下已选择的定义建立依赖，再绑定数据目录；不要把每条明细取第一行当成周期指标。\n"
            "只能实现选中的指标；基础量是依赖，不要额外作为输出指标。原文规则与例外必须保留。\n"
            "每条输出指标带 source_definition_id，写所选定义的 id。一行定义有多个量时可以拆成多条，均指回该定义，名称区分清楚。\n"
            "每条指标的 citations 必须逐字抄原文（不少于 8 字），chunk 写逐段原文方括号里的段落 id（定义给出 chunk 时可直接用）。\n"
            "配置覆盖、生产日、实体映射等缺少依据时列出未决问题，不猜、不换计算方法。\n"
            "加权平均与比值之和必须区别；未开班显示为零不等于计算值为零。\n"
            "声明为单值的查询最外层必须收敛到一行；不能用 LIMIT、ROWNUM 或取第一行掩盖多行。\n"
            + json.dumps({"definitions": items, "global_rules": rules}, ensure_ascii=False))
