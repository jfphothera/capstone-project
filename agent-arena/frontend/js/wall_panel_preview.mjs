// Artwork reviewer. Example state and trace animation is never live telemetry.
const $=s=>document.querySelector(s),ROOT=new URL('../assets/wall-panels-v1/',import.meta.url),SCENES=new URL('../assets/scenes/',import.meta.url),DOORS=new URL('../assets/doors-v1/',import.meta.url);
const roomCtx=$('#room-canvas').getContext('2d'),detailCtx=$('#item-canvas').getContext('2d'),reduced=matchMedia('(prefers-reduced-motion: reduce)');
const scratch=document.createElement('canvas'),scratchCtx=scratch.getContext('2d');
let catalog,profiles,doorProfiles,selected,profile,scene,roomLayers=[],roomDoors=[],signs=new Map(),debug=null,assets=new Map(),ready=false,generation=0,lastPaint=0;
const byId=new Map();
async function json(url){const r=await fetch(url);if(!r.ok)throw new Error(`${r.status}: ${url}`);return r.json()}
async function bitmap(url,bounds){const r=await fetch(url);if(!r.ok)throw new Error(`${r.status}: ${url}`);const blob=await r.blob();return bounds?createImageBitmap(blob,...bounds):createImageBitmap(blob)}
async function channelMask(url){
  // PNG L masks encode intensity, not alpha. Convert their luminance before
  // source-in composition; drawing an L PNG directly would mask nothing.
  const b=await bitmap(url),c=document.createElement('canvas');c.width=b.width;c.height=b.height;
  const cx=c.getContext('2d',{willReadFrequently:true});cx.drawImage(b,0,0);b.close();const pixels=cx.getImageData(0,0,c.width,c.height);
  for(let i=0;i<pixels.data.length;i+=4){const value=pixels.data[i];pixels.data[i]=pixels.data[i+1]=pixels.data[i+2]=255;pixels.data[i+3]=value}
  cx.putImageData(pixels,0,0);return createImageBitmap(c);
}
function busy(value){$('#room').disabled=value;$('#state').disabled=value;for(const b of $('#catalog').children)b.disabled=value}
function fail(e){busy(false);$('#status').textContent=e.message;console.error(e)}
function itemFiles(item){const s=item.states[$('#state').value];return[item.body,s.emission,s.glow,...item.channels.flatMap(c=>[c.mask,...(c.actuated_overlay?[c.actuated_overlay]:[])])]}
async function prepare(mine){
  const ids=new Set([selected.id,...profile.placements.map(p=>p.item)]),files=new Set([...ids].flatMap(id=>itemFiles(byId.get(id)))),next=new Map();
  const results=await Promise.allSettled([...files].map(async f=>next.set(f,await(f.includes('/masks/')?channelMask(new URL(f,ROOT)):bitmap(new URL(f,ROOT))))));
  if(results.some(r=>r.status==='rejected')){for(const b of next.values())b.close();throw results.find(r=>r.status==='rejected').reason}
  if(mine!==generation){for(const b of next.values())b.close();return false}
  for(const b of assets.values())b.close();assets=next;return true;
}
function selectionText(){
  $('#item-title').textContent=selected.name;$('#item-description').textContent=selected.description;
  $('#channel').replaceChildren(...[{id:'all',name:'All indicators'},...selected.channels.map(c=>({id:c.id,name:c.id.replaceAll('-',' ')}))].map(c=>{const o=document.createElement('option');o.value=c.id;o.textContent=c.name;return o}));
  $('#control').replaceChildren(...[{id:'none',name:'None actuated'},...selected.channels.filter(c=>c.actuated_overlay).map(c=>({id:c.id,name:c.id.replaceAll('-',' ')}))].map(c=>{const o=document.createElement('option');o.value=c.id;o.textContent=c.name;return o}));
  for(const b of $('#catalog').children)b.setAttribute('aria-pressed',String(b.dataset.item===selected.id));
}
function done(){ready=true;busy(false);$('#status').textContent='40 panels · 20 matched rooms ready';paint(performance.now())}
async function choose(id){selected=byId.get(id);selectionText();ready=false;busy(true);const mine=++generation;$('#status').textContent='Preparing selected panel…';if(await prepare(mine))done()}
async function stateChange(){ready=false;busy(true);const mine=++generation;$('#status').textContent='Preparing example state…';if(await prepare(mine))done()}
async function loadRoom(name){
  const mine=++generation;ready=false;busy(true);$('#status').textContent='Preparing room controls…';profile=profiles.profiles.find(p=>p.scene===name);selected=byId.get(profile.placements[0].item);selectionText();
  const folder=new URL(name+'/',SCENES),m=await json(new URL('scene.json',folder));
  const files=[m.base,...(m.screens?[m.screens.file]:[]),...m.plants.map(p=>p.file),m.whiteboard.frames[0],...(m.panel?[m.panel.file]:[])];
  const next=[],nextDoors=[],nextSigns=new Map();let nextDebug;
  const jobs=files.map(async(f,i)=>{const b=m.layer_bounds_px[f];next.push({order:i,bitmap:await bitmap(new URL(f,folder),b),x:b?.[0]||0,y:b?.[1]||0})});
  const dp=doorProfiles.profiles.find(p=>p.scene===name);
  for(const placement of Object.values(dp.doors))jobs.push((async()=>nextDoors.push({bitmap:await bitmap(new URL(placement.design+'/frame-01.png',DOORS)),point:placement.origin_px}))());
  for(const[state,f]of Object.entries(m.sign.files))jobs.push((async()=>{const b=m.layer_bounds_px[f];nextSigns.set(state,{bitmap:await bitmap(new URL(f,folder),b),x:b[0],y:b[1]})})());
  jobs.push((async()=>{nextDebug=await bitmap(new URL(m.layout_overlay,folder))})());
  const results=await Promise.allSettled(jobs);
  function releaseNext(){for(const l of [...next,...nextDoors,...nextSigns.values()])l.bitmap.close();nextDebug?.close()}
  if(results.some(r=>r.status==='rejected')){releaseNext();throw results.find(r=>r.status==='rejected').reason}
  if(mine!==generation){releaseNext();return}
  for(const l of [...roomLayers,...roomDoors,...signs.values()])l.bitmap.close();debug?.close();roomLayers=next.sort((a,b)=>a.order-b.order);roomDoors=nextDoors;signs=nextSigns;debug=nextDebug;scene=m;
  if(await prepare(mine)){$('#room-title').textContent=name.replaceAll('-',' ').replace(/\b\w/g,c=>c.toUpperCase());done()}
}
function frameAt(item,time,seed){const s=item.states[$('#state').value];if(!$('#animate').checked||reduced.matches||s.frame_count===1)return s.reduced_motion_frame;return Math.floor((time+seed*13)/s.frame_ms)%s.frame_count}
function stamp(ctx,item,origin,size,time,seed,detail=false){
  const state=item.states[$('#state').value],n=frameAt(item,time,seed),r=state.frame_rects_px[n],brightness=Number($('#brightness').value)/100;
  ctx.save();ctx.imageSmoothingEnabled=false;ctx.drawImage(assets.get(item.body),...origin,...size);
  if(item.id===selected.id&&$('#control').value!=='none'){
    const control=item.channels.find(c=>c.id===$('#control').value);if(control?.actuated_overlay)ctx.drawImage(assets.get(control.actuated_overlay),...origin,...size);
  }
  ctx.globalAlpha=brightness;
  const channel=detail&&$('#channel').value!=='all'?item.channels.find(c=>c.id===$('#channel').value):null;
  if(channel){
    scratch.width=item.size[0];scratch.height=item.size[1];scratchCtx.clearRect(0,0,...item.size);scratchCtx.drawImage(assets.get(channel.mask),0,0);
    scratchCtx.globalCompositeOperation='source-in';scratchCtx.drawImage(assets.get(state.emission),...r,0,0,...item.size);scratchCtx.globalCompositeOperation='source-over';
    ctx.drawImage(scratch,...origin,...size);ctx.globalCompositeOperation='screen';ctx.globalAlpha=brightness*.48;ctx.filter='blur(4px)';ctx.drawImage(scratch,...origin,...size);ctx.filter='none';
  }else{
    ctx.drawImage(assets.get(state.emission),...r,...origin,...size);ctx.globalCompositeOperation='screen';ctx.globalAlpha=brightness*item.lighting.maximum_glow_opacity;ctx.drawImage(assets.get(state.glow),...r,...origin,...size);
  }
  ctx.restore();
}
function paint(time){
  if(!ready)return;roomCtx.imageSmoothingEnabled=false;roomCtx.clearRect(0,0,1536,1024);roomCtx.drawImage(roomLayers[0].bitmap,0,0);
  for(const d of roomDoors)roomCtx.drawImage(d.bitmap,...d.point);for(const l of roomLayers.slice(1))roomCtx.drawImage(l.bitmap,l.x,l.y);
  const sign=signs.get(({working:'working',idle:'empty',waiting:'waiting',alert:'unknown',disconnected:'offline',off:'offline'})[$('#state').value]);roomCtx.drawImage(sign.bitmap,sign.x,sign.y);
  for(const p of profile.placements)stamp(roomCtx,byId.get(p.item),p.origin_px,p.draw_size_px,time,p.phase_seed);
  if($('#paths').checked)roomCtx.drawImage(debug,0,0);
  detailCtx.clearRect(0,0,420,350);const scale=Math.min(360/selected.size[0],290/selected.size[1]),size=selected.size.map(v=>Math.round(v*scale));stamp(detailCtx,selected,[(420-size[0])/2,(350-size[1])/2],size,time,19,true);
}
function motion(){if(reduced.matches){$('#animate').checked=false;$('#animate').disabled=true;$('#motion-note').textContent='Reduced motion: indicators remain still; selected state and lighting stay visible.'}else{$('#animate').disabled=false;$('#motion-note').textContent='Art preview · example states · existing agent paths preserved.'}}
async function init(){
  [catalog,profiles,doorProfiles]=await Promise.all([json(new URL('catalog.json',ROOT)),json(new URL('room-profiles.json',ROOT)),json(new URL('room-profiles.json',DOORS))]);for(const item of catalog.items)byId.set(item.id,item);
  $('#room').replaceChildren(...profiles.profiles.map(p=>{const o=document.createElement('option');o.value=p.scene;o.textContent=p.scene.replaceAll('-',' ');return o}));
  $('#catalog').replaceChildren(...catalog.items.map(item=>{const b=document.createElement('button');b.className='card';b.type='button';b.dataset.item=item.id;b.dataset.role=item.role;const img=document.createElement('img');img.src=new URL(item.thumbnail,ROOT);img.alt='';img.loading='lazy';img.width=156;img.height=146;const name=document.createElement('span');name.textContent=item.name;const tag=document.createElement('small');tag.textContent=item.role==='main'?'Main control panel':'Auxiliary controls';b.append(img,name,tag);b.onclick=()=>choose(item.id).catch(fail);return b}));
  $('#room').onchange=()=>loadRoom($('#room').value).catch(fail);$('#state').onchange=()=>stateChange().catch(fail);
  $('#role').onchange=()=>{for(const b of $('#catalog').children)b.hidden=$('#role').value!=='all'&&b.dataset.role!==$('#role').value};
  for(const id of ['brightness','animate','paths','channel','control'])$('#'+id).addEventListener('input',()=>{$('#brightness-label').textContent=$('#brightness').value+'%';paint(performance.now())});
  reduced.addEventListener('change',()=>{motion();paint(performance.now())});motion();await loadRoom($('#room').value);
  function tick(t){if(t-lastPaint>65&&!document.hidden){lastPaint=t;paint(t)}requestAnimationFrame(tick)}requestAnimationFrame(tick);
}
init().catch(fail);
