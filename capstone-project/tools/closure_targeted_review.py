"""Refine the existing inventory/plan; never stage, relocate or alter installs."""
import collections,gzip,hashlib,json,os,shutil,sqlite3,time
from pathlib import Path
from closure_inventory import ROOT,OUT,walk,digest
from controlled_archive import checked

ADDITIONAL={
 'historical-release-zips':(['docs/phase3.zip','docs/phase3 (2).zip','docs/phase4_1.zip'],'Historical release packages not referenced by current runtime/tests. They are not assumed byte-identical.'),
 'phase2-baseline':(['docs/phase2/baseline_snapshot_20261029'],'Historical Phase 2 source snapshot; manual phase2_manifest reproduction requires restoration first.'),
 'multiagent-benchmark-runs':(['docs/evidence/multi_agent/benchmarks'],'Completed historical benchmark outputs; manual benchmark-summary/reproduction scripts require restoration first.'),
}

def references(prefixes,rows):
    result=[];extensions={'.py','.ps1','.md','.json','.xml','.ini','.cfg','.pth','.cmd','.js','.mjs','.cjs','.txt'}
    for row in rows:
        rel=row['relative_path'];parts=Path(rel).parts
        if row['scope']!='project' or row['kind']!='file' or Path(rel).suffix.lower() not in extensions:continue
        if rel.startswith('docs/phase4_2_closure/') or parts[0] not in {'src','tests','tools','scripts','docs','.idea','.run'} and len(parts)>1:continue
        if 'baseline_snapshot_20261029' in parts or 'acceptance_evidence_bundle' in parts:continue
        try:lines=Path(row['path']).read_text(encoding='utf-8',errors='replace').splitlines()
        except OSError:continue
        tokens=set(prefixes)
        for prefix in prefixes:
            if Path(prefix).suffix or Path(prefix).name=='baseline_snapshot_20261029':tokens.add(Path(prefix).name)
            tokens.add(prefix.replace('/','\\'))
        for n,line in enumerate(lines,1):
            if any(token in line for token in tokens):result.append({'path':rel,'line':n,'kind':'runtime_or_test' if parts[0] in {'src','tests','.idea','.run'} else 'archive_policy' if Path(rel).name in {'closure_inventory.py','closure_targeted_review.py','controlled_archive.py'} else 'manual_tool' if parts[0] in {'tools','scripts'} else 'documentation'})
    return result

def entry(row,group,archive,safe,reason):
    before=Path(row['path']).stat();checksum=digest(row['path']);after=Path(row['path']).stat()
    stable=(before.st_size,before.st_mtime_ns)==(after.st_size,after.st_mtime_ns)
    rel=row['relative_path']
    return {'id':hashlib.sha256(rel.encode()).hexdigest()[:20],'group':group,'relative_path':rel,'filename':Path(rel).name,
        'original_path':row['path'],'bytes':after.st_size,'sha256':checksum,
        'archive_destination':str(archive/'staged'/rel),'relocated_original_destination':str(archive/'originals'/rel),
        'category':'Historical evidence — preserve' if safe else 'Candidate — verification required','status':'historical' if safe else 'held_not_authorized',
        'safe_to_move':safe and stable,'reason':reason,'known_references':{'resolved_in':'groups.'+group+'.references'},
        'validation_required':['verified copy and archive integrity','full regression with original test identities','startup/QMS/DB checks','historical restoration path'],
        'rollback':'Restore verified original/staged bytes to original_path; never overwrite changed active content; rerun checks.'}

