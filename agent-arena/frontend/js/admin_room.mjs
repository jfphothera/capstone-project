// Admin hub room: the IT admin at the hub's workstation, working in the dark by lamp and monitor
// light. Night staff come and go through the one door: the mop and broom crew switch the room's
// lights on while they work, the guard walks it by flashlight, the drone flies its own light.
// The monitor shows the hub's status: real in live mode (window.adminRoom.live.status), labelled
// SAMPLE in the preview. Art: admin-hub-v1 (room, desk, poses, lights) and characters-v1 (staff).
import {catHere,ADMIN_HUB} from './cat_tour.mjs';
import {VIEWS,viewFrames,frameOf} from './body_frames.mjs';
const Q=new URLSearchParams(location.search),LIVE=Q.get('live')==='1',W=1536,H=1024;
if(LIVE)document.body.classList.add('embed','live');
const $=s=>document.querySelector(s);
const ASSETS=new URL('../assets/',import.meta.url),at=(dir,file)=>new URL(file,new URL(dir,ASSETS));
const back=$('#back').getContext('2d'),glow=$('#glow').getContext('2d');back.imageSmoothingEnabled=glow.imageSmoothingEnabled=false;
const still=()=>$('#still').checked||matchMedia('(prefers-reduced-motion: reduce)').matches;
async function json(url){const r=await fetch(url);if(!r.ok)throw new Error(`${r.status} ${url}`);return r.json()}
const bitmaps=new Map();
function bitmap(url){const k=String(url);if(!bitmaps.has(k))bitmaps.set(k,fetch(url).then(r=>{if(!r.ok)throw new Error(`${r.status} ${url}`);return r.blob()}).then(b=>createImageBitmap(b)));return bitmaps.get(k)}
const rand=Math.random,pick=a=>a[Math.floor(rand()*a.length)],between=(a,b)=>a+(b-a)*rand();
function canvas(w,h){const c=document.createElement('canvas');c.width=w;c.height=h;const g=c.getContext('2d');g.imageSmoothingEnabled=false;return [c,g]}
const tints=new Map();
function tint(img,color,key){const k=key+color;if(!tints.has(k)){const [c,g]=canvas(img.width,img.height);g.drawImage(img,0,0);g.globalCompositeOperation='source-in';g.fillStyle=color;g.fillRect(0,0,c.width,c.height);tints.set(k,c)}return tints.get(k)}

// ------------------------------------------------------------------ room geometry (measured 1:1)
// The door leaf is 242-375 x 90-317; people step out of it at its sill. The switch is on the flat
// panel right of the door pipes. Walkable floor runs from the back wall (y 345) to the front ledge;
// the desk, chair and admin block x 470-1066, y 560-875 (feet).
const DOOR={leaf:[242,90,133,228],sill:[308,338],inside:[308,326],entry:[330,420]};
const SWITCH_STAND=[494,364],DESK=[470,560,1066,875],FLOOR={x:[262,1280],y:[372,920]};
const WORK_FEET=857;
// Sizes against the door: a person in the doorway stands about 0.83 of the leaf (190 px of 228),
// growing about 40% toward the front ledge.
const K_DOOR=190/84,kAt=y=>K_DOOR*(1+.4*(y-DOOR.sill[1])/560);
const inDesk=([x,y],m=0)=>x>DESK[0]-m&&x<DESK[2]+m&&y>DESK[1]-m&&y<DESK[3]+m;
const clampFloor=([x,y])=>[Math.max(FLOOR.x[0],Math.min(FLOOR.x[1],x)),Math.max(FLOOR.y[0],Math.min(FLOOR.y[1],y))];
function crosses(a,b){const n=Math.ceil(Math.hypot(b[0]-a[0],b[1]-a[1])/10);for(let i=1;i<n;i++){const t=i/n;if(inDesk([a[0]+(b[0]-a[0])*t,a[1]+(b[1]-a[1])*t]))return true}return false}
// Around the desk by its corners when the straight line would cross it.
const CORNERS=[[450,540],[1090,540],[1090,900],[450,900]];
function route(from,to){
  if(!crosses(from,to))return [to];
  let best=null,len=Infinity;const d=(p,q)=>Math.hypot(q[0]-p[0],q[1]-p[1]);
  for(const via of [...CORNERS.map(c=>[c]),...CORNERS.flatMap((c,i)=>[[c,CORNERS[(i+1)%4]],[c,CORNERS[(i+3)%4]]])]){
    const pts=[from,...via,to];if(pts.slice(1).some((p,i)=>crosses(pts[i],p)))continue;
    const l=pts.slice(1).reduce((s,p,i)=>s+d(pts[i],p),0);if(l<len){len=l;best=[...via,to]}}
  return best||[to];
}
function floorPoint(){for(let i=0;i<40;i++){const p=[between(FLOOR.x[0],FLOOR.x[1]),between(FLOOR.y[0],FLOOR.y[1])];if(!inDesk(p,120)&&Math.hypot(p[0]-DOOR.entry[0],p[1]-DOOR.entry[1])>90)return p}return [1150,700]}

