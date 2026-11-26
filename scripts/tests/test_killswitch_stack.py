"""Text checks on the budget kill switch's Terraform (ADR 0024): least privilege, a private service, a
digest-pinned image, a budget scoped to this project. No cloud call. Comments are stripped first."""

import re

from test_gcp_stack import all_code, block, code


def res(kind: str, name: str) -> str:
    return block("killswitch.tf", f'resource "{kind}" "{name}"')


def test_custom_role_has_exactly_the_two_iam_policy_permissions():
    b = res("google_project_iam_custom_role", "killswitch")
    perms = re.search(r"permissions\s*=\s*\[(.*?)\]", b, re.DOTALL).group(1)
    assert re.findall(r'"([^"]+)"', perms) == [
        "run.services.getIamPolicy",
        "run.services.setIamPolicy",
    ]
    assert 'role_id     = "fleetKillSwitch"' in b


def test_custom_role_is_bound_on_the_broker_service_only():
    text = all_code()
    assert text.count("google_project_iam_custom_role.killswitch.name") == 1
    b = res("google_cloud_run_v2_service_iam_member", "killswitch_edits_broker")
    assert "google_project_iam_custom_role.killswitch.name" in b
    assert "google_cloud_run_v2_service.broker[0].name" in b
    assert "google_service_account.killswitch.email" in b
    # never a project-level or other binding of the role
    for m in re.finditer(
        r'resource "(google_project_iam_\w+|google_\w+_iam_(?:binding|policy))"[^{]*\{(.*?)\n\}',
        text,
        re.DOTALL,
    ):
        assert "killswitch" not in m.group(2) or m.group(1) == "google_project_iam_custom_role", (
            m.group(1)
        )


def test_the_runtime_account_holds_no_other_role():
    text = code("killswitch.tf") + code("main.tf") + code("cloudrun.tf") + code("cloudsql.tf")
    members = re.findall(
        r"role\s*=\s*([^\n]+)\n\s*member\s*=\s*\"serviceAccount:\$\{google_service_account\.killswitch\.email",
        text,
    )
    assert members == ["google_project_iam_custom_role.killswitch.name"]


def test_the_killswitch_service_is_private():
    text = all_code()
    assert text.count("allUsers") == 1  # only the broker's public binding in cloudrun.tf
    assert "allUsers" in code("cloudrun.tf")
    assert "allAuthenticatedUsers" not in text
    b = res("google_cloud_run_v2_service_iam_member", "pusher_invokes")
    assert 'role     = "roles/run.invoker"' in b
    assert "google_cloud_run_v2_service.killswitch[0].name" in b
    assert "google_service_account.budget_pusher.email" in b


def test_killswitch_service_is_small_protected_and_gated_on_its_image():
    b = res("google_cloud_run_v2_service", "killswitch")
    assert "count = var.killswitch_image == null ? 0 : 1" in b
    assert re.search(r"(?m)^\s*deletion_protection\s*=\s*true", b)
    assert re.search(r"max_instance_count\s*=\s*1\b", b)
    assert re.search(r"min_instance_count\s*=\s*0\b", b)
    assert re.search(r"cpu_idle\s*=\s*true", b)
    assert "image = var.killswitch_image" in b
    assert "FLEET_BROKER_SERVICE" in b and "local.broker_resource" in b


def test_killswitch_image_is_pinned_by_digest():
    var = block("variables.tf", 'variable "killswitch_image"')
    assert "@sha256:[0-9a-f]{64}" in var and "validation" in var
    assert re.search(r"default\s*=\s*null", var)


def test_billing_account_is_sensitive_and_has_no_default():
    var = block("variables.tf", 'variable "billing_account"')
    assert re.search(r"sensitive\s*=\s*true", var)
    assert "default" not in var


def test_budget_is_scoped_to_one_project_20_dollars_with_a_100_percent_rule():
    b = res("google_billing_budget", "fleet")
    assert re.search(r'projects\s*=\s*\["projects/\$\{data\.google_project\.this\.number\}"\]', b)
    assert 'currency_code = "USD"' in b and "units         = tostring(var.budget_usd)" in b
    assert re.search(r"threshold_percent\s*=\s*1\.0\b", b)
    assert re.search(r"threshold_percent\s*=\s*0\.5\b", b) and re.search(
        r"threshold_percent\s*=\s*0\.9\b", b
    )
    assert "CURRENT_SPEND" in b and "FORECASTED_SPEND" not in b
    assert re.search(r'schema_version\s*=\s*"1\.0"', b)
    assert "google_pubsub_topic.budget.id" in b
    var = block("variables.tf", 'variable "budget_usd"')
    assert re.search(r"default\s*=\s*20\b", var)


def test_budget_default_recipients_are_kept():
    b = res("google_billing_budget", "fleet")
    assert re.search(r"disable_default_iam_recipients\s*=\s*false", b)


def test_push_uses_an_oidc_token_for_the_pusher_with_the_service_as_audience():
    b = res("google_pubsub_subscription", "killswitch")
    assert re.search(r"(?m)^\s*oidc_token\s*\{", b)
    assert "service_account_email = google_service_account.budget_pusher.email" in b
    assert re.search(r"audience\s*=\s*google_cloud_run_v2_service\.killswitch\[0\]\.uri", b)
    assert re.search(r"ack_deadline_seconds\s*=\s*60\b", b)
    assert "retry_policy" in b and "minimum_backoff" in b and "maximum_backoff" in b
    assert re.search(r'message_retention_duration\s*=\s*"\d+s"', b)


def test_no_billing_account_id_is_written_in_the_stack():
    assert not re.search(
        r"[0-9A-F]{6}-[0-9A-F]{6}-[0-9A-F]{6}", all_code().replace("012345-6789AB-CDEF01", "")
    )


def test_provider_bills_budget_calls_to_the_project():
    v = code("versions.tf")
    assert re.search(r"user_project_override\s*=\s*true", v)
    assert re.search(r"billing_project\s*=\s*var\.project_id", v)


def test_the_two_apis_are_enabled():
    m = code("main.tf")
    assert '"billingbudgets.googleapis.com"' in m and '"pubsub.googleapis.com"' in m
