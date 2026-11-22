import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from capstone_rag import config
from capstone_rag.document_curator import (
    AUTHORITY_FIELDS, CuratorIndex, build_chunks, build_index, create_snapshot,
    curation_proposal, load_chunks, validate_selection, verify_chunks,
)
from capstone_rag.experimental_architecture import template


SOURCE='''---
document_id: DOC-TEST
procedure_id: PROC-TEST
title: Controlled test
model: MODEL-X
applicable_revisions: [A]
document_version: 2.0
approval_status: Approved
audiences: [customer, representative]
document_type: customer_user_manual
access_permissions: [customer_disclosable, agent_internal]
permitted_uses: [customer_answer]
---

## Warning

> WARNING: Stop if the enclosure is damaged.

| Signal | Meaning |
| --- | --- |
| Red | Stop |

## Procedure

Disconnect the cable and wait. Reconnect only after inspection.

### Completion

Record the result without changing the approved instruction.
'''


def corpus(tmp_path):
    source=tmp_path/'qms';source.mkdir();(source/'controlled.md').write_text(SOURCE,encoding='utf-8')
    return source


def test_snapshot_and_chunks_are_immutable_exact_source_spans(tmp_path):
    source=corpus(tmp_path);root=tmp_path/'curator'
    snapshot=create_snapshot(source,root)
    again=create_snapshot(source,root)
    assert again['snapshot_hash']==snapshot['snapshot_hash'] and snapshot['immutable']
    artifact=build_chunks(snapshot['snapshot_hash'],'fixed',{'size':200,'overlap':30},root)
    assert artifact['operational_activation_permitted'] is False
    chunks=load_chunks(artifact['artifact_hash'],root)
    copied=(root/'snapshots'/snapshot['snapshot_hash']/'sources'/'controlled.md').read_text(encoding='utf-8')
    for chunk in chunks:
        assert chunk['text']==copied[chunk['source_offset_start']:chunk['source_offset_end']]
        assert set(AUTHORITY_FIELDS)<=set(chunk)
        assert chunk['approval_status']=='Approved'
        assert chunk['access_permissions']==['customer_disclosable','agent_internal']
    assert verify_chunks(artifact['artifact_hash'],root)['passed']


def test_structural_parent_child_and_semantic_strategies_preserve_warnings_and_tables(tmp_path):
    root=tmp_path/'curator';snapshot=create_snapshot(corpus(tmp_path),root)
    heading=build_chunks(snapshot['snapshot_hash'],'heading',{},root)
    heading_text='\n'.join(row['text'] for row in load_chunks(heading['artifact_hash'],root))
    assert '> WARNING:' in heading_text and '| Signal | Meaning |' in heading_text
    parent=build_chunks(snapshot['snapshot_hash'],'parent_child',{'size':200,'overlap':20},root)
    rows=load_chunks(parent['artifact_hash'],root)
    assert {'parent','child'}=={row['relationship'] for row in rows}
    assert all(row['parent_source_span'] for row in rows if row['relationship']=='child')
    semantic=build_chunks(snapshot['snapshot_hash'],'semantic_lexical',{'size':220},root)
    assert verify_chunks(semantic['artifact_hash'],root)['passed']


def test_indexes_are_isolated_content_addressed_and_never_promoted(tmp_path,monkeypatch):
    root=tmp_path/'curator';snapshot=create_snapshot(corpus(tmp_path),root)
    chunks=build_chunks(snapshot['snapshot_hash'],'heading',{},root)
    operational=tmp_path/'approved-index.npz';operational.write_bytes(b'approved')
    before=hashlib.sha256(operational.read_bytes()).hexdigest()
    sparse=build_index(chunks['artifact_hash'],'sparse',root)
    assert sparse['operational_index_unchanged'] and not sparse['operational_activation_permitted']
    assert (root/'artifacts'/'indexes'/sparse['index_hash']/'sparse.json').exists()
    monkeypatch.setattr('capstone_rag.document_curator.embeddings.model_name',lambda:'fixture-embedding')
    monkeypatch.setattr('capstone_rag.document_curator.embeddings.embed',
        lambda texts:np.asarray([[len(text),text.count('Warning'),1] for text in texts],dtype=np.float32))
    hybrid=build_index(chunks['artifact_hash'],'hybrid',root)
    assert set(hybrid['files'])=={'sparse.json','dense.npz'}
    assert hashlib.sha256(operational.read_bytes()).hexdigest()==before


def test_curator_proposals_cannot_rewrite_or_change_authority(tmp_path):
    snapshot=create_snapshot(corpus(tmp_path),tmp_path/'curator')
    proposal=curation_proposal(snapshot['snapshot_hash'])
    assert proposal['status']=='proposal_only'
    assert proposal['source_rewrite_permitted'] is False
    assert proposal['operational_activation_permitted'] is False
    assert proposal['metadata_authority_changes_require_human_validation']


def test_sparse_artifact_is_exercised_by_candidate_b_runtime(tmp_path):
    root=tmp_path/'curator';snapshot=create_snapshot(corpus(tmp_path),root)
    chunks=build_chunks(snapshot['snapshot_hash'],'fixed',{'size':200,'overlap':30},root)
    index=build_index(chunks['artifact_hash'],'sparse',root)
    architecture=template('candidate-b-direct')
    selected=validate_selection({'chunk_artifact_hash':chunks['artifact_hash'],
        'index_hash':index['index_hash']},architecture,root)
    runtime=CuratorIndex(selected,architecture,root)
    hits=runtime.search('damaged enclosure warning',3)
    assert hits and hits[0].chunk.document_id=='DOC-TEST'
    assert hits[0].chunk.approval_status=='Approved'
    assert runtime.kind=='sparse' and selected['strategy']=='fixed'


def test_candidate_retrieval_selection_rejects_component_mismatch(tmp_path):
    root=tmp_path/'curator';snapshot=create_snapshot(corpus(tmp_path),root)
    chunks=build_chunks(snapshot['snapshot_hash'],'heading',{},root)
    index=build_index(chunks['artifact_hash'],'sparse',root)
    with pytest.raises(ValueError,match='candidate components|chunk strategy'):
        validate_selection({'chunk_artifact_hash':chunks['artifact_hash'],
            'index_hash':index['index_hash']},template('candidate-b-direct'),root)
