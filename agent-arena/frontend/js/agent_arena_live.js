// Classic Arcade renderer (arena.v2). Data comes from the local desktop bridge, which
// signs requests to the shared event service. The browser never holds a credential.
import{applySnapshot,applyDelta,locationView,clusterView,counts,locationLabel,TERMINAL}from'./agent_arena_locations.mjs';
import{demoScript}from'./agent_arena_demo_v2.mjs';
import{createIdleClock,firstVisible,ACTION_LABELS,IDLES,standingLayout,doorRoute,depthAt,footPoint}from'./agent_arena_choreography.mjs';
import{sceneModel,chooseRoom,stepRoom,reshuffled}from'./scene_live.mjs';
import{ROOMS}from'./composer_rooms.mjs';

const $=s=>document.querySelector(s),floor=$('#floor'),layer=$('#agent-layer'),transit=$('#transit-layer');
const rosterTpl=$('#roster-template'),actorTpl=$('#actor-template'),rosterEls=new Map(),actorEls=new Map();
let identity=null,state=null,zone=0,generation=0,needSnapshot=true,lastGood=null,demo=null,linkError=null;
const entryCache=new Map(),motionTimers=new Set(),walkerAnimations=new Set();
const idleClock=createIdleClock('arena:'+Date.now()),idlePoses=new Map(),reducedMotion=matchMedia('(prefers-reduced-motion: reduce)');
let visibleSeats=[],selectedEntry=null,viewKey='',feedKey='',journeyTimer=null;
let holdingPage=0,standingOrder=[];
const STATE_TEXT={executing:'working',waiting:'waiting',queued:'queued',arrived:'arrived',completed:'completed',failed:'failed',canceled:'canceled',expired:'expired',idle:'idle',away:'away',stale:'status unknown',producer_offline:'producer offline'};
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
// The last snapshot this display received, kept on this computer. A display that starts, or is rebuilt, while the
// event service is unreachable shows it marked offline (the room's "be right back"), as a display that stayed up does.
// Until 2.1.5 it showed the empty work floor and a text banner: heads 7 and 8, restarted by FIX-LAB-HEAD while the hub
// was off (2026-11-10), looked like the old Arena while the heads that had stayed up showed their rooms offline.
const LAST_KNOWN='arena.lastKnown.v1';
function saveLastKnown(snap){try{const s=JSON.stringify({at:Date.now(),identity,snap});if(s.length<4e6)localStorage.setItem(LAST_KNOWN,s);else localStorage.removeItem(LAST_KNOWN)}catch{}}
function loadLastKnown(){try{const d=JSON.parse(localStorage.getItem(LAST_KNOWN)||'null');return d&&d.identity&&d.snap&&!d.identity.demo?d:null}catch{return null}}
// Only an unreachable service shows the last known state; a refused or untrusted credential must not look live.
const NO_CACHE=new Set(['not_enrolled','not_authorized','service_identity']);

async function api(path){
  let r;try{r=await fetch(path,{cache:'no-store'})}catch(e){const x=new Error('Local Arena runtime is not responding');x.code='local_runtime';throw x}
  let body=null;try{body=await r.json()}catch{}
  if(!r.ok){const e=new Error(body?.message||`HTTP ${r.status}`);e.status=r.status;e.code=body?.error||'http_'+r.status;e.origin=body?.origin;throw e}
  return body;
}

// ------------------------------------------------------------------ view selection
function currentLocation(){return identity?.location_id||''}
function selectedView(){
  const scope=$('#scope').value,value=$('#scope-value').value;
  if(scope==='here')return{kind:'location',value:currentLocation()};
  if(scope==='location')return{kind:'location',value:value||currentLocation()};
  if(scope==='app')return{kind:'app',value};if(scope==='role')return{kind:'role',value};
  return{kind:'cluster'};
}
function viewModel(now=Date.now()){
  const v=selectedView();
  if(v.kind==='location')return locationView(state,v.value,now);
  if(v.kind==='app')return{...clusterView(state,now,(a,agent)=>(a?a.app_id:agent?.app_id)===v.value),label:appName(v.value)};
  if(v.kind==='role')return{...clusterView(state,now,(a,agent)=>(agent||state.agents[a?.agent_key])?.role===v.value),label:'Role '+v.value};
  return clusterView(state,now);
}
function appName(id){return state?.apps[id]?.display_name||id}
function agentOf(entry){return state.agents[entry.agentKey]||{display_name:entry.agentKey,color:'#9eb8c7',role:'unregistered',app_id:entry.agentKey.split('/')[0]}}
function matches(entry){const q=$('#agent-search').value.trim().toLowerCase();if(!q)return true;const a=agentOf(entry);return`${a.display_name} ${a.role} ${entry.agentKey} ${appName(a.app_id)}`.toLowerCase().includes(q)}
function updateSelectors(){
  if(!state)return;const scope=$('#scope').value,sel=$('#scope-value'),old=sel.value;let values=[];
  if(scope==='location')values=[...Object.values(state.orchestrators).map(o=>[o.location_id,o.display_name]),...Object.values(state.heads).map(h=>[h.location_id,h.display_name])];
  else if(scope==='app')values=Object.values(state.apps).map(a=>[a.app_id,a.display_name]);
  else if(scope==='role')values=[...new Set(Object.values(state.agents).map(a=>a.role))].map(r=>[r,r]);
  $('#value-wrap').hidden=!values.length;
  const key=values.map(v=>v[0]).join('|');if(sel.dataset.key!==key){sel.replaceChildren(...values.map(([v,l])=>{const o=document.createElement('option');o.value=v;o.textContent=l;return o}));sel.dataset.key=key;if(values.some(v=>v[0]===old))sel.value=old}
}

