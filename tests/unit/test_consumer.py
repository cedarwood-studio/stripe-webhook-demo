from __future__ import annotations

import json

import pytest

from orders.consumer import PoisonMessage, process
from tests.events import checkout_event


def record(order_id="cs_test_1"):
    return {"messageId": "m-1", "body": json.dumps({"order_id": order_id})}


def test_first_delivery_runs_the_step_and_a_redelivery_does_not(h):
    h.deliver(checkout_event("evt_1", "cs_test_1"))
    assert process(record(), h.store, h.clock()) == "first"
    submitted_at = h.order("cs_test_1")["submitted_at"]
    h.clock.advance(30)
    assert process(record(), h.store, h.clock()) == "repeat"
    order = h.order("cs_test_1")
    assert order["deliveries"]["N"] == "2"
    assert order["submitted_at"] == submitted_at


def test_message_for_an_order_that_does_not_exist_is_poison(h):
    with pytest.raises(PoisonMessage, match="does not exist or is not paid"):
        process(record("cs_test_missing"), h.store, h.clock())
    assert h.order("cs_test_missing") is None  # and it did not create one


def test_message_for_an_unpaid_order_is_poison(h):
    h.deliver(checkout_event("evt_1", "cs_test_1", payment_status="unpaid"))
    with pytest.raises(PoisonMessage):
        process(record(), h.store, h.clock())
    assert "submitted_at" not in h.order("cs_test_1")


@pytest.mark.parametrize("body", ["not json", "{}", '{"order_id": ""}', '{"order_id": 7}', "[]"])
def test_malformed_message_is_poison(h, body):
    with pytest.raises(PoisonMessage, match="no order_id"):
        process({"messageId": "m-1", "body": body}, h.store, h.clock())
