// Scene composer: one room composed on its clean source plate from the delivered kits, using a
// fitted socket layout (composer_rooms.mjs) filled from a seed, and driven by a labelled SYNTHETIC
// event loop so look and movement can be judged. Preview only: it never contacts the event
// service; every count shown is illustrative.
import {depthAt} from './agent_arena_choreography.mjs';
import {ROOMS,BULBS,TICKER_TOP,BAY_BACK,roomLayout} from './composer_rooms.mjs';
import {catHere} from './cat_tour.mjs';
import {VIEWS,PROPS,viewFrames,frameOf} from './body_frames.mjs';

const Q=new URLSearchParams(location.search);
const ROOM_ID=Q.get('room')||'loading-dock',L=roomLayout(ROOM_ID),LAB=L.type==='lab',W=1536,H=1024;
// Fixed-layout rooms (composer_rooms PLATES): their own plate, door leaves, props and status board; no kit sockets.
const PLATE=L.kit==='plate',FLOOR=PLATE?{x:[330,1220],y:[600,870]}:{x:[300,1220],y:[400,850]};
// Cluster mode: several rooms in one page, one composer per frame, talking over a local channel.
// A lab dispatches agents to named heads; a head shows only work actually sent to it and reports
// each result back. Nothing here contacts the event service.
const EMBED=Q.get('embed')==='1',CLUSTER=Q.get('cluster'),APP=Q.get('app')||'Ticketing',HEAD=Q.get('head'),HEADS=(Q.get('heads')||'').split(',').filter(Boolean);
// Live mode: the room is embedded in the Arena viewer, which drives it from the event service
// (window.composer.live). No synthetic work: every agent, result and state shown is real.
const LIVE=Q.get('live')==='1',SEED=Q.get('seed')||'preview-display';
const DRIVEN=LIVE||(!!CLUSTER&&!LAB);     // a head in a cluster only gets work dispatched by an orchestrator
const bus=CLUSTER&&!LIVE?new BroadcastChannel('arena-cluster-'+CLUSTER):null,returns=new Map();
if(EMBED||LIVE)document.body.classList.add('embed');if(LIVE)document.body.classList.add('live');
const $=s=>document.querySelector(s);
const ASSETS=new URL('../assets/',import.meta.url);
const at=(dir,file)=>new URL(file,new URL(dir,ASSETS));
const depth=y=>depthAt({top:0,height:H},{y});
const persp=y=>depth(y)/depth(900);   // prop sizes are authored as if standing at the front
// Sizes are set against the doors: a person in the doorway stands about two thirds of its opening.
const SC=L.scale,BENCH_W=208*.44*SC.furniture*persp(262);
const ctx={back:$('#back').getContext('2d'),dark:$('#dark').getContext('2d'),glow:$('#glow').getContext('2d'),front:$('#front').getContext('2d')};
for(const c of Object.values(ctx))c.imageSmoothingEnabled=false;
const offline=()=>$('#offline').checked;
const still=()=>$('#still').checked||offline()||matchMedia('(prefers-reduced-motion: reduce)').matches;

async function json(url){const r=await fetch(url);if(!r.ok)throw new Error(`${r.status} ${url}`);return r.json()}
const bitmaps=new Map();
function crop(url,[x,y,w,h]){return fetch(url).then(r=>r.blob()).then(b=>createImageBitmap(b,Math.round(x),Math.round(y),Math.round(w),Math.round(h)))}
function bitmap(url){const k=String(url);if(!bitmaps.has(k))bitmaps.set(k,fetch(url).then(r=>{if(!r.ok)throw new Error(`${r.status} ${url}`);return r.blob()}).then(b=>createImageBitmap(b)));return bitmaps.get(k)}

// ------------------------------------------------------------------ seeds
// Production seeds from stable display identity plus the date; the preview adds a reshuffle step.
function hash(text){let h=2166136261;for(const c of text){h^=c.charCodeAt(0);h=Math.imul(h,16777619)}return h>>>0}
function localDay(){const d=new Date();return`${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`}
function rng(seed){let s=seed>>>0||1;return()=>{s^=s<<13;s^=s>>>17;s^=s<<5;return(s>>>0)/4294967296}}
let shuffle=Number(Q.get('shuffle'))||0,seedText='',rand=rng(1);
const pick=(a,r=rand)=>a[Math.floor(r()*a.length)],between=(a,b,r=rand)=>a+(b-a)*r();
function choose(list,n,r){const pool=[...list],out=[];while(out.length<n&&pool.length)out.push(pool.splice(Math.floor(r()*pool.length),1)[0]);return out}

// ------------------------------------------------------------------ kits
const R={};
async function loadKits(){
  const kit=f=>PLATE?null:f();   // the kit-socket rooms' own pieces; fixed-layout rooms carry theirs
  const [scene,doorCat,doorProf,panelProf,panelCat,decoCat,finCat,asm,sideCat,shared,svg]=await Promise.all([
    kit(()=>json(at('',L.scene))),kit(()=>json(at('doors-v1/','catalog.json'))),kit(()=>json(at('doors-v1/','room-profiles.json'))),kit(()=>json(at('wall-panels-v1/','room-profiles.json'))),kit(()=>json(at('wall-panels-v1/','catalog.json'))),json(at('decorations-v1/','catalog.json')),
    json(at('finishing-v1/','catalog.json')),json(at('finishing-v1/','assemblies.json')),json(at('sidewall-v2/','catalog.json')),
    json(at('scenes/_shared/','sprites.json')),fetch(at('classic-arcade/','classic-refined.svg')).then(r=>r.text())]);
  Object.assign(R,{scene,decoCat,finCat,assemblies:asm.assemblies,sideCat,shared,svg});
  R.plate=await bitmap(at('',L.plate));
  if(PLATE)await loadPlate();else{
  // The room's own matched kit pieces, from the kits' room profiles.
  L.doorDesign=doorProf.profiles.find(p=>p.scene===ROOM_ID).doors.a.design;
  L.panels=panelProf.profiles.find(p=>p.scene===ROOM_ID).placements[0].item.split('/')[0];
  // Labs: crop the two result screens out of the kit's full-canvas layer, to mount them in the bay.
  if(LAB){const s=scene.screens,url=at(`scenes/${ROOM_ID}/`,s.file);R.screens={};
    for(const side of ['pass','fail']){const b=s[side].bounds,ta=s[side].text_area,px=[b[0]*W,b[1]*H,b[2]*W,b[3]*H];
      R.screens[side]={img:await crop(url,px),size:[px[2],px[3]],text:[(ta[0]-b[0])/b[2],(ta[1]-b[1])/b[3],ta[2]/b[2],ta[3]/b[3]]}}}
  // Doors: the matched rig, centred in each painted alcove, standing on its sill.
  const item=doorCat.items.find(i=>i.id===L.doorDesign),[px,py,pw,ph]=item.aperture_px;
  const parts=await Promise.all([item.portal,item.frame,item.emission,item.glow,...item.animation.panel_frames].map(f=>bitmap(at('doors-v1/',f))));
  R.doors={};
  for(const [k,d] of Object.entries(L.doors)){
    const origin=[d.center-item.anchor_px[0],d.sill-item.anchor_px[1]];
    R.doors[k]={key:k,portal:parts[0],frame:parts[1],emission:parts[2],glow:parts[3],panels:parts.slice(4),origin,frameMs:item.animation.frame_ms,
      aperture:[origin[0]+px,origin[1]+py,pw,ph],threshold:[d.center,d.sill],approach:[d.center,L.staffLaneY],entry:[d.center,L.staffLaneY+72],
      frame_i:0,last:0,holds:new Set(),idleSince:0};
  }
  R.panelItems=await Promise.all(panelCat.items.filter(i=>i.set===L.panels).map(async it=>{
    const states={};await Promise.all(Object.entries(it.states).map(async([n,s])=>{states[n]={...s,em:await bitmap(at('wall-panels-v1/',s.emission)),gl:await bitmap(at('wall-panels-v1/',s.glow))}}));
    return {item:it,states,body:await bitmap(at('wall-panels-v1/',it.body))};
  }));
  // Tickers (ticker-signs-v2): the room's housing above each door; lettering lit in the room's colour.
  const tc=await json(at('ticker-signs-v2/','catalog.json'));R.tick={item:tc.items.find(i=>i.id===L.tickerHousing),states:new Map(),msg:null};
  R.tick.housing=await bitmap(at('ticker-signs-v2/',R.tick.item.housing));
  }
  // People (characters-v1): the cleaners, guard, mechanic and inspector, each with front, rear and
  // (new crew) angled views; and the agents' walking rig in four directions.
  R.chars=await json(at('characters-v1/crew/','catalog.json'));
  R.agentDirs=await json(at('characters-v1/agents/','catalog.json'));R.agentSvg=new Map();
  await Promise.all(R.agentDirs.views.flatMap(v=>v.frames.map((f,i)=>fetch(at('characters-v1/agents/',f.replace(/\.png$/,'.svg'))).then(r=>r.text())
    .then(tx=>R.agentSvg.set(`${v.view}:${i}`,document.importNode(new DOMParser().parseFromString(tx,'image/svg+xml').documentElement,true))))));
  // Arena 2.4.0: the eight-character family (characters-v2). Seven agent bodies, each recolored per agent through
  // its SVG palette properties (--primary-shade/base/shine, --accent) set once on the figure, and the robot cat.
  // Same 64 x 80 frame and foot anchor as the classic rig: idle plus a four-frame walk, in four directions.
  // Arena 2.5.0: plus the artist's animations (body_frames.mjs): talk, listen, blink, look-aside, startled, run-frantic,
  // tap, fidget; the cat's sit, leap, tail-flick. Keys body|view:idle, body|view:0..3 (walk), body|view:<anim>-NN.
  R.bodySvg=new Map();
  await Promise.all(BODY_FILES.flatMap(b=>VIEWS.flatMap(v=>viewFrames(b==='h-robot-cat',v).map(f=>
    fetch(at(`characters-v2/bots/${b}/${v}/`,f+'.svg')).then(r=>r.ok?r.text():null).then(tx=>{if(!tx)return;const svg=document.importNode(new DOMParser().parseFromString(tx,'image/svg+xml').documentElement,true);
      svg.classList.add('avatar','body-svg');R.bodySvg.set(`${b}|${v}:${/^walk-/.test(f)?Number(f.slice(-2))-1:f}`,svg)}).catch(()=>{})))));
  R.propSrc=Object.fromEntries(Object.entries(PROPS).map(([n,p])=>[n,Array.from({length:p.frames},(_,i)=>at('characters-v2/props/',`${n}-0${i+1}.svg`).href)]));
  R.crates=await Promise.all(shared.props.crates.map(c=>bitmap(at('scenes/_shared/',c.file))));
  R.debris=await Promise.all(shared.props.debris.map(c=>bitmap(at('scenes/_shared/',c.file))));
  $('#bulb-design').replaceChildren(...finCat.items.filter(i=>i.category==='bulb').map(d=>new Option(d.name,d.id)));
}

// ------------------------------------------------------------------ fixed-layout rooms (kit 'plate')
// The plate paints the dark doorway; only the leaves move (nine frames, 80 ms apart, frame 8 fully open = empty).
// Images shown smaller than drawn are scaled ONCE here (createImageBitmap resize) and drawn 1:1 afterwards: a scaled
// draw of a fresh canvas on every paint is what grew the heads' graphics memory until 2.1.5 (see tickerFrame).
const scaled=(url,k)=>fetch(url).then(r=>r.blob()).then(async b=>{const i=await createImageBitmap(b);const w=Math.max(1,Math.round(i.width*k)),h=Math.max(1,Math.round(i.height*k));i.close();
  return createImageBitmap(b,{resizeWidth:w,resizeHeight:h,resizeQuality:'high'})});
