/* The views that are not the inbox: trends, movements, quality, audit.
 *
 * Each one takes over the workspace grid. They are information-dense on
 * purpose - a supervisor reading these wants the numbers and the
 * denominators, not cards with a lot of air around them. */

import { api } from "./api.js?v=rc28";
import { ask, attempt, chip, el, render, when } from "./dom.js?v=rc28";

const num = (v) => (v === null || v === undefined ? "-" : v);
const pct = (n, d) => (d ? `${Math.round((n / d) * 100)}%` : "-");

function card(k, v, sub) {
  return el("div.card", {},
    el("div.k", { text: k }),
    el("div.v", { text: String(num(v)) }),
    sub ? el("div.sub", { text: sub }) : null);
}

function table(headers, rows) {
  if (!rows.length) return el("div.empty", {}, "Nothing recorded yet.");
  return el('div.table-scroll',{tabindex:0,'aria-label':'Scrollable data table'},el("table.grid", {},
    el("thead", {}, el("tr", {}, headers.map((h) => el("th", { text: h })))),
    el("tbody", {}, rows)));
}

/* ----------------------------------------------------------- trends */

export async function renderTrends(host, refresh) {
  const [data, props] = await Promise.all([api.analytics(), api.proposals()]);
  const s = data.safety || {};
  const comp = data.complaints || {};
  const ident = data.identification || {};
  const retr = data.retrieval || {};
  const mv = data.movements || {};
  const life = data.lifecycle || {};
  const corr = data.corrections || {};
  const gaps = data.documentation_gaps || {};

  render(host,
    el("h1", {}, "Trends & improvements"),
    el("p.lede", {}, data.scope?.note || ""),

    el("h2", {}, "Volume and scope"),
    el("div.cards", {},
      card("Unique cases", data.scope?.unique_cases,
           "live operation, excludes replays"),
      card("Evaluation case rows", data.scope?.evaluation_case_rows,
           "repeated runs of stored scenarios"),
      card("Evaluation runs", (data.scope?.evaluation_runs || []).length),
      card("Open", life.open ?? num(life.open), "not resolved or closed"),
      card("Resolved", life.resolved),
      card("Closed unresolved", life.closed_unresolved),
    ),

    el("h2", {}, "Safety"),
    el("div.cards", {},
      card("Confirmed adverse events", s.adverse_events_confirmed),
      card("Possible adverse events", s.adverse_events_suspected),
      card("Device hazards, no injury", s.device_safety_hazards),
      card("Awaiting determination", s.awaiting_human_determination),
    ),

    el("h2", {}, "Complaints by category"),
    barTable(comp.by_classification || {}),

    el("h2", {}, "Recurring issues and device-model patterns"),
    table(["Model", "Cases", "Complaints", "Rate"],
      Object.entries(data.failures_by_model || {}).map(([model, v]) =>
        el("tr", {},
          el("td", { text: model }),
          el("td", { text: num(v.devices_on_file) }),
          el("td", { text: num(v.complaints) }),
          el("td", {}, el("div.bar", {},
            el("span", { style: `width:${Math.min(100,
              Math.round(100 * (v.complaints || 0) /
                         Math.max(1, v.devices_on_file || 1)))}%` })),
            el("span", { style: "font-size:11px;color:var(--text-faint)",
                         text: ` ${pct(v.complaints || 0,
                                       v.devices_on_file || 0)}` })),
        ))),

    el("h2", {}, "Recurring symptom clusters"),
    table(["Symptom", "Occurrences"],
      (data.symptom_clusters || []).map((c) => el("tr", {},
        el("td", { text: c.symptom || c[0] }),
        el("td", { text: num(c.count ?? c[1]) })))),

    el("h2", {}, "RMAs and replacement parts"),
    el("div.cards", {},
      card("Proposed", mv.by_status?.proposed || 0),
      card("Approved", mv.by_status?.approved || 0),
      card("Rejected", mv.by_status?.rejected || 0),
      card("Executed", mv.by_status?.executed || 0),
      card("Failed", mv.by_status?.failed || 0),
      card("Avoidable whole-device returns",
           (mv.potentially_avoidable_device_returns || []).length,
           "a part would have done"),
      card("Parts shipped instead", mv.part_shipments),
    ),

    el("h2", {}, "Repeat contact and unresolved"),
    el("div.cards", {},
      card("Repeated contact flagged",
           (data.escalation || {}).customers_contacting_more_than_once),
      card("Closed unresolved", life.closed_without_resolution),
      card("Needs human review", (data.escalation || {}).needing_human_review),
    ),
    (life.closed_unresolved_ids || []).length
      ? el("p.lede", {}, "Unresolved: " +
           life.closed_unresolved_ids.join(", "))
      : null,

    el("h2", {}, "Agent performance"),
    el("div.cards", {},
      card("Identity verified", ident.identity_verified,
           `of ${num(ident.cases)} cases`),
      card("Not verified", (ident.cases || 0) - (ident.identity_verified || 0)),
      card("Documents considered", retr.chunks_considered,
           `${num(retr.cases_investigated)} cases investigated`),
      card("Rejected on applicability", retr.chunks_rejected),
      card("Procedures used", (retr.procedures_used || []).length),
      card("Representative corrections", corr.checklist_items_rejected),
    ),
    gaps.uncovered_models?.length
      ? el("p.lede", {}, "No approved procedure exists for: " +
           gaps.uncovered_models.join(", "))
      : null,

    el("h2", {}, "Queue and processing"),
    el("div.cards", {},
      card("Mean investigation time",
           data.processing?.mean_seconds
             ? `${data.processing.mean_seconds}s` : "-"),
      card("Longest", data.processing?.max_seconds
             ? `${data.processing.max_seconds}s` : "-"),
      card("Waiting in queue", data.queue?.queued || 0),
      card("Currently processing", data.queue?.current ? 1 : 0,
           data.processing?.basis || ""),
    ),

    el("h2", {}, "Proposed actions from the data"),
    table(["Finding", "Proposed action"],
      (data.proposed_actions || []).map((a) => el("tr", {},
        el("td", { text: `${a.area || "finding"} · ${a.priority || ""}` }),
        el("td", { text: a.action || a.proposal || "" })))),

    el("h2", {}, "Improvement proposals awaiting review"),
    el("p.lede", {}, props.boundary),
    table(["Proposal", "Raised", "Trigger", "Body", "Status", ""],
      (props.proposals || []).map((p) => el("tr", {},
        el("td.mono", { text: p.proposal_id }),
        el("td", { text: when(p.created_at) }),
        el("td", { text: p.trigger_summary || "" }),
        el("td", { text: p.body || "" }),
        el("td", {}, chip(p.review_status,
          p.review_status === "approved" ? "ok"
            : p.review_status === "rejected" ? "muted" : "high")),
        el("td", {}, p.review_status === "pending_review"
          ? el("div", { style: "display:flex;gap:5px" },
              el("button.btn.small.primary", {
                onclick: async () => {
                  const got = await ask({
                    title: "Approve this proposal",
                    message: props.boundary, label: "Note", required: false,
                    confirm: "Approve",
                  });
                  if (!got) return;
                  attempt(() => api.reviewProposal(p.proposal_id, "approved",
                                                   got.text),
                          { success: "Approved.", onDone: refresh });
                },
              }, "Approve"),
              el("button.btn.small.danger", {
                onclick: async () => {
                  const got = await ask({
                    title: "Reject this proposal", label: "Why",
                    confirm: "Reject", danger: true,
                  });
                  if (!got) return;
                  attempt(() => api.reviewProposal(p.proposal_id, "rejected",
                                                   got.text),
                          { success: "Rejected.", onDone: refresh });
                },
              }, "Reject"))
          : el("span", { style: "color:var(--text-faint)",
                         text: p.reviewed_by || "" })),
      ))),

    el("h2", {}, "Reusable resolutions awaiting review"),
    table(["Memory", "Device", "Symptom", "Procedure", ""],
      (props.memory_pending || []).map((m) => el("tr", {},
        el("td.mono", { text: m.memory_id }),
        el("td", { text: m.model_revision || m.model || "" }),
        el("td", { text: m.symptom || "" }),
        el("td", { text: m.procedure_id || "" }),
        el("td", {}, el("div", { style: "display:flex;gap:5px" },
          el("button.btn.small.primary", {
            onclick: () => attempt(
              () => api.reviewMemory(m.memory_id, "approved", ""),
              { success: "Approved. Applicability is still re-checked at " +
                         "recall.", onDone: refresh }),
          }, "Approve"),
          el("button.btn.small.danger", {
            onclick: () => attempt(
              () => api.reviewMemory(m.memory_id, "rejected", ""),
              { success: "Rejected.", onDone: refresh }),
          }, "Reject"))),
      ))),

    el("h2", {}, "Before and after an approved correction"),
    el("p.lede", {},
      "Comparison runs the same stored evaluation cases before and after a " +
      "change and reports both with the same denominator. Run it from the " +
      "command line with scripts/compare_runs.py; completed runs appear " +
      "here."),
    table(["Run", "Started", "Label", "Mode"],
      (data.scope?.evaluation_runs || []).map((r) => el("tr", {},
        el("td.mono", { text: r.run_id }),
        el("td", { text: when(r.started_at) }),
        el("td", { text: r.label || "" }),
        el("td", { text: r.mode || "" })))),
  );
}