// ------------------------------------------------------------------ simulation clock
let clock=0;const waiters=new Set();
const until=cond=>new Promise(res=>waiters.add({cond,res}));
const wait=ms=>{const t=clock+ms;return until(()=>clock>=t)};
function settle(){for(const w of [...waiters]){const v=w.cond();if(v){waiters.delete(w);w.res(v)}}}

// ------------------------------------------------------------------ people
const R={light:0,switchOn:false,force:null,flipAt:-1e9,door:0,status:null,visits:0};
const E=new Set();
const CHAR={broom:'broom-cleaner',mop:'mop-cleaner',guard:'security-guard'},ACT={work:'clean',look:'watch'};
function spawn(kind){const e={kind,x:DOOR.inside[0],y:DOOR.inside[1],path:[],speed:kind==='drone'?150:kind==='guard'?100:92,anim:kind==='drone'?'hover':'walk',facing:1,view:'front'};E.add(e);return e}
const go=(e,points)=>new Promise(res=>{e.path=points.flatMap((p,i)=>i?route(points[i-1],p):route([e.x,e.y],p)).map(p=>[...p]);e.arrived=res;if(!e.path.length)res()});
function step(e,dt){
  if(e.kind==='bucket'){if(e.owner.gone){E.delete(e);return}if(e.carried){e.x=e.owner.x-30*e.owner.facing;e.y=e.owner.y+6;e.portal=e.owner.portal}return}
  if(e.leap){leapStep(e);return}
  if(!e.path.length)return;
  const [tx,ty]=e.path[0],dx=tx-e.x,dy=ty-e.y,dist=Math.hypot(dx,dy),move=e.speed*dt/1000*kAt(e.y)/K_DOOR;
  if(Math.abs(dx)>.5)e.facing=dx<0?-1:1;
  if(move>=dist){e.x=tx;e.y=ty;e.path.shift();if(!e.path.length){const f=e.arrived;e.arrived=null;f?.()}}
  else{e.x+=dx/dist*move;e.y+=dy/dist*move}
}
// The door opens before anyone crosses its sill and closes once nobody is in it.
const holds=new Set();
async function enter(e){holds.add(e);e.portal=true;await until(()=>R.door>=1);await go(e,[DOOR.sill]);e.portal=false;await go(e,[DOOR.entry]);holds.delete(e);maybeLook()}
async function leave(e){await go(e,[DOOR.entry]);holds.add(e);await until(()=>R.door>=1);await go(e,[DOOR.sill]);e.portal=true;await go(e,[DOOR.inside]);holds.delete(e);E.delete(e);e.gone=true}
// Crew switch the lights on when they come in and off on their way out; the switch flickers through
// its middle state as it flips.
async function flip(e,on){await go(e,[SWITCH_STAND]);e.faceAway=true;await wait(380);R.switchOn=on;R.flipAt=clock;await wait(420);e.faceAway=false}

