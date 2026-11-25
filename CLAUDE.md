# Fleet project: how to work here

A learning project: the maintainer is learning AWS, Google Cloud, Terraform, Ansible, Docker and Kubernetes by
building this. Design and decisions come from the Metaphor accessibility mod's issues #1013 and #1025
(`gh issue view 1025 -R Sour-Dev-Home/metaphor-accessibility --comments`): read them at the start of a phase.

## Learning mode (every phase)
1. A short concept brief: what this layer is and why it exists; tie it to the AWS Solutions Architect exam where it fits.
2. Console first for any cloud piece: walk the maintainer through it once in the AWS or Google Cloud console.
3. Claude writes the code, Terraform and playbooks (maintainer's choice, 2026-11-25: no TODO(human) hand-offs), then
   gives a short review of the 3-5 concepts with the best learning return: exam-relevant, reusable, or the bug it
   prevents. Use back-of-the-napkin math to show whether a concept matters at this scale; map SQL to PostgreSQL.
4. Review every piece like a senior engineer (correctness, security, cost) and verify it: tests that fail without
   the change, mutation checks with `scripts/mutate.py`.
5. 3-4 checkpoint questions before the phase is marked done in docs/phases.md.
6. Append to docs/learning-log.md: what was built, what was learned, the numbers (time, cost, throughput).
Feed in the maintainer's Terraform and Ansible books when they share paths; follow their approach.

## Decisions already made (2026-11-24)
- $0 compute: local GPU, free notebooks, Google Cloud credits. AWS is coordination only (S3; no AWS compute).
- Broker API gives leases, heartbeats and presigned S3 URLs; workers never hold cloud keys. Cloud Run reaches AWS by
  workload identity federation (OIDC), never by stored keys.
- Jobs are leased and idempotent; results are content-addressed (input hash + model + prompt version).
- Worker ships as a pip package (notebooks, native Windows) and a Docker image (Cloud Run, Kubernetes, rented GPUs).
- Kubernetes is a backend (kind locally, then GKE Autopilot + KEDA on backlog), not the control layer.
- Copyrighted footage only goes to the maintainer's own backends; volunteers get text-only or open jobs.
- Local AMD GPU (RX 9070, ROCm): Ollama with `use_mmap=false` and an explicit `num_ctx`.

## Rules
- Secrets: follow docs/secrets.md (log in instead of storing keys; the local `fleet` vault via
  `scripts/with-secrets.ps1`; sops + age for anything committed). Never ask the maintainer to paste a secret into chat.
- Never commit secrets, state, footage or personal information (name, email, absolute paths into a user profile; use repo-relative paths or environment variables).
  Pre-commit and CI enforce it; keep private patterns in the git-ignored `.private-patterns.txt`.
- `terraform plan` is reviewed (by the `infra-reviewer` agent and the maintainer) before every `apply`.
- Before any change to IAM, networking, Terraform, Dockerfiles or Kubernetes manifests: run the `infra-reviewer`
  agent in the foreground and fix what it finds.
- Every behaviour change has a test that fails without it. Narrow tests first, the full suite last.
- Conventional commit subjects; short PR bodies. Check with `scripts/check.sh` before committing; ship with
  `scripts/ship-pr.sh` (auto-merges after CI), or `--hold` for anything awaiting the maintainer's approval.
- Every decision about architecture, protocols, storage, security posture, cost or tooling gets an ADR in
  `docs/adr/` (index in its README) in the same PR as the change. Mark it Proposed until the maintainer approves.
  A changed decision gets a new ADR that supersedes the old one.
