/* The inbox: one click opens a case.
 *
 * The old table was read-only with a separate dropdown to pick the same case
 * again (defect C11). Here the row *is* the control. Filtering, search and
 * sorting are done by the API, so the list the representative sees is the
 * list the server produced rather than a slice of it. */

import { api } from "./api.js?v=rc28";
import { $, chip, el, priorityClass, render, ago } from "./dom.js?v=rc28";

const state = {
  filters: { q: "", priority: "", sort: "priority", include_closed: false },
  cases: [],
  counts: {},
  selected: null,
};

let onSelect = () => {};

export function initInbox(handler) {
  onSelect = handler;
  const rerun = () => { readFilters(); refreshInbox(); };
  $("#inbox-search").addEventListener("input", debounce(rerun, 250));
  $("#inbox-priority").addEventListener("change", rerun);
  $("#inbox-sort").addEventListener("change", rerun);
  $("#inbox-status").addEventListener("change", rerun);
  $("#inbox-closed").addEventListener("change", rerun);
}

function readFilters() {
  state.filters.q = $("#inbox-search").value.trim();
  state.filters.priority = $("#inbox-priority").value;
  state.filters.sort = $("#inbox-sort").value;
  state.filters.include_closed = $("#inbox-closed").checked;
  state.filters.status = $("#inbox-status").value;
  if(['Resolved','Closed'].includes(state.filters.status)) state.filters.include_closed=true;
}

function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

export function selectedCaseId() {
  return state.selected;
}

export async function refreshInbox() {
  const data = await api.cases(state.filters);
  state.cases = data.cases;
  state.counts = data.counts;
  draw();
  return data;
}

export function setSelected(caseId) {
  state.selected = caseId;
  const current=state.cases.find(c=>c.case_id===caseId);
  if(current){seen[caseId]=current.updated_at;try{localStorage.setItem('luma-seen-tickets',JSON.stringify(seen));}catch{}}
  draw();
}

function draw() {
  const list = $("#inbox-list");
  const scroll = list.scrollTop;
  document.querySelector('.queue-caption span').textContent={priority:'Priority first',newest:'Newest first',updated:'Recently updated',oldest:'Oldest first'}[state.filters.sort]||'Priority first';
  $("#inbox-count").textContent = `${state.cases.length}`;
  const openCount = state.counts?._open ?? state.cases.length;
  const critical = state.counts?._critical_open ?? 0;
  const pip = $("#pip-inbox");
  pip.textContent = String(openCount);
  pip.classList.toggle("critical", critical > 0);

  if (!state.cases.length) {
    render(list, el("div.empty", {},
      "No matching cases. Try another search or filter."));
    return;
  }
  render(list, state.cases.map(row));
  list.scrollTop = scroll;
}

// Read markers are browser-local preferences; they never alter operational history.
let seen={}; try {seen=JSON.parse(localStorage.getItem('luma-seen-tickets')||'{}');} catch {}
function row(c) {
  const viewed=seen[c.case_id]===c.updated_at;
  const classes=['case-row'];
  if(c.case_id===state.selected)classes.push('selected');
  if(c.priority==='Critical')classes.push('critical');
  if(!viewed)classes.push('unread');
  const open=()=>{seen[c.case_id]=c.updated_at;try{localStorage.setItem('luma-seen-tickets',JSON.stringify(seen));}catch{} state.selected=c.case_id;draw();onSelect(c.case_id);};
  const extras=[c.is_adverse_event?'Safety notification':null,c.is_complaint?'Complaint':null,c.rma_outcome?`Movement ${c.rma_outcome}`:null,c.tracking_status?`Shipment: ${c.tracking_status.replaceAll('_',' ')}`:null,c.assigned_to?`Assigned: ${c.assigned_to}`:null,`Investigation: ${c.ai_state}`].filter(Boolean);
  const stateLabel=c.open_decisions?`${c.open_decisions} pending decision${c.open_decisions>1?'s':''}`:c.ai_state==='processing'?'Investigating':c.rma_needed?'Movement approval due':c.status;
  return el('article.'+classes.join('.'),{role:'button',tabindex:0,'aria-label':`${c.case_id}, ${c.priority} priority, ${c.subject}, ${stateLabel}`, 'aria-current':c.case_id===state.selected?'true':null,
    dataset:{caseId:c.case_id,priority:c.priority.toLowerCase()},onclick:open,onkeydown:e=>{if(e.target===e.currentTarget&&['Enter',' '].includes(e.key)){e.preventDefault();open();}}},
    el('div.line1',{},chip(c.priority,'priority '+priorityClass(c.priority)),el('span.id',{},c.case_id)),
    el('span.subject',{title:c.subject||'(no subject)'},!viewed?el('span.unread-dot',{'aria-label':'Not viewed at this update on this browser'}):null,c.subject||'(no subject)'),
    el('div.preview',{title:c.preview||''},c.preview||''),
    el('div.customer-line',{},el('span.customer-name',{title:c.customer_label||'Customer not matched'},c.customer_label||'Customer not matched'),el('span.pending-indicator',{title:stateLabel},c.open_decisions?`${c.open_decisions} decision${c.open_decisions>1?'s':''}`:stateLabel)),
    extras.length?el('details.row-details',{onclick:e=>e.stopPropagation(),onkeydown:e=>e.stopPropagation()},el('summary',{'aria-label':`More context for ${c.case_id}`,title:'More ticket context'},'…'),el('p',{},c.preview||''),el('p',{},extras.join(' · ')),el('small',{},`Updated ${ago(c.updated_at)}`)):null);
}

function aiChip(aiState) {
  const label = {
    queued: "AI queued",
    processing: "AI investigating",
    completed: "AI done",
    failed: "AI failed",
    retriable: "AI retrying",
  }[aiState] || aiState;
  const kind = {
    processing: "accent", completed: "ok", failed: "critical",
    retriable: "high",
  }[aiState] || "muted";
  return chip(label, kind);
}

/** Applied when a server event names a case that is already on screen, so a
 *  new arrival or a finished investigation shows without a manual refresh. */
export function patchCase(caseId, changes) {
  const found = state.cases.find((c) => c.case_id === caseId);
  if (!found) return false;
  Object.assign(found, changes);
  draw();
  return true;
}
