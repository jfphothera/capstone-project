// Admin hub screen: polls the hub's console (arena_service.admin_console) for status and traffic,
// shows them on the room's monitor and the traffic board, and sends the admin's actions back from
// the menu dialogs. Every request carries the token the console printed at start; the page keeps
// it for this tab only.
import {createBoard} from './traffic_board.mjs';
const $=s=>document.querySelector(s);
const STATES={up:'Up','no-telemetry':'No report',warn:'Warning','not-enrolled':'Not enrolled',retired:'Retired'};
const ROLE_HINT={head:'Inference head',orchestrator:'Orchestrator display',admin:'Admin machine: nothing to register'};
const POLL_MS=5000,TRAFFIC_MS=2000;

// The token arrives in the address once, from the launcher; it leaves the address bar at once.
const fromHash=new URLSearchParams(location.hash.slice(1)).get('token');
if(fromHash){sessionStorage.setItem('arena-admin-token',fromHash);history.replaceState(null,'',location.pathname)}
const TOKEN=sessionStorage.getItem('arena-admin-token');

async function api(path,body){
  const r=await fetch('/api/'+path,{method:body?'POST':'GET',headers:{'X-Arena-Admin-Token':TOKEN,...(body?{'Content-Type':'application/json'}:{})},body:body?JSON.stringify(body):undefined});
  const data=await r.json().catch(()=>({}));
  if(!r.ok)throw new Error(data.message||{token_required:'The console did not accept this tab. Open it again from the launcher.',local_only:'Only the hub itself can use this console.'}[data.error]||`${r.status} ${data.error||''}`);
  return data;
}
function showError(e){const el=$('#error');el.hidden=!e;el.textContent=e?e.message||String(e):''}
const el=(tag,props={},...kids)=>{const n=Object.assign(document.createElement(tag),props);n.append(...kids);return n};
const until=t=>{const s=t-Date.now()/1000;return s<5400?`${Math.round(s/60)} min`:`${Math.round(s/3600)} h`};

// ------------------------------------------------------------------ the room's monitor
let roomReady=false,last=null;
addEventListener('message',e=>{if(e.origin===location.origin&&e.data?.type==='arena-scene-ready'){roomReady=true;if(last)toRoom(last)}});
function toRoom(s){
  if(!roomReady)return;
  $('#room').contentWindow.adminRoom?.live?.status({service:{up:s.service.up,version:s.service.version||''},heads:s.heads,issues:s.issues,codes:s.codes,hour:s.hour});
}

// ------------------------------------------------------------------ the traffic board
const boardReady=createBoard($('#board')).catch(e=>{showError(e);return null});
const STILL_KEY='arena-admin-still';
$('#still').checked=localStorage.getItem(STILL_KEY)==='1'||matchMedia('(prefers-reduced-motion: reduce)').matches;
$('#still').onchange=async()=>{localStorage.setItem(STILL_KEY,$('#still').checked?'1':'0');(await boardReady)?.setStill($('#still').checked)};
boardReady.then(b=>b?.setStill($('#still').checked));
async function traffic(){
  const board=await boardReady;if(!board)return;
  try{const t=await api('traffic');board.traffic(t);
    $('#board').setAttribute('aria-label',`Traffic board: ${t.departures.length} out at the heads, ${t.arrivals.length} back recently`)}
  catch(e){board.offline('NOT CONNECTED')}
}

