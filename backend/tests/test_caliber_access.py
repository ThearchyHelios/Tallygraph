"""口径资产授权（apps/access.usable_calibers / caliber_role / caliber_granting_app）：普通使用者「我能按哪些口径用」看的是
应用授权，不是角色。

- 授权就是同一张 AppGrant：给本人、给他所在的组、给所有人，至少 use；
- 对象是应用**当前发布版**固定下来的那一版口径（AppVersion.pins，工作流和版本都精确对上），归档的应用不算、草稿里的固定不算；
- 同一版取最高档（use / observe / manage）；
- 没有主体（actor 为空、用户名为空）、人停用、没有授权都是 None；
- builder / admin / 关登录**不在**这两个函数里自动放行：由调用方决定（ledger/issue.check_target 先显式放行搭建者及以上，
  使用者才走这份判定）。

测试会话共用一个临时库，别的工作流、应用留下的行都在：断言只按这个用例自己的 (workflow_id, version) 和用户名看。
"""
from __future__ import annotations

import uuid

from app.api.deps import Principal
from app.apps.access import caliber_granting_app, caliber_role, usable_calibers
from app.db.base import SessionLocal
from app.db.models import Group, GroupMember, User, Workflow
from app.db.models_apps import App, AppGrant, AppVersion

USER = "user"
GROUP = "group"
EVERYONE = "everyone"


def _name(prefix: str) -> str:
    """用例自己的名字：别的工作流、应用、账号同名会串进这次断言（测试会话共用一个库）。"""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def _workflow(version: int = 1) -> str:
    """一份已发布的工作流；只为了 pins 里有一个真实的工作流 id，判定本身不读它。"""
    async with SessionLocal() as session:
        wf = Workflow(name=_name("周销售"), graph={}, status="published", published_version=version)
        session.add(wf)
        await session.commit()
        return wf.id


async def _app(*pins, name: str = "看板", status: str = "active", draft_pins: list | None = None) -> str:
    """一个应用：当前发布版（v1）固定 pins；给了 draft_pins 就再存一版没发布的草稿（v2）。"""
    async with SessionLocal() as session:
        app = App(name=_name(name), status=status, published_version=1)
        session.add(app)
        await session.flush()
        session.add(AppVersion(app_id=app.id, version=1, spec={}, spec_hash="x", pins=list(pins)))
        if draft_pins is not None:
            session.add(AppVersion(app_id=app.id, version=2, spec={}, spec_hash="y", pins=list(draft_pins)))
        await session.commit()
        return app.id


async def _grant(app_id: str, kind: str, principal: str, role: str) -> None:
    async with SessionLocal() as session:
        session.add(AppGrant(app_id=app_id, principal_kind=kind, principal=principal, role=role))
        await session.commit()


async def _person(username: str, *, role: str = "user", disabled: bool = False, groups: tuple[str, ...] = ()) -> str:
    """建一个人（要判组、停用、角色时）；返回用户名。"""
    async with SessionLocal() as session:
        user = User(username=username, display_name=username, role=role, disabled=disabled)
        session.add(user)
        await session.flush()
        for group in groups:
            row = Group(name=group, label=group)
            session.add(row)
            await session.flush()
            session.add(GroupMember(group_id=row.id, user_id=user.id))
        await session.commit()
    return username


def _principal(username: str, *, role: str = "user", mode: str = "local") -> Principal:
    return Principal(username=username, role=role, verified=True, mode=mode)


# --------------------------------------------------------------------------
# 给本人、给组、给所有人
# --------------------------------------------------------------------------

async def test_a_grant_to_the_person_lets_them_use_the_pinned_version():
    wf = await _workflow()
    app = await _app({"alias": "week", "workflow_id": wf, "version": 1})
    carol = _name("carol")
    await _grant(app, USER, carol, "use")

    async with SessionLocal() as session:
        assert (await usable_calibers(session, carol)).get((wf, 1)) == "use"
        assert await caliber_role(session, carol, wf, 1) == "use"
        assert await caliber_granting_app(session, carol, wf, 1) == app
        # 当面请求给的是 Principal 时同一份判定
        assert await caliber_role(session, _principal(carol), wf, 1) == "use"


async def test_a_grant_to_a_group_reaches_the_people_in_it():
    wf = await _workflow()
    app = await _app({"alias": "week", "workflow_id": wf, "version": 1})
    fin = _name("财务")
    dave, erin = _name("dave"), _name("erin")
    await _person(dave, groups=(fin,))
    await _person(erin)
    await _grant(app, GROUP, fin, "observe")

    async with SessionLocal() as session:
        # 给用户名：组从名录取；给 Principal：他带的组是空的，也从名录取
        assert await caliber_role(session, dave, wf, 1) == "observe"
        assert await caliber_role(session, _principal(dave), wf, 1) == "observe"
        # 不在这个组里的人拿不到
        assert await caliber_role(session, erin, wf, 1) is None
        assert (wf, 1) not in await usable_calibers(session, erin)


