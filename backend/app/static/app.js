"use strict";
const API = "/api/v1";
const $ = (s) => document.querySelector(s);
const state = { token: null, key: null, company: null, ledgers: [], vtypes: [], user: null };

function store(k, v) { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch (_) {} }
function load(k) { try { return localStorage.getItem(k); } catch (_) { return null; } }
function esc(v) { return String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
function chip(v) { return `<span class="chip ${esc(v)}">${esc(String(v ?? "").replace(/_/g, " "))}</span>`; }
function money(v) { const n = Number(v || 0); return n.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
function when(v) { return v ? new Date(v).toLocaleString() : ""; }
function today() { return new Date().toISOString().slice(0, 10); }

// People sign in with a password (session token); integrations may use an API key.
function authHeaders() { return state.token ? { Authorization: `Bearer ${state.token}` } : { "X-API-Key": state.key || "" }; }

async function api(path, opts = {}) {
  const headers = { ...authHeaders(), ...(opts.body ? { "Content-Type": "application/json" } : {}) };
  const body = opts.form || (opts.body ? JSON.stringify(opts.body) : undefined);
  const res = await fetch(API + path, { ...opts, headers, body });
  if (res.status === 401) { signOut(); throw new Error("Please sign in"); }
  if (res.status === 409 && state.token) {   // away too long and the licence seat was taken
    const d = await res.clone().json().catch(() => ({}));
    if (/licence seat/.test(d.detail || "")) { signOut(); $("#login-error").textContent = d.detail; throw new Error(d.detail); }
  }
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) {
    const d = data && data.detail;
    throw new Error(Array.isArray(d) ? d.map((x) => `${x.loc.slice(1).join(".")}: ${x.msg}`).join("; ") : d || res.statusText);
  }
  return data;
}
const cq = () => `company_id=${state.company}`;

function table(el, cols, rows, onClick) {
  const head = `<thead><tr>${cols.map((c) => `<th class="${c.num ? "num" : ""}">${esc(c.label)}</th>`).join("")}</tr></thead>`;
  const body = rows.length ? rows.map((r, i) => `<tr data-i="${i}" class="${onClick ? "click" : ""} ${r._class || ""}">${cols.map((c) =>
    `<td class="${c.num ? "num" : ""} ${c.wrap ? "wrap" : ""}">${c.html ? c.html(r) : esc(c.get ? c.get(r) : r[c.key])}</td>`).join("")}</tr>`).join("")
    : `<tr><td colspan="${cols.length}" class="muted">Nothing to show</td></tr>`;
  el.innerHTML = head + `<tbody>${body}</tbody>`;
  if (onClick) el.querySelectorAll("tbody tr[data-i]").forEach((tr) => tr.addEventListener("click", (e) => {
    if (e.target.closest("button")) return; onClick(rows[+tr.dataset.i]);
  }));
}

// ------------------------------------------------------------------ session & tabs
function signOut() {
  if (state.token) fetch(API + "/auth/logout", { method: "POST", headers: authHeaders() }).catch(() => {});
  clearInterval(pingTimer);
  state.token = state.key = state.user = null; store("erp_token", null); store("erp_key", null);
  $("#login").hidden = false; document.querySelector("main").hidden = true; $("header nav").hidden = true;
  $("#whoami").textContent = ""; $("#change-pw").hidden = true; $("#open-tally").hidden = true; $("#lic-banner").hidden = true;
  document.querySelectorAll("dialog[open]").forEach((d) => d.close());
}
$("#logout").onclick = signOut;
$("#login-form").onsubmit = async (e) => {
  e.preventDefault(); $("#login-error").textContent = "";
  const btn = e.target.querySelector("button"); btn.disabled = true;
  try {
    const res = await fetch(API + "/auth/login", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: $("#login-user").value.trim(), password: $("#login-pass").value }) });
    const d = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(d.detail || res.statusText);
    state.token = d.token; state.key = null; store("erp_token", d.token); store("erp_key", null);
    $("#login-pass").value = "";
    await start();
  } catch (err) { $("#login-error").textContent = err.message; } finally { btn.disabled = false; }
};
$("#key-form").onsubmit = async (e) => {
  e.preventDefault(); $("#login-error").textContent = ""; state.token = null; state.key = $("#apikey").value.trim();
  try { await start(); store("erp_key", state.key); } catch (err) { $("#login-error").textContent = err.message; }
};

let pingTimer = null;
async function start() {
  state.user = await api("/me");
  clearInterval(pingTimer);
  if (state.token) pingTimer = setInterval(() => { if (state.token) api("/auth/ping", { method: "POST" }).catch(() => {}); }, 120000);
  $("#whoami").textContent = state.user.display_name || state.user.username;
  $("#change-pw").hidden = !state.user.has_password || !state.token;
  $("#login").hidden = true; document.querySelector("main").hidden = false; $("header nav").hidden = false;
  if (state.user.must_change_password && !(await askNewPassword(true))) return;
  $("#open-tally").hidden = !state.user.is_superadmin;
  await loadCompanies();
}

// ------------------------------------------------------------------ permissions of the signed-in user
// The server checks every request; this only hides what the user may not use in the selected company.
function myPerms() {
  if (state.user?.is_superadmin) return null;   // everything
  const a = (state.user?.companies || []).find((c) => c.company_id === state.company);
  return new Set(a ? a.permissions : []);
}
function can(perm) {
  const p = myPerms();
  return p === null || p.has(perm) || (perm.startsWith("report.") && p.has("reports.read"));
}
// Tabs that do not belong to a books company.
const SPECIAL_TABS = {
  "portal-admin": () => state.user?.is_superadmin && state.user.license_mode === "server",
  "portal-customer": () => state.user?.license_mode === "server" && !!state.user.customer_id,
  "licence-client": () => state.user?.is_superadmin && state.user.license_mode === "client",
  superadmin: () => !!state.user?.is_superadmin,
};
function applyPerms() {
  const reportOpts = [...$("#r-name").options];
  reportOpts.forEach((o) => { o.disabled = o.hidden = !can(`report.${o.dataset.report}`); });
  if ($("#r-name").selectedOptions[0]?.disabled) { const ok = reportOpts.find((o) => !o.disabled); if (ok) $("#r-name").value = ok.value; }
  document.querySelectorAll("#tabs button").forEach((b) => {
    const special = SPECIAL_TABS[b.dataset.perm];
    b.hidden = special ? !special() : b.dataset.perm === "report" ? !reportOpts.some((o) => !o.disabled) : !can(b.dataset.perm);
  });
  $("#r-csv").hidden = $("#errors-csv").hidden = !can("export");
  $("#l-new").hidden = $("#ld-edit").hidden = !can("masters.write");
}
function tabAllowed(tab) { const b = document.querySelector(`#tabs button[data-tab="${tab}"]`); return !!b && !b.hidden; }
function firstTab() { return [...document.querySelectorAll("#tabs button")].find((b) => !b.hidden)?.dataset.tab; }

// ------------------------------------------------------------------ change password
function askNewPassword(forced) {
  return new Promise((resolve) => {
    const d = $("#pw-dlg"), f = $("#pwf"), el = (n) => f.elements.namedItem(n);
    f.reset(); $("#pw-msg").textContent = "";
    $("#pw-note").textContent = forced ? "Your admin chose your first password. Choose your own to continue." : "";
    $("#pw-cancel").textContent = forced ? "Sign out" : "Cancel";
    d.oncancel = (e) => { if (forced) e.preventDefault(); };
    $("#pw-cancel").onclick = () => { d.close(); if (forced) signOut(); resolve(false); };
    f.onsubmit = async (e) => {
      e.preventDefault(); $("#pw-msg").textContent = "";
      if (el("new1").value !== el("new2").value) { $("#pw-msg").textContent = "The two new passwords differ"; return; }
      try {
        await api("/auth/password", { method: "POST", body: { current_password: el("current").value, new_password: el("new1").value } });
        d.close(); state.user.must_change_password = false; resolve(true);
        if (!forced) alert("Your password is changed. Other browsers where you were signed in are signed out.");
      } catch (err) { $("#pw-msg").textContent = err.message; }
    };
    d.showModal(); el("current").focus();
  });
}
$("#change-pw").onclick = () => askNewPassword(false);

async function loadCompanies(select) {
  resetCheck();
  const companies = await api("/companies");
  $("#company").innerHTML = companies.map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join("");
  const want = select || load("erp_company");
  state.company = want && companies.some((c) => c.id === want) ? want : companies[0]?.id;
  if (!state.company) {
    // No books yet: only the licence tabs (a company admin of the portal may have no books at all).
    document.querySelectorAll("#tabs button").forEach((b) => { const sp = SPECIAL_TABS[b.dataset.perm]; b.hidden = !(sp && sp()); });
    $("#company").hidden = true;
    $("#noaccess-msg").innerHTML = state.user.is_superadmin ? "No companies yet. Use <b>Open Tally company</b> to link one."
      : "You have no access to any company yet. Ask your company admin.";
    refreshBanner();
    const tab = firstTab();
    if (tab) show(load("erp_tab") || tab); else document.querySelectorAll(".tab").forEach((t) => (t.hidden = t.id !== "noaccess"));
    return;
  }
  $("#company").hidden = false;
  $("#company").value = state.company; store("erp_company", state.company);
  applyPerms(); refreshBanner();
  await loadMasters();
  show(select ? "dashboard" : load("erp_tab") || "dashboard");
}
$("#company").onchange = async () => {
  resetCheck(); state.company = $("#company").value; store("erp_company", state.company);
  applyPerms(); refreshBanner(); await loadMasters(); show(currentTab);
};

// ------------------------------------------------------------------ open / link a Tally company
$("#open-tally").onclick = () => { $("#td-msg").textContent = ""; $("#td-table").innerHTML = ""; $("#tally-dlg").showModal(); };
$("#td-close").onclick = () => $("#tally-dlg").close();
async function discoverTally() {
  const target = { host: $("#td-host").value.trim(), port: +$("#td-port").value };
  $("#td-msg").textContent = "Asking Tally for its open companies…"; $("#td-table").innerHTML = "";
  let r;
  try { r = await api("/import/tally/discover", { method: "POST", body: target }); } catch (err) { $("#td-msg").textContent = err.message; return; }
  $("#td-msg").textContent = r.companies.length ? `${r.companies.length} compan${r.companies.length === 1 ? "y" : "ies"} open in Tally.` : "No company is open in Tally. Open one in Tally and list again.";
  table($("#td-table"), [
    { label: "Tally company", key: "name", wrap: true },
    { label: "Books from", key: "books_from" },
    { label: "", html: (c) => c.erp_company_id ? `<button class="secondary" data-open="${esc(c.erp_company_id)}">Open</button>`
        : `<button data-link="${esc(c.name)}">Link &amp; import masters</button>` },
  ], r.companies);
  $("#td-table").querySelectorAll("[data-open]").forEach((b) => (b.onclick = async () => { $("#tally-dlg").close(); await loadCompanies(b.dataset.open); }));
  $("#td-table").querySelectorAll("[data-link]").forEach((b) => (b.onclick = async () => {
    const name = prompt("Name for this company in the ERP", b.dataset.link);
    if (!name) return;
    b.disabled = true; $("#td-msg").textContent = "Linking…";
    try {
      const res = await api("/import/tally/link", { method: "POST", body: { ...target, tally_company_name: b.dataset.link, erp_company_name: name } });
      $("#tally-dlg").close();
      await loadCompanies(res.company_id);
      alert(`Linked. Masters are importing in the background; progress shows on the Sync tab.
Mode: ${res.mode}`);
    } catch (err) { $("#td-msg").textContent = err.message; b.disabled = false; }
  }));
}
$("#td-form").onsubmit = (e) => { e.preventDefault(); discoverTally(); };

function resetCheck() { $("#run-sync").dataset.checked = ""; $("#sync-progress").hidden = true; $("#tally-check").hidden = true; $("#run-sync").disabled = true; $("#run-sync").title = "Check Tally first"; }

let currentTab = "dashboard";
const renderers = {};
function show(tab) {
  if (!tabAllowed(tab)) tab = firstTab();
  if (!tab) { document.querySelectorAll(".tab").forEach((s) => (s.hidden = s.id !== "noaccess")); return; }
  currentTab = tab; store("erp_tab", tab);
  document.querySelectorAll(".tab").forEach((s) => (s.hidden = s.id !== tab));
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  if (tab !== "dashboard") { clearTimeout(progressTimer); progressTimer = null; }
  renderers[tab] && renderers[tab]().catch((e) => alert(e.message));
}
document.querySelectorAll("#tabs button").forEach((b) => (b.onclick = () => show(b.dataset.tab)));

async function loadMasters() {
  itemsCache = null;
  if (!can("masters.read")) {   // e.g. a reports-only user: nothing to look up
    state.ledgers = []; state.vtypes = []; state.groups = {};
    $("#ledger-names").innerHTML = $("#ve-type").innerHTML = $("#group-names").innerHTML = "";
    $("#vf-type").innerHTML = `<option value="">All types</option>`;
    return;
  }
  [state.ledgers, state.vtypes] = await Promise.all([
    api(`/companies/${state.company}/masters/ledgers?limit=5000`), api(`/companies/${state.company}/masters/voucher-types`)]);
  $("#ledger-names").innerHTML = state.ledgers.map((l) => `<option value="${esc(l.name)}">`).join("");
  const opts = state.vtypes.filter((v) => v.is_active).map((v) => `<option>${esc(v.name)}</option>`).join("");
  $("#ve-type").innerHTML = opts; $("#vf-type").innerHTML = `<option value="">All types</option>` + opts;
  const groups = await api(`/companies/${state.company}/masters/groups`);
  state.groups = Object.fromEntries(groups.map((g) => [g.name, g]));
  $("#group-names").innerHTML = groups.map((g) => `<option value="${esc(g.name)}">`).join("");
}

// ------------------------------------------------------------------ sync dashboard
renderers.dashboard = async () => {
  if (!progressTimer) watchProgress();
  const d = await api(`/sync/dashboard?${cq()}`);
  const c = d.connector || {};
  const a = d.auto_sync || {};
  const autoText = !a.enabled ? "off: sync only runs when you click Sync now" : c.voucher_backlog
    ? `catching up: ${Number(c.voucher_backlog).toLocaleString("en-IN")} changed vouchers left, next slot right after this one`
    : `every ${a.poll_seconds} s while Tally is open${a.last_round_at ? ` · last ${when(a.last_round_at)}` : ""}`;
  const cards = [
    ["Automatic sync", `${a.enabled ? (c.voucher_backlog ? "Catching up" : "On") : "Off"}
      ${state.user?.is_superadmin ? `<button type="button" class="secondary small" id="auto-toggle">${a.enabled ? "Turn off" : "Turn on"}</button>` : ""}`, esc(autoText)],
    ["Tally connection", chip(c.health_status || "not configured"), esc(c.health_message || c.tally_company_name || "")],
    ["Pending", d.pending, d.oldest_pending_seconds != null ? `oldest ${Math.round(d.oldest_pending_seconds / 60)} min` : ""],
    ["Failed", d.failed, Object.entries(d.failed_by_error).map(([k, v]) => `${k}: ${v}`).join(", ")],
    ["Open conflicts", d.open_conflicts, ""], ["Inbound failures", d.inbox_failed, ""],
    ["Vouchers in Tally", Number(d.vouchers.in_tally).toLocaleString("en-IN"),
      d.vouchers.waiting_in_tally ? `${Number(d.vouchers.waiting_in_tally).toLocaleString("en-IN")} still to read` : "all read"],
    ["Synced vouchers", Number(d.vouchers.synced).toLocaleString("en-IN"),
      d.vouchers.in_tally ? `${Math.floor((d.vouchers.synced / d.vouchers.in_tally) * 100)}% of Tally` +
        (d.vouchers.not_stored ? ` · ${d.vouchers.not_stored} need attention` : "") : ""],
    ["Amended in Tally", Number(d.vouchers.amended_in_tally).toLocaleString("en-IN"), "vouchers whose amendment was synced"],
    ["Last import", when(c.last_inbound_at) || "never", c.freeze_inbound ? "inbound frozen" : ""],
    ["Last export", when(c.last_outbound_at) || "never", c.freeze_outbound ? "outbound frozen" : (c.outbound_enabled ? "" : "outbound disabled (shadow mode)")],
  ];
  $("#health").innerHTML = cards.map(([k, v, s]) => `<div class="card"><div class="k">${k}</div><div class="v">${v}</div><div class="small muted">${s}</div></div>`).join("");
  setBadge(d.open_conflicts);
  $("#auto-toggle") && ($("#auto-toggle").onclick = async () => {
    if (!a.enabled && !confirm("Automatic sync reads every company's changes from Tally on its own, and works through any backlog " +
      "one slot after another. While a large backlog is read, Tally is busy most of the time. Turn it on?")) return;
    await api("/sync/auto", { method: "PUT", body: { enabled: !a.enabled } });
    renderers.dashboard();
  });
  const amends = await api(`/sync/amendments?${cq()}&limit=50`);
  table($("#amends"), [{ label: "Synced", get: (x) => when(x.at) }, { label: "Voucher", get: (x) => `${x.voucher_type} ${x.voucher_number || ""}` },
    { label: "Date", key: "date" }, { label: "Version", get: (x) => `v${x.version}` },
    { label: "What changed", wrap: true, get: (x) => x.changes.map(fieldLabel).join(", ") || "no content change" }],
    amends, (x) => openVoucher(x.voucher_id));
  $("#errors-csv").href = "#";
  await loadJobs();
  const inbox = await api(`/sync/inbox?${cq()}&limit=50`);
  table($("#inbox"), [
    { label: "Received", get: (r) => when(r.received_at) }, { label: "Type", key: "voucher_type" }, { label: "No.", key: "voucher_number" },
    { label: "AlterID", key: "tally_alter_id", num: true }, { label: "Status", html: (r) => chip(r.status) },
    { label: "Error", wrap: true, get: (r) => [r.error_code, r.error].filter(Boolean).join(": ") },
    { label: "", html: (r) => r.status === "failed" ? `<button class="secondary" data-reprocess="${r.id}">Reprocess</button>` : "" },
  ], inbox);
  $("#inbox").querySelectorAll("[data-reprocess]").forEach((b) => (b.onclick = async () => {
    const r = await api(`/sync/inbox/${b.dataset.reprocess}/reprocess`, { method: "POST" }); alert(`${r.outcome}${r.error ? ": " + r.error : ""}`); renderers.dashboard();
  }));
};
function setBadge(n) { const b = $("#conflict-badge"); b.hidden = !n; b.textContent = n; }