async function runMop(){
  const c=spawn('mop'),b={kind:'bucket',owner:c,x:c.x,y:c.y,carried:true,path:[]};E.add(b);
  await enter(c);await flip(c,true);
  const spot=floorPoint();await go(c,[spot]);b.carried=false;b.portal=false;await wait(600);
  for(let i=0;i<between(4,7);i++){c.anim='work';c.speed=30;let p=clampFloor([spot[0]+between(-150,150),spot[1]+between(-70,70)]);if(inDesk(p,90))p=spot;await go(c,[p]);await wait(between(500,1400))}
  c.anim='walk';c.speed=92;await go(c,[[b.x+30,b.y-6]]);c.facing=1;await wait(500);b.carried=true;
  await flip(c,false);await leave(c);
}
async function runBroom(){
  const c=spawn('broom');await enter(c);await flip(c,true);
  const rows=[[440,300,1250],[500,300,1250],[915,280,1270],[640,290,420],[640,1110,1260]];
  for(const [y,x0,x1] of rows.sort(()=>rand()-.5).slice(0,Math.round(between(3,5)))){
    const [a,b]=rand()<.5?[x0,x1]:[x1,x0];c.anim='walk';c.speed=92;await go(c,[[a,y]]);c.anim='work';c.speed=38;await go(c,[[b,y+between(-20,20)]])}
  c.anim='walk';c.speed=92;await flip(c,false);await leave(c);
}
// The guard keeps the lights off and checks the room by flashlight: the racks, the shelves, and a
// word with the admin.
async function runGuard(){
  const c=spawn('guard');await enter(c);
  const stops=[{p:[300,650],face:-1},{p:[1240,640],face:1},{p:[770,420],away:true},{p:[420,800],face:1,admin:true}];
  for(const s of stops.sort(()=>rand()-.5).slice(0,Math.round(between(3,4)))){
    c.anim='walk';await go(c,[s.p]);c.anim='look';if(s.face)c.facing=s.face;c.faceAway=!!s.away;c.view=s.away?'rear':'angle';
    if(s.admin)poseSeq('look-back',2600);
    await wait(between(2200,4200));c.faceAway=false}
  c.anim='walk';await leave(c);
}
async function runDrone(){
  const d=spawn('drone');await enter(d);
  for(let i=0;i<between(3,6);i++){await go(d,[floorPoint()]);await wait(between(700,2200))}
  await leave(d);
}
// The cat: a leap is a parabola between two points (onto the desk, or back down), drawn above the desk throughout.
function leap(e,to,ms=620,height=70){return new Promise(res=>{e.path=[];e.leap={from:[e.x,e.y],to:[...to],t0:clock,ms,height,res}})}
function leapStep(e){
  const L=e.leap;if(!L)return;const p=Math.min(1,(clock-L.t0)/L.ms);
  e.x=L.from[0]+(L.to[0]-L.from[0])*p;e.y=L.from[1]+(L.to[1]-L.from[1])*p;
  if(p>=1){e.leap=null;L.res()}
}
const leapHop=e=>e.leap?Math.sin(Math.min(1,(clock-e.leap.t0)/e.leap.ms)*Math.PI)*e.leap.height/2:0;
// Straight moves on the desktop (the floor router would walk around the desk).
const deskWalk=(e,p)=>new Promise(res=>{e.path=[[...p]];e.arrived=res});
async function runCat(){
  const c=spawn('cat');c.speed=90;c.sitFace='front';
  await enter(c);
  for(let i=0;i<Math.round(between(1,2));i++){await go(c,[floorPoint()]);c.sitFace=pick(['front','left','right']);await wait(between(1500,3500))}
  await go(c,[DESK_STEP_DOWN]);c.sitFace='left';await wait(between(600,1200));
  c.onDesk=true;c.deskScaleY=DESK_STEP_DOWN[1];await leap(c,DESK_LAND,640,150);poseSeq('look-back',1400);
  const stay=clock+between(25000,60000);
  while(clock<stay&&catPresent()){
    const spot=pick(DESK_SPOTS);c.speed=55;await deskWalk(c,spot);   // to the lamp it crosses the keyboard
    c.sitFace=spot[0]<760?'right':'left';await wait(between(6000,14000));
  }
  await deskWalk(c,DESK_LAND);await leap(c,DESK_STEP_DOWN,560,60);c.onDesk=false;c.deskScaleY=null;
  await leave(c);
}
const CAT_HERE=Q.get('cat')==='here',catPresent=()=>CAT_HERE||catHere(ADMIN_HUB,Date.now());
async function catLife(){for(;;){await until(()=>catPresent());await runCat().catch(console.error);await until(()=>!catPresent()||CAT_HERE&&rand()<.002)}}
const VISITS={guard:runGuard,mop:runMop,broom:runBroom,drone:runDrone};
function visit(kind){R.visits++;VISITS[kind]().catch(console.error).finally(()=>R.visits--)}
// One visitor at a time, every few minutes live (sooner in the preview); the drone may also drop in.
async function staffLife(){
  await wait(LIVE?between(40000,90000):between(4000,9000));
  for(;;){
    if($('#auto').checked&&!R.visits)visit(pick(['guard','guard','mop','broom','drone']));
    await wait(LIVE?between(90000,240000):between(20000,45000));
  }
}

