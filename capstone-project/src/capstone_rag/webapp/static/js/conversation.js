/* The centre column: the customer, the correspondence and the reply.
 *
 * The composer is editable and the draft is never sent by anything except an
 * explicit Approve and send. Rejecting a draft asks for the reason in the
 * representative's own words and that text is what the agent redrafts from,
 * so a correction is a correction rather than a note. A revision that cannot
 * be verified against the sources stays a draft and says so. */

import { api } from "./api.js?v=rc28";
import { ask, attempt, chip, el, priorityClass, render, toast, when, renderKeeping }
  from "./dom.js?v=rc28";
import {simulatedReplyPanel,operationalPanel,feedbackPanel} from './workflow.js?v=rc28';
import {documentationPanel,coordinationActions} from './documentation.js?v=rc28';
import {caseDecisionPanel} from './copilot.js?v=rc28';

let latest=null,refreshCurrent=null,activeTab='conversation',currentCase=null;
const replyEdits=new Map();
function rememberReply(){
 const area=document.querySelector('#conv-composer textarea');
 if(area?.dataset.messageId)replyEdits.set(area.dataset.messageId,{value:area.value,server:area.dataset.serverBody});
}
export function renderConversation(w, refresh) {
  rememberReply(); latest=w;refreshCurrent=refresh;
  if(currentCase!==w.case.case_id){currentCase=w.case.case_id;activeTab=!w.draft&&(w.coordination?.contact_tasks||[]).length?'notes':'conversation';}
  const body=document.querySelector('#conv-body'),composer=document.querySelector('#conv-composer');
  render(document.querySelector('#conv-actions'),caseActions(w,refresh));
  document.querySelector('.case-menu').hidden=false;
  document.querySelector('.case-toolbar .eyebrow').textContent=w.case.case_id;
  render(document.querySelector('#case-overview'),header(w),banners(w));
  document.querySelectorAll('#case-tabs [data-tab]').forEach(b=>{b.disabled=false;b.setAttribute('aria-selected',String(b.dataset.tab===activeTab));b.tabIndex=b.dataset.tab===activeTab?0:-1;});
  body.setAttribute('aria-labelledby','tab-'+activeTab);
  if(activeTab==='notes')render(body,documentationPanel(w,refresh));
  else if(activeTab==='actions')renderKeeping(body,caseDecisionPanel(w,refresh),operationalPanel(w,refresh),coordinationActions(w,refresh),feedbackPanel(w,refresh));
  else if(activeTab==='history')render(body,historyPanel(w));
  else renderKeeping(body,messages(w),simulatedReplyPanel(w,refresh));
  composer.hidden=activeTab!=='conversation';
  const edit=replyEdits.get(w.draft?.message_id);
  // A changed server draft starts a fresh edit; ordinary live updates preserve local typing.
  const edited=edit?.server===(w.draft?.body||'')?edit.value:null;
  if(activeTab==='conversation') render(composer,w.case.is_closed?closedFooter(w,refresh):composerFor(w,refresh,edited));
}
function selectTab(tab,focus=false){
 if(!latest||!['conversation','notes','actions','history'].includes(tab))return;
 activeTab=tab;renderConversation(latest,refreshCurrent);
 if(focus)document.querySelector('#tab-'+tab).focus();
}
document.querySelector('#case-tabs').addEventListener('click',e=>{const b=e.target.closest('[data-tab]');if(b)selectTab(b.dataset.tab);});
document.querySelector('#case-tabs').addEventListener('keydown',e=>{
 const tabs=['conversation','notes','actions','history'];let next=tabs.indexOf(activeTab);
 if(e.key==='ArrowRight')next=(next+1)%4;else if(e.key==='ArrowLeft')next=(next+3)%4;else if(e.key==='Home')next=0;else if(e.key==='End')next=3;else return;
 e.preventDefault();selectTab(tabs[next],true);
});
document.addEventListener('case-section',e=>{
 selectTab(e.detail,true);document.querySelector('.app').dataset.region='case';document.querySelector('#panel-copilot').classList.remove('region-active');
 document.querySelectorAll('.region-switch button').forEach(b=>{b.classList.toggle('active',b.dataset.region==='case');b.setAttribute('aria-pressed',String(b.dataset.region==='case'));});
});
function historyPanel(w){
 return el('section',{},el('h3',{},'Case history'),el('p.history-summary',{},'Rejected drafts, internal messages and system events are retained here. Customer correspondence stays in Conversation.'),
 (w.messages||[]).filter(m=>m.direction==='internal'||m.status==='rejected'||m.channel==='qa_notification').map(m=>message(m,{...w,draft:null})),
 (w.notifications||[]).map(n=>el('article.case-note',{},el('small',{},`Quality notification · ${when(n.created_at)} · ${n.status}`),el('p',{},n.body),n.determination?el('p',{},`${n.determined_by}: ${n.determination}`):null)),
 (w.audit||[]).map(e=>el('details.case-note',{},el('summary',{},`${when(e.at||e.created_at)} · ${(e.event_type||e.action||'Recorded event').replaceAll('_',' ')}`),el('pre',{text:JSON.stringify(e,null,2)}))));
}