// ------------------------------------------------------------------ rendering
function robot(){return actorTpl.content.querySelector('svg.avatar').cloneNode(true)}
function bindDetails(el,id){el.onclick=()=>showDetails(id);el.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();showDetails(id)}}}
function rosterElement(id){let el=rosterEls.get(id);if(!el){el=rosterTpl.content.firstElementChild.cloneNode(true);el.dataset.id=id;el.querySelector('.mini-robot').append(robot());bindDetails(el,id);rosterEls.set(id,el)}return el}
function actorElement(id){let el=actorEls.get(id);if(!el){el=actorTpl.content.firstElementChild.cloneNode(true);el.dataset.id=id;const u=document.createElement('div');u.className='unknown';el.append(u);bindDetails(el,id);layer.append(el);actorEls.set(id,el)}return el}
function positions(n){const width=(floor.clientWidth||700)*.58,cols=Math.min(n||1,Math.max(1,Math.floor(width/165))),rows=Math.max(1,Math.ceil(n/cols));return Array.from({length:n},(_,i)=>({x:((i%cols)+.5)/cols*54+37,y:((Math.floor(i/cols)+.5)/rows)*39+38}))}
function capacity(){const width=(floor.clientWidth||700)*.58,height=floor.clientHeight||500;return Math.max(1,Math.floor(width/165)*Math.max(1,Math.floor((height-100)/170)))}
function hostText(entry){return[appName(agentOf(entry).app_id),entry.model,entry.where,entry.task&&entry.task.slice(0,18)].filter(Boolean).join(' · ')}
function statusText(e){return e.render!==e.state?(STATE_TEXT[e.render]||e.render):e.waitReason?'Waiting · '+e.waitReason:e.state==='executing'?(ACTION_LABELS[e.action]||'Working'):(STATE_TEXT[e.render]||e.render)}
function later(fn,ms){const id=setTimeout(()=>{motionTimers.delete(id);fn()},ms);motionTimers.add(id);return id}
function motionClasses(el){return['awaiting-walker','speaking','listening','turn-left'].filter(c=>el.classList.contains(c)).join(' ')}
function clearMotion(){for(const timer of motionTimers)clearTimeout(timer);motionTimers.clear();for(const animation of walkerAnimations)animation.cancel();walkerAnimations.clear();transit.replaceChildren();for(const el of [...actorEls.values(),...rosterEls.values()]){el.classList.remove('awaiting-walker','speaking','listening','turn-left');el.querySelector('.peer-tag')?.remove()}}
function showJourney(text){clearTimeout(journeyTimer);$('#journey-note').textContent=text;$('#journey-note').hidden=false;journeyTimer=setTimeout(()=>{$('#journey-note').hidden=true},4000)}
function showDetails(id){selectedEntry=id;renderDetails()}
function renderDetails(){
  const e=entryCache.get(selectedEntry),panel=$('#agent-details');panel.hidden=!e;if(!e)return;
  const a=agentOf(e);$('#detail-name').textContent=a.display_name;$('#detail-state').textContent=statusText(e);
  const fields=[['Application',appName(a.app_id)],['Role',a.role],['Location',e.where||(e.away?.length?e.away.join(', '):locationLabel(state,currentLocation()))],['Task',e.task],['Workflow',e.workflow],['Model',e.model],['Activity',e.label]];
  $('#detail-fields').replaceChildren(...fields.filter(([,v])=>v).map(([label,value])=>{const d=document.createElement('div'),dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=label;dd.textContent=value;d.append(dt,dd);return d}));
}
function tickIdles(){
  const poses=idleClock.step(visibleSeats,Date.now(),{paused:!!linkError||document.hidden,reduced:reducedMotion.matches});
  for(const e of visibleSeats){const el=rosterEls.get(e.id),next=poses.get(e.id);if(idlePoses.get(e.id)===next)continue;for(const x of IDLES)el?.classList.remove('idle-'+x.name);if(next)el?.classList.add('idle-'+next);if(next)idlePoses.set(e.id,next);else idlePoses.delete(e.id)}
}

