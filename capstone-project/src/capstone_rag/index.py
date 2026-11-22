"""A small persistent vector index over the QMS chunks.

Exact (brute-force) cosine nearest-neighbour search over L2-normalised
embeddings. With a corpus this size that is the same answer a FAISS
IndexFlatIP would give, without the extra dependency - the search is a single
matrix product, not an approximation and not a keyword match.

The index stores the embedding matrix plus each chunk's metadata, so a rebuild
is only needed when the QMS corpus changes.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass

import numpy as np

from . import config, embeddings, qms


@dataclass
class Hit:
    chunk: qms.Chunk
    score: float
    rank: int


def corpus_fingerprint(chunks) -> str:
    h = hashlib.sha256()
    h.update(embeddings.model_name().encode())
    for c in chunks:
        h.update(c.chunk_id.encode())
        h.update(c.embed_text.encode())
        h.update(json.dumps(c.as_dict(), sort_keys=True).encode())
    return h.hexdigest()[:16]


class VectorIndex:
    def __init__(self, chunks, matrix):
        self.chunks = chunks
        self.matrix = matrix

    # -- build / persist ----------------------------------------------------

    @classmethod
    def build(cls, chunks=None, verbose: bool = False):
        chunks = chunks if chunks is not None else qms.load_chunks()
        if verbose:
            print(f"  embedding {len(chunks)} chunks with "
                  f"{embeddings.model_name()} ...")
        matrix = embeddings.embed([c.embed_text for c in chunks])
        return cls(chunks, matrix)

    def save(self, path=None):
        path = path or config.INDEX_PATH
        os.makedirs(os.path.dirname(path), exist_ok=True)
        meta = json.dumps(
            {
                "model": embeddings.model_name(),
                "fingerprint": corpus_fingerprint(self.chunks),
                "chunks": [
                    {
                        **c.as_dict(),
                        "text": c.text,
                        "source_path": c.source_path,
                        "effective_date": c.effective_date,
                        "superseded_by": c.superseded_by,
                    }
                    for c in self.chunks
                ],
            }
        )
        np.savez_compressed(path, matrix=self.matrix, meta=np.array(meta))
        return path

    @classmethod
    def load_or_build(cls, path=None, verbose: bool = False):
        """Load a cached index, rebuilding if the corpus or model changed."""
        path = path or config.INDEX_PATH
        chunks = qms.load_chunks()
        want = corpus_fingerprint(chunks)

        if os.path.exists(path):
            try:
                blob = np.load(path, allow_pickle=False)
                meta = json.loads(str(blob["meta"]))
                if meta.get("fingerprint") == want:
                    if verbose:
                        print(f"  loaded cached index ({len(chunks)} chunks)")
                    return cls(chunks, blob["matrix"])
                if verbose:
                    print("  cached index is stale; rebuilding")
            except Exception as exc:  # corrupt cache should never be fatal
                if verbose:
                    print(f"  could not read cached index ({exc}); rebuilding")

        idx = cls.build(chunks, verbose=verbose)
        idx.save(path)
        return idx

    # -- search -------------------------------------------------------------

    def search(self, query: str, k: int = 5) -> list:
        """Pure semantic search. No metadata filtering happens here."""
        qvec = embeddings.embed(query)[0]
        scores = self.matrix @ qvec
        order = np.argsort(-scores)[:k]
        return [
            Hit(chunk=self.chunks[i], score=float(scores[i]), rank=r)
            for r, i in enumerate(order, start=1)
        ]

    def search_procedures(self, query: str, k: int = 5) -> list:
        """Return the best semantic chunk from each distinct procedure.

        Diversity selection stops sections from one long document hiding
        competing procedures. It is not an applicability filter: every chunk
        is scored first and deterministic validation remains authoritative.
        """
        ranked = self.search(query, k=len(self.chunks))
        hits, seen = [], set()
        for hit in ranked:
            procedure = hit.chunk.procedure_id
            if procedure in seen:
                continue
            seen.add(procedure)
            hits.append(Hit(chunk=hit.chunk, score=hit.score,
                            rank=len(hits) + 1))
            if len(hits) >= k:
                break
        return hits