function header(w) {
  const c = w.case;
  const d = w.device;
  return el("div.case-head", {},
    el("h1", { text: c.subject || "(no subject)" }),
    el('div.case-statuses',{},chip(c.priority,priorityClass(c.priority)),' ',chip(c.status,'accent'),' ',chip(`Investigation ${c.ai_state}`,'muted')),
    el('details',{},el('summary',{},`${c.customer_name||'Customer not matched'} · ${d?.model||'Device not identified'} · Account and equipment details`),el("div.facts", {},
      fact("Case", c.case_id),
      fact("Customer", c.customer_name || (c.account_id ? c.account_id
                                                        : "not matched")),
      fact("Account", c.account_id || "-"),
      fact("Identity", c.identity_verified
        ? `verified on ${(c.verification_points || []).join(", ")}`
        : "not verified"),
      d ? fact("Device", `${d.model} rev ${d.revision} (${d.serial_number})`)
        : fact("Device", "not identified"),
      fact("Channel", c.channel),
      fact("Opened", when(c.created_at)),
    )),
  );
}

const fact = (k, v) => el("span", {}, `${k}: `, el("b", { text: v ?? "-" }));

function banners(w) {
  const out = [];
  const c = w.case;
  if (c.priority === "Critical") {
    out.push(el("div.banner.critical", {},
      el("b", { text: "Critical. " }),
      c.priority_reason || "",
      " Routine troubleshooting is stopped until a person reviews this.",
    ));
  }
  if (!c.identity_verified && activeTab==='actions') {
    out.push(el("div.banner.warning", {},
      el("b", { text: "Identity not verified. " }),
      "Protected account work requires verification. Applicable public-manual assistance is still available. ",
      (w.summary?.identity?.outcome === "No match"
        ? "The details supplied match no record."
        : "Two of name, date of birth and exact serial are needed."),
    ));
  }
  if (w.revision?.needs_confirmation && activeTab==='actions') {
    out.push(el("div.banner.warning", {},
      el("b", { text: "Device revision not verified. " }),
      w.revision.reason || "",
    ));
  }
  if (c.rma_needed && activeTab!=='actions') {
    out.push(el("div.banner.info", {},
      el("b", { text: "A product movement is waiting for your approval. " }),
      "See the copilot panel.",
    ));
  }
  if (c.tracking_number) {
    out.push(el("div.banner.info", {},
      `Shipment ${c.tracking_number} — ${c.tracking_status || "created"}.`,
    ));
  }
  return out;
}

function messages(w) {
  const notes = w.notifications || [];
  const rows = (w.messages || []).filter(m=>m.direction!=='internal'&&m.status!=='rejected').map((m) => message(m, w));
  if (!rows.length) rows.push(el("div.empty", {}, "No correspondence yet."));
  return rows;
}