function barTable(byCategory) {
  const entries = Object.entries(byCategory || {})
    .filter(([, v]) => typeof v === "number");
  const total = entries.reduce((a, [, v]) => a + v, 0);
  if (!entries.length) return el("div.empty", {}, "Nothing recorded yet.");
  return table(["Category", "Cases", "Share"],
    entries.sort((a, b) => b[1] - a[1]).map(([k, v]) => el("tr", {},
      el("td", { text: k.replace(/_/g, " ") }),
      el("td", { text: String(v) }),
      el("td", {}, el("div.bar", {},
        el("span", { style: `width:${Math.round((v / total) * 100)}%` })),
        el("span", { style: "font-size:11px;color:var(--text-faint)",
                     text: ` ${pct(v, total)} of ${total}` })))));
}

/* -------------------------------------------------------- movements */

export async function renderMovements(host, refresh, openCase) {
  const data = await api.rmas();
  render(host,
    el("h1", {}, "RMAs & shipments"),
    el("p.lede", {},
      "An RMA here is a simulated ERP inventory movement, not necessarily a " +
      "device return. Where an approved procedure covers a replaceable " +
      "part, the part is sent and the unit stays with the customer."),
    el("div.cards", {},
      ...Object.entries(data.by_status || {}).map(([k, v]) => card(k, v)),
    ),
    el("h2", {}, "Movements"),
    table(["RMA", "Case", "Type", "Direction", "Items", "Status",
           "Authorised by", "ERP call", "Tracking", ""],
      (data.rmas || []).map((r) => el("tr", {},
        el("td.mono", {}, el("a", {
          href: "#", style: "color:var(--accent)",
          onclick: (e) => { e.preventDefault(); openCase(r.case_id); },
        }, r.rma_id)),
        el("td", {},
          el("div.mono", { text: r.case_id || "-" }),
          el("div", { style: "color:var(--text-faint)",
                      text: r.case?.subject || "" })),
        el("td", { text: r.movement_type }),
        el("td", { text: r.direction }),
        el("td", { text: (r.items || []).map((i) => i.name).join(", ") }),
        el("td", {}, chip(r.status,
          r.status === "executed" ? "ok"
            : r.status === "failed" ? "critical"
            : r.status === "proposed" ? "high" : "muted")),
        el("td", { text: r.approved_by || "-" }),
        el("td.mono", { text: r.call?.call_id || "-" }),
        el("td.mono", {}, r.shipment
          ? el("a", { href: `/api/shipments/${r.shipment.tracking_number}` +
                            `/label`, target: "_blank",
                      style: "color:var(--accent)" },
              r.shipment.tracking_number)
          : "-"),
        el("td", {}, movementActions(r, refresh)),
      ))),
    el("h2", {}, "Shipment events"),
    shipmentEvents(data.rmas || []),
  );
}

