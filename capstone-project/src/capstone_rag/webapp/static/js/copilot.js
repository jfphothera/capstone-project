/* The right column: what the AI found, what needs a person, what happens next.
 *
 * Three sections in that order, with the single most consequential
 * outstanding decision pinned at the top. The full sixteen-step trace is
 * preserved and reachable, but it is not the decision interface: a reviewer
 * should not have to read `validate_procedure_applicability` to approve a
 * replacement cord.
 *
 * Every approval panel states its consequences before the representative
 * commits, and a rejection is never only a note - the server applies the
 * consequence and reports it back, which is what gets shown. */

import { api, REP } from "./api.js?v=rc28";
import { ask, attempt, chip, el, priorityClass, render, toast, when, disclosure, renderKeeping } from "./dom.js?v=rc28";
import {operationalPanel,feedbackPanel,movement} from './workflow.js?v=rc28';

export function renderCopilot(w, refresh) {
  const host = document.querySelector("#copilot-body");
  const stateChip = document.querySelector("#copilot-state");
  stateChip.textContent = aiLabel(w.case.ai_state);
  stateChip.className = "chip " + ({
    processing: "accent", completed: "ok", failed: "critical",
  }[w.case.ai_state] || "muted");

  // rc.28: the Details pane is only what a representative needs to follow the case live, in order:
  // who/what, the investigation steps, and the next step for the person. Everything else is in Agent Activity.
  const c=w.case;
  currentCaseId = c.case_id;
  latestCase = w;
  renderKeeping(host,
    el('section.details-section.case-line',{},
      el('strong',{},c.customer_name||'Customer not matched yet'),' ',chip(c.priority,priorityClass(c.priority)),
      el('p.context-meta',{},[w.device?`${w.device.model} · serial ${w.device.serial_number}`:'Device not identified yet',
        c.identity_verified?'identity verified':'identity not verified'].join(' · ')),
      el('p.context-meta',{},`Status: ${c.status}`)),
    progressSection(w),
    humanActionsSection(w, refresh),
    el('p.context-meta.activity-link',{},el('a',{href:`/trace?case=${c.case_id}`,target:'_blank'},'Full agent detail: open Agent Activity ↗')));
}

export function caseDecisionPanel(w,refresh) {
  return el('div',{},primary(w,refresh),el('details.case-note',{},el('summary',{},'All investigation decisions & follow-up steps'),panels(w,refresh),nextSteps(w,refresh)));
}

const aiLabel = (s) => ({
  queued: "queued", processing: "investigating", completed: "investigated",
  failed: "investigation failed", retriable: "retrying",
}[s] || s || "-");

/* ------------------------------------------- the decision that matters */

function primary(w, refresh) {
  const d = w.primary_decision;
  if (!d) return null;
  if (d.kind === "movement_approval") return null; // grouped in operationalPanel
  if (d.kind === "adverse_event_determination") {
    return determinationPanel(w, refresh);
  }
  if (d.kind === "verification") return verificationPanel(w, d.item, refresh);
  if (d.kind === "reply_approval") {
    return el("div.decision", {},
      el("h4", {}, "A reply is waiting for your approval"),
      el("div", { style: "color:var(--text-dim)" },
        "The draft is in the composer on the left. Nothing is sent until " +
        "you approve it."),
    );
  }
  return findingPanel(w, d.item, refresh);
}

