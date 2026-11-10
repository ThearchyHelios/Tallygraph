"""口径的领域边界（K03、A01）：一份口径卡按卡和指标说话，不说节点、data.config 和路径。

图仍是唯一真相（A01：每份口径只有一个权威写入源）。GraphBackedCaliberRepository 在图上给一层很薄的领域接口：
按卡 id 打开一张源头口径卡、按稳定 metric_id 读一条指标、把改过的这几条换回去——换在**整图的深拷贝**上，
没提到的字段、别的指标、别人加的扩展字段和其他节点原样带着走。这里不做整图 ↔ 领域模型的双向转换，也不另存一份
可编辑的定义：那就是第二份真相，A01 明令禁止。

只认源头口径卡：转抄别处的口径卡（caliber_from，ledger/compose 内联实例化出来的那些）不是权威写入源，改它等于
在别处另存一份定义。指标的完整身份是「卡 + 卡上的 metric_id」（4.1），所以这里所有读写都同时带着这两个。

定义指纹用现成的 data/caliber_identity（只按语义字段算，空值不进哈希）：变更服务据此说「这次动没动定义」，
不另造一套指纹。
"""
from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


class CaliberGone(ValueError):
    """图里没有这张可写的口径卡：不在图上、不是口径卡节点，或者是转抄别处的口径卡（不是权威写入源）。"""


class MetricGone(LookupError):
    """这张口径卡上没有这个 metric_id。"""


@dataclass(frozen=True)
class CaliberCard:
    """一张源头口径卡的身份：图上的节点 id、给人看的标题、口径名和口径版本。"""

    node_id: str
    title: str
    caliber: str
    caliber_version: str


def _config_of(node: Any) -> dict[str, Any]:
    """节点上的配置（图的原始形状 data.config）；没有或者不是对象时交回空的那一份（读的时候不改图）。"""
    data = node.get("data") if isinstance(node, Mapping) else None
    config = data.get("config") if isinstance(data, Mapping) else None
    return config if isinstance(config, dict) else {}


class GraphBackedCaliberRepository:
    """一张源头口径卡在图上的一层薄领域接口。构造时就把卡定位好，定位不到直接 CaliberGone。"""

    def __init__(self, graph: Mapping[str, Any] | None, *, card_id: str) -> None:
        self._graph = graph if isinstance(graph, dict) else {}
        self.card_id = str(card_id or "")
        self._node = self._card_node()

    def _card_node(self) -> dict[str, Any]:
        for node in self._graph.get("nodes") or []:
            if not isinstance(node, dict) or str(node.get("id")) != self.card_id:
                continue
            if node.get("type") != "metrics":
                raise CaliberGone(f"图上这个节点不是口径卡：{self.card_id}")
            if _config_of(node).get("caliber_from") not in (None, "", {}):
                raise CaliberGone("这张卡是从别处转抄来的口径：要改的是它抄的那一份")
            return node
        raise CaliberGone(f"图上没有这张口径卡：{self.card_id}")

    @property
    def card(self) -> CaliberCard:
        data = self._node.get("data") if isinstance(self._node.get("data"), dict) else {}
        config = _config_of(self._node)
        return CaliberCard(node_id=self.card_id, title=str(data.get("label") or self.card_id),
                           caliber=str(config.get("caliber") or ""),
                           caliber_version=str(config.get("caliber_version") or ""))

    def ids(self) -> list[str]:
        """卡上的 metric_id，按它们在图里的先后（重排只改顺序，不改 ID）。没有 id 的条目（画布上留下的半成品）跳过。"""
        return [str(metric["id"]) for metric in self._metrics()
                if isinstance(metric, dict) and str(metric.get("id") or "").strip()]

    def metrics(self) -> list[dict[str, Any]]:
        """卡上每条指标的深拷贝（顺序照图里）。"""
        return [copy.deepcopy(metric) for metric in self._metrics()]

    def metric(self, metric_id: str) -> dict[str, Any]:
        """一条指标的深拷贝；卡上没有它时 MetricGone。调用方随便改这一份，图不动。"""
        wanted = str(metric_id or "")
        for metric in self._metrics():
            if isinstance(metric, dict) and str(metric.get("id") or "") == wanted:
                return copy.deepcopy(metric)
        raise MetricGone(wanted)

    def graph_copy(self) -> dict[str, Any]:
        """整图的深拷贝：调用方（变更服务）把改过的那一份交回去保存，原图不动。"""
        return copy.deepcopy(self._graph)

    def replace_node_configs(self, configs: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
        """换掉图上这几个取数节点的 data.config（node_id → 整份新配置），交回整图的深拷贝。

        K04 的绑定同步用：调用方（ledger/bindings.plan）给的新配置是在**原配置的深拷贝**上只改两处得来的，
        所以节点上别的键（tool、args 里别的项、扩展字段）、别的地方一个字不动。指到图里没有的节点时 KeyError——
        这是服务端的 bug，照实抛出去，不当成一次成功的同步。
        """
        graph = copy.deepcopy(self._graph)
        nodes = {str(node.get("id")): node for node in graph.get("nodes") or [] if isinstance(node, dict)}
        for node_id, config in configs.items():
            node = nodes.get(str(node_id))
            if node is None:
                raise KeyError(f"图上没有这个节点：{node_id}")
            data = node.get("data")
            if not isinstance(data, dict):
                data = node["data"] = {}
            data["config"] = copy.deepcopy(dict(config))
        return graph

    def replace_metrics(self, metrics: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
        """换掉 map 里这几条指标（metric_id → 整条指标），交回**整图的深拷贝**：别的节点、别的指标、图级扩展
        字段和这条指标里没被换掉的键都在。map 里有卡上不存在的 id 时 MetricGone——首版不支持用字段操作新增指标。"""
        wanted = {str(key): value for key, value in metrics.items()}
        known = set(self.ids())
        for metric_id in wanted:
            if metric_id not in known:
                raise MetricGone(metric_id)
        graph = copy.deepcopy(self._graph)
        node = next(item for item in graph.get("nodes") or [] if str(item.get("id")) == self.card_id)
        data = node.get("data")
        if not isinstance(data, dict):
            data = node["data"] = {}
        config = data.get("config")
        if not isinstance(config, dict):
            config = data["config"] = {}
        replaced: list[Any] = []
        for metric in self._metrics_of(node):
            metric_id = str(metric.get("id") or "") if isinstance(metric, dict) else ""
            replaced.append(copy.deepcopy(wanted[metric_id]) if metric_id in wanted else metric)
        config["metrics"] = replaced
        return graph

    def definition_hash(self) -> str:
        """这张卡的指标定义指纹（data/caliber_identity：只取语义字段，空值不进哈希，指标按 id 排序）。"""
        from app.data.caliber_identity import definition_hash

        card = self.card
        return definition_hash(card.caliber, card.caliber_version, self._metrics())

    def _metrics(self) -> list[Any]:
        return self._metrics_of(self._node)

    @staticmethod
    def _metrics_of(node: Mapping[str, Any]) -> list[Any]:
        metrics = _config_of(node).get("metrics")
        return metrics if isinstance(metrics, list) else []


__all__ = ["CaliberCard", "CaliberGone", "GraphBackedCaliberRepository", "MetricGone"]
