// Gateway Models screen. The server (gateway_models.py) does every change through the Gateway's own
// tools; this page only shows the state and sends the desired state. The token arrives in the address
// fragment, moves to sessionStorage and leaves the address bar.
"use strict";
(function () {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const hash = new URLSearchParams(location.hash.slice(1));
  if (hash.get("token")) { sessionStorage.setItem("gm-token", hash.get("token")); history.replaceState(null, "", location.pathname); }
  const token = sessionStorage.getItem("gm-token") || "";

  let view = null;          // what the Gateway has now
  let desired = null;       // what the operator has ticked
  let retest = new Set();   // "rid|model" pairs to test again although their last test failed
  const key = (rid, m) => rid + "|" + m;
  const short = (addr) => "." + String(addr).split(".").pop();

  async function api(path, body) {
    const r = await fetch(path, { method: body === undefined ? "GET" : "POST", headers: { "X-Console-Token": token, "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body) });
    const j = await r.json().catch(() => ({ error: "no answer from the screen's server" }));
    if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
    return j;
  }
  function banner(text) { const b = $("banner"); b.textContent = text || ""; b.hidden = !text; }

  function fromView(v) {
    const d = { apps: {}, blocked: new Set(), flags: new Set() };
    for (const o of v.orchestrators) d.apps[o.app_id] = new Set(o.models);
    for (const r of v.models) {
      if (r.available_to_all) d.flags.add(r.model);
      for (const [rid, s] of Object.entries(r.cells)) if (s === "blocked") d.blocked.add(key(rid, r.model));
    }
    // A model marked Available to all is offered to an orchestrator that does not have it yet (a pending change),
    // except to one whose models are granted one at a time (explicit_only: Coder).
    const explicit = v.explicit_only || {};
    for (const m of d.flags) for (const a of Object.keys(d.apps)) if (!explicit[a]) d.apps[a].add(m);
    return d;
  }
  function desiredBody() {
    const apps = {}; for (const [a, s] of Object.entries(desired.apps)) apps[a] = [...s].sort();
    return { desired: { apps, blocked: [...desired.blocked].map((k) => k.split("|")), flags: [...desired.flags].sort() }, retest: [...retest].map((k) => k.split("|")) };
  }
  function countChanges() {
    if (!view) return 0;
    let n = 0;
    for (const o of view.orchestrators) { const now = new Set(o.models), want = desired.apps[o.app_id]; for (const m of want) if (!now.has(m)) n++; for (const m of now) if (!want.has(m)) n++; }
    const nowBlocked = new Set(); for (const r of view.models) for (const [rid, s] of Object.entries(r.cells)) if (s === "blocked") nowBlocked.add(key(rid, r.model));
    for (const k of desired.blocked) if (!nowBlocked.has(k)) n++;
    for (const k of nowBlocked) if (!desired.blocked.has(k)) n++;
    const nowFlags = new Set(view.models.filter((r) => r.available_to_all).map((r) => r.model));
    for (const m of desired.flags) if (!nowFlags.has(m)) n++;
    for (const m of nowFlags) if (!desired.flags.has(m)) n++;
    return n;
  }

  function stateTitle(s, model, h) {
    return ({ usable: `${model} passed the Gateway's test on ${h.address} and is in use there`,
      offline: `${model} passed on ${h.address}, but the head is offline now`,
      untested: `${model} is installed on ${h.address} but not tested yet: Apply tests it if an orchestrator may use it`,
      failed: `${model} failed the Gateway's test on ${h.address}`,
      lapsed: `${model} on ${h.address} passed before, but the model or Ollama changed there: Apply tests it again`,
      blocked: `Do not use: ${model} is never used on ${h.address}`,
      absent: `${model} is not installed on ${h.address}`,
      "not-enrolled": `${h.address} is not in the Gateway yet (Audit heads > Add to Gateway)` })[s] || s;
  }
  const SYMBOL = { usable: "✓", offline: "✓", untested: "○", failed: "✗", lapsed: "↻", blocked: "⛔", absent: "–", "not-enrolled": "·" };

  function render() {
    const g = $("grid");
    if (!view) { g.innerHTML = ""; return; }
    const apps = view.orchestrators, heads = view.heads;
    const showEmbed = $("show-embed").checked, onlyRelevant = $("only-relevant").checked;
    const rows = view.models.filter((r) => (showEmbed || r.chat) && (!onlyRelevant || r.allowed.length || desired.flags.has(r.model) || [...Object.values(desired.apps)].some((s) => s.has(r.model)) || Object.values(r.cells).some((s) => s !== "absent")));
    let h = `<thead><tr><th rowspan="2">Model</th><th class="group sep" colspan="${apps.length + 1}">ORCHESTRATORS MAY USE</th><th class="group sep" colspan="${heads.length || 1}">ON EACH HEAD</th></tr><tr>`;
    apps.forEach((o, i) => { h += `<th class="app${i ? "" : " sep"}" title="${esc(o.app_id)}${o.enabled ? "" : " (credential revoked)"}">${esc(o.app_id)}${o.enabled ? "" : " (revoked)"}</th>`; });
    h += `<th class="app" title="Every orchestrator, including ones added later">All</th>`;
    if (!heads.length) h += `<th class="head sep">no head yet</th>`;
    heads.forEach((hd, i) => {
      const cls = !hd.in_gateway ? "out" : hd.online ? "" : "off";
      const HEALTH = { healthy: "online", degraded: "nothing usable", unqualified: "nothing tested", maintenance: "testing", quarantined: "QUARANTINED", offline: "offline" };
      const sub = !hd.in_gateway ? "not in Gateway" : hd.online ? (HEALTH[hd.health] || hd.health) : "offline";
      h += `<th class="head ${cls}${i ? "" : " sep"}" title="${esc(hd.resource_id)} · Ollama ${esc(hd.ollama || "?")} · models from ${esc(hd.models_source)}">${esc(short(hd.address))}<small>${esc(sub)}</small></th>`;
    });
    h += `</tr></thead><tbody>`;
    for (const r of rows) {
      h += `<tr><td class="name">${esc(r.model)}${r.note ? `<small>${esc(r.note)}</small>` : ""}</td>`;
      apps.forEach((o, i) => {
        const on = desired.apps[o.app_id].has(r.model), was = r.allowed.includes(o.app_id);
        const only = (view.explicit_only || {})[o.app_id], locked = only && !was;
        h += `<td class="${i ? "" : "sep"}${on !== was ? " changed" : ""}"><input type="checkbox" data-app="${esc(o.app_id)}" data-model="${esc(r.model)}" ${on ? "checked" : ""} ${locked ? "disabled" : ""} title="${esc(locked ? only : `${o.app_id} may use ${r.model}`)}"></td>`;
      });
      const flag = desired.flags.has(r.model);
      h += `<td class="allc${flag !== r.available_to_all ? " changed" : ""}"><input type="checkbox" data-all="${esc(r.model)}" ${flag ? "checked" : ""} title="Available to all: every orchestrator, including ones added later"></td>`;
      if (!heads.length) h += `<td class="sep dim">–</td>`;
      heads.forEach((hd, i) => {
        const s0 = r.cells[hd.resource_id] || "absent";
        const k = key(hd.resource_id, r.model), wantBlocked = desired.blocked.has(k), isBlocked = s0 === "blocked";
        const installed = hd.models.includes(r.model);
        const shown = wantBlocked ? "blocked" : (isBlocked ? (installed ? "untested" : "absent") : s0);
        const pending = wantBlocked !== isBlocked;
        const can = hd.in_gateway && (installed || isBlocked);
        const rt = s0 === "failed" && retest.has(k) ? " (will be tested again)" : "";
        const unused = (shown === "usable" || shown === "offline") && !Object.values(desired.apps).some((s) => s.has(r.model));
        const tip = stateTitle(shown, r.model, hd) + (unused ? " (passed here, but no orchestrator may use it)" : "") + rt + (can ? (wantBlocked ? " · click to allow it here again" : " · click to mark Do not use here") : "");
        h += `<td class="${i ? "" : "sep"}"><button class="c ${shown}${unused ? " unused" : ""}${pending ? " pending" : ""}" data-rid="${esc(hd.resource_id)}" data-model="${esc(r.model)}" ${can ? "" : "disabled"} title="${esc(tip)}">${SYMBOL[shown] || "?"}</button></td>`;
      });
      h += `</tr>`;
    }
    if (!rows.length) h += `<tr><td colspan="${apps.length + 2 + Math.max(1, heads.length)}" class="dim">No model to show. Click Audit heads.</td></tr>`;
    g.innerHTML = h + `</tbody>`;
    const n = countChanges();
    $("changes").textContent = n ? `${n} change${n > 1 ? "s" : ""} not applied yet.` : "No changes.";
    $("changes").className = n ? "some" : "";
    // Review stays available: with no change it still lists what would be tested, and failed tests can be retried there.
    $("review").disabled = false;
    $("undo").disabled = !n && !retest.size;
    $("review").textContent = n || retest.size ? "Review and apply…" : needsTests() ? "Test new models…" : "Review…";
  }
  function needsTests() {
    // Allowed models installed on a head in the Gateway but never tested there, or needing a new test.
    if (!view) return false;
    const allowed = new Set(); for (const s of Object.values(desired.apps)) for (const m of s) allowed.add(m);
    return view.models.some((r) => allowed.has(r.model) && Object.entries(r.cells).some(([rid, s]) => (s === "untested" || s === "lapsed") && !desired.blocked.has(key(rid, r.model)) && (view.heads.find((x) => x.resource_id === rid) || {}).online));
  }

  $("grid").addEventListener("change", (e) => {
    const t = e.target;
    const explicit = (view && view.explicit_only) || {};
    if (t.dataset.app) { const s = desired.apps[t.dataset.app]; t.checked ? s.add(t.dataset.model) : s.delete(t.dataset.model); if (!t.checked && !explicit[t.dataset.app]) desired.flags.delete(t.dataset.model); }
    else if (t.dataset.all) { const m = t.dataset.all; if (t.checked) { desired.flags.add(m); for (const [a, s] of Object.entries(desired.apps)) if (!explicit[a]) s.add(m); } else desired.flags.delete(m); }
    render();
  });
  $("grid").addEventListener("click", (e) => {
    const b = e.target.closest("button.c"); if (!b || b.disabled) return;
    const k = key(b.dataset.rid, b.dataset.model);
    desired.blocked.has(k) ? desired.blocked.delete(k) : desired.blocked.add(k);
    render();
  });
  $("show-embed").addEventListener("change", render);
  $("only-relevant").addEventListener("change", render);
  $("undo").addEventListener("click", () => { desired = fromView(view); retest = new Set(); render(); });

  // The system health view: the Gateway, its heads, their tests and Do not use, from the same read.
  function health(v) {
    const heads = v.heads.filter((h) => h.in_gateway);
    const healthy = heads.filter((h) => h.health === "healthy" && h.online).length;
    const offline = heads.filter((h) => !h.online).map((h) => h.address.split(".").pop());
    const unhealthy = heads.filter((h) => h.online && h.health !== "healthy").map((h) => `${h.address.split(".").pop()} (${h.health})`);
    const failed = [], blocked = [], usable = new Set();
    for (const r of v.models) for (const [rid, s] of Object.entries(r.cells)) {
      const n = rid.split("-").pop();
      if (s === "failed") failed.push(`${r.model} on ${n}`);
      if (s === "blocked") blocked.push(`${r.model} on ${n}`);
      if (s === "usable") usable.add(r.model);
    }
    const tile = (title, value, cls, detail) => `<div class="tile ${cls}"><b>${value}</b><span>${title}</span>${detail ? `<small>${detail}</small>` : ""}</div>`;
    const esc = (t) => String(t).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
    $("health").innerHTML = [
      tile("admission", v.gateway.admission_open ? "open" : "CLOSED", v.gateway.admission_open ? "up" : "down", "the Gateway accepts work"),
      tile("heads healthy", `${healthy} / ${heads.length}`, healthy === heads.length && heads.length ? "up" : "warn",
        esc([offline.length ? "offline: " + offline.join(", ") : "", unhealthy.length ? "not healthy: " + unhealthy.join(", ") : ""].filter(Boolean).join(" · ") || "all reporting")),
      tile("heads not in the Gateway", String(v.heads.length - heads.length), v.heads.length - heads.length ? "warn" : "up", "found by Audit; add them in the admin hub's Fleet menu"),
      tile("models usable now", String(usable.size), usable.size ? "up" : "warn", ""),
      tile("failed tests", String(failed.length), failed.length ? "warn" : "up", esc(failed.slice(0, 4).join(", ") + (failed.length > 4 ? " …" : ""))),
      tile("Do not use", String(blocked.length), blocked.length ? "warn" : "up", esc(blocked.slice(0, 4).join(", ") + (blocked.length > 4 ? " …" : ""))),
    ].join("");
  }

  function stamp(v) {
    health(v);
    $("hub").textContent = v.computer + " · 192.0.2.254";
    const gw = $("gw");
    gw.textContent = `Gateway ${v.gateway.release} · ledger ${String(v.gateway.ledger_id || "?").slice(0, 15)}… · admission ${v.gateway.admission_open ? "open" : "CLOSED"}`;
    gw.className = "pill " + (v.gateway.admission_open ? "up" : "warn");
    $("updated").textContent = "read " + new Date().toLocaleTimeString() + (v.scan_at ? " · heads audited " + new Date(v.scan_at * 1000).toLocaleTimeString() : " · heads: Gateway's view (click Audit heads for a live look)");
  }
  async function load(keepDesired) {
    try {
      const v = await api("/api/state");
      const pending = keepDesired && desired && countChanges();
      view = v;
      if (!pending) desired = fromView(v);
      stamp(v); banner(""); render(); renderInstalls();
      if (v.job && v.job.running) watchJob();
    } catch (err) { banner("Could not read the Gateway: " + err.message); }
  }

  // ---- Apply
  function li(items) { return items.length ? "<ul>" + items.map((x) => `<li>${x}</li>`).join("") + "</ul>" : ""; }
  async function review() {
    let plan;
    try { plan = await api("/api/plan", desiredBody()); } catch (err) { banner(err.message); return; }
    const addr = (rid) => short((view.heads.find((h) => h.resource_id === rid) || {}).address || rid);
    let h = "";
    if (plan.problems.length) h += `<p class="notice error">${plan.problems.map(esc).join("<br>")}</p>`;
    const pol = Object.entries(plan.policy);
    if (pol.length) h += `<h2>Allowed models</h2>` + li(pol.map(([a, c]) => `<b>${esc(a)}</b>: ${c.add.map((m) => `<span class="ok">+ ${esc(m)}</span>`).concat(c.remove.map((m) => `<span class="bad">− ${esc(m)}</span>`)).join(", ")}<br><span class="dim">will use: ${esc(c.models.join(", "))}</span>`));
    if (plan.block.length) h += `<h2>Do not use</h2>` + li(plan.block.map(([rid, m]) => `<span class="bad">⛔ ${esc(m)} on ${esc(addr(rid))}</span> <span class="dim">(stops at once; work already running there finishes)</span>`));
    if (plan.unblock.length) h += `<h2>Allow again</h2>` + li(plan.unblock.map(([rid, m]) => `${esc(m)} on ${esc(addr(rid))} <span class="dim">(tested again first)</span>`));
    if (plan.qualify.length) h += `<h2>Test on the heads (${plan.qualify.length}, about ${Math.max(1, Math.round(plan.qualify.length * 15 / 60))} min)</h2><p class="hint">Real inference on each head at 8192 tokens; a model is usable only after it passes. Each head is paused for its own test.</p>` + li(plan.qualify.map(([rid, m]) => `${esc(m)} on ${esc(addr(rid))}`));
    if (plan.skipped.length) h += `<h2>Not tested</h2>` + li(plan.skipped.map((s) => `${esc(s.model)} on ${esc(addr(s.resource_id))}: ${esc(s.reason)}` + (s.reason.indexOf("failed") >= 0 ? ` <label><input type="checkbox" data-retest="${esc(s.resource_id + "|" + s.model)}"> Retest</label>` : "")));
    const fl = plan.flags.filter((m) => !view.models.some((r) => r.model === m && r.available_to_all)), unfl = view.models.filter((r) => r.available_to_all && !plan.flags.includes(r.model)).map((r) => r.model);
    if (fl.length || unfl.length) h += `<h2>Available to all</h2>` + li(fl.map((m) => `<span class="ok">${esc(m)}</span>: every orchestrator, also later ones`).concat(unfl.map((m) => `${esc(m)}: no longer automatic for new orchestrators`)));
    if (!h) h = `<p>Nothing to change.</p>`;
    $("plan-body").innerHTML = h;
    $("plan-body").querySelectorAll("[data-retest]").forEach((c) => c.addEventListener("change", () => { c.checked ? retest.add(c.dataset.retest) : retest.delete(c.dataset.retest); review(); }));
    for (const k of retest) { const c = $("plan-body").querySelector(`[data-retest="${CSS.escape(k)}"]`); if (c) c.checked = true; }
    $("plan-apply").disabled = !!plan.problems.length || plan.empty;
    $("plan-apply").hidden = false; $("plan-cancel").textContent = "Cancel";
    if (!$("plan-dlg").open) $("plan-dlg").showModal();
  }
  $("review").addEventListener("click", review);
  $("plan-apply").addEventListener("click", async () => {
    $("plan-apply").disabled = true;
    try { await api("/api/apply", desiredBody()); retest = new Set(); watchJob(); }
    catch (err) { $("plan-body").insertAdjacentHTML("afterbegin", `<p class="notice error">${esc(err.message)}</p>`); $("plan-apply").disabled = false; }
  });
  let watching = false;
  async function watchJob() {
    if (watching) return; watching = true;
    if (!$("plan-dlg").open) $("plan-dlg").showModal();
    $("plan-apply").hidden = true; $("plan-cancel").textContent = "Close";
    try {
      for (;;) {
        const { job } = await api("/api/job");
        if (!job) break;
        $("plan-body").innerHTML = `<h2>${esc(job.title)}${job.running ? " — running" : ""}</h2><pre class="log">${esc(job.lines.join("\n"))}</pre>` +
          (job.running ? `<p class="hint">You can close this; it keeps running. Do not close the black window.</p>` :
            job.result && job.result.ok ? `<p class="notice">Finished. The screen now shows what the Gateway has.</p>` : `<p class="notice error">Finished with problems: see the lines above.</p>`);
        const log = $("plan-body").querySelector(".log"); if (log) log.scrollTop = log.scrollHeight;
        if (!job.running) break;
        await new Promise((r) => setTimeout(r, 1000));
      }
    } catch (err) { banner(err.message); }
    watching = false;
    await load(false);
  }

  // ---- Audit and heads
  async function audit() {
    const b = $("audit"); b.disabled = true; b.textContent = "Auditing…";
    try {
      const r = await api("/api/audit", {});
      const pending = desired && countChanges(); view = r.view; if (!pending) desired = fromView(view);
      stamp(view); render(); renderInstalls(); showHeads(r.found);
    } catch (err) { banner("Audit failed: " + err.message); }
    b.disabled = false; b.textContent = "Audit heads";
  }
  function showHeads(found) {
    let h = `<p class="hint">Asked every address 192.0.2.2–199 for Ollama just now: ${found} answered. A head must be in the Gateway before any orchestrator can use it.</p>`;
    h += `<table class="facts"><thead><tr><th>Head</th><th>Gateway</th><th>Answers now</th><th>Ollama</th><th>Models</th><th></th></tr></thead><tbody>`;
    for (const hd of view.heads) {
      const chat = hd.models.filter((m) => !/embed|minilm/i.test(m));
      const usable = hd.qualified.length ? `<br><span class="ok">usable: ${esc(hd.qualified.join(", "))}</span>` : "";
      h += `<tr><td class="addr">${esc(hd.address)}</td><td>${hd.in_gateway ? `<span class="${hd.health === "healthy" ? "ok" : "warn"}">in, ${esc(hd.health)}</span>` : `<span class="warn">not in the Gateway</span>`}</td>` +
        `<td>${hd.reachable === false ? `<span class="bad">no</span>` : hd.reachable ? `<span class="ok">yes</span>` : "?"}</td><td>${esc(hd.ollama || "")}</td>` +
        `<td class="models">${chat.length} chat model(s): ${esc(chat.join(", "))}${usable}</td>` +
        `<td>${!hd.in_gateway && hd.reachable ? `<button class="small primary" data-enrol="${esc(hd.address)}">Add to Gateway</button>` : ""}</td></tr>`;
    }
    h += `</tbody></table>`;
    $("heads-body").innerHTML = h;
    $("heads-body").querySelectorAll("[data-enrol]").forEach((b) => b.addEventListener("click", async () => {
      if (!confirm(`Add ${b.dataset.enrol} to the Gateway for every orchestrator, then test the allowed models installed on it?`)) return;
      try { await api("/api/enrol", { address: b.dataset.enrol }); $("heads-dlg").close(); watchJob(); } catch (err) { alert(err.message); }
    }));
    if (!$("heads-dlg").open) $("heads-dlg").showModal();
  }
  $("audit").addEventListener("click", audit);

  // ---- Install notes
  function renderInstalls() {
    if (!view) return;
    const list = view.installs || [];
    $("inst-list").innerHTML = list.length ? `<table class="facts"><thead><tr><th>Model</th><th>Heads</th><th>Asked</th></tr></thead><tbody>` + list.map((r) =>
      `<tr><td class="addr">${esc(r.model)}</td><td>${Object.entries(r.status).map(([a, s]) => `<span class="${s === "installed" ? "ok" : "warn"}">${esc(short(a))} ${s === "installed" ? "✓ installed" : "waiting"}</span>`).join(" · ")}</td><td class="dim">${esc(r.asked_at)}</td></tr>`).join("") +
      `</tbody></table><div class="row end"><button id="inst-clear" class="small">Remove finished</button></div>` : `<p class="dim">Nothing asked for.</p>`;
    const c = $("inst-clear"); if (c) c.addEventListener("click", async () => { try { view = await api("/api/install/clear", {}); renderInstalls(); } catch (err) { alert(err.message); } });
    $("check-installs").textContent = list.some((r) => !r.done) ? `Check installs (${list.filter((r) => !r.done).length})` : "Check installs";
  }
  $("install-open").addEventListener("click", () => {
    const heads = view ? view.heads : [];
    $("inst-heads").innerHTML = heads.length ? heads.map((h) => `<label class="head"><input type="checkbox" value="${esc(h.address)}" checked> ${esc(h.address)}</label>`).join("") : `<span class="dim">No head known yet: click Audit heads first.</span>`;
    $("inst-result").hidden = true; renderInstalls(); $("install-dlg").showModal(); $("inst-model").focus();
  });
  $("inst-make").addEventListener("click", async () => {
    const model = $("inst-model").value.trim();
    const heads = [...$("inst-heads").querySelectorAll("input:checked")].map((c) => c.value);
    try {
      const r = await api("/api/install", { model, heads });
      $("inst-cmd").textContent = "ollama pull " + model; $("inst-note").textContent = r.note; $("inst-file").textContent = "Saved on the hub: " + r.file;
      $("inst-result").hidden = false; await load(true);
    } catch (err) { alert(err.message); }
  });
  $("inst-copy").addEventListener("click", () => navigator.clipboard.writeText($("inst-cmd").textContent).then(() => { $("inst-copy").textContent = "Copied"; setTimeout(() => ($("inst-copy").textContent = "Copy"), 1500); }));
  $("check-installs").addEventListener("click", async () => {
    const b = $("check-installs"); b.disabled = true; b.textContent = "Checking…";
    try { const r = await api("/api/audit", {}); const pending = desired && countChanges(); view = r.view; if (!pending) desired = fromView(view); stamp(view); render(); renderInstalls(); if (!$("install-dlg").open) $("install-dlg").showModal(); $("inst-result").hidden = true; }
    catch (err) { banner(err.message); }
    b.disabled = false; renderInstalls();
  });

  document.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", () => b.closest("dialog").close()));
  $("close").addEventListener("click", async () => {
    if (countChanges() && !confirm("You have changes that are not applied. Close anyway?")) return;
    try { await api("/api/close", {}); } catch (err) { /* already closing */ }
    document.body.innerHTML = `<main><p class="notice">Gateway Models is closed. Open it again from its shortcut.</p></main>`;
  });

  if (!token) banner("Open this screen from its shortcut (Gateway Models on the desktop).");
  load(false);
  setInterval(() => { if (!document.querySelector("dialog[open]")) load(true); }, 30000);
  // Keeps the screen's server alive while this page is open, whatever dialog is showing.
  setInterval(() => { api("/api/ping").catch(() => banner("The screen's server has stopped. Open Gateway Models again from its shortcut.")); }, 60000);
})();
