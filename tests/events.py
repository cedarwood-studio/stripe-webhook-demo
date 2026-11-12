"""Synthetic Stripe events. Invented ids, a made-up signing secret, no Stripe account."""

from __future__ import annotations

import json

from orders import signature

TEST_SECRET = "whsec_test_not_a_real_secret"  # noqa: S105


def checkout_event(
    event_id: str,
    session_id: str,
    event_type: str = "checkout.session.completed",
    payment_status: str = "paid",
    amount_total: int | None = 4900,
    created: int = 1_800_000_000,
) -> dict:
    session = {
        "id": session_id,
        "object": "checkout.session",
        "mode": "payment",
        "payment_status": payment_status,
        "amount_total": amount_total,
        "currency": "usd",
        "metadata": {"synthetic": "true"},
    }
    return {
        "id": event_id,
        "object": "event",
        "api_version": "2024-06-20",
        "created": created,
        "livemode": False,
        "type": event_type,
        "data": {"object": session},
    }


def signed(event: dict | bytes, timestamp: int, secret: str = TEST_SECRET) -> tuple[bytes, str]:
    """Return (raw body, Stripe-Signature header) for an event."""
    body = event if isinstance(event, bytes) else json.dumps(event).encode()
    return body, signature.sign(body, secret, timestamp)


def http_api_event(body: bytes, header: str | None, base64_encoded: bool = False) -> dict:
    """The shape API Gateway HTTP API (payload format 2.0) hands to Lambda."""
    import base64

    headers = {"content-type": "application/json"}
    if header is not None:
        headers["stripe-signature"] = header
    return {
        "version": "2.0",
        "routeKey": "POST /webhooks/stripe",
        "rawPath": "/webhooks/stripe",
        "headers": headers,
        "body": base64.b64encode(body).decode() if base64_encoded else body.decode(),
        "isBase64Encoded": base64_encoded,
    }
