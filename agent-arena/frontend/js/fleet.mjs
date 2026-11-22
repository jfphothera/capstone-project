// The admin hub's Fleet window: heads by hardware tier with their models, the projects, the catalog, and
// the changes waiting for one Commit. Everything here is read from the console (arena_service.fleet);
// a change only joins the list. Commit asks Windows for an administrator's password, and the Fleet Commit
// helper makes the changes after the operator types APPROVE in its window.
const $=s=>document.querySelector(s);
const TOKEN=sessionStorage.getItem('arena-admin-token');
const el=(tag,props={},...kids)=>{const n=Object.assign(document.createElement(tag),props);n.append(...kids.filter(k=>k!=null&&k!==false));return n};
const pill=(text,cls,title)=>el('span',{className:'pill '+cls,textContent:text,title:title||''});
const ago=t=>{if(!t)return'never';const s=Math.max(0,Date.now()/1000-t);return s<90?`${Math.round(s)} s ago`:s<5400?`${Math.round(s/60)} min ago`:`${Math.round(s/3600)} h ago`};
const gb=n=>n==null?'—':n<1?`${Math.round(n*1000)} MB`:`${n.toFixed(1)} GB`;
const headNo=a=>a.split('.').pop();
const EMBED=/embed|minilm|(^|[\/:-])bge|(^|[\/:-])gte/i;   // as gateway_models.NOT_CHAT and fleet.NOT_CHAT
let view=null,tab='heads',openHeads=new Set(),catOpen=null,busy=false;

async function api(path,body){
  const r=await fetch('/api/'+path,{method:body?'POST':'GET',headers:{'X-Arena-Admin-Token':TOKEN,...(body?{'Content-Type':'application/json'}:{})},body:body?JSON.stringify(body):undefined});
  const data=await r.json().catch(()=>({}));
  if(!r.ok)throw new Error(data.message||data.error||`${r.status}`);
  return data;
}
function fail(e){const p=$('#fl-error');p.hidden=!e;p.textContent=e?e.message||String(e):''}

// ------------------------------------------------------------------ changes (the same words as the helper's window)
const key=c=>JSON.stringify(Object.fromEntries(Object.entries(c).filter(([k])=>k!=='on'&&k!=='applications').sort()));
const OPPOSITE={install:'uninstall',uninstall:'install',allow:'disallow',disallow:'allow'};
function describe(c){
  const h=c.address?`head ${headNo(c.address)}`:'';
  return {allow:`Allow ${c.model} for ${c.app}`,disallow:`Stop allowing ${c.model} for ${c.app}`,
    'head-projects':`${h} (${c.address}) serves: ${(c.applications||[]).join(', ')}`,
    'do-not-use':c.on?`Do not use ${c.model} on ${h}`:`Use ${c.model} on ${h} again`,
    install:`Install ${c.model} on ${h} (an exact copy from the hub's library)`,
    uninstall:`Uninstall ${c.model} from ${h} (out of the Gateway first)`,
    'test-missing':'Test every allowed model that is not usable yet, on each Gateway head that has it'}[c.op]||c.op;
}
async function setPending(list){
  try{fail(null);const r=await api('fleet/pending',{changes:list});view.pending=r.changes;render()}catch(e){fail(e)}
}
function addChange(c){
  let list=(view.pending||[]).slice();
  const opp=OPPOSITE[c.op]&&list.findIndex(x=>key(x)===key({...c,op:OPPOSITE[c.op]}));
  if(opp>=0&&opp!==false){list.splice(opp,1);return setPending(list)}          // undoing a waiting change
  list=list.filter(x=>key(x)!==key(c));
  if(c.op==='do-not-use'||c.op==='head-projects'){
    const h=view.heads.find(x=>x.address===c.address);
    const now=c.op==='do-not-use'?h.do_not_use.includes(c.model):JSON.stringify(h.gateway.applications)===JSON.stringify(c.applications);
    if(c.op==='do-not-use'?now===c.on:now&&h.gateway.in_gateway)return setPending(list);   // back to how it is: nothing to do
  }
  list.push(c);return setPending(list);
}
const waiting=(op,address,model)=>(view.pending||[]).find(c=>c.op===op&&(!address||c.address===address)&&(!model||c.model===model));

