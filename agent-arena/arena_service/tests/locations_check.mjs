// Node checks of the browser location model against real arena.v2 output.
// Usage: node locations_check.mjs <fixture.json>  (fixture written by test_frontend_model.py)
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {pathToFileURL} from 'node:url';
import {resolve,dirname} from 'node:path';

const fixture=JSON.parse(readFileSync(process.argv[2],'utf8'));
const model=await import(pathToFileURL(resolve(fixture.module)).href);
const {applySnapshot,applyDelta,locationView,clusterView,renderState}=model;
const H3='head:head-192-0-2-3',HOME='orch:capstone/main',ERP='capstone/erp_research';

function replay(until){let s=applySnapshot(fixture.initial,0);const moves=[];let at3=null;for(const e of fixture.events){if(until&&e.seq>until)break;const r=applyDelta(s,e,0);s=r.state;if(r.transition?.kind==='move')moves.push(r.transition);if(!at3&&locationView(s,H3,0).floor.some(f=>f.agentKey===ERP))at3={seq:e.seq,orch:locationView(s,HOME,0)}}return{s,moves,at3}}

test('deltas from an empty snapshot reproduce the final service snapshot',()=>{
  const {s}=replay();const fin=applySnapshot(fixture.final,0);
  const keyed=st=>Object.fromEntries(Object.values(st.assignments).map(a=>[a.assignment_key,[a.location_id,a.state]]));
  assert.deepEqual(keyed(s),keyed(fin));assert.equal(Object.keys(s.agents).length,Object.keys(fin.agents).length);
});
test('real ERP inference arrives on head 3 and departs back to Ticketing',()=>{
  const {moves,at3}=replay();const erp=moves.filter(m=>m.agentKey===ERP&&(m.from===H3||m.to===H3));
  assert.deepEqual(erp.map(m=>[m.from,m.to]),[[HOME,H3],[H3,HOME]]);
  assert.ok(at3,'ERP executes on the head 3 floor at some point');
  const away=at3.orch.bleachers.find(b=>b.agentKey===ERP);
  assert.equal(away.state,'away');assert.deepEqual(away.away,['Head 192-0-2-3']);
  assert.ok(!at3.orch.floor.some(f=>f.agentKey===ERP),'not shown working at the orchestrator at the same time');
});
// The viewer gets the adapter's rows in batches (up to 200 a second): a visit can start and end in one.
test('a head room still shows a real visit that came and went inside one batch, as a result (2.1.7)',async()=>{
  const {sceneModel}=await import(pathToFileURL(resolve(dirname(fixture.module),'scene_live.mjs')).href);
  let s=applySnapshot(fixture.initial,0);const all=[];for(const e of fixture.events){const r=applyDelta(s,e,0);s=r.state;if(r.transition)all.push(r.transition)}
  const visit=sceneModel(s,H3,{transitions:all,localNow:0}).actors.filter(a=>a.agentKey===ERP);
  assert.equal(visit.length,1);assert.ok(visit[0].replay,'replayed');assert.ok(['pass','fail'].includes(visit[0].want),visit[0].want);
  assert.deepEqual(sceneModel(s,H3,{localNow:0}).actors.filter(a=>a.agentKey===ERP),[],'and only once');
});
test('heads are empty when no work is assigned (no invented agents)',()=>{
  const {s}=replay();for(const n of [3,6,7]){const v=locationView(s,`head:head-192-0-2-${n}`,1e15);assert.equal(v.floor.length+v.bleachers.length,0)}
});
test('applying a snapshot reports no transitions and old events are ignored',()=>{
  const s=applySnapshot(fixture.final,0);const old=fixture.events[0];const r=applyDelta(s,old,0);assert.equal(r.transition,null);assert.equal(r.state,s);
});
test('stale and producer-offline are render states, not failure',()=>{
  const s=applySnapshot(fixture.final,0);s.skewMs=0;const o=Object.values(s.orchestrators)[0];o.last_seen=Date.now()/1000;o.heartbeat_seconds=30;
  const a={app_id:o.app_id,orchestrator_id:o.orchestrator_id,state:'executing',lease_expires_at:Date.now()/1000-5};
  assert.equal(renderState(a,s,Date.now()),'stale');o.last_seen=Date.now()/1000-1000;assert.equal(renderState(a,s,Date.now()),'producer_offline');assert.equal(a.state,'executing');
});
test('one agent with two concurrent instances appears on both heads, registered once',()=>{
  const s=applySnapshot(fixture.final,0);const base={app_id:'capstone',orchestrator_id:'main',agent_key:ERP,state:'executing'};
  s.assignments['capstone/X3']={...base,assignment_key:'capstone/X3',location_id:H3};s.assignments['capstone/X6']={...base,assignment_key:'capstone/X6',location_id:'head:head-192-0-2-6'};
  assert.equal(locationView(s,H3,0).floor.length,1);assert.equal(locationView(s,'head:head-192-0-2-6',0).floor.length,1);
  assert.equal(Object.values(s.agents).filter(a=>a.agent_key===ERP).length,1);
  assert.equal(clusterView(s,0).floor.filter(f=>f.agentKey===ERP&&f.state==='executing').length,2);
});
test('days of finished work do not accumulate (2.1.3: the heads froze after days)',()=>{
  const {prune,TERMINAL_KEEP_MS,HANDOFF_KEEP}=model;
  let s=applySnapshot({as_of_seq:0,server_time:0,applications:[],orchestrators:[],heads:[],agents:[],assignments:[],handoffs:[]},0);
  let seq=1,now=0;
  for(let i=0;i<5000;i++){
    now+=5000;   // one task every 5 s: about 7 hours
    for(const st of ['running','completed'])s=applyDelta(s,{seq:seq++,app_id:'capstone',type:'assignment.updated',applied:true,payload:{},
      result:{assignment_key:`capstone/task-${i}`,agent_key:'capstone/a1',state:st,location_id:'head:head-192-0-2-3',updated_at:now/1000}},now).state;
    s=applyDelta(s,{seq:seq++,app_id:'capstone',type:'handoff.sent',applied:true,payload:{correlation_id:`c${i}`,sender_agent_id:'a1',receiver_agent_id:'a2'}},now).state;
  }
  s=applyDelta(s,{seq:seq++,app_id:'capstone',type:'assignment.updated',applied:true,payload:{},result:{assignment_key:'capstone/live',agent_key:'capstone/a2',state:'running',location_id:'head:head-192-0-2-7',updated_at:now/1000}},now).state;
  const kept=Object.keys(s.assignments);
  assert.ok(kept.length<=Math.ceil(TERMINAL_KEEP_MS/5000)+2,`only the last minute of finished work is kept, got ${kept.length}`);
  assert.ok(kept.includes('capstone/live'),'work still running is never dropped');
  assert.ok(kept.includes('capstone/task-4999'),'just-finished work stays for its grace period');
  assert.ok(Object.keys(s.handoffs).length<=HANDOFF_KEEP,'handoffs are capped');
  assert.equal(typeof prune,'function');
});
