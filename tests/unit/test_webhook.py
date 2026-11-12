"""The handler against in-memory DynamoDB and SQS (moto). Each test name is the claim."""

from __future__ import annotations

from botocore.exceptions import ClientError

from orders import signature
from tests.events import TEST_SECRET, checkout_event, signed

PAID = "checkout.session.completed"
ASYNC_OK = "checkout.session.async_payment_succeeded"
ASYNC_FAILED = "checkout.session.async_payment_failed"


def status_of(h, session_id):
    return h.order(session_id)["status"]["S"]


# -- signature at the HTTP boundary -------------------------------------------


def test_unsigned_request_is_rejected_and_writes_nothing(h):
    body, _ = signed(checkout_event("evt_1", "cs_test_1"), h.clock())
    assert h.post(body, None)["statusCode"] == 400
    assert h.item_count() == 0
    assert h.queued() == []


def test_request_signed_with_another_secret_is_rejected(h):
    body, header = signed(checkout_event("evt_1", "cs_test_1"), h.clock(), secret="whsec_other")
    assert h.post(body, header)["statusCode"] == 400
    assert h.item_count() == 0
    assert h.queued() == []


def test_body_changed_after_signing_is_rejected(h):
    body, header = signed(checkout_event("evt_1", "cs_test_1", amount_total=4900), h.clock())
    assert h.post(body.replace(b"4900", b"1"), header)["statusCode"] == 400
    assert h.item_count() == 0


def test_captured_request_replayed_after_tolerance_is_rejected(h):
    body, header = signed(checkout_event("evt_1", "cs_test_1"), h.clock())
    h.clock.advance(301)
    assert h.post(body, header)["statusCode"] == 400
    assert h.item_count() == 0


def test_base64_encoded_body_is_verified_on_the_decoded_bytes(h):
    body, header = signed(checkout_event("evt_1", "cs_test_1"), h.clock())
    assert h.post(body, header, base64_encoded=True)["statusCode"] == 200
    assert status_of(h, "cs_test_1") == "paid"


def test_signed_body_that_is_not_an_event_is_rejected(h):
    for raw in (b"not json", b"[]", b'{"id": 5, "type": "x"}', b'{"type": "x"}'):
        body, header = signed(raw, h.clock())
        assert h.post(body, header)["statusCode"] == 400
    assert h.item_count() == 0


# -- the ordinary path --------------------------------------------------------


def test_paid_checkout_creates_one_order_and_one_message(h):
    r = h.deliver(checkout_event("evt_1", "cs_test_1"))
    assert r["statusCode"] == 200
    assert r["json"] == {"status": "applied", "order_id": "cs_test_1", "queued": True}
    order = h.order("cs_test_1")
    assert order["status"]["S"] == "paid"
    assert order["amount_total"]["N"] == "4900"
    messages = h.queued()
    assert [m["order_id"] for m in messages] == ["cs_test_1"]
    assert "customer" not in str(messages)  # the message carries ids and amounts only


def test_event_type_we_do_not_handle_is_acknowledged_and_writes_nothing(h):
    event = checkout_event("evt_1", "cs_test_1", event_type="invoice.paid")
    r = h.deliver(event)
    assert (r["statusCode"], r["json"]["status"]) == (200, "ignored")
    assert h.item_count() == 0


def test_event_record_carries_a_ttl(h):
    h.deliver(checkout_event("evt_1", "cs_test_1"))
    item = h.db.get_item(TableName=h.store.table, Key={"pk": {"S": "EVENT#evt_1"}})["Item"]
    assert int(item["expires_at"]["N"]) == h.clock() + 30 * 24 * 3600
    assert item["state"]["S"] == "done"


# -- duplicates ---------------------------------------------------------------


def test_same_event_delivered_five_times_makes_one_order_and_one_message(h):
    event = checkout_event("evt_1", "cs_test_1")
    statuses = [h.deliver(event)["json"]["status"] for _ in range(5)]
    assert statuses == ["applied"] + ["duplicate"] * 4
    assert len(h.queued()) == 1


