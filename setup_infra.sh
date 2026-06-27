#!/usr/bin/env bash
# Standalone provisioning for the Kueue Batch Queue demo: GKE cluster + cluster-
# scoped prerequisites (Kueue operator + CRDs, CPU Spot ComputeClass, the fixed-
# quota ClusterQueue + ResourceFlavor + WorkloadPriorityClasses). Run
# deploy_app.sh after this to build/push the image and deploy the controller +
# LocalQueue.
#
# The Hub IGNORES this file — it assumes a live cluster, installs cluster/ during
# build_infra.sh, and applies infra/ per deploy.
set -e

# --- Load configuration ----------------------------------------------------
if [ -f .env ]; then
  source .env
else
  echo "Error: .env file not found. Create one with: cp .env.example .env"
  exit 1
fi

for cmd in gcloud kubectl python3; do
  if ! command -v "$cmd" &>/dev/null; then
    echo "Error: $cmd is required but not installed."
    exit 1
  fi
done

REGION="${REGION:-${ZONE%-*}}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Mode dispatch ---------------------------------------------------------
#   (no flag)         create cluster + prerequisites
#   --delete          remove cluster-scoped prereqs (keep the cluster)
#   --delete-cluster  the above, plus delete the GKE cluster
MODE="create"
case "${1:-}" in
  --delete)         MODE="delete" ;;
  --delete-cluster) MODE="delete-cluster" ;;
  -h|--help)        echo "Usage: $0 [--delete | --delete-cluster]"; exit 0 ;;
  "")               MODE="create" ;;
  *) echo "Unknown argument: $1 (use --delete, --delete-cluster, or no flag)"; exit 1 ;;
esac

cluster_exists() {
  gcloud container clusters describe "${CLUSTER_NAME}" \
    --zone="${ZONE}" --project="${PROJECT_ID}" &>/dev/null
}

if [ "$MODE" = "delete" ] || [ "$MODE" = "delete-cluster" ]; then
  if cluster_exists; then
    gcloud container clusters get-credentials "${CLUSTER_NAME}" --zone="${ZONE}" --project="${PROJECT_ID}"
    echo "=== Removing cluster-scoped prerequisites ==="
    # The cluster/ kustomization (Kueue release manifest + ComputeClass), mirroring
    # the Hub's `apply -k cluster/`. The fixed-quota queue config lives in infra/
    # (applied at deploy time), so remove those cluster-scoped CRs first.
    # --ignore-not-found so partial state tears down cleanly.
    kubectl delete -f "${ROOT}/infra/queue-config.yaml" --ignore-not-found || true
    kubectl delete -k "${ROOT}/cluster" --ignore-not-found || true
  else
    echo "Cluster ${CLUSTER_NAME} does not exist; nothing to remove."
  fi
  if [ "$MODE" = "delete-cluster" ] && cluster_exists; then
    echo "=== Deleting GKE cluster ${CLUSTER_NAME} (several minutes) ==="
    gcloud container clusters delete "${CLUSTER_NAME}" --zone="${ZONE}" --project="${PROJECT_ID}" --quiet || true
  fi
  echo "=== Teardown complete ==="
  exit 0
fi

# --- Step 1: Create the GKE cluster ---------------------------------------
# Node Auto-Provisioning is required so the kueue-demo-cpu ComputeClass can
# create Spot node pools on demand for the admitted Job pods. The small default
# pool hosts the Kueue operator and the controller.
echo "=== Step 1: Creating GKE cluster ${CLUSTER_NAME} (${ZONE}) ==="
if cluster_exists; then
  echo "Cluster ${CLUSTER_NAME} already exists. Skipping creation."
else
  gcloud container clusters create "${CLUSTER_NAME}" \
    --project="${PROJECT_ID}" \
    --zone="${ZONE}" \
    --machine-type="${MACHINE_TYPE}" \
    --num-nodes="${NUM_NODES}" \
    --gateway-api=standard \
    --enable-autoprovisioning \
    --min-cpu 0 --max-cpu "${MAX_CPU:-200}" \
    --min-memory 0 --max-memory "${MAX_MEMORY:-800}"
fi

echo "=== Step 2: Getting cluster credentials ==="
gcloud container clusters get-credentials "${CLUSTER_NAME}" --project="${PROJECT_ID}" --zone="${ZONE}"

# --- Step 3: Cluster-scoped prerequisites (one kustomization) -------------
# The top-level kustomization composes the self-contained Kueue release manifest
# (operator + CRDs + kueue-system Namespace + internal webhook cert) and the CPU
# Spot ComputeClass — the exact same dir and command the Hub's build_infra.sh
# runs, so standalone and Hub never drift. Server-side apply because Kueue's CRDs
# exceed the client-side 256KB annotation limit. This bundle has NO Kueue CRs, so
# there is no CRD-registration race; the fixed-quota queue config is applied later
# by deploy_app.sh (infra/), once the webhook below is ready. A short retry rides
# out transient apiserver hiccups on a freshly created cluster.
echo "=== Step 3: Installing cluster prerequisites (Kueue operator + CRDs + CPU ComputeClass) ==="
apply_cluster() {
  kubectl apply --server-side --force-conflicts -k "${ROOT}/cluster"
}
ok=""
for attempt in 1 2 3; do
  if apply_cluster; then
    ok="1"; break
  fi
  echo "    cluster apply attempt ${attempt} failed; retrying in 10s..."
  sleep 10
done
if [ -z "${ok}" ]; then
  echo "Error: cluster prerequisites did not apply after retries. Inspect with:"
  echo "    kubectl get crd | grep kueue"
  exit 1
fi

# Wait for the operator: its validating webhook must be serving before deploy_app.sh
# creates the ResourceFlavor/ClusterQueue/WorkloadPriorityClasses (infra/), or those
# CR applies fail with a webhook connection error.
echo "=== Step 4: Waiting for the Kueue controller (webhook) to be ready ==="
kubectl -n kueue-system rollout status deploy/kueue-controller-manager --timeout=300s

echo "=== Setup complete. Next: ./deploy_app.sh ==="