function renderRoster(entries){
  const byId=new Map(entries.map(e=>[e.id,e]));standingOrder=standingOrder.filter(id=>byId.has(id));const known=new Set(standingOrder);for(const e of entries)if(!known.has(e.id))standingOrder.push(e.id);
  const layout=standingLayout(floor.clientWidth,floor.clientHeight,entries.length,holdingPage);holdingPage=layout.page;
  visibleSeats=standingOrder.slice(layout.start,layout.end).map(id=>byId.get(id));const shown=new Set(visibleSeats.map(e=>e.id));
  for(const id of idlePoses.keys())if(!shown.has(id))idlePoses.delete(id);
  visibleSeats.forEach((e,i)=>{const a=agentOf(e),el=rosterElement(e.id),pos=layout.positions[i];el.hidden=false;el.style.setProperty('--agent',a.color);
    el.className=`roster-agent ${e.state} ${e.state==='idle'||e.state==='away'?'':'active'} render-${e.render} ${e.waitReason?'wait-'+e.waitReason:''} ${idlePoses.has(e.id)?'idle-'+idlePoses.get(e.id):''} ${motionClasses(el)}`;
    if(el.parentElement!==$('#roster-list'))$('#roster-list').append(el);el.style.left=pos.x+'%';el.style.top=pos.y+'%';el.style.setProperty('--depth',pos.depth);el.style.setProperty('--order',Math.round(pos.y));
    el.title=`${a.display_name} · ${a.role} · ${appName(a.app_id)}${e.label?' · '+e.label:''}`;
    el.setAttribute('aria-label',`${a.display_name}: ${statusText(e)}. Show details`);
    el.querySelector('strong').textContent=a.display_name;
    el.querySelector('.roster-state').textContent=e.state==='away'&&e.render==='away'?'↗ '+e.away.join(', '):statusText(e);
    const small=el.querySelector('small');small.replaceChildren(document.createTextNode(`${a.role} · ${appName(a.app_id)}`));
    if(e.away?.length){const b=document.createElement('span');b.className='away-badge';b.textContent='away · '+e.away.join(', ');small.append(document.createElement('br'),b)}
    else if(e.where){const w=document.createElement('span');w.className='where';w.textContent=' · '+e.where;small.append(w)}
  });
  for(const[id,el]of rosterEls)if(!shown.has(id)){el.remove();rosterEls.delete(id)}
  $('#holding-page').textContent=`Group ${layout.page+1} / ${layout.pages}`;$('#holding-prev').disabled=layout.page===0;$('#holding-next').disabled=layout.page===layout.pages-1;
  $('#holding-overflow').textContent=entries.length?`${visibleSeats.length} / ${entries.length} shown`:'Empty';
  tickIdles();
}

function renderFloor(entries){
  const cap=capacity(),zones=Math.max(1,Math.ceil(entries.length/cap));zone=Math.min(Math.max(0,zone),zones-1);
  const page=entries.slice(zone*cap,zone*cap+cap),pos=positions(page.length),visible=new Set(page.map(e=>e.id));
  page.forEach((e,i)=>{const a=agentOf(e),el=actorElement(e.id);el.hidden=false;
    el.className=`actor ${e.state} act-${e.action||'none'} render-${e.render}${e.waitReason?' wait-'+e.waitReason:''} ${motionClasses(el)}`;
    el.style.setProperty('--agent',a.color);el.style.left=pos[i].x+'%';el.style.top=pos[i].y+'%';
    el.style.setProperty('--depth',depthAt({top:0,height:100},{y:pos[i].y}));
    el.title=`${a.display_name} · ${STATE_TEXT[e.render]||e.render} · ${hostText(e)}${e.label?' · '+e.label:''}`;
    el.setAttribute('aria-label',`${a.display_name}: ${statusText(e)}. Show details`);
    el.querySelector('.actor-name').textContent=a.display_name;el.querySelector('.actor-state').textContent=statusText(e);
    el.querySelector('.actor-host').textContent=hostText(e);el.querySelector('.unknown').textContent=e.render==='producer_offline'?'producer offline':'status unknown';
    el.querySelector('.speech').textContent=''});
  const current=new Set(entries.map(e=>e.id));for(const[id,el]of actorEls){el.hidden=!visible.has(id);if(!current.has(id)&&!el.classList.contains('awaiting-walker')){el.remove();actorEls.delete(id)}}
  $('#empty-floor').hidden=page.length>0;$('#floor-count').textContent=`${entries.length} ON FLOOR · ${Math.max(0,entries.length-page.length)} OTHER ZONES`;
  $('#zone-label').textContent=`Zone ${zone+1} of ${zones}`;$('#offscreen-count').textContent=`${Math.max(0,entries.length-page.length)} in other zones`;$('#zone-prev').disabled=zone===0;$('#zone-next').disabled=zone>=zones-1;
}

