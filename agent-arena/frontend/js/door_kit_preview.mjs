// Optional door art reviewer. It does not drive production door or agent state.
const $=s=>document.querySelector(s),ROOT=new URL('../assets/doors-v1/',import.meta.url),SCENES=new URL('../assets/scenes/',import.meta.url);
const roomCtx=$('#room-canvas').getContext('2d'),detailCtx=$('#item-canvas').getContext('2d'),reduced=matchMedia('(prefers-reduced-motion: reduce)');
let catalog,profiles,selected,profile,scene,layers=[],debug=null,doorAssets=new Map(),ready=false,generation=0,lastPaint=0;
const byId=new Map();
async function json(url){const r=await fetch(url);if(!r.ok)throw new Error(`${r.status}: ${url}`);return r.json()}
async function bitmap(url,bounds){const r=await fetch(url);if(!r.ok)throw new Error(`${r.status}: ${url}`);const b=await r.blob();return bounds?createImageBitmap(b,...bounds):createImageBitmap(b)}
function busy(value){$('#room').disabled=value;$('#door-style').disabled=value;for(const b of $('#catalog').children)b.disabled=value}
function fail(e){busy(false);$('#status').textContent=e.message;console.error(e)}
async function fetchDoors(mine){
  const next=new Map();const files=[...selected.animation.frames,selected.glow];
  const results=await Promise.allSettled(files.map(async f=>next.set(f,await bitmap(new URL(f,ROOT)))));
  if(results.some(r=>r.status==='rejected')){for(const b of next.values())b.close();throw results.find(r=>r.status==='rejected').reason}
  if(mine!==generation){for(const b of next.values())b.close();return false}
  for(const b of doorAssets.values())b.close();doorAssets=next;return true;
}
function selectionText(){
  $('#door-style').value=selected.id;$('#item-title').textContent=selected.name;$('#item-description').textContent=selected.description;$('#item-motion').textContent=selected.motion_label;
  for(const b of $('#catalog').children)b.setAttribute('aria-pressed',String(b.dataset.item===selected.id));
}
function done(){ready=true;busy(false);$('#status').textContent='20 door varieties · 6 opening mechanisms ready';paint(performance.now())}
async function choose(id){
  selected=byId.get(id);selectionText();ready=false;busy(true);$('#status').textContent='Preparing door animation…';const mine=++generation;
  if(await fetchDoors(mine))done();
}
async function loadRoom(name){
  const mine=++generation;ready=false;busy(true);$('#status').textContent='Preparing matched background…';
  profile=profiles.profiles.find(p=>p.scene===name);selected=byId.get(profile.doors.a.design);selectionText();
  const folder=new URL(name+'/',SCENES),m=await json(new URL('scene.json',folder));
  // New complete door rigs cover the old baked frame bays. Never load old leaves.
  const files=[m.base,...(m.screens?[m.screens.file]:[]),...m.plants.map(p=>p.file),m.whiteboard.frames[0],m.sign.files.empty,...(m.panel?[m.panel.file]:[])];
  const next=[];let nextDebug;
  const results=await Promise.allSettled(files.map(async(f,i)=>{const b=m.layer_bounds_px[f];next.push({order:i,bitmap:await bitmap(new URL(f,folder),b),x:b?.[0]||0,y:b?.[1]||0})}));
  if(results.some(r=>r.status==='rejected')){for(const l of next)l.bitmap.close();throw results.find(r=>r.status==='rejected').reason}
  try{nextDebug=await bitmap(new URL(m.layout_overlay,folder))}catch(e){for(const l of next)l.bitmap.close();throw e}
  if(mine!==generation){for(const l of next)l.bitmap.close();nextDebug.close();return}
  for(const l of layers)l.bitmap.close();debug?.close();layers=next.sort((a,b)=>a.order-b.order);debug=nextDebug;scene=m;
  if(await fetchDoors(mine)){$('#room-title').textContent=name.replaceAll('-',' ').replace(/\b\w/g,c=>c.toUpperCase());done()}
}
function frameAt(time,offset=0){
  if(!$('#animate').checked||reduced.matches)return Number($('#opening').value);
  const phase=((time+offset)%5200)/5200;
  // A closed hold, open travel, open hold, then close travel. Positional easing
  // changes progress only; the artwork itself never changes scale.
  const f=phase<.20?0:phase<.40?(phase-.20)/.20:phase<.68?1:phase<.88?1-(phase-.68)/.20:0;
  const smooth=f*f*(3-2*f);return Math.max(0,Math.min(8,Math.round(smooth*8)));
}
function paint(time){
  if(!ready)return;roomCtx.imageSmoothingEnabled=false;roomCtx.clearRect(0,0,1536,1024);
  roomCtx.drawImage(layers[0].bitmap,0,0);
  for(const[key,placement]of Object.entries(profile.doors)){
    const n=frameAt(time,key==='b'?850:0),[x,y]=placement.origin_px;
    roomCtx.drawImage(doorAssets.get(selected.animation.frames[n]),x,y);
    roomCtx.save();roomCtx.globalCompositeOperation='screen';roomCtx.globalAlpha=.32;roomCtx.drawImage(doorAssets.get(selected.glow),x,y);roomCtx.restore();
  }
  for(const l of layers.slice(1))roomCtx.drawImage(l.bitmap,l.x,l.y);
  if($('#paths').checked)roomCtx.drawImage(debug,0,0);
  detailCtx.imageSmoothingEnabled=false;detailCtx.clearRect(0,0,384,320);
  const n=frameAt(time);detailCtx.drawImage(doorAssets.get(selected.animation.frames[n]),58,14,268.8,280);
  detailCtx.save();detailCtx.globalCompositeOperation='screen';detailCtx.globalAlpha=.32;detailCtx.drawImage(doorAssets.get(selected.glow),58,14,268.8,280);detailCtx.restore();
}
function motion(){
  if(reduced.matches){$('#animate').checked=false;$('#animate').disabled=true;$('#motion-note').textContent='Reduced motion: automatic door cycling is off. Use the opening slider to inspect a still frame.'}
  else{$('#animate').disabled=false;$('#motion-note').textContent='Artwork preview. Production door state and agent passage are controlled by the live renderer.'}
}
async function init(){
  [catalog,profiles]=await Promise.all([json(new URL('catalog.json',ROOT)),json(new URL('room-profiles.json',ROOT))]);
  for(const item of catalog.items)byId.set(item.id,item);
  $('#room').replaceChildren(...profiles.profiles.map(p=>{const o=document.createElement('option');o.value=p.scene;o.textContent=p.scene.replaceAll('-',' ');return o}));
  $('#door-style').replaceChildren(...catalog.items.map(item=>{const o=document.createElement('option');o.value=item.id;o.textContent=item.name;return o}));
  $('#catalog').replaceChildren(...catalog.items.map(item=>{const b=document.createElement('button');b.className='card';b.type='button';b.dataset.item=item.id;b.dataset.motion=item.motion;
    const img=document.createElement('img');img.src=new URL(item.thumbnail,ROOT);img.alt='';img.loading='lazy';img.width=148;img.height=154;
    const name=document.createElement('span');name.textContent=item.name;const motion=document.createElement('small');motion.textContent=item.motion_label;b.append(img,name,motion);b.onclick=()=>choose(item.id).catch(fail);return b}));
  $('#room').onchange=()=>loadRoom($('#room').value).catch(fail);$('#door-style').onchange=()=>choose($('#door-style').value).catch(fail);
  $('#mechanism').onchange=()=>{for(const b of $('#catalog').children)b.hidden=$('#mechanism').value!=='all'&&b.dataset.motion!==$('#mechanism').value};
  $('#opening').addEventListener('input',()=>{$('#animate').checked=false;$('#opening-label').textContent=Number($('#opening').value)*12.5+'%';paint(performance.now())});
  for(const id of ['animate','paths'])$('#'+id).addEventListener('input',()=>paint(performance.now()));
  reduced.addEventListener('change',()=>{motion();paint(performance.now())});motion();await loadRoom($('#room').value);
  function tick(time){if(time-lastPaint>60&&!document.hidden){lastPaint=time;if($('#animate').checked&&!reduced.matches){$('#opening').value=frameAt(time);$('#opening-label').textContent=frameAt(time)*12.5+'%'}paint(time)}requestAnimationFrame(tick)}requestAnimationFrame(tick);
}
init().catch(fail);
