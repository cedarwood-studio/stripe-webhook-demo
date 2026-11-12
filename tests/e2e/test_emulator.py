"""End to end: signed events over HTTPS into the Terraform-built stack on AWS Emulator.

Nothing here is faked on our side. Requests go through the emulated HTTP API
to the webhook Lambda, which writes to the emulated DynamoDB table and sends
to the emulated SQS queue; the consumer Lambda is fed by the queue. The tests
read the results back through the AWS API.

Run with scripts/e2e.sh, which starts the emulator and applies the Terraform.
"""

from __future__ import annotations

import json
import os
import random
import ssl
import statistics
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

import boto3
import pytest

from tests.events import checkout_event, signed

pytestmark = pytest.mark.e2e

RUN = uuid.uuid4().hex[:8]
PAID = "checkout.session.completed"
ASYNC_OK = "checkout.session.async_payment_succeeded"
ASYNC_FAILED = "checkout.session.async_payment_failed"


@pytest.fixture(scope="session")
def stack():
    path = os.environ.get("E2E_OUTPUTS")
    if not path:
        pytest.skip("E2E_OUTPUTS is not set; run scripts/e2e.sh")
    with open(path) as f:
        out = {k: v["value"] for k, v in json.load(f).items()}
    out["url"] = out["api_endpoint"].rstrip("/") + "/webhooks/stripe"
    out["tls"] = ssl.create_default_context(cafile=os.environ["AWS_CA_BUNDLE"])
    out["db"] = boto3.client("dynamodb")
    out["sqs"] = boto3.client("sqs")
    return out


def ids(label: str) -> tuple[str, str]:
    return f"evt_test_{RUN}_{label}", f"cs_test_{RUN}_{label}"


def post(stack, body: bytes, header: str | None) -> tuple[int, dict]:
    headers = {"content-type": "application/json"}
    if header is not None:
        headers["Stripe-Signature"] = header
    request = urllib.request.Request(stack["url"], data=body, headers=headers, method="POST")  # noqa: S310
    try:
        with urllib.request.urlopen(request, context=stack["tls"], timeout=30) as r:  # noqa: S310
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def deliver(stack, event: dict) -> tuple[int, dict]:
    body, header = signed(event, int(time.time()))
    return post(stack, body, header)


def order(stack, session_id: str) -> dict | None:
    return (
        stack["db"]
        .get_item(
            TableName=stack["table_name"],
            Key={"pk": {"S": f"ORDER#{session_id}"}},
            ConsistentRead=True,
        )
        .get("Item")
    )


def wait_for(check, seconds: float = 30, every: float = 0.5):
    deadline = time.monotonic() + seconds
    while True:
        value = check()
        if value or time.monotonic() > deadline:
            return value
        time.sleep(every)


def wait_submitted(stack, session_id: str, seconds: float = 30) -> dict:
    item = wait_for(
        lambda: (o := order(stack, session_id)) and "submitted_at" in o and o, seconds=seconds
    )
    assert item, f"consumer never processed {session_id}"
    return item


def queue_is_idle(stack) -> bool:
    attrs = stack["sqs"].get_queue_attributes(
        QueueUrl=stack["orders_queue_url"],
        AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
    )["Attributes"]
    return all(int(v) == 0 for v in attrs.values())


def settle(stack) -> None:
    """Wait until nothing is queued or in flight, so a late second delivery would show."""
    assert wait_for(lambda: queue_is_idle(stack), seconds=60), "orders queue never drained"
    time.sleep(2)


