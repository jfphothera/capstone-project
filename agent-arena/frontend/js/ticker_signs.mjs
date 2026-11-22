// Shared art-only ticker loader. Call load(style,state) when authoritative state
// changes; draw() never invents status, task counts or telemetry.
const ROOT=new URL('../assets/ticker-signs-v1/',import.meta.url);
async function json(file){const r=await fetch(new URL(file,ROOT));if(!r.ok)throw new Error(`Ticker ${r.status}: ${file}`);return r.json()}
async function bitmap(file){const r=await fetch(new URL(file,ROOT));if(!r.ok)throw new Error(`Ticker ${r.status}: ${file}`);return createImageBitmap(await r.blob())}
export class TickerSigns{
  constructor(){this.assets=[];this.generation=0;this.item=null;this.state=null;this.ready=false}
  async init(){const[catalog,profiles]=await Promise.all([json('catalog.json'),json('room-profiles.json')]);this.items=catalog.items;this.profiles=profiles.profiles;return this}
  async load(id,state){
    const mine=++this.generation;this.ready=false;
    const item=this.items.find(i=>i.id===id),s=item?.states[state];if(!s)throw new Error('Unknown ticker design or state');
    const results=await Promise.allSettled([item.body,s.emission,s.glow].map(bitmap));const good=results.filter(r=>r.status==='fulfilled').map(r=>r.value);
    if(results.some(r=>r.status==='rejected')){for(const b of good)b.close();throw results.find(r=>r.status==='rejected').reason}
    if(mine!==this.generation){for(const b of good)b.close();return false}
    for(const b of this.assets)b.close();this.assets=good;this.item=item;this.state=s;this.ready=true;return true;
  }
  draw(ctx,point,time,{animate=true,brightness=1,scale=1}={}){
    if(!this.ready)return;const s=this.state,n=animate&&s.frame_count>1?Math.floor(time/s.frame_ms)%s.frame_count:0,r=s.frame_rects_px[n];
    const [x,y]=point,w=392*scale,h=54*scale;ctx.save();ctx.imageSmoothingEnabled=false;ctx.drawImage(this.assets[0],x,y,w,h);ctx.globalAlpha=brightness;
    ctx.drawImage(this.assets[1],...r,x,y,w,h);ctx.globalAlpha=brightness*.32;ctx.globalCompositeOperation='screen';ctx.drawImage(this.assets[2],...r,x,y,w,h);ctx.restore();
  }
  close(){++this.generation;for(const b of this.assets)b.close();this.assets=[];this.ready=false}
}
