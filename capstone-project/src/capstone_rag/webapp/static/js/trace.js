const $ = id => document.getElementById(id);
let bundle = null, busy = false, selectedEvent = null, signature = '', paused = false, displayedCount = 0, coordinationSignature='',subjectQuery='';
async function get(url) { const r = await fetch(url); if (!r.ok) throw Error(await r.text()); return r.json(); }
function text(tag, value) { const node = document.createElement(tag); node.textContent = value; return node; }
async function subjects() {
  const data = await get('/api/trace/subjects'), current = $('subject').value;
  const nextSignature = JSON.stringify(data.subjects);
  const active = data.active?.[0];
  if ($('follow-latest').checked && active) {
    const id = active.conversation_id ? `conversation/${active.conversation_id}` : `case/${active.case_id}`;
    if (signature === nextSignature) { $('subject').value = id; return; }
  }
  if (signature === nextSignature) return;
  signature = nextSignature;
  $('subject').replaceChildren(new Option('Select a conversation or case', ''),
    ...data.subjects.filter(s=>!subjectQuery||s.label.toLowerCase().includes(subjectQuery)||`${s.kind}/${s.id}`===current).map(s => new Option(s.label, `${s.kind}/${s.id}`)));
  $('subject').value = current;
  const requested = new URLSearchParams(location.search).get('case');
  if (!current && /^CASE-\d+$/.test(requested||'')) $('subject').value = `case/${requested}`;
  if ($('follow-latest').checked && active) $('subject').value = active.conversation_id ? `conversation/${active.conversation_id}` : `case/${active.case_id}`;
  if (!$('subject').value && $('subject').options.length>1) $('subject').selectedIndex=1;
}
function render() {
  if (!bundle) return;
  updateActive();
  if (paused) { $('pause').textContent = `Resume console (${Math.max(0,bundle.events.length-displayedCount)} new)`; return; }
  const coordination=$('coordination-summary');
  const nextCoordination=JSON.stringify([bundle.case_id,bundle.case?.case_version,(bundle.coordination?.specialist_findings||[]).map(f=>f.finding_id),(bundle.candidate_plans||[]).map(p=>[p.plan_id,p.status])]);
  if(nextCoordination!==coordinationSignature){
  coordinationSignature=nextCoordination;
  coordination.replaceChildren(text('p',`Case version ${bundle.case?.case_version??'—'} · ${bundle.coordination?.specialist_findings?.length||0} specialist findings · ${bundle.candidate_plans?.length||0} candidate states`));
  coordination.append(text('p','Recorded findings retain their original case versions; the run header describes the selected investigation.'));
  const chosen=(bundle.candidate_plans||[]).filter(p=>p.status==='selected');
  for(const p of chosen){const payload=JSON.parse(p.payload);const card=text('p',`${p.parent_plan_id?'Refined decision':'Selected path'} · depth ${payload.depth} · version ${p.based_on_version}: ${payload.action.replaceAll('_',' ')}. ${payload.rationale||''}`);card.className='selected-plan';coordination.append(card);}
  for(const f of bundle.coordination?.specialist_findings||[]){
    const entry=document.createElement('details');entry.append(text('summary',`${f.role.replaceAll('_',' ')} · version ${f.based_on_version}`));
    const payload=JSON.parse(f.payload);
    const readable=payload.verification?`Exact verification: ${payload.verification.verified?'passed':'not established'}. ${payload.verification.reason||''} Advisory candidates: ${payload.candidates?.candidates?.length||0}.`:
      payload.message_type?`${payload.message_type} · customer reply: ${payload.outbound_message?.decision||'review'}`:
      payload.selected_action?`${payload.mode} · selected ${payload.selected_action} · model preference ${payload.model_preferred_action||'no recorded preference'} · ${payload.shared_note_ids?.length||0} shared notes consulted`:
      payload.sources?`${payload.sources.length} applicable public sections; operational controls: ${payload.operational?.actions?.join(', ')||payload.blocked||'see evidence'}`:
      typeof payload.passed==='boolean'?`Independent validation ${payload.passed?'passed':'requires correction'} · ${payload.checks?.length||0} customer drafts checked`:
      payload.outcome||payload.observation||payload.reason||payload.status||`Safety hold: ${payload.hold??'see evidence'}`;
    entry.append(text('p',readable));
    const usage=payload.inference_usage;entry.append(text('p',usage?usage.llm_requested?`Recorded model calls: ${usage.completed_calls} completed · `+usage.calls.map(c=>`${c.role}: ${c.model} @ ${c.endpoint_id}`).join('; '):'Deterministic stage: no model call recorded.':'Historical finding: per-stage model usage was not recorded.'));
    const full=document.createElement('details');full.append(text('summary','Full finding evidence'),text('pre',JSON.stringify(payload,null,2)));
    entry.append(full);coordination.append(entry);
  }
  for(const p of bundle.candidate_plans||[]){
    const payload=JSON.parse(p.payload),entry=document.createElement('details');
    entry.dataset.planStatus=p.status;
    entry.append(text('summary',`${p.status} · ${payload.action.replaceAll('_',' ')} · depth ${payload.depth}`));
    entry.append(text('p','Advisory candidate rationale: '+(payload.rationale||'')),text('p','Independent constraint checks: '+((payload.hard_gate_verdicts||[]).join('; ')||'Hard checks passed; any consequential action still needs its domain approval.')));
    coordination.append(entry);
  }
  }
  const filter = $('category').value;
  const categories = [...new Set(bundle.events.map(e => e.category))];
  $('category').replaceChildren(new Option('All events', ''), ...categories.map(c => new Option(c,c)));
  $('category').value = filter;
  $('context').textContent = `Conversation ${bundle.conversation_id || '—'} · Case ${bundle.case_id || 'not handed off'} · ${bundle.case?.status || 'customer chat'} · ${bundle.events.length} recorded events`;
  $('chat-link').hidden=!bundle.conversation_id;
  if(bundle.conversation_id) $('chat-link').href='/chat?conversation='+encodeURIComponent(bundle.conversation_id);
  const scroll = $('timeline').scrollTop;
  $('timeline').replaceChildren(...bundle.events.filter(e => !filter || e.category === filter).map(event => {
    const item = document.createElement('li'), button = document.createElement('button');
    button.dataset.status = event.status;
    button.dataset.eventId=event.event_id;
    button.setAttribute('aria-pressed',String(event.event_id===selectedEvent));
    button.append(text('time', `[${new Date(event.at).toLocaleTimeString()}]`),
                  text('strong', event.summary), text('small', `${event.category==='model'?'Model invocation':event.category.replaceAll('_',' ')} · ${event.status}${event.duration_ms != null ? ' · ' + Math.round(event.duration_ms) + ' ms' : ''}`));
    button.onclick = () => inspect(event); item.append(button); return item;
  }));
  $('timeline').scrollTop = $('auto-follow').checked ? $('timeline').scrollHeight : scroll;
  displayedCount = bundle.events.length;
  if (selectedEvent) inspect(bundle.events.find(e => e.event_id === selectedEvent) || bundle.events.at(-1));
  else if (bundle.events.length) inspect(bundle.events.at(-1));
}
function inspect(event) { if (!event) return; selectedEvent = event.event_id;
  document.querySelectorAll('#timeline button').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.eventId===event.event_id)));
  $('selected').textContent = `${event.summary} · ${event.status}`;
  $('evidence').textContent = JSON.stringify(event, null, 2);
  const detail = $('structured-evidence'); detail.replaceChildren();
  const entries = {time:event.at, outcome:event.status, category:event.category, ...event.evidence};
  for (const [key,value] of Object.entries(entries)) {
    if(value === null || value === undefined) continue;
    const summary = Array.isArray(value) ? `${value.length} recorded items` : typeof value === 'object' ? Object.entries(value).slice(0,5).map(([k,v])=>`${k}: ${typeof v === 'object' ? 'expand JSON for detail' : v}`).join(' · ') : String(value);
    detail.append(text('dt',key.replaceAll('_',' ')),text('dd',summary.slice(0,500)));
  }
}
function updateActive() {
  if(!bundle) return;
  const start = bundle.events.filter(e=>e.category==='investigation' && e.status==='running').at(-1);
  if(!start) { $('processing-state').textContent='Historical evidence'; $('current-task').textContent='No recorded live investigation'; $('active-run').textContent='—'; $('elapsed').textContent='—'; $('active-model').textContent='No recorded request'; $('active-endpoint').textContent='—'; document.querySelector('.active-strip').classList.remove('running'); return; }
  const events = bundle.events.filter(e=>e.investigation_id===start.investigation_id);
  const finish = events.find(e=>e.category==='investigation' && ['completed','failed'].includes(e.status));
  const last = events.at(-1) || start;
  const model = events.filter(e=>e.category==='inference' && e.evidence?.model).at(-1)||events.filter(e=>e.category==='model' && e.evidence?.model).at(-1);
  $('processing-state').textContent = finish ? finish.status : '● Investigation active';
  $('current-task').textContent = finish ? finish.summary : last.summary;
  $('active-run').textContent = start.investigation_id;
  $('active-model').textContent = model?.evidence?.model || 'No recorded model request';
  $('active-endpoint').textContent=model?.evidence?.endpoint_url||'Not recorded for this run';
  const end = finish ? Date.parse(finish.at) : Date.now();
  $('elapsed').textContent = `${Math.max(0,Math.floor((end-Date.parse(start.at))/1000))}s run` + (finish ? '' : ` · ${Math.max(0,Math.floor((end-Date.parse(last.at))/1000))}s current operation`);
  document.querySelector('.active-strip').classList.toggle('running',!finish);
}
async function refresh() {
  if (busy) return; busy = true;
  try { await subjects(); const subject = $('subject').value;
    if (subject) { bundle = await get('/api/trace/' + subject); render(); $('export').disabled = false; }
    $('live').textContent = 'Updated ' + new Date().toLocaleTimeString() + ' · observing shared backend';
  } catch(error) { $('live').textContent = 'Connection error: ' + error.message; } finally { busy = false; }
}
async function runtime() {
  try { const r = await get('/api/trace/runtime'), m = r.model;
    $('runtime').textContent = `${r.inference_workers} worker · Queue ${r.queue.queued ?? 0} · Ollama ${m.ollama_available ? 'available' : 'unavailable'} · ${m.model} ${m.installed ? 'installed' : 'not installed'} / ${m.loaded ? 'loaded' : 'not loaded'} · ${r.prefer_llm ? 'model enabled' : 'templates selected'}. Usage is recorded per call below.`;
  } catch(error) { $('runtime').textContent = error.message; }
}
$('subject').onchange = () => { selectedEvent = null; $('follow-latest').checked=false; refresh(); };
$('subject-search').oninput = () => {subjectQuery=$('subject-search').value.trim().toLowerCase();signature='';subjects().catch(error=>$('live').textContent=error.message);};
$('follow-latest').onchange = refresh;
$('pause').onclick = () => { paused=!paused; $('pause').textContent=paused?'Resume console':'Pause console'; render(); };
$('category').onchange = render; $('refresh').onclick = refresh;
$('export').onclick = async () => { try {
  const response = await fetch('/api/trace/' + $('subject').value + '/export', { method:'POST' });
  if (!response.ok) throw Error(await response.text()); const result = await response.json();
  $('export-result').textContent = `Saved ${result.json_path} and ${result.markdown_path}`;
} catch(error) { $('export-result').textContent = error.message; } };
await refresh(); runtime().catch(error => { $('runtime').textContent = error.message; });
setInterval(refresh, 2000); setInterval(runtime, 15000); setInterval(updateActive,1000);
try {
  const inventory = await get('/api/trace/qms/inventory');
  const table = document.createElement('table'), head = document.createElement('tr');
  for (const label of ['Document / version', 'Type / status', 'Access', 'Permitted uses', 'Scope / applicability', 'Workflow / approvals']) head.append(text('th', label));
  table.append(head);
  for (const entry of inventory.documents) {
    const row = document.createElement('tr');
    for (const value of [entry.document_id+' v'+entry.document_version,
      entry.document_type+' · '+entry.approval_status,
      entry.access_permissions.join(', '), entry.permitted_uses.join(', '),
      entry.scope+' · '+entry.model+' / '+entry.applicable_revisions.join('/'),
      entry.workflow_associations.join(', ')+' · approvals: '+entry.required_approvals.join(', ')]) row.append(text('td', value));
    table.append(row);
  }
  $('qms-inventory').replaceChildren(text('p', `${inventory.document_count} documents · ${inventory.chunk_count} chunks · ${inventory.policy}`), table);
} catch(error) { $('qms-inventory').textContent = error.message; }
