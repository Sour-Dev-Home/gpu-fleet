# 0002. Pull-based broker; workers never hold cloud keys

**Status:** Accepted (maintainer, 2026-11-24, Metaphor #1025)
**Date:** 2026-11-24

## Context
Workers run on very different backends:
- the local RX 9070
- Kaggle and Colab notebooks
- Cloud Run Jobs
- Kubernetes
- homelab VMs
- later, volunteers

Most of them sit behind NAT and can't accept incoming connections. Many of them can't be trusted with
cloud credentials. The budget is $0 for compute.

## Decision
- A small broker API (Cloud Run, scales to zero) gives job **leases**, accepts **heartbeats** and
  **completions**, and hands out **presigned S3 URLs** for inputs and results.
- Workers **pull** over HTTPS and never hold AWS or Google Cloud keys.
- Cloud Run reaches AWS through workload identity federation (OIDC), never through stored keys.
- Kubernetes is a backend, not the control layer.
- Copyrighted footage only goes to the maintainer's own backends; volunteers get text-only or open jobs.

## Consequences
- Every backend works the same way: outbound HTTPS only.
- The broker is the single place for scheduling, trust rules and auditing (every result records its worker).
- The broker becomes a dependency every worker needs. It must stay small, stateless per request (state lives in
  its store) and cheap when idle; see [0008](0008-short-polling-with-backoff.md).

## Alternatives considered
- **Workers read SQS directly:** every worker would need AWS credentials. Rejected for notebooks and volunteers.
- **Push to workers:** unreachable behind NAT.