// ------------------------------------------------------------------ the strip at the top
function strip(){
  const h={...view.health,pending:(view.pending||[]).length},last=view.published?.last_commit,st=view.commit?.status;
  const items=[pill(`${h.answering} of ${h.heads} heads answering`,h.answering===h.heads?'up':'warn'),
    pill(`${h.in_gateway} in the Gateway`,h.in_gateway?'up':'warn'),
    h.new?pill(`${h.new} new head${h.new>1?'s':''} found`,'warn','answers on the lab cable but is not in Arena or the Gateway yet'):null,
    pill('Gateway '+({ok:'live',stale:'not reporting',not_configured:'not configured for Arena',not_published:'nothing published yet',invalid:'observation unreadable'}[h.gateway]||h.gateway||'?'),h.gateway==='ok'?'up':'warn'),
    h.pending?pill(`${h.pending} change${h.pending>1?'s':''} waiting for Commit`,'warn'):pill('no changes waiting','not-enrolled'),
    st&&['checking','waiting-approval','running'].includes(st.state)?pill('Commit running: '+st.state.replace('-',' '),'warn'):
      last?pill(`last Commit ${ago(last.finished_at)}: ${last.ok?'all done':'with problems'}`,last.ok?'up':'down',last.summary):null,
    !view.helper_installed?pill('Commit helper not installed (Administration hub > 5)','down'):null,
    !view.catalog.present?pill('catalog not installed','down'):null];
  $('#fl-strip').replaceChildren(...items.filter(Boolean));
  $('#fl-summary').textContent=view.published?`Gateway state as of ${ago(view.published.written_at)}`:'Gateway state not published yet';
  const pend=$('#fl-pending');pend.hidden=!h.pending;pend.textContent=h.pending;
  const top=$('#fleet-pill');top.className='pill fleet-pill '+(h.answering<h.heads||h.new||h.gateway!=='ok'?'warn':'up');
  top.textContent=`Fleet ${h.answering}/${h.heads}`+(h.pending?` · ${h.pending} waiting`:'');
}