function message(m, w) {
  const kind = m.direction === 'internal' ? 'internal' : m.direction === "inbound" ? "inbound" : "outbound";
  const classes = ["msg", kind];
  if (m.status === "rejected") classes.push("rejected");
  if (m.channel === "qa_notification") return null;
  // The draft under review is shown in the composer, not twice.
  if (w.draft && m.message_id === w.draft.message_id) return null;

  return el('div.' + classes.join("."), {},
    el("div.meta", {},
    el("span.who", { text: kind==='inbound'?'Customer · '+(m.author||'incoming'):m.status==='rejected'?'Rejected draft':m.approved_by?'Representative-approved reply':kind==='internal'?'Internal record':'Proposed support response' }),
      el("span", { text: when(m.created_at) }),
      chip(m.status, m.status === "sent" ? "ok"
        : m.status === "rejected" ? "critical" : "muted"),
      m.approved_by ? el("span", { style: "color:var(--text-faint)",
                                   text: `by ${m.approved_by}` }) : null,
    ),
    el("div.body", { text: m.body || "" }),
    m.metadata && JSON.parse(m.metadata).synthetic ? chip('Simulated customer reply','muted') : null,
    m.internal_note
      ? el("div.quote", { text: `Internal note: ${m.internal_note}` })
      : null,
  );
}

/* ----------------------------------------------------------- composer */

function composerFor(w, refresh, edited=null) {
  const draft = w.draft;
  if (!draft) {
    return el("div.composer", {},
      el("div.hint", {},
        w.case.ai_state === "completed"
          ? ((w.coordination?.contact_tasks||[]).length ? "Internal work is recorded in Notes and Actions. No customer email is due." : "No customer draft is outstanding. Review the recorded findings and case disposition.")
          : "The AI is still investigating; a proposed reply will appear here."),
      el("div.row", {},
        el("button.btn.primary",{onclick:()=>selectTab('notes',true)},'Add internal note'),
        el("button.btn.ghost", {
          onclick: () => attempt(() => api.investigate(w.case.case_id),
                                 { success: "Queued for investigation.",
                                   onDone: refresh }),
        }, "Investigate now"),
      ),
    );
  }

  const area = el("textarea", { rows: 7, 'aria-label':'Proposed customer email', dataset:{messageId:draft.message_id,serverBody:draft.body||''} });
  area.value = edited ?? draft.body ?? '';

  const review=(w.response_reviews||[]).find(r=>r.message_id===draft.message_id);
  const shipping = draft.metadata && JSON.parse(draft.metadata).kind === 'shipping_update';
  const mode = draft.metadata ? JSON.parse(draft.metadata).generation_mode : null;
  const verified = shipping ? undefined : w.summary?.reply?.grounded;
  const source = shipping ? 'recorded simulated shipment' : w.summary?.procedure?.selected;
  const blocked = verified === false;

  return el("div.composer", {},
    el("div.row", { style: "margin:0 0 6px" },
      el("span.hint", {},
        el('span.composer-title',{},'Proposed customer response'),
        mode==='llm'?'Local model draft · ':mode?'Rules / recorded facts · ':'Generation mode not recorded · ',
        `Draft ${draft.message_id}`,
        source ? ` — written from ${source}` : "",
      ),
      el("span", { style: "flex:1" }),
      shipping ? chip('recorded shipping facts','ok') : verified === true ? chip("verified against source", "ok")
        : verified === false ? chip("not verified against source", "critical")
        : chip("no source required", "muted"),
    ),
    area,
    review && !review.passed ? el('div.banner.warning',{},el('b',{},'Independent review needs attention. '),JSON.parse(review.payload).issues?.join(' '),el('button.btn.ghost',{onclick:async()=>{const a=await ask({title:'Review independent validation concern',message:'This records your rationale for overriding an advisory model concern. Deterministic QMS, identity, shipping and safety checks still apply and cannot be overridden.',label:'Representative review rationale',confirm:'Approve reviewed response'});if(a)await attempt(()=>api.send(draft.message_id,area.value,a.text),{success:'Reviewed response sent to the simulated outbox.',onDone:refresh});}},'Review concern and authorize')):null,
    draft.internal_note
      ? el("div.quote", { text: `Internal note (never sent): ` +
                                draft.internal_note })
      : null,
    blocked
      ? el("div.banner.critical", { style: "margin:7px 0 0;border-radius:4px" },
          el("b", {}, "This draft did not pass source verification. "),
          "Request a revision or write the reply yourself before sending.")
      : null,
    el("div.row", {},
      el("button.btn.primary", {
        onclick: () => attempt(
          () => api.send(draft.message_id, area.value),
          { success: "Sent to the simulated outbox.", onDone: refresh, key: "send:" + draft.message_id }),
      }, "Approve and send"),
      el("button.btn", {
        onclick: async () => {
          const got = await ask({
            title: "Request a revision",
            message: "Say what is wrong with the draft. The agent redrafts " +
                     "from this, and the revision is checked against the " +
                     "same sources - it will not send anything it cannot " +
                     "support.",
            label: "What should change",
            confirm: "Request revision",
          });
          if (!got) return;
          toast("Redrafting. This can take a while with the local model.");
          await attempt(() => api.regenerate(draft.message_id, got.text),
                        { success: (out) => out.summary, onDone: refresh, key: "regen:" + draft.message_id });
        },
      }, "Reject and request revision"),
      el("button.btn.danger", {
        onclick: async () => {
          const got = await ask({
            title: "Reject this draft",
            message: "The draft is kept in the record as rejected. Nothing " +
                     "is sent.",
            label: "Reason", confirm: "Reject", danger: true,
          });
          if (!got) return;
          attempt(() => api.rejectDraft(draft.message_id, got.text),
                  { success: "Draft rejected. Nothing was sent.",
                    onDone: refresh, key: "reject:" + draft.message_id });
        },
      }, "Reject"),
      el("span.spacer", { style: "flex:1" }),
      el("span.hint", {},
        "Sending is the only way anything reaches the customer."),
    ),
  );
}