// The feed follows the view: one location shows what happened there (an orchestrator also shows
// its own agents wherever they are); an application or role shows only its agents' events.
function feedFilter(){
  const v=selectedView();
  if(v.kind==='location'){const loc=v.value;return f=>f.from===loc||f.to===loc||(loc.startsWith('orch:')&&state.agents[f.agent]?.home_location===loc)}
  if(v.kind==='app')return f=>f.app_id===v.value;
  if(v.kind==='role')return f=>state.agents[f.agent]?.role===v.value;
  return()=>true;
}
function renderFeed(){
  // The feed only grows at its front and is capped: its length and its newest and oldest seq identify it.
  const shown=state.feed.filter(feedFilter()),key=JSON.stringify(selectedView())+`${shown.length}:${shown[0]?.seq}:${shown.at(-1)?.seq}`;if(key===feedKey)return;feedKey=key;
  const host=$('#feed');host.replaceChildren();
  for(const f of shown.slice(0,120)){const item=document.createElement('article');item.className='event'+(f.applied===false?' not-applied':'');
    const time=document.createElement('time');time.textContent=`#${f.seq} · ${new Date(f.occurred_at*1000).toLocaleTimeString()} · ${f.type}${f.applied===false?' · '+(f.note||'not applied'):''}`;
    const text=document.createElement('div'),who=document.createElement('strong');who.textContent=f.agent?(state.agents[f.agent]?.display_name||f.agent):appName(f.app_id);
    text.append(who,document.createTextNode(` · ${f.label||[STATE_TEXT[f.state]||f.state,f.action&&f.action.replaceAll('_',' ')].filter(Boolean).join(' · ')||f.type.replaceAll('.',' ')}`));
    if(f.from&&f.to&&f.from!==f.to){const r=document.createElement('span');r.className='route';r.textContent=` · ${locationLabel(state,f.from)} → ${locationLabel(state,f.to)}`;text.append(r)}
    item.append(time,text);host.append(item)}
}

function renderHealth(view){
  const c=counts(view),agents=new Set([...view.floor,...view.bleachers].map(e=>e.agentKey));
  $('#registered-count').textContent=agents.size;$('#idle-count').textContent=view.bleachers.filter(e=>e.state==='idle').length;$('#active-count').textContent=c.active;$('#waiting-count').textContent=c.waiting;$('#failed-count').textContent=c.failed;$('#stale-count').textContent=c.stale;
  const orchs=Object.values(state.orchestrators),online=orchs.filter(o=>o.producer_state==='online'&&(Date.now()/1000+state.skewMs/1000-(o.last_seen||0))<3*(o.heartbeat_seconds||30)).length;
  $('#head-count').textContent=linkError?'unknown':`${online} / ${orchs.length}`;
  const head=view.locationId?.startsWith('head:')?state.heads[view.locationId.slice(5)]:null,t=head?.telemetry;
  const metric=(k,v)=>{const d=document.createElement('div');d.className='resource';d.innerHTML='<span></span><b></b>';d.querySelector('span').textContent=k;d.querySelector('b').textContent=v;return d};
  const pct=v=>v==null?'unavailable':Math.round(v)+'%',gb=v=>v==null?'unavailable':(v/1024**3).toFixed(1)+' GB';
  const rows=t?[['CPU',pct(t.cpu_percent)],['RAM FREE',gb(t.ram_available_bytes)],['GPU',t.gpus?.length?pct(t.gpus[0].util_percent):(t.validity?.gpu||'none')],['OLLAMA',t.ollama?.reachable?'reachable':'not reachable']]:[['HOST TELEMETRY',head?'not reported':'n/a']];
  const res=$('#resources'),resKey=JSON.stringify(rows);if(res.dataset.key!==resKey){res.dataset.key=resKey;res.replaceChildren(...rows.map(([k,v])=>metric(k,v)))}
  const rc=['app','role'].includes(selectedView().kind)?null:state.results?.[view.locationId];   // per location, or the whole cluster
  $('#result-counts').textContent=rc?['1h','8h','24h'].map(w=>`${rc[w]?.pass||0}✓ ${rc[w]?.fail||0}✗`).join(' · '):'—';
  $('#models').textContent=[...new Set(view.floor.map(e=>e.model).filter(Boolean))].join(' · ')||'none in use';
}

