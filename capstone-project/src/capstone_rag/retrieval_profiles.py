"""Separate experimental vectors. MiniLM stays untouched and remains the default."""
import hashlib,json,time,uuid
from pathlib import Path
import numpy as np
from . import config,qms,qms_policy,inference
from .ai_settings import canonical,fingerprint

def corpus_hash(chunks):return fingerprint([{'metadata':c.as_dict(),'embed_text':c.embed_text} for c in chunks])
def normalize(matrix):
    result=np.asarray(matrix,dtype=np.float32)
    if result.ndim!=2 or not np.isfinite(result).all() or np.any(np.linalg.norm(result,axis=1)<1e-9):raise ValueError('Invalid embedding matrix')
    return result/np.linalg.norm(result,axis=1,keepdims=True)
def build(registry,profile_id='qwen3-experimental'):
    profile=registry.get('retrieval_profiles',profile_id)
    if not profile or profile_id=='minilm':raise ValueError('Only a separate experimental profile may be built')
    endpoint=registry.get('endpoints',profile['endpoint']);chunks=qms.load_chunks();batches=[];metrics=[]
    for offset in range(0,len(chunks),12):
        vectors,metric=inference.embed(registry,endpoint,profile['model'],[c.embed_text for c in chunks[offset:offset+12]])
        if len(vectors)!=len(chunks[offset:offset+12]):raise ValueError('Embedding response row count differs from corpus batch')
        batches.extend(vectors);metrics.append(metric)
    matrix=normalize(batches);digest=metrics[0]['model_digest']
    if any(m['model_digest']!=digest for m in metrics):raise ValueError('Embedding model changed during build')
    build_id='qwen3-'+uuid.uuid4().hex;directory=Path(config.DATA_DIR)/'index/profiles';directory.mkdir(parents=True,exist_ok=True)
    path=directory/(build_id+'.npz')
    metadata={'id':profile_id,'build_id':build_id,'model':profile['model'],'model_digest':digest,'dimensions':int(matrix.shape[1]),'corpus_hash':corpus_hash(chunks),'chunking_strategy':'Existing QMS heading sections and embed_text; unchanged from baseline','build_version':1,'chunk_ids':[c.chunk_id for c in chunks],'built_at':time.time(),'metrics':metrics}
    np.savez_compressed(path,matrix=matrix,metadata=canonical(metadata))
    # Small, explicit retrieval relevance probes, kept separate from the corpus
    # and from the customer prompt. They do not establish broad quality claims.
    probes=[];positive=[]
    manuals={c.model:c for c in chunks if c.document_type=='customer_user_manual' and c.approval_status=='Approved' and c.model!='ALL'}
    for model,c in manuals.items():
        query=f'How do I read the indicators and find the user guide for my {model}?'
        vector,metric=inference.embed(registry,endpoint,profile['model'],[query],expected_digest=digest)
        scores=matrix@normalize(vector)[0]
        context={'model':model,'revision':'A',
                 'revision_context':{'status':'controlled_probe',
                    'usable_for_troubleshooting':True,
                    'reason':'controlled retrieval-profile validation fixture'},
                 'compatibility':qms.load_compatibility()}
        accepted=[i for i in np.argsort(-scores) if chunks[i].document_type=='customer_user_manual' and qms_policy.authorize(chunks[i],operation='customer_answer',recipient='customer',context=context).allowed]
        selected=chunks[accepted[0]] if accepted else None
        passed=bool(selected and selected.model==model)
        if passed:positive.append(float(scores[accepted[0]]))
        denials=sum(not qms_policy.authorize(x,operation='customer_answer',recipient='customer',context=context).allowed for x in chunks)
        probes.append({'query':query,'expected_model':model,'selected_chunk':selected.chunk_id if selected else None,'correct_applicability':passed,'unauthorized_candidates_rejected':denials,'top_authorized_similarity':positive[-1] if passed else None})
    validated=bool(probes) and all(p['correct_applicability'] for p in probes)
    threshold=max(.12,min(.45,min(positive)-.08)) if positive else .45
    metadata.update(validation={'passed':validated,'probes':probes,'scope':'Small public-manual applicability probes, not broad held-out retrieval quality'},thresholds={'minimum_similarity':threshold,'public_manual_similarity':threshold,'max_relative_score_gap':.15})
    metadata_path=path.with_suffix('.json');metadata_path.write_text(json.dumps(metadata,indent=2)+'\n',encoding='utf-8')
    value={**profile,'path':str(path),'index_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'metadata_path':str(metadata_path),'enabled':False,'validated':validated,**{k:metadata[k] for k in ('build_id','model_digest','dimensions','corpus_hash','thresholds','validation')}}
    registry.put('retrieval_profiles',value)
    return value
class ExperimentalIndex:
    def __init__(self,profile,registry,endpoint):
        self.profile=profile;self.registry=registry;self.endpoint=endpoint;self.chunks=qms.load_chunks()
        if hashlib.sha256(Path(profile['path']).read_bytes()).hexdigest()!=profile.get('index_sha256'):raise RuntimeError('Experimental index checksum mismatch; rebuild before use')
        if corpus_hash(self.chunks)!=profile['corpus_hash']:raise RuntimeError('Experimental retrieval corpus changed; rebuild before use')
        with np.load(profile['path'],allow_pickle=False) as blob:
            self.matrix=blob['matrix'];metadata=json.loads(str(blob['metadata']))
        if metadata['model_digest']!=profile['model_digest'] or self.matrix.shape!=(len(self.chunks),profile['dimensions']):raise RuntimeError('Experimental index metadata mismatch')
    def search(self,query,k=5):
        from .index import Hit
        vectors,_=inference.embed(self.registry,self.endpoint,self.profile['model'],[query],expected_digest=self.profile['model_digest'])
        vector=normalize(vectors)[0]
        if vector.shape[0]!=self.matrix.shape[1]:raise RuntimeError('Query embedding dimensions differ from this index')
        scores=self.matrix@vector
        return [Hit(self.chunks[i],float(scores[i]),rank) for rank,i in enumerate(np.argsort(-scores)[:k],1)]
class ProfileIndex:
    def __init__(self,baseline):self.baseline=baseline;self.chunks=baseline.chunks;self.cache={}
    @property
    def matrix(self):return self.selected().matrix
    def selected(self):
        state=inference.current();profile=state['snapshot'].get('retrieval') if state else None
        if not profile or profile['id']=='minilm':return self.baseline
        if not profile.get('enabled') or not profile.get('validated'):raise RuntimeError('Selected experimental retrieval profile is disabled or unvalidated')
        key=profile['build_id']
        if key not in self.cache:self.cache[key]=ExperimentalIndex(profile,state['registry'],state['snapshot']['endpoints'][profile['endpoint']])
        return self.cache[key]
    @property
    def minimum_similarity(self):
        state=inference.current();return (state['snapshot'].get('retrieval',{}).get('thresholds',{}).get('minimum_similarity',config.MIN_SIMILARITY)) if state else config.MIN_SIMILARITY
    @property
    def public_manual_similarity(self):
        state=inference.current();return state['snapshot'].get('retrieval',{}).get('thresholds',{}).get('public_manual_similarity',.12) if state else .12
    def search(self,query,k=5):return self.selected().search(query,k)
    def search_procedures(self,query,k=5):
        from .index import Hit
        seen=set();result=[]
        for hit in self.search(query,len(self.chunks)):
            if hit.chunk.procedure_id not in seen:
                seen.add(hit.chunk.procedure_id);result.append(Hit(hit.chunk,hit.score,len(result)+1))
            if len(result)>=k:break
        return result
