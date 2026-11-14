from __future__ import annotations

import asyncio
import contextlib
from collections import OrderedDict, defaultdict
from collections.abc import Callable
from typing import Any, AsyncIterator

from app.core.events import RunEventModel

# 单个订阅者的积压上限。慢消费者（比如卡住的浏览器标签）不应该拖垮整个 run，
# 所以队列满了就丢最旧的事件并打标记，而不是阻塞发布方。
_QUEUE_MAXSIZE = 2048
_MAX_SEQ_ENTRIES = 4096
_OUT_OF_ORDER_WINDOW = 64


def _is_ephemeral(event: Any) -> bool:
    if event is None:
        return False
    if isinstance(event, dict):
        if event.get("ephemeral"):
            return True
        ev_type = event.get("type")
    else:
        if getattr(event, "ephemeral", False):
            return True
        ev_type = getattr(event, "type", None)
        if ev_type is None:
            data = getattr(event, "data", None)
            if isinstance(data, dict) and data.get("ephemeral"):
                return True
    ev_type_str = ev_type.value if hasattr(ev_type, "value") else str(ev_type or "")
    return ev_type_str in ("llm.token", "llm.thinking.delta")


def _event_seq(event: Any) -> int:
    raw = event.get("seq") if isinstance(event, dict) else getattr(event, "seq", None)
    return int(raw or 0)


class _Subscriber:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[RunEventModel | None] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self.dropped = 0
        self.closed = False


class EventBus:
    """按 run_id 分频道的内存 pub/sub。

    只负责实时分发；历史事件由 DB 承担，订阅者接入时先读库补齐再接实时流，
    这样刷新页面或中途接入都能看到完整时间线。

    多进程（app/cluster/relay.py）：本进程发布的每一条也交给 tap（转发器据此给别的进程发通知、给在看的进程转流式增量）；
    别的进程执行的运行，转发器从库里按 seq 读出来再在本进程发布一遍（relayed=True，不再交给 tap，免得来回转）。
    """

    def __init__(self) -> None:
        self._subs: dict[str, set[_Subscriber]] = defaultdict(set)
        self._seq: OrderedDict[str, int] = OrderedDict()
        self._active: set[str] = set()
        self._lock = asyncio.Lock()
        self.tap: Callable[[RunEventModel], None] | None = None

    def mark_active(self, run_id: str) -> None:
        if run_id:
            self._active.add(run_id)

    def mark_inactive(self, run_id: str) -> None:
        self._active.discard(run_id)

    def _trim_seq(self) -> None:
        if len(self._seq) <= _MAX_SEQ_ENTRIES:
            return
        for rid in list(self._seq.keys()):
            if len(self._seq) <= _MAX_SEQ_ENTRIES:
                break
            if self._subs.get(rid) or rid in self._active:
                continue
            self._seq.pop(rid, None)

    def next_seq(self, run_id: str) -> int:
        nxt = self._seq.get(run_id, 0) + 1
        self._seq[run_id] = nxt
        self._seq.move_to_end(run_id)
        self._trim_seq()
        return nxt

    def set_seq(self, run_id: str, value: int) -> None:
        """恢复一个已存在的 run 时，把序号接上，保证前端排序不乱。"""
        self._seq[run_id] = max(self._seq.get(run_id, 0), value)
        self._seq.move_to_end(run_id)
        self._trim_seq()

    async def publish(self, event: RunEventModel, *, relayed: bool = False) -> None:
        if not relayed and self.tap is not None:
            try:
                self.tap(event)
            except Exception:  # noqa: BLE001 - 转发出错不能拦住本进程的推送
                pass
        run_id = event.get("run_id") if isinstance(event, dict) else event.run_id
        incoming_ephemeral = _is_ephemeral(event)
        for sub in list(self._subs.get(run_id, ())):
            if sub.closed:
                continue
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                dq = sub.queue._queue  # type: ignore[attr-defined]
                evicted_ephemeral = False
                for existing in dq:
                    if _is_ephemeral(existing):
                        dq.remove(existing)
                        evicted_ephemeral = True
                        break
                if incoming_ephemeral:
                    sub.dropped += 1
                    if evicted_ephemeral:
                        with contextlib.suppress(asyncio.QueueFull):
                            sub.queue.put_nowait(event)
                    # 若队列里全是结构性事件，则直接丢弃当前 token，绝不挤掉 node.*/run.* 事件
                else:
                    if evicted_ephemeral:
                        sub.dropped += 1
                    else:
                        if dq and dq[0] is None:
                            continue
                        with contextlib.suppress(asyncio.QueueEmpty):
                            sub.queue.get_nowait()
                            sub.dropped += 1
                    with contextlib.suppress(asyncio.QueueFull):
                        sub.queue.put_nowait(event)

    async def close(self, run_id: str) -> None:
        """给所有订阅者发结束哨兵，让 WebSocket 干净地收尾。"""
        self._seq.pop(run_id, None)
        self._active.discard(run_id)
        for sub in list(self._subs.get(run_id, ())):
            sub.closed = True
            if sub.queue.full():
                dq = sub.queue._queue  # type: ignore[attr-defined]
                if dq and dq[-1] is None:
                    continue
                evicted = False
                for existing in dq:
                    if _is_ephemeral(existing):
                        dq.remove(existing)
                        sub.dropped += 1
                        evicted = True
                        break
                if not evicted:
                    with contextlib.suppress(asyncio.QueueEmpty):
                        popped = sub.queue.get_nowait()
                        if popped is not None:
                            sub.dropped += 1
            with contextlib.suppress(asyncio.QueueFull):
                sub.queue.put_nowait(None)

    def attach(self, run_id: str) -> "Subscription":
        """立刻占一个位，之后再慢慢消费。

        必须能在读历史**之前**占位。`subscribe()` 做不到这件事：它是异步
        生成器，订阅者要等到第一次迭代才真正注册——而调用方通常先读完历史
        才开始迭代。这中间发出的事件既不在历史里、也不在实时流里，就这么
        没了。一次跑图从发起到订阅之间正好有几毫秒，丢掉的往往是开头那
        两三条 node.started/node.finished，表现为"某个节点永远在转圈、
        它的子步骤跑到了顶层"。
        """
        sub = _Subscriber()
        self._subs[run_id].add(sub)
        return Subscription(self, run_id, sub)

    def _detach(self, run_id: str, sub: _Subscriber) -> None:
        self._subs.get(run_id, set()).discard(sub)
        if run_id in self._subs and not self._subs[run_id]:
            self._subs.pop(run_id, None)

    async def subscribe(self, run_id: str) -> AsyncIterator[RunEventModel]:
        """便捷入口：占位即消费。只适合不需要补历史的场景。"""
        with self.attach(run_id) as sub:
            async for event in sub.stream():
                yield event

    def subscriber_count(self, run_id: str) -> int:
        return len(self._subs.get(run_id, ()))