function movementActions(r, refresh) {
  const row = el("div", { style: "display:flex;gap:4px" });
  if (r.status === "proposed") {
    row.append(
      el("button.btn.small.primary", {
        onclick: async () => {
          const got = await ask({
            title: `Approve ${r.rma_id}`,
            message: `${r.consequences?.headline}\n` +
                     `${r.consequences?.erp_action}\n` +
                     `${r.consequences?.shipping}`,
            label: "Note", required: false, confirm: "Approve",
          });
          if (!got) return;
          attempt(() => api.approveRma(r.rma_id, got.text),
                  { success: (o) => `Approved. Tracking ${o.tracking_number}.`,
                    onDone: refresh });
        },
      }, "Approve"),
      el("button.btn.small.danger", {
        onclick: async () => {
          const got = await ask({ title: `Reject ${r.rma_id}`, label: "Why",
                                  confirm: "Reject", danger: true });
          if (!got) return;
          attempt(() => api.rejectRma(r.rma_id, got.text),
                  { success: "Rejected.", onDone: refresh });
        },
      }, "Reject"));
  } else if (r.status === "failed") {
    row.append(el("button.btn.small", {
      onclick: async () => {
        const got = await ask({
          title: `Retry ${r.rma_id}`,
          message: `It failed with: ${r.call?.result?.error || "unknown"}. ` +
                   "Retrying is recorded against your name.",
          label: "What was fixed", confirm: "Retry",
        });
        if (!got) return;
        attempt(() => api.retryRma(r.rma_id, got.text),
                { success: (o) => o.ok ? "Executed." : o.reason,
                  onDone: refresh });
      },
    }, "Retry"));
  }
  return row;
}

