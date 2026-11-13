/* SCOPE-X Constraint Lab dashboard (vanilla JS SPA). The store backend stays; this UI only tests constraints. */
(function () {
  const $ = (sel, root = document) => root.querySelector(sel);
  const view = $("#view");
  const C = window.BZCharts;
  const state = { health: null, testbed: null, timers: [], lab: { test: null, poll: null, whatif: null, whatifTimer: null, shieldPoll: null, drop: null } };
  /** Interval that is cleared automatically when the user changes page. */
  const every = (fn, ms) => { const id = setInterval(fn, ms); state.timers.push(id); return id; };
  /** Where live results were measured (set by the server via SCOPEX_TESTBED_LABEL). */
  const LABEL = () => (state.health && state.health.testbed_label) || "validated on local testbed";
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const ago = (ts) => { if (!ts) return "–"; const s = Math.max(0, Date.now() / 1000 - ts); return s < 60 ? `${Math.round(s)}s ago` : s < 3600 ? `${Math.round(s / 60)}m ago` : new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); };

  /** Read-only snapshot mode (static hosting, e.g. Vercel): GETs come from /snapshot/*.json, writes are refused. */
  const DEMO = window.SCOPEX_DEMO || null;
  const snapKey = (path) => path.replace(/^\/api\//, "").replace(/[/?=&]/g, "_") + ".json";
  async function demoApi(path, opts) {
    const method = (opts.method || "GET").toUpperCase();
    let file = snapKey(path);
    if (method !== "GET") {
      if (path === "/api/lab/whatif" && opts.body === JSON.stringify(DEMO.whatif_default)) file = "lab_whatif.json";
      else { const e = new Error("Read-only snapshot: run the lab locally (python -m storefront) to change settings or start tests."); e.status = 405; throw e; }
    }
    const res = await fetch("/snapshot/" + file);
    if (!res.ok) { const e = new Error("Not in this snapshot."); e.status = 404; throw e; }
    return res.json();
  }

  async function api(path, opts = {}) {
    if (DEMO) return demoApi(path, opts);
    let token = "";
    try { token = localStorage.getItem("scopex_lab_token") || ""; } catch { /* storage blocked */ }
    const res = await fetch(path, { headers: { "content-type": "application/json", ...(token ? { "x-lab-token": token } : {}) }, credentials: "same-origin", ...opts });
    if (res.status === 401 && !opts._retried) {
      const t = window.prompt("This lab is write-protected. Enter the lab token to run tests or change settings:");
      if (t) { try { localStorage.setItem("scopex_lab_token", t.trim()); } catch { /* storage blocked */ } return api(path, { ...opts, _retried: true }); }
    }
    const text = await res.text();
    let data;
    try { data = text ? JSON.parse(text) : {}; } catch { data = { detail: text }; }
    if (!res.ok) {
      const d = data.detail;
      const msg = typeof d === "string" ? d : d && d.message ? d.message : JSON.stringify(d);
      const err = new Error(msg || res.statusText);
      err.status = res.status; err.data = data;
      throw err;
    }
    return data;
  }

  // ------------------------------------------------------------------ shell
  const ICON = {
    overview: '<rect x="3" y="3" width="7" height="7" rx="2"/><rect x="14" y="3" width="7" height="7" rx="2"/><rect x="3" y="14" width="7" height="7" rx="2"/><rect x="14" y="14" width="7" height="7" rx="2"/>',
    live: '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
    model: '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
    shield: '<path d="M12 3 4 6v6c0 4.5 3.4 8.2 8 9 4.6-.8 8-4.5 8-9V6l-8-3Z"/>',
    repo: '<ellipse cx="12" cy="5.5" rx="8" ry="2.5"/><path d="M4 5.5v6c0 1.4 3.6 2.5 8 2.5s8-1.1 8-2.5v-6M4 11.5v6c0 1.4 3.6 2.5 8 2.5s8-1.1 8-2.5v-6"/>',
    server: '<rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 7.5h.01M7 16.5h.01"/>',
    gauge: '<path d="M12 14 16 9"/><path d="M3.5 17a9 9 0 1 1 17 0"/>',
    alert: '<path d="M12 3 2 20h20L12 3Z"/><path d="M12 10v4M12 17h.01"/>',
    target: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3.5"/>',
    list: '<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>',
    sweep: '<path d="M3 20 9 13l4 4 8-11"/><path d="M15 6h6v6"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
  };
  const icon = (name, size = 18) => `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICON[name]}</svg>`;
  const PAGES = [
    { id: "overview", hash: "#/", label: "Overview", title: "Overview", sub: "Live state of the testbed, its constraints and recent runs." },
    { id: "live", hash: "#/live", label: "Live tests", title: "Live tests", sub: "Run sweeps, spikes and steady load against the local testbed and watch the SLO bound." },
    { id: "model", hash: "#/model", label: "Model what-if", title: "Model what-if", sub: "Apply stored constraints to cluster configs you can't run on one machine. Simulated." },
    { id: "shield", hash: "#/shield", label: "Flash-sale shield", title: "Flash-sale shield", sub: "Waiting room, stock reservations and bot defence, plus the off-vs-on drop test." },
    { id: "repo", hash: "#/repo", label: "Repository", title: "Constraint repository", sub: "Every stored constraint and its validity under static, HPA and vertical scaling." },
  ];

  function initShell() {
    if (DEMO) {
      const d = $("#demo-note");
      d.hidden = false;
      d.innerHTML = `<b>Read-only snapshot.</b> Results below were measured on the local SCOPE-X testbed on ${esc(DEMO.captured)} and are shown as recorded. Controls are disabled here; run <code>python -m storefront</code> locally to run tests.`;
    }
    $("#nav").innerHTML = PAGES.map((p) => `<a href="${p.hash}" data-page="${p.id}">${icon(p.id)}<span>${p.label}</span></a>`).join("");
    pollHealth();
    setInterval(pollHealth, 3000);
  }

  async function pollHealth() {
    try {
      const h = await api("/api/health");
      state.health = h;
      const p95 = Math.max(h.store_p95_ms || 0, h.load_test_p95_ms || 0);
      $("#health-dot").className = "dot " + (h.testbed === "running" ? h.level : "off");
      $("#health-title").textContent = h.testbed === "running" ? `Testbed ${h.level}` : `Testbed ${h.testbed}`;
      $("#health-sub").textContent = `p95 ${p95 ? Math.round(p95) + " ms" : "–"} · SLO ${h.slo.p95_ms} ms`;
      const banner = $("#banner");
      if (h.testbed === "running" && h.level === "degraded") {
        banner.className = "banner show degraded";
        banner.innerHTML = `<b>SLO violated right now.</b> Live p95 ${Math.round(p95)} ms vs ${h.slo.p95_ms} ms${h.error_pct > h.slo.error_pct ? `, errors ${h.error_pct}% vs ${h.slo.error_pct}%` : ""}.`;
      } else if (h.testbed !== "running") {
        banner.className = "banner show busy";
        banner.textContent = h.testbed === "starting" ? "Starting the testbed services…" : `Testbed is ${h.testbed}. Apply a config on the Live tests page.`;
      } else banner.className = "banner";
      if (state.page === "overview") renderQuick();
    } catch { /* server unreachable; keep last state */ }
  }

  function setActive(page) {
    document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.page === page));
    const p = PAGES.find((x) => x.id === page);
    $("#page-title").textContent = p ? p.title : "Not found";
    $("#page-sub").textContent = p ? p.sub : "";
    document.title = p && page !== "overview" ? `${p.title} · SCOPE-X Lab` : "SCOPE-X Lab";
  }

  const clsPill = (c) => {
    const key = (c || "untested").replace(/\s*\(.*\)/, "").replace(/\s+/g, "-").replace("violated-by-autoscaler", "violated");
    return `<span class="pill ${key}">${esc(c || "untested")}</span>`;
  };
  const statusPill = (s) => `<span class="status ${s === "done" ? "ok" : s === "running" ? "run" : s === "error" ? "bad" : ""}">${esc(s)}</span>`;

  // ------------------------------------------------------------------ overview
  async function overviewPage() {
    view.innerHTML = `
      <section class="card quick">
        <div class="card-head"><div><h2>Quick stats</h2><div class="muted small">Live values from the testbed (last 30 s window)</div></div></div>
        <div class="tiles" id="tiles"></div>
      </section>
      <div class="ov-grid">
        <section class="card">
          <div class="card-head"><div><h2>Statistics</h2><div class="muted small" id="stat-sub">Checkout p95 per offered load, latest boundary sweep</div></div>
            <div class="legend-dots"><span><i class="ok"></i>within SLO</span><span><i class="bad"></i>violates SLO</span></div></div>
          <div id="stat-chart"></div>
          <div id="stat-note" class="tiny"></div>
        </section>
        <section class="card">
          <div class="card-head"><h2>Testbed services</h2><a class="icon-btn" href="#/live" title="Configure testbed">${icon("server", 16)}</a></div>
          <div id="svc-list" class="svc-list"><div class="muted small">Loading…</div></div>
        </section>
      </div>
      <section class="card">
        <div class="card-head"><div><h2>Test runs</h2><div class="muted small">Finished runs on this server (in-memory, last 20)</div></div>
          <div class="head-actions">
            <select id="run-filter" aria-label="Filter runs"><option value="">All types</option><option>sweep</option><option>flash</option><option>steady</option><option>drop</option></select>
            <a class="btn" href="#/live">${icon("plus", 16)} New test</a></div></div>
        <div class="scroll-x" id="runs"></div>
      </section>`;
    $("#run-filter").addEventListener("change", renderRuns);
    state.constraints = state.constraints || await api("/api/lab/constraints").catch(() => []);
    await refreshOverview();
    every(refreshOverview, 2500);
  }

  async function refreshOverview() {
    if (!$("#tiles")) return;
    const [tb, hist] = await Promise.all([api("/api/testbed").catch(() => null), api("/api/lab/history").catch(() => [])]);
    state.testbed = tb; state.history = hist;
    renderQuick(); renderServices(); renderRuns();
    await renderStats();
  }

  function renderQuick() {
    const el = $("#tiles");
    if (!el) return;
    const h = state.health, tb = state.testbed, hist = state.history || [];
    const p95 = h ? Math.max(h.store_p95_ms || 0, h.load_test_p95_ms || 0) : 0;
    const sweeps = hist.filter((x) => x.kind === "sweep" && x.result && x.result.observed_bound);
    const bound = sweeps.length ? sweeps[0].result.observed_bound.last_rps_meeting_slo : null;
    const stored = (state.constraints || []).length;
    const alive = tb ? tb.services.filter((s) => s.alive).length : 0;
    const t = (ic, v, k, cls = "") => `<div class="tile ${cls}"><span class="ic">${icon(ic, 18)}</span><b>${v}</b><span>${k}</span></div>`;
    el.innerHTML =
      t("server", tb ? `${alive}/${tb.services.length}` : "–", "services up", tb && tb.state !== "running" ? "warn" : "") +
      t("gauge", p95 ? Math.round(p95) + "<small>ms</small>" : "–", "live p95", h && p95 > h.slo.p95_ms ? "bad" : "") +
      t("alert", h ? h.error_pct + "<small>%</small>" : "–", "error rate", h && h.error_pct > h.slo.error_pct ? "bad" : "") +
      t("target", h ? h.slo.p95_ms + "<small>ms</small>" : "–", `SLO p95 · ≤ ${h ? h.slo.error_pct : 1}% err`, "dark") +
      t("sweep", bound != null ? bound + "<small>rps</small>" : "–", "last bound meeting SLO") +
      t("repo", stored || "–", "stored constraints") +
      t("list", hist.length, "runs this session");
  }

  function renderServices() {
    const el = $("#svc-list"), tb = state.testbed;
    if (!el) return;
    if (!tb) { el.innerHTML = `<div class="muted small">Testbed unreachable.</div>`; return; }
    el.innerHTML = tb.services.map((s) => `<div class="svc-row">
        <span class="avatar">${esc(s.service.slice(0, 1).toUpperCase())}</span>
        <div><b>${esc(s.service)}</b><div class="tiny">×${s.workers} worker${s.workers === 1 ? "" : "s"} · :${s.port}${s.ready_seconds != null ? ` · ready in ${s.ready_seconds}s` : ""}</div></div>
        <span class="status ${s.alive ? "ok" : "bad"}">${s.alive ? "Running" : "Down"}</span></div>`).join("") +
      `<div class="svc-foot tiny">Testbed <b>${esc(tb.state)}</b>${tb.error ? ": " + esc(tb.error) : ""} · ${esc(tb.storage || "")}</div>`;
  }

  function renderRuns() {
    const el = $("#runs");
    if (!el) return;
    const f = $("#run-filter").value;
    const rows = (state.history || []).filter((x) => !f || x.kind === f);
    if (!rows.length) { el.innerHTML = `<div class="empty">No ${f ? f + " " : ""}runs yet. <a href="#/live">Start a test</a>.</div>`; return; }
    const outcome = (x) => {
      const r = x.result || {};
      if (r.observed_bound) return `<code>${esc(r.observed_bound.constraint)}</code>`;
      if (r.recovered !== undefined) return r.recovered ? "recovered after spike" : "did not recover";
      if (r.meets_slo !== undefined) return r.meets_slo ? "meets SLO" : "violates SLO";
      if (r.arms) return r.arms.map((a) => `shield ${a.arm}: ${a.units_to_bots} to bots, ${a.oversold_units} oversold`).join(" · ");
      return x.status === "error" ? "error" : "–";
    };
    el.innerHTML = `<table class="t runs"><tr><th>Run</th><th>Type</th><th>Config</th><th>Outcome</th><th>Finished</th><th>Status</th></tr>
      ${rows.map((x) => { const c = x.config || {}; return `<tr><td><code>${esc(x.id)}</code></td><td><span class="kind">${esc(x.kind)}</span></td>
        <td class="tiny">checkout ×${c.checkout_workers ?? "–"} · inv ×${c.inventory_workers ?? "–"} · pay ${c.payment_latency_ms ?? "–"} ms · cache ${c.cache_enabled ? "on" : "off"}</td>
        <td class="small">${outcome(x)}</td><td class="tiny">${ago(x.finished_at)}</td><td>${statusPill(x.status)}</td></tr>`; }).join("")}</table>`;
  }

  async function renderStats() {
    const el = $("#stat-chart");
    if (!el) return;
    const slo = state.health ? state.health.slo.p95_ms : 500;
    const cur = state.lab.test && state.lab.test.status === "running" ? state.lab.test : null;
    let bars = [], sub, note = "";
    if (cur) {
      try { state.lab.test = await api(`/api/lab/tests/${cur.id}`); } catch { /* ignore */ }
      const s = (state.lab.test.seconds || []).slice(-30);
      bars = s.map((x) => ({ label: `${Math.round(x.t)}s`, value: x.checkout_p95_ms, tip: `${Math.round(x.checkout_p95_ms)} ms @ ${x.offered_rps} rps` }));
      sub = `Live: checkout p95 per second, ${state.lab.test.kind} test ${state.lab.test.id}`;
    } else {
      const last = (state.history || []).find((x) => x.kind === "sweep" && x.status === "done");
      if (last) {
        if (!state.sweepCache || state.sweepCache.id !== last.id) {
          try { state.sweepCache = await api(`/api/lab/tests/${last.id}`); } catch { state.sweepCache = { id: last.id, steps: [] }; }
        }
        bars = (state.sweepCache.steps || []).map((s) => ({ label: `${s.offered_rps}`, value: s.checkout_p95_ms, tip: `${Math.round(s.checkout_p95_ms)} ms · ${s.error_pct}% err` }));
        sub = `Checkout p95 per offered load (rps), sweep ${last.id}`;
        const b = last.result.observed_bound;
        note = b ? `<span class="label-chip live">${esc(LABEL())}</span> ${esc(b.constraint)} · tested ${b.tested_region.rps[0]}–${b.tested_region.rps[1]} rps` : "";
      } else sub = "No finished sweep yet. Run a boundary sweep on the Live tests page.";
    }
    if (!$("#stat-chart")) return;
    $("#stat-sub").textContent = sub;
    $("#stat-note").innerHTML = note;
    C.barChart(el, { bars: bars.map((b) => ({ ...b, color: b.value > slo ? C.css("--bad") : C.css("--ink") })), hLine: { y: slo, label: `SLO ${slo} ms` }, yUnit: " ms", height: 330, empty: "No data yet" });
  }

  // ------------------------------------------------------------------ lab pages (unchanged logic)
  function labPage(fn) {
    view.innerHTML = `<div id="lab-body"></div>`;
    return fn();
  }

  // ---- live
  async function liveTab() {
    const body = $("#lab-body");
    const tb = await api("/api/testbed");
    const cfg = tb.config;
    body.innerHTML = `
      <div class="lab-grid">
        <div class="stack">
          <div class="panel controls">
            <h3>Testbed config <span class="label-chip live">${esc(LABEL())}</span></h3>
            <div class="tiny" style="margin-bottom:10px">Apply = reset to a clean baseline and relaunch all six services with these settings. ${esc(tb.storage)}.</div>
            ${slider("checkout_workers", "Checkout workers (replicas)", 1, 8, 1, cfg.checkout_workers)}
            ${slider("inventory_workers", "Inventory workers", 1, 4, 1, cfg.inventory_workers)}
            ${slider("payment_latency_ms", "Payment latency (ms)", 0, 1000, 25, cfg.payment_latency_ms)}
            ${slider("payment_failure_rate", "Payment failure rate", 0, 0.2, 0.01, cfg.payment_failure_rate)}
            ${slider("order_delay_ms", "Order processing delay (ms)", 0, 500, 10, cfg.order_delay_ms)}
            <label class="check"><input type="checkbox" id="cache_enabled" ${cfg.cache_enabled ? "checked" : ""}/> Product cache enabled</label>
            <button class="btn" id="apply" style="width:100%">Apply & reset baseline</button>
            <div id="tb-status"></div>
          </div>
          <div class="panel controls">
            <h3>SLO (configurable defaults)</h3>
            <div style="display:flex;gap:8px"><div class="field" style="flex:1"><label>p95 ≤ (ms)</label><input id="slo-p95" type="number" value="${state.health ? state.health.slo.p95_ms : 500}"/></div>
            <div class="field" style="flex:1"><label>errors ≤ (%)</label><input id="slo-err" type="number" step="0.1" value="${state.health ? state.health.slo.error_pct : 1}"/></div></div>
            <button class="btn secondary" id="slo-save" style="width:100%">Save SLO</button>
          </div>
          <div class="panel controls">
            <h3>Run a constraint test</h3>
            <div class="field"><label>Test</label><select id="t-kind">
              <option value="sweep">Boundary sweep (find the SLO bound)</option>
              <option value="flash">Flash sale (spike, then recovery)</option>
              <option value="steady">Steady load</option></select></div>
            <div id="t-params"></div>
            ${slider("checkout_share", "Share of checkouts (rest = browsing)", 0, 1, 0.05, 0.5)}
            <div style="display:flex;gap:8px"><button class="btn" id="t-run" style="flex:1">Run test</button><button class="btn danger" id="t-cancel" disabled>Stop</button></div>
          </div>
        </div>
        <div class="stack">
          <div class="kpis" id="t-kpis">${kpi("–", "checkout p95 (last s)")}${kpi("–", "error rate")}${kpi("–", "offered rps")}${kpi("–", "in-flight")}</div>
          <div class="panel"><h3>Latest result per experiment type <span class="label-chip live">${esc(LABEL())}</span></h3>
            <div class="tiny" style="margin-bottom:10px">Most recent finished run of each type on this server, at the testbed config it ran with. “Run again” loads that type into the test form.</div>
            <div id="t-types" class="type-grid"></div></div>
          <div class="panel"><h3>Live per-second telemetry</h3><div id="t-legend"></div><div id="t-chart"></div></div>
          <div class="panel"><h3>Result</h3><div id="t-result" class="small muted">Run a test to see the observed constraint.</div></div>
          <div class="panel"><h3>How the constraint changes across configs</h3>
            <div class="tiny" style="margin-bottom:8px">Every finished sweep on this server. Re-run the sweep after changing workers, payment latency or cache to see the bound shift.</div>
            <div id="t-history" class="scroll-x"></div></div>
        </div>
      </div>`;
    bindSliders(body);
    renderParams();
    $("#t-kind").addEventListener("change", renderParams);
    renderTbStatus(tb);
    $("#apply").addEventListener("click", async (e) => {
      e.target.disabled = true; e.target.textContent = "Resetting & relaunching…";
      const conf = Object.fromEntries(["checkout_workers", "inventory_workers", "payment_latency_ms", "payment_failure_rate", "order_delay_ms"].map((k) => [k, +$("#" + k).value]));
      conf.cache_enabled = $("#cache_enabled").checked;
      try { renderTbStatus(await api("/api/testbed/apply", { method: "POST", body: JSON.stringify(conf) })); }
      catch (err) { $("#tb-status").innerHTML = `<div class="notice bad">${esc(err.message)}</div>`; }
      e.target.disabled = false; e.target.textContent = "Apply & reset baseline";
    });
    $("#slo-save").addEventListener("click", async () => {
      await api("/api/lab/slo", { method: "POST", body: JSON.stringify({ p95_ms: +$("#slo-p95").value, error_pct: +$("#slo-err").value }) });
      pollHealth();
    });
    $("#t-run").addEventListener("click", startTest);
    $("#t-cancel").addEventListener("click", async () => { if (state.lab.test) await api(`/api/lab/tests/${state.lab.test.id}/cancel`, { method: "POST" }); });
    renderHistory();
    renderTypes();
    if (state.lab.test) { renderTest(state.lab.test); if (state.lab.test.status === "running") pollTest(); }
    else drawLive({ seconds: [] });
  }

  // ---- latest values per experiment type
  const TYPES = [
    { kind: "sweep", name: "Boundary sweep", what: "Steps offered load up until the SLO breaks" },
    { kind: "flash", name: "Flash sale", what: "Baseline → spike → baseline; checks recovery" },
    { kind: "steady", name: "Steady load", what: "Constant offered load for a fixed duration" },
    { kind: "drop", name: "Drop test", what: "Limited-stock drop, shield off vs on" },
  ];
  async function renderTypes() {
    const el = $("#t-types");
    if (!el) return;
    const hist = await api("/api/lab/history").catch(() => []);
    state.typeCache = state.typeCache || {};
    const latest = {};
    for (const T of TYPES) {
      const h = hist.find((x) => x.kind === T.kind && x.status === "done");
      if (!h) continue;
      if (!state.typeCache[h.id]) { try { state.typeCache[h.id] = await api(`/api/lab/tests/${h.id}`); } catch { state.typeCache[h.id] = { ...h, steps: [], params: {} }; } }
      latest[T.kind] = { h, t: state.typeCache[h.id] };
    }
    if (!$("#t-types")) return;
    const slo = state.health ? state.health.slo : { p95_ms: 500, error_pct: 1 };
    const v = (label, value, bad) => `<div class="tv"><span>${label}</span><b class="${bad === true ? "bad" : bad === false ? "ok" : ""}">${value ?? "–"}</b></div>`;
    const ms = (x) => (x == null ? "–" : Math.round(x) + " ms");
    const cfgLine = (c) => c ? `checkout ×${c.checkout_workers} · inv ×${c.inventory_workers} · pay ${c.payment_latency_ms} ms · cache ${c.cache_enabled ? "on" : "off"}` : "";
    const body = (kind, { h, t }) => {
      const r = h.result || {}, st = t.steps || [], p = t.params || {};
      if (kind === "sweep") {
        const b = r.observed_bound || {};
        const worst = st.length ? Math.max(...st.map((s) => s.checkout_p95_ms)) : null;
        return v("last rps meeting SLO", b.last_rps_meeting_slo != null ? b.last_rps_meeting_slo + " rps" : "none", b.last_rps_meeting_slo == null) +
          v("first rps violating SLO", b.first_rps_violating_slo != null ? b.first_rps_violating_slo + " rps" : "none seen", b.first_rps_violating_slo != null) +
          v("tested region", b.tested_region ? `${b.tested_region.rps[0]}–${b.tested_region.rps[1]} rps` : "–") +
          v("levels · s/level", `${st.length} · ${p.step_seconds ?? "–"} s`) +
          v("worst checkout p95", ms(worst), worst != null && worst > slo.p95_ms) +
          v("client-saturated steps", (b.client_saturated_steps || []).length ? b.client_saturated_steps.join(", ") + " rps" : "none") +
          `<div class="tconstraint"><code>${esc(b.constraint || "")}</code></div>`;
      }
      if (kind === "flash") {
        const ph = r.phases || {};
        const f = (k) => ph[k] || st.find((s) => s.phase === k) || {};
        const b = f("before"), sp = f("spike"), a = f("after");
        return v("baseline → spike", `${p.base_rps ?? b.offered_rps} → ${p.peak_rps ?? sp.offered_rps} rps`) +
          v("spike duration", (p.duration ?? "–") + " s") +
          v("p95 before", ms(b.checkout_p95_ms), b.checkout_p95_ms > slo.p95_ms) +
          v("p95 during spike", ms(sp.checkout_p95_ms), sp.checkout_p95_ms > slo.p95_ms) +
          v("p95 after", ms(a.checkout_p95_ms), a.checkout_p95_ms > slo.p95_ms) +
          v("errors during spike", sp.error_pct != null ? sp.error_pct + "%" : "–", sp.error_pct > slo.error_pct) +
          v("achieved during spike", sp.achieved_rps != null ? sp.achieved_rps + " rps" : "–") +
          v("recovered", r.recovered === undefined ? "–" : r.recovered ? "yes" : "no", r.recovered === undefined ? undefined : !r.recovered);
      }
      if (kind === "steady") {
        const s = st[0] || {};
        return v("offered → achieved", `${s.offered_rps ?? p.rps} → ${s.achieved_rps ?? "–"} rps`) +
          v("duration", (p.duration ?? "–") + " s") +
          v("checkout p50", ms(s.checkout_p50_ms)) +
          v("checkout p95", ms(s.checkout_p95_ms), s.checkout_p95_ms > slo.p95_ms) +
          v("browse p95", ms(s.browse_p95_ms)) +
          v("error rate", s.error_pct != null ? s.error_pct + "%" : "–", s.error_pct > slo.error_pct) +
          v("meets SLO", r.meets_slo === undefined ? "–" : r.meets_slo ? "yes" : "no", r.meets_slo === undefined ? undefined : !r.meets_slo);
      }
      const A = r.arms || [];
      const both = (f) => A.map((a) => `${a.arm}: ${f(a)}`).join(" · ");
      return v("stock · humans · bots", r.params ? `${r.params.stock} · ${r.params.humans} · ${r.params.bots}` : "–") +
        v("units to bots", both((a) => `${a.units_to_bots} (${a.bot_share_pct}%)`)) +
        v("oversold units", both((a) => a.oversold_units)) +
        v("humans who got one", both((a) => a.human_success_pct + "%")) +
        v("human checkout p95", both((a) => ms(a.human_checkout_p95_ms))) +
        v("server errors", both((a) => a.server_error_pct + "%")) +
        v("max queue", both((a) => a.max_queue));
    };
    el.innerHTML = TYPES.map((T) => {
      const L = latest[T.kind];
      return `<div class="type-card">
        <div class="type-top"><div><b>${T.name}</b><div class="tiny">${T.what}</div></div>
          ${L ? `<span class="tiny">${L.h.id} · ${ago(L.h.finished_at)}</span>` : `<span class="status">not run</span>`}</div>
        ${L ? `<div class="tvals">${body(T.kind, L)}</div><div class="tiny tcfg">${cfgLine(L.h.config || (L.t.result || {}).testbed_config)}</div>`
            : `<div class="empty small">No finished ${T.name.toLowerCase()} yet.</div>`}
        <button class="btn secondary" data-run-type="${T.kind}">${L ? "Run again" : "Set up this test"}</button></div>`;
    }).join("");
    el.querySelectorAll("[data-run-type]").forEach((b) => b.addEventListener("click", () => {
      const k = b.dataset.runType;
      if (k === "drop") { location.hash = "#/shield"; return; }
      $("#t-kind").value = k; renderParams();
      $("#t-kind").scrollIntoView({ behavior: "smooth", block: "center" });
    }));
  }

  function slider(id, label, min, max, step, value) {
    return `<div class="field"><span><label for="${id}">${label}</label><b id="${id}-v">${value}</b></span>
      <input type="range" id="${id}" min="${min}" max="${max}" step="${step}" value="${value}"/></div>`;
  }
  function bindSliders(root, onChange) {
    root.querySelectorAll('input[type="range"]').forEach((r) => r.addEventListener("input", () => {
      $("#" + r.id + "-v").textContent = r.value;
      if (onChange) onChange();
    }));
  }
  const kpi = (v, k, color) => `<div class="kpi"><div class="v" style="${color ? "color:" + color : ""}">${v}</div><div class="k">${k}</div></div>`;

  function renderParams() {
    const kind = $("#t-kind").value;
    const p = $("#t-params");
    if (kind === "sweep") p.innerHTML = `<div class="field"><label>Offered rps levels</label><input id="p-levels" value="25, 50, 100, 200, 300, 450"/></div>
      ${slider("p-step", "Seconds per level", 3, 20, 1, 5)}`;
    else if (kind === "flash") p.innerHTML = `${slider("p-base", "Baseline rps", 1, 100, 1, 10)}${slider("p-peak", "Spike rps", 10, 800, 10, 300)}${slider("p-dur", "Spike seconds", 3, 30, 1, 8)}`;
    else p.innerHTML = `${slider("p-rps", "Offered rps", 1, 800, 1, 50)}${slider("p-dur", "Duration (s)", 3, 60, 1, 15)}`;
    bindSliders(p);
  }

  function renderTbStatus(tb) {
    $("#tb-status").innerHTML = `<div class="notice small ${tb.state === "running" ? "good" : tb.state === "error" ? "bad" : ""}">Testbed <b>${tb.state}</b>${tb.error ? ": " + esc(tb.error) : ""}</div>
      <div class="svc">${tb.services.map((s) => `<div>${s.alive ? "🟢" : "⚪"} ${s.service} ×${s.workers}<br><span class="tiny">${s.ready_seconds !== null ? "ready in " + s.ready_seconds + "s" : ":" + s.port}</span></div>`).join("")}</div>`;
  }

  async function startTest() {
    const kind = $("#t-kind").value;
    const share = +$("#checkout_share").value;
    let params;
    if (kind === "sweep") params = { levels: $("#p-levels").value.split(/[,\s]+/).filter(Boolean).map(Number), step_seconds: +$("#p-step").value };
    else if (kind === "flash") params = { base_rps: +$("#p-base").value, peak_rps: +$("#p-peak").value, duration: +$("#p-dur").value };
    else params = { rps: +$("#p-rps").value, duration: +$("#p-dur").value };
    params.checkout_share = share;
    try {
      state.lab.test = await api("/api/lab/tests", { method: "POST", body: JSON.stringify({ kind, params }) });
      $("#t-run").disabled = true; $("#t-cancel").disabled = false;
      $("#t-result").innerHTML = `<span class="muted">Running…</span>`;
      pollTest();
    } catch (err) { $("#t-result").innerHTML = `<div class="notice bad">${esc(err.message)}</div>`; }
  }

  function pollTest() {
    clearInterval(state.lab.poll);
    state.lab.poll = setInterval(async () => {
      if (!state.lab.test) return;
      const t = await api(`/api/lab/tests/${state.lab.test.id}`);
      state.lab.test = t;
      if (!$("#t-chart")) { if (t.status !== "running") clearInterval(state.lab.poll); return; }
      renderTest(t);
      if (t.status !== "running") { clearInterval(state.lab.poll); renderHistory(); renderTypes(); pollHealth(); }
    }, 1000);
  }

  function drawLive(t) {
    const slo = state.health ? state.health.slo.p95_ms : 500;
    const s = t.seconds || [];
    const series = [
      { name: "checkout p95 (ms)", color: C.css("--s1"), points: s.filter((x) => x.checkout_p95_ms > 0).map((x) => [x.t, x.checkout_p95_ms]) },
      { name: "browse p95 (ms)", color: C.css("--s3"), points: s.filter((x) => x.browse_p95_ms > 0).map((x) => [x.t, x.browse_p95_ms]) },
      { name: "offered rps", color: C.css("--s2"), points: s.map((x) => [x.t, x.offered_rps]), dash: true },
    ];
    C.legend($("#t-legend"), [...series.map((x) => ({ name: x.name, color: x.color, dash: x.dash })), { name: `SLO p95 ${slo} ms`, color: C.css("--bad"), dash: true }]);
    C.lineChart($("#t-chart"), { series, xLabel: "seconds", yLabel: "ms · rps (log)", logY: true, hLines: [{ y: slo, label: "SLO" }], empty: "Run a test to stream per-second telemetry" });
  }

  function renderTest(t) {
    $("#t-run").disabled = t.status === "running"; $("#t-cancel").disabled = t.status !== "running";
    const last = t.seconds[t.seconds.length - 1];
    const slo = state.health ? state.health.slo : { p95_ms: 500, error_pct: 1 };
    if (last) {
      $("#t-kpis").innerHTML = kpi(Math.round(last.checkout_p95_ms) + " ms", "checkout p95 (last s)", last.checkout_p95_ms > slo.p95_ms ? "var(--bad)" : "") +
        kpi(last.error_pct + "%", "error rate", last.error_pct > slo.error_pct ? "var(--bad)" : "") + kpi(last.offered_rps, "offered rps") + kpi(last.inflight, "in-flight");
    }
    drawLive(t);
    if (t.status === "running") return;
    if (t.status === "error") { $("#t-result").innerHTML = `<div class="notice bad">${esc(t.error)}</div>`; return; }
    const r = t.result;
    const rows = t.steps.map((s) => `<tr><td>${s.phase || s.offered_rps}</td><td>${s.offered_rps}</td><td>${s.achieved_rps}</td><td>${s.checkout_p50_ms}</td><td><b>${s.checkout_p95_ms}</b></td><td>${s.error_pct}%</td>
      <td>${s.client_saturated ? "⚠️ client" : s.server_throughput_below_offered ? "server < offered" : "ok"}</td><td>${s.meets_slo === undefined ? "" : s.meets_slo ? clsPill("valid") : '<span class="pill fires">violates</span>'}</td></tr>`).join("");
    let head = "";
    if (r.observed_bound) {
      head = `<div class="notice ${r.observed_bound.first_rps_violating_slo ? "" : "good"}"><span class="label-chip live">${esc(LABEL())}</span><br><b>Observed constraint:</b> <code>${esc(r.observed_bound.constraint)}</code>
        <div class="tiny">Tested region: ${esc(JSON.stringify(r.observed_bound.tested_region))}${r.aborted ? ` · abort condition: ${esc(r.aborted)}` : ""}</div></div>`;
    } else if (r.recovered !== undefined) {
      head = `<div class="notice ${r.recovered ? "good" : "bad"}">Recovery after spike: <b>${r.recovered ? "recovered to baseline" : "did not recover within 8 s"}</b></div>`;
    } else if (r.meets_slo !== undefined) {
      head = `<div class="notice ${r.meets_slo ? "good" : "bad"}">Steady load ${r.meets_slo ? "meets" : "violates"} the SLO.</div>`;
    }
    $("#t-result").innerHTML = head + `<div class="scroll-x"><table class="t"><tr><th>step</th><th>offered</th><th>achieved</th><th>p50 ms</th><th>p95 ms</th><th>errors</th><th>validity</th><th>SLO</th></tr>${rows}</table></div>
      <div class="tiny" style="margin-top:6px">“⚠️ client” = the load generator could not send at the requested rate, so that step measures the generator as much as the store.</div>`;
  }

  async function renderHistory() {
    const el = $("#t-history");
    if (!el) return;
    const h = (await api("/api/lab/history")).filter((x) => x.kind === "sweep" && x.result.observed_bound);
    if (!h.length) { el.innerHTML = `<span class="small muted">No sweeps yet.</span>`; return; }
    el.innerHTML = `<table class="t"><tr><th>checkout ×</th><th>inventory ×</th><th>payment ms</th><th>fail rate</th><th>cache</th><th>last rps OK</th><th>first rps ✗</th></tr>
      ${h.map((x) => { const c = x.config, b = x.result.observed_bound; return `<tr><td>${c.checkout_workers}</td><td>${c.inventory_workers}</td><td>${c.payment_latency_ms}</td><td>${c.payment_failure_rate}</td><td>${c.cache_enabled ? "on" : "off"}</td><td><b>${b.last_rps_meeting_slo ?? "–"}</b></td><td>${b.first_rps_violating_slo ?? "none seen"}</td></tr>`; }).join("")}</table>`;
  }

  // ---- model
  async function modelTab() {
    const body = $("#lab-body");
    const w = state.lab.whatifCfg || { replicas: 4, cpu_cores: 0.5, memory_mib: 1024, db_pool_size: 50, cache_enabled: true, target_rps: 300, workload: "flash_sale", mode: "static" };
    state.lab.whatifCfg = w;
    body.innerHTML = `
      <div class="lab-grid">
        <div class="panel controls">
          <h3>Cluster config <span class="label-chip model">model (simulated)</span></h3>
          <div class="tiny" style="margin-bottom:10px">SCOPE-X queuing model + the Task 14D CPU capacity model. Shows how stored constraints apply to a configuration you can't run on one laptop.</div>
          ${slider("w-replicas", "Checkout replicas", 1, 10, 1, w.replicas)}
          <div class="field"><label>CPU limit per replica</label><select id="w-cpu">${[0.5, 1, 2, 4].map((c) => `<option value="${c}" ${+w.cpu_cores === c ? "selected" : ""}>${c} core${c === 1 ? "" : "s"}</option>`).join("")}</select></div>
          <div class="field"><label>Memory limit</label><select id="w-mem">${[512, 1024, 2048].map((m) => `<option value="${m}" ${+w.memory_mib === m ? "selected" : ""}>${m >= 1024 ? m / 1024 + " GiB" : m + " MiB"}</option>`).join("")}</select></div>
          ${slider("w-pool", "DB pool size", 5, 150, 5, w.db_pool_size)}
          ${slider("w-rps", "Offered load (rps)", 10, 1500, 10, w.target_rps)}
          <label class="check"><input type="checkbox" id="w-cache" ${w.cache_enabled ? "checked" : ""}/> Cache enabled</label>
          <div class="field"><label>Workload mix</label><select id="w-work">${["flash_sale", "normal", "ramp", "viral_product", "slow_payment_api"].map((x) => `<option ${w.workload === x ? "selected" : ""}>${x}</option>`).join("")}</select></div>
          <div class="field"><label>Scaling mode (which validity applies)</label><select id="w-mode">${["static", "hpa", "vertical"].map((x) => `<option ${w.mode === x ? "selected" : ""}>${x}</option>`).join("")}</select></div>
        </div>
        <div class="stack">
          <div class="kpis" id="w-kpis"></div>
          <div id="w-warn"></div>
          <div class="panel"><h3>Predicted p95 vs offered load</h3><div id="w-legend1"></div><div id="w-curve"></div></div>
          <div class="panel"><h3>How the SLO boundary moves with scale</h3>
            <div class="tiny">Max rps the model keeps within SLO. Horizontal: vs replicas at the current CPU limit. Vertical: vs CPU limit at the current replica count. The dashed line is the DB-pool connection cap (K01: replicas ≤ pool / 6).</div>
            <div id="w-legend2"></div><div id="w-bound"></div><div id="w-bound-cpu"></div></div>
          <div class="panel"><h3>Stored constraints at this config</h3><div class="tiny" style="margin-bottom:8px">“Fires” = the rule's condition holds here (it predicts an SLO violation or a broken invariant). Validity = what the scaling study found for the selected mode.</div><div id="w-cons" class="scroll-x"></div></div>
        </div>
      </div>`;
    const read = () => ({
      replicas: +$("#w-replicas").value, cpu_cores: +$("#w-cpu").value, memory_mib: +$("#w-mem").value, db_pool_size: +$("#w-pool").value,
      target_rps: +$("#w-rps").value, cache_enabled: $("#w-cache").checked, workload: $("#w-work").value, mode: $("#w-mode").value,
    });
    const update = () => {
      state.lab.whatifCfg = read();
      clearTimeout(state.lab.whatifTimer);
      state.lab.whatifTimer = setTimeout(runWhatIf, 180);
    };
    bindSliders(body, update);
    body.querySelectorAll("select, input[type=checkbox]").forEach((x) => x.addEventListener("change", update));
    runWhatIf();
  }

  async function runWhatIf() {
    const cfg = state.lab.whatifCfg;
    const r = await api("/api/lab/whatif", { method: "POST", body: JSON.stringify(cfg) });
    if (!$("#w-kpis")) return;
    const slo = r.slo, cap = r.capacity;
    $("#w-kpis").innerHTML = kpi(Math.round(r.prediction.p95_ms) + " ms", "predicted p95", r.prediction.p95_ms > slo.p95_ms ? "var(--bad)" : "var(--good)") +
      kpi(r.prediction.error_pct + "%", "predicted errors", r.prediction.error_pct > slo.error_pct ? "var(--bad)" : "") +
      kpi(Math.round(cap.max_safe_rps), "max rps within SLO") + kpi(cap.bottleneck.split(" ")[0], `bottleneck · ${cap.per_replica_rps} rps/replica`);
    $("#w-warn").innerHTML = r.warnings.map((w) => `<div class="notice bad small">⚠️ ${esc(w)}</div>`).join("") +
      `<div class="notice small">Frontend capacity <b>${cap.frontend_rps}</b> rps vs database <b>${cap.database_rps}</b> rps → <b>${esc(cap.bottleneck)}</b> binds first. Headroom at ${cfg.target_rps} rps: <b>${cap.headroom_pct}%</b>.</div>`;
    const curve = [
      { name: "predicted p95", color: C.css("--s1"), points: r.curve.map((p) => [p.rps, p.p95_ms]) },
    ];
    C.legend($("#w-legend1"), [{ name: "predicted p95 (ms)", color: C.css("--s1") }, { name: `SLO ${slo.p95_ms} ms`, color: C.css("--bad"), dash: true }, { name: "your load", color: C.css("--ink-2"), dash: true }]);
    C.lineChart($("#w-curve"), { series: curve, xLabel: "offered rps", yLabel: "p95 ms", hLines: [{ y: slo.p95_ms, label: "SLO" }], vLines: [{ x: cfg.target_rps, label: `${cfg.target_rps} rps` }], yUnit: " ms", height: 240 });
    C.legend($("#w-legend2"), [{ name: "max safe rps vs replicas", color: C.css("--s2") }, { name: "max safe rps vs CPU limit", color: C.css("--s3") }]);
    C.lineChart($("#w-bound"), {
      series: [{ name: "max safe rps", color: C.css("--s2"), points: r.boundary_by_replicas.map((b) => [b.replicas, b.max_safe_rps]), markers: true }],
      xLabel: "checkout replicas", yLabel: "rps", vLines: [{ x: cap.max_replicas_by_pool, label: `pool cap: ${cap.max_replicas_by_pool} replicas` }, { x: cfg.replicas, label: "now", color: C.css("--s1") }], height: 220, yMin: 0,
    });
    C.lineChart($("#w-bound-cpu"), {
      series: [{ name: "max safe rps", color: C.css("--s3"), points: r.boundary_by_cpu.map((b) => [b.cpu_cores, b.max_safe_rps]), markers: true }],
      xLabel: "CPU limit (cores)", yLabel: "rps", height: 200, yMin: 0, xFmt: (v) => +v.toFixed(2) + "",
    });
    $("#w-cons").innerHTML = `<table class="t"><tr><th>ID</th><th>rule (condition ⇒ violation)</th><th>fires here?</th><th>validity in ${esc(cfg.mode)} mode</th><th>static</th><th>HPA</th><th>vertical</th></tr>
      ${r.constraints.map((c) => `<tr><td><b>${c.id}</b><div class="tiny">${esc(c.kind)}</div></td><td><code>${esc(c.rule)}</code></td><td>${c.fires ? '<span class="pill fires">fires</span>' : '<span class="pill">no</span>'}</td>
        <td>${clsPill(c.validity)}</td><td>${clsPill(c.classes.static)}</td><td>${clsPill(c.classes.hpa)}</td><td>${clsPill(c.classes.vertical)}</td></tr>`).join("")}</table>`;
  }


  // ---- flash-sale shield
  const toggle = (id, label, on, hint) => `<label class="check"><input type="checkbox" id="${id}" ${on ? "checked" : ""}/> <span><b>${label}</b>${hint ? `<br><span class="tiny">${hint}</span>` : ""}</span></label>`;

  async function shieldTab() {
    const body = $("#lab-body");
    const st = await api("/api/lab/shield");
    const c = st.config;
    body.innerHTML = `
      <div class="lab-grid">
        <div class="stack">
          <div class="panel controls">
            <h3>Shield layers <span class="label-chip live">live on this store</span></h3>
            <div class="tiny" style="margin-bottom:8px">Changes apply instantly to real shoppers (no restart). The shield guards add-to-cart and checkout; browsing stays open.</div>
            ${toggle("sh-waiting_room", "Virtual waiting room", c.waiting_room, "Queue new shoppers when traffic passes a threshold")}
            ${toggle("sh-force_queue", "Drop mode: always queue", c.force_queue, "Queue everyone, regardless of load")}
            ${toggle("sh-reservations", "Stock reservation with TTL", c.reservations, "Add-to-cart holds stock; unpaid holds expire")}
            ${toggle("sh-bot_defense", "Bot defence at the edge", c.bot_defense, "Rate limits, behaviour score, proof-of-work")}
            <h3 style="margin-top:12px">Waiting room</h3>
            ${slider("sh-admit_per_minute", "Admit shoppers / minute", 10, 3000, 10, c.admit_per_minute)}
            ${slider("sh-activate_rps", "Open when purchase rps ≥", 1, 400, 1, c.activate_rps)}
            ${slider("sh-max_active_shoppers", "…or active shoppers ≥", 5, 2000, 5, c.max_active_shoppers)}
            <h3 style="margin-top:12px">Reservations</h3>
            ${slider("sh-hold_ttl_s", "Hold TTL (s)", 5, 900, 5, c.hold_ttl_s)}
            ${slider("sh-max_per_customer", "Limited items: max per customer", 1, 10, 1, c.max_per_customer)}
            <h3 style="margin-top:12px">Bot defence</h3>
            ${slider("sh-ip_rate_per_s", "Per-IP rate (req/s)", 1, 50, 1, c.ip_rate_per_s)}
            ${slider("sh-device_rate_per_s", "Per-device rate (req/s)", 1, 50, 1, c.device_rate_per_s)}
            ${slider("sh-pow_bits", "Proof-of-work difficulty (bits)", 8, 20, 1, c.pow_bits)}
            ${slider("sh-challenge_score", "Challenge at score ≥", 0, 100, 5, c.challenge_score)}
            ${slider("sh-block_score", "Block at score ≥", 0, 100, 5, c.block_score)}
            <div style="display:flex;gap:8px"><button class="btn" id="sh-save" style="flex:1">Apply shield settings</button><button class="btn ghost" id="sh-reset" title="Clears queue, passes and counters">Clear state</button></div>
            <div id="sh-msg"></div>
          </div>
          <div class="panel controls">
            <h3>Drop test: shield off vs on</h3>
            <div class="tiny" style="margin-bottom:8px">Simulated shoppers and bots buy the limited turntable through the store's real API. Each arm resets its stock to a known baseline first.</div>
            ${slider("d-stock", "Units in the drop", 5, 200, 5, 50)}
            ${slider("d-humans", "Human shoppers", 20, 600, 10, 200)}
            ${slider("d-bots", "Bots", 0, 40, 1, 10)}
            ${slider("d-bot_rps", "Attempts / s per bot", 1, 30, 1, 8)}
            ${slider("d-arrival_s", "Humans arrive over (s)", 2, 30, 1, 10)}
            ${slider("d-abandon_pct", "Share who abandon their cart", 0, 0.8, 0.05, 0.25)}
            ${slider("d-hold_ttl_s", "Hold TTL during test (s)", 5, 120, 5, 20)}
            ${slider("d-patience_s", "Human patience in queue (s)", 10, 120, 5, 45)}
            <label class="check"><input type="checkbox" id="d-bots_solve_pow"/> Bots also solve proof-of-work</label>
            <div class="field"><label>Arms</label><select id="d-arms"><option value="both">Both: off, then on</option><option value="off">Shield off only</option><option value="on">Shield on only</option></select></div>
            <div style="display:flex;gap:8px"><button class="btn" id="d-run" style="flex:1">Run drop test</button><button class="btn danger" id="d-cancel" disabled>Stop</button></div>
          </div>
        </div>
        <div class="stack">
          <div class="kpis" id="sh-kpis"></div>
          <div class="panel"><h3>Shield right now</h3><div id="sh-legend"></div><div id="sh-chart"></div><div id="sh-counters" class="tiny"></div></div>
          <div class="panel"><h3>Drop test: stock over time</h3><div id="d-legend1"></div><div id="d-stock-chart"></div>
            <h3 style="margin-top:14px">Queue and human checkout latency</h3><div id="d-legend2"></div><div id="d-queue-chart"></div><div id="d-lat-chart"></div></div>
          <div class="panel"><h3>Drop test result</h3><div id="d-result" class="small muted">Run the drop test to compare the store with and without the shield.</div></div>
        </div>
      </div>`;
    bindSliders(body);
    const keys = ["admit_per_minute", "activate_rps", "max_active_shoppers", "hold_ttl_s", "max_per_customer", "ip_rate_per_s", "device_rate_per_s", "pow_bits", "challenge_score", "block_score"];
    $("#sh-save").addEventListener("click", async () => {
      const patch = Object.fromEntries(keys.map((k) => [k, +$("#sh-" + k).value]));
      ["waiting_room", "force_queue", "reservations", "bot_defense"].forEach((k) => (patch[k] = $("#sh-" + k).checked));
      try { renderShield(await api("/api/lab/shield/config", { method: "POST", body: JSON.stringify(patch) })); $("#sh-msg").innerHTML = `<div class="notice good small">Applied.</div>`; pollHealth(); }
      catch (err) { $("#sh-msg").innerHTML = `<div class="notice bad small">${esc(err.message)}</div>`; }
    });
    $("#sh-reset").addEventListener("click", async () => renderShield(await api("/api/lab/shield/reset", { method: "POST" })));
    $("#d-run").addEventListener("click", startDrop);
    $("#d-cancel").addEventListener("click", async () => { if (state.lab.drop) await api(`/api/lab/tests/${state.lab.drop.id}/cancel`, { method: "POST" }); });
    renderShield(st);
    state.lab.shieldPoll = setInterval(async () => {
      if (!$("#sh-kpis")) return clearInterval(state.lab.shieldPoll);
      try { renderShield(await api("/api/lab/shield")); } catch { /* ignore */ }
      if (state.lab.drop && state.lab.drop.status === "running") {
        state.lab.drop = await api(`/api/lab/tests/${state.lab.drop.id}`);
        renderDrop(state.lab.drop);
      }
    }, 1500);
    if (!state.lab.drop) {
      const last = (await api("/api/lab/history")).find((x) => x.kind === "drop");
      if (last) { try { state.lab.drop = await api(`/api/lab/tests/${last.id}`); } catch { /* server restarted */ } }
    }
    renderDrop(state.lab.drop || { seconds: [], status: "idle" });
  }

  function renderShield(st) {
    if (!$("#sh-kpis")) return;
    const c = st.counters || {};
    const layers = ["waiting_room", "reservations", "bot_defense"].filter((k) => st.config[k]).length;
    $("#sh-kpis").innerHTML = kpi(st.room_open ? "open" : "closed", `waiting room · ${layers}/3 layers on`, st.room_open ? "var(--s2)" : "") +
      kpi(st.queue_length, `in line (max ${st.max_queue})`) + kpi(st.active_shoppers, "active shoppers") + kpi(st.purchase_rps, "purchase-path rps (5 s)");
    const tl = st.timeline || [];
    const t0 = tl.length ? tl[0].ts : 0;
    const series = [
      { name: "in line", color: C.css("--s1"), points: tl.map((x) => [x.ts - t0, x.queue]) },
      { name: "active shoppers", color: C.css("--s3"), points: tl.map((x) => [x.ts - t0, x.active_shoppers]) },
      { name: "blocked + rate-limited / s", color: C.css("--s2"), points: tl.map((x) => [x.ts - t0, (x.blocked || 0) + (x.rate_limited_ip || 0) + (x.rate_limited_device || 0)]) },
    ];
    C.legend($("#sh-legend"), series.map((x) => ({ name: x.name, color: x.color })));
    C.lineChart($("#sh-chart"), { series, xLabel: "seconds (last 3 min)", yLabel: "count", height: 200, yMin: 0, empty: "No purchase-path traffic yet" });
    const show = ["requests", "queue_required", "joined_queue", "challenged", "pow_solved", "pow_failed", "blocked", "rate_limited_ip", "rate_limited_device"];
    $("#sh-counters").innerHTML = show.map((k) => `${k.replace(/_/g, " ")}: <b>${c[k] || 0}</b>`).join(" · ") +
      (st.wait_p95_s != null ? ` · queue wait p95: <b>${st.wait_p95_s}s</b>` : "") + ` · admitted: <b>${st.admitted_total}</b>`;
  }

  async function startDrop() {
    const ids = ["stock", "humans", "bots", "bot_rps", "arrival_s", "abandon_pct", "hold_ttl_s", "patience_s"];
    const params = Object.fromEntries(ids.map((k) => [k, +$("#d-" + k).value]));
    params.bots_solve_pow = $("#d-bots_solve_pow").checked;
    params.arms = $("#d-arms").value;
    try {
      state.lab.drop = await api("/api/lab/tests", { method: "POST", body: JSON.stringify({ kind: "drop", params }) });
      $("#d-result").innerHTML = `<span class="muted">Running… each arm takes roughly arrival + checkout time + the hold TTL.</span>`;
      renderDrop(state.lab.drop);
    } catch (err) { $("#d-result").innerHTML = `<div class="notice bad">${esc(err.message)}</div>`; }
  }

  function renderDrop(t) {
    if (!$("#d-run")) return;
    const running = t.status === "running";
    $("#d-run").disabled = running; $("#d-cancel").disabled = !running;
    const s = t.seconds || [];
    const arms = [...new Set(s.map((x) => x.arm))];
    const starts = arms.map((a) => ({ x: s.find((x) => x.arm === a).t, label: `shield ${a}` }));
    const span = s.length ? { xMin: s[0].t, xMax: s[s.length - 1].t } : {};
    const lines = (key, name, color) => arms.map((a) => ({ name: arms.length > 1 ? `${name} (${a})` : name, color, dash: a === "off", points: s.filter((x) => x.arm === a && x[key] != null).map((x) => [x.t, x[key]]) }));
    const stockSeries = [...lines("available", "available", C.css("--s1")), ...lines("held", "reserved", C.css("--s4")), ...lines("sold", "sold", C.css("--s3"))];
    C.legend($("#d-legend1"), [{ name: "available", color: C.css("--s1") }, { name: "reserved (held / checking out)", color: C.css("--s4") }, { name: "units sold", color: C.css("--s3") }, { name: "stock", color: C.css("--bad"), dash: true }, { name: "dashed segment = shield off", color: C.css("--ink-2"), dash: true }]);
    const stock = t.params && t.params.stock;
    C.lineChart($("#d-stock-chart"), { ...span, series: stockSeries, xLabel: "seconds", yLabel: "units", height: 240, yMin: 0, vLines: starts, hLines: stock ? [{ y: stock, label: "stock" }] : [], empty: "Run the drop test" });
    C.legend($("#d-legend2"), [{ name: "shoppers in line", color: C.css("--s2") }, { name: "human checkout p95 (ms)", color: C.css("--s1") }, { name: `SLO ${state.health ? state.health.slo.p95_ms : 500} ms`, color: C.css("--bad"), dash: true }]);
    C.lineChart($("#d-queue-chart"), { ...span, series: lines("queue", "in line", C.css("--s2")), xLabel: "seconds", yLabel: "in line", height: 170, yMin: 0, vLines: starts, empty: "" });
    C.lineChart($("#d-lat-chart"), { ...span, series: lines("checkout_p95_ms", "checkout p95", C.css("--s1")).map((x) => ({ ...x, points: x.points.filter((p) => p[1] > 0) })), xLabel: "seconds", yLabel: "ms", height: 170, yMin: 0, yUnit: " ms", vLines: starts, hLines: [{ y: state.health ? state.health.slo.p95_ms : 500, label: "SLO" }], empty: "" });
    if (running || t.status === "idle") return;
    if (t.status === "error") { $("#d-result").innerHTML = `<div class="notice bad">${esc(t.error)}</div>`; return; }
    const r = t.result || {};
    const A = r.arms || [];
    if (!A.length) { $("#d-result").innerHTML = `<span class="muted">Cancelled before an arm finished.</span>`; return; }
    const fmtObj = (o) => Object.entries(o || {}).map(([k, v]) => `${k.replace(/_/g, " ")} ${v}`).join(", ") || "–";
    const row = (label, f, good) => `<tr><td>${label}</td>${A.map((a) => { const v = f(a); const g = good && v != null ? good(a) : null; return `<td>${g === null ? "" : g ? "🟢 " : "🔴 "}<b>${v ?? "–"}</b></td>`; }).join("")}</tr>`;
    $("#d-result").innerHTML = `<div class="notice"><span class="label-chip live">${esc(LABEL())}</span> Drop of <b>${r.params.stock}</b> units, ${r.params.humans} humans, ${r.params.bots} bots at ${r.params.bot_rps}/s, ${Math.round(r.params.abandon_pct * 100)}% abandon, hold TTL ${r.params.hold_ttl_s}s. <span class="tiny">${esc(r.caveat)}</span></div>
      <div class="scroll-x"><table class="t"><tr><th>metric</th>${A.map((a) => `<th>shield ${a.arm}</th>`).join("")}</tr>
      ${row("units sold / stock", (a) => `${a.units_sold} / ${a.stock}`)}
      ${row("oversold units", (a) => a.oversold_units, (a) => a.oversold_units === 0)}
      ${row("units bought by bots", (a) => `${a.units_to_bots} (${a.bot_share_pct}%)`, (a) => a.bot_share_pct < 5)}
      ${row("units bought by humans", (a) => a.units_to_humans)}
      ${row("humans who tried and got one", (a) => a.human_success_pct + "%")}
      ${row("human checkout p95", (a) => a.human_checkout_p95_ms == null ? null : a.human_checkout_p95_ms + " ms", (a) => a.human_checkout_p95_ms != null && a.human_checkout_p95_ms <= (state.health ? state.health.slo.p95_ms : 500))}
      ${row("server errors (5xx / timeouts)", (a) => a.server_error_pct + "%", (a) => a.server_error_pct <= 1)}
      ${row("max queue · wait p50 / p95", (a) => `${a.max_queue} · ${a.queue_wait_p50_s ?? "–"}s / ${a.queue_wait_p95_s ?? "–"}s`)}
      ${row("expired holds returned to sale", (a) => a.expired_hold_units_returned + " units")}
      ${row("sold out after", (a) => a.sold_out_after_s == null ? "not sold out" : a.sold_out_after_s + " s")}
      ${row("human outcomes", (a) => fmtObj(a.humans))}
      ${row("bot attempts → outcomes", (a) => `${a.bot_attempts}: ${fmtObj(a.bot_outcomes)}`)}
      ${row("inventory at end (available / reserved)", (a) => `${a.inventory_available_end} / ${a.inventory_reserved_end}`)}
      </table></div>
      <div class="tiny" style="margin-top:6px">Oversold = units confirmed to buyers minus units in stock. A human who “tried” added to cart and didn't abandon.</div>`;
  }

  // ---- repository
  async function repoTab() {
    const body = $("#lab-body");
    const reg = await api("/api/lab/constraints");
    const alpha = reg.length ? reg[0].alpha : null;
    body.innerHTML = `<div class="panel" style="margin-top:16px">
      <h3>Constraint repository: validity by scaling mode</h3>
      <div class="tiny" style="margin-bottom:10px">Merged from <code>data/constraints_summary.json</code> (static, Task 12), <code>hpa_transfer_summary.json</code> (HPA, 14C) and <code>vertical_transfer_summary.json</code> (vertical, 14D). Simulated studies; “disappeared” means not observed within the tested region.${alpha ? ` Fitted CPU scaling exponent α = ${alpha}.` : ""}</div>
      <div class="scroll-x"><table class="t"><tr><th>ID</th><th>rule</th><th>static</th><th>HPA</th><th>vertical</th><th>evidence</th></tr>
      ${reg.map((c) => `<tr><td><b>${c.id}</b><div class="tiny">${esc(c.kind)}</div></td><td><code>${esc(c.rule)}</code></td><td>${clsPill(c.classes.static)}</td><td>${clsPill(c.classes.hpa)}</td><td>${clsPill(c.classes.vertical)}</td>
        <td class="tiny">${esc([c.evidence.hpa && "HPA: " + c.evidence.hpa, c.evidence.vertical && "Vertical: " + c.evidence.vertical].filter(Boolean).join(" · ").slice(0, 320))}</td></tr>`).join("")}</table></div></div>`;
  }

  // ------------------------------------------------------------------ router
  const ROUTES = { "": ["overview", overviewPage], live: ["live", () => labPage(liveTab)], model: ["model", () => labPage(modelTab)], shield: ["shield", () => labPage(shieldTab)], repo: ["repo", () => labPage(repoTab)] };
  async function route() {
    const parts = (location.hash || "#/").slice(1).split("?")[0].split("/").filter(Boolean);
    const [page, render] = ROUTES[parts[0] || ""] || [null, null];
    state.page = page;
    setActive(page);
    clearInterval(state.lab.shieldPoll);
    state.timers.forEach(clearInterval); state.timers = [];
    window.scrollTo(0, 0);
    try {
      if (render) await render();
      else view.innerHTML = `<section class="card"><h2>Page not found</h2><a href="#/">Back to overview</a></section>`;
    } catch (err) {
      view.innerHTML = `<section class="card"><h2>Something went wrong</h2><div class="notice bad">${esc(err.message)}</div><p class="muted small">If the server just started, the testbed services may still be launching.</p></section>`;
    }
  }

  window.addEventListener("hashchange", route);
  initShell();
  route();
})();