// ------------------------------------------------------------------ heads, grouped by tier
function modelState(h,m){
  if(h.do_not_use.includes(m))return['blocked','Do not use','marked Do not use on this head'];
  if(h.gateway.qualified.includes(m))return['up','usable','passed the Gateway\'s test here'];
  const key=`${h.resource_id} ${m}`,f=(view.failures||{})[key];
  if(f||view.failed_tests.includes(key)){
    const when=f&&f.at?` (test of ${new Date(f.at*1000).toLocaleString()})`:'';
    if(f&&f.kind==='rule')return['warn','Gateway rule',f.reason+when];
    if(f&&f.kind==='retest')return['warn','retest',f.reason+when];
    return['down','failed',(f?f.reason:'failed the Gateway\'s test here')+when];
  }
  if(h.too_large.includes(m))return['warn','plan: no','the plan spreadsheet says NO for this tier (too large or not wanted here)'];
  return['not-enrolled','installed','installed; not tested by the Gateway (no project allows it here, or not tested yet)'];
}
function headCard(h){
  const showEmbed=$('#fl-embed').checked,open=openHeads.has(h.address);
  const models=h.models.filter(m=>showEmbed||!EMBED.test(m.name));
  const g=h.gateway;
  const state=!h.reachable?pill('not answering','down','its Ollama did not answer the hub over the lab cable'):
    h.late?pill(`slow: last answer ${ago(h.seen_at)}`,'warn','it did not answer the last scan in time; it answered within the last 3 minutes'):
    h.new?pill('new','warn','not in Arena or the Gateway yet'):g.in_gateway?pill('Gateway '+(g.health||'?'),g.health==='healthy'?'up':'warn'):pill('not in the Gateway','warn');
  const title=el('button',{className:'fl-head-title',onclick:()=>{open?openHeads.delete(h.address):openHeads.add(h.address);render()},'aria-expanded':String(open)},
    el('strong',{textContent:`Head ${headNo(h.address)}`}),el('span',{className:'addr',textContent:h.address}),state,
    el('span',{className:'hint',textContent:[h.tier.id||'tier unknown',h.tier.ram_gb?`${h.tier.ram_gb} GB RAM`:'',h.tier.gpu?`${h.tier.gpu} ${h.tier.vram_gb} GB`:'',h.ollama?`Ollama ${h.ollama}`:''].filter(Boolean).join(' · ')}),
    el('span',{className:'fl-serves',textContent:g.applications.length?'serves '+g.applications.join(', '):'serves no project'}),
    el('span',{className:'fl-count',textContent:`${h.models.length} models`}));
  const chips=el('div',{className:'fl-chips'},...models.map(m=>{
    const [cls,word,why]=modelState(h,m.name),un=waiting('uninstall',h.address,m.name);
    return el('span',{className:`chip ${cls}${un?' leaving':''}`,title:`${m.name}: ${why}${un?' · will be uninstalled at Commit':''}`,textContent:m.name});
  }),...(view.pending||[]).filter(c=>c.op==='install'&&c.address===h.address).map(c=>el('span',{className:'chip arriving',title:'will be installed at Commit',textContent:'+ '+c.model})));
  const card=el('div',{className:'fl-head'+(open?' open':'')},title,chips);
  if(open)card.append(headDetail(h));
  return card;
}
function headDetail(h){
  const d=el('div',{className:'fl-detail'});
  const projects=view.projects.filter(p=>p.in_gateway);
  const want=(waiting('head-projects',h.address)||{}).applications||h.gateway.applications;
  const boxes=projects.map(p=>el('label',{},el('input',{type:'checkbox',checked:want.includes(p.app_id),onchange:e=>{
    const set=new Set(want);e.target.checked?set.add(p.app_id):set.delete(p.app_id);
    if(!set.size&&!h.gateway.in_gateway){setPending((view.pending||[]).filter(c=>!(c.op==='head-projects'&&c.address===h.address)));return}
    if(!set.size){fail(new Error('A head in the Gateway serves at least one project. To stop using a head, mark its models Do not use.'));render();return}
    addChange({op:'head-projects',address:h.address,applications:[...set].sort()})}}),' '+p.app_id));
  d.append(el('h3',{textContent:h.gateway.in_gateway?'Projects this head serves':'Add this head to the Gateway for'}),el('div',{className:'row'},...boxes,
    !h.reachable?el('span',{className:'hint',textContent:'It must answer on the lab cable when you Commit.'}):null));
  const rows=h.models.map(m=>{
    const [cls,word,why]=modelState(h,m.name),blocked=h.do_not_use.includes(m.name);
    const dnu=waiting('do-not-use',h.address,m.name),un=waiting('uninstall',h.address,m.name);
    return el('tr',{},el('td',{className:'mono',textContent:m.name}),el('td',{},pill(word,cls,why)),el('td',{className:'hint',textContent:cls==='up'?'':why}),el('td',{textContent:gb((m.size||0)/1e9)}),
      el('td',{className:'act'},
        el('button',{className:'small'+(dnu?' on':''),textContent:dnu?(dnu.on?'Do not use: waiting':'Use again: waiting'):blocked?'Use again':'Do not use',
          title:'Keeps the model installed but the Gateway stops sending work for it to this head',onclick:()=>addChange({op:'do-not-use',address:h.address,model:m.name,on:!blocked})}),
        el('button',{className:'small danger'+(un?' on':''),textContent:un?'Uninstall: waiting':'Uninstall',onclick:()=>addChange({op:'uninstall',address:h.address,model:m.name})})));
  });
  d.append(el('h3',{textContent:`Models on head ${headNo(h.address)}`}),el('table',{className:'grid'},el('thead',{},el('tr',{},...['Model','State','Why','Size',''].map(t=>el('th',{textContent:t})))),
    el('tbody',{},...(rows.length?rows:[el('tr',{},el('td',{colSpan:5,className:'hint',textContent:h.reachable?'No models installed.':'Not answering: models unknown.'}))]))));
  // install: the catalog's models for this tier first
  const have=new Set(h.models.map(m=>m.name));
  const fits=view.catalog.models.filter(m=>!have.has(m.name)&&h.tier.id&&m.tiers[h.tier.id]==='YES');
  const others=view.catalog.models.filter(m=>!have.has(m.name)&&!(h.tier.id&&m.tiers[h.tier.id]==='YES'));
  const sel=el('select',{'aria-label':'Model to install'},el('optgroup',{label:h.tier.id?`Planned for ${h.tier.id}`:'Catalog'},...fits.map(m=>el('option',{value:m.name,textContent:`${m.name} · ${gb(m.download_gb)}`}))),
    el('optgroup',{label:'Other catalog models (not planned for this tier)'},...others.map(m=>el('option',{value:m.name,textContent:`${m.name} · ${gb(m.download_gb)}${h.tier.id&&m.tiers[h.tier.id]==='NO'?' · plan: no':''}`}))));
  d.append(el('h3',{textContent:'Install a model'}),el('div',{className:'row'},sel,el('button',{textContent:'Add to changes',onclick:()=>sel.value&&addChange({op:'install',address:h.address,model:sel.value})}),
    fits.length?el('button',{textContent:`Add all ${fits.length} planned`,title:fits.map(m=>m.name).join(', '),onclick:async()=>{for(const m of fits)await addChange({op:'install',address:h.address,model:m.name})}}):
      el('span',{className:'hint',textContent:h.tier.id?`Every model planned for ${h.tier.id} is installed here.`:'Its hardware tier is unknown, so no model is planned for it: choose its Tier below.'}),
    el('span',{className:'hint',textContent:'The hub downloads it once over its Wi-Fi and copies the identical model to the head over the lab cable.'})));
  // tier
  const tsel=el('select',{'aria-label':'Tier',onchange:async e=>{try{await api('fleet/tier',{address:h.address,tier:e.target.value||null});refresh()}catch(err){fail(err)}}},
    el('option',{value:'',textContent:'From its report or the lab\'s head list'}),...Object.entries(view.catalog.tiers).map(([id,label])=>el('option',{value:id,textContent:label,selected:h.tier.source.startsWith('chosen')&&h.tier.id===id})));
  d.append(el('h3',{textContent:'Tier'}),el('div',{className:'row'},el('span',{textContent:`${h.tier.label} (${h.tier.source})`}),tsel));
  return d;
}
function renderHeads(){
  // One list in head-number order (grouping by tier listed them 6, 3-5, 7-10, 2); each card names its tier.
  const heads=[...view.heads].sort((a,b)=>Number(headNo(a.address))-Number(headNo(b.address)));
  $('#fl-heads').replaceChildren(...(heads.length?[el('section',{className:'fl-tier'},el('h2',{textContent:`Heads · ${heads.length}`}),...heads.map(headCard))]:
    [el('p',{className:'hint',textContent:'No heads yet: Arena knows none and the scan found none.'})]));
  $('#fl-scan-at').textContent=view.scan.running?'scanning the head addresses…':`last scan ${ago(view.scan.at)}; every minute`;
}