function shipmentEvents(rmas) {
  const rows = [];
  for (const r of rmas) {
    for (const e of r.shipment?.events || []) {
      rows.push(el("tr", {},
        el("td.mono", { text: r.shipment.tracking_number }),
        el("td", { text: r.shipment.direction }),
        el("td", { text: e.status.replace(/_/g, " ") }),
        el("td", { text: e.location || "" }),
        el("td", { text: e.note || "" }),
        el("td", { text: when(e.at) })));
    }
  }
  return table(["Tracking", "Direction", "Event", "Location", "Note", "At"],
               rows);
}

/* ---------------------------------------------------------- quality */

export async function renderQuality(host, refresh, openCase) {
  const q = await api.quality();
  const group = (title, rows, lede, tone = "") => el("div", {},
    el("h2", {}, title),
    lede ? el("p.lede", {}, lede) : null,
    table(["Notification", "Case", "Certainty", "Finding",
           "Customer's own words", "Status", "Determination", ""],
      rows.map((n) => el("tr", {},
        el("td.mono", { text: n.notification_id }),
        el("td", {},
          el("a", { href: "#", style: "color:var(--accent)",
                    onclick: (e) => { e.preventDefault();
                                      openCase(n.case_id); } },
            n.case_id),
          el("div", { style: "color:var(--text-faint)",
                      text: n.case?.subject || "" })),
        el("td", {}, chip(n.certainty || "-",
                          n.certainty === "confirmed" ? "critical" : "high")),
        el("td", { text: n.body || "" }),
        el("td", {}, el("div.quote", { text: n.customer_statement || "" })),
        el("td", {}, chip(n.status, n.status === "determined" ? "ok" : "high")),
        el("td", {}, n.determination
          ? el("div", {}, n.determination,
              el("div", { style: "color:var(--text-faint);font-size:11px",
                          text: `${n.determined_by} · ` +
                                `${when(n.determined_at)}` }))
          : "-"),
        el("td", {}, n.status === "open"
          ? el("button.btn.small.primary", {
              onclick: async () => {
                const got = await ask({
                  title: "Record a determination",
                  message: "Recorded against your name and changes the case " +
                           "classification. This is a simulated QA review; " +
                           "no reportability decision is made and nothing " +
                           "is filed.",
                  label: "Determination", confirm: "Record",
                });
                if (!got) return;
                attempt(() => api.determine(n.notification_id, got.text),
                        { success: "Recorded.", onDone: refresh });
              },
            }, "Determine")
          : null),
      ))));

  render(host,
    el("h1", {}, "Quality / safety review"),
    el("p.lede", {}, q.mdr_note),
    el("div.cards", {},
      card("Reported injury", q.groups.reported_injury.length),
      card("Possible adverse events", q.groups.possible_adverse_event.length),
      card("QC discrepancies", q.groups.qc_discrepancy.length),
      card("QA document concerns", q.groups.qa_document_concern.length),
      card("Awaiting determination", q.open.length),
    ),
    group("Confirmed reported injury", q.groups.reported_injury,
          "A person has reported harm. Ordinary troubleshooting is stopped " +
          "and the case stays with a representative."),
    group("Possible adverse events and device hazards",
          q.groups.possible_adverse_event,
          "Either a possible reaction, or a device hazard with no injury " +
          "stated. Both are retained for a human determination rather than " +
          "being resolved either way automatically."),
    group("Quality control: product discrepancies", q.groups.qc_discrepancy,
          "The product as received may not match its specification. That is " +
          "a discrepancy to investigate, not an established defect."),
    group("Quality assurance: document concerns",
          q.groups.qa_document_concern,
          "A question about the documentation itself rather than a unit."),
    el("h2", {}, "Complaint categories"),
    table(["Classification", "Cases"],
      (q.complaint_categories || []).map((c) => el("tr", {},
        el("td", { text: (c.classification || "").replace(/_/g, " ") }),
        el("td", { text: String(c.n) })))),
  );
}

