# Architecture — how the Kueue Batch Queue demo works (a learning guide)

This walks through the whole implementation. The compute is deliberately simple (a
prime-sieve CPU burn) so the interesting part is the **batch-scheduling machinery** —
fixed-quota admission and priority preemption with Kueue on GKE — not the math.

---

## 1. The one-sentence idea

You submit CPU batch jobs against a **fixed CPU quota**; **Kueue** admits what fits,
queues the rest, and when a **high-priority** job arrives at a full queue it
**preempts (evicts)** a running **low-priority** job to make room — all on
auto-provisioned **Spot** nodes.

---

## 2. The moving parts

```
Browser (playroom)                     ── served by the controller (standalone) or the Hub
  │  POST /submit, GET /workloads, GET /pods, GET /quota, DELETE /jobs
  ▼
Gateway (gke-l7-global-external-managed)   ── dedicated L7 load balancer, one public IP
  │  HTTPRoute "/" → kueue-controller:80
  ▼
Controller  (Deployment, FastAPI)          ── submits Jobs + reports Kueue state
  │  create batch/Job (labelled queue-name + priority-class, suspend: true)
  ▼
Kueue (operator in kueue-system)
  ├── LocalQueue (this namespace) ─────────► ClusterQueue (fixed 6-CPU nominalQuota)
  │                                            preemption.withinClusterQueue: LowerPriority
  ├── ResourceFlavor "kueue-demo-spot" ─────► nodeLabel cloud.google.com/compute-class=kueue-demo-cpu
  └── WorkloadPriorityClass high/low ───────► decides admission order + who preempts whom
        │  admits a Workload  → un-suspends its Job → pod schedules
        │  preempts a Workload→ Job re-suspended / pod evicted (SIGTERM)
        ▼
   Job pods  (busy-compute, app/jobwork.py)   ── run on GKE NAP-created Spot nodes
```

Repo map:

| Path | Role |
|---|---|
| `app/controller.py` | FastAPI: submit Jobs, summarize Workloads, list pods + quota |
| `app/jobwork.py` | the REAL CPU work each Job runs (prime sieve, SIGTERM-aware) |
| `frontend/` | the playroom (submit form, quota bar, workloads + pods tables) |
| `infra/` | per-namespace K8s: controller, Service, Gateway/HTTPRoute, RBAC, LocalQueue |
| `cluster/` | cluster-scoped: Kueue operator + ComputeClass + ClusterQueue/flavor/priorities |
| `hub_router.py` | thin Hub data-plane router + honest offline MOCK |
| `*.sh`, `.env` | standalone lifecycle (create cluster, build/deploy, verify) |

---

## 3. The admission + preemption flow (the heart of it)

1. **Browser → `POST /submit`** with `{priority, duration, cpu}`.
2. **Controller builds a `batch/Job`** (`build_job_manifest`) with two Kueue labels:
   - `kueue.x-k8s.io/queue-name: kueue-demo-queue` — route the Workload through the LocalQueue.
   - `kueue.x-k8s.io/priority-class: high-priority | low-priority` — the WorkloadPriorityClass.
   The Job is created with **`suspend: true`**, handing scheduling to Kueue. Its
   container runs `python jobwork.py` for `JOB_DURATION_SECONDS` of real compute.
3. **Kueue creates a `Workload`** wrapping the Job and tries to admit it against the
   ClusterQueue's **fixed `nominalQuota` (6 CPU)**.
   - Fits → Kueue **un-suspends** the Job; its pod schedules onto a Spot node.
   - Doesn't fit → the Workload stays **pending** (queued).
4. **Preemption.** When a **high-priority** Workload is pending and the queue is full,
   `preemption.withinClusterQueue: LowerPriority` lets Kueue **evict** an admitted
   **lower-priority** Workload. The evicted Job is re-suspended and its pod gets
   `SIGTERM` (jobwork.py exits cleanly). The high-priority Job is then admitted.
5. **Controller reports state** by reading the `Workload` objects
   (`summarize_workload`): `admitted` / `pending` / `preempted` (Evicted condition,
   reason `Preempted`). `/pods` adds node placement + elapsed runtime; `/quota`
   reads the LocalQueue status for live usage.
6. **The board auto-refreshes** every 2s, so admission and the eviction animate live.

**Why `suspend: true`?** Kueue admits *Workloads*, not Jobs directly. A suspended
Job is inert until Kueue flips `suspend: false`; that is exactly the hook Kueue uses
to gate admission and to re-suspend on preemption.

---

## 4. Kueue concepts you can take away

- **ResourceFlavor** = a named slice of cluster capacity (here, the CPU Spot
  ComputeClass nodes via a `nodeLabel`). Admitted Workloads inherit its node affinity.
- **ClusterQueue** = the quota pool. `nominalQuota` is the **fixed capacity**;
  `preemption.withinClusterQueue: LowerPriority` enables eviction within the queue.
- **LocalQueue** = the namespaced entrypoint that points at a ClusterQueue; jobs
  reference it by name.
- **Workload** = the unit Kueue schedules (auto-created per Job); its
  `.status.conditions` (`QuotaReserved`, `Admitted`, `Evicted`, `Finished`) are the
  source of truth for the UI.
- **WorkloadPriorityClass** = a value (higher wins) attached via the Job label;
  drives admission ordering and preemption — distinct from a pod `PriorityClass`.

