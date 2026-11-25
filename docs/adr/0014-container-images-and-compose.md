# 0014. Container images and local Compose; the model server stays outside the worker

**Status:** Accepted (maintainer, 2026-11-25, in a /grill-with-docs session: Q1-Q15, all as recommended)
**Date:** 2026-11-25

## Context
Phase 2 packages the **Worker** and the **Broker** as images and runs them together locally. The worker image is
what every later **Backend** runs (Cloud Run, Kubernetes, rented GPUs). Measured on 2026-11-25: native Ollama
0.40.17 on Windows serves `qwen3:8b` fully from the RX 9070 (`size_vram` equals the model size), and a container
reaches it at `host.docker.internal:11434`. Docker Desktop's GPU support is documented for NVIDIA only; Ollama's
ROCm image needs native-Linux `/dev/kfd`, which WSL2 lacks.

## Decision
**Images**
- The worker image holds no model runtime. **Runners** reach a **Model server** (Ollama) at `FLEET_MODEL_URL`.
  GPU specifics stay with the model server, which has official images per vendor; on Cloud Run GPU (phase 6)
  it runs as a sidecar.
- Both images: `python:3.11-slim` pinned by digest (the version CI tests), multi-stage with uv from the lockfile,
  non-root (uid 10001). Dependabot's `docker` ecosystem bumps digests with the 7-day cooldown.
- Footage, results, databases and secrets never enter an image or its build context (`.dockerignore`, tested).
- Phase 2 builds locally; CI builds without pushing. Pushing to a registry starts in phase 4.

**Compose**
- `broker`: one instance, SQLite on the `broker-data` volume, published only on `127.0.0.1:8000`.
- `worker`: 1 by default, `--scale worker=N`; `restart: unless-stopped`, `stop_grace_period: 30s`. A longer job
  killed on stop is retried after its **Lease** expires (ADR 0006).
- Tokens only as environment variables from the vault (`with-secrets.ps1 … -- docker compose up`), required with
  `${…:?}`. No `.env` or secrets files. The admin token goes to the broker only.
- Workers use plain HTTP to the broker on the private Compose network only (`FLEET_ALLOW_HTTP=1`).
- Jobs are added with `docker compose run --rm broker fleet-enqueue --demo N`.

**Worker and broker behaviour**
- **Background heartbeat:** while a runner runs, a thread renews the lease at 2/3 of its TTL, so runners may
  block (a queued model request, a model load). A refused renewal keeps the valid result (content-addressed, so
  the job's new holder finds it) but doesn't complete. Transient errors retry at the next poll; anything else
  stops the worker. `keepalive` still works for runners that call it.
- **`smoke` runner:** one fixed tiny prompt to the model server (temperature 0, seed 0, `use_mmap=false`,
  `num_ctx` 2048, `think` off), built only from what the **Result key** covers. A startup check exits with an
  actionable message if the model server or model is missing.
- **`GET /healthz`:** unauthenticated, 204, no body, for Compose, Cloud Run and Kubernetes probes.

## Consequences
- One small worker image serves every backend. Changing GPU vendor or model server never rebuilds it.
- The local GPU stays on the path that's measured to work (native Ollama), not an unsupported one.
- `docker inspect` shows container environment variables, tokens included, to anyone with Docker access on the PC:
  inside the threat model (docs/security.md), and replaced by Secret Manager in phase 4.
- With several workers, requests queue at the single model server. Background heartbeats keep the leases alive
  while they wait.

## Alternatives considered
- **Ollama inside the worker image:** 3–8 GB per GPU vendor, and no supported AMD GPU path in Docker Desktop.
- **distroless or alpine base:** distroless has no shell to debug with while learning; alpine's musl breaks many
  wheels.
- **Runner-driven keepalive only:** every runner would have to heartbeat correctly, and blocking calls can't.
- **A `.env` file or Compose `secrets:` files:** both write tokens to disk.