async function loadJobs() {
  const params = new URLSearchParams({ company_id: state.company, limit: 100 });
  if ($("#job-status").value) params.set("status", $("#job-status").value);
  if ($("#job-number").value) params.set("voucher_number", $("#job-number").value);
  const jobs = await api(`/sync/jobs?${params}`);
  table($("#jobs"), [
    { label: "Created", get: (r) => when(r.created_at) }, { label: "Entity", key: "entity_type" }, { label: "Op", key: "operation" },
    { label: "Type", key: "voucher_type" }, { label: "No./name", key: "voucher_number" }, { label: "Status", html: (r) => chip(r.status) },
    { label: "Tries", key: "attempts", num: true }, { label: "Error", wrap: true, get: (r) => [r.error_code, r.last_error].filter(Boolean).join(": ") },
    { label: "", html: (r) => (["failed", "pending", "paused"].includes(r.status) ? `<button class="secondary" data-retry="${r.id}">Retry</button>` : "") },
  ], jobs, async (r) => {
    const j = await api(`/sync/jobs/${r.id}`);
    $("#recon-result").textContent = j.attempts.map((a) => `${when(a.at)}  ${a.success ? "OK" : a.error_code + ": " + a.error}\n--- request ---\n${a.request_xml || ""}\n--- response ---\n${a.response_xml || ""}`).join("\n\n") || "No attempts yet";
    $("#recon-result").scrollIntoView({ behavior: "smooth" });
  });
  $("#jobs").querySelectorAll("[data-retry]").forEach((b) => (b.onclick = async () => { await api(`/sync/jobs/${b.dataset.retry}/retry`, { method: "POST" }); loadJobs(); }));
}
$("#job-filter").onclick = loadJobs;
// Check Tally: reachable? linked company open (same name + GUID)? Only then allow Sync now.
function showCheck(r) {
  const el = $("#tally-check"); el.hidden = false; el.className = `check-panel ${r.matched ? "ok" : "bad"}`;
  const open = r.open_companies.length
    ? `<div class="small muted">Open in Tally:</div><ul>${r.open_companies.map((n) => `<li>${esc(n)}${n === r.tally_company_name ? " ✓" : ""}</li>`).join("")}</ul>` : "";
  el.innerHTML = r.matched
    ? `<div class="small muted">Tally is running. Linked company found:</div><div class="company">✓ ${esc(r.tally_company_name)}</div>
       <div class="small muted">Books from ${esc(r.books_from)} · checked ${when(r.checked_at)}</div>
       <div class="row"><button id="check-sync">Sync now</button></div>`
    : `<div class="small muted">Linked company: <b>${esc(r.tally_company_name)}</b></div><div>${chip(r.status)} ${esc(r.message)}</div>${open}`;
  $("#run-sync").dataset.checked = r.matched ? "yes" : "";
  $("#run-sync").disabled = !r.matched; $("#run-sync").title = r.matched ? "" : "Check Tally first";
  if (r.matched) $("#check-sync").onclick = () => $("#run-sync").click();
}
$("#check-tally").onclick = async (e) => {
  e.target.disabled = true; const el = $("#tally-check");
  el.hidden = false; el.className = "check-panel"; el.textContent = "Contacting Tally…";
  try { showCheck(await api(`/sync/connectors/${state.company}/check`, { method: "POST" })); }
  catch (err) { el.className = "check-panel bad"; el.textContent = err.message; }
  finally { e.target.disabled = false; renderers.dashboard(); }
};
// ------------------------------------------------------------------ sync progress
// One overall bar for the whole run, the four steps with their status, and a bar under any step
// that has a count (e.g. vouchers fetched). Polls while a run is going, then refreshes the dashboard.
const STEP_ICON = { done: "✓", running: "●", failed: "✗", skipped: "–", pending: "○" };
let progressTimer = null;
function renderProgress(p) {
  const el = $("#sync-progress");
  if (!p || p.state === "never") { el.hidden = true; return; }
  el.hidden = false;
  el.className = `panel progress-panel ${p.state}`;
  const steps = p.steps || [];
  const finished = steps.filter((st) => ["done", "skipped", "failed"].includes(st.state)).length;
  const current = steps.find((st) => st.state === "running");
  const part = current && current.total ? current.done / current.total : 0;
  const overall = p.state === "running" ? Math.min(99, Math.round(((finished + part) / (steps.length || 1)) * 100)) : 100;
  const started = new Date(p.started_at), ended = p.finished_at ? new Date(p.finished_at) : new Date();
  const secs = Math.max(0, Math.round((ended - started) / 1000));
  const took = secs >= 60 ? `${Math.floor(secs / 60)} min ${secs % 60} s` : `${secs} s`;
  const title = { running: "Syncing with Tally…", done: "Last sync finished", failed: "Last sync stopped" }[p.state] || p.state;
  el.innerHTML = `<div class="head"><b>${title}</b><span class="muted small">${esc(p.trigger || "")} · started ${started.toLocaleTimeString()} · ${took}</span></div>
    <div class="bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${overall}" aria-label="Overall sync progress"><span style="width:${overall}%"></span></div>
    <div class="small muted">${p.state === "running" ? `Step ${Math.min(finished + 1, steps.length)} of ${steps.length} · ${overall}%` : esc(p.summary || "")}</div>
    <ol class="steps">${steps.map((st) => {
      const pctDone = st.total ? Math.round((st.done / st.total) * 100) : null;
      return `<li class="${st.state}"><span class="icon">${STEP_ICON[st.state] || ""}</span><div><b>${esc(st.name)}</b>
        <div class="small">${esc(st.detail || "")}${st.total ? ` — ${Number(st.done).toLocaleString("en-IN")} / ${Number(st.total).toLocaleString("en-IN")}` : ""}</div>
        ${pctDone !== null && st.state !== "skipped" ? `<div class="bar small" role="progressbar" aria-valuenow="${pctDone}" aria-valuemin="0" aria-valuemax="100" aria-label="${esc(st.name)}"><span style="width:${pctDone}%"></span></div>` : ""}</div></li>`;
    }).join("")}</ol>`;
  $("#run-sync").disabled = p.state === "running" || $("#run-sync").dataset.checked !== "yes";
}
async function watchProgress() {
  clearTimeout(progressTimer);
  if (currentTab !== "dashboard" || !state.company) return;
  let p;
  try { p = await api(`/sync/progress?${cq()}`); } catch (_) { return; }
  const wasRunning = $("#sync-progress").dataset.state === "running";
  $("#sync-progress").dataset.state = p.state;
  renderProgress(p);
  if (p.state === "running") progressTimer = setTimeout(watchProgress, 1500);
  else {
    if (wasRunning) renderers.dashboard();   // show what the run changed
    progressTimer = setTimeout(watchProgress, 15000);   // notice runs started by the background worker
  }
}
$("#run-sync").onclick = async (e) => {
  e.target.disabled = true;
  try {
    await api(`/sync/run?${cq()}`, { method: "POST" });
    $("#sync-progress").dataset.state = "running";
    watchProgress();
  } catch (err) { alert(err.message); e.target.disabled = false; }
};
$("#inbox-retry").onclick = async (e) => {
  e.target.disabled = true; e.target.textContent = "Reprocessing…";
  try {
    const r = await api(`/sync/inbox/reprocess-failed?${cq()}`, { method: "POST" });
    const left = Object.entries(r.still_failing).map(([k, v]) => `${k}: ${v}`).join(", ");
    alert(`Tried ${r.tried} failed voucher(s): ${Object.entries(r.outcomes).map(([k, v]) => `${v} ${k}`).join(", ") || "nothing to do"}.`
      + (left ? `\nStill failing: ${left}` : ""));
  } catch (err) { alert(err.message); }
  finally { e.target.disabled = false; e.target.textContent = "Reprocess all failed"; renderers.dashboard(); }
};
$("#retry-failed").onclick = async () => { const r = await api(`/sync/retry-failed?${cq()}`, { method: "POST" }); alert(JSON.stringify(r)); renderers.dashboard(); };
$("#errors-csv").onclick = (e) => { e.preventDefault(); download(`/sync/errors.csv?${cq()}`, "sync-errors.csv"); };
$("#recon-form").onsubmit = async (e) => {
  e.preventDefault(); $("#recon-result").textContent = "Reconciling…";
  try {
    const r = await api("/sync/reconcile", { method: "POST", body: { company_id: state.company, from_date: $("#recon-from").value, to_date: $("#recon-to").value } });
    $("#recon-result").textContent = `${r.status.toUpperCase()} – ${r.differences} difference(s)\n\n` + JSON.stringify(r.result, null, 2);
  } catch (err) { $("#recon-result").textContent = err.message; }
};

async function download(path, name) {
  const res = await fetch(API + path, { headers: authHeaders() });
  if (!res.ok) return alert((await res.json()).detail);
  const a = document.createElement("a"); a.href = URL.createObjectURL(await res.blob()); a.download = name; a.click();
}

// ------------------------------------------------------------------ vouchers
renderers.vouchers = async () => {
  const p = new URLSearchParams({ limit: 200 });
  for (const [id, key] of [["#vf-from", "from_date"], ["#vf-to", "to_date"], ["#vf-type", "voucher_type"], ["#vf-sync", "sync_status"], ["#vf-q", "q"]])
    if ($(id).value) p.set(key, $(id).value);
  const rows = await api(`/companies/${state.company}/vouchers?${p}`);
  table($("#vlist"), [
    { label: "Date", key: "date" }, { label: "Type", key: "voucher_type" }, { label: "No.", key: "voucher_number" },
    { label: "Party", key: "party_ledger" }, { label: "Narration", key: "narration", wrap: true },
    { label: "Amount", num: true, get: (r) => money(r.amount) }, { label: "Status", html: (r) => chip(r.status) },
    { label: "Sync", html: (r) => chip(r.sync_status) }, { label: "Origin", key: "source_system" }, { label: "v", key: "version", num: true },
  ], rows, (r) => openVoucher(r.id));
};
$("#vfilter").onsubmit = (e) => { e.preventDefault(); renderers.vouchers(); };

// Amendment history: every version with who changed it, where (ERP or Tally) and what changed.
function changeText(v) {
  if (v == null) return "—";
  if (Array.isArray(v)) return v.map((x) => (x.ledger ? `${x.ledger} ${money(x.amount)}` : `${x.item} ${x.quantity} ${money(x.amount)}`)).join("; ");
  if (typeof v === "object") return v.ledger ? `${v.ledger} ${money(v.amount)}` : JSON.stringify(v);
  return String(v);
}
const FIELD_LABEL = { voucher_number: "Voucher no.", date: "Date", narration: "Narration", party_ledger: "Party",
  reference_no: "Reference", is_cancelled: "Cancelled", status: "Status", voucher_type: "Voucher type" };
const fieldLabel = (f) => FIELD_LABEL[f] || f.replace(/^entries\[(.*)\]$/, "Ledger: $1").replace(/^inventory\[(.*)@(.*)\]$/, "Item: $1");
async function historyHtml(id) {
  const versions = await api(`/vouchers/${id}/versions`);
  const amendedInTally = versions.filter((x, i) => i > 0 && x.source === "tally" && x.changes.length).length;
  return `<h3>History <span class="muted small">${versions.length} version(s)${amendedInTally ? ` · amended in Tally ${amendedInTally} time(s)` : ""}</span></h3>
    <table><thead><tr><th>Version</th><th>When</th><th>By</th><th>From</th><th>What changed</th></tr></thead><tbody>
    ${versions.slice().reverse().map((x, i, all) => `<tr><td>v${x.version}${i === 0 ? " (current)" : ""}</td><td>${when(x.at)}</td>
      <td>${esc(x.changed_by || "")}</td><td>${x.source === "tally" ? "Tally" : "ERP"}</td><td class="wrap">${x.version === versions[0].version ? `Created (${esc(x.status)})`
        : x.changes.length ? x.changes.map((c) => `<div><b>${esc(fieldLabel(c.field))}</b>: ${esc(changeText(c.before))} → ${esc(changeText(c.after))}</div>`).join("")
        : `<span class="muted">${esc(x.reason || "no content change")}</span>`}</td></tr>`).join("")}</tbody></table>`;
}

// Stock lines as Tally shows them: quantities without a sign. The stored sign (inward +, outward −) only
// matters when one voucher moves stock both ways, as stock and manufacturing journals do; then an
// In / Out column says which lines were produced and which consumed.
function inventoryTable(lines) {
  const dir = (i) => (Number(i.quantity) > 0 || (Number(i.quantity) === 0 && i.direction === "in") ? "In" : "Out");
  const mixed = new Set(lines.map(dir)).size > 1;
  const qtyText = (i) => `${Math.abs(Number(i.quantity)).toLocaleString("en-IN", { maximumFractionDigits: 4 })} ${i.unit || ""}`.trim();
  return `<h3>Inventory</h3><table><thead><tr>${mixed ? "<th>In / Out</th>" : ""}<th>Item</th><th class="num">Qty</th>
    <th class="num">Rate</th><th class="num">Amount</th><th>Godown</th><th>Batch</th></tr></thead><tbody>${lines.map((i) =>
    `<tr>${mixed ? `<td>${dir(i) === "In" ? "In (produced / received)" : "Out (consumed / issued)"}</td>` : ""}<td>${esc(i.item)}</td>
      <td class="num">${Number(i.quantity) === 0 ? "value only" : esc(qtyText(i))}</td><td class="num">${money(i.rate)}</td>
      <td class="num">${money(i.amount)}</td><td>${esc(i.godown || "")}</td><td>${esc(i.batch || "")}</td></tr>`).join("")}</tbody></table>`;
}

async function openVoucher(id) {
  if (currentTab !== "vouchers") show("vouchers");
  const v = await api(`/vouchers/${id}`);
  const d = v.doc;
  const acts = { draft: ["post", "submit", "cancel", "delete"], pending_approval: ["approve", "reject", "cancel"], approved: ["post", "cancel"],
    posted: ["cancel", "delete"], rejected: ["submit", "cancel", "delete"], cancelled: ["delete"] }[v.status] || [];
  const el = $("#vdetail"); el.hidden = false;
  el.innerHTML = `<div class="row between"><h3>${esc(d.voucher_type)} ${esc(d.voucher_number)} · ${esc(d.date)}</h3>
    <div>${chip(v.status)} ${chip(v.sync_status)} <span class="muted small">v${v.version} · by ${esc(v.updated_by)} · origin ${esc(v.source_system)}</span></div></div>
    <p>${esc(d.narration)}</p>
    <table><thead><tr><th>Ledger</th><th class="num">Debit</th><th class="num">Credit</th><th>Bills</th><th>Bank</th></tr></thead><tbody>
    ${d.entries.map((e) => `<tr><td>${esc(e.ledger)}</td><td class="num">${+e.amount > 0 ? money(e.amount) : ""}</td><td class="num">${+e.amount < 0 ? money(-e.amount) : ""}</td>
      <td>${e.bills.map((b) => esc(`${b.type} ${b.name || ""}`)).join(", ")}</td><td>${e.bank.map((b) => esc(`${b.transaction_type || ""} ${b.instrument_no || ""}`)).join(", ")}</td></tr>`).join("")}
    </tbody></table>
    ${d.inventory.length ? inventoryTable(d.inventory) : ""}
    <p class="small muted">${v.tally ? `Tally: MasterID ${esc(v.tally.master_id)} · AlterID ${esc(v.tally.alter_id)} · GUID ${esc(v.tally.guid)} · last sync ${when(v.tally.last_synced_at)}
      ${v.tally.last_error ? `<br><span class="error">${esc(v.tally.error_code)}: ${esc(v.tally.last_error)}</span>` : ""}` : "Not yet in Tally"}</p>
    <div class="row">${!["cancelled", "deleted"].includes(v.status) ? `<button data-edit>Edit</button>` : ""}
      ${acts.map((a) => `<button class="secondary" data-act="${a}">${a[0].toUpperCase() + a.slice(1)}</button>`).join("")}
      <button class="secondary" data-dup>Duplicate</button><button class="link" data-hist>History</button></div><div id="vhist" hidden></div>`;
  el.querySelectorAll("[data-act]").forEach((b) => (b.onclick = async () => {
    const reason = ["cancel", "delete", "reject"].includes(b.dataset.act) ? prompt(`Reason to ${b.dataset.act}?`) : null;
    if (reason === null && ["cancel", "delete", "reject"].includes(b.dataset.act)) return;
    try {
      if (b.dataset.act === "delete") await api(`/vouchers/${id}?reason=${encodeURIComponent(reason)}`, { method: "DELETE" });
      else await api(`/vouchers/${id}/${b.dataset.act}`, { method: "POST", body: { reason, expected_version: v.version } });
      await renderers.vouchers(); openVoucher(id);
    } catch (err) { alert(err.message); }
  }));
  el.querySelector("[data-edit]") && (el.querySelector("[data-edit]").onclick = () => editVoucher(v));
  el.querySelector("[data-dup]").onclick = async () => { const n = await api(`/vouchers/${id}/duplicate`, { method: "POST" }); openVoucher(n.id); };
  el.querySelector("[data-hist]").onclick = async () => {
    const box = $("#vhist"); box.hidden = false; box.innerHTML = await historyHtml(id);
  };
  el.scrollIntoView({ behavior: "smooth" });
}

