import test from 'node:test';
import assert from 'node:assert/strict';
import {chooseRoom,stepRoom,reshuffled,roomsFor,sceneModel,LAB_EXTRA_CAP} from '../../frontend/js/scene_live.mjs';
import {applySnapshot,applyDelta} from '../../frontend/js/agent_arena_locations.mjs';
import {ROOMS} from '../../frontend/js/composer_rooms.mjs';

test('heads get workshops and orchestrators get labs',()=>{
  assert.equal(roomsFor('head').length,14);assert.equal(roomsFor('orchestrator').length,6);
  assert.ok(roomsFor('head').every(id=>ROOMS[id].type==='shop'));assert.ok(roomsFor('orchestrator').every(id=>ROOMS[id].type==='lab'));
});

test('each head starts in its own room, and an update picks again unless a room was kept',()=>{
  const heads=[2,3,4,5,6,7,8,9,10].map(n=>chooseRoom(null,{kind:'head',locationId:`head:head-192-0-2-${n}`,version:'2.1.0'}).room);
  assert.equal(new Set(heads).size,9);
  const first=chooseRoom(null,{kind:'head',locationId:'head:head-192-0-2-3',version:'2.1.0'});
  assert.deepEqual(chooseRoom(first,{kind:'head',locationId:'head:head-192-0-2-3',version:'2.1.0'}),first);   // restart: same room
  const picks=new Set(['2.1.1','2.1.2','2.2.0','2.3.0'].map(v=>chooseRoom(first,{kind:'head',locationId:'head:head-192-0-2-3',version:v}).room));
  assert.ok(picks.size>1,'updates pick a new room');
  const kept=stepRoom(first,'head',1);assert.equal(kept.pinned,true);assert.notEqual(kept.room,first.room);
  assert.equal(chooseRoom(kept,{kind:'head',locationId:'head:head-192-0-2-3',version:'9.9.9'}).room,kept.room);   // kept across updates
  assert.equal(stepRoom(stepRoom(kept,'head',-1),'head',1).room,kept.room);
  const list=roomsFor('head');assert.equal(stepRoom({...first,room:list.at(-1)},'head',1).room,list[0]);   // wraps
  assert.equal(reshuffled(kept).shuffle,kept.shuffle+1);assert.equal(reshuffled(kept).room,kept.room);
  const lab=chooseRoom({room:'loading-dock',pinned:true},{kind:'orchestrator',locationId:'orch:capstone/main',version:'2.1.0'});
  assert.equal(ROOMS[lab.room].type,'lab','a saved room of the wrong kind is replaced');
});

test('Capstone and Coder always get their own rooms; other orchestrators share the labs without one',()=>{
  for(const v of ['2.1.0','2.1.6','9.9.9']){
    assert.equal(chooseRoom(null,{kind:'orchestrator',locationId:'orch:capstone/main',version:v}).room,'ticketing-operations');
    assert.equal(chooseRoom(null,{kind:'orchestrator',locationId:'orch:capstone-research/main',version:v}).room,'ticketing-operations');
    assert.equal(chooseRoom(null,{kind:'orchestrator',locationId:'orch:coder/main',version:v}).room,'research-laboratory');
  }
  // A Capstone display that had a random lab saved (before 2.1.6), even pinned, moves to its own room.
  assert.equal(chooseRoom({room:'astronomy-lab',pinned:true,version:'2.1.5'},{kind:'orchestrator',locationId:'orch:capstone/main',version:'2.1.6'}).room,'ticketing-operations');
  // The arrows stay in the app's own room(s).
  const cap=chooseRoom(null,{kind:'orchestrator',locationId:'orch:capstone/main',version:'2.1.6'});
  assert.equal(stepRoom(cap,'orchestrator',1,'orch:capstone/main').room,'ticketing-operations');
  // Nobody else draws an application's own room.
  const others=['orch:lab-traffic/main','orch:qms/main','orch:other/x'].map(l=>chooseRoom(null,{kind:'orchestrator',locationId:l,version:'2.1.6'}).room);
  assert.ok(others.every(r=>!ROOMS[r].apps),others.join());
  assert.ok(!roomsFor('orchestrator').includes('ticketing-operations')&&!roomsFor('orchestrator').includes('research-laboratory'));
});

const NOW=1_800_000_000;
function world(){
  const agent=(id,name)=>({agent_key:'cap/'+id,app_id:'cap',agent_id:id,display_name:name,role:'r',orchestrator_id:'main',home_location:'orch:cap/main',color:'hsl(1 50% 50%)',retired:false});
  return applySnapshot({as_of_seq:10,server_time:NOW,applications:[{app_id:'cap',display_name:'Capstone'}],
    orchestrators:[{app_id:'cap',orchestrator_id:'main',display_name:'Capstone',location_id:'orch:cap/main',last_seen:NOW,heartbeat_seconds:30}],
    heads:[{node_id:'h3',location_id:'head:h3',display_name:'Head 3'}],
    agents:[agent('a','Alpha'),agent('b','Bravo'),agent('c','Charlie')],
    assignments:[{assignment_key:'cap/T1',app_id:'cap',agent_key:'cap/a',orchestrator_id:'main',location_id:'head:h3',state:'executing',visual_action:'inference',updated_at:NOW},
                 {assignment_key:'cap/T2',app_id:'cap',agent_key:'cap/b',orchestrator_id:'main',location_id:'orch:cap/main',state:'executing',visual_action:'review',updated_at:NOW}],
    result_counts:{'orch:cap/main':{'1h':{pass:1,fail:0},'8h':{pass:4,fail:2},'24h':{pass:9,fail:3}}}},NOW*1000);
}
const event=(seq,assignment)=>({seq,app_id:'cap',type:'assignment.state',applied:true,received_at:NOW,occurred_at:NOW,payload:{orchestrator_id:'main'},result:{...assignment,updated_at:NOW+seq}});

