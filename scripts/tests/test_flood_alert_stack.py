"""Text checks on the flood alert -> kill switch wiring (Q93) and the service-level instance cap (Q95).
No cloud call. Comments are stripped first."""

import re

from test_gcp_stack import STACK, block, code


def res(file: str, kind: str, name: str) -> str:
    return block(file, f'resource "{kind}" "{name}"')


def test_flood_topic_is_separate_from_the_budget_topic():
    b = res("killswitch.tf", "google_pubsub_topic", "flood")
    assert 'name = "fleet-flood"' in b
    assert 'name = "fleet-budget"' in res("killswitch.tf", "google_pubsub_topic", "budget")


def test_the_pubsub_channel_publishes_to_the_flood_topic_and_the_4xx_alert_uses_it():
    ch = res("monitoring.tf", "google_monitoring_notification_channel", "flood")
    assert 'type         = "pubsub"' in ch
    assert re.search(r"topic\s*=\s*google_pubsub_topic\.flood\.id", ch)
    # The CHANNEL comes first: it is what creates Monitoring's notification service agent, so the grant waits for
    # it (a grant to a not-yet-existing agent fails).
    assert "google_pubsub_topic_iam_member.monitoring_publishes_flood" not in ch
    grant = res("killswitch.tf", "google_pubsub_topic_iam_member", "monitoring_publishes_flood")
    assert re.search(
        r"depends_on\s*=\s*\[[^\]]*google_monitoring_notification_channel\.flood", grant
    )
    pol = res("monitoring.tf", "google_monitoring_alert_policy", "broker_4xx")
    chans = re.search(r"notification_channels\s*=\s*\[(.*?)\]", pol, re.DOTALL).group(1)
    assert "google_monitoring_notification_channel.email.id" in chans
    assert "google_monitoring_notification_channel.flood.id" in chans
    # the IAM-change alert keeps email only: it must never trip the switch
    other = res("monitoring.tf", "google_monitoring_alert_policy", "killswitch_iam_change")
    assert "flood" not in other


def test_only_the_monitoring_service_agent_may_publish_and_only_on_the_flood_topic():
    b = res("killswitch.tf", "google_pubsub_topic_iam_member", "monitoring_publishes_flood")
    assert "google_pubsub_topic.flood.name" in b
    assert 'role   = "roles/pubsub.publisher"' in b
    assert (
        "service-${data.google_project.this.number}@gcp-sa-monitoring-notification.iam.gserviceaccount.com"
        in b
    )
    text = code("killswitch.tf") + code("monitoring.tf")
    assert text.count("gcp-sa-monitoring-notification") == 1
    assert 'resource "google_pubsub_topic_iam_member"' not in code("monitoring.tf")
    assert code("killswitch.tf").count("gcp-sa-monitoring-notification") == 1


def test_flood_subscription_pushes_to_the_alert_path_with_the_same_pusher_and_oidc():
    b = res("killswitch.tf", "google_pubsub_subscription", "flood")
    assert "count = var.killswitch_image == null ? 0 : 1" in b
    assert "topic = google_pubsub_topic.flood.id" in b
    assert re.search(
        r'push_endpoint\s*=\s*"\$\{google_cloud_run_v2_service\.killswitch\[0\]\.uri\}/alert"', b
    )
    assert "service_account_email = google_service_account.budget_pusher.email" in b
    assert re.search(r"audience\s*=\s*google_cloud_run_v2_service\.killswitch\[0\]\.uri", b)
    # the budget subscription still pushes to the bare URI (its own path)
    budget = res("killswitch.tf", "google_pubsub_subscription", "killswitch")
    assert re.search(
        r"push_endpoint\s*=\s*google_cloud_run_v2_service\.killswitch\[0\]\.uri\s", budget
    )


def test_the_switch_learns_the_expected_policy_from_the_policy_not_from_a_literal():
    b = res("killswitch.tf", "google_cloud_run_v2_service", "killswitch")
    assert re.search(
        r'name\s*=\s*"FLEET_FLOOD_POLICY"\s*value\s*=\s*google_monitoring_alert_policy\.broker_4xx\.display_name',
        b,
    )


def test_the_4xx_alert_is_a_sustained_flood_not_a_twitch():
    # A misconfigured worker's 401s arrive at about 1/s (its poll interval); the policy needs more than
    # 5/s for 5 minutes (1500+ requests), and the switch only acts when the incident opens.
    b = res("monitoring.tf", "google_monitoring_alert_policy", "broker_4xx")
    assert re.search(r"threshold_value\s*=\s*5\b", b)
    assert re.search(r'duration\s*=\s*"300s"', b)


def test_the_policy_display_name_has_no_surprise_edit_path():
    # the env value is the display name, so renaming the policy renames the env in the same plan
    b = res("monitoring.tf", "google_monitoring_alert_policy", "broker_4xx")
    assert re.search(r'display_name = "fleet-broker: 4xx request rate"', b)


def test_both_services_cap_instances_at_the_service_level_too():
    # run.googleapis.com/maxScale is a service-level annotation the revision cap does not set; state it so a
    # console or gcloud change cannot silently diverge from the revision cap.
    for kind, name, expr in (
        ("google_cloud_run_v2_service", "broker", r"var\.max_instances"),
        ("google_cloud_run_v2_service", "killswitch", r"1"),
    ):
        file = "cloudrun.tf" if name == "broker" else "killswitch.tf"
        b = res(file, kind, name)
        body = b[: b.index("template {")]  # before the template: service-level scaling
        assert re.search(rf"scaling\s*\{{[^}}]*max_instance_count\s*=\s*{expr}\b", body), name


def test_readme_has_the_flood_drill_with_a_fake_open_incident():
    text = (STACK / "README.md").read_text(encoding="utf-8")
    assert "gcloud pubsub topics publish fleet-flood" in text
    assert '"state":"open"' in text and "policy_name" in text
    # The drill must pass the parser's checks, or it is silently ignored: version 1.2, a current started_at
    # (the 1-hour replay window) and the policy's real display name.
    drill = text[text.index("**Drill** (cuts public access, like the budget drill") :]
    drill = drill[: drill.index("```", drill.index("```") + 3)]
    assert '"version":"1.2"' in drill
    assert re.search(r"\"started_at\":' \+ \$now", drill) and "ToUnixTimeSeconds()" in drill
    policy = res("monitoring.tf", "google_monitoring_alert_policy", "broker_4xx")
    name = re.search(r'display_name\s*=\s*"([^"]+)"', policy).group(1)
    assert f'"policy_name":"{name}"' in drill


def test_the_comment_explaining_service_level_scaling_exists():
    for f in ("cloudrun.tf", "killswitch.tf"):
        assert "maxScale" in (STACK / f).read_text(encoding="utf-8"), f


def test_the_flood_condition_counts_4xx_except_429():
    # I3: 429 is Cloud Run's "no free instance" and the broker's own rate limit: load shedding doing its job,
    # not a flood of bad requests, and it must not trip the kill switch.
    b = res("monitoring.tf", "google_monitoring_alert_policy", "broker_4xx")
    f = re.search(r'filter\s*=\s*"(.*)"', b).group(1).replace('\\"', '"')
    assert 'metric.labels.response_code_class = "4xx"' in f
    assert 'metric.labels.response_code != "429"' in f