async function loadPlate(){
  const P=L.plateRoom,dir=`plate-rooms-v1/${ROOM_ID}/`;R.doors={};
  for(const [k,d] of Object.entries(L.doors)){
    const panels=await Promise.all([...Array(9).keys()].map(n=>bitmap(at(dir,`doors/${d.side}/frame-${String(n).padStart(2,'0')}.png`))));
    R.doors[k]={key:k,plate:true,panels,origin:d.origin,frameMs:80,aperture:[d.origin[0],d.origin[1],panels[0].width,panels[0].height],threshold:[d.center,d.sill],
      approach:[d.center,L.approachY],entry:L.entries[k],frame_i:0,last:0,holds:new Set(),idleSince:0};
  }
  R.panelItems=[];R.tick={item:null,states:new Map(),msg:null};
  const m=P.board.monitor;R.boardMonitor=m?{img:await scaled(at(dir,m.file),m.scale),x:m.x,y:m.y}:null;
}
// A prop at its own width and height (furniture() is for the square kit sprites).
function furnitureBox(url,x,y,w,h,z){
  const img=document.createElement('img');img.className='furniture';img.src=url;img.alt='';
  Object.assign(img.style,{left:Math.round(x)+'px',top:Math.round(y)+'px',width:Math.round(w)+'px',height:Math.round(h)+'px',zIndex:Math.round(z)});$('#actors').append(img);
}
// A working screen: a few frames of made-up output (or an oscilloscope trace), composed once at the size shown, kept as
// images and flipped 1:1 on a small canvas over the prop's screen face. Nothing is redrawn while it plays.
async function screenSet(p,[bx0,by0,bx1,by1],x,y,z,seed){
  const s=p.scale,left=Math.round(x+bx0*s),top=Math.round(y+by0*s),w=Math.max(4,Math.round((bx1-bx0+1)*s)),h=Math.max(4,Math.round((by1-by0+1)*s));
  const color=L.plateRoom.screenColor,r=rng(hash(`${ROOM_ID}|screen|${seed}`)),frames=[],n=p.wave?8:6;
  for(let f=0;f<n;f++){
    const c=new OffscreenCanvas(w,h),g=c.getContext('2d');g.fillStyle='rgba(4,14,10,.92)';g.fillRect(0,0,w,h);g.fillStyle=color;g.strokeStyle=color;
    if(p.wave){g.lineWidth=1.5;g.globalAlpha=.9;g.beginPath();for(let i=0;i<=w;i++){const v=h/2+h*.3*Math.sin((i/w)*Math.PI*4+f*Math.PI/4)*(0.75+.25*Math.sin(i/7+f));i?g.lineTo(i,v):g.moveTo(i,v)}g.stroke();
      g.globalAlpha=.25;g.fillRect(0,Math.round(h/2),w,1)}
    else{const fh=Math.max(4,Math.floor(h/5)),cols=Math.max(3,Math.floor((w-3)/(fh*.62)));g.font=`${fh}px Consolas,monospace`;g.textBaseline='top';
      for(let row=0,yy=2;yy+fh<=h-1;row++,yy+=fh+1){const len=1+Math.floor(r()*cols);g.globalAlpha=.55+.45*r();g.fillText([...Array(len)].map(()=>'0123456789ABCDEF<>=:#/.-'[Math.floor(r()*24)]).join(''),2,yy)}
      if(f%2===0){g.globalAlpha=1;g.fillRect(2,h-Math.max(2,Math.floor(h/9))-1,Math.max(2,Math.floor(fh*.55)),Math.max(2,Math.floor(h/9)))}}   // the blinking cursor
    frames.push(c.transferToImageBitmap());
  }
  const el=document.createElement('canvas');el.className='prop screen';el.width=w;el.height=h;
  Object.assign(el.style,{left:left+'px',top:top+'px',width:w+'px',height:h+'px',zIndex:Math.round(z)});$('#actors').append(el);
  const g=el.getContext('2d');g.imageSmoothingEnabled=false;
  return {el,g,frames,shown:-1,ms:p.wave?140:420+(seed*53)%260,seed};
}
async function dressPlate(){
  R.day=localDay();seedText=`${SEED}|${ROOM_ID}|${R.day}|${shuffle}`;
  const r=rng(hash(seedText));rand=rng(hash(seedText+'|events'));
  for(const el of document.querySelectorAll('#actors .furniture,#actors .prop'))el.remove();
  for(const s of R.liveScreens||[])for(const f of s.frames)f.close();   // a re-dress replaces them: release the old images
  const P=L.plateRoom,dir=`plate-rooms-v1/${ROOM_ID}/`;
  Object.assign(R,{benches:[],obstacles:[],wall:[],floor:[],crateSpots:[],debrisSpots:[],dock:L.dock,liveScreens:[],painted:P.painted.map(([x,y])=>({feet:[x,y],cast:200}))});
  let n=0;
  for(const p of P.props){
    const s=p.scale,x=p.feet[0]-p.anchor[0]*s,y=p.feet[1]-p.anchor[1]*s,z=p.z??p.feet[1];
    furnitureBox(at(dir,p.file),x,y,p.size[0]*s,p.size[1]*s,z);
    if(p.block){const [b0,b1,b2,b3]=p.block;R.obstacles.push([x+b0*s,y+b1*s,(b2-b0)*s,(b3-b1)*s])}
    for(const box of p.screens||[])R.liveScreens.push(await screenSet(p,box,x,y,z+1,n++));
  }
  // Agents tinker at the two small desks; the inspector taps the big desk to put each result on the board.
  R.benches=[{stand:[442,546-BAY_BACK],facing:1,busy:null},{stand:[1094,546-BAY_BACK],facing:-1,busy:null}];
  R.screenStands={pass:[716,534-BAY_BACK],fail:[820,534-BAY_BACK]};R.bulbXs={pass:[],fail:[]};
  $('#bulb-design').value=pick(BULBS,r);await loadBulb($('#bulb-design').value);
  const clear=p=>!R.obstacles.some(([x,y,w,h])=>p[0]>x-30&&p[0]<x+w+30&&p[1]>y-6&&p[1]<y+h+14);
  R.stand=scatter(L.stand,8,72*SC.people,r).filter(s=>clear(s.p));R.work=scatter(L.work,10,92*SC.people,r).filter(s=>clear(s.p));
  R.pool=floorPool(r);
  $('#seed').textContent=seedText;
}
// The back-wall status board: the room's state in its own colour, drawn once per change and shown 1:1.
function paintBoard(g,state){
  const P=L.plateRoom,[tx,ty,tw,th]=P.board.text,bm=R.boardMonitor;if(bm)g.drawImage(bm.img,bm.x,bm.y);
  const a=[...E].filter(e=>e.kind==='agent'),working=a.filter(x=>x.state==='executing').length;
  const lines=state==='offline'?[P.boardTitle,'BE RIGHT BACK','showing the last known room']:
    [P.boardTitle,`Home ${a.length}   Working ${working}   At heads ${away.size}`,`Passed ${screenCounts.pass}   Failed ${screenCounts.fail}`];
  // The counts change with every agent's state: one image at a time, composed on one canvas and the old image released
  // at once (close), never a new canvas per change left to the garbage collector.
  const key=lines.join('|'),B=R.board||={key:null,img:null,scratch:new OffscreenCanvas(tw,th)};
  if(B.key!==key){const tg=B.scratch.getContext('2d'),fh=Math.floor(th/3.9);tg.clearRect(0,0,tw,th);tg.font=`bold ${fh}px Consolas,monospace`;tg.textBaseline='top';
    tg.fillStyle=P.boardColor;tg.shadowColor=P.boardColor;tg.shadowBlur=5;lines.forEach((t,i)=>tg.fillText(t,6,4+i*(fh+Math.floor(fh*.35))));
    B.img?.close();B.img=B.scratch.transferToImageBitmap();B.key=key}
  g.globalAlpha=state==='offline'?.6:1;g.drawImage(B.img,tx,ty);g.globalAlpha=1;
}

// Floor props near the side walls keep their whole visible width on the floor side of the wall's
// foot line (L.floorEdge), so a wide pot never pushes into the wall.
function onFloor(id,feet,height){
  const it=R.decoCat.items.find(i=>i.id===id),[x0,y0,x1,y1]=it.alpha_bounds_px,s=height/(y1-y0),[ax]=it.anchor_px,[fx,fy]=feet,e=L.floorEdge;
  const minX=e.left[0]+e.left[2]*(fy-e.left[1])+6+(ax-x0)*s,maxX=e.right[0]+e.right[2]*(fy-e.right[1])-6-(x1-ax)*s;
  return [Math.min(Math.max(fx,minX),maxX),fy];
}
// A decoration prop sized by its VISIBLE height, standing (or hanging) with its anchor at `feet`.
async function deco(id,feet,height,seed){
  const item=R.decoCat.items.find(i=>i.id===id),[,y0,,y1]=item.alpha_bounds_px,scale=height/(y1-y0),lit=!!item.lighting?.enabled;
  const frames=await Promise.all(item.animation.frames.map(f=>bitmap(at('decorations-v1/',f))));
  return {id,item,feet,scale,size:256*scale,x:feet[0]-item.anchor_px[0]*scale,y:feet[1]-item.anchor_px[1]*scale,seed,lit,frames,
    unlit:lit?await bitmap(at('decorations-v1/',item.unlit)):null,glows:lit?await Promise.all(item.animation.glow_frames.map(f=>bitmap(at('decorations-v1/',f)))):null,
    width:(item.alpha_bounds_px[2]-item.alpha_bounds_px[0])*scale,glowMax:item.lighting?.maximum_opacity??.5};
}

