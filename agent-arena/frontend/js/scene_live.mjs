// Live rooms: which room a display shows, and what the room should show right now.
// Pure functions only (tested in Node, like the location model); the viewer stores the choice
// and the room engine (scene_composer.mjs, live mode) animates from one model to the next.
import{ROOMS}from'./composer_rooms.mjs';
import{locationView,TERMINAL}from'./agent_arena_locations.mjs';

// Heads get the workshop rooms, orchestrators the labs. An application with its own room (ROOMS[id].apps: Capstone's
// Ticketing Operations, Coder's Research Laboratory) always gets that room; the others share the labs without one.
export function roomsFor(kind,locationId=''){
  const type=kind==='orchestrator'?'lab':'shop',app=/^orch:([^/]+)/.exec(locationId)?.[1];
  const own=app?Object.keys(ROOMS).filter(id=>ROOMS[id].type===type&&ROOMS[id].apps?.includes(app)):[];
  return own.length?own:Object.keys(ROOMS).filter(id=>ROOMS[id].type===type&&!ROOMS[id].apps);
}
function hash(text){let h=2166136261;for(const c of text){h^=c.charCodeAt(0);h=Math.imul(h,16777619)}return h>>>0}

// A display picks its room when it first runs and again after each update, unless someone chose
// one with the arrows (pinned), which then stays. Heads are numbered by address, and the step of
// five is coprime with both room counts, so consecutive heads always start in different rooms.
export function chooseRoom(saved,{kind,locationId,version='dev'}){
  const list=roomsFor(kind,locationId),keep=saved&&list.includes(saved.room)&&(saved.pinned||saved.version===version);
  if(keep)return{room:saved.room,pinned:!!saved.pinned,version,shuffle:saved.shuffle||0};
  const n=Number(/(\d+)$/.exec(locationId||'')?.[1]??hash(locationId||''));
  return{room:list[(hash(version)+n*5)%list.length],pinned:false,version,shuffle:saved?.shuffle||0};
}
export function stepRoom(prefs,kind,dir,locationId=''){
  const list=roomsFor(kind,locationId),i=Math.max(0,list.indexOf(prefs.room));
  return{...prefs,room:list[(i+dir+list.length)%list.length],pinned:true};
}
export function reshuffled(prefs){return{...prefs,shuffle:(prefs.shuffle||0)+1}}

const WANT={executing:'executing',completed:'pass',failed:'fail',canceled:'canceled',expired:'canceled'};
const ENDS=new Set(['pass','fail','canceled']);
const agentOf=(state,key)=>state.agents[key]||{display_name:key.split('/').pop(),color:'#9eb8c7'};
// Work that left this head in this batch of updates. Capstone runs each inference inside its task, so a head visit
// usually ends with the task walking home with the answer (assignment.moved, not a completion): that is this
// head's result, pass unless the return is error handling. Before Agent Arena 2.1.7 / Arena 2.3.9 such a visit just vanished ('gone'), so heads
// recorded no results and the mechanics (crates) and the inspector (bulbs) never came. Work that both arrived and
// left between two updates (the adapter publishes up to 200 rows a second) is marked replay: the room still shows it.
function departures(loc,transitions){
  const out=new Map(),arrived=new Set();
  for(const t of transitions){
    if(!t.assignmentKey)continue;
    if(t.kind==='move'&&t.to===loc){arrived.add(t.assignmentKey);out.delete(t.assignmentKey);
      if(TERMINAL.has(t.toState))out.set(t.assignmentKey,{agentKey:t.agentKey,want:WANT[t.toState]})}
    else if(t.kind==='move'&&t.from===loc)out.set(t.assignmentKey,{agentKey:t.agentKey,want:TERMINAL.has(t.toState)?WANT[t.toState]:t.action==='error_handling'?'fail':'pass'});
    else if(t.kind==='state'&&t.location===loc&&TERMINAL.has(t.toState)&&!TERMINAL.has(t.fromState))out.set(t.assignmentKey,{agentKey:t.agentKey,want:WANT[t.toState]});
  }
  return{out,arrived};
}
export const LAB_EXTRA_CAP=12;
// Arena 2.5.0: Capstone's hand-offs in this batch (handoff.sent / handoff.received, with sender and receiver), for the
// room to bring the two agents together. The room ignores the ones whose sender is not one of its figures.
const handoffsOf=transitions=>transitions.filter(t=>t.kind==='handoff'&&t.sender&&t.receiver).map(t=>({key:t.key,sender:t.sender,receiver:t.receiver,status:t.status}));

