"""Controller unit tests — pure helpers (job spec / labels / workload summary)
and the /submit endpoint with the Kubernetes client mocked. No live cluster.
"""

import sys
from unittest import mock

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

import controller  # noqa: E402


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def test_job_resources_for_size():
    assert controller.job_resources_for_size("2") == ("2", "4Gi")
    with pytest.raises(ValueError):
        controller.job_resources_for_size("99")


def test_build_job_manifest_labels_for_kueue():
    m = controller.build_job_manifest(
        priority="high", duration="short", cpu="2",
        namespace="ns-x", queue_name="q1", image="img:1",
    )
    labels = m["metadata"]["labels"]
    assert labels[controller.QUEUE_LABEL] == "q1"
    assert labels[controller.PRIORITY_LABEL] == "high-priority"
    assert labels["app"] == controller.APP_LABEL
    assert m["metadata"]["namespace"] == "ns-x"
    # Suspended so Kueue controls admission/preemption.
    assert m["spec"]["suspend"] is True
    c = m["spec"]["template"]["spec"]["containers"][0]
    assert c["image"] == "img:1"
    assert c["command"] == ["python", "jobwork.py"]
    # Real duration is wired into the busy-compute payload (short -> 60s).
    env = {e["name"]: e["value"] for e in c["env"]}
    assert env["JOB_DURATION_SECONDS"] == "60"
    assert c["resources"]["requests"]["cpu"] == "2"
    # Admitted work targets the CPU Spot ComputeClass.
    sel = m["spec"]["template"]["spec"]["nodeSelector"]
    assert sel["cloud.google.com/compute-class"] == "kueue-demo-cpu"


def test_build_job_manifest_low_priority_and_long():
    m = controller.build_job_manifest(
        priority="low", duration="long", cpu="3",
        namespace="ns", queue_name="q", image="i",
    )
    assert m["metadata"]["labels"][controller.PRIORITY_LABEL] == "low-priority"
    env = {e["name"]: e["value"] for e in m["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["JOB_DURATION_SECONDS"] == "600"


def test_build_job_manifest_rejects_bad_input():
    with pytest.raises(ValueError):
        controller.build_job_manifest(
            priority="urgent", duration="short", cpu="1",
            namespace="n", queue_name="q", image="i",
        )


def test_summarize_workload_states():
    admitted = {
        "metadata": {"name": "wl-a", "ownerReferences": [{"kind": "Job", "name": "job-a"}]},
        "spec": {"priority": 1000, "priorityClassName": "high-priority"},
        "status": {"conditions": [{"type": "Admitted", "status": "True"}]},
    }
    s = controller.summarize_workload(admitted)
    assert s["state"] == "admitted"
    assert s["job"] == "job-a"
    assert s["priority_class"] == "high-priority"

    preempted = {
        "metadata": {"name": "wl-b"},
        "spec": {"priority": 100, "priorityClassName": "low-priority"},
        "status": {"conditions": [
            {"type": "Admitted", "status": "True"},
            {"type": "Evicted", "status": "True", "reason": "Preempted"},
        ]},
    }
    s = controller.summarize_workload(preempted)
    assert s["state"] == "preempted"
    assert s["reason"] == "Preempted"

    pending = {
        "metadata": {"name": "wl-c"},
        "spec": {"priority": 100},
        "status": {"conditions": []},
    }
    assert controller.summarize_workload(pending)["state"] == "pending"


# --------------------------------------------------------------------------- #
# /submit with the k8s client mocked
# --------------------------------------------------------------------------- #
@pytest.fixture()
def client():
    return TestClient(controller.app)


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_options(client):
    body = client.get("/options").json()
    assert set(body["priorities"]) == {"high", "low"}
    assert "short" in body["durations"]


def test_submit_creates_job_with_mocked_client(client):
    fake_batch = mock.MagicMock()
    with mock.patch.object(controller, "_k8s", return_value=(fake_batch, mock.MagicMock(), mock.MagicMock())):
        r = client.post("/submit", json={"priority": "high", "duration": "short", "cpu": "2"})
    assert r.status_code == 200
    body = r.json()
    assert body["job"].startswith("kueue-demo-high-")
    # The Job was actually created with the Kueue labels.
    assert fake_batch.create_namespaced_job.called
    _, manifest = fake_batch.create_namespaced_job.call_args[0]
    assert manifest["metadata"]["labels"][controller.PRIORITY_LABEL] == "high-priority"


def test_submit_rejects_bad_priority(client):
    # 400 from validation BEFORE any cluster call.
    r = client.post("/submit", json={"priority": "nope", "duration": "short", "cpu": "2"})
    assert r.status_code == 400


def test_workloads_filters_to_demo_app(client):
    items = {
        "items": [
            {"metadata": {"name": "ours", "labels": {"app": controller.APP_LABEL}},
             "spec": {"priority": 100, "priorityClassName": "low-priority"},
             "status": {"conditions": [{"type": "Admitted", "status": "True"}]}},
            {"metadata": {"name": "someone-else", "labels": {"app": "other"}},
             "spec": {}, "status": {}},
        ]
    }
    fake_custom = mock.MagicMock()
    fake_custom.list_namespaced_custom_object.return_value = items
    with mock.patch.object(controller, "_k8s", return_value=(mock.MagicMock(), mock.MagicMock(), fake_custom)):
        r = client.get("/workloads")
    assert r.status_code == 200
    names = [w["workload"] for w in r.json()["workloads"]]
    assert names == ["ours"]
