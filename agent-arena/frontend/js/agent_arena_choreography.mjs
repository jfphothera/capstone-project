// Presentation-only randomness. No generated action is ever sent to the service.
export const IDLES=Object.freeze([
  {name:'look',weight:4,duration:2600}, {name:'blink',weight:5,duration:1800},
  {name:'stretch',weight:2,duration:3600}, {name:'settle',weight:3,duration:2400},
  {name:'inspect',weight:2,duration:3200}, {name:'polish',weight:1,duration:3000},
  {name:'watch',weight:3,duration:2800}, {name:'ankles',weight:2,duration:3400}
]);
export function seededRandom(seed){
  let h=2166136261;for(const c of String(seed))h=Math.imul(h^c.charCodeAt(0),16777619);
  return()=>{h+=0x6D2B79F5;let t=h;t=Math.imul(t^t>>>15,t|1);t^=t+Math.imul(t^t>>>7,t|61);return((t^t>>>14)>>>0)/4294967296};
}
export function createIdleClock(seed){
  const random=seededRandom(seed),actors=new Map();
  return{
    step(entries,now,{paused=false,reduced=false}={}){
      const eligible=new Set(entries.filter(e=>e.state==='idle'&&e.render==='idle').map(e=>e.id));
      for(const key of actors.keys())if(!eligible.has(key))actors.delete(key);
      if(paused||reduced){actors.clear();return new Map()}
      const result=new Map(),limit=Math.max(1,Math.ceil(eligible.size*.2));
      // Register all agents before selecting: first render never triggers an idle.
      for(const key of eligible)if(!actors.has(key))actors.set(key,{next:now+12000+random()*28000,until:0,last:null,cooldowns:{}});
      for(const key of eligible){const a=actors.get(key);if(a.until>now&&result.size<limit)result.set(key,a.last)}
      for(const key of eligible){const a=actors.get(key);if(a.until>now||a.next>now||result.size>=limit)continue;
        const pool=IDLES.filter(x=>x.name!==a.last&&!(a.cooldowns[x.name]>now));
        let pick=random()*pool.reduce((n,x)=>n+x.weight,0),chosen=pool.at(-1);
        for(const x of pool){pick-=x.weight;if(pick<0){chosen=x;break}}
        a.last=chosen.name;a.until=now+chosen.duration;a.next=a.until+18000+random()*47000;
        a.cooldowns[chosen.name]=now+(chosen.weight<=2?120000+random()*120000:30000);
        result.set(key,chosen.name);
      }
      return result;
    },
    reset(){actors.clear()}
  };
}

// A cached hidden floor actor must never mask a visible waiting seat.
export function firstVisible(...elements){return elements.find(e=>e&&!e.hidden&&e.isConnected)||null}

// The background is stretched to the scene bounds, so normalized art coordinates
// stay aligned at every viewport size. These are floor/foot points, not sign centers.
export const DOOR=Object.freeze({x:.858,y:.252});
export function scenePoint(bounds,point){return{x:bounds.left+bounds.width*point.x,y:bounds.top+bounds.height*point.y}}
export function doorRoute(bounds){return{threshold:scenePoint(bounds,DOOR),approach:scenePoint(bounds,{x:.82,y:.34})}}
export function depthAt(bounds,point){return Math.max(.84,Math.min(1,.82+.18*(point.y-bounds.top)/bounds.height))}
export function footPoint(rect){return{x:rect.left+rect.width*.5,y:rect.top+rect.height*.95}}

// A fixed physical holding area, with explicit pages instead of an ever-growing scene.
export function standingLayout(width,height,count,page=0){
  // Loose, repeatable crowd positions. Screen-space separation leaves room for
  // the entire sprite and its name; the same positions serve small/large rosters.
  const candidates=[{x:.14,y:.46},{x:.285,y:.51},{x:.17,y:.70},{x:.295,y:.80},{x:.12,y:.84},{x:.32,y:.65},{x:.24,y:.43},{x:.23,y:.61},{x:.225,y:.87}];
  for(let i=0;i<100;i++)candidates.push({x:.12+((i*.61803398875+.29)%1)*.20,y:.43+((i*.754877666+.13)%1)*.43});
  const points=[];for(const p of candidates){if(points.length>=16)break;if(points.every(q=>Math.hypot((p.x-q.x)*width/75,(p.y-q.y)*height/96)>=1))points.push(p)}
  const capacity=Math.max(1,points.length),pages=Math.max(1,Math.ceil(count/capacity)),current=Math.max(0,Math.min(page,pages-1));
  return{capacity,pages,page:current,start:current*capacity,end:Math.min(count,(current+1)*capacity),
    positions:points.map(p=>({x:p.x*100,y:p.y*100,depth:depthAt({top:0,height:1},{y:p.y})}))};
}

export const ACTION_LABELS=Object.freeze({triage:'Triage',retrieval:'Retrieving',evaluation:'Evaluating',validation:'Validating',review:'Reviewing',code_generation:'Writing code',testing:'Testing',tool_use:'Using tool',inference:'Inference',waiting_model:'Waiting for model',waiting_human:'Waiting for review',error_handling:'Handling error',completion:'Complete',handoff:'Handoff',idle:'Idle'});
