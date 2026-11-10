"""草稿入口字段的类型合并（R02）：同一个字段在各组里声明成不同类型时，合并结果与各组先后无关。

**反例。** 两组把同一个入口字段一个声明成 integer、一个声明成 number：修复前合并出来的类型跟着**第一组**走
（`"string" if "string" in (old, new) else old`），第一组说整数，后面那组填小数就在入口表单被拒收——同一份草稿，只是
两组的先后不同，出的图和试跑结果就不一样。

**真实入口。** 各组草案由 draftplan.merge_entry_fields / _merge_fields 合并（起草后台 authoring 用它写 payload 的
entry_fields，build_graph 用它写 start 节点的 fields），合并出来的类型进 apps.forms.input_schema，试跑、组合式问数、
出具都按它收值（api/ledger.trial_caliber、草稿试跑、ledger/compose、ledger/issue 里的 validate_form）。所以这条规则
不是辅助函数内部的事：改错了值就进不来，改松了不该收的值就进来了。

规则（draftplan._merged_type）：数 + 整数并成数（数能填小数，收得下整数那一组的值，反过来 3.5 会被「整数」拒收）；
文字跟谁都能并；是或否和数（整数）不能互相表示，退回文字；认不出的写法（date、int、写空）按文字算。合并结果永远
落在 ENTRY_TYPES 里，两边对调结果一样。

覆盖：正序 / 逆序 / 三条以上混合顺序 / 非法类型 / 建图 / 入口表单 / 真引擎试跑。
"""
from __future__ import annotations

import itertools
import json
import sqlite3

import pytest
from sqlalchemy import select

from app.api import ledger as api_ledger
from app.db.base import SessionLocal
from app.db.models import DataSource, Run
from app.engine.runner import run_manager
from app.engine.schema import GraphSpec, validate_graph
from app.ledger import draftplan
from tests.test_ledger_docdraft_api import (
    REAL_RUN_TRIAL, client, datasource, documents, extract, fake_model, plan_reply,
)

#: 这个文件里每一组都声明的同一个入口字段：要合并的就是它的类型
FIELD = "threshold"
#: 能认的四种类型（draftplan.ENTRY_TYPES），加上几种认不出的写法（大小写、多空格也不放宽：入口这一层只认原样的四个词）
KINDS = (*draftplan.ENTRY_TYPES, "date", "int", None, "", "INTEGER ")


def field(kind, *, required: bool = True, **extra) -> dict:
    return {"name": FIELD, "type": kind, "required": required, **extra}


def block(kind, *, sql: str = "SELECT 1 AS gmv FROM orders", metrics: list[dict] | None = None) -> dict:
    """一组草案：一条取数 SQL、一份入口字段声明、几个指标。"""
    return {"sql": sql, "entry_fields": [field(kind)], "metrics": list(metrics or [])}


def merged_type(*kinds) -> str:
    """按各组先后合并同一个字段，返回合并后的类型（也顺带确认每组都只声明这一个字段）。"""
    fields, _ = draftplan.merge_entry_fields([block(kind) for kind in kinds])
    assert [f["name"] for f in fields] == [FIELD]
    return fields[0]["type"]


def start_fields(graph: dict) -> list[dict]:
    return next(node for node in graph["nodes"] if node["id"] == "start")["data"]["config"]["fields"]


# ==========================================================================
# 合并规则：正序、逆序、混合顺序、非法类型
# ==========================================================================

def test_integer_and_number_merge_to_number_whichever_group_comes_first():
    """R02 的最小反例：两组一个整数、一个数，两个先后都并成数（修复前跟着第一组走）。"""
    assert merged_type("integer", "number") == "number"
    assert merged_type("number", "integer") == "number"
    fields, notes = draftplan.merge_entry_fields([block("integer"), block("number")])
    assert fields[0]["type"] == "number"
    assert any(FIELD in note and "类型" in note and "number" in note for note in notes)


@pytest.mark.parametrize("left,right", list(itertools.permutations(KINDS, 2)))
def test_merging_a_pair_does_not_depend_on_which_group_comes_first(left, right):
    """任意两种声明（含认不出的写法）对调先后，合并出来的类型一样，而且落在 ENTRY_TYPES 里。"""
    forward, backward = merged_type(left, right), merged_type(right, left)
    assert forward == backward
    assert forward in draftplan.ENTRY_TYPES


@pytest.mark.parametrize("kinds", list(itertools.permutations(["integer", "number", "string"]))
                         + list(itertools.permutations(["integer", "number", "boolean"]))
                         + list(itertools.permutations(["integer", "number", "boolean", "string"])))
