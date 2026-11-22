import {api} from './api.js?v=rc28';
import {el,attempt,when,ask} from './dom.js?v=rc28';
const noteDrafts=new Map();
export function documentationPanel(w,refresh){
  const notes=w.coordination?.case_notes||[],events=w.coordination?.case_tag_events||[];
  const tags=new Map(); for(const e of events)tags.set(e.tag,e.operation==='add');
  const body=el('textarea',{rows:3,'aria-label':'Representative case note',placeholder:'Record observations, decisions or a correction. Notes do not authorize account or inventory changes.'});
  body.value=noteDrafts.get(w.case.case_id)||'';
  body.addEventListener('input',()=>noteDrafts.set(w.case.case_id,body.value));
  const amendment=el('select',{'aria-label':'Amend previous note'},el('option',{value:''},'New note'),...notes.map(n=>el('option',{value:n.note_id},`${n.author}: ${n.body.slice(0,70)}`)));
  const tag=el('select',{'aria-label':'Typed case tag'},...['callback_required','transcript_missing','internal_task','document_only','safety_review','identity_ambiguous','follow_up','manual_support'].map(t=>el('option',{value:t},t.replaceAll('_',' '))));
  const run=(fn)=>()=>attempt(fn,{onDone:refresh});
  return el('section.section.documentation',{},el('h3',{},'Internal notes & tags'),
    el('p',{},`Case version ${w.case.case_version} · notes and tag changes retain history`),
    el('div',{},...[...tags].filter(([t,active])=>active).map(([t])=>el('span.chip.accent',{},t.replaceAll('_',' ')))),
    notes.map(n=>el('article.case-note',{},el('small',{},`${n.author} · ${n.authority} · ${when(n.at)}${n.amends_note_id?' · amendment':''}`),el('p',{},n.body))),
    el('label.field',{},el('span',{},'Documentation / correction'),body),amendment,
    el('button.btn.primary',{onclick:run(async()=>{const out=await api.addNote(w.case.case_id,{body:body.value,version:w.case.case_version,command_id:crypto.randomUUID(),amends_note_id:amendment.value||null});noteDrafts.delete(w.case.case_id);return out;})},'Append case note'),
    el('div.workflow-controls',{},tag,...['add','remove'].map(op=>el('button.btn.ghost',{onclick:run(()=>api.caseTag(w.case.case_id,{tag:tag.value,operation:op,version:w.case.case_version}))},`${op==='add'?'Add':'Remove'} tag`))),
    (w.coordination?.contact_tasks||[]).map(t=>el('p',{},`Internal task: ${t.kind} · ${t.status} · ${t.endpoint||'endpoint review required'}`)));
}

export function coordinationActions(w,refresh){
  const run=(fn)=>()=>attempt(fn,{onDone:refresh});
  return el('section.documentation',{},el('h3',{},'Contact review & workflow coordination'),
    (w.contact_observations||[]).map(o=>el('section.case-note',{},el('p',{},`Reported ${o.field}: ${o.value} · ${o.decision||'awaiting review'}`),
      !o.decision?el('div.row',{},...['approve','reject'].map(d=>el('button.btn',{onclick:run(async()=>{const a=await ask({title:'Review reported contact',message:'Promotion requires a verified account. Existing conflicting ERP contact values are preserved.',label:'Contact-review evidence',confirm:'Record review'});return a?api.contactReview(o.observation_id,{decision:d,version:w.case.case_version,note:a.text}):null;})},`${d==='approve'?'Approve':'Reject'} contact`))):null)),
    (w.workflow_runs||[]).filter(r=>r.status==='paused').slice(0,1).map(r=>el('div',{},el('p',{},'Workflow paused for existing domain approvals. Resuming only rechecks prerequisites.'),
      el('button.btn',{onclick:run(()=>api.resumeWorkflow(w.case.case_id))},'Recheck approvals and resume'))));
}