function movementPanel(w, rma, refresh) {
  if (!rma) return null;
  const c = rma.consequences || {};
  return el("div.decision", {},
    el("h4", {}, c.headline || "A product movement needs your approval"),
    el("dl", {},
      el("dt", {}, "Reason"), el("dd", { text: c.reason || rma.reason || "" }),
      el("dt", {}, "ERP action"), el("dd", { text: c.erp_action || "" }),
      el("dt", {}, "Shipping"), el("dd", { text: c.shipping || "" }),
      el("dt", {}, "If rejected"), el("dd", { text: c.if_rejected || "" }),
      el("dt", {}, "Movement"),
      el("dd", { text: `${rma.rma_id} · ${rma.movement_type} · ` +
                       `${rma.direction}` }),
      rma.items?.length
        ? [el("dt", {}, "Items"),
           el("dd", { text: rma.items.map((i) =>
             `${i.name}${i.part_number ? ` (${i.part_number})` : ""}`)
             .join(", ") })]
        : null,
    ),
    el("div.actions", {},
      el("button.btn.primary", {
        onclick: async () => {
          const got = await ask({
            title: "Approve this movement",
            message: `${c.headline}\n\n${c.erp_action}\n${c.shipping}\n\n` +
                     "Your name is recorded against the authorisation.",
            label: "Note (optional)", required: false, confirm: "Approve",
          });
          if (!got) return;
          attempt(() => api.approveRma(rma.rma_id, got.text), {
            success: (out) => `Approved. Tracking ${out.tracking_number}.`,
            onDone: refresh,
          });
        },
      }, "Approve movement"),
      el("button.btn.danger", {
        onclick: async () => {
          const got = await ask({
            title: "Reject this movement",
            message: "No RMA is finalised and no label is created. The " +
                     "proposal cannot be approved afterwards without a new " +
                     "authorised decision.",
            label: "Why", confirm: "Reject", danger: true,
          });
          if (!got) return;
          attempt(() => api.rejectRma(rma.rma_id, got.text),
                  { success: "Rejected. Nothing was created.",
                    onDone: refresh });
        },
      }, "Reject"),
    ),
  );
}

function determinationPanel(w, refresh) {
  const open = (w.notifications || []).find((n) => n.status === "open");
  if (!open) return null;
  const statement = (w.messages || []).find((m) => m.direction === "inbound");
  return el("div.decision.critical", {},
    el("h4", {}, "A possible adverse event needs your determination"),
    el("div", { style: "color:var(--text-dim)", text: open.body }),
    statement
      ? el("div.quote", { text: statement.body })
      : null,
    el("dl", {},
      el("dt", {}, "Classification"), el("dd", { text: open.kind.replace(/_/g, " ") }),
      el("dt", {}, "Certainty"), el("dd", { text: open.certainty || "-" }),
      el("dt", {}, "Effect"),
      el("dd", {}, "A determination changes how the case is classified and " +
                   "is written to the QA record with your name. It is a " +
                   "simulated review - no reportability decision is made " +
                   "and nothing is filed."),
    ),
    el("div.actions", {},
      el("button.btn.danger", {
        onclick: () => determine(open, "confirmed adverse event", refresh),
      }, "Confirm adverse event"),
      el("button.btn", {
        onclick: () => determine(open, "not an adverse event", refresh),
      }, "Not an adverse event"),
      el("button.btn.ghost", {
        onclick: () => determine(open, "", refresh, true),
      }, "Other wording"),
    ),
  );
}

async function determine(note, text, refresh, prompt = false) {
  let determination = text;
  const got = await ask({
    title: "Record the determination",
    message: "This is recorded against your name and changes the case " +
             "classification.",
      label: "Supporting rationale (required)", value: "", confirm: "Record determination",
      choiceLabel: "Safety determination (required)",
      choices: [
        { value: "", label: "Select a determination", selected: !text },
        ...["confirmed adverse event", "suspected adverse event",
            "device hazard without confirmed injury", "not an adverse event",
            "insufficient information"].map(value => ({ value, label: value, selected: value === text })),
      ],
    required: true,
  });
  if (!got) return;
    determination = got.choice;
    attempt(() => api.determine(note.notification_id, determination, got.text),
          { success: "Determination recorded.", onDone: refresh, key: "determine:" + note.notification_id });
}

function verificationPanel(w, item, refresh) {
  // The revision is confirmed through its own control, not a generic approve.
  if (item.action === "assess_device_revision") {
    return revisionPanel(w, item, refresh);
  }
  return findingPanel(w, item, refresh);
}

