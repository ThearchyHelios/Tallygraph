"""Published query semantics. These are author-maintained bindings, never guesses from SQL.

One metric describes one executable grain. Aggregation remains in the reviewed implementation;
the query router cannot invent a rollup, reinterpret an identifier, or discard a filter.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Member(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    value: str = Field(min_length=1, max_length=100)
    label: str = Field(default="", max_length=100)
    aliases: list[str] = Field(default_factory=list, max_length=30)


class EntityBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    field: str = Field(min_length=1, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    label: str = Field(min_length=1, max_length=50)
    entity_key: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    members: list[Member] = Field(min_length=1, max_length=1000)


class TimeBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    end: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    timezone: str
    basis: Literal["calendar_day", "production_day"]
    periods: list[Literal["day", "month"]] = Field(default_factory=lambda: ["day"], min_length=1, max_length=2)
    description: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def valid(self):
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("必须提供有效的业务时区") from None
        if self.start == self.end:
            raise ValueError("开始和结束参数不能相同")
        return self


class QuerySemantics(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    version: Literal[1] = 1
    enabled: bool = False
    aliases: list[str] = Field(default_factory=list, max_length=30)
    grain: str = Field(min_length=1, max_length=200)
    aggregation: str = Field(min_length=1, max_length=1000)
    time: TimeBinding | None = None
    entities: list[EntityBinding] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def distinct_fields(self):
        names = [e.field for e in self.entities]
        if self.time:
            names += [self.time.start, self.time.end]
        if len(names) != len(set(names)):
            raise ValueError("语义参数绑定不能重复")
        return self


def now() -> datetime:
    return datetime.now(timezone.utc)


def interval(text: str, binding: TimeBinding, *, at: datetime | None = None):
    """Resolve one complete day/month or one relative calendar label, in the declared timezone.

    Production-day parameters are date labels; SQL owns the approved shift attribution.
    Multiple dates are ambiguous, not silently reduced to the first date.
    """
    from app.ledger import nlchange

    relative = list(re.finditer(r"今天|昨天|昨日|前天", text))
    spans = nlchange._auto_date_spans(text)
    unique = []
    for start, end in spans:
        if not unique or start >= unique[-1][1]:
            unique.append((start, end))
    if len(relative) + len(unique) != 1:
        return None, text
    if relative:
        if "day" not in binding.periods:
            return None, text
        match = relative[0]
        day = (at or now()).astimezone(ZoneInfo(binding.timezone)).date()
        day -= timedelta(days={"今天": 0, "昨天": 1, "昨日": 1, "前天": 2}[match.group()])
        dates = (day.isoformat(), (day + timedelta(days=1)).isoformat())
        start, end = match.span()
    else:
        start, end = unique[0]
        phrase = text[start:end]
        period = "day" if (nlchange._AUTO_DAY_CN.fullmatch(phrase) or nlchange._AUTO_DAY_DASH.fullmatch(phrase)) else "month"
        if period not in binding.periods:
            return None, text
        dates = nlchange._auto_date_interval(phrase)
    return dates, text[:start] + " " + text[end:]


def mention(text: str, phrase: str) -> bool:
    """ASCII identifiers must match as a whole: P100 is not P1001, OEE is not TEEP/OEEfoo."""
    if not phrase.strip():
        return False
    left = r"(?<![A-Za-z0-9_])" if phrase[0].isascii() and phrase[0].isalnum() else ""
    right = r"(?![A-Za-z0-9_])" if phrase[-1].isascii() and phrase[-1].isalnum() else ""
    return bool(re.search(left + re.escape(phrase) + right, text, re.I))


def bind(text: str, semantic: QuerySemantics) -> tuple[dict, str, list[str]]:
    """Consume only recognized dates and explicitly registered entity values. Leave every other word."""
    values: dict = {}
    problems: list[str] = []
    remaining = text
    if semantic.time:
        dates, remaining = interval(remaining, semantic.time)
        if dates is None:
            periods = "、".join({"day": "单日", "month": "整月"}[p] for p in semantic.time.periods)
            problems.append(f"这份指标支持{periods}查询，请提供相应日期；其他周期需选择匹配口径")
        else:
            values.update(zip((semantic.time.start, semantic.time.end), dates))
    # Match on the original remainder, so aliases shared by different entity types stay ambiguous.
    matches = []
    for entity in semantic.entities:
        found = [(m, term) for m in entity.members for term in [m.value, m.label, *m.aliases]
                 if mention(remaining, term)]
        ids = {m.value for m, _ in found}
        if len(ids) != 1:
            problems.append(f"请明确一个{entity.label}")
            continue
        values[entity.field] = next(iter(ids))
        matches.append((entity, found))
    owners: dict[str, set[str]] = {}
    for entity, found in matches:
        for _, term in found:
            owners.setdefault(term.lower(), set()).add(entity.field)
    if any(len(fields) > 1 for fields in owners.values()):
        problems.append("这个编号对应多个对象类型，请明确对象")
    for entity, found in matches:
        for term in sorted({t for _, t in found}, key=len, reverse=True):
            left = r"(?<![A-Za-z0-9_])" if term[0].isascii() and term[0].isalnum() else ""
            right = r"(?![A-Za-z0-9_])" if term[-1].isascii() and term[-1].isalnum() else ""
            remaining = re.sub(left + re.escape(term) + right, " ", remaining, flags=re.I)
        remaining = remaining.replace(entity.label, " ")
    return values, remaining, problems