// ------------------------------------------------------------------ lights and the admin
// The room is lit while the crew's switch is on; clicking the switch (or L) overrides for a while,
// makes everyone in the room jump and the admin spin round. Lamp and monitor stay on throughout.
function lightStep(dt){
  const forced=R.force&&clock<R.force.until,target=forced?(R.force.on?1:0):R.switchOn?1:0,was=R.light;
  if(target>R.light)R.light=Math.min(1,R.light+dt/140);else if(target<R.light)R.light=Math.max(0,R.light-dt/260);
  if(was<.5&&R.light>=.5&&!forced)poseSeq('lights-on-reaction',1800);
  R.door=holds.size?Math.min(1,R.door+dt/250):Math.max(0,R.door-dt/350);
}
function lightSwitch(){const on=R.light<.5;R.force={on,until:clock+90000};R.flipAt=clock;startle()}
function startle(){const now=performance.now();for(const e of E){const lag=rand()*220;e.jumpAt=now+lag;e.jumpUntil=now+lag+950}poseSeq('lights-on-reaction',1800)}
// The admin types, with pauses to read; now and then leans back, stretches or has some coffee.
const A={seq:null,next:0,readUntil:0,typeUntil:0};
function poseSeq(name,hold=0){const act=R.cat.actions[name]||{frames:[R.cat.pose_order.indexOf(name)],frame_ms:400};
  const frames=name==='look-back'?[0,6]:act.frames;A.seq={frames,ms:act.frame_ms,start:clock,hold,back:name==='lights-on-reaction'||name==='look-back'}}
function maybeLook(){if(!A.seq&&rand()<.5)poseSeq('look-back',1400)}
function adminPose(){
  const order=R.cat.pose_order;
  if(A.seq){const s=A.seq,n=s.frames.length,t=clock-s.start,i=Math.floor(t/s.ms);
    if(i<n)return order[s.frames[i]];
    if(t<n*s.ms+s.hold)return order[s.frames[n-1]];
    if(s.back){const j=Math.floor((t-n*s.ms-s.hold)/s.ms);if(j<n-1)return order[s.frames[n-2-j]]}
    A.seq=null;A.next=clock+between(25000,70000)}
  if(clock>=A.next&&A.next){poseSeq(pick(['lean-back','coffee','coffee']));return order[0]}
  if(!A.next)A.next=clock+between(15000,40000);
  if(clock>=A.typeUntil&&clock>=A.readUntil){if(rand()<.35)A.readUntil=clock+between(2000,5000);A.typeUntil=Math.max(clock,A.readUntil)+between(4000,9000)}
  if(clock<A.readUntil||still())return order[0];
  return order[Math.floor(clock/R.cat.actions.typing.frame_ms)%2];
}

// ------------------------------------------------------------------ the monitor
// Live: the viewer passes the hub's status. Preview: labelled sample status, cycled by "Cycle alert".
const SAMPLE=[
  {service:{up:true,version:'2.2.0'},heads:{up:9,total:9},issues:[],codes:0,hour:{pass:214,fail:3}},
  {service:{up:true,version:'2.2.0'},heads:{up:8,total:9},issues:[{who:'HEAD 2',what:'Ollama not reachable',level:'warn'}],codes:1,hour:{pass:198,fail:12}},
  {service:{up:false,version:'2.2.0'},heads:{up:0,total:9},issues:[{who:'SERVICE',what:'not answering on :8790',level:'down'}],codes:0,hour:{pass:0,fail:0}}];
