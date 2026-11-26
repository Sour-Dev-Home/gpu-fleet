# gpu-fleet

A compute-agnostic GPU worker fleet: a small broker hands out leased jobs and presigned storage URLs, and the same
worker runs unchanged on a local GPU, free notebooks (Kaggle, Colab), Cloud Run Jobs and Kubernetes. First workload:
cutscene audio descriptions for the Metaphor: ReFantazio accessibility mod
([#1013](https://github.com/Sour-Dev-Home/metaphor-accessibility/issues/1013),
[#1025](https://github.com/Sour-Dev-Home/metaphor-accessibility/issues/1025)).

Built as a learning project, phase by phase: see [docs/phases.md](docs/phases.md).

## Run it locally

A PostgreSQL job store ([ADR 0020](docs/adr/0020-postgres-job-store.md)), a broker and a worker in Docker Compose
([ADR 0014](docs/adr/0014-container-images-and-compose.md)). The tokens and the database password
(`FLEET_DB_PASSWORD`, generate it once: docs/secrets.md)
come from the local `fleet` vault as environment variables for that one command (see
[docs/secrets.md](docs/secrets.md)). Compose refuses to run any command without them. In PowerShell:

```powershell
# the broker image installs with uv sync --frozen: check its lock is current first
uv lock --check --directory broker
# broker on 127.0.0.1:8000 and one echo worker (Ctrl+C stops them)
./scripts/with-secrets.ps1 -Names FLEET_WORKER_TOKEN,FLEET_ADMIN_TOKEN,FLEET_DB_PASSWORD -- docker compose up --build
# in a second terminal: add 20 demo jobs for the echo runner, then stop the stack (volumes are kept)
./scripts/with-secrets.ps1 -Names FLEET_WORKER_TOKEN,FLEET_ADMIN_TOKEN,FLEET_DB_PASSWORD -- docker compose run --rm broker fleet-enqueue --demo 20
./scripts/with-secrets.ps1 -Names FLEET_WORKER_TOKEN,FLEET_ADMIN_TOKEN,FLEET_DB_PASSWORD -- docker compose down
```

- Jobs must match the runner: `echo` implements `echo-v1` with model `echo` (`fleet-enqueue`'s defaults),
  `smoke` implements `smoke-v1` with the models the model server has. Each worker declares these when it
  leases (ADR 0015); a job no running worker can run stays queued. `--data-class` (default `open`) says who
  may see a job's input; the broker's `FLEET_WORKER_CLEARANCE` (required) says what the worker token may lease.
- The default `echo` runner needs no GPU. `$env:FLEET_RUNNER = 'smoke'` before `up` sends one tiny prompt per
  job to the model server: native Ollama on the host at `http://host.docker.internal:11434` (`FLEET_MODEL_URL`).
  Enqueue its jobs with `--prompt-version smoke-v1` (`--model` picks the model, default `qwen3:8b`).
- More workers: add `--scale worker=3` to `up`.
- Results are in the `results` volume, named by their result key.