// ------------------------------------------------------------------ menus and dialogs
const menus=[...document.querySelectorAll('.menu')];
function closeMenus(except){for(const m of menus)if(m!==except){m.querySelector('.drop').hidden=true;m.querySelector('.top').setAttribute('aria-expanded','false')}}
for(const m of menus){
  const top=m.querySelector('.top'),drop=m.querySelector('.drop');
  top.onclick=e=>{e.stopPropagation();const open=drop.hidden;closeMenus();drop.hidden=!open;top.setAttribute('aria-expanded',String(open))};
  top.onmouseenter=()=>{if(menus.some(o=>o!==m&&!o.querySelector('.drop').hidden)){closeMenus();drop.hidden=false;top.setAttribute('aria-expanded','true')}};
  drop.onclick=e=>{if(e.target.closest('button'))closeMenus()};
}
addEventListener('click',e=>{if(!e.target.closest('.menu'))closeMenus()});
// Full screen: in the admin hub window (AgentArena.exe --admin) the window itself goes full screen;
// in a browser, the page does. F11 and Esc work while the page has focus.
const shell=window.chrome?.webview;
function toggleFullscreen(){
  if(shell)shell.postMessage('toggleFullscreen');
  else if(document.fullscreenElement)document.exitFullscreen();
  else document.documentElement.requestFullscreen?.();
}
$('#fullscreen').onclick=()=>{closeMenus();toggleFullscreen()};
addEventListener('keydown',e=>{
  if(e.key==='F11'){e.preventDefault();toggleFullscreen()}
  else if(e.key==='Escape'){closeMenus();if(shell)shell.postMessage('exitFullscreen')}
});
function openWindow(id,focus){
  closeMenus();const d=$('#dlg-'+id);if(!d.open)d.showModal();
  if(id==='warnings')warnings();
  if(id==='gateway')gateway();
  if(focus)$('#'+focus)?.focus();
}
for(const b of document.querySelectorAll('[data-open]'))b.addEventListener('click',()=>openWindow(b.dataset.open,b.dataset.focus));
for(const d of document.querySelectorAll('dialog.window')){
  d.querySelector('.close').onclick=()=>d.close();
  d.addEventListener('click',e=>{if(e.target===d)d.close()});      // a click on the backdrop closes it
}

// ------------------------------------------------------------------ the gateway (read only)
// What the Inference Gateway's operator publishes for Arena, refreshed while the dialog is open.
const GW_STATE={ok:'live',stale:'no recent observation',invalid:'observation unreadable',resync:'resynchronize the event cursor',
  not_published:'nothing published yet',not_configured:'not configured'};
