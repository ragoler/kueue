"""MODE=MOCK hub_router tests — the playroom imports offline AND, critically,
NEVER fabricates jobs/nodes/quota. MOCK must return honest empty state.
"""

import os

import pytest

os.environ["MODE"] = "MOCK"

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import hub_router  # noqa: E402


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(hub_router.router, prefix="/api/features/kueue")
    return TestClient(app)


def test_config_mock(client):
    body = client.get("/api/features/kueue/config").json()
    assert body["mode"] == "MOCK"
    assert body["gateway_ip"] is None  # nothing to link to offline
    assert "not connected" in body["note"].lower()


def test_options_safe_offline(client):
    body = client.get("/api/features/kueue/options").json()
    assert set(body["priorities"]) == {"high", "low"}


def test_workloads_is_honest_empty(client):
    body = client.get("/api/features/kueue/workloads").json()
    # NO fabricated jobs — the critical NO-MOCKING rule.
    assert body["workloads"] == []
    assert "not connected" in body["note"].lower()


def test_pods_is_honest_empty(client):
    body = client.get("/api/features/kueue/pods").json()
    assert body["pods"] == []  # no fake nodes / runtimes


def test_quota_is_honest_empty(client):
    body = client.get("/api/features/kueue/quota").json()
    # Honest "unknown" usage, never a fabricated number.
    assert body["admitted_workloads"] is None
    assert body["pending_workloads"] is None
    assert body["flavors_usage"] is None
