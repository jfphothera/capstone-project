// Asset review only. This page never contacts the event service or supplies live counts.
import {TickerSigns} from './ticker_signs.mjs';
const tickers=new TickerSigns();
const $=s=>document.querySelector(s),canvas=$('#canvas'),ctx=canvas.getContext('2d');
const ROOT=new URL('../assets/scenes/',import.meta.url),sharedURL=new URL('_shared/',ROOT);
const reduced=matchMedia('(prefers-reduced-motion: reduce)');
let manifest=null,shared=null,assets=new Map(),spriteAssets=new Map(),generation=0,start=performance.now(),loaded=false;
ctx.imageSmoothingEnabled=false;
async function json(url){const r=await fetch(url);if(!r.ok)throw new Error(`${r.status}: ${url}`);return r.json()}
async function bitmap(url,bounds){const r=await fetch(url);if(!r.ok)throw new Error(`${r.status}: ${url}`);const blob=await r.blob();return bounds?createImageBitmap(blob,...bounds):createImageBitmap(blob)}
function release(map){for(const value of map.values())value.bitmap.close();map.clear()}
function coordinates(p){return[p[0]*1536,p[1]*1024]}
function draw(file,opacity=1,rotation=null){const asset=assets.get(file)||spriteAssets.get(file);if(!asset)return;ctx.save();ctx.globalAlpha=opacity;
  if(rotation){const [x,y]=coordinates(rotation.pivot);ctx.translate(x,y);ctx.rotate(rotation.angle);ctx.translate(-x,-y)}
  ctx.drawImage(asset.bitmap,asset.x,asset.y);ctx.restore();}
function stamp(file,point,anchor){const asset=spriteAssets.get(file);if(!asset)return;const[x,y]=coordinates(point);ctx.drawImage(asset.bitmap,x-asset.bitmap.width*anchor[0],y-asset.bitmap.height*anchor[1])}
function interpolate(points,t){const lengths=points.slice(1).map((p,i)=>Math.hypot((p[0]-points[i][0])*1536,(p[1]-points[i][1])*1024));let remaining=Math.max(0,Math.min(1,t))*lengths.reduce((a,b)=>a+b,0);
  for(let i=0;i<lengths.length;i++){if(remaining<=lengths[i]){const k=lengths[i]?remaining/lengths[i]:0;return points[i].map((v,j)=>v+(points[i+1][j]-v)*k)}remaining-=lengths[i]}return points.at(-1)}
