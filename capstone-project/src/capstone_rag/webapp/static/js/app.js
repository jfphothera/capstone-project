/* Bootstrap: navigation, the live event stream, and the status bar.
 *
 * The inbox updates from server-sent events rather than polling, so a
 * Critical ticket appears without the representative pressing anything, and
 * an investigation finishing refreshes the open case in place. */

import { api, REP } from "./api.js?v=rc28";
import { $, ask, attempt, el, render, toast } from "./dom.js?v=rc28";
import { initInbox, patchCase, refreshInbox, selectedCaseId, setSelected }
  from "./inbox.js?v=rc28";
import { renderConversation } from "./conversation.js?v=rc28";
import { renderCopilot, applyProgress } from "./copilot.js?v=rc28";
let wasDisconnected = false;
import { renderAudit, renderMovements, renderQuality, renderTrends }
  from "./views.js?v=rc28";

const workspace = $("#workspace");
const panels = {
  inbox: $("#panel-inbox"),
  conversation: $("#panel-conversation"),
  copilot: $("#panel-copilot"),
};
let fullview = null;
let currentView = "inbox";
let arrivalsRunning = false;

/* ------------------------------------------------------------ routing */

function showView(name) {
  currentView = name;
  for (const b of document.querySelectorAll("#nav button")) {
    b.classList.toggle("active", b.dataset.view === name);
    b.setAttribute('aria-pressed',String(b.dataset.view===name));
  }
  const workspaceView = name === "inbox";
  for (const p of Object.values(panels)) p.hidden = !workspaceView;
  if (fullview) { fullview.remove(); fullview = null; }
  if (workspaceView) {
    history.replaceState(null, "", selectedCaseId()
      ? `#case/${selectedCaseId()}` : "#inbox");
    return;
  }
  fullview = el("div.fullview", {}, el("div.empty", {}, "Loading…"));
  workspace.append(fullview);
  history.replaceState(null, "", `#${name}`);
  renderFullView(name);
}

async function renderFullView(name) {
  const again = () => renderFullView(name);
  const open = (caseId) => { showView("inbox"); openCase(caseId); };
  try {
    if (name === "trends") await renderTrends(fullview, again);
    else if (name === "movements") await renderMovements(fullview, again, open);
    else if (name === "quality") await renderQuality(fullview, again, open);
    else if (name === "audit") await renderAudit(fullview, again, open);
    else if (name === 'experiments') await (await import('./experiment_ui.js?v=rc28')).renderExperiments(fullview,again);
  } catch (err) {
    render(fullview, el("div.banner.critical", {},
      `Could not load this view: ${err.message}`));
  }
}

/* ------------------------------------------------------------- a case */

let openCaseId = null;
let loading = false;
let pendingReload = false;

export async function openCase(caseId) {
  if (!caseId) return;
  openCaseId = caseId;
  setSelected(caseId);
  setRegion('case');
  history.replaceState(null, "", `#case/${caseId}`);
  await loadCase();
}

async function loadCase() {
  if (!openCaseId) return;
  if (loading) { pendingReload=true; return; }
  loading = true;
  const requestedCase = openCaseId;
  try {
    const w = await api.case(requestedCase);
    if (requestedCase !== openCaseId) { pendingReload=true; return; }
    renderConversation(w, reloadCase);
    renderCopilot(w, reloadCase);
    lastAiState = w.case.ai_state;
    pollProgress(true);
  } catch (err) {
    render($("#conv-body"), el("div.banner.critical", {},
      `Could not load ${openCaseId}: ${err.message}`));
    render($("#copilot-body"), el("div.empty", {}, ""));
  } finally {
    loading = false;
    if (pendingReload) { pendingReload=false; await loadCase(); }
  }
}

/* rc.27: live progress of the selected ticket. A small read-only poll with an event cursor while its investigation
 * is queued or running (visible tab only); a late answer for another ticket is ignored; when the pass ends the
 * whole case is reloaded once. Never starts or resubmits work. */
