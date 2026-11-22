"""Per-run isolated QMS corpora for controlled-document test cases.

A Testing Lab run whose scenario names a `qms_overlay` gets its own copy of
the QMS documents, the compatibility map and the retrieval index inside its
run directory, with fixture documents added. The demonstration corpus and its
index are never modified.

Index rows for unchanged chunks are reused from the run's baseline index copy.
Fixture chunks reuse the vector of the base document section they mirror
(recorded in qms-fixture.json as `vector_source`), so no embedding service is
needed and retrieval ranks the twin alongside its base document.
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

CONFLICT_STEP = ('## Alternate inlet check\n\nIf the unit still shows no power, remove the rear panel and replace the inlet fuse '
                 'with a 4 A fuse before calling support.\n')
REVISED_STEP = ('## Revised inlet check\n\nIf the unit still shows no power, do not open the unit. Note the status LED '
                'colour and contact support for a service return.\n')

OVERLAYS = {
    # Two current approved procedures answer the same question for P4800 rev A
    # and neither declares precedence: release must stop for document control.
    'p4800a-power-conflict': {
        'base_document': 'QMS-DOC-0101', 'topic': 'p4800_rev_a_no_power', 'compatibility_key': 'P4800|A',
        'twin': {'document_id': 'QMS-FIX-0901', 'procedure_id': 'PROC-P4800-PWR-A-ALT', 'document_version': '1.0',
                 'title': 'Unit Does Not Power On — P4800 Rev A (alternate approved)', 'append': CONFLICT_STEP, 'precedence_over': []}},
    # Same pair, but the newer document declares precedence over the base:
    # controlled precedence resolves it; only the authority may be released.
    'p4800a-power-precedence': {
        'base_document': 'QMS-DOC-0101', 'topic': 'p4800_rev_a_no_power', 'compatibility_key': 'P4800|A',
        'twin': {'document_id': 'QMS-FIX-0902', 'procedure_id': 'PROC-P4800-PWR-A-REV2', 'document_version': '4.0',
                 'title': 'Unit Does Not Power On — P4800 Rev A (revision 2)', 'append': REVISED_STEP, 'precedence_over': ['QMS-DOC-0101']}},
}


def _set_field(frontmatter, key, value):
    line = f'{key}: {value}'
    if re.search(rf'(?m)^{key}:.*$', frontmatter):
        return re.sub(rf'(?m)^{key}:.*$', line, frontmatter)
    return frontmatter.rstrip('\n') + '\n' + line + '\n'


def _drop_field(frontmatter, key):
    return re.sub(rf'(?m)^{key}:.*\n?', '', frontmatter)


def apply(run_dir, key):
    """Build the isolated corpus for `key` in run_dir and repoint config."""
    import numpy as np
    from . import config, index, qms, remote_services
    spec = OVERLAYS[key]
    run_dir = Path(run_dir)
    target = run_dir / 'qms'
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(config.QMS_DIR, target)
    base_path = next(p for p in target.glob('*.md') if re.search(rf'(?m)^document_id: {spec["base_document"]}\s*$', p.read_text(encoding='utf-8')))
    _, fm, body = base_path.read_text(encoding='utf-8').split('---', 2)
    base_fm = _set_field(fm, 'topic', spec['topic'])
    base_path.write_text('---' + base_fm + '---' + body, encoding='utf-8')
    twin = spec['twin']
    twin_fm = base_fm
    for field in ('document_id', 'procedure_id', 'title', 'document_version'):
        twin_fm = _set_field(twin_fm, field, json.dumps(twin[field]) if field == 'document_version' else twin[field])
    twin_fm = _drop_field(twin_fm, 'supersedes')
    if twin['precedence_over']:
        twin_fm = _set_field(twin_fm, 'precedence_over', '[' + ', '.join(twin['precedence_over']) + ']')
    (target / f'{twin["document_id"]}.md').write_text('---' + twin_fm + '---' + body.rstrip('\n') + '\n\n' + twin['append'], encoding='utf-8')

    compatibility = json.loads(Path(config.COMPATIBILITY_PATH).read_text(encoding='utf-8'))
    allowed = compatibility['compatibility'].setdefault(spec['compatibility_key'], [])
    if twin['procedure_id'] not in allowed:
        allowed.append(twin['procedure_id'])
    compat_path = run_dir / 'qms_compatibility.json'
    compat_path.write_text(json.dumps(compatibility, indent=2), encoding='utf-8')

    # Retrieval index aligned to the isolated corpus.
    baseline = np.load(config.INDEX_PATH, allow_pickle=False)
    baseline_meta = json.loads(str(baseline['meta']))
    rows = {c['chunk_id']: i for i, c in enumerate(baseline_meta['chunks'])}
    chunks = qms.load_chunks(str(target))
    base_sections = [c for c in chunks if c.document_id == spec['base_document']]
    matrix, vector_source = [], {}
    for chunk in chunks:
        if chunk.chunk_id in rows:
            matrix.append(baseline['matrix'][rows[chunk.chunk_id]])
            continue
        section = int(re.search(r'#S(\d+)-', chunk.chunk_id).group(1))
        mirror = base_sections[section - 1] if section <= len(base_sections) else base_sections[0]
        matrix.append(baseline['matrix'][rows[mirror.chunk_id]])
        vector_source[chunk.chunk_id] = mirror.chunk_id
    previous_qms, previous_compat = config.QMS_DIR, config.COMPATIBILITY_PATH
    config.QMS_DIR, config.COMPATIBILITY_PATH = str(target), str(compat_path)
    index_path = run_dir / 'qms-fixture-index.npz'
    index.VectorIndex(chunks, np.asarray(matrix, dtype=baseline['matrix'].dtype)).save(str(index_path))
    config.INDEX_PATH = str(index_path)
    # The run must read its own corpus, never a remote QMS service.
    remote_disabled = remote_services.configured('qms')
    if remote_disabled:
        import os
        os.environ.pop(remote_services.CONFIG_ENVS['qms'], None)
    report = {'overlay': key, 'qms_dir': str(target), 'compatibility_path': str(compat_path), 'index_path': str(index_path),
              'base_document': spec['base_document'], 'topic': spec['topic'], 'fixture_document': twin['document_id'],
              'fixture_procedure': twin['procedure_id'], 'precedence_over': twin['precedence_over'],
              'vector_source': vector_source, 'remote_qms_disabled_for_run': remote_disabled,
              'demo_corpus_unchanged': {'qms_dir': previous_qms, 'compatibility_path': previous_compat}}
    (run_dir / 'qms-fixture.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    return report