---

## 5. Kueue + GKE autoscaling (the GKE story)

- **Kueue operator** (installed once, cluster-scoped, in `kueue-system`) watches
  ClusterQueue/LocalQueue/Workload CRs across all feature namespaces.
- **Fixed quota is the constraint, not nodes.** The ClusterQueue caps admitted CPU at
  6, so the demo shows *queueing + preemption* rather than unbounded autoscaling.
- **Admitted pods select the CPU Spot ComputeClass** (`cloud.google.com/compute-class:
  kueue-demo-cpu`), so **GKE Node Auto-Provisioning** creates Spot nodes on demand and
  scales them back to zero when the queue drains. NAP must be enabled at cluster
  creation (`--enable-autoprovisioning`).

---

## 6. Networking

- **Dedicated Gateway** (`gke-l7-global-external-managed`) — a dedicated L7 LB per
  Gateway, so this feature never collides with another's route on a shared cluster.
- **`HTTPRoute`** sends `/` to the controller Service (`kueue-controller:80`).
- **CORS** on the controller is mandatory: the browser calls the Gateway IP directly
  (the Hub does not proxy data-plane traffic).

---

## 7. Operator-before-CRs ordering (the one gotcha)

`infra/queue-config.yaml` (ResourceFlavor / ClusterQueue / WorkloadPriorityClass)
are Kueue CRs whose creation is gated by the Kueue operator's **validating webhook**.
So they are deliberately kept out of the bootstrap bundle and applied at **deploy
time** instead:

- `cluster/` installs only the self-contained Kueue **release manifest** (operator +
  CRDs + `kueue-system` Namespace + an *internal* webhook cert — no cert-manager) plus
  the CPU ComputeClass. This is what the Hub's `build_infra.sh` applies once at
  bootstrap (no retry), and what `setup_infra.sh` applies standalone.
- By the time `infra/` is applied (Hub deploy, or `deploy_app.sh` standalone), the
  operator is already serving its webhook, so the queue CRs apply cleanly.
- `setup_infra.sh` **waits** for `deploy/kueue-controller-manager` to be Ready
  (Step 4) before handing off to `deploy_app.sh`, closing the race for the standalone
  path. The queue CRs are cluster-scoped, so kubectl ignores the deploy namespace.

---

## 8. Standalone *and* Hub (the feature contract)

- `feature.yaml` is the descriptor the Hub reads (paths, gateway, build, router).
- **Standalone:** `setup_infra.sh` (cluster + operator + ComputeClass + quota config)
  → `deploy_app.sh` (image + `infra/`) → `verify_setup.sh`. The controller serves the
  playroom itself, so the Gateway IP shows the full UI.
- **Hub:** the Hub builds the image, applies `cluster/` once + `infra/` per deploy,
  serves the playroom at `/kueue/`, and mounts `hub_router.py` at
  `/api/features/kueue`. The same frontend probes `/api/features/kueue/config` (Hub)
  and falls back to its own origin (standalone).
- **`MODE=MOCK`** makes `hub_router.py` return **honest empty** state so the playroom
  imports offline — it never fabricates jobs/nodes/quota (the NO-MOCKING rule).

---

## 9. Extending to GPUs via Dynamic Workload Scheduler / Flex Start (DESIGN ONLY)

> **Not implemented here** — this section documents how the same machinery scales to
> scarce GPU capacity. The CPU demo is the shipped artifact.

GPUs are scarce and expensive, so you don't want a fixed always-on quota of them; you
want capacity that is **provisioned just-in-time** when a job is admitted and released
when it finishes. GKE's **Dynamic Workload Scheduler (DWS) Flex Start** provides this
via the Kubernetes `ProvisioningRequest` API, and Kueue integrates with it through a
**provisioning admission check**.

The extension would be:

1. **A GPU ResourceFlavor.** Add a second `ResourceFlavor` (e.g. `gpu-flex`) whose
   `nodeLabels` select a GPU ComputeClass (an accelerator machine family, Spot or
   on-demand) created by NAP with `--max-accelerator`.
2. **A GPU resource group in the ClusterQueue.** Add `nvidia.com/gpu` to
   `coveredResources` with its own `nominalQuota`, mapped to the `gpu-flex` flavor.
3. **An `AdmissionCheck` of type `ProvisioningRequest`** bound to the ClusterQueue
   (`spec.admissionChecksStrategy`). With Flex Start, admitting a GPU Workload makes
   Kueue create a `ProvisioningRequest`; DWS finds a capacity window (up to a deadline)
   and provisions the GPU nodes **only then**. The Workload waits in `pending` (with a
   "provisioning" check state) until the nodes appear — no idle GPU burn.
4. **Job spec gains `nvidia.com/gpu` requests** and the GPU nodeSelector/toleration;
   `jobwork.py` would run a GPU payload (e.g. a small CUDA/torch burn) instead of the
   CPU prime sieve.

The preemption story is unchanged: WorkloadPriorityClasses still decide who preempts
whom within the GPU resource group. The only new concept is the **provisioning
admission check** that defers admission until DWS secures the scarce hardware — which
is the right pattern for GPUs precisely because a fixed standing quota of accelerators
is wasteful. (References: Kueue "Provisioning Request" admission check; GKE DWS Flex
Start with `ProvisioningRequest`.)