// ------------------------------------------------------------------ voucher entry (keyboard first)
function addLine(l = {}) {
  const tr = document.createElement("tr");
  const amt = +l.amount || 0;
  const bill = (l.bills && l.bills[0]) || {};
  tr.innerHTML = `<td><input class="l-ledger" list="ledger-names" value="${esc(l.ledger || "")}" required></td>
    <td><input class="l-dr num" inputmode="decimal" value="${amt > 0 ? amt : ""}"></td>
    <td><input class="l-cr num" inputmode="decimal" value="${amt < 0 ? -amt : ""}"></td>
    <td><input class="l-bill" placeholder="New Ref / Agst <ref>" value="${esc(bill.name ? (bill.type === "Agst Ref" ? "Agst " : "") + bill.name : "")}"></td>
    <td><button type="button" class="link" tabindex="-1" title="Remove line">✕</button></td>`;
  tr.querySelector("button").onclick = () => { tr.remove(); totals(); };
  tr.querySelectorAll("input").forEach((i) => i.addEventListener("input", totals));
  tr.querySelector(".l-dr").addEventListener("focus", (e) => { // Tally-style: prefill the balancing amount
    const diff = totals(); if (!e.target.value && !tr.querySelector(".l-cr").value && diff < 0) e.target.value = (-diff).toFixed(2), totals();
  });
  tr.querySelector(".l-cr").addEventListener("focus", (e) => {
    const diff = totals(); if (!e.target.value && !tr.querySelector(".l-dr").value && diff > 0) e.target.value = diff.toFixed(2), totals();
  });
  $("#ve-lines").appendChild(tr);
  return tr;
}
function totals() {
  let dr = 0, cr = 0;
  $("#ve-lines").querySelectorAll("tr").forEach((tr) => { dr += +tr.querySelector(".l-dr").value || 0; cr += +tr.querySelector(".l-cr").value || 0; });
  $("#ve-dr").textContent = money(dr); $("#ve-cr").textContent = money(cr);
  const diff = Math.round((dr - cr) * 100) / 100;
  $("#ve-diff").innerHTML = diff ? `<span class="error">Difference ${money(Math.abs(diff))} ${diff > 0 ? "Dr" : "Cr"}</span>` : `<span class="okmsg">Balanced</span>`;
  return diff;
}
function resetEntry(type) {
  $("#vform").reset(); $("#ve-lines").innerHTML = ""; $("#ve-id").value = ""; $("#ve-version").value = "";
  $("#entry-title").textContent = "New voucher"; $("#ve-date").value = today(); $("#ve-msg").textContent = "";
  if (type && state.vtypes.some((v) => v.name === type)) $("#ve-type").value = type;
  addLine(); addLine(); totals();
}
renderers.entry = async () => { if (!$("#ve-lines").children.length) resetEntry(); $("#ve-type").focus(); };
$("#ve-add").onclick = () => addLine().querySelector("input").focus();

function editVoucher(v) {
  show("entry"); resetEntry(); $("#ve-lines").innerHTML = "";
  const d = v.doc;
  $("#entry-title").textContent = `Edit ${d.voucher_type} ${d.voucher_number}`;
  $("#ve-id").value = v.id; $("#ve-version").value = v.version;
  $("#ve-type").value = d.voucher_type; $("#ve-number").value = d.voucher_number; $("#ve-date").value = d.date;
  $("#ve-party").value = d.party_ledger || ""; $("#ve-ref").value = d.reference_no || ""; $("#ve-narration").value = d.narration || "";
  d.entries.forEach(addLine); totals();
  $("#ve-action").value = v.status === "posted" ? "post" : "draft";
  editing = d;
}
let editing = null;

$("#vform").onsubmit = async (e) => {
  e.preventDefault();
  if (totals() !== 0) { $("#ve-msg").innerHTML = `<span class="error">Debits and credits must match</span>`; return; }
  const prev = editing && $("#ve-id").value ? editing : null;
  const entries = [...$("#ve-lines").querySelectorAll("tr")].map((tr, i) => {
    const dr = +tr.querySelector(".l-dr").value || 0, cr = +tr.querySelector(".l-cr").value || 0;
    const amount = (dr - cr).toFixed(2), billText = tr.querySelector(".l-bill").value.trim();
    const old = prev && prev.entries[i] && prev.entries[i].ledger === tr.querySelector(".l-ledger").value ? prev.entries[i] : {};
    const bills = billText ? [{ type: billText.startsWith("Agst ") ? "Agst Ref" : "New Ref", name: billText.replace(/^Agst /, ""), amount }]
      : (old.bills && old.bills.length === 1 ? [{ ...old.bills[0], amount }] : []);
    return { ledger: tr.querySelector(".l-ledger").value, amount, bills, cost: old.cost || [], bank: old.bank || [] };
  }).filter((l) => +l.amount !== 0);
  const body = {
    voucher_type: $("#ve-type").value, voucher_number: $("#ve-number").value || null, date: $("#ve-date").value,
    party_ledger: $("#ve-party").value || null, reference_no: $("#ve-ref").value || null, narration: $("#ve-narration").value, entries,
    inventory: prev ? prev.inventory : [],
  };
  try {
    let v;
    if ($("#ve-id").value) {
      v = await api(`/vouchers/${$("#ve-id").value}`, { method: "PATCH", body: { ...body, expected_version: +$("#ve-version").value } });
      if ($("#ve-action").value === "post" && v.status !== "posted") v = await api(`/vouchers/${v.id}/post`, { method: "POST", body: {} });
    } else v = await api(`/companies/${state.company}/vouchers`, { method: "POST", body: { ...body, action: $("#ve-action").value } });
    $("#ve-msg").innerHTML = `<span class="okmsg">Saved ${esc(v.doc.voucher_type)} ${esc(v.doc.voucher_number)} (${esc(v.status)}, sync ${esc(v.sync_status)})</span>`;
    const type = $("#ve-type").value; editing = null; resetEntry(type);
  } catch (err) { $("#ve-msg").innerHTML = `<span class="error">${esc(err.message)}</span>`; }
};
const FKEYS = { F4: "Contra", F5: "Payment", F6: "Receipt", F7: "Journal", F8: "Sales", F9: "Purchase" };
document.addEventListener("keydown", (e) => {
  if (currentTab !== "entry") return;
  if (FKEYS[e.key]) { e.preventDefault(); $("#ve-type").value = FKEYS[e.key]; $("#ve-date").focus(); }
  if (e.key === "Enter" && e.ctrlKey) { e.preventDefault(); $("#vform").requestSubmit(); }
  if (e.key.toLowerCase() === "n" && e.altKey) { e.preventDefault(); addLine().querySelector("input").focus(); }
});

// ------------------------------------------------------------------ conflicts
renderers.conflicts = async () => {
  const rows = await api(`/sync/conflicts?${cq()}`);
  setBadge(rows.length);
  $("#conflict-list").innerHTML = rows.length ? "" : `<p class="muted">No open conflicts.</p>`;
  for (const c of rows) {
    const div = document.createElement("div"); div.className = "panel";
    const e = c.erp_snapshot, t = c.tally_snapshot;
    div.innerHTML = `<div class="row between"><h3>${esc(e.voucher_type)} ${esc(e.voucher_number)} · ${esc(e.date)}</h3>
      <span class="muted small">detected ${esc(c.detected_by)} · ${when(c.created_at)}</span></div>
      ${t.deleted_in_tally ? `<p class="error">This voucher was deleted in Tally.</p>` : ""}
      <table class="diff"><thead><tr><th>Field</th><th>ERP</th><th>Tally</th></tr></thead><tbody>${c.diff.map((d) =>
        `<tr><td>${esc(d.field)}</td><td class="erp wrap">${esc(typeof d.erp === "object" ? JSON.stringify(d.erp) : d.erp)}</td><td class="tally wrap">${esc(typeof d.tally === "object" ? JSON.stringify(d.tally) : d.tally)}</td></tr>`).join("")}</tbody></table>
      <div class="row" style="margin-top:10px"><button data-c="erp">Keep ERP version</button><button data-c="tally" class="secondary">Accept Tally version</button>
      <button data-open class="link">Open voucher</button></div>`;
    div.querySelectorAll("[data-c]").forEach((b) => (b.onclick = async () => {
      const reason = prompt("Reason for this decision (kept in the audit trail):", ""); if (reason === null) return;
      try { await api(`/sync/conflicts/${c.id}/resolve`, { method: "POST", body: { choice: b.dataset.c, reason } }); renderers.conflicts(); }
      catch (err) { alert(err.message); }
    }));
    div.querySelector("[data-open]").onclick = () => openVoucher(c.erp_id);
    $("#conflict-list").appendChild(div);
  }
};

// ------------------------------------------------------------------ reports with drill-down
function reportUrl(fmt) {
  const name = $("#r-name").value;
  const p = new URLSearchParams({ company_id: state.company });
  const f = $("#r-from").value, t = $("#r-to").value;
  let path = name;
  if (name === "outstanding-payable") { path = "outstanding"; p.set("kind", "payable"); }
  if (name.startsWith("register-")) { path = "register"; p.set("base_type", name.split("-")[1]); }
  if (["balance-sheet", "outstanding"].includes(path)) { if (t) p.set("as_of", t); }
  else { if (f) p.set("from_date", f); if (t) p.set("to_date", t); }
  if (name in ANALYSIS || name === "fifo-costing") {
    for (const [k, v] of Object.entries(analysisParams())) p.set(k, v);
    if (name in ANALYSIS) p.set("by", $("#ra-by").value);
  }
  if (fmt) p.set("format", fmt);
  return `/reports/${path}?${p}`;
}

// ------------------------------------------------------------------ sales / purchase analysis, FIFO costing
const ANALYSIS = { "sales-analysis": "Sales", "purchase-analysis": "Purchases", "sales-rm-cost": "Sales" };
let raFilters = null;            // {company, parties, items, categories, stock_groups}
async function loadAnalysisFilters() {
  if (raFilters && raFilters.company === state.company) return raFilters;
  const f = await api(`/reports/filters?company_id=${state.company}`);
  raFilters = { company: state.company, ...f };
  $("#ra-parties").innerHTML = f.parties.map((x) => `<option value="${esc(x.name)}">`).join("");
  $("#ra-items").innerHTML = f.items.map((x) => `<option value="${esc(x.name)}">`).join("");
  $("#ra-cat").innerHTML = `<option value="">all</option>` + f.categories.map((c) => `<option>${esc(c)}</option>`).join("");
  $("#ra-group").innerHTML = `<option value="">all</option>` + f.stock_groups.map((g) => `<option value="${esc(g.id)}">${esc(g.name)}</option>`).join("");
  return raFilters;
}
function showAnalysisControls() {
  const name = $("#r-name").value;
  const trade = name in ANALYSIS, fifo = name === "fifo-costing";
  $("#ra-filters").hidden = !(trade || fifo);
  ["#ra-by-label", "#ra-party-label", "#ra-returns-label"].forEach((id) => ($(id).hidden = !trade));
  if (name === "sales-rm-cost" && $("#ra-by").dataset.touched !== "1") $("#ra-by").value = "item";
  if (trade || fifo) loadAnalysisFilters().catch(() => {});
}
$("#r-name").addEventListener("change", showAnalysisControls);
$("#ra-by").addEventListener("change", (e) => (e.target.dataset.touched = "1"));
function analysisParams() {
  const f = raFilters || { parties: [], items: [] };
  const id = (list, name) => (list.find((x) => x.name === name) || {}).id;
  const p = {};
  const party = $("#ra-party").value.trim(), item = $("#ra-item").value.trim();
  if (party) p.party = id(f.parties, party) || "00000000-0000-0000-0000-000000000000";
  if (item) p.item = id(f.items, item) || "00000000-0000-0000-0000-000000000000";
  if ($("#ra-cat").value) p.category = $("#ra-cat").value;
  if ($("#ra-group").value) p.stock_group = $("#ra-group").value;
  if (!$("#ra-returns").checked) p.include_returns = "false";
  return p;
}
const qtyText = (q, unit) => (q == null ? "" : `${Number(q).toLocaleString("en-IN", { maximumFractionDigits: 3 })}${unit ? " " + esc(unit) : ""}`);
const BY_LABEL = { party: "Party", date: "Date", month: "Month", item: "Item", category: "Item category",
  stock_group: "Stock group", voucher: "Voucher" };

async function analysisView(el, report, fixed = {}) {
  await loadAnalysisFilters();
  const by = fixed.by || $("#ra-by").value;
  const p = rParams({ ...analysisParams(), by });
  for (const [k, v] of Object.entries(fixed)) p.set(k, v);
  const r = await api(`/reports/${report}?${p}`);
  const rm = report === "sales-rm-cost";
  const counted = r.voucher_types.filter((t) => t.counted).map((t) => t.name).join(", ");
  const left = r.voucher_types.filter((t) => !t.counted).map((t) => t.name).join(", ");
  el.innerHTML = `<p class="muted">${esc(r.from)} to ${esc(r.to)} · ${esc(counted || "no voucher types")}`
    + `${left ? ` (not counted: ${esc(left)})` : ""}${r.include_returns ? " · returns deducted" : ""}`
    + `${by === "voucher" ? " · click a voucher to open it" : " · click a row for its vouchers"}</p>`
    + `${rm ? `<p class="small muted">Raw-material cost: what the components of each sold item cost in its production vouchers
       (weighted average up to the sale date); labour and other costs are not included.</p>` : ""}<table id="t"></table>`;
  const rmCols = rm ? [
    { label: "Raw-material cost", num: true, get: (x) => (x.rm_cost == null ? "—" : money(x.rm_cost)) },
    { label: "Margin", num: true, get: (x) => (x.margin == null ? "—" : money(x.margin)) },
    ...(by === "voucher" ? [{ label: "RM cost / unit", num: true, get: (x) => (x.rm_cost_per_unit == null ? "no production" : money(x.rm_cost_per_unit)) }]
      : [{ label: "Margin %", num: true, get: (x) => (x.margin_pct == null ? "" : `${x.margin_pct}%`) }]),
  ] : [];
  const tot = { ...r.total, _class: "total" };
  if (by === "voucher") {
    table($("#t"), [
      { label: "Date", key: "date" }, { label: "Voucher", get: (x) => (x.voucher_type ? `${x.voucher_type} ${x.voucher_number}` : "Total") },
      { label: "Party", key: "party", wrap: true }, { label: "Item", key: "item", wrap: true },
      { label: "Quantity", num: true, get: (x) => qtyText(x.quantity, x.unit) }, { label: "Rate", num: true, get: (x) => (x.rate ? money(x.rate) : "") },
      { label: "Value", num: true, get: (x) => money(x.value) }, ...rmCols,
    ], [...r.lines, tot], (x) => x.voucher_id && drillTo(`${x.voucher_type} ${x.voucher_number}`, (el2) => voucherView(el2, x.voucher_id)));
    return;
  }
  const qtyCol = ["item", "category", "stock_group"].includes(by)
    ? [{ label: "Quantity", num: true, get: (x) => qtyText(x.quantity, x.unit) }] : [];
  table($("#t"), [
    { label: BY_LABEL[by], get: (x) => x.name ?? "Total", wrap: true }, { label: "Vouchers", num: true, key: "vouchers" },
    ...qtyCol, { label: "Value", num: true, get: (x) => money(x.value) }, ...rmCols,
  ], [...r.rows, tot], (x) => {
    if (x._class === "total") return;
    const next = { by: "voucher" };
    if (by === "party") next.party = x.key;
    else if (by === "item") next.item = x.key;
    else if (by === "category") next.category = x.key;
    else if (by === "stock_group") next.stock_group = x.key;
    else if (by === "date") { next.from_date = x.key; next.to_date = x.key; }
    else if (by === "month") {
      const [y, mth] = x.key.split("-").map(Number);
      next.from_date = x.key + "-01"; next.to_date = new Date(Date.UTC(y, mth, 0)).toISOString().slice(0, 10);
    }
    drillTo(x.name, (el2) => analysisView(el2, report, { ...fixed, ...next }));
  });
}

async function fifoView(el) {
  await loadAnalysisFilters();
  const r = await api(`/reports/fifo-costing?${rParams(analysisParams())}`);
  el.innerHTML = `<p class="muted">${esc(r.from)} to ${esc(r.to)} · valued first-in-first-out: every issue takes the cost of the
    oldest stock still on hand · gross profit counts sales only · click an item for its movements</p>
    <p><input id="fifo-q" placeholder="Search item"></p><table id="t"></table>`;
  const draw = () => {
    const q = $("#fifo-q").value.trim().toLowerCase();
    const rows = r.items.filter((x) => !q || x.item.toLowerCase().includes(q));
    table($("#t"), [
      { label: "Item", key: "item", wrap: true }, { label: "Stock group", key: "stock_group" },
      { label: "Opening", num: true, get: (x) => (x.item ? qtyText(x.opening_qty, x.unit) : "") },
      { label: "Opening value", num: true, get: (x) => money(x.opening_value) },
      { label: "In", num: true, get: (x) => (x.item ? qtyText(x.in_qty, x.unit) : "") },
      { label: "In value", num: true, get: (x) => money(x.in_value) },
      { label: "Out", num: true, get: (x) => (x.item ? qtyText(x.out_qty, x.unit) : "") },
      { label: "Out at FIFO cost", num: true, get: (x) => money(x.out_cost) },
      { label: "Sales value", num: true, get: (x) => money(x.sold_value) },
      { label: "Gross profit", num: true, get: (x) => money(x.gross_profit) },
      { label: "Closing", num: true, get: (x) => (x.item ? qtyText(x.closing_qty, x.unit) : "") },
      { label: "Closing value", num: true, get: (x) => money(x.closing_value) },
      { label: "Rate", num: true, get: (x) => (x.closing_rate ? money(x.closing_rate) : "") },
      { label: "", html: (x) => (x.short_qty ? `<span class="chip failed" title="Issued more than was in stock">short ${esc(x.short_qty)}</span>` : "") },
    ], [...rows, { ...r.total, _class: "total" }], (x) => x.item_id && drillTo(x.item, (el2) => stockItemView(el2, x.item_id)));
  };
  $("#fifo-q").oninput = draw;
  draw();
}

