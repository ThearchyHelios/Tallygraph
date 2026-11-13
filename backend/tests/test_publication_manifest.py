"""K07 的发布依据：不可变清单、发布事件、版本解析（任务书 4.9 与 E10）。

这份文件盯住几件不变量：

1. **每次发布留一份不可变清单 + 追加一行发布事件。** 清单是内容寻址工件，固定图/定义/绑定/参数契约/依赖/编译器与
   验证方法版本，以及发布这一刻引用的 ValidationRecord / ReviewDecision。
2. **重发旧版产生新事件，旧事件和旧清单一个字不改**（E10）：版本行上那几列发布元数据是最新投影，指针
   Workflow.published_version 是推荐指针。
3. **默认调用读推荐版**：目录（descriptor）、look、账本挑候选一致；最高已发布版只用于升级发现。
4. **显式版本始终固定**：点名哪一版就是哪一版。
5. **清单尽量确定**：内容里没有 actor、没有时间；原始输入还在时按它重建一字不差，原始输入缺失/工件读不出来明确失败。
   **观察到的环境（目录/结构/能力版本、绑定工具在不在）不进内容**：环境变了重发同一版，清单一字不差，变化只记在事件行上。
6. **发布失败无事件**：发布这一步没提交，事件也不会落。事件和版本行上的最新投影同一个事务：没提交之前别的会话看不到，
   回滚之后一个字都不留。
7. **发布只有一个入口**：只有 app/apps/publications.py 建 WorkflowPublication 行，接口和发布单都走 commit_publish。
8. **清单工件没有运行引用也不被回收**：put_json 记了一行 run_id 为空的引用，评测回收（artifact_store.forget）按引用数删，
   这一份照留。
9. **发布这一刻的严格性记在事件行上**（WorkflowPublication.required）：之后清掉接管标记也不降级。加这一列之前的事件行
   由迁移回填 0，判定照旧看当前的接管状态（补 0 不放松）。
10. **每次有发布事件的执行都复验工件与编译器约定**（`verified_content` = read 复验 + compiler_problems，和 K09 的
   固定执行同一份核对）：清单工件被删/被改过、按别的约定物化的，正式运行与出具都拒；legacy（没有事件）没有清单可复验，
   保持原行为。比的一律是**工件内容**，不认可变的事件行列。

数据是虚构店铺库（tests/ledger_samples），不连真实业务库。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.workflows import commit_publish
from app.apps import publications
from app.core import artifact_store
from app.core.artifact_store import canonical_json, graph_hash
from app.db.base import SessionLocal
from app.db.models import Artifact, CaliberValidationRecord, DataSource, Workflow, WorkflowVersion
from app.engine.runner import run_manager
from app.ledger import validation
from app.main import app
from tests import ledger_samples as S
from tests.publication_samples import (CARD, add_record, add_version, confirm, events_of, issue_plan,
                                       launch_formal, latest_event, make_workflow, persisted, publication_of, publish,
                                       report_model, version_row)

#: 另一份「只算已支付」改成「算所有状态」的取数（定义/取数都变了）
OTHER_GRAPH = S.caliber_graph(S.TEMPLATE.replace("'paid'", "'open'"))


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


# --------------------------------------------------------------------------
# 1、每次发布留一份清单 + 一行事件
# --------------------------------------------------------------------------


async def test_publishing_leaves_a_manifest_and_an_event(client):
    asset = await make_workflow()
    record_id = await add_record(asset)
    review_id = await confirm(asset, record_id)
    out = await publish(client, asset)
    assert out["ok"] and out["version"] == 1
    assert out["manifest"] and out["publication"]["manifest"] == out["manifest"]
    assert out["publication"]["proven"] is True and out["publication"]["required"] is True
    assert out["publication"]["problems"] == []
    # 旧返回键一个不动
    assert out["level"] == "published" and out["graph_hash"] and out["published_by"] == "小王"
    events = await events_of(asset["workflow_id"], 1)
    assert len(events) == 1 and events[0].manifest == out["manifest"]
    assert events[0].actor == "小王" and events[0].level == "published"
    assert (await persisted(asset["workflow_id"])).published_version == 1
    async with SessionLocal() as session:
        content = publications.read(await publications.latest(session, asset["workflow_id"], 1))
    assert content["workflow_id"] == asset["workflow_id"] and content["version"] == 1
    assert content["graph_hash"] == graph_hash(asset["graph"])
    [card] = content["cards"]
    assert card["card_id"] == CARD and card["metric_ids"] == ["gmv"]
    assert card["definition_hash"] and card["semantic_hash"]
    assert card["binding_status"] == "unbound"          # 这张卡没声明业务字段：不判同步
    assert card["validation"]["record_id"] == record_id and card["validation"]["status"] == "matched"
    assert card["review"]["decision_id"] == review_id and card["review"]["decision"] == "confirmed"
    assert card["review"]["scope"] == ["gmv"] and card["proof"]["ok"] is True
    # 参数契约、依赖、编译器与验证方法版本都固定下来了
    assert [f["name"] for f in content["parameters"]] == ["start", "end", "store"]
    assert content["dependencies"]["sources"] == [S.CURRENT["source"]]
    assert content["dependencies"]["tables"] == ["orders"]
    assert content["compiler"]["binding"]["binder"] == "controlled_template"
    assert content["compiler"]["validation_method"] == validation.METHOD_VERSION
    # 版本详情里也看得到这一版按哪份清单发布的（老资产是 null）
    detail = (await client.get(f"/api/workflows/{asset['workflow_id']}/versions/1")).json()
    assert detail["publication"]["manifest"] == out["manifest"] and detail["publication"]["proven"] is True
    assert detail["publication"]["problems"] == []


async def test_the_manifest_holds_no_actor_and_no_time(client):
    """清单要被重建，所以内容里不能有会变的东西：发布人、时间、目录/能力版本、工具在不在都不进内容（记在事件行上）。"""
    asset = await make_workflow()
    await publish(client, asset)
    row = await latest_event(asset["workflow_id"], 1)
    text = canonical_json(publications.read(row))
    assert "小王" not in text and "actor" not in text and "published_at" not in text and "created_at" not in text
    assert row.observed["at"] and "catalog_versions" in row.observed and "missing_tools" in row.observed
    assert isinstance(row.observed["capability_versions"], dict) and row.observed["capability_versions"]
    assert "capability_versions" not in text and "catalog_versions" not in text


# --------------------------------------------------------------------------
# 2、E10：重发旧版
# --------------------------------------------------------------------------


async def test_republishing_an_old_version_appends_and_never_rewrites(client):
    asset = await make_workflow()
    record_id = await add_record(asset)
    await confirm(asset, record_id)
    first = await publish(client, asset)                        # v1
    asset = await add_version(asset, OTHER_GRAPH, version=2)    # 图上改了取数
    second = await publish(client, asset, version=2)
    third = await publish(client, asset, version=1)             # 把旧版 v1 重新推上去
    assert first["manifest"] == third["manifest"], "v1 两次发布的依据一模一样（引用的记录没变）"
    assert second["manifest"] != first["manifest"]
    # 重发不是「继承」身份，而是按这一版重新核对：v2 的定义/取数改过，老记录不覆盖它，依据不齐；回到 v1 又对上了
    assert second["publication"]["proven"] is False
    assert second["publication"]["problems"][0]["code"] == "publication_record_stale"
    assert third["publication"]["proven"] is True and third["publication"]["problems"] == []
    # 推荐指针跟着最新一次发布事件走
    assert (await persisted(asset["workflow_id"])).published_version == 1
    v1_events = await events_of(asset["workflow_id"], 1)
    assert len(v1_events) == 2 and v1_events[0].manifest == first["manifest"]
    assert v1_events[1].manifest == first["manifest"] and v1_events[1].id != v1_events[0].id
    v2_events = await events_of(asset["workflow_id"], 2)
    assert len(v2_events) == 1 and v2_events[0].manifest == second["manifest"]
    # v2 那一行一个字没改
    async with SessionLocal() as session:
        v2 = await publications.latest(session, asset["workflow_id"], 2)
        assert v2.cards == v2_events[0].cards and v2.proven is False
        assert publications.read(v2)["version"] == 2


async def test_default_calls_follow_the_recommended_version_and_the_upgrade_hint(client):
    """目录、look、账本挑候选读推荐版；最高已发布版只用来提示「有一版更新的」。"""
    from app.ledger import descriptor
    from app.ledger import match as ledger_match
    from app.look.calibers import CaliberKind

    asset = await make_workflow()
    await publish(client, asset)
    asset = await add_version(asset, OTHER_GRAPH, version=2)
    await publish(client, asset, version=2)
    await publish(client, asset, version=1)                     # 重新推上一版旧版
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, asset["workflow_id"])
        recommended = await publications.recommended(session, workflow)
        assert recommended.version == 1
        assert (await publications.highest(session, workflow.id)).version == 2
        assert (await publications.upgrade_of(session, workflow))["version"] == 2
        described = {item["workflow_id"]: item for item in await descriptor.list_calibers(session)}[workflow.id]
        assert described["version"] == 1 and described["publication"]["manifest"]
        kind = await CaliberKind.load(session)
        assert {i.version for i in kind.items if i.workflow_id == workflow.id} == {1}
        candidates = {c.workflow_id: c for c in await ledger_match.load_calibers(session)}
        assert candidates[workflow.id].version == 1
        # 图里两条 SQL 的差别就是「只算已支付」：候选取的是 v1（推荐版）那一版的取数
        [fetch] = candidates[workflow.id].fetches
        assert "paid" in fetch.sql
    # 显式版本始终固定
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, asset["workflow_id"])
        pinned = await publications.resolve(session, workflow, version=2, purpose=publications.PURPOSE_FORMAL)
        assert pinned.version == 2 and pinned.explicit is True
        follow = await publications.resolve(session, workflow, purpose=publications.PURPOSE_EXPLORE)
        assert follow.version == 1 and follow.explicit is False
        assert (await descriptor.caliber_descriptor(session, workflow.id, 2, CARD))["version"] == 2


# --------------------------------------------------------------------------
# 3、清单：可读、可重建、不可读与原始输入缺失都明确失败
# --------------------------------------------------------------------------


async def test_the_manifest_rebuilds_to_the_same_bytes(client):
    asset = await make_workflow()
    record_id = await add_record(asset)
    await confirm(asset, record_id)
    out = await publish(client, asset)
    async with SessionLocal() as session:
        row = await publications.latest(session, asset["workflow_id"], 1)
        rebuilt = await publications.rebuild(session, row)
    assert rebuilt["matches"] is True and rebuilt["computed"] == out["manifest"]
    assert rebuilt["content"] == rebuilt["original"] and rebuilt["problems"] == []


async def test_an_unreadable_or_missing_artifact_fails_explicitly(client):
    asset = await make_workflow()
    await publish(client, asset)
    async with SessionLocal() as session:
        row = await publications.latest(session, asset["workflow_id"], 1)
        path = artifact_store._path_of(row.manifest)
        original = path.read_text(encoding="utf-8")
        # 被改过：读的时候复验哈希
        path.write_text(original.replace('"level":"published"', '"level":"governed"'), encoding="utf-8")
        with pytest.raises(publications.PublicationUnreadable) as caught:
            publications.read(row)
        assert caught.value.code == publications.MANIFEST_TAMPERED
        # 工件不见了
        path.unlink()
        with pytest.raises(publications.PublicationUnreadable) as gone:
            publications.read(row)
        assert gone.value.code == publications.MANIFEST_MISSING
        path.write_text(original, encoding="utf-8")
        assert publications.read(row)["version"] == 1
        # 没有事件的那一版：没有依据
        with pytest.raises(publications.PublicationUnreadable) as nothing:
            publications.read(None)
        assert nothing.value.code == publications.BASIS_MISSING


async def test_missing_raw_inputs_fail_explicitly(client):
    """原始输入（这一版不可变的图、引用的那两行记录）缺了一样就重建不上，明确失败，不拿别的版本顶。"""
    asset = await make_workflow()
    record_id = await add_record(asset)
    await confirm(asset, record_id)
    await publish(client, asset)
    async with SessionLocal() as session:
        record = await session.get(CaliberValidationRecord, record_id)
        await session.delete(record)                            # 引用的验证记录被删（删草稿时级联删）
        await session.commit()
    row = await latest_event(asset["workflow_id"], 1)
    assert publications.read(row)["cards"][0]["validation"]["record_id"] == record_id
    async with SessionLocal() as session:
        with pytest.raises(publications.PublicationUnreadable) as caught:
            await publications.rebuild(session, row)
    assert caught.value.code == publications.INPUTS_MISSING
    # 版本行（这一版不可变的图）被删了：同样重建不上
    async with SessionLocal() as session:
        version = (await session.execute(select(WorkflowVersion).where(
            WorkflowVersion.workflow_id == asset["workflow_id"],
            WorkflowVersion.version == 1))).scalar_one()
        await session.delete(version)
        await session.commit()
    async with SessionLocal() as session:
        with pytest.raises(publications.PublicationUnreadable) as gone:
            await publications.rebuild(session, row)
    assert gone.value.code == publications.INPUTS_MISSING


# --------------------------------------------------------------------------
# 4、发布失败无事件；发布只有一个入口
# --------------------------------------------------------------------------


async def test_a_failed_publish_leaves_no_event(client):
    asset = await make_workflow()
    # 指名的版本不存在：404，没有事件
    res = await client.post(f"/api/workflows/{asset['workflow_id']}/publish", json={"level": "published", "version": 9})
    assert res.status_code == 404
    assert await events_of(asset["workflow_id"]) == []
    # 级别写坏了：400，没有事件
    bad = await client.post(f"/api/workflows/{asset['workflow_id']}/publish", json={"level": "gold"})
    assert bad.status_code == 400
    assert await events_of(asset["workflow_id"]) == []
    # 图过不了发布检查：ok=False，同样没有事件
    broken = S.chain(S.node("start", "input"),
                     S.node("out", "output", fields=[{"name": "x", "value": "{{ nodes.missing.text }}"}]))
    asset = await add_version(asset, broken, version=2)
    out = (await client.post(f"/api/workflows/{asset['workflow_id']}/publish",
                             json={"level": "published", "version": 2})).json()
    assert out["ok"] is False and out["issues"]
    assert await events_of(asset["workflow_id"]) == []


def test_publishing_has_a_single_writer():
    """发布只有一个入口：只有 app/apps/publications.py 建发布事件行；接口和发布单都调 commit_publish。"""
    root = Path(__file__).resolve().parents[1] / "app"
    writers: list[str] = []
    for path in sorted(root.rglob("*.py")):
        for line in path.read_text(encoding="utf-8").splitlines():
            code = line.strip()
            if "WorkflowPublication(" in code and not code.startswith("class "):
                writers.append(path.relative_to(root).as_posix())
                break
    assert writers == ["apps/publications.py"]
    callers = {path.relative_to(root.parent).as_posix()
               for path in root.rglob("*.py")
               if "commit_publish(" in path.read_text(encoding="utf-8")}
    assert "app/api/workflows.py" in callers and "app/blueprints/publish_plan.py" in callers
    # 发布单那一步也把这一次实际留下的清单交回（执行完能对上是哪一份）
    plan = (root / "blueprints/publish_plan.py").read_text(encoding="utf-8")
    assert 'out.get("manifest")' in plan


async def test_the_publish_plan_step_reports_the_same_manifest(client):
    """发布单那一步走的是同一个 commit_publish：交回的就是这一次实际留下的清单；已经是这一档发布版时不再多发事件，
    但交回的还是那一次留下的那一份（发布依据只有一份，不会因为出单又补一份）。"""
    from app.api.deps import Principal
    from app.blueprints import publish_plan

    admin = Principal(username="boss", role="admin", mode="off", via="off")

    async def run_step(workflow_id: str, version: int) -> dict:
        async with SessionLocal() as session:
            ctx = publish_plan._Run(row=None, bp=None, principal=admin, actor="发布人", options={},
                                    level="published",
                                    results={"save_version": {"workflow_id": workflow_id, "version": version}})
            out = await publish_plan._do_publish_workflow(session, ctx)
            await session.commit()
            return out

    asset = await make_workflow()
    out = await run_step(asset["workflow_id"], 1)
    assert out["manifest"] and out["proven"] is False, "这一步交回的是它刚留下的那一份发布依据"
    [event] = await events_of(asset["workflow_id"], 1)
    assert event.manifest == out["manifest"] and out["publication_id"] == event.id
    # 再执行一次同一步：已经是这一档的发布版，不再多发一次事件
    again = await run_step(asset["workflow_id"], 1)
    assert again["already"] is True and again["manifest"] == event.manifest
    assert len(await events_of(asset["workflow_id"], 1)) == 1


# --------------------------------------------------------------------------
# 5、事件与版本投影同一个事务；清单工件没有运行引用也不被回收；环境变化不改内容
# --------------------------------------------------------------------------


async def _row_in(session, asset: dict) -> WorkflowVersion:
    """这个会话里这一版的版本行（commit_publish 要的是同一个会话里的行）。"""
    return (await session.execute(select(WorkflowVersion).where(
        WorkflowVersion.workflow_id == asset["workflow_id"],
        WorkflowVersion.version == asset["version"]))).scalar_one()


async def test_the_event_and_the_version_projection_are_one_transaction(client):
    """发布事件和版本行上的最新投影同一个事务：没提交之前别的会话看不到，回滚之后一个字都不留（「发布失败无事件」）。"""
    asset = await make_workflow()
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, asset["workflow_id"])
        out = await publications.publish(session, workflow=workflow, snapshot=await _row_in(session, asset),
                                         level="published", actor="发布人")
        async with SessionLocal() as other:
            assert await publications.events(other, asset["workflow_id"]) == [], "没提交的发布事件不该被别的会话读到"
        await session.rollback()
    assert await events_of(asset["workflow_id"]) == []
    workflow = await persisted(asset["workflow_id"])
    assert workflow.published_version is None and workflow.published_by is None
    # 提交了才是事件：走发布接口发布一次，指针和事件一起落；清单是内容寻址的，还是刚才那一份
    published = await publish(client, asset)
    assert published["manifest"] == out["manifest"]
    assert (await persisted(asset["workflow_id"])).published_version == 1
    assert len(await events_of(asset["workflow_id"], 1)) == 1


async def test_the_manifest_artifact_is_kept_without_a_run(client):
    """清单工件没有运行引用（run_id 为空），回收按引用数删：不挂运行的那一行也算引用，这一份照留。"""
    asset = await make_workflow()
    out = await publish(client, asset)
    manifest = out["manifest"]
    async with SessionLocal() as session:
        rows = list((await session.execute(select(Artifact).where(Artifact.id == manifest))).scalars())
    assert [row.run_id for row in rows] == [""] and rows[0].kind == publications.KIND
    assert await artifact_store.forget([manifest]) == [], "还有引用行的工件不该被回收"
    assert artifact_store.load(manifest) is not None
    async with SessionLocal() as session:
        assert (await publications.rebuild(session, await publications.latest(session, asset["workflow_id"], 1))
                )["matches"] is True


async def test_the_manifest_does_not_depend_on_the_observed_environment(client):
    """环境变了（数据源停用、绑定的工具现在不在本机）重发同一版：清单内容一字不差，变化只记在事件行上。"""
    asset = await make_workflow()
    first = await publish(client, asset)
    async with SessionLocal() as session:
        source = (await session.execute(select(DataSource).where(
            DataSource.name == S.CURRENT["source"]))).scalar_one()
        source.enabled = False
        await session.commit()
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, asset["workflow_id"])
        second = await commit_publish(session, workflow, await _row_in(session, asset), "published", "发布人")
    assert second["manifest"] == first["manifest"], "观察到的环境不进清单内容"
    events = await events_of(asset["workflow_id"], 1)
    assert len(events) == 2 and events[0].manifest == events[1].manifest
    assert events[0].observed["missing_tools"], "工具现在不在本机这一件记在事件行上"
    assert "missing_tools" not in canonical_json(publications.read(events[0]))
    async with SessionLocal() as session:
        assert (await publications.rebuild(session, events[0]))["matches"] is True


# --------------------------------------------------------------------------
# 6、老库补 required 列（发布这一刻的严格性）；补列之前的事件行照旧按当前状态判
# --------------------------------------------------------------------------


def test_an_old_database_gets_the_required_column():
    """老库补 workflow_publications.required：存量事件行回填 0；和后端启动同一套迁移，跑两遍不出错。"""
    from sqlalchemy import create_engine, text

    from app.db.base import _migrate

    db = create_engine("sqlite://")
    with db.begin() as conn:
        conn.execute(text("CREATE TABLE workflow_publications (id VARCHAR(32) PRIMARY KEY, workflow_id VARCHAR(32), "
                          "version INTEGER, level VARCHAR(20), graph_hash VARCHAR(64), manifest VARCHAR(64))"))
        conn.execute(text("INSERT INTO workflow_publications (id, workflow_id, version, level, manifest) "
                          "VALUES ('p1', 'w1', 1, 'published', 'm1')"))
        _migrate(conn)
        _migrate(conn)  # 再执行一次不出错：列已经在了
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(workflow_publications)"))}
        assert "required" in cols
        assert conn.execute(text("SELECT required FROM workflow_publications WHERE id = 'p1'")).scalar() == 0


async def test_an_event_without_the_flag_still_follows_the_current_state(client):
    """补列之前留下的事件行（required 是回填的 0）：判定照旧看当前的接管状态，不会因为补了个 0 就不要求证明。"""
    asset = await make_workflow()                            # 已接管、还没有任何验证记录
    await publish(client, asset)
    async with SessionLocal() as session:
        row = await publications.latest(session, asset["workflow_id"], 1)
        row.required = False                                 # 存量行的样子
        await session.commit()
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, asset["workflow_id"])
        judged = await publications.judge(session, workflow, version=1, purpose=publications.PURPOSE_FORMAL)
    assert judged.requires_proof is True and judged.code == publications.RECORD_MISSING


async def test_the_event_records_whether_this_version_needed_proof(client):
    """事件行上记下发布这一刻要不要证明：接管着的记真；手建的工作流不在证明范围内，记假。"""
    strict = await make_workflow()                           # tags=["doc_draft"]：已接管
    assert (await publish(client, strict))["publication"]["required"] is True
    assert (await latest_event(strict["workflow_id"], 1)).required is True
    plain = await make_workflow(tags=[])                      # 手建的画布工作流
    out = await publish(client, plain)
    assert out["publication"]["required"] is False
    assert (await latest_event(plain["workflow_id"], 1)).required is False


# --------------------------------------------------------------------------
# 7、每次有发布事件的执行都复验工件与编译器约定（和 K09 的固定执行同一份核对）
# --------------------------------------------------------------------------


async def _point_at(session, row, content: dict) -> str:
    """把事件行指向一份新写下的清单工件（内容寻址，读的时候复验哈希）。"""
    from app.core.artifact_store import put_json

    row.manifest = await put_json(content, kind=publications.KIND, run_id=None)
    await session.commit()
    return row.manifest


@pytest.mark.parametrize("broken,code", [("missing", publications.MANIFEST_MISSING),
                                         ("tampered", publications.MANIFEST_TAMPERED)])
async def test_a_broken_manifest_stops_formal_runs(client, broken, code):
    """清单工件被删、被改过：正式运行与按口径出具都拒——每次都复验工件，过关才继续。"""
    from app.engine.launch import LaunchRefused
    asset = await make_workflow()
    record_id = await add_record(asset)
    await confirm(asset, record_id)
    assert (await publish(client, asset))["publication"]["proven"] is True
    row = await latest_event(asset["workflow_id"], 1)
    path = artifact_store._path_of(row.manifest)
    if broken == "missing":
        path.unlink()
    else:
        path.write_text(path.read_text(encoding="utf-8").replace('"level":"published"', '"level":"governed"'),
                        encoding="utf-8")
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, asset["workflow_id"])
        judged = await publications.judge(session, workflow, version=1, purpose=publications.PURPOSE_FORMAL)
        plan = await publications.resolve(session, workflow, version=1, purpose=publications.PURPOSE_FORMAL)
    assert judged.code == code and judged.identity == publications.UNPROVEN
    assert plan.publication_content is None and plan.publication_problem["code"] == code
    with pytest.raises(LaunchRefused) as caught:
        await launch_formal(asset)
    assert caught.value.code == code
    async with SessionLocal() as session:
        got = await issue_plan(session, asset)
    assert got.code == code


async def test_a_broken_manifest_stops_runs_outside_the_proof_scope_too(client):
    """不在证明范围内的资产（发布时 required=false）：清单工件没了照样拒——事件说有清单，清单就得在。"""
    from app.engine.launch import LaunchRefused

    asset = await make_workflow(tags=[])                     # 手建的画布工作流：不需要证明
    await publish(client, asset)
    row = await latest_event(asset["workflow_id"], 1)
    assert row.required is False
    artifact_store._path_of(row.manifest).unlink()
    with pytest.raises(LaunchRefused) as caught:
        await launch_formal(asset)
    assert caught.value.code == publications.MANIFEST_MISSING


@pytest.mark.parametrize("broken,code", [("no_compiler", publications.COMPILER_MISSING),
                                         ("old_resolver", publications.COMPILER_MISMATCH)])
async def test_a_manifest_written_under_another_compiler_is_refused(client, broken, code):
    """清单是按别的约定物化的（整段没有 / 少了依赖闭包的解析器这一项）：不按新规则重解释旧发布，正式运行拒。"""
    from app.engine.launch import LaunchRefused

    asset = await make_workflow()
    record_id = await add_record(asset)
    await confirm(asset, record_id)
    await publish(client, asset)
    row = await latest_event(asset["workflow_id"], 1)
    content = publications.read(row)
    if broken == "no_compiler":
        content.pop("compiler")
    else:
        content["compiler"].pop("resolver")
    async with SessionLocal() as session:
        stored = await publications.latest(session, asset["workflow_id"], 1)
        await _point_at(session, stored, content)
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, asset["workflow_id"])
        judged = await publications.judge(session, workflow, version=1, purpose=publications.PURPOSE_FORMAL)
    assert judged.code == code and judged.identity == publications.UNPROVEN
    with pytest.raises(LaunchRefused) as caught:
        await launch_formal(asset)
    assert caught.value.code == code


async def test_a_legacy_version_without_any_manifest_still_runs(client, monkeypatch):
    """legacy（这一版没有发布事件）没有清单可复验：保持原行为，正式运行照旧发得起来、身份 formal_legacy。"""
    from tests.publication_samples import publication_of

    report_model(monkeypatch)
    workflow_id = await S.publish()                           # 直接写库（老写法，没有发布事件、没有清单）
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, workflow_id)
        workflow.tags = ["doc_draft"]
        await session.commit()
    from app.engine.launch import launch_run

    async with SessionLocal() as session:
        run = await launch_run(session, workflow_id=workflow_id, run_class="formal", actor="发布人",
                               input_payload={"start": "2026-09-01", "end": "2026-09-08"})
    assert run.version == 1
    assert publication_of(run)["identity"] == publications.LEGACY
    assert publication_of(run)["manifest"] is None