async def test_a_grant_to_everyone_reaches_anyone_with_an_identity():
    wf = await _workflow()
    app = await _app({"alias": "week", "workflow_id": wf, "version": 1})
    await _grant(app, EVERYONE, "", "use")

    async with SessionLocal() as session:
        assert await caliber_role(session, _name("zoe"), wf, 1) == "use"
        assert await caliber_role(session, _principal(_name("amy")), wf, 1) == "use"
        assert (await usable_calibers(session, _name("leo"))).get((wf, 1)) == "use"


# --------------------------------------------------------------------------
# 固定在哪一版：版本要精确对上，归档、草稿不算
# --------------------------------------------------------------------------

async def test_only_the_exact_workflow_and_version_is_granted():
    wf = await _workflow(version=2)
    other = await _workflow()
    app = await _app({"alias": "new", "workflow_id": wf, "version": 2},
                     {"alias": "old", "workflow_id": other, "version": 1})
    carol = _name("carol")
    await _grant(app, USER, carol, "use")

    async with SessionLocal() as session:
        found = await usable_calibers(session, carol)
        assert found.get((wf, 2)) == "use" and found.get((other, 1)) == "use"
        # 同一个工作流的别的版本没有固定，不算能用
        assert await caliber_role(session, carol, wf, 1) is None
        assert (wf, 1) not in found
        # 别的工作流的同一版也不算
        assert await caliber_role(session, carol, _name("别的口径"), 2) is None


async def test_an_archived_app_grants_nothing():
    wf = await _workflow()
    app = await _app({"alias": "week", "workflow_id": wf, "version": 1}, status="archived")
    carol = _name("carol")
    await _grant(app, USER, carol, "use")

    async with SessionLocal() as session:
        assert await caliber_role(session, carol, wf, 1) is None
        assert (wf, 1) not in await usable_calibers(session, carol)


async def test_a_caliber_pinned_only_by_a_draft_is_not_granted():
    wf = await _workflow()
    # 当前发布版（v1）没固定它，v2 的草稿里才有
    app = await _app({"alias": "other", "workflow_id": await _workflow(), "version": 1},
                     draft_pins=[{"alias": "week", "workflow_id": wf, "version": 1}])
    carol = _name("carol")
    await _grant(app, USER, carol, "use")

    async with SessionLocal() as session:
        assert await caliber_role(session, carol, wf, 1) is None
        assert (wf, 1) not in await usable_calibers(session, carol)


# --------------------------------------------------------------------------
# 档位：use / observe / manage 取最高
# --------------------------------------------------------------------------

async def test_the_highest_grant_wins_and_names_the_app_it_came_from():
    wf = await _workflow()
    only_use = await _app({"alias": "week", "workflow_id": wf, "version": 1})
    observing = await _app({"alias": "week", "workflow_id": wf, "version": 1})
    managing = await _app({"alias": "week", "workflow_id": wf, "version": 1})
    elsewhere = await _app({"alias": "other", "workflow_id": await _workflow(), "version": 1})
    carol = _name("carol")
    await _grant(only_use, USER, carol, "use")
    await _grant(observing, USER, carol, "observe")
    await _grant(managing, USER, carol, "manage")
    await _grant(elsewhere, USER, carol, "use")

    async with SessionLocal() as session:
        assert (await usable_calibers(session, carol)).get((wf, 1)) == "manage"
        assert await caliber_role(session, carol, wf, 1) == "manage"
        assert await caliber_granting_app(session, carol, wf, 1) == managing


async def test_a_grant_below_use_is_not_a_grant():
    wf = await _workflow()
    app = await _app({"alias": "week", "workflow_id": wf, "version": 1})
    carol = _name("carol")
    await _grant(app, USER, carol, "reader")

    async with SessionLocal() as session:
        assert await caliber_role(session, carol, wf, 1) is None
        assert (wf, 1) not in await usable_calibers(session, carol)


async def test_a_grant_on_an_app_that_does_not_pin_this_version_grants_nothing():
    wf = await _workflow()
    app = await _app({"alias": "week", "workflow_id": wf, "version": 7})
    carol = _name("carol")
    await _grant(app, USER, carol, "manage")

    async with SessionLocal() as session:
        assert await caliber_role(session, carol, wf, 1) is None
        assert await caliber_granting_app(session, carol, wf, 1) is None
        assert (wf, 1) not in await usable_calibers(session, carol)