// ------------------------------------------------------------------ dressing the room from a seed
async function dress(){
  if(PLATE)return dressPlate();
  R.day=localDay();seedText=`${SEED}|${ROOM_ID}|${R.day}|${shuffle}`;
  const r=rng(hash(seedText));rand=rng(hash(seedText+'|events'));
  for(const el of document.querySelectorAll('#actors .furniture,#actors .prop'))el.remove();
  const used=new Set(),fresh=pool=>{const left=pool.filter(p=>!used.has(p));const id=pick(left.length?left:pool,r);used.add(id);return id};
  const wall=[],floor=[];R.benches=[];R.obstacles=[];R.screenStands={};
  const T=L.theme;   // the room's themed pools
  const dz=L.design||{};   // this room's hand-made choices (composer_rooms DESIGN)
  // Lighting is one scheme per room: a matched pair of hanging lamps, symmetric about the bay,
  // or a matched pair of wall lamps centred on the two pillars.
  const lg=L.lighting,bayMid=(L.bay.x[0]+L.bay.x[1])/2;let region=[...L.bay.x];
  const scheme=dz.lightingScheme||L.lightingScheme||(r()<.5?'hanging':'pillars');
  R.painted=(dz.paintedLights||[]).map(([x,y])=>({feet:[x,y],cast:200}));
  if(scheme==='hanging'||scheme==='both'){
    const id=dz.hangingLamp||fresh(T.hangingLamp),off=between(...lg.hangingOffset,r),h=between(...lg.hangingHeight,r);
    const xs=dz.hangingX||[bayMid-off,bayMid+off];   // a design can hang them under fixtures painted on the plate
    for(const x of xs){const d=await deco(id,[x,lg.hangingFeet],h,hash(seedText+x)%997);d.cast=210;wall.push({kind:'deco',d})}
    if(!dz.hangingX)region=[bayMid-off+42,bayMid+off-42];   // panels sit between the lamps, never under one
  }
  if(scheme==='pillars'||scheme==='both'){
    const id=dz.wallLamp||fresh(T.wallLamp),h=between(...lg.pillarHeight,r);
    for(const x of lg.pillarCenters){const d=await deco(id,[x,lg.pillarFeet],h,hash(seedText+x)%997);d.cast=230;wall.push({kind:'deco',d})}
  }
  // Labs: the two result screens own the bay (with the auxiliary panel between them when it fits).
  if(LAB){
    const sh=L.screens.height,items=['pass','aux','fail'].map(k=>k==='aux'?{kind:'panel',p:R.panelItems.find(p=>p.item.role!=='main'),h:56}:{kind:'screen',side:k,h:sh});
    for(const b of items)b.w=b.kind==='screen'?sh*R.screens[b.side].size[0]/R.screens[b.side].size[1]:b.h*b.p.item.size[0]/b.p.item.size[1];
    let list=items;if(items.reduce((s,b)=>s+b.w,0)+40>region[1]-region[0])list=items.filter(b=>b.kind==='screen');
    {const span=region[1]-region[0]-14*(list.length+1),sum=list.reduce((s,b)=>s+b.w,0);if(sum>span){const k=span/sum;for(const b of list){b.w*=k;b.h*=k}}}   // never overlap
    const g=(region[1]-region[0]-list.reduce((s,b)=>s+b.w,0))/(list.length+1),line=(L.bay.y[0]+L.bay.y[1])/2;let sx=region[0]+g;
    for(const b of list){const top=line-b.h/2;
      if(b.kind==='screen'){const sc=R.screens[b.side],tx=sx+sc.text[0]*b.w,ty=top+sc.text[1]*b.h;
        wall.push({kind:'screen',side:b.side,img:sc.img,x:sx,y:top,w:b.w,h:b.h,text:[tx,ty,sc.text[2]*b.w,sc.text[3]*b.h]});R.screenStands[b.side]=[sx+b.w/2,L.staffLaneY-8]}
      else wall.push({kind:'panel',p:b.p,x:sx,y:top,w:b.w,h:b.h,seed:hash(seedText+b.p.item.id)%997});
      sx+=b.w+g}
  }else{
  // Back wall: the matched panels (and sometimes a themed sign) on one centre line, evenly spaced.
  const bayItems=R.panelItems.map(p=>({kind:'panel',p,h:p.item.role==='main'?between(82,90,r):between(56,62,r)}));
  if(r()<.4)bayItems.push({kind:'sign',id:fresh(T.wallSign),h:between(30,36,r)});
  for(const b of bayItems){if(b.kind==='panel')b.w=b.h*b.p.item.size[0]/b.p.item.size[1];else{const [x0,y0,x1,y1]=R.decoCat.items.find(i=>i.id===b.id).alpha_bounds_px;b.w=b.h*(x1-x0)/(y1-y0)}}
  // Never overflow the bay: drop the sign first, then scale everything down to fit.
  const span=region[1]-region[0],sum=list=>list.reduce((s,b)=>s+b.w,0)+12*(list.length+1);
  let fitting=sum(bayItems)>span?bayItems.filter(b=>b.kind!=='sign'):bayItems;
  if(sum(fitting)>span){const k=(span-12*(fitting.length+1))/fitting.reduce((s,b)=>s+b.w,0);for(const b of fitting){b.w*=k;b.h*=k}}
  const order=choose(fitting,fitting.length,r),total=order.reduce((s,b)=>s+b.w,0),gap=(span-total)/(order.length+1),line=(L.bay.y[0]+L.bay.y[1])/2;
  let cx=region[0]+gap;
  for(const b of order){
    const top=line-b.h/2;
    if(b.kind==='panel')wall.push({kind:'panel',p:b.p,x:cx,y:top,w:b.w,h:b.h,seed:hash(seedText+b.p.item.id)%997});
    else wall.push({kind:'deco',d:await deco(b.id,[cx+b.w/2,top+b.h],b.h,hash(seedText+b.id)%997)});
    cx+=b.w+gap;
  }
  }
  // A signal tower beside Door B shows the room's state: green working, amber waiting, red after a
  // failure (blinking), dark when the room is empty or offline.
  if(!LAB&&dz.signal!==false){const [sx,sf,sh]=dz.signal||L.signal;wall.push({kind:'signal',d:await deco('lights/signal-tower',[sx,sf],sh,hash(seedText+'signal')%997)})}
  // Back row between the doors: one light pattern per seed, spread across the floor with seeded gaps.
  const bf=L.backFloor,pattern=dz.backRow||pick(L.backRow,r),benches=dz.backBenches||choose(T.bench,2,r);benches.forEach(b=>used.add(b));let bi=0,ui=0;
  const row=[];
  for(const kind of pattern){
    if(kind==='bench'){row.push({kind,id:benches[bi++],w:BENCH_W})}
    else{const id=dz.backUnits?.[ui++]||fresh(T.floorUnit);used.add(id);const h=between(56,68,r)*SC.furniture,[x0,y0,x1,y1]=R.decoCat.items.find(i=>i.id===id).alpha_bounds_px;row.push({kind,id,h,w:h*(x1-x0)/(y1-y0)})}
  }
  const free=bf.x[1]-bf.x[0]-row.reduce((s,i)=>s+i.w,0),parts=[...row,null].map(()=>between(.6,1.4,r)),psum=parts.reduce((a,b)=>a+b,0);
  let x=bf.x[0]+free*parts[0]/psum;
  for(const [i,it] of row.entries()){
    const feet=Math.round(between(...bf.feet,r)),mid=x+it.w/2;
    if(it.kind==='bench')await bench(it.id,mid,feet);
    else floor.push(await deco(it.id,[mid,feet],it.h*persp(feet),hash(seedText+it.id)%997));
    x+=it.w+free*parts[i+1]/psum;
  }
  // Piles beside the alcoves (three loose columns each), and the drone's dock by Door A.
  const col=(p,n)=>[0,1,2].map(k=>[p.x[0]+(p.x[1]-p.x[0])*k/2+between(-3,3,r),p.feet+Math.round(between(-2,2,r))]);
  R.dock=L.dock;   // every room has a drone; labs have no piles for it to haul
  if(!LAB){R.crateSpots=col(L.cratePile);R.debrisSpots=col(L.debrisPile)}
  else{R.crateSpots=[];R.debrisSpots=[];   // labs: greenery or instruments beside each alcove instead
    // (a room with planters built into its wall sets beside:false and needs none)
    for(const s of dz.beside===false?[]:L.beside){const plant=r()<.7,f=[between(...s.x,r),s.feet];floor.push(await deco(fresh(plant?T.floorPlant:T.floorUnit),f,(plant?between(84,108,r)*SC.plants:between(58,70,r)*SC.furniture)*persp(f[1]),hash(seedText+s.x[0]+'b')%997))}}
  // Back corners: usually a tall plant.
  for(const c of L.corners){const plant=true,id=fresh(plant?T.floorPlant:T.floorUnit),f0=[between(...c.x,r),between(...c.feet,r)],h=(plant?between(112,136,r)*SC.plants:between(66,78,r)*SC.furniture)*persp(f0[1]);floor.push(await deco(id,onFloor(id,f0,h),h,hash(seedText+c.x[0])%997))}
  // Side walls: each wall gets its own piece at its own depth: a wall-depth surface with one themed
  // piece on it (sidewall-v2), or one of that wall's themed assemblies (drawn for that side's camera,
  // never mirrored), and sometimes a smaller second piece or a plant. Nothing used here repeats.
  const depths=[];
  for(const s of L.sideWalls){
    const left=s.side==='left',own=T.bench.filter(id=>id.startsWith(s.side)&&!used.has(id)),fixed=dz.walls?.[s.side];let near;
    if(fixed||!own.length||r()<.6){
      let feet=fixed?.feet;if(feet==null){let tries=0;do{feet=between(...L.wallFeet,r)}while(depths.some(d=>Math.abs(d-feet)<120)&&++tries<30)}
      // A piece drawn for the other wall is mirrored: its anchors and bounds flip with it.
      const item=id=>{const it=R.sideCat.items.find(i=>i.id===id);if(it.side===s.side)return {...it,mirror:false};
        const fx=x=>256-x,[a0,a1,a2,a3]=it.alpha_bounds_px;
        return {...it,mirror:true,alpha_bounds_px:[fx(a2),a1,fx(a0),a3],support_anchor_px:it.support_anchor_px&&[fx(it.support_anchor_px[0]),it.support_anchor_px[1]],contact_anchor_px:it.contact_anchor_px&&[fx(it.contact_anchor_px[0]),it.contact_anchor_px[1]]}};
      const tb=item(fixed?.surface||fresh(T.wallSurface[s.side])),crt=item(fixed?.prop||fresh(T.wallProp[s.side]));
      used.add(tb.id);used.add(crt.id);
      const {k,x:px,top}=wallPose(s.side,feet,tb),x=px+(fixed?.dx||0);
      furniture(at('sidewall-v2/',tb.body),x,top,256*k,feet,tb.mirror);
      const [sx,sy]=tb.support_anchor_px,ps=crt.staging_scale*SC.props*k,[cx2,cy2]=crt.contact_anchor_px;
      furniture(at('sidewall-v2/',crt.body),x+sx*k-cx2*ps,top+sy*k-cy2*ps,256*ps,feet+1,crt.mirror);
      const [x0,y0,x1,y1]=tb.alpha_bounds_px;R.obstacles.push([x+x0*k,top+y0*k,(x1-x0)*k,(y1-y0)*k]);
      const midY=top+(y0+y1)/2*k;R.benches.push({stand:left?[x+x1*k+56*k/1.25,midY+18]:[x+x0*k-54*k/1.25,midY+18],facing:left?-1:1,busy:null});
      near=feet;R.wallSpan=[top+y0*k,feet];   // the bench runs from its far end down to its feet
    }else{
      const id=pick(own,r);used.add(id);let feet,tries=0;do{feet=between(500,820,r)}while(depths.some(d=>Math.abs(d-feet)<90)&&++tries<30);
      await bench(id,sideX(s.side,id,feet,.56),feet,{scale:.56,side:s.side});near=feet;
    }
    depths.push(near);
    // Free-standing assemblies by the side walls only where a design has checked they fit (extrasOk);
    // elsewhere they collide with the wall's angles and stay in the back row.
    const extras=dz.extrasOk&&fixed?.extra?[fixed.extra]:[];
    // The free-standing piece goes clear of the bench: in front of its near end, or behind its far end.
    let f2=null;const [far,front]=R.wallSpan&&near===R.wallSpan[1]?R.wallSpan:[near-170,near];
    // ...and on clear floor: not on other furniture, nor on a corner unit or plant already standing there.
    const clearAt=(id,f)=>{if(!id)return true;const a=R.assemblies.find(x=>x.id===id),[x0,y0,x1]=R.finCat.items.find(x=>x.id===a.surface).alpha_bounds_px,s=.5*SC.furniture*persp(f),cx=sideX(s.side??sSide,id,f,.5);
      // Its footprint must not touch furniture; a unit or plant standing just behind it (within 130 px
      // of depth) would also look perched on top, so those need that gap as well.
      const top=f-(236-y0)*s*.5,hit=(gap)=>([x,y,w,h])=>cx+(x0-128)*s<x+w&&cx+(x1-128)*s>x&&top-gap<y+h&&f>y;
      return !R.obstacles.some(hit(0))&&!floor.some(d=>{const [b0,c0,b1,c1]=d.item.alpha_bounds_px;return hit(130)([d.x+b0*d.scale,d.y+c0*d.scale,(b1-b0)*d.scale,(c1-c0)*d.scale])})};
    const sSide=s.side,want=fixed?.extra||(extras.length?extras[0]:null);
    for(let i=0;i<60;i++){const f=between(390,675,r);if((f>front+90||f<far-70)&&clearAt(want,f)){f2=f;break}}   // the straight wall only
    R.wallSpan=null;
    if(f2!==null&&extras.length){const id=fixed?.extra||pick(extras,r);used.add(id);await bench(id,sideX(s.side,id,f2,.5),f2,{scale:.5,side:s.side})}
    else if(f2!==null){const id=fresh(T.floorPlant),h=between(78,104,r)*SC.plants*persp(f2);floor.push(await deco(id,onFloor(id,[between(...L.sideWallPlant[s.side],r),f2],h),h,hash(seedText+s.side)%997))}
  }
  // Front corners: sometimes a small plant or unit.
  for(const c of L.frontCorners)if(dz.frontCorners!==false&&(LAB||r()<.6)){const plant=LAB||r()<.6,id=fresh(plant?T.floorPlant:T.floorUnit),h=(plant?between(64,86,r)*SC.plants:between(52,62,r)*SC.furniture)*persp(c.feet);floor.push(await deco(id,onFloor(id,[between(...c.x,r),c.feet],h),h,hash(seedText+c.x[0]+'f')%997))}
  R.wall=wall;R.floor=floor;
  for(const f of floor){const c=document.createElement('canvas');c.className='prop';c.width=Math.ceil(f.size);c.height=Math.ceil(f.size);
    Object.assign(c.style,{left:f.x+'px',top:f.y+'px',width:f.size+'px',height:f.size+'px',zIndex:Math.round(f.feet[1])});f.el=c;f.g=c.getContext('2d');f.g.imageSmoothingEnabled=false;f.shown=-1;$('#actors').append(c)}
  // Result bulbs sit on the front trim's top ledge, one group of five per ledge segment.
  const B=L.bulbs;R.bulbXs={pass:[],fail:[]};
  if(!LAB)for(const [side,[a,b]] of Object.entries(B.groups)){const span=B.spacing*4,c0=between(a+span/2+8,b-span/2-8,r);R.bulbXs[side]=[0,1,2,3,4].map(i=>c0-span/2+i*B.spacing)}
  $('#bulb-design').value=pick(BULBS,r);await loadBulb($('#bulb-design').value);
  for(const f of floor){const [x0,y0,x1,y1]=f.item.alpha_bounds_px;R.obstacles.push([f.x+x0*f.scale,f.y+y0*f.scale,(x1-x0)*f.scale,(y1-y0)*f.scale])}
  const clear=p=>!R.obstacles.some(([x,y,w,h])=>p[0]>x-30&&p[0]<x+w+30&&p[1]>y-6&&p[1]<y+h+14);
  R.stand=scatter(L.stand,8,72*SC.people,r).filter(s=>clear(s.p));R.work=scatter(L.work,10,92*SC.people,r).filter(s=>clear(s.p));R.benches=R.benches.filter(b=>!R.obstacles.some(([x,y,w,h])=>b.stand[0]>x&&b.stand[0]<x+w&&b.stand[1]>y&&b.stand[1]<y+h));   // beside a bench, never inside furniture
  R.pool=floorPool(r);
  $('#seed').textContent=seedText;
}
// Wall-depth furniture follows the room's perspective. The artist's fitted pose (left x -60, right
// x 1220, scale 1.25, feet at 740) is the reference; nearer or deeper pieces scale with depth and
// slide along the line where the side wall meets the floor, keeping their wall side on it.
function wallPose(side,feet,item){
  const k=1.25*persp(feet)/persp(740),left=side==='left',[bx0,,bx1]=item.alpha_bounds_px,edge=left?bx0:bx1;
  const wallX=(left?-60:1220)+edge*1.25+(left?-1:1)*L.wallSeam*(feet-740);
  return {k,x:wallX-edge*k,top:feet-236*k};
}
// The x for a free-standing assembly by a side wall: its whole surface on the floor side of the
// wall's foot line at that depth (L.floorEdge), with a small gap.
function sideX(side,id,feet,scale){
  const a=R.assemblies.find(x=>x.id===id),[x0,,x1]=R.finCat.items.find(x=>x.id===a.surface).alpha_bounds_px,s=scale*SC.furniture*persp(feet),e=L.floorEdge;
  // Near the front the wall line runs past the plate's edges: keep the piece inside the picture too.
  return side==='left'?Math.max(e.left[0]+e.left[2]*(feet-e.left[1]),56)+14+(128-x0)*s:Math.min(e.right[0]+e.right[2]*(feet-e.right[1]),1480)-14-(x1-128)*s;
}
async function bench(id,cx,feet,{scale=.44,side='back'}={}){
  const a=R.assemblies.find(x=>x.id===id),surface=R.finCat.items.find(x=>x.id===a.surface),prop=R.finCat.items.find(x=>x.id===a.prop),s=scale*SC.furniture*persp(feet),x=cx-128*s,y=feet-236*s;
  furniture(at('finishing-v1/',surface.body),x,y,256*s,feet);
  furniture(at('finishing-v1/',prop.body),x+a.prop_origin_px[0]*s,y+a.prop_origin_px[1]*s,256*a.prop_scale*s,feet+1);
  const [x0,y0,x1,y1]=surface.alpha_bounds_px;R.obstacles.push([x+x0*s,y+y0*s,(x1-x0)*s,(y1-y0)*s]);
  // Tinker at the end that faces the room: right end for back-row and left-wall pieces, left end on the right wall.
  R.benches.push(side==='right'?{stand:[cx+(x0-128)*s-30,feet+14],facing:1,busy:null}:{stand:[cx+(x1-128)*s+30,feet+(side==='back'?22:14)],facing:-1,busy:null});
}
// Arena 2.5.0: the room's floor map. Every spot an agent can stand or work on, spread over the whole walkable floor
// (the side-wall foot lines in kit rooms, the measured floor in the fixed rooms), minus the furniture (R.obstacles)
// and the door lanes. Replaces the 8 standing / 10 work spots whose overflow all went to one shared point.
const WALK_PLATE={x:[150,1385],y:[585-BAY_BACK-20,945]};
function floorX(y){
  const e=L.floorEdge;if(PLATE||!e)return WALK_PLATE.x;
  return [Math.max(e.left[0]+e.left[2]*(y-e.left[1]),40)+46,Math.min(e.right[0]+e.right[2]*(y-e.right[1]),1496)-46];
}
function floorPool(r){
  const [y0,y1]=PLATE?WALK_PLATE.y:[300,905],step=46*SC.people,out=[];
  const lanes=Object.values(R.doors||{}).flatMap(d=>[d.approach,d.entry,d.threshold].filter(Boolean));
  for(let y=y0;y<=y1;y+=step*.82){
    const [xa,xb]=floorX(y);
    for(let x=xa;x<=xb;x+=step){
      const p=[x+(r()-.5)*step*.5,y+(r()-.5)*step*.3];
      if(R.obstacles.some(([ox,oy,w,h])=>p[0]>ox-28&&p[0]<ox+w+28&&p[1]>oy-6&&p[1]<oy+h+16))continue;
      if(lanes.some(q=>Math.hypot(q[0]-p[0],(q[1]-p[1])*1.4)<70))continue;
      out.push({p,busy:null});
    }
  }
  return out;
}
// A free spot as far as possible from everyone already placed (a little randomness keeps it from looking drilled);
// when the room is fuller than its floor, the least crowded spot is shared rather than one point for everyone.
function takeSpot(who){
  const pool=R.pool||[];if(!pool.length)return null;
  const taken=pool.filter(s=>s.busy).map(s=>s.p),free=pool.filter(s=>!s.busy),list=free.length?free:pool;
  let best=null,score=-1;
  for(const s of list){const d=taken.length?Math.min(...taken.map(q=>Math.hypot(q[0]-s.p[0],(q[1]-s.p[1])*1.5))):1e4,v=d*(.75+rand()*.5);if(v>score){score=v;best=s}}
  if(free.length)best.busy=who;
  return best;
}
function furniture(url,x,y,size,feetY,mirror=false){
  const img=document.createElement('img');img.className='furniture';img.src=url;img.alt='';
  Object.assign(img.style,{left:x+'px',top:y+'px',width:size+'px',height:size+'px',zIndex:Math.round(feetY)});if(mirror)img.style.transform='scaleX(-1)';$('#actors').append(img);
}
function scatter(box,count,gap,r){
  const out=[];
  for(let i=0;i<5000&&out.length<count;i++){const p=[between(...box.x,r),between(...box.y,r)];if(out.every(q=>Math.hypot((p[0]-q[0])*.8,p[1]-q[1])>gap))out.push(p)}
  return out.map(p=>({p,busy:null}));
}

// ------------------------------------------------------------------ bulbs
async function loadBulb(id){
  const item=R.finCat.items.find(i=>i.id===id);
  const [body,em,gl]=await Promise.all([item.body,item.emission_mask,item.glow_mask].map(f=>bitmap(at('finishing-v1/',f))));
  const [,y0,,y1]=item.alpha_bounds_px;R.bulb={item,body,em,gl,tints:new Map(),scale:L.bulbs.height/(y1-y0)};
}
function tint(mask,color,key){
  const cache=R.bulb.tints,k=key+color;if(cache.has(k))return cache.get(k);
  const c=new OffscreenCanvas(256,256),g=c.getContext('2d');g.fillStyle=color;g.fillRect(0,0,256,256);g.globalCompositeOperation='destination-in';g.drawImage(mask,0,0);
  cache.set(k,c);return c;
}

// ------------------------------------------------------------------ simulation clock
let clock=0;const waiters=new Set();
const until=cond=>new Promise(res=>waiters.add({cond,res}));
const wait=ms=>{const t=clock+ms;return until(()=>clock>=t)};
function settle(){for(const w of [...waiters]){const v=w.cond();if(v){waiters.delete(w);w.res(v)}}}

// ------------------------------------------------------------------ entities
const E=new Set();
const NAMES=[['Validator','hsl(160 60% 66%)'],['Architect','hsl(36 70% 64%)'],['Code Writer','hsl(208 75% 70%)'],['Reviewer','hsl(340 65% 72%)'],
  ['Retriever','hsl(258 60% 74%)'],['Triage','hsl(98 55% 64%)'],['Tester','hsl(48 85% 66%)'],['Planner','hsl(188 70% 64%)'],['Router','hsl(12 70% 68%)'],['Summarizer','hsl(290 45% 72%)']];
