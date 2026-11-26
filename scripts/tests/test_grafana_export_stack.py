"""Text checks on the Grafana Cloud export's Terraform (ADR 0027): the token's secret container, who can read
it, the scheduler job and its identity, and that grafana_export = false leaves the broker's env untouched.
No cloud call. Comments are stripped first (test_gcp_stack.code)."""

import re

from test_gcp_stack import ROOT, STACK, all_code, block, code


def res(kind: str, name: str) -> str:
    return block("grafana.tf", f'resource "{kind}" "{name}"')


def test_token_secret_is_a_container_with_no_version_and_no_value():
    b = res("google_secret_manager_secret", "grafana_token_broker")
    assert 'secret_id = "fleet-grafana-token-broker"' in b
    assert "auto {}" in b
    # the only versions Terraform writes are the two non-secret identifiers (checked below)
    versions = re.findall(r'resource "google_secret_manager_secret_version" "(\w+)"', all_code())
    assert sorted(versions) == ["grafana_instance_id", "grafana_otlp_endpoint"]
    assert all_code().count("secret_data") == 2


def test_only_the_broker_reads_the_grafana_token_and_only_that_secret():
    b = res("google_secret_manager_secret_iam_member", "broker_reads_grafana")
    assert "google_secret_manager_secret.grafana_token_broker.secret_id" in b
    assert 'role      = "roles/secretmanager.secretAccessor"' in b
    assert "google_service_account.broker.email" in b
    # the scheduler account holds no secret access at all
    for m in re.finditer(
        r'resource "google_secret_manager_secret_iam_\w+" "\w+" \{(.*?)\n\}', all_code(), re.DOTALL
    ):
        assert "scheduler" not in m.group(1)


def test_the_token_reaches_the_broker_as_a_secret_env_only_when_exporting():
    b = code("cloudrun.tf")
    m = re.search(
        r'dynamic "env" \{\s*for_each = var\.grafana_export \? \[1\] : \[\]\s*content \{\s*name = "FLEET_GRAFANA_TOKEN"(.*?)\n      \}\n',
        b,
        re.DOTALL,
    )
    assert m, "the token env must be gated on var.grafana_export"
    body = m.group(1)
    assert "google_secret_manager_secret.grafana_token_broker.secret_id" in body
    assert "secret_key_ref" in body and 'version = "latest"' in body
    assert "value =" not in body.replace("value_source", "")  # never a literal


def test_grafana_export_false_adds_no_env_at_all():
    g = code("grafana.tf")
    assert re.search(
        r"grafana_env_names\s*=\s*var\.grafana_export\s*\?\s*toset\(\[(.*?)\]\)\s*:\s*toset\(\[\]\)",
        g,
        re.DOTALL,
    )
    names = re.findall(
        r'"([A-Z_]+)"',
        re.search(
            r"grafana_env_names\s*=\s*var\.grafana_export\s*\?\s*toset\(\[(.*?)\]\)", g, re.DOTALL
        ).group(1),
    )
    assert names == [
        "FLEET_FLUSH_OIDC_AUDIENCE",
        "FLEET_FLUSH_OIDC_EMAIL",
    ]
    c = code("cloudrun.tf")
    assert re.search(r'dynamic "env" \{\s*for_each = local\.grafana_env_names\s*content \{', c)
    # nothing else in the stack sets these names
    for n in names + [
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "FLEET_GRAFANA_INSTANCE_ID",
        "FLEET_GRAFANA_TOKEN",
        "OTEL_EXPORTER_OTLP_HEADERS",
    ]:
        assert c.count(f'"{n}"') <= 1, n


def test_endpoint_and_instance_id_are_sensitive_nullable_variables_defaulting_to_null():
    for name in ("grafana_otlp_endpoint", "grafana_instance_id"):
        v = block("variables.tf", f'variable "{name}"')
        assert re.search(r"sensitive\s*=\s*true", v), name
        assert re.search(r"default\s*=\s*null", v), name
        assert re.search(r"nullable\s*=\s*true", v), name
    v = block("variables.tf", 'variable "grafana_export"')
    assert "type        = bool" in v and re.search(r"default\s*=\s*false", v)
    assert "https://" in block("variables.tf", 'variable "grafana_otlp_endpoint"')