function render(transitions=[],snapshot=false){
  if(!state)return;updateSelectors();
  const view=viewModel(),bleachers=view.bleachers.filter(matches),floorEntries=view.floor.filter(matches);
  const nextView=JSON.stringify([selectedView(),zone,holdingPage,$('#agent-search').value]);
  const changed=viewKey!==nextView;if(changed||snapshot){clearMotion();idleClock.reset();idlePoses.clear();if(changed)standingOrder=[];viewKey=nextView}
  // One location is shown as its room; the cluster, application and role views keep the classic floor.
  const sceneOn=selectedView().kind==='location'&&(view.kind==='head'||view.kind==='orchestrator');
  $('.arena').classList.toggle('scene-mode',sceneOn);
  const priorEntries=new Map(entryCache),priorActors=new Map([...actorEls].filter(([,el])=>!el.hidden).map(([id,el])=>[id,actorFoot(el)]));
  entryCache.clear();for(const e of [...bleachers,...floorEntries])entryCache.set(e.id,e);
  if(sceneOn){renderScene(view,transitions,snapshot||changed);renderFeed();renderHealth(view);renderDetails();
    $('#floor-count').textContent=`${floorEntries.length} WORKING · ${bleachers.length} WAITING OR IDLE`;
    $('#location-label').textContent=(view.kind==='head'?'INFERENCE HEAD · ':'ORCHESTRATOR · ')+(view.label||'').toUpperCase();return}
  if(sceneKey)dropScene();
  const before=snapshotRects(transitions,view);
  renderRoster(bleachers);renderFloor(floorEntries);renderFeed();renderHealth(view);
  const loc=selectedView().kind==='location'?view.locationId:null;
  $('#location-label').textContent=(view.kind==='head'?'INFERENCE HEAD · ':view.kind==='orchestrator'?'ORCHESTRATOR · ':'')+(view.label||'').toUpperCase();
  $('#roster-hint').textContent=view.kind==='head'?'Arrived work waiting beside this head.':view.kind==='orchestrator'?'Local agents · away markers show remote work.':'Idle and waiting agents · select one for details.';
  renderDetails();
  animate(transitions,loc,before);
  // Finishing a local task returns the same identity to its standing position.
  // Snapshot reconciliation and view changes are intentionally silent.
  if(!snapshot&&!changed)for(const[id,point]of priorActors){const old=priorEntries.get(id);if(!old||!TERMINAL.has(old.state)||entryCache.has(id))continue;const idle=visibleSeats.find(e=>e.agentKey===old.agentKey&&e.state==='idle');if(idle){const target=rosterEls.get(idle.id);walk(old.agentKey,point,actorFoot(target),{reveal:target})}}
}

// ------------------------------------------------------------------ rooms
// Each location's room is chosen per display and remembered here: picked anew after each update,
// or the one chosen with the arrows, which stays. A new layout only rearranges the furniture.
let sceneKey='',sceneLive=null,sceneLoc=null;
function roomPrefs(loc,kind){
  let saved=null;try{saved=JSON.parse(localStorage.getItem('arena.room.'+loc)||'null')}catch{}
  const p=chooseRoom(saved,{kind,locationId:loc,version:identity?.version||'dev'});if(JSON.stringify(p)!==JSON.stringify(saved))saveRoom(loc,p);return p;
}
function saveRoom(loc,p){try{localStorage.setItem('arena.room.'+loc,JSON.stringify(p))}catch{}}
function renderScene(view,transitions,fresh){
  const loc=view.locationId,kind=view.kind,prefs=roomPrefs(loc,kind),frame=$('#scene-frame'),key=`${loc}|${prefs.room}`;
  sceneLoc={loc,kind};
  if(key!==sceneKey){sceneKey=key;sceneLive=null;frame.hidden=false;
    frame.src='arena-room.html?'+new URLSearchParams({live:'1',room:prefs.room,seed:identity?.display_id||'display',shuffle:prefs.shuffle})}
  $('#room-title').textContent=ROOMS[prefs.room]?.title||prefs.room;$('#room-pinned').textContent=prefs.pinned?'kept':'';
  if(!sceneLive)return;   // still loading: the room asks for a full sync when it is ready
  sceneLive.setOffline(!!linkError);
  const shown=new Map([...(sceneLive.actors||[])].map(([k,a])=>[k,a.ref]));   // residents keep the run they show
  const model=sceneModel(state,loc,{transitions,labelOf:statusText,shown});model.actors=model.actors.filter(a=>matches({agentKey:a.agentKey}));
  sceneLive.sync(model,{instant:fresh});
  $('#scene-overflow').textContent=sceneLive.overflow?`${sceneLive.overflow} more not shown · see the activity feed`:'';
}
function dropScene(){const f=$('#scene-frame'),n=f.cloneNode(false);n.removeAttribute('src');n.hidden=true;f.replaceWith(n);sceneKey='';sceneLive=null;sceneLoc=null}
function changeRoom(dir){if(!sceneLoc)return;const{loc,kind}=sceneLoc;saveRoom(loc,stepRoom(roomPrefs(loc,kind),kind,dir,loc));render()}
function newLayout(){if(!sceneLoc)return;const{loc,kind}=sceneLoc,p=reshuffled(roomPrefs(loc,kind));saveRoom(loc,p);sceneLive?.reshuffle(p.shuffle)}
addEventListener('message',e=>{
  const frame=$('#scene-frame');if(e.source!==frame.contentWindow||e.origin!==location.origin)return;
  if(e.data?.type==='arena-scene-ready'){sceneLive=frame.contentWindow.composer.live;sceneLive.onSelect=id=>{if(entryCache.has(id))showDetails(id)};render([],true)}
  else if(e.data?.type==='arena-key')onKey(e.data.key);
});
function onKey(key,e){
  if((key==='ArrowLeft'||key==='ArrowRight')&&sceneLoc){e?.preventDefault();changeRoom(key==='ArrowLeft'?-1:1);return}
  if((key==='l'||key==='L')&&sceneLoc){sceneLive?.lightSwitch();return}
  if(!window.chrome?.webview)return;
  if(key==='F11'){e?.preventDefault();window.chrome.webview.postMessage({command:'toggleFullscreen'})}else if(key==='Escape')window.chrome.webview.postMessage({command:'exitFullscreen'});
}

