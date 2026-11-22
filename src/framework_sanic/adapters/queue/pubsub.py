"""Google Cloud Pub/Sub Queue adapter (production). Requires the ``gcp`` extra.

One topic + subscription per (topic, lane, shard): ``{prefix}-{topic}-{lane}-{shard}``. Pulls
drain higher lanes first. Delayed redelivery uses ``modify_ack_deadline`` (max 600 s); attempts
come from ``delivery_attempt`` (requires a dead-letter policy on the subscription).
Autoscaling should use the Cloud Monitoring metric ``subscription/oldest_unacked_message_age``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any

from framework_sanic.core.domain.records import LANE_ORDER, Lane, shard_for
from framework_sanic.core.ports.queue import Delivery, QueueMessage, QueueStats

MAX_ACK_DEADLINE = 600


class PubSubQueue:
    def __init__(
        self,
        project: str,
        *,
        prefix: str,
        shard_count: int,
        topics: Sequence[str],
        publisher: Any = None,
        subscriber: Any = None,
    ) -> None:
        from google.cloud import pubsub_v1  # type: ignore[attr-defined]

        self.shard_count = shard_count
        self._project = project
        self._prefix = prefix
        self._topics = list(topics)
        self._publisher = publisher or pubsub_v1.PublisherClient()
        self._subscriber = subscriber or pubsub_v1.SubscriberClient()

    def _name(self, topic: str, lane: Lane, shard: int) -> str:
        return f"{self._prefix}-{topic}-{lane.value}-{shard}"

    def _topic_path(self, topic: str, lane: Lane, shard: int) -> str:
        return str(self._publisher.topic_path(self._project, self._name(topic, lane, shard)))

    def _sub_path(self, topic: str, lane: Lane, shard: int) -> str:
        return str(self._subscriber.subscription_path(self._project, self._name(topic, lane, shard) + "-sub"))

    async def ensure_resources(self) -> None:
        """Create topics/subscriptions (for the emulator; terraform owns them in real projects)."""
        from google.api_core.exceptions import AlreadyExists

        def create() -> None:
            for topic in self._topics:
                for lane in LANE_ORDER:
                    for shard in range(self.shard_count):
                        tp = self._topic_path(topic, lane, shard)
                        try:
                            self._publisher.create_topic(request={"name": tp})
                        except AlreadyExists:
                            pass
                        try:
                            self._subscriber.create_subscription(
                                request={
                                    "name": self._sub_path(topic, lane, shard),
                                    "topic": tp,
                                    "ack_deadline_seconds": 60,
                                }
                            )
                        except AlreadyExists:
                            pass

        await asyncio.to_thread(create)

    async def publish(self, message: QueueMessage) -> None:
        shard = shard_for(message.company_id, self.shard_count)
        data = message.model_dump_json().encode()
        future = self._publisher.publish(self._topic_path(message.topic, message.lane, shard), data)
        await asyncio.wrap_future(future)  # pubsub futures subclass concurrent.futures.Future

    async def pull(self, topic: str, shards: Sequence[int], max_messages: int) -> list[Delivery]:
        out: list[Delivery] = []
        for lane in LANE_ORDER:
            for shard in shards:
                if len(out) >= max_messages:
                    return out
                sub = self._sub_path(topic, lane, shard)
                request = {"subscription": sub, "max_messages": max_messages - len(out)}
                resp = await asyncio.to_thread(self._subscriber.pull, request=request, timeout=5.0)
                for rm in resp.received_messages:
                    msg = QueueMessage.model_validate(json.loads(rm.message.data))
                    if getattr(rm, "delivery_attempt", 0):
                        msg = msg.model_copy(update={"attempt": max(msg.attempt, rm.delivery_attempt - 1)})
                    out.append(Delivery(message=msg, shard=shard, ack_id=f"{sub}|{rm.ack_id}"))
        return out

    async def ack(self, delivery: Delivery) -> None:
        sub, ack_id = delivery.ack_id.split("|", 1)
        await asyncio.to_thread(
            lambda: self._subscriber.acknowledge(request={"subscription": sub, "ack_ids": [ack_id]})
        )

    async def nack(self, delivery: Delivery, delay_seconds: float, *, count_attempt: bool = True) -> None:
        sub, ack_id = delivery.ack_id.split("|", 1)
        if delay_seconds > MAX_ACK_DEADLINE:
            # Longer delays: republish later is not supported natively; cap at the max deadline.
            delay_seconds = MAX_ACK_DEADLINE
        await asyncio.to_thread(
            lambda: self._subscriber.modify_ack_deadline(
                request={"subscription": sub, "ack_ids": [ack_id], "ack_deadline_seconds": int(delay_seconds)}
            )
        )

    async def stats(self, topic: str) -> QueueStats:
        return QueueStats(depth=-1, oldest_age_seconds=-1.0)  # read from Cloud Monitoring instead

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        await asyncio.to_thread(self._subscriber.close)
