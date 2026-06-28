"""FastAPI controller for the Kueue batch-queueing demo.

This process is the demo's data plane. It:

* **submits** CPU batch Jobs into a Kueue ``LocalQueue`` — each Job is labelled
  ``kueue.x-k8s.io/queue-name`` (which queue) and
  ``kueue.x-k8s.io/priority-class`` (high/low, driving preemption), and runs a
  REAL busy-compute payload (``app/jobwork.py``) for the chosen duration;
* reports **Workload** state (admitted / pending / PREEMPTED) by reading the
  Kueue ``Workload`` objects Kueue creates for each Job;
* lists the Job **pods** with their node placement, start time, and elapsed
  runtime, plus live **quota usage** from the ClusterQueue.

Data-plane only: the browser calls this directly via the Gateway IP, so CORS is
mandatory. The Hub's JWT-protected control plane lives in ``hub_router.py``.

The pure helpers (``build_job_manifest``, ``job_resources_for_size``,
``summarize_workload``) are import-safe and unit-tested with the k8s client mocked.
"""

from __future__ import annotations

import logging
import os
import pathlib
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

logger = logging.getLogger("kueue-controller")
logging.basicConfig(level=logging.INFO)

# --------------------------------------------------------------------------- #
# Configuration (all namespace-portable; nothing hardcodes "default").
# --------------------------------------------------------------------------- #
POD_NAMESPACE = os.environ.get("POD_NAMESPACE", "default")
LOCAL_QUEUE_NAME = os.environ.get("LOCAL_QUEUE_NAME", "kueue-demo-queue")
JOB_IMAGE = os.environ.get("JOB_IMAGE", "kueue-demo:latest")
APP_LABEL = "kueue-demo"  # tags every Job/pod we create so we can list & clear them

# Kueue label keys (stable API).
QUEUE_LABEL = "kueue.x-k8s.io/queue-name"
PRIORITY_LABEL = "kueue.x-k8s.io/priority-class"

# Map the UI's coarse choices onto concrete values.
PRIORITY_CLASSES = {"high": "high-priority", "low": "low-priority"}
# CPU size -> (cpu request, memory request). Memory tracks CPU so quota math is simple.
CPU_SIZES = {
    "1": ("1", "2Gi"),
    "2": ("2", "4Gi"),
    "3": ("3", "6Gi"),
}
# Duration presets (seconds of real busy-compute).
DURATIONS = {"short": 60, "long": 600}

app = FastAPI(title="Kueue Batch Queue Controller")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# Serve the playroom UI ourselves so the feature is fully functional STANDALONE
# (the Hub serves the same UI at /<slug>/, but standalone there is no Hub). The
# UI calls its own API same-origin (its /api/features/kueue/config probe 404s here
# and it falls back to LIVE against this origin). Mirrors the Hub static layout
# (/static/features/kueue/...) so index.html's asset paths resolve in both.
_FRONTEND = pathlib.Path(__file__).resolve().parent / "frontend"
if _FRONTEND.is_dir():
    app.mount(
        "/static/features/kueue",
        StaticFiles(directory=str(_FRONTEND)),
        name="assets",
    )

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(str(_FRONTEND / "index.html"))

    @app.middleware("http")
    async def _no_cache_ui(request, call_next):
        resp = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class SubmitRequest(BaseModel):
    priority: str = Field(default="low")     # "high" | "low"
    duration: str = Field(default="short")   # "short" | "long"
    cpu: str = Field(default="2")            # "1" | "2" | "3"


# --------------------------------------------------------------------------- #
# Pure helpers (no cluster access — unit tested)
# --------------------------------------------------------------------------- #
def job_resources_for_size(cpu: str) -> tuple[str, str]:
    """Map a UI CPU choice to (cpu_request, mem_request). Raises on unknown."""
    if cpu not in CPU_SIZES:
        raise ValueError(f"unknown cpu size {cpu!r} (expected one of {list(CPU_SIZES)})")
    return CPU_SIZES[cpu]


