import {TickerSigns} from './ticker_signs.mjs';
const $=s=>document.querySelector(s),tickers=new TickerSigns(),ROOT=new URL('../assets/scenes/',import.meta.url),DOORS=new URL('../assets/doors-v1/',import.meta.url);
const detail=$('#ticker-canvas').getContext('2d'),wall=$('#room-canvas').getContext('2d'),reduced=matchMedia('(prefers-reduced-motion: reduce)');
let layers=[],doors,loaded=false,roomGeneration=0,last=0;
async function json(url){const r=await fetch(url);if(!r.ok)throw new Error(`${r.status}: ${url}`);return r.json()}
async function bitmap(url,b){const r=await fetch(url);if(!r.ok)throw new Error(`${r.status}: ${url}`);const blob=await r.blob();return b?createImageBitmap(blob,...b):createImageBitmap(blob)}
function busy(v){$('#room').disabled=v;$('#state').disabled=v;for(const b of $('#catalog').children)b.disabled=v}
function fail(e){busy(false);$('#status').textContent=e.message;console.error(e)}
function selectedText(){const item=tickers.item;$('#item-title').textContent=item.name;$('#description').textContent=item.description;for(const b of $('#catalog').children)b.setAttribute('aria-pressed',String(b.dataset.item===item.id))}
async function choose(id){busy(true);$('#status').textContent='Preparing ticker…';if(await tickers.load(id,$('#state').value)){selectedText();busy(false);$('#status').textContent='8 ticker styles · 20 room matches ready';paint(performance.now())}}
async function loadRoom(name){
  const mine=++roomGeneration;loaded=false;busy(true);$('#status').textContent='Preparing room wall…';
  const folder=new URL(name+'/',ROOT),m=await json(new URL('scene.json',folder));
  const files=[m.base,...(m.screens?[m.screens.file]:[]),...m.plants.map(p=>p.file),m.whiteboard.frames[0]],next=[],nextDoors=[];
  const dp=doors.profiles.find(p=>p.scene===name),jobs=files.map(async(f,i)=>{const b=m.layer_bounds_px[f];next.push({order:i,bitmap:await bitmap(new URL(f,folder),b),x:b?.[0]||0,y:b?.[1]||0})});
  for(const p of Object.values(dp.doors))jobs.push((async()=>nextDoors.push({bitmap:await bitmap(new URL(p.design+'/frame-01.png',DOORS)),point:p.origin_px}))());
  const results=await Promise.allSettled(jobs);
  if(mine!==roomGeneration||results.some(r=>r.status==='rejected')){for(const l of [...next,...nextDoors])l.bitmap.close();if(results.some(r=>r.status==='rejected'))throw results.find(r=>r.status==='rejected').reason;return}
  for(const l of [...layers,...(doors.images||[])])l.bitmap.close();layers=next.sort((a,b)=>a.order-b.order);doors.images=nextDoors;
  const profile=tickers.profiles.find(p=>p.scene===name);await tickers.load(profile.style,$('#state').value);if(mine!==roomGeneration)return;
  loaded=true;selectedText();$('#room-title').textContent=name.replaceAll('-',' ').replace(/\b\w/g,c=>c.toUpperCase());busy(false);$('#status').textContent='8 ticker styles · 20 room matches ready';paint(performance.now());
}
function paint(time){if(!loaded||!tickers.ready)return;const animate=$('#animate').checked&&!reduced.matches&&!document.hidden;
  detail.clearRect(0,0,784,150);tickers.draw(detail,[0,21],time,{animate,scale:2});wall.imageSmoothingEnabled=false;wall.clearRect(0,0,1536,400);wall.drawImage(layers[0].bitmap,0,0);for(const d of doors.images)wall.drawImage(d.bitmap,...d.point);
  tickers.draw(wall,[572,266],time,{animate});for(const l of layers.slice(1))wall.drawImage(l.bitmap,l.x,l.y);
}
function motion(){if(reduced.matches){$('#animate').checked=false;$('#animate').disabled=true;$('#motion-note').textContent='Reduced motion: ticker details are still; the complete status remains visible.'}else{$('#animate').disabled=false;$('#motion-note').textContent='Art preview only. Status comes from the selected example.'}}
async function init(){await tickers.init();doors=await json(new URL('room-profiles.json',DOORS));$('#room').replaceChildren(...tickers.profiles.map(p=>{const o=document.createElement('option');o.value=p.scene;o.textContent=p.scene.replaceAll('-',' ');return o}));
  $('#catalog').replaceChildren(...tickers.items.map(item=>{const b=document.createElement('button');b.type='button';b.className='card';b.dataset.item=item.id;const img=document.createElement('img');img.src=new URL('../assets/ticker-signs-v1/'+item.thumbnail,import.meta.url);img.alt='';img.width=392;img.height=54;const name=document.createElement('span');name.textContent=item.name;const tag=document.createElement('small');tag.textContent=item.kind;b.append(img,name,tag);b.onclick=()=>choose(item.id).catch(fail);return b}));
  $('#room').onchange=()=>loadRoom($('#room').value).catch(fail);$('#state').onchange=()=>choose(tickers.item.id).catch(fail);$('#animate').oninput=()=>paint(performance.now());reduced.addEventListener('change',()=>{motion();paint(performance.now())});motion();await loadRoom($('#room').value);
  function loop(t){if(t-last>65&&!document.hidden){last=t;paint(t)}requestAnimationFrame(loop)}requestAnimationFrame(loop);
}
init().catch(fail);
