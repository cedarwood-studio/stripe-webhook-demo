"""Stripe webhook signature scheme (v1), standard library only.

Stripe signs "<timestamp>.<raw body>" with HMAC-SHA256 using the endpoint's
signing secret and sends "t=<timestamp>,v1=<hex>[,v1=<hex>...]" in the
Stripe-Signature header. Verification must use the raw request bytes: parsing
and re-serializing the JSON changes them and breaks the signature.
"""

from __future__ import annotations

import hashlib
import hmac

DEFAULT_TOLERANCE_SECONDS = 300


class SignatureError(Exception):
    """The request cannot be shown to come from Stripe."""


def compute(payload: bytes, secret: str, timestamp: int) -> str:
    signed = str(timestamp).encode() + b"." + payload
    return hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()


def sign(payload: bytes, secret: str, timestamp: int) -> str:
    """Build a Stripe-Signature header value. Used by the tests to make events."""
    return f"t={timestamp},v1={compute(payload, secret, timestamp)}"


def verify(
    payload: bytes,
    header: str | None,
    secret: str,
    now: int,
    tolerance: int = DEFAULT_TOLERANCE_SECONDS,
) -> None:
    """Raise SignatureError unless the header carries a valid, recent signature."""
    if not secret:
        raise SignatureError("no signing secret configured")
    if not header:
        raise SignatureError("missing Stripe-Signature header")

    timestamp: int | None = None
    candidates: list[str] = []
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            # Digits only. int() alone would also take "+1", " 1" and "1_0".
            if not (value.isascii() and value.isdigit() and len(value) <= 12):
                raise SignatureError("malformed timestamp")
            timestamp = int(value)
        elif key == "v1":
            candidates.append(value)
    if timestamp is None or not candidates:
        raise SignatureError("header has no timestamp or no v1 signature")

    expected = compute(payload, secret, timestamp).encode()
    # Compare every candidate without short-circuiting on the first mismatch.
    # Bytes, not str: compare_digest raises TypeError on a non-ASCII str, and
    # the header is attacker-controlled.
    matched = False
    for candidate in candidates:
        if hmac.compare_digest(expected, candidate.encode("utf-8", "replace")):
            matched = True
    if not matched:
        raise SignatureError("signature does not match")

    # Checked after the signature so an attacker learns nothing from the order.
    if abs(now - timestamp) > tolerance:
        raise SignatureError("timestamp outside tolerance")