// What the room shows now. Heads: one actor per assignment located here (waiting, working, or
// finished within its completion beat). Labs: one resident per agent whose home this is: home,
// working here, or away at another location. Lab results come from this batch of transitions
// (a resnapshot never replays them); a head sees its own results as an assignment finishing.
// `shown` (key -> ref, what the room is showing now) keeps each lab resident on the run it is already showing.
export function sceneModel(state,locationId,{transitions=[],labelOf=()=>'',localNow=Date.now(),shown=new Map()}={}){
  const view=locationView(state,locationId,localNow),entries=[...view.floor,...view.bleachers];
  const totals=state.results?.[locationId]?.['8h']||null;
  if(view.kind==='head'){
    const{out,arrived}=departures(locationId,transitions);
    const actors=entries.map(e=>{const a=agentOf(state,e.agentKey),want=WANT[e.state]||'waiting';
      return{key:e.id,ref:e.id,agentKey:e.agentKey,name:a.display_name,color:a.color,body:a.body,palette:a.palette,want,action:e.action,label:labelOf(e),replay:ENDS.has(want)&&arrived.has(e.id)}});
    const here=new Set(actors.map(a=>a.key));
    for(const[key,d]of out){if(here.has(key))continue;const a=agentOf(state,d.agentKey),x=state.assignments[key]||{};
      actors.push({key,ref:key,agentKey:d.agentKey,name:a.display_name,color:a.color,body:a.body,palette:a.palette,want:d.want,action:x.visual_action,label:'',replay:arrived.has(key)})}
    actors.sort((x,y)=>(x.want==='executing'?0:1)-(y.want==='executing'?0:1));   // working agents first when the room is full
    return{kind:'head',actors,results:[],totals,handoffs:handoffsOf(transitions)};
  }
  // Labs: one resident per agent whose home this is. Capstone's Testing Lab runs many cases at once with the same
  // registered agents, so an agent can hold several live assignments; before 2.1.7 / 2.3.9 the last one won and the resident
  // flickered between them. Now the resident shows one, and each further concurrent assignment brings a colleague
  // (key agent#assignment), up to LAB_EXTRA_CAP, working first.
  const here=new Map(entries.map(e=>[e.id,e])),owned=Object.values(state.agents).filter(a=>a.home_location===locationId&&!a.retired);
  const live=new Map(owned.map(a=>[a.agent_key,[]]));
  for(const x of Object.values(state.assignments))if(!TERMINAL.has(x.state)&&live.has(x.agent_key))live.get(x.agent_key).push(x);
  const entryOf=x=>here.get(x.assignment_key)||{id:x.assignment_key,assignmentKey:x.assignment_key,agentKey:x.agent_key,state:'away',action:x.visual_action,label:x.label,workflow:x.workflow_id,task:x.task_id};
  const wantOf=x=>x.location_id!==locationId?'away':x.state==='executing'?'executing':'home';
  const rank=x=>(wantOf(x)==='executing'?0:wantOf(x)==='away'?1:2);
  const actors=[],extras=[];
  for(const a of owned){
    const keep=shown.get(a.agent_key),list=live.get(a.agent_key).sort((p,q)=>(p.assignment_key===keep?-1:0)-(q.assignment_key===keep?-1:0)||rank(p)-rank(q)||(p.updated_at||0)-(q.updated_at||0));
    const first=list[0],e=first&&entryOf(first);
    actors.push({key:a.agent_key,ref:first?.assignment_key||'idle:'+a.agent_key,agentKey:a.agent_key,name:a.display_name,color:a.color,body:a.body,palette:a.palette,want:first?wantOf(first):'home',action:e?.action,label:e?labelOf(e):''});
    for(const x of list.slice(1))extras.push({key:a.agent_key+'#'+x.assignment_key,ref:x.assignment_key,agentKey:a.agent_key,name:a.display_name,color:a.color,body:a.body,palette:a.palette,want:wantOf(x),action:x.visual_action,label:labelOf(entryOf(x)),rank:rank(x),at:x.updated_at||0});
  }
  extras.sort((p,q)=>p.rank-q.rank||p.at-q.at);
  for(const{rank:_r,at:_a,...x}of extras.slice(0,LAB_EXTRA_CAP))actors.push(x);
  const results=[];
  for(const t of transitions){
    if(!t.assignmentKey||state.agents[t.agentKey]?.home_location!==locationId)continue;
    const now=state.assignments[t.assignmentKey]?.state,before=t.kind==='state'?t.fromState:null;
    const extra=t.agentKey+'#'+t.assignmentKey,key=shown.has(extra)?extra:t.agentKey;   // the colleague showing that run
    if((now==='completed'||now==='failed')&&!TERMINAL.has(before))results.push({key,outcome:now==='completed'?'pass':'fail'});
  }
  return{kind:'lab',actors,results,totals,handoffs:handoffsOf(transitions)};
}
