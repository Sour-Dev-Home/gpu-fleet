# 0008. Short polling with exponential backoff and jitter

**Status:** Proposed (awaiting the maintainer's sign-off; implementation planned for phase 1 step 3b)
**Date:** 2026-11-25

## Context
Workers must learn when there is work. Most of them can't be reached from outside ([0002](0002-pull-based-broker.md)).
The broker runs on Cloud Run, which bills for instance time while a request is open. Free tier: about 2M
requests and about 180k vCPU-seconds a month.

Napkin math for 10 idle workers, around the clock, over 30 days:

| Option | Load | Cost |
|---|---|---|
| Short poll every 1 s | 26M requests a month | ~13x the free requests |
| Long poll (requests held open) | An instance always busy: ~2.6M vCPU-seconds | ~14x the free compute; no scale-to-zero |
| Short poll, backoff capped at 60 s | ~430k requests; ~43k vCPU-seconds at 100 ms billing | $0 |

## Decision
- Workers short-poll `/lease`. On 204 or a transient error they wait with **exponential backoff and full
  jitter**: a random time between 0 and `min(cap, base * 2^n)`, with a cap of about 60 s. Any success resets it.
- A busy worker asks again immediately after finishing, so throughput while working is unaffected.
- Cloud capacity is scaled from the backlog count instead (KEDA in phase 7, Cloud Run Jobs in phase 6), so
  there are no idle cloud workers at all.

## Consequences
- Up to about 60 s before an idle worker notices new work. That's negligible for batch jobs that take hours.
- Jitter prevents synchronized retries (a thundering herd) after a broker restart.

## Alternatives considered
- **Long polling:** the right choice on SQS, which bills per request (exam: "reduce empty receives"), but the
  wrong one on Cloud Run, which bills open time.
- **Push, WebSockets or SSE:** workers behind NAT can't be reached, and persistent connections cost the same as
  long polling.