// ------------------------------------------------------------------ projects
function renderProjects(){
  // Embedding models are offered too, marked as such: Capstone 6.0.0-rc.4's retrieval needs one allowed for capstone
  // (Fleet Commit tests them with a real embedding). Chat models are listed first.
  const catalogNames=view.catalog.models.map(m=>m.name);
  const installed=[...new Set(view.heads.flatMap(h=>h.models.map(m=>m.name)))];
  $('#fl-projects').replaceChildren(...view.projects.map(p=>{
    const card=el('div',{className:'fl-project'});
    card.append(el('div',{className:'row'},el('strong',{textContent:p.name}),el('span',{className:'mono hint',textContent:p.app_id}),
      p.in_gateway?pill(p.enabled?'in the Gateway':'disabled in the Gateway',p.enabled?'up':'warn'):pill('not connected yet','not-enrolled',p.note)));
    if(!p.in_gateway){card.append(el('p',{className:'hint',textContent:p.note}));return card}
    const allowed=p.models.map(m=>{const off=waiting('disallow',null,m)&&(view.pending.find(c=>c.op==='disallow'&&c.app===p.app_id&&c.model===m));
      return el('span',{className:'chip up'+(off?' leaving':'')},m,el('button',{className:'x',title:`Stop allowing ${m} for ${p.app_id}`,textContent:'×',onclick:()=>addChange({op:'disallow',app:p.app_id,model:m})}))});
    const adding=(view.pending||[]).filter(c=>c.op==='allow'&&c.app===p.app_id).map(c=>el('span',{className:'chip arriving',textContent:'+ '+c.model}));
    const options=[...new Set([...installed,...catalogNames])].filter(n=>!p.models.includes(n)).sort((a,b)=>(EMBED.test(a)-EMBED.test(b))||a.localeCompare(b));
    const sel=el('select',{'aria-label':'Model to allow'},...options.map(n=>el('option',{value:n,textContent:n+(EMBED.test(n)?' (embedding)':'')+(installed.includes(n)?' (on a head)':'')})));
    card.append(el('h3',{textContent:'Models it may use'}),el('div',{className:'fl-chips'},...allowed,...adding),
      el('div',{className:'row'},sel,el('button',{textContent:'Allow',onclick:()=>sel.value&&addChange({op:'allow',app:p.app_id,model:sel.value})})));
    // Every head: those in the Gateway, and those the scan found that are not yet (ticking one adds it). 1.4.5 listed
    // only Gateway heads here, so newly provisioned heads 7-10 could not be chosen (DESKTOP-REDACT10, 2026-11-07).
    const heads=view.heads.filter(h=>h.gateway.in_gateway||h.reachable||waiting('head-projects',h.address));
    card.append(el('h3',{textContent:'Heads that serve it'}),el('div',{className:'row wrap'},...heads.map(h=>{
      const want=(waiting('head-projects',h.address)||{}).applications||h.gateway.applications,on=want.includes(p.app_id);
      const outside=!h.gateway.in_gateway;
      return el('label',{className:'fl-headbox'+(outside?' dim':''),title:outside?'not in the Gateway yet: ticking adds it at Commit':''},el('input',{type:'checkbox',checked:on,onchange:e=>{
        const set=new Set(want);e.target.checked?set.add(p.app_id):set.delete(p.app_id);
        if(!set.size&&outside){setPending((view.pending||[]).filter(c=>!(c.op==='head-projects'&&c.address===h.address)));return}
        if(!set.size){fail(new Error(`Head ${headNo(h.address)} would serve no project. Leave one project on it, or mark its models Do not use.`));render();return}
        addChange({op:'head-projects',address:h.address,applications:[...set].sort()})}}),` Head ${headNo(h.address)}${outside?' (new)':''}`)})));
    return card;
  }));
}