// ------------------------------------------------------------------ movement
function visibleActor(key,agentKey){return firstVisible(actorEls.get(key),rosterEls.get(key),rosterEls.get('idle:'+agentKey))}
function actorFoot(el){const svg=el?.querySelector('svg.avatar');return svg?footPoint(svg.getBoundingClientRect()):null}
function snapshotRects(transitions,view){const r={};for(const t of transitions){const el=visibleActor(t.assignmentKey,t.agentKey);if(el)r[t.assignmentKey]=actorFoot(el)}return r}
function walk(agentKey,from,to,{fade=false,reveal=null,via=[],arrival=false}={}){
  if(!from||!to)return;
  if(matchMedia('(prefers-reduced-motion: reduce)').matches){reveal?.classList.remove('awaiting-walker');return}
  if(transit.querySelectorAll('.walker').length>=8){showJourney('More movement · see activity feed');return}
  const a=state.agents[agentKey]||{color:'#9eb8c7'},w=document.createElement('div');w.className='walker actor walking';w.style.setProperty('--agent',a.color);w.append(robot());
  w.style.setProperty('--facing',to.x<from.x?'-1':'1');transit.append(w);reveal?.classList.add('awaiting-walker');
  const bounds=floor.getBoundingClientRect(),points=[from,...via,to];
  const frames=points.map((p,i)=>({left:p.x+'px',top:p.y+'px',transform:`translate(-50%,-95%) scale(${depthAt(bounds,p)})`,opacity:arrival&&i===0?0:1,offset:i/(points.length-1)}));
  // Reach the threshold before fading. Feet stay pinned to the same floor point.
  if(fade)frames.push({...frames.at(-1),opacity:0,offset:1});
  if(fade){for(let i=0;i<frames.length-1;i++)frames[i].offset*=.88}
  const animation=w.animate(frames,{duration:1550,easing:'linear',fill:'forwards'});walkerAnimations.add(animation);
  const finish=()=>{walkerAnimations.delete(animation);w.remove();reveal?.classList.remove('awaiting-walker')};animation.onfinish=finish;animation.oncancel=finish;
}
const center=r=>({x:r.left+r.width/2,y:r.top+r.height/2});
function animate(transitions,loc,before){
  if(document.hidden)return;
  const route=doorRoute(floor.getBoundingClientRect()),gate=route.threshold;
  // Bound effects during event bursts; the feed retains the full event history.
  const batch=transitions.slice(-12);if(transitions.length>12)showJourney(`${transitions.length} updates · see activity feed`);
  for(const t of batch){
    if(t.kind==='handoff'){animateHandoff(t);continue}if(!loc)continue;
    const target=visibleActor(t.assignmentKey,t.agentKey),tr=target?actorFoot(target):null;
    if(t.kind==='move'&&t.to===loc&&tr){const localStart=!t.from&&before[t.assignmentKey];walk(t.agentKey,localStart||gate,tr,{reveal:target,via:localStart?[]:[route.approach],arrival:!localStart})}
    else if(t.kind==='move'&&t.from===loc&&before[t.assignmentKey])walk(t.agentKey,before[t.assignmentKey],gate,{fade:true,via:[route.approach]});
    else if(t.kind==='state'&&t.location===loc&&t.fromZone&&t.fromZone!==t.toZone&&before[t.assignmentKey]&&tr)walk(t.agentKey,before[t.assignmentKey],tr,{reveal:target});
    else if(t.kind==='move'&&(t.from===loc||t.to===loc))showJourney(`${state.agents[t.agentKey]?.display_name||t.agentKey}: ${locationLabel(state,t.from)} → ${locationLabel(state,t.to)}`);
  }
}
function peerElement(agentKey){const matches=[...actorEls.values(),...rosterEls.values()].filter(el=>!el.hidden&&el.isConnected&&entryCache.get(el.dataset.id)?.agentKey===agentKey&&entryCache.get(el.dataset.id)?.state!=='away');return matches.length===1?matches[0]:null}
function peerTag(el,text,kind){if(!el)return;el.querySelector('.peer-tag')?.remove();el.classList.remove('speaking','listening');el.classList.add(kind);const tag=document.createElement('span');tag.className='peer-tag';tag.textContent=text;el.append(tag);later(()=>{if(tag.isConnected){tag.remove();el.classList.remove(kind,'turn-left')}},2200)}
function animateHandoff(t){
  const sender=peerElement(t.sender),receiver=peerElement(t.receiver);if(!sender&&!receiver)return;
  const gate=doorRoute(floor.getBoundingClientRect()).threshold,s=sender?center(sender.getBoundingClientRect()):gate,r=receiver?center(receiver.getBoundingClientRect()):gate;
  const senderName=state.agents[t.sender]?.display_name||t.sender,receiverName=state.agents[t.receiver]?.display_name||t.receiver;
  if(sender&&s.x>r.x)sender.classList.add('turn-left');if(receiver&&r.x>s.x)receiver.classList.add('turn-left');
  peerTag(sender,t.status==='received'?'Delivered':'Sending','speaking');peerTag(receiver,t.status==='received'?'Received':'Incoming','listening');
  showJourney(`${senderName} → ${receiverName} · ${t.status==='received'?'received':'sent'}${!sender||!receiver?' · remote or another zone':''}`);
  if(reducedMotion.matches||transit.querySelectorAll('.packet-route').length>=6)return;
  const route=document.createElement('div');route.className='packet-route';const dx=r.x-s.x,dy=r.y-s.y;
  route.style.left=s.x+'px';route.style.top=(s.y-25)+'px';route.style.width=Math.hypot(dx,dy)+'px';route.style.transform=`rotate(${Math.atan2(dy,dx)}rad)`;transit.append(route);
  const packet=document.createElement('div');packet.className='handoff-packet';packet.style.left=(s.x-9)+'px';packet.style.top=(s.y-31)+'px';transit.append(packet);
  packet.animate([{transform:'translate(0,0)'},{transform:`translate(${dx}px,${dy}px)`}],{duration:850,easing:'ease-in-out',fill:'forwards'});
  later(()=>{route.remove();packet.remove()},1800);
}