class Subscription:
    """一个已经在收事件的订阅位。"""

    def __init__(self, bus: EventBus, run_id: str, sub: _Subscriber) -> None:
        self._bus = bus
        self._run_id = run_id
        self._sub = sub

    @property
    def dropped(self) -> int:
        """因为消费太慢被丢掉的条数。不是 0 就说明这条时间线有洞。"""
        return self._sub.dropped

    @property
    def closed(self) -> bool:
        return self._sub.closed

    @closed.setter
    def closed(self, value: bool) -> None:
        self._sub.closed = bool(value)

    async def stream(self, after: int = 0, seen: set[int] | None = None) -> AsyncIterator[RunEventModel]:
        """吐出实时事件。

        after 用来去重：占位到读完历史之间发出的事件，两边都有一份。
        按 seq 过滤掉已经补过的，前端就不会看到同一步出现两次。
        若调用方传入回放窗口的 seen 集合，允许迟提交（seq <= after 但不在 seen 且在近端窗口内）
        的落库事件补发，同时按 seen 去重保证同一 seq 不发两次。
        """
        seen_seqs: set[int] | None = set(seen) if seen is not None else None
        floor = max(0, int(after or 0) - _OUT_OF_ORDER_WINDOW)
        while True:
            if self._sub.closed and self._sub.queue.empty():
                break
            item = await self._sub.queue.get()
            if item is None:
                break
            seq = _event_seq(item)
            if seen_seqs is None:
                if after and seq <= after:
                    continue
                yield item
                continue
            if _is_ephemeral(item):
                if after and seq and seq <= after:
                    continue
                yield item
                continue
            if seq:
                if seq in seen_seqs:
                    continue
                if after and seq <= after and seq <= floor:
                    continue
                seen_seqs.add(seq)
            yield item

    def close(self) -> None:
        self._bus._detach(self._run_id, self._sub)

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


bus = EventBus()
