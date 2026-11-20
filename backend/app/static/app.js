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
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (_) {   // e.g. "Internal Server Error" as plain text
    if (!res.ok) throw new Error(`The server could not do this (${res.status} ${text.slice(0, 120) || res.statusText}). See the server log.`);
    throw new Error("The server's answer could not be read.");
  }
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
function isAdmin() {   // company admin or accounts admin in the selected company, or the system admin
  if (state.user?.is_superadmin) return true;
  const a = (state.user?.companies || []).find((c) => c.company_id === state.company);
  return !!a && ["owner", "accounts_admin"].includes(a.role);
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
  $("#r-csv").hidden = $("#r-xlsx").hidden = $("#errors-csv").hidden = !can("export");
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
  $("#td-msg").innerHTML = esc(r.companies.length ? `${r.companies.length} compan${r.companies.length === 1 ? "y" : "ies"} open in Tally.` : "No company is open in Tally. Open one in Tally and list again.")
    + (r.warning ? `<br><span class="error">${esc(r.warning)}</span>` : "");
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
  for (const k of Object.keys(listCache)) delete listCache[k];
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
    ["Last export", when(c.last_outbound_at) || "never", c.freeze_outbound ? "outbound frozen" : (c.outbound_enabled ? "bidirectional" : "one way: Tally → software")],
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
function inventoryTable(lines, groups = {}) {
  const dir = (i) => (Number(i.quantity) > 0 || (Number(i.quantity) === 0 && i.direction === "in") ? "In" : "Out");
  const mixed = new Set(lines.map(dir)).size > 1;
  const qtyNum = (n) => n.toLocaleString("en-IN", { maximumFractionDigits: 4 });
  const qtyText = (i) => `${qtyNum(Math.abs(Number(i.quantity)))} ${i.unit || ""}`.trim();
  const part = (title, rows) => {
    const units = new Set(rows.map((i) => i.unit || ""));
    const qty = rows.reduce((a, i) => a + Math.abs(Number(i.quantity)), 0);
    const amount = rows.reduce((a, i) => a + paise(i.amount), 0);
    return `<h3>${esc(title)} <span class="muted small">${rows.length} item${rows.length === 1 ? "" : "s"}</span></h3>
      <table><thead><tr><th>Item</th><th>Item group</th><th class="num">Qty</th><th class="num">Rate</th><th class="num">Amount</th>
      <th>Godown</th><th>Batch</th></tr></thead><tbody>${rows.map((i) =>
      `<tr><td>${esc(i.item)}</td><td>${esc(groups[i.item] || "")}</td>
        <td class="num">${Number(i.quantity) === 0 ? "value only" : esc(qtyText(i))}</td><td class="num">${money(i.rate)}</td>
        <td class="num">${money(i.amount)}</td><td>${esc(i.godown || "")}</td><td>${esc(i.batch || "")}</td></tr>`).join("")}
      <tr class="total"><td><b>Total</b></td><td></td><td class="num"><b>${units.size === 1 ? esc(`${qtyNum(qty)} ${[...units][0]}`.trim()) : ""}</b></td>
        <td></td><td class="num"><b>${rupees(amount)}</b></td><td></td><td></td></tr></tbody></table>`;
  };
  // stock and manufacturing journals move stock both ways: what came in and what went out, as separate tables
  if (mixed) return part("In (produced / received)", lines.filter((i) => dir(i) === "In"))
    + part("Out (consumed / issued)", lines.filter((i) => dir(i) === "Out"));
  return part("Inventory", lines);
}

async function openVoucher(id) {
  if (currentTab !== "vouchers") show("vouchers");
  const v = await api(`/vouchers/${id}`);
  const d = v.doc;
  const acts = { draft: ["post", "submit", "cancel", "delete"], pending_approval: ["approve", "reject", "cancel"], approved: ["post", "cancel"],
    posted: ["cancel", "delete"], rejected: ["submit", "cancel", "delete"], cancelled: ["delete"] }[v.status]
    ?.filter((a) => a !== "delete" || (isAdmin() && v.source_system === "erp")) || [];   // delete: admin, software vouchers only
  const el = $("#vdetail"); el.hidden = false;
  el.innerHTML = `<div class="row between"><h3>${esc(d.voucher_type)} ${esc(d.voucher_number)} · ${esc(d.date)}</h3>
    <div>${chip(v.status)} ${chip(v.sync_status)} <span class="muted small">v${v.version} · by ${esc(v.updated_by)} · origin ${esc(v.source_system)}</span></div></div>
    <p>${esc(d.narration)}</p>
    <table><thead><tr><th>Ledger</th><th class="num">Debit</th><th class="num">Credit</th><th>Bills</th><th>Bank</th></tr></thead><tbody>
    ${d.entries.map((e) => `<tr><td>${esc(e.ledger)}</td><td class="num">${+e.amount > 0 ? money(e.amount) : ""}</td><td class="num">${+e.amount < 0 ? money(-e.amount) : ""}</td>
      <td>${e.bills.map((b) => esc(`${b.type} ${b.name || ""}`)).join(", ")}</td><td>${e.bank.map((b) => esc(`${b.transaction_type || ""} ${b.instrument_no || ""}`)).join(", ")}</td></tr>`).join("")}
    </tbody></table>
    ${d.inventory.length ? inventoryTable(d.inventory, v.item_groups || {}) : ""}
    <p class="small muted">${v.tally ? `Tally: MasterID ${esc(v.tally.master_id)} · AlterID ${esc(v.tally.alter_id)} · GUID ${esc(v.tally.guid)} · last sync ${when(v.tally.last_synced_at)}
      ${v.tally.last_error ? `<br><span class="error">${esc(v.tally.error_code)}: ${esc(v.tally.last_error)}</span>` : ""}` : "Not yet in Tally"}</p>
    <div class="row">${!["cancelled", "deleted"].includes(v.status) ? `<button data-edit>Edit</button>` : ""}
      ${acts.map((a) => `<button class="secondary" data-act="${a}">${a[0].toUpperCase() + a.slice(1)}</button>`).join("")}
      <button class="secondary" data-dup>Duplicate</button>${can("export") ? `<button class="secondary" data-xlsx>Excel</button>` : ""}
      <button class="link" data-hist>History</button></div><div id="vhist" hidden></div>`;
  el.querySelectorAll("[data-act]").forEach((b) => (b.onclick = async () => {
    const reason = ["cancel", "delete", "reject"].includes(b.dataset.act)
      ? prompt(b.dataset.act === "delete" ? "Remarks: why is this voucher deleted? (it is deleted in Tally too)" : `Reason to ${b.dataset.act}?`) : null;
    if (reason === null && ["cancel", "delete", "reject"].includes(b.dataset.act)) return;
    if (b.dataset.act === "delete" && reason.trim().length < 3) return alert("Enter remarks to delete the voucher");
    try {
      if (b.dataset.act === "delete") await api(`/vouchers/${id}?reason=${encodeURIComponent(reason)}`, { method: "DELETE" });
      else await api(`/vouchers/${id}/${b.dataset.act}`, { method: "POST", body: { reason, expected_version: v.version } });
      await renderers.vouchers(); openVoucher(id);
    } catch (err) { alert(err.message); }
  }));
  el.querySelector("[data-edit]") && (el.querySelector("[data-edit]").onclick = () => editVoucher(v));
  el.querySelector("[data-xlsx]") && (el.querySelector("[data-xlsx]").onclick = () =>
    download(`/vouchers/${id}/xlsx`, `${d.voucher_type} ${d.voucher_number}.xlsx`.replace(/[\\/:*?"<>|]/g, "-")));
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
// The entry screen changes with the voucher type, like Tally: ledger Dr/Cr for accounting vouchers, party +
// items + additional ledgers for invoices, source / destination for a stock journal, and product + BOM
// components for a manufacturing journal.
const INVOICE_BASES = ["Sales", "Purchase", "Credit Note", "Debit Note", "Sales Order", "Purchase Order", "Delivery Note",
  "Receipt Note", "Rejections In", "Rejections Out", "Material In", "Material Out", "Job Work In Order", "Job Work Out Order"];
const INWARD_BASES = ["Purchase", "Credit Note", "Purchase Order", "Receipt Note", "Rejections In", "Material In", "Job Work In Order"];
const PARTY_DR_BASES = ["Sales", "Debit Note"];   // party debited; Purchase and Credit Note credit it
const ACC_INVOICE_BASES = ["Sales", "Purchase", "Credit Note", "Debit Note"];   // may also be entered as accounting invoices
function vtOf(name) { return state.vtypes.find((v) => v.name === name) || { name, base_type: name, affects_accounts: true, affects_stock: true }; }
function layoutOf(vt) {
  const b = vt.base_type || "";
  if (["Stock Journal", "Manufacturing Journal"].includes(b) && (/manufactur/i.test(b) || /manufactur/i.test(vt.name))) return "mfg";
  if (["Stock Journal", "Physical Stock"].includes(b)) return "sj";
  if (INVOICE_BASES.includes(b)) return ACC_INVOICE_BASES.includes(b) && $("#ve-acc-mode").checked ? "acc" : "inv";
  return "acc";
}
let entryLayout = "acc";
let entryItems = null;   // { company, byName } for rates, units and BOMs
async function loadEntryItems() {
  if (entryItems && entryItems.company === state.company) return entryItems;
  const [items, godowns] = await Promise.all([api(`/companies/${state.company}/masters/stock-items`),
    api(`/companies/${state.company}/masters/godowns`).catch(() => [])]);
  entryItems = { company: state.company, byName: Object.fromEntries(items.map((i) => [i.name, i])) };
  $("#item-names").innerHTML = items.map((i) => `<option value="${esc(i.name)}">`).join("");
  $("#godown-names").innerHTML = godowns.map((g) => `<option value="${esc(g.name)}">`).join("");
  return entryItems;
}
const itemInfo = (name) => (entryItems && entryItems.byName[name]) || null;
const num2 = (v) => Math.round((+v || 0) * 100) / 100;

// Rates of stock lines in stock / manufacturing journals and job-work or rejection vouchers default to the
// item's FIFO closing rate on the voucher date (the cost of the stock on hand). A rate typed over is kept.
const FIFO_INV_BASES = ["Material In", "Material Out", "Rejections In", "Rejections Out"];
function usesFifo() {
  return entryLayout === "sj" || entryLayout === "mfg"
    || (entryLayout === "inv" && FIFO_INV_BASES.includes(vtOf($("#ve-type").value).base_type));
}
const fifoCache = {};
async function fifoRates(names) {
  const day = $("#ve-date").value || today(), ex = $("#ve-id").value;
  const want = [...new Set(names.filter(Boolean))].filter((n) => !(`${state.company}|${day}|${ex}|${n}` in fifoCache));
  if (want.length) {
    const p = new URLSearchParams({ as_of: day });
    want.forEach((n) => p.append("item", n));
    if (ex) p.set("exclude_voucher", ex);
    const r = await api(`/companies/${state.company}/masters/fifo-rates?${p}`);
    want.forEach((n) => (fifoCache[`${state.company}|${day}|${ex}|${n}`] = r[n] || null));
  }
  return Object.fromEntries(names.map((n) => [n, fifoCache[`${state.company}|${day}|${ex}|${n}`]]));
}
function setFifoRate(tr, info) {
  if (tr.dataset.rateEdited) return;
  const input = tr.querySelector(".i-rate");
  input.value = info && info.rate != null ? (+info.rate).toFixed(2) : "";
  input.title = info ? `${info.basis} on ${$("#ve-date").value}${info.qty ? ` · on hand ${info.qty}` : ""} · type over to change` : "";
  input.classList.toggle("auto-rate", !!(info && info.rate != null));
  tr._recalc && tr._recalc();
}
async function refreshFifoRates() {
  if (!usesFifo()) return;
  const rows = [...document.querySelectorAll("#ve-items tr, #ve-src tr, #ve-dst tr, #vm-comp tr, #vm-by tr")]
    .filter((tr) => !tr.dataset.rateEdited && tr.querySelector(".i-item").value.trim());
  if (!rows.length) return;
  const r = await fifoRates(rows.map((tr) => tr.querySelector(".i-item").value.trim()));
  rows.forEach((tr) => setFifoRate(tr, r[tr.querySelector(".i-item").value.trim()]));
}
$("#ve-date").addEventListener("change", () => refreshFifoRates().catch((e) => ($("#ve-msg").textContent = e.message)));

// One item line (Name of item, Godown, Batch, Quantity, Rate, per, Amount). Amount follows quantity x rate
// until it is typed over.
function addItemLine(tbodyId, l = {}) {
  const tr = document.createElement("tr");
  const qty = l.quantity == null ? "" : Math.abs(+l.quantity);
  tr.innerHTML = `<td><input class="i-item" list="item-names" value="${esc(l.item || "")}"></td>
    <td><input class="i-godown" list="godown-names" value="${esc(l.godown || "")}"></td>
    <td><input class="i-batch" value="${esc(l.batch || "")}"></td>
    <td><input class="i-qty num" inputmode="decimal" value="${qty === 0 ? "" : qty}"></td>
    <td><input class="i-rate num" inputmode="decimal" value="${l.rate && +l.rate ? +l.rate : ""}"></td>
    <td class="i-unit small">${esc(l.unit || "")}</td>
    <td><input class="i-amount num" inputmode="decimal" value="${l.amount && +l.amount ? Math.abs(+l.amount) : ""}"></td>
    <td><button type="button" class="link" tabindex="-1" title="Remove line">✕</button></td>`;
  tr.dataset.unit = l.unit || "";
  if (l.rate && +l.rate) tr.dataset.rateEdited = "1";   // a rate already on the voucher stays
  tr.dataset.manual = l.amount && +l.amount && Math.abs(num2((+l.quantity || 0) * (+l.rate || 0))) !== Math.abs(num2(l.amount)) ? "1" : "";
  const $q = (c) => tr.querySelector(c);
  const recalc = () => { if (!tr.dataset.manual) { const a = num2(Math.abs(+$q(".i-qty").value || 0) * (+$q(".i-rate").value || 0)); $q(".i-amount").value = a ? a.toFixed(2) : ""; } entryTotals(); };
  tr._recalc = recalc;
  $q(".i-item").addEventListener("change", () => {
    const it = itemInfo($q(".i-item").value);
    if (!it) return;
    tr.dataset.unit = it.unit || ""; $q(".i-unit").textContent = it.unit || "";
    if (usesFifo()) { delete tr.dataset.rateEdited; fifoRates([it.name]).then((r) => setFifoRate(tr, r[it.name])).catch(() => {}); }
    recalc();
  });
  $q(".i-qty").addEventListener("input", recalc);
  $q(".i-rate").addEventListener("input", () => { tr.dataset.rateEdited = $q(".i-rate").value ? "1" : ""; $q(".i-rate").classList.remove("auto-rate"); recalc(); });
  $q(".i-amount").addEventListener("input", () => { tr.dataset.manual = $q(".i-amount").value ? "1" : ""; entryTotals(); });
  $q("button").onclick = () => { tr.remove(); entryTotals(); };
  $(`#${tbodyId}`).appendChild(tr);
  return tr;
}
function itemLines(tbodyId) {
  return [...$(`#${tbodyId}`).querySelectorAll("tr")].map((tr) => ({
    item: tr.querySelector(".i-item").value.trim(), godown: tr.querySelector(".i-godown").value.trim() || null,
    batch: tr.querySelector(".i-batch").value.trim() || null, qty: Math.abs(+tr.querySelector(".i-qty").value || 0),
    rate: +tr.querySelector(".i-rate").value || 0, amount: num2(tr.querySelector(".i-amount").value), unit: tr.dataset.unit || null,
  })).filter((l) => l.item);
}
const linesTotal = (id) => itemLines(id).reduce((s, l) => s + l.amount, 0);

function addExtraLine(l = {}) {
  const tr = document.createElement("tr");
  tr.innerHTML = `<td><input class="x-ledger" list="ledger-names" value="${esc(l.ledger || "")}"></td>
    <td><input class="x-rate num" inputmode="decimal" placeholder="%" value="${l.rate || ""}"></td>
    <td><input class="x-amount num" inputmode="decimal" value="${l.amount == null ? "" : l.amount}"></td>
    <td><button type="button" class="link" tabindex="-1" title="Remove line">✕</button></td>`;
  tr.querySelector(".x-rate").addEventListener("input", entryTotals);
  tr.querySelector(".x-amount").addEventListener("input", () => { tr.querySelector(".x-rate").value = ""; entryTotals(); });
  tr.querySelector("button").onclick = () => { tr.remove(); entryTotals(); };
  $("#ve-extra").appendChild(tr);
  return tr;
}
function extraLines() {   // additional ledgers; a rate % works out the amount on the items total
  const items = linesTotal("ve-items");
  return [...$("#ve-extra").querySelectorAll("tr")].map((tr) => {
    const rate = tr.querySelector(".x-rate").value;
    if (rate !== "") tr.querySelector(".x-amount").value = num2(items * (+rate) / 100).toFixed(2);
    return { ledger: tr.querySelector(".x-ledger").value.trim(), amount: num2(tr.querySelector(".x-amount").value) };
  }).filter((l) => l.ledger && l.amount);
}

// Manufacturing journal: the product's BOM fills the components (and co/by-products, scrap) for the quantity.
function mfgBoms() { const it = itemInfo($("#vm-item").value.trim()); return (it && it.bom) || []; }
function mfgFillBom() {
  const boms = mfgBoms();
  $("#vm-bom").innerHTML = boms.length ? boms.map((b, i) => `<option value="${i}">${esc(b.name || `BOM ${i + 1}`)} (for ${esc(qtyText(b.basic_qty, b.unit))})</option>`).join("")
    : `<option value="">No BOM: enter the components</option>`;
  mfgApplyBom();
}
function mfgApplyBom() {
  const b = mfgBoms()[+$("#vm-bom").value];
  if (!b) { entryTotals(); return; }
  const per = (+$("#vm-qty").value || 0) / (+b.basic_qty || 1);
  $("#vm-comp").innerHTML = ""; $("#vm-by").innerHTML = "";
  for (const c of b.components) {
    const it = itemInfo(c.item) || {};
    const qty = Math.round(+c.qty * per * 10000) / 10000;
    addItemLine(c.nature === "Component" ? "vm-comp" : "vm-by", { item: c.item, godown: c.godown, quantity: qty, unit: c.unit || it.unit });
  }
  if (!$("#vm-comp").children.length) addItemLine("vm-comp");
  entryTotals();
  refreshFifoRates().catch((e) => ($("#ve-msg").textContent = e.message));   // components at FIFO cost
}
$("#vm-item").addEventListener("change", () => {
  const it = itemInfo($("#vm-item").value.trim());
  if (it && !$("#vm-qty").value) { const b = (it.bom || [])[0]; $("#vm-qty").value = b ? +b.basic_qty : 1; }
  mfgFillBom();
});
$("#vm-bom").onchange = mfgApplyBom;
$("#vm-qty").addEventListener("change", mfgApplyBom);

function entryTotals() {
  if (entryLayout === "acc") return totals();
  if (entryLayout === "inv") {
    const items = linesTotal("ve-items"), extra = extraLines().reduce((s, l) => s + l.amount, 0);
    $("#ve-items-total").textContent = money(items); $("#ve-inv-total").textContent = money(items + extra);
  } else if (entryLayout === "sj") {
    $("#ve-src-total").textContent = money(linesTotal("ve-src")); $("#ve-dst-total").textContent = money(linesTotal("ve-dst"));
  } else {
    const comp = linesTotal("vm-comp"), by = linesTotal("vm-by"), qty = +$("#vm-qty").value || 0;
    $("#vm-comp-total").textContent = money(comp); $("#vm-by-total").textContent = money(by);
    const cost = comp - by;
    $("#vm-cost").innerHTML = `Cost of production <b>${money(cost)}</b>` + (qty ? ` · rate <b>${money(cost / qty)}</b> per ${esc(itemInfo($("#vm-item").value.trim())?.unit || "unit")}` : "")
      + ` <span class="muted">(components less co-products / by-products / scrap)</span>`;
  }
  return 0;
}

// Show the layout of the chosen voucher type.
function applyLayout(keep = false) {
  const vt = vtOf($("#ve-type").value);
  const b = vt.base_type;
  $("#ve-mode-toggle").hidden = !ACC_INVOICE_BASES.includes(b);
  entryLayout = layoutOf(vt);
  for (const [id, lay] of [["#ve-acc", "acc"], ["#ve-inv", "inv"], ["#ve-sj", "sj"], ["#ve-mfg", "mfg"]]) $(id).hidden = entryLayout !== lay;
  const books = vt.affects_accounts !== false;
  $("#ve-party-label").hidden = entryLayout === "sj" || entryLayout === "mfg";
  $("#ve-party-text").textContent = entryLayout === "inv" ? "Party A/c name" : "Party";
  $("#ve-ledger-label").hidden = !(entryLayout === "inv" && books);
  $("#ve-ledger-text").textContent = INWARD_BASES.includes(b) && b !== "Credit Note" || b === "Debit Note" ? "Purchase ledger" : "Sales ledger";
  $("#ve-extra-box").hidden = !books;
  $("#ve-mode-note").textContent = {
    acc: "", sj: "Stock journal: what is consumed on the left, what is produced on the right. Rates: FIFO closing rate on the voucher date (type over to change).",
    mfg: "Manufacturing journal: pick the product and quantity; its BOM fills the components at their FIFO closing rate.",
    inv: books ? (PARTY_DR_BASES.includes(b) ? "Items go out; the party is debited with the invoice total." : "Items come in; the party is credited with the invoice total.")
      : `${b}: items and quantities only (no accounting entries).`,
  }[entryLayout];
  if (!keep) {
    if (entryLayout === "inv" && !$("#ve-items").children.length) { addItemLine("ve-items"); }
    if (entryLayout === "sj") { if (!$("#ve-src").children.length) addItemLine("ve-src"); if (!$("#ve-dst").children.length) addItemLine("ve-dst"); }
    if (entryLayout === "mfg" && !$("#vm-comp").children.length) mfgFillBom();
    if (entryLayout === "acc" && !$("#ve-lines").children.length) { addLine(); addLine(); }
  }
  if (entryLayout !== "acc") loadEntryItems().catch(() => {});
  entryTotals();
}
$("#ve-type").addEventListener("change", () => applyLayout());
$("#ve-acc-mode").addEventListener("change", () => applyLayout());
document.querySelectorAll("[data-add-item]").forEach((b) => (b.onclick = () => addItemLine(b.dataset.addItem).querySelector("input").focus()));
$("#ve-add-extra").onclick = () => addExtraLine().querySelector("input").focus();

function clearEntryTables() {
  for (const id of ["#ve-lines", "#ve-items", "#ve-extra", "#ve-src", "#ve-dst", "#vm-comp", "#vm-by"]) $(id).innerHTML = "";
  for (const id of ["#vm-item", "#vm-godown", "#vm-batch", "#vm-qty", "#ve-itemledger"]) $(id).value = "";
  $("#vm-bom").innerHTML = "";
}
function resetEntry(type) {
  const acc = $("#ve-acc-mode").checked;
  $("#vform").reset(); clearEntryTables(); $("#ve-id").value = ""; $("#ve-version").value = "";
  $("#ve-acc-mode").checked = acc;
  $("#entry-title").textContent = "New voucher"; $("#ve-date").value = today(); $("#ve-msg").textContent = "";
  if (type && state.vtypes.some((v) => v.name === type)) $("#ve-type").value = type;
  applyLayout();
}
renderers.entry = async () => { if (!$("#ve-lines").children.length && !$("#ve-items").children.length && !$("#ve-src").children.length && !$("#vm-comp").children.length) resetEntry(); $("#ve-type").focus(); };
$("#ve-add").onclick = () => addLine().querySelector("input").focus();

async function editVoucher(v) {
  show("entry"); resetEntry();
  const d = v.doc;
  $("#entry-title").textContent = `Edit ${d.voucher_type} ${d.voucher_number}`;
  $("#ve-id").value = v.id; $("#ve-version").value = v.version;
  $("#ve-type").value = d.voucher_type; $("#ve-number").value = d.voucher_number; $("#ve-date").value = d.date;
  $("#ve-party").value = d.party_ledger || ""; $("#ve-ref").value = d.reference_no || ""; $("#ve-narration").value = d.narration || "";
  const vt = vtOf(d.voucher_type);
  // An invoice saved with ledgers only (no items) opens as an accounting invoice.
  $("#ve-acc-mode").checked = ACC_INVOICE_BASES.includes(vt.base_type) && !d.inventory.length;
  clearEntryTables();
  applyLayout(true);
  await loadEntryItems().catch(() => {});
  const inward = (i) => (+i.quantity ? +i.quantity > 0 : i.direction === "in");
  if (entryLayout === "acc") d.entries.forEach(addLine);
  else if (entryLayout === "inv") {
    const s = PARTY_DR_BASES.includes(vt.base_type) ? 1 : -1;
    const itemLedger = (d.inventory.find((i) => i.ledger) || {}).ledger || "";
    $("#ve-itemledger").value = itemLedger;
    d.inventory.forEach((i) => addItemLine("ve-items", i));
    d.entries.filter((e) => e.ledger !== d.party_ledger && e.ledger !== itemLedger)
      .forEach((e) => addExtraLine({ ledger: e.ledger, amount: num2(-s * +e.amount) }));
  } else if (entryLayout === "sj") {
    d.inventory.forEach((i) => addItemLine(inward(i) ? "ve-dst" : "ve-src", i));
  } else {
    const ins = d.inventory.filter(inward), outs = d.inventory.filter((i) => !inward(i));
    const p = ins[0] || {};
    $("#vm-item").value = p.item || ""; $("#vm-godown").value = p.godown || ""; $("#vm-batch").value = p.batch || "";
    $("#vm-qty").value = p.quantity ? Math.abs(+p.quantity) : "";
    const boms = mfgBoms();
    $("#vm-bom").innerHTML = boms.map((b, i) => `<option value="${i}">${esc(b.name || `BOM ${i + 1}`)}</option>`).join("") || `<option value="">No BOM</option>`;
    outs.forEach((i) => addItemLine("vm-comp", i));
    ins.slice(1).forEach((i) => addItemLine("vm-by", i));
  }
  entryTotals();
  $("#ve-action").value = v.status === "posted" ? "post" : "draft";
  editing = d;
}
let editing = null;

// What the voucher sends, by layout. Quantities: inward positive, outward negative.
function entryBody(prev) {
  const vt = vtOf($("#ve-type").value), b = vt.base_type, books = vt.affects_accounts !== false;
  const head = {
    voucher_type: $("#ve-type").value, voucher_number: $("#ve-number").value || null, date: $("#ve-date").value,
    party_ledger: $("#ve-party").value || null, reference_no: $("#ve-ref").value || null, narration: $("#ve-narration").value,
  };
  const inv = (l, inward, ledger = null) => ({ item: l.item, quantity: (inward ? l.qty : -l.qty).toString(), unit: l.unit, rate: l.rate.toString(),
    amount: l.amount.toFixed(2), godown: l.godown, batch: l.batch, ledger, ...(l.qty ? {} : { direction: inward ? "in" : "out" }) });
  if (entryLayout === "acc") {
    if (totals() !== 0) throw new Error("Debits and credits must match");
    const entries = [...$("#ve-lines").querySelectorAll("tr")].map((tr, i) => {
      const dr = +tr.querySelector(".l-dr").value || 0, cr = +tr.querySelector(".l-cr").value || 0;
      const amount = (dr - cr).toFixed(2), billText = tr.querySelector(".l-bill").value.trim();
      const old = prev && prev.entries[i] && prev.entries[i].ledger === tr.querySelector(".l-ledger").value ? prev.entries[i] : {};
      const bills = billText ? [{ type: billText.startsWith("Agst ") ? "Agst Ref" : "New Ref", name: billText.replace(/^Agst /, ""), amount }]
        : (old.bills && old.bills.length === 1 ? [{ ...old.bills[0], amount }] : []);
      return { ledger: tr.querySelector(".l-ledger").value, amount, bills, cost: old.cost || [], bank: old.bank || [] };
    }).filter((l) => +l.amount !== 0);
    return { ...head, entries, inventory: prev ? prev.inventory : [] };
  }
  if (entryLayout === "inv") {
    const lines = itemLines("ve-items");
    if (!lines.length) throw new Error("Enter at least one item");
    if (!head.party_ledger) throw new Error("Enter the Party A/c name");
    const inward = INWARD_BASES.includes(b);
    if (!books) return { ...head, entries: [], inventory: lines.map((l) => inv(l, inward)) };
    const itemLedger = $("#ve-itemledger").value.trim();
    if (!itemLedger) throw new Error(`Enter the ${$("#ve-ledger-text").textContent.toLowerCase()}`);
    const s = PARTY_DR_BASES.includes(b) ? 1 : -1;
    const items = num2(lines.reduce((t, l) => t + l.amount, 0)), extras = extraLines();
    const total = num2(items + extras.reduce((t, l) => t + l.amount, 0));
    const entries = [{ ledger: head.party_ledger, amount: (s * total).toFixed(2) },
      { ledger: itemLedger, amount: (-s * items).toFixed(2) },
      ...extras.map((x) => ({ ledger: x.ledger, amount: (-s * x.amount).toFixed(2) }))];
    return { ...head, entries, inventory: lines.map((l) => inv(l, inward, itemLedger)) };
  }
  if (entryLayout === "sj") {
    const src = itemLines("ve-src"), dst = itemLines("ve-dst");
    if (!src.length && !dst.length) throw new Error("Enter the items consumed and / or produced");
    return { ...head, party_ledger: null, entries: [], inventory: [...src.map((l) => inv(l, false)), ...dst.map((l) => inv(l, true))] };
  }
  const product = $("#vm-item").value.trim(), qty = Math.abs(+$("#vm-qty").value || 0);
  if (!product || !qty) throw new Error("Enter the name of the product and its quantity");
  const comp = itemLines("vm-comp"), by = itemLines("vm-by");
  if (!comp.length) throw new Error("Enter the components consumed");
  const cost = num2(comp.reduce((t, l) => t + l.amount, 0) - by.reduce((t, l) => t + l.amount, 0));
  const made = { item: product, qty, rate: num2(cost / qty), amount: cost, unit: itemInfo(product)?.unit || null,
                 godown: $("#vm-godown").value.trim() || null, batch: $("#vm-batch").value.trim() || null };
  return { ...head, party_ledger: null, entries: [], inventory: [inv(made, true), ...comp.map((l) => inv(l, false)), ...by.map((l) => inv(l, true))] };
}

$("#vform").onsubmit = async (e) => {
  e.preventDefault();
  const prev = editing && $("#ve-id").value ? editing : null;
  let body;
  try { body = entryBody(prev); } catch (err) { $("#ve-msg").innerHTML = `<span class="error">${esc(err.message)}</span>`; return; }
  try {
    let v;
    if ($("#ve-id").value) {
      v = await api(`/vouchers/${$("#ve-id").value}`, { method: "PATCH", body: { ...body, expected_version: +$("#ve-version").value } });
      if ($("#ve-action").value === "post" && v.status !== "posted") v = await api(`/vouchers/${v.id}/post`, { method: "POST", body: {} });
    } else v = await api(`/companies/${state.company}/vouchers`, { method: "POST", body: { ...body, action: $("#ve-action").value } });
    $("#ve-msg").innerHTML = `<span class="okmsg">Saved ${esc(v.doc.voucher_type)} ${esc(v.doc.voucher_number)} (${esc(v.status)}, sync ${esc(v.sync_status)})</span>`;
    const type = $("#ve-type").value; editing = null; resetEntry(type);
    $("#ve-msg").innerHTML = `<span class="okmsg">Saved ${esc(v.doc.voucher_type)} ${esc(v.doc.voucher_number)} (${esc(v.status)}, sync ${esc(v.sync_status)})</span>`;
  } catch (err) { $("#ve-msg").innerHTML = `<span class="error">${esc(err.message)}</span>`; }
};
const FKEYS = { F4: "Contra", F5: "Payment", F6: "Receipt", F7: "Journal", F8: "Sales", F9: "Purchase" };
document.addEventListener("keydown", (e) => {
  if (currentTab !== "entry") return;
  if (FKEYS[e.key] || (e.key === "F7" && e.altKey)) {   // Alt+F7 stock journal, as in Tally
    e.preventDefault();
    const want = e.altKey ? "Stock Journal" : FKEYS[e.key];
    const vt = state.vtypes.find((v) => v.is_active && v.name === want) || state.vtypes.find((v) => v.is_active && v.base_type === want);
    if (vt) { $("#ve-type").value = vt.name; resetEntry(vt.name); }
    $("#ve-date").focus();
  }
  if (e.key === "Enter" && e.ctrlKey) { e.preventDefault(); $("#vform").requestSubmit(); }
  if (e.key.toLowerCase() === "n" && e.altKey) {
    e.preventDefault();
    const target = { acc: null, inv: "ve-items", sj: "ve-src", mfg: "vm-comp" }[entryLayout];
    (target ? addItemLine(target) : addLine()).querySelector("input").focus();
  }
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
  if (name === "stock-journal" || name.startsWith("material-transfer-wip") || name === "wo-manufacturing") {
    const f2 = analysisParams();
    for (const k of ["item", "stock_group"]) if (f2[k]) p.set(k, f2[k]);
    if (sjType && name === "stock-journal") p.set("voucher_type", sjType);
  }
  if (name in ANALYSIS || name === "fifo-costing" || name === "sales-group-wise") {
    for (const [k, v] of Object.entries(analysisParams())) p.set(k, v);
    if (name in ANALYSIS) p.set("by", $("#ra-by").value);
  }
  if (fmt) p.set("format", fmt);
  return `/reports/${path}?${p}`;
}

// ------------------------------------------------------------------ sales / purchase analysis, FIFO costing
const ANALYSIS = { "sales-analysis": "Sales", "purchase-analysis": "Purchases", "sales-rm-cost": "Sales",
  "sales-fifo-cost": "Sales" };
// Reports with a cost column, and the view they open in.
const COSTED = { "sales-rm-cost": { label: "Raw-material cost", by: "item", none: "no production" },
  "sales-fifo-cost": { label: "FIFO cost", by: "voucher", none: "no stock movement" } };
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
  const trade = name in ANALYSIS, fifo = name === "fifo-costing", grouped = name === "sales-group-wise";
  const journal = name === "stock-journal" || name.startsWith("material-transfer-wip") || name === "wo-manufacturing";
  $("#ra-filters").hidden = !(trade || fifo || grouped || journal);
  $("#ra-cat").closest("label").hidden = journal;
  $("#ra-by-label").hidden = !trade;
  ["#ra-party-label", "#ra-returns-label"].forEach((id) => ($(id).hidden = !(trade || grouped)));
  $("#ra-item-label").hidden = grouped;
  if (journal) loadAnalysisFilters().catch(() => {});
  if (COSTED[name] && $("#ra-by").dataset.touched !== "1") $("#ra-by").value = COSTED[name].by;
  if (trade || fifo || grouped) loadAnalysisFilters().catch(() => {});
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
  // one fetch of every item line; the groups, vouchers and their totals are built here, so every level expands
  const p = rParams({ ...analysisParams(), by: "voucher" });
  for (const [k, v] of Object.entries(fixed)) if (k !== "by") p.set(k, v);
  const r = await api(`/reports/${report}?${p}`);
  const costed = COSTED[report];
  const counted = r.voucher_types.filter((t) => t.counted).map((t) => t.name).join(", ");
  const left = r.voucher_types.filter((t) => !t.counted).map((t) => t.name).join(", ");
  el.innerHTML = `<p class="muted">${esc(r.from)} to ${esc(r.to)} · ${esc(counted || "no voucher types")}`
    + `${left ? ` (not counted: ${esc(left)})` : ""}${r.include_returns ? " · returns deducted" : ""}`
    + ` · ${by === "voucher" ? "vouchers" : `${esc(BY_LABEL[by].toLowerCase())} rows`} expand; click an item line to open its voucher</p>`
    + `${report === "sales-rm-cost" ? `<p class="small muted">Raw-material cost: what the components of each sold item cost in its
       production vouchers (weighted average up to the sale date); labour and other costs are not included.</p>` : ""}`
    + `${report === "sales-fifo-cost" ? `<p class="small muted">FIFO cost: each sale takes the cost of the oldest stock of that item
       still on hand; returns come back at the cost they went out at. Tax: the GST on the item itself; GST on packing &amp;
       forwarding and other charges belongs to no item and is not included.</p>` : ""}<div id="t"></div>`;
  const node = (label, extra = {}) => ({ label, children: [], value: 0, tax: 0, cost: 0, noCost: 0, qty: 0, units: new Set(), vouchers: new Set(), ...extra });
  const add = (n, x) => {
    n.value += paise(x.value); n.tax += paise(x.tax); n.qty += Number(x.quantity || 0); n.units.add(x.unit || "");
    n.vouchers.add(x.voucher_id);
    if (costed) { if (x.cost == null) n.noCost += 1; else n.cost += paise(x.cost); }
  };
  const lineNode = (x, label) => {
    const n = node(label, { x, open: () => drillVoucher(x.voucher_id, `${x.voucher_type} ${x.voucher_number}`) });
    delete n.children;
    add(n, x);
    return n;
  };
  const vchLabel = (x) => `${x.voucher_type} ${x.voucher_number}`;
  const month = (d) => new Date(`${d}T00:00:00`).toLocaleString("en-IN", { month: "long", year: "numeric" });
  const keyOf = { party: (x) => [x.party || "(no party)", x.party || "(no party)"], date: (x) => [x.date, x.date],
    month: (x) => [x.date.slice(0, 7), month(x.date)], item: (x) => [x.item, x.item],
    category: (x) => [x.category || "", x.category || "(no category)"], stock_group: (x) => [x.item_group || "", x.item_group || "(no group)"] };
  const total = node("Total", { cls: "total" });
  const groups = new Map(), vouchers = new Map();
  for (const x of r.lines) {
    add(total, x);
    if (by === "voucher") {
      if (!vouchers.has(x.voucher_id)) vouchers.set(x.voucher_id, node(vchLabel(x), { v: x }));
      const v = vouchers.get(x.voucher_id);
      add(v, x); v.children.push(lineNode(x, x.item));
      continue;
    }
    const [k, label] = keyOf[by](x);
    if (!groups.has(k)) groups.set(k, node(label, { key: k, vmap: new Map() }));
    const g = groups.get(k);
    add(g, x);
    if (["item", "category", "stock_group"].includes(by)) { g.children.push(lineNode(x, `${x.date} · ${vchLabel(x)}${by === "item" ? "" : " · " + x.item}`)); continue; }
    if (!g.vmap.has(x.voucher_id)) { const v = node(vchLabel(x), { v: x }); g.vmap.set(x.voucher_id, v); g.children.push(v); }
    const v = g.vmap.get(x.voucher_id);
    add(v, x); v.children.push(lineNode(x, x.item));
  }
  let roots = by === "voucher" ? [...vouchers.values()] : [...groups.values()];
  if (!["voucher", "date", "month"].includes(by)) roots.sort((a, b) => Math.abs(b.value) - Math.abs(a.value));
  if (by === "month" || by === "date") roots.sort((a, b) => (a.key < b.key ? -1 : 1));
  const qty = (n) => (n.units.size === 1 ? qtyText(n.qty, [...n.units][0]) : "");
  const costCols = costed ? [
    { label: "Cost / unit", num: true, get: (n) => (n.x ? (n.x.cost_per_unit == null ? costed.none : money(n.x.cost_per_unit)) : "") },
    { label: costed.label, num: true, get: (n) => (n.noCost && !n.cost ? "—" : rupees(n.cost)) },
    { label: "Gross profit", num: true, get: (n) => (n.noCost && !n.cost ? "—" : rupees(n.value - n.cost)) },
    { label: "Margin %", num: true, get: (n) => (n.value && !(n.noCost && !n.cost) ? `${(((n.value - n.cost) / n.value) * 100).toFixed(2)}%` : "") },
  ] : [];
  treeTable($("#t"), [
    { label: "Date", get: (n) => (n.v ? esc(n.v.date) : n.x ? esc(n.x.date) : "") },
    { label: "Party", get: (n) => (n.v ? esc(n.v.party || "") : n.x && ["item", "category", "stock_group"].includes(by) ? esc(n.x.party || "") : "") },
    { label: "Item group", get: (n) => (n.x ? esc(n.x.item_group || "") : "") },
    { label: "Vouchers", num: true, get: (n) => (n.x ? "" : n.vouchers.size) },
    { label: "Quantity", num: true, get: qty },
    { label: "Rate", num: true, get: (n) => (n.x && n.x.rate ? money(n.x.rate) : "") },
    { label: "Value", num: true, get: (n) => rupees(n.value) },
    { label: "Tax", num: true, get: (n) => rupees(n.tax) },
    ...costCols,
  ], [...roots, total], { title: by === "voucher" ? "Voucher / item" : `${BY_LABEL[by]} / ${["item", "category", "stock_group"].includes(by) ? "voucher" : "voucher / item"}` });
}

// Stock journals as Tally shows them: each voucher with what came in and what went out side by side.
let sjType = null;   // chosen voucher type; null = Material Transfer to WIP when there is one
async function stockJournalView(el, report = "stock-journal") {
  await loadAnalysisFilters();
  const f = analysisParams();
  const p = rParams();
  for (const k of ["item", "stock_group"]) if (f[k]) p.set(k, f[k]);
  const fixedType = report !== "stock-journal";
  if (sjType && !fixedType) p.set("voucher_type", sjType);
  const r = await api(`/reports/${report}?${p}`);
  const qtyT = (q, u) => (q == null ? "" : qtyText(q, u));
  const mainCol = report === "material-transfer-wip-main";   // the group under Primary, after the item group
  const side = (title, lines, t) => `<div><h4>${esc(title)} <span class="muted small">${lines.length} item${lines.length === 1 ? "" : "s"}</span></h4>
    <table><thead><tr><th>Item</th><th>Item group</th>${mainCol ? "<th>Main group</th>" : ""}<th class="num">Qty</th><th class="num">Rate</th><th class="num">Amount</th><th>Godown</th><th>Batch</th></tr></thead><tbody>
    ${lines.map((x) => `<tr class="click" data-item="${esc(x.item_id)}" data-name="${esc(x.item)}"><td>${esc(x.item)}</td><td>${esc(x.item_group || "")}</td>
      ${mainCol ? `<td>${esc(x.main_group || "")}</td>` : ""}
      <td class="num">${qtyT(x.quantity, x.unit)}</td><td class="num">${money(x.rate)}</td><td class="num">${money(x.amount)}</td>
      <td>${esc(x.godown || "")}</td><td>${esc(x.batch || "")}</td></tr>`).join("") || `<tr><td colspan="${mainCol ? 8 : 7}" class="muted">Nothing</td></tr>`}
    <tr class="total"><td>Total</td><td></td>${mainCol ? "<td></td>" : ""}<td class="num">${qtyT(t.quantity, t.unit)}</td><td></td><td class="num">${money(t.amount)}</td><td></td><td></td></tr></tbody></table></div>`;
  const shown = r.vouchers.slice(0, 300);
  el.innerHTML = (fixedType ? "" : `<div class="row wrap"><label>Voucher type<select id="sj-type"><option value="all">All stock journals</option>
      ${r.voucher_types.map((t) => `<option ${t === r.voucher_type ? "selected" : ""}>${esc(t)}</option>`).join("")}</select></label></div>`) + `
    <p class="muted">${esc(r.from)} to ${esc(r.to)} · ${r.total.vouchers} voucher${r.total.vouchers === 1 ? "" : "s"} · in ${money(r.total.in_amount)}
      · out ${money(r.total.out_amount)} · vouchers expand; click an item for its movements${r.vouchers.length > shown.length ? ` · first ${shown.length} shown (narrow the period or filter by item)` : ""}</p>
    <div class="tree-tools"><button type="button" class="link" data-sj="1">Expand all</button><button type="button" class="link" data-sj="0">Collapse all</button></div>
    ${shown.map((v, i) => `<details class="fs-block" ${i < 5 ? "open" : ""}><summary><h3>${esc(v.voucher_type)} ${esc(v.voucher_number)} · ${esc(v.date)}</h3>
      <span class="muted small"> in ${money(v.in_total.amount)} · out ${money(v.out_total.amount)}</span>
      <button type="button" class="link" data-v="${esc(v.id)}" data-t="${esc(`${v.voucher_type} ${v.voucher_number}`)}">open voucher</button></summary>
      ${v.narration ? `<p class="small muted">${esc(v.narration)}</p>` : ""}
      <div class="sj-pair">${side("In (produced / received)", v.in, v.in_total)}${side("Out (consumed / issued)", v.out, v.out_total)}</div></details>`).join("")
      || `<p class="muted">No stock journals in this period</p>`}`;
  if ($("#sj-type")) $("#sj-type").onchange = (e) => { sjType = e.target.value; drawNav(); };
  el.querySelectorAll("[data-sj]").forEach((b) => (b.onclick = () => el.querySelectorAll("details.fs-block").forEach((d) => (d.open = b.dataset.sj === "1"))));
  el.querySelectorAll("button[data-v]").forEach((b) => (b.onclick = (e) => { e.preventDefault(); drillVoucher(b.dataset.v, b.dataset.t); }));
  el.querySelectorAll("tr[data-item]").forEach((tr) => (tr.onclick = () => drillTo(tr.dataset.name, (e2) => stockItemView(e2, tr.dataset.item))));
}

// WO Manufacturing vouchers: each produced item expands into the consumed item groups, and those into items.
async function woManufacturingView(el) {
  await loadAnalysisFilters();
  const f = analysisParams();
  const p = rParams();
  for (const k of ["item", "stock_group"]) if (f[k]) p.set(k, f[k]);
  const r = await api(`/reports/wo-manufacturing?${p}`);
  const qtyT = (q, u) => (q == null ? "" : qtyText(q, u));
  const itemOpen = (x) => () => drillTo(x.item, (e2) => stockItemView(e2, x.item_id));
  const roots = r.vouchers.map((v) => {
    const made = v.in[0] || {};
    return {
      label: v.in.map((x) => x.item).join(", ") || "(nothing produced)",
      row: { batch: v.in.map((x) => x.batch || "").filter(Boolean).join(", "), voucher: v.voucher_number, vtype: v.voucher_type, date: v.date, group: v.in.map((x) => x.item_group || "").join(", "),
             qty: v.in.length === 1 ? qtyT(made.quantity, made.unit) : qtyT(v.in_total.quantity, v.in_total.unit),
             amount: v.out_total.amount, div: v.divisions },
      children: v.consumed_groups.map((g) => ({
        label: g.group, cls: "", row: { qty: qtyT(g.quantity, g.unit), amount: g.amount, items: g.items.length, div: g.divisions },
        children: g.items.map((x) => ({ label: x.item, row: { batch: x.batch, qty: qtyT(x.quantity, x.unit), rate: x.rate, amount: x.amount, div: { [x.division]: x.amount } }, open: itemOpen(x) })),
      })),
    };
  });
  if (roots.length) roots.push({ label: "Total consumed", cls: "total", row: { amount: r.total.out_amount, div: r.total.divisions } });
  el.innerHTML = `<p class="muted">${esc(r.from)} to ${esc(r.to)} · ${r.total.vouchers} voucher${r.total.vouchers === 1 ? "" : "s"}
    · consumed ${money(r.total.out_amount)} · click a produced item for what it consumed, group-wise</p>
    <p class="small muted">Hydraulic, COC and OC split the consumption by the division of each consumed item's stock group (MIS settings)${
      Number(r.total.divisions.other) ? ` · ${money(r.total.divisions.other)} is in groups with none of these divisions` : ""}.</p><div id="t"></div>`;
  treeTable($("#t"), [
    { label: "Batch", get: (x) => esc(x.row.batch || "") },
    { label: "Voucher", get: (x) => esc(x.row.voucher || "") },
    { label: "Voucher type", get: (x) => esc(x.row.vtype || "") },
    { label: "Date", get: (x) => esc(x.row.date || "") },
    { label: "Item group", get: (x) => esc(x.row.group || "") },
    { label: "Quantity", num: true, get: (x) => x.row.qty || "" },
    { label: "Rate", num: true, get: (x) => (x.row.rate == null ? "" : money(x.row.rate)) },
    { label: "Consumed", num: true, get: (x) => (x.row.amount == null ? "" : money(x.row.amount)) },
    ...[["hydraulic", "Hydraulic"], ["coc", "COC"], ["cooler", "OC"]].map(([k, label]) => (
      { label, num: true, get: (x) => (x.row.div && Number(x.row.div[k]) ? money(x.row.div[k]) : "") })),
  ], roots, { title: "Produced item / consumed group / item" });
}

// Item group-wise sales: stock groups expand into sub-groups and items; an item opens its invoices.
async function groupWiseView(el) {
  await loadAnalysisFilters();
  const p = rParams(analysisParams());
  p.delete("item");
  const r = await api(`/reports/sales-group-wise?${p}`);
  const counted = r.voucher_types.filter((t) => t.counted).map((t) => t.name).join(", ");
  const total = Number(r.total.value) || 0;
  const node = (n) => ({
    label: n.name, n, cls: n.kind === "group" ? "" : "",
    children: n.kind === "group" ? n.children.map(node) : undefined,
    open: n.kind === "item" ? () => drillTo(n.name, (e) => analysisView(e, "sales-fifo-cost", { by: "voucher", item: n.key })) : undefined,
  });
  const roots = r.tree.map(node);
  roots.push({ label: "Total", n: { ...r.total, kind: "total" }, cls: "total" });
  const share = (n) => {
    if (!total || n.kind === "total") return "";
    const pc = (Number(n.value) / total) * 100;
    return `<span class="bar" style="width:${Math.max(1, Math.min(60, Math.abs(pc) * 0.6))}px"></span>${pc.toFixed(1)}%`;
  };
  el.innerHTML = `<p class="muted">${esc(r.from)} to ${esc(r.to)} · ${esc(counted || "no voucher types")}`
    + `${r.include_returns ? " · returns deducted" : ""} · click a group to expand it, an item for its invoices</p>`
    + `<p class="small muted">Groups include all their sub-groups and items. Cost is FIFO; tax is the GST on the items
       (not on packing &amp; forwarding or other charges).</p><div id="t"></div>`;
  treeTable($("#t"), [
    { label: "Items", num: true, get: (x) => (x.n.kind === "group" ? x.n.items : "") },
    { label: "Quantity", num: true, get: (x) => qtyText(x.n.quantity, x.n.unit) },
    { label: "Value", num: true, get: (x) => money(x.n.value) },
    { label: "Share", num: true, get: (x) => share(x.n) },
    { label: "Tax", num: true, get: (x) => money(x.n.tax) },
    { label: "FIFO cost", num: true, get: (x) => (x.n.cost == null ? "" : money(x.n.cost)) },
    { label: "Gross profit", num: true, get: (x) => (x.n.margin == null ? "" : money(x.n.margin)) },
    { label: "Margin %", num: true, get: (x) => (x.n.margin_pct == null ? "" : `${x.n.margin_pct}%`) },
  ], roots, { title: "Stock group / item" });
}

// ------------------------------------------------------------------ MIS reports by division
let misCache = null;   // settings of the current company
async function misSettings(fresh = false) {
  if (fresh || !misCache || misCache.company !== state.company)
    misCache = { company: state.company, ...(await api(`/mis/settings?company_id=${state.company}`)) };
  return misCache;
}
const misPeriod = () => rParams();
const misNote = (r) => `<p class="muted">${esc(r.from)} to ${esc(r.to)} · divisions: ${r.divisions.map((d) => esc(d.name)).join(", ")}
  · <a href="#" class="mis-settings-link">division settings</a></p>`;
function bindMisLinks(el) {
  el.querySelectorAll(".mis-settings-link").forEach((a) => (a.onclick = (e) => { e.preventDefault(); show("masters"); openMaster("mis"); }));
}

async function misPlView(el) {
  const [r, st] = await Promise.all([api(`/reports/mis-pl?${misPeriod()}`), misSettings()]);
  const divs = r.divisions;
  const editable = st.can_edit && r.month;
  const cell = (ln, k) => {
    const v = k ? ln.by[k] : ln.total;
    const over = k && (ln.overridden || []).includes(k);
    const key = ln.editable && k && editable ? `${ln.editable}:${k}` : "";
    return `<span class="${key ? "edit" : ""} ${over ? "over" : ""}" ${key ? `data-edit="${key}"` : ""} title="${over ? "Entered by hand for this month" : ""}">${money(v)}${over ? " ✎" : ""}</span>`;
  };
  const roots = r.sections.map((sec) => ({ label: sec.name, sec: true, children: sec.lines.flatMap((ln) => [
    { label: ln.label, ln, cls: ln.strong ? "strong" : "" },
    ...(ln.pct ? [{ label: "% of net revenue", pct: ln, cls: "pcrow" }] : []),
  ]) }));
  el.innerHTML = misNote(r)
    + `<p class="small muted">Stock values come from the stock groups set in division settings; items below zero count as nil.
      ${editable ? "Click an opening or closing stock value to enter it by hand for this month." : r.month ? "" : "Choose a single month (from the 1st) to enter stock values by hand."}
      ${r.shared_cost_centres.length ? ` Shared cost centres (split by each ledger's %): ${esc(r.shared_cost_centres.join(", "))}.` : ""}</p><div id="t" class="mis"></div>`;
  const pc = (v) => (v == null ? "" : `${v}%`);
  treeTable($("#t"), [
    ...divs.map((d) => ({ label: d.name, num: true, get: (n) => (n.ln ? cell(n.ln, d.key) : n.pct ? pc(n.pct.pct[d.key]) : "") })),
    { label: "Total", num: true, get: (n) => (n.ln ? cell(n.ln, null) : n.pct ? pc(n.pct.pct_total) : "") },
  ], roots, { expandDepth: 1 });
  bindMisLinks(el);
  if (editable) $("#t").onclick = async (e) => {
    const sp = e.target.closest("[data-edit]");
    if (!sp) return;
    e.stopPropagation();
    try { if (await misEditStock(r.month, divs, sp.dataset.edit)) await drawNav(); } catch (err) { alert(err.message); }
  };
}

// enter an opening/closing stock value by hand for one month; key = "opening:rm:cooler"
async function misEditStock(month, divs, key) {
  const st = await misSettings();
  const [side, kind, div] = key.split(":");
  const label = `${side === "opening" ? "Opening" : "Closing"} ${{ rm: "RM", wip: "WIP", fg: "FG" }[kind]} value, ${divs.find((d) => d.key === div).name}, ${month}`;
  const v = prompt(`${label}\nEnter the value, or leave empty to use the value from Tally stock:`, "");
  if (v === null) return false;
  const clean = v.replace(/,/g, "").trim();
  if (clean && !/^-?\d+(\.\d{1,2})?$/.test(clean)) { alert("Enter an amount such as 125000.50"); return false; }
  const m = { ...(st.stock_overrides[month] || {}) };
  const d = { ...(m[div] || {}) };
  const k = { ...(d[kind] || {}) };
  if (clean) k[side] = clean; else delete k[side];
  d[kind] = k; m[div] = d;
  await api(`/mis/settings?company_id=${state.company}`, { method: "PUT", body: { stock_overrides: { [month]: m } } });
  await misSettings(true);
  return true;
}

// The P&L as the finance workbook lays it out: a block per stage, a row per division, a total and its %.
async function misSheetView(el) {
  const [r, st] = await Promise.all([api(`/reports/mis-finance-sheet?${misPeriod()}`), misSettings()]);
  const editable = st.can_edit && r.month;
  const pctText = (v) => (v == null ? "" : `${Number(v).toFixed(2)}%`);
  const block = (b, withMonth = true) => {
    const head = `<tr>${withMonth ? "<th>Month</th>" : ""}<th>Category</th>${b.columns.map((c) => `<th class="num">${esc(c)}</th>`).join("")}${b.pct ? `<th class="num">%</th>` : ""}</tr>`;
    const body = b.rows.map((row) => `<tr class="${row.total ? "total" : ""}">${withMonth ? `<td>${esc(row.month || "")}</td>` : ""}<td>${esc(row.category)}</td>`
      + row.values.map((v, i) => {
        const key = b.editable && b.editable[i] && row.division ? `${b.editable[i]}:${row.division}` : "";
        const over = key && (b.overridden[i] || []).includes(row.division);
        return `<td class="num ${key && editable ? "edit" : ""} ${over ? "over" : ""}" ${key ? `data-edit="${key}"` : ""}>${money(v)}${over ? " ✎" : ""}</td>`;
      }).join("")
      + `${b.pct ? `<td class="num">${pctText(row.pct)}</td>` : ""}</tr>`).join("");
    return `<details class="fs-block" open><summary><h3>${esc(b.title)}</h3></summary><div class="mis-grid"><table class="mis fs"><thead>${head}</thead><tbody>${body}</tbody></table></div></details>`;
  };
  const blocks = r.blocks.map((b, i) => block(b) + (i === 0 && r.purchase.columns.length > 1 ? block(r.purchase, false) : "")).join("");
  el.innerHTML = `<p class="fs-title">Monthly Finance Sheet - ${esc(r.period)}</p>${misNote(r)}`
    + `<div class="tree-tools"><button type="button" class="link" data-fs="1">Expand all</button><button type="button" class="link" data-fs="0">Collapse all</button></div>`
    + `<p class="small muted">% is of each division's net revenue. ${editable ? "Click an opening or closing stock value to enter it by hand for this month." : r.month ? "" : "Choose a single month (from the 1st) to enter stock values by hand."}</p>`
    + `<div class="fs-blocks">${blocks}</div>`;
  bindMisLinks(el);
  el.querySelectorAll("[data-fs]").forEach((b) => (b.onclick = () => el.querySelectorAll("details.fs-block").forEach((d) => (d.open = b.dataset.fs === "1"))));
  if (!editable) return;
  el.querySelectorAll("td[data-edit]").forEach((td) => (td.onclick = async () => {
    try { if (await misEditStock(r.month, r.divisions, td.dataset.edit)) await drawNav(); } catch (err) { alert(err.message); }
  }));
}

async function misExpenseView(el) {
  const [r, st] = await Promise.all([api(`/reports/mis-exp-breakup?${misPeriod()}`), misSettings()]);
  const divs = r.divisions;
  el.innerHTML = misNote(r) + `<p class="small muted">Overheads expand into expense heads (the ledger's group, or the head set for it)
    and heads into ledgers. Shared costs are split by each ledger's %. ${st.can_edit ? "Click a ledger to set its split." : "Click a ledger for its statement."}</p><div id="t" class="mis"></div>`;
  const sum = (rows, k) => rows.reduce((a, x) => a + paise(k ? x.by[k] : x.amount), 0);
  const roots = r.sections.map((sec) => {
    const heads = new Map();
    for (const x of sec.rows) { if (!heads.has(x.head)) heads.set(x.head, []); heads.get(x.head).push(x); }
    return { label: sec.name, rows: sec.rows, children: [...heads.entries()].map(([h, rows]) => ({ label: h, rows,
      children: rows.map((x) => ({ label: x.ledger, rows: [x], open: () => misLedgerClick(x, st) })) }))
      .sort((a, b) => sum(b.rows) - sum(a.rows)) };
  });
  const all = r.sections.flatMap((sec) => sec.rows);
  roots.push({ label: "Total overheads", rows: all, cls: "total" });
  treeTable($("#t"), [
    ...divs.map((d) => ({ label: d.name, num: true, get: (n) => rupees(sum(n.rows, d.key)) })),
    { label: "Total", num: true, get: (n) => rupees(sum(n.rows)) },
  ], roots, { expandDepth: 1, title: "Overhead / head / ledger" });
  bindMisLinks(el);
}
function misLedgerClick(x, st) {
  if (!x.ledger_id) return;
  if (!st.can_edit) return openLedgerStatement(x.ledger_id, x.ledger);
  const led = st.ledgers.find((l) => l.id === x.ledger_id) || { id: x.ledger_id, name: x.ledger, class: x.class, head: x.head };
  editMisLedger(led, () => drawNav());
}

async function misBreakupView(el, head = null) {
  const [r, st] = await Promise.all([api(`/reports/mis-exp-breakup?${misPeriod()}`), misSettings()]);
  const divs = r.divisions;
  el.innerHTML = misNote(r) + `<p class="small muted">${head ? `Head: <b>${esc(head)}</b> · ` : ""}Each overhead ledger, with deferred
    charges added. ${st.can_edit ? "Click a ledger to set its split %, division, head or class." : ""}</p><div id="t" class="mis"></div>`;
  const splitText = (x) => (x.division ? (divs.find((d) => d.key === x.division)?.name || "shared")
    : x.split ? divs.map((d) => `${Number(x.split[d.key] || 0)}`).join("/") : "equal");
  const roots = [];
  for (const sec of r.sections) {
    const list = head ? sec.rows.filter((x) => x.head === head) : sec.rows;
    if (!list.length) continue;
    roots.push({ label: sec.name, rows: list, children: list.map((x) => ({ label: x.ledger, x, rows: [x], open: () => misLedgerClick(x, st) })) });
  }
  const sum = (rows, k) => rows.reduce((a, x) => a + paise(k ? x.by[k] : x.amount), 0);
  treeTable($("#t"), [
    { label: "Head", get: (n) => (n.x ? esc(n.x.head) : "") },
    { label: "In Tally", num: true, get: (n) => rupees(n.rows.reduce((a, x) => a + paise(x.tally), 0)) },
    { label: "Deferred", num: true, get: (n) => { const v = n.rows.reduce((a, x) => a + paise(x.deferred), 0); return v ? rupees(v) + (n.x && n.x.deferred_mode === "replace" ? " *" : "") : ""; } },
    { label: "Amount", num: true, get: (n) => rupees(sum(n.rows)) },
    { label: "%", num: true, get: (n) => (n.x && n.x.pct != null ? `${n.x.pct}%` : "") },
    { label: "Split", get: (n) => (n.x ? esc(splitText(n.x)) : "") },
    ...divs.map((d) => ({ label: d.name, num: true, get: (n) => rupees(sum(n.rows, d.key)) })),
  ], roots, { expandDepth: 1, title: "Overhead / ledger" });
  if (roots.some((n) => n.rows.some((x) => x.deferred_mode === "replace" && Number(x.deferred)))) el.insertAdjacentHTML("beforeend", `<p class="small muted">* the deferred charge replaces the amount booked in Tally.</p>`);
  bindMisLinks(el);
}

// one dialog for all MIS edits; fill(body) builds the fields, save() returns true to close
function misDialog(title, fill, save) {
  $("#mis-title").textContent = title;
  $("#mis-msg").textContent = "";
  fill($("#mis-body"));
  $("#mis-cancel").onclick = () => $("#mis-dlg").close();
  $("#mis-form").onsubmit = async (e) => {
    e.preventDefault();
    try { if ((await save()) !== false) $("#mis-dlg").close(); } catch (err) { $("#mis-msg").textContent = err.message; }
  };
  $("#mis-dlg").showModal();
}

async function editMisLedger(led, after) {
  const st = await misSettings();
  const divs = st.divisions;
  misDialog(`MIS settings: ${led.name}`, (b) => {
    b.innerHTML = `<p class="muted small">${esc(led.group || "")}${led.primary ? " · " + esc(led.primary) : ""}</p>
      <div class="grid">
        <label>Counts as<select name="class">${Object.entries(st.classes).map(([k, v]) => `<option value="${k}" ${k === led.class ? "selected" : ""}>${esc(v)}</option>`).join("")}</select></label>
        <label>Expense head<input name="head" value="${esc(led.head || "")}" placeholder="${esc(led.default_head || "")}"></label>
        <label>Division (when no cost centre is given)<select name="division"><option value="">split by % below</option>
          ${divs.map((d) => `<option value="${d.key}" ${led.division === d.key ? "selected" : ""}>${esc(d.name)} only</option>`).join("")}</select></label>
        <label>Deferred schedules for this ledger<select name="deferred">
          <option value="add" ${led.deferred !== "replace" ? "selected" : ""}>add to the amount in Tally</option>
          <option value="replace" ${led.deferred === "replace" ? "selected" : ""}>replace the amount in Tally</option></select></label>
      </div>
      <h3>Split of shared amounts (%)</h3><p class="small muted">Amounts on a shared cost centre (such as H.O) or with no cost centre. Leave all empty for equal parts.</p>
      <div class="split-row">${divs.map((d) => `<label>${esc(d.name)}<input name="split_${d.key}" inputmode="decimal" value="${led.split && led.split[d.key] != null ? esc(led.split[d.key]) : ""}"></label>`).join("")}</div>
      <p><button type="button" class="link" id="mis-open-ledger">Open ledger statement</button></p>`;
    $("#mis-open-ledger").onclick = () => { $("#mis-dlg").close(); openLedgerStatement(led.id, led.name); };
  }, async () => {
    const f = $("#mis-form").elements;
    const split = {};
    for (const d of divs) {
      const v = f[`split_${d.key}`].value.trim();
      if (v !== "") { if (!/^\d+(\.\d+)?$/.test(v)) throw new Error(`${d.name}: enter a percentage`); split[d.key] = Number(v); }
    }
    const sum = Object.values(split).reduce((a, v) => a + v, 0);
    if (Object.keys(split).length && Math.abs(sum - 100) > 0.01 && !confirm(`The split adds up to ${sum}%, not 100%. Amounts are shared in these proportions. Save?`)) return false;
    const body = { class: f.class.value === led.default_class ? null : f.class.value, head: f.head.value.trim() || null,
      division: f.division.value || null, split: Object.keys(split).length ? split : null, deferred: f.deferred.value };
    for (const k of Object.keys(body)) if (body[k] === null) delete body[k];
    await api(`/mis/settings?company_id=${state.company}`, { method: "PUT", body: { ledgers: { [led.id]: body } } });
    await misSettings(true);
    if (after) await after();
  });
}

async function misDeferredView(el) {
  const [r, st] = await Promise.all([api(`/reports/mis-deferred?${rParams()}`), misSettings()]);
  const mon = (k) => new Date(`${k}-01T00:00:00`).toLocaleString("en-IN", { month: "short", year: "2-digit" });
  el.innerHTML = `<p class="muted">${esc(r.from)} to ${esc(r.to)} · prepaid costs spread evenly over months
    ${st.can_edit ? ` · <button type="button" id="dx-add">Add schedule</button>` : ""}</p>
    <h3>Schedules</h3><div class="mis-grid"><table class="mis" id="dx-s"></table></div>
    <h3>Profit &amp; loss charge by head</h3><div class="mis-grid" id="dx-pl"></div>
    <h3>Balance still to charge (balance sheet) by head</h3><div class="mis-grid" id="dx-bs"></div>`;
  const head = `<th class="num">${r.months.map(mon).join(`</th><th class="num">`)}</th>`;
  const nums = (arr) => arr.map((v) => `<td class="num">${Number(v) ? money(v) : ""}</td>`).join("");
  $("#dx-s").innerHTML = `<thead><tr><th>Head</th><th>Description</th><th class="num">Amount</th><th>From</th><th class="num">Months</th><th class="num">Per month</th>${head}</tr></thead><tbody>`
    + (r.schedules.map((x, i) => `<tr data-i="${i}" class="${st.can_edit ? "click" : ""}"><td>${esc(x.head)}</td><td class="wrap">${esc(x.description || "")}</td>`
      + `<td class="num">${money(x.total_amount)}</td><td>${mon(x.first_month)} – ${mon(x.last_month)}</td><td class="num">${x.months}</td>`
      + `<td class="num">${money(x.monthly)}</td>${nums(x.charge)}</tr>`).join("")
      || `<tr><td colspan="${6 + r.months.length}" class="muted">No schedules yet${st.can_edit ? " – use Add schedule" : ""}</td></tr>`) + `</tbody>`;
  // by head, each head expanding into its schedules
  const grid = (elId, key) => {
    const roots = r.heads.map((h) => ({ label: h.head, vals: h[key], children: r.schedules.filter((x) => x.head === h.head)
      .map((x) => ({ label: x.description || `${mon(x.first_month)} – ${mon(x.last_month)}`, vals: x[key] })) }));
    roots.push({ label: "Total", vals: r.total[key], cls: "total" });
    treeTable($(elId), r.months.map((mo, i) => ({ label: mon(mo), num: true, get: (n) => (Number(n.vals[i]) ? money(n.vals[i]) : "") })),
      roots, { title: "Head / schedule" });
  };
  grid("#dx-pl", "charge");
  grid("#dx-bs", "balance");
  if (!st.can_edit) return;
  $("#dx-add").onclick = () => editDeferred(null);
  $("#dx-s").querySelectorAll("tr.click").forEach((tr) => (tr.onclick = () => editDeferred(r.schedules[+tr.dataset.i])));
}

async function editDeferred(x) {
  const st = await misSettings();
  const ledgers = st.ledgers.filter((l) => ["factory", "admin"].includes(l.class));
  misDialog(x ? `Deferred: ${x.head}` : "New deferred schedule", (b) => {
    b.innerHTML = `<div class="grid">
      <label>Expense ledger<select name="ledger_id"><option value="">(none – own line in admin overheads)</option>
        ${ledgers.map((l) => `<option value="${l.id}" ${x && x.ledger_id === l.id ? "selected" : ""}>${esc(l.name)}</option>`).join("")}</select></label>
      <label>Head *<input name="head" required maxlength="200" value="${esc(x ? x.head : "")}" placeholder="e.g. Insurance Expenses"></label>
      <label>Total amount *<input name="total_amount" inputmode="decimal" required value="${esc(x ? x.total_amount : "")}"></label>
      <label>First month *<input name="first_month" type="month" required value="${esc(x ? x.first_month : new Date().toISOString().slice(0, 7))}"></label>
      <label>Number of months *<input name="months" type="number" min="1" max="120" required value="${x ? x.months : 12}"></label>
      <label>Description<input name="description" maxlength="1000" value="${esc(x ? x.description || "" : "")}"></label></div>
      <p class="small muted">The P&amp;L is charged amount ÷ months each month from the first month. Choose the ledger so the charge
        follows that ledger's division split, and set the ledger to "replace" if the full payment is booked to it in Tally.</p>
      ${x ? `<p><button type="button" class="secondary" id="dx-del">Delete schedule</button></p>` : ""}`;
    const f = $("#mis-form").elements;
    f.ledger_id.onchange = () => { if (!f.head.value) f.head.value = f.ledger_id.selectedOptions[0].textContent; };
    if (x) $("#dx-del").onclick = async () => {
      if (!confirm(`Delete the schedule "${x.head}"?`)) return;
      try { await api(`/mis/deferred/${x.id}?company_id=${state.company}`, { method: "DELETE" }); $("#mis-dlg").close(); await drawNav(); }
      catch (err) { $("#mis-msg").textContent = err.message; }
    };
  }, async () => {
    const f = $("#mis-form").elements;
    const body = { head: f.head.value.trim(), ledger_id: f.ledger_id.value || null, description: f.description.value.trim() || null,
      total_amount: f.total_amount.value.replace(/,/g, "").trim(), first_month: f.first_month.value, months: Number(f.months.value) };
    if (!body.head || !body.total_amount || !body.first_month) throw new Error("Head, amount and first month are needed");
    await api(`/mis/deferred${x ? "/" + x.id : ""}?company_id=${state.company}`, { method: x ? "PUT" : "POST", body });
    await drawNav();
  });
}

// Masters → MIS divisions: divisions, cost centres, stock groups and ledgers
let misTab = "centres";
async function misSettingsView(el) {
  const st = await misSettings(true);
  const divs = st.divisions;
  const ro = !st.can_edit;
  const divOpts = (sel, extra) => `${extra || ""}${divs.map((d) => `<option value="${d.key}" ${sel === d.key ? "selected" : ""}>${esc(d.name)}</option>`).join("")}`;
  el.innerHTML = `<h3>MIS divisions</h3><p class="small muted">How the MIS reports place amounts in divisions: by the Tally cost centre first,
      then the ledger's own division, then (for sales and purchases) the stock group of the items; anything else is shared by the ledger's %.</p>
    <nav class="subnav" id="mis-tabs"><button type="button" data-t="centres">Divisions &amp; cost centres</button>
      <button type="button" data-t="groups">Stock groups</button><button type="button" data-t="ledgers">Ledgers</button></nav>
    <div id="mis-tab"></div>`;
  el.querySelectorAll("#mis-tabs button").forEach((b) => { b.classList.toggle("active", b.dataset.t === misTab); b.onclick = () => { misTab = b.dataset.t; misSettingsView(el); }; });
  const box = $("#mis-tab");
  const saveBtn = (id) => (ro ? "" : `<p><button type="button" id="${id}">Save</button> <span id="${id}-msg"></span></p>`);
  const done = async (id, body) => {
    try { await api(`/mis/settings?company_id=${state.company}`, { method: "PUT", body }); $(`#${id}-msg`).innerHTML = `<span class="okmsg">Saved</span>`; await misSettings(true); }
    catch (err) { $(`#${id}-msg`).innerHTML = `<span class="error">${esc(err.message)}</span>`; }
  };
  if (misTab === "centres") {
    box.innerHTML = `<h3>Divisions</h3><table><thead><tr><th>Key</th><th>Name</th></tr></thead><tbody>
      ${divs.map((d) => `<tr><td>${esc(d.key)}</td><td><input data-div="${esc(d.key)}" value="${esc(d.name)}" ${ro ? "disabled" : ""}></td></tr>`).join("")}</tbody></table>
      <h3>Cost centres</h3><table><thead><tr><th>Cost centre in Tally</th><th>Division</th></tr></thead><tbody>
      ${st.cost_centres.map((c) => `<tr><td>${esc(c.name)}</td><td><select data-cc="${esc(c.name)}" ${ro ? "disabled" : ""}>${divOpts(c.division, `<option value="shared" ${c.division === "shared" ? "selected" : ""}>Shared (split by %)</option>`)}</select></td></tr>`).join("")
        || `<tr><td colspan="2" class="muted">No cost centres in this company</td></tr>`}</tbody></table>${saveBtn("mis-cc")}`;
    if (!ro) $("#mis-cc").onclick = () => done("mis-cc", {
      divisions: divs.map((d) => ({ key: d.key, name: box.querySelector(`[data-div="${d.key}"]`).value.trim() || d.name })),
      cost_centres: Object.fromEntries([...box.querySelectorAll("[data-cc]")].map((s) => [s.dataset.cc, s.value])) });
  } else if (misTab === "groups") {
    box.innerHTML = `<p class="row between"><span class="small muted">Sub-groups take their parent's setting unless set themselves.
      Kind marks stock counted as raw material, WIP or finished goods in the P&amp;L.</span><input id="mis-gq" placeholder="Search groups"></p>
      <table id="mis-gt"></table>${saveBtn("mis-sg")}`;
    const changed = {};
    const draw = () => {
      const q = $("#mis-gq").value.trim().toLowerCase();
      const rows = st.stock_groups.filter((g) => !q || g.name.toLowerCase().includes(q) || (g.parent || "").toLowerCase().includes(q));
      $("#mis-gt").innerHTML = `<thead><tr><th>Stock group</th><th>Under</th><th>Division</th><th>Kind</th><th>In effect</th></tr></thead><tbody>`
        + rows.slice(0, 400).map((g) => `<tr><td>${esc(g.name)}</td><td class="small muted">${esc(g.parent || "")}</td>
          <td><select data-g="${g.id}" data-f="division" ${ro ? "disabled" : ""}>${divOpts(g.division, `<option value="">(from parent)</option>`)}</select></td>
          <td><select data-g="${g.id}" data-f="kind" ${ro ? "disabled" : ""}><option value="">(from parent)</option>
            ${Object.entries(st.kinds).map(([k, v]) => `<option value="${k}" ${g.kind === k ? "selected" : ""}>${esc(v)}</option>`).join("")}</select></td>
          <td class="small">${esc([divs.find((d) => d.key === g.effective_division)?.name, st.kinds[g.effective_kind]].filter(Boolean).join(" · ") || "—")}</td></tr>`).join("")
        + `</tbody>`;
      $("#mis-gt").querySelectorAll("select").forEach((s) => (s.onchange = () => {
        const g = st.stock_groups.find((x) => x.id === s.dataset.g);
        g[s.dataset.f] = s.value || null;
        changed[g.id] = { division: g.division, kind: g.kind };
      }));
    };
    $("#mis-gq").oninput = draw;
    draw();
    if (!ro) $("#mis-sg").onclick = () => done("mis-sg", { stock_groups: changed }).then(() => misSettingsView(el));
  } else {
    box.innerHTML = `<p class="row between"><span class="small muted">P&amp;L ledgers. Click one to change what it counts as, its head, division or split.</span>
      <input id="mis-lq" placeholder="Search ledgers"></p><table id="mis-lt"></table>`;
    const draw = () => {
      const q = $("#mis-lq").value.trim().toLowerCase();
      const rows = st.ledgers.filter((l) => !q || l.name.toLowerCase().includes(q) || (l.head || "").toLowerCase().includes(q));
      table($("#mis-lt"), [
        { label: "Ledger", key: "name", wrap: true }, { label: "Group", key: "group", wrap: true },
        { label: "Counts as", get: (l) => st.classes[l.class] || "" }, { label: "Head", key: "head", wrap: true },
        { label: "Division / split", get: (l) => (l.division ? divs.find((d) => d.key === l.division)?.name || l.division
          : l.split ? divs.map((d) => `${d.name} ${Number(l.split[d.key] || 0)}%`).join(", ") : "cost centre, else equal") },
        { label: "Deferred", get: (l) => (l.deferred === "replace" ? "replaces Tally" : "") },
      ], rows.slice(0, 1000), ro ? null : (l) => editMisLedger(l, () => misSettingsView(el)));
    };
    $("#mis-lq").oninput = draw;
    draw();
  }
}

async function fifoView(el) {
  await loadAnalysisFilters();
  const r = await api(`/reports/fifo-costing?${rParams(analysisParams())}`);
  el.innerHTML = `<p class="muted">${esc(r.from)} to ${esc(r.to)} · valued first-in-first-out: every issue takes the cost of the
    oldest stock still on hand · gross profit counts sales only · stock groups expand; click an item for its movements</p>
    <p><input id="fifo-q" placeholder="Search item"></p><div id="t"></div>`;
  const draw = () => {
    const q = $("#fifo-q").value.trim().toLowerCase();
    const groups = new Map();
    for (const x of r.items.filter((i) => !q || i.item.toLowerCase().includes(q))) {
      const g = x.stock_group || "(no group)";
      if (!groups.has(g)) groups.set(g, { label: g, items: [], children: [] });
      groups.get(g).items.push(x);
      groups.get(g).children.push({ label: x.item, x, items: [x], open: () => drillTo(x.item, (el2) => stockItemView(el2, x.item_id)) });
    }
    const roots = [...groups.values()].sort((a, b) => a.label.localeCompare(b.label));
    roots.push({ label: "Total", total: r.total, cls: "total" });
    const sum = (n, k) => (n.total ? money(n.total[k]) : rupees(n.items.reduce((a, x) => a + paise(x[k]), 0)));
    const q2 = (k) => (n) => (n.x ? qtyText(n.x[k], n.x.unit) : "");
    treeTable($("#t"), [
      { label: "Opening", num: true, get: q2("opening_qty") }, { label: "Opening value", num: true, get: (n) => sum(n, "opening_value") },
      { label: "In", num: true, get: q2("in_qty") }, { label: "In value", num: true, get: (n) => sum(n, "in_value") },
      { label: "Out", num: true, get: q2("out_qty") }, { label: "Out at FIFO cost", num: true, get: (n) => sum(n, "out_cost") },
      { label: "Sales value", num: true, get: (n) => sum(n, "sold_value") }, { label: "Gross profit", num: true, get: (n) => sum(n, "gross_profit") },
      { label: "Closing", num: true, get: q2("closing_qty") }, { label: "Closing value", num: true, get: (n) => sum(n, "closing_value") },
      { label: "Rate", num: true, get: (n) => (n.x && n.x.closing_rate ? money(n.x.closing_rate) : "") },
      { label: "", get: (n) => (n.x && n.x.short_qty ? `<span class="chip failed" title="Issued more than was in stock">short ${esc(n.x.short_qty)}</span>` : "") },
    ], roots, { title: "Stock group / item", expandDepth: q ? 1 : 0 });
  };
  $("#fifo-q").oninput = draw;
  draw();
}

$("#rform").onsubmit = (e) => { e.preventDefault(); renderers.reports(); };
$("#r-csv").onclick = (e) => { e.preventDefault(); download(reportUrl("csv"), `${$("#r-name").value}.csv`); };
$("#r-xlsx").onclick = (e) => { e.preventDefault(); download(reportUrl("xlsx"), `${$("#r-name").value}.xlsx`); };

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
    "sales-fifo-cost": (el) => analysisView(el, "sales-fifo-cost"),
    "sales-group-wise": groupWiseView, "stock-journal": (el) => stockJournalView(el), "material-transfer-wip": (el) => stockJournalView(el, "material-transfer-wip"),
    "material-transfer-wip-main": (el) => stockJournalView(el, "material-transfer-wip-main"),
    "wo-manufacturing": woManufacturingView,
    "mis-finance-sheet": misSheetView, "mis-pl": misPlView, "mis-expense": misExpenseView, "mis-exp-breakup": (el) => misBreakupView(el),
    "mis-deferred": misDeferredView,
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
  el.innerHTML = `<p class="muted">${esc(r.from)} to ${esc(r.to)} · opening ${q(r.opening_qty)} · closing ${q(r.closing_qty)} (value ${money(r.closing_value)})
    · months expand; click a movement to open its voucher</p><div id="t"></div>`;
  const month = (d) => new Date(`${d}T00:00:00`).toLocaleString("en-IN", { month: "long", year: "numeric" });
  const months = new Map();
  for (const x of r.lines) {
    const k = x.date.slice(0, 7);
    if (!months.has(k)) months.set(k, { label: month(x.date), inQ: 0, outQ: 0, val: 0, last: null, children: [] });
    const g = months.get(k);
    g.inQ += Number(x.inward_qty || 0); g.outQ += Number(x.outward_qty || 0); g.last = x.balance_qty;
    g.children.push({ label: `${x.date} · ${x.particulars || ""}`, x, open: () => drillVoucher(x.voucher_id, `${x.voucher_type} ${x.voucher_number}`) });
  }
  treeTable($("#t"), [
    { label: "Type / no.", get: (n) => (n.x ? esc(`${n.x.voucher_type} ${n.x.voucher_number}`) : "") },
    { label: "Godown", get: (n) => (n.x ? esc(n.x.godown || "") : "") },
    { label: "Inward", num: true, get: (n) => (n.x ? q(n.x.inward_qty) : n.inQ ? q(n.inQ) : "") },
    { label: "Outward", num: true, get: (n) => (n.x ? q(n.x.outward_qty) : n.outQ ? q(n.outQ) : "") },
    { label: "Value", num: true, get: (n) => (n.x ? (n.x.inward_value ? money(n.x.inward_value) : n.x.outward_value ? money(n.x.outward_value) : "") : "") },
    { label: "Balance", num: true, get: (n) => (n.x ? q(n.x.balance_qty) : q(n.last)) },
  ], [...months.values()], { title: "Month / movement" });
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
  el.innerHTML = `<p class="muted">${esc(r.group)} · ${esc(r.from)} to ${esc(r.to)} · months expand; click a voucher to open it</p><div id="t"></div>`;
  const bal = (v) => (v == null ? "" : `${money(Math.abs(v))} ${+v >= 0 ? "Dr" : "Cr"}`);
  const month = (d) => new Date(`${d}T00:00:00`).toLocaleString("en-IN", { month: "long", year: "numeric" });
  const months = new Map();
  for (const x of r.lines) {
    const k = x.date.slice(0, 7);
    if (!months.has(k)) months.set(k, { label: month(x.date), dr: 0, cr: 0, balance: null, children: [] });
    const g = months.get(k);
    g.dr += paise(x.debit); g.cr += paise(x.credit); g.balance = x.balance;
    g.children.push({ label: `${x.date} · ${x.particulars || ""}`, x, open: () => x.voucher_id && drillVoucher(x.voucher_id, `${x.voucher_type} ${x.voucher_number}`) });
  }
  const roots = [{ label: "Opening balance", balance: r.opening, cls: "total" }, ...months.values(),
    { label: "Closing balance", dr: paise(r.total_debit), cr: paise(r.total_credit), balance: r.closing, cls: "total" }];
  treeTable($("#t"), [
    { label: "Type / no.", get: (n) => (n.x ? esc(`${n.x.voucher_type} ${n.x.voucher_number}`) : "") },
    { label: "Debit", num: true, get: (n) => (n.x ? (+n.x.debit ? money(n.x.debit) : "") : n.dr ? rupees(n.dr) : "") },
    { label: "Credit", num: true, get: (n) => (n.x ? (+n.x.credit ? money(n.x.credit) : "") : n.cr ? rupees(n.cr) : "") },
    { label: "Balance", num: true, get: (n) => bal(n.x ? n.x.balance : n.balance) },
  ], roots, { title: "Month / voucher" });
}

// The voucher itself, inside the report trail; ledgers and items in it drill further
function drillVoucher(id, title) { return drillTo(title || "Voucher", (el) => voucherView(el, id)); }
async function voucherView(el, id) {
  const v = await api(`/vouchers/${id}`), d = v.doc;
  const ledgerIds = Object.fromEntries((state.ledgers || []).map((l) => [l.name, l.id]));
  el.innerHTML = `<div class="row between"><h3>${esc(d.voucher_type)} ${esc(d.voucher_number)} · ${esc(d.date)}</h3>
      <span>${chip(v.status)} ${can("export") ? `<button type="button" class="secondary" id="v-xlsx">Excel</button>` : ""}
      <button type="button" id="v-open">Open in Vouchers tab</button></span></div>
    ${d.party_ledger ? `<p>Party: <b>${esc(d.party_ledger)}</b>${d.reference_no ? ` · Ref ${esc(d.reference_no)}` : ""}</p>` : ""}
    <p>${esc(d.narration || "")}</p>
    ${d.entries.length ? `<table id="v-e"></table>` : `<p class="muted">No ledger entries (stock-only voucher)</p>`}
    ${d.inventory.length ? inventoryTable(d.inventory, v.item_groups || {}) : ""}
    <div id="v-hist"><p class="muted">Loading history…</p></div>`;
  historyHtml(id).then((html) => { if ($("#v-hist")) $("#v-hist").innerHTML = html; }).catch(() => {});
  if ($("#v-xlsx")) $("#v-xlsx").onclick = () => download(`/vouchers/${id}/xlsx`, `${d.voucher_type} ${d.voucher_number}.xlsx`.replace(/[\\/:*?"<>|]/g, "-"));
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

// ------------------------------------------------------------------ masters menu
const MASTER_VIEWS = ["items", "stock-groups", "stock-categories", "units", "bom", "ledgers", "ledger-groups", "ledger-categories", "mis"];
let masterView = (() => { try { const v = localStorage.getItem("erp_master"); return MASTER_VIEWS.includes(v) ? v : "items"; } catch (_) { return "items"; } })();
let ledgerGroup = null;   // set when a ledger group is opened from the Ledger group list
const listCache = {};
renderers.masters = async () => {
  document.querySelectorAll("#m-nav button").forEach((b) => b.classList.toggle("active", b.dataset.m === masterView));
  $("#mp-ledgers").hidden = masterView !== "ledgers";
  $("#mp-items").hidden = masterView !== "items";
  $("#mp-mis").hidden = masterView !== "mis";
  $("#mp-bom").hidden = masterView !== "bom";
  $("#mp-list").hidden = ["ledgers", "items", "mis", "bom"].includes(masterView);
  if (masterView === "mis") return misSettingsView($("#mp-mis"));
  if (masterView === "bom") return renderBom();
  if (masterView === "ledgers") return renderLedgers();
  if (masterView === "items") return renderItems();
  return renderMasterList();
};
function openMaster(view) {
  masterView = view;
  try { localStorage.setItem("erp_master", view); } catch (_) { /* private window */ }
  return renderers.masters().catch((e) => alert(e.message));
}
document.querySelectorAll("#m-nav button").forEach((b) => (b.onclick = () => {
  if (b.dataset.m === "ledgers") { ledgerGroup = null; $("#lq").value = ""; }
  if (b.dataset.m === "items") { $("#iq").value = ""; itemFilter = null; }
  if (b.dataset.m !== masterView) $("#ml-q").value = "";
  openMaster(b.dataset.m);
}));
const NATURE = { asset: "Assets", liability: "Liabilities", income: "Income", expense: "Expenses" };
const MASTER_LISTS = {
  "stock-groups": { title: "Stock group", path: "stock-groups", note: "Click a group for its stock items.",
    cols: [{ label: "Name", key: "name", wrap: true }, { label: "Under", get: (g) => g.parent || "Primary", wrap: true },
      { label: "HSN", key: "hsn_code" }, { label: "GST", num: true, get: (g) => (g.gst_rate == null ? "" : `${Number(g.gst_rate)}%`) },
      { label: "Stock items", num: true, key: "items" }],
    open: (g) => openItems("group", g.name, "Stock group") },
  "stock-categories": { title: "Stock category", path: "stock-categories", note: "Categories given to stock items in Tally. Click one for its items.",
    cols: [{ label: "Name", key: "name", wrap: true }, { label: "Stock items", num: true, key: "items" }],
    open: (c) => openItems("category", c.name, "Stock category") },
  units: { title: "UOM (units of measure)", path: "units", note: "Click a unit for the stock items that use it.",
    cols: [{ label: "Symbol", key: "symbol" }, { label: "Formal name", key: "formal_name", wrap: true },
      { label: "Decimals", num: true, key: "decimals" },
      { label: "Compound unit", get: (u) => (u.base_unit && u.additional_unit ? `${u.base_unit} of ${Number(u.conversion || 1)} ${u.additional_unit}` : "") },
      { label: "Stock items", num: true, key: "items" }],
    search: (u) => [u.symbol, u.formal_name],
    open: (u) => openItems("unit", u.symbol, "Unit") },
  "ledger-groups": { title: "Ledger group", path: "groups", note: "Click a group for its ledgers.",
    cols: [{ label: "Name", key: "name", wrap: true }, { label: "Under", get: (g) => g.parent || "Primary", wrap: true },
      { label: "Category", get: (g) => NATURE[g.nature] || g.nature },
      { label: "Affects gross profit", get: (g) => (g.affects_gross_profit ? "Yes" : "") },
      { label: "Ledgers", num: true, key: "ledgers" }],
    search: (g) => [g.name, g.parent, NATURE[g.nature]],
    open: (g) => { ledgerGroup = g.name; $("#lq").value = ""; openMaster("ledgers"); } },
  "ledger-categories": { title: "Ledger categories", path: "ledger-categories",
    note: "Every ledger group belongs to one of Tally's four kinds. Click one for its groups.",
    cols: [{ label: "Category", key: "name" }, { label: "Primary groups", get: (c) => c.primary_groups.join(", "), wrap: true },
      { label: "Groups", num: true, key: "groups" }, { label: "Ledgers", num: true, key: "ledgers" }],
    open: (c) => { $("#ml-q").value = c.name; openMaster("ledger-groups"); } },
};
async function renderMasterList() {
  const cfg = MASTER_LISTS[masterView];
  const key = `${state.company}:${masterView}`;
  if (!listCache[key]) listCache[key] = await api(`/companies/${state.company}/masters/${cfg.path}`);
  const all = listCache[key];
  const q = $("#ml-q").value.trim().toLowerCase();
  const rows = !q ? all : all.filter((r) => (cfg.search ? cfg.search(r) : [r.name, r.parent]).some((v) => (v || "").toLowerCase().includes(q)));
  $("#ml-title").textContent = cfg.title;
  $("#ml-note").textContent = `${rows.length} of ${all.length} · ${cfg.note}`;
  table($("#ml-table"), cfg.cols, rows, cfg.open);
}
let mlTimer; $("#ml-q").oninput = () => { clearTimeout(mlTimer); mlTimer = setTimeout(renderMasterList, 200); };

// BOM: every item that has one; an item expands into its BOM(s) and their components.
async function renderBom() {
  const q = $("#bq").value.trim();
  const rows = await api(`/companies/${state.company}/masters/bom${q ? `?q=${encodeURIComponent(q)}` : ""}`);
  const comps = rows.reduce((n, r) => n + r.boms.reduce((m, b) => m + b.components.length, 0), 0);
  $("#bcount").textContent = rows.length
    ? `${rows.length} item${rows.length === 1 ? "" : "s"} with a BOM · ${comps} component lines · click an item for its components`
    : (q ? "No BOM matches the search" : "No BOMs yet: click Read BOMs from Tally");
  const itemOpen = (name, id) => (id ? () => { $("#iq").value = name; itemFilter = null; openMaster("items"); } : undefined);
  const comp = (c) => ({ label: c.item, row: { group: c.item_group, primary: c.primary_group, nature: c.nature, qty: qtyText(c.qty, c.unit), godown: c.godown },
    open: itemOpen(c.item, c.item_id) });
  const roots = rows.map((r) => {
    const one = r.boms.length === 1 ? r.boms[0] : null;
    return {
      label: r.item,
      row: { group: r.item_group, primary: r.primary_group, bom: one ? one.name : `${r.boms.length} BOMs`, per: one ? qtyText(one.basic_qty, one.unit) : "",
             count: r.boms.reduce((m, b) => m + b.components.length, 0) },
      children: one ? one.components.map(comp)
        : r.boms.map((b) => ({ label: b.name || "(BOM)", row: { per: qtyText(b.basic_qty, b.unit), count: b.components.length },
                               children: b.components.map(comp) })),
    };
  });
  treeTable($("#btree"), [
    { label: "Item group", get: (x) => esc(x.row.group || "") },
    { label: "Primary group", get: (x) => esc(x.row.primary || "") },
    { label: "BOM", get: (x) => esc(x.row.bom || "") },
    { label: "For qty", num: true, get: (x) => x.row.per || "" },
    { label: "Components", num: true, get: (x) => x.row.count ?? "" },
    { label: "Nature", get: (x) => esc(x.row.nature || "") },
    { label: "Qty", num: true, get: (x) => x.row.qty || "" },
    { label: "Godown", get: (x) => esc(x.row.godown || "") },
  ], roots, { title: "Item / component", expandDepth: q ? 2 : 0 });
}
let bqTimer; $("#bq").oninput = () => { clearTimeout(bqTimer); bqTimer = setTimeout(() => renderBom().catch((e) => alert(e.message)), 250); };
$("#b-xlsx").onclick = () => download(`/companies/${state.company}/masters/bom/xlsx${$("#bq").value.trim() ? `?q=${encodeURIComponent($("#bq").value.trim())}` : ""}`, "Bill of materials.xlsx");
$("#b-refresh").onclick = async () => {
  const b = $("#b-refresh"); b.disabled = true; b.textContent = "Reading from Tally…";
  try {
    const r = await api(`/companies/${state.company}/masters/bom/refresh`, { method: "POST" });
    alert(`${r.items_with_bom} items have a BOM in Tally; ${r.updated} updated here.`
      + (r.not_in_software.length ? `\nNot in the software yet (sync masters first): ${r.not_in_software.slice(0, 10).join(", ")}` : ""));
    await renderBom();
  } catch (e) { alert(e.message); } finally { b.disabled = false; b.textContent = "Read BOMs from Tally"; }
};

async function renderLedgers() {
  const q = $("#lq").value;
  const p = new URLSearchParams();
  if (q) p.set("q", q);
  if (ledgerGroup) p.set("group", ledgerGroup);
  $("#l-filter").innerHTML = ledgerGroup ? `· group ${esc(ledgerGroup)} <button type="button" class="link" id="l-clear">show all</button>` : "";
  if ($("#l-clear")) $("#l-clear").onclick = () => { ledgerGroup = null; renderLedgers(); };
  const rows = q || ledgerGroup ? await api(`/companies/${state.company}/masters/ledgers?${p}`) : state.ledgers;
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
}
let lqTimer; $("#lq").oninput = () => { clearTimeout(lqTimer); lqTimer = setTimeout(renderLedgers, 250); };

// ------------------------------------------------------------------ stock items
const qty = (v, unit) => (v == null ? "" : `${Number(v).toLocaleString("en-IN", { maximumFractionDigits: 4 })} ${unit || ""}`.trim());
const pct = (v) => (v == null ? "" : `${Number(v)}%`);
const amt = (v) => (v == null ? null : money(v));
const conversionText = (i) => (i.alternate_unit && i.conversion ? `${Number(i.denominator || 1)} ${i.unit} = ${Number(i.conversion)} ${i.alternate_unit}` : "");
let itemsCache = null;
let itemFilter = null;   // { field, value, label } when opened from a stock group, category or unit
function openItems(field, value, label) { itemFilter = { field, value, label }; $("#iq").value = ""; openMaster("items"); }
async function renderItems() {
  if (!itemsCache || itemsCache.company !== state.company)
    itemsCache = { company: state.company, rows: await api(`/companies/${state.company}/masters/stock-items`) };
  const q = $("#iq").value.trim().toLowerCase();
  let rows = itemFilter ? itemsCache.rows.filter((i) => i[itemFilter.field] === itemFilter.value) : itemsCache.rows;
  if (q) rows = rows.filter((i) => [i.name, i.group, i.category, i.hsn_code, i.alias].some((v) => (v || "").toLowerCase().includes(q)));
  $("#icount").innerHTML = (itemFilter ? `${esc(itemFilter.label)}: <b>${esc(itemFilter.value)}</b> `
    + `<button type="button" class="link" id="i-clear">show all</button> · ` : "")
    + `${rows.length} of ${itemsCache.rows.length} items` + (rows.length > 1000 ? " (first 1,000 shown; search to narrow)" : "");
  if ($("#i-clear")) $("#i-clear").onclick = () => { itemFilter = null; renderItems(); };
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
}
let iqTimer; $("#iq").oninput = () => { clearTimeout(iqTimer); iqTimer = setTimeout(renderItems, 200); };

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
const CB = { enabled: "#c-enabled", freeze_outbound: "#c-fout", freeze_inbound: "#c-fin" };
const cMode = () => (document.querySelector('input[name="c-mode"]:checked') || {}).value || "one_way";
renderers.settings = async () => {
  let c = null;
  try { c = await api(`/sync/connectors/${state.company}`); } catch (_) { $("#c-msg").textContent = "Not configured yet"; }
  if (!c) return;
  for (const [k, s] of Object.entries(CF)) $(s).value = c[k] ?? "";
  for (const [k, s] of Object.entries(CB)) $(s).checked = !!c[k];
  const both = c.sync_mode === "bidirectional";
  $("#c-mode-both").checked = $("#c-mode-both").defaultChecked = both;
  $("#c-mode-one").checked = $("#c-mode-one").defaultChecked = !both;
  $("#c-waiting").textContent = c.waiting_for_tally
    ? `${c.waiting_for_tally} change(s) made in the software are waiting to be written into Tally.` : "";
  $("#c-ftypes").value = (c.frozen_voucher_types || []).join(", ");
};
$("#cform").onsubmit = async (e) => {
  e.preventDefault();
  const body = { frozen_voucher_types: $("#c-ftypes").value.split(",").map((s) => s.trim()).filter(Boolean) };
  for (const [k, s] of Object.entries(CF)) body[k] = $(s).value || null;
  for (const [k, s] of Object.entries(CB)) body[k] = $(s).checked;
  body.port = +body.port;
  body.sync_mode = cMode();
  if (body.sync_mode === "bidirectional" && !$("#c-mode-both").defaultChecked && !confirm("Bidirectional sync writes vouchers "
    + "into Tally. Use a test company until reconciliation is signed off. Continue?")) return;
  const save = async () => {
    const r = await api(`/sync/connectors/${state.company}`, { method: "PUT", body });
    await renderers.settings();
    $("#c-msg").innerHTML = `<span class="okmsg">Saved · ${r.sync_mode === "bidirectional" ? "bidirectional" : "one way (Tally → software)"}`
      + `${r.queued_changes_discarded ? ` · ${r.queued_changes_discarded} waiting change(s) discarded` : ""}</span>`;
  };
  try { await save(); }
  catch (err) {
    if (!/waiting for Tally/.test(err.message)) { $("#c-msg").innerHTML = `<span class="error">${esc(err.message)}</span>`; return; }
    if (confirm(`${err.message}\n\nOK: send them to Tally.\nCancel: choose whether to discard them.`)) body.queued_changes = "send";
    else if (confirm("Discard the waiting changes? They stay in the software but are not written into Tally.")) body.queued_changes = "discard";
    else return;
    try { await save(); } catch (e2) { $("#c-msg").innerHTML = `<span class="error">${esc(e2.message)}</span>`; }
  }
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

// System admin > Security: allow Tally on a public IP address (off by default; Tally's port has no password).
async function securityView() {
  const el = $("#sv-security");
  const s = await api("/admin/security");
  el.innerHTML = `<label class="row"><input type="checkbox" id="sec-public" ${s.allow_public_tally ? "checked" : ""}>
      <b>Allow Tally on a public IP address</b></label>
    <p class="small ${s.allow_public_tally ? "error" : "muted"}">${s.allow_public_tally
      ? `Allowed${s.updated_by ? ` by ${esc(s.updated_by)} ${when(s.updated_at)}` : ""}. Tally's port has no password: anyone on the internet who finds
         it can read and change those books. Allow only this server's address on the office router or firewall, or better, use a VPN or the Tally tunnel.`
      : "Off: Tally Cloud connects only to Tally on the office network (e.g. 192.168.x.x), a VPN or the Tally tunnel. Tally's port has no password, so it must not be open to the internet."}</p>
    ${s.public_connectors.length ? `<p class="small error">Companies linked to a public address: ${s.public_connectors.map((c) => esc(`${c.company} (${c.host}:${c.port})`)).join(", ")}</p>` : ""}
    <span id="sec-msg" class="small"></span>`;
  $("#sec-public").onchange = async (e) => {
    const on = e.target.checked;
    let confirm = null;
    if (on) {
      confirm = prompt("Tally's port has no password: anyone on the internet who finds it can read and change your books. "
        + "Type ALLOW to let Tally Cloud connect to Tally on a public IP address.");
      if (confirm === null) { e.target.checked = false; return; }
    }
    try { await api("/admin/security", { method: "PUT", body: { allow_public_tally: on, confirm } }); await securityView(); }
    catch (err) { e.target.checked = !on; $("#sec-msg").innerHTML = `<span class="error">${esc(err.message)}</span>`; }
  };
}

renderers.server = async () => {
  securityView().catch((err) => ($("#sv-security").innerHTML = `<p class="error">${esc(err.message)}</p>`));
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
    ["renew", "Renew"], ["suspend", "Suspend"], ["resume", "Resume"], ["new_activation_key", "New activation key"],
    ["reissue_key", "New key (servers keep working)"], ["cancel", "Cancel"], ["revoke", "Revoke"]]
    .map(([a, t]) => `<button type="button" class="secondary small ${a === "revoke" ? "danger" : ""}" data-lic="${l.id}" data-act="${a}">${t}</button>`).join("")}
    <button type="button" class="small" data-lic-edit="${l.id}">Change</button></div>` : "";
  const act = l.activation || { activated: l.active_installations > 0, label: l.active_installations ? "Activated" : "Not activated" };
  const keyRow = `<div class="row small"><span class="muted">Licence key</span> <span class="keyline" id="lk-${l.id}">${esc(l.key_hint)}</span>
    ${l.key_viewable ? `<button type="button" class="secondary small" data-showkey="${l.id}" data-admin="${admin ? 1 : 0}">Show key</button>`
      : `<span class="muted">(made before keys could be shown again${admin ? ": use New activation key" : ": ask Voyager for a new activation key"})</span>`}
    · <b class="${act.activated ? "okmsg" : "muted"}">${esc(act.label)}</b>${act.activated && act.since ? ` <span class="muted">since ${when(act.since)}${
      act.servers && act.servers.length ? ` on ${esc(act.servers.join(", "))}` : ""}</span>` : ""}</div>`;
  return `<div class="lic-box"><div class="row between"><h4>${esc(l.plan[0].toUpperCase() + l.plan.slice(1))} licence ${esc(l.key_hint)}</h4>
    <span>${chip(act.activated ? "activated" : "not activated")} ${chip(l.effective_status)}</span></div>
    ${keyRow}
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

document.addEventListener("click", async (e) => {   // Show key on a licence box (super admin or the customer's admin)
  const b = e.target.closest("[data-showkey]");
  if (!b) return;
  e.preventDefault();
  try {
    if (b.dataset.key) { await navigator.clipboard.writeText(b.dataset.key); b.textContent = "Copied"; return; }
    const r = await api(b.dataset.admin === "1" ? `/admin/licenses/${b.dataset.showkey}/key` : `/portal/licenses/${b.dataset.showkey}/key`);
    const el = document.getElementById(`lk-${b.dataset.showkey}`);
    if (el) el.textContent = r.license_key;
    b.dataset.key = r.license_key; b.textContent = "Copy";
  } catch (err) { alert(err.message); }
});

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
    $("#cd-key").innerHTML = `<div class="lic-box"><p><b>Licence key</b> (Show key on the licence brings it back later): give it to ${esc(c.admin_username)}.
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
      cancel: "Cancel this licence?", reissue_key: "Make a new licence key? The old key stops working; servers already activated keep working.",
      new_activation_key: "Release a new activation key? The old key stops working and every server activated with it is deactivated: it must be activated again with the new key." }[a];
    if (ask && !confirm(ask)) return;
    if (a === "new_activation_key") {
      const why = prompt("Reason for the new activation key (e.g. server replaced, reinstalled):", "");
      if (why === null) return;
      body.reason = why;
    }
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

// ------------------------------------------------------------------ Excel button on every table
// Each table gets an "Excel" button that downloads it as it is shown: the same columns, the rows that are
// open, headings bold on yellow, totals and groups bold, amounts as numbers, tree levels indented.
function tableTitle(t) {
  const crumb = t.closest("#r-out") && nav.length ? nav.map((n) => n.title).join(" - ") : "";
  let sub = "";
  for (let n = t; n && n !== document.body && !sub; n = n.parentElement) {
    if (n.tagName === "DETAILS") { sub = (n.querySelector(":scope > summary h3") || n.querySelector(":scope > summary"))?.textContent.trim() || ""; break; }
    for (let s = n.previousElementSibling; s && !sub; s = s.previousElementSibling) {
      const h = s.matches("h2,h3,h4") ? s : s.querySelector && s.querySelector("h2,h3,h4");
      if (h && h.textContent.trim()) sub = h.textContent.trim();
    }
    if (crumb && n.id === "r-body") break;
  }
  const tab = document.querySelector("#tabs button.active")?.textContent.trim() || "Table";
  return [crumb || (sub ? "" : tab), sub].filter(Boolean).join(" - ").replace(/\s+/g, " ").slice(0, 110) || tab;
}
function tableGrid(t) {
  const NUM = /^\(?-?[\d,]+(\.\d+)?\)?$/;
  const rows = [], widths = [];
  for (const tr of t.querySelectorAll(":scope > thead > tr, :scope > tbody > tr, :scope > tfoot > tr, :scope > tr")) {
    if (tr.hidden) continue;
    const head = tr.parentElement.tagName === "THEAD";
    const bold = head || /\b(total|group)\b/.test(tr.className) || tr.querySelector("th") !== null;
    const row = [];
    tr.querySelectorAll(":scope > td, :scope > th").forEach((td, i) => {
      const field = td.querySelector("select, input:not([type=checkbox]), textarea");
      let text = (field ? (field.tagName === "SELECT" ? field.selectedOptions[0]?.textContent : field.value) : td.innerText || "").replace(/\s+/g, " ").trim();
      if (i === 0 && td.querySelector(".tw")) {   // tree level as indentation
        const level = Math.max(0, Math.round((parseFloat(td.style.paddingLeft) - 8) / 18) || 0);
        text = " ".repeat(level * 3) + text.replace(/^[▸▾]\s*/, "");
      }
      let cell;
      if (td.classList.contains("num") && NUM.test(text) && !head) {
        const neg = text.startsWith("(") || text.startsWith("-");
        cell = { v: (neg ? "-" : "") + text.replace(/[(),-]/g, ""), s: bold ? "boldnum" : "num" };
      } else cell = text === "" && !head ? null : { v: text, s: head ? "head" : bold ? "bold" : null };
      row.push(cell);
      const span = +td.getAttribute("colspan") || 1;
      for (let k = 1; k < span; k++) row.push(head ? { v: "", s: "head" } : null);
      widths[row.length - span] = Math.max(widths[row.length - span] || 8, Math.min(60, text.length + 2));
    });
    rows.push(row);
  }
  return { rows, widths: widths.map((w) => w || 10) };
}
async function exportTable(t) {
  const title = tableTitle(t);
  const { rows, widths } = tableGrid(t);
  const res = await fetch(`${API}/export/table?${cq()}`, {
    method: "POST", headers: { ...authHeaders(), "Content-Type": "application/json" },
    body: JSON.stringify({ title, rows: [[{ v: title, s: "bold" }], [], ...rows], widths }),
  });
  if (!res.ok) return alert((await res.json().catch(() => ({}))).detail || res.statusText);
  const a = document.createElement("a"); a.href = URL.createObjectURL(await res.blob()); a.download = `${title.replace(/[\\/:*?"<>|]+/g, " ")}.xlsx`; a.click();
}
function addExcelButtons() {
  if (!state.company || !can("export")) return;
  document.querySelectorAll("main table:not([data-xl]), dialog table:not([data-xl])").forEach((t) => {
    if (t.closest("[data-no-xl]") || t.querySelector("table")) return;
    t.dataset.xl = "1";
    const b = document.createElement("button");
    b.type = "button"; b.className = "link"; b.title = "Download this table as shown, in Excel"; b.textContent = "⬇ Excel";
    b.onclick = (e) => { e.preventDefault(); e.stopPropagation(); exportTable(t).catch((err) => alert(err.message)); };
    const tools = t.previousElementSibling;
    if (tools && tools.classList.contains("tree-tools")) { tools.prepend(b); return; }   // beside Expand all
    const bar = document.createElement("div");
    bar.className = "xl-bar";
    bar.append(b);
    t.before(bar);
  });
}
let xlQueued = false;
new MutationObserver(() => {
  if (xlQueued) return;
  xlQueued = true;
  requestAnimationFrame(() => { xlQueued = false; addExcelButtons(); });
}).observe(document.body, { childList: true, subtree: true });

if (location.hash.startsWith("#setup=")) { signOut(); setupFromLink(location.hash.slice(7)); }
else if (state.token || state.key) start().catch(() => { signOut(); firstRunCheck(); });
else { signOut(); firstRunCheck(); }