const ago=t=>{const s=Math.max(0,Date.now()/1000-t);return s<90?`${Math.round(s)} s ago`:s<5400?`${Math.round(s/60)} min ago`:`${Math.round(s/3600)} h ago`};
const leaseLeft=t=>{const s=Math.round(t-Date.now()/1000);return s>=0?`${s} s left`:`${-s} s past`};
const short=id=>id&&id.length>16?id.slice(0,12)+'…':id||'—';
const pill=(text,cls)=>el('span',{className:'pill '+cls,textContent:text});
function grid(id,head,rows,empty){
  $('#'+id).replaceChildren(el('thead',{},el('tr',{},...head.map(h=>el('th',{textContent:h})))),
    el('tbody',{},...(rows.length?rows.map(r=>el('tr',{},...r.map(c=>c instanceof Node?el('td',{},c):el('td',{textContent:c??'—'}))))
      :[el('tr',{},el('td',{colSpan:head.length,className:'hint',textContent:empty}))])));
}
function facts(id,pairs){$('#'+id).replaceChildren(...pairs.flatMap(([k,v])=>[el('dt',{textContent:k}),el('dd',{textContent:v})]))}
const tally=o=>Object.entries(o).filter(([,n])=>n).map(([k,n])=>`${n} ${k.replaceAll('_',' ')}`).join(' · ')||'none';
function renderGateway(g){
  $('#gw-state').textContent=GW_STATE[g.state]||g.state;
  const none=!('observed_at' in g);$('#gw-body').hidden=none;$('#gw-none').hidden=!none;
  if(none){$('#gw-none').textContent=g.state==='not_configured'?'No gateway observation is configured. Add "gateway": {"observation_file": …} to the service configuration (docs/handoff/GATEWAY-OBSERVATION-INTEGRATION.md).'
    :g.state==='invalid'?`The published observation could not be read: ${g.error}`:'The gateway operator has not published an observation yet.';return}
  facts('gw-facts',[['Observed',ago(g.observed_at)],['Admission',g.admission_open?'open':'closed'],['Queue',`${g.queued} waiting`],
    ['Reservations',`${g.active} active, ${g.executing} executing`],
    ['Allocation',g.utilization==null?'unavailable: no eligible head':`${Math.round(g.utilization*100)} % of eligible heads leased (allocation, not load)`],
    ['Heads',`${g.heads_up} of ${g.heads_total} healthy and reporting`],...(g.dropped?[['Dropped records',String(g.dropped)]]:[]),
    ...(g.error?[['Last read',g.error]]:[])]);
  grid('gw-apps',['Application','Arena name','Authorized','Concurrency','Queue limit','Priority'],
    g.applications.map(a=>[a.app_id,a.arena_name||'not in Arena',a.enabled?'yes':'no',a.max_concurrency,a.queue_limit,a.priority]),'No applications registered.');
  grid('gw-heads',['Resource','Arena head','Health','Reporting','Models','Leased by'],
    g.heads.map(h=>[h.resource_id,h.head_name||'not mapped',pill(h.health,h.available?'up':'warn'),h.fresh?'yes':'no',
      h.qualified_models.join(', ')||'none',h.leased_by.join(', ')||'—']),'No inference heads.');
  grid('gw-models',['Model','Available on','Listed on'],g.models.map(m=>[m.model,m.available_on.join(', ')||'none',m.listed_on.join(', ')]),'No qualified models.');
  grid('gw-leases',['Request','Application','Head','Status','Executing','Lease'],
    g.leases.map(l=>[short(l.request_id),l.app_id,l.head_name||l.resource_id,l.status.replaceAll('_',' '),l.executing?'yes':'no',
      l.overdue?pill('elapsed, not yet reconciled','warn'):leaseLeft(l.expires)]),'No active reservations.');
  grid('gw-quals',['Resource','Model','Result','When'],
    g.qualifications.map(q=>[q.resource_id,q.model,pill(q.passed?'passed':'failed',q.passed?'up':'down'),ago(q.at)]),'No qualification evidence.');
  const failures=(g.request_failures||[]).map(f=>`${f.count} ${f.model||'unknown model'} ${f.kind} on ${f.head_name||f.resource_id||'no head'} (${f.app_id}, last ${ago(f.last_at)})`);
  if(g.request_failures_omitted)failures.push(`${g.request_failures_omitted} more`);
  facts('gw-counts',[['Requests',tally(g.requests)],['Invocations',tally(g.invocations)],
    ['Failed requests, last hour',g.failed_last_hour==null?'not published':failures.join(' · ')||(g.failed_last_hour?`${g.failed_last_hour} (no per-head detail published)`:'none')],
    ['Recent trouble',Object.entries(g.trouble||{}).map(([k,n])=>`${n} ${k}`).join(' · ')||'none in 5 min']]);
  grid('gw-others',['Resource','Type','Health'],g.other_resources.map(r=>[r.resource_id,r.resource_type.replaceAll('_',' '),r.health]),'None.');
  grid('gw-events',['When','Event','Application','Request'],g.recent_events.map(e=>[ago(e.at),e.event_type,e.app_id,short(e.request_id)]),'No events yet.');
}
async function gateway(){if(!$('#dlg-gateway').open)return;try{renderGateway(await api('gateway'))}catch(e){showError(e)}}