async function loadScene(name){
  for(const id of ['scene','sign','ticker-style'])$('#'+id).disabled=true;
  const mine=++generation;loaded=false;$('#status').textContent='Loading layers…';
  const folder=new URL(name+'/',ROOT),m=await json(new URL('scene.json',folder));
  const files=[m.base,m.layout_overlay,...Object.keys(m.layer_bounds_px)];
  const next=new Map();
  try{await Promise.all(files.map(async file=>{const bounds=m.layer_bounds_px[file];const b=await bitmap(new URL(file,folder),bounds);next.set(file,{bitmap:b,x:bounds?.[0]||0,y:bounds?.[1]||0})}));}
  catch(e){release(next);throw e}
  if(mine!==generation){release(next);return}
  release(assets);assets=next;manifest=m;start=performance.now();
  const profile=tickers.profiles.find(p=>p.scene===name);$('#ticker-style').value=profile.style;
  if(!await tickers.load(profile.style,$('#sign').value)||mine!==generation)return;loaded=true;
  for(const id of ['scene','sign','ticker-style'])$('#'+id).disabled=false;
  $('#room-title').textContent=m.name.replaceAll('-',' ').replace(/\b\w/g,c=>c.toUpperCase());$('#room-type').textContent=m.type==='lab'?'ORCHESTRATOR / LAB':'INFERENCE HEAD / SHOP FLOOR';
  $('#geometry').textContent=m.geometry_id;$('#manifest-link').href=new URL('scene.json',folder);$('#debug-link').href=new URL(m.layout_debug,folder);
  const old=$('#lane').value;$('#lane').replaceChildren(...Object.keys(m.staff_lanes).map(name=>{const o=document.createElement('option');o.value=name;o.textContent=name.replaceAll('-',' ');return o}));
  if(Object.hasOwn(m.staff_lanes,old))$('#lane').value=old;
  $('#bulb').disabled=!m.panel;$('#flash').disabled=!m.panel;$('#status').textContent=`${files.length} aligned layers ready`;paint(performance.now());
}
function paint(now){if(!loaded||!manifest)return;ctx.clearRect(0,0,1536,1024);const moving=$('#animate').checked&&!reduced.matches&&!document.hidden;
  const elapsed=now-start,phase=moving?(elapsed%14000)/14000:Number($('#progress').value)/1000;
  draw(manifest.base);
  // The original sign bay partly underlaps lab screen bezels. Draw the new
  // casing first; its centered primary text is wholly inside the clear gap.
  tickers.draw(ctx,[572,266],elapsed,{animate:moving,brightness:1});
  for(const[key,door]of Object.entries(manifest.doors)){
    let frame=Number($('#door').value);
    if(moving&&$('#lane').value.startsWith(key+'-'))frame=phase<.14?5:phase<.24?Math.max(0,Math.round(5*(.24-phase)/.1)):0;
    draw(door.frames[frame]);
  }
  if(manifest.screens)draw(manifest.screens.file);
  for(const p of manifest.plants)draw(p.file,1,moving?{pivot:p.pivot,angle:Math.sin(elapsed/2100+p.pivot[0]*8)*(p.sway_degrees*Math.PI/180)}:null);
  const board=moving&&$('#sign').value==='working'?Math.floor(elapsed/manifest.whiteboard.frame_ms)%manifest.whiteboard.frames.length:0;
  draw(manifest.whiteboard.frames[board]);
  for(const light of manifest.lights){const level=Number($('#light').value)/100;const pulse=moving?1-Math.max(0,Math.sin(elapsed/1700+light.position[0]*8))*.035:1;draw(light.file,level*pulse)}
  for(let i=0;i<manifest.crate_spots.length;i++){const c=shared.props.crates[i%shared.props.crates.length];stamp(c.file,manifest.crate_spots[i],c.anchor)}
  for(let i=0;i<manifest.debris_spots.length;i++){const c=shared.props.debris[i%shared.props.debris.length];stamp(c.file,manifest.debris_spots[i],c.anchor)}
  const[role,action]=$('#staff').value.split(':'),actor=shared.staff[role],lane=manifest.staff_lanes[$('#lane').value];
  let pose=action,travel=phase;
  if(['press','tap','set-down'].includes(action)){travel=Math.min(1,phase/.70);if(phase<.70&&moving)pose='walk'}
  if(role==='mechanic'&&action==='carry'&&phase>.80&&moving)pose='set-down';
  const animation=actor.animations[pose],frames=animation.frames;
  const frame=moving?Math.floor(elapsed/animation.frame_ms)%frames.length:Math.min(frames.length-1,Math.floor(phase*frames.length));
  stamp(frames[frame],interpolate(lane,travel),actor.anchor);
  if(manifest.panel){draw(manifest.panel.file);const bulb=$('#bulb').value,flash=moving&&$('#flash').checked&&Math.floor(elapsed/300)%2===0;
    for(const[state,points]of [['pass',manifest.panel.pass_bulbs],['fail',manifest.panel.fail_bulbs]])for(const p of points){const variant=bulb===state?state:'off';stamp(shared.props.bulbs[variant+(flash&&variant!=='off'?'-flash':'')].file,p,[.5,.5])}}
  if($('#debug').checked)draw(manifest.layout_overlay);
}
function loop(now){if($('#animate').checked&&!reduced.matches&&!document.hidden)paint(now);requestAnimationFrame(loop)}
function error(e){for(const id of ['scene','sign','ticker-style'])$('#'+id).disabled=false;$('#status').textContent=e.message;console.error(e)}
async function init(){
  await tickers.init();
  $('#ticker-style').replaceChildren(...tickers.items.map(item=>{const o=document.createElement('option');o.value=item.id;o.textContent=item.name;return o}));
  const[index,meta]=await Promise.all([json(new URL('index.json',ROOT)),json(new URL('sprites.json',sharedURL))]);shared=meta;
  const files=new Set(Object.values(meta.staff).flatMap(actor=>Object.values(actor.animations).flatMap(a=>a.frames)));
  for(const c of [...Object.values(meta.props.bulbs),...meta.props.crates,...meta.props.debris,meta.props.bundle])files.add(c.file);
  await Promise.all([...files].map(async file=>spriteAssets.set(file,{bitmap:await bitmap(new URL(file,sharedURL)),x:0,y:0})));
  $('#scene').replaceChildren(...index.scenes.map(s=>{const o=document.createElement('option');o.value=s.name;o.textContent=s.name.replaceAll('-',' ');return o}));
  const requested=new URLSearchParams(location.search).get('scene');if(index.scenes.some(s=>s.name===requested))$('#scene').value=requested;
  $('#scene').onchange=()=>loadScene($('#scene').value).catch(error);
  for(const el of document.querySelectorAll('input,select'))if(el.id!=='scene')el.addEventListener('input',()=>paint(performance.now()));
  async function changeTicker(){for(const id of ['scene','sign','ticker-style'])$('#'+id).disabled=true;try{await tickers.load($('#ticker-style').value,$('#sign').value);paint(performance.now())}catch(e){error(e)}finally{for(const id of ['scene','sign','ticker-style'])$('#'+id).disabled=false}}
  $('#ticker-style').onchange=changeTicker;$('#sign').onchange=changeTicker;
  $('#staff').onchange=()=>{const[role,action]=$('#staff').value.split(':');const target=action==='press'?'a-to-panel-pass':action==='tap'?'a-to-pass-screen':role==='drone'?'a-to-drone':'a-to-crate';if(Object.hasOwn(manifest.staff_lanes,target))$('#lane').value=target;start=performance.now()};
  function motion(){if(reduced.matches){$('#animate').checked=false;$('#animate').disabled=true;$('#motion-note').textContent='Reduced motion: decorative animation is off.'}else{$('#animate').disabled=false;$('#motion-note').textContent='Staff poses share a fixed ground anchor.'}}
  reduced.addEventListener('change',motion);motion();await loadScene($('#scene').value);requestAnimationFrame(loop);
}
init().catch(error);