def build_job_manifest(
    *,
    priority: str,
    duration: str,
    cpu: str,
    namespace: str,
    queue_name: str,
    image: str,
    name: str | None = None,
) -> dict:
    """Construct a batch/v1 Job dict labelled for Kueue admission + preemption.

    The labels are the contract with Kueue:
      * ``kueue.x-k8s.io/queue-name``   -> route the Workload through this LocalQueue
      * ``kueue.x-k8s.io/priority-class`` -> WorkloadPriorityClass driving preemption
    ``suspend: true`` hands scheduling control to Kueue: the Job stays suspended
    until Kueue admits its Workload (and is re-suspended / deleted on preemption).
    """
    if priority not in PRIORITY_CLASSES:
        raise ValueError(f"unknown priority {priority!r} (expected {list(PRIORITY_CLASSES)})")
    if duration not in DURATIONS:
        raise ValueError(f"unknown duration {duration!r} (expected {list(DURATIONS)})")
    cpu_req, mem_req = job_resources_for_size(cpu)
    seconds = DURATIONS[duration]
    priority_class = PRIORITY_CLASSES[priority]
    job_name = name or f"{APP_LABEL}-{priority}-{uuid.uuid4().hex[:6]}"

    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job_name,
            "namespace": namespace,
            "labels": {
                QUEUE_LABEL: queue_name,
                PRIORITY_LABEL: priority_class,
                "app": APP_LABEL,
                "kueue-demo/priority": priority,
                "kueue-demo/duration": duration,
            },
        },
        "spec": {
            # Kueue admits Workloads, not Jobs directly; suspend hands it control.
            "suspend": True,
            # Don't let the Job retry forever; preemption is a clean stop.
            "backoffLimit": 0,
            "completions": 1,
            "parallelism": 1,
            # Clean up finished Jobs after a few minutes so the board stays tidy.
            "ttlSecondsAfterFinished": 300,
            "template": {
                "metadata": {"labels": {"app": APP_LABEL}},
                "spec": {
                    "restartPolicy": "Never",
                    "terminationGracePeriodSeconds": 10,
                    # Land admitted work on the CPU Spot ComputeClass nodes.
                    "nodeSelector": {"cloud.google.com/compute-class": "kueue-demo-cpu"},
                    "tolerations": [
                        {
                            "key": "cloud.google.com/compute-class",
                            "operator": "Equal",
                            "value": "kueue-demo-cpu",
                            "effect": "NoSchedule",
                        }
                    ],
                    "containers": [
                        {
                            "name": "busy-compute",
                            "image": image,
                            "command": ["python", "jobwork.py"],
                            "env": [{"name": "JOB_DURATION_SECONDS", "value": str(seconds)}],
                            "resources": {
                                "requests": {"cpu": cpu_req, "memory": mem_req},
                                "limits": {"cpu": cpu_req, "memory": mem_req},
                            },
                        }
                    ],
                },
            },
        },
    }


def summarize_workload(wl: dict) -> dict:
    """Reduce a Kueue Workload object to the UI's status fields.

    Status mapping (from Workload .status.conditions):
      * Admitted=True                  -> "admitted"
      * Evicted=True (Preempted reason)-> "preempted"
      * QuotaReserved but not Admitted -> "pending"
      * otherwise                      -> "pending"
    """
    meta = wl.get("metadata", {})
    spec = wl.get("spec", {})
    status = wl.get("status", {})
    conditions = {c.get("type"): c for c in status.get("conditions", [])}

    def _true(name: str) -> bool:
        return conditions.get(name, {}).get("status") == "True"

    state = "pending"
    reason = None
    if _true("Finished"):
        state = "finished"
    elif _true("Evicted"):
        state = "preempted"
        reason = conditions.get("Evicted", {}).get("reason")
    elif _true("Admitted"):
        state = "admitted"
    elif _true("QuotaReserved"):
        state = "admitted"

    # The Job this Workload wraps (ownerReference) and its requested priority.
    owner = next(
        (o.get("name") for o in meta.get("ownerReferences", []) if o.get("kind") == "Job"),
        meta.get("name"),
    )
    # Kueue copies neither the Job's priority-class label nor a class NAME onto the
    # Workload spec (only the numeric spec.priority), so when priorityClassName is
    # absent, fall back to the owning Job's name — the controller always stamps it
    # as kueue-demo-<priority>-<hash> (build_job_manifest).
    priority_class = spec.get("priorityClassName") or spec.get("priorityClassSource")
    if not priority_class and owner:
        for key, cls in PRIORITY_CLASSES.items():
            if f"-{key}-" in owner or owner.endswith(f"-{key}"):
                priority_class = cls
                break
    return {
        "workload": meta.get("name"),
        "job": owner,
        "state": state,
        "reason": reason,
        "priority": spec.get("priority"),
        "priority_class": priority_class,
    }


# --------------------------------------------------------------------------- #
# Kubernetes client (lazy; never imported at module import for offline tests)
# --------------------------------------------------------------------------- #
def _k8s():
    """Return (BatchV1Api, CoreV1Api, CustomObjectsApi), loading config lazily."""
    from kubernetes import client, config

    try:
        config.load_incluster_config()
    except Exception:
        config.load_kube_config()
    return client.BatchV1Api(), client.CoreV1Api(), client.CustomObjectsApi()