def find_dead_letter(stack, match, seconds: float = 120) -> dict | None:
    """Look through the dead-letter queue for a message; delete it when found."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        received = stack["sqs"].receive_message(
            QueueUrl=stack["dead_letter_queue_url"],
            MaxNumberOfMessages=10,
            WaitTimeSeconds=2,
            VisibilityTimeout=1,
            AttributeNames=["ApproximateReceiveCount"],
        )
        for m in received.get("Messages", []):
            if match(m["Body"]):
                stack["sqs"].delete_message(
                    QueueUrl=stack["dead_letter_queue_url"], ReceiptHandle=m["ReceiptHandle"]
                )
                return m
        time.sleep(1)
    return None


# -- signature ----------------------------------------------------------------


def test_unsigned_request_gets_400_and_writes_nothing(stack):
    event_id, session_id = ids("unsigned")
    body, _ = signed(checkout_event(event_id, session_id), int(time.time()))
    assert post(stack, body, None)[0] == 400
    assert order(stack, session_id) is None


def test_wrong_secret_gets_400_and_writes_nothing(stack):
    event_id, session_id = ids("wrongsecret")
    body, header = signed(checkout_event(event_id, session_id), int(time.time()), "whsec_other")
    assert post(stack, body, header)[0] == 400
    assert order(stack, session_id) is None


def test_body_changed_after_signing_gets_400(stack):
    event_id, session_id = ids("tampered")
    body, header = signed(checkout_event(event_id, session_id), int(time.time()))
    assert post(stack, body.replace(b"4900", b"1"), header)[0] == 400
    assert order(stack, session_id) is None


def test_request_signed_ten_minutes_ago_gets_400(stack):
    event_id, session_id = ids("old")
    body, header = signed(checkout_event(event_id, session_id), int(time.time()) - 600)
    assert post(stack, body, header)[0] == 400
    assert order(stack, session_id) is None


# -- orders -------------------------------------------------------------------


def test_paid_checkout_becomes_one_order_processed_once(stack):
    event_id, session_id = ids("paid")
    code, body = deliver(stack, checkout_event(event_id, session_id))
    assert (code, body["status"], body["queued"]) == (200, "applied", True)
    item = wait_submitted(stack, session_id)
    assert item["status"]["S"] == "paid"
    settle(stack)
    assert order(stack, session_id)["deliveries"]["N"] == "1"


def test_event_type_we_do_not_handle_gets_200(stack):
    event_id, session_id = ids("ignored")
    code, body = deliver(stack, checkout_event(event_id, session_id, "invoice.paid"))
    assert (code, body["status"]) == (200, "ignored")
    assert order(stack, session_id) is None


def test_same_event_five_times_in_a_row_is_processed_once(stack):
    event_id, session_id = ids("dup")
    event = checkout_event(event_id, session_id)
    statuses = [deliver(stack, event)[1]["status"] for _ in range(5)]
    assert statuses == ["applied"] + ["duplicate"] * 4
    wait_submitted(stack, session_id)
    settle(stack)
    assert order(stack, session_id)["deliveries"]["N"] == "1"


def test_same_event_twenty_times_at_once_is_processed_once(stack):
    event_id, session_id = ids("race")
    event = checkout_event(event_id, session_id)
    with ThreadPoolExecutor(20) as pool:
        results = list(pool.map(lambda _: deliver(stack, event), range(20)))
    # 409 means "another request holds this event"; Stripe would retry it.
    assert {code for code, _ in results} <= {200, 409}
    assert sum(1 for _, b in results if b.get("status") == "applied") == 1
    assert deliver(stack, event)[1]["status"] == "duplicate"
    wait_submitted(stack, session_id)
    settle(stack)
    assert order(stack, session_id)["deliveries"]["N"] == "1"


def test_two_different_events_for_one_checkout_at_once_make_one_order(stack):
    (e1, session_id), (e2, _) = ids("pair"), ids("pair_b")
    events = [checkout_event(e1, session_id, PAID), checkout_event(e2, session_id, ASYNC_OK)]
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda e: deliver(stack, e), events))
    assert [code for code, _ in results] == [200, 200]
    assert sorted(b["status"] for _, b in results) == ["applied", "stale"]
    wait_submitted(stack, session_id)
    settle(stack)
    assert order(stack, session_id)["deliveries"]["N"] == "1"


def test_unpaid_checkout_waits_then_is_processed_when_payment_succeeds(stack):
    (e1, session_id), (e2, _) = ids("delayed"), ids("delayed_b")
    deliver(stack, checkout_event(e1, session_id, PAID, payment_status="unpaid"))
    settle(stack)
    item = order(stack, session_id)
    assert item["status"]["S"] == "awaiting_payment"
    assert "queued_at" not in item
    deliver(stack, checkout_event(e2, session_id, ASYNC_OK))
    assert wait_submitted(stack, session_id)["status"]["S"] == "paid"


def test_payment_succeeded_before_checkout_completed_stays_paid(stack):
    (e1, session_id), (e2, _) = ids("ooo"), ids("ooo_b")
    assert deliver(stack, checkout_event(e2, session_id, ASYNC_OK))[1]["status"] == "applied"
    late = checkout_event(e1, session_id, PAID, payment_status="unpaid")
    assert deliver(stack, late)[1]["status"] == "stale"
    wait_submitted(stack, session_id)
    settle(stack)
    item = order(stack, session_id)
    assert (item["status"]["S"], item["deliveries"]["N"]) == ("paid", "1")


def test_payment_failed_before_checkout_completed_stays_failed(stack):
    (e1, session_id), (e2, _) = ids("failed"), ids("failed_b")
    deliver(stack, checkout_event(e2, session_id, ASYNC_FAILED))
    deliver(stack, checkout_event(e1, session_id, PAID, payment_status="unpaid"))
    settle(stack)
    item = order(stack, session_id)
    assert item["status"]["S"] == "payment_failed"
    assert "queued_at" not in item and "submitted_at" not in item


# -- dead-letter path ---------------------------------------------------------


def test_signed_event_the_handler_cannot_process_lands_in_the_dead_letter_queue(stack):
    event_id, session_id = ids("parked")
    event = checkout_event(event_id, session_id, amount_total=None)
    code, body = deliver(stack, event)
    assert (code, body["status"]) == (200, "parked")
    assert deliver(stack, event)[1]["status"] == "duplicate"
    message = find_dead_letter(stack, lambda b: event_id in b)
    assert message, "parked event not found in the dead-letter queue"
    parked = json.loads(message["Body"])
    assert parked["source"] == "webhook" and "amount_total" in parked["reason"]
    assert parked["event"] == event
    assert order(stack, session_id) is None


def test_message_the_consumer_cannot_process_moves_to_the_dead_letter_queue(stack):
    # A message for an order that does not exist. The consumer raises, SQS
    # redelivers, and after max_receive_count (3) SQS moves it aside.
    _, session_id = ids("poison")
    stack["sqs"].send_message(
        QueueUrl=stack["orders_queue_url"], MessageBody=json.dumps({"order_id": session_id})
    )
    started = time.monotonic()
    message = find_dead_letter(stack, lambda b: session_id in b)
    assert message, "poison message never reached the dead-letter queue"
    print(f"\npoison message dead-lettered after {time.monotonic() - started:.0f}s")
    assert json.loads(message["Body"]) == {"order_id": session_id}
    assert order(stack, session_id) is None
    assert wait_for(lambda: queue_is_idle(stack), seconds=30)


# -- a burst ------------------------------------------------------------------


def test_fifty_orders_each_delivered_three_times_shuffled_make_fifty_orders(stack):
    n, copies = 50, 3
    events = [checkout_event(*ids(f"burst{i}")) for i in range(n)]
    deliveries = events * copies
    random.Random(RUN).shuffle(deliveries)  # noqa: S311

    def timed(event):
        t = time.monotonic()
        code, body = deliver(stack, event)
        return code, body, time.monotonic() - t

    started = time.monotonic()
    with ThreadPoolExecutor(10) as pool:
        results = list(pool.map(timed, deliveries))
    elapsed = time.monotonic() - started

    # A 409 is a retry signal. Do what Stripe would do and send those again.
    retried = 0
    for (code, _, _), event in zip(results, deliveries, strict=True):
        if code == 409:
            retried += 1
            assert deliver(stack, event)[0] in (200, 409)
    assert {code for code, _, _ in results} <= {200, 409}
    assert sum(1 for _, b, _ in results if b.get("status") == "applied") == n

    # The emulator's queue poller hands the consumer about one message every
    # two seconds, so the queue takes a couple of minutes to drain.
    assert wait_for(lambda: queue_is_idle(stack), seconds=300, every=2), "queue never drained"
    drained = time.monotonic() - started
    for i in range(n):
        wait_submitted(stack, ids(f"burst{i}")[1])
    settle(stack)
    counts = [order(stack, ids(f"burst{i}")[1])["deliveries"]["N"] for i in range(n)]
    assert counts == ["1"] * n

    times = sorted(t for _, _, t in results)
    print(
        f"\n{len(deliveries)} requests in {elapsed:.1f}s, 10 at a time: "
        f"median {statistics.median(times) * 1000:.0f} ms, "
        f"p95 {times[int(len(times) * 0.95)] * 1000:.0f} ms, "
        f"{retried} answered 409 and were sent again; "
        f"consumer finished all {n} orders {drained:.0f}s after the first request"
    )
