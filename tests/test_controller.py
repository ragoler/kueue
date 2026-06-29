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


def test_summarize_workload_priority_from_owner_when_class_absent():
    # Real Kueue copies neither the priority-class label nor a class NAME onto the
    # Workload (only the numeric spec.priority), so priority_class must be derived
    # from the owning Job's name.
    high = {
        "metadata": {"name": "job-x-46108",
                     "ownerReferences": [{"kind": "Job", "name": "kueue-demo-high-abc123"}]},
        "spec": {"priority": 1000},  # no priorityClassName / priorityClassSource
        "status": {"conditions": [{"type": "Admitted", "status": "True"}]},
    }
    s = controller.summarize_workload(high)
    assert s["state"] == "admitted"
    assert s["priority_class"] == "high-priority"

    low = {
        "metadata": {"name": "job-y-9f0",
                     "ownerReferences": [{"kind": "Job", "name": "kueue-demo-low-def456"}]},
        "spec": {"priority": 100},
        "status": {"conditions": [{"type": "Admitted", "status": "True"},
                                  {"type": "Evicted", "status": "True", "reason": "Preempted"}]},
    }
    assert controller.summarize_workload(low)["priority_class"] == "low-priority"

    medium = {
        "metadata": {"name": "job-z-1a2",
                     "ownerReferences": [{"kind": "Job", "name": "kueue-demo-medium-aaa999"}]},
        "spec": {"priority": 500},
        "status": {"conditions": [{"type": "Admitted", "status": "True"}]},
    }
    assert controller.summarize_workload(medium)["priority_class"] == "medium-priority"


def test_summarize_workload_extracts_cpu_and_duration():
    # CPU request + busy-compute duration come from the Workload's first podSet.
    wl = {
        "metadata": {"name": "wl", "ownerReferences": [{"kind": "Job", "name": "kueue-demo-low-x"}]},
        "spec": {
            "priority": 100,
            "podSets": [{"template": {"spec": {"containers": [{
                "resources": {"requests": {"cpu": "3", "memory": "6Gi"}},
                "env": [{"name": "JOB_DURATION_SECONDS", "value": "600"}],
            }]}}}],
        },
        "status": {"conditions": [{"type": "Admitted", "status": "True"}]},
    }
    s = controller.summarize_workload(wl)
    assert s["cpu"] == "3"
    assert s["duration_seconds"] == 600

    # Missing podSets -> graceful None, None.
    bare = {"metadata": {"name": "b"}, "spec": {"priority": 100}, "status": {}}
    s2 = controller.summarize_workload(bare)
    assert s2["cpu"] is None and s2["duration_seconds"] is None


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
    assert set(body["priorities"]) == {"high", "medium", "low"}
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
            # Realistic Kueue Workload: no `app` label (only job-uid), identified
            # by its owning Job's name prefix.
            {"metadata": {"name": "ours",
                          "labels": {"kueue.x-k8s.io/job-uid": "uid-1"},
                          "ownerReferences": [{"kind": "Job", "name": "kueue-demo-low-aaa111"}]},
             "spec": {"priority": 100},
             "status": {"conditions": [{"type": "Admitted", "status": "True"}]}},
            # Legacy path still works if a Kueue config copies the app label.
            {"metadata": {"name": "ours-labelled", "labels": {"app": controller.APP_LABEL},
                          "ownerReferences": [{"kind": "Job", "name": "other-thing"}]},
             "spec": {"priority": 100, "priorityClassName": "low-priority"},
             "status": {"conditions": [{"type": "Admitted", "status": "True"}]}},
            # Someone else's workload: no app label, non-demo owner -> excluded.
            {"metadata": {"name": "someone-else", "labels": {"app": "other"},
                          "ownerReferences": [{"kind": "Job", "name": "unrelated-job"}]},
             "spec": {}, "status": {}},
        ]
    }
    fake_custom = mock.MagicMock()
    fake_custom.list_namespaced_custom_object.return_value = items
    with mock.patch.object(controller, "_k8s", return_value=(mock.MagicMock(), mock.MagicMock(), fake_custom)):
        r = client.get("/workloads")
    assert r.status_code == 200
    names = [w["workload"] for w in r.json()["workloads"]]
    assert names == ["ours", "ours-labelled"]


def test_clear_finished_jobs_only_deletes_finished(client):
    # Build fake Job objects with varied completion state.
    def job(name, *, completion_time=None, failed=None, conditions=None):
        st = mock.MagicMock()
        st.completion_time = completion_time
        st.failed = failed
        st.conditions = conditions
        j = mock.MagicMock()
        j.metadata.name = name
        j.status = st
        return j

    def cond(t, s):
        c = mock.MagicMock(); c.type = t; c.status = s
        return c

    jobs = mock.MagicMock()
    jobs.items = [
        job("done-complete", completion_time="t"),
        job("done-failed", conditions=[cond("Failed", "True")]),
        job("running"),  # no completion -> kept
        job("complete-cond", conditions=[cond("Complete", "True")]),
    ]
    fake_batch = mock.MagicMock()
    fake_batch.list_namespaced_job.return_value = jobs
    with mock.patch.object(controller, "_k8s", return_value=(fake_batch, mock.MagicMock(), mock.MagicMock())):
        r = client.delete("/jobs/finished")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 3
    assert set(body["deleted"]) == {"done-complete", "done-failed", "complete-cond"}
    deleted = {c.args[0] for c in fake_batch.delete_namespaced_job.call_args_list}
    assert "running" not in deleted
