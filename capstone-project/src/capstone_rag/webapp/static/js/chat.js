/* The customer chat widget.
 *
 * The conversation id is kept in localStorage and the transcript is reloaded
 * from the server, so closing the tab and coming back resumes the same
 * conversation rather than silently starting an unrelated one. The customer
 * is never shown the identifier, the claim, the verification internals or
 * anything about the model - only the conversation and, when it happens, a
 * clear statement that it has been passed to a person. */

const KEY = "device-support-conversation";

const transcript = document.querySelector("#transcript");
const form = document.querySelector("#composer");
const input = document.querySelector("#input");
const send = document.querySelector("#send");
const handoff = document.querySelector("#handoff");
const presence = document.querySelector("#presence");
const resumeNote = document.querySelector("#resume-note");

let conversationId = null;
let busy = false;
let queueNotice = null;

function bubble(role, text, sources) {
  const node = document.createElement("div");
  node.className = `bubble ${role === "customer" ? "customer" : "assistant"}`;
  node.setAttribute('aria-label',role==='customer'?'You':'Phothera Bot');
  node.textContent = text;
  if (sources?.length) {
    const s = document.createElement("div");
    s.className = "sources";
    s.textContent = 'Based on the applicable user guide.';
    node.append(s);
  }
  transcript.append(node);
  transcript.scrollTop = transcript.scrollHeight;
  return node;
}

function typing(on) {
  document.querySelector(".typing")?.remove();
  if (!on) return;
  const node = document.createElement("div");
  node.className = "typing";
  node.setAttribute('role','status');node.setAttribute('aria-label','Phothera Bot is composing a reply');
  node.innerHTML = "<i></i><i></i><i></i>";
  transcript.append(node);
  transcript.scrollTop = transcript.scrollHeight;
}

function setBusy(on) {
  busy = on;
  send.disabled = on;
  input.disabled = on;
  clearTimeout(queueNotice);
  presence.textContent = on ? "Checking Gateway availability…" : "";
  if (on) queueNotice=setTimeout(()=>{presence.textContent="Waiting in the Gateway queue…";},1500);
  typing(on);
}

function showHandoff(caseRef) {
  handoff.hidden = false;
  handoff.innerHTML = "";
  const strong = document.createElement("b");
  strong.textContent = "This conversation has been passed to our support team. ";
  handoff.append(strong, document.createTextNode(
    `Your reference is ${caseRef}. Everything you have told me has gone with ` +
    `it, and someone will come back to you. You can keep adding anything ` +
    `else you would like them to know.`));
}

async function post(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const value=await res.json();
  if (!res.ok) throw new Error(typeof value.detail==='string'?value.detail:`Request failed (${res.status})`);
  return value;
}

async function startNew() {
  const data = await post("/api/chat/sessions");
  conversationId = data.conversation_id;
  localStorage.setItem(KEY, conversationId);
  history.replaceState(null,'',location.pathname);
  transcript.replaceChildren();
  handoff.hidden = true;
  resumeNote.textContent = "";
  bubble("assistant", data.opening);
}

async function resume() {
  const requested=new URLSearchParams(location.search).get('conversation');
  const saved = /^CONV-\d+$/.test(requested||'') ? requested : localStorage.getItem(KEY);
  if (!saved) return false;
  const res = await fetch(`/api/chat/sessions/${encodeURIComponent(saved)}`);
  if (!res.ok) return false;
  const data = await res.json();
  conversationId = data.conversation_id;
  localStorage.setItem(KEY,conversationId);
  transcript.replaceChildren();
  if (!data.turns.length) bubble("assistant", data.opening);
  for (const t of data.turns) bubble(t.role, t.text);
  if (data.handed_off) showHandoff(data.case_reference);
  resumeNote.textContent = "Continuing your earlier conversation.";
  return true;
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text || busy) return;
  input.value = "";
  bubble("customer", text);
  setBusy(true);
  try {
    const data = await post(
      `/api/chat/sessions/${encodeURIComponent(conversationId)}/messages`,
      { text });
    setBusy(false);
    const answer=bubble("assistant", data.reply.text, data.reply.sources);
    if(data.reply.execution?.head){const meta=document.createElement('div');meta.className='sources';meta.textContent=`Answered by head ${data.reply.execution.head.split('-').pop()} · ${data.reply.execution.model}`;answer.append(meta)}
    if (data.reply.handed_off || data.handed_off) {
      showHandoff(data.reply.case_reference || data.case_reference);
    }
  } catch (cause) {
    setBusy(false);
    const error=bubble("assistant",cause?.message ||
           "The inference request failed. Please try again in a moment.");
    error.classList.add('error');error.setAttribute('role','alert');input.value=text;input.focus();
  }
});

document.querySelector("#btn-restart").addEventListener("click", async () => {
  if (busy) return;
  await startNew();
  input.focus();
});

(async function init() {
  try {
    if (!(await resume())) await startNew();
  } catch {
    bubble("assistant",
           "Sorry, support chat is unavailable at the moment. Please try " +
           "again shortly.");
    setBusy(true);
    send.disabled = true;
    return;
  }
  input.focus();
})();