// ------------------------------------------------------------------ status
async function render(s){
  last=s;toRoom(s);(await boardReady)?.status(s);
  const svc=$('#service');svc.className='pill '+(s.service.up?(s.issues.length?'warn':'up'):'down');
  svc.textContent=s.service.slow?`Service slow to answer · ${s.service.version||''}`:s.service.up?`Service up · ${s.service.version}`:'Service not answering';   // Arena 2.5.1: one missed check is "slow"
  $('#updated').textContent='Updated '+new Date(s.time*1000).toLocaleTimeString();
  $('#endpoints').textContent=s.endpoints.join(', ')||'—';$('#hour').textContent=`${s.hour.pass} passed · ${s.hour.fail} failed`;
  if(document.activeElement!==$('#endpoints-input'))$('#endpoints-input').value=s.endpoints.join(', ');
  const count=$('#warn-count');count.hidden=!s.issues.length;count.textContent=s.issues.length;
  count.className='badge'+(s.issues.some(i=>i.level==='down')?' down':'');
  $('#issues').replaceChildren(...(s.issues.length?s.issues.map(i=>el('li',{className:i.level},el('strong',{textContent:i.who}),' '+i.what))
    :[el('li',{className:'none',textContent:'Nothing needs attention.'})]));
  const active=s.machines.filter(m=>m.state!=='retired');
  $('#machine-count').textContent=`${active.length} active${s.machines.length>active.length?` · ${s.machines.length-active.length} retired`:''}`;
  $('#machines').replaceChildren(...s.machines.map(row));
  const orch=$('#add-orch'),keep=orch.value;
  orch.replaceChildren(el('option',{value:'',textContent:'Shows which orchestrator?'}),...s.orchestrators.map(o=>el('option',{value:o.location_id,textContent:`${o.name} (${o.location_id})`})));orch.value=keep;
  renderAddresses(s);renderApps(s);
}