// ------------------------------------------------------------------ catalog
function renderCatalog(){
  const f=$('#fl-cat-filter').value.trim().toLowerCase(),tiers=Object.keys(view.catalog.tiers);
  const rows=view.catalog.models.filter(m=>!f||m.name.toLowerCase().includes(f)||String(m.capability||'').toLowerCase().includes(f));
  const on=n=>view.heads.filter(h=>h.models.some(m=>m.name===n)).map(h=>headNo(h.address));
  const body=[];
  for(const m of rows){
    body.push(el('tr',{},el('td',{className:'mono',textContent:m.name}),el('td',{textContent:m.capability||''}),el('td',{textContent:gb(m.download_gb)}),
      ...tiers.map(t=>el('td',{className:'tier '+(m.tiers[t]==='YES'?'yes':m.tiers[t]==='NO'?'no':''),textContent:m.tiers[t]==='YES'?'✓':m.tiers[t]==='NO'?'×':''})),
      el('td',{textContent:view.library.includes(m.name)?'yes':''}),el('td',{textContent:on(m.name).join(', ')}),
      el('td',{className:'act'},el('button',{className:'small',textContent:catOpen===m.name?'Close':'Install on…',onclick:()=>{catOpen=catOpen===m.name?null:m.name;render()}}))));
    if(catOpen===m.name){
      const boxes=view.heads.filter(h=>!h.models.some(x=>x.name===m.name)).map(h=>{
        const fits=h.tier.id&&m.tiers[h.tier.id]==='YES';
        return el('label',{className:'fl-headbox'+(fits?'':' dim'),title:fits?'planned for its tier':'not planned for its tier'},el('input',{type:'checkbox',value:h.address,checked:fits&&h.reachable}),` Head ${headNo(h.address)}`)});
      const cell=el('td',{colSpan:6+tiers.length},el('div',{className:'row wrap'},...boxes,el('button',{textContent:'Add to changes',onclick:async e=>{
        for(const b of e.target.closest('td').querySelectorAll('input:checked'))await addChange({op:'install',address:b.value,model:m.name});catOpen=null;render()}})));
      body.push(el('tr',{className:'fl-install-row'},cell));
    }
  }
  $('#fl-catalog').replaceChildren(el('thead',{},el('tr',{},...['Model','Capability','Download',...tiers,'In library','On heads',''].map(t=>el('th',{textContent:t})))),el('tbody',{},...body));
  $('#fl-cat-note').textContent=`${view.catalog.models.length} models Ollama can serve, from the plan spreadsheet${view.catalog.imported_at?' ('+String(view.catalog.imported_at).slice(0,10)+')':''}; ${view.catalog.other_runtime} need another runtime and are not listed.`;
}

