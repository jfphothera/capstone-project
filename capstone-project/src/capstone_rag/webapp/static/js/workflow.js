import {api} from './api.js?v=rc28';
import {ask,attempt,chip,el,when,keptText,forgetText,disclosure} from './dom.js?v=rc28';
// rc.27: one in-flight submission per button target (a second click does nothing until the server answered).
const button=(label,run,success,refresh,kind='',key=null)=>el('button.btn.'+(kind||'ghost'),{onclick:e=>{const b=e.currentTarget;b.disabled=true;return attempt(run,{success,onDone:refresh,key:key||label}).finally(()=>{b.disabled=false;});}},label);
const field=(label,control)=>el('label.field',{},el('span',{},label),control);

export function operationalPanel(w,refresh,{compact=false}={}){
  const calls=w.service_calls||[], pending=(w.rmas||[]).filter(r=>r.status==='proposed');
  const internalTask=(w.coordination?.contact_tasks||[]).find(t=>t.status!=='completed');
  const failed=(w.rmas||[]).some(r=>r.status==='failed');
  const receiving=(w.rmas||[]).some(r=>r.direction==='inbound' && r.shipment?.status==='delivered' && !r.received_at);
  const next=failed?'Shipping creation failed. Review the failure and retry the approved operation; no label or tracking was created.':receiving?'Carrier delivery is recorded. Verify physical receiving before authorizing any linked replacement.':pending.length?'Review the proposed movements below.':w.draft?'Review/edit the proposed customer email, then authorize sending.':w.shipments?.length?((w.rmas||[]).some(r=>r.direction==='inbound')?'Monitor the recorded return; confirm receiving only after physical receipt.':'Monitor the outbound shipment and customer delivery feedback.'):'Review the investigation and continue the customer conversation.';
  const safety=w.primary_decision?.kind==='adverse_event_determination';
  if(compact) return el('section.workflow',{},el('h3',{},'Next action'),el('p.next-action',{},safety?'Review the safety notification and record a human determination. Routine troubleshooting remains stopped.':w.case.ai_state==='failed'?'Investigation failed. Inspect the recorded failure and decide whether to retry; no resolution is assumed.':internalTask?`Review the ${internalTask.kind.replaceAll('_',' ')} task and original phone record. No customer email is due.`:next),
    el('div.copilot-shortcuts',{},el('button.btn.primary',{onclick:()=>document.dispatchEvent(new CustomEvent('case-section',{detail:pending.length||receiving||failed||safety?'actions':internalTask?'notes':'conversation'}))},pending.length||receiving||failed||safety?'Review actions':internalTask?'Open internal notes':w.draft?'Review customer reply':'Open conversation')));
  return el('section.workflow',{},el('h3',{},'Current action'),el('p.next-action',{},internalTask?`Internal ${internalTask.kind.replaceAll('_',' ')} task: ${internalTask.status.replaceAll('_',' ')}. Review the contact endpoint or original phone record; no customer email is due.`:next),
    el('div',{},chip(w.case.status,'accent'),' ',chip(`Investigation: ${w.case.ai_state}`,'muted')),
    calls.map(c=>el('p.service-call',{},el('b',{},`ERP service call ${c.call_id}`),` · service: ${c.service_status||'active'} · continuing service record`)),
    (w.rmas||[]).map(r=>movement(w,r,refresh)),
    w.case.identity_verified?button('Prepare linked exchange proposal',async()=>{
      const a=await ask({title:'Prepare an exchange proposal',message:'Explain why troubleshooting or a component remedy is insufficient. This creates linked return and replacement proposals; neither is authorized. Replacement dispatch requires verified receiving and separate approval.',label:'Resolution evidence and rationale',confirm:'Prepare proposals'});
      return a?api.exchangeProposal(w.case.case_id,a.text):null;
    },'Exchange proposals prepared. Review each movement separately.',refresh):null,
    el('a.btn.ghost',{href:`/trace?case=${w.case.case_id}`,target:'_blank'},'Open live activity and evidence'));
}