function closedFooter(w, refresh) {
  const c = w.case;
  return el("div.composer", {},
    el("div.row", { style: "margin:0" },
      chip(c.status, c.resolved ? "ok" : "muted"),
      el("span.hint", { text: c.resolution || "" }),
      el("span", { style: "flex:1" }),
      el("button.btn.ghost", {
        onclick: () => attempt(() => api.investigate(c.case_id),
                               { success: "Re-queued.", onDone: refresh }),
      }, "Re-investigate"),
    ),
  );
}

/* ------------------------------------------------------- case actions */

function caseActions(w, refresh) {
  const c = w.case;
  if (c.is_closed) return [chip(c.status, c.resolved ? "ok" : "muted")];
  return [
    el("button.btn.small.ghost", {
      onclick: async () => {
        const got = await ask({
          title: "Override priority",
          message: "A priority override is recorded against your name with " +
                   "the reason you give. The reason is required.",
          label: "Why", confirm: "Override",
          choices: ["Critical", "High", "Medium", "Low"].map((p) => ({
            value: p, label: p, selected: p === c.priority })),
          choiceLabel: "New priority",
        });
        if (!got) return;
        attempt(() => api.setPriority(c.case_id, got.choice, got.text),
                { success: `Priority set to ${got.choice}.`,
                  onDone: refresh });
      },
    }, `Priority: ${c.priority}`),
    el("button.btn.small.ghost", {
      onclick: async () => {
        const got = await ask({
          title: "Assign this case", label: "Representative",
          value: c.assigned_to || "rep.jordan", confirm: "Assign",
        });
        if (!got) return;
        attempt(() => api.assign(c.case_id, got.text),
                { success: `Assigned to ${got.text}.`, onDone: refresh });
      },
    }, c.assigned_to ? `Assigned: ${c.assigned_to}` : "Assign"),
    el("button.btn.small.ghost", {
      onclick: async () => {
        const got = await ask({
          title: "Resolve this case",
          message: "Recorded as resolved, with your name and the time.",
          label: "Resolution", confirm: "Resolve",
        });
        if (!got) return;
        attempt(() => api.closeCase(c.case_id, true, got.text),
                { success: "Case resolved.", onDone: refresh });
      },
    }, "Resolve"),
    el("button.btn.small.ghost", {
      onclick: async () => {
        const got = await ask({
          title: "Close without resolving",
          message: "This stays visible in reporting as closed unresolved. " +
                   "It is not tidied away.",
          label: "Why it could not be resolved", confirm: "Close unresolved",
          danger: true,
        });
        if (!got) return;
        attempt(() => api.closeCase(c.case_id, false, got.text),
                { success: "Closed without resolution.", onDone: refresh });
      },
    }, "Close unresolved"),
  ];
}
