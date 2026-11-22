// arena.v2 location model: snapshot + deltas -> per-location bleachers/floor.
// Pure functions only; the renderer animates the transitions this module reports.
// Rules R1-R6 are in ARCHITECTURE.md (location model).

export const TERMINAL=new Set(['completed','failed','canceled','expired']);
export const TERMINAL_GRACE_MS=6000;
const ACTIVE_FLOOR=new Set(['executing']);

export function createLiveState(){return{asOf:0,skewMs:0,apps:{},orchestrators:{},heads:{},agents:{},assignments:{},handoffs:{},feed:[],results:{}}}

function index(rows,key){return Object.fromEntries((rows||[]).map(r=>[r[key],r]))}
function orchKey(o){return`${o.app_id}/${o.orchestrator_id}`}

export function applySnapshot(snapshot,localNow=Date.now()){
  // Authoritative reconstruction: replaces state wholesale and reports no transitions,
  // so restored placement is never animated as new work.
  const s=createLiveState();
  s.asOf=snapshot.as_of_seq||0;s.skewMs=snapshot.server_time?snapshot.server_time*1000-localNow:0;
  s.apps=index(snapshot.applications,'app_id');
  s.orchestrators=Object.fromEntries((snapshot.orchestrators||[]).map(o=>[orchKey(o),{...o}]));
  s.heads=index(snapshot.heads,'node_id');s.agents=index(snapshot.agents,'agent_key');
  s.assignments=index(snapshot.assignments,'assignment_key');
  // Finished work keeps its real age, so a resnapshot never replays a completion beat.
  for(const a of Object.values(s.assignments))if(TERMINAL.has(a.state))a.terminalLocalAt=a.terminal_at&&snapshot.server_time?localNow-(snapshot.server_time-a.terminal_at)*1000:localNow-TERMINAL_GRACE_MS;
  s.handoffs=Object.fromEntries((snapshot.handoffs||[]).map(h=>[`${h.app_id}/${h.correlation_id}`,h]));
  // Rolling pass/fail totals per location (1h/8h/24h), counted by the service from its event history.
  s.results=snapshot.result_counts||{};
  return prune(s,localNow);
}

// Finished work is shown for TERMINAL_GRACE_MS; its record is dropped after TERMINAL_KEEP_MS, and only the newest
// HANDOFF_KEEP handoffs are kept. Before 2.1.3 neither was ever removed: every event copied the whole history and the
// once-a-second render walked it, so memory and CPU grew for as long as the app ran (the heads froze after days).
export const TERMINAL_KEEP_MS=60000,HANDOFF_KEEP=200;
export function prune(s,localNow=Date.now()){
  for(const[k,a]of Object.entries(s.assignments))if(TERMINAL.has(a.state)&&localNow-(a.terminalLocalAt||0)>TERMINAL_KEEP_MS)delete s.assignments[k];
  const hk=Object.keys(s.handoffs);if(hk.length>HANDOFF_KEEP)for(const k of hk.slice(0,hk.length-HANDOFF_KEEP))delete s.handoffs[k];
  return s;
}

export function applyDelta(state,event,localNow=Date.now()){
  // Returns {state,transition}. Events at or below the snapshot sequence are ignored.
  if(!event||event.seq<=state.asOf)return{state,transition:null};
  const s={...state,asOf:event.seq,agents:{...state.agents},assignments:{...state.assignments},orchestrators:{...state.orchestrators},apps:{...state.apps},handoffs:{...state.handoffs}};
  if(event.type!=='orchestrator.heartbeat')s.feed=[{seq:event.seq,app_id:event.app_id,type:event.type,applied:event.applied,note:event.note,label:event.payload?.label,state:event.result?.state,action:event.result?.visual_action,agent:event.result?.agent_key||(event.payload?.agent_id&&`${event.app_id}/${event.payload.agent_id}`)||(event.payload?.sender_agent_id&&`${event.app_id}/${event.payload.sender_agent_id}`),from:event.from_location,to:event.to_location,occurred_at:event.occurred_at,source:event.payload?.source_event_type},...state.feed].slice(0,1000);
  const p=event.payload||{},orch=s.orchestrators[`${event.app_id}/${p.orchestrator_id}`];
  if(orch)s.orchestrators[`${event.app_id}/${p.orchestrator_id}`]={...orch,last_seen:event.received_at,producer_state:'online'};
  if(!event.applied)return{state:prune(s,localNow),transition:null};
  const r=event.result;let transition=null;
  if(event.type==='application.updated'&&s.apps[event.app_id])s.apps[event.app_id]={...s.apps[event.app_id],display_name:p.display_name};
  else if(event.type==='orchestrator.heartbeat'&&orch){const o=s.orchestrators[`${event.app_id}/${p.orchestrator_id}`];if(p.display_name)o.display_name=p.display_name;if(p.heartbeat_seconds)o.heartbeat_seconds=p.heartbeat_seconds}
  else if(event.type.startsWith('agent.')&&r){s.agents[r.agent_key]=r;transition={kind:'registry',agentKey:r.agent_key}}
  else if(event.type.startsWith('handoff.')){const k=`${event.app_id}/${p.correlation_id}`,status=event.type==='handoff.sent'?'sent':'received';s.handoffs[k]={app_id:event.app_id,correlation_id:p.correlation_id,sender_agent_id:p.sender_agent_id,receiver_agent_id:p.receiver_agent_id,status,updated_at:event.received_at};transition={kind:'handoff',key:k,sender:`${event.app_id}/${p.sender_agent_id}`,receiver:`${event.app_id}/${p.receiver_agent_id}`,status,seq:event.seq}}
  else if(r&&r.assignment_key){
    const prior=state.assignments[r.assignment_key],next={...r};
    if(TERMINAL.has(next.state))next.terminalLocalAt=localNow;
    s.assignments[r.assignment_key]=next;
    const from=prior?prior.location_id:null,to=next.location_id;
    if(from!==to)transition={kind:'move',assignmentKey:r.assignment_key,agentKey:r.agent_key,from,to,toState:next.state,action:next.visual_action};
    else transition={kind:'state',assignmentKey:r.assignment_key,agentKey:r.agent_key,location:to,fromState:prior?.state,toState:next.state,fromZone:prior?zoneOf(prior):null,toZone:zoneOf(next)};
  }
  return{state:prune(s,localNow),transition};
}