$("#rform").onsubmit = (e) => { e.preventDefault(); renderers.reports(); };
$("#r-csv").onclick = (e) => { e.preventDefault(); download(reportUrl("csv"), `${$("#r-name").value}.csv`); };

// Drill-down like Tally: report → group → sub-group → ledger / item → voucher. A breadcrumb (or Esc)
// goes back up. Group rows expand in place; amounts are summed in paise so totals never drift.
const nav = [];
const paise = (v) => Math.round(Number(v || 0) * 100);
const rupees = (p) => money(p / 100);
function rParams(extra = {}, { asOf = false } = {}) {
  const p = new URLSearchParams({ company_id: state.company, ...extra });
  const f = $("#r-from").value, t = $("#r-to").value;
  if (asOf) { if (t) p.set("as_of", t); } else { if (f) p.set("from_date", f); if (t) p.set("to_date", t); }
  return p;
}
async function drillTo(title, render) { nav.push({ title, render }); await drawNav(); }
async function drawNav() {
  const out = $("#r-out"), top = nav[nav.length - 1];
  out.innerHTML = `<nav class="crumbs">${nav.map((n, i) => i < nav.length - 1
    ? `<a href="#" data-crumb="${i}">${esc(n.title)}</a>` : `<b>${esc(n.title)}</b>`).join(" › ")}
    ${nav.length > 1 ? `<span class="muted small"> · Esc to go back</span>` : ""}</nav><div id="r-body"><p class="muted">Loading…</p></div>`;
  out.querySelectorAll("[data-crumb]").forEach((a) => (a.onclick = (e) => { e.preventDefault(); nav.length = +a.dataset.crumb + 1; drawNav(); }));
  try { await top.render($("#r-body")); } catch (err) { $("#r-body").innerHTML = `<p class="error">${esc(err.message)}</p>`; }
}
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && currentTab === "reports" && nav.length > 1 && !document.querySelector("dialog[open]")) { nav.pop(); drawNav(); }
});

// Expandable table. node = {label, amount?, children?, open?, cls?, ...}; group rows toggle, leaf rows open.
function treeTable(el, columns, roots, { expandDepth = 0, title = "Particulars" } = {}) {
  const all = [];
  const walk = (n, depth) => { n._i = all.length; n._d = depth; n._open = depth < expandDepth; all.push(n); (n.children || []).forEach((c) => walk(c, depth + 1)); };
  roots.forEach((r) => walk(r, 0));
  const draw = () => {
    const rows = [];
    const visit = (n) => { rows.push(n); if (n._open) (n.children || []).forEach(visit); };
    roots.forEach(visit);
    const anyGroups = all.some((n) => n.children && n.children.length);
    el.innerHTML = (anyGroups ? `<div class="tree-tools"><button type="button" class="link" data-all="1">Expand all</button>
      <button type="button" class="link" data-all="0">Collapse all</button></div>` : "") +
      `<table class="tree"><thead><tr><th>${esc(title)}</th>${columns.map((c) => `<th class="${c.num ? "num" : ""}">${esc(c.label)}</th>`).join("")}</tr></thead><tbody>
      ${rows.map((n) => {
        const kids = n.children && n.children.length;
        return `<tr data-i="${n._i}" class="${kids || n.open ? "click" : ""} ${kids ? "group" : "leaf"} ${n.cls || ""}">
          <td style="padding-left:${8 + n._d * 18}px"><span class="tw">${kids ? (n._open ? "▾" : "▸") : ""}</span>${esc(n.label)}</td>
          ${columns.map((c) => `<td class="${c.num ? "num" : ""}">${c.get(n) ?? ""}</td>`).join("")}</tr>`;
      }).join("") || `<tr><td colspan="${columns.length + 1}" class="muted">Nothing to show</td></tr>`}</tbody></table>`;
    el.querySelectorAll("[data-all]").forEach((b) => (b.onclick = () => { all.forEach((n) => (n._open = b.dataset.all === "1")); draw(); }));
    el.querySelectorAll("tr[data-i]").forEach((tr) => (tr.onclick = () => {
      const n = all[+tr.dataset.i];
      if (n.children && n.children.length) { n._open = !n._open; draw(); } else if (n.open) n.open();
    }));
  };
  draw();
}
const byName = (a, b) => (!!b.children - !!a.children) || a.label.localeCompare(b.label);
function sortTree(nodes) { nodes.sort(byName); nodes.forEach((n) => n.children && sortTree(n.children)); return nodes; }

// Ledgers grouped under their group chain (path = primary group … own group); amount(row) in paise.
function ledgerTree(rows, amount, skip = 0) {
  const roots = [];
  for (const r of rows) {
    const v = amount(r);
    if (!v) continue;
    let list = roots;
    const chain = [];
    for (const g of r.path.slice(skip)) {
      let n = list.find((x) => x.children && x.label === g);
      if (!n) { n = { label: g, amount: 0, children: [] }; list.push(n); }
      chain.push(n);
      list = n.children;
    }
    list.push({ label: r.ledger, amount: v, open: () => openLedgerStatement(r.ledger_id, r.ledger) });
    chain.forEach((n) => (n.amount += v));
  }
  return sortTree(roots);
}
const drCr = [{ label: "Debit", num: true, get: (n) => (n.amount > 0 ? rupees(n.amount) : "") },
              { label: "Credit", num: true, get: (n) => (n.amount < 0 ? rupees(-n.amount) : "") }];
const amountCol = [{ label: "Amount", num: true, get: (n) => (n.amount == null ? "" : rupees(n.amount)) }];

// ---------------------------------------------------------------- the reports
renderers.reports = async () => {
  const name = $("#r-name").value;
  showAnalysisControls();
  nav.length = 0;
  const label = $("#r-name").selectedOptions[0].textContent;
  const roots = {
    "trial-balance": trialBalanceView, "profit-loss": profitLossView, "balance-sheet": balanceSheetView,
    "day-book": dayBookView, "stock-summary": stockSummaryView, outstanding: (el) => outstandingView(el, "receivable"),
    "outstanding-payable": (el) => outstandingView(el, "payable"),
    "sales-analysis": (el) => analysisView(el, "sales-analysis"),
    "purchase-analysis": (el) => analysisView(el, "purchase-analysis"),
    "sales-rm-cost": (el) => analysisView(el, "sales-rm-cost"),
    "fifo-costing": fifoView,
  };
  await drillTo(label, roots[name] || ((el) => registerView(el, name.split("-")[1])));
};

async function trialBalanceView(el) {
  const [rows, tb] = await Promise.all([api(`/reports/ledger-balances?${rParams()}`), api(`/reports/trial-balance?${rParams()}`)]);
  const roots = ledgerTree(rows, (r) => paise(r.closing));
  const total = roots.reduce((t, n) => ({ dr: t.dr + Math.max(n.amount, 0), cr: t.cr + Math.max(-n.amount, 0) }), { dr: 0, cr: 0 });
  roots.push({ label: "Grand Total", amount: null, cls: "total", dr: total.dr, cr: total.cr });
  el.innerHTML = `<p class="muted">${esc(tb.from)} to ${esc(tb.to)} · click a group to expand, a ledger for its vouchers</p><div id="t"></div>
    ${+tb.opening_balance_difference ? `<p class="error">Difference in opening balances: ${money(tb.opening_balance_difference)}</p>` : ""}`;
  treeTable($("#t"), [
    { label: "Debit", num: true, get: (n) => (n.dr != null ? rupees(n.dr) : n.amount > 0 ? rupees(n.amount) : "") },
    { label: "Credit", num: true, get: (n) => (n.cr != null ? rupees(n.cr) : n.amount < 0 ? rupees(-n.amount) : "") }], roots);
}

// One side of P&L / Balance Sheet: backend sections (primary group + amount) expanded into their ledgers.
function sideNodes(sections, rows, sign, revenue) {
  return sections.map((s) => {
    const kids = ledgerTree(rows.filter((r) => r.is_revenue === revenue && r.path[0] === s.group), (r) => sign * paise(r.closing), 1);
    return { label: s.group, amount: paise(s.amount), children: kids, open: SPECIAL[s.group] };
  });
}
const SPECIAL = {
  "Profit & Loss A/c": () => drillTo("Profit & Loss", profitLossView),
  "Closing Stock": () => drillTo("Stock Summary", stockSummaryView),
};

async function profitLossView(el) {
  const [r, rows] = await Promise.all([api(`/reports/profit-loss?${rParams()}`), api(`/reports/ledger-balances?${rParams()}`)]);
  const stockLeaf = (label, v) => (+v ? [{ label, amount: paise(v), open: () => drillTo("Stock Summary", stockSummaryView) }] : []);
  const left = [...stockLeaf("Opening Stock", r.opening_stock), ...sideNodes(r.direct_expenses, rows, 1, true),
    { label: +r.gross_profit >= 0 ? "Gross Profit" : "Gross Loss", amount: paise(r.gross_profit), cls: "total" },
    ...sideNodes(r.indirect_expenses, rows, 1, true),
    { label: +r.net_profit >= 0 ? "Net Profit" : "Net Loss", amount: paise(r.net_profit), cls: "total" }];
  const right = [...sideNodes(r.direct_incomes, rows, -1, true), ...stockLeaf("Closing Stock", r.closing_stock),
    ...sideNodes(r.indirect_incomes, rows, -1, true)];
  el.innerHTML = `<p class="muted">${esc(r.from)} to ${esc(r.to)}${r.stock_source === "ledgers" ? " · stock is carried in Stock-in-Hand ledgers" : ""}</p>
    <div class="side"><div id="pl-l"></div><div id="pl-r"></div></div>`;
  treeTable($("#pl-l"), amountCol, left, { title: "Expenses" });
  treeTable($("#pl-r"), amountCol, right, { title: "Incomes" });
}

async function balanceSheetView(el) {
  const asOf = rParams({}, { asOf: true });
  const to = $("#r-to").value;
  const [r, rows] = await Promise.all([api(`/reports/balance-sheet?${asOf}`),
    api(`/reports/ledger-balances?${new URLSearchParams({ company_id: state.company, ...(to ? { to_date: to } : {}) })}`)]);
  const side = (sections, sign) => sideNodes(sections, rows, sign, false);
  el.innerHTML = `<p class="muted">As of ${esc(r.as_of)}</p><div class="side"><div id="bs-l"></div><div id="bs-r"></div></div>
    ${+r.difference ? `<p class="error">Difference ${money(r.difference)}</p>` : ""}`;
  treeTable($("#bs-l"), amountCol, [...side(r.liabilities, -1), { label: "Total", amount: paise(r.total_liabilities), cls: "total" }], { title: "Liabilities" });
  treeTable($("#bs-r"), amountCol, [...side(r.assets, 1), { label: "Total", amount: paise(r.total_assets), cls: "total" }], { title: "Assets" });
}

async function stockSummaryView(el) {
  const r = await api(`/reports/stock-summary?${rParams()}`);
  const roots = [];
  for (const it of r.items) {
    let list = roots;
    const chain = [];
    for (const g of it.path || []) {
      let n = list.find((x) => x.children && x.label === g);
      if (!n) { n = { label: g, value: 0, children: [] }; list.push(n); }
      chain.push(n);
      list = n.children;
    }
    list.push({ label: it.item, it, value: paise(it.closing_value), open: () => drillTo(it.item, (e) => stockItemView(e, it.item_id)) });
    chain.forEach((n) => (n.value += paise(it.closing_value)));
  }
  sortTree(roots);
  const qty = (k) => (n) => (n.it ? `${Number(n.it[k]).toLocaleString("en-IN", { maximumFractionDigits: 4 })} ${esc(n.it.unit || "")}` : "");
  el.innerHTML = `<p class="muted">To ${esc(r.to)} · groups expand; click an item for its vouchers</p><div id="t"></div><p><b>Closing value ${money(r.total_value)}</b></p>`;
  treeTable($("#t"), [{ label: "Opening", num: true, get: qty("opening_qty") }, { label: "Inward", num: true, get: qty("inward_qty") },
    { label: "Outward", num: true, get: qty("outward_qty") }, { label: "Closing", num: true, get: qty("closing_qty") },
    { label: "Rate", num: true, get: (n) => (n.it ? money(n.it.rate) : "") }, { label: "Value", num: true, get: (n) => rupees(n.value) }], roots);
}

async function stockItemView(el, itemId) {
  const r = await api(`/reports/stock-item?${rParams({ item_id: itemId })}`);
  const q = (v) => (v === "" || v == null ? "" : `${Number(v).toLocaleString("en-IN", { maximumFractionDigits: 4 })} ${esc(r.unit || "")}`);
  el.innerHTML = `<p class="muted">${esc(r.from)} to ${esc(r.to)} · opening ${q(r.opening_qty)} · closing ${q(r.closing_qty)} (value ${money(r.closing_value)})</p><table id="t"></table>`;
  table($("#t"), [{ label: "Date", key: "date" }, { label: "Particulars", key: "particulars", wrap: true }, { label: "Type", key: "voucher_type" },
    { label: "No.", key: "voucher_number" }, { label: "Godown", key: "godown" },
    { label: "Inward", num: true, get: (x) => q(x.inward_qty) }, { label: "Outward", num: true, get: (x) => q(x.outward_qty) },
    { label: "Value", num: true, get: (x) => (x.inward_value ? money(x.inward_value) : x.outward_value ? money(x.outward_value) : "") },
    { label: "Balance", num: true, get: (x) => q(x.balance_qty) }], r.lines, (x) => drillVoucher(x.voucher_id, `${x.voucher_type} ${x.voucher_number}`));
}

async function outstandingView(el, kind) {
  const r = await api(`/reports/outstanding?${rParams({ kind }, { asOf: true })}`);
  const parties = new Map();
  for (const b of r.bills) {
    if (!parties.has(b.party)) parties.set(b.party, { label: b.party, amount: 0, children: [
      { label: "Ledger statement ›", cls: "muted", open: () => openLedgerStatement(b.ledger_id, b.party) }] });
    const p = parties.get(b.party);
    p.amount += paise(b.pending);
    p.children.push({ label: b.bill || "(on account)", amount: paise(b.pending), b, open: () => drillVoucher(b.voucher_id, `Bill ${b.bill}`) });
  }
  el.innerHTML = `<div class="cards">${Object.entries(r.ageing).map(([k, v]) => `<div class="card"><div class="k">${k} days</div><div class="v">${money(v)}</div></div>`).join("")}
    <div class="card"><div class="k">Total</div><div class="v">${money(r.total)}</div></div></div><div id="t"></div>`;
  treeTable($("#t"), [{ label: "Date", get: (n) => (n.b ? esc(n.b.date) : "") }, { label: "Due", get: (n) => (n.b ? esc(n.b.due_date) : "") },
    { label: "Pending", num: true, get: (n) => (n.amount == null ? "" : rupees(n.amount)) },
    { label: "Overdue days", num: true, get: (n) => (n.b ? n.b.overdue_days : "") }], [...parties.values()].sort(byName), { title: "Party / bill" });
}

// Vouchers grouped by month (registers) or by day (day book), each opening the voucher
function vouchersByPeriod(vouchers, keyOf, labelOf) {
  const groups = new Map();
  for (const v of vouchers) {
    const k = keyOf(v);
    if (!groups.has(k)) groups.set(k, { label: labelOf(v), amount: 0, children: [] });
    const g = groups.get(k);
    g.amount += paise(v.amount);
    g.children.push({ label: `${v.voucher_type || ""} ${v.voucher_number || ""}`.trim() || "(no number)", amount: paise(v.amount), v,
      open: () => drillVoucher(v.id, `${v.voucher_type || ""} ${v.voucher_number || ""}`) });
  }
  return [...groups.values()];
}
async function registerView(el, base) {
  const r = await api(`/reports/register?${rParams({ base_type: base })}`);
  const month = (v) => new Date(v.date).toLocaleString("en-IN", { month: "long", year: "numeric" });
  const skipped = (r.not_included || []).map((x) => `${x.vouchers} ${x.voucher_type}`).join(", ");
  el.innerHTML = `<p class="muted">${esc(r.from)} to ${esc(r.to)} · months expand into vouchers · amount is the invoice value`
    + `${skipped ? ` · not included, as in Tally: ${esc(skipped)}` : ""}</p><div id="t"></div>`
    + `<p><b>Total ${money(r.total)}</b> · tax ${money(r.tax_total)}</p>`;
  treeTable($("#t"), [{ label: "Date", get: (n) => (n.v ? esc(n.v.date) : "") }, { label: "Party", get: (n) => (n.v ? esc(n.v.party || "") : "") },
    { label: "GSTIN", get: (n) => (n.v ? esc(n.v.gstin || "") : "") }, { label: "Tax", num: true, get: (n) => (n.v ? money(n.v.tax) : "") },
    ...amountCol], vouchersByPeriod(r.vouchers, (v) => v.date.slice(0, 7), month), { title: "Month / voucher" });
}
async function dayBookView(el) {
  const r = await api(`/reports/day-book?${rParams()}`);
  el.innerHTML = `<p class="muted">${esc(r.from)} to ${esc(r.to)}</p><div id="t"></div><p><b>Posted total ${money(r.posted_total)}</b></p>`;
  treeTable($("#t"), [{ label: "Particulars", get: (n) => (n.v ? esc(n.v.party || "") : "") },
    { label: "Narration", get: (n) => (n.v ? esc(n.v.narration || "") : "") }, { label: "Status", get: (n) => (n.v ? chip(n.v.status) : "") },
    ...amountCol], vouchersByPeriod(r.vouchers, (v) => v.date, (v) => v.date), { title: "Date / voucher", expandDepth: 1 });
}