def test_enabling_the_export_without_its_values_fails_the_plan():
    b = block("cloudrun.tf", 'resource "google_cloud_run_v2_service" "broker"')
    pre = re.search(r"precondition\s*\{(.*?)\n\s*\}", b, re.DOTALL).group(1)
    assert "var.grafana_export" in pre
    assert "var.grafana_otlp_endpoint != null" in pre and "var.grafana_instance_id != null" in pre


def test_the_scheduler_job_is_every_minute_a_post_with_a_short_deadline_and_no_retry():
    b = res("google_cloud_scheduler_job", "telemetry_flush")
    assert 'schedule         = "* * * * *"' in b
    assert "region           = var.region" in b and "var.region" in b
    assert 'attempt_deadline = "30s"' in b
    assert re.search(r"retry_count\s*=\s*0\b", b)
    assert 'http_method = "POST"' in b
    assert '/telemetry/flush"' in b
    assert "count = local.flush_enabled ? 1 : 0" in b
    assert "authorization_header" not in b and "headers" not in b  # no admin token in the job


def test_the_scheduler_job_uses_an_oidc_token_for_the_dedicated_account():
    b = res("google_cloud_scheduler_job", "telemetry_flush")
    o = re.search(r"oidc_token\s*\{(.*?)\}", b, re.DOTALL).group(1)
    assert "service_account_email = google_service_account.scheduler.email" in o
    sa = res("google_service_account", "scheduler")
    assert 'account_id   = "fleet-scheduler"' in sa


def _resolve(expr: str, g: str) -> str:
    """A literal string, or a local.NAME resolved to its literal."""
    expr = expr.strip()
    if expr.startswith("local."):
        name = expr.removeprefix("local.")
        expr = re.search(rf"(?m)^\s*{name}\s*=\s*(.+)$", g).group(1).strip()
    assert expr.startswith('"') and expr.endswith('"'), expr
    return expr[1:-1]


def test_one_audience_string_on_the_service_the_job_and_the_broker_env():
    g = code("grafana.tf")
    job = res("google_cloud_scheduler_job", "telemetry_flush")
    sched = re.search(r"audience\s*=\s*(.+)", job).group(1)
    env = re.search(r"FLEET_FLUSH_OIDC_AUDIENCE\s*=\s*(.+)", g).group(1)
    svc = block("cloudrun.tf", 'resource "google_cloud_run_v2_service" "broker"')
    custom = re.search(r"custom_audiences\s*=\s*\[(.+?)\]", svc).group(1)
    values = {_resolve(sched, g), _resolve(env, g), _resolve(custom, g)}
    assert values == {"fleet-telemetry-flush"}, values
    assert re.search(r"FLEET_FLUSH_OIDC_EMAIL\s*=\s*google_service_account\.scheduler\.email", g)


def test_the_audience_is_not_a_predicted_url():
    g = code("grafana.tf")
    assert "broker_url" not in g and "run.app" not in g
    assert 'check "' not in g


def test_scheduler_keeps_invoker_on_the_broker_service_only_so_flushes_survive_the_kill_switch():
    b = res("google_cloud_run_v2_service_iam_member", "scheduler_invokes")
    assert 'role     = "roles/run.invoker"' in b
    assert "google_cloud_run_v2_service.broker[0].name" in b
    assert "google_service_account.scheduler.email" in b
    assert "count = local.flush_enabled ? 1 : 0" in b
    # an additive member resource, like the public binding: no authoritative binding/policy that would
    # drop the member (or the switch's edit) on apply
    assert not re.search(r'resource "google_cloud_run_v2_service_iam_(binding|policy)"', all_code())
    # the scheduler account holds no project-level role
    for m in re.finditer(
        r'resource "google_project_iam_\w+" "\w+" \{(.*?)\n\}', all_code(), re.DOTALL
    ):
        assert "scheduler" not in m.group(1)


def test_the_scheduler_api_is_enabled_and_the_job_waits_for_it():
    assert '"cloudscheduler.googleapis.com"' in code("main.tf")
    b = res("google_cloud_scheduler_job", "telemetry_flush")
    assert "google_project_service.api" in b
    assert "google_cloud_run_v2_service_iam_member.scheduler_invokes" in b


def test_no_account_identifier_or_endpoint_is_written_in_the_stack():
    text = all_code()
    assert not re.search(r"grafana\.net", text)
    assert not re.search(r"glc_[A-Za-z0-9]", text)
    readme = (STACK / "README.md").read_text(encoding="utf-8")
    assert not re.search(r"glc_[A-Za-z0-9]|grafana\.net/otlp", readme.replace("<stack>", ""))