function revisionPanel(w, item, refresh) {
  const rev = w.revision || {};
  return el("div.decision", {},
    el("h4", {}, "Confirm the hardware revision"),
    el("dl", {},
      el("dt", {}, "Recorded"),
      el("dd", { text: `${w.device?.model || "-"} revision ` +
                       `${rev.revision || "unknown"}` }),
      el("dt", {}, "Status"), el("dd", { text: rev.status || "-" }),
      el("dt", {}, "Evidence"),
      el("dd", { text: rev.basis || "no basis recorded" }),
      el("dt", {}, "Why it matters"), el("dd", { text: rev.reason || "" }),
      el("dt", {}, "If confirmed"),
      el("dd", {}, "The document search and the applicability check are run " +
                   "again for the confirmed revision. Confirming which " +
                   "revision this unit is does not authorise a procedure - " +
                   "the deterministic check still decides that."),
    ),
    el("div.actions", {},
      el("button.btn.primary", {
        onclick: async () => {
          const got = await ask({
            title: "Confirm the revision",
            message: "Say how you established it, for example that you read " +
                     "the label with the customer. This is recorded against " +
                     "your name on the case.",
            label: "How it was established", confirm: "Confirm revision",
          });
          if (!got) return;
          toast("Re-checking document eligibility.");
          attempt(() => api.confirmRevision(w.case.case_id, "confirmed",
                                            got.text),
                  { success: (out) => out.summary, onDone: refresh });
        },
      }, "Confirm revision"),
      el("button.btn.danger", {
        onclick: async () => {
          const got = await ask({
            title: "Reject the proposed revision",
            message: "No revision-specific instructions may be issued until " +
                     "the revision is established another way.",
            label: "What is wrong with it", confirm: "Reject", danger: true,
          });
          if (!got) return;
          attempt(() => api.confirmRevision(w.case.case_id, "rejected",
                                            got.text),
                  { success: (out) => out.summary, onDone: refresh });
        },
      }, "Reject"),
    ),
  );
}

function findingPanel(w, item, refresh) {
  if (!item) return null;
  const cons = item.consequences || {};
  return el("div.decision", {},
    el("h4", { text: item.label }),
    el("div", { style: "color:var(--text-dim)",
                text: item.missing_info || item.description || "" }),
    el("dl", {},
      el("dt", {}, "If approved"), el("dd", { text: cons.approve || "-" }),
      el("dt", {}, "If rejected"), el("dd", { text: cons.reject || "-" }),
    ),
    el("div.actions", {},
      el("button.btn.primary", {
        onclick: async () => {
          const got = await ask({
            title: `Approve: ${item.label}`, message: cons.approve || "",
            label: "Note (optional)", required: false, confirm: "Approve",
          });
          if (!got) return;
          attempt(() => api.decideItem(item.item_id, "approved", got.text),
                  { success: (out) => out.consequence || "Recorded.",
                    onDone: refresh });
        },
      }, "Approve"),
      el("button.btn.danger", {
        onclick: async () => {
          const got = await ask({
            title: `Reject: ${item.label}`, message: cons.reject || "",
            label: "Why", confirm: "Reject", danger: true,
          });
          if (!got) return;
          attempt(() => api.decideItem(item.item_id, "rejected", got.text),
                  { success: (out) => out.consequence || "Recorded.",
                    onDone: refresh });
        },
      }, "Reject / correct"),
    ),
  );
}

/* ------------------------------------------------------------ summary */

function summarySection(w) {
  const s = w.summary || {};
  const lines = [
    ["Priority", s.priority, s.priority_reason],
    ["Classification", (s.classification || "").replace(/_/g, " ")],
    ["Identity", s.identity?.verified ? "verified" : "not verified",
     s.identity?.verified
       ? `on ${(s.identity.points || []).join(", ")}`
       : s.identity?.outcome],
    ["Device", s.device?.serial_number
      ? `${s.device.model} rev ${s.device.revision}` : "not identified",
     s.device?.revision_status
       ? `revision ${s.device.revision_status}` : ""],
    ["Safety", s.safety?.is_adverse_event
      ? `adverse event (${s.safety.certainty})`
      : (s.safety?.outcome || "no concern found")],
    ["Complaint", s.complaint?.is_complaint ? "yes" : "no",
     s.complaint?.reason],
    ["Procedure", s.procedure?.selected || s.procedure?.outcome || "-",
     s.procedure?.missing_info],
    ["Agent resolution recommendation", s.movement?.reason || s.movement?.outcome || "-"],
  ];
  return el('div.finding-summary',{},lines.map(([label,value,detail])=>el('div.finding-fact',{},el('small',{},label),el('p',{},String(value??'-')),detail?el('p.context-meta',{},String(detail)):null)));
}

