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

const QUOTA_CPU = 6; // last-resort default; the live total comes from /quota.total_cpu

const els = {
  mode: document.getElementById("mode-badge"),
  priority: document.getElementById("priority"),
  duration: document.getElementById("duration"),
  cpu: document.getElementById("cpu"),
  submit: document.getElementById("submit"),
  clear: document.getElementById("clear"),
  clearFinished: document.getElementById("clear-finished"),
  actionNote: document.getElementById("action-note"),
  quotaLabel: document.getElementById("quota-label"),
  quotaFill: document.getElementById("quota-fill"),
  quotaText: document.getElementById("quota-text"),
  capacityLine: document.getElementById("capacity-line"),
  board: document.getElementById("board"),
  jobCount: document.getElementById("job-count"),
  offlineNote: document.getElementById("offline-note"),
};

let cfg = { mode: "MOCK", dataBase: HUB_BASE };
let timer = null;
// LIVE only: the browser submits to the feature's Gateway directly, and the global LB
// is PROGRAMMED minutes after the Deployment is ready. Until the data path serves, show
// "provisioning…" rather than falling back to the hub_router (which has no /submit -> 404).
let dataReady = false;

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
  // Start disabled; the refresh loop enables the controls once the data plane is
  // actually reachable (LIVE) — in MOCK they stay disabled.
  els.submit.disabled = true;
  els.clear.disabled = true;
  els.clearFinished.disabled = true;
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

async function clearFinishedJobs() {
  els.clearFinished.disabled = true;
  try {
    const r = await fetch(dataUrl("/jobs/finished"), { method: "DELETE", headers: dataHeaders() });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || `clear finished failed: ${r.status}`);
    note(`Cleared ${data.count ?? 0} finished job(s)`, false);
    await refresh();
  } catch (e) {
    note(e.message, true);
  } finally {
    els.clearFinished.disabled = cfg.mode === "MOCK";
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
  const total = q && q.total_cpu != null ? q.total_cpu : QUOTA_CPU;
  let used = q && q.used_cpu != null ? q.used_cpu : null;
  // Fallback: derive used CPU from the flavor reservation if not provided.
  if (used === null && q && Array.isArray(q.flavors_usage)) {
    for (const fl of q.flavors_usage) {
      for (const res of fl.resources || []) {
        if (res.name === "cpu") used = parseFloat(res.total || res.borrowed || "0");
      }
    }
  }
  const usedTxt = used === null ? "?" : used;
  const pct = used === null ? 0 : Math.min(100, (used / total) * 100);
  els.quotaFill.style.width = pct + "%";
  els.quotaFill.classList.toggle("full", pct >= 99);
  els.quotaText.textContent = `${usedTxt} / ${total} CPU`;
  if (used !== null) {
    const free = Math.max(0, total - used);
    els.capacityLine.textContent = `${used} of ${total} CPU used · ${free} free`;
  } else {
    els.capacityLine.textContent = `Fixed capacity: ${total} CPU`;
  }
  if (q) {
    const a = q.admitted_workloads ?? 0;
    const p = q.pending_workloads ?? 0;
    els.quotaLabel.textContent = `· ${a} admitted · ${p} pending`;
  }
}

// One row per Job: Workload state/priority/duration/cpu joined with its pod's
// node + (frozen-when-finished) elapsed. Solves correlating the old two tables.
function renderBoard(workloads, pods) {
  // Map job -> best pod (prefer a running pod, else the most recent by start).
  const podByJob = {};
  for (const p of pods) {
    if (!p.job) continue;
    const cur = podByJob[p.job];
    if (!cur) { podByJob[p.job] = p; continue; }
    const better = !p.finished && cur.finished;
    const newer = (p.started || "") > (cur.started || "");
    if (better || (p.finished === cur.finished && newer)) podByJob[p.job] = p;
  }

  els.board.innerHTML = "";
  els.jobCount.textContent = workloads.length ? `· ${workloads.length}` : "";
  if (!workloads.length) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td colspan="8" class="empty">No jobs.</td>`;
    els.board.appendChild(tr);
    return;
  }
  for (const w of workloads) {
    const pod = podByJob[w.job] || null;
    const cls = STATE_CLASS[w.state] || "state-pending";
    const label = (w.state || "pending").toUpperCase();
    const isFinished = w.state === "finished" || (pod && pod.finished);
    const elapsed = pod && pod.elapsed_seconds != null ? `${pod.elapsed_seconds}s` : "—";
    // When finished, show the completion time right next to the status badge.
    const completedAt = isFinished && pod && pod.finished_at ? fmtTime(pod.finished_at) : null;
    const statusCell = `<span class="state ${cls}">${label}</span>` +
      (completedAt ? ` <span class="completed-at">@ ${completedAt}</span>` : "");
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td class="mono">${w.job || w.workload || "—"}</td>` +
      `<td>${prettyPriority(w.priority_class)}</td>` +
      `<td>${prettyDuration(w.duration_seconds)}</td>` +
      `<td>${w.cpu != null ? w.cpu : "—"}</td>` +
      `<td>${w.submitted ? fmtTime(w.submitted) : "—"}</td>` +
      `<td>${statusCell}</td>` +
      `<td class="mono">${pod && pod.node ? pod.node : "—"}</td>` +
      `<td>${elapsed}</td>`;
    if (w.state === "preempted") tr.classList.add("row-preempted");
    if (isFinished) tr.classList.add("row-finished");
    els.board.appendChild(tr);
  }
}

