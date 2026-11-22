// Deterministic SYNTHETIC replay in arena.v2 shape, for visual acceptance only.
// Every application name says DEMO and every label starts with "DEMO:"; it never
// talks to the event service and the page shows the SYNTHETIC DEMO badge.
export function demoScript(viewLocation='orch:ticketing-demo/main',crowd=0){
  const now=Date.now()/1000,heads=[['head-demo-3','Demo Head 3'],['head-demo-6','Demo Head 6']];
  const apps=[['ticketing-demo','Ticketing (DEMO)','main'],['coder-demo','Coder (DEMO)','main']];
  const agents=[['ticketing-demo','validator','Validator','validation'],['ticketing-demo','retriever','Retriever','retrieval'],['ticketing-demo','triage','Triage','triage'],['coder-demo','python_validator','Coder Validator','validation'],['coder-demo','code_writer','Coder Writer','code-generation'],...['Planner','Analyst','Reviewer','Coordinator','Monitor','Auditor','Dispatcher','Archivist','Responder'].map(name=>['ticketing-demo',name.toLowerCase(),name,name.toLowerCase()])];
  for(let i=0;i<Math.min(1000,Math.max(0,Number(crowd)||0));i++)agents.push(['ticketing-demo','crowd-'+i,'Agent '+String(i+1).padStart(3,'0'),'demo']);
  const color=i=>`hsl(${Math.round((197+i*137.508)%360)} 65% 67%)`;
  const snapshot={as_of_seq:0,server_time:now,applications:apps.map(([a,d])=>({app_id:a,display_name:d})),
    orchestrators:apps.map(([a,d,o])=>({app_id:a,orchestrator_id:o,display_name:d+' orchestrator',location_id:`orch:${a}/${o}`,last_seen:now,heartbeat_seconds:3600,producer_state:'online'})),
    heads:heads.map(([n,d])=>({node_id:n,location_id:'head:'+n,display_name:d,status:'active',telemetry:null})),
    agents:agents.map(([a,id,d,r],i)=>({agent_key:`${a}/${id}`,app_id:a,agent_id:id,display_name:d,role:r,orchestrator_id:'main',home_location:`orch:${a}/main`,color:color(i),retired:false})),
    assignments:[],handoffs:[]};
  snapshot.assignments=[['T-1','validator','validation'],['T-2','retriever','retrieval'],['T-3','triage','triage']].map(([id,agent,action])=>({app_id:'ticketing-demo',assignment_id:id,assignment_key:`ticketing-demo/${id}`,agent_id:agent,agent_key:`ticketing-demo/${agent}`,orchestrator_id:'main',kind:'orchestration',location_id:'orch:ticketing-demo/main',state:'executing',visual_action:action,updated_at:now,label:'DEMO: '+action,task_id:id,model_id:null}));
  const live=Object.fromEntries(snapshot.assignments.map(a=>[a.assignment_key,{...a}]));let seq=0;const steps=[];
  const step=(app,type,id,fields)=>{const key=`${app}/${id}`,prior=live[key];const agent=fields.agent||prior?.agent_id;
    const next={...(prior||{app_id:app,assignment_id:id,assignment_key:key,agent_id:agent,agent_key:`${app}/${agent}`,orchestrator_id:'main',kind:'inference',location_id:`orch:${app}/main`,state:'queued'}),...fields.set,updated_at:now+seq};
    if(fields.label)next.label=fields.label;delete next.agent;live[key]=next;seq+=1;
    steps.push({seq,app_id:app,event_id:`DEMO-${seq}`,type,received_at:now+seq,occurred_at:now+seq,applied:true,note:null,from_location:prior?prior.location_id:null,to_location:next.location_id,payload:{orchestrator_id:'main',label:fields.label,source_event_type:'demo'},result:{...next,render_state:next.state,lease_expires_at:null}})};
  const H3='head:head-demo-3',H6='head:head-demo-6',T='orch:ticketing-demo/main',C='orch:coder-demo/main';
  const handoff=(type,sender,receiver,correlation)=>{seq++;steps.push({seq,app_id:'ticketing-demo',event_id:`DEMO-${seq}`,type,received_at:now+seq,occurred_at:now+seq,applied:true,from_location:T,to_location:T,payload:{orchestrator_id:'main',sender_agent_id:sender,receiver_agent_id:receiver,correlation_id:correlation,label:'DEMO: agent handoff'}})};
  handoff('handoff.sent','retriever','validator','demo-exchange-1');
  handoff('handoff.received','retriever','validator','demo-exchange-1');
  step('ticketing-demo','assignment.state','T-1',{agent:'validator',set:{kind:'orchestration',state:'executing',visual_action:'validation'},label:'DEMO: validating draft at orchestrator'});
  step('ticketing-demo','assignment.moved','T-1',{set:{location_id:H3,state:'waiting',wait_reason:'model',visual_action:'waiting_model'},label:'DEMO: dispatched to Head 3'});
  step('coder-demo','assignment.created','C-1',{agent:'python_validator',set:{kind:'orchestration',state:'executing',visual_action:'testing'},label:'DEMO: preparing test run'});
  step('ticketing-demo','assignment.state','T-1',{set:{state:'executing',visual_action:'inference',wait_reason:null},label:'DEMO: inference executing'});
  step('coder-demo','assignment.moved','C-1',{set:{location_id:H3,state:'waiting',wait_reason:'capacity',visual_action:'waiting_model'},label:'DEMO: queued behind Ticketing on Head 3'});
  step('ticketing-demo','assignment.state','T-2',{agent:'retriever',set:{kind:'orchestration',state:'executing',visual_action:'retrieval'},label:'DEMO: retrieving evidence'});
  step('ticketing-demo','assignment.moved','T-1',{set:{location_id:T,state:'executing',visual_action:'review'},label:'DEMO: returned with model output'});
  step('coder-demo','assignment.state','C-1',{set:{state:'executing',visual_action:'inference',wait_reason:null},label:'DEMO: inference executing'});
  step('coder-demo','assignment.created','C-2',{agent:'code_writer',set:{kind:'orchestration',state:'executing',visual_action:'code_generation'},label:'DEMO: writing patch'});
  step('coder-demo','assignment.moved','C-1',{set:{location_id:H6,state:'executing',visual_action:'inference'},label:'DEMO: next step on Head 6'});
  step('ticketing-demo','assignment.state','T-2',{set:{state:'failed',visual_action:'error_handling'},label:'DEMO: retrieval failed'});
  step('ticketing-demo','assignment.state','T-1',{set:{state:'completed',visual_action:'completion'},label:'DEMO: validation complete'});
  step('coder-demo','assignment.moved','C-1',{set:{location_id:C,state:'executing',visual_action:'review'},label:'DEMO: back at Coder orchestrator'});
  step('coder-demo','assignment.state','C-1',{set:{state:'completed',visual_action:'completion'},label:'DEMO: tests passed'});
  for(const action of ['evaluation','review','code_generation','testing','tool_use','inference','error_handling'])step('ticketing-demo','assignment.state','T-3',{set:{state:'executing',visual_action:action},label:'DEMO: '+action});
  handoff('handoff.sent','triage','planner','demo-exchange-2');
  handoff('handoff.received','triage','planner','demo-exchange-2');
  for(const [waitReason,action]of [['human','waiting_human'],['model','waiting_model'],['dependency','idle'],['tool','idle'],['capacity','waiting_model']])step('ticketing-demo','assignment.state','T-3',{set:{state:'waiting',wait_reason:waitReason,visual_action:action},label:'DEMO: waiting for '+waitReason});
  step('ticketing-demo','assignment.state','T-3',{set:{state:'completed',wait_reason:null,visual_action:'completion'},label:'DEMO: task complete'});
  return{identity:{display_id:'demo',role:viewLocation.startsWith('head:')?'head':'orchestrator',location_id:viewLocation,demo:true},snapshot,events:steps};
}