// ------------------------------------------------------------------ addresses and applications
const ADDRESS_STATE={reserved:['Reserved','not-enrolled'],claimed:['Claimed','up']};
function renderAddresses(s){
  const claimed=s.addresses.filter(a=>a.status==='claimed').length;
  $('#address-count').textContent=s.addresses.length?`${claimed} claimed · ${s.addresses.length-claimed} waiting`:'';
  const appName=id=>s.applications.find(a=>a.app_id===id)?.name||id;
  grid('addresses',['Address','For','State','Machine',''],s.addresses.map(a=>{
    const [word,cls]=ADDRESS_STATE[a.status]||[a.status,''];
    const machine=a.status==='claimed'?`${a.computer||'—'} · ${new Date(a.claimed_at*1000).toLocaleString()}`:a.code_expires_at?`code waiting · ${until(a.code_expires_at)} left`:'code expired';
    const acts=el('span',{className:'act'});
    if(a.status!=='claimed')acts.append(el('button',{className:'small',textContent:'New code',onclick:()=>reserve(a.address,a.role,a.app_id,a.orchestrator_id,a.label)}));
    acts.append(el('button',{className:'small danger',textContent:'Release',onclick:()=>release(a)}));
    return [a.address,a.role==='head'?(a.label||'Inference head'):`${appName(a.app_id)} (${a.app_id}/${a.orchestrator_id})`,pill(word,cls),machine,acts];
  }),'Nothing reserved. Existing machines are in the list above.');
  const role=$('#reserve-role').value,input=$('#reserve-address');
  if(document.activeElement!==input&&!input.dataset.typed)input.value=s.next_free[role]||'';
  const pick=$('#reserve-app'),keep=pick.value;
  pick.replaceChildren(...s.applications.flatMap(a=>a.orchestrators.map(o=>el('option',{value:`${a.app_id}/${o.orchestrator_id}`,textContent:`${a.name} (${a.app_id}/${o.orchestrator_id})`}))));
  if(keep)pick.value=keep;
  pick.hidden=role!=='app';
}
function renderApps(s){
  grid('apps',['Application','Orchestrator','Laptop','Producer',''],s.applications.flatMap(a=>a.orchestrators.map(o=>[
    `${a.name} (${a.app_id})`,o.orchestrator_id,o.address||'no address claimed',
    pill(o.producer_state==='online'?'online':o.has_credential?'offline':'no credential',o.producer_state==='online'?'up':o.has_credential?'warn':'not-enrolled'),
    el('span',{className:'act'},el('button',{className:'small',textContent:o.code_expires_at?'New app code':'App code',onclick:()=>appCode(a,o)}))])),
    'No applications yet.');
}
function showCode(title,code,hint){
  codeFor=null;$('#code-for').textContent=title;$('#code').textContent=code;$('#code-hint').textContent=hint;
  $('#code-box').hidden=false;$('#code-box').scrollIntoView({block:'nearest'});
}
async function reserve(address,role,app_id,orchestrator_id,label){
  const r=await act('addresses',{address,role,app_id,orchestrator_id,label,hours:72});if(!r)return;
  showCode(`Claim code · ${r.address} · ${role==='head'?'inference head':`${app_id}/${orchestrator_id}`}`,r.code,
    `Valid ${r.hours} h, usable once, only from ${r.address}. Type it into the provisioner on that machine; it is not shown again.`);
}
async function release(a){
  const what=a.status==='claimed'?`${a.computer||'A machine'} claimed it. Releasing frees the address in this list; it does not change the machine or its display.`:'Its unused code stops working.';
  if(confirm(`Release ${a.address}?\n\n${what}`))await act('addresses/release',{address:a.address});
}
async function appCode(a,o){
  if(o.has_credential&&!confirm(`${a.name} already has a credential. A new app code replaces it when used: the current adapter or publisher stops publishing. Continue?`))return;
  const r=await act('app-codes',{app_id:a.app_id,orchestrator_id:o.orchestrator_id,hours:24});if(!r)return;
  showCode(`App code · ${a.name} (${a.app_id}/${o.orchestrator_id})`,r.code,
    `Valid ${r.hours} h, usable once${r.address?`, only from ${r.address}`:''}. Type it into the ${a.app_id==='capstone'?'Ticketing adapter installer':'application\'s Arena publisher'}; it is not shown again.`);
}
$('#reserve-role').onchange=()=>{$('#reserve-address').dataset.typed='';if(last)renderAddresses(last)};
$('#reserve-address').oninput=()=>{$('#reserve-address').dataset.typed='1'};
$('#reserve').onsubmit=async e=>{
  e.preventDefault();const role=$('#reserve-role').value,address=$('#reserve-address').value.trim();
  const [app_id,orchestrator_id]=role==='app'?($('#reserve-app').value||'/').split('/'):[undefined,undefined];
  if(role==='app'&&!app_id){showError(new Error('Add the application first, then reserve its laptop\'s address.'));return}
  await reserve(address,role,app_id,orchestrator_id,$('#reserve-label').value);
  $('#reserve-address').dataset.typed='';$('#reserve-label').value='';
};
$('#add-app').onsubmit=async e=>{
  e.preventDefault();
  const r=await act('apps',{app_id:$('#app-id').value.trim(),name:$('#app-name').value.trim()});
  if(r){$('#app-id').value='';$('#app-name').value=''}
};
function row(m){
  const acts=[];
  if(m.state==='retired')acts.push(el('button',{className:'small',textContent:'Restore',onclick:()=>act('machines/restore',{display_id:m.display_id})}));
  else{
    acts.push(el('button',{className:'small',textContent:m.enrolled?'New code':'Code',title:m.enrolled?'Re-enroll this display (its current credential is replaced when the code is used)':'Issue an enrollment code',onclick:()=>issueCode(m)}));
    acts.push(el('button',{className:'small danger',textContent:'Retire',onclick:()=>retire(m)}));
  }
  const detail=m.code_expires_at?`code waiting · ${until(m.code_expires_at)} left`:m.detail;
  return el('tr',{className:m.state==='retired'?'retired':''},
    el('td',{className:'addr',textContent:m.address||'—'}),
    el('td',{},el('div',{textContent:m.name}),el('div',{className:'detail',textContent:detail})),
    el('td',{},el('span',{className:'pill '+m.state,textContent:STATES[m.state]||m.state})),
    el('td',{className:'act'},...acts));
}
async function act(path,body){try{showError(null);const r=await api(path,body);refresh();return r}catch(e){showError(e)}}   // results show at once; the list catches up
async function retire(m){
  const what=m.kind==='head'?'No new work goes there and its display stops.':'Its display stops.';
  if(!confirm(`Retire ${m.name}?\n\n${what} Restore brings it back.`))return;
  if(await act('machines/retire',{display_id:m.display_id})&&codeFor===m.display_id)$('#code-done').click();   // its code no longer works
}
async function issueCode(m){
  if(m.enrolled&&!confirm(`${m.name} is already enrolled. Issue a new code to re-enroll it?`))return;
  const r=await act('codes',{display_id:m.display_id,hours:Number($('#code-hours').value)});if(!r)return;
  codeFor=m.display_id;$('#code-for').textContent=`${m.name} · ${m.display_id}`;$('#code').textContent=r.code;
  $('#code-hint').textContent=`Valid ${r.hours} h, usable once, only from ${m.address||'its registered address'}. It is not shown again; type it into the Agent Arena installer.`;
  $('#code-box').hidden=false;$('#code-box').scrollIntoView({block:'nearest'});
}
let codeFor=null;
$('#code-done').onclick=()=>{codeFor=null;$('#code').textContent='';$('#code-box').hidden=true};
$('#code-copy').onclick=()=>navigator.clipboard?.writeText($('#code').textContent).then(()=>{$('#code-copy').textContent='Copied'},()=>{});