const ACTIONS=['inference','code_generation','testing','review','retrieval','tool_use','validation','evaluation'];
const LABEL={inference:'Inference',code_generation:'Writing code',testing:'Testing',review:'Reviewing',retrieval:'Retrieving',tool_use:'Using a tool',validation:'Validating',evaluation:'Evaluating'};
const IDLES=['idle-look','idle-blink','idle-settle','idle-watch','idle-ankles'];
// Each application's agents get their own hue family, so Ticketing and Coder agents read apart.
function appColor(hsl){if(APP==='Ticketing')return hsl;const m=/hsl\((\d+)/.exec(hsl);return m?hsl.replace(m[1],String((+m[1]+150)%360)):hsl}
let nameIndex=0;
// Arena 2.4.0: agent bodies (arena_service/appearance.py decides them for live agents); the cat is not an agent.
const BODY_FILES=['a-round','b-wheel','c-hex','d-wide','e-dome','f-kite','g-tread','h-robot-cat'];
const AGENT_BODIES=['classic','a-round','b-wheel','c-hex','d-wide','e-dome','f-kite','g-tread'];
const PALETTE_KEYS=['primary-shade','primary-base','primary-shine','accent'];
// A palette from one color when the hub sent none (demo agents, or a hub older than Arena 2.4.0).
const paletteOf=c=>({'primary-shade':`color-mix(in srgb,${c} 62%,#071421)`,'primary-base':c,'primary-shine':`color-mix(in srgb,${c} 55%,#ffffff)`});
function dressAgent(a,body,palette,color){
  a.body=AGENT_BODIES.includes(body)?body:'classic';
  const p={...paletteOf(color),...(palette||{})};for(const k of PALETTE_KEYS)if(p[k])a.el.style.setProperty('--'+k,p[k]);else a.el.style.removeProperty('--'+k);
  a.el.dataset.body=a.body;a.palette=palette||null;a.svgKey=null;a.svgCache=null;
}
function spawnAgent(identity){
  const slot=nameIndex,[base,hue]=NAMES[nameIndex++%NAMES.length],[name,color]=identity?[identity.name,identity.color]:[base,appColor(hue)];
  const el=document.createElement('article');el.className='actor idle';
  el.innerHTML=`<div class="speech"></div>${R.svg}<div class="work-prop" aria-hidden="true"><i></i><i></i><i></i></div><div class="actor-name"></div><div class="actor-state"></div><div class="actor-host"></div>`;
  el.style.setProperty('--agent',color);el.querySelector('.actor-name').textContent=name;$('#actors').append(el);
  const a={kind:'agent',el,name,x:0,y:0,path:[],speed:between(150,176),state:'walking',action:null,facing:1,force:null,idle:null,idleUntil:0,nextIdle:0};E.add(a);
  a.baseSvg=el.querySelector('svg.avatar');   // the classic rig's posed SVG, kept for a body change back to classic
  dressAgent(a,identity?identity.body:AGENT_BODIES[slot%AGENT_BODIES.length],identity?.palette,color);
  if(LIVE)el.onclick=()=>window.composer?.live?.onSelect?.(a.ref);
  return a;
}
function spawnStaff(kind){
  const el=document.createElement('div');el.className='staff '+kind+(CHAR[kind]?' char':'');const img=document.createElement('img');img.alt='';el.append(img);$('#actors').append(el);
  const s={kind,el,img,x:0,y:0,path:[],speed:kind==='drone'?150:112,anim:kind==='drone'?'hover':'walk',facing:1};E.add(s);return s;
}
// People drawn from characters-v1: the catalog actor for each kind, and how our actions map onto
// the delivered animations. Every frame is 160 x 144 with the feet at (80, 124).
const CHAR={broom:'broom-cleaner',mop:'mop-cleaner',scrubber:'floor-scrubber',guard:'security-guard',mechanic:'mechanic',inspector:'inspector'};
const ACT={work:'clean',look:'watch'};
// The night crew: broom, mop (with her bucket), guard and the scrubber robot.
function spawnCrew(kind){
  const el=document.createElement('div');el.className='staff char '+kind;const img=document.createElement('img');img.alt='';el.append(img);$('#actors').append(el);
  const c={kind,el,img,x:0,y:0,path:[],speed:kind==='scrubber'?60:kind==='guard'?100:92,anim:'walk',facing:1,view:'angle'};E.add(c);return c;
}
// The robot cat (Arena 2.4.0, the operator's easter egg): it tours the rooms one at a time on the shared clock
// (cat_tour.mjs), comes in by a door, wanders, sits, sometimes follows the cleaning drone, and leaves by a door.
// ?cat=here keeps it in this room (previews). Not an agent: no name, no status, no events.
const CAT_HERE=Q.get('cat')==='here';
function spawnCat(){
  const el=document.createElement('article');el.className='actor cat-bot framed';el.style.display='none';
  el.innerHTML=`<div class="speech"></div>${R.svg}<div class="actor-name"></div><div class="actor-state"></div>`;$('#actors').append(el);
  return {kind:'cat',el,name:'cat',x:0,y:0,path:[],speed:96,facing:1,sitFace:'front'};
}
async function catLife(){
  const cat=spawnCat(),here=()=>CAT_HERE||catHere(ROOM_ID,Date.now());
  for(;;){
    await until(()=>here()&&!offline());
    await enter(cat,randomDoor());
    while(here()){
      const drone=[...E].find(o=>o.kind==='drone'&&!o.portal);
      const p=drone&&rand()<.35?[Math.min(FLOOR.x[1],Math.max(FLOOR.x[0],drone.x+between(-70,70))),Math.min(FLOOR.y[1],Math.max(FLOOR.y[0],drone.y+40))]:floorPoint();
      cat.speed=between(70,125);await go(cat,[p]);
      cat.sitFace=pick(['front','front','left','right']);cat.sitAt=clock;await wait(between(2500,8000));
    }
    await exit(cat,randomDoor(),[],{keep:true});
  }
}
function spawnBucket(owner){
  const el=document.createElement('div');el.className='prop-sprite char bucket';const img=document.createElement('img');img.alt='';img.src=at('characters-v1/',R.chars.props['mop-bucket'].png).href;el.append(img);$('#actors').append(el);
  const b={kind:'bucket',el,img,x:owner.x,y:owner.y,path:[],speed:0,facing:1,owner};E.add(b);return b;
}
const HUMANS=new Set(['mechanic','inspector','broom','mop']);   // people turn the lights on; the guard keeps them off
function remove(e){e.el.remove();E.delete(e);e.gone=true}
function place(e,p){e.x=p[0];e.y=p[1]}
const go=(e,points)=>new Promise(res=>{e.path=points.map(p=>[...p]);e.arrived=res;if(!e.path.length)res()});
function step(e,dt){
  if(e.kind==='bucket'&&e.owner){if(e.owner.gone){remove(e);return}if(e.carried){e.x=e.owner.x-26*e.owner.facing;e.y=e.owner.y+5;e.portal=e.owner.portal}return}
  if(!e.path.length)return;
  const [tx,ty]=e.path[0],dx=tx-e.x,dy=ty-e.y,dist=Math.hypot(dx,dy),move=e.speed*dt/1000*depth(e.y);
  if(Math.abs(dx)>.5)e.facing=dx<0?-1:1;
  if(move>=dist){e.x=tx;e.y=ty;e.path.shift();if(!e.path.length){const f=e.arrived;e.arrived=null;f?.()}}
  else{e.x+=dx/dist*move;e.y+=dy/dist*move}
}
// Doors open fully before anyone crosses and stay open while anyone is in or at the portal.
const doorOpen=d=>d.frame_i>=d.panels.length-1;
function doorTick(d){
  const target=d.holds.size?d.panels.length-1:0;
  if(d.holds.size)d.idleSince=clock;else if(clock-d.idleSince<450)return;
  if(d.frame_i!==target&&clock-d.last>=d.frameMs){d.frame_i+=Math.sign(target-d.frame_i);d.last=clock}
}
async function enter(e,key){
  if(!E.has(e)){E.add(e);e.el.style.display='';e.gone=false}
  const d=R.doors[key],inside=[d.threshold[0],d.threshold[1]-22];
  place(e,inside);e.portal=d;d.holds.add(e);e.waiting=true;
  await until(()=>doorOpen(d));e.waiting=false;
  await go(e,[d.threshold]);e.portal=null;await go(e,[d.approach]);d.holds.delete(e);
  return d;
}
async function exit(e,key,via=[],{keep=false}={}){
  const d=R.doors[key];
  await go(e,[...via,d.approach]);d.holds.add(e);e.waiting=true;
  await until(()=>doorOpen(d));e.waiting=false;
  await go(e,[d.threshold]);e.portal=d;await go(e,[[d.threshold[0],d.threshold[1]-22]]);
  d.holds.delete(e);
  if(keep){e.el.style.display='none';E.delete(e);e.portal=null;e.el.style.clipPath='';e.clipped=false}else remove(e);
}
const randomDoor=()=>pick(['a','b']);
function take(list,who){const free=list.filter(s=>!s.busy);if(!free.length)return null;const s=pick(free);s.busy=who;return s}

// An agent's visit: either door in, wait at a bench or in the crowd (sometimes shifting spot),
// work on the floor, finish, and leave by either door. Buttons can force the outcome.
async function agentLife(a,job=null){
  const door=randomDoor();await enter(a,door);
  a.state='waiting';a.action='waiting_model';
  const bench=rand()<.5?take(R.benches,a):null;let spot=bench?null:take(R.stand,a);
  await go(a,[R.doors[door].entry,bench?bench.stand:spot?.p||[360,640]]);
  a.bench=bench;a.poking=!!bench;
  const deadline=clock+between(4000,11000);
  while(!a.force&&clock<deadline){
    await until(()=>a.force||clock>=deadline||clock>=(a.shiftAt||=clock+between(2500,6000)));
    if(a.force||clock>=deadline)break;
    a.shiftAt=0;if(!bench&&rand()<.45){const next=take(R.stand,a);if(next){if(spot)spot.busy=null;spot=next;a.idle=null;await go(a,[spot.p])}}
  }
  a.poking=false;a.bench=null;if(bench)bench.busy=null;if(spot)spot.busy=null;a.idle=null;
  if(a.force==='leave'){a.state='walking';const o=randomDoor();await exit(a,o,[R.doors[o].entry]);if(job&&bus)bus.postMessage({type:'return',app:job.app,id:job.id,outcome:'canceled'});return}
  const slot=take(R.work,a);a.state='walking';
  await go(a,[slot?slot.p:[900,660]]);
  a.state='executing';a.action=pick(ACTIONS);
  const done=clock+between(8000,16000);
  await until(()=>a.force||clock>=done);
  const outcome=a.force==='leave'?'canceled':a.force||(rand()<.78?'pass':'fail');a.force=null;
  if(outcome!=='canceled'){a.state=outcome==='pass'?'completed':'failed';await wait(1400);record(outcome)}
  if(slot)slot.busy=null;a.state='walking';
  const out=randomDoor();await exit(a,out,[R.doors[out].entry]);
  if(job&&bus)bus.postMessage({type:'return',app:job.app,id:job.id,outcome});
}

// ------------------------------------------------------------------ results and staff
const counts={pass:0,fail:0},bulbs={pass:{lit:0,celebrate:0},fail:{lit:0,celebrate:0}},pile={crates:[],debris:[]};
const queue={crates:[],press:{pass:0,fail:0}},staff={mechanics:0,inspector:false,drone:null};
function record(outcome){counts[outcome]++;if(outcome==='fail')R.alertUntil=clock+3500;if(LAB){queue.press[outcome]++;runLabInspector();return}queue.crates.push(outcome);queue.press[outcome]++;runMechanic();runInspector()}
const delivered={pass:0,fail:0};
// Mechanics: each trip brings up to three results of one kind along the back lane and sets each
// one down. Pass: stacked by Door A. Fail: dropped by Door B, where it breaks. Under load a second
// mechanic joins, so the delivered crates keep up with the (always exact) counts.
async function runMechanic(){
  if(!queue.crates.length||staff.mechanics>=2||(staff.mechanics===1&&queue.crates.length<5))return;
  staff.mechanics++;
  try{
    while(queue.crates.length){
      const kind=queue.crates[0],batch=[];
      while(batch.length<3&&queue.crates[0]===kind)batch.push(queue.crates.shift());
      const m=spawnStaff('mechanic');m.anim='carry';await enter(m,randomDoor());
      const spots=kind==='pass'?R.crateSpots:R.debrisSpots,list=pile[kind==='pass'?'crates':'debris'];
      for(const _ of batch){
        if(kind==='pass'&&list.length>=10){await until(()=>list.length<10)}   // the drone is collecting a full stack
        const target=spots[list.length%spots.length],stand=[target[0],target[1]+12];
        m.anim='carry';await go(m,[[stand[0],L.staffLaneY],stand]);m.faceAway=true;m.anim='set-down';await wait(660);m.faceAway=false;
        if(kind==='pass')list.push({v:Math.floor(rand()*R.crates.length),dx:between(-3,3)});
        else for(let i=0;i<2;i++)list.push({v:Math.floor(rand()*R.debris.length),dx:between(-16,16),dy:between(-3,4)});
        if(pile.crates.length>=10||pile.debris.length>=8)runDrone();
      }
      m.anim='walk';const out=randomDoor();await exit(m,out,[[m.x,L.staffLaneY]]);
      if(queue.crates.length>=5)runMechanic();
    }
  }finally{staff.mechanics--}
}
// Inspector: one visit presses every pending result, down the aisle to the floor's front edge.
async function runInspector(){
  if(staff.inspector||!(queue.press.pass+queue.press.fail))return;staff.inspector=true;
  try{
    while(queue.press.pass+queue.press.fail){
      const s=spawnStaff('inspector'),door=randomDoor(),d=await enter(s,door);
      await go(s,[d.entry,[L.aisle,460],[L.aisle,L.inspectorY]]);
      for(const side of ['pass','fail']){
        if(!queue.press[side])continue;
        const xs=R.bulbXs[side];s.anim='walk';await go(s,[[(xs[0]+xs[4])/2,L.inspectorY]]);
        while(queue.press[side]){await until(()=>!bulbs[side].celebrate);s.anim='press';await wait(660);queue.press[side]--;lightBulb(side);s.anim='idle';await wait(350)}
      }
      if(rand()<.5){const px=between(L.bay.x[0]+40,L.bay.x[1]-40);s.anim='walk';await go(s,[[L.aisle,L.inspectorY],[L.aisle,460],[px,L.staffLaneY+6]]);
        s.faceAway=true;for(let i=0;i<2;i++){s.anim='tap';await wait(660);s.anim='idle';await wait(420)}s.faceAway=false;
        s.anim='walk';const out=randomDoor();await exit(s,out,[R.doors[out].entry])}
      else{s.anim='walk';const out=randomDoor();await exit(s,out,[[L.aisle,L.inspectorY],[L.aisle,460],R.doors[out].entry])}
    }
  }finally{staff.inspector=false}
}
function lightBulb(side){
  const b=bulbs[side];b.lit++;
  if(b.lit>=5){b.celebrate=clock;until(()=>clock-b.celebrate>=1500).then(()=>{b.lit=0;b.celebrate=0;if(side==='fail')runDrone()})}
}
// Drone: docked beside Door A's alcove. Hauls a full crate stack out a door; sweeps debris.
async function runDrone(){
  const d=staff.drone;if(!d||d.busy)return;
  const haul=pile.crates.length>=10,sweep=!haul&&pile.debris.length>=8;if(!haul&&!sweep)return;d.busy=true;
  try{
    if(haul){const c=R.crateSpots[1];d.anim='hover';await go(d,[[c[0],L.staffLaneY]]);await wait(500);pile.crates.splice(0,10);d.anim='carry';
      await exit(d,randomDoor());await wait(2500);const back=spawnStaff('drone');staff.drone=back;back.busy=true;await enter(back,randomDoor());await go(back,[R.dock]);back.busy=false}
    else{const c=R.debrisSpots[1];d.anim='hover';await go(d,[[c[0],L.staffLaneY]]);d.anim='sweep';await wait(2600);pile.debris.length=0;d.anim='hover';await go(d,[R.dock]);d.busy=false}
  }catch(e){console.error(e);d.busy=false}
  if(pile.crates.length>=10||pile.debris.length>=8)runDrone();
}

// The drone drifts about the room all the time, parks at its dock now and then, and sometimes
// slips out a door and comes back. Hauling and sweeping (runDrone) always take priority.
function floorPoint(){for(let i=0;i<40;i++){const p=[between(...FLOOR.x),between(...FLOOR.y)];if(!R.obstacles.some(([x,y,w,h])=>p[0]>x-20&&p[0]<x+w+20&&p[1]>y&&p[1]<y+h+20))return p}return[760,640]}
async function glide(e,points){e.path=points.map(p=>[...p]);e.arrived=null;await until(()=>!e.path.length||e.busy)}
async function droneLife(){
  for(;;){
    await wait(between(2500,7000));
    const d=staff.drone;if(!d||d.busy||d.gone||!E.has(d))continue;
    const roll=rand();d.anim='hover';
    if(roll<.12){
      d.busy=true;
      try{const o=randomDoor();await exit(d,o,[R.doors[o].approach],{keep:true});await wait(between(5000,14000));await enter(d,randomDoor());await go(d,[R.dock])}
      catch(e){console.error(e)}finally{d.busy=false}
      runDrone();
    }else await glide(d,[roll<.35?R.dock:floorPoint()]);
  }
}
// Night crew rounds, on an interval, mostly when the room is quiet.
async function crewLife(){
  await wait(between(15000,45000));
  for(;;){
    if(offline()||[...E].filter(e=>e.kind==='agent').length>4){await wait(30000);continue}
    try{await ({broom:runBroom,mop:runMop,guard:runGuard,scrubber:runScrubber})[pick(['broom','mop','guard','scrubber'])]()}catch(e){console.error(e)}
    await wait(between(60000,170000));
  }
}
async function crewIn(kind){const c=spawnCrew(kind),door=randomDoor(),d=await enter(c,door);await go(c,[d.entry]);return c}
async function crewOut(c){c.anim='walk';const o=randomDoor();await exit(c,o,[R.doors[o].entry])}
async function runBroom(){
  const c=await crewIn('broom');
  for(let pass=0;pass<between(3,5);pass++){
    const y=between(Math.max(470,FLOOR.y[0]),840),x0=between(300,560),x1=between(860,1180),[a,b]=rand()<.5?[x0,x1]:[x1,x0];
    c.anim='walk';c.speed=92;await go(c,[[a,y]]);c.anim='work';c.speed=38;await go(c,[[b,y+between(-30,30)]]);
  }
  c.speed=92;await crewOut(c);
}
async function runMop(){
  const c=await crewIn('mop'),bucket=spawnBucket(c);bucket.carried=true;
  const spot=floorPoint();c.anim='walk';await go(c,[spot]);bucket.carried=false;bucket.portal=null;await wait(600);
  for(let i=0;i<between(4,7);i++){c.anim='work';c.speed=30;await go(c,[[spot[0]+between(-150,150),spot[1]+between(-70,70)]]);await wait(between(500,1400))}
  c.anim='walk';c.speed=92;await go(c,[[bucket.x+26,bucket.y-5]]);c.facing=1;await wait(500);bucket.carried=true;
  await crewOut(c);
}
async function runGuard(){
  const c=await crewIn('guard'),stops=[...R.benches.map(b=>b.stand),...Object.values(R.doors).map(d=>d.entry),floorPoint()];
  for(const s of choose(stops,Math.min(stops.length,Math.round(between(3,5))),rand)){c.anim='walk';await go(c,[s]);c.anim='look';await wait(between(2200,4200))}
  await crewOut(c);
}
async function runScrubber(){
  const c=await crewIn('scrubber');c.anim='work';
  const x0=between(320,520),x1=between(940,1180);
  for(let y=between(Math.max(470,FLOOR.y[0]),Math.max(520,FLOOR.y[0]+50)),dir=1;y<840;y+=between(70,100),dir=-dir){await go(c,[[dir>0?x0:x1,y],[dir>0?x1:x0,y]])}
  await crewOut(c);
}
// Lights follow people: on while anyone (agent or human staff) is in the room, fading out a few
// seconds after the last one leaves. The wall switch (or L) overrides for a while, and startles
// everyone in the room.
// The robot cat counts as company: it switches an empty room's lights on while it visits.
const company=e=>e.kind==='agent'||e.kind==='cat'||HUMANS.has(e.kind);
const lightsWanted=()=>R.lightForce&&clock<R.lightForce.until?R.lightForce.on:[...E].some(company);
function lightStep(dt){
  const forced=R.lightForce&&clock<R.lightForce.until;
  if(!forced&&[...E].some(company))R.peopleAt=clock;
  if(R.scatterOn&&R.light>.5&&!(forced&&!R.lightForce.on))R.scatterOn=false;   // light is back: everyone walks back
  // The switch acts at once (on in ~140 ms, off in ~260 ms); people leaving fade the room out slowly.
  const target=forced?(R.lightForce.on?1:0):clock-(R.peopleAt??-1e9)<3500?1:0;
  if(target>R.light)R.light=Math.min(1,R.light+dt/(forced?140:320));else if(target<R.light)R.light=Math.max(0,R.light-dt/(forced?260:1600));
}
// Switched off, everyone scatters to the edges (render: visual); switched back on (or when the override ends and
// people are still here), they walk back.
function lightSwitch(){const on=R.light<.5;R.lightForce={on,until:clock+90000};startle();R.scatterOn=!on}
// Everyone in the room startles: a big double hop, rippling through the room. The cat is unimpressed.
function startle(){const now=performance.now();for(const e of E){if(e.kind==='cat')continue;const lag=rand()*220;e.jumpAt=now+lag;e.jumpUntil=now+lag+950}}
function roomState(){if(offline())return'offline';const a=[...E].filter(e=>e.kind==='agent');
  if(!a.length&&!away.size)return'empty';return a.some(x=>x.state==='executing')||away.size?'working':'waiting'}

// ------------------------------------------------------------------ orchestrator (lab) rooms
// A lab is an orchestrator's home: a resident crowd. Some work is done locally on the floor; the
// rest is dispatched out a door to an inference head and comes back. Each result sends the
// inspector to tap that side's back-wall screen, which shows the room's running totals.
const away=new Set(),screenCounts={pass:0,fail:0};
async function residentLife(a,arrive){
  if(arrive){const door=randomDoor();await enter(a,door);await go(a,[R.doors[door].entry])}
  for(;;){
    a.state='waiting';a.action='idle';
    const bench=rand()<.3?take(R.benches,a):null;let spot=bench?null:take(R.stand,a);
    await go(a,[bench?bench.stand:spot?.p||[360,640]]);a.bench=bench;a.poking=!!bench;
    const rest=clock+between(5000,16000);await until(()=>a.force||clock>=rest);
    a.poking=false;a.bench=null;if(bench)bench.busy=null;if(spot)spot.busy=null;a.idle=null;
    if(a.force==='leave'){a.force=null;a.state='walking';const o=randomDoor();return exit(a,o,[R.doors[o].entry])}
    let outcome;
    if(rand()<(bus?.35:.45)){   // local work on the floor
      const slot=take(R.work,a);a.state='walking';await go(a,[slot?slot.p:[900,660]]);
      a.state='executing';a.action=pick(ACTIONS);const done=clock+between(8000,14000);await until(()=>a.force||clock>=done);
      outcome=a.force==='leave'?'canceled':a.force||(rand()<.8?'pass':'fail');a.force=null;if(slot)slot.busy=null;
    }else{            // dispatched to a head: out through a door, back later
      a.state='walking';const o=randomDoor();await exit(a,o,[R.doors[o].entry],{keep:true});
      away.add(a);
      if(bus&&HEADS.length){
        const id=`${APP}-${a.name}-${Math.round(clock)}`,head=pick(HEADS);a.away=head;
        bus.postMessage({type:'dispatch',app:APP,id,head,name:a.name,color:a.el.style.getPropertyValue('--agent'),body:a.body,palette:a.palette});
        const limit=clock+180000;await until(()=>returns.has(id)||clock>=limit);   // a head that never answers: give up
        outcome=returns.get(id)?.outcome||'canceled';returns.delete(id);
      }else{await wait(between(14000,30000))}
      away.delete(a);a.away=null;
      const b=randomDoor();await enter(a,b);await go(a,[R.doors[b].entry]);outcome??=rand()<.8?'pass':'fail';
    }
    if(outcome!=='canceled'){a.state=outcome==='pass'?'completed':'failed';await wait(1300);record(outcome)}
  }
}
async function runLabInspector(){
  if(staff.inspector||!(queue.press.pass+queue.press.fail))return;staff.inspector=true;
  try{while(queue.press.pass+queue.press.fail){
    const s=spawnStaff('inspector'),din=await enter(s,randomDoor());if(PLATE)await go(s,[din.entry]);   // fixed rooms: round the desks
    for(const side of ['pass','fail']){
      if(!queue.press[side])continue;const st=R.screenStands[side];s.anim='walk';await go(s,[[st[0],L.staffLaneY],st]);
      s.faceAway=true;
      while(queue.press[side]){s.anim='tap';await wait(660);queue.press[side]--;screenCounts[side]++;s.anim='idle';await wait(300)}
      s.faceAway=false;
    }
    s.anim='walk';const out=randomDoor();await exit(s,out,[[s.x,L.staffLaneY],...(PLATE?[R.doors[out].entry]:[])]);
  }}finally{staff.inspector=false}
}

// ------------------------------------------------------------------ live drive
// The viewer hands the room what is true now: who is here and what each one is doing. Every actor
// walks from what it shows to what is true; results come only from real completions and failures.
// Heads: one actor per assignment here. Labs: one resident per agent whose home this is.
// Labs hold their residents plus a colleague per further concurrent run (scene_live LAB_EXTRA_CAP), so more fit.
// MIN_WORK: work is shown for at least this long, however fast the updates behind it come and go.
const actors=new Map(),finished=new Set(),RESULT=new Set(['pass','fail']),ENDED=new Set(['pass','fail','canceled']),CAP=LAB?28:16,MIN_WORK=3000;
let overflow=0;
function sync(model,{instant=false}={}){
  const seen=new Set();overflow=0;
  for(const d of model.actors){
    seen.add(d.key);let a=actors.get(d.key);
    if(!a){
      if(finished.has(d.key))continue;
      // Already over before this room saw it: skipped, unless it came and went between two updates (replay).
      if(ENDED.has(d.want)&&!(d.replay&&RESULT.has(d.want))){finished.add(d.key);continue}
      if(actors.size>=CAP){overflow++;continue}
      a=spawnAgent({name:d.name,color:d.color,body:d.body,palette:d.palette});a.key=d.key;a.want=d.want;a.replay=!!d.replay;actors.set(d.key,a);
      (LAB?residentDriven:visitDriven)(a,instant).catch(console.error).finally(()=>{if(actors.get(d.key)===a)actors.delete(d.key)});
    }
    if(!ENDED.has(a.want))a.want=d.want;
    a.ref=d.ref;a.agentKey=d.agentKey;a.label=d.label||'';a.wantAction=d.action||'inference';if(a.state==='executing')a.action=a.wantAction;
    if(a.name!==d.name){a.name=d.name;a.el.querySelector('.actor-name').textContent=d.name}
    if(d.color&&a.el.style.getPropertyValue('--agent')!==d.color)a.el.style.setProperty('--agent',d.color);if(d.body&&(d.body!==a.body||JSON.stringify(d.palette||null)!==JSON.stringify(a.palette)))dressAgent(a,d.body,d.palette,d.color);
  }
  for(const [k,a] of actors)if(!seen.has(k)&&!ENDED.has(a.want))a.want='gone';
  for(const k of finished)if(!seen.has(k))finished.delete(k);
  for(const r of model.results||[]){const a=actors.get(r.key);if(a)(a.results||=[]).push(r.outcome);else record(r.outcome)}
  if(!instant)for(const h of model.handoffs||[])handoff(h);   // Arena 2.5.0: hand-offs bring sender and receiver together
  if(model.totals)for(const side of ['pass','fail'])screenCounts[side]=Math.max(0,(model.totals[side]||0)-queue.press[side]);
}
function holder(a){
  const h={spot:null,slot:null,bench:null};
  h.release=()=>{if(h.spot)h.spot.busy=null;if(h.slot)h.slot.busy=null;if(h.bench)h.bench.busy=null;h.spot=h.slot=h.bench=null;a.poking=false;a.bench=null;a.idle=null};
  h.rest=async(instant,benchOdds)=>{h.release();h.bench=rand()<benchOdds?take(R.benches,a):null;h.spot=h.bench?null:takeSpot(a);
    const p=h.bench?h.bench.stand:h.spot?.p||[360,640];a.state='walking';instant?place(a,p):await go(a,[p]);a.state='waiting';a.bench=h.bench;a.poking=!!h.bench};
  h.work=async instant=>{h.release();h.slot=takeSpot(a);const p=h.slot?h.slot.p:[900,660];a.state='walking';instant?place(a,p):await go(a,[p]);a.state='executing';a.action=a.wantAction};
  return h;
}
// Arena 2.5.0: idle and waiting agents wander. Every 5-15 s (some linger longer) they move to another free spot of
// the floor map, or now and then to a bench, until their state changes; what they are doing (waiting) is unchanged.
async function idleWhile(a,h,changed,benchOdds){
  for(;;){
    const next=clock+between(5000,15000)*(rand()<.2?2:1);
    if(await until(()=>changed()||clock>=next)&&changed())return;
    if(changed())return;
    if(a.meet||R.scatterOn)continue;   // a hand-off or the lights-off run takes the figure for now
    await h.rest(false,benchOdds*.6);
    if(changed())return;
  }
}
async function visitDriven(a,instant){
  if(!instant){const door=randomDoor();await enter(a,door);await go(a,[R.doors[door].entry])}
  const h=holder(a);
  if(a.replay&&RESULT.has(a.want)){await h.work(false);await wait(MIN_WORK)}   // the work it did, then its result
  for(;;){
    const want=a.want;
    if(want==='waiting'){a.action='waiting_model';await h.rest(instant,.5);await idleWhile(a,h,()=>a.want!=='waiting',.5)}
    else if(want==='executing'){await h.work(instant);const since=clock;await until(()=>a.want!=='executing'&&clock-since>=MIN_WORK)}
    else{
      if(ENDED.has(want))finished.add(a.key);
      if(RESULT.has(want)){a.state=want==='pass'?'completed':'failed';await wait(1400);record(want)}
      break;   // finished, canceled, or moved to another location: leave
    }
    instant=false;
  }
  h.release();a.state='walking';const out=randomDoor();await exit(a,out,[R.doors[out].entry]);
}
async function residentDriven(a,instant){
  const h=holder(a);
  if(!instant){const door=randomDoor();await enter(a,door);await go(a,[R.doors[door].entry])}
  for(;;){
    const news=()=>a.results?.length;
    if(news()){const o=a.results.shift();a.state=o==='pass'?'completed':'failed';await wait(1300);record(o);continue}
    const want=a.want;
    if(want==='home'){a.action='idle';await h.rest(instant,.3);await idleWhile(a,h,()=>a.want!=='home'||news(),.3)}
    else if(want==='executing'){await h.work(instant);const since=clock;await until(()=>(a.want!=='executing'&&clock-since>=MIN_WORK)||news())}
    else if(want==='away'){
      h.release();a.state='walking';
      if(instant){a.el.style.display='none';E.delete(a)}else{const o=randomDoor();await exit(a,o,[R.doors[o].entry],{keep:true})}
      away.add(a);await until(()=>a.want!=='away'||news());away.delete(a);
      if(a.want==='gone'&&!news()){a.el.remove();a.gone=true;return}   // a result comes home first, then leaves
      const b=randomDoor();await enter(a,b);await go(a,[R.doors[b].entry]);
    }else{h.release();a.state='walking';const o=randomDoor();await exit(a,o,[R.doors[o].entry]);return}   // retired or removed
    instant=false;
  }
}
function setOffline(on){$('#offline').checked=!!on}

// ------------------------------------------------------------------ paint
function decoFrame(d,t){   // eight-frame burst then rest, seeded per prop
  const a=d.item.animation,span=Math.max(1,a.maximum_interval_ms-a.minimum_interval_ms),cycle=a.minimum_interval_ms+(d.seed*7919%span),local=(t+d.seed*977)%cycle;
  return local<a.frames.length*a.frame_ms?Math.floor(local/a.frame_ms):0;
}
function lampLevel(state,t,seed){return state==='offline'?.1:(.1+.75*(R.light??1))*(still()?1:.94+.04*Math.sin(t/430+seed)+.02*Math.sin(t/97+seed*3))}
function drawDeco(g,d,t,state,x=d.x,y=d.y){
  const n=still()?0:decoFrame(d,t),s=d.size;
  if(!d.lit){g.drawImage(d.frames[n],x,y,s,s);return}
  const level=lampLevel(state,t,d.seed);g.drawImage(d.unlit,x,y,s,s);g.globalAlpha=level;g.drawImage(d.frames[n],x,y,s,s);g.globalAlpha=1;
  const gl=ctx.glow;gl.globalAlpha=level*d.glowMax;gl.drawImage(d.glows[n],d.x,d.y,s,s);gl.globalAlpha=1;
  if(d.cast)castPool(d,level);
}
// Light and shadow shapes are drawn once into a small image and reused every frame, scaled and faded. Until 2.1.3 each
// frame made new radial gradients (every lamp pool, every hole in the darkness, the guard's flashlight) and a new
// canvas filter per bulb: the WebView2 GPU process kept the resources for them and grew until it crashed
// ("GpuProcessExited Crashed" on the heads), while the frames kept a CPU core busy.
const shapes=new Map();
function shape(key,w,h,paint){let c=shapes.get(key);if(!c){c=new OffscreenCanvas(Math.max(1,Math.ceil(w)),Math.max(1,Math.ceil(h)));paint(c.getContext('2d'),c.width,c.height);shapes.set(key,c)}return c}
// A round soft spot: full alpha in the middle, `mid` at 60 %, nothing at the edge (darkness holes, flashlight).
function softSpot(colour,mid,at=.6){return shape(`spot:${colour}:${mid}:${at}`,256,256,(g,w)=>{const gr=g.createRadialGradient(w/2,w/2,0,w/2,w/2,w/2);
  gr.addColorStop(0,`rgba(${colour},1)`);gr.addColorStop(at,`rgba(${colour},${mid})`);gr.addColorStop(1,`rgba(${colour},0)`);g.fillStyle=gr;g.fillRect(0,0,w,w)})}
// The screens' text changes with every result: keep the newest few renderings only.
const texts=new Map();
function screenText(key,w,h,paint){let c=texts.get(key);if(c){texts.delete(key);texts.set(key,c);return c}
  c=new OffscreenCanvas(Math.max(1,Math.ceil(w)),Math.max(1,Math.ceil(h)));paint(c.getContext('2d'));texts.set(key,c);
  while(texts.size>8)texts.delete(texts.keys().next().value);return c}
// A warm pool on the wall and floor below a lamp, gone when the room is dark.
function castPool(d,level){
  const gl=ctx.glow,cx=d.feet[0],cy=d.feet[1]+d.cast*.45,rr=d.cast,a=.34*level;
  // the lamp's own pool at full strength (a circular gradient of radius rr cut to an ellipse rr x .8 rr), faded by level
  const img=shape(`pool:${rr}`,2*rr,1.6*rr,(g,w,h)=>{const gr=g.createRadialGradient(w/2,h/2,0,w/2,h/2,rr);
    gr.addColorStop(0,'rgba(255,206,142,1)');gr.addColorStop(.55,'rgba(255,196,128,.45)');gr.addColorStop(1,'rgba(255,190,120,0)');
    g.fillStyle=gr;g.beginPath();g.ellipse(w/2,h/2,rr,rr*.8,0,0,7);g.fill()});
  gl.globalAlpha=a;gl.drawImage(img,cx-rr,cy-rr*.8,2*rr,1.6*rr);gl.globalAlpha=1;
}
function paintBack(t,state){
  const g=ctx.back,move=!still();g.clearRect(0,0,W,H);g.drawImage(R.plate,0,0);
  for(const d of Object.values(R.doors)){const [x,y]=d.origin;if(d.plate){g.drawImage(d.panels[d.frame_i],x,y);continue}g.drawImage(d.portal,x,y);g.drawImage(d.panels[d.frame_i],x,y);g.drawImage(d.frame,x,y);
    if(d.frame_i>0){g.globalAlpha=d.frame_i/(d.panels.length-1);g.drawImage(d.emission,x,y);g.globalAlpha=1;ctx.glow.globalAlpha=.6*d.frame_i/(d.panels.length-1);ctx.glow.drawImage(d.glow,x,y);ctx.glow.globalAlpha=1}}
  const panelState=state==='offline'?'disconnected':state==='empty'?'off':clock<(R.alertUntil||0)?'alert':state;
  for(const w of R.wall){
    if(w.kind==='panel'){const s=w.p.states[panelState],n=move&&s.frame_count>1?Math.floor((t+w.seed*131)/s.frame_ms)%s.frame_count:s.reduced_motion_frame||0,rc=s.frame_rects_px[n];
      g.drawImage(w.p.body,w.x,w.y,w.w,w.h);g.drawImage(s.em,...rc,w.x,w.y,w.w,w.h);g.save();g.globalCompositeOperation='screen';g.globalAlpha=w.p.item.lighting.maximum_glow_opacity;g.drawImage(s.gl,...rc,w.x,w.y,w.w,w.h);g.restore()}
    else if(w.kind==='screen'){
      g.drawImage(w.img,w.x,w.y,w.w,w.h);const [tx,ty,tw,th]=w.text,c=w.side==='pass'?'#62ffa0':'#ff6464',pad=16;
      // the glowing PASS/FAIL text, drawn once per count (blurred text per frame was redrawn 30 times a second)
      const label=screenText(`${w.side}:${screenCounts[w.side]}:${Math.round(tw)}x${Math.round(th)}`,tw+2*pad,th+2*pad,tg=>{
        tg.fillStyle=c;tg.shadowColor=c;tg.shadowBlur=8;tg.textAlign='center';tg.textBaseline='middle';
        tg.font=`bold ${Math.round(th*.22)}px Consolas,monospace`;tg.fillText(w.side==='pass'?'PASS':'FAIL',pad+tw/2,pad+th*.2);
        tg.font=`bold ${Math.round(th*.5)}px Consolas,monospace`;tg.fillText(String(screenCounts[w.side]),pad+tw/2,pad+th*.64)});
      g.globalAlpha=state==='offline'?.35:1;g.drawImage(label,tx-pad,ty-pad);g.globalAlpha=1;
    }
    else if(w.kind==='signal'){
      const d=w.d,lamp=state==='offline'?null:clock<(R.alertUntil||0)?'red':state==='working'?'green':state==='waiting'?'amber':null;
      g.drawImage(d.unlit,d.x,d.y,d.size,d.size);
      const band={red:[40,94],amber:[98,154],green:[156,212]}[lamp];
      if(band&&(lamp!=='red'||Math.floor(t/320)%2===0)){const [b0,b1]=band,y=d.y+b0*d.scale,h=(b1-b0)*d.scale;
        g.drawImage(d.frames[0],0,b0,256,b1-b0,d.x,y,d.size,h);
        const gl=ctx.glow;gl.globalAlpha=d.glowMax;gl.drawImage(d.glows[0],0,b0-16,256,b1-b0+32,d.x,y-16*d.scale,d.size,h+32*d.scale);gl.globalAlpha=1}
    }
    else drawDeco(g,w.d,t,state);
  }
  if(PLATE)paintBoard(g,state);
  // Light fixtures painted on the plate itself cast their pools like the added lamps.
  if(R.painted?.length){const level=lampLevel(state,t,1);for(const d of R.painted)castPool(d,level)}
  // Tickers: housing, then the message's emission frame and glow frame tinted at runtime. Composed
  // at native size and scaled with smoothing (one-pixel lettering dots vanish otherwise).
  const tk=R.tick,ts=tk.states.get(tk.msg);
  if(ts){
    const n=move&&ts.frame_count>1?Math.floor(t/ts.frame_ms)%ts.frame_count:ts.reduced_motion_frame||0;
    const s=L.tickerScale2,color=tickerColor(tk.msg),bright=tk.msg==='closed'?.55:1,[body,glow]=tickerFrame(tk,ts,n,color,bright,s);
    const top=Math.round(2-(TICKER_TOP[tk.item.id]||0)*s),gl=ctx.glow;gl.globalAlpha=.55*bright;
    for(const d of Object.values(R.doors)){const x=Math.round(d.threshold[0]-body.width/2);g.drawImage(body,x,top);gl.drawImage(glow,x,top)}
    gl.globalAlpha=1;
  }
  const cs=46;
  pile.crates.forEach((c,i)=>{const s=R.crateSpots[i%3],lvl=Math.floor(i/3);g.drawImage(R.crates[c.v],s[0]-cs/2+c.dx,s[1]-cs*.906-lvl*20,cs,cs)});
  pile.debris.forEach((p,i)=>{const s=R.debrisSpots[i%3];g.drawImage(R.debris[p.v],s[0]-22+p.dx,s[1]-30+p.dy-Math.floor(i/3)*4,44,33)});
}
// One ticker frame (housing + tinted lettering, and its tinted glow): composed at native size, scaled once to the size
// it is shown at (with smoothing: one-pixel lettering dots vanish otherwise) and kept as immutable images, drawn 1:1.
// Until 2.1.5 every paint recomposed the frame and drew it scaled to 0.42 with high-quality smoothing, the only such draw
// in the room: the display's GPU process kept new graphics allocations for each one until the display closed. Windows'
// pool tag Vi54 (dxgmms2.sys) grew 400-600 MB an hour per open room (measured here 2026-11-11, within one page load:
// flat with the room's background off, about 2 MB/min with the tickers off against 8-10 with them on), 15 GB on head 8
// and 24 GB on head 7. The last four messages are kept (16 frames each); older ones are released with close().
function tickerFrame(tk,ts,n,color,bright,s){
  const key=`${tk.item.id}|${tk.msg}|${color}|${bright}|${s}`,cache=tk.frameSets||=new Map();
  let set=cache.get(key);if(set){cache.delete(key);cache.set(key,set)}
  else{set=[];cache.set(key,set);while(cache.size>4){const [old,frames]=cache.entries().next().value;cache.delete(old);for(const f of frames)if(f){f[0].close();f[1].close()}}}
  if(set[n])return set[n];
  const [fx,fy,fw,fh]=ts.frames_px[n],c=tk.scratch||=new OffscreenCanvas(512,128),cg=c.getContext('2d'),lit=tk.lit||=new OffscreenCanvas(512,128),lg=lit.getContext('2d');
  const w=Math.max(1,Math.round(512*s)),h=Math.max(1,Math.round(128*s)),out=tk.scaled||=new OffscreenCanvas(w,h);out.width=w;out.height=h;
  const tint=atlas=>{lg.globalCompositeOperation='source-over';lg.clearRect(0,0,512,128);lg.fillStyle=color;lg.fillRect(0,0,512,128);
    lg.globalCompositeOperation='destination-in';lg.drawImage(atlas,fx,fy,fw,fh,0,0,512,128)};
  const shrink=()=>{const og=out.getContext('2d');og.clearRect(0,0,w,h);og.imageSmoothingEnabled=true;og.imageSmoothingQuality='high';og.drawImage(c,0,0,w,h);return out.transferToImageBitmap()};
  cg.globalCompositeOperation='source-over';cg.globalAlpha=1;cg.clearRect(0,0,512,128);cg.drawImage(tk.housing,0,0);
  tint(ts.em);cg.globalAlpha=bright;cg.drawImage(lit,0,0);cg.globalAlpha=1;const body=shrink();
  cg.clearRect(0,0,512,128);tint(ts.gl);cg.drawImage(lit,0,0);const glow=shrink();
  return set[n]=[body,glow];
}
// Darkness over the room (people and props included), with holes where light is: the drone's
// lamp, the guard's flashlight, the scrubber's headlight and any open doorway.
function paintDark(){
  const g=ctx.dark;g.clearRect(0,0,W,H);const k=1-(R.light??1);if(k<.01)return;
  g.globalCompositeOperation='source-over';g.fillStyle=`rgba(3,7,15,${(.8*k).toFixed(3)})`;g.fillRect(0,0,W,H);
  g.globalCompositeOperation='destination-out';
  const hole=softSpot('0,0,0',.55);
  const pool=(x,y,rx,ry,a)=>{g.globalAlpha=a;g.drawImage(hole,x-rx,y-ry,2*rx,2*ry);g.globalAlpha=1};
  if(R.tick.item){const it=R.tick.item,s=L.tickerScale2,top=2-(TICKER_TOP[it.id]||0)*s,[fx0,fy0,fx1,fy1]=it.face_rect_px,a=R.tick.msg==='closed'?.35:.9;
    for(const d of Object.values(R.doors)){const x=d.threshold[0]-256*s;g.fillStyle=`rgba(0,0,0,${a})`;g.fillRect(x+fx0*s,top+fy0*s,(fx1-fx0)*s,(fy1-fy0)*s);pool(d.threshold[0],top+(fy0+fy1)/2*s,150,44,.35*a)}}
  for(const d of Object.values(R.doors))if(d.frame_i>0){const [ax,ay,aw,ah]=d.aperture,o=d.frame_i/(d.panels.length-1);pool(ax+aw/2,ay+ah*.7,aw*.9,ah*.6,.85*o);pool(d.threshold[0],d.threshold[1]+40,aw*.8,40,.6*o)}
  for(const e of E){
    if(e.gone||e.el.style.display==='none')continue;const s=depth(e.y);
    if(e.kind==='drone'){pool(e.x+e.facing*24*s,e.y+6,92*s,42*s,.95);pool(e.x,e.y-46*s,36*s,30*s,.7)}
    else if(e.kind==='scrubber')pool(e.x+e.facing*70*s,e.y,70*s,30*s,.8);
    else if(e.kind==='guard'){   // a flashlight cone from the hand, the way the guard is looking
      const [ux,uy]=e.view==='front'?[0,1]:e.view==='rear'?[0,-1]:[e.facing,0],hx=e.x+ux*10*s+(uy?14*s:0),hy=e.y-44*s+uy*18*s;
      const reach=(uy?170:250)*s,ex=hx+ux*reach,ey=e.y+uy*reach*.55,R2=reach+20*s;
      g.save();g.beginPath();g.moveTo(hx,hy);
      if(uy){g.lineTo(ex-70*s,ey);g.lineTo(ex+70*s,ey)}else{g.lineTo(ex,e.y-40*s);g.lineTo(ex,e.y+70*s)}
      g.closePath();g.clip();g.globalAlpha=.95;g.drawImage(softSpot('0,0,0',(.6/.95).toFixed(3),.7),hx-R2,hy-R2,2*R2,2*R2);g.restore();
      pool(uy?ex:e.x+ux*200*s,uy?ey:e.y+20*s,85*s,38*s,.9);pool(e.x,e.y-40*s,30*s,44*s,.45);
    }
  }
  g.globalCompositeOperation='source-over';
}
function paintProps(t,state){
  for(const s of R.liveScreens||[]){const n=still()?0:Math.floor((t+s.seed*97)/s.ms)%s.frames.length;if(n!==s.shown){s.shown=n;s.g.clearRect(0,0,s.el.width,s.el.height);s.g.drawImage(s.frames[n],0,0)}}
  for(const f of R.floor){const n=still()?0:decoFrame(f,t),key=n*10+(f.lit?Math.round(lampLevel(state,t,f.seed)*8):0);
    if(key!==f.shown){f.shown=key;f.g.clearRect(0,0,f.el.width,f.el.height);drawDeco(f.g,f,t,state,0,0)}
    else if(f.lit){const gl=ctx.glow,n2=still()?0:decoFrame(f,t);gl.globalAlpha=lampLevel(state,t,f.seed)*f.glowMax;gl.drawImage(f.glows[n2],f.x,f.y,f.size,f.size);gl.globalAlpha=1}}
}
function paintFront(state){
  const g=ctx.glow,f=ctx.front,B=L.bulbs,b=R.bulb,s=b.scale;
  for(const side of ['pass','fail']){
    const st=bulbs[side],color=$(`#${side}-color`).value,on=!st.celebrate||Math.floor((clock-st.celebrate)/250)%2===0;
    // the bulb body at the room's brightness, one image per brightness step (21 at most) instead of a filter per frame
    const bright=(Math.round((1-.72*(1-(R.light??1)))*20)/20).toFixed(2),body=shape(`bulb:${bright}`,256,256,g2=>{g2.filter=`brightness(${bright})`;g2.drawImage(b.body,0,0,256,256)});
    R.bulbXs[side].forEach((x,i)=>{const dx=x-128*s,dy=B.feet-236*s,sz=256*s;f.drawImage(body,dx,dy,sz,sz);
      if(i<st.lit&&on){f.drawImage(tint(b.em,color,'e'),dx,dy,sz,sz);g.globalAlpha=b.item.lighting.maximum_glow_opacity;g.drawImage(tint(b.gl,color,'g'),dx,dy,sz,sz);g.globalAlpha=1}});
  }
  if($('#overlay').checked)overlay(f);
}
function poly(g,pts,stroke,fill){g.beginPath();pts.forEach(([x,y],i)=>g[i?'lineTo':'moveTo'](x*W,y*H));g.closePath();if(fill){g.fillStyle=fill;g.fill()}g.strokeStyle=stroke;g.stroke()}
function overlay(g){
  const s=R.scene;g.save();g.lineWidth=2;
  if(s){poly(g,s.octagon,'#f0c068');poly(g,s.zones.standing,'#55c4cc','#55c4cc18');poly(g,s.zones.work,'#7f9cff','#7f9cff14')}
  g.strokeStyle='#ff8ad8';g.setLineDash([6,5]);g.beginPath();g.moveTo(80,L.staffLaneY);g.lineTo(1456,L.staffLaneY);g.moveTo(L.aisle,460);g.lineTo(L.aisle,L.inspectorY);g.stroke();g.setLineDash([]);
  g.fillStyle='#ff6b6b44';g.strokeStyle='#ff6b6b';for(const [x,y,w,h] of R.obstacles){g.fillRect(x,y,w,h);g.strokeRect(x,y,w,h)}
  const dot=(p,c,r=5)=>{g.fillStyle=c;g.beginPath();g.arc(p[0],p[1],r,0,7);g.fill()};
  for(const d of Object.values(R.doors)){dot(d.threshold,'#fff');dot(d.approach,'#ffd36e');dot(d.entry,'#9be37f')}
  for(const sl of R.stand)dot(sl.p,'#55c4cc');for(const sl of R.work)dot(sl.p,'#7f9cff');for(const b of R.benches)dot(b.stand,'#ff9f43',7);
  for(const p of [...R.crateSpots,...R.debrisSpots,...(R.dock?[R.dock]:[]),...Object.values(R.screenStands||{})])dot(p,'#e6e6e6',4);
  g.restore();
}
// DOM characters: feet at left/top, depth from y only, sorted front-to-back by feet.
// Arena 2.5.0, drawing overrides. An agent's logical position and path keep running (live sync and timing are
// untouched); only its figure is taken over, then walks back to where the agent really is:
//  - lights off (the operator's choice): a "!", a hop, then running around the room frantically, new direction every
//    half second or so, until the light is back; then back to their places. The robot cat is unimpressed.
//  - a hand-off (Capstone's handoff.sent / handoff.received): the sender walks to the receiver, they face each other
//    and talk ("…" back and forth) until it is received ("Received"), then each goes back.
const SCATTER=PLATE?{x:[150,1385],y:[585-BAY_BACK-20,935]}:{x:[220,1310],y:[320,905]};
// A dash of 120-380 px from where the figure is, in a random direction (short dashes from everywhere keep the room
// spread out; far random targets retaken every second made everyone hover round the middle).
function franticPoint(from){
  for(let i=0;i<40;i++){const a=rand()*Math.PI*2,r=between(120,380),p=[from[0]+Math.cos(a)*r,from[1]+Math.sin(a)*r*.6];
    if(p[0]<SCATTER.x[0]||p[0]>SCATTER.x[1]||p[1]<SCATTER.y[0]||p[1]>SCATTER.y[1])continue;
    if(!R.obstacles.some(([x,y,w,h])=>p[0]>x-20&&p[0]<x+w+20&&p[1]>y&&p[1]<y+h+20))return p}
  return R.pool?.length?[...pick(R.pool).p]:[W/2,(SCATTER.y[0]+SCATTER.y[1])/2];
}
function visual(e){
  const plain={x:e.x,y:e.y,walking:e.path.length>0,dir:e.path.length?[e.path[0][0]-e.x,e.path[0][1]-e.y]:[0,0]};
  const inRoom=e.kind==='agent'&&!e.portal&&!e.waiting,mode=R.scatterOn&&inRoom?'frantic':e.meet&&inRoom?'meet':null;
  if(!e.ov&&!mode)return plain;
  const o=e.ov||=(e.ov={x:e.x,y:e.y,t:clock,target:null,next:0});
  const dt=Math.min(250,Math.max(0,clock-o.t)),hop=performance.now()<(e.jumpUntil||0);o.t=clock;
  let target,speed;
  if(mode==='frantic'){
    if(!o.target||o.mode!=='frantic'||clock>=o.next||Math.hypot(o.target[0]-o.x,o.target[1]-o.y)<8){o.target=franticPoint([o.x,o.y]);o.next=clock+between(900,1800)}
    target=o.target;speed=e.speed*2.6;
  }else if(mode==='meet'){target=e.meet.spot;speed=e.speed}
  else{target=[e.x,e.y];speed=e.speed*1.3}
  o.mode=mode;
  const dx=target[0]-o.x,dy=target[1]-o.y,dist=Math.hypot(dx,dy),move=hop?0:speed*dt/1000*depth(o.y);
  if(dist<=move||dist<1){o.x=target[0];o.y=target[1]}else{o.x+=dx/dist*move;o.y+=dy/dist*move}
  const arrived=Math.hypot(target[0]-o.x,target[1]-o.y)<1;
  if(!mode&&arrived){e.ov=null;return plain}
  if(mode==='meet'&&arrived)e.meet.here=true;
  return{x:o.x,y:o.y,walking:!hop&&!arrived,dir:[dx,dy],fast:mode==='frantic'};
}
// Hand-offs in the room. A meeting holds both figures until it is received (or 20 s pass), then lets them go.
const meets=new Map(),MEET_MAX=20000,MEET_RECEIVED_MS=1800,MEET_TURN_MS=950;
const findActor=agentKey=>{let best=null;for(const a of actors.values())if(a.agentKey===agentKey&&E.has(a)&&!a.portal&&!a.waiting){if(a.key===agentKey)return a;best||=a}return best};
function handoff(h){
  let m=meets.get(h.key);
  if(!m){
    const s=findActor(h.sender);if(!s||s.meet)return;                     // the sender is not in this room (or busy)
    const r=findActor(h.receiver),rv=r&&!r.meet?(r.ov?[r.ov.x,r.ov.y]:[r.x,r.y]):null;
    let spot,face;
    if(rv){const side=s.x<rv[0]?-1:1;spot=[Math.min(SCATTER.x[1],Math.max(SCATTER.x[0],rv[0]+side*58*SC.people)),rv[1]+4];face=-side;
      r.meet={key:h.key,role:'receiver',spot:rv,face:side,say:'',talking:false};}
    else{const d=Object.values(R.doors).sort((p,q)=>Math.hypot(p.entry[0]-s.x,p.entry[1]-s.y)-Math.hypot(q.entry[0]-s.x,q.entry[1]-s.y))[0];
      spot=[...d.entry];face=d.entry[0]<W/2?-1:1}
    s.meet={key:h.key,role:'sender',spot,face,say:'…',talking:true};
    m={key:h.key,s,r:rv?r:null,t0:clock,turn:clock,received:false,doneAt:0};meets.set(h.key,m);
  }
  if(h.status==='received')m.received=true;
}
function meetTick(){
  for(const [k,m] of meets){
    const gone=!E.has(m.s)||(m.r&&!E.has(m.r));
    if(!gone&&m.s.meet?.here){
      if(m.received&&!m.doneAt){m.doneAt=clock+MEET_RECEIVED_MS;m.s.meet.say='';m.s.meet.talking=false;if(m.r)Object.assign(m.r.meet,{say:'Received',talking:true});else m.s.meet.say='Received'}
      else if(!m.received&&m.r&&clock-m.turn>=MEET_TURN_MS){m.turn=clock;const a=m.s.meet.talking;   // back and forth
        Object.assign(m.s.meet,{talking:!a,say:a?'':'…'});Object.assign(m.r.meet,{talking:a,say:a?'…':''})}
    }
    if(gone||(m.doneAt&&clock>=m.doneAt)||clock-m.t0>MEET_MAX){if(m.s.meet?.key===k)m.s.meet=null;if(m.r?.meet?.key===k)m.r.meet=null;meets.delete(k)}
  }
}
const svgFor=(e,key)=>e.body&&e.body!=='classic'?R.bodySvg.get(e.body+'|'+key):R.agentSvg.get(key);
function viewKey(e,v,t){
  // Walking: four steps in four directions (both rigs). Standing: the classic rig keeps its original posed SVG
  // ('base'); the new bodies stand in their idle frame, facing the bench, the room, or the viewer.
  // Arena 2.5.0, the artist's frames on the new bodies: run-frantic in the dark, startled on the hop, talk and listen in
  // a hand-off, tap at a bench, and the idle gestures (look-aside, fidget, listen, blink; a blink every few seconds).
  const body=e.body&&e.body!=='classic';
  if(body&&e.startled&&!still()){const dx=v.dir[0];return v.walking&&Math.abs(dx)>Math.abs(v.dir[1])?`${dx<0?'left':'right'}:startled-01`:'front:startled-01'}
  if(v.walking&&!still()){const [dx,dy]=v.dir,w=dy>Math.abs(dx)*1.1?'front':-dy>Math.abs(dx)*1.1?'back':dx<0?'left':'right';
    if(body&&v.fast)return`${w}:${frameOf(w,'run-frantic',t+e.speed*37)}`;
    return`${w}:${Math.floor((t+e.speed*37)/(v.fast?80:150))%4}`}
  if(!body)return'base';
  if(still())return'front:idle';
  if(e.meet?.here){const s=e.meet.face<0?'left':'right';return`${s}:${frameOf(s,e.meet.talking?'talk':'listen',t)}`}
  if(e.poking&&e.bench){const s=e.bench.facing<0?'left':'right';return`${s}:${frameOf(s,'tap',t+e.speed*37)}`}
  if(e.idle){const p=t-(e.idleAt??t);
    if(e.idle==='idle-look')return`front:look-aside-0${p<(e.idleUntil-e.idleAt)/2?1:2}`;
    if(e.idle==='idle-settle'||e.idle==='idle-ankles')return p<700?`front:${frameOf('front','fidget',p)}`:'front:idle';
    if(e.idle==='idle-watch')return`front:${frameOf('front','listen',t)}`;
    if(e.idle==='idle-blink')return p<160||(p>420&&p<580)?'front:blink-01':'front:idle'}
  return(t+e.speed*997)%4300<130?'front:blink-01':'front:idle';
}
// The prop over a figure's head (characters-v2/props): the startle "!" growing in, the typing "…" bubble, the received check.
function propOver(e,t,jumping){
  if(jumping)return['startle-mark',Math.min(2,Math.floor((performance.now()-e.jumpAt)/PROPS['startle-mark'].ms))];
  if(!e.meet?.here||!e.meet.say)return null;
  return e.meet.say==='…'?['speech-bubble-typing',Math.floor(t/PROPS['speech-bubble-typing'].ms)%3]:['speech-bubble-received',0];
}
function render(e,t){
  const v=e.kind==='agent'||e.kind==='cat'?visual(e):null,ex=v?v.x:e.x,ey=v?v.y:e.y;
  const d=depth(ey);e.el.style.left=ex+'px';e.el.style.top=ey+'px';e.el.style.zIndex=Math.round(ey);
  const walking=v?v.walking:e.path.length>0;
  if(e.kind==='cat'){
    // Arena 2.5.0: stands a moment, sits, and flicks its tail now and then.
    const p=t-(e.sitAt??t),sit=p<400?'idle':(p-400)%4200>3000?frameOf(e.sitFace||'front','tail-flick',p,0,true):'sit-01';
    const key=v.walking&&!still()?viewKey(e,v,t):(e.sitFace||'front')+':'+(still()?'idle':sit);
    if(key!==e.svgKey){const cur=e.el.querySelector('svg.avatar'),next=(e.svgCache||={})[key]||=R.bodySvg.get('h-robot-cat|'+key)?.cloneNode(true);if(next&&cur!==next)cur.replaceWith(next);e.svgKey=key}
    e.el.style.setProperty('--depth',d*SC.people);e.el.style.setProperty('--facing',1);clip(e,d*SC.people,[65,111,130,150]);
    return;
  }
  if(e.kind==='agent'){
    if(!walking&&e.state==='waiting'&&!e.poking&&!still()){
      if(e.idle&&clock>=e.idleUntil)e.idle=null;
      if(!e.idle&&clock>=e.nextIdle){e.idle=pick(IDLES);e.idleAt=clock;e.idleUntil=clock+between(2000,3200);e.nextIdle=e.idleUntil+between(2500,8000)}
    }else e.idle=null;
    const jumping=performance.now()>=(e.jumpAt||0)&&performance.now()<(e.jumpUntil||0);e.startled=jumping;
    const key=viewKey(e,v,t);
    if(key!==e.svgKey){const cur=e.el.querySelector('svg.avatar'),next=key==='base'?(e.baseSvg||=cur):((e.svgCache||={})[key]||=svgFor(e,key)?.cloneNode(true));if(next&&cur!==next)cur.replaceWith(next);e.svgKey=key}
    const cls=['actor',walking?'walking':e.state,'act-'+(walking||!e.action?'none':e.action)];if(key!=='base')cls.push('framed');
    if(e.poking)cls.push('poking');if(e.idle)cls.push(e.idle);if(jumping)cls.push('jump');
    // Arena 2.5.0: the startle "!" (lights off), the hand-off bubbles ("…" back and forth, then the received check),
    // the artist's fixed-palette props drawn at twice their pixel size over the head.
    const prop=propOver(e,t,jumping);if(prop)cls.push(jumping?'bang':'say');if(e.meet?.here&&!jumping)cls.push('meeting');if(e.meet?.here&&e.meet.talking&&!jumping)cls.push('talking');
    const sp=e.el.querySelector('.speech'),src=prop?R.propSrc[prop[0]][prop[1]]:'';
    if(sp&&sp.dataset.prop!==src){sp.dataset.prop=src;sp.textContent='';if(prop){const img=new Image(),[w,h]=PROPS[prop[0]].size;img.alt='';img.width=w*2;img.height=h*2;img.src=src;sp.append(img)}}
    if(!walking&&e.poking&&e.bench?.facing===-1)cls.push('turn-left');
    const name=cls.join(' ');if(e.el.className!==name)e.el.className=name;
    e.el.style.setProperty('--depth',d*SC.people);e.el.style.setProperty('--facing',key==='base'?(e.meet?.here?e.meet.face:e.facing):1);
    const label=e.meet?.here?(e.meet.role==='sender'?'Handing off':'Receiving'):e.waiting?'At the door':walking?'Walking':e.label&&(e.state==='executing'||e.state==='waiting')?e.label:e.state==='executing'?LABEL[e.action]||'Working':e.state==='completed'?'Done ✓':e.state==='failed'?'Failed':'Waiting · model';
    const st=e.el.querySelector('.actor-state');if(st.textContent!==label)st.textContent=label;
    clip(e,d*SC.people,[65,111,130,150]);
  }else if(e.kind==='bucket'){
    e.el.style.transform=`translate(-80px,-124px) scale(${d*SC.people})`;e.el.classList.toggle('jump',performance.now()>=(e.jumpAt||0)&&performance.now()<(e.jumpUntil||0));clip(e,d*SC.people,[80,124,160,144]);
  }else if(CHAR[e.kind]){
    // Front view coming toward the camera, rear going away (or facing something behind), and the
    // angled view sideways (mirrored for left). Staff without an angled view use their front art.
    const views=R.chars.actors[CHAR[e.kind]].views;
    if(walking){const [tx,ty]=e.path[0],dx=tx-e.x,dy=ty-e.y;if(Math.abs(dx)>.5||Math.abs(dy)>.5)e.view=dy>Math.abs(dx)*1.2?'front':-dy>Math.abs(dx)*1.2?'rear':views.angle?'angle':'front'}
    const view=e.faceAway?'rear':views[e.view]?e.view:views.angle?'angle':'front',set=views[view].animations;
    const want=ACT[e.anim]||(e.anim==='walk'&&e.kind==='scrubber'?'drive':walking&&!set[e.anim]?'walk':e.anim);
    const anim=set[want]||set[walking?'walk':'idle']||set.walk||set.drive||Object.values(set)[0];
    const hold=!walking&&(want==='walk'||want==='drive'||want==='carry'),file=still()||hold?anim.frames[0]:anim.frames[Math.floor((t+(e.x|0)*3)/anim.frame_ms)%anim.frames.length];
    const src=new URL(file,new URL('characters-v1/',ASSETS)).href;if(e.img.src!==src)e.img.src=src;
    e.el.style.transform=`translate(-80px,-124px) scale(${d*SC.people})`;e.el.classList.toggle('flip',e.facing<0&&(view==='angle'||(view==='front'&&!views.angle)));
    e.el.classList.toggle('jump',performance.now()>=(e.jumpAt||0)&&performance.now()<(e.jumpUntil||0));clip(e,d*SC.people,[80,124,160,144]);
  }else{
    const set=R.shared.staff[e.kind].animations,key=e.kind==='drone'?e.anim:walking?(e.anim==='carry'?'carry':'walk'):(e.anim==='walk'?'idle':e.anim);
    const anim=set[key]||set.hover||set.idle,holding=!walking&&(key==='carry'||key==='walk');
    const file=still()||holding?anim.frames[0]:anim.frames[Math.floor((t+(e.kind==='drone'?0:e.x))/anim.frame_ms)%anim.frames.length];
    const src=new URL(file,new URL('scenes/_shared/',ASSETS)).href;if(e.img.src!==src)e.img.src=src;
    e.el.style.transform=`translate(-48px,-104px) scale(${d*SC.people})`;e.el.classList.toggle('flip',e.facing<0);
    e.el.classList.toggle('jump',performance.now()>=(e.jumpAt||0)&&performance.now()<(e.jumpUntil||0));clip(e,d*SC.people,[48,104,96,112]);
  }
}
// Only while inside the doorway is a character clipped to the opening, so it emerges from the portal.
function clip(e,d,[ox,oy,bw,bh]){
  if(!e.portal){if(e.clipped){e.el.style.clipPath='';e.clipped=false}return}
  const [ax,ay,aw,ah]=e.portal.aperture,l=ox+(ax-e.x)/d,t=oy+(ay-e.y)/d,r=ox+(ax+aw-e.x)/d,b=oy+(ay+ah-e.y)/d;
  e.el.style.clipPath=`inset(${t}px ${bw-r}px ${bh-b}px ${l}px)`;e.clipped=true;
}

// ------------------------------------------------------------------ HUD and loop
function hud(state){
  const a=[...E].filter(e=>e.kind==='agent'),staffNow=[...E].filter(e=>e.kind==='mechanic'||e.kind==='inspector').length;
  const room=['Room',{empty:'Out to lunch',waiting:'Waiting',working:'Working',offline:'Be right back (offline)'}[state]];
  const rows=LAB?[room,['Agents home',a.length],['Idle',a.filter(x=>x.state==='waiting').length],['Working here',a.filter(x=>x.state==='executing').length],['Away at heads',away.size],['Passed',counts.pass],['Failed',counts.fail],['On screens',`${screenCounts.pass} / ${screenCounts.fail}`]]
    :[room,['Agents here',a.length],['Waiting',a.filter(x=>x.state==='waiting').length],
    ['Working',a.filter(x=>x.state==='executing').length],['Passed',counts.pass],['Failed',counts.fail],['Crates',pile.crates.length+' / 10'],['Debris',pile.debris.length],['Staff on floor',staffNow]];
  const html=rows.map(([k,v])=>`<dt>${k}</dt><dd>${v}</dd>`).join('');if($('#hud').innerHTML!==html)$('#hud').innerHTML=html;
}
let nextArrival=1200;
function autoSim(){
  if(DRIVEN||!$('#auto').checked||clock<nextArrival)return;
  if(LAB){const home=[...E].filter(e=>e.kind==='agent').length+away.size;if(home<9)residentLife(spawnAgent(),true).catch(console.error);nextArrival=clock+between(9000,16000);return}
  if([...E].filter(e=>e.kind==='agent').length<7)agentLife(spawnAgent()).catch(console.error);
  nextArrival=clock+between(3000,8000);
}
let last=performance.now(),owed=0;
function busy(){
  for(const e of E)if(e.path&&e.path.length)return true;
  for(const d of Object.values(R.doors||{}))if(d.panels&&d.frame_i!==(d.holds?.size?d.panels.length-1:0))return true;
  return false;
}
function loop(now){
  let real=Math.min(100,now-last);last=now;
  // Embedded and live rooms paint at about 15 fps while someone walks or a door moves, and 4 fps when the room is
  // idle (lamps flicker, screens glow), leaving the display's CPU to the inference work it is showing. Until 2.1.3 they
  // painted at 20-30 fps always: about half a CPU core on a head, all day. The room's clock keeps real time either way.
  if(EMBED||LIVE){owed+=real;const moving=busy();if(owed<(moving?66:250)){requestAnimationFrame(loop);return}real=Math.min(moving?150:300,owed);owed=0}
  try{if(!document.hidden&&R.ready)frameStep(real)}catch(e){$('#status').textContent='Error: '+e.message;console.error(e)}
  requestAnimationFrame(loop);
}
function frameStep(real){
  // Disconnected: nothing moves and no new work is shown; the room holds its last known state.
  const cut=offline();document.body.classList.toggle('disconnected',cut);
  const dt=cut?0:real*Number($('#speed').value);clock+=dt;
  if(!cut){autoSim();for(const d of Object.values(R.doors))doorTick(d);for(const e of E)step(e,dt);settle();lightStep(dt);meetTick()}
  const state=roomState(),msg=tickerMessage(state);
  if(msg!==R.tick.want){R.tick.want=msg;loadTick(msg).catch(console.error)}
  ctx.glow.clearRect(0,0,W,H);ctx.front.clearRect(0,0,W,H);
  document.querySelector('#actors .light-switch')?.classList.toggle('on',(R.light??1)>.5);
  paintBack(clock,state);paintProps(clock,state);for(const e of E)render(e,clock);paintDark();paintFront(state);hud(state);
}
// What the sign says: the room's real state. Dark and empty reads CLOSED.
function tickerMessage(state){return state==='offline'?'be-right-back':state==='working'?'working':state==='waiting'?'thinking':(R.light??1)<.5?'closed':hash(seedText)%2?'out-for-lunch':'coffee-break'}
const TICKER_COLORS={led:{working:'#7ee6b1',thinking:'#f2c46c','out-for-lunch':'#a9d6e2','coffee-break':'#a9d6e2',closed:'#6d8796','be-right-back':'#ff8f72'},
  incandescent:{working:'#ffe2a8',thinking:'#ffcf7a','out-for-lunch':'#ffd9a0','coffee-break':'#ffd9a0',closed:'#b88a58','be-right-back':'#ff9a6a'}};
function tickerColor(msg){return(TICKER_COLORS[R.tick.item.kind]||TICKER_COLORS.led)[msg]||'#ffffff'}
async function loadTick(msg){
  if(!R.tick.item)return;   // fixed-layout rooms: the board says it, no ticker
  const s=R.tick.item.states[msg];if(!s)return;
  if(!R.tick.states.has(msg)){const [em,gl]=await Promise.all([bitmap(at('ticker-signs-v2/',s.emission_atlas)),bitmap(at('ticker-signs-v2/',s.glow_atlas))]);R.tick.states.set(msg,{...s,em,gl})}
  if(R.tick.want===msg||!R.tick.msg)R.tick.msg=msg;
}
function fit(){const v=$('#viewport'),k=Math.min(v.clientWidth/W,v.clientHeight/H);const s=$('#stage');s.style.transform=`scale(${k})`;s.style.left=(v.clientWidth-W*k)/2+'px';s.style.top=(v.clientHeight-H*k)/2+'px'}

async function init(){
  await loadKits();await dress();R.light=0;R.peopleAt=-1e9;R.tick.want='closed';await loadTick('closed');R.ready=true;fit();addEventListener('resize',fit);
  if(LIVE)$('#speed').value='1';
  {const dr=spawnStaff('drone');staff.drone=dr;place(dr,R.dock)}
  droneLife().catch(console.error);crewLife().catch(console.error);catLife().catch(console.error);
  // Easter egg: the light switch beside the left door (or L). Everyone in the room jumps.
  {const sw=document.createElement('button');sw.className='light-switch';sw.title='Lights';sw.setAttribute('aria-label','Light switch');
    Object.assign(sw.style,{left:L.lightSwitch[0]+'px',top:L.lightSwitch[1]+'px'});sw.onclick=e=>{e.stopPropagation();lightSwitch()};$('#actors').append(sw)}
  addEventListener('keydown',e=>{if((e.key==='l'||e.key==='L')&&!e.target.closest?.('input,select,textarea'))lightSwitch()});
  $('#lights')?.addEventListener('click',lightSwitch);
  if(LAB&&!LIVE)for(let i=0;i<7&&i<R.stand.length;i++){const a=spawnAgent();place(a,R.stand[i].p);residentLife(a,false).catch(console.error)}   // the orchestrator's home crowd
  $('#arrive').onclick=()=>(LAB?residentLife(spawnAgent(),true):agentLife(spawnAgent())).catch(console.error);
  $('#room').replaceChildren(...Object.entries(ROOMS).map(([id,r])=>new Option(`${r.title} · ${r.type==='lab'?'orchestrator lab':'inference head'}`,id)));
  $('#room').value=ROOM_ID;$('#room').onchange=()=>{location.search='?room='+$('#room').value};
  $('#room-name').textContent=`${L.title} · ${LAB?'orchestrator lab':'inference head'}`;
  const force=(value,filter)=>{const a=[...E].find(e=>e.kind==='agent'&&!e.force&&filter(e));if(a)a.force=value;$('#status').textContent=a?`${a.name}: ${value}`:'Nobody is working right now'};
  $('#succeed').onclick=()=>force('pass',a=>a.state==='executing');$('#fail').onclick=()=>force('fail',a=>a.state==='executing');
  $('#empty').onclick=()=>{for(const e of E)if(e.kind==='agent')e.force='leave'};
  $('#bulb-design').onchange=()=>loadBulb($('#bulb-design').value);
  const redress=async()=>{R.ready=false;await dress();if(staff.drone&&!staff.drone.busy&&R.dock)place(staff.drone,R.dock);R.ready=true};
  $('#reshuffle').onclick=()=>{shuffle++;return redress()};
  if(bus)bus.onmessage=({data:m})=>{
    if(m.type==='dispatch'&&!LAB&&m.head===HEAD)agentLife(spawnAgent({name:m.name,color:m.color,body:m.body,palette:m.palette}),m).catch(console.error);
    else if(m.type==='return'&&LAB&&m.app===APP)returns.set(m.id,m);
    else if(m.type==='speed')$('#speed').value=m.value;
    else if(m.type==='offline')$('#offline').checked=m.value;
    else if(m.type==='reshuffle')$('#reshuffle').click();
  };
  // Review hook: step the simulation when the page gets no animation frames (hidden preview pane).
  window.composer={R,E,queue,pile,bulbs,staff,counts,away,screenCounts,L,returns,HEAD,APP,lightSwitch,runBroom,runMop,runGuard,runScrubber,get clock(){return clock},async advance(ms){let n=0;for(let t=0;t<ms;t+=50){frameStep(50/Number($('#speed').value));for(let k=0;k<6;k++)await null;if(++n%10===0)await new Promise(r=>{const c=new MessageChannel();c.port1.onmessage=r;c.port2.postMessage(0)})}},   // let cross-frame messages arrive
    async reshuffle(){shuffle++;R.ready=false;await dress();if(staff.drone&&!staff.drone.busy&&R.dock)place(staff.drone,R.dock);R.ready=true;frameStep(1)},
    zoom(x,y,k){const v=$('#viewport'),s=$('#stage');s.style.transform=`scale(${k})`;s.style.left=(v.clientWidth/2-x*k)+'px';s.style.top=(v.clientHeight/2-y*k)+'px'},fit};
  $('#status').textContent=`Composed on the clean plate · ${R.wall.length} wall items · ${R.floor.length} floor props · ${R.benches.length} benches`;
  if(LIVE){
    // The viewer's handle. A new layout keeps everyone where they are; the next day re-dresses too.
    window.composer.live={sync,setOffline,lightSwitch,onSelect:null,get overflow(){return overflow},actors,
      reshuffle(n){shuffle=Number(n)||shuffle+1;return redress()}};
    setInterval(()=>{if(R.ready&&localDay()!==R.day)redress().catch(console.error)},60000);
    // Keys pressed while the room has focus still reach the viewer (room arrows, full screen).
    addEventListener('keydown',e=>{if(['ArrowLeft','ArrowRight','F11','Escape'].includes(e.key)){e.preventDefault();parent.postMessage({type:'arena-key',key:e.key},location.origin)}});
    parent.postMessage({type:'arena-scene-ready',room:ROOM_ID},location.origin);
  }
  requestAnimationFrame(loop);
}
init().catch(e=>{$('#status').textContent='Failed: '+e.message;console.error(e)});
