"""Manifest + descriptor checks: every ${VAR} is declared, refs are consistent,
and rendered manifests are valid YAML. Mirrors the Hub pre-merge checklist so a
broken descriptor fails locally.
"""

import os
import pathlib
import re

import pytest

yaml = pytest.importorskip("yaml")

ROOT = pathlib.Path(__file__).resolve().parents[1]
INFRA = ROOT / "infra"
CLUSTER = ROOT / "cluster"

# Hub-provided variables (see feature.md §3).
HUB_VARS = {
    "NAMESPACE", "PROJECT_NAME", "REGION", "ARTIFACT_REGISTRY_REPO",
    "GOOGLE_GENAI_USE_VERTEXAI", "OPENAI_API_BASE", "GCS_MODEL_BUCKET",
}

SAMPLE_ENV = {
    "NAMESPACE": "gke-showcase-kueue",
    "PROJECT_NAME": "demo-project",
    "REGION": "us-central1",
    "ARTIFACT_REGISTRY_REPO": "kueue-demo",
    "KUEUE_IMAGE": "us-central1-docker.pkg.dev/demo-project/kueue-demo/kueue-demo:latest",
    "LOCAL_QUEUE_NAME": "kueue-demo-queue",
    "CLUSTER_QUEUE_NAME": "kueue-demo-clusterqueue",
}


def _feature():
    return yaml.safe_load((ROOT / "feature.yaml").read_text())


def _render(text: str) -> str:
    """Mirror the scripts' os.path.expandvars rendering with the sample env."""
    old = dict(os.environ)
    try:
        os.environ.update(SAMPLE_ENV)
        return os.path.expandvars(text)
    finally:
        os.environ.clear()
        os.environ.update(old)


def test_feature_yaml_required_keys():
    f = _feature()
    for key in ("name", "paths", "deployment_name", "gateway"):
        assert key in f, f"feature.yaml missing {key}"
    assert f["name"] == "kueue"
    assert f["gateway"]["name"] == "kueue-demo-gw"
    assert f["hub_router"] == "hub_router:router"


def test_exactly_one_ui_model():
    f = _feature()
    has_playroom = "frontend_dir" in f["paths"] and "playroom_slug" in f["paths"]
    has_linkout = "entrypoint_service" in f
    assert has_playroom and not has_linkout, "must be hub-hosted playroom only"


def test_every_var_is_hub_standard_or_defaulted():
    f = _feature()
    declared = HUB_VARS | set(f.get("template_defaults", {}).keys())
    pattern = re.compile(r"\$\{([A-Z_]+)\}")
    missing = {}
    for path in list(INFRA.glob("*.yaml")) + list(CLUSTER.rglob("*.yaml")):
        for var in pattern.findall(path.read_text()):
            if var not in declared:
                missing.setdefault(path.name, set()).add(var)
    assert not missing, f"undeclared template vars: {missing}"


def test_each_infra_manifest_renders_to_valid_yaml():
    for path in INFRA.glob("*.yaml"):
        rendered = _render(path.read_text())
        # Every ${VAR} must be gone after rendering with the sample env.
        assert "${" not in rendered, f"{path.name}: unresolved var after render"
        docs = list(yaml.safe_load_all(rendered))
        assert docs, f"{path.name}: rendered to no documents"
        for doc in docs:
            if doc:
                assert "kind" in doc, f"{path.name}: a doc has no kind"


def test_deployment_name_matches_descriptor():
    f = _feature()
    name = f["deployment_name"]
    found = False
    for path in INFRA.glob("*.yaml"):
        for doc in yaml.safe_load_all(path.read_text()):
            if doc and doc.get("kind") == "Deployment" and doc["metadata"]["name"] == name:
                found = True
    assert found, f"no Deployment named {name}"


def test_gateway_name_matches_descriptor():
    f = _feature()
    gw = f["gateway"]["name"]
    names = []
    for path in INFRA.glob("*.yaml"):
        for doc in yaml.safe_load_all(path.read_text()):
            if doc and doc.get("kind") == "Gateway":
                names.append(doc["metadata"]["name"])
    assert gw in names, f"Gateway {gw} not found (found {names})"


