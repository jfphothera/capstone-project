/* The only place that talks to the server.
 *
 * Every mutating call carries the representative's name, because the API
 * requires one: who approved something is part of the approval. The UI never
 * decides whether an action is allowed - it asks, and shows what came back,
 * including a refusal. */

export const REP = { name: "rep.jordan" };

async function request(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  let payload = null;
  const text = await res.text();
  if (text) {
    try { payload = JSON.parse(text); } catch { payload = { detail: text }; }
  }
  if (!res.ok) {
    const detail = payload?.detail ?? res.statusText;
    const message = Array.isArray(detail)
      ? detail.map((d) => d.msg || JSON.stringify(d)).join("; ")
      : String(detail);
    const err = new Error(message);
    err.status = res.status;
    err.payload = payload;
    throw err;
  }
  return payload;
}

const qs = (params) => {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params || {})) {
    if (v !== "" && v !== null && v !== undefined && v !== false) sp.set(k, v);
  }
  const s = sp.toString();
  return s ? `?${s}` : "";
};

const authorised = (extra = {}) => ({ authorizer: REP.name, ...extra });

export const api = {
  addNote:(id,body)=>request('POST',`/api/cases/${id}/notes`,authorised(body)),
  caseTag:(id,body)=>request('POST',`/api/cases/${id}/tags`,authorised(body)),
  contactReview:(id,body)=>request('POST',`/api/contacts/${id}/review`,authorised(body)),
  resumeWorkflow:(id)=>request('POST',`/api/cases/${id}/coordination/resume`,authorised()),
  health: () => request("GET", "/api/health"),
  reference: () => request("GET", "/api/reference"),

  cases: (params) => request("GET", `/api/cases${qs(params)}`),
  case: (id) => request("GET", `/api/cases/${encodeURIComponent(id)}`),
  progress: (id, after = 0) => request("GET", `/api/cases/${encodeURIComponent(id)}/progress?after=${after}`),
  createCase: (body) => request("POST", "/api/cases", body),
  investigate: (id) =>
    request("POST", `/api/cases/${encodeURIComponent(id)}/investigate`),
  setPriority: (id, priority, reason) =>
    request("POST", `/api/cases/${encodeURIComponent(id)}/priority`,
            authorised({ priority, reason })),
  assign: (id, assignee) =>
    request("POST", `/api/cases/${encodeURIComponent(id)}/assign`,
            authorised({ assignee })),
  confirmRevision: (id, decision, note) =>
    request("POST",
            `/api/cases/${encodeURIComponent(id)}/revision-confirmation`,
            authorised({ decision, note })),
  closeCase: (id, resolved, resolution) =>
    request("POST", `/api/cases/${encodeURIComponent(id)}/close`,
            authorised({ resolved, resolution })),

  decideItem: (itemId, decision, feedback) =>
    request("POST", `/api/checklist/${encodeURIComponent(itemId)}/decision`,
            authorised({ decision, feedback })),

  send: (messageId, body, review_override="") =>
    request("POST", `/api/messages/${encodeURIComponent(messageId)}/send`,
            authorised({ body, review_override })),
  rejectDraft: (messageId, note) =>
    request("POST", `/api/messages/${encodeURIComponent(messageId)}/reject`,
            authorised({ note })),
  regenerate: (messageId, feedback) =>
    request("POST", `/api/messages/${encodeURIComponent(messageId)}/regenerate`,
            authorised({ feedback })),

  rmas: (params) => request("GET", `/api/rmas${qs(params)}`),
  approveRma: (id, note) =>
    request("POST", `/api/rmas/${encodeURIComponent(id)}/approve`,
            authorised({ note })),
  rejectRma: (id, note) =>
    request("POST", `/api/rmas/${encodeURIComponent(id)}/reject`,
            authorised({ note })),
  retryRma: (id, note) =>
    request("POST", `/api/rmas/${encodeURIComponent(id)}/retry`,
            authorised({ note })),

  shipments: () => request("GET", "/api/shipments"),
  track: (t) => request("GET", `/api/shipments/${encodeURIComponent(t)}`),
  label: (t) => request("GET", `/api/shipments/${encodeURIComponent(t)}/label`),
  advance: (t, status, location, note) =>
    request("POST", `/api/shipments/${encodeURIComponent(t)}/advance`,
            { status, location: location || "", note: note || "" }),

  quality: () => request("GET", "/api/quality"),
  notifications: (params) => request("GET", `/api/notifications${qs(params)}`),
  determine: (id, determination, rationale) =>
    request("POST",
            `/api/notifications/${encodeURIComponent(id)}/determination`,
            authorised({ determination, rationale })),

  analytics: (params) => request("GET", `/api/analytics${qs(params)}`),
  proposals: (params) => request("GET", `/api/proposals${qs(params)}`),
  simulatedPreview: id => request('POST', `/api/cases/${encodeURIComponent(id)}/simulated-reply-preview`, authorised()),
  replayFeedback: id => request('POST', `/api/feedback/${encodeURIComponent(id)}/replay`, authorised()),
  simulatedReply: (id,text,parent_message_id,continue_investigation=true) => request('POST', `/api/cases/${encodeURIComponent(id)}/simulated-reply`, authorised({text,parent_message_id,continue_investigation})),
  feedback: (id,values) => request('POST', `/api/cases/${encodeURIComponent(id)}/feedback`, authorised(values)),
  returnMethod: (id,method,note) => request('POST', `/api/rmas/${encodeURIComponent(id)}/return-method`, authorised({method,note})),
  receiveReturn: (id,note) => request('POST', `/api/rmas/${encodeURIComponent(id)}/receive`, authorised({note})),
  exchangeProposal: (id,note) => request('POST', `/api/cases/${encodeURIComponent(id)}/exchange-proposal`, authorised({note})),
  approveMovement: (id,note,simulate_failure=false) => request('POST', `/api/rmas/${encodeURIComponent(id)}/approve`, authorised({note,simulate_failure})),
  reviewProposal: (id, decision, note) =>
    request("POST", `/api/proposals/${encodeURIComponent(id)}/review`,
            authorised({ decision, note })),
  reviewMemory: (id, decision, note) =>
    request("POST", `/api/memory/${encodeURIComponent(id)}/review`,
            authorised({ decision, note })),

  audit: (params) => request("GET", `/api/audit${qs(params)}`),

  queue: () => request("GET", "/api/ops/queue"),
  startArrivals: () => request("POST", "/api/ops/arrivals/start"),
  pauseArrivals: () => request("POST", "/api/ops/arrivals/pause"),
  releaseOne: () => request("POST", "/api/ops/arrivals/next"),
  seed: (n) => request("POST", `/api/ops/arrivals/seed${qs({ n })}`),
  resetArrivals: () => request("POST", "/api/ops/arrivals/reset"),

  scenarios: (params) => request("GET", `/api/eval/scenarios${qs(params)}`),
  runScenarios: (body) => request("POST", "/api/eval/scenarios/run", body),
  evalRuns: () => request("GET", "/api/eval/runs"),
  triage: (text) => request("POST", "/api/eval/triage", { text }),
};
