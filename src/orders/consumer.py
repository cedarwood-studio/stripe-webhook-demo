"""Lambda fed by the orders queue: a stand-in for the step that places the print job.

It does no printing. It shows two things the real step needs: a redelivered
message does not run the step twice, and a message that can never succeed
ends up in the dead-letter queue instead of looping.
"""

from __future__ import annotations

import json
import logging
import os
import time

from orders.store import Store

log = logging.getLogger()
log.setLevel(logging.INFO)


class PoisonMessage(Exception):
    """Raised so SQS redelivers the message and, after maxReceiveCount, dead-letters it."""


def process(record: dict, store: Store, now: int) -> str:
    try:
        order_id = json.loads(record["body"])["order_id"]
        if not isinstance(order_id, str) or not order_id:
            raise ValueError("empty order_id")
    except (ValueError, KeyError, TypeError) as exc:
        raise PoisonMessage(f"message {record.get('messageId')} has no order_id") from exc

    result = store.record_delivery(order_id, now)
    if result == "unknown":
        raise PoisonMessage(f"order {order_id} does not exist or is not paid")
    if result == "first":
        log.info("order %s: fulfillment step runs here, once", order_id)
    else:
        log.info("order %s: repeat delivery, nothing to do", order_id)
    return result


_store: Store | None = None


def handler(event, context):
    global _store
    if _store is None:
        import boto3

        _store = Store(boto3.client("dynamodb"), os.environ["TABLE_NAME"])
    # batch_size is 1 in the Terraform, so one bad message cannot take good ones down with it.
    return {"results": [process(r, _store, int(time.time())) for r in event.get("Records", [])]}