export function movement(w,r,refresh){
  const inbound=r.direction==='inbound', ship=r.shipment;
  const types={device_return:'Inbound device return',part_outbound:'Outbound component shipment',device_replacement_outbound:'Outbound replacement device'};
  const controls=[];
  if(r.status==='proposed'){
    if(inbound){const method=el('select',{'aria-label':`Return method ${r.rma_id}`},el('option',{value:'drop_off_label'},'Customer drop-off using label'),el('option',{value:'carrier_pickup'},'Carrier pickup · up to three attempts'));method.value=r.return_method||'drop_off_label';
      controls.push(field('Return method',method),button('Save return method',async()=>{const a=await ask({title:'Record return method',message:'Records the method and rationale without authorizing shipping.',label:'Rationale',confirm:'Save method'});return a?api.returnMethod(r.rma_id,method.value,a.text):null;},'Return method saved. Movement still needs approval.',refresh));}
    const fail=el('input',{type:'checkbox'});
    controls.push(el('label.sim-failure',{},fail,' Simulate shipping failure when approved'));
    controls.push(button(inbound?'Authorize return and create shipping arrangement':'Authorize outbound movement and create label',async()=>{
      const a=await ask({title:'Authorize simulated inventory movement',message:`${r.reason}\n${r.eligibility_reason}\nCreates a simulated shipping record and a separate customer draft. No email is sent.`,label:'Authorization rationale',confirm:'Authorize movement'});
      return a?api.approveMovement(r.rma_id,a.text,fail.checked):null;
    },o=>`Movement ${o.rma_id} executed. Recorded tracking ${o.tracking_number}. Review the new shipping email draft.`,refresh,'primary'));
    controls.push(button('Reject movement',async()=>{const a=await ask({title:'Reject movement',message:'Rejection permanently blocks this proposal. No shipment or label is created.',label:'Reason',confirm:'Reject',danger:true});return a?api.rejectRma(r.rma_id,a.text):null;},'Movement rejected. No shipment created; review the customer reply or record a revised resolution.',refresh,'danger'));
  }
  if(r.status==='failed')controls.push(button('Retry approved shipping operation',async()=>{const a=await ask({title:'Retry failed simulated shipping',message:'Uses the existing recorded inventory approval and creates at most one shipment.',label:'Recovery evidence',confirm:'Retry'});return a?api.retryRma(r.rma_id,a.text):null;},'Retry completed. Check the recorded result and shipping draft.',refresh));
  if(ship){
    if(ship.return_method==='carrier_pickup' && ship.status==='pickup_attempt_failed' && ship.pickup_attempts>=3){
      controls.push(button('Arrange reviewed drop-off alternative',async()=>{
        const a=await ask({title:'Review alternative return arrangement',message:'Reuse the existing return label and tracking for customer drop-off after three failed pickups. No second inventory movement or label is created. A new customer draft still requires approval.',label:'Alternative arrangement rationale',confirm:'Arrange drop-off'});
        return a?api.returnMethod(r.rma_id,'drop_off_label',a.text):null;
      },'Drop-off alternative recorded using the existing label. Review the new customer draft.',refresh,'primary'));
    }
    const steps=ship.return_method==='carrier_pickup'?['pickup_scheduled','pickup_attempt_failed','picked_up','in_transit','delivered']:inbound?['drop_off_pending','carrier_accepted','in_transit','delivered']:['in_transit','delivered'];
    const state=el('select',{'aria-label':`Next carrier event ${ship.tracking_number}`},...steps.map(s=>el('option',{value:s},s.replaceAll('_',' '))));
    controls.push(el('p',{},`Tracking: ${ship.tracking_number} · ${ship.status.replaceAll('_',' ')}${ship.return_method?' · '+ship.return_method.replaceAll('_',' '):''} · ${ship.pickup_attempts||0} pickup attempts`),
      field('Simulated carrier event',state),button('Record carrier event',()=>api.advance(ship.tracking_number,state.value,'Simulated carrier','Evaluator recorded carrier event'),'Carrier event recorded. Check the new state and any follow-up draft.',refresh),
      el('a.btn.ghost',{href:`/api/shipments/${ship.tracking_number}/label`,target:'_blank'},'Inspect simulated label'));
    if(inbound && ship.status==='delivered' && !r.received_at)controls.push(button('Confirm device received and accepted',async()=>{const a=await ask({title:'Verify physical receiving',message:'Carrier delivery is not receiving. Confirm that the original device has physically arrived and been accepted by the simulated service operation. This enables review of a linked replacement; it does not dispatch it.',label:'Receiving/acceptance evidence',confirm:'Record receiving'});return a?api.receiveReturn(r.rma_id,a.text):null;},'Verified receiving recorded. Review any linked replacement for separate authorization.',refresh,'primary'));
  }
  return el('details.movement-card',{open:r.status==='proposed'||r.status==='failed'||ship?.status==='pickup_attempt_failed'||(inbound && ship?.status==='delivered'&&!r.received_at)},el('summary',{},`${types[r.movement_type]||r.movement_type} · ${r.rma_id} · ${r.status}`),
    el('p',{},r.reason),r.prerequisite_rma_id?el('p.eligibility',{},`${r.prerequisite_rma_id}: ${r.eligibility_reason}`):null,
    r.status==='failed'?el('p.eligibility',{},`Creation failed: ${r.call?.result?.error||'See the recorded failure in the evidence view'}. No tracking number or label was created.`):null,
    r.received_at?el('p',{},`Received and accepted by ${r.received_by} · ${when(r.received_at)}`):null,
    el('div.workflow-controls',{},controls));
}

