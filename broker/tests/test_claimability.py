"""fleet.jobs.unclaimable (Q84, ADR 0026): queued jobs no recent lease request could have taken."""

from conftest import make_store
from fastapi.testclient import TestClient
from fleet_broker import telemetry
from fleet_broker.app import create_app
from fleet_broker.claimability import MAX_SERIES, OTHER, WINDOW_SECONDS, Claimability
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from tokens import ADMIN, WORKER

NOW = 1_800_000_000.0
ALL = {"footage", "derived", "open"}


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


def waiting(*pairs, data_class="open", n=1):
    return {(m, v, data_class): n for m, v in pairs}


# --- the predicate ---


def test_a_pair_nobody_declared_is_unclaimable():
    c = Claimability(Clock(), ALL)
    assert c.unclaimable(waiting(("m", "v1"))) == {("m", "v1"): 1}


def test_declared_means_the_model_in_models_and_the_version_in_capabilities():
    c = Claimability(Clock(), ALL)
    c.record(["m"], ["v1"])
    assert c.unclaimable(waiting(("m", "v1"))) == {("m", "v1"): 0}
    assert c.unclaimable(waiting(("m", "v2"), ("other-model", "v1"))) == {
        ("m", "v2"): 1,
        ("other-model", "v1"): 1,
    }


def test_the_two_halves_must_come_from_the_same_request():
    # One worker runs (m, v1), another (n, v2). Nobody runs (m, v2), though m and v2 were each named.
    c = Claimability(Clock(), ALL)
    c.record(["m"], ["v1"])
    c.record(["n"], ["v2"])
    assert c.unclaimable(waiting(("m", "v2"), ("n", "v1"), ("m", "v1"), ("n", "v2"))) == {
        ("m", "v2"): 1,
        ("n", "v1"): 1,
        ("m", "v1"): 0,
        ("n", "v2"): 0,
    }


def test_a_declaration_lasts_ten_minutes():
    clock = Clock()
    c = Claimability(clock, ALL)
    c.record(["m"], ["v1"])
    clock.now += WINDOW_SECONDS - 1
    assert c.unclaimable(waiting(("m", "v1"))) == {("m", "v1"): 0}
    clock.now += 2
    assert c.unclaimable(waiting(("m", "v1"))) == {("m", "v1"): 1}


def test_asking_again_renews_the_window():
    clock = Clock()
    c = Claimability(clock, ALL)
    c.record(["m"], ["v1"])
    clock.now += WINDOW_SECONDS - 1
    c.record(["m"], ["v1"])
    clock.now += WINDOW_SECONDS - 1
    assert c.unclaimable(waiting(("m", "v1"))) == {("m", "v1"): 0}


def test_a_class_outside_the_brokers_clearance_is_unclaimable_even_if_declared():
    c = Claimability(Clock(), {"open"})
    c.record(["m"], ["v1"])  # every worker of this broker asks for (m, v1) ...
    w = {("m", "v1", "open"): 2, ("m", "v1", "footage"): 3}  # ... but footage can never be leased
    assert c.unclaimable(w) == {("m", "v1"): 3}


def test_the_count_adds_up_the_data_classes_of_a_pair():
    c = Claimability(Clock(), ALL)
    w = {("m", "v1", "open"): 2, ("m", "v1", "derived"): 3}
    assert c.unclaimable(w) == {("m", "v1"): 5}


def test_series_are_capped_with_an_other_bucket():
    c = Claimability(Clock(), ALL)
    pairs = [(f"m{i:03}", "v1") for i in range(MAX_SERIES + 20)]
    out = c.unclaimable(waiting(*pairs))
    assert len(out) == MAX_SERIES
    assert out[(OTHER, OTHER)] == 21  # 70 pairs, 49 kept: 21 folded in
    assert sum(out.values()) == MAX_SERIES + 20  # nothing lost


def test_the_cap_keeps_the_biggest_problems_by_name():
    c = Claimability(Clock(), ALL)
    big = {("big", "v1", "open"): 9}
    rest = {(f"m{i:03}", "v1", "open"): 1 for i in range(MAX_SERIES + 5)}
    out = c.unclaimable({**big, **rest})
    assert out[("big", "v1")] == 9


