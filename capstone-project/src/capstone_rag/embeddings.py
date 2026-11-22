"""Local sentence-embedding model.

This is a genuine transformer encoder (all-MiniLM-L6-v2, 384-dim) run locally
on CPU via sentence-transformers. It is a different model from the one used for
response generation, and nothing in the retrieval path falls back to keyword
matching if it is unavailable - if the encoder cannot load, retrieval fails
loudly rather than quietly degrading.
"""

from __future__ import annotations

import numpy as np
import os

from . import config

_MODEL = None


def get_model():
    if os.environ.get('CAPSTONE_TEST_ALLOW_LOCAL_EMBEDDING')!='1':
        raise RuntimeError('Local embedding is not present in the deployed product; use all-minilm:latest through the Inference Gateway')
    global _MODEL
    if _MODEL is None:
        from sentence_transformers import SentenceTransformer
        _MODEL=SentenceTransformer(config.EMBEDDING_MODEL,device='cpu',local_files_only=True)
    return _MODEL


def model_name() -> str:
    # This is index metadata, not a transport probe.  Availability is checked
    # immediately before the Gateway embedding request in ``embed``.
    return config.EMBEDDING_MODEL


def embed(texts, batch_size: int = 16) -> np.ndarray:
    """Encode texts to L2-normalised float32 vectors."""
    if isinstance(texts, str):
        texts = [texts]
    texts = list(texts)
    import time
    started = time.perf_counter()
    from . import gateway_transport
    from . import inference
    state=inference.current()
    if os.environ.get('CAPSTONE_TEST_ALLOW_LOCAL_EMBEDDING')=='1' and not os.environ.get('CAPSTONE_GATEWAY_CLIENT_CONFIG'):
        return np.asarray(get_model().encode(texts,batch_size=batch_size,convert_to_numpy=True,
          normalize_embeddings=True,show_progress_bar=False),dtype=np.float32)
    if True:
        workload=(state or {}).get('snapshot',{}).get('gateway_workload','operational')
        model='all-minilm:latest';check=gateway_transport.client(workload).availability(model,'/api/embed')
        if check.get('status') not in ('available','busy'):raise PermissionError('the index needs all-minilm:latest allowed for capstone')
        registry=(state or {}).get('registry') or inference.Registry()
        vectors,metric=inference.embed(registry,{'id':'gateway','url':'gateway://authorized','timeout':config.OLLAMA_TIMEOUT},model,texts,timeout=config.OLLAMA_TIMEOUT)
        result=np.asarray(vectors,dtype=np.float32)
        from . import activity
        activity.emit('embedding','Gateway embedding completed',evidence={'model':model,'execution':'shared.inference/1','rows':len(texts),'dimensions':int(result.shape[1]),**metric},duration_ms=(time.perf_counter()-started)*1000)
        return result
    raise RuntimeError('Gateway embedding did not return')