/* -------------------------------------------------- the three sections */

function panels(w, refresh) {
  const p = w.checklist?.panels || {};
  return [
    section("Investigation complete", p.investigation_complete, w, refresh),
    section("Needs your attention", p.needs_your_attention, w, refresh, true),
  ];
}

function nextSteps(w, refresh) {
  const p = w.checklist?.panels || {};
  const rows = [...(p.next_steps || [])];
  const extras = [];

  for (const r of w.rmas || []) {
    if (r.status === "proposed") continue;
    extras.push(el("div.item." + (r.status === "executed" ? "positive"
      : r.status === "rejected" || r.status === "canceled" ? "neutral"
      : "negative"), {},
      el("span.mark", { text: r.status === "executed" ? "✓" : "✕" }),
      el("div", {},
        el("div.label", { text: `${r.rma_id} ${r.movement_type}` }),
        el("div.detail", { text: `${r.status}${r.approved_by ?
          ` by ${r.approved_by}` : ""}` }),
        r.shipment
          ? el("div.detail", {},
              `Tracking ${r.shipment.tracking_number} — ` +
              `${r.shipment.status}. `,
              el("a", { href: `/api/shipments/${r.shipment.tracking_number}` +
                              `/label`, target: "_blank",
                        style: "color:var(--accent)" }, "sample label"))
          : null,
      ),
      el("span.outcome", {}, chip(r.status, r.status === "executed" ? "ok"
        : r.status === "failed" ? "critical" : "muted")),
    ));
  }

  for (const s of w.shipments || []) {
    if (s.status === "delivered") continue;
    extras.push(el("div.item.neutral", {},
      el("span.mark", { text: "→" }),
      el("div", {},
        el("div.label", { text: `Shipment ${s.tracking_number}` }),
        el("div.detail", { text: `${s.status.replace(/_/g, " ")}` }),
        el("div.actions", { style: "display:flex;gap:5px;margin-top:4px" },
          s.status === "label_created"
            ? el("button.btn.small.ghost", {
                onclick: () => attempt(
                  () => api.advance(s.tracking_number, "in_transit",
                                    "Riverton MN", "simulated carrier scan"),
                  { success: "Marked in transit.", onDone: refresh }),
              }, "Mark in transit")
            : null,
          el("button.btn.small.ghost", {
            onclick: () => attempt(
              () => api.advance(s.tracking_number, "delivered",
                                "Destination", "simulated delivery scan"),
              { success: "Marked delivered.", onDone: refresh }),
          }, "Mark delivered"),
        ),
      ),
      el("span.outcome", {}, chip("in progress", "accent")),
    ));
  }

  if (!rows.length && !extras.length) return null;
  return el("div.section", {},
    el("h3", {}, "Next steps"),
    rows.map((d) => itemRow(d, w, refresh)),
    extras,
  );
}

function section(title, rows, w, refresh, highlight = false) {
  if (!rows || !rows.length) {
    if (!highlight) return null;
    return el("div.section", {},
      el("h3", {}, title, el("span.n", {}, chip("nothing outstanding", "ok"))),
    );
  }
  return el("div.section", {},
    el("h3", {}, title, el("span.n", {}, chip(String(rows.length),
                                              highlight ? "count" : "muted"))),
    rows.map((d) => itemRow(d, w, refresh)),
  );
}

function itemRow(d, w, refresh) {
  const who = d.authorizer
    ? `${d.approval_decision} by ${d.authorizer} ${when(d.decided_at)}`
    : (d.actor === "representative" ? "representative" : "automatic");
  return el(`div.item.${d.tone}`, {},
    el("span.mark", { text: markFor(d.outcome) }),
    el("div", {},
      el("div.label", { text: d.label }),
      d.missing_info ? el("div.detail", { text: d.missing_info }) : null,
      d.consequence ? el("div.detail", { text: d.consequence }) : null,
      el("div.who", { text: who }),
      d.offers_decision
        ? el("div", { style: "display:flex;gap:5px;margin-top:4px" },
            el("button.btn.small", {
              onclick: () => openDecision(w, d, refresh),
            }, "Review"))
        : null,
    ),
    el("span.outcome", {}, chip(d.outcome, toneChip(d.tone))),
  );
}

