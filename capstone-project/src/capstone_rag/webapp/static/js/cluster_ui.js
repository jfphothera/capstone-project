import {el,render,attempt} from './dom.js?v=rc28';
import {request} from './ai_client.js?v=rc28';

export async function renderCluster(host,refresh){
 const data=await request('/api/cluster');
 const addresses=el('textarea',{'aria-label':'Authorized inference addresses',rows:3},data.settings.addresses.join(', '));
 const model=el('input',{'aria-label':'Qualification model',value:data.settings.model||'gemma3:270m'});
 const enabled=el('input',{type:'checkbox',checked:data.settings.enabled});
 const result=el('pre');
 const nodes=el('select',{'aria-label':'Enrolled head'},...data.workers.filter(w=>w.endpoint).map(w=>el('option',{value:w.node_id},`${w.node_id} · ${w.compute_class} · ${w.health_state}`)));
 const eligible=data.workers.filter(w=>w.endpoint&&w.health_state==='healthy');
 const tiers=el('select',{'aria-label':'Hardware tier filter'},el('option',{value:''},'All measured or declared tiers'),...[...new Set(eligible.map(w=>w.compute_class))].map(t=>el('option',{value:t},t)));
 const selections=el('select',{multiple:true,size:6,'aria-label':'Experiment physical nodes'});
 const redraw=()=>{render(selections,...eligible.filter(w=>!tiers.value||w.compute_class===tiers.value).map(w=>el('option',{value:w.node_id},`${w.node_id} · ${w.compute_class}`)));};tiers.onchange=redraw;redraw();
 const candidate=el('select',{'aria-label':'Qualified model'},...[...new Set(eligible.flatMap(w=>w.available_models))].map(m=>el('option',{value:m},m)));
 const profileId=el('input',{'aria-label':'Cluster profile ID',value:'cluster-comparison'});
 const costs=Object.fromEntries(['acquisition_price','electricity_per_kwh','estimated_watts','maintenance_per_year','lifetime_years'].map(k=>[k,el('input',{type:'number',min:0,step:'any','aria-label':k,placeholder:'Unspecified'})]));
 render(host,el('h2',{},'Physical inference cluster'),
  el('p',{},'Enroll authorized private addresses, verify model execution, and inspect measured capacity.'),
  el('label',{},'Addresses (comma separated)',addresses),el('label',{},'Explicit qualification model',model),
  el('label',{},enabled,' Enable periodic inventory, digest and residency checks'),
  el('button.btn',{onclick:()=>attempt(async()=>{await request('/api/cluster/configure',{...data.settings,addresses:addresses.value.split(',').map(x=>x.trim()).filter(Boolean),model:model.value,enabled:enabled.checked,interval_seconds:60});await refresh();})},'Save enrollment settings'),
  el('button.btn',{onclick:()=>attempt(async()=>{result.textContent='Qualification running; real inference may take up to 90 seconds per head.';const value=await request('/api/cluster/qualify',{});result.textContent=JSON.stringify(value,null,2);})},'Discover and qualify selected heads'),
  el('h3',{},'Node lifecycle'),nodes,...['disabled','quarantined','retired','reenroll'].map(action=>el('button.btn',{onclick:()=>attempt(async()=>{await request(`/api/cluster/nodes/${nodes.value}/${action}`,{});await refresh();})},action)),
  el('h3',{},'Verified workers'),el('pre',{},JSON.stringify(data.workers.map(w=>({node:w.node_id,health:w.health_state,tier:w.compute_class,installed:w.installed_models,qualified:w.available_models,loaded:w.loaded_models,active:w.active_tasks,queued:w.queued_tasks})),null,2)),
  el('h3',{},'Configure a distributed experiment profile'),tiers,selections,candidate,profileId,
  el('button.btn',{onclick:()=>attempt(async()=>{await request('/api/cluster/profile',{id:profileId.value,nodes:[...selections.selectedOptions].map(o=>o.value),model:candidate.value});result.textContent='Profile saved. Select it in Experiments and preview the campaign. The active profile was not changed.';})},'Save qualified cluster profile'),
  el('h3',{},'Optional per-node economic assumptions'),el('p',{},'Applies to the selected lifecycle node above. These inputs are user estimates, not measured wall power or verified equipment prices.'),
  ...Object.entries(costs).map(([k,input])=>el('label',{},k.replaceAll('_',' '),input)),
  el('button.btn',{onclick:()=>attempt(async()=>{const value=Object.fromEntries(Object.entries(costs).map(([k,input])=>[k,input.value===''?null:Number(input.value)]));result.textContent=JSON.stringify(await request(`/api/cluster/economics/${nodes.value}`,value),null,2);})},'Save user assumptions'),
  el('h3',{},'Host telemetry'),el('p',{},'Host metrics require the authenticated Windows sender. Missing measurements remain unavailable.'),el('pre',{},JSON.stringify(data.metrics.map(m=>({...m,payload:JSON.parse(m.payload)})),null,2)),result);
}
