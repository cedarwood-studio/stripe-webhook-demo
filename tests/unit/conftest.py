from __future__ import annotations

import json

import boto3
import pytest
from moto import mock_aws

from orders.store import Store
from orders.webhook import Deps, handle
from tests.events import TEST_SECRET, http_api_event, signed

TABLE = "orders-test"


class Clock:
    def __init__(self, start: int = 1_800_000_000):
        self.t = start

    def __call__(self) -> int:
        return self.t

    def advance(self, seconds: int) -> None:
        self.t += seconds


class Harness:
    """The handler wired to in-memory AWS fakes, with helpers the tests read well with."""

    def __init__(self):
        self.clock = Clock()
        self.db = boto3.client("dynamodb", region_name="us-east-1")
        self.sqs = boto3.client("sqs", region_name="us-east-1")
        self.db.create_table(
            TableName=TABLE,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        )
        self.dead_letter_url = self.sqs.create_queue(QueueName="orders-dlq")["QueueUrl"]
        self.queue_url = self.sqs.create_queue(QueueName="orders")["QueueUrl"]
        self.store = Store(self.db, TABLE)
        self.deps = Deps(
            store=self.store,
            sqs=self.sqs,
            queue_url=self.queue_url,
            dead_letter_url=self.dead_letter_url,
            secret=TEST_SECRET,
            now=self.clock,
        )

    def deliver(self, event: dict | bytes, **kwargs) -> dict:
        body, header = signed(event, self.clock())
        return self.post(body, header, **kwargs)

    def post(self, body: bytes, header: str | None, base64_encoded: bool = False) -> dict:
        response = handle(http_api_event(body, header, base64_encoded), self.deps)
        response["json"] = json.loads(response["body"])
        return response

    def _drain(self, url: str) -> list[dict]:
        out = []
        while True:
            batch = self.sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=10).get(
                "Messages", []
            )
            if not batch:
                return out
            for m in batch:
                out.append(json.loads(m["Body"]))
                self.sqs.delete_message(QueueUrl=url, ReceiptHandle=m["ReceiptHandle"])

    def queued(self) -> list[dict]:
        return self._drain(self.queue_url)

    def dead_letters(self) -> list[dict]:
        return self._drain(self.dead_letter_url)

    def order(self, session_id: str) -> dict | None:
        return self.store.get_order(session_id)

    def item_count(self) -> int:
        return self.db.scan(TableName=TABLE, Select="COUNT")["Count"]


@pytest.fixture
def h(monkeypatch):
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, "testing")
    for name in (
        "AWS_ENDPOINT_URL",
        "AWS_PROFILE",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_CONFIG_FILE",
        "AWS_CA_BUNDLE",
    ):
        monkeypatch.delenv(name, raising=False)
    with mock_aws():
        yield Harness()