// ISO timestamp -> local HH:MM:SS (board is a live view, so local time reads best).
function fmtTime(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return "—";
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function prettyPriority(pc) {
  if (!pc) return "—";
  if (pc.includes("high")) return `<span class="prio prio-high">high</span>`;
  if (pc.includes("medium")) return `<span class="prio prio-medium">medium</span>`;
  if (pc.includes("low")) return `<span class="prio prio-low">low</span>`;
  return pc;
}

function prettyDuration(secs) {
  if (secs == null) return "—";
  return secs >= 60 && secs % 60 === 0 ? `${secs / 60}m` : `${secs}s`;
}

function showOffline(text) {
  els.offlineNote.hidden = !text;
  els.offlineNote.textContent = text || "";
}

/* ---- data-plane readiness gate --------------------------------------- */
// Re-resolve the Gateway IP (it programs minutes after the Deployment is ready) and
// probe the data path itself. Sets dataReady. LIVE only; MOCK is handled separately.
async function refreshDataPlane() {
  const override = new URLSearchParams(location.search).get("api");
  try {
    const r = await fetch(`${HUB_BASE}/config`, { headers: hubHeaders() });
    if (r.ok) {
      const c = await r.json();
      cfg.mode = c.mode || "LIVE";
      if (cfg.mode === "MOCK") { dataReady = false; return; }
      // Empty when the Hub has no PROGRAMMED gateway yet (get_gateway_ip returns "").
      cfg.dataBase = override || (c.gateway_ip ? `http://${c.gateway_ip}` : "");
    } else {
      cfg.mode = "LIVE";
      cfg.dataBase = override || location.origin; // standalone (no Hub)
    }
  } catch (_) {
    cfg.mode = "LIVE";
    cfg.dataBase = override || location.origin;
  }
  if (!cfg.dataBase) { dataReady = false; return; }
  try {
    const h = await fetch(`${cfg.dataBase}/healthz`, { headers: dataHeaders() });
    dataReady = h.ok;
  } catch (_) { dataReady = false; }
}

function renderProvisioning() {
  els.mode.textContent = cfg.mode;
  els.mode.className = "badge badge-live";
  els.submit.disabled = true;
  els.clear.disabled = true;
  els.clearFinished.disabled = true;
  showOffline(cfg.dataBase
    ? "Provisioning the load balancer — this can take a few minutes…"
    : "Waiting for the gateway IP…");
}

/* ---- refresh loop ---------------------------------------------------- */
async function refresh() {
  // LIVE submits to the Gateway directly — gate on it actually serving, so the submit
  // button never POSTs to the hub_router (no /submit there -> 404) before it's ready.
  if (cfg.mode !== "MOCK" && !dataReady) {
    await refreshDataPlane();
    if (!dataReady) { renderProvisioning(); return; }
    // Just became reachable — enable the controls and clear the provisioning note.
    els.submit.disabled = false;
    els.clear.disabled = false;
    els.clearFinished.disabled = false;
    showOffline("");
  }
  try {
    const [wRes, pRes, qRes] = await Promise.all([
      fetch(dataUrl("/workloads"), { headers: dataHeaders() }),
      fetch(dataUrl("/pods"), { headers: dataHeaders() }),
      fetch(dataUrl("/quota"), { headers: dataHeaders() }),
    ]);
    const w = wRes.ok ? await wRes.json() : { workloads: [] };
    const p = pRes.ok ? await pRes.json() : { pods: [] };
    const q = qRes.ok ? await qRes.json() : null;

    renderBoard(w.workloads || [], p.pods || []);
    renderQuota(q);
    showOffline(cfg.mode === "MOCK" ? (w.note || q?.note || "") : "");
  } catch (_) {
    /* transient — keep last render */
  }
}

/* ---- init ----------------------------------------------------------- */
els.submit.addEventListener("click", submitJob);
els.clear.addEventListener("click", clearJobs);
els.clearFinished.addEventListener("click", clearFinishedJobs);

(async function init() {
  await loadConfig();
  applyConfigUI();
  await refresh();
  timer = setInterval(refresh, 2000);
})();