function roleOf(address){
  const m=/^192\.0\.2\.(\d{1,3})$/.exec(address.trim());if(!m)return null;const n=+m[1];
  return n>=2&&n<=199?'head':n>=200&&n<=249?'orchestrator':n>=250&&n<=254?'admin':null;
}
$('#add-address').oninput=()=>{const r=roleOf($('#add-address').value);$('#add-orch').hidden=r!=='orchestrator';
  $('#add-hint').textContent=r?ROLE_HINT[r]:'Heads .2–.199 · orchestrator displays .200–.249 · admin machines .250–.254'};
$('#add').onsubmit=async e=>{
  e.preventDefault();const address=$('#add-address').value.trim(),role=roleOf(address);
  if(!role){showError(new Error('Enter an address on 192.0.2.x, from .2 to .254.'));return}
  if(role==='orchestrator'&&!$('#add-orch').value){showError(new Error('Choose which orchestrator the display shows.'));return}
  const r=await act('machines',{address,location_id:$('#add-orch').value||undefined});
  if(r){$('#add-address').value='';$('#add-hint').textContent=`Added ${r.display_id}. Next: issue it a code.`}
};
$('#endpoints-form').onsubmit=async e=>{
  e.preventDefault();
  const now=last?.endpoints.join(', ')||'',next=$('#endpoints-input').value.trim();
  if(!next||next===now)return;
  if(confirm(`Point every display at:\n${next}\n\nA wrong address disconnects them until it is fixed here.`))await act('endpoints',{urls:next.split(',').map(s=>s.trim()).filter(Boolean)});
  $('#endpoints-input').blur();
};
$('#backup').onclick=async()=>{const r=await act('backup',{});if(r)$('#backup-result').textContent=`Saved ${r.path} (${Math.round(r.bytes/1024)} KB)`};

async function refresh(){
  try{await render(await api('status'));showError(null)}catch(e){showError(e)}
}
async function warnings(){try{const w=await api('warnings');$('#warnings').textContent=w.lines.join('\n')||'None.'}catch{}}
if(!TOKEN){$('#no-token').hidden=false;$('#service').textContent='Not connected';boardReady.then(b=>b?.offline('NOT CONNECTED'))}
else{refresh();warnings();traffic();setInterval(refresh,POLL_MS);setInterval(warnings,60000);setInterval(traffic,TRAFFIC_MS);setInterval(gateway,TRAFFIC_MS)}