def test_two_different_events_for_one_checkout_make_one_message(h):
    # Stripe can send both of these for one payment. Deduplicating on the
    # event id alone would queue the order twice.
    h.deliver(checkout_event("evt_1", "cs_test_1", PAID))
    r = h.deliver(checkout_event("evt_2", "cs_test_1", ASYNC_OK))
    assert r["json"] == {"status": "stale", "order_id": "cs_test_1", "queued": False}
    assert len(h.queued()) == 1


def test_two_checkouts_stay_separate(h):
    h.deliver(checkout_event("evt_1", "cs_test_1"))
    h.deliver(checkout_event("evt_2", "cs_test_2"))
    assert sorted(m["order_id"] for m in h.queued()) == ["cs_test_1", "cs_test_2"]


# -- out of order -------------------------------------------------------------


def test_unpaid_checkout_waits_then_queues_when_payment_succeeds(h):
    h.deliver(checkout_event("evt_1", "cs_test_1", PAID, payment_status="unpaid"))
    assert status_of(h, "cs_test_1") == "awaiting_payment"
    assert h.queued() == []
    h.deliver(checkout_event("evt_2", "cs_test_1", ASYNC_OK))
    assert status_of(h, "cs_test_1") == "paid"
    assert len(h.queued()) == 1


def test_payment_succeeded_arriving_before_checkout_completed_is_not_undone(h):
    h.deliver(checkout_event("evt_2", "cs_test_1", ASYNC_OK))
    r = h.deliver(checkout_event("evt_1", "cs_test_1", PAID, payment_status="unpaid"))
    assert r["json"]["status"] == "stale"
    assert status_of(h, "cs_test_1") == "paid"
    assert len(h.queued()) == 1


def test_payment_failed_arriving_before_checkout_completed_is_not_undone(h):
    h.deliver(checkout_event("evt_2", "cs_test_1", ASYNC_FAILED))
    h.deliver(checkout_event("evt_1", "cs_test_1", PAID, payment_status="unpaid"))
    assert status_of(h, "cs_test_1") == "payment_failed"
    assert h.queued() == []


def test_late_duplicate_of_an_early_event_does_not_touch_a_paid_order(h):
    unpaid = checkout_event("evt_1", "cs_test_1", PAID, payment_status="unpaid")
    h.deliver(unpaid)
    h.deliver(checkout_event("evt_2", "cs_test_1", ASYNC_OK))
    before = h.order("cs_test_1")
    h.clock.advance(3600)
    assert h.deliver(unpaid)["json"]["status"] == "duplicate"
    assert h.order("cs_test_1") == before
    assert len(h.queued()) == 1


# -- partial failures ---------------------------------------------------------


class FailingSend:
    """SQS client whose send_message fails a set number of times."""

    def __init__(self, real, failures=1):
        self.real, self.failures = real, failures

    def send_message(self, **kwargs):
        if self.failures:
            self.failures -= 1
            raise ClientError({"Error": {"Code": "ServiceUnavailable", "Message": "x"}}, "Send")
        return self.real.send_message(**kwargs)


def test_queue_failure_returns_500_and_the_retry_queues_the_order(h):
    # The bug this guards against: the event is recorded as seen, the queue
    # send fails, and the retry is skipped as a duplicate. The customer has
    # paid and the order never reaches fulfillment.
    h.deps.sqs = FailingSend(h.sqs)
    event = checkout_event("evt_1", "cs_test_1")
    assert h.deliver(event)["statusCode"] == 500
    assert status_of(h, "cs_test_1") == "paid"
    assert h.queued() == []
    r = h.deliver(event)  # Stripe's retry, same event id
    assert (r["statusCode"], r["json"]["queued"]) == (200, True)
    assert len(h.queued()) == 1


