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
# broker on 127.0.0.1:8000 and one worker (Ctrl+C stops them)
./scripts/with-secrets.ps1 -Names FLEET_WORKER_TOKEN,FLEET_ADMIN_TOKEN -- docker compose up
# in a second terminal: add 20 demo jobs, then stop the stack (volumes with jobs and results are kept)
./scripts/with-secrets.ps1 -Names FLEET_WORKER_TOKEN,FLEET_ADMIN_TOKEN -- docker compose run --rm broker fleet-enqueue --demo 20
./scripts/with-secrets.ps1 -Names FLEET_WORKER_TOKEN,FLEET_ADMIN_TOKEN -- docker compose down
```

- The default `echo` runner needs no GPU. `$env:FLEET_RUNNER = 'smoke'` before `up` sends one tiny prompt per
  job to the model server: native Ollama on the host at `http://host.docker.internal:11434` (`FLEET_MODEL_URL`).
- More workers: add `--scale worker=3` to `up`.
- Results are in the `results` volume, named by their result key.
