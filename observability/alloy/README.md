# PC telemetry: Grafana Alloy to Grafana Cloud (ADR 0027)

`config.alloy` collects the PC's host metrics (Windows exporter collectors `cpu`, `memory`, `logical_disk`, `net`,
`gpu`) and sends them over OTLP/HTTP to Grafana Cloud Free with basic auth. Every series carries
`service.instance.id` = the worker's install ID, so the PC and the worker share one identity. The local LGTM stack
(compose.obs.yaml) is unchanged and is still what you use for development.

## Vault entries (prompted, never pasted into chat)

```powershell
Set-Secret -Vault fleet -Name FLEET_GRAFANA_OTLP_URL            # the OTLP gateway base URL, ending in /otlp
Set-Secret -Vault fleet -Name FLEET_GRAFANA_INSTANCE_ID         # numeric OTLP instance ID (the basic-auth user)
Set-Secret -Vault fleet -Name FLEET_GRAFANA_TOKEN_PC_ALLOY      # write-only token for Alloy (metrics:write)
Set-Secret -Vault fleet -Name FLEET_GRAFANA_TOKEN_PC_WORKER     # write-only token for the worker (traces/metrics/logs:write)
```

One token per source, 90-day expiry; rotation is in `infra/gcp/fleet/README.md` ("Rotating the Grafana tokens").

## Alloy

1. Install once (Windows installer; the service is named `Alloy` and runs as LocalSystem):
   `winget install GrafanaLabs.Alloy`, or the installer from grafana.com's Alloy download page.
2. In an elevated PowerShell at the repo root: `./scripts/setup-alloy.ps1`. It writes the token to
   `%ProgramData%\fleet\alloy\grafana-token` (SYSTEM and Administrators only, inheritance off, owner Administrators;
   it refuses a symlink or junction in that place), copies `observability/alloy/config.alloy` to
   `%ProgramData%\fleet\alloy\config.alloy` in the same protected directory, sets the service's environment, points
   `run` at the COPY, pins `--storage.path` to `%ProgramData%\fleet\alloy\data` (the unsent-metrics buffer; same
   owner and ACL, so a user cannot read or replace it; the old default under `GrafanaLabs` is no longer used) and
   restarts the service. The service runs as SYSTEM, so it must never run a file a user can
   edit. Re-run it after rotating the token or editing the config (it refreshes the copy); it is idempotent and never
   prints the token. The environment holds the endpoint, the instance ID, the token file's path and the install ID,
   not the token.
3. Validate the copy before trusting the restart: in an elevated PowerShell,
   `alloy validate "$env:ProgramData\fleet\alloy\config.alloy"` (checks syntax and component arguments without
   starting Alloy; `alloy fmt` only checks formatting). If it complains about an empty `sys.env` value, that is
   the shell, not the service (the variables live in the service's registry entry); then restart the service and
   read its log instead.
4. Check: the service is Running (`Get-Service Alloy`); Alloy's own UI at http://127.0.0.1:12345 shows the components
   healthy; Grafana Explore shows `windows_cpu_time_total` with `service_name="fleet-pc"` within a couple of minutes.

The service reads the copy under `%ProgramData%`, so moving or deleting the checkout does not stop it; edits to the
repo file take effect only after re-running `setup-alloy.ps1`.

## The worker (one process at a time)

```powershell
pip install "fleet-worker[otel]"        # the opt-in extra (ADR 0018)
./scripts/with-secrets.ps1 -Names FLEET_WORKER_TOKEN -- pwsh -NoProfile -File ./scripts/grafana-env.ps1 -- fleet-worker <your usual arguments>
```

`scripts/grafana-env.ps1` sets `OTEL_EXPORTER_OTLP_ENDPOINT` and `OTEL_EXPORTER_OTLP_HEADERS`
(`Authorization=Basic%20<base64 of instance:token>`) for that command only, then removes them.

## UNVERIFIED (nothing here has run on a Windows host with Alloy)

- The registry key `HKLM\SOFTWARE\GrafanaLabs\Alloy` with multi-string values `Arguments` and `Environment` is how
  the Windows installer's service is configured per Grafana's documentation as I know it. If the key or value names
  differ, `setup-alloy.ps1` stops with a message before changing anything it cannot find.
- The `gpu` collector of `prometheus.exporter.windows` needs a Windows exporter and Alloy release that include it;
  an older one refuses the config at startup (the Alloy service log says which collector).
- Whether `otelcol.receiver.prometheus` plus the transform processor keeps `service.name = fleet-pc` on the free tier's
  OTLP gateway unchanged. Check the first series in Explore.