# --------------------------------------------------------------------------
# 没有主体、停用、没有授权
# --------------------------------------------------------------------------

async def test_without_a_subject_there_is_no_caliber_authorization():
    wf = await _workflow()
    app = await _app({"alias": "week", "workflow_id": wf, "version": 1})
    await _grant(app, EVERYONE, "", "manage")  # 所有人都有，但没有主体的人不算

    async with SessionLocal() as session:
        assert await caliber_role(session, None, wf, 1) is None
        assert await caliber_role(session, "", wf, 1) is None
        assert await caliber_role(session, "   ", wf, 1) is None
        # 关登录的署名可以是空的；管理员、关登录都不在这里自动放行
        assert await caliber_role(session, Principal(username=None, role="admin", mode="off", via="off"), wf, 1) is None
        assert await usable_calibers(session, None) == {}


async def test_a_disabled_person_has_no_caliber_authorization():
    wf = await _workflow()
    app = await _app({"alias": "week", "workflow_id": wf, "version": 1})
    frank = await _person(_name("frank"), disabled=True)
    await _grant(app, USER, frank, "manage")

    async with SessionLocal() as session:
        assert await caliber_role(session, frank, wf, 1) is None
        assert await caliber_role(session, _principal(frank), wf, 1) is None
        assert (wf, 1) not in await usable_calibers(session, frank)


async def test_a_person_without_a_grant_gets_nothing():
    wf = await _workflow()
    app = await _app({"alias": "week", "workflow_id": wf, "version": 1})
    gina = _name("gina")
    await _grant(app, USER, _name("someone-else"), "manage")

    async with SessionLocal() as session:
        assert await caliber_role(session, gina, wf, 1) is None
        assert await caliber_granting_app(session, gina, wf, 1) is None
        assert (wf, 1) not in await usable_calibers(session, gina)


# --------------------------------------------------------------------------
# 角色不是资产授权
# --------------------------------------------------------------------------

async def test_builder_and_admin_roles_are_not_asset_authorization():
    wf = await _workflow()
    app = await _app({"alias": "week", "workflow_id": wf, "version": 1})
    bob = await _person(_name("bob"), role="builder")
    root = await _person(_name("root"), role="admin")

    async with SessionLocal() as session:
        # 搭建者、管理员，没有授权就是没有：这两个函数不按角色放行（调用方显式放行搭建者及以上）
        assert await caliber_role(session, bob, wf, 1) is None
        assert await caliber_role(session, _principal(bob, role="builder"), wf, 1) is None
        assert await caliber_role(session, root, wf, 1) is None
        assert await caliber_role(session, _principal(root, role="admin"), wf, 1) is None
        # 关登录的 Principal 也一样：拿到的是授权那一档，不是 manage
        assert await caliber_role(session, _principal(_name("carol"), mode="off"), wf, 1) is None

    # 有了授权，拿到的也是授权那一档本身（搭建者不因此升成 manage）
    await _grant(app, USER, bob, "observe")
    carol = _name("carol")
    await _grant(app, USER, carol, "use")
    async with SessionLocal() as session:
        assert await caliber_role(session, _principal(bob, role="builder"), wf, 1) == "observe"
        assert await caliber_role(session, _principal(carol, mode="off"), wf, 1) == "use"


# --------------------------------------------------------------------------
# 正式出具复用同一份判定（ledger/issue._granting_app）
# --------------------------------------------------------------------------

async def test_issue_granting_app_uses_the_same_judgment():
    from app.ledger import issue as ledger_issue

    wf = await _workflow()
    app = await _app({"alias": "week", "workflow_id": wf, "version": 1})
    carol = _name("carol")
    await _grant(app, USER, carol, "observe")

    async with SessionLocal() as session:
        # 至少 use 的授权够出具，靠的是这份应用
        assert await ledger_issue._granting_app(session, _principal(carol), wf, 1) == app
        # 没有授权的使用者、搭建者都没有「靠哪份应用」：拒绝的话是 check_target 里那句「请搭建者出具」
        assert await ledger_issue._granting_app(session, _principal(_name("dave")), wf, 1) is None
        assert await ledger_issue._granting_app(session, _principal(_name("bob"), role="builder"), wf, 1) is None
        # 换一版不在授权里
        assert await ledger_issue._granting_app(session, _principal(carol), wf, 2) is None

    archived = await _app({"alias": "week", "workflow_id": wf, "version": 1}, status="archived")
    await _grant(archived, USER, carol, "manage")
    async with SessionLocal() as session:
        # 归档的应用固定不住任何东西：还是原来那份发布着的
        assert await ledger_issue._granting_app(session, _principal(carol), wf, 1) == app
