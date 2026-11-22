import test from 'node:test';
import assert from 'node:assert/strict';
import {createIdleClock,IDLES,firstVisible,standingLayout,doorRoute,depthAt,footPoint} from '../../frontend/js/agent_arena_choreography.mjs';
import {applySnapshot,applyDelta,locationView,clusterView} from '../../frontend/js/agent_arena_locations.mjs';
import {demoScript} from '../../frontend/js/agent_arena_demo_v2.mjs';

test('idle gestures are varied, independently timed, repeatable and bounded',()=>{
  const entries=Array.from({length:15},(_,i)=>({id:'agent-'+i,state:'idle',render:'idle'}));
  const a=createIdleClock('fixed'),b=createIdleClock('fixed'),seen=new Set(),firstAt=new Set(),started=new Set(),last=new Map();
  assert.equal(a.step(entries,0).size,0);assert.equal(b.step(entries,0).size,0);
  for(let now=400;now<1800000;now+=400){
    const actual=a.step(entries,now),again=b.step(entries,now);assert.deepEqual(actual,again);assert.ok(actual.size<=3);
    for(const [key,pose] of actual){seen.add(pose);if(!started.has(key)){firstAt.add(now);started.add(key)}if(last.has(key)&&!last.get(key).active)assert.notEqual(last.get(key).pose,pose);last.set(key,{pose,active:true})}
    for(const [key,value]of last)if(!actual.has(key))value.active=false;
  }
  assert.equal(seen.size,IDLES.length);assert.ok(firstAt.size>2,'agents should not start together');assert.equal(started.size,15);
});

test('new work, offline status, pause and reduced motion interrupt idles',()=>{
  const clock=createIdleClock('interrupt'),idle={id:'a',state:'idle',render:'idle'};
  clock.step([idle],0);let activeAt=0;for(let now=400;now<50000;now+=400)if(clock.step([idle],now).size){activeAt=now;break}
  assert.ok(activeAt);assert.equal(clock.step([{...idle,state:'executing'}],activeAt+1).size,0);
  assert.equal(clock.step([{...idle,render:'producer_offline'}],activeAt+2).size,0);
  assert.equal(clock.step([idle],activeAt+3,{paused:true}).size,0);
  assert.equal(clock.step([idle],activeAt+4,{reduced:true}).size,0);
  assert.equal(clock.step([idle],activeAt+5).size,0,'resume starts a fresh delay, not a backlog');
});

test('a hidden cached floor actor cannot mask the visible seat',()=>{
  const hidden={hidden:true,isConnected:true},seat={hidden:false,isConnected:true};
  assert.equal(firstVisible(hidden,seat),seat);assert.equal(firstVisible({...seat,isConnected:false},null),null);
});

test('batched handoffs retain sent and received direction independently',()=>{
  const script=demoScript(),events=script.events.filter(e=>e.type.startsWith('handoff.'));
  let state=applySnapshot(script.snapshot),transitions=[];
  for(const event of events){const next=applyDelta(state,event);state=next.state;transitions.push(next.transition)}
  assert.deepEqual(transitions.slice(0,2).map(t=>t.status),['sent','received']);
  assert.ok(transitions.every(t=>t.sender.startsWith('ticketing-demo/')&&t.receiver.startsWith('ticketing-demo/')));
  assert.equal(transitions[0].sender,transitions[1].sender);assert.equal(transitions[0].receiver,transitions[1].receiver);
});

test('offline unassigned agents are unknown, while idle heads stay empty',()=>{
  const script=demoScript(),now=Date.now(),s=applySnapshot(script.snapshot,now);
  s.orchestrators['ticketing-demo/main'].last_seen=0;
  assert.ok(locationView(s,'orch:ticketing-demo/main',now).bleachers.every(e=>e.render==='producer_offline'));
  assert.ok(clusterView(s,now).bleachers.filter(e=>e.agentKey.startsWith('ticketing-demo/')).every(e=>e.render==='producer_offline'));
  assert.equal(locationView(s,'head:head-demo-6',now).floor.length,0);
  assert.equal(locationView(s,'head:head-demo-6',now).bleachers.length,0);
});

test('heartbeat interval changes take effect on deltas without a resnapshot',()=>{
  const script=demoScript(),s=applySnapshot(script.snapshot,1000);
  const next=applyDelta(s,{seq:1,app_id:'ticketing-demo',type:'orchestrator.heartbeat',applied:true,received_at:100,payload:{orchestrator_id:'main',heartbeat_seconds:5}}).state;
  assert.equal(next.orchestrators['ticketing-demo/main'].heartbeat_seconds,5);
});

test('crowds paginate in bounded standing space without shrinking the whole roster',()=>{
  for(const [w,h]of [[1440,900],[1050,610],[800,460],[600,400]]){
    const small=standingLayout(w,h,9),crowd=standingLayout(w,h,10000);
    assert.equal(small.capacity,crowd.capacity);assert.ok(crowd.capacity<=16&&crowd.capacity>=4);
    assert.equal(crowd.end-crowd.start,crowd.capacity);
    const last=standingLayout(w,h,10000,100000);assert.equal(last.end,10000);assert.ok(last.end-last.start<=last.capacity);
    const ids=[];for(let p=0;p<crowd.pages;p++){const page=standingLayout(w,h,10000,p);for(let i=page.start;i<page.end;i++)ids.push(i)}
    assert.equal(new Set(ids).size,10000);assert.equal(ids.length,10000);
    assert.ok(crowd.positions.every(p=>p.x>=12&&p.x<=33&&p.y>40&&p.y<90&&p.depth>=.84&&p.depth<=1));
    for(let i=0;i<crowd.positions.length;i++)for(let j=0;j<i;j++){
      const a=crowd.positions[i],b=crowd.positions[j];assert.ok(Math.hypot((a.x-b.x)*w/7500,(a.y-b.y)*h/9600)>=.999);
    }
    assert.ok(new Set(crowd.positions.map(p=>p.y)).size>2,'not regimented rows');
  }
});

test('door travel uses the painted threshold and feet at different scene sizes',()=>{
  for(const bounds of [{left:8,top:150,width:1100,height:620},{left:100,top:80,width:600,height:400}]){
    const {threshold,approach}=doorRoute(bounds);
    assert.equal(threshold.x,bounds.left+bounds.width*.858);assert.equal(threshold.y,bounds.top+bounds.height*.252);
    assert.ok(approach.y>threshold.y&&approach.x<threshold.x);
    assert.ok(depthAt(bounds,threshold)<depthAt(bounds,{x:threshold.x,y:bounds.top+bounds.height*.8}));
    assert.equal(depthAt(bounds,{x:0,y:threshold.y}),depthAt(bounds,{x:9999,y:threshold.y}),'horizontal motion cannot change size');
    assert.ok(depthAt(bounds,threshold)>=.84,'no miniature arrivals');
    assert.deepEqual(footPoint({left:20,top:30,width:80,height:100}),{x:60,y:125});
  }
});