export function simulatedReplyPanel(w,refresh){
  const sent=(w.messages||[]).filter(m=>m.direction==='outbound' && m.channel==='email' && m.status==='sent').at(-1);
  if(!sent)return null;
  const tkey='reply:'+w.case.case_id+':'+sent.message_id;
  const text=keptText(tkey,{rows:4,'aria-label':'Customer reply text',placeholder:'Type or paste the customer reply to the last sent email'});
  const record=(cont)=>async()=>{const out=await api.simulatedReply(w.case.case_id,text.value,sent.message_id,cont);if(out&&!out.duplicate)forgetText(tkey);return out;};
  return disclosure('sim:'+w.case.case_id,'details.simulation-panel','CUSTOMER REPLY (SIMULATED / MANUAL ENTRY)',[
    el('p',{},`Replies to ${sent.message_id}. The same case/account and previous messages are retained. No live email is connected.`),
    button('Generate simulated customer reply',async()=>{const p=await api.simulatedPreview(w.case.case_id);text.value=p.text;return p;},o=>`Preview generated using ${o.generation_mode}. Review before adding it to the case.`,null),
    field('Customer reply (recording / simulation)',text),
    el('div.workflow-controls',{},
      button('Record reply and continue investigation',record(true),'Reply recorded on this case and queued for a follow-up pass. Any new email still requires your approval.',refresh,'primary','reply-continue:'+tkey),
      button('Record reply only',record(false),'Reply recorded on this case. No investigation pass was queued; decide the next step (for example, close a confirmed fix).',refresh,'','reply-only:'+tkey))]);
}

export function feedbackPanel(w,refresh){
  const targets=[...(w.messages||[]).filter(m=>m.direction==='outbound').map(m=>({id:m.message_id,label:`Response ${m.message_id} · ${m.status}`})),...(w.checklist?.all||[]).map(i=>({id:i.item_id,label:i.label||i.description||i.action}))];
  const target=el('select',{'aria-label':'Feedback decision or response'},...targets.map(t=>el('option',{value:t.id},t.label)));
  const category=el('select',{'aria-label':'Feedback category'},...['classification','priority','qms_source','wording','identity_device','unnecessary_escalation','movement_shipping','other'].map(c=>el('option',{value:c},c.replaceAll('_',' '))));
  const correction=el('textarea',{rows:3,'aria-label':'Human correction'}), explanation=el('textarea',{rows:2,'aria-label':'Correction explanation'});
  return el('details.feedback-panel',{},el('summary',{},`Representative feedback and reviewed improvements (${w.feedback?.length||0})`),
    field('Specific decision/response',target),field('Concern',category),field('Human correction',correction),field('Explanation',explanation),
    button('Record feedback for review',()=>api.feedback(w.case.case_id,{target_id:target.value,category:category.value,correction:correction.value,explanation:explanation.value}),'Feedback recorded for review. Controlled documents, rules and model weights are unchanged.',refresh,'primary'),
    (w.feedback||[]).map(p=>el('details.feedback-entry',{},el('summary',{},`${p.proposal_id} · ${p.evidence.category} · ${p.review_status}`),
      el('p',{},`Recorded by ${p.evidence.representative} · ${when(p.created_at)} · Target ${p.evidence.target_id}`),
      el('div.feedback-comparison',{},el('section',{},el('b',{},'Original recommendation'),el('p',{},p.evidence.original_recommendation.body||p.evidence.original_recommendation.tool_result||p.evidence.original_recommendation.description)),el('section',{},el('b',{},'Human correction'),el('p',{},p.body))),el('p',{},p.evidence.explanation),
      p.review_status==='pending_review'?el('div.row',{},...['approved','rejected'].map(decision=>button(`${decision==='approved'?'Approve reviewed correction':'Reject proposed correction'}`,async()=>{const a=await ask({title:'Review feedback',message:'Records review status. It does not alter QMS, business rules or model weights.',label:'Review rationale',confirm:'Record review'});return a?api.reviewProposal(p.proposal_id,decision,a.text):null;},'Feedback review recorded.',refresh))):el('p',{},`Reviewed by ${p.reviewed_by} · ${when(p.reviewed_at)}`),
      p.review_status==='approved'?button('Replay this reviewed example',()=>api.replayFeedback(p.proposal_id),'Replay recorded internally. Compare the replay with the original; no customer email was sent.',refresh):null)));
}
