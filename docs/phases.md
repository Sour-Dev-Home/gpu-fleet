# Phases

- [ ] 0a. Secrets walkthrough (docs/secrets.md): set up Bitwarden (master passphrase on paper, two-factor with a
      printed recovery code), back up the age key as a Secure Note, store a test token in the vault, run a command
      with it, encrypt and decrypt a sops file
- [ ] 0. Repo, worker skeleton, the job protocol, tests (Python packaging, protocol design); Dependabot pip entry
- [ ] 1. Broker API locally: leases, heartbeats, retries (at-least-once delivery, idempotency)
- [ ] 2. Docker images for worker and broker; compose locally; Dependabot docker entry
- [ ] 3. Terraform for AWS: S3 bucket, IAM, remote state; Dependabot terraform entry
- [ ] 4. Terraform for Google Cloud: Cloud Run broker, budget alert, presigned URLs
- [ ] 5. Notebook workers (Kaggle, Colab) with the pip package
- [ ] 6. Cloud Run Jobs GPU backend + workload identity federation to AWS
- [ ] 7. Kubernetes: kind locally, then GKE Autopilot + KEDA on backlog
- [ ] 8. Ansible roles deploying workers to homelab VMs (from WSL)
- [ ] 9. Progress view, then Metaphor #1013's real run through the fleet
- [ ] 10. Contributor mode: tokens, trust rules, docs