export function zoneOf(a){return ACTIVE_FLOOR.has(a.state)||TERMINAL.has(a.state)?'floor':'bleachers'}

export function renderState(a,state,localNow=Date.now()){
  // Stale / producer-offline are rendering states, never proof of failure.
  if(TERMINAL.has(a.state))return a.state;
  const serverNow=(localNow+state.skewMs)/1000,o=state.orchestrators[`${a.app_id}/${a.orchestrator_id}`];
  if(o&&(o.last_seen==null||serverNow-o.last_seen>3*(o.heartbeat_seconds||30)))return'producer_offline';
  if(a.lease_expires_at&&serverNow>a.lease_expires_at)return'stale';
  return a.state;
}

function newestActive(state){const n={};for(const a of Object.values(state.assignments))if(!TERMINAL.has(a.state))n[a.agent_key]=Math.max(n[a.agent_key]||0,a.updated_at||0);return n}
// A finished instance lingers for a short completion beat, unless the same agent has
// already moved on to newer active work (it must not appear in two places at once).
function visibleIn(state,localNow){const n=newestActive(state);return a=>!TERMINAL.has(a.state)||(localNow-(a.terminalLocalAt||0)<TERMINAL_GRACE_MS&&!(n[a.agent_key]>=(a.updated_at||0)))}
function locationLabel(state,loc){if(!loc)return'—';if(loc.startsWith('head:')){const h=state.heads[loc.slice(5)];return h?h.display_name:loc.slice(5)}const o=Object.values(state.orchestrators).find(x=>x.location_id===loc);return o?o.display_name:loc}
export{locationLabel};

export function locationView(state,locationId,localNow=Date.now()){
  // R4 orchestrator: its own agents; idle/waiting/away in bleachers, local executing work on the floor.
  // R5 head: only assignments located here; waiting/arrived in bleachers, executing on the floor.
  const shown=visibleIn(state,localNow),here=Object.values(state.assignments).filter(a=>a.location_id===locationId&&shown(a));
  const entry=a=>({id:a.assignment_key,assignmentKey:a.assignment_key,agentKey:a.agent_key,state:a.state,render:renderState(a,state,localNow),action:a.visual_action,waitReason:a.wait_reason,label:a.label,model:a.model_id,workflow:a.workflow_id,task:a.task_id});
  const floor=here.filter(a=>zoneOf(a)==='floor').map(entry);
  let bleachers=here.filter(a=>zoneOf(a)==='bleachers').map(entry);
  if(locationId.startsWith('orch:')){
    const owned=Object.values(state.agents).filter(a=>a.home_location===locationId&&!a.retired),busy=new Set(here.map(a=>a.agent_key));
    const away={};for(const a of Object.values(state.assignments))if(a.location_id!==locationId&&!TERMINAL.has(a.state))(away[a.agent_key]||=[]).push(locationLabel(state,a.location_id));
    for(const agent of owned)if(!busy.has(agent.agent_key)){const idleState=away[agent.agent_key]?'away':'idle';bleachers.push({id:'idle:'+agent.agent_key,agentKey:agent.agent_key,state:idleState,render:renderState({...agent,state:idleState},state,localNow),away:away[agent.agent_key]||[]})}
  }
  return{locationId,kind:locationId.startsWith('head:')?'head':'orchestrator',label:locationLabel(state,locationId),floor,bleachers};
}

export function clusterView(state,localNow=Date.now(),filter=()=>true){
  // Whole-cluster: every executing assignment on the floor; everything else waiting/idle in the bleachers,
  // each labelled with its actual location.
  const shown=visibleIn(state,localNow),active=Object.values(state.assignments).filter(a=>shown(a)&&filter(a,state.agents[a.agent_key]));
  const entry=a=>({id:a.assignment_key,assignmentKey:a.assignment_key,agentKey:a.agent_key,state:a.state,render:renderState(a,state,localNow),action:a.visual_action,waitReason:a.wait_reason,label:a.label,model:a.model_id,where:locationLabel(state,a.location_id),locationId:a.location_id});
  const busy=new Set(active.map(a=>a.agent_key));
  const idle=Object.values(state.agents).filter(a=>!a.retired&&!busy.has(a.agent_key)&&filter(null,a)).map(a=>({id:'idle:'+a.agent_key,agentKey:a.agent_key,state:'idle',render:renderState({...a,state:'idle'},state,localNow),where:locationLabel(state,a.home_location)}));
  return{locationId:'cluster',kind:'cluster',label:'Whole cluster',floor:active.filter(a=>zoneOf(a)==='floor').map(entry),bleachers:[...active.filter(a=>zoneOf(a)==='bleachers').map(entry),...idle]};
}

export function counts(view){
  const all=[...view.floor,...view.bleachers];
  return{active:view.floor.filter(e=>!TERMINAL.has(e.state)).length,waiting:all.filter(e=>['waiting','queued','arrived'].includes(e.state)).length,failed:all.filter(e=>e.state==='failed').length,stale:all.filter(e=>['stale','producer_offline'].includes(e.render)).length,idle:view.bleachers.filter(e=>e.state==='idle'||e.state==='away').length};
}