const toneChip = (tone) =>
  ({ positive: "ok", negative: "critical", neutral: "muted" }[tone] || "muted");

function markFor(outcome) {
  const marks = {
    Completed: "✓", "No concern found": "✓",
    "Match found": "✓", Approved: "✓", Executed: "✓",
    "No match": "✕", Failed: "✕", Rejected: "✕",
    "Information missing": "?", Uncertain: "?", Blocked: "■",
    "Human review required": "▲", "Not applicable": "–",
  };
  return marks[outcome] || "·";
}

function openDecision(w, item, refresh) {
  const host = document.querySelector("#copilot-body");
  const panel = item.action === "assess_device_revision"
    ? revisionPanel(w, item, refresh)
    : findingPanel(w, item, refresh);
  host.prepend(panel);
  panel.scrollIntoView({ block: "nearest" });
}

/* -------------------------------------------------------- full trace */

function trace(w) {
  const all = w.checklist?.all || [];
  return el("details.trace", {},
    el("summary", {}, `Full investigation trace, evidence and audit ` +
                      `(${all.length} steps)`),
    el("div", { style: "padding:4px 0" },
      all.map((d) => el("div.item." + d.tone, {},
        el("span.mark", { text: markFor(d.outcome) }),
        el("div", {},
          el("div.label", {}, `${d.seq}. ${d.label} `,
            el("span", { style: "color:var(--text-faint);font-family:" +
                                "var(--mono);font-size:11px",
                         text: d.technical_name })),
          el("div.detail", { text: d.description || "" }),
          d.missing_info ? el("div.detail", { text: d.missing_info }) : null,
          el("div.who", { text: `${d.decision_class_label} · ` +
                                `${d.status} · ${when(d.at)}` }),
        ),
        el("span.outcome", {}, chip(d.outcome, toneChip(d.tone))),
      )),
    ),
    el("details.trace", {},
      el("summary", {}, "Retrieved sources and validation results"),
      el("pre", { text: JSON.stringify(
        w.evidence?.retrieved_sources || [], null, 1) }),
    ),
    el("details.trace", {},
      el("summary", {}, "Reasoning attempts and tool calls"),
      el("pre", { text: JSON.stringify({
        reasoning: w.evidence?.reasoning_attempts || [],
        tools: w.evidence?.tool_calls || [],
      }, null, 1) }),
    ),
    el("details.trace", {},
      el("summary", {}, `Audit events (${(w.audit || []).length})`),
      el("div", { style: "padding:4px 0" },
        (w.audit || []).map((e) => el("div.item.neutral", {},
          el("span.mark", { text: "·" }),
          el("div", {},
            el("div.label", { text: e.event_type.replace(/_/g, " ") }),
            el("div.detail", { text: e.summary || "" }),
            el("div.who", { text: `${e.actor} · ${when(e.at)}` }),
          ),
          el("span", {}),
        )),
      ),
    ),
  );
}

/* ------------------------------------------------------------------ rc.28
 * The Details pane for a live representative: a clean step list of the investigation (plain words, one line per
 * step) and "Your next step" (only real pending decisions, with the existing handlers). Raw events stay in
 * Agent Activity. */

let currentCaseId = null, latestCase = null;
const progressCache = new Map();   // case_id -> latest progress payload (server read model)

const ICON = { completed: "✓", running: "…", queued: "…", failed: "✕", blocked: "!", not_applicable: "–",
  not_started: "○", waiting_for_human: "→", waiting_for_customer: "→", closed: "✓" };

