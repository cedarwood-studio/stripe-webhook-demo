# stripe-webhook-demo

A small Stripe webhook handler in Python for AWS Lambda. It takes Stripe Checkout events, checks the signature, records the order once in DynamoDB however many times and in whatever order the events arrive, and hands each paid order to an SQS queue for the next step. Messages that can never succeed end up in a dead-letter queue.

This is a demonstration built by Cedarwood Studio for a job posting that asks for a review of a Stripe and print-on-demand order pipeline. It is not client work, it contains no client code or data, and it has never handled a real payment. Every event in the tests is invented, and the signing secret is a made-up string.

It has been tested on AWS Emulator, AWS's local emulator, and not in an AWS account. The section "What the emulator does not prove" says what that leaves open.

## What it does

```
Stripe --POST /webhooks/stripe--> HTTP API --> webhook Lambda --> DynamoDB (events, orders)
                                                     |
                                                     +--> SQS orders queue --> consumer Lambda
                                                     |          |
                                                     |          +-- after 3 failed receives
                                                     v          v
                                                   SQS dead-letter queue
```

- **Signature check** (`src/orders/signature.py`). HMAC-SHA256 over the timestamp and the raw request bytes, compared in constant time, with a five minute tolerance on the timestamp. A request that fails gets a 400 and nothing is read or written. Standard library only.
- **One event, one effect** (`src/orders/store.py`, `claim_event`). Each Stripe event id is claimed with a DynamoDB conditional write. A redelivered event finds the record and returns 200 without doing anything.
- **One checkout, one order** (`apply_status`, `claim_enqueue`). Stripe can send two different events for one payment, so the event id is not enough. The order is keyed by the Checkout session id, and a second conditional write lets exactly one caller queue it.
- **Out of order events** (`STATUS_RANK`). An order only moves up: awaiting payment, payment failed, paid. An event that would move it down or sideways arrived late and changes nothing.
- **Partial failure**. The event claim is a 60 second lease, not a permanent mark. If the queue send fails, the handler gives the claim back and returns 500, and Stripe's retry finishes the job. If the Lambda dies mid-event, a later delivery takes over once the lease runs out. Any paid event for an order that is paid but not yet queued will queue it.
- **Dead-letter path, two ways in**. A correctly signed event the handler cannot turn into an order (no amount, an unknown payment status) is put on the dead-letter queue with the reason and the full event, and Stripe gets a 200 so it stops retrying. A message the consumer cannot process is redelivered by SQS and moved to the same queue after three receives.
- **The consumer** (`src/orders/consumer.py`) is a stand-in for the step that would place a print job. It places nothing. It shows that a redelivered message does not run the step twice.

Event types handled: `checkout.session.completed`, `checkout.session.async_payment_succeeded`, `checkout.session.async_payment_failed`. Any other type gets a 200 and is ignored.

### Response codes

| Code | Meaning | What Stripe does |
| --- | --- | --- |
| 200 | Handled, a duplicate, ignored, or parked in the dead-letter queue | Stops |
| 400 | Signature missing, wrong or too old, or the body is not an event | Retries, and keeps failing |
| 409 | Another request is processing this same event right now | Retries later |
| 500 | DynamoDB or SQS failed part-way | Retries, and the retry finishes the work |

### The one known gap

If the message is sent to SQS and the write that records the send then fails, the retry sends a second copy. Delivery to the queue is at-least-once. The consumer is written for that: its first delivery of an order runs the step and every later one is counted and skipped. A unit test forces this case (`test_failure_after_the_send_can_duplicate_the_message_and_the_consumer_absorbs_it`).

## What was tested, and the results

All results below are from one run on 2026-11-12, on a 4-core arm64 Linux machine with 8 GB of memory.

### Unit tests: 49 passed

`pytest`, 49 tests in about 4 seconds. The handler runs in-process against moto's in-memory DynamoDB and SQS, with a clock the tests control. These also run in CI on every push.

- **Signature (17 tests):** a valid signature passes; a wrong secret, a changed body, a re-serialized body, a changed timestamp, a missing or malformed header (6 cases), a `v0`-only header, an old request, a timestamp from the future and an empty secret all fail; one valid signature among several passes; one known value computed with `openssl` outside Python.
- **Webhook handler (24 tests):** bad signatures get 400 and write nothing; a paid checkout makes one order and one message; the same event five times makes one message; two different events for one checkout make one message; four multi-event sequences end in the right state, two of them with the events arriving in the wrong order; a queue failure returns 500 and the retry queues the order; an invocation that died mid-event is finished by a later delivery; a database failure returns 500; unprocessable signed events are parked once with their reason.
- **Consumer (8 tests):** the first delivery runs the step and a redelivery does not; a message for a missing or unpaid order, or a malformed message (5 cases), raises so SQS will dead-letter it.

### End-to-end tests on AWS Emulator: 15 passed

`scripts/e2e.sh` starts the emulator, applies the Terraform in `infra/` (18 resources), sends signed events over HTTPS through the emulated HTTP API, reads the results back through the AWS API, destroys the stack and stops the emulator. Nothing is faked on the test's side.