let sampleIndex=1;
const COLORS={ok:'#96e6aa',info:'#8bdde4',warn:'#ffbe5a',down:'#ff7864',dim:'#3c6e76'};
const level=s=>!s?'info':!s.service?.up||s.issues?.some(i=>i.level==='down')?'down':s.issues?.length?'warn':'ok';
const [screen,sg]=canvas(277,130);let screenKey='';
function paintScreen(){
  const s=R.status,lv=level(s),cursor=still()||Math.floor(clock/530)%2===0,key=JSON.stringify(s)+cursor+LIVE;if(key===screenKey)return;screenKey=key;
  sg.fillStyle='#06101a';sg.fillRect(0,0,277,130);sg.textBaseline='top';
  sg.font='17px Consolas,monospace';sg.fillStyle=lv==='down'?COLORS.down:COLORS.info;sg.fillText('ADMIN HUB  .254',10,6);
  if(!LIVE){sg.font='10px Consolas,monospace';sg.fillStyle=COLORS.warn;sg.fillText('SAMPLE',224,10)}
  sg.fillStyle=COLORS.dim;sg.fillRect(10,28,257,1);
  const rows=!s?[['WAITING FOR STATUS…','info']]:[
    [`SERVICE   ${s.service.up?'up':'DOWN'}  ${s.service.version||''}`,s.service.up?'ok':'down'],
    [`HEADS     ${s.heads.up} of ${s.heads.total} up`,s.heads.up===s.heads.total?'ok':'warn'],
    ...s.issues.slice(0,1).map(i=>[`${i.who.padEnd(10)}${i.what}${s.issues.length>1?` +${s.issues.length-1}`:''}`,i.level]),
    [`CODES     ${s.codes} waiting`,'info'],[`LAST HOUR ${s.hour.pass} pass  ${s.hour.fail} fail`,s.hour.fail>=10?'warn':'info']].slice(0,6);
  sg.font='12px Consolas,monospace';
  rows.forEach(([text,c],i)=>{sg.fillStyle=COLORS[c];sg.fillText(text,10,35+i*15)});
  if(cursor){sg.fillStyle=COLORS.info;sg.fillRect(10,35+Math.min(rows.length,6)*15+2,7,10)}
}
const screenLight=()=>{const lv=level(R.status);return lv==='down'?COLORS.down:lv==='warn'?COLORS.warn:R.cat.lighting.screen_color};

// ------------------------------------------------------------------ painting
const [reveal,rg]=canvas(W,H);
// Darkness keeps the dark plate; light sources reveal the lit plate through soft pools.
function paintRoom(){
  back.globalAlpha=1;back.drawImage(R.dark,0,0);
  if(R.light>0){back.globalAlpha=R.light;back.drawImage(R.lit,0,0);back.globalAlpha=1}
  if(R.light>=.99)return;
  rg.globalCompositeOperation='source-over';rg.clearRect(0,0,W,H);
  const pool=(x,y,rx,ry,a)=>{const gr=rg.createRadialGradient(x,y,0,x,y,rx);gr.addColorStop(0,`rgba(255,255,255,${a})`);gr.addColorStop(.6,`rgba(255,255,255,${a*.5})`);gr.addColorStop(1,'rgba(255,255,255,0)');
    rg.save();rg.translate(x,y);rg.scale(1,ry/rx);rg.translate(-x,-y);rg.fillStyle=gr;rg.beginPath();rg.arc(x,y,rx,0,7);rg.fill();rg.restore()};
  if(R.door>0){pool(DOOR.sill[0],DOOR.sill[1]+40,150,60,.8*R.door);pool(DOOR.sill[0],DOOR.leaf[1]+150,90,140,.5*R.door)}
  for(const e of E){if(e.gone||e.portal)continue;const k=kAt(e.y);
    if(e.kind==='drone'){pool(e.x+e.facing*24*k/2,e.y+8,90*k/2,40*k/2,.9)}
    else if(e.kind==='guard'){
      const [ux,uy]=e.faceAway?[0,-1]:e.view==='front'?[0,1]:[e.facing,0],reach=(uy?150:230)*k/2,cx=e.x+ux*reach,cy=e.y+(uy?uy*reach*.5:10);
      pool(cx,cy,95*k/2,45*k/2,.95);pool((e.x+cx)/2,(e.y+cy)/2,70*k/2,30*k/2,.6)}
  }
  rg.globalCompositeOperation='source-in';rg.drawImage(R.lit,0,0);
  back.globalAlpha=1-R.light;back.drawImage(reveal,0,0);back.globalAlpha=1;
}
// The door has no open-leaf art: open, its leaf reads as a dark doorway with the corridor's light.
function paintDoor(){
  if(R.door<=0)return;const [x,y,w,h]=DOOR.leaf;back.globalAlpha=R.door;back.fillStyle='#05080d';back.fillRect(x,y,w,h);
  const gr=back.createLinearGradient(0,y,0,y+h);gr.addColorStop(0,'rgba(255,214,160,.22)');gr.addColorStop(1,'rgba(255,214,160,.06)');back.fillStyle=gr;back.fillRect(x,y,w,h);back.globalAlpha=1;
}
function sprite(img,x,y,ax,ay,k,flip,bright,hop=0){
  back.save();back.filter=bright<.99?`brightness(${bright.toFixed(2)})`:'none';back.translate(Math.round(x),Math.round(y-hop));if(flip)back.scale(-1,1);
  back.drawImage(img,-ax*k,-ay*k,img.width*k,img.height*k);back.restore();
}
const hopAt=e=>{const now=performance.now();if(!e.jumpAt||now<e.jumpAt||now>e.jumpUntil)return 0;const p=(now-e.jumpAt)/950;return p<.5?Math.sin(p/.5*Math.PI)*46:Math.sin((p-.5)/.5*Math.PI)*20};
function bang(x,y){back.save();back.font='900 22px Consolas,monospace';back.textAlign='center';back.fillStyle='#ffd34d';back.strokeStyle='#1b1203';back.lineWidth=2;
  back.fillRect(x-12,y-26,24,26);back.strokeRect(x-12,y-26,24,26);back.fillStyle='#1b1203';back.fillText('!',x,y-4);back.restore()}