function openLedgerStatement(ledgerId, name) { return drillTo(name, (el) => ledgerStatementView(el, ledgerId)); }
async function ledgerStatementView(el, ledgerId) {
  const r = await api(`/reports/ledger?${rParams({ ledger_id: ledgerId })}`);
  el.innerHTML = `<p class="muted">${esc(r.group)} · ${esc(r.from)} to ${esc(r.to)} · click a voucher to open it</p><table id="t"></table>`;
  const rows = [{ particulars: "Opening balance", balance: r.opening, _class: "total" }, ...r.lines,
    { particulars: "Closing balance", debit: r.total_debit, credit: r.total_credit, balance: r.closing, _class: "total" }];
  const bal = (v) => (v == null ? "" : `${money(Math.abs(v))} ${+v >= 0 ? "Dr" : "Cr"}`);
  table($("#t"), [{ label: "Date", key: "date" }, { label: "Particulars", key: "particulars", wrap: true }, { label: "Type", key: "voucher_type" },
    { label: "No.", key: "voucher_number" }, { label: "Debit", num: true, get: (x) => (+x.debit ? money(x.debit) : "") },
    { label: "Credit", num: true, get: (x) => (+x.credit ? money(x.credit) : "") }, { label: "Balance", num: true, get: (x) => bal(x.balance) }],
    rows, (x) => x.voucher_id && drillVoucher(x.voucher_id, `${x.voucher_type} ${x.voucher_number}`));
}

// The voucher itself, inside the report trail; ledgers and items in it drill further
function drillVoucher(id, title) { return drillTo(title || "Voucher", (el) => voucherView(el, id)); }
async function voucherView(el, id) {
  const v = await api(`/vouchers/${id}`), d = v.doc;
  const ledgerIds = Object.fromEntries((state.ledgers || []).map((l) => [l.name, l.id]));
  el.innerHTML = `<div class="row between"><h3>${esc(d.voucher_type)} ${esc(d.voucher_number)} · ${esc(d.date)}</h3>
      <span>${chip(v.status)} <button type="button" id="v-open">Open in Vouchers tab</button></span></div>
    ${d.party_ledger ? `<p>Party: <b>${esc(d.party_ledger)}</b>${d.reference_no ? ` · Ref ${esc(d.reference_no)}` : ""}</p>` : ""}
    <p>${esc(d.narration || "")}</p>
    ${d.entries.length ? `<table id="v-e"></table>` : `<p class="muted">No ledger entries (stock-only voucher)</p>`}
    ${d.inventory.length ? inventoryTable(d.inventory) : ""}
    <div id="v-hist"><p class="muted">Loading history…</p></div>`;
  historyHtml(id).then((html) => { if ($("#v-hist")) $("#v-hist").innerHTML = html; }).catch(() => {});
  if (d.entries.length) table($("#v-e"), [{ label: "Ledger", key: "ledger" },
    { label: "Debit", num: true, get: (e) => (+e.amount > 0 ? money(e.amount) : "") },
    { label: "Credit", num: true, get: (e) => (+e.amount < 0 ? money(-e.amount) : "") },
    { label: "Bills", get: (e) => e.bills.map((b) => `${b.type} ${b.name || ""}`).join(", ") }],
    d.entries, (e) => ledgerIds[e.ledger] && openLedgerStatement(ledgerIds[e.ledger], e.ledger));
  $("#v-open").onclick = () => openVoucher(id);
}

// ------------------------------------------------------------------ ledgers
// Shared by the ledger and stock item detail dialogs
function detailBlock(title, pairs) {
  const shown = pairs.filter(([, v]) => v !== null && v !== undefined && v !== "");
  return `<section><h3>${title}</h3>${shown.length ? `<dl>${shown.map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join("")}</dl>` : `<p class="muted small">Not given in Tally</p>`}</section>`;
}

renderers.masters = async () => {
  const q = $("#lq").value;
  const rows = q ? await api(`/companies/${state.company}/masters/ledgers?q=${encodeURIComponent(q)}`) : state.ledgers;
  const bank = (r) => (r.party_banks && r.party_banks[0]) ||
    (r.bank_account_no ? { bank_name: r.bank_name || "Own account", account_no: r.bank_account_no, ifsc: r.bank_ifsc } : null);
  table($("#ltable"), [
    { label: "Name", key: "name", wrap: true }, { label: "Group", key: "group", wrap: true },
    { label: "Address", wrap: true, get: (r) => (r.address || "").split("\n").join(", ") },
    { label: "State", key: "state" }, { label: "PIN", key: "pincode" },
    { label: "Contact", wrap: true, get: (r) => [r.contact_person, r.mobile, r.phone].filter(Boolean).join(" · ") },
    { label: "Email", key: "email", wrap: true }, { label: "PAN", key: "pan" }, { label: "GSTIN", key: "gstin" },
    { label: "Credit days", num: true, key: "credit_days" },
    { label: "MSME", wrap: true, get: (r) => [r.msme_type, r.msme_udyam_no].filter(Boolean).join(" ") },
    { label: "Bank", wrap: true, get: (r) => { const b = bank(r); return b ? [b.bank_name, b.account_no, b.ifsc].filter(Boolean).join(" ") : ""; } },
    { label: "Opening", num: true, get: (r) => money(r.opening_balance) },
    { label: "In Tally", get: (r) => (r.tally_guid ? "✓" : "") },
  ], rows.slice(0, 1000), (r) => showLedger(r.name).catch((e) => alert(e.message)));
};
let lqTimer; $("#lq").oninput = () => { clearTimeout(lqTimer); lqTimer = setTimeout(renderers.masters, 250); };

// ------------------------------------------------------------------ stock items
const qty = (v, unit) => (v == null ? "" : `${Number(v).toLocaleString("en-IN", { maximumFractionDigits: 4 })} ${unit || ""}`.trim());
const pct = (v) => (v == null ? "" : `${Number(v)}%`);
const amt = (v) => (v == null ? null : money(v));
const conversionText = (i) => (i.alternate_unit && i.conversion ? `${Number(i.denominator || 1)} ${i.unit} = ${Number(i.conversion)} ${i.alternate_unit}` : "");
let itemsCache = null;
renderers.items = async () => {
  if (!itemsCache || itemsCache.company !== state.company)
    itemsCache = { company: state.company, rows: await api(`/companies/${state.company}/masters/stock-items`) };
  const q = $("#iq").value.trim().toLowerCase();
  const rows = q ? itemsCache.rows.filter((i) => [i.name, i.group, i.hsn_code, i.alias].some((v) => (v || "").toLowerCase().includes(q))) : itemsCache.rows;
  $("#icount").textContent = `${rows.length} of ${itemsCache.rows.length} items` + (rows.length > 1000 ? " (first 1,000 shown; search to narrow)" : "");
  table($("#itable"), [
    { label: "Name", key: "name", wrap: true }, { label: "Group", key: "group", wrap: true },
    { label: "Unit", wrap: true, get: (i) => [i.unit, conversionText(i)].filter(Boolean).join(" · ") },
    { label: "HSN", key: "hsn_code" }, { label: "GST", num: true, get: (i) => pct(i.gst_rate) },
    { label: "GST from", get: (i) => i.gst_source || "" },
    { label: "Opening", num: true, get: (i) => qty(i.opening_qty, i.unit) }, { label: "Opening value", num: true, get: (i) => money(i.opening_value) },
    { label: "Closing", num: true, get: (i) => qty(i.closing_qty, i.unit) }, { label: "Closing value", num: true, get: (i) => amt(i.closing_value) || "" },
    { label: "Std cost", num: true, get: (i) => amt(i.standard_cost) || "" },
    { label: "Costing", key: "valuation_method" },
  ], rows.slice(0, 1000), showItem);
};
let iqTimer; $("#iq").oninput = () => { clearTimeout(iqTimer); iqTimer = setTimeout(renderers.items, 200); };

function showItem(i) {
  const yn = (b) => (b ? "Yes" : "No");
  const split = Object.entries(i.gst_rates || {}).map(([k, v]) => `${k} ${v}%`).join(", ");
  const godowns = (i.opening_allocations || []).map((a) =>
    `<tr><td>${esc(a.godown)}</td><td>${esc(a.batch)}</td><td class="num">${esc(qty(a.qty, i.unit))}</td><td class="num">${money(a.rate)}</td><td class="num">${money(a.value)}</td></tr>`).join("");
  $("#id-name").textContent = i.name;
  $("#id-body").innerHTML = `<p class="muted">${esc(i.group || "No group")}${i.alias ? " · alias " + esc(i.alias) : ""}</p><div class="details">
    ${detailBlock("Units", [["Base unit", i.unit], ["Alternate unit", i.alternate_unit], ["Conversion", conversionText(i)], ["Category", i.category]])}
    ${detailBlock("GST", [["Applicable", i.gst_applicable], ["Supply type", i.supply_type], ["HSN/SAC", i.hsn_code], ["HSN description", i.hsn_description],
      ["GST rate", pct(i.gst_rate)], ["Rate split", split], ["Taxability", i.taxability], ["ITC eligible", i.itc_eligible == null ? null : yn(i.itc_eligible)],
      ["Taken from", i.gst_source === "company" ? "company GST settings (not imported)" : i.gst_source]])}
    ${detailBlock("Stock", [["Opening", qty(i.opening_qty, i.unit)], ["Opening rate", money(i.opening_rate)], ["Opening value", money(i.opening_value)],
      ["Closing", qty(i.closing_qty, i.unit)], ["Closing rate", amt(i.closing_rate)], ["Closing value", amt(i.closing_value)],
      ["Reorder level", i.reorder_level == null ? null : qty(i.reorder_level, i.unit)]])}
    ${detailBlock("Costing & settings", [["Costing method", i.valuation_method], ["Market valuation", i.market_valuation],
      ["Standard cost", amt(i.standard_cost)], ["Standard selling price", amt(i.standard_price)],
      ["Batch-wise", yn(i.batch_wise)], ["Manufacturing date", yn(i.has_mfg_date)], ["Expiry dates", yn(i.perishable)],
      ["Ignore negative stock", yn(i.ignore_negative_stock)], ["Entered by", i.entered_by]])}
  </div>` + (godowns ? `<h3>Opening stock by godown</h3><table><thead><tr><th>Godown</th><th>Batch</th><th class="num">Qty</th><th class="num">Rate</th><th class="num">Value</th></tr></thead><tbody>${godowns}</tbody></table>` : "");
  $("#item-dlg").showModal();
}
$("#id-close").onclick = () => $("#item-dlg").close();

// Ledger details: address, contacts, bank and MSME as imported from Tally
async function showLedger(name, reveal = false) {
  const rows = await api(`/companies/${state.company}/masters/ledgers?q=${encodeURIComponent(name)}${reveal ? "&reveal=true" : ""}`);
  const l = rows.find((r) => r.name === name);
  if (!l) return;
  const block = detailBlock;
  const contacts = (l.contacts || []).map((c) => [c.label || "Phone", `${c.isd || ""} ${c.number}${c.whatsapp ? " (WhatsApp)" : ""}`]);
  const banks = (l.party_banks || []).map((b, i) => block(`Party bank ${l.party_banks.length > 1 ? i + 1 : ""}${b.default ? " (default)" : ""}`, [
    ["Bank", b.bank_name], ["Account no.", b.account_no], ["IFSC", b.ifsc], ["Favouring", b.favouring], ["Transfer", b.transfer_type]]));
  $("#ld-name").textContent = l.name;
  $("#ld-body").innerHTML = `<p class="muted">${esc(l.group)} · ${l.tally_guid ? "linked to Tally" : "ERP only"}</p><div class="details">
    ${block("Address", [["Mailing name", l.mailing_name], ["Address", l.address], ["PIN code", l.pincode], ["State", l.state], ["Country", l.country]])}
    ${block("Contact", [["Contact person", l.contact_person], ["Phone", l.phone], ["Mobile", l.mobile], ["Fax", l.fax], ["Email", l.email], ["Email CC", l.email_cc], ["Website", l.website], ...contacts])}
    ${block("Tax & credit", [["PAN", l.pan], ["GSTIN", l.gstin], ["GST registration", l.gst_registration_type], ["Place of supply", l.place_of_supply],
      ["Credit period", l.credit_days != null ? `${l.credit_days} days` : null], ["Bill-wise", l.is_billwise ? "Yes" : "No"], ["Opening", money(l.opening_balance)]])}
    ${block("MSME", [["Enterprise type", l.msme_type], ["Udyam no.", l.msme_udyam_no], ["Activity", l.msme_activity], ["Registered from", l.msme_from]])}
    ${l.bank_account_no || l.bank_ifsc ? block("Bank account", [["Account no.", l.bank_account_no], ["IFSC", l.bank_ifsc], ["Branch", l.bank_branch], ["Holder", l.bank_account_holder]]) : ""}
    ${banks.join("") || block("Party bank", [])}
  </div>`;
  $("#ld-reveal").hidden = reveal;
  $("#ld-reveal").onclick = () => showLedger(name, true).catch((e) => alert(e.message));
  if (!$("#ledger-dlg").open) $("#ledger-dlg").showModal();
}
$("#ld-close").onclick = () => $("#ledger-dlg").close();

// ------------------------------------------------------------------ ledger creation / alteration (Tally's fields)
const STATES = ["Andaman & Nicobar Islands", "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chandigarh",
  "Chhattisgarh", "Dadra & Nagar Haveli and Daman & Diu", "Delhi", "Goa", "Gujarat", "Haryana", "Himachal Pradesh",
  "Jammu & Kashmir", "Jharkhand", "Karnataka", "Kerala", "Ladakh", "Lakshadweep", "Madhya Pradesh", "Maharashtra",
  "Manipur", "Meghalaya", "Mizoram", "Nagaland", "Odisha", "Puducherry", "Punjab", "Rajasthan", "Sikkim", "Tamil Nadu",
  "Telangana", "Tripura", "Uttar Pradesh", "Uttarakhand", "West Bengal", "Other Territory"];
const COUNTRIES = ["India", "Nepal", "Bhutan", "Bangladesh", "Sri Lanka", "UAE", "Saudi Arabia", "China",
  "United States of America", "United Kingdom", "Germany", "Italy", "Turkey"];
$("#lf-states").innerHTML = STATES.map((n) => `<option value="${esc(n)}">`).join("");
$("#lf-countries").innerHTML = COUNTRIES.map((n) => `<option value="${esc(n)}">`).join("");
const LF_TEXT = ["name", "group", "mailing_name", "address", "state", "country", "pincode", "pan", "gst_registration_type",
  "gstin", "place_of_supply", "contact_person", "phone", "mobile", "fax", "email", "email_cc", "website", "msme_type",
  "msme_udyam_no", "msme_activity", "msme_from", "bank_account_holder", "bank_account_no", "bank_ifsc", "bank_name", "bank_branch"];
let lfOriginal = null;   // ledger being altered (null when creating)
// form.elements, not form[name]: on a <form>, `.name` is the form's own attribute
const lfField = (n) => $("#lf").elements.namedItem(n);

function underGroup(groupName, ...targets) {
  for (let g = state.groups?.[groupName], hops = 0; g && hops < 50; g = state.groups[g.parent], hops++)
    if (targets.includes(g.name)) return true;
  return false;
}
function lfBankSections() {
  const isBank = underGroup(lfField("group").value, "Bank Accounts", "Bank OD A/c");
  $("#lf-own-bank").hidden = !isBank;
  $("#lf-party-banks").hidden = isBank;
}
function lfBankRow(b = {}) {
  const div = document.createElement("div");
  div.className = "bank-row";
  div.innerHTML = `<div class="row"><label>Bank name<input data-b="bank_name" value="${esc(b.bank_name)}"></label>
      <label>A/c no.<input data-b="account_no" value="${esc(b.account_no)}"></label>
      <label>IFS code<input data-b="ifsc" class="upper" maxlength="11" value="${esc(b.ifsc)}"></label></div>
    <div class="row"><label>Favouring name<input data-b="favouring" value="${esc(b.favouring)}"></label>
      <label>Transaction type<select data-b="transfer_type">${["", "Inter Bank Transfer", "Same Bank Transfer", "NEFT", "RTGS", "IMPS", "Cheque"]
        .map((t) => `<option ${t === (b.transfer_type || "") ? "selected" : ""}>${t}</option>`).join("")}</select></label>
      <label class="check"><input type="checkbox" data-b="default" ${b.default ? "checked" : ""}> Default</label>
      <button type="button" class="link" data-remove>Remove</button></div>`;
  div.dataset.label = b.label || "";
  div.querySelector("[data-remove]").onclick = () => div.remove();
  $("#lf-banks").appendChild(div);
}
function lfBanks() {
  return [...document.querySelectorAll("#lf-banks .bank-row")].map((row) => {
    const b = { label: row.dataset.label || null };
    row.querySelectorAll("[data-b]").forEach((el) => (b[el.dataset.b] = el.type === "checkbox" ? el.checked : el.value.trim() || null));
    return b;
  }).filter((b) => b.bank_name || b.account_no);
}
$("#lf-add-bank").onclick = () => lfBankRow({ favouring: lfField("name").value });
lfField("group").oninput = lfBankSections;
$("#lf-cancel").onclick = () => $("#lf-dlg").close();

function openLedgerForm(l = null) {
  const f = $("#lf");
  f.reset(); $("#lf-msg").textContent = ""; $("#lf-banks").innerHTML = "";
  lfOriginal = l;
  $("#lf-title").textContent = l ? `Ledger Alteration: ${l.name}` : "Ledger Creation";
  if (l) {
    for (const k of LF_TEXT) if (lfField(k)) lfField(k).value = l[k] ?? "";
    lfField("is_billwise").checked = !!l.is_billwise;
    lfField("credit_days").value = l.credit_days ?? "";
    const ob = Number(l.opening_balance || 0);
    lfField("opening_amount").value = ob ? Math.abs(ob).toFixed(2) : "";
    lfField("opening_side").value = ob < 0 ? "Cr" : "Dr";
    (l.party_banks || []).forEach((b) => lfBankRow(b));
  } else {
    lfField("country").value = "India";
  }
  lfBankSections();
  $("#lf-dlg").showModal();
  lfField("name").focus();
}

