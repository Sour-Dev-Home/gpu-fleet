# fleet-killswitch

A tiny private Cloud Run service (ADR 0024). Pub/Sub pushes the project's budget notifications to it; when actual
spend reaches the budget it removes `allUsers` from `roles/run.invoker` on the broker, so the broker's public URL
answers Google's 403. Reset: `terraform apply` in `infra/gcp/fleet` (see its README, "Kill switch").

Environment: `FLEET_BROKER_SERVICE` (the broker's full resource name, `projects/P/locations/R/services/NAME`) and
`PORT` (set by Cloud Run). It holds no keys: it uses the runtime service account's identity.

    cd killswitch && uv run pytest