// The robot cat (Arena 2.4.0): visits the hub on the shared tour (cat_tour.mjs), jumps onto the desk, sits by the
// lamp or at the right end, and sometimes strolls across the keyboard. Frames: characters-v2 h-robot-cat (64 x 80,
// feet at 32,78). Desktop spots measured on the composed desk + admin art: the desktop runs y 575-645, x 500-1040.
const CAT_DIR=v=>`characters-v2/bots/h-robot-cat/${v}/`,CAT_VIEWS=VIEWS,CAT_FRAMES=['idle','walk-01','walk-02','walk-03','walk-04'];
const DESK_LAND=[1015,640],DESK_SPOTS=[[955,628],[625,628]],DESK_STEP_DOWN=[1078,892];
const catFile=(view,f)=>at(CAT_DIR(view),f+'.png');
function catFrame(e,t){
  let view=e.sitFace||'front',f='idle';
  if(e.path.length&&!still()){const [tx,ty]=e.path[0],dx=tx-e.x,dy=ty-e.y;view=dy>Math.abs(dx)*1.1?'front':-dy>Math.abs(dx)*1.1?'back':dx<0?'left':'right';f=CAT_FRAMES[1+Math.floor(t/160)%4]}
  // Arena 2.5.0, the artist's frames: the three-frame leap over the jump, then it stands a moment, sits, and flicks its tail.
  else if(e.leap){view=e.leap.to[0]<e.leap.from[0]?'left':'right';f='leap-0'+(1+Math.min(2,Math.floor(3*(clock-e.leap.t0)/e.leap.ms)))}
  if(e.path.length||e.leap||still())e.sitAt=null;
  else{const p=t-(e.sitAt??=t);if(p>=400)f=(p-400)%4200>3000?frameOf(view,'tail-flick',p,0,true):'sit-01'}
  return {file:catFile(view,f),ax:32,ay:78,flip:false};
}
function frameFor(e,t){
  if(e.kind==='cat')return catFrame(e,t);
  if(e.kind==='drone'){const a=R.drone.animations[e.anim]||R.drone.animations.hover;return {file:at('scenes/_shared/',a.frames[still()?0:Math.floor(t/a.frame_ms)%a.frames.length]),ax:48,ay:104,flip:e.facing<0}}
  if(e.kind==='bucket')return {file:at('characters-v1/',R.chars.props['mop-bucket'].png),ax:80,ay:124,flip:false};
  const views=R.chars.actors[CHAR[e.kind]].views,walking=e.path.length>0;
  if(walking){const [tx,ty]=e.path[0],dx=tx-e.x,dy=ty-e.y;if(Math.abs(dx)>.5||Math.abs(dy)>.5)e.view=dy>Math.abs(dx)*1.2?'front':-dy>Math.abs(dx)*1.2?'rear':views.angle?'angle':'front'}
  const view=e.faceAway?'rear':views[e.view]?e.view:'angle',set=views[view].animations,want=ACT[e.anim]||(walking?'walk':e.anim);
  const anim=set[want]||set.walk,hold=!walking&&want==='walk';
  const file=still()||hold?anim.frames[0]:anim.frames[Math.floor((t+(e.x|0)*3)/anim.frame_ms)%anim.frames.length];
  return {file:at('characters-v1/',file),ax:80,ay:124,flip:e.facing<0&&view==='angle'};
}
// Every staff frame is loaded up front, so nobody blinks out while a frame is still on its way.
const images=new Map();
const image=url=>images.get(String(url));
async function preload(){
  const urls=[at('characters-v1/',R.chars.props['mop-bucket'].png),...Object.values(R.drone.animations).flatMap(a=>a.frames.map(f=>at('scenes/_shared/',f)))];
  for(const id of Object.values(CHAR))for(const v of Object.values(R.chars.actors[id].views))for(const a of Object.values(v.animations))urls.push(...a.frames.map(f=>at('characters-v1/',f)));
  for(const v of CAT_VIEWS)for(const f of viewFrames(true,v))urls.push(catFile(v,f));
  await Promise.all(urls.map(u=>bitmap(u).then(b=>images.set(String(u),b))));
}
function paintPeople(list,t){
  for(const e of list){
    const f=frameFor(e,t),img=image(f.file);if(!img)continue;
    // The cat keeps its floor scale while on the desk (no smaller "far away" size on top of it).
    const k=(e.kind==='cat'?.73*kAt(e.deskScaleY||e.y):kAt(e.y)*(e.kind==='drone'?.85:1)),hop=e.kind==='cat'?leapHop(e)*k:hopAt(e)*k/K_DOOR;
    const lit=e.kind==='drone'?.8:e.kind==='guard'?.6:e.kind==='cat'?.55:.42,bright=lit+(1-lit)*R.light;
    if(e.portal){back.save();const [x,y,w,h]=DOOR.leaf;back.beginPath();back.rect(x,y,w,h+12);back.clip()}
    sprite(img,e.x,e.y,f.ax,f.ay,k,f.flip,e.portal?bright*.6:bright,hop);
    if(e.portal)back.restore();
  }
}
function paintWorkstation(pose,onDesk=()=>{}){
  const [ox,oy]=R.cat.workstation.origin_px,P=R.poses[pose],dim=.55+.45*R.light,f=dim<.99?`brightness(${dim.toFixed(2)})`:'none';
  // Desk, then whatever stands on it (the cat: farther from us than the admin we see from behind), then the admin.
  back.save();back.filter=f;back.drawImage(R.desk,ox,oy);back.restore();
  onDesk();
  back.save();back.filter=f;back.drawImage(P.person,ox,oy);back.restore();
  const [sx,sy]=R.cat.workstation.screen_rect_px;paintScreen();back.drawImage(screen,ox+sx,oy+sy);
}
function paintGlow(pose,t){
  const g=glow,[ox,oy]=R.cat.workstation.origin_px,P=R.poses[pose],Lc=R.cat.lighting,dark=1-R.light;g.clearRect(0,0,W,H);
  const flicker=still()?.93:.93+.015*Math.sin(t/430)+.01*Math.sin(t/97),alert=level(R.status)==='down'?.75+.25*Math.sin(t/180):1;
  g.globalAlpha=.5*flicker*(.35+.65*dark);g.drawImage(tint(R.pool,Lc.lamp_color,'pool'),...Lc.floor_pool.origin_px);
  g.globalAlpha=Lc.lamp_relight_opacity*flicker*(.3+.7*dark);g.drawImage(tint(P.lamp,Lc.lamp_color,pose+'l'),ox,oy);
  g.globalAlpha=Lc.screen_relight_opacity*alert*(.35+.65*dark);g.drawImage(tint(P.monitor,screenLight(),pose+'m'),ox,oy);
  g.globalAlpha=.8*flicker*(.5+.5*dark);g.drawImage(tint(R.lampGlow,Lc.lamp_color,'glow'),ox,oy);
  g.globalAlpha=flicker;g.drawImage(tint(R.lampEm,Lc.lamp_color,'em'),ox,oy);g.globalAlpha=1;
}
function paintSwitch(){
  const s=R.cat.switch,[x,y]=s.origin_px,[w,h]=s.size,on=R.light>=.5,state=clock-R.flipAt<160?'transition':on?'on':'off',b=.5+.5*R.light;
  back.save();back.filter=b<.99?`brightness(${b.toFixed(2)})`:'none';back.drawImage(R.switch[state],x,y,w,h);back.restore();
}
function paint(t){
  paintRoom();paintDoor();
  back.globalAlpha=.85;back.drawImage(R.shadow,...R.cat.lighting.contact_shadow.origin_px);back.globalAlpha=1;
  paintSwitch();
  const people=[...E].filter(e=>!e.gone).sort((a,b)=>a.y-b.y),pose=adminPose(),floor=people.filter(e=>!e.onDesk);
  paintPeople(floor.filter(e=>e.y<WORK_FEET),t);paintWorkstation(pose,()=>paintPeople(people.filter(e=>e.onDesk),t));paintPeople(floor.filter(e=>e.y>=WORK_FEET),t);
  paintGlow(pose,t);
}
function hud(){
  const who=[...E].filter(e=>e.kind!=='bucket').map(e=>({guard:'Guard',mop:'Mop crew',broom:'Broom crew',drone:'Drone',cat:'Robot cat'})[e.kind]);
  const rows=[['Lights',R.light>=.5?'On':'Off'],['In the room',who.length?who.join(', '):'Only the admin'],['Monitor',{ok:'All clear',warn:'Warning',down:'Service down',info:'Waiting'}[level(R.status)]]];
  const html=rows.map(([k,v])=>`<dt>${k}</dt><dd>${v}</dd>`).join('');if($('#hud').innerHTML!==html)$('#hud').innerHTML=html;
}

