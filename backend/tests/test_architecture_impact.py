from app.apps.impact import dependents
from app.db.base import SessionLocal
from tests.test_app_versions import chain, node, workflow


async def test_impact_follows_caliber_and_subgraph_dependencies_transitively():
    root = await workflow({1: "published", 2: "published"}, current=2)
    one = chain(node("in", "input"), node("card", "metrics", caliber_from={
        "workflow_id": root, "workflow_version": 1, "node_id": "card"}))
    parent = await workflow({1: "published"}, current=1, graphs={1: one})
    two = chain(node("in", "input"), node("child", "subgraph", workflow_id=parent, workflow_version=1))
    grandparent = await workflow({1: "published"}, current=1, graphs={1: two})
    async with SessionLocal() as session:
        impact = await dependents(session, root, 1)
        unaffected = await dependents(session, root, 2)
    found = {(r["workflow_id"], r["depth"]) for r in impact["workflows"]}
    assert (parent, 1) in found and (grandparent, 2) in found
    assert not unaffected["workflows"]
