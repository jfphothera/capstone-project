// Placement check for every room (development aid; not shipped). Run in the browser console on any
// page served from frontend/:  (await import('/js/room_check.mjs')).checkRooms()
// Loads each room in a hidden frame and reports furniture or props that cross a side wall's foot
// line, pieces whose footprints overlap, and where the bulb strip stands against the trim edge.
import {ROOMS,roomLayout} from './composer_rooms.mjs';

const wait=ms=>new Promise(r=>setTimeout(r,ms));
export async function checkRooms(ids=Object.keys(ROOMS)){
  const out={};
  for(const id of ids){
    const f=document.createElement('iframe');f.style.cssText='position:fixed;left:-4000px;top:0;width:1536px;height:1024px';
    f.src=`/arena-room.html?live=1&room=${id}&seed=check`;document.body.append(f);
    for(let i=0;i<120&&!f.contentWindow?.composer?.R?.ready;i++)await wait(250);
    const c=f.contentWindow.composer,L=roomLayout(id),e=L.floorEdge,doc=f.contentDocument,issues=[];
    if(!c){out[id]=['did not load'];f.remove();continue}
    // Footprints: the lower third of each piece's box (where it meets the floor), in room pixels.
    const pieces=[...doc.querySelectorAll('#actors .furniture')].map(el=>{
      const x=parseFloat(el.style.left),y=parseFloat(el.style.top),w=parseFloat(el.style.width),h=parseFloat(el.style.height),feet=+el.style.zIndex;
      return {name:(el.src||'prop').split('/assets/')[1]?.replace('/body.png','')||'floor prop',x,y,w,h,feet};
    });
    for(const p of c.R.floor)pieces.push({name:p.id,x:p.x,y:p.y,w:p.size,h:p.size,feet:p.feet[1],deco:p});
    const visible=p=>{if(p.deco){const [x0,,x1]=p.deco.item.alpha_bounds_px;return [p.x+x0*p.deco.scale,p.x+x1*p.deco.scale]}return [p.x+p.w*.22,p.x+p.w*.78]};
    for(const p of pieces){
      const [vx0,vx1]=visible(p),lx=e.left[0]+e.left[2]*(p.feet-e.left[1]),rx=e.right[0]+e.right[2]*(p.feet-e.right[1]);
      if(p.feet<330||p.feet>930)continue;   // back wall row and the trim are checked by eye
      if(vx0<lx-30)issues.push(`${p.name}: ${Math.round(lx-vx0)} px into the left wall at y ${Math.round(p.feet)}`);
      if(vx1>rx+30)issues.push(`${p.name}: ${Math.round(vx1-rx)} px into the right wall at y ${Math.round(p.feet)}`);
    }
    const foot=p=>{const [a,b]=visible(p);return [a,p.feet-24,b,p.feet]};
    for(let i=0;i<c.R.floor.length;i++)for(const q of pieces){const p=pieces[pieces.length-c.R.floor.length+i];if(p===q)continue;
      const [a0,b0,a1,b1]=foot(p),[c0,d0,c1,d1]=foot(q),ox=Math.min(a1,c1)-Math.max(a0,c0),oy=Math.min(b1,d1)-Math.max(b0,d0);
      if(ox>8&&oy>8&&p!==q&&p.name!==q.name)issues.push(`${p.name} overlaps ${q.name}`)}
    out[id]={issues:[...new Set(issues)],bulbsFeet:L.bulbs.feet,wall:c.R.wall.map(w=>w.d?.id||w.kind).join(', '),sides:pieces.filter(p=>!p.deco).map(p=>p.name).join(', ')};
    f.remove();
  }
  return out;
}
