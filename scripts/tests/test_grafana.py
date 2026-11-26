"""The Fleet dashboard and alerts (observability/grafana) stay in step with the broker's metrics.

Grafana shows a renamed metric as an empty panel and a silent alert, so the drift is caught here.
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GRAFANA = ROOT / "observability" / "grafana"
DASHBOARD = json.loads((GRAFANA / "dashboards" / "fleet.json").read_text(encoding="utf-8"))
ALERTS = (GRAFANA / "alerts.yaml").read_text(encoding="utf-8")
TELEMETRY = (ROOT / "broker" / "src" / "fleet_broker" / "telemetry.py").read_text(encoding="utf-8")

# OTel name -> the names Prometheus stores it under (dots become _, units and _total are added).
PROMETHEUS_NAMES = {
    "fleet.jobs": {"fleet_jobs"},
    "fleet.queue.oldest_age": {"fleet_queue_oldest_age_seconds"},
    "fleet.leases.reissued": {"fleet_leases_reissued_total"},
    "fleet.jobs.completed": {"fleet_jobs_completed_total"},
    "fleet.http.busy": {"fleet_http_busy_total"},
    "fleet.http.limited": {"fleet_http_limited_total"},
    "fleet.job.turnaround": {
        "fleet_job_turnaround_seconds_bucket",
        "fleet_job_turnaround_seconds_count",
        "fleet_job_turnaround_seconds_sum",
    },
}
GAUGES = ("fleet_jobs", "fleet_queue_oldest_age_seconds")
DATASOURCES = {"prometheus", "tempo", "loki"}  # the uids otel-lgtm provisions


def promql() -> list[str]:
    exprs = [t["expr"] for p in DASHBOARD["panels"] for t in p.get("targets", []) if "expr" in t]
    exprs += re.findall(r"^\s*expr: (.+)$", ALERTS, flags=re.MULTILINE)
    return [e for e in exprs if "fleet_" in e]


def test_the_metric_names_exist_in_the_broker():
    for otel_name in PROMETHEUS_NAMES:
        assert f'"{otel_name}"' in TELEMETRY, f"{otel_name} is no longer defined in telemetry.py"
    known = set().union(*PROMETHEUS_NAMES.values())
    for expr in promql():
        for used in re.findall(r"\bfleet_[a-z_]+", expr):
            assert used in known, f"{used} in {expr!r} is not a broker metric"


def test_gauges_are_combined_with_max_across_broker_instances():
    # A restarted broker leaves its old series for ~5 min; summing them double counts the queue.
    for expr in promql():
        for gauge in GAUGES:
            if re.search(rf"\b{gauge}\b", expr):
                assert re.search(rf"max(\s+by\s*\([^)]*\))?\s*\(\s*{gauge}\b", expr), expr


def test_the_slo_uses_the_900_second_bucket_and_95_percent():
    slo = next(p for p in DASHBOARD["panels"] if p["title"].startswith("SLO"))
    assert 'le="900"' in slo["targets"][0]["expr"]
    steps = slo["fieldConfig"]["defaults"]["thresholds"]["steps"]
    assert steps[-1] == {"color": "green", "value": 0.95}
    budget = next(p for p in DASHBOARD["panels"] if p["title"] == "Error budget left")
    assert budget["targets"][0]["expr"].endswith("/ 0.05")  # 1 - 0.95
    # 900 must be a real bucket edge, or the le="900" series doesn't exist and the SLO is empty.
    buckets = re.search(r"TURNAROUND_BUCKETS = \(([^)]*)\)", TELEMETRY).group(1)
    assert "900" in re.findall(r"\d+", buckets)
    # Nothing finished in the range: no value (the panel says so), not 0/0 = NaN.
    for panel in (slo, budget):
        assert "_count[$__range])) > 0)" in panel["targets"][0]["expr"]


def test_panels_use_the_provisioned_datasources_and_unique_ids():
    ids = [p["id"] for p in DASHBOARD["panels"]]
    assert len(ids) == len(set(ids))
    for p in DASHBOARD["panels"]:
        assert p["datasource"]["uid"] in DATASOURCES, p["title"]
    for uid in re.findall(r"datasourceUid: (\S+)", ALERTS):
        assert uid in DATASOURCES | {"__expr__"}


def test_the_two_alerts_and_their_thresholds():
    assert re.findall(r"^\s*- uid: (\S+)", ALERTS, flags=re.MULTILINE) == [
        "fleet-dead-letters",
        "fleet-queue-stuck",
    ]
    assert re.search(r'fleet_jobs\{state="dead"\}.*\n(.*\n)*?.*params: \[0\]', ALERTS)
    assert re.search(r"oldest_age_seconds.*\n(.*\n)*?.*params: \[600\]", ALERTS)
    # The age gauge reports 0 for an empty queue: missing data means the broker went quiet.
    stuck = ALERTS.split("uid: fleet-queue-stuck")[1]
    assert "noDataState: Alerting" in stuck


def test_compose_mounts_the_provisioning_read_only():
    obs = (ROOT / "compose.obs.yaml").read_text(encoding="utf-8")
    for source in ("dashboards.yaml", "dashboards:", "alerts.yaml"):
        (line,) = [x for x in obs.splitlines() if f"./observability/grafana/{source}" in x]
        assert line.rstrip().endswith(":ro"), line
    # The provider reads the folder the dashboards are mounted at.
    path = re.search(r"path: (\S+)", (GRAFANA / "dashboards.yaml").read_text(encoding="utf-8"))
    assert f"./observability/grafana/dashboards:{path.group(1)}:ro" in obs