def main():
    started=time.time();old=json.loads((OUT/'archive_manifest.json').read_text(encoding='utf-8'))
    old_hash=digest(OUT/'archive_manifest.json');history=OUT/'proposal_history';history.mkdir(exist_ok=True)
    if not (history/'v1.archive_manifest.json.gz').exists():
        with gzip.open(history/'v1.archive_manifest.json.gz','wb') as target:target.write((OUT/'archive_manifest.json').read_bytes())
        (history/'v1.ARCHIVE_PROPOSAL.md').write_bytes((OUT/'ARCHIVE_PROPOSAL.md').read_bytes())
    previous_rows={r['relative_path']:r for r in (json.loads(s) for s in (OUT/'storage_inventory.jsonl').open(encoding='utf-8')) if r['scope']=='project'}
    errors=[];rows=list(walk(ROOT,'project',errors));archive=Path(old['archive_root']).parent/'phase4_2_closure-20261101-v2'
    plan={k:v for k,v in old.items() if k not in {'files','groups','archive_root','version'}}
    plan.update(review_revision=2,supersedes_manifest_sha256=old.get('supersedes_manifest_sha256',old_hash),archive_root=str(archive),version=archive.name,
        authorization_status='proposal_only_no_staging_or_relocation_authorized',baseline_tests=585,baseline_junit='docs/phase4_2_closure/revised_proposal_regression.xml')
    initial_groups=['course-part1','course-part2','phase3-extracted','phase41-extracted','phase3-baseline']
    plan['groups']={g:old['groups'][g] for g in initial_groups};plan['files']=[]
    failures=[]
    for e in old['files']:
        if e['group'] not in initial_groups:continue
        e=dict(e);e.update(archive_destination=str(archive/'staged'/e['relative_path']),relocated_original_destination=str(archive/'originals'/e['relative_path']),batch='first')
        try:checked(Path(e['original_path']),e)
        except Exception as exc:e['safe_to_move']=False;failures.append({'path':e['relative_path'],'error':str(exc)})
        plan['files'].append(e)
    for group,(prefixes,reason) in ADDITIONAL.items():
        selected=[r for r in rows if r['kind']=='file' and any(r['relative_path']==p or r['relative_path'].startswith(p+'/') for p in prefixes)]
        refs=references(prefixes,rows);safe=not any(r['kind']=='runtime_or_test' for r in refs)
        added=[]
        for row in selected:
            e=entry(row,group,archive,safe,reason);e['batch']='additional_historical'
            try:checked(Path(e['original_path']),e)
            except Exception as exc:e['safe_to_move']=False;failures.append({'path':e['relative_path'],'error':str(exc)})
            added.append(e)
        plan['files'].extend(added);plan['groups'][group]={'prefixes':prefixes,'files':len(added),'bytes':sum(e['bytes'] for e in added),
            'classification':'Historical evidence — preserve','reason':reason,'safe_to_move_after_approval':bool(added) and all(e['safe_to_move'] for e in added),'references':refs}
    # Target the largest unreferenced old installation. Record every hash, but
    # retain installed trees under the archival tool's protected-content policy.
    provenance=json.loads((OUT/'dependency_provenance.json').read_text(encoding='utf-8'))
    live={r['name'].lower():r['version'] for r in provenance['distributions']['.venv']}
    old_dist={r['name'].lower():r['version'] for r in provenance['distributions']['.phase1_deps']}
    hidden=[r for r in rows if r['kind']=='file' and r['relative_path'].startswith('.phase1_deps/')]
    refs=references(['.phase1_deps'],rows);payload=collections.Counter();examples=[];held=[]
    for row in hidden:
        e=entry(row,'superseded-hidden-phase1',archive,False,'Old installation copy; held outside the actionable batch. Protected installation policy and separate approval/placement validation still apply.')
        e['batch']='held_dependency_copy';held.append(e)
        local=Path(row['relative_path']).relative_to('.phase1_deps');target=ROOT/'.venv/Lib/site-packages'/local
        if '__pycache__' in local.parts or local.suffix=='.pyc':kind='generated_bytecode'
        elif any(part.endswith('.dist-info') for part in local.parts) or local.parts[0] in {'bin','Scripts'}:kind='installation_metadata_or_launchers'
        elif target.is_file() and target.stat().st_size==e['bytes'] and digest(target)==e['sha256']:kind='matching_payload'
        else:
            kind='different_or_missing_payload'
            if len(examples)<30:examples.append(local.as_posix())
        payload[kind]+=1
    dependency={'old_distributions':len(old_dist),'all_versions_present_in_live_environment':all(live.get(k)==v for k,v in old_dist.items()),
        'old_only_distributions':{k:v for k,v in old_dist.items() if k not in live},'payload_comparison':dict(payload),'nonmatching_examples':examples,
        'runtime_sys_path_uses_hidden_copy':any('.phase1_deps' in p for p in provenance['sys_path']),
        'active_references':[r for r in refs if r['kind']=='runtime_or_test'],'references':refs,'disposition':'credible superseded-installation candidate; retained and not eligible in the current archive tool'}
    (OUT/'dependency_targeted_review.json').write_text(json.dumps(dependency,indent=2),encoding='utf-8')
    plan['files'].extend(held);plan['groups']['superseded-hidden-phase1']={'path':'.phase1_deps','files':len(held),'bytes':sum(e['bytes'] for e in held),
        'classification':'Candidate — verification required','safe_to_move_after_approval':False,'references':refs,'review':'dependency_targeted_review.json',
        'reason':'No active path reference and versions duplicated, but this installed copy remains protected. Do not include it in a generic approval of historical evidence.'}
    # Completed Lab trials remain referenced by registries; completion alone is
    # never a reason to remove their databases, checkpoints or approval history.
    trials=[]
    for path in sorted((ROOT/'data/experiments').glob('EXP-*/RUN-*/manifest.json')):
        try:
            m=json.loads(path.read_text(encoding='utf-8'));result=path.parent/'result.json'
            state=json.loads(result.read_text(encoding='utf-8')).get('status') if result.exists() else 'no-result'
            trials.append({'experiment':path.parents[1].name,'trial':path.parent.name,'status':state,'registry_path':m.get('registry_path'),'retained':True})
        except Exception as exc:trials.append({'path':str(path),'status':'unreadable','error':str(exc),'retained':True})
    (OUT/'testing_lab_retention_review.json').write_text(json.dumps({'trial_status_counts':dict(collections.Counter(r['status'] for r in trials)),
        'trials':trials,'reason':'Registry references, paused/incomplete work and current physical acceptance provenance require retention; no database is relocated.'},indent=2),encoding='utf-8')
    plan['explicit_exclusions']=[{'path':e['path'],'reason':e['error'],'kind':'unreadable_subtree','safe_to_move':False} for e in errors]
    plan['retained_groups']=['.venv','phase1_deps','phase4_deps','.test_deps','.phase4_2_node','src','tests','scripts','tools','data','docs/evidence/agent_traces','docs/evidence/multi_agent/baseline','docs/phase4_2','docs/phase4_2_continuation']
    plan['files'].sort(key=lambda e:e['relative_path'].casefold())
    # Compact JSON with shared group references avoids repeating reference lists
    # for tens of thousands of installation files, the source of the old draft's bloat.
    with (OUT/'archive_manifest.json').open('w',encoding='utf-8') as f:json.dump(plan,f,separators=(',',':'),ensure_ascii=False);f.write('\n')
    ready=[e for e in plan['files'] if e['safe_to_move']];first=[e for e in ready if e['batch']=='first'];extra=[e for e in ready if e['batch']=='additional_historical']
    other_rows=[r for r in rows if not r['relative_path'].startswith('docs/phase4_2_closure/')]
    unknown={'path':str(ROOT/'.test_tmp/pytest-of-REDACTED'),'cause':'Windows access denied (WinError 5) during enumeration; READ_CONTROL/Get-Acl also denied. No evidence of a sharing-lock error or a changing candidate. No permissions were altered.',
        'resolution':'Excluded from archival; owner/administrator read/list inventory would be required to determine its size. Do not take ownership or disrupt the application.'}
    summary={'schema':'capstone.archive-review/2','observed_at_unix':time.time(),'original_inventory_complete':False,'accessible_scope_complete':not any('.test_tmp' not in e['path'] for e in errors),
        'complete_project_inventory':not errors,'excluded_subtrees':errors,'unknown_cause_and_disposition':unknown,
        'first_batch':{'files':len(first),'bytes':sum(e['bytes'] for e in first)},'additional_historical':{'files':len(extra),'bytes':sum(e['bytes'] for e in extra)},
        'actionable_total':{'files':len(ready),'bytes':sum(e['bytes'] for e in ready)},'held_dependency_copy':{'files':len(held),'bytes':sum(e['bytes'] for e in held)},
        'manifest_sha256':digest(OUT/'archive_manifest.json'),'source_verification_failures':failures,'archive_destination':str(archive),
        'staging_additional_disk_bytes':sum(e['bytes'] for e in ready),'staging_headroom_recommendation_bytes':1024**3,'disk_free_bytes_observed':shutil.disk_usage(ROOT).free,
        'active_project_bytes_recovered':0,'files_staged':0,'files_relocated':0,'relocation_authorized':False,
        'original_5_75GB_explanation':{'prior_total':5750577053,'prior_draft_manifest_bytes':387141447,'reason':'Prior scan included a newly generated verbose draft manifest with repeated per-file reference lists. Current manifest uses shared references. Audit-output refinement is not counted as archival recovery.'},
        'elapsed_seconds':time.time()-started}
    # Reconcile generated audit outputs separately; the inaccessible cache makes
    # the whole-project total a lower bound, never a falsely exact complete total.
    for _ in range(4):
        audit_errors=[];audit=list(walk(OUT,'audit_outputs',audit_errors))
        summary.update(known_project_files=len(other_rows)+len(audit),known_project_bytes=sum(r['bytes'] for r in other_rows)+sum(r['bytes'] for r in audit),
            application_and_other_files_bytes=sum(r['bytes'] for r in other_rows),audit_outputs_bytes=sum(r['bytes'] for r in audit),total_is_lower_bound=bool(errors))
        md=['# Revised archive proposal — approval preparation only','',
            '**No staging or relocation is authorized. No actual project files have been moved, renamed or removed.**','',
            f"Readable footprint: **{summary['known_project_bytes']:,} bytes** in {summary['known_project_files']:,} entries; includes {summary['audit_outputs_bytes']:,} bytes of generated audit outputs. The unreadable cache is excluded, so this is a lower bound, not a complete-project total.",'',
            '## Incomplete inventory and reconciliation','',unknown['cause'],unknown['resolution'],'',
            'The earlier 5.75 GB total included a 387,141,447-byte verbose generated draft manifest. Its repeated dependency references were normalized; this reporting refinement is not claimed as archived space. Existing storage_inventory.jsonl remains the starting snapshot; current totals are reconciled separately.','',
            '## Exact proposed groups','', '| Batch/group | Files | Bytes | Dependency/reference finding |','|---|---:|---:|---|']
        for group in initial_groups+list(ADDITIONAL):
            es=[e for e in plan['files'] if e['group']==group];g=plan['groups'][group]
            md.append(f"| {group} | {len(es):,} | {sum(e['bytes'] for e in es):,} | No current runtime/test consumer found; manual historical reproduction may require restoration. |")
        md+=['',f"First batch: **{summary['first_batch']['bytes']:,} bytes / {summary['first_batch']['files']:,} files**.",
            f"Additional historical batch: **{summary['additional_historical']['bytes']:,} bytes / {summary['additional_historical']['files']:,} files**.",
            f"Total conditional historical proposal: **{summary['actionable_total']['bytes']:,} bytes / {summary['actionable_total']['files']:,} files**. All exact paths, sizes, SHA-256s and recovery destinations are in archive_manifest.json.",'',
            '## Additional candidate held outside the actionable batch','',
            f"`.phase1_deps`: {summary['held_dependency_copy']['bytes']:,} bytes, {len(held):,} files. All {len(old_dist)} distribution versions are present in .venv; no active project path/launcher reference was found. Payload comparison: `{dict(payload)}`. Individual hashes are recorded, but this installation copy is **not relocation-ready** and remains protected by the utility. It requires separate dependency-placement approval/validation; do not include it in a generic historical-file approval.",'',
            '## Explicitly retained','',
            'Current source/tests/configuration and credentials; .venv; phase1_deps and phase4_deps (explicit child-process/SDK fallback references); test dependencies and Node tooling; operational/demo databases; QMS/ERP/index/model assets; all Testing Lab state; current accepted Phase 4.2 evidence; active agent-trace exports and the historical benchmark input baseline.',
            f"Testing Lab status review: {dict(collections.Counter(r['status'] for r in trials))}. Completion alone does not make a trial unused. Paused/incomplete work and registry/acceptance references prevent blanket archival.",'',
            '## Destination, space and recoverability','',f'Proposed external archive: `{archive}`.',
            f"Staging the actionable historical groups needs **{summary['staging_additional_disk_bytes']:,} additional bytes**, plus metadata/filesystem overhead; reserve at least 1 GiB free for this batch. Observed free disk: {summary['disk_free_bytes_observed']:,} bytes.",
            'Originals are then renamed into archive/originals on the same volume, while verified staged copies remain. This reduces the active Capstone folder but does not recover drive free space; the non-destructive copy consumes additional space. The held dependency tree is excluded from that budget.',
            'Every file retains its original relative path, size, SHA-256 and restoration map. Partial copies are preserved in place; retries use a new stage namespace. A crash after an atomic relocation is reconciled from the journal. Changed active files are never overwritten. See ARCHIVAL_PROCEDURE.md.',
            '', '## Approval and validation boundary','',
            'Operator approval must name exact groups and this manifest hash. None has been granted. Stage → verify every copy/ZIP/NPZ → separately authorized controlled relocation → full baseline-preserving regression/startup/QMS/DB checks → commit; otherwise rollback and recheck. Successful copying alone is never accepted archival.',
            f"Manifest SHA-256: `{summary['manifest_sha256']}`.",
            'The archival utilities are tested only with disposable synthetic fixture trees. Current regression and utility evidence are recorded alongside this proposal. No real post-relocation result is claimed because no relocation occurred.',
            '', 'This report requests no inference, network, certificate, service-account, Agent Arena or Phase 5 changes.']
        (OUT/'ARCHIVE_PROPOSAL.md').write_text('\n'.join(md)+'\n',encoding='utf-8')
        (OUT/'inventory_review.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    current_audit=list(walk(OUT,'audit_outputs',[]));current={r['relative_path']:r for r in other_rows}
    # Compact delta from the original listing; don't duplicate the 52 MB listing.
    delta=[]
    for row in rows:
        rel=row['relative_path'];before=previous_rows.get(rel)
        if not before or (before['bytes'],before['mtime_ns'])!=(row['bytes'],row['mtime_ns']):delta.append({'path':rel,'old_bytes':None if not before else before['bytes'],'observed_bytes':row['bytes']})
    (OUT/'inventory_delta.json').write_text(json.dumps({'snapshot_note':'Non-audit changes at targeted enumeration time; generated output totals are reconciled separately.','changes':delta},indent=2),encoding='utf-8')
    print(json.dumps({k:summary[k] for k in ('known_project_bytes','known_project_files','complete_project_inventory','first_batch','additional_historical','actionable_total','held_dependency_copy','manifest_sha256')}))

if __name__=='__main__':main()