let lastAiState = null, progressTimer = null, progressCursor = { caseId: null, after: 0 }, progressBusy = false;
async function pollProgress(fresh = false) {
  clearTimeout(progressTimer);
  const caseId = openCaseId;
  if (!caseId) return;
  if (fresh || progressCursor.caseId !== caseId) progressCursor = { caseId, after: 0 };
  if (document.hidden || progressBusy) { progressTimer = setTimeout(pollProgress, 3000); return; }
  progressBusy = true;
  try {
    const p = await api.progress(caseId, 0);   // the current pass is small; the cursor marks what is new
    if (caseId !== openCaseId) return;           // switched ticket: drop the late answer
    progressCursor.after = p.cursor;
    applyProgress(caseId, p);
    const running = ["queued", "processing", "retriable"].includes(p.ai_state);
    const wasRunning = ["queued", "processing", "retriable"].includes(lastAiState);
    if (wasRunning && !running) { lastAiState = p.ai_state; await loadCase(); return; }
    lastAiState = p.ai_state;
    if (running) progressTimer = setTimeout(pollProgress, 2500);
  } catch { if (caseId === openCaseId) progressTimer = setTimeout(pollProgress, 10000); }
  finally { progressBusy = false; }
}
document.addEventListener("visibilitychange", () => { if (!document.hidden && openCaseId) pollProgress(); });

async function reloadCase() {
  await Promise.all([loadCase(), refreshInbox()]);
}

/* --------------------------------------------------------- operations */

function wireOps() {
  $('#btn-manual').addEventListener('click',async()=>{
    const a=await ask({title:'Create a simulated customer ticket',message:'Use fictional customer details and a customer request. This enters the existing investigation queue; no live email is contacted.',label:'Fictional incoming message',confirm:'Create ticket'});
    if(!a)return;
    const out=await attempt(()=>api.createCase({subject:a.text.split('\n')[0].slice(0,110),body:a.text,channel:'email'}),{success:'Simulated ticket created; investigation queued.',onDone:refreshInbox});
    if(out)await openCase(out.case_id);
  });
  $("#btn-arrivals")?.addEventListener("click", async () => {
    const out = await attempt(() => arrivalsRunning ? api.pauseArrivals()
                                                    : api.startArrivals());
    if (out) applySimulator(out);
  });
  $("#btn-seed")?.addEventListener("click", () =>
    attempt(() => api.seed(10), {
      success: (o) => `Released ${o.released.length} tickets.`,
      onDone: refreshInbox,
    }));
  $("#btn-add")?.addEventListener("click", () =>
    attempt(() => api.releaseOne(), {
      success: (o) => o.case_id ? `Released ${o.case_id}.`
                                : "The ticket pool is empty.",
      onDone: refreshInbox,
    }));
}

function setRegion(region) {
  document.querySelector('.app').dataset.region=region;
  document.querySelector('#panel-copilot').classList.toggle('region-active',region==='copilot');
  for(const b of document.querySelectorAll('.region-switch button')) {
    b.classList.toggle('active',b.dataset.region===region);
    b.setAttribute('aria-pressed',String(b.dataset.region===region));
  }
}

function applySimulator(sim) {
  arrivalsRunning = !!sim.running;
  if($("#btn-arrivals")) $("#btn-arrivals").textContent = arrivalsRunning ? "Pause arrivals"
                                                   : "Start arrivals";
  $("#sb-sim").textContent =
    `arrivals ${arrivalsRunning ? "on" : "off"} · ` +
    `${sim.pool_available ?? "-"} in pool` +
    (sim.next_due_in != null ? ` · next in ${sim.next_due_in}s` : "");
}

function applyQueue(q) {
  $("#sb-queue").textContent =
    `queue ${q.queued} waiting · ${q.current ? "1 processing" : "idle"}` +
    ` · ${q.completed} done · ${q.failed} failed` +
    (q.dropped_full ? ` · ${q.dropped_full} deferred (queue full)` : "");
  const dot = $("#ops-dot");
  dot.className = "dot " + (q.current ? "busy" : q.running ? "live" : "off");
  $("#ops-text").textContent = q.current
    ? `investigating ${q.current}`
    : q.running ? "worker idle" : "worker stopped";
}

function applyModel(m) {
  $("#sb-model").textContent = m.available
    ? `model ${m.model || "configured"} · ${m.profile_name || ""}${m.prefer_llm ? "" : " (deterministic mode)"}`
    : "Gateway unavailable · ticket model work is paused";
}

/* ------------------------------------------------------------- events */

