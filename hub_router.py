"""Hub data-plane router for the Kueue batch-queueing feature.

Mounted by the Hub at ``/api/features/kueue`` behind the admin JWT. Kept thin:

* **LIVE** — the browser talks to the controller directly via the Gateway IP
  (CORS) for the heavy data plane (submit / workloads / pods / quota / clear).
  This router only resolves ``/config`` (the gateway IP) using the shared SDK.
* **MOCK** — no cluster exists. Per the feature's NO-MOCKING rule, MOCK NEVER
  fabricates jobs, nodes, runtimes, or quota usage. It returns HONEST empty /
  "not connected to a cluster" states so the playroom imports and renders offline
  without inventing fake data. There is nothing real to compute offline, so the
  data-plane endpoints report that plainly.
"""

from __future__ import annotations

from fastapi import APIRouter

# --------------------------------------------------------------------------- #
# Shared SDK — imported tolerantly so the router also loads standalone/in tests.
# --------------------------------------------------------------------------- #
try:  # pragma: no cover - exercised inside the Hub container
    from showcase_admin.app import config, database, k8s_client
except Exception:  # standalone / unit tests
    config = None
    database = None
    k8s_client = None


def _mode() -> str:
    """Resolve the current mode *dynamically* (never cache at import).

    The Hub's test harness sets ``MODE=MOCK`` after this module is imported, so a
    value captured at import time would go stale. Prefer the live ``config.MODE``
    when the Hub SDK is present, else the ``MODE`` env var, defaulting to MOCK.
    """
    if config is not None:
        return getattr(config, "MODE", "MOCK")
    import os

    return os.environ.get("MODE", "MOCK").upper()


FEATURE = "kueue"
GATEWAY_NAME = "kueue-demo-gw"

# Honest "offline" payloads — empty, never fabricated. The frontend renders these
# as an explicit "not connected to a cluster" state.
_OFFLINE_NOTE = "MOCK mode: not connected to a cluster — no real jobs to show."

router = APIRouter()


@router.get("/config")
async def config_endpoint() -> dict:
    if _mode() == "MOCK":
        return {"mode": "MOCK", "gateway_ip": None, "note": _OFFLINE_NOTE}

    # LIVE: resolve the feature's deployed namespace, then this feature's Gateway IP
    # so the browser can reach the controller's data plane directly (CORS).
    gateway_ip = None
    if database is not None and k8s_client is not None:
        db = next(database.get_db())
        try:
            ns = database.get_feature_namespace(db, FEATURE)
            gateway_ip = await k8s_client.get_gateway_ip(ns, GATEWAY_NAME)
        except Exception:
            gateway_ip = None
        finally:
            db.close()
    return {"mode": "LIVE", "gateway_ip": gateway_ip}


@router.get("/options")
def options() -> dict:
    """Static submit-form choices — safe to serve offline (no fabricated state)."""
    return {
        "priorities": ["high", "low"],
        "durations": {"short": 60, "long": 600},
        "cpu_sizes": ["1", "2", "3"],
        "queue": "kueue-demo-queue",
    }


# --------------------------------------------------------------------------- #
# Data-plane endpoints. In MOCK there is no cluster and we refuse to fabricate
# jobs/nodes/quota, so these return honest empty state. In LIVE they are served
# by the controller at the Gateway IP, not here.
# --------------------------------------------------------------------------- #
@router.get("/workloads")
def workloads() -> dict:
    return {"namespace": None, "workloads": [], "note": _OFFLINE_NOTE}


@router.get("/pods")
def pods() -> dict:
    return {"namespace": None, "pods": [], "note": _OFFLINE_NOTE}


@router.get("/quota")
def quota() -> dict:
    return {
        "queue": "kueue-demo-queue",
        "cluster_queue": "kueue-demo-clusterqueue",
        "admitted_workloads": None,
        "pending_workloads": None,
        "flavors_usage": None,
        "note": _OFFLINE_NOTE,
    }