| What was sent | Result |
| --- | --- |
| Unsigned request; wrong secret; body changed after signing; request signed ten minutes ago | 400 each, no order written |
| One paid checkout | One order, consumer ran once |
| An event type the handler does not handle | 200, nothing written |
| The same event five times in a row | First applied, four reported as duplicates, consumer ran once |
| The same event twenty times at once | Exactly one applied, consumer ran once |
| Two different events for one checkout, at once | One applied, one stale, consumer ran once |
| Unpaid checkout, then payment succeeded | Waited unqueued, then processed |
| Payment succeeded arriving before checkout completed | Stayed paid, consumer ran once |
| Payment failed arriving before checkout completed | Stayed failed, never queued |
| Signed event with no amount | 200, found in the dead-letter queue with its reason and the full event |
| A message for an order that does not exist, put on the orders queue | In the dead-letter queue 32 seconds later, after three failed receives at a 10 second visibility timeout |
| 50 orders, each event delivered three times, shuffled, ten requests at a time | 50 orders, each processed by the consumer exactly once |

Measured in that run:

- **Terraform apply:** 18 resources in 64 seconds. Destroy: 18 destroyed.
- **Whole test run:** 15 tests in 213 seconds, most of it waiting for queues to drain.
- **The burst:** 150 requests in 73 seconds, median 4.6 seconds per request and 6.4 seconds at the 95th percentile with ten in flight. A single request with nothing else running took about 0.35 seconds in an earlier manual check. The consumer finished all 50 orders 141 seconds after the first request. None of the 150 was answered 409.

These timings describe the emulator on a small machine. They say nothing about how fast this would be on AWS.

## What the emulator does not prove

AWS Emulator was version 1.0.336 (released 2026-11-12, commit `f515581`), run with telemetry off. AWS publishes a score file with each release; for this version it shows about six in ten of AWS's own fidelity checks passing overall, and about half for HTTP APIs. A pass here is evidence and not acceptance.

- **It has not run on AWS.** No AWS account was used at any point. The first real deployment may turn up differences.
- **The IAM policies are untested.** The Terraform gives each function a narrow policy, but AWS says not to use the emulator to test permissions, and it does not enforce them faithfully. Whether these policies are sufficient, or as tight as they look, is only shown on AWS.
- **Stripe has not called it.** Signatures are generated by the tests with the same documented scheme (`t=...,v1=...`, HMAC-SHA256). No Stripe account, Stripe CLI or Stripe library was involved, and the event payloads are hand-written to the shape of Stripe's Checkout session events, with only the fields the handler reads.
- **Lambda concurrency and cold starts are the emulator's.** The twenty-at-once test shows the conditional writes hold under the emulator's concurrency. The same writes are atomic on DynamoDB, but that run was not on DynamoDB.
- **Timeouts and crashes were simulated in unit tests only.** The end-to-end run did not kill a Lambda mid-request.
- **Encryption, point-in-time recovery, TTL expiry, log retention and API throttling** are set in the Terraform and were accepted by the emulator. None of them was exercised.
- **A second `terraform plan` was not checked for drift.**

## What is not here

A real pipeline needs more than this, and a review would look for it:

- Refunds and disputes (`charge.refunded`, `charge.dispute.created`), which arrive keyed by payment intent and need a lookup to find the order.
- Alarms: on dead-letter queue depth, on the age of the oldest message, on 5xx from the webhook.
- A tool to inspect and redrive the dead-letter queue. Parked events carry the full Stripe event, which can include a customer's email address, so that queue needs the same care as the orders table.
- A better home for the signing secret's value. Here Terraform writes it to SSM Parameter Store, so it also sits in the Terraform state.
- A sweep for orders that are paid and never queued, for the case where no retry ever comes.
- FIFO queues or a deduplication id, if the downstream step cannot be made safe to repeat.

## Run it

Unit tests and lint need Python 3.12 and nothing else:

```sh
make install
make lint test
```

The end-to-end run needs two more programs on the `PATH`, and no accounts:

- **AWS Emulator 1.0.336.** Install it from AWS's package as described in the [user guide](https://docs.aws.amazon.com/emulator/latest/userguide/getting-started-linux.html), or set `AWS_EMULATOR_BIN` to the binary. It releases almost daily; a newer version may behave differently.
- **Terraform 1.9.8** with AWS provider 5.100.0 (pinned in `infra/.terraform.lock.hcl`).

```sh
make e2e
```

The script always starts the emulator with `--telemetry off` (it is on by default), keeps the emulator's keys and certificate under `.emulator/` in this directory, and removes the stack when it ends. `EMULATOR_PORT` changes the port from 9297. `KEEP_STACK=1` skips the destroy.

CI (`.github/workflows/ci.yml`) runs ruff, the 49 unit tests, `terraform fmt -check` and `terraform validate` on every push. The end-to-end run is a local step and is not in CI.

## Layout

| Path | What it is |
| --- | --- |
| `src/orders/signature.py` | Stripe signature check and the signer the tests use |
| `src/orders/store.py` | Every DynamoDB read and write |
| `src/orders/webhook.py` | The webhook Lambda |
| `src/orders/consumer.py` | The queue consumer Lambda |
| `infra/` | Terraform: table, two queues, two functions and roles, HTTP API, the signing secret in SSM Parameter Store |
| `tests/events.py` | Synthetic Stripe events and signing |
| `tests/unit/` | Unit tests |
| `tests/e2e/` | End-to-end tests for the emulator |
| `scripts/e2e.sh` | Start, apply, test, destroy |

## License

No license is granted; this repository is published as a work sample.
