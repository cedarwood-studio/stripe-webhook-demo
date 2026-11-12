"""Lambda behind an API Gateway HTTP API: POST /webhooks/stripe.

Response codes, as Stripe reads them:
  200  handled, or safely ignored. Stripe stops retrying.
  400  not a valid signed Stripe request. Nothing was read or written.
  409  the same event is being processed right now. Stripe retries later.
  500  a dependency failed part-way. Stripe retries, and the retry finishes the work.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass

from orders import signature
from orders.store import CLAIMED, DONE, Store

log = logging.getLogger()
log.setLevel(logging.INFO)

# Event type -> order status. checkout.session.completed is decided by its
# payment_status, because a delayed payment method completes checkout unpaid.
ASYNC_STATUS = {
    "checkout.session.async_payment_succeeded": "paid",
    "checkout.session.async_payment_failed": "payment_failed",
}
HANDLED_TYPES = {"checkout.session.completed", *ASYNC_STATUS}


class Unprocessable(Exception):
    """A correctly signed event that this handler cannot turn into an order."""


@dataclass
class Deps:
    store: Store
    sqs: object
    queue_url: str
    dead_letter_url: str
    secret: str
    now: Callable[[], int]


def _response(code: int, **body) -> dict:
    return {
        "statusCode": code,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }


# Stripe ids are far shorter than this. The bound keeps a key that DynamoDB
# would refuse (2048 bytes) from turning into a 500 that Stripe retries for days.
MAX_ID_LENGTH = 255
# DynamoDB numbers hold 38 digits; no real amount in minor units comes close.
MAX_AMOUNT = 10**15


class BadRequest(Exception):
    """The request body cannot be read at all."""


def _raw_body(event: dict) -> bytes:
    body = event.get("body") or ""
    if not isinstance(body, str):
        raise BadRequest("body is not a string")
    if event.get("isBase64Encoded"):
        try:
            return base64.b64decode(body, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise BadRequest("body is not valid base64") from exc
    return body.encode()


def _valid_id(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= MAX_ID_LENGTH


def _order_fields(stripe_event: dict) -> tuple[str, str, int, str]:
    """Return (order id, target status, amount, currency) or raise Unprocessable."""
    data = stripe_event.get("data")
    session = data.get("object") if isinstance(data, dict) else None
    if not isinstance(session, dict) or session.get("object") != "checkout.session":
        raise Unprocessable("data.object is not a checkout session")
    order_id = session.get("id")
    if not _valid_id(order_id):
        raise Unprocessable("checkout session has no usable id")
    amount = session.get("amount_total")
    currency = session.get("currency")
    if not isinstance(amount, int) or isinstance(amount, bool) or not 0 <= amount <= MAX_AMOUNT:
        raise Unprocessable("amount_total is missing, not a whole number or out of range")
    if not isinstance(currency, str) or not 0 < len(currency) <= MAX_ID_LENGTH:
        raise Unprocessable("currency is missing")

    event_type = stripe_event["type"]
    if event_type in ASYNC_STATUS:
        return order_id, ASYNC_STATUS[event_type], amount, currency
    payment_status = session.get("payment_status")
    if payment_status in ("paid", "no_payment_required"):
        return order_id, "paid", amount, currency
    if payment_status == "unpaid":
        return order_id, "awaiting_payment", amount, currency
    raise Unprocessable(f"unknown payment_status {payment_status!r}")


def _park(deps: Deps, stripe_event: dict, reason: str) -> None:
    """Put an event we cannot process where a person will find it."""
    deps.sqs.send_message(
        QueueUrl=deps.dead_letter_url,
        MessageBody=json.dumps(
            {
                "source": "webhook",
                "reason": reason,
                "event_id": stripe_event.get("id"),
                "event_type": stripe_event.get("type"),
                "event": stripe_event,
            }
        ),
    )


def handle(event: dict, deps: Deps) -> dict:
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    now = deps.now()
    try:
        payload = _raw_body(event)
        signature.verify(payload, headers.get("stripe-signature"), deps.secret, now)
    except (BadRequest, signature.SignatureError) as exc:
        log.warning("rejected: %s", exc)
        return _response(400, error="invalid signature")

    try:
        stripe_event = json.loads(payload)
        event_id = stripe_event["id"]
        event_type = stripe_event["type"]
        if not _valid_id(event_id) or not isinstance(event_type, str):
            raise TypeError("id and type must be strings, and id a usable key")
    except (ValueError, KeyError, TypeError):
        return _response(400, error="not a Stripe event")

    if event_type not in HANDLED_TYPES:
        return _response(200, status="ignored", event_type=event_type)

    try:
        claim = deps.store.claim_event(event_id, now)
    except Exception:
        log.exception("could not claim %s", event_id)
        return _response(500, error="temporary failure, retry")
    if claim == DONE:
        return _response(200, status="duplicate", event_id=event_id)
    if claim != CLAIMED:
        return _response(409, status="in_flight", event_id=event_id)

    order_id = None
    holds_enqueue = False
    try:
        try:
            order_id, status, amount, currency = _order_fields(stripe_event)
        except Unprocessable as exc:
            _park(deps, stripe_event, str(exc))
            deps.store.finish_event(event_id, "parked", now)
            log.error("parked %s: %s", event_id, exc)
            return _response(200, status="parked", event_id=event_id)

        moved = deps.store.apply_status(order_id, status, event_id, now, amount, currency)

        # Run for every "paid" event, including one that did not move the
        # order: that is what repairs an order whose first attempt died
        # between the status write and the queue send.
        queued = False
        if status == "paid" and deps.store.claim_enqueue(order_id, now):
            holds_enqueue = True
            sent = deps.sqs.send_message(
                QueueUrl=deps.queue_url,
                MessageBody=json.dumps(
                    {
                        "order_id": order_id,
                        "amount_total": amount,
                        "currency": currency,
                        "event_id": event_id,
                    }
                ),
            )
            # The message is out. If this write fails, the retry sends a second
            # copy and records it; the consumer absorbs the repeat (at-least-once).
            deps.store.mark_queued(order_id, sent["MessageId"], now)
            holds_enqueue = False
            queued = True

        outcome = "applied" if moved else "stale"
        deps.store.finish_event(event_id, outcome, now)
        log.info("event %s order %s: %s, queued=%s", event_id, order_id, outcome, queued)
        return _response(200, status=outcome, order_id=order_id, queued=queued)
    except Exception:
        log.exception("failed on %s; releasing the claim so the retry can finish", event_id)
        try:
            if holds_enqueue:
                deps.store.release_enqueue(order_id)
            deps.store.release_event(event_id)
        except Exception:
            log.exception("release failed; the lease will expire on its own")
        return _response(500, error="temporary failure, retry")


_deps: Deps | None = None


def _load_secret() -> str:
    """Read the signing secret from SSM Parameter Store, once per container."""
    import boto3

    name = os.environ["STRIPE_WEBHOOK_SECRET_PARAM"]
    return boto3.client("ssm").get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]


def handler(event, context):
    global _deps
    if _deps is None:
        import boto3

        _deps = Deps(
            store=Store(boto3.client("dynamodb"), os.environ["TABLE_NAME"]),
            sqs=boto3.client("sqs"),
            queue_url=os.environ["ORDERS_QUEUE_URL"],
            dead_letter_url=os.environ["DEAD_LETTER_QUEUE_URL"],
            secret=_load_secret(),
            now=lambda: int(time.time()),
        )
    return handle(event, _deps)