// ------------------------------------------------------------------ loop
function fit(){const v=$('#viewport'),k=Math.min(v.clientWidth/W,v.clientHeight/H);const s=$('#stage');s.style.transform=`scale(${k})`;s.style.left=(v.clientWidth-W*k)/2+'px';s.style.top=(v.clientHeight-H*k)/2+'px'}
function frameStep(real){const dt=real*(LIVE?1:Number($('#speed').value));clock+=dt;for(const e of E)step(e,dt);settle();lightStep(dt);paint(clock);hud()}
let last=performance.now(),owed=0;
function loop(now){
  let real=Math.min(100,now-last);last=now;
  // About 30 fps, leaving the hub's CPU to the event service.
  owed+=real;if(owed<32){requestAnimationFrame(loop);return}real=Math.min(150,owed);owed=0;
  try{if(!document.hidden&&R.ready)frameStep(real)}catch(e){$('#status').textContent='Error: '+e.message;console.error(e)}
  requestAnimationFrame(loop);
}
async function init(){
  const cat=await json(at('admin-hub-v1/','catalog.json'));R.cat=cat;const A_=f=>bitmap(at('admin-hub-v1/',f));
  const [dark,lit,desk,pool,shadow,lampGlow,lampEm]=await Promise.all([cat.rooms.dark,cat.rooms.lit,cat.workstation.desk,cat.lighting.floor_pool.file,cat.lighting.contact_shadow.file,cat.lighting.lamp_glow,cat.lighting.lamp_emission].map(A_));
  Object.assign(R,{dark,lit,desk,pool,shadow,lampGlow,lampEm});R.poses={};
  await Promise.all(Object.entries(cat.poses).map(async([id,p])=>{R.poses[id]={person:await A_(p.person),lamp:await A_(p.lamp),monitor:await A_(p.monitor)}}));
  R.switch={};await Promise.all(Object.entries(cat.switch.states).map(async([s,f])=>{R.switch[s]=await A_(f)}));
  R.chars=await json(at('characters-v1/crew/','catalog.json'));R.drone=(await json(at('scenes/_shared/','sprites.json'))).staff.drone;await preload();
  if(!LIVE)R.status=SAMPLE[sampleIndex];
  R.ready=true;fit();addEventListener('resize',fit);
  staffLife().catch(console.error);catLife().catch(console.error);
  $('#wall-switch').onclick=e=>{e.stopPropagation();lightSwitch()};$('#lights').onclick=lightSwitch;
  addEventListener('keydown',e=>{if((e.key==='l'||e.key==='L')&&!e.target.closest?.('input,select,textarea'))lightSwitch()});
  for(const k of Object.keys(VISITS))$('#visit-'+k).onclick=()=>visit(k);
  $('#alert').onclick=()=>{sampleIndex=(sampleIndex+1)%SAMPLE.length;R.status=SAMPLE[sampleIndex]};
  $('#status').textContent='Admin hub room · sample status';
  // Review hook: step the room when the page gets no animation frames (hidden preview pane).
  window.adminRoom={R,E,visit,lightSwitch,get clock(){return clock},async advance(ms){for(let t=0;t<ms;t+=50){frameStep(50/(LIVE?1:Number($('#speed').value)));await null}}};
  if(LIVE){
    window.adminRoom.live={status(s){R.status=s},lightSwitch};
    addEventListener('keydown',e=>{if(['F11','Escape'].includes(e.key)){e.preventDefault();parent.postMessage({type:'arena-key',key:e.key},location.origin)}});
    parent.postMessage({type:'arena-scene-ready',room:'admin-hub'},location.origin);
  }
  requestAnimationFrame(loop);
}
init().catch(e=>{$('#status').textContent='Failed: '+e.message;console.error(e)});