// ------------------------------------------------------------------ connection
function setLink(ok,error){
  linkError=ok?null:error;document.body.classList.toggle('disconnected',!ok);
  const c=$('#connection'),banner=$('#link-banner');
  if(ok){lastGood=Date.now();c.textContent=`Live · seq ${state?.asOf??0}`;c.className='online';banner.hidden=true;return}
  const at=lastGood?new Date(lastGood):null,since=at?` · showing last known state from ${at.toDateString()===new Date().toDateString()?at.toLocaleTimeString():at.toLocaleString()}`:'';
  const text={service_unreachable:`Event service unreachable${error.message?' ('+error.message+')':''}${since} · retrying`,
    service_identity:'Event service failed its authenticity check · not trusted · see Diagnostics',not_enrolled:'This display is not enrolled with the event service · run AGENT ARENA DIAGNOSTICS',
    not_authorized:'This display\'s credential was revoked or expired · re-enroll with the installer',local_runtime:'Local Arena runtime is not responding · restart Agent Arena'}[error.code]||`${error.message}${since} · retrying`;
  c.textContent='Disconnected';c.className='offline';banner.textContent=text;banner.className='link-banner'+(error.code==='service_unreachable'?' reconnecting':'');banner.hidden=false;
}

async function liveLoop(){
  const mine=++generation;let backoff=1000,snapAt=0,configAt=0;needSnapshot=true;
  while(mine===generation){
    try{
      if(!identity||identity.demo||Date.now()-configAt>30000){const nextIdentity=await api('/api/local/identity');if(mine!==generation)return;identity=nextIdentity;configAt=Date.now()}
      if(Date.now()-snapAt>60000)needSnapshot=true;   // periodic silent reconciliation (also refreshes head telemetry)
      if(needSnapshot){const snap=await api('/api/v2/snapshot?view=cluster');if(mine!==generation)return;state=applySnapshot(snap);needSnapshot=false;snapAt=Date.now();setLink(true);render([],true);saveLastKnown(snap)}
      const d=await api(`/api/v2/events?after=${state.asOf}&view=cluster&wait=20`);if(mine!==generation)return;
      const transitions=[];for(const e of d.events){const r=applyDelta(state,e);state=r.state;if(r.transition)transitions.push(r.transition)}
      if(d.next>state.asOf)state={...state,asOf:d.next};setLink(true);render(transitions);backoff=1000;
    }catch(e){
      if(mine!==generation)return;
      if(e.status===410||e.code==='resnapshot'){needSnapshot=true;continue}
      let restored=false;
      if(!state&&!NO_CACHE.has(e.code)){const k=loadLastKnown();if(k&&(!identity||k.identity.display_id===identity.display_id)){identity=identity||k.identity;state=applySnapshot(k.snap);lastGood=k.at;restored=true}}
      setLink(false,e);if(state)restored?render([],true):render();
      clearMotion();configAt=0;
      needSnapshot=true;                       // reconcile against authoritative state on return
      await sleep(backoff);backoff=Math.min(10000,backoff*2);
    }
  }
}

