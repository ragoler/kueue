#!/usr/bin/env bash
# Post-deployment validation for the Kueue Batch Queue demo: waits for the
# controller, discovers the Gateway IP, then runs a REAL preemption smoke test —
# fill the fixed 6-CPU quota with low-priority long jobs, submit a high-priority
# job, and assert via the API that the high-priority Workload is admitted while a
# low-priority Workload is PREEMPTED, and that admitted pods report a node.
set -e

if [ -f .env ]; then
  source .env
else
  echo "Error: .env file not found."
  exit 1
fi
NAMESPACE="${NAMESPACE:-default}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATEWAY_NAME=$(awk '/kind: Gateway/{f=1} f&&/^  name:/{print $2; exit}' "${ROOT}/infra/gateway.yaml")

echo "=== Targeting cluster ${CLUSTER_NAME} (${ZONE}) ==="
gcloud container clusters get-credentials "${CLUSTER_NAME}" --zone="${ZONE}" --project="${PROJECT_ID}"

echo "=== Waiting for the controller to be Ready ==="
kubectl -n "${NAMESPACE}" rollout status deployment/kueue-controller-deployment --timeout=300s

echo "=== Discovering Gateway IP ==="
gateway_ip() {
  local ip
  ip=$(kubectl -n "${NAMESPACE}" get gateway "${GATEWAY_NAME}" -o jsonpath='{.status.addresses[0].value}' 2>/dev/null || true)
  [ -z "${ip}" ] && ip=$(gcloud compute forwarding-rules list --global --project="${PROJECT_ID}" \
    --filter="name~gkegw1.*-${NAMESPACE}-${GATEWAY_NAME}" --format="value(IPAddress)" 2>/dev/null | head -1)
  echo "${ip}"
}
for i in {1..30}; do
  GATEWAY_IP=$(gateway_ip)
  [ -n "${GATEWAY_IP}" ] && break
  sleep 10
done
if [ -z "${GATEWAY_IP:-}" ]; then
  echo "Error: Gateway did not receive an IP within 5 minutes."
  exit 1
fi
echo "Gateway IP: ${GATEWAY_IP}"
BASE="http://${GATEWAY_IP}"

# A freshly-created L7 gateway reports an IP before its backend + health checks are
# programmed; poll /healthz until the data path is actually serving (up to ~8 min).
echo "=== Waiting for the Gateway data path to be healthy ==="
HEALTHY=""
for i in $(seq 1 32); do
  if curl -fsS -m 10 "${BASE}/healthz" >/dev/null 2>&1; then
    HEALTHY=1
    echo "Gateway healthy after ~$((i * 15))s"
    break
  fi
  sleep 15
done
if [ -z "${HEALTHY}" ]; then
  echo "Error: Gateway data path not healthy yet (LB still programming). Re-run shortly."
  exit 1
fi

curl -fsS "${BASE}/healthz" && echo

jpath() { python3 -c "import sys,json;d=json.load(sys.stdin);print(eval(sys.argv[1]))" "$1"; }
submit() {  # submit <priority> <duration> <cpu>
  curl -fsS -X POST "${BASE}/submit" -H 'Content-Type: application/json' \
    -d "{\"priority\":\"$1\",\"duration\":\"$2\",\"cpu\":\"$3\"}" \
    | jpath "d['job']"
}

echo "=== Resetting the board ==="
curl -fsS -X DELETE "${BASE}/jobs" >/dev/null && echo "cleared"
sleep 5

echo "=== Filling the fixed 6-CPU quota with two low-priority long jobs (3 CPU each) ==="
LOW1=$(submit low long 3); echo "  low #1: ${LOW1}"
LOW2=$(submit low long 3); echo "  low #2: ${LOW2}"

echo "=== Waiting for both low-priority jobs to be admitted ==="
admitted_count() {
  curl -fsS "${BASE}/workloads" | python3 -c \
    "import sys,json;d=json.load(sys.stdin);print(sum(1 for w in d['workloads'] if w['state']=='admitted'))"
}
for i in $(seq 1 40); do
  N=$(admitted_count || echo 0)
  echo "    admitted workloads: ${N}"
  [ "${N}" -ge 2 ] && break
  sleep 10
done
[ "$(admitted_count)" -ge 2 ] || { echo "Error: low-priority jobs were not admitted (quota/NAP issue)."; exit 1; }

echo "=== Submitting a HIGH-priority job (3 CPU) into the full queue -> expect preemption ==="
HIGH=$(submit high long 3); echo "  high: ${HIGH}"

echo "=== Asserting the high-priority job is admitted AND a low-priority job is PREEMPTED ==="
check() {
  curl -fsS "${BASE}/workloads" | python3 - "$HIGH" <<'PY'
import sys, json
high = sys.argv[1]
d = json.load(sys.stdin)
wls = d["workloads"]
high_admitted = any(w["state"] == "admitted" and (w.get("job") or "").find(high) >= 0 for w in wls)
# Fall back: any high-priority workload admitted, any low-priority preempted.
high_admitted = high_admitted or any(
    w["state"] == "admitted" and "high" in (w.get("priority_class") or "") for w in wls
)
low_preempted = any(
    w["state"] == "preempted" and "low" in (w.get("priority_class") or "") for w in wls
)
print("OK" if (high_admitted and low_preempted) else "WAIT")
PY
}
PASS=""
for i in $(seq 1 30); do
  R=$(check || echo WAIT)
  echo "    preemption check: ${R}"
  if [ "${R}" = "OK" ]; then PASS=1; break; fi
  sleep 10
done
[ -n "${PASS}" ] || { echo "Error: did not observe high admitted + low preempted within timeout."; exit 1; }

echo "=== Asserting admitted pods report a node ==="
curl -fsS "${BASE}/pods" | python3 -c \
  "import sys,json;d=json.load(sys.stdin);pods=d['pods'];print('pods:',[(p['pod_name'],p['node']) for p in pods]);assert any(p['node'] for p in pods),'no pod reported a node';print('node placement OK')"

echo "=== Verification successful — Kueue admitted, queued, and PREEMPTED as designed. ==="
echo "Open the demo UI at: ${BASE}/"