def test_many_distinct_requests_do_not_grow_memory_without_bound():
    c = Claimability(Clock(), ALL)
    for i in range(5000):
        c.record([f"m{i}"], ["v1"])
    assert len(c._seen) <= 1000


# --- the gauge, through the real SDK ---


def rig(db_url, clearance=ALL):
    clock, reader = Clock(), InMemoryMetricReader()
    claimability = Claimability(clock, clearance)
    reader_store = make_store(db_url)
    metrics = telemetry.OtelMetrics(
        MeterProvider(metric_readers=[reader]),
        lambda: (clock(), reader_store.stats(clock())),
        claimability,
    )
    app = create_app(
        db_url,
        WORKER,
        ADMIN,
        clearance,
        clock=clock,
        metrics=metrics,
        claimability=claimability,
    )
    return TestClient(app), clock, reader


def unclaimable(reader) -> dict[tuple[str, str], int]:
    out = {}
    data = reader.get_metrics_data()
    for rm in data.resource_metrics if data else []:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == "fleet.jobs.unclaimable":
                    for d in m.data.data_points:
                        out[(d.attributes["model"], d.attributes["prompt_version"])] = d.value
    return out


def add(client, job_id, model="m", version="v1", data_class="open"):
    r = client.post(
        "/jobs",
        json={
            "job_id": job_id,
            "input_sha256": "a" * 64,
            "model": model,
            "prompt_version": version,
            "data_class": data_class,
        },
        headers={"Authorization": f"Bearer {ADMIN}"},
    )
    assert r.status_code == 201


def lease(client, models, caps):
    return client.post(
        "/lease",
        json={"models": models, "capabilities": caps},
        headers={"Authorization": f"Bearer {WORKER}"},
    )


def test_gauge_reports_queued_jobs_no_lease_request_declared(db_url):
    client, _, reader = rig(db_url)
    add(client, "a")
    assert unclaimable(reader) == {("m", "v1"): 1}


def test_a_lease_request_that_declares_the_pair_clears_it(db_url):
    client, _, reader = rig(db_url)
    add(client, "a", model="m", version="v1")
    add(client, "b", model="n", version="v2")
    assert lease(client, ["m"], ["v1"]).status_code == 200  # takes "a"
    add(client, "c", model="m", version="v1")
    assert unclaimable(reader) == {("m", "v1"): 0, ("n", "v2"): 1}


def test_an_empty_poll_still_counts_as_a_declaration(db_url):
    # The worker asked, there was nothing it could take: that is the case the gauge exists for.
    client, _, reader = rig(db_url)
    assert lease(client, ["m"], ["v1"]).status_code == 204
    add(client, "a")
    assert unclaimable(reader) == {("m", "v1"): 0}


def test_requests_without_a_valid_token_declare_nothing(db_url):
    client, _, reader = rig(db_url)
    add(client, "a")
    r = client.post(
        "/lease",
        json={"models": ["m"], "capabilities": ["v1"]},
        headers={"Authorization": "Bearer wrong"},
    )
    assert r.status_code == 401
    assert unclaimable(reader) == {("m", "v1"): 1}


def test_a_class_outside_clearance_is_unclaimable_even_when_declared(db_url):
    client, _, reader = rig(db_url, clearance={"open"})
    add(client, "a", data_class="footage")
    assert lease(client, ["m"], ["v1"]).status_code == 204
    assert unclaimable(reader) == {("m", "v1"): 1}


def test_leased_and_cancelled_jobs_are_not_waiting(db_url):
    client, _, reader = rig(db_url)
    add(client, "a")
    add(client, "b", model="n")
    client.post("/jobs/b/cancel", headers={"Authorization": f"Bearer {ADMIN}"})
    assert lease(client, ["m"], ["v1"]).status_code == 200
    assert unclaimable(reader) == {}


def test_a_job_whose_lease_ran_out_waits_again(db_url):
    client, clock, reader = rig(db_url)
    add(client, "a")
    assert lease(client, ["m"], ["v1"]).status_code == 200
    clock.now += 61  # its lease (60 s) ran out, and nobody asked since; the window is open still
    clock.now += WINDOW_SECONDS  # ... and now it is closed
    assert unclaimable(reader) == {("m", "v1"): 1}