function lfBody() {
  const f = $("#lf"), body = {};
  for (const k of LF_TEXT) if (lfField(k)) body[k] = lfField(k).value.trim() || null;
  body.is_billwise = lfField("is_billwise").checked;
  body.credit_days = lfField("credit_days").value === "" ? null : Number(lfField("credit_days").value);
  const amount = Number(lfField("opening_amount").value || 0);
  body.opening_balance = (lfField("opening_side").value === "Cr" ? -amount : amount).toFixed(2);
  body.party_banks = $("#lf-party-banks").hidden ? (lfOriginal?.party_banks || null) : (lfBanks().length ? lfBanks() : null);
  return body;
}
const same = (a, b) => JSON.stringify(a ?? null) === JSON.stringify(b ?? null) ||
  (a != null && b != null && !isNaN(a) && !isNaN(b) && Number(a) === Number(b));

$("#lf").onsubmit = async (e) => {
  e.preventDefault();
  const body = lfBody();
  if (!body.name || !body.group) { $("#lf-msg").textContent = "Name and group are required"; return; }
  try {
    let saved;
    if (lfOriginal) {
      const changes = Object.fromEntries(Object.entries(body).filter(([k, v]) => !same(v, lfOriginal[k])));
      if (!Object.keys(changes).length) { $("#lf-dlg").close(); return; }
      saved = await api(`/companies/${state.company}/masters/ledgers/${lfOriginal.id}`,
        { method: "PATCH", body: { ...changes, expected_version: lfOriginal.version } });
    } else {
      saved = await api(`/companies/${state.company}/masters/ledgers`, { method: "POST", body });
    }
    $("#lf-dlg").close();
    await loadMasters();
    if (currentTab === "masters") renderers.masters();
    await showLedger(saved.name);
  } catch (err) { $("#lf-msg").textContent = err.message; }
};
$("#l-new").onclick = () => openLedgerForm();
$("#ld-edit").onclick = async () => {
  // the form needs the unmasked PAN and bank numbers, which requires permission to edit ledgers
  try {
    const name = $("#ld-name").textContent;
    const rows = await api(`/companies/${state.company}/masters/ledgers?q=${encodeURIComponent(name)}&reveal=true`);
    const l = rows.find((r) => r.name === name);
    $("#ledger-dlg").close();
    openLedgerForm(l);
  } catch (err) { alert(err.message); }
};

// ------------------------------------------------------------------ connector settings
const CF = { host: "#c-host", port: "#c-port", tally_company_name: "#c-name", alter_by: "#c-alter", inbound_from_date: "#c-from" };
const CB = { enabled: "#c-enabled", inbound_enabled: "#c-in", outbound_enabled: "#c-out", freeze_outbound: "#c-fout", freeze_inbound: "#c-fin" };
renderers.settings = async () => {
  let c = null;
  try { c = await api(`/sync/connectors/${state.company}`); } catch (_) { $("#c-msg").textContent = "Not configured yet"; }
  if (!c) return;
  for (const [k, s] of Object.entries(CF)) $(s).value = c[k] ?? "";
  for (const [k, s] of Object.entries(CB)) $(s).checked = !!c[k];
  $("#c-ftypes").value = (c.frozen_voucher_types || []).join(", ");
};
$("#cform").onsubmit = async (e) => {
  e.preventDefault();
  const body = { frozen_voucher_types: $("#c-ftypes").value.split(",").map((s) => s.trim()).filter(Boolean) };
  for (const [k, s] of Object.entries(CF)) body[k] = $(s).value || null;
  for (const [k, s] of Object.entries(CB)) body[k] = $(s).checked;
  body.port = +body.port;
  if (body.outbound_enabled && !confirm("Outbound sync writes vouchers into Tally. Use a test company until reconciliation is signed off. Continue?")) return;
  try { await api(`/sync/connectors/${state.company}`, { method: "PUT", body }); $("#c-msg").innerHTML = `<span class="okmsg">Saved</span>`; }
  catch (err) { $("#c-msg").innerHTML = `<span class="error">${esc(err.message)}</span>`; }
};
function cResult(text) { const el = $("#c-result"); el.hidden = false; el.textContent = text; }
$("#c-test").onclick = async (e) => {
  e.target.disabled = true; cResult("Contacting Tally…");
  try {
    const r = await api(`/sync/connectors/${state.company}/check`, { method: "POST" });
    cResult([
      `Tally server:     ${r.server_running ? "running" : "not reachable"}`,
      `Linked company:   ${r.tally_company_name}`,
      `Result:           ${r.matched ? "✓ found in Tally (name and GUID match)" : "✗ " + r.status.replace(/_/g, " ")}`,
      r.matched ? `Books from:       ${r.books_from}` : `Details:          ${r.message}`,
      r.open_companies.length ? `Open in Tally:    ${r.open_companies.join("\n                  ")}` : "",
      `Checked:          ${when(r.checked_at)}`,
    ].filter(Boolean).join("\n"));
  } catch (err) { cResult(err.message); } finally { e.target.disabled = false; }
};
$("#c-import").onclick = async () => {
  await api("/import/tally", { method: "POST", body: { company_id: state.company, masters: true } });
  cResult("Master import started. Batches:");
  setTimeout(async () => { cResult(JSON.stringify(await api(`/import/batches?${cq()}`), null, 2)); await loadMasters(); }, 3000);
};

// ------------------------------------------------------------------ audit
renderers.audit = async () => {
  const rows = await api(`/audit?${cq()}&limit=200`);
  table($("#atable"), [{ label: "When", get: (r) => when(r.created_at) }, { label: "Actor", key: "actor" }, { label: "Kind", key: "actor_type" },
    { label: "Action", key: "action" }, { label: "Entity", key: "entity_type" }, { label: "Reason", key: "reason", wrap: true },
    { label: "Correlation", get: (r) => (r.correlation_id || "").slice(0, 8) }], rows,
    (r) => alert(JSON.stringify({ before: r.before, after: r.after }, null, 2)));
};


// ------------------------------------------------------------------ users and permissions (company admin)
let permCatalog = null;
let editingUser = null;
let chosenPerms = new Set();
const catalog = async () => (permCatalog ||= await api("/permissions"));
const rolePerms = (key) => new Set(permCatalog.roles.find((r) => r.key === key)?.permissions || []);
const sameSet = (a, b) => a.size === b.size && [...a].every((x) => b.has(x));

function permSummary(perms) {
  const has = new Set(perms);
  const parts = [];
  for (const g of permCatalog.groups) {
    if (g.name === "Reports") {
      const n = g.permissions.filter((p) => p.key.startsWith("report.") && has.has(p.key)).length;
      if (has.has("reports.read")) parts.push("All reports"); else if (n) parts.push(`${n} report${n === 1 ? "" : "s"}`);
    } else if (g.permissions.some((p) => has.has(p.key))) parts.push(g.name);
  }
  return parts.join(", ") || "Nothing";
}

renderers.users = async () => {
  await catalog();
  const [rows, lic] = await Promise.all([api(`/companies/${state.company}/users`),
    api(`/licence?company_id=${state.company}`).catch(() => ({}))]);
  $("#u-company").textContent = $("#company").selectedOptions[0]?.textContent || "";
  $("#u-seats").textContent = lic.max_users != null
    ? `Licence: ${lic.max_users} users at the same time · ${lic.users_used} working now. You can make as many user IDs as you need; `
      + "when all seats are taken, the next person can sign in once someone signs out." : "";
  const status = (u) => !u.is_active ? chip("switched_off") : u.locked ? chip("locked")
    : u.must_change_password ? chip("password_pending") : chip("active");
  table($("#utable"), [
    { label: "User ID", key: "username" }, { label: "Name", key: "display_name" },
    { label: "Role", get: (u) => (u.custom ? "Custom" : u.role_label) },
    { label: "Can use", wrap: true, get: (u) => permSummary(u.permissions) },
    { label: "Status", html: status },
    { label: "Working now", html: (u) => (u.online ? `${chip("active")} <span class="small muted">${when(u.active_at)}</span>` : "") },
    { label: "Last sign-in", get: (u) => when(u.last_login_at) || "never" },
    { label: "Other companies", num: true, get: (u) => u.other_companies || "" },
  ], rows.map((u) => ({ ...u, _class: u.is_active ? "" : "muted" })), openUserForm);
};

function drawPerms() {
  const self = editingUser && editingUser.id === state.user.id;
  const all = chosenPerms.has("reports.read");
  $("#uf-perms").innerHTML = permCatalog.groups.map((g) => `<fieldset><legend>${esc(g.name)}</legend>${g.permissions.map((p) => {
    const sub = p.key.startsWith("report.");
    const on = chosenPerms.has(p.key) || (sub && all);
    // An admin can only hand out what they may do themselves; your own ticks are changed by another admin.
    const off = self || !can(p.key) || (sub && all);
    return `<label class="check ${sub ? "sub" : ""}"><input type="checkbox" data-perm="${esc(p.key)}" ${on ? "checked" : ""} ${off ? "disabled" : ""}> ${esc(p.label)}</label>`;
  }).join("")}</fieldset>`).join("");
  $("#uf-perms").querySelectorAll("input[data-perm]").forEach((cb) => (cb.onchange = () => {
    cb.checked ? chosenPerms.add(cb.dataset.perm) : chosenPerms.delete(cb.dataset.perm);
    if (cb.dataset.perm === "reports.read" && cb.checked)   // "All reports" replaces the single ones
      [...chosenPerms].filter((k) => k.startsWith("report.")).forEach((k) => chosenPerms.delete(k));
    const match = permCatalog.roles.find((r) => sameSet(new Set(r.permissions), chosenPerms));
    $("#uf").elements.namedItem("role").value = match ? match.key : "custom";
    drawPerms();
  }));
}

async function openUserForm(u) {
  await catalog();
  editingUser = u || null;
  const f = $("#uf"), el = (n) => f.elements.namedItem(n);
  const self = !!u && u.id === state.user.id;
  f.reset(); $("#uf-msg").textContent = ""; $("#uf-existing-row").hidden = true;
  $("#uf-title").textContent = u ? `Alter user: ${u.username}` : "Add user";
  el("username").value = u ? u.username : ""; el("username").disabled = !!u;
  el("display_name").value = u?.display_name || "";
  el("password").value = ""; el("password").disabled = self; $("#uf-gen").disabled = self;
  $("#uf-pw-label").textContent = self ? "Password (use Change password at the top)" : u ? "New password (leave empty to keep)" : "First password *";
  el("role").innerHTML = permCatalog.roles.map((r) => `<option value="${esc(r.key)}">${esc(r.label)}</option>`).join("")
    + `<option value="custom">Custom</option>`;
  el("role").value = u ? (u.custom ? "custom" : u.role) : "accountant";
  el("role").disabled = self;
  chosenPerms = new Set(u ? u.permissions : rolePerms("accountant"));
  $("#uf-perm-note").textContent = self ? "These are your own permissions; another admin can change them."
    : "Choosing a role ticks its permissions. Change any tick for a custom set. Running Tally sync and changing the "
      + "Tally connection are for the company admin (manager) only.";
  drawPerms();
  $("#uf-actions").hidden = !u || self;
  if (u) { $("#uf-active").textContent = u.is_active ? "Switch off user" : "Switch on user"; $("#uf-unlock").hidden = !u.locked; $("#uf-signout").hidden = !u.online; }
  $("#user-dlg").showModal();
  (u ? el("display_name") : el("username")).focus();
}
$("#u-new").onclick = () => openUserForm(null);
$("#uf-cancel").onclick = () => $("#user-dlg").close();
$("#uf").elements.namedItem("role").onchange = (e) => {
  if (e.target.value !== "custom") { chosenPerms = rolePerms(e.target.value); drawPerms(); }
};
$("#uf-gen").onclick = () => {
  const abc = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789";
  const r = crypto.getRandomValues(new Uint32Array(10));
  $("#uf").elements.namedItem("password").value = [...r].map((n) => abc[n % abc.length]).join("");
};

$("#uf").onsubmit = async (e) => {
  e.preventDefault();
  const f = e.target, el = (n) => f.elements.namedItem(n);
  const role = el("role").value, password = el("password").value;
  const permissions = role === "custom" ? [...chosenPerms] : null;
  const username = editingUser ? editingUser.username : el("username").value.trim();
  $("#uf-msg").textContent = "";
  try {
    if (editingUser) {
      const body = { display_name: el("display_name").value.trim() || null };
      const wasRole = editingUser.custom ? "custom" : editingUser.role;
      if (editingUser.id !== state.user.id && (role !== wasRole || !sameSet(chosenPerms, new Set(editingUser.permissions)))) {
        body.role = role; body.permissions = permissions;
      }
      if (password) body.password = password;
      await api(`/companies/${state.company}/users/${editingUser.id}`, { method: "PATCH", body });
    } else {
      await api(`/companies/${state.company}/users`, { method: "POST", body: {
        username, display_name: el("display_name").value.trim() || null, password: password || null,
        role, permissions, add_existing: el("add_existing").checked } });
    }
    $("#user-dlg").close();
    if (password) alert(`Give ${username} this password:\n\n${password}\n\nThey choose their own at the first sign-in.`);
    await renderers.users();
  } catch (err) {
    $("#uf-msg").textContent = err.message;
    if (/already taken/.test(err.message)) $("#uf-existing-row").hidden = false;
  }
};

async function patchUser(body, done) {
  try { await api(`/companies/${state.company}/users/${editingUser.id}`, { method: "PATCH", body }); $("#user-dlg").close(); await renderers.users(); if (done) alert(done); }
  catch (err) { $("#uf-msg").textContent = err.message; }
}
$("#uf-active").onclick = () => {
  const on = !editingUser.is_active;
  if (!on && !confirm(`Switch off ${editingUser.username}? They are signed out at once and cannot sign in until switched on again.`)) return;
  patchUser({ is_active: on });
};
$("#uf-unlock").onclick = () => patchUser({ unlock: true }, `${editingUser.username} can sign in again.`);
$("#uf-signout").onclick = async () => {
  if (!confirm(`Sign ${editingUser.username} out now? Unsaved work in their browser is lost; their licence seat is free at once.`)) return;
  try { await api(`/companies/${state.company}/users/${editingUser.id}/sign-out`, { method: "POST" }); $("#user-dlg").close(); await renderers.users(); }
  catch (err) { $("#uf-msg").textContent = err.message; }
};
$("#uf-remove").onclick = async () => {
  const name = $("#company").selectedOptions[0]?.textContent;
  if (!confirm(`Remove ${editingUser.username} from ${name}? Their entries stay in the books and the audit trail.`)) return;
  try { await api(`/companies/${state.company}/users/${editingUser.id}`, { method: "DELETE" }); $("#user-dlg").close(); await renderers.users(); }
  catch (err) { $("#uf-msg").textContent = err.message; }
};

// ------------------------------------------------------------------ server: update from GitHub and restart (super admin)
let serverTimer = null;
const busy = (s) => s.request_pending || s.state === "running";
function pollServer(ms) {
  clearTimeout(serverTimer);
  serverTimer = setTimeout(() => { if (currentTab === "server") renderers.server().catch(() => pollServer(3000)); }, ms);
}
// A customer's server updates from the licence server; the cloud deploys from GitHub.
async function clientUpdateView() {
  const l = await api("/licence");
  const u = l.update_available;
  $("#svc-cards").innerHTML = [
    ["Running version", esc(l.version), esc(l.customer ? `licensed to ${l.customer}` : "")],
    ["On the licence server", u ? esc(u.version) : "—", u ? (u.verified ? "downloaded and checked" : "ready to download")
      : "press Update to check"],
    ["Last licence check", esc(when(l.last_check_at) || "never"), esc(l.server_url || "")],
  ].map(([k, v, sub]) => `<div class="card"><div class="k">${esc(k)}</div><div class="v">${v}</div><div class="small muted">${sub}</div></div>`).join("");
  $("#svc-update").disabled = !l.activated;
  if (!l.activated) $("#svc-msg").textContent = "Activate this server on the Licence tab first.";
  else if (!l.can_install_updates) $("#svc-msg").textContent = "This server was not set up with the Windows installer, so updates are installed by hand.";
}
$("#svc-update").onclick = async (e) => {
  const btn = e.target, msg = $("#svc-msg");
  btn.disabled = true; msg.className = "muted"; msg.textContent = "Asking the licence server…";
  try {
    const r = await api("/licence/update/download", { method: "POST" });
    if (!r.update) { msg.textContent = r.message; return; }
    await clientUpdateView();
    if (!confirm(`Version ${r.update.version} is downloaded and checked (signed by Voyager).\n\n${r.update.release_notes || ""}\n\n`
      + "Install it now? Tally Cloud restarts and everyone is disconnected for a minute or two.")) { msg.textContent = r.message; return; }
    const i = await api("/licence/update/install", { method: "POST" });
    msg.textContent = i.message;
    waitForRestart(r.update.version, msg);
  } catch (err) { msg.className = "error"; msg.textContent = err.message; }
  finally { btn.disabled = false; }
};

