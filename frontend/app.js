/* Kueue Batch Queue — playroom frontend.
 *
 * Two call surfaces:
 *   - control plane: `/api/features/kueue/...` (Hub, JWT). Used for /config and
 *     ALL calls when MODE=MOCK (which returns honest empty state — there are no
 *     real jobs offline).
 *   - data plane: the Gateway IP directly (CORS, no auth). Used for submit /
 *     workloads / pods / quota / clear when running LIVE.
 *
 * The board auto-refreshes so you can watch admission and preemption happen.
 */

const HUB_BASE = "/api/features/kueue";

const QUOTA_CPU = 6; // matches ClusterQueue nominalQuota (cluster/queue-config.yaml)

const els = {
  mode: document.getElementById("mode-badge"),
  priority: document.getElementById("priority"),
  duration: document.getElementById("duration"),
  cpu: document.getElementById("cpu"),
  submit: document.getElementById("submit"),
  clear: document.getElementById("clear"),
  actionNote: document.getElementById("action-note"),
  quotaLabel: document.getElementById("quota-label"),
  quotaFill: document.getElementById("quota-fill"),
  quotaText: document.getElementById("quota-text"),
  workloads: document.getElementById("workloads"),
  offlineNote: document.getElementById("offline-note"),
  pods: document.getElementById("pods"),
  podCount: document.getElementById("pod-count"),
};

let cfg = { mode: "MOCK", dataBase: HUB_BASE };
let timer = null;

/* ---- auth + bases ----------------------------------------------------- */
function jwt() {
  return localStorage.getItem("admin_jwt") || "";
}
function hubHeaders() {
  const h = { "Content-Type": "application/json" };
  const t = jwt();
  if (t) h["Authorization"] = `Bearer ${t}`;
  return h;
}
// In MOCK everything flows through the Hub (JWT). In LIVE the data plane hits the
// Gateway IP with CORS and no auth.
function dataHeaders() {
  return cfg.mode === "MOCK" ? hubHeaders() : { "Content-Type": "application/json" };
}
function dataUrl(path) {
  return cfg.mode === "MOCK" ? `${HUB_BASE}${path}` : `${cfg.dataBase}${path}`;
}

/* ---- config / bootstrap ---------------------------------------------- */
async function loadConfig() {
  const override = new URLSearchParams(location.search).get("api");
  try {
    const r = await fetch(`${HUB_BASE}/config`, { headers: hubHeaders() });
    if (r.ok) {
      const c = await r.json();
      cfg.mode = c.mode || "LIVE";
      cfg.dataBase =
        cfg.mode === "MOCK"
          ? HUB_BASE
          : override || (c.gateway_ip ? `http://${c.gateway_ip}` : HUB_BASE);
      return;
    }
  } catch (_) {
    /* fall through to standalone */
  }
  // Standalone: no Hub. Talk to the controller directly.
  cfg.mode = "LIVE";
  cfg.dataBase = override || location.origin;
}

function applyConfigUI() {
  els.mode.textContent = cfg.mode;
  els.mode.className = "badge " + (cfg.mode === "MOCK" ? "badge-mock" : "badge-live");
  const mock = cfg.mode === "MOCK";
  els.submit.disabled = mock;
  els.clear.disabled = mock;
}

/* ---- actions ---------------------------------------------------------- */
function note(msg, isError) {
  els.actionNote.textContent = msg;
  els.actionNote.className = "action-note" + (isError ? " err" : "");
}