test('a head room shows the work located there',()=>{
  const m=sceneModel(world(),'head:h3',{localNow:NOW*1000,labelOf:e=>e.state});
  assert.deepEqual(m.actors.map(a=>[a.key,a.name,a.want,a.label]),[['cap/T1','Alpha','executing','executing']]);
  const s=applyDelta(world(),event(11,{...world().assignments['cap/T1'],state:'completed'}),NOW*1000).state;
  assert.equal(sceneModel(s,'head:h3',{localNow:NOW*1000}).actors[0].want,'pass');
});

// Capstone runs each inference inside its task: the task walks to the head and back home with the answer.
test('a head visit that ends by going home is a result there (crates and bulbs), even inside one batch',()=>{
  let s=world();const steps=[];const go=(seq,patch)=>{const r=applyDelta(s,event(seq,{...s.assignments['cap/T1'],...patch}),NOW*1000);s=r.state;if(r.transition)steps.push(r.transition)};
  go(11,{location_id:'orch:cap/main',state:'executing',visual_action:'evaluation'});
  let m=sceneModel(s,'head:h3',{transitions:steps,localNow:NOW*1000});
  assert.deepEqual(m.actors.map(a=>[a.key,a.want,a.replay]),[['cap/T1','pass',false]],'was shown, now plays its pass');
  assert.deepEqual(sceneModel(s,'head:h3',{localNow:NOW*1000}).actors,[],'the next quiet render has nobody new');
  steps.length=0;go(12,{location_id:'head:h3',state:'executing',visual_action:'inference'});go(13,{location_id:'orch:cap/main',state:'executing',visual_action:'error_handling'});
  m=sceneModel(s,'head:h3',{transitions:steps,localNow:NOW*1000});
  assert.deepEqual(m.actors.map(a=>[a.key,a.want,a.replay]),[['cap/T1','fail',true]],'came and failed between two updates: replayed');
  steps.length=0;go(14,{location_id:'head:h3',state:'executing'});go(15,{location_id:'orch:cap/main'});go(16,{location_id:'head:h3',state:'waiting'});
  m=sceneModel(s,'head:h3',{transitions:steps,localNow:NOW*1000});
  assert.deepEqual(m.actors.map(a=>[a.key,a.want]),[['cap/T1','waiting']],'back again: shown once, as it is now');
});

test('a lab shows every concurrent run of an agent, capped, and keeps each resident on its run',()=>{
  let s=world();
  for(let i=0;i<20;i++)s=applyDelta(s,{...event(11+i,{assignment_key:`cap/R${i}`,app_id:'cap',agent_key:'cap/c',orchestrator_id:'main',location_id:i%2?'head:h3':'orch:cap/main',state:'executing',visual_action:'triage'}),type:'assignment.created'},NOW*1000).state;
  const m=sceneModel(s,'orch:cap/main',{localNow:NOW*1000});
  const c=m.actors.filter(a=>a.agentKey==='cap/c');
  assert.equal(c.length,1+LAB_EXTRA_CAP,'one resident plus capped colleagues');
  assert.equal(new Set(m.actors.map(a=>a.key)).size,m.actors.length,'keys are unique');
  assert.equal(c[0].key,'cap/c');assert.ok(c.slice(1).every(a=>a.key.startsWith('cap/c#cap/R')));
  assert.ok(c.slice(1,8).every(a=>a.want==='executing'),'working colleagues first');
  const keep=sceneModel(s,'orch:cap/main',{localNow:NOW*1000,shown:new Map([['cap/c','cap/R7']])}).actors.find(a=>a.key==='cap/c');
  assert.deepEqual([keep.ref,keep.want],['cap/R7','away'],'the resident stays on the run it shows');
  const shown=new Map([['cap/c#cap/R4','cap/R4']]),r=applyDelta(s,event(40,{...s.assignments['cap/R4'],state:'completed'}),NOW*1000);
  assert.deepEqual(sceneModel(r.state,'orch:cap/main',{transitions:[r.transition],localNow:NOW*1000,shown}).results,[{key:'cap/c#cap/R4',outcome:'pass'}],'the colleague who ran it reports it');
});

test('a lab shows its residents home, working or away, and reports their results',()=>{
  let s=world();const m=sceneModel(s,'orch:cap/main',{localNow:NOW*1000});
  assert.deepEqual(m.actors.map(a=>[a.key,a.want]),[['cap/a','away'],['cap/b','executing'],['cap/c','home']]);
  assert.deepEqual(m.totals,{pass:4,fail:2});assert.deepEqual(m.results,[]);
  const r=applyDelta(s,event(11,{...s.assignments['cap/T1'],state:'failed'}),NOW*1000);s=r.state;
  const after=sceneModel(s,'orch:cap/main',{transitions:[r.transition],localNow:NOW*1000});
  assert.deepEqual(after.results,[{key:'cap/a',outcome:'fail'}]);
  assert.equal(after.actors.find(a=>a.key==='cap/a').want,'home');
  assert.deepEqual(sceneModel(s,'orch:cap/main',{localNow:NOW*1000}).results,[],'no transitions, no results (resnapshot)');
});
