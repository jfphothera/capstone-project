"""Immutable, authority-preserving experimental document representations.

The curator never edits QMS sources or the approved index.  Source snapshots and
derived artifacts are content addressed, and every chunk is an exact source span.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path

import numpy as np

from . import config, embeddings, qms
from .ai_settings import canonical

FORMAT='capstone-document-curator-v1'
STRATEGIES=('heading','fixed','semantic_lexical','parent_child','document_type')
AUTHORITY_FIELDS=('document_id','procedure_id','document_version','approval_status',
    'model','applicable_revisions','audiences','document_type','access_permissions',
    'permitted_uses','scope','revision_scope','owning_function','required_approvals',
    'safety_restrictions','workflow_associations','workflow_actions','effective_date',
    'supersedes','superseded_by')


def _hash(value):
    data=value if isinstance(value,bytes) else canonical(value).encode()
    return hashlib.sha256(data).hexdigest()


def _root(path=None):
    return Path(path or Path(config.DATA_DIR)/'experiments'/'document-curator')


def _document(raw,path):
    if not raw.startswith('---'):
        raise ValueError(f'{path}: missing frontmatter block')
    closing=raw.find('---',3)
    if closing<0:raise ValueError(f'{path}: unterminated frontmatter block')
    meta=qms._parse_frontmatter(raw[3:closing])
    body_start=closing+3
    while body_start<len(raw) and raw[body_start] in '\r\n':body_start+=1
    body=raw[body_start:]
    required=('document_id','procedure_id','model','applicable_revisions',
        'document_version','approval_status','audiences','title')
    missing=[key for key in required if not meta.get(key)]
    if missing:raise ValueError(f'{path}: frontmatter missing {missing}')
    # Reuse the existing controlled parser as the source of derived authority defaults.
    return meta,body_start,body


def create_snapshot(qms_dir=None,output_root=None):
    """Copy a controlled corpus into an immutable, content-addressed snapshot."""
    source=Path(qms_dir or config.QMS_DIR)
    rows=[]
    for path in sorted(source.glob('*.md')):
        raw=path.read_bytes();text=raw.decode('utf-8')
        meta,body_start,_=_document(text,path)
        rows.append({'name':path.name,'source_path':os.path.relpath(path,config.PROJECT_ROOT),
            'sha256':_hash(raw),'bytes':len(raw),'body_start':body_start,
            'document_id':meta['document_id'],'procedure_id':meta['procedure_id'],
            'document_version':str(meta['document_version']),'model':meta['model'],
            'applicable_revisions':list(meta['applicable_revisions']),
            'approval_status':meta['approval_status'],'audiences':list(meta['audiences']),
            'effective_date':meta.get('effective_date'),'superseded_by':meta.get('superseded_by'),
            'document_type':meta.get('document_type')})
    if not rows:raise ValueError(f'no QMS documents found in {source}')
    identity={'format':FORMAT,'documents':rows}
    snapshot_hash=_hash(identity);root=_root(output_root)
    target=root/'snapshots'/snapshot_hash
    manifest={**identity,'snapshot_hash':snapshot_hash,'source_count':len(rows),
        'immutable':True,'created_at':time.time(),'operational_activation_permitted':False}
    if target.exists():
        existing=json.loads((target/'manifest.json').read_text(encoding='utf-8'))
        if existing['snapshot_hash']!=snapshot_hash:raise RuntimeError('Snapshot identity mismatch')
        return existing
    staging=root/'staging'/('snapshot-'+uuid.uuid4().hex);(staging/'sources').mkdir(parents=True)
    try:
        for row in rows:shutil.copy2(source/row['name'],staging/'sources'/row['name'])
        (staging/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
        target.parent.mkdir(parents=True,exist_ok=True)
        staging.replace(target)
        for path in target.rglob('*'):
            if path.is_file():path.chmod(0o444)
    finally:
        if staging.exists():shutil.rmtree(staging)
    return manifest


def _authority(meta):
    service='service' in meta['audiences'];historical=meta['approval_status']=='Superseded'
    defaults={'document_type':'superseded_historical' if historical else
            'qualified_service_instruction' if service else 'customer_troubleshooting_instruction',
        'access_permissions':['audit_only'] if 'audit_only' in meta['audiences'] else
            ['qualified_service_only','representative'] if service else
            ['customer_disclosable','agent_internal','representative'],
        'permitted_uses':['historical_review'] if 'audit_only' in meta['audiences'] else
            ['qualified_service','representative_reference'] if service else
            ['customer_answer','revision_troubleshooting'],
        'scope':'exact_model_revision','revision_scope':'listed_revisions',
        'owning_function':meta.get('owner','Customer Service'),'required_approvals':[],
        'safety_restrictions':['stop_on_harm_or_hazard'],'workflow_associations':[],
        'workflow_actions':[]}
    return {key:meta.get(key,defaults.get(key)) for key in AUTHORITY_FIELDS}


def _heading_spans(body):
    matches=list(re.finditer(r'^(#{2,3})\s+(.+?)\s*$',body,re.MULTILINE))
    parents={}
    for index,match in enumerate(matches):
        level=len(match.group(1));parents[level]=match.group(2).strip()
        parents={key:value for key,value in parents.items() if key<=level}
        end=matches[index+1].start() if index+1<len(matches) else len(body)
        if body[match.start():end].strip():
            yield match.start(),end,' > '.join(parents[key] for key in sorted(parents)),level


def _fixed_spans(text,size,overlap):
    if not 200<=size<=4000 or not 0<=overlap<min(size,1000):
        raise ValueError('Fixed size must be 200–4000 and overlap smaller than the size')
    start=0
    while start<len(text):
        end=min(len(text),start+size)
        if end<len(text):
            boundary=max(text.rfind('\n',start+max(1,size//2),end),
                         text.rfind(' ',start+max(1,size//2),end))
            if boundary>start:end=boundary+1
        yield start,end,None,None
        if end==len(text):break
        start=max(start+1,end-overlap)


def _semantic_spans(text,target):
    """Deterministic lexical-cohesion paragraph grouping; no model claim is made."""
    paragraphs=[match for match in re.finditer(r'\S(?:.|\n)*?(?=\n\s*\n|\Z)',text)]
    if not paragraphs:return
    start=paragraphs[0].start();previous=set();end=start
    for match in paragraphs:
        words=set(re.findall(r'[a-z0-9]+',match.group().lower()))
        cohesion=len(previous&words)/max(1,len(previous|words)) if previous else 1
        if end>start and end-start>=target//2 and (end-start>=target or cohesion<.04):
            yield start,end,None,None;start=match.start()
        end=match.end();previous=words
    if end>start:yield start,end,None,None


def _span_records(snapshot_dir,row,strategy,settings):
    raw=(snapshot_dir/'sources'/row['name']).read_text(encoding='utf-8')
    meta,body_start,body=_document(raw,row['name']);authority=_authority(meta)
    size=int(settings.get('size',800));overlap=int(settings.get('overlap',100))
    if strategy=='heading':spans=list(_heading_spans(body))
    elif strategy=='fixed':spans=list(_fixed_spans(body,size,overlap))
    elif strategy=='semantic_lexical':spans=list(_semantic_spans(body,size))
    elif strategy=='document_type':
        structural=meta.get('document_type') in ('customer_user_manual','qualified_service_instruction')
        spans=list(_heading_spans(body) if structural else _fixed_spans(body,size,overlap))
    elif strategy=='parent_child':
        spans=[]
        for pstart,pend,path,level in _heading_spans(body):
            spans.append((pstart,pend,path,level,'parent',None))
            parent_key=f"{meta['document_id']}:{body_start+pstart}:{body_start+pend}"
            for cstart,cend,_,_ in _fixed_spans(body[pstart:pend],size,overlap):
                spans.append((pstart+cstart,pstart+cend,path,level,'child',parent_key))
    else:raise ValueError('Unknown chunking strategy')
    records=[]
    for number,span in enumerate(spans,1):
        start,end,path,level,*relationship=span;absolute_start=body_start+start;absolute_end=body_start+end
        text=raw[absolute_start:absolute_end]
        kind,parent=(relationship+[None,None])[:2]
        identity={'source_sha256':row['sha256'],'start':absolute_start,'end':absolute_end,
            'strategy':strategy,'kind':kind or 'chunk'}
        records.append({'chunk_id':meta['document_id']+'#'+_hash(identity)[:16],
            'source_snapshot_hash':json.loads((snapshot_dir/'manifest.json').read_text())['snapshot_hash'],
            'source_name':row['name'],'source_sha256':row['sha256'],
            'source_offset_start':absolute_start,'source_offset_end':absolute_end,
            'text_sha256':_hash(text.encode()),'text':text,'strategy':strategy,
            'section_path':path,'heading_level':level,'relationship':kind or 'chunk',
            'parent_source_span':parent,**authority})
    return records


def build_chunks(snapshot_hash,strategy,settings=None,output_root=None):
    """Create an immutable chunk manifest derived from one source snapshot."""
    if strategy not in STRATEGIES:raise ValueError('Unknown chunking strategy')
    root=_root(output_root);snapshot_dir=root/'snapshots'/snapshot_hash
    if not snapshot_dir.exists():raise ValueError('Unknown source snapshot')
    snapshot=json.loads((snapshot_dir/'manifest.json').read_text(encoding='utf-8'))
    settings=dict(settings or {})
    spec={'format':FORMAT,'snapshot_hash':snapshot_hash,'strategy':strategy,
        'settings':settings,'curator_version':1}
    artifact_hash=_hash(spec);target=root/'artifacts'/'chunks'/artifact_hash
    if target.exists():return json.loads((target/'manifest.json').read_text(encoding='utf-8'))
    chunks=[]
    for row in snapshot['documents']:
        chunks.extend(_span_records(snapshot_dir,row,strategy,settings))
    manifest={**spec,'artifact_hash':artifact_hash,'chunk_count':len(chunks),
        'documents':len(snapshot['documents']),'immutable':True,'created_at':time.time(),
        'authority_metadata_inherited':True,'exact_source_spans':True,
        'operational_activation_permitted':False,
        'chunks_sha256':_hash(chunks)}
    staging=root/'staging'/('chunks-'+uuid.uuid4().hex);staging.mkdir(parents=True)
    try:
        (staging/'chunks.jsonl').write_text(''.join(canonical(row)+'\n' for row in chunks),encoding='utf-8')
        (staging/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
        target.parent.mkdir(parents=True,exist_ok=True);staging.replace(target)
        for path in target.iterdir():path.chmod(0o444)
    finally:
        if staging.exists():shutil.rmtree(staging)
    return manifest


def load_chunks(artifact_hash,output_root=None):
    path=_root(output_root)/'artifacts'/'chunks'/artifact_hash/'chunks.jsonl'
    if not path.exists():raise ValueError('Unknown chunk artifact')
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line]


def verify_chunks(artifact_hash,output_root=None):
    root=_root(output_root);artifact=root/'artifacts'/'chunks'/artifact_hash
    manifest=json.loads((artifact/'manifest.json').read_text(encoding='utf-8'))
    snapshot=root/'snapshots'/manifest['snapshot_hash'];errors=[]
    chunks=load_chunks(artifact_hash,root)
    for chunk in chunks:
        raw=(snapshot/'sources'/chunk['source_name']).read_text(encoding='utf-8')
        exact=raw[chunk['source_offset_start']:chunk['source_offset_end']]
        if exact!=chunk['text']:errors.append({'chunk_id':chunk['chunk_id'],'error':'source span mismatch'})
        elif _hash(exact.encode())!=chunk['text_sha256']:
            errors.append({'chunk_id':chunk['chunk_id'],'error':'text hash mismatch'})
        for field in AUTHORITY_FIELDS:
            if field not in chunk:errors.append({'chunk_id':chunk['chunk_id'],'error':'missing authority field '+field})
    if _hash(chunks)!=manifest['chunks_sha256']:errors.append({'error':'chunk collection hash mismatch'})
    return {'passed':not errors,'errors':errors,'checked_chunks':len(chunks),
        'artifact_hash':artifact_hash,'source_snapshot_hash':manifest['snapshot_hash']}


def _tokens(text):return re.findall(r'[a-z0-9]+',text.lower())


def build_index(chunk_artifact_hash,kind='sparse',output_root=None):
    """Build an isolated sparse, dense, or hybrid index; never the operational path."""
    if kind not in ('sparse','dense','hybrid'):raise ValueError('Index kind must be sparse, dense or hybrid')
    root=_root(output_root);chunks=load_chunks(chunk_artifact_hash,root)
    chunk_manifest=json.loads((root/'artifacts'/'chunks'/chunk_artifact_hash/'manifest.json').read_text())
    model=embeddings.model_name() if kind in ('dense','hybrid') else None
    spec={'format':FORMAT,'chunk_artifact_hash':chunk_artifact_hash,'kind':kind,
        'embedding_model':model,'index_version':1}
    index_hash=_hash(spec);target=root/'artifacts'/'indexes'/index_hash
    if target.exists():return json.loads((target/'manifest.json').read_text(encoding='utf-8'))
    staging=root/'staging'/('index-'+uuid.uuid4().hex);staging.mkdir(parents=True)
    files={}
    try:
        if kind in ('sparse','hybrid'):
            postings={}
            for index,chunk in enumerate(chunks):
                counts={token:_tokens(chunk['text']).count(token) for token in set(_tokens(chunk['text']))}
                for token,count in counts.items():postings.setdefault(token,[]).append([index,count])
            sparse={'chunk_ids':[row['chunk_id'] for row in chunks],
                'postings':{key:postings[key] for key in sorted(postings)}}
            path=staging/'sparse.json';path.write_text(canonical(sparse),encoding='utf-8')
            files[path.name]=_hash(path.read_bytes())
        dimensions=None
        if kind in ('dense','hybrid'):
            matrix=np.asarray(embeddings.embed([row['text'] for row in chunks]),dtype=np.float32)
            if matrix.ndim!=2 or matrix.shape[0]!=len(chunks) or not np.isfinite(matrix).all():
                raise ValueError('Embedding output does not match chunk artifact')
            dimensions=int(matrix.shape[1]);path=staging/'dense.npz'
            np.savez_compressed(path,matrix=matrix,chunk_ids=np.array([row['chunk_id'] for row in chunks]))
            files[path.name]=_hash(path.read_bytes())
        manifest={**spec,'index_hash':index_hash,'source_snapshot_hash':chunk_manifest['snapshot_hash'],
            'chunk_count':len(chunks),'dimensions':dimensions,'files':files,'immutable':True,
            'created_at':time.time(),'operational_index_unchanged':True,
            'operational_activation_permitted':False}
        (staging/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
        target.parent.mkdir(parents=True,exist_ok=True);staging.replace(target)
        for path in target.iterdir():path.chmod(0o444)
    finally:
        if staging.exists():shutil.rmtree(staging)
    return manifest


def curation_proposal(snapshot_hash):
    """A proposal-only deterministic search space; it changes no source authority."""
    return {'format':FORMAT,'snapshot_hash':snapshot_hash,'status':'proposal_only',
        'candidate_strategies':[
            {'strategy':'fixed','settings':{'size':600,'overlap':80}},
            {'strategy':'fixed','settings':{'size':1000,'overlap':120}},
            {'strategy':'heading','settings':{}},
            {'strategy':'semantic_lexical','settings':{'size':800}},
            {'strategy':'parent_child','settings':{'size':500,'overlap':50}},
            {'strategy':'document_type','settings':{'size':800,'overlap':100}}],
        'index_candidates':['sparse','dense','hybrid'],
        'authority_fields_locked':list(AUTHORITY_FIELDS),
        'metadata_authority_changes_require_human_validation':True,
        'source_rewrite_permitted':False,'operational_activation_permitted':False}


def validate_selection(selection,architecture,output_root=None):
    """Fail closed unless an immutable artifact matches the candidate components."""
    if not isinstance(selection,dict):raise ValueError('Candidate requires an experimental retrieval artifact')
    root=_root(output_root);chunk_hash=selection.get('chunk_artifact_hash');index_hash=selection.get('index_hash')
    chunk_path=root/'artifacts'/'chunks'/str(chunk_hash)/'manifest.json'
    index_path=root/'artifacts'/'indexes'/str(index_hash)/'manifest.json'
    if not chunk_path.exists() or not index_path.exists():raise ValueError('Unknown curator chunk or index artifact')
    chunk=json.loads(chunk_path.read_text(encoding='utf-8'));index=json.loads(index_path.read_text(encoding='utf-8'))
    if index['chunk_artifact_hash']!=chunk_hash:raise ValueError('Curator index does not reference the selected chunks')
    if index['source_snapshot_hash']!=chunk['snapshot_hash']:raise ValueError('Curator source snapshot lineage mismatch')
    if not verify_chunks(chunk_hash,root)['passed']:raise ValueError('Curator chunks fail exact source-span verification')
    for name,digest in index['files'].items():
        if _hash((index_path.parent/name).read_bytes())!=digest:raise ValueError('Curator index checksum mismatch: '+name)
    components=set(architecture.get('components') or []);kind=index['kind'];strategy=chunk['strategy']
    required={'sparse':('index.sparse_lexical','retrieve.sparse_authority_filtered'),
        'dense':('embed.minilm_l6_v2','retrieve.dense_authority_filtered'),
        'hybrid':('index.hybrid_minilm_sparse','retrieve.hybrid_rrf_authority_filtered')}[kind]
    if not set(required)<=components:raise ValueError('Curator index kind does not match candidate components')
    chunk_component={'fixed':'chunk.fixed_window','heading':'chunk.heading_sections',
        'semantic_lexical':'chunk.semantic_lexical','parent_child':'chunk.parent_child',
        'document_type':'chunk.document_type'}[strategy]
    if chunk_component not in components:raise ValueError('Curator chunk strategy does not match candidate components')
    return {'format':FORMAT,'snapshot_hash':chunk['snapshot_hash'],'chunk_artifact_hash':chunk_hash,
        'index_hash':index_hash,'kind':kind,'strategy':strategy,'chunk_count':chunk['chunk_count'],
        'selection_hash':_hash({'architecture':architecture.get('configuration_hash'),
            'chunks':chunk_hash,'index':index_hash}),'operational_activation_permitted':False}


class CuratorIndex:
    """Read-only runtime adapter for isolated curator indexes."""
    def __init__(self,selection,architecture,output_root=None):
        self.root=_root(output_root);self.selection=validate_selection(selection,architecture,self.root)
        self.records=load_chunks(self.selection['chunk_artifact_hash'],self.root)
        self.chunks=[self._chunk(row) for row in self.records]
        directory=self.root/'artifacts'/'indexes'/self.selection['index_hash']
        self.kind=self.selection['kind'];self.minimum_similarity=.05;self.public_manual_similarity=.05
        self.sparse=json.loads((directory/'sparse.json').read_text(encoding='utf-8')) if self.kind in ('sparse','hybrid') else None
        self.matrix=None
        if self.kind in ('dense','hybrid'):
            with np.load(directory/'dense.npz',allow_pickle=False) as blob:self.matrix=np.asarray(blob['matrix'],dtype=np.float32)
            norms=np.linalg.norm(self.matrix,axis=1,keepdims=True);self.matrix=self.matrix/np.maximum(norms,1e-9)
    @staticmethod
    def _chunk(row):
        return qms.Chunk(chunk_id=row['chunk_id'],document_id=row['document_id'],
            procedure_id=row['procedure_id'],title=row.get('title') or row['document_id'],
            model=row['model'],applicable_revisions=list(row['applicable_revisions']),
            document_version=str(row['document_version']),approval_status=row['approval_status'],
            audiences=list(row['audiences']),section=(row.get('section_path') or 'Source span').split(' > ')[-1],
            section_path=row.get('section_path') or 'Source span',heading_level=row.get('heading_level') or 0,
            text=row['text'],source_path=row['source_name'],effective_date=row.get('effective_date'),
            superseded_by=row.get('superseded_by'),document_type=row['document_type'],
            access_permissions=list(row['access_permissions']),permitted_uses=list(row['permitted_uses']),
            scope=row['scope'],revision_scope=row['revision_scope'],owning_function=row['owning_function'],
            required_approvals=list(row['required_approvals']),safety_restrictions=list(row['safety_restrictions']),
            workflow_associations=list(row['workflow_associations']),workflow_actions=list(row['workflow_actions']),
            supersedes=row.get('supersedes'))
    def _sparse_scores(self,query):
        query_tokens=_tokens(query);scores=np.zeros(len(self.chunks),dtype=np.float32)
        lengths=np.asarray([max(1,len(_tokens(row['text']))) for row in self.records]);average=float(lengths.mean())
        for token in set(query_tokens):
            postings=self.sparse['postings'].get(token,[]);df=len(postings)
            if not df:continue
            idf=np.log(1+(len(self.chunks)-df+.5)/(df+.5))
            for index,count in postings:
                scores[index]+=idf*(count*2.2)/(count+1.2*(.25+.75*lengths[index]/average))
        maximum=float(scores.max()) if len(scores) else 0
        return scores/maximum if maximum>0 else scores
    def _dense_scores(self,query):
        vector=np.asarray(embeddings.embed(query)[0],dtype=np.float32);vector=vector/max(float(np.linalg.norm(vector)),1e-9)
        if vector.shape[0]!=self.matrix.shape[1]:raise RuntimeError('Curator query embedding dimensions differ from index')
        return self.matrix@vector
    def search(self,query,k=5):
        from .index import Hit
        if self.kind=='sparse':scores=self._sparse_scores(query)
        elif self.kind=='dense':scores=self._dense_scores(query)
        else:
            dense=self._dense_scores(query);sparse=self._sparse_scores(query)
            dr=np.argsort(-dense);sr=np.argsort(-sparse);scores=np.zeros(len(self.chunks),dtype=np.float32)
            for rank,index in enumerate(dr):scores[index]+=1/(60+rank+1)
            for rank,index in enumerate(sr):scores[index]+=1/(60+rank+1)
            scores/=max(float(scores.max()),1e-9)
        order=np.argsort(-scores)[:max(1,min(int(k),len(self.chunks)))]
        return [Hit(self.chunks[index],float(scores[index]),rank) for rank,index in enumerate(order,1)]
    def search_procedures(self,query,k=5):
        from .index import Hit
        result=[];seen=set()
        for hit in self.search(query,len(self.chunks)):
            if hit.chunk.procedure_id in seen:continue
            seen.add(hit.chunk.procedure_id);result.append(Hit(hit.chunk,hit.score,len(result)+1))
            if len(result)>=k:break
        return result


def apply_to_agent(agent,selection,architecture,output_root=None):
    index=CuratorIndex(selection,architecture,output_root)
    agent.index=index;agent.toolbox.index=index
    return index.selection
