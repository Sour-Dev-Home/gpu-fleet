# Local secrets: how this project handles them

Rule of thumb, in order of preference:
1. **No secret at all**: log in instead. AWS through IAM Identity Center (`aws sso login`, short-lived credentials the
   CLI caches), Google Cloud (`gcloud auth login`, `gcloud auth application-default login`), GitHub (`gh auth login`,
   stored in Windows Credential Manager). In the cloud: roles, service accounts and workload identity federation.
2. **A secret that must exist on this PC** (a Kaggle API key, a Hugging Face token, the broker's admin token, a test
   worker token): the encrypted local vault `fleet` (PowerShell SecretManagement + SecretStore).
3. **A secret that must be shared with a deployment or live next to config** (Kubernetes secrets, a compose env
   file): encrypt it with **sops + age** and commit only the encrypted file `secrets/*.enc.yaml`.
4. **Never**: plain `.env` files in the repo, secrets in `*.tfvars`, in Dockerfiles or images, in notebook cells,
   in shell history, or pasted into a chat. Pre-commit (gitleaks), CI and GitHub push protection back this up.

## The master copy: a password manager (do this first)
Bitwarden (free, open source, audited) holds anything you'd hate to lose; the local vault below is only the working
copy for scripts. Setup:
1. Account with a master passphrase of 4-6 random words, written once on paper and kept safe at home.
2. Two-factor with an authenticator app (or a hardware key); **print the recovery code** and keep it with the
   passphrase.
3. Back up the age key: copy `%APPDATA%\sops\age\keys.txt` (three lines) into a Secure Note "age key: fleet repo"
   (free plan has no attachments; the key is short text). No email, no extra copies.
4. Over time: GitHub, Google and AWS recovery codes, the AWS root login (two-factor on root), API tokens you create.
If your PC dies, everything is rebuilt from Bitwarden. Offline alternative: KeePassXC with its database file in a
Syncthing-backed folder plus a USB copy (you own the backups).

## The vault (step 2)
- Add: `Set-Secret -Vault fleet -Name HF_TOKEN` (it prompts; the value never touches history).
- List: `Get-SecretInfo -Vault fleet`. Read for a check: `Get-Secret -Vault fleet HF_TOKEN -AsPlainText`.
- Use: `./scripts/with-secrets.ps1 -Names HF_TOKEN -- <command>` sets env vars for that one process only.
- Terraform: name the secret `TF_VAR_<variable>` and run `with-secrets.ps1 -Names TF_VAR_x -- terraform plan`;
  mark the variable `sensitive = true`. State can still hold secret values: keep state in the private, encrypted
  remote backend (phase 3), never in git.
- The vault locks after 15 minutes; `Unlock-SecretStore` to unlock early. Exam tie-in: same idea as Secrets Manager /
  Parameter Store, but local and free.
- Broker tokens (ADR 0011): two random, different tokens, generated straight into the vault so you never see them:
  ```powershell
  foreach ($n in 'FLEET_WORKER_TOKEN','FLEET_ADMIN_TOKEN') {
    Set-Secret -Vault fleet -Name $n -Secret ([Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes(32)))
  }
  ./scripts/with-secrets.ps1 -Names FLEET_WORKER_TOKEN,FLEET_ADMIN_TOKEN -- uv run --directory broker --with uvicorn uvicorn fleet_broker.app:app
  ```
  Workers get only the worker token.
- Database password (ADR 0020): `FLEET_DB_PASSWORD`, for the Compose `postgres` service and the broker's
  `FLEET_DATABASE_URL`. Generate it into the vault as hex, not base64: it goes into a URL, where `+`, `/` and `=` break:
  ```powershell
  Set-Secret -Vault fleet -Name FLEET_DB_PASSWORD -Secret ([Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32)))
  ./scripts/with-secrets.ps1 -Names FLEET_WORKER_TOKEN,FLEET_ADMIN_TOKEN,FLEET_DB_PASSWORD -- docker compose up --build
  ```
  Changing it later does not change a database that already exists: the password is set when the `postgres-data`
  volume is first initialised. Rotate with `ALTER ROLE fleet PASSWORD '...'` and then the vault, or `down -v` to start
  empty. Run the broker outside Compose with `FLEET_DATABASE_URL` in the vault (a `postgresql://` URL with the password).

### Rotating broker tokens (no downtime)
The broker accepts a second, previous token per role while a rotation is under way, so workers switch one at a
time. The same steps work for `FLEET_ADMIN_TOKEN` (with `FLEET_ADMIN_TOKEN_PREVIOUS`).
1. Move the current value aside, then generate the new one straight into the vault:
   ```powershell
   Set-Secret -Vault fleet -Name FLEET_WORKER_TOKEN_PREVIOUS -Secret (Get-Secret -Vault fleet FLEET_WORKER_TOKEN)
   Set-Secret -Vault fleet -Name FLEET_WORKER_TOKEN -Secret ([Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes(32)))
   ```
2. Restart the broker with both: `-Names FLEET_WORKER_TOKEN,FLEET_WORKER_TOKEN_PREVIOUS,FLEET_ADMIN_TOKEN`.
   It now accepts either worker token.
3. Restart each worker; they read the new `FLEET_WORKER_TOKEN`.
4. When no worker uses the old one, remove it and restart the broker without it:
   `Remove-Secret -Vault fleet -Name FLEET_WORKER_TOKEN_PREVIOUS`.

For a **leaked** token, skip the overlap: replace it and restart everything at once, so the leaked value stops
working immediately. On Cloud Run (phase 4) the tokens live in Secret Manager, and step 2 is a new revision.

## sops + age (step 3)
- Your age private key: `%APPDATA%\sops\age\keys.txt` (only your user can read it). **Back it up** in a password
  manager: lose it and every encrypted file is unreadable. The public key is in `.sops.yaml` (safe to commit).
- Create or edit: `sops secrets/k8s-worker.enc.yaml` (opens decrypted in your editor, saves encrypted).
- Decrypt for use: `sops -d secrets/k8s-worker.enc.yaml | kubectl apply -f -` (decrypted text never hits disk).
- Rotate: change the value, re-encrypt, and revoke the old one where it was issued.

## Notebooks (Kaggle, Colab)
Use their built-in secret stores (Kaggle "Add-ons > Secrets", Colab "Secrets" panel). Workers get only a short-lived
worker token from the broker, never cloud keys. Rotate notebook tokens after each run.

## If a secret leaks
1. Revoke or rotate it at the source first (AWS, Google Cloud, Kaggle, Hugging Face, GitHub).
2. Then remove it from history if it was committed; push protection should have stopped it.
3. Note it in docs/learning-log.md: what leaked, how, and which guard you added.
