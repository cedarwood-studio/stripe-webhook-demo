"""DynamoDB access. One table, two kinds of item:

EVENT#<stripe event id>     one per webhook event, so a redelivered event is a no-op
ORDER#<checkout session id> one per order, so two different events cannot make two orders

Every write that decides something is a conditional write, so the decision is
made by DynamoDB and holds when two Lambda invocations race.
"""

from __future__ import annotations

from botocore.exceptions import ClientError

EVENT_TTL_SECONDS = 30 * 24 * 3600  # Stripe retries for up to three days
LEASE_SECONDS = 60

# An order only ever moves up this ladder. An event that would move it down
# or sideways arrived late or twice and changes nothing.
STATUS_RANK = {"awaiting_payment": 1, "payment_failed": 2, "paid": 3}

CLAIMED = "claimed"
DONE = "done"
IN_FLIGHT = "in_flight"


def _conditional_failed(exc: ClientError) -> bool:
    return exc.response["Error"]["Code"] == "ConditionalCheckFailedException"


class Store:
    def __init__(self, dynamodb, table: str):
        self.db = dynamodb
        self.table = table

    # -- events ------------------------------------------------------------

    def claim_event(self, event_id: str, now: int) -> str:
        """Take the event for processing. Returns CLAIMED, DONE or IN_FLIGHT.

        A claim is a lease: if the invocation that holds it dies, the next
        delivery after the lease runs out takes over and finishes the work.
        """
        try:
            self.db.put_item(
                TableName=self.table,
                Item={
                    "pk": {"S": f"EVENT#{event_id}"},
                    "state": {"S": "processing"},
                    "lease_until": {"N": str(now + LEASE_SECONDS)},
                    "expires_at": {"N": str(now + EVENT_TTL_SECONDS)},
                },
                ConditionExpression=(
                    "attribute_not_exists(pk) OR (#state = :processing AND lease_until < :now)"
                ),
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={
                    ":processing": {"S": "processing"},
                    ":now": {"N": str(now)},
                },
            )
            return CLAIMED
        except ClientError as exc:
            if not _conditional_failed(exc):
                raise
        item = self.db.get_item(
            TableName=self.table, Key={"pk": {"S": f"EVENT#{event_id}"}}, ConsistentRead=True
        ).get("Item")
        if item and item["state"]["S"] == "done":
            return DONE
        return IN_FLIGHT

    def finish_event(self, event_id: str, outcome: str, now: int) -> None:
        self.db.update_item(
            TableName=self.table,
            Key={"pk": {"S": f"EVENT#{event_id}"}},
            UpdateExpression="SET #state = :done, outcome = :outcome, finished_at = :now "
            "REMOVE lease_until",
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={
                ":done": {"S": "done"},
                ":outcome": {"S": outcome},
                ":now": {"N": str(now)},
            },
        )

    def release_event(self, event_id: str) -> None:
        """Give the claim back after a failure so the next delivery can retry at once."""
        try:
            self.db.delete_item(
                TableName=self.table,
                Key={"pk": {"S": f"EVENT#{event_id}"}},
                ConditionExpression="#state = :processing",
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={":processing": {"S": "processing"}},
            )
        except ClientError as exc:
            if not _conditional_failed(exc):
                raise

    # -- orders ------------------------------------------------------------

    def apply_status(
        self, order_id: str, status: str, event_id: str, now: int, amount_total: int, currency: str
    ) -> bool:
        """Move the order to `status` if that is a step up. False if it is not."""
        rank = STATUS_RANK[status]
        try:
            self.db.update_item(
                TableName=self.table,
                Key={"pk": {"S": f"ORDER#{order_id}"}},
                UpdateExpression=(
                    "SET #status = :status, status_rank = :rank, amount_total = :amount, "
                    "currency = :currency, last_event_id = :event, updated_at = :now, "
                    "created_at = if_not_exists(created_at, :now)"
                ),
                ConditionExpression="attribute_not_exists(pk) OR status_rank < :rank",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":status": {"S": status},
                    ":rank": {"N": str(rank)},
                    ":amount": {"N": str(amount_total)},
                    ":currency": {"S": currency},
                    ":event": {"S": event_id},
                    ":now": {"N": str(now)},
                },
            )
            return True
        except ClientError as exc:
            if _conditional_failed(exc):
                return False
            raise

    def claim_enqueue(self, order_id: str, now: int) -> bool:
        """True for exactly one caller per paid order that is not yet queued."""
        try:
            self.db.update_item(
                TableName=self.table,
                Key={"pk": {"S": f"ORDER#{order_id}"}},
                UpdateExpression="SET enqueue_lease_until = :until",
                ConditionExpression=(
                    "#status = :paid AND attribute_not_exists(queued_at) AND "
                    "(attribute_not_exists(enqueue_lease_until) OR enqueue_lease_until < :now)"
                ),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":paid": {"S": "paid"},
                    ":until": {"N": str(now + LEASE_SECONDS)},
                    ":now": {"N": str(now)},
                },
            )
            return True
        except ClientError as exc:
            if _conditional_failed(exc):
                return False
            raise

    def release_enqueue(self, order_id: str) -> None:
        try:
            self.db.update_item(
                TableName=self.table,
                Key={"pk": {"S": f"ORDER#{order_id}"}},
                UpdateExpression="REMOVE enqueue_lease_until",
                ConditionExpression="attribute_exists(pk) AND attribute_not_exists(queued_at)",
            )
        except ClientError as exc:
            if not _conditional_failed(exc):
                raise

    def mark_queued(self, order_id: str, message_id: str, now: int) -> None:
        self.db.update_item(
            TableName=self.table,
            Key={"pk": {"S": f"ORDER#{order_id}"}},
            UpdateExpression="SET queued_at = :now, queue_message_id = :mid "
            "REMOVE enqueue_lease_until",
            ExpressionAttributeValues={":now": {"N": str(now)}, ":mid": {"S": message_id}},
        )

    def get_order(self, order_id: str) -> dict | None:
        return self.db.get_item(
            TableName=self.table, Key={"pk": {"S": f"ORDER#{order_id}"}}, ConsistentRead=True
        ).get("Item")

    # -- consumer ----------------------------------------------------------

    def record_delivery(self, order_id: str, now: int) -> str:
        """Count a delivery of the order message. Returns "first", "repeat" or "unknown".

        "first" is returned once per order, whatever SQS redelivers, so the
        step that costs money (a print job) is safe to hang on it.
        """
        key = {"pk": {"S": f"ORDER#{order_id}"}}
        try:
            self.db.update_item(
                TableName=self.table,
                Key=key,
                UpdateExpression="SET submitted_at = :now, fulfillment = :submitted "
                "ADD deliveries :one",
                ConditionExpression="#status = :paid AND attribute_not_exists(submitted_at)",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":paid": {"S": "paid"},
                    ":submitted": {"S": "submitted"},
                    ":now": {"N": str(now)},
                    ":one": {"N": "1"},
                },
            )
            return "first"
        except ClientError as exc:
            if not _conditional_failed(exc):
                raise
        try:
            self.db.update_item(
                TableName=self.table,
                Key=key,
                UpdateExpression="ADD deliveries :one",
                ConditionExpression="#status = :paid AND attribute_exists(submitted_at)",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":paid": {"S": "paid"}, ":one": {"N": "1"}},
            )
            return "repeat"
        except ClientError as exc:
            if _conditional_failed(exc):
                return "unknown"
            raise