/** Plain-language line for one step, from its observed status (and the case facts the person already sees). */
function stepText(s, w) {
  const c = w?.case || {}, failed = s.status === "failed";
  const t = {
    intake: { running: "Reading the customer's message…", completed: "Message read and classified" },
    safety: { running: "Screening for safety concerns…", completed: c.priority === "Critical" ? "Safety concern found: routine troubleshooting stopped" : "No safety concern found" },
    erp: { running: "Checking the customer's account and device…", completed: c.identity_verified ? "Account and device verified" : "Account or device could not be verified" },
    qms: { running: "Searching the approved procedures…", completed: "Approved procedures checked", not_applicable: "Not needed for this case" },
    planning: { running: "Choosing the next step…", completed: s.outcome?.startsWith("Branched") ? "Compared alternatives and chose a next step" : "Next step chosen (single path)" },
    response: { running: "Drafting a reply…", completed: w?.draft ? "Reply drafted for your review" : "No reply needed yet" },
    checks: { running: "Checking the draft…", completed: "Independent checks done" },
  }[s.key] || {};
  if (failed) return "Could not complete: handed to you";
  if (s.status === "not_started") return "Not reached yet";
  return t[s.status] || s.outcome || "";
}

/** Store a progress payload from the poller; repaints only the step list of the case on screen. */
export function applyProgress(caseId, data) {
  if (!data || data.case_id !== caseId) return;
  progressCache.set(caseId, data);
  if (caseId === currentCaseId) paintProgress(caseId);
}

function progressSection(w) {
  const body = el("div", { id: "progress-body" });
  queueMicrotask(() => paintProgress(w.case.case_id));
  return el("section.details-section.progress-panel", {}, el("h3", {}, "Investigation steps"), body);
}

function paintProgress(caseId) {
  const host = document.querySelector("#progress-body");
  if (!host) return;
  const p = progressCache.get(caseId), w = latestCase && latestCase.case.case_id === caseId ? latestCase : null;
  if (!p) { render(host, el("p.context-meta", {}, "Loading…")); return; }
  const steps = p.stages.filter((s) => s.key !== "waiting");
  const waiting = p.stages.find((s) => s.key === "waiting");
  render(host,
    el("ol.step-list", {}, ...steps.map((s) => el(`li.step.${s.status}`, {},
      el("span.step-icon", { "aria-hidden": "true" }, ICON[s.status] || "○"),
      el("span.step-body", {}, el("span.step-label", {}, s.label), el("span.step-text", {}, stepText(s, w)))))),
    waiting ? el(`p.step-now.${waiting.status}`, {}, waiting.outcome) : null,
    el("p.context-meta", {}, "A finished step means that part of the analysis ended, not that the case is solved."));
}

/* One card per real pending decision, using the same handlers as the rest of the workspace. */
function actionCard(title, says, body, kind = "") {
  return el(`div.action-card${kind ? "." + kind : ""}`, {}, el("h4", {}, title), says ? el("p", {}, says) : null, body);
}

const focusDraft = () => {
  document.dispatchEvent(new CustomEvent("case-section", { detail: "conversation" }));
  setTimeout(() => document.querySelector("#conv-composer textarea")?.focus(), 50);
};

function qaHandoff(w, refresh) {
  // The closure rule for a held safety report: QA determination recorded, case assigned to qa_team, then closed
  // unresolved (held/escalated). One button does the last two steps, recorded under the representative's name.
  return el("button.btn.primary", { onclick: async (e) => {
    const got = await ask({ title: "Hand the case to QA", message: "Assigns the case to the QA team and closes it here as escalated (not resolved). It stays visible in Quality & Safety.", label: "Handoff note", confirm: "Hand off to QA" });
    if (!got) return;
    e.currentTarget.disabled = true;
    await attempt(async () => { const a = await api.assign(w.case.case_id, "qa_team"); if (a && a.ok === false) return a; return api.closeCase(w.case.case_id, false, "Escalated to QA: " + got.text); },
      { success: "Handed to QA. The case is closed here as escalated (not resolved).", onDone: refresh, key: "qa:" + w.case.case_id });
  } }, "Hand off to QA");
}

