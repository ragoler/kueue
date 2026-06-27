#!/usr/bin/env bash
# Build & push the Kueue demo image, then deploy the per-namespace infra
# (controller + Service + Gateway + HTTPRoute + RBAC + LocalQueue). Run
# setup_infra.sh first.
#
# The Hub IGNORES this file — it builds images from feature.yaml `build:` entries
# and applies infra/ itself.
set -e

if [ -f .env ]; then
  source .env
else
  echo "Error: .env file not found. Create one with: cp .env.example .env"
  exit 1
fi

REGION="${REGION:-${ZONE%-*}}"
NAMESPACE="${NAMESPACE:-default}"
LOCAL_QUEUE_NAME="${LOCAL_QUEUE_NAME:-kueue-demo-queue}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --reset-gateway: delete the Gateway + HTTPRoute before applying, forcing the
# GKE controller to reconcile them fresh. Use this if the Gateway is wedged.
RESET_GW=false
case "${1:-}" in
  --reset-gateway) RESET_GW=true ;;
  -h|--help)       echo "Usage: $0 [--reset-gateway]"; exit 0 ;;
  "")              ;;
  *) echo "Unknown argument: $1 (use --reset-gateway or no flag)"; exit 1 ;;
esac

# Per-cluster image tag so multiple clusters never clobber each other's image.
IMAGE_TAG="${IMAGE_TAG:-${CLUSTER_NAME}}"
REGISTRY="${REGION}-docker.pkg.dev/${PROJECT_ID}/${ARTIFACT_REGISTRY_REPO}"
KUEUE_IMAGE="${REGISTRY}/kueue-demo:${IMAGE_TAG}"

# Source of truth for the gateway name is the manifest, not .env (which can drift).
GATEWAY_NAME=$(awk '/kind: Gateway/{f=1} f&&/^  name:/{print $2; exit}' "${ROOT}/infra/gateway.yaml")

# Portable ${VAR} substitution (leaves $(VAR) downward-API refs intact).
render() { python3 -c "import os,sys;sys.stdout.write(os.path.expandvars(open(sys.argv[1]).read()))" "$1"; }

echo "=== Targeting cluster ${CLUSTER_NAME} (${ZONE}) ==="
gcloud container clusters get-credentials "${CLUSTER_NAME}" --zone="${ZONE}" --project="${PROJECT_ID}"

echo "=== Ensuring Artifact Registry repo ${ARTIFACT_REGISTRY_REPO} exists ==="
gcloud artifacts repositories create "${ARTIFACT_REGISTRY_REPO}" \
  --repository-format=docker --location="${REGION}" \
  --description="Kueue demo images" --project="${PROJECT_ID}" \
  || echo "Repo may already exist; continuing."

echo "=== Authenticating Docker to ${REGION}-docker.pkg.dev ==="
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet

echo "=== Building image (linux/amd64 for GKE nodes): ${KUEUE_IMAGE} ==="
# Context is the repo root so the image can include both app/ and frontend/.
docker build --platform linux/amd64 -t "${KUEUE_IMAGE}" -f "${ROOT}/app/Dockerfile" "${ROOT}"

echo "=== Pushing image ==="
docker push "${KUEUE_IMAGE}"

echo "=== Deploying per-namespace infra into: ${NAMESPACE} ==="
# Guard: infra/ contains Kueue CRs (LocalQueue + the queue config). If the operator
# CRDs aren't registered, the apply fails with a cryptic 'no matches for kind
# LocalQueue'. Fail fast with the real cause instead.
if ! kubectl get crd localqueues.kueue.x-k8s.io >/dev/null 2>&1; then
  echo "Error: Kueue CRDs are not installed on this cluster."
  echo "       Run ./setup_infra.sh first (it installs the Kueue operator + CRDs)."
  exit 1
fi
kubectl get namespace "${NAMESPACE}" >/dev/null 2>&1 || kubectl create namespace "${NAMESPACE}"

# Force-delete the Gateway, stripping a stuck finalizer if its controller never
# adopted it. Safe here: a wedged Gateway has no GCP resources to orphan.
force_delete_gateway() {
  local gw="$1"
  kubectl -n "${NAMESPACE}" get gateway "${gw}" >/dev/null 2>&1 || return 0
  kubectl -n "${NAMESPACE}" delete gateway "${gw}" --ignore-not-found --timeout=60s && return 0
  echo "Gateway delete is stuck; removing finalizer..."
  kubectl -n "${NAMESPACE}" patch gateway "${gw}" --type=merge -p '{"metadata":{"finalizers":null}}' || true
  kubectl -n "${NAMESPACE}" wait --for=delete "gateway/${gw}" --timeout=60s || true
}

DESIRED_GW_CLASS=$(grep -m1 'gatewayClassName:' "${ROOT}/infra/gateway.yaml" | awk '{print $2}')
CUR_GW_CLASS=$(kubectl -n "${NAMESPACE}" get gateway "${GATEWAY_NAME}" -o jsonpath='{.spec.gatewayClassName}' 2>/dev/null || true)
if [ "${RESET_GW}" = true ] || { [ -n "${CUR_GW_CLASS}" ] && [ "${CUR_GW_CLASS}" != "${DESIRED_GW_CLASS}" ]; }; then
  echo "Recreating Gateway + HTTPRoute for a clean reconcile (its IP will change)."
  kubectl -n "${NAMESPACE}" delete httproute kueue-route --ignore-not-found
  force_delete_gateway "${GATEWAY_NAME}"
fi

# Variables the infra manifests reference.
export NAMESPACE KUEUE_IMAGE LOCAL_QUEUE_NAME
for f in "${ROOT}"/infra/*.yaml; do
  base=$(basename "$f")
  echo "    applying ${base}"
  render "$f" | kubectl apply -n "${NAMESPACE}" -f -
done

echo "=== Rolling out the controller ==="
# Force a fresh pull of the rebuilt image (stable per-cluster tag + Always policy).
kubectl -n "${NAMESPACE}" rollout restart deployment/kueue-controller-deployment
kubectl -n "${NAMESPACE}" rollout status deployment/kueue-controller-deployment --timeout=600s || true

echo "=== Deployed. Discovering Gateway IP (may take 3-5 minutes) ==="
gateway_ip() {
  local ip
  ip=$(kubectl -n "${NAMESPACE}" get gateway "${GATEWAY_NAME}" -o jsonpath='{.status.addresses[0].value}' 2>/dev/null || true)
  [ -z "${ip}" ] && ip=$(gcloud compute forwarding-rules list --global --project="${PROJECT_ID}" \
    --filter="name~gkegw1.*-${NAMESPACE}-${GATEWAY_NAME}" --format="value(IPAddress)" 2>/dev/null | head -1)
  echo "${ip}"
}
for i in {1..30}; do
  GATEWAY_IP=$(gateway_ip)
  if [ -n "${GATEWAY_IP}" ]; then
    echo "Gateway IP: ${GATEWAY_IP}"
    echo "  Demo UI:   http://${GATEWAY_IP}/"
    echo "  Demo API:  http://${GATEWAY_IP}/healthz"
    break
  fi
  sleep 10
done
[ -z "${GATEWAY_IP:-}" ] && echo "Gateway IP not ready yet; check: kubectl -n ${NAMESPACE} get gateway ${GATEWAY_NAME}"

echo "=== Done. Run ./verify_setup.sh to smoke-test. ==="