KUEUE_GROUP = "kueue.x-k8s.io"
KUEUE_VERSION = "v1beta2"


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/options")
def options() -> dict:
    """Static choices for the submit form (priorities, durations, cpu sizes)."""
    return {
        "priorities": list(PRIORITY_CLASSES),
        "durations": {k: v for k, v in DURATIONS.items()},
        "cpu_sizes": list(CPU_SIZES),
        "queue": LOCAL_QUEUE_NAME,
    }


@app.post("/submit")
def submit(req: SubmitRequest) -> dict:
    """Submit one batch Job into the LocalQueue with the chosen priority/size."""
    try:
        manifest = build_job_manifest(
            priority=req.priority,
            duration=req.duration,
            cpu=req.cpu,
            namespace=POD_NAMESPACE,
            queue_name=LOCAL_QUEUE_NAME,
            image=JOB_IMAGE,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        batch, _, _ = _k8s()
        batch.create_namespaced_job(POD_NAMESPACE, manifest)
    except Exception as exc:  # surface, don't swallow
        logger.error("job submit failed", exc_info=True)
        raise HTTPException(status_code=503, detail=f"cannot submit job: {exc}")
    return {"job": manifest["metadata"]["name"], "namespace": POD_NAMESPACE}


@app.get("/workloads")
def workloads() -> dict:
    """List the demo's Kueue Workloads with admission / preemption status."""
    try:
        _, _, custom = _k8s()
        resp = custom.list_namespaced_custom_object(
            KUEUE_GROUP, KUEUE_VERSION, POD_NAMESPACE, "workloads"
        )
    except Exception as exc:
        logger.error("listing workloads failed", exc_info=True)
        raise HTTPException(status_code=503, detail=f"cannot list workloads: {exc}")

    out = []
    for wl in resp.get("items", []):
        summary = summarize_workload(wl)
        labels = wl.get("metadata", {}).get("labels", {})
        job = summary.get("job") or ""
        # Identify our demo workloads. Kueue does NOT copy the Job's `app` label
        # onto the Workload (only kueue.x-k8s.io/job-uid), so match on the owning
        # Job's name prefix; still honor the app label in case a future Kueue
        # config (labelKeysToCopy) propagates it.
        if labels.get("app") != APP_LABEL and not job.startswith(f"{APP_LABEL}-"):
            continue
        out.append(summary)
    return {"namespace": POD_NAMESPACE, "workloads": out}


@app.get("/pods")
def pods() -> dict:
    """List the demo's Job pods with node placement + elapsed runtime."""
    try:
        _, core, _ = _k8s()
        resp = core.list_namespaced_pod(POD_NAMESPACE, label_selector=f"app={APP_LABEL}")
    except Exception as exc:
        logger.error("listing pods failed", exc_info=True)
        raise HTTPException(status_code=503, detail=f"cannot list pods: {exc}")

    now = datetime.now(timezone.utc)
    out = []
    for p in resp.items:
        start = p.status.start_time
        elapsed = int((now - start).total_seconds()) if start else None
        out.append(
            {
                "pod_name": p.metadata.name,
                "job": (p.metadata.labels or {}).get("job-name"),
                "node": p.spec.node_name,
                "status": p.status.phase,
                "started": start.isoformat() if start else None,
                "elapsed_seconds": elapsed,
            }
        )
    return {"namespace": POD_NAMESPACE, "pods": out}


@app.get("/quota")
def quota() -> dict:
    """Report the ClusterQueue's fixed nominal quota and current usage."""
    try:
        _, _, custom = _k8s()
        # The LocalQueue mirrors the ClusterQueue's flavor usage in its status.
        lq = custom.get_namespaced_custom_object(
            KUEUE_GROUP, KUEUE_VERSION, POD_NAMESPACE, "localqueues", LOCAL_QUEUE_NAME
        )
    except Exception as exc:
        logger.error("reading quota failed", exc_info=True)
        raise HTTPException(status_code=503, detail=f"cannot read quota: {exc}")

    status = lq.get("status", {})
    return {
        "queue": LOCAL_QUEUE_NAME,
        "cluster_queue": lq.get("spec", {}).get("clusterQueue"),
        "admitted_workloads": status.get("admittedWorkloads"),
        "pending_workloads": status.get("pendingWorkloads"),
        "flavors_usage": status.get("flavorsReservation", status.get("flavorUsage")),
    }


@app.delete("/jobs")
def clear_jobs() -> dict:
    """Delete all demo Jobs (and their pods) to reset the board."""
    try:
        batch, _, _ = _k8s()
        batch.delete_collection_namespaced_job(
            POD_NAMESPACE,
            label_selector=f"app={APP_LABEL}",
            propagation_policy="Background",
        )
    except Exception as exc:
        logger.error("clearing jobs failed", exc_info=True)
        raise HTTPException(status_code=503, detail=f"cannot clear jobs: {exc}")
    return {"status": "cleared", "namespace": POD_NAMESPACE}