async function handleLiveEvent(kind,raw) {
  if(kind==='connection_error'){$("#ops-dot").className="dot off";$("#ops-text").textContent="reconnecting";wasDisconnected=true;return}
  const data=JSON.parse(raw);
  if(kind==='hello') {
    // rc.27: after a reconnect, catch the selected ticket up once (events may have been missed).
    if (wasDisconnected && openCaseId) { wasDisconnected=false; loadCase(); }
    const snap = data;
    applyQueue(snap.queue);
    applySimulator(snap.simulator);
    applyModel(snap.model);
    REP.name = snap.representative || REP.name;
    $("#sb-rep").textContent = `signed in as ${REP.name}`;
  } else if(kind==='heartbeat') {
    const d = data;
    applyQueue(d.queue);
    applySimulator(d.simulator);
  } else if(kind==='case_arrived') {
    const d = data;
    await refreshInbox();
    toast(`${d.priority} · ${d.subject || d.case_id}`,
          d.priority === "Critical" ? "critical" : "");
  } else if(kind==='case_updated') {
    const d = data;
    await refreshInbox();
    if (d.case_id === openCaseId) await loadCase();
  } else if(kind==='simulator') applySimulator(data);
  else if(kind==='reset') {
    openCaseId = null;
    setSelected(null);
    render($("#conv-body"), el("div.empty", {},
      "Select a case from the inbox."));
    render($("#copilot-body"), el("div.empty", {}, "No case selected."));
    await refreshInbox();
  }
}

function connectEvents() {
  if('SharedWorker' in window){
    const worker=new SharedWorker('/static/js/ops_shared_worker.js?v=rc28',{name:'capstone-ops-live-rc28'}),port=worker.port;
    port.onmessage=event=>handleLiveEvent(event.data.event,event.data.data).catch(()=>{});port.start();
    const visibility=()=>port.postMessage({type:'visibility',visible:!document.hidden});
    document.addEventListener('visibilitychange',visibility);visibility();
    window.addEventListener('pagehide',()=>port.postMessage({type:'disconnect'}),{once:true});
    return;
  }
  // Older browsers use finite polling rather than holding one HTTP/1.1
  // connection per tab. Hidden tabs make no requests.
  // rc.27: the fallback also refreshes the selected ticket, not only the inbox.
  const poll=async()=>{if(document.hidden)return;try{const value=await api.queue();applyQueue(value.queue);applySimulator(value.simulator);await refreshInbox();if(openCaseId)await loadCase()}catch{}};
  poll();setInterval(poll,15000);
}

/* --------------------------------------------------------------- init */

async function start() {
  initInbox(openCase);
  wireOps();
  // Original line icons; no corporate logo, icon font, or external dependency.
  const paths={inbox:'M3 4h18v15H3z M3 12h5l2 3h4l2-3h5',trends:'M4 19V5 M4 19h17 M7 14l4-5 4 3 6-8',movements:'M3 6h11v12H3z M14 10h4l3 4v4h-7 M6 18a2 2 0 1 0 4 0 M16 18a2 2 0 1 0 4 0',quality:'M12 3l8 3v6c0 5-8 9-8 9s-8-4-8-9V6z M12 8v5 M12 16h.01',audit:'M4 5h16v16H4z M8 3v4 M16 3v4 M8 12h8 M8 16h6'};
  for(const b of document.querySelectorAll('#nav button')){
    if(!paths[b.dataset.view])continue;
    const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 24 24');svg.setAttribute('class','icon');svg.setAttribute('aria-hidden','true');
    const path=document.createElementNS('http://www.w3.org/2000/svg','path');path.setAttribute('d',paths[b.dataset.view]);svg.append(path);b.prepend(svg);
  }
  document.querySelectorAll('[data-region]').forEach(b=>{
    if(b.tagName==='BUTTON') b.addEventListener('click',()=>setRegion(b.dataset.region));
  });
  for (const b of document.querySelectorAll("#nav button")) {
    b.addEventListener("click", () => {showView(b.dataset.view);b.closest("details")?.removeAttribute("open");});
  }

  try {
    const health = await api.health();
    REP.name = health.representative || REP.name;
    $("#sb-rep").textContent = `signed in as ${REP.name}`;
    applyQueue(health.queue);
    applySimulator(health.simulator);
    applyModel(health.model);
  } catch (err) {
    toast(`Cannot reach the API: ${err.message}`, "error");
  }

  await refreshInbox();
  await refreshQualityPip();

  const hash = location.hash.replace(/^#/, "");
  if (hash.startsWith("case/")) await openCase(hash.slice(5));
  else if (hash && hash !== "inbox") showView(hash);

  connectEvents();
  setInterval(()=>{if(!document.hidden)refreshQualityPip()},120000);
}

async function refreshQualityPip() {
  try {
    const data = await api.notifications({ status: "open" });
    const n = data.notifications.length;
    const pip = $("#pip-quality");
    pip.textContent = String(n);
    pip.classList.toggle("critical", data.notifications.some(
      (x) => x.kind === "adverse_event"));
    pip.hidden = n === 0;
  } catch { /* the status bar already reports a lost connection */ }
}

window.addEventListener('hashchange',()=>{
  const hash=location.hash.replace(/^#/,'');
  if(hash.startsWith('case/')) { showView('inbox'); openCase(hash.slice(5)); }
});
start();