function startDemo(){
  generation++;const crowd=Number(new URLSearchParams(location.search).get('crowd'))||0,script=demoScript(undefined,crowd);identity=script.identity;state=applySnapshot(script.snapshot);setLink(true);$('#connection').textContent='Demo replay';
  let i=0;render([],true);demo=setInterval(()=>{if(i>=script.events.length){const again=demoScript(undefined,crowd);state=applySnapshot(again.snapshot);script.events=again.events;i=0;render([],true);return}
    const r=applyDelta(state,script.events[i++]);state=r.state;render(r.transition?[r.transition]:[])},2200);
}
function setMode(){
  clearInterval(demo);const isDemo=$('#mode').value==='demo';$('#demo-badge').hidden=!isDemo;
  $('#mode-label').textContent=isDemo?'DEMO · DETERMINISTIC SYNTHETIC REPLAY':'LIVE · SHARED EVENT SERVICE';
  clearMotion();idleClock.reset();idlePoses.clear();standingOrder=[];holdingPage=0;viewKey='';feedKey='';visibleSeats=[];entryCache.clear();selectedEntry=null;$('#agent-details').hidden=true;$('#feed').replaceChildren();clearTimeout(journeyTimer);$('#journey-note').hidden=true;
  identity=null;state=null;dropScene();rosterEls.forEach(e=>e.remove());actorEls.forEach(e=>e.remove());rosterEls.clear();actorEls.clear();
  isDemo?startDemo():liveLoop();
}

$('#mode').onchange=setMode;$('#scope').onchange=()=>{zone=0;holdingPage=0;render()};$('#scope-value').onchange=()=>{zone=0;holdingPage=0;render()};$('#agent-search').oninput=()=>{zone=0;holdingPage=0;render()};
$('#holding-prev').onclick=()=>{holdingPage--;render()};$('#holding-next').onclick=()=>{holdingPage++;render()};
$('#zone-prev').onclick=()=>{zone--;render()};$('#zone-next').onclick=()=>{zone++;render()};$('#connect').onclick=()=>{if($('#mode').value==='live'){needSnapshot=true;liveLoop()}};
$('#fullscreen').onclick=()=>{if(window.chrome?.webview)window.chrome.webview.postMessage({command:'toggleFullscreen'});else if(document.fullscreenElement)document.exitFullscreen();else document.documentElement.requestFullscreen()};
addEventListener('keydown',e=>{if(e.target.closest?.('input,select,textarea'))return;onKey(e.key,e)});
$('#room-prev').onclick=()=>changeRoom(-1);$('#room-next').onclick=()=>changeRoom(1);$('#room-shuffle').onclick=newLayout;$('#room-lights').onclick=()=>sceneLive?.lightSwitch();
addEventListener('resize',()=>render());setInterval(()=>{if(state)render()},1000);
setInterval(tickIdles,400);document.addEventListener('visibilitychange',()=>{document.body.classList.toggle('presentation-paused',document.hidden);if(document.hidden)clearMotion();tickIdles()});
$('#close-details').onclick=()=>{selectedEntry=null;$('#agent-details').hidden=true;render()};
if(new URLSearchParams(location.search).get('demo')==='1')$('#mode').value='demo';
setMode();