def test_no_hardcoded_default_namespace():
    """Namespaced resources must template their namespace, never literally 'default'."""
    for path in INFRA.glob("*.yaml"):
        for doc in yaml.safe_load_all(path.read_text()):
            if not doc:
                continue
            ns = doc.get("metadata", {}).get("namespace")
            if ns is not None:
                assert ns == "${NAMESPACE}", f"{path.name}: hardcoded namespace {ns}"


def test_httproute_points_at_controller():
    route = None
    for doc in yaml.safe_load_all((INFRA / "http-route.yaml").read_text()):
        if doc and doc.get("kind") == "HTTPRoute":
            route = doc
    assert route is not None
    backends = {
        b["name"]
        for rule in route["spec"]["rules"]
        for b in rule.get("backendRefs", [])
    }
    assert backends == {"kueue-controller"}


def test_service_name_consistent():
    svc = None
    for doc in yaml.safe_load_all((INFRA / "service.yaml").read_text()):
        if doc and doc.get("kind") == "Service":
            svc = doc
    assert svc is not None
    assert svc["metadata"]["name"] == "kueue-controller"
    assert svc["spec"]["ports"][0]["targetPort"] == 8000


def test_localqueue_references_clusterqueue():
    lq = None
    for doc in yaml.safe_load_all((INFRA / "localqueue.yaml").read_text()):
        if doc and doc.get("kind") == "LocalQueue":
            lq = doc
    assert lq is not None
    assert lq["spec"]["clusterQueue"] == "kueue-demo-clusterqueue"


def test_clusterqueue_has_fixed_quota_and_preemption():
    """The ClusterQueue must declare a fixed CPU quota and enable preemption."""
    cq = None
    for doc in yaml.safe_load_all((INFRA / "queue-config.yaml").read_text()):
        if doc and doc.get("kind") == "ClusterQueue":
            cq = doc
    assert cq is not None
    assert cq["spec"]["preemption"]["withinClusterQueue"] == "LowerPriority"
    cpu = None
    for rg in cq["spec"]["resourceGroups"]:
        for fl in rg["flavors"]:
            for res in fl["resources"]:
                if res["name"] == "cpu":
                    cpu = res["nominalQuota"]
    assert cpu == "6", f"expected fixed 6 CPU nominalQuota, got {cpu}"


def test_priority_classes_present():
    names = {}
    for doc in yaml.safe_load_all((INFRA / "queue-config.yaml").read_text()):
        if doc and doc.get("kind") == "WorkloadPriorityClass":
            names[doc["metadata"]["name"]] = doc["value"]
    assert "high-priority" in names and "low-priority" in names
    assert names["high-priority"] > names["low-priority"]


def test_resourceflavor_maps_to_computeclass():
    rf = None
    for doc in yaml.safe_load_all((INFRA / "queue-config.yaml").read_text()):
        if doc and doc.get("kind") == "ResourceFlavor":
            rf = doc
    assert rf is not None
    assert rf["spec"]["nodeLabels"]["cloud.google.com/compute-class"] == "kueue-demo-cpu"

    cc = None
    for doc in yaml.safe_load_all((CLUSTER / "cpu-computeclass.yaml").read_text()):
        if doc and doc.get("kind") == "ComputeClass":
            cc = doc
    assert cc is not None
    assert cc["metadata"]["name"] == "kueue-demo-cpu"
    assert cc["spec"]["nodePoolAutoCreation"]["enabled"] is True


def test_cluster_kustomization_pins_kueue_version():
    # The operator is installed from the self-contained Kueue release manifest,
    # referenced directly by the top-level cluster kustomization (no subdir,
    # no config/default, no cert-manager). The version is pinned in the URL.
    text = (CLUSTER / "kustomization.yaml").read_text()
    assert "kubernetes-sigs/kueue/releases/download/v" in text
    # A concrete pinned tag, not a moving ref.
    assert re.search(r"releases/download/v\d+\.\d+\.\d+/manifests\.yaml", text), \
        "Kueue operator must pin a real release version"
