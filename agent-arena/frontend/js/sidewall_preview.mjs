const $=s=>document.querySelector(s), ROOT=new URL('../assets/sidewall-v1/',import.meta.url);
let catalog,images=new Map(),backdrop,geometry,roomRequest=0;
const contexts=Object.fromEntries(['room','left','right'].map(s=>[s,$('#'+s+'-stage').getContext('2d')]));
function error(e){$('#status').textContent=e.message;console.error(e)}
async function bitmap(url){const r=await fetch(url);if(!r.ok)throw new Error(`Image unavailable: ${r.status}`);return createImageBitmap(await r.blob())}
function selection(side,kind){return catalog.items.find(i=>i.id===$('#'+side+'-'+kind).value)}
function assembly(ctx,side,x,y,scale){
  const table=selection(side,'table'),prop=selection(side,'prop');
  if(!table||!prop)return;
  ctx.save();ctx.imageSmoothingEnabled=false;ctx.translate(x,y);ctx.scale(scale,scale);
  ctx.drawImage(images.get(table.id),0,0);
  const [ax,ay]=table.support_anchor_px, ps=prop.staging_scale??.45, [px,py]=prop.contact_anchor_px??[128,236];
  ctx.drawImage(images.get(prop.id),ax-px*ps,ay-py*ps,256*ps,256*ps);
  ctx.restore();
}
function paint(){
  for(const side of ['left','right']){
    const ctx=contexts[side];ctx.clearRect(0,0,512,400);assembly(ctx,side,95,80,1.25);
  }
  if(backdrop){const ctx=contexts.room;ctx.imageSmoothingEnabled=false;ctx.clearRect(0,0,1536,1024);ctx.drawImage(backdrop,0,0);
    if($('#routes').checked){
      ctx.save();ctx.lineWidth=3;
      for(const [name,points] of Object.entries(geometry.zones)){
        ctx.beginPath();points.forEach(([x,y],i)=>ctx[i?'lineTo':'moveTo'](x*1536,y*1024));ctx.closePath();ctx.fillStyle=name==='standing'?'#55c4cc30':'#edb66025';ctx.strokeStyle=name==='standing'?'#55c4cc':'#edb660';ctx.fill();ctx.stroke();
      }
      ctx.strokeStyle='#8ddbbb';for(const lane of Object.values(geometry.staff_lanes)){ctx.beginPath();lane.forEach(([x,y],i)=>ctx[i?'lineTo':'moveTo'](x*1536,y*1024));ctx.stroke()}
      ctx.restore();
    }
    for(const [side,x] of [['left',-60],['right',1220]])if(selection(side,'table').fitment_candidate&&selection(side,'prop').fitment_candidate)assembly(ctx,side,x,445,1.25);
  }
}
async function room(){const request=++roomRequest;const roomId=$('#room').value;const base=new URL('../assets/scenes/'+roomId+'/',import.meta.url);const [img,response]=await Promise.all([bitmap(new URL('base.png',base)),fetch(new URL('scene.json',base))]);if(!response.ok){img.close();throw new Error('Room geometry unavailable')}const nextGeometry=await response.json();if(request!==roomRequest){img.close();return}backdrop?.close();backdrop=img;geometry=nextGeometry;paint();$('#room-stage').dataset.room=roomId}
async function init(){
  const response=await fetch(new URL('catalog.json',ROOT));if(!response.ok)throw new Error('Wall-view catalog unavailable');catalog=await response.json();
  const results=await Promise.allSettled(catalog.items.map(async i=>images.set(i.id,await bitmap(new URL(i.body,ROOT)))));
  const failed=results.find(r=>r.status==='rejected');if(failed)throw failed.reason;
  for(const side of ['left','right'])for(const kind of ['table','prop']){
    const items=catalog.items.filter(i=>i.side===side&&(kind==='table'?i.category==='surface':i.category!=='surface'));
    const select=$('#'+side+'-'+kind);select.replaceChildren(...items.map(i=>{const o=document.createElement('option');o.value=i.id;o.textContent=i.name;return o}));select.onchange=paint;
    const fit=items.find(i=>i.fitment_candidate);if(fit)select.value=fit.id;
  }
  $('#catalog').replaceChildren(...catalog.items.map(item=>{
    const card=document.createElement('button');card.type='button';card.className='card';card.dataset.side=item.side;card.dataset.category=item.category;
    const img=document.createElement('img');img.src=new URL(item.body,ROOT);img.alt='';img.loading='lazy';
    const title=document.createElement('span');title.textContent=item.name;const tag=document.createElement('small');tag.textContent=item.side+' wall / '+item.category;card.append(img,title,tag);
    card.onclick=()=>{$('#'+item.side+'-'+(item.category==='surface'?'table':'prop')).value=item.id;paint()};return card;
  }));
  $('#filter').onchange=()=>{const value=$('#filter').value;for(const card of $('#catalog').children)card.hidden=value!=='all'&&card.dataset.side!==value&&(value==='prop'?card.dataset.category==='surface':card.dataset.category!==value)};
  const filters=[['all','All 9 prototypes'],['left','Left wall · 7'],['right','Right wall · 2'],['surface','Tables & counters · 5'],['prop','Tabletop equipment · 4']];
  $('#filter').replaceChildren(...filters.map(([value,label])=>{const o=document.createElement('option');o.value=value;o.textContent=label;return o}));
  $('#room').onchange=()=>room().catch(error);$('#routes').onchange=paint;await room();$('#status').textContent='4 revised fit candidates · 5 earlier drafts preserved';
}
init().catch(error);