def test_readme_documents_the_secret_version_cost_rotation_and_platform_metrics():
    r = (STACK / "README.md").read_text(encoding="utf-8")
    assert "fleet-grafana-token-broker" in r
    assert "43,200" in r and "2M" in r and "3 free jobs" in r
    assert "90 days" in r or "90-day" in r
    assert "TF_VAR_grafana_otlp_endpoint" in r and "TF_VAR_grafana_instance_id" in r
    assert "fleet-scheduler" in r
    assert "Google console" in r or "Google Cloud console" in r  # Q90: platform metrics stay there


def test_adr_0027_exists_is_accepted_and_indexed():
    adr = (ROOT / "docs/adr/0027-telemetry-destination.md").read_text(encoding="utf-8")
    assert "**Status:** Accepted" in adr and "Q88-Q92" in adr
    index = (ROOT / "docs/adr/README.md").read_text(encoding="utf-8")
    assert "0027-telemetry-destination.md" in index


# --- the broker's env stays readable in plans -----------------------------------------------------------------


def _sensitive_variables() -> set[str]:
    found = set()
    for m in re.finditer(r'variable "(\w+)" \{(.*?)\n\}', code("variables.tf"), re.DOTALL):
        if re.search(r"(?m)^\s*sensitive\s*=\s*true", m.group(2)):
            found.add(m.group(1))
    return found


def _local_text(name: str) -> str:
    """The text of `name = ...` (a locals entry), up to the line where its braces balance."""
    text = all_code()
    m = re.search(rf"(?m)^\s*{name}\s*=\s*", text)
    assert m, name
    depth = 0
    for j in range(m.end(), len(text)):
        depth += text[j] in "{[("
        depth -= text[j] in "}])"
        if depth <= 0 and text[j] == "\n":
            return text[m.end() : j]
    return text[m.end() :]


def test_no_env_value_in_the_broker_depends_on_a_sensitive_variable():
    """One sensitive value inside the env set makes every entry (sensitive) in the plan."""
    sensitive = _sensitive_variables()
    assert {"grafana_otlp_endpoint", "grafana_instance_id"} <= sensitive
    svc = block("cloudrun.tf", 'resource "google_cloud_run_v2_service" "broker"')
    exprs = re.findall(r"(?m)^\s*value\s*=\s*(.+)$", svc)
    assert exprs, "the broker has plain env values to check"
    for expr in exprs:
        text = expr
        for loc in re.findall(r"local\.(\w+)", expr):
            text += _local_text(loc)
        for v in sensitive:
            assert not re.search(rf"var\.{v}\b", text), f"env value {expr!r} depends on var.{v}"


def test_the_grafana_identifiers_are_secret_versions_pinned_to_the_terraform_managed_version():
    c = code("cloudrun.tf")
    gate = "count = var.grafana_export ? 1 : 0"
    for sid, name, env in [
        ("fleet-grafana-otlp-endpoint", "grafana_otlp_endpoint", "OTEL_EXPORTER_OTLP_ENDPOINT"),
        ("fleet-grafana-instance-id", "grafana_instance_id", "FLEET_GRAFANA_INSTANCE_ID"),
    ]:
        sec = res("google_secret_manager_secret", name)
        assert f'secret_id = "{sid}"' in sec and gate in sec
        ver = res("google_secret_manager_secret_version", name)
        assert f"secret_data = var.{name}" in ver and gate in ver
        assert f"google_secret_manager_secret.{name}[0].id" in ver
        iam = res("google_secret_manager_secret_iam_member", f"broker_reads_{name}")
        assert f"google_secret_manager_secret.{name}[0].secret_id" in iam and gate in iam
        assert 'role      = "roles/secretmanager.secretAccessor"' in iam
        assert "google_service_account.broker.email" in iam
        assert f"google_secret_manager_secret_iam_member.broker_reads_{name}" in c
        m = re.search(
            rf'name = "{env}"\s*value_source \{{\s*secret_key_ref \{{(.*?)\}}', c, re.DOTALL
        )
        assert m, env
        assert f"google_secret_manager_secret.{name}[0].secret_id" in m.group(1)
        assert f"google_secret_manager_secret_version.{name}[0].version" in m.group(1)
        assert '"latest"' not in m.group(1)