// ------------------------------------------------------------------ changes and Commit
function renderChanges(){
  const list=view.pending||[];
  const rows=list.map((c,i)=>el('tr',{},el('td',{textContent:String(i+1)}),el('td',{textContent:describe(c)}),
    el('td',{className:'act'},el('button',{className:'small',textContent:'Remove',onclick:()=>setPending(list.filter((_,j)=>j!==i))}))));
  $('#fl-changes').replaceChildren(el('tbody',{},...(rows.length?rows:[el('tr',{},el('td',{className:'hint',textContent:'No changes waiting. Make them in Heads, Projects or Catalog.'}))])));
  const st=view.commit?.status,running=st&&['checking','waiting-approval','running'].includes(st.state);
  $('#fl-commit').disabled=!list.length||running||busy||!view.helper_installed;
  $('#fl-clear').disabled=!list.length||running;
  $('#fl-test-missing').disabled=running||list.some(c=>c.op==='test-missing');
  $('#fl-commit-note').textContent=!view.helper_installed?'The Commit helper is not installed on this hub yet (provisioner: Administration hub > 5, as LabOperator1).':
    running?{checking:'The helper is checking the list…','waiting-approval':'Type APPROVE in the Fleet Commit window (it may be behind this one).',running:'Making the changes; this window shows the progress.'}[st.state]:'';
  const run=$('#fl-run');run.hidden=!st;
  if(st){
    const word={checking:'checking',['waiting-approval']:'waiting for APPROVE in its window',running:'running',done:st.ok?'done':'done, with problems',failed:'stopped',declined:'cancelled: nothing changed',refused:'refused: nothing changed'}[st.state]||st.state;
    $('#fl-run-state').textContent=word+(st.summary?` · ${st.summary}`:'')+(st.error?` · ${st.error}`:'');
    $('#fl-results').replaceChildren(el('tbody',{},...(st.results||[]).map(r=>el('tr',{},el('td',{},pill(r.ok?'done':'failed',r.ok?'up':'down')),el('td',{textContent:r.change}),el('td',{className:'hint',textContent:r.detail})))));
    const pre=$('#fl-lines'),bottom=pre.scrollTop+pre.clientHeight>=pre.scrollHeight-4;pre.textContent=(st.lines||[]).join('\n');if(bottom)pre.scrollTop=pre.scrollHeight;
  }
}
$('#fl-commit').onclick=async()=>{
  const n=view.pending.length;
  if(!confirm(`Commit ${n} change${n>1?'s':''}?\n\nWindows asks for an administrator's password (LabOperator1). Then the Fleet Commit window lists the changes again and asks you to type APPROVE.`))return;
  busy=true;render();
  try{fail(null);await api('fleet/commit',{});tab='changes'}catch(e){fail(e)}finally{busy=false;refresh()}
};
$('#fl-clear').onclick=()=>{if(confirm('Discard every waiting change?'))setPending([])};
$('#fl-test-missing').onclick=()=>{const list=(view.pending||[]).filter(c=>c.op!=='test-missing');list.push({op:'test-missing'});setPending(list)};
$('#fl-scan').onclick=async()=>{try{await api('fleet/scan',{});setTimeout(refresh,4000)}catch(e){fail(e)}};
$('#fl-embed').onchange=()=>render();
$('#fl-cat-filter').oninput=()=>render();

// ------------------------------------------------------------------ the window
function render(){
  if(!view)return;
  strip();
  if(!$('#dlg-fleet').open)return;
  for(const b of document.querySelectorAll('#dlg-fleet [data-tab]'))b.setAttribute('aria-selected',String(b.dataset.tab===tab));
  for(const s of document.querySelectorAll('#dlg-fleet [data-panel]'))s.hidden=s.dataset.panel!==tab;
  if(tab==='heads')renderHeads();else if(tab==='projects')renderProjects();else if(tab==='catalog')renderCatalog();else renderChanges();
}
for(const b of document.querySelectorAll('#dlg-fleet [data-tab]'))b.onclick=()=>{tab=b.dataset.tab;render()};
for(const b of document.querySelectorAll('[data-fleet]'))b.addEventListener('click',()=>{
  tab=b.dataset.fleet;const d=$('#dlg-fleet');if(!d.open)d.showModal();refresh();
});
async function refresh(){
  if(!TOKEN)return;
  // Keep a click's place: a refresh while the operator is choosing in a list would reset it.
  if($('#dlg-fleet').open&&document.activeElement?.matches?.('#dlg-fleet select'))return;
  try{view=await api('fleet');render()}catch(e){if($('#dlg-fleet').open)fail(e)}
}
if(TOKEN){refresh();setInterval(refresh,4000)}
