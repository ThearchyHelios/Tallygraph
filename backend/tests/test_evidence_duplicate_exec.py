"""K10：同一份查询快照被执行两次（两条一模一样的取数）时的证据身份与消费。

同一次运行里两条完全相同的查询（循环里同一个节点跑两次、或者两个节点写了同一条 SQL）结果一样、查询
快照的内容 id 也一样（内容寻址：同内容只存一份）。以前按 artifact 认，第二次会把第一次盖掉：

- 封存读取（engine/evidence_reader）：sealed.queries 是按 artifact 的，两条并成一条；
- 目录与 make_eid（engine/evidence）：两条 query 条目的 eid 一模一样，图上、文档里分不出是哪一次；
- HTTP 证据（api/evidence）：点开 [[v:Q2.r0.x]] 时出处链又把 artifact 反查成 Q1；
- 应用取快照（apps/resolve）：同一节点的两次执行并成一条，exec 取第几次就不准了。

这份文件盯住这几条，并保证旧行为不变：**新条目**（带 query_id / execution_id / attempt）按
(artifact, node_id, exec, execution_id) 这一类复合身份索引，同一份快照两次执行各得一个 eid、两条点得开的
记录；**老条目**（一个执行身份字段都没有）照旧只按 artifact 算 eid、按 artifact 找，一个字都不变；
旧文档、复算、重放不变量都不受影响。

数据全是合成的（虚构零售库的一份地区销售额快照、真跑一次 app.analysis 的结论），落成一次封存运行后走真实的
HTTP 证据接口与真实的复算，不连真实业务库、不读密钥。工具/模型可见的结果里不因此多出 SQL 正文或绑定值。
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.analysis.contract import ARTIFACT_KIND, ledger_entry
from app.apps.resolve import _query_artifact
from app.core import artifact_store
from app.core.artifact_store import canonical_json, content_hash, manifest_hash
from app.db.base import SessionLocal
from app.db.models import Run, RunEvent
from app.engine.evidence import build_catalog, compose_doc, iter_segments, make_eid, writer_catalog
from app.engine.evidence_reader import read_sealed
from app.main import app
from tests.test_analysis_evidence import SNAPSHOT, analyzed

GRAPH = {"nodes": [{"id": "a", "type": "tool", "data": {"label": "取数"}},
                   {"id": "b", "type": "tool", "data": {"label": "取数（同一条 SQL）"}},
                   {"id": "an", "type": "agent", "data": {"label": "分析"}},
                   {"id": "write", "type": "report", "data": {"label": "写周报"}}]}
#: 报告正文同时引两次：Q1 是第一次执行、Q2 是第二次
TEXT = "第一次 [[v:Q1.r0.gmv]]，第二次 [[v:Q2.r0.gmv]]。"
#: 这份快照进工件库后的 id（内容寻址，先算得出，好让合成的分析结论指向它）
SNAPSHOT_ID = content_hash(canonical_json(SNAPSHOT))
#: 换个 sql 就换了一份快照（同一个节点在循环里跑两次、SQL 不重样的那种）
SNAPSHOT_B = {**SNAPSHOT, "sql": SNAPSHOT["sql"] + " LIMIT 100"}


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def step_of(node_id: str, *, exec_no: int = 1, execution_id: str | None = None, query_id: str | None = None,
            attempt: int | None = None, snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    """合成的一步：事件节点、第几次执行、执行身份、（可选）换一份快照内容。"""
    return {"node_id": node_id, "exec": exec_no, "snapshot": snapshot or SNAPSHOT,
            **{k: v for k, v in (("query_id", query_id), ("execution_id", execution_id), ("attempt", attempt))
               if v is not None}}


async def sealed_run(steps: list[dict[str, Any]], text: str, analysis: dict[str, Any] | None = None) -> dict[str, Any]:
    """落一次封存好的运行：每一步一个 node.finished（带一条 query 台账条目），（可选）加一份分析结论，
    再按真实的写法组报告文档、按 runner 的算法封存。两步的 snapshot 一样就是「同一份快照被执行两次」。"""
    run_id = uuid.uuid4().hex
    events: list[tuple[int, str, str | None, dict[str, Any]]] = [(1, "run.started", None, {"input": {}})]
    entries: list[dict[str, Any]] = []
    for spec in steps:
        node_id = str(spec["node_id"])
        content = spec.get("snapshot") or SNAPSHOT
        artifact = await artifact_store.put_json(content, kind="query_snapshot", run_id=run_id, node_id=node_id)
        entry = {"kind": "query", "node_id": node_id, "exec": spec.get("exec", 1), "artifact": artifact,
                 "tool": "db_query__shop", "source": "shop", "columns": list(content["columns"]),
                 "rows": len(content["rows"]), "truncated": False,
                 **{k: spec[k] for k in ("query_id", "execution_id", "attempt") if k in spec}}
        entries.append(entry)
        events.append((len(events) + 1, "node.finished", node_id, {"evidence": [entry]}))
    ledger = list(entries)
    if analysis is not None:
        made = await artifact_store.put_json(analysis, kind=ARTIFACT_KIND, run_id=run_id, node_id="an")
        aentry = ledger_entry(analysis, node_id="an", exec_no=1, artifact=made, item="drop")
        ledger.append(aentry)
        events.append((len(events) + 1, "node.finished", "an", {"evidence": [aentry]}))
    catalog = build_catalog(nodes={}, ledger=ledger)
    doc = compose_doc(text, catalog, node_id="write", run_id=run_id)
    doc_id = await artifact_store.put_json(doc, kind="report_doc", run_id=run_id, node_id="write")
    events.append((len(events) + 1, "report.checked", "write", {"doc_artifact": doc_id, "ok": True}))
    events.append((len(events) + 1, "run.finished", None, {"output": {}}))
    async with SessionLocal() as session:
        session.add(Run(id=run_id, workflow_name="重复执行", status="succeeded", run_class="exploratory",
                        graph=GRAPH, agent_limits={}, manifest_hash=manifest_hash(events),
                        manifest_seq=events[-1][0]))
        await session.flush()
        for seq, etype, node_id, data in events:
            session.add(RunEvent(run_id=run_id, seq=seq, type=etype, node_id=node_id, ts=float(seq), data=data))
        await session.commit()
    return {"run": run_id, "doc": doc, "artifact": entries[0]["artifact"], "entries": entries, "catalog": catalog}


def seg_id(doc: dict[str, Any], ref: str) -> str:
    return next(s["id"] for s in iter_segments(doc) if s.get("ref") == ref)


async def chain_of(client: AsyncClient, made: dict[str, Any], ref: str) -> list[dict[str, Any]]:
    seg = seg_id(made["doc"], ref)
    body = (await client.get(f"/api/runs/{made['run']}/evidence/segments/{seg}")).json()
    return body["chain"]


#: 同一个节点跑两次，两次都是同一条 SQL（内容一样、工件 id 相同）
TWO_EXECS = [step_of("a", exec_no=1, execution_id="run:a:1", query_id="wf:a", attempt=1),
             step_of("a", exec_no=2, execution_id="run:a:2", query_id="wf:a", attempt=2)]


# ==========================================================================
# make_eid / 目录：新条目两次执行各一个 eid，老条目一字不差
# ==========================================================================


def test_a_snapshot_executed_twice_gets_two_eids_on_new_entries():
    """新条目按执行身份算 eid：同一份 artifact、同一个节点跑两次也各得一个，目录里不再 Q1、Q2 同 id。"""
    one = {"kind": "query", "node_id": "a", "exec": 1, "artifact": "A" * 64, "execution_id": "run:a:1",
           "query_id": "wf:a", "attempt": 1}
    two = {"kind": "query", "node_id": "a", "exec": 2, "artifact": "A" * 64, "execution_id": "run:a:2",
           "query_id": "wf:a", "attempt": 2}
    catalog = build_catalog(nodes={}, ledger=[one, two])
    assert catalog["Q1"]["eid"] != catalog["Q2"]["eid"], "同一份工件两次执行必须是两条可定位的记录"
    assert catalog["Q1"]["execution_id"] == "run:a:1" and catalog["Q2"]["execution_id"] == "run:a:2"
    assert catalog["Q1"]["query_id"] == catalog["Q2"]["query_id"] == "wf:a"      # 逻辑身份相同
    assert catalog["Q1"]["artifact"] == catalog["Q2"]["artifact"]


def test_old_entries_keep_the_artifact_only_eid():
    """老条目（没有执行身份）的 eid 与以前一字不差：不把所有旧 eid 重算。"""
    catalog = build_catalog(nodes={}, ledger=[{"kind": "query", "node_id": "a", "exec": 1, "artifact": "B" * 64}])
    assert catalog["Q1"]["eid"] == make_eid("query", "B" * 64, {})
    assert "execution_id" not in catalog["Q1"]


def test_the_model_visible_catalog_leaks_no_sql_or_binding_values():
    """工具/模型可见的那一份（写作目录）里不因此多出 SQL 正文或绑定值：新加的只有执行身份。"""
    one = {"kind": "query", "node_id": "a", "exec": 1, "artifact": "C" * 64, "execution_id": "run:a:1",
           "query_id": "wf:a", "attempt": 1}
    two = {"kind": "query", "node_id": "a", "exec": 2, "artifact": "C" * 64, "execution_id": "run:a:2",
           "query_id": "wf:a", "attempt": 2}
    text = writer_catalog(build_catalog(nodes={}, ledger=[one, two]), loader=lambda _a: SNAPSHOT).text
    assert "SELECT" not in text and "run:a:" not in text
    assert "Q1" in text and "Q2" in text


# ==========================================================================
# 封存读取：两次执行逐条留着，按 exec / execution_id 取第几次
# ==========================================================================


async def test_sealed_read_keeps_both_executions_and_selects_the_second():
    made = await sealed_run(TWO_EXECS, TEXT)
    sealed = await read_sealed(made["run"])

    records = sealed.query_records_of("a")
    assert [r["execution_id"] for r in records] == ["run:a:1", "run:a:2"], "两次执行各留一条"
    assert {r["artifact"] for r in records} == {made["artifact"]}, "内容一样、工件 id 相同"
    # 按执行身份取：exec=2 取第二次，不是被盖住的那一条
    assert sealed.query_at(made["artifact"], node_id="a", exec_no=2)["execution_id"] == "run:a:2"
    assert sealed.query_at(made["artifact"], node_id="a", exec_no=1)["execution_id"] == "run:a:1"
    assert sealed.query_at(made["artifact"], node_id="a", execution_id="run:a:2")["execution_id"] == "run:a:2"
    # 兼容：按 artifact 的那份映射照旧（老消费方按它认「这次运行交回过这件快照」）
    assert sealed.queries[made["artifact"]]["node_id"] == "a"
    assert set(sealed.queries) == {made["artifact"]}


# ==========================================================================
# HTTP 证据：图上是两条、点开认得出是哪一次
# ==========================================================================


async def test_http_evidence_keeps_two_locatable_records(client):
    made = await sealed_run(TWO_EXECS, TEXT)
    body = (await client.get(f"/api/runs/{made['run']}/evidence")).json()
    assert body["mode"] == "cited"
    queries = {e["alias"]: e for e in body["evidence"] if e["kind"] == "query"}
    assert set(queries) == {"Q1", "Q2"}
    assert queries["Q1"]["eid"] != queries["Q2"]["eid"]          # Q eid 不再冲突
    assert queries["Q1"]["artifact"] == queries["Q2"]["artifact"] == made["artifact"]
    assert all(e["sealed"] is True for e in queries.values())

    # 点开 [[v:Q2.r0.gmv]]：出处链认的是**第二次**执行，不是被盖掉的第一次
    step = next(s for s in await chain_of(client, made, "v:Q2.r0.gmv") if s["step"] == "query")
    assert step["alias"] == "Q2" and step["artifact"] == made["artifact"]
    assert step["execution_id"] == "run:a:2" and step["attempt"] == 2 and step["query_id"] == "wf:a"
    first = next(s for s in await chain_of(client, made, "v:Q1.r0.gmv") if s["step"] == "query")
    assert first["alias"] == "Q1" and first["execution_id"] == "run:a:1" and first["attempt"] == 1


async def test_old_runs_query_steps_are_unchanged(client):
    """老条目（只有 exec）、老文档：查询步骤里一个执行身份字段都不加，eid 还是按 artifact 算的那个——
    期 4 之前一字不差的承诺不因此破掉。"""
    made = await sealed_run([
        step_of("a", exec_no=1),
        step_of("a", exec_no=2),
    ], TEXT)
    step = next(s for s in await chain_of(client, made, "v:Q2.r0.gmv") if s["step"] == "query")
    assert step["node_id"] == "a" and step["artifact"] == made["artifact"]
    assert {"query_id", "execution_id", "attempt"} & set(step) == set()
    body = (await client.get(f"/api/runs/{made['run']}/evidence")).json()
    queries = {e["alias"]: e for e in body["evidence"] if e["kind"] == "query"}
    assert queries["Q1"]["eid"] == queries["Q2"]["eid"] == make_eid("query", made["artifact"], {})
    # exec=2 仍然取第二次（老条目也记 exec，逐次记的那份分得开）
    sealed = await read_sealed(made["run"])
    assert sealed.query_at(made["artifact"], node_id="a", exec_no=2)["exec"] == 2
    assert len(sealed.query_records_of("a")) == 2


async def test_the_two_records_are_locatable_across_two_nodes(client):
    """两个节点写同一条 SQL：同一份快照、两个节点，各认各的。"""
    made = await sealed_run([
        step_of("a", execution_id="run:a:1", query_id="wf:a"),
        step_of("b", execution_id="run:b:1", query_id="wf:b"),
    ], TEXT)
    sealed = await read_sealed(made["run"])
    assert [r["node_id"] for r in sealed.query_records] == ["a", "b"]
    step = next(s for s in await chain_of(client, made, "v:Q2.r0.gmv") if s["step"] == "query")
    assert step["alias"] == "Q2" and step["node_id"] == "b" and step["execution_id"] == "run:b:1"
    first = next(s for s in await chain_of(client, made, "v:Q1.r0.gmv") if s["step"] == "query")
    assert first["alias"] == "Q1" and first["node_id"] == "a"


# ==========================================================================
# 应用取快照：exec 指的是第几次**执行**（同一份内容重复出现也不并成一条）
# ==========================================================================


async def test_app_selects_the_nth_execution_not_the_nth_distinct_snapshot():
    """一个节点跑三次、快照内容是 A、B、A：按 artifact 去重只剩两条（A、B），exec=3 / exec=2 都会取错；
    按执行顺序逐条记才对得上。"""
    made = await sealed_run([
        step_of("a", exec_no=1, execution_id="run:a:1", snapshot=SNAPSHOT),
        step_of("a", exec_no=2, execution_id="run:a:2", snapshot=SNAPSHOT_B),
        step_of("a", exec_no=3, execution_id="run:a:3", snapshot=SNAPSHOT),
    ], "[[v:Q1.r0.gmv]]、[[v:Q2.r0.gmv]]、[[v:Q3.r0.gmv]]。")
    sealed = await read_sealed(made["run"])
    source = SimpleNamespace(sealed=sealed)
    first, second, third = (e["artifact"] for e in made["entries"])
    assert first == third != second
    assert _query_artifact(source, "a", 1) == first
    assert _query_artifact(source, "a", 2) == second
    assert _query_artifact(source, "a", 3) == third, "第三次执行取的是第三次，不是按内容去重后的最后一条"
    assert _query_artifact(source, "a") == third, "不给 exec 取最后一次"


# ==========================================================================
# 复算：快照被执行两次时，分析结论的输入与复算照旧认得出来
# ==========================================================================


async def test_recompute_still_locates_the_input_when_the_snapshot_ran_twice(client):
    """分析结论的输入快照恰好也被执行了两次：出处链照样接上查询步骤，复算用封存的原内容重算、逐字段一致。"""
    result = await analyzed({"Q2": (SNAPSHOT, SNAPSHOT_ID)})
    made = await sealed_run(TWO_EXECS, "按地区拆 [[a:A1]]。[[see:A1]]", analysis=result)
    assert made["artifact"] == SNAPSHOT_ID                # 合成的结论指的就是这次运行的那份快照
    chain = await chain_of(client, made, "a:A1")
    assert "analysis_input" in {s["step"] for s in chain}
    recheck = next(s for s in chain if s["step"] == "analysis_recompute")
    assert recheck["status"] == "same", f"复算应按封存的原内容算出同一份结论：{recheck}"
    # 输入快照那次查询照样接出查询步骤（同一份工件，不因执行了两次而接不上）
    assert any(s["step"] == "query" and s["artifact"] == SNAPSHOT_ID for s in chain)
