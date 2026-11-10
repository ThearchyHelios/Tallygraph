"""应用级权限（§3.12）：use 能用（入口、视图、自己的实例）；observe 另能看全部实例；manage 能搭建、发布、授权、管钩子。

- 关登录：全部放行，等同 manage（界面上挂横幅说明，签批人是自报的署名）；
- admin：所有应用 manage；
- builder：所有应用至少 observe（能用、能看全部实例）；自己建的、或者有 manage 授权的才 manage；
- user：按授权（给本人、给所在的组、给所有人）取最高的那一档，最高到 observe：搭建、发布、绑定岗位、授权、钩子、按草稿
  预览、改派催办都不给 user 角色（§3.12），授给它的 manage 按 observe 算。没有授权就看不到这个应用。
- 个人应用（scope=personal，docs/design/normal-user-apps.md N7）：只有拥有者能用，按 manage 算（改页面、刷新、提交审核）；
  别人一律没有权限、看不到，管理员也一样——管理员只在应用列表里看得到它存在、能归档（can_archive），不看内容（P4）。
  个人应用没有授权（不能分享），授权表里不会有它。

**口径资产授权**（usable_calibers / caliber_role / caliber_granting_app，文件末尾）：普通使用者按口径问数、按口径出具时，
「我能不能用这一版口径」看的是同一张 AppGrant，对象换成应用当前发布版固定下来的那一版口径（AppVersion.pins，工作流和版本
都要精确对上）。给本人、给他所在的组、给所有人的授权都算，至少 use；同一版取最高档。**builder / admin / 关登录不在这里
放行**——调用方先显式放行，这几个函数只判「授权有没有把这一版口径给到这个主体」。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal
from app.db.models_apps import AppGrant

RANK = {"use": 1, "observe": 2, "manage": 3}
#: 三档授权的叫法（同前端 GRANT_ROLE_LABEL）：权限不够的报错、授权核对的说明里写这个
ROLE_TEXT = {"use": "使用", "observe": "查看全部实例", "manage": "管理"}


def at_least(role: str | None, need: str) -> bool:
    return RANK.get(role or "", 0) >= RANK[need]


def granted_role(principal: Principal, grants: list[Any]) -> str | None:
    """授权里这个人能拿到的最高一档。"""
    best: str | None = None
    groups = set(principal.groups)
    for g in grants:
        hit = (g.principal_kind == "everyone"
               or (g.principal_kind == "user" and principal.username and g.principal == principal.username)
               or (g.principal_kind == "group" and g.principal in groups))
        if hit and RANK.get(g.role, 0) > RANK.get(best or "", 0):
            best = g.role
    return best


def personal_role(principal: Principal, app: Any) -> str | None:
    """个人应用上这个人的权限：拥有者 manage，别人 None。关登录时全部放行（没有可信的身份，同共享应用）。"""
    if principal.mode == "off":
        return "manage"
    return "manage" if app.owner and principal.username == app.owner else None


def is_personal(app: Any) -> bool:
    return getattr(app, "scope", "shared") == "personal"


def can_archive(principal: Principal, app: Any, role: str | None) -> bool:
    """能不能归档：管理权限；个人应用另外管理员也能（看不到内容，但能把它收起来）。"""
    return at_least(role, "manage") or (is_personal(app) and principal.role == "admin")


def role_of(principal: Principal, app: Any, grants: list[Any]) -> str | None:
    if is_personal(app):
        return personal_role(principal, app)
    if principal.mode == "off" or principal.role == "admin":
        return "manage"
    granted = granted_role(principal, grants)
    if principal.role == "builder":
        if app.created_by and principal.username and app.created_by == principal.username:
            return "manage"
        return granted if at_least(granted, "manage") else "observe"
    return "observe" if at_least(granted, "manage") else granted


async def grants_of(session: AsyncSession, app_id: str) -> list[AppGrant]:
    return list((await session.execute(select(AppGrant).where(AppGrant.app_id == app_id)
                                       .order_by(AppGrant.created_at, AppGrant.id))).scalars())


async def app_role(session: AsyncSession, principal: Principal, app: Any) -> str | None:
    if is_personal(app):
        return personal_role(principal, app)
    if principal.mode == "off" or principal.role == "admin":
        return "manage"
    return role_of(principal, app, await grants_of(session, app.id))


# --------------------------------------------------------------------------
# 口径资产授权：应用授权把哪几版口径给了这个主体（普通使用者「我能用哪些口径」）
# --------------------------------------------------------------------------

async def _subject(session: AsyncSession, actor: Any) -> Principal | None:
    """按谁判口径资产授权：actor 可以是这次请求的 Principal，也可以是用户名。没有主体时 None。

    - 用户名（或以 username 为身份的对象）：从名录取组，授权本来就是按名字授的，署名、服务账号、名录里还没有的人都照名字算；
    - 停用的人算没有主体（auth/directory：停用的人在名录里，认得出「这个人停用了」，不是「没有这个人」）；
    - actor 为空、用户名为空（关登录时署名可以是空的）都算没有主体。
    角色（builder / admin）和关登录都不在这里放行：由调用方决定。
    """
    from app.auth.sessions import groups_of
    from app.db.models import User

    if actor is None:
        return None
    if isinstance(actor, str):
        name, groups = actor.strip(), ()
    else:
        name = str(getattr(actor, "username", "") or "").strip()
        groups = tuple(getattr(actor, "groups", ()) or ())
    if not name:
        return None
    user = (await session.execute(select(User).where(User.username == name))).scalars().first()
    if user is not None:
        if user.disabled:
            return None
        if not groups:
            groups = await groups_of(session, user.id)
    return Principal(username=name, role="user", groups=groups)


def _pinned_key(pin: Any) -> tuple[str, int] | None:
    """AppVersion.pins 里的一项指的是哪一版口径：工作流和版本都要在，版本认不成整数的不算。"""
    if not isinstance(pin, dict) or not pin.get("workflow_id") or pin.get("version") is None:
        return None
    try:
        return str(pin["workflow_id"]), int(pin["version"])
    except (TypeError, ValueError):
        return None


async def _caliber_grants(session: AsyncSession, actor: Any) -> dict[tuple[str, int], tuple[str, str]]:
    """这个主体能按哪些（工作流, 版本）用：{(workflow_id, version): (最高一档授权, 靠哪一份应用)}。

    只看**未归档应用的当前发布版**固定着的版本（AppVersion.pins，工作流和版本都精确对上）：应用升过级、不再固定它的旧版本
    不算「正在用」，草稿里的固定也不算。授权按 AppGrant 取给本人、他所在的组、所有人的最高一档，低于 use 的不算。
    """
    from app.db.models_apps import App, AppVersion

    subject = await _subject(session, actor)
    if subject is None:
        return {}
    rows = (await session.execute(
        select(App.id, AppVersion.pins)
        .join(AppVersion, and_(AppVersion.app_id == App.id, AppVersion.version == App.published_version))
        .where(App.status != "archived").order_by(App.id))).all()
    found: dict[tuple[str, int], tuple[str, str]] = {}
    for app_id, pins in rows:
        role = granted_role(subject, await grants_of(session, app_id))
        if not at_least(role, "use"):
            continue
        for pin in pins or []:
            key = _pinned_key(pin)
            if key is not None and RANK.get(role, 0) > RANK.get((found.get(key) or ("",))[0], 0):
                found[key] = (role, app_id)
    return found


async def _lookup(session: AsyncSession, actor: Any, workflow_id: str, version: int) -> tuple[str, str] | None:
    """问的这一版在这个主体的授权表里吗：在就给（最高一档授权, 靠哪一份应用），问的版本认不成整数就是不在。"""
    try:
        key = (str(workflow_id), int(version))
    except (TypeError, ValueError):
        return None
    return (await _caliber_grants(session, actor)).get(key)


async def usable_calibers(session: AsyncSession, actor: Any) -> dict[tuple[str, int], str]:
    """这个主体能按哪些口径版本使用：{(workflow_id, version): 最高一档授权}（use / observe / manage）。

    「能用」= 未归档应用的当前发布版精确固定着这一版（AppVersion.pins），且 AppGrant 里给了他本人、他所在的组或所有人
    至少 use 的授权。没有主体、停用、没有授权的口径不在表里（不是记 None）。builder / admin / 关登录由调用方先放行。
    """
    return {key: role for key, (role, _) in (await _caliber_grants(session, actor)).items()}


async def caliber_role(session: AsyncSession, actor: Any, workflow_id: str, version: int) -> str | None:
    """这个主体按这一版口径拿到的最高一档资产授权；没有主体、停用、没有授权都是 None。

    调用方判「谁是使用者」：builder / admin / 关登录、以及个人应用、共享链接这些身份，各自在自己的入口先放行或收窄，
    这里只回答「授权有没有把这一版口径给到这个主体」。
    """
    found = await _lookup(session, actor, workflow_id, version)
    return found[0] if found else None


async def caliber_granting_app(session: AsyncSession, actor: Any, workflow_id: str, version: int) -> str | None:
    """这一版的资产授权是靠哪一份应用给的（和 caliber_role 同一份判定，最高档的那份）：没有为 None。

    正式出具（ledger/issue）把它记进 run.origin.granted_by_app：使用者能按这一版出具，靠的是哪个应用的授权。
    """
    found = await _lookup(session, actor, workflow_id, version)
    return found[1] if found else None