async function submitJob() {
  els.submit.disabled = true;
  const body = JSON.stringify({
    priority: els.priority.value,
    duration: els.duration.value,
    cpu: els.cpu.value,
  });
  try {
    const r = await fetch(dataUrl("/submit"), {
      method: "POST",
      headers: dataHeaders(),
      body,
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || `submit failed: ${r.status}`);
    note(`Submitted ${data.job}`, false);
    await refresh();
  } catch (e) {
    note(e.message, true);
  } finally {
    els.submit.disabled = cfg.mode === "MOCK";
  }
}

async function clearJobs() {
  els.clear.disabled = true;
  try {
    const r = await fetch(dataUrl("/jobs"), { method: "DELETE", headers: dataHeaders() });
    if (!r.ok) {
      const data = await r.json().catch(() => ({}));
      throw new Error(data.detail || `clear failed: ${r.status}`);
    }
    note("Cleared all jobs", false);
    await refresh();
  } catch (e) {
    note(e.message, true);
  } finally {
    els.clear.disabled = cfg.mode === "MOCK";
  }
}

/* ---- rendering ------------------------------------------------------- */
const STATE_CLASS = {
  admitted: "state-admitted",
  pending: "state-pending",
  preempted: "state-preempted",
  finished: "state-finished",
};

function renderQuota(q) {
  // Derive used CPU from admitted pods if the queue status doesn't expose it.
  let used = null;
  if (q && Array.isArray(q.flavors_usage)) {
    for (const fl of q.flavors_usage) {
      for (const res of fl.resources || []) {
        if (res.name === "cpu") used = parseFloat(res.total || res.borrowed || "0");
      }
    }
  }
  if (used === null && q && q.admitted_cpu != null) used = q.admitted_cpu;
  const usedTxt = used === null ? "?" : used;
  const pct = used === null ? 0 : Math.min(100, (used / QUOTA_CPU) * 100);
  els.quotaFill.style.width = pct + "%";
  els.quotaFill.classList.toggle("full", pct >= 99);
  els.quotaText.textContent = `${usedTxt} / ${QUOTA_CPU} CPU`;
  if (q) {
    const a = q.admitted_workloads ?? 0;
    const p = q.pending_workloads ?? 0;
    els.quotaLabel.textContent = `· ${a} admitted · ${p} pending`;
  }
}

function renderWorkloads(list) {
  els.workloads.innerHTML = "";
  if (!list.length) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td colspan="3" class="empty">No workloads.</td>`;
    els.workloads.appendChild(tr);
    return;
  }
  for (const w of list) {
    const tr = document.createElement("tr");
    const cls = STATE_CLASS[w.state] || "state-pending";
    const label = (w.state || "pending").toUpperCase();
    tr.innerHTML =
      `<td class="mono">${w.job || w.workload || "—"}</td>` +
      `<td>${prettyPriority(w.priority_class)}</td>` +
      `<td><span class="state ${cls}">${label}</span></td>`;
    if (w.state === "preempted") tr.classList.add("row-preempted");
    els.workloads.appendChild(tr);
  }
}

function prettyPriority(pc) {
  if (!pc) return "—";
  if (pc.includes("high")) return `<span class="prio prio-high">high</span>`;
  if (pc.includes("low")) return `<span class="prio prio-low">low</span>`;
  return pc;
}

function renderPods(list) {
  els.pods.innerHTML = "";
  els.podCount.textContent = list.length ? `· ${list.length}` : "";
  if (!list.length) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td colspan="4" class="empty">No running pods.</td>`;
    els.pods.appendChild(tr);
    return;
  }
  for (const p of list) {
    const tr = document.createElement("tr");
    const elapsed = p.elapsed_seconds == null ? "—" : `${p.elapsed_seconds}s`;
    tr.innerHTML =
      `<td class="mono">${p.pod_name}</td>` +
      `<td class="mono">${p.node || "—"}</td>` +
      `<td>${p.status || "—"}</td>` +
      `<td>${elapsed}</td>`;
    els.pods.appendChild(tr);
  }
}

function showOffline(text) {
  els.offlineNote.hidden = !text;
  els.offlineNote.textContent = text || "";
}

/* ---- refresh loop ---------------------------------------------------- */
async function refresh() {
  try {
    const [wRes, pRes, qRes] = await Promise.all([
      fetch(dataUrl("/workloads"), { headers: dataHeaders() }),
      fetch(dataUrl("/pods"), { headers: dataHeaders() }),
      fetch(dataUrl("/quota"), { headers: dataHeaders() }),
    ]);
    const w = wRes.ok ? await wRes.json() : { workloads: [] };
    const p = pRes.ok ? await pRes.json() : { pods: [] };
    const q = qRes.ok ? await qRes.json() : null;

    renderWorkloads(w.workloads || []);
    renderPods(p.pods || []);
    renderQuota(q);
    showOffline(cfg.mode === "MOCK" ? (w.note || q?.note || "") : "");
  } catch (_) {
    /* transient — keep last render */
  }
}

/* ---- init ----------------------------------------------------------- */
els.submit.addEventListener("click", submitJob);
els.clear.addEventListener("click", clearJobs);

(async function init() {
  await loadConfig();
  applyConfigUI();
  await refresh();
  timer = setInterval(refresh, 2000);
})();
