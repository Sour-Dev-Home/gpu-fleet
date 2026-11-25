# Phases

- [x] 0a. Secrets walkthrough (docs/secrets.md): set up Bitwarden (master passphrase on paper, two-factor with a
      printed recovery code), back up the age key as a Secure Note, store a test token in the vault, run a command
      with it, encrypt and decrypt a sops file
- [x] 0. Repo, worker skeleton, the job protocol, tests (Python packaging, protocol design); Dependabot uv entry
- [x] 1. Broker API locally: leases, heartbeats, retries (at-least-once delivery, idempotency)
- [x] 2. Docker images for worker and broker; compose locally; Dependabot docker entry
- [x] 3. Data class, clearance and capabilities in the claim first (ADR 0015, the docs/security.md gate); then
      Terraform for AWS: state and results buckets, remote state, adopted budget; Dependabot terraform entry
      (ADR 0017; the broker's IAM role moved to phase 4, its first user)
- [ ] 3b. Observability (ADR 0018): OpenTelemetry metrics, logs and traces; local Grafana LGTM stack as a
      Compose override (compose.obs.yaml); dashboard, two alerts and an SLO as code
- [ ] 4. Terraform for Google Cloud: Cloud Run broker, budget alert, presigned URLs; close the "before phase 4"
      items in docs/security.md and re-run the security review first
- [ ] 5. Notebook workers (Kaggle, Colab) with the pip package
- [ ] 6. Cloud Run Jobs GPU backend + workload identity federation to AWS
- [ ] 7. Kubernetes: kind locally, then GKE Autopilot + KEDA on backlog
- [ ] 8. Ansible roles deploying workers to homelab VMs (from WSL)
- [ ] 9. Progress view, then Metaphor #1013's real run through the fleet
- [ ] 10. Contributor mode: tokens, trust rules, docs; close the "before phase 10" items in docs/security.md
