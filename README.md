# gpu-fleet

A compute-agnostic GPU worker fleet: a small broker hands out leased jobs and presigned storage URLs, and the same
worker runs unchanged on a local GPU, free notebooks (Kaggle, Colab), Cloud Run Jobs and Kubernetes. First workload:
cutscene audio descriptions for the Metaphor: ReFantazio accessibility mod
([#1013](https://github.com/Sour-Dev-Home/metaphor-accessibility/issues/1013),
[#1025](https://github.com/Sour-Dev-Home/metaphor-accessibility/issues/1025)).

Built as a learning project, phase by phase: see [docs/phases.md](docs/phases.md).

## Run it locally

A broker and a worker in Docker Compose ([ADR 0014](docs/adr/0014-container-images-and-compose.md)). The tokens
come from the local `fleet` vault as environment variables for that one command (see
[docs/secrets.md](docs/secrets.md)). Compose refuses to run any command without them. In PowerShell:

```powershell
# the broker image installs with uv sync --frozen: check its lock is current first
uv lock --check --directory broker
# broker on 127.0.0.1:8000 and one echo worker (Ctrl+C stops them)
./scripts/with-secrets.ps1 -Names FLEET_WORKER_TOKEN,FLEET_ADMIN_TOKEN -- docker compose up --build
# in a second terminal: add 20 demo jobs for the echo runner, then stop the stack (volumes are kept)
./scripts/with-secrets.ps1 -Names FLEET_WORKER_TOKEN,FLEET_ADMIN_TOKEN -- docker compose run --rm broker fleet-enqueue --demo 20
./scripts/with-secrets.ps1 -Names FLEET_WORKER_TOKEN,FLEET_ADMIN_TOKEN -- docker compose down
```

- Jobs must match the runner: `echo` implements `echo-v1` (`fleet-enqueue`'s default), `smoke` implements
  `smoke-v1`. A worker given a job for another prompt version, or (smoke) a model the model server doesn't
  have, stops with an error saying what to fix. It is not restarted until ADR 0015, so the job just waits.
- The default `echo` runner needs no GPU. `$env:FLEET_RUNNER = 'smoke'` before `up` sends one tiny prompt per
  job to the model server: native Ollama on the host at `http://host.docker.internal:11434` (`FLEET_MODEL_URL`).
  Enqueue its jobs with `--prompt-version smoke-v1` (`--model` picks the model, default `qwen3:8b`).
- More workers: add `--scale worker=3` to `up`.
- Results are in the `results` volume, named by their result key.