def test_three_or_more_groups_merge_the_same_in_any_order(kinds):
    """三条以上混合顺序：合并结果只跟这几种声明有关，跟它们谁先谁后无关。"""
    assert merged_type(*kinds) == merged_type(*reversed(kinds))


def test_a_multiset_of_declarations_always_merges_to_one_result():
    """穷举（含认不出的写法）：同一组声明的任意先后只并出一个类型，重复声明、三条以上也一样。"""
    kinds = [*draftplan.ENTRY_TYPES, "date", "int", "", None]
    for size in (2, 3, 4):
        for declared in itertools.product(kinds, repeat=size):
            got = {merged_type(*order) for order in set(itertools.permutations(declared))}
            assert len(got) == 1, (declared, got)


@pytest.mark.parametrize("kinds,expect", [
    (("integer", "number"), "number"),
    (("number", "integer", "number"), "number"),
    (("string", "number"), "string"),
    (("string", "integer"), "string"),
    (("boolean", "integer"), "string"),
    (("boolean", "number"), "string"),
    (("integer", "number", "boolean"), "string"),
])
def test_the_merged_type_is_the_wider_one(kinds, expect):
    """并出来的类型：数收得下整数（也收得下小数），文字跟谁都能并，是或否与数不能互相表示就退回文字。"""
    assert merged_type(*kinds) == expect


@pytest.mark.parametrize("kind", ["date", "int", "float", "INTEGER ", "", None, 3, ["number"]])
def test_declarations_we_do_not_recognise_count_as_text(kind):
    """认不出的声明按文字算（和 _entry_fields、apps.forms.input_schema 一个认法）：跟谁并都并成文字，也不照抄进图里。"""
    assert merged_type(kind) == "string"
    assert merged_type(kind, "integer") == "string"
    assert merged_type("integer", kind) == "string"
    assert merged_type(kind, kind) == "string"


def test_an_unrecognised_declaration_is_normalised_even_with_one_group():
    """认不出的声明按文字算（和 _entry_fields 同一份判断）：只有一组时也规范化，不把 date 之类的原样写进图。"""
    fields, notes = draftplan.merge_entry_fields([block("date")])
    assert fields[0]["type"] == "string" and notes == []
    fields, notes = draftplan.merge_entry_fields([block("number")])
    assert fields[0]["type"] == "number" and notes == []


# ==========================================================================
# 建图：start 节点的 fields 与组序无关
# ==========================================================================

def test_build_graph_start_fields_do_not_depend_on_group_order():
    """建图走的是同一条合并规则：start 节点的 fields（图里的入口契约）与 payload 里的入口字段一致，两序相同。"""
    metric = {"id": "m1", "name": "销售额", "expression": "cell(nodes.fetch, 0, 'gmv')"}
    first = block("integer", metrics=[metric])
    second = block("number", metrics=[{**metric, "id": "m2", "name": "订单数",
                                      "expression": "cell(nodes.fetch, 0, 'cnt')"}])
    forward = draftplan.build_graph([first, second], name="金额下限", datasource="shop")
    backward = draftplan.build_graph([second, first], name="金额下限", datasource="shop")
    assert start_fields(forward) == start_fields(backward) == \
        draftplan.merge_entry_fields([first, second])[0]
    assert start_fields(forward)[0]["type"] == "number"
    for graph in (forward, backward):
        result = validate_graph(GraphSpec.model_validate(graph))
        assert result.ok, [issue.message for issue in result.issues if issue.level == "error"]


def test_the_entry_form_takes_whole_and_fractional_values_after_merging():
    """合并出来的类型就是入口表单收值用的那一份：并成数之后 3 和 3.5 都收得下（并成整数时 3.5 会被拒）。"""
    from app.apps.forms import input_schema, validate_form

    values = {FIELD: 3.5}
    for kinds in (("integer", "number"), ("number", "integer")):
        fields, _ = draftplan.merge_entry_fields([block(kind) for kind in kinds])
        schema = input_schema(fields)
        assert schema["properties"][FIELD]["type"] == "number"
        assert validate_form(schema, values) == []
        assert validate_form(schema, {FIELD: 3}) == []
    # 对照：只声明成整数时小数确实收不下（修复前 integer 在前的顺序就是并成这个，试跑会 422）
    integer_schema = input_schema(draftplan.merge_entry_fields([block("integer")])[0])
    assert validate_form(integer_schema, values), "整数类型不收小数"


# ==========================================================================
# 真引擎：正序、逆序各跑一次，小数进到 SQL 里
# ==========================================================================

