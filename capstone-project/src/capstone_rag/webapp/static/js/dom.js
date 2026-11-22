/* Small DOM helpers. Deliberately not a framework.
 *
 * Three screens and a handful of panels do not justify a build step, and
 * keeping the project installable with pip alone matters more here than
 * ergonomics. `el` is enough structure to keep the view code declarative. */

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) =>
  Array.from(root.querySelectorAll(sel));

/** el("div.case-row", {onclick}, child, child) */
export function el(spec, attrs, ...children) {
  const [tag, ...classes] = String(spec).split(".");
  const node = document.createElement(tag || "div");
  if (classes.length) node.className = classes.join(" ");
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className += ` ${v}`;
    else if (k === "html") node.innerHTML = v;
    else if (k === "text") node.textContent = v;
    else if (k === "dataset") Object.assign(node.dataset, v);
    else if (k.startsWith("on") && typeof v === "function") {
      node.addEventListener(k.slice(2), v);
    } else if (v === true) node.setAttribute(k, "");
    else node.setAttribute(k, v);
  }
  append(node, children);
  return node;
}

function append(node, children) {
  for (const c of children.flat(4)) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

export function clear(node) {
  while (node.firstChild) node.firstChild.remove();
  return node;
}

export function render(node, ...children) {
  clear(node);
  append(node, children);
  return node;
}

export const chip = (text, kind = "") =>
  el(`span.chip${kind ? "." + kind : ""}`, { text });

export function priorityClass(priority) {
  return { Critical: "critical", High: "high", Medium: "medium", Low: "low" }[priority] || "muted";
}

/** Outcomes have a fixed vocabulary; the mark says what kind it was. */
const MARKS = {
  Completed: "✓",
  "No concern found": "✓",
  "Match found": "✓",
  Approved: "✓",
  Executed: "✓",
  "No match": "✕",
  Failed: "✕",
  Rejected: "✕",
  "Information missing": "?",
  Uncertain: "?",
  Blocked: "■",
  "Human review required": "▲",
  "Not applicable": "–",
};
export const outcomeMark = (outcome) => MARKS[outcome] || "·";

export function when(value) {
  if (!value) return "";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value);
  return d.toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

export function ago(value) {
  if (!value) return "";
  const secs = (Date.now() - new Date(value).getTime()) / 1000;
  if (Number.isNaN(secs)) return "";
  if (secs < 60) return "just now";
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}

export function toast(message, kind = "") {
  const host = $("#toasts");
  if (!host) return;
  const node = el(`div.toast${kind ? "." + kind : ""}`, {
    text: message,
    onclick: () => node.remove(),
  });
  host.append(node);
  setTimeout(() => node.remove(), kind === "error" ? 9000 : 5000);
}

/** A modal that returns what the representative typed, or null if cancelled.
 *  Used wherever a decision must carry a reason - the reason is not optional
 *  for an override, so the UI cannot submit without one. */
export function ask({ title, message, label = "Reason", value = "",
                      confirm = "Confirm", required = true, choices = null,
                      choiceLabel = "Choice", danger = false }) {
  const dialog = $("#prompt-dialog");
  $("#prompt-title").textContent = title;
  $("#prompt-message").textContent = message || "";
  $("#prompt-label").textContent = label;
  const input = $("#prompt-input");
  input.value = value;
  $("#prompt-field").hidden = label === null;
  const selField = $("#prompt-select-field");
  const sel = $("#prompt-select");
  selField.hidden = !choices;
  if (choices) {
    $("#prompt-select-label").textContent = choiceLabel;
    render(sel, choices.map((c) =>
      el("option", { value: c.value, selected: c.selected }, c.label)));
  }
  const ok = $("#prompt-ok");
  ok.textContent = confirm;
  ok.className = danger ? "btn danger" : "btn primary";

  return new Promise((resolve) => {
    const finish = (result) => {
      ok.removeEventListener("click", onOk);
      $("#prompt-cancel").removeEventListener("click", onCancel);
      dialog.removeEventListener("close", onCancel);
      dialog.close();
      resolve(result);
    };
    const onOk = () => {
      if (choices && !sel.value) {
        sel.setCustomValidity('Please select a decision; the explanation does not replace it.');
        sel.reportValidity();
        return;
      }
      sel.setCustomValidity('');
      const text = input.value.trim();
      if (required && label !== null && !text) {
        input.focus();
        input.style.borderColor = "var(--critical)";
        return;
      }
      finish({ text, choice: choices ? sel.value : null });
    };
    const onCancel = () => finish(null);
    ok.addEventListener("click", onOk);
    $("#prompt-cancel").addEventListener("click", onCancel);
    dialog.addEventListener("close", onCancel, { once: true });
    input.style.borderColor = "";
    dialog.showModal();
    input.focus();
  });
}

/** rc.27: words for a refusal the server returned with HTTP 200 and ok:false (never shown as success). */
export function refusalText(out) {
  const reason = out.reason || out.message || out.error || out.detail || "The server refused that action.";
  const kind = out.block_type || out.kind || (out.hard_block ? "hard block" : "");
  return (kind ? `${String(kind).replaceAll("_", " ")}: ` : "") + (typeof reason === "string" ? reason : JSON.stringify(reason));
}

const inFlight = new Set();

/** Run an action, report a refusal rather than failing silently.
 *  rc.27: `key` makes a second click on the same target do nothing while the first is in flight; a 200 response
 *  with ok:false is reported as a refusal, not a success; the refresh follows the server's real outcome. */
export async function attempt(fn, { success, onDone, key } = {}) {
  if (key && inFlight.has(key)) return null;
  if (key) inFlight.add(key);
  try {
    const out = await fn();
    if (out === null || out === undefined) return null;
    if (out && typeof out === "object" && out.ok === false) {
      toast(refusalText(out), "error");
      if (onDone) await onDone(null);
      return null;
    }
    if (success) toast(typeof success === "function" ? success(out) : success);
    if (onDone) await onDone(out);
    return out;
  } catch (err) {
    toast(err.message || "The server refused that action.", "error");
    if (onDone) await onDone(null);
    return null;
  } finally {
    if (key) inFlight.delete(key);
  }
}

/* rc.27: state that must survive live refreshes (open disclosures, typed text), kept per case/panel/target key. */
const openState = new Map();
const textState = new Map();

/** A <details> that remembers being opened or closed by the person across re-renders. */
export function disclosure(key, spec, summary, children, { defaultOpen = false } = {}) {
  const node = el(spec || "details", {}, el("summary", {}, summary), children);
  node.open = openState.has(key) ? openState.get(key) : defaultOpen;
  node.dataset.key = key;
  node.addEventListener("toggle", () => openState.set(key, node.open));
  return node;
}

/** A textarea whose unsent text survives re-renders (cleared with forgetText after the server accepted it). */
export function keptText(key, attrs = {}) {
  const area = el("textarea", attrs);
  if (textState.has(key)) area.value = textState.get(key);
  area.addEventListener("input", () => textState.set(key, area.value));
  area.dataset.keptKey = key;
  return area;
}
export const forgetText = (key) => textState.delete(key);

/** Re-render a host keeping its scroll position and the focused kept field. */
export function renderKeeping(host, ...children) {
  const top = host.scrollTop;
  const focused = document.activeElement && host.contains(document.activeElement) ? document.activeElement.dataset?.keptKey : null;
  // Disclosures without their own key keep the open state they had, matched by their summary text.
  const opened = new Map();
  for (const d of host.querySelectorAll("details:not([data-key])")) {
    const label = d.querySelector(":scope > summary")?.textContent;
    if (label) opened.set(label, d.open);
  }
  render(host, ...children);
  for (const d of host.querySelectorAll("details:not([data-key])")) {
    const label = d.querySelector(":scope > summary")?.textContent;
    if (label && opened.has(label)) d.open = opened.get(label);
  }
  host.scrollTop = top;
  if (focused) {
    const again = host.querySelector(`[data-kept-key="${CSS.escape(focused)}"]`);
    if (again) { again.focus(); const n = again.value.length; try { again.setSelectionRange(n, n); } catch { /* not a text field */ } }
  }
}
