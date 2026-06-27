# Kueue Batch Queue — Fixed-Quota CPU Job Queueing with Priority Preemption

A GKE Feature Showcase demonstrating **[Kueue](https://kueue.sigs.k8s.io/)**: CPU
batch jobs queued against a **fixed quota**, with **priority preemption** — when a
high-priority job arrives at a full queue, Kueue **evicts** a running low-priority
job to make room. The submitted jobs run **real CPU work** (a prime sieve), not a
sleep, on auto-provisioned **Spot** nodes.

This repo follows the [gke_all feature contract](https://github.com/ragoler/gke_all/blob/main/feature.md):
it runs **standalone** (its own cluster + scripts) and as a **Hub feature** (mounted
as a submodule, driven by `feature.yaml`).

## What you see

- A submit form: **priority** (high/low), **duration** (short ~60s / long ~600s),
  and **CPU request** (1/2/3).
- A live board:
  - a **fixed-capacity bar** (the ClusterQueue's 6-CPU `nominalQuota`),
  - a **workloads table** (job, priority, status: admitted / pending / **PREEMPTED**),
  - a **pods table** (which node, and elapsed runtime).
- Submit two low-priority jobs to fill the quota, then a high-priority job — watch a
  low-priority row flip to **PREEMPTED** as the high-priority job is admitted.

## The mechanism (how "fixed capacity + preemption" is built)

| Piece | File | Role |
|---|---|---|
| CPU Spot `ComputeClass` (`nodePoolAutoCreation`) | `cluster/cpu-computeclass.yaml` | the physical capacity (NAP creates Spot nodes) |
| Kueue `ResourceFlavor` -> the ComputeClass | `cluster/queue-config.yaml` | maps logical quota onto those nodes |
| `ClusterQueue` with fixed `nominalQuota: 6` CPU + `preemption.withinClusterQueue: LowerPriority` | `cluster/queue-config.yaml` | **the fixed quota + the preemption rule** |
| `WorkloadPriorityClass` high/low | `cluster/queue-config.yaml` | drive who preempts whom |
| namespaced `LocalQueue` -> the ClusterQueue | `infra/localqueue.yaml` | per-namespace entrypoint jobs are submitted into |
| FastAPI controller | `app/controller.py` | submits labelled Jobs, reports admission/preemption |
| busy-compute payload | `app/jobwork.py` | the REAL CPU work each Job runs |

## Standalone usage

```bash
cp .env.example .env        # set PROJECT_ID, CLUSTER_NAME, REGION/ZONE, ...
./setup_infra.sh            # create the GKE cluster + Kueue operator + quota config
./deploy_app.sh             # build/push the image + deploy the controller & LocalQueue
./verify_setup.sh           # REAL smoke test: fills the quota, forces a preemption,
                            #   asserts high admitted + low PREEMPTED + pods on a node
```

Then open `http://<GATEWAY_IP>/` (printed by `deploy_app.sh`).

Teardown:

```bash
./setup_infra.sh --delete           # remove cluster-scoped prereqs (keep the cluster)
./setup_infra.sh --delete-cluster   # also delete the GKE cluster
```

### Configuration variables (`.env`)

See `.env.example` for the full list. Standalone uses `PROJECT_ID`; the Hub injects
the same value as `PROJECT_NAME` (the most common gotcha). `NAMESPACE` defaults to
`default` so `${NAMESPACE}` renders standalone; the Hub overrides it with
`gke-showcase-kueue`.

## Hub usage

Added as a submodule under `features/kueue/`. The Hub:

1. reads `feature.yaml` (paths, gateway, build, router),
2. builds the `kueue-demo` image,
3. applies `cluster/` once at bootstrap (`kubectl apply --server-side -k cluster/`)
   and `infra/` per deploy,
4. serves the playroom at `/kueue/` and mounts `hub_router.py` at
   `/api/features/kueue`.

The same frontend works both ways: it probes `/api/features/kueue/config` (Hub) and
falls back to its own origin / the Gateway IP (standalone).

> **New CRD kinds:** this feature introduces Kueue CRDs (`ClusterQueue`,
> `LocalQueue`, `Workload`, `ResourceFlavor`, `WorkloadPriorityClass`) and creates
> `batch/Job` objects. When wiring into the Hub, ensure the admin ClusterRole in
> `infra/main-app.yaml` grants `kueue.x-k8s.io` resources and `batch/jobs` (else the
> deploy 403s). This is in-cluster Kubernetes RBAC, not GCP IAM.

## Kueue version

The operator is pinned to **Kueue v0.18.2** (latest stable at authoring) in
`cluster/kueue-operator/kustomization.yaml`. Bump deliberately and keep this README
in sync.

## NO mocking

The submitted Jobs run **real** CPU work on **real** nodes and the UI reports **real**
cluster state. The Hub's offline `MODE=MOCK` returns **honest empty** state ("not
connected to a cluster") — it never fabricates jobs, nodes, runtimes, or quota.

## Tests

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install fastapi 'uvicorn[standard]' pydantic pyyaml kubernetes pytest httpx
python -m pytest -q
```

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design, including a section on
extending this to **GPUs via Dynamic Workload Scheduler / Flex Start** (documented,
not implemented).