async def _orders_draft(client, documents, datasource, monkeypatch, tmp_path, *, first_type: str):
    """从文档起草一份两组的草稿：同一个入口字段一组声明成 first_type、另一组声明成另一种，试跑走真引擎。

    门店库换成临时文件（orders 五行，>= 2.5 的是 5、2.5、3 三行）；两条 SQL 都用裸写的 `amount >= {{ input.threshold }}`
    （裸写位置只收数）。返回（草稿 id, 草稿详情）。
    """
    doc = await documents("dd-merge-" + first_type)
    source_id = await datasource()
    path = tmp_path / f"merge-{first_type}.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE orders (status TEXT, amount REAL)")
        db.executemany("INSERT INTO orders VALUES (?, ?)",
                       [("paid", 5.0), ("paid", 2.5), ("paid", 2.0), ("paid", 3.0), ("refunded", 100.0)])
        db.commit()
    async with SessionLocal() as session:
        source = await session.get(DataSource, source_id)
        source.database, source.readonly, source.enabled = str(path), True, True
        await session.commit()

    gmv, count = json.loads(plan_reply(f"{doc}-2")), json.loads(plan_reply(f"{doc}-2"))
    other = "number" if first_type == "integer" else "integer"
    gmv["entry_fields"] = [field(first_type, required=True, description="金额下限")]
    gmv["sql"] = "SELECT SUM(amount) AS gmv FROM orders WHERE status = 'paid' AND amount >= {{ input.threshold }}"
    gmv["metrics"] = [{**gmv["metrics"][0], "id": "m1", "name": "金额下限以上销售额",
                       "expression": "cell(nodes.fetch, 0, 'gmv')"}]
    gmv["uncovered"] = ["金额下限以上订单数"]
    count["entry_fields"] = [field(other, required=True, description="金额下限")]
    count["sql"] = "SELECT COUNT(*) AS cnt FROM orders WHERE status = 'paid' AND amount >= {{ input.threshold }}"
    count["metrics"] = [{**count["metrics"][0], "id": "m1", "name": "金额下限以上订单数",
                         "expression": "cell(nodes.fetch, 0, 'cnt')"}]
    count["uncovered"] = []
    fake_model(monkeypatch, json.dumps(gmv, ensure_ascii=False), json.dumps(count, ensure_ascii=False))
    monkeypatch.setattr(api_ledger, "_run_trial", REAL_RUN_TRIAL)

    created = await extract(client, doc, source_id)
    made = (await client.get("/api/ledger/drafts/" + created["draft_id"])).json()
    assert made["status"] in ("ready", "questions_open"), made.get("error")
    assert made["blocks"] == 2, made.get("blocks")
    return created["draft_id"], made


async def _cleanup_run(run_id: str) -> None:
    async with SessionLocal() as session:
        row = await session.get(Run, run_id)
        if row is not None:
            await session.delete(row)
            await session.commit()


@pytest.mark.parametrize("first_type", ["integer", "number"])
async def test_a_fractional_entry_value_reaches_sql_whichever_group_comes_first(
        client, documents, datasource, monkeypatch, tmp_path, first_type):
    """完整请求链：文档起草两组 → 合并入口类型 → 草稿图 → 试跑入口校验 → 真引擎跑一次。

    两组的先后不同，草稿里的入口类型、试跑收到的值（2.5 没被截成 2）和两条 SQL 的结果都该一样。
    """
    await run_manager.setup()
    run_id = ""
    try:
        draft_id, made = await _orders_draft(client, documents, datasource, monkeypatch, tmp_path,
                                             first_type=first_type)
        fields = {f["name"]: f for f in made["entry_fields"]}
        assert fields[FIELD]["type"] == "number", made["entry_fields"]
        assert any(FIELD in note and "类型" in note for note in made["entry_notes"])

        trial = (await client.post(f"/api/ledger/drafts/{draft_id}/trial",
                                   json={"inputs": {FIELD: 2.5}})).json()
        run_id = str(trial.get("run_id") or "")
        assert trial["status"] == "succeeded", trial.get("error")
        assert {m["id"]: m["value"] for m in trial["metrics"]} == {"m1": 10.5, "m2": 3}, trial["metrics"]
        # 两条取数各自把 2.5 渲染进 SQL：值被当成整数截断的话这里会是 12.5 和 4
        rows = {query["node_id"]: query["rows"] for query in trial["queries"]}
        assert sorted(rows) == ["fetch_1", "fetch_2"]
        assert sorted(row[0] for got in rows.values() for row in got) == [3, 10.5]
    finally:
        await run_manager.shutdown()
        await _cleanup_run(run_id)
