// Isolated asset reviewer. Decorative loops never claim to be live events.
const $=s=>document.querySelector(s),ROOT=new URL('../assets/decorations-v1/',import.meta.url),SCENES=new URL('../assets/scenes/',import.meta.url);
const roomCanvas=$('#room-canvas'),roomCtx=roomCanvas.getContext('2d'),detailCtx=$('#item-canvas').getContext('2d');
const reduced=matchMedia('(prefers-reduced-motion: reduce)');
let catalog,profiles,selected,profile,scene,roomLayers=[],debugLayer=null,cache=new Map(),generation=0,ready=false,lastPaint=0;
const byId=new Map();
async function json(url){const r=await fetch(url);if(!r.ok)throw new Error(`${r.status}: ${url}`);return r.json()}
async function bitmap(url,bounds){const r=await fetch(url);if(!r.ok)throw new Error(`${r.status}: ${url}`);const blob=await r.blob();return bounds?createImageBitmap(blob,...bounds):createImageBitmap(blob)}
function busy(value){$('#room').disabled=value;for(const button of $('#catalog').children)button.disabled=value;}
function fail(e){busy(false);$('#status').textContent=e.message;console.error(e)}
function propFiles(item){return[item.unlit,...item.animation.frames,...item.animation.glow_frames]}
async function prepareProps(mine){
  const ids=new Set([selected.id,...profile.instances.map(p=>p.item)]),files=new Set([...ids].flatMap(id=>propFiles(byId.get(id))));
  const next=new Map();
  try{await Promise.all([...files].map(async file=>{next.set(file,await bitmap(new URL(file,ROOT)))}))}catch(e){for(const b of next.values())b.close();throw e}
  if(mine!==generation){for(const b of next.values())b.close();return false}
  for(const b of cache.values())b.close();cache=next;return true;
}
async function loadRoom(name){
  const mine=++generation;ready=false;busy(true);$('#status').textContent='Preparing the room…';
  profile=profiles.profiles.find(p=>p.scene===name);
  const folder=new URL(name+'/',SCENES),m=await json(new URL('scene.json',folder));
  const files=[m.base,...Object.values(m.doors).map(d=>d.frames[0]),m.whiteboard.frames[0],m.sign.files.empty];
  if(m.screens)files.push(m.screens.file);if(m.panel)files.push(m.panel.file);
  const layers=[];let debug;
  try{await Promise.all(files.map(async file=>{const bounds=m.layer_bounds_px[file];layers.push({file,order:files.indexOf(file),bitmap:await bitmap(new URL(file,folder),bounds),x:bounds?.[0]||0,y:bounds?.[1]||0})}));debug=await bitmap(new URL(m.layout_overlay,folder));}
  catch(e){for(const l of layers)l.bitmap.close();debug?.close();throw e}
  if(mine!==generation){for(const l of layers)l.bitmap.close();debug?.close();return}
  for(const l of roomLayers)l.bitmap.close();debugLayer?.close();roomLayers=layers.sort((a,b)=>a.order-b.order);debugLayer=debug;scene=m;
  if(!await prepareProps(mine))return;
  $('#room-title').textContent=name.replaceAll('-',' ').replace(/\b\w/g,c=>c.toUpperCase());ready=true;busy(false);$('#status').textContent='60 props · 20 room profiles ready';paint(performance.now());
}
function frameAt(item,time,seed=0){
  if(!$('#animate').checked||reduced.matches)return item.animation.reduced_motion_frame;
  const a=item.animation,duration=a.frames.length*a.frame_ms,hash=Math.abs(Math.sin(seed*19.17+7.4));
  const interval=a.minimum_interval_ms+hash*(a.maximum_interval_ms-a.minimum_interval_ms),t=(time+hash*interval*3)%interval;
  return t<duration?Math.floor(t/a.frame_ms):0;
}
function stamp(ctx,item,point,scale,time,seed){
  const index=frameAt(item,time,seed),a=item.anchor_px,x=point[0]-a[0]*scale,y=point[1]-a[1]*scale;
  const brightness=Number($('#brightness').value)/100;
  ctx.save();ctx.imageSmoothingEnabled=false;
  if(item.lighting.enabled){ctx.drawImage(cache.get(item.unlit),x,y,256*scale,256*scale);ctx.globalAlpha=brightness;}
  ctx.drawImage(cache.get(item.animation.frames[index]),x,y,256*scale,256*scale);
  if(item.lighting.enabled&&brightness>0){ctx.globalAlpha=brightness*item.lighting.maximum_opacity;ctx.globalCompositeOperation='screen';ctx.drawImage(cache.get(item.animation.glow_frames[index]),x,y,256*scale,256*scale)}
  ctx.restore();
}
function paint(time){
  if(!ready)return;roomCtx.imageSmoothingEnabled=false;roomCtx.clearRect(0,0,1536,1024);
  for(const l of roomLayers)roomCtx.drawImage(l.bitmap,l.x,l.y);
  for(const instance of profile.instances)stamp(roomCtx,byId.get(instance.item),instance.anchor_px,instance.scale,time,instance.phase_seed);
  if($('#paths').checked)roomCtx.drawImage(debugLayer,0,0);
  detailCtx.clearRect(0,0,384,320);stamp(detailCtx,selected,[192,285],1.15,time,37);
}
function selectedText(){
  $('#item-title').textContent=selected.name;$('#item-family').textContent=selected.category.toUpperCase();$('#item-description').textContent=selected.description;
  $('#item-rig').textContent=selected.animation.rig.replaceAll('-',' ');
  for(const button of $('#catalog').querySelectorAll('button'))button.setAttribute('aria-pressed',String(button.dataset.item===selected.id));
}
async function choose(item){
  selected=item;selectedText();const mine=++generation;ready=false;busy(true);$('#status').textContent='Loading selected prop…';
  if(await prepareProps(mine)){ready=true;busy(false);$('#status').textContent='60 props · 20 room profiles ready';paint(performance.now())}
}
function motion(){
  if(reduced.matches){$('#animate').checked=false;$('#animate').disabled=true;$('#motion-note').textContent='Reduced motion: decorative loops are still. Lighting remains adjustable.'}
  else{$('#animate').disabled=false;$('#motion-note').textContent='Decorative animation is illustrative; it does not represent live results.'}
}
async function init(){
  [catalog,profiles]=await Promise.all([json(new URL('catalog.json',ROOT)),json(new URL('room-profiles.json',ROOT))]);
  for(const item of catalog.items)byId.set(item.id,item);selected=catalog.items[0];
  $('#room').replaceChildren(...profiles.profiles.map(p=>{const o=document.createElement('option');o.value=p.scene;o.textContent=p.scene.replaceAll('-',' ');return o}));
  $('#catalog').replaceChildren(...catalog.items.map(item=>{const button=document.createElement('button');button.className='card';button.type='button';button.dataset.item=item.id;button.dataset.family=item.category;button.setAttribute('aria-pressed',String(item.id===selected.id));
    const img=document.createElement('img');img.src=new URL(item.base,ROOT);img.alt='';img.loading='lazy';img.width=148;img.height=148;
    const name=document.createElement('span');name.textContent=item.name;const tag=document.createElement('small');tag.textContent=item.category;
    button.append(img,name,tag);button.onclick=()=>choose(item).catch(fail);return button}));
  $('#family').onchange=()=>{for(const c of $('#catalog').children)c.hidden=$('#family').value!=='all'&&c.dataset.family!==$('#family').value};
  $('#room').onchange=()=>loadRoom($('#room').value).catch(fail);
  for(const id of ['brightness','animate','paths'])$('#'+id).addEventListener('input',()=>{$('#brightness-label').textContent=$('#brightness').value+'%';paint(performance.now())});
  reduced.addEventListener('change',()=>{motion();paint(performance.now())});motion();selectedText();await loadRoom($('#room').value);
  function tick(time){if(time-lastPaint>65&& !document.hidden){lastPaint=time;paint(time)}requestAnimationFrame(tick)}requestAnimationFrame(tick);
}
init().catch(fail);