renderers.server = async () => {
  const client = state.user?.license_mode === "client";
  $("#sv-client").hidden = !client; $("#sv-github").hidden = client;
  if (client) return clientUpdateView();
  let s;
  try { s = await api("/admin/system"); }
  catch (err) {
    // During a restart the server does not answer for a few seconds.
    if (/not set up/.test(err.message)) {
      $("#sv-msg").textContent = "Deploying from this page is not set up on this server yet. Run once on the server, as root: "
        + "cd ~/projects/tallycloud && git pull && sudo bash deploy/server-setup.sh tallycloud.voyager.co.in <your e-mail>";
      ["#sv-check", "#sv-update", "#sv-restart"].forEach((id) => ($(id).disabled = true));
      return;
    }
    $("#sv-msg").textContent = "Waiting for the server to come back…"; pollServer(3000); return;
  }
  const label = { ok: "done", failed: "failed", running: "running", never: "never run" }[s.state] || s.state;
  $("#sv-cards").innerHTML = [
    ["Running version", esc(s.commit || "unknown"), esc([s.subject, s.committed_at && when(s.committed_at)].filter(Boolean).join(" · "))],
    ["Updates on GitHub", s.behind == null ? "?" : s.behind, s.behind == null ? "Check for updates to find out" : s.behind ? "ready to install" : "up to date"],
    ["Last run", `${chip(s.state === "ok" ? "completed" : s.state === "running" ? "processing" : s.state)}`,
      esc(`${s.action || ""} ${label}${s.finished_at ? " · " + when(s.finished_at) : ""}`)],
  ].map(([k, v, sub]) => `<div class="card"><div class="k">${esc(k)}</div><div class="v">${v}</div><div class="small muted">${sub}</div></div>`).join("");
  $("#sv-pending").innerHTML = s.pending?.length
    ? `<h3>New on GitHub</h3><pre class="small">${esc(s.pending.join("\n"))}</pre>` : "";
  $("#sv-log").textContent = s.log || "No run yet.";
  const wait = busy(s);
  ["#sv-check", "#sv-update", "#sv-restart"].forEach((id) => ($(id).disabled = wait));
  $("#sv-msg").textContent = wait ? "Working… this page refreshes by itself." : s.message || "";
  $("#sv-msg").className = s.state === "failed" && !wait ? "error" : "muted";
  if (wait) pollServer(2000);
};
async function serverAction(action, question) {
  if (question && !confirm(question)) return;
  try { await api(`/admin/system/${action}`, { method: "POST" }); $("#sv-msg").textContent = "Asked the server…"; pollServer(1500); }
  catch (err) { alert(err.message); }
}
$("#sv-check").onclick = () => serverAction("check");
$("#sv-update").onclick = () => serverAction("update", "Deploy the latest code from GitHub and restart Tally Cloud?\n\nEveryone is disconnected for a few seconds. If the new version does not start, the server goes back to the previous one.");
$("#sv-restart").onclick = () => serverAction("restart", "Restart Tally Cloud?\n\nEveryone is disconnected for a few seconds.");

// ------------------------------------------------------------------ licence: banner on every page
async function refreshBanner() {
  const b = $("#lic-banner");
  if (!state.user || state.user.license_mode === "off") { b.hidden = true; return; }
  try {
    const l = await api(`/licence${state.company ? `?company_id=${state.company}` : ""}`);
    const text = l.read_only ? l.reason : (l.warnings || []).join(" ");
    // On a customer's server the manager sees every new version at once, with one click to install it.
    const upd = l.update_available ? `Version ${esc(l.update_available.version)} is available on the licence server `
      + `(this server runs ${esc(l.version)}). <button type="button" class="small" id="banner-update">Update now</button>` : "";
    b.hidden = !text && !upd;
    b.innerHTML = [esc(text), upd].filter(Boolean).join(" ");
    b.classList.toggle("bad", !!l.read_only);
    const btn = $("#banner-update");
    if (btn) btn.onclick = () => show("server");
  } catch (_) { b.hidden = true; }
}

