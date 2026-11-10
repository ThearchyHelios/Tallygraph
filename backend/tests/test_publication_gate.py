"""K07 的用途判定：requires_proof、正式身份、探索不受限（任务书 4.9，E10/E15）。

这份文件盯住这几条：

1. **正式身份只有一个入口。** 正式运行（engine/launch.launch_run）和按口径出具（ledger/issue.check_target）在
   requires_proof 时，必须有一份**就是这一版这一张图的**（图指纹对得上、图上的源头口径卡一张不缺）、覆盖当前
   定义 + 绑定 + 取数语义、最新的那条验证记录是 matched、且认证确认盖住这份记录全部指标**的发布依据；部分审阅不行，
   缺记录/旧记录不覆盖/错图/缺卡各有机器码，别的入口绕不过去。两条入口对同一份依据不足给的机器码和身份完全一致。
   **转抄卡（caliber_from）固定引用的上游那张卡按同一个用途递归判**：上游要证明就得同样满足，失败按上游那条机器码
   加下游引用拒；只判被引用那张卡、循环引用截断、上游是老资产的不升级。
2. **requires_proof = 发布时要求过证明（事件行上的 required）或现在还接管着**（apps/versions.taken_over），两者取或；
   引用的上游要证明也算。清标签不能让已经按严格发的那些版本降级，普通 PATCH 也清不掉接管标记。老资产（这一版没有
   清单）保持原行为，只是身份记 formal_legacy；不需要证明的记 formal_unproven——**都不是 verified**。
3. **试算、探索、组合不因缺验证审阅被误伤**（A03/A07）：purpose 由服务端入口常量决定，只有正式运行和出具会拦。
4. **运行来源/日志保留实际清单、版本和身份**（要求 5）；来源里的 explicit 照实记调用方有没有点名版本——默认发起
   （不传 version）是 false，只按推荐指针取一次并固定。
5. **删掉没验证过的语义字段不会把旧依据升成通过**（E15/R07）。
6. **依据只能在发布那一刻定下来。** 发布之后补的验证记录/审阅不会把这一版的依据补成通过（不在核对发布事件时另挑
   更新的记录），清单里没有的卡也不会现场挑一条记录顶上；图里每张源头口径卡各要一份自己的依据，一张齐了不算另一张
   也齐；之后又试运行过一次、那一次不是 matched 的，说明最新一次核对的结论就不是「对得上」，不拿更早那条顶上。

数据是虚构店铺库（tests/ledger_samples），跑的是真实入口；正式运行在假模型下跑完，不连真实业务库。
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.apps import publications
from app.db.base import SessionLocal
from app.core import artifact_store
from app.db.models import Run, RunEvent, Workflow
from app.engine.runner import run_manager
from app.ledger import validation
from app.main import app
from tests import ledger_samples as S
from tests.publication_samples import (CARD, add_record, add_version, confirm, launch_formal, latest_event,
                                       issue_plan, make_workflow, persisted, publication_of, publish, report_model, wait_run)

@pytest.fixture(autouse=True)
async def engine_up(tmp_path):
    await run_manager.setup()
    await S.fresh_source(tmp_path / "shop.db")
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def principal(role: str = "builder"):
    from app.api.deps import Principal

    return Principal(username="boss", role=role, mode="off", via="off")


def two_metric_graph():
    """同一张卡上两条指标（都读 fetch 的 gmv 列）：用来演「只确认了一部分」。"""
    graph = S.caliber_graph()
    card = next(node for node in graph["nodes"] if node["id"] == CARD)
    card["data"]["config"]["metrics"].append(
        {"id": "orders", "name": "订单数", "unit": "单", "expression": "cell(nodes.fetch, 0, 'gmv') * 2"})
    return graph


def two_card_graph():
    """同一张取数上两张源头口径卡：用来演「只证明了其中一张，另一张不能跟着算过」。"""
    graph = S.caliber_graph()
    graph["nodes"].insert(3, S.node("card2", "metrics", caliber="退款额", caliber_version="v1",
                                    metrics=[{"id": "refunds", "name": "退款额",
                                              "expression": "cell(nodes.fetch, 0, 'gmv')"}]))
    return graph


def graph_without_definition():
    """同一张卡、只把「没验证过的那段业务描述」删掉（语义字段变了，定义指纹跟着变）。"""
    graph = S.caliber_graph()
    card = next(node for node in graph["nodes"] if node["id"] == CARD)
    card["data"]["config"]["metrics"][0].pop("definition", None)
    return graph


def graph_with_definition():
    graph = S.caliber_graph()
    card = next(node for node in graph["nodes"] if node["id"] == CARD)
    card["data"]["config"]["metrics"][0]["definition"] = {"text": "只算已支付订单的销售额", "exclusions": ["退款"]}
    return graph


# --------------------------------------------------------------------------
# 1、正式运行：依据齐全才发得起来，身份与清单记在来源和日志里
# --------------------------------------------------------------------------


async def test_a_proven_asset_pins_its_manifest_identity_and_log(client, monkeypatch):
    report_model(monkeypatch)
    asset = await make_workflow()
    record_id = await add_record(asset)
    await confirm(asset, record_id)
    out = await publish(client, asset)
    run = await launch_formal(asset)
    assert run.version == 1
    info = publication_of(run)
    assert info["manifest"] == out["manifest"] and info["version"] == 1
    assert info["identity"] == publications.PROVEN and info["requires_proof"] is True
    assert info["publication"] and info["purpose"] == publications.PURPOSE_FORMAL
    done = await wait_run(run.id)
    assert done.status == "succeeded", done.error
    # 事件流里也有一条：回看这次运行能对上是哪一份清单
    async with SessionLocal() as session:
        events = (await session.execute(select(RunEvent).where(RunEvent.run_id == run.id))).scalars().all()
    [pinned] = [e for e in events if isinstance(e.data, dict) and e.data.get("code") == "publication.pinned"]
    assert pinned.data["manifest"] == out["manifest"] and pinned.data["identity"] == publications.PROVEN
    assert pinned.data["version"] == 1 and "发布依据齐全" in pinned.data["message"]
    # 运行来源里也记着（run.origin 事件）
    [origin_event] = [e for e in events if e.type == "run.origin"]
    assert origin_event.data["publication"]["manifest"] == out["manifest"]


@pytest.mark.parametrize("missing", ["record", "unmatched", "review", "rejected", "partial"])
async def test_formal_run_and_issue_are_refused_with_the_same_code(client, missing):
    """缺记录 / 不是 matched / 没有审阅 / 被驳回 / 只确认了一部分：两条入口逐个明确拒，机器码和身份一致。"""
    from app.engine.launch import LaunchRefused
    from app.ledger import issue as ledger_issue

    asset = await make_workflow(graph=two_metric_graph())
    if missing != "record":
        metric_ids = ("gmv", "orders")
        record_id = await add_record(asset, status="observed" if missing == "unmatched" else "matched",
                                     metric_ids=metric_ids)
        if missing == "rejected":
            await confirm(asset, record_id, decision="rejected", scope=list(metric_ids))
        elif missing == "partial":
            await confirm(asset, record_id, scope=["gmv"])
        elif missing == "unmatched":
            await confirm(asset, record_id, scope=list(metric_ids))
        # missing == "review"：有记录、没有审阅
    await publish(client, asset)
    with pytest.raises(LaunchRefused) as caught:
        await launch_formal(asset)
    expected = {"record": publications.RECORD_MISSING, "unmatched": publications.RECORD_NOT_MATCHED,
                "review": publications.REVIEW_MISSING, "rejected": publications.REVIEW_REJECTED,
                "partial": publications.REVIEW_PARTIAL}[missing]
    assert caught.value.code == expected, caught.value
    assert "发布依据" in caught.value.message
    # 按口径出具走的是同一套判定：机器码、身份、要求证明这三样一模一样（出具这条路没有另一套说法）
    async with SessionLocal() as session:
        plan = await issue_plan(session, asset)
    assert plan.code == expected and plan.publication["identity"] == publications.UNPROVEN
    assert plan.publication["requires_proof"] is True and plan.publication["manifest"]
    assert "发布依据" in plan.reason


async def test_an_unauthenticated_review_is_refused_when_login_is_on(client, monkeypatch):
    """开着登录的部署里，审阅必须是认证主体做的（K05 记的 verified）——自报署名不算正式依据。"""
    from app import auth
    from app.engine.launch import LaunchRefused

    asset = await make_workflow(graph=two_metric_graph())
    record_id = await add_record(asset, metric_ids=("gmv", "orders"))
    await confirm(asset, record_id, verified=False, scope=["gmv", "orders"])
    await publish(client, asset)
    monkeypatch.setattr("app.auth.current_mode", lambda: "local")
    assert auth.current_mode() == "local"
    with pytest.raises(LaunchRefused) as caught:
        await launch_formal(asset)
    assert caught.value.code == publications.REVIEW_UNVERIFIED
    monkeypatch.undo()
    # 认证过的审阅照常放行
    record2 = await add_record(asset, metric_ids=("gmv", "orders"), status="matched")
    await confirm(asset, record2, verified=True, scope=["gmv", "orders"])
    await publish(client, asset)
    assert publication_of(await launch_formal(asset))["identity"] == publications.PROVEN


async def test_a_record_that_no_longer_covers_the_version_is_refused(client):
    """定义/取数改过之后，旧记录不算覆盖新版本：正式运行明确拒，不回退成「没要求证明」。"""
    from app.engine.launch import LaunchRefused

    asset = await make_workflow()
    record_id = await add_record(asset)
    await confirm(asset, record_id)
    await publish(client, asset)
    asset = await add_version(asset, S.caliber_graph(S.TEMPLATE.replace("'paid'", "'open'")), version=2)
    out = await publish(client, asset, version=2)
    assert out["publication"]["problems"][0]["code"] == publications.RECORD_STALE
    with pytest.raises(LaunchRefused) as caught:
        await launch_formal(asset, version=2)
    assert caught.value.code == publications.RECORD_STALE
    # 老的 v1 仍然按它自己那份依据发起（清单是那一版的）
    run = await launch_formal(asset, version=1)
    assert publication_of(run)["identity"] == publications.PROVEN


async def test_the_http_entry_refuses_with_the_same_machine_code(client):
    asset = await make_workflow()
    await publish(client, asset)                            # 没有任何验证记录
    res = await client.post("/api/runs", json={"workflow_id": asset["workflow_id"], "run_class": "formal",
                                               "version": 1, "input": {"start": "2026-09-01",
                                                                       "end": "2026-09-08"}})
    assert res.status_code == 409 and res.json()["code"] == publications.RECORD_MISSING
    assert "发布依据" in res.json()["detail"]
    async with SessionLocal() as session:
        assert (await session.execute(select(Run).where(Run.workflow_id == asset["workflow_id"]))).scalars().all() == []


# --------------------------------------------------------------------------
# 2、老资产与「不需要证明」的资产：保持原行为，但都不能被说成验证过了
# --------------------------------------------------------------------------


async def test_a_legacy_asset_keeps_its_behaviour_and_is_marked_legacy(client, monkeypatch):
    """加发布清单这一机制之前发布的资产（这一版没有清单）：正式运行照旧发得起来，身份记 formal_legacy。"""
    report_model(monkeypatch)
    workflow_id = await S.publish()                         # 直接写库（老写法，不经过发布入口）
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, workflow_id)
        workflow.tags = ["doc_draft"]                        # 已接管，但没有清单
        await session.commit()
    run = await launch_formal({"workflow_id": workflow_id, "version": 1})
    info = publication_of(run)
    assert info["identity"] == publications.LEGACY and info["requires_proof"] is False
    assert info["manifest"] is None and info["publication"] is None
    assert (await wait_run(run.id)).status == "succeeded"


async def test_an_asset_outside_the_proof_scope_is_unproven_not_verified(client, monkeypatch):
    """不是「已接管」的资产（手建的画布工作流）：这一版照样有清单，但不要求证明，身份是 formal_unproven。"""
    report_model(monkeypatch)
    asset = await make_workflow(tags=[])
    out = await publish(client, asset)
    assert out["manifest"] and out["publication"]["required"] is False
    run = await launch_formal(asset)
    info = publication_of(run)
    assert info["identity"] == publications.UNPROVEN and info["requires_proof"] is False
    assert info["manifest"] == out["manifest"]
    assert (await wait_run(run.id)).status == "succeeded"


# --------------------------------------------------------------------------
# 3、试算、探索、组合不被这套门禁误伤
# --------------------------------------------------------------------------


async def test_explore_trial_and_compose_are_never_refused_for_missing_proof(client):
    from app.look.calibers import CaliberKind

    asset = await make_workflow()
    await publish(client, asset)                            # 有清单、没有验证记录、也没有审阅
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, asset["workflow_id"])
        for purpose in (publications.PURPOSE_EXPLORE, publications.PURPOSE_TRIAL,
                        publications.PURPOSE_COMPOSE):
            judged = await publications.judge(session, workflow, version=1, purpose=purpose)
            assert judged.refusal is None, f"{purpose} 不该被发布依据拦"
            assert judged.requires_proof is False
        # 目录和组合式问数照常看得见这一版
        kind = await CaliberKind.load(session, compose=True)
        items = [i for i in kind.items if i.workflow_id == workflow.id]
        assert [i.version for i in items] == [1]
        [option] = [o for o in kind.options or [] if o.workflow_id == workflow.id]
        assert option.composable is True
    # 画布上的探索运行照常发起（同一条 launch_run，run_class=exploratory）
    from app.engine.launch import launch_run

    async with SessionLocal() as session:
        exploratory = await launch_run(session, workflow_id=asset["workflow_id"], run_class="exploratory",
                                       actor="试跑人", input_payload={"start": "2026-09-01", "end": "2026-09-08"})
    assert exploratory.run_class == "exploratory"
    assert publication_of(exploratory) == {}, "探索运行不继承正式身份，也不固定发布依据"
    assert (await wait_run(exploratory.id)).status in ("succeeded", "failed")


# --------------------------------------------------------------------------
# 4、按口径出具：同一套判定，不能经另一条路拿到正式身份
# --------------------------------------------------------------------------


async def test_issue_is_refused_without_the_publication_basis(client):
    from app.ledger import issue as ledger_issue

    asset = await make_workflow()
    await publish(client, asset)                            # 没有验证记录
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, asset["workflow_id"])
        plan = await issue_plan(session, asset)
        assert plan.reason and plan.code == publications.RECORD_MISSING
        assert "发布依据" in plan.reason
        # 依据齐全之后（同一版重新发布），出具有了正式身份，缺的是别的东西（入口必填没值）
        record_id = await add_record(asset)
        await confirm(asset, record_id)
        # 依据补上之后重新发布（同一个发布入口）：这一版才有正式身份
        from app.api.workflows import commit_publish

        await commit_publish(session, workflow, await _version_row(session, asset), "published", "发布人")
        plan = await issue_plan(session, asset)
    assert plan.code not in (publications.RECORD_MISSING, publications.RECORD_STALE, publications.REVIEW_MISSING,
                             publications.REVIEW_PARTIAL)
    assert plan.publication["identity"] == publications.PROVEN


async def _version_row(session, asset: dict):
    from app.db.models import WorkflowVersion

    return (await session.execute(select(WorkflowVersion).where(
        WorkflowVersion.workflow_id == asset["workflow_id"],
        WorkflowVersion.version == asset["version"]))).scalar_one()


# --------------------------------------------------------------------------
# 5、E15：删掉没验证过的语义字段不会把旧依据升成通过
# --------------------------------------------------------------------------


async def test_deleting_an_unverified_field_does_not_upgrade_the_basis(client):
    """删掉一段业务描述就改了定义指纹：旧记录/旧审阅仍可追，但不再覆盖新版本——正式运行照样拒。"""
    from app.engine.launch import LaunchRefused

    asset = await make_workflow(graph=graph_with_definition())
    record_id = await add_record(asset)
    await confirm(asset, record_id)
    await publish(client, asset)
    run = await launch_formal(asset)
    assert publication_of(run)["identity"] == publications.PROVEN
    # 把那一段描述删掉（没验证过的那一项），存出新的一版并重新发布
    changed = graph_without_definition()
    asset = await add_version(asset, changed, version=2)
    out = await publish(client, asset, version=2)
    assert out["publication"]["proven"] is False
    assert out["publication"]["problems"][0]["code"] == publications.RECORD_STALE
    with pytest.raises(LaunchRefused) as caught:
        await launch_formal(asset, version=2)
    assert caught.value.code == publications.RECORD_STALE
    # 旧记录和旧审阅都还在库里、照旧可追，只是不算覆盖这一版
    async with SessionLocal() as session:
        record = await validation.get_record(session, asset["draft_id"], record_id)
        current = validation.materials(changed, CARD, workflow_id=asset["workflow_id"])
        assert record.status == "matched" and not validation.covers_current(record, current=current)
        summary = validation.summarize([record], current=current)
        assert summary["verdict"] == "stale" and summary["covers_current"] is False
    # 那一版最后一次发布留下的清单里照实写着依据不齐
    event = await latest_event(asset["workflow_id"], 2)
    assert event.proven is False and event.cards[0]["proof"]["code"] == publications.RECORD_STALE


# --------------------------------------------------------------------------
# 6、依据只能在发布那一刻定下来：补的记录、别的卡、之后没过的那一次，都升不上去
# --------------------------------------------------------------------------


async def test_a_basis_is_never_upgraded_by_records_added_later(client, monkeypatch):
    """发布时没有记录，之后补上记录和审阅：这一版的依据照旧不齐，要重新发布才算数（另挑更新的记录就是后门）。"""
    from app.engine.launch import LaunchRefused

    report_model(monkeypatch)
    asset = await make_workflow()
    out = await publish(client, asset)
    assert out["publication"]["proven"] is False
    record_id = await add_record(asset)
    await confirm(asset, record_id)
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, asset["workflow_id"])
        judged = await publications.judge(session, workflow, version=1, purpose=publications.PURPOSE_FORMAL)
    assert judged.code == publications.RECORD_MISSING and judged.identity == publications.UNPROVEN
    with pytest.raises(LaunchRefused) as caught:
        await launch_formal(asset)
    assert caught.value.code == publications.RECORD_MISSING
    # 重新发布（同一个入口）才把这一版重新按现在的记录认可一遍
    assert (await publish(client, asset))["publication"]["proven"] is True
    run = await launch_formal(asset)
    assert publication_of(run)["identity"] == publications.PROVEN
    assert (await wait_run(run.id)).status == "succeeded"


async def test_every_source_card_needs_its_own_basis(client, monkeypatch):
    """图里两张源头口径卡：证明了一张不算另一张也过了，缺的那张照样拦，补上之后重新发布才齐。"""
    from app.engine.launch import LaunchRefused

    report_model(monkeypatch)
    asset = await make_workflow(graph=two_card_graph())
    record_id = await add_record(asset)                     # 只覆盖 card
    await confirm(asset, record_id)
    out = await publish(client, asset)
    assert out["publication"]["proven"] is False
    assert [p["card_id"] for p in out["publication"]["problems"]] == ["card2"]
    assert out["publication"]["problems"][0]["code"] == publications.RECORD_MISSING
    with pytest.raises(LaunchRefused) as caught:
        await launch_formal(asset)
    assert caught.value.code == publications.RECORD_MISSING
    # 另一张卡也有了自己的 matched + 审阅，重新发布之后才整份齐
    other = await add_record(asset, card_id="card2", metric_ids=("refunds",))
    await confirm(asset, other, scope=["refunds"])
    assert (await publish(client, asset))["publication"]["proven"] is True
    run = await launch_formal(asset)
    assert publication_of(run)["identity"] == publications.PROVEN
    assert (await wait_run(run.id)).status == "succeeded"


@pytest.mark.parametrize("later", ["mismatched", "observed"])
async def test_a_later_trial_that_is_not_matched_takes_the_basis_away(client, later):
    """先有一条 matched + 审阅，之后又跑了一次没过（同一份定义）：已发布那一版的依据是发布那一刻定下的，照旧；
    **重新发布**时按最新的那条重算——不拿更早那条顶上（K05 的卡级结论按的也是最新那条）。"""
    from app.engine.launch import LaunchRefused

    asset = await make_workflow()
    record_id = await add_record(asset)
    await confirm(asset, record_id)
    assert (await publish(client, asset))["publication"]["proven"] is True
    await add_record(asset, status=later)                   # 同一份定义、更新的一条
    # 已经发布的那一份依据不被后来的记录改写：不另挑更新的记录去补，也不被它顶掉
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, asset["workflow_id"])
        judged = await publications.judge(session, workflow, version=1, purpose=publications.PURPOSE_FORMAL)
    assert judged.identity == publications.PROVEN and judged.code is None
    # K05 的卡级结论按的是最新那条：草稿页上看到的就是「这一次没过」
    async with SessionLocal() as session:
        rows = await validation.list_for_draft(session, asset["draft_id"])
        current = validation.materials(asset["graph"], CARD, workflow_id=asset["workflow_id"])
        assert validation.summarize(rows, current=current)["verdict"] == later
    # 重新发布等于按现在的记录重新认可这一版：最新的那条不是 matched，旧的不得顶上
    out = await publish(client, asset)
    assert out["publication"]["proven"] is False
    assert out["publication"]["problems"][0]["code"] == publications.RECORD_NOT_MATCHED
    with pytest.raises(LaunchRefused) as caught:
        await launch_formal(asset)
    assert caught.value.code == publications.RECORD_NOT_MATCHED


async def test_issue_on_a_legacy_asset_keeps_its_behaviour(client):
    """加发布清单这一机制之前发布的资产（这一版没有清单）：按口径出具照旧按原来的说法判，身份记 formal_legacy。"""
    from app.ledger import issue as ledger_issue

    workflow_id = await S.publish()                         # 直接写库（老写法，不经过发布入口）
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, workflow_id)
        workflow.tags = ["doc_draft"]                        # 已接管，但没有清单
        await session.commit()
    async with SessionLocal() as session:
        plan = await issue_plan(session, {"workflow_id": workflow_id, "version": 1})
    assert "发布依据" not in (plan.reason or ""), plan.reason
    assert plan.publication["identity"] == publications.LEGACY
    assert plan.publication["requires_proof"] is False and plan.publication["manifest"] is None


# --------------------------------------------------------------------------
# 7、清单得是这一版这一张图的；发布时的严格性记在事件行上
# --------------------------------------------------------------------------


async def test_a_strict_publication_keeps_its_strictness_after_a_tag_change(client):
    """发布时按严格记下的一版：之后把接管标记去掉（清 tags）也不降级——事件行上的 required 说了算。"""
    from app.engine.launch import LaunchRefused

    asset = await make_workflow()                            # tags=["doc_draft"]、没有 origin.kind
    out = await publish(client, asset)
    assert out["publication"]["required"] is True and out["publication"]["proven"] is False
    assert (await latest_event(asset["workflow_id"], 1)).required is True
    # 直接清掉标记：API 会拒（见下一节），这里演的是别的路径/老数据把它清掉了
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, asset["workflow_id"])
        workflow.tags = []
        await session.commit()
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, asset["workflow_id"])
        judged = await publications.judge(session, workflow, version=1, purpose=publications.PURPOSE_FORMAL)
    assert judged.requires_proof is True and judged.code == publications.RECORD_MISSING
    with pytest.raises(LaunchRefused) as caught:
        await launch_formal(asset)
    assert caught.value.code == publications.RECORD_MISSING
    # 重新发布才按现在的状态重判：这一回不算接管，记 required=False，身份 formal_unproven
    again = await publish(client, asset)
    assert again["publication"]["required"] is False
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, asset["workflow_id"])
        judged = await publications.judge(session, workflow, version=1, purpose=publications.PURPOSE_FORMAL)
    assert judged.requires_proof is False and judged.identity == publications.UNPROVEN and judged.code is None


async def test_a_manifest_for_another_graph_is_refused(client):
    """版本行上的图被别的工具改过（清单还是这张图当时的）：formal 和出具都按 BASIS_MISMATCH 拒，不拿它顶。"""
    from app.engine.launch import LaunchRefused
    from app.ledger import issue as ledger_issue

    asset = await make_workflow()
    record_id = await add_record(asset)
    await confirm(asset, record_id)
    assert (await publish(client, asset))["publication"]["proven"] is True
    async with SessionLocal() as session:
        row = await _version_row(session, asset)
        row.graph = S.caliber_graph(S.TEMPLATE.replace("'paid'", "'open'"))   # 同一版号、换了图
        await session.commit()
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, asset["workflow_id"])
        resolved = await publications.resolve(session, workflow, version=1, purpose=publications.PURPOSE_FORMAL)
        judged = await publications.judge(session, workflow, version=1, purpose=publications.PURPOSE_FORMAL)
        plan = await issue_plan(session, asset)
    assert resolved.publication_problem and resolved.publication_problem["code"] == publications.BASIS_MISMATCH
    assert "图指纹" in resolved.publication_problem["message"]
    assert judged.code == publications.BASIS_MISMATCH and judged.identity == publications.UNPROVEN
    assert plan.code == publications.BASIS_MISMATCH
    with pytest.raises(LaunchRefused) as caught:
        await launch_formal(asset)
    assert caught.value.code == publications.BASIS_MISMATCH


async def test_a_manifest_missing_a_card_is_refused_without_padding(client):
    """清单里没有图上的某张源头口径卡：按 BASIS_MISMATCH 拒，**不去库里另挑一条记录补上**。

    清单可能不是本代码写的（更早一版、别的工具）：这里直接写一份图指纹对得上、但少了 card2 那一段的工件，再把事件
    行指过去——比的是**工件内容**，事件行上的 cards 列说什么都不算。"""
    from app.core.artifact_store import put_json
    from app.engine.launch import LaunchRefused

    asset = await make_workflow(graph=two_card_graph())
    record_id = await add_record(asset)                      # 先只覆盖 card
    await confirm(asset, record_id)
    await publish(client, asset)
    # card2 现在也有 matched + 审阅了：库里挑得到，但这一版的清单里根本没有它那一条
    other = await add_record(asset, card_id="card2", metric_ids=("refunds",))
    await confirm(asset, other, scope=["refunds"])
    async with SessionLocal() as session:
        row = await publications.latest(session, asset["workflow_id"], 1)
        content = publications.read(row)
        content["cards"] = [card for card in content["cards"] if card["card_id"] != "card2"]
        row.manifest = await put_json(content, kind=publications.KIND, run_id=None)
        await session.commit()
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, asset["workflow_id"])
        resolved = await publications.resolve(session, workflow, version=1, purpose=publications.PURPOSE_FORMAL)
        judged = await publications.judge(session, workflow, version=1, purpose=publications.PURPOSE_FORMAL)
    assert resolved.publication_problem["card_id"] == "card2"
    assert resolved.publication_problem["code"] == publications.BASIS_MISMATCH
    assert judged.code == publications.BASIS_MISMATCH and judged.identity == publications.UNPROVEN
    with pytest.raises(LaunchRefused) as caught:
        await launch_formal(asset)
    assert caught.value.code == publications.BASIS_MISMATCH


async def test_the_comparison_follows_the_artifact_not_the_event_row(client):
    """以工件内容为准：事件行的 cards 列被改过（可变的那几列）不影响判定，照旧按工件那一份认。"""
    asset = await make_workflow()
    record_id = await add_record(asset)
    await confirm(asset, record_id)
    assert (await publish(client, asset))["publication"]["proven"] is True
    async with SessionLocal() as session:
        row = await publications.latest(session, asset["workflow_id"], 1)
        row.cards = []                                   # 事件行的摘要被抹掉（工件一个字没动）
        await session.commit()
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, asset["workflow_id"])
        resolved = await publications.resolve(session, workflow, version=1, purpose=publications.PURPOSE_FORMAL)
        judged = await publications.judge(session, workflow, version=1, purpose=publications.PURPOSE_FORMAL)
    assert resolved.publication_content and resolved.publication_problem is None
    assert judged.identity == publications.PROVEN and judged.problems == ()


async def test_a_legacy_version_has_no_manifest_to_check(client):
    """老资产（这一版没有发布事件）：没有清单可核，resolve 的结论是 None、judge 记 formal_legacy，按原行为走。"""
    workflow_id = await S.publish()
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, workflow_id)
        workflow.tags = ["doc_draft"]
        await session.commit()
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, workflow_id)
        resolved = await publications.resolve(session, workflow, version=1, purpose=publications.PURPOSE_FORMAL)
        judged = await publications.judge(session, workflow, version=1, purpose=publications.PURPOSE_FORMAL)
    assert resolved.publication is None and resolved.publication_problem is None
    assert judged.identity == publications.LEGACY and judged.code is None and judged.requires_proof is False


# --------------------------------------------------------------------------
# 8、已接管的资产不能经普通 PATCH 把自己降成普通工作流
# --------------------------------------------------------------------------


async def test_the_takeover_tag_cannot_be_cleared_through_the_workflow_api(client):
    """只靠标签认接管的那类资产：PATCH 清掉标记被拒（会同时关掉 K02 的并发保护和 K07 的严格性），标签原样留着。"""
    from app.api.workflows import PROTECTED_ASSET

    asset = await make_workflow()                            # tags=["doc_draft"]、没有 origin.kind
    res = await client.patch(f"/api/workflows/{asset['workflow_id']}", json={"tags": []})
    assert res.status_code == 409 and res.json()["code"] == PROTECTED_ASSET
    assert "接管" in res.json()["detail"]
    assert (await persisted(asset["workflow_id"])).tags == ["doc_draft"]
    # 保护一个都没丢：清完标记这一步没生效，改图照旧要带基准版本（K02）
    patch = await client.patch(f"/api/workflows/{asset['workflow_id']}", json={"graph": asset["graph"]})
    assert patch.status_code == 409 and patch.json()["code"] == "revision_required"
    # 别的标签照旧增改，标记留着就行
    ok = await client.patch(f"/api/workflows/{asset['workflow_id']}", json={"tags": ["doc_draft", "周销售"]})
    assert ok.status_code == 200 and (await persisted(asset["workflow_id"])).tags == ["doc_draft", "周销售"]


async def test_ordinary_workflows_are_not_touched_by_that_guard(client):
    """纯普通资产照旧随便改标签；origin 里记着文档起草的资产也不靠标签认接管，标签清空不算「降级」。"""
    plain = await make_workflow(tags=["blueprint"])
    res = await client.patch(f"/api/workflows/{plain['workflow_id']}", json={"tags": []})
    assert res.status_code == 200 and (await persisted(plain["workflow_id"])).tags == []
    marked = await make_workflow(tags=[])
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, marked["workflow_id"])
        workflow.origin = {"kind": "doc_draft"}
        await session.commit()
    res = await client.patch(f"/api/workflows/{marked['workflow_id']}", json={"tags": ["周销售"]})
    assert res.status_code == 200 and (await persisted(marked["workflow_id"])).tags == ["周销售"]


# --------------------------------------------------------------------------
# 9、转抄卡（caliber_from）：上游那张卡按同一个用途递归判
# --------------------------------------------------------------------------


def chained_graph(upstream: dict, *, upstream_card: str = CARD) -> dict:
    """下游的图：取数还是本地的，图上那张口径卡转抄上游某一版的那一张（caliber_from）——图里没有源头口径卡。"""
    graph = S.caliber_graph()
    card = next(node for node in graph["nodes"] if node["id"] == CARD)
    card["data"]["config"] = {"caliber_from": {"workflow_id": upstream["workflow_id"],
                                               "workflow_version": upstream["version"],
                                               "node_id": upstream_card}}
    return graph


async def verified_upstream(client, *, graph: dict | None = None, card_id: str = CARD,
                            metric_ids: tuple[str, ...] = ("gmv",)) -> dict:
    """一个已接管、已按新发布入口发布、依据齐全的上游（口径库）。"""
    upstream = await make_workflow(graph=graph)
    record_id = await add_record(upstream, card_id=card_id, metric_ids=metric_ids)
    await confirm(upstream, record_id, scope=list(metric_ids))
    assert (await publish(client, upstream))["publication"]["proven"] is True
    return upstream


async def test_a_downstream_that_only_copies_an_unproven_upstream_is_refused(client):
    """只含转抄卡的下游（自己 cards=[]）：上游那一张卡没有发布依据时，下游也拿不到正式身份。"""
    from app.engine.launch import LaunchRefused
    from app.ledger import issue as ledger_issue

    upstream = await make_workflow()                          # 已接管、没有任何验证记录
    await publish(client, upstream)
    assert (await latest_event(upstream["workflow_id"], 1)).proven is False
    asset = await make_workflow(graph=chained_graph(upstream))
    await publish(client, asset)
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, asset["workflow_id"])
        judged = await publications.judge(session, workflow, version=1, purpose=publications.PURPOSE_FORMAL)
        plan = await issue_plan(session, asset)
    # 机器码是上游那一条，问题点名下游的节点并带上游引用（可追）
    assert judged.code == publications.RECORD_MISSING and judged.identity == publications.UNPROVEN
    [found] = judged.problems
    assert found["upstream"] == {"node_id": "card", "workflow_id": upstream["workflow_id"], "version": 1,
                                "card_id": CARD}
    assert "转抄的" in found["message"] and "没有留下任何验证记录" in found["message"]
    assert plan.code == publications.RECORD_MISSING
    with pytest.raises(LaunchRefused) as caught:
        await launch_formal(asset)
    assert caught.value.code == publications.RECORD_MISSING
    assert "转抄的" in caught.value.message


async def test_a_downstream_with_a_verified_upstream_is_allowed(client, monkeypatch):
    """上游那张卡依据齐全（成功上游），下游才允许：身份 formal_proven，正式运行发得起来。"""
    report_model(monkeypatch)
    upstream = await verified_upstream(client)
    asset = await make_workflow(graph=chained_graph(upstream))
    await publish(client, asset)
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, asset["workflow_id"])
        judged = await publications.judge(session, workflow, version=1, purpose=publications.PURPOSE_FORMAL)
    assert judged.refusal is None and judged.requires_proof is True
    assert judged.identity == publications.PROVEN and judged.problems == ()
    run = await launch_formal(asset)
    info = publication_of(run)
    assert info["identity"] == publications.PROVEN and info["requires_proof"] is True
    assert (await wait_run(run.id)).status == "succeeded"


async def test_only_the_referenced_card_of_the_upstream_matters(client):
    """只判被引用的那一张卡：上游同工作流里别的卡没验证，不牵连下游；被引用的那张没验证，照旧拦。"""
    from app.engine.launch import LaunchRefused

    # 上游两张卡，只验证 card：下游引用的正是它，另一张（card2）没依据，下游不受牵连
    upstream = await make_workflow(graph=two_card_graph())
    record_id = await add_record(upstream, card_id=CARD, metric_ids=("gmv",))
    await confirm(upstream, record_id, scope=["gmv"])
    out = await publish(client, upstream)
    assert out["publication"]["proven"] is False                  # 上游自己那一版依据不齐（card2）
    assert [p["card_id"] for p in out["publication"]["problems"]] == ["card2"]
    asset = await make_workflow(graph=chained_graph(upstream))
    await publish(client, asset)
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, asset["workflow_id"])
        judged = await publications.judge(session, workflow, version=1, purpose=publications.PURPOSE_FORMAL)
    assert judged.refusal is None and judged.identity == publications.PROVEN, judged.problems
    # 反过来：只验证 card2，下游引用的 card 没依据 → 拦
    other = await make_workflow(graph=two_card_graph())
    record_id = await add_record(other, card_id="card2", metric_ids=("refunds",))
    await confirm(other, record_id, scope=["refunds"])
    await publish(client, other)
    asset2 = await make_workflow(graph=chained_graph(other))
    await publish(client, asset2)
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, asset2["workflow_id"])
        judged = await publications.judge(session, workflow, version=1, purpose=publications.PURPOSE_FORMAL)
    assert judged.code == publications.RECORD_MISSING
    with pytest.raises(LaunchRefused) as caught:
        await launch_formal(asset2)
    assert caught.value.code == publications.RECORD_MISSING


async def test_a_legacy_upstream_is_not_upgraded(client):
    """上游那一版没有发布事件（老资产）：明确不升级——下游照旧按原行为走，不替它补一道新门禁。"""
    upstream_id = await S.publish()                            # 直接写库（老写法，没有发布事件）
    asset = await make_workflow(graph=chained_graph({"workflow_id": upstream_id, "version": 1}))
    await publish(client, asset)
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, asset["workflow_id"])
        judged = await publications.judge(session, workflow, version=1, purpose=publications.PURPOSE_FORMAL)
    assert judged.refusal is None and judged.identity == publications.UNPROVEN
    assert judged.requires_proof is False


async def test_an_upstream_outside_the_proof_scope_is_not_made_stricter(client):
    """上游那一版不要求证明（不在证明范围内）：下游不另立一套更严的说法——上游自己发起也就是 formal_unproven。"""
    upstream = await make_workflow(tags=[])                     # 手建的画布工作流
    await publish(client, upstream)
    assert (await latest_event(upstream["workflow_id"], 1)).required is False
    asset = await make_workflow(graph=chained_graph(upstream))
    await publish(client, asset)
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, asset["workflow_id"])
        judged = await publications.judge(session, workflow, version=1, purpose=publications.PURPOSE_FORMAL)
    assert judged.refusal is None and judged.requires_proof is False
    assert judged.identity == publications.UNPROVEN


async def test_a_cycle_between_two_chained_assets_terminates(client):
    """循环引用（A 的卡转抄 B、B 的卡又转抄回 A）：判得下去，不转圈，也不凭空报一条。"""
    a = await make_workflow()
    b = await make_workflow()
    async with SessionLocal() as session:
        for asset, other in ((a, b), (b, a)):
            row = await _version_row(session, asset)
            row.graph = chained_graph(other)
            await session.commit()
    await publish(client, a)
    await publish(client, b)
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, a["workflow_id"])
        judged = await publications.judge(session, workflow, version=1, purpose=publications.PURPOSE_FORMAL)
    # 两边图里都没有源头口径卡：链子转回自己就截断，没有可判的依据
    assert judged.problems == ()


async def test_a_two_hop_chain_judges_the_terminal_card(client):
    """两层转抄：下游 → 中间那一版的转抄卡 → 上游的源头卡；判到源头卡那一层。"""
    from app.engine.launch import LaunchRefused

    source = await make_workflow()                              # 源头：有卡、没有记录
    await publish(client, source)
    middle = await make_workflow(graph=chained_graph(source))    # 中间：转抄源头那张卡
    await publish(client, middle)
    asset = await make_workflow(graph=chained_graph(middle))     # 下游：转抄中间那一版的那张卡
    await publish(client, asset)
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, asset["workflow_id"])
        judged = await publications.judge(session, workflow, version=1, purpose=publications.PURPOSE_FORMAL)
    assert judged.code == publications.RECORD_MISSING
    assert judged.problems[0]["upstream"]["workflow_id"] == middle["workflow_id"]
    with pytest.raises(LaunchRefused) as caught:
        await launch_formal(asset)
    assert caught.value.code == publications.RECORD_MISSING


# --------------------------------------------------------------------------
# 10、默认发起与点名版本：运行来源里的 explicit
# --------------------------------------------------------------------------


async def test_a_default_launch_records_the_recommended_version_as_not_explicit(client, monkeypatch):
    """默认发起（不传 version）：只按工作流上的推荐指针取一次并固定，运行来源里 explicit=false；
    点名版本才是 true。并发下指针就算漂了，这一次用的还是发起那一刻定下来的那一版。"""
    report_model(monkeypatch)
    asset = await make_workflow()
    record_id = await add_record(asset)
    await confirm(asset, record_id)
    await publish(client, asset)
    asset = await add_version(asset, S.caliber_graph(S.TEMPLATE.replace("'paid'", "'open'")), version=2)
    await publish(client, asset, version=2)
    await publish(client, asset, version=1)                      # 把 v1 重新推上去（推荐版是 1，最高版是 2）

    resolved: list[str] = []
    original = publications.recommended

    async def counted(session, workflow):
        resolved.append(workflow.id)
        return await original(session, workflow)

    monkeypatch.setattr(publications, "recommended", counted)
    # 默认发起：不传 version（launch_formal 会带上资产当前的版本，这里直接调 launch_run）
    from app.engine.launch import launch_run

    async with SessionLocal() as session:
        run = await launch_run(session, workflow_id=asset["workflow_id"], run_class="formal", actor="发布人",
                               input_payload={"start": "2026-09-01", "end": "2026-09-08"},
                               origin_kind="launcher")
    assert run.version == 1, "默认发起用的是推荐版"
    assert resolved == [], "只读一次推荐指针（工作流上的），不再解析一遍推荐版"
    info = publication_of(run)
    assert info["explicit"] is False and info["version"] == 1
    assert (await wait_run(run.id)).status == "succeeded"
    monkeypatch.setattr(publications, "recommended", original)   # 只还原这一处（report_model 的假模型还要用）
    named = await launch_formal(asset, version=1)                # 点名版本
    assert publication_of(named)["explicit"] is True
    assert (await wait_run(named.id)).status == "succeeded"


# --------------------------------------------------------------------------
# 11、三层转抄链（D → M → S）：链子上哪一层坏了都拦，问题逐层保留、可追
# --------------------------------------------------------------------------


async def three_layer_chain(client, *, source_verified: bool = True) -> dict:
    """D → M → S：S 是源头口径库、M 转抄 S、D 转抄 M。S 参差（默认依据齐全）。"""
    source = (await verified_upstream(client) if source_verified
              else await make_workflow())
    if not source_verified:
        await publish(client, source)
    middle = await make_workflow(graph=chained_graph(source))
    await publish(client, middle)
    top = await make_workflow(graph=chained_graph(middle))
    await publish(client, top)
    return {"d": top, "m": middle, "s": source}


def _drop_resolver(row) -> dict:
    content = publications.read(row)
    content["compiler"].pop("resolver")
    return content


@pytest.mark.parametrize("broken,code", [("missing", publications.MANIFEST_MISSING),
                                         ("tampered", publications.MANIFEST_TAMPERED),
                                         ("compiler", publications.COMPILER_MISMATCH)])
async def test_a_broken_middle_layer_stops_the_whole_chain(client, broken, code):
    """D → M → S：S 依据齐全也不能让 D 拿到 formal_proven——M 这一层的清单坏了就整条链子拦，问题追到 M 那一层。"""
    from app.core.artifact_store import put_json
    from app.engine.launch import LaunchRefused

    chain = await three_layer_chain(client)
    row = await latest_event(chain["m"]["workflow_id"], 1)
    path = artifact_store._path_of(row.manifest)
    if broken == "missing":
        path.unlink()
    elif broken == "tampered":
        path.write_text(path.read_text(encoding="utf-8").replace('"level":"published"', '"level":"governed"'),
                        encoding="utf-8")
    else:
        async with SessionLocal() as session:
            stored = await publications.latest(session, chain["m"]["workflow_id"], 1)
            stored.manifest = await put_json(_drop_resolver(stored), kind=publications.KIND, run_id=None)
            await session.commit()
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, chain["d"]["workflow_id"])
        judged = await publications.judge(session, workflow, version=1, purpose=publications.PURPOSE_FORMAL)
        plan = await issue_plan(session, chain["d"])
    assert judged.code == code and judged.identity == publications.UNPROVEN
    [found] = judged.problems
    # 问题出在 M 这一层：引用链只有 D 的节点 → M 这一跳，链路终点就是 M
    assert [hop["workflow_id"] for hop in found["trace"]] == [chain["m"]["workflow_id"]]
    assert found["trace"][-1]["workflow_id"] == chain["m"]["workflow_id"]
    assert found["upstream"]["workflow_id"] == chain["m"]["workflow_id"]
    assert found["card_id"] == CARD and "转抄的" in found["message"]
    assert chain["s"]["workflow_id"] not in found["message"]
    assert plan.code == code
    with pytest.raises(LaunchRefused) as caught:
        await launch_formal(chain["d"])
    assert caught.value.code == code


async def test_every_layer_of_the_chain_keeps_its_own_problem(client):
    """链子上两层都坏了（M 的清单没了、S 的清单也没了）：两条都在、各带各的引用链，不合成一条。"""
    from app.engine.launch import LaunchRefused

    chain = await three_layer_chain(client)
    for who in ("m", "s"):
        row = await latest_event(chain[who]["workflow_id"], 1)
        artifact_store._path_of(row.manifest).unlink()
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, chain["d"]["workflow_id"])
        judged = await publications.judge(session, workflow, version=1, purpose=publications.PURPOSE_FORMAL)
    assert judged.code == publications.MANIFEST_MISSING and judged.identity == publications.UNPROVEN
    assert len(judged.problems) == 2, judged.problems
    chains = {tuple(hop["workflow_id"] for hop in found["trace"]) for found in judged.problems}
    assert chains == {(chain["m"]["workflow_id"],), (chain["m"]["workflow_id"], chain["s"]["workflow_id"])}
    for found in judged.problems:
        assert found["code"] == publications.MANIFEST_MISSING and found["card_id"] == CARD
        assert "转抄的" in found["message"] and "发布清单工件不在了" in found["message"]
    with pytest.raises(LaunchRefused) as caught:
        await launch_formal(chain["d"])
    assert caught.value.code == publications.MANIFEST_MISSING


async def test_a_broken_layer_keeps_its_problem_when_the_reference_points_nowhere(client):
    """被引用的那张卡在图里找不到（nested_node 为空）：这一层的问题照样保留，不因为往下没得判就放过。"""
    from app.engine.launch import LaunchRefused

    chain = await three_layer_chain(client)
    # M 引用的卡 id 在 S 的图上不存在（图检查只管形状，具体有没有这个节点由运行时判）
    async with SessionLocal() as session:
        row = await _version_row(session, chain["m"])
        graph = row.graph
        card = next(node for node in graph["nodes"] if node["id"] == CARD)
        card["data"]["config"]["caliber_from"]["node_id"] = "not-there"
        row.graph = graph
        await session.commit()
    middle_row = await latest_event(chain["m"]["workflow_id"], 1)
    artifact_store._path_of(middle_row.manifest).unlink()
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, chain["d"]["workflow_id"])
        judged = await publications.judge(session, workflow, version=1, purpose=publications.PURPOSE_FORMAL)
    assert judged.code == publications.MANIFEST_MISSING and judged.identity == publications.UNPROVEN
    assert [hop["workflow_id"] for hop in judged.problems[0]["trace"]] == [chain["m"]["workflow_id"]]
    with pytest.raises(LaunchRefused) as caught:
        await launch_formal(chain["d"])
    assert caught.value.code == publications.MANIFEST_MISSING


async def test_a_broken_layer_still_counts_when_another_layer_requires_proof(client):
    """链子上只要有一层要求证明，别的层的工件问题也保留：M 不做证明（手建的），D 和 S 要——M 的清单没了照样拦。"""
    chain = await three_layer_chain(client)
    async with SessionLocal() as session:                  # M 改成手建的口径库：它自己不再要求证明
        workflow = await session.get(Workflow, chain["m"]["workflow_id"])
        workflow.tags = []
        await session.commit()
    row = await latest_event(chain["m"]["workflow_id"], 1)
    assert row.required is False and row.proven is False
    artifact_store._path_of(row.manifest).unlink()
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, chain["d"]["workflow_id"])
        judged = await publications.judge(session, workflow, version=1, purpose=publications.PURPOSE_FORMAL)
    assert judged.code == publications.MANIFEST_MISSING and judged.identity == publications.UNPROVEN
    assert [hop["workflow_id"] for hop in judged.problems[0]["trace"]] == [chain["m"]["workflow_id"]]


async def test_a_chain_that_asks_nothing_of_any_layer_is_left_alone(client):
    """链子上没有任何一层要求证明（都是手建的工作流、发布时也没要求）：不升级——M 的清单坏了也不拦。"""
    source = await make_workflow(tags=[])                  # 手建的口径库
    await publish(client, source)
    middle = await make_workflow(graph=chained_graph(source), tags=[])
    await publish(client, middle)
    top = await make_workflow(graph=chained_graph(middle), tags=[])
    await publish(client, top)
    row = await latest_event(middle["workflow_id"], 1)
    assert row.required is False
    artifact_store._path_of(row.manifest).unlink()
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, top["workflow_id"])
        judged = await publications.judge(session, workflow, version=1, purpose=publications.PURPOSE_FORMAL)
    assert judged.refusal is None and judged.problems == ()
    assert judged.requires_proof is False and judged.identity == publications.UNPROVEN