def test_invocation_that_died_mid_event_is_finished_by_a_later_delivery(h):
    # Simulate a Lambda that was killed after writing the order and before
    # the queue send: its claim on the event is still held.
    event = checkout_event("evt_1", "cs_test_1")
    assert h.store.claim_event("evt_1", h.clock()) == "claimed"
    h.store.apply_status("cs_test_1", "paid", "evt_1", h.clock(), 4900, "usd")

    r = h.deliver(event)  # while the dead invocation's lease is still live
    assert (r["statusCode"], r["json"]["status"]) == (409, "in_flight")
    assert h.queued() == []

    h.clock.advance(61)  # lease expired
    r = h.deliver(event)
    assert (r["statusCode"], r["json"]["queued"]) == (200, True)
    assert len(h.queued()) == 1


def test_a_different_event_repairs_an_order_that_was_paid_but_never_queued(h):
    h.store.claim_event("evt_1", h.clock())
    h.store.apply_status("cs_test_1", "paid", "evt_1", h.clock(), 4900, "usd")
    r = h.deliver(checkout_event("evt_2", "cs_test_1", ASYNC_OK))
    assert r["json"] == {"status": "stale", "order_id": "cs_test_1", "queued": True}
    assert len(h.queued()) == 1


def test_failure_after_the_send_can_duplicate_the_message_and_the_consumer_absorbs_it(h):
    # The one window this design leaves open: the message is sent and the
    # write that records it fails. The retry sends a second copy. Delivery to
    # the queue is at-least-once, and the consumer runs its step once.
    from orders.consumer import process

    real_mark = h.store.mark_queued
    calls = {"n": 0}

    def flaky_mark(*args):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ClientError({"Error": {"Code": "InternalServerError", "Message": "x"}}, "Update")
        return real_mark(*args)

    h.store.mark_queued = flaky_mark
    event = checkout_event("evt_1", "cs_test_1")
    assert h.deliver(event)["statusCode"] == 500
    assert h.deliver(event)["statusCode"] == 200
    messages = h.queued()
    assert [m["order_id"] for m in messages] == ["cs_test_1", "cs_test_1"]

    import json

    results = [process({"body": json.dumps(m)}, h.store, h.clock()) for m in messages]
    assert results == ["first", "repeat"]


def test_database_failure_returns_500_so_stripe_retries(h):
    class Down:
        def __getattr__(self, name):
            def call(**kwargs):
                raise ClientError({"Error": {"Code": "InternalServerError", "Message": "x"}}, name)

            return call

    h.store.db = Down()
    assert h.deliver(checkout_event("evt_1", "cs_test_1"))["statusCode"] == 500
    assert h.queued() == []


# -- dead-letter path ---------------------------------------------------------


def test_signed_event_we_cannot_process_is_parked_once_with_its_reason(h):
    event = checkout_event("evt_1", "cs_test_1", amount_total=None)
    r = h.deliver(event)
    assert (r["statusCode"], r["json"]["status"]) == (200, "parked")
    assert h.deliver(event)["json"]["status"] == "duplicate"
    parked = h.dead_letters()
    assert len(parked) == 1
    assert parked[0]["event_id"] == "evt_1"
    assert "amount_total" in parked[0]["reason"]
    assert parked[0]["event"] == event
    assert h.order("cs_test_1") is None
    assert h.queued() == []


def test_unknown_payment_status_is_parked_not_guessed(h):
    r = h.deliver(checkout_event("evt_1", "cs_test_1", payment_status="something_new"))
    assert r["json"]["status"] == "parked"
    assert h.order("cs_test_1") is None


def test_secret_is_never_echoed(h):
    body, _ = signed(checkout_event("evt_1", "cs_test_1"), h.clock())
    r = h.post(body, signature.sign(body, "whsec_wrong", h.clock()))
    assert TEST_SECRET not in r["body"]