const fmtDate = (d) => (d ? new Date(d + (d.length === 10 ? "T00:00:00" : "")).toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" }) : "");
const size = (n) => (n > 1 << 20 ? `${(n / (1 << 20)).toFixed(1)} MB` : `${Math.ceil(n / 1024)} KB`);
const MODULE_LABELS = { accounting: "Vouchers and ledgers", inventory: "Inventory", reports: "Reports", tally_sync: "Tally sync", export: "Export to CSV" };

function licenceBox(l, admin) {
  const days = l.days_left == null ? "never expires" : l.days_left < 0 ? `expired ${-l.days_left} day(s) ago` : `${l.days_left} day(s) left`;
  const inst = l.installations.length ? `<table class="small"><thead><tr><th>Server</th><th>Version</th><th>Status</th><th>Activated</th><th>Last check-in</th>${admin ? "<th></th>" : ""}</tr></thead><tbody>${
    l.installations.map((i) => `<tr><td>${esc(i.server_label || i.hostname || i.installation_id.slice(0, 10))}</td><td>${esc(i.product_version || "")}</td>
      <td>${chip(i.status)}</td><td>${when(i.activated_at)}</td><td>${when(i.last_seen_at)}</td>${admin ? `<td>${
        i.status === "active" ? `<button type="button" class="secondary small" data-inst="${i.id}" data-act="release">Release slot</button>` : ""}
        ${i.status !== "blocked" ? `<button type="button" class="secondary small danger" data-inst="${i.id}" data-act="block">Block</button>`
          : `<button type="button" class="secondary small" data-inst="${i.id}" data-act="unblock">Unblock</button>`}</td>` : ""}</tr>`).join("")}</tbody></table>`
    : `<p class="muted small">Not activated on any server yet.</p>`;
  const actions = admin ? `<div class="row">${[
    ["renew", "Renew"], ["suspend", "Suspend"], ["resume", "Resume"], ["reissue_key", "New key"], ["cancel", "Cancel"], ["revoke", "Revoke"]]
    .map(([a, t]) => `<button type="button" class="secondary small ${a === "revoke" ? "danger" : ""}" data-lic="${l.id}" data-act="${a}">${t}</button>`).join("")}
    <button type="button" class="small" data-lic-edit="${l.id}">Change</button></div>` : "";
  return `<div class="lic-box"><div class="row between"><h4>${esc(l.plan[0].toUpperCase() + l.plan.slice(1))} licence ${esc(l.key_hint)}</h4>${chip(l.effective_status)}</div>
    <div class="grid small">
      <div><span class="muted">Valid</span><br>${fmtDate(l.starts_at)} – ${l.expires_at ? fmtDate(l.expires_at) : "no end"} (${days})</div>
      <div><span class="muted">Users at the same time</span><br>${l.max_users}</div>
      <div><span class="muted">Servers</span><br>${l.active_installations} of ${l.max_installations} in use</div>
      <div><span class="muted">Tally companies</span><br>${l.max_companies ?? "any number"}</div>
      <div><span class="muted">Versions</span><br>${esc(l.min_version || "any")} – ${esc(l.max_version || "any")}</div>
      <div><span class="muted">Offline grace</span><br>${l.offline_grace_days} days</div>
    </div>
    <p class="small"><span class="muted">Modules:</span> ${l.modules.map((m) => esc(m.label)).join(", ") || "none"}</p>
    ${inst}${actions}</div>`;
}

// ------------------------------------------------------------------ licence portal: super admin
let custCache = [];
renderers.customers = async () => {
  const [d, rows] = await Promise.all([api("/admin/licensing/dashboard"),
    api(`/admin/customers${$("#cust-q").value ? `?q=${encodeURIComponent($("#cust-q").value)}` : ""}`)]);
  custCache = rows;
  const st = d.licenses_by_status;
  $("#lic-cards").innerHTML = [
    ["Customers", d.customers, d.customers_suspended ? `${d.customers_suspended} suspended` : ""],
    ["Active licences", st.active || 0, `${st.pending || 0} not yet activated`],
    ["Expiring in 30 days", d.expiring_30_days.length, ""],
    ["Expired / suspended", (st.expired || 0) + (st.suspended || 0), `${st.revoked || 0} revoked`],
    ["Servers activated", d.installations_active, Object.entries(d.versions).map(([v, n]) => `v${v}: ${n}`).join(", ")],
    ["Failed activations (7 days)", d.activation_failures_7_days, ""],
    ["Downloads (30 days)", d.downloads_30_days, ""],
  ].map(([k, v, sub]) => `<div class="card"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div><div class="small muted">${esc(sub)}</div></div>`).join("");
  $("#lic-expiring").innerHTML = d.expiring_30_days.length ? `<h3>Expiring soon</h3><table>${d.expiring_30_days.map((e) =>
    `<tr class="click" data-cust="${e.customer_id}"><td>${esc(e.customer)}</td><td>${fmtDate(e.expires_at)}</td><td class="num">${e.days_left} day(s)</td></tr>`).join("")}</table>` : "";
  $("#lic-renewals").innerHTML = d.renewal_requests.length ? `<h3>Renewal requests</h3><table>${d.renewal_requests.map((e) =>
    `<tr class="click" data-cust="${e.customer_id}"><td>${when(e.at)}</td><td>${esc(e.customer)}</td><td>${esc(e.by)}</td><td class="wrap">${esc(e.message || "")}</td></tr>`).join("")}</table>` : "";
  document.querySelectorAll("#customers [data-cust]").forEach((tr) => (tr.onclick = () => openCustomer(tr.dataset.cust)));
  table($("#cust-table"), [
    { label: "Code", key: "code" }, { label: "Customer", key: "legal_name", wrap: true },
    { label: "Licence", html: (c) => (c.license_status ? chip(c.license_status) : `<span class="muted">none</span>`) },
    { label: "Plan", key: "plan" }, { label: "Expires", get: (c) => fmtDate(c.expires_at) },
    { label: "Servers", num: true, key: "installations" },
    { label: "Company admins", get: (c) => c.admins.map((a) => a.username).join(", ") },
    { label: "Account", html: (c) => chip(c.status) },
  ], rows, (c) => openCustomer(c.id));
};
$("#cust-q").onchange = () => renderers.customers();

$("#cust-new").onclick = () => {
  const f = $("#ncf"); f.reset(); $("#ncf-msg").textContent = "";
  f.elements.namedItem("starts_at").value = today();
  $("#newcust-dlg").showModal(); f.elements.namedItem("code").focus();
};
$("#ncf-cancel").onclick = () => $("#newcust-dlg").close();
$("#ncf").onsubmit = async (e) => {
  e.preventDefault();
  const f = e.target, v = (n) => f.elements.namedItem(n).value.trim() || null;
  const num = (n) => (v(n) ? Number(v(n)) : null);
  if (!v("code") || !v("legal_name") || !v("admin_username") || !num("max_users")) {
    $("#ncf-msg").textContent = "Fill in the customer code, legal name, number of users and the admin's user ID."; return;
  }
  const license = { plan: v("plan"), starts_at: v("starts_at"), max_users: num("max_users"),
    max_installations: num("max_installations"), max_companies: num("max_companies") };
  if (v("expires_at")) license.expires_at = v("expires_at");
  const body = { code: v("code"), legal_name: v("legal_name"), contact_name: v("contact_name"), contact_email: v("contact_email"),
    contact_phone: v("contact_phone"), gstin: v("gstin"), license,
    admin: { username: v("admin_username"), display_name: v("admin_name"), email: v("admin_email") || v("contact_email") } };
  const btn = f.querySelector("button"); btn.disabled = true;
  try {
    const c = await api("/admin/customers", { method: "POST", body });
    $("#newcust-dlg").close();
    await renderers.customers();
    await openCustomer(c.id);
    $("#cd-key").innerHTML = `<div class="lic-box"><p><b>Licence key</b> (shown only now): give it to ${esc(c.admin_username)}.
      They enter it when installing the software.</p><div class="keyline">${esc(c.license_key)}</div></div>`;
    showLink(c, c.admin_username);
  } catch (err) { $("#ncf-msg").textContent = err.message; } finally { btn.disabled = false; }
};

let custOpen = null;
async function openCustomer(id) {
  const c = await api(`/admin/customers/${id}`);
  custOpen = c;
  const companies = await api("/companies");
  $("#cd-title").textContent = `${c.legal_name} (${c.code})`; $("#cd-msg").textContent = "";
  const hosted = new Set(c.hosted_companies.map((h) => h.id));
  $("#cd-body").innerHTML = `
    <div class="row between"><span>${chip(c.status)} <span class="muted small">customer since ${fmtDate(c.created_at?.slice(0, 10))}</span></span>
      <span>${c.status === "active" ? `<button type="button" class="secondary danger" data-cstatus="suspended">Suspend customer</button>`
        : `<button type="button" class="secondary" data-cstatus="active">Make active</button>`}
        ${c.status !== "archived" ? `<button type="button" class="secondary" data-cstatus="archived">Archive</button>` : ""}</span></div>
    <form id="cd-form" class="grid">
      <label>Legal name<input name="legal_name" value="${esc(c.legal_name)}" required></label>
      <label>Contact person<input name="contact_name" value="${esc(c.contact_name || "")}"></label>
      <label>Contact e-mail<input name="contact_email" type="email" value="${esc(c.contact_email || "")}"></label>
      <label>Phone<input name="contact_phone" value="${esc(c.contact_phone || "")}"></label>
      <label>GSTIN<input name="gstin" value="${esc(c.gstin || "")}" class="upper"></label>
      <label>Notes<input name="notes" value="${esc(c.notes || "")}"></label>
      <div><button>Save details</button></div>
    </form>
    <h3>Company admins</h3>
    <table class="small">${c.admins.length ? c.admins.map((a) => `<tr><td>${esc(a.username)}</td><td>${esc(a.display_name || "")}</td><td>${esc(a.email || "")}</td>
      <td>${a.is_active ? (a.has_password ? chip("active") : chip("password_pending")) : chip("switched_off")}</td><td>${when(a.last_login_at) || "never signed in"}</td>
      <td><button type="button" class="secondary small" data-invite="${a.id}">${a.has_password ? "Password reset link" : "New invitation link"}</button>
        <button type="button" class="secondary small" data-admin-active="${a.id}" data-on="${a.is_active ? "0" : "1"}">${a.is_active ? "Switch off" : "Switch on"}</button></td></tr>`).join("")
      : `<tr><td class="muted">No company admin yet.</td></tr>`}</table>
    <button type="button" id="cd-add-admin" class="secondary">Add company admin</button>
    <div id="cd-link"></div>
    <div class="row between"><h3>Licences</h3><button type="button" id="cd-new-lic">New licence</button></div>
    <div id="cd-key"></div>
    ${c.license_list.map((l) => licenceBox(l, true)).join("") || `<p class="muted">No licence yet.</p>`}
    <h3>Books hosted on this server</h3>
    <p class="muted small">Tick the companies on this server that belong to ${esc(c.legal_name)}. They then follow its licence, and its company admins manage them.</p>
    <div class="row wrap">${companies.map((co) => `<label class="check"><input type="checkbox" data-hosted="${co.id}" ${hosted.has(co.id) ? "checked" : ""}> ${esc(co.name)}</label>`).join("") || `<span class="muted">No companies on this server.</span>`}</div>
    <button type="button" id="cd-hosted" class="secondary">Save hosted companies</button>
    <h3>History</h3><table id="cd-events" class="small"></table>`;
  if (!$("#cust-dlg").open) $("#cust-dlg").showModal();
  wireCustomer(c);
  api(`/admin/licensing/events?customer_id=${c.id}&limit=50`).then((ev) => table($("#cd-events"), [
    { label: "When", get: (e) => when(e.created_at) }, { label: "Event", get: (e) => e.event_type.replace(/_/g, " ") },
    { label: "Result", html: (e) => chip(e.result) }, { label: "By", key: "actor" },
    { label: "Details", wrap: true, get: (e) => Object.entries(e.details || {}).filter(([, v]) => v != null).map(([k, v]) => `${k}: ${v}`).join("; ") }], ev)).catch(() => {});
}
$("#cd-close").onclick = () => { $("#cust-dlg").close(); renderers.customers().catch(() => {}); };

function showLink(r, who) {
  $("#cd-link").innerHTML = `<div class="lic-box"><p>${r.emailed ? `E-mailed to ${esc(who)}. ` : ""}Send this one-time link to ${esc(who)}
    (valid ${r.expires_in_hours} hours). They set their own password with it; no password is sent.</p><div class="keyline">${esc(r.setup_link)}</div></div>`;
}

function wireCustomer(c) {
  const msg = (e) => ($("#cd-msg").textContent = e.message);
  const again = () => openCustomer(c.id).catch(msg);
  $("#cd-form").onsubmit = async (e) => {
    e.preventDefault();
    const body = Object.fromEntries([...new FormData(e.target)].map(([k, v]) => [k, v.trim() || null]));
    try { await api(`/admin/customers/${c.id}`, { method: "PATCH", body }); await again(); } catch (err) { msg(err); }
  };
  document.querySelectorAll("#cd-body [data-cstatus]").forEach((b) => (b.onclick = async () => {
    const s = b.dataset.cstatus;
    if (s !== "active" && !confirm(`${s === "suspended" ? "Suspend" : "Archive"} ${c.legal_name}? Its company admins are signed out and its servers become read-only at their next check-in. No data is deleted.`)) return;
    try { await api(`/admin/customers/${c.id}/status`, { method: "POST", body: { status: s } }); await again(); } catch (err) { msg(err); }
  }));
  $("#cd-add-admin").onclick = async () => {
    const username = prompt("User ID for the company admin (e.g. their e-mail or name)");
    if (!username) return;
    const display_name = prompt("Name", "") || null;
    const email = prompt("E-mail", c.contact_email || "") || null;
    try { const r = await api(`/admin/customers/${c.id}/admins`, { method: "POST", body: { username, display_name, email } }); await again(); showLink(r, username); }
    catch (err) { msg(err); }
  };
  document.querySelectorAll("#cd-body [data-invite]").forEach((b) => (b.onclick = async () => {
    try { const r = await api(`/admin/users/${b.dataset.invite}/invite`, { method: "POST" }); showLink(r, b.closest("tr").cells[0].textContent); } catch (err) { msg(err); }
  }));
  document.querySelectorAll("#cd-body [data-admin-active]").forEach((b) => (b.onclick = async () => {
    try { await api(`/admin/users/${b.dataset.adminActive}/active`, { method: "POST", body: { is_active: b.dataset.on === "1" } }); await again(); } catch (err) { msg(err); }
  }));
  $("#cd-new-lic").onclick = () => openLicenceForm(null, c);
  document.querySelectorAll("#cd-body [data-lic-edit]").forEach((b) => (b.onclick = () => openLicenceForm(c.license_list.find((l) => l.id === b.dataset.licEdit), c)));
  document.querySelectorAll("#cd-body [data-lic]").forEach((b) => (b.onclick = async () => {
    const a = b.dataset.act, body = { action: a };
    const ask = { suspend: "Suspend this licence? Its servers become read-only at their next check-in.",
      revoke: "Revoke this licence for good? Its servers are released and it cannot be used again.",
      cancel: "Cancel this licence?", reissue_key: "Make a new licence key? The old key stops working; servers already activated keep working." }[a];
    if (ask && !confirm(ask)) return;
    if (a === "renew") {
      const until = prompt("Renew until (YYYY-MM-DD). Leave empty for one more plan period.", "");
      if (until === null) return;
      if (until) body.expires_at = until;
    }
    try {
      const r = await api(`/admin/licenses/${b.dataset.lic}/action`, { method: "POST", body });
      await again();
      if (r.license_key) $("#cd-key").innerHTML = `<div class="lic-box"><p>${esc(r.note)}</p><div class="keyline">${esc(r.license_key)}</div></div>`;
    } catch (err) { msg(err); }
  }));
  document.querySelectorAll("#cd-body [data-inst]").forEach((b) => (b.onclick = async () => {
    const a = b.dataset.act;
    if (a === "block" && !confirm("Block this server? It becomes read-only at its next check-in and may not activate again.")) return;
    try { await api(`/admin/installations/${b.dataset.inst}/action`, { method: "POST", body: { action: a } }); await again(); } catch (err) { msg(err); }
  }));
  $("#cd-hosted").onclick = async () => {
    const ids = [...document.querySelectorAll("#cd-body [data-hosted]:checked")].map((x) => x.dataset.hosted);
    try { await api(`/admin/customers/${c.id}/companies`, { method: "PUT", body: { company_ids: ids } }); await again(); } catch (err) { msg(err); }
  };
}

let licEditing = null;
function openLicenceForm(l, c) {
  licEditing = { lic: l, customer: c };
  const f = $("#licf"), el = (n) => f.elements.namedItem(n);
  f.reset(); $("#licf-msg").textContent = "";
  $("#licf-title").textContent = l ? `Change licence ${l.key_hint}` : `New licence for ${c.legal_name}`;
  const mods = new Set(l ? l.modules.map((m) => m.code) : Object.keys(MODULE_LABELS));
  $("#licf-modules").innerHTML = Object.entries(MODULE_LABELS).map(([k, v]) =>
    `<label class="check"><input type="checkbox" data-mod="${k}" ${mods.has(k) ? "checked" : ""}> ${esc(v)}</label>`).join("");
  if (l) for (const k of ["plan", "starts_at", "expires_at", "max_users", "max_installations", "max_companies", "offline_grace_days", "heartbeat_hours", "min_version", "max_version", "notes"])
    el(k).value = l[k] ?? "";
  else el("starts_at").value = today();
  $("#licf-dlg").showModal();
}
$("#licf-cancel").onclick = () => $("#licf-dlg").close();
$("#licf").onsubmit = async (e) => {
  e.preventDefault();
  const f = e.target, el = (n) => f.elements.namedItem(n).value.trim();
  const num = (n) => (el(n) ? Number(el(n)) : null);
  const body = { plan: el("plan"), starts_at: el("starts_at") || null, max_users: num("max_users"), max_installations: num("max_installations"),
    max_companies: num("max_companies"), offline_grace_days: num("offline_grace_days"), heartbeat_hours: num("heartbeat_hours"),
    min_version: el("min_version") || null, max_version: el("max_version") || null, notes: el("notes") || null,
    modules: [...document.querySelectorAll("#licf-modules [data-mod]:checked")].map((x) => x.dataset.mod) };
  if (el("expires_at") || licEditing.lic) body.expires_at = el("expires_at") || null;
  const { lic, customer } = licEditing;
  try {
    const r = lic ? await api(`/admin/licenses/${lic.id}`, { method: "PATCH", body })
      : await api("/admin/licenses", { method: "POST", body: { ...body, customer_id: customer.id } });
    $("#licf-dlg").close();
    await openCustomer(customer.id);
    if (r.license_key) $("#cd-key").innerHTML = `<div class="lic-box"><p>${esc(r.note)}</p><div class="keyline">${esc(r.license_key)}</div></div>`;
  } catch (err) { $("#licf-msg").textContent = err.message; }
};

// ------------------------------------------------------------------ licence portal: releases
renderers.releases = async () => {
  const [rows, nv] = await Promise.all([api("/admin/releases"), api("/admin/releases/next-version")]);
  $("#rel-latest").textContent = nv.latest ? `Latest release: ${nv.latest}. The next one must be ${nv.next} or higher.`
    : "No release yet: the first one is usually 1.0.0.";
  const vin = $("#rel-form").elements.namedItem("version");
  if (!vin.value) vin.value = nv.next;
  table($("#rel-table"), [
    { label: "Version", key: "version" }, { label: "Platform", key: "platform" }, { label: "File", key: "file_name" },
    { label: "Size", num: true, get: (r) => size(r.file_size) }, { label: "SHA-256", get: (r) => r.sha256.slice(0, 12) + "…" },
    { label: "Signed", get: (r) => (r.signature ? "yes" : "no") }, { label: "Status", html: (r) => chip(r.status) },
    { label: "Published", get: (r) => when(r.published_at) }, { label: "Downloads", num: true, key: "downloads" },
    { label: "Notes", key: "release_notes", wrap: true },
    { label: "", html: (r) => r.status === "published" ? `<button type="button" class="secondary small" data-rel="${r.id}" data-st="withdrawn">Withdraw</button>`
      : `<button type="button" class="small" data-rel="${r.id}" data-st="published">Publish</button>` },
  ], rows);
  document.querySelectorAll("#rel-table [data-rel]").forEach((b) => (b.onclick = async () => {
    if (b.dataset.st === "published" && !confirm("Publish this release? Company admins whose licence covers it can download it.")) return;
    try { await api(`/admin/releases/${b.dataset.rel}/status`, { method: "POST", body: { status: b.dataset.st } }); await renderers.releases(); } catch (err) { alert(err.message); }
  }));
};
$("#rel-build").onclick = async () => {
  if (!confirm("Build the installer for the version this portal runs and publish it to customers?")) return;
  $("#rel-msg").textContent = "Building…";
  try { const r = await api("/admin/releases/build-current", { method: "POST" }); $("#rel-msg").textContent = `Version ${r.version} is published.`; await renderers.releases(); }
  catch (err) { $("#rel-msg").textContent = err.message; }
};
$("#rel-form").onsubmit = async (e) => {
  e.preventDefault();
  const btn = e.target.querySelector("button"); btn.disabled = true; $("#rel-msg").textContent = "Uploading…";
  try { await api("/admin/releases", { method: "POST", form: new FormData(e.target) }); e.target.reset(); $("#rel-msg").textContent = "Uploaded as draft. Publish it when it is tested."; await renderers.releases(); }
  catch (err) { $("#rel-msg").textContent = err.message; } finally { btn.disabled = false; }
};

// ------------------------------------------------------------------ licence portal: the customer's company admin
renderers.mylicence = async () => {
  const [p, dl] = await Promise.all([api("/portal/license"), api("/portal/downloads")]);
  $("#ml-title").textContent = `Licence of ${p.customer.legal_name}`;
  $("#ml-body").innerHTML = `
    ${p.warnings.map((w) => `<p class="error">${esc(w)}</p>`).join("")}
    ${p.licenses.map((l) => licenceBox(l, false)).join("") || `<p class="muted">There is no licence yet. Contact Voyager.</p>`}
    ${p.hosted_companies.length ? `<p class="small muted">Books hosted on Tally Cloud: ${p.hosted_companies.map((h) => esc(h.name)).join(", ")}
      ${p.hosted_users != null ? ` · ${p.hosted_users} user(s) working now` : ""}</p>` : ""}
    <h3>Downloads</h3>
    ${dl.length ? `<table><thead><tr><th>Version</th><th>File</th><th class="num">Size</th><th>Released</th><th>What is new</th><th></th></tr></thead><tbody>${dl.map((r) =>
      `<tr><td>${esc(r.version)}</td><td>${esc(r.file_name)}<div class="small muted">SHA-256 ${esc(r.sha256)}</div></td><td class="num">${size(r.file_size)}</td>
        <td>${fmtDate(r.published_at?.slice(0, 10))}</td><td class="wrap">${esc(r.release_notes || "")}</td>
        <td><button type="button" data-dl="${r.id}">Download</button></td></tr>`).join("")}</tbody></table>`
      : `<p class="muted">No installer is available for your licence yet. Voyager publishes one shortly; if it
        does not appear, click Request renewal above and ask for the installer.</p>`}
    <p class="small muted">Install on the Windows server or PC next to Tally, then enter your licence key on its Licence tab.</p>
    ${p.downloads.length ? `<h3>Recent downloads</h3><table class="small">${p.downloads.map((d) => `<tr><td>${when(d.at)}</td><td>${esc(d.user)}</td><td>v${esc(d.version || "")}</td><td>${chip(d.result)}</td></tr>`).join("")}</table>` : ""}`;
  document.querySelectorAll("#ml-body [data-dl]").forEach((b) => (b.onclick = async () => {
    try { const r = await api(`/downloads/${b.dataset.dl}/link`, { method: "POST" }); window.location.href = r.url; } catch (err) { alert(err.message); }
  }));
  $("#ml-renew").onclick = async () => {
    const message = prompt("Message for Voyager (optional): e.g. renew for one year, add 2 users");
    if (message === null) return;
    try { const r = await api("/licenses/renew", { method: "POST", body: { license_id: p.licenses[0]?.id || null, message } }); alert(r.message); }
    catch (err) { alert(err.message); }
  };
};

// ------------------------------------------------------------------ installation: this server's licence
renderers.licence = async () => {
  const l = await api("/licence");
  const stateLabel = { active: "Active", grace: "Working offline", read_only: "Read-only", not_activated: "Not activated" }[l.state] || l.state;
  $("#lc-cards").innerHTML = [
    ["Status", chip(l.state) + ` ${esc(stateLabel)}`, l.reason || (l.warnings || []).join(" ")],
    ["Licensed to", esc(l.customer || "—"), esc(l.plan ? `${l.plan} · ${l.expires_at ? "until " + fmtDate(l.expires_at) : "no end date"}` : "")],
    ["Users working now", l.max_users != null ? `${l.users_used} of ${l.max_users}` : "—", "users at the same time"],
    ["Tally companies", l.max_companies != null ? `${l.companies_used} of ${l.max_companies}` : String(l.companies_used ?? "—"), ""],
    ["Last check-in", esc(when(l.last_check_at) || "never"), esc(l.server_url || "")],
    ["This server", esc((l.installation_id || "").slice(0, 12)), `version ${esc(l.version)}${l.update_available ? ` · version ${esc(l.update_available.version)} is in the licence portal` : ""}`],
  ].map(([k, v, sub]) => `<div class="card"><div class="k">${esc(k)}</div><div class="v">${v}</div><div class="small muted">${sub}</div></div>`).join("");
  $("#lc-error").textContent = l.last_error ? `Last attempt: ${l.last_error}` : "";
  $("#lc-form").hidden = !!l.activated; $("#lc-actions").hidden = !l.activated;
  const u = l.update_available;
  $("#lc-update").hidden = !u;
  if (u) $("#lc-update").innerHTML = `<h4>Version ${esc(u.version)} is available</h4>
    <p class="small">${esc(u.release_notes || "")}</p>
    ${u.verified ? `<p class="small okmsg">Downloaded and checked: signed by Voyager, fingerprint matches.</p>
      ${l.can_install_updates ? `<button type="button" id="lc-install">Install update and restart</button>`
        : `<p class="small muted">This server was not set up with the Windows installer; install the package by hand.</p>`}`
      : `<button type="button" id="lc-download">Download and check</button>`}`;
  const dl = $("#lc-download"), inst = $("#lc-install");
  if (dl) dl.onclick = () => getUpdate();
  if (inst) inst.onclick = async () => {
    if (!confirm(`Install version ${u.version}? Tally Cloud restarts: everyone is disconnected for a minute or two. `
      + "If the new version does not start, the previous one comes back by itself.")) return;
    try { const r = await api("/licence/update/install", { method: "POST" }); alert(r.message); waitForRestart(u.version); }
    catch (err) { $("#lc-error").textContent = err.message; }
  };
  $("#lc-form").elements.namedItem("server_url").value = l.server_url || "";
};
$("#lc-form").onsubmit = async (e) => {
  e.preventDefault();
  const el = (n) => e.target.elements.namedItem(n).value.trim();
  const btn = e.target.querySelector("button"); btn.disabled = true;
  try { await api("/licence/activate", { method: "POST", body: { license_key: el("license_key"), server_label: el("server_label") || null, server_url: el("server_url") || null } });
    await renderers.licence(); refreshBanner(); alert("Activated."); }
  catch (err) { $("#lc-error").textContent = err.message; } finally { btn.disabled = false; }
};
async function getUpdate() {
  $("#lc-error").textContent = "Looking for updates…";
  try { const r = await api("/licence/update/download", { method: "POST" }); $("#lc-error").textContent = r.message; await renderers.licence(); }
  catch (err) { $("#lc-error").textContent = err.message; }
}
$("#lc-get-update").onclick = () => getUpdate();
function waitForRestart(version, out = $("#lc-error")) {
  out.textContent = `Installing ${version}… this page reloads when Tally Cloud is back.`;
  const started = Date.now();
  const poll = async () => {
    try {
      const l = await api("/licence");
      if (l.version === version) { location.reload(); return; }
    } catch (_) { /* restarting */ }
    if (Date.now() - started < 10 * 60000) setTimeout(poll, 5000);
    else out.textContent = "The update is taking long. Reload this page; the Licence tab shows the version that runs.";
  };
  setTimeout(poll, 10000);
}
$("#lc-check").onclick = async () => { try { await api("/licence/check", { method: "POST" }); await renderers.licence(); refreshBanner(); } catch (err) { $("#lc-error").textContent = err.message; } };
$("#lc-deactivate").onclick = async () => {
  if (!confirm("Deactivate this server? Its licence slot is given back so you can activate another server. This server becomes read-only; no data is deleted.")) return;
  try { await api("/licence/deactivate", { method: "POST" }); await renderers.licence(); refreshBanner(); } catch (err) { $("#lc-error").textContent = err.message; }
};

// ------------------------------------------------------------------ first sign-in of an invited company admin
async function setupFromLink(token) {
  $("#login").hidden = true; $("#setup").hidden = false;
  try { const who = await (await fetch(`${API}/auth/setup/${encodeURIComponent(token)}`)).json();
    if (!who.username) throw new Error(who.detail || "This link is not valid");
    $("#setup-who").textContent = who.display_name ? `Welcome, ${who.display_name}.` : "Welcome.";
    $("#setup-user").value = who.username;
  } catch (err) { $("#setup-msg").textContent = err.message; $("#setup-form").hidden = true; return; }
  $("#setup-form").onsubmit = async (e) => {
    e.preventDefault(); $("#setup-msg").textContent = "";
    if ($("#setup-pw1").value !== $("#setup-pw2").value) { $("#setup-msg").textContent = "The two passwords differ"; return; }
    const res = await fetch(`${API}/auth/setup`, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token, password: $("#setup-pw1").value, username: $("#setup-user").value.trim() }) });
    const d = await res.json().catch(() => ({}));
    if (!res.ok) { $("#setup-msg").textContent = d.detail || res.statusText; return; }
    history.replaceState(null, "", location.pathname);
    $("#setup").hidden = true; $("#login").hidden = false;
    $("#login-user").value = d.username; $("#login-pass").value = $("#setup-pw1").value;
    $("#login-form").requestSubmit();
  };
}

// ------------------------------------------------------------------ boot
document.querySelector("main").hidden = true; $("header nav").hidden = true;
$("#recon-from").value = `${new Date().getFullYear() - (new Date().getMonth() < 3 ? 1 : 0)}-04-01`; $("#recon-to").value = today();
state.token = load("erp_token");
state.key = state.token ? null : load("erp_key");
async function firstRunCheck() {
  try {
    const r = await (await fetch(`${API}/licence/first-run`)).json();
    if (!r.needed) return;
    $("#login").hidden = true; $("#firstrun").hidden = false;
    $("#fr-form").elements.namedItem("server_url").value = r.server_url || "";
  } catch (_) { /* not a new installation */ }
}
$("#fr-form").onsubmit = async (e) => {
  e.preventDefault();
  const f = e.target, v = (n) => f.elements.namedItem(n).value.trim();
  $("#fr-msg").textContent = "";
  if (f.elements.namedItem("password").value !== f.elements.namedItem("password2").value) { $("#fr-msg").textContent = "The two passwords differ"; return; }
  const btn = f.querySelector("button"); btn.disabled = true; btn.textContent = "Activating…";
  try {
    const res = await fetch(`${API}/licence/first-run`, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ license_key: v("license_key"), server_label: v("server_label") || null, server_url: v("server_url") || null,
        username: v("username"), display_name: v("display_name") || null, password: f.elements.namedItem("password").value }) });
    const d = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(d.detail || res.statusText);
    $("#firstrun").hidden = true; $("#login").hidden = false;
    $("#login-user").value = d.username; $("#login-pass").value = f.elements.namedItem("password").value;
    $("#login-form").requestSubmit();
  } catch (err) { $("#fr-msg").textContent = err.message; } finally { btn.disabled = false; btn.textContent = "Activate and create manager"; }
};

if (location.hash.startsWith("#setup=")) { signOut(); setupFromLink(location.hash.slice(7)); }
else if (state.token || state.key) start().catch(() => { signOut(); firstRunCheck(); });
else { signOut(); firstRunCheck(); }