function humanActions(w, refresh) {
  const c = w.case, cards = [], covered = new Set();
  if (c.is_closed) return cards;
  const attention = (w.checklist?.panels?.needs_your_attention || []).filter((d) => d.offers_decision);
  const notes = w.notifications || [];
  const openNote = notes.find((n) => n.status === "open" && !n.determination);
  const held = c.priority === "Critical" || notes.length;
  if (openNote || w.primary_decision?.kind === "adverse_event_determination") {
    covered.add("screen_adverse_event");
    cards.push(actionCard("Record the safety determination", "Possible safety issue. Routine troubleshooting is stopped until you decide.",
      determinationPanel(w, refresh), "critical"));
  } else if (held && notes.some((n) => n.determination)) {
    covered.add("screen_adverse_event");
    cards.push(actionCard("Hand the case to QA", "Your determination is recorded. Hand the case to the QA team; it closes here as escalated, not resolved.",
      qaHandoff(w, refresh), "critical"));
  }
  const identityItem = attention.find((d) => d.action === "verify_identity");
  if (!c.identity_verified && !held) {
    covered.add("verify_identity");
    cards.push(actionCard("Confirm who the customer is", "The account could not be verified. Ask the customer for the serial number; when they reply, record it with “Record reply and continue investigation”.",
      identityItem ? verificationPanel(w, identityItem, refresh) : null));
  }
  if (w.revision?.needs_confirmation) {
    covered.add("assess_device_revision");
    const item = attention.find((d) => d.action === "assess_device_revision") || { action: "assess_device_revision" };
    cards.push(actionCard("Confirm the device revision", w.revision.reason || "The procedure depends on the device revision.", revisionPanel(w, item, refresh)));
  }
  for (const r of (w.rmas || []).filter((r) => r.status === "proposed" || r.status === "failed" || (r.direction === "inbound" && r.shipment?.status === "delivered" && !r.received_at))) {
    covered.add("determine_product_movement");
    const what = /device/.test(r.movement_type || "") ? "device" : "part";
    cards.push(actionCard(r.status === "proposed" ? `Approve or reject: ship a ${what} (${r.direction})` : r.status === "failed" ? "Shipping failed: retry" : "Confirm the returned device arrived",
      r.status === "proposed" ? `${r.reason} Nothing ships unless you approve.` : null, movement(w, r, refresh)));
  }
  if (w.draft) {
    cards.push(actionCard("Review the reply to the customer", "Read or edit it in the middle, then Approve and send, request a revision, or reject. Nothing is sent without you.",
      el("button.btn", { onclick: focusDraft }, "Go to the draft")));
  }
  for (const item of attention.filter((d) => !covered.has(d.action) && d.action !== "prepare_response")) {
    cards.push(actionCard(item.label || item.action.replaceAll("_", " "), item.description || item.outcome, findingPanel(w, item, refresh)));
  }
  const lastIn = [...(w.messages || [])].reverse().find((m) => m.direction === "inbound");
  const lastSent = [...(w.messages || [])].reverse().find((m) => m.direction === "outbound" && m.status === "sent");
  if (!cards.length && !held && lastIn && lastSent && lastIn.created_at > lastSent.created_at && !["queued", "processing"].includes(c.ai_state)) {
    cards.push(actionCard("The customer replied", "If they confirmed the fix, resolve the case. Otherwise record their reply and continue.",
      el("button.btn.primary", { onclick: async () => {
        const got = await ask({ title: "Resolve this case", message: "Recorded as resolved, with your name and the time.", label: "Resolution", confirm: "Resolve" });
        if (got) attempt(() => api.closeCase(c.case_id, true, got.text), { success: "Case resolved.", onDone: refresh, key: "resolve:" + c.case_id });
      } }, "Resolve case")));
  }
  return cards;
}

function humanActionsSection(w, refresh) {
  const busy = ["queued", "processing", "retriable"].includes(w.case.ai_state);
  const cards = humanActions(w, refresh);
  return el("section.details-section.actions-panel", {}, el("h3", {}, "Your next step"),
    cards.length ? cards : el("p", {}, w.case.is_closed ? "Case closed. Nothing to do." : busy ? "The agents are working. Your step appears here when they finish." : "Nothing needs you right now."),
    el("p.context-meta", {}, `Signed in as ${REP.name}.`));
}