/* ------------------------------------------------------------ audit */

export async function renderAudit(host, refresh, openCase) {
  const state = { case_id: "", event_type: "", actor: "", limit: 200 };
  const body = el("div", {});

  async function load() {
    const data = await api.audit(state);
    render(body,
      table(["At", "Event", "Actor", "Case", "Summary"],
        data.events.map((e) => el("tr", {},
          el("td", { text: when(e.at) }),
          el("td", { text: e.event_type.replace(/_/g, " ") }),
          el("td", { text: e.actor || "" }),
          el("td.mono", {}, e.case_id
            ? el("a", { href: "#", style: "color:var(--accent)",
                        onclick: (ev) => { ev.preventDefault();
                                           openCase(e.case_id); } },
                e.case_id)
            : "-"),
          el("td", {}, e.summary || "",
            e.detail ? el("details.trace", {},
              el("summary", {}, "detail"),
              el("pre", { text: JSON.stringify(e.detail, null, 1) })) : null),
        ))));
  }

  const caseInput = el("input", { type: "search", placeholder: "Case id",
                                  style: "width:160px" });
  const typeInput = el("input", { type: "search", placeholder: "Event type",
                                  style: "width:180px" });
  const actorInput = el("input", { type: "search", placeholder: "Actor",
                                   style: "width:140px" });
  const apply = () => {
    state.case_id = caseInput.value.trim();
    state.event_type = typeInput.value.trim();
    state.actor = actorInput.value.trim();
    load();
  };
  for (const i of [caseInput, typeInput, actorInput]) {
    i.addEventListener("change", apply);
  }

  render(host,
    el("h1", {}, "Audit history"),
    el("p.lede", {},
      "Append-only. Nothing in this table is updated or deleted, including " +
      "failed and rejected actions and cases removed by a demonstration " +
      "reset."),
    el("div", { style: "display:flex;gap:6px;margin-bottom:10px" },
      caseInput, typeInput, actorInput,
      el("button.btn", { onclick: apply }, "Filter")),
    body,
  );
  await load();
}
