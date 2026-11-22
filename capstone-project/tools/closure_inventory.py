"""Read-only storage/reference/provenance inventory. Never relocates a file."""
import argparse,collections,hashlib,importlib,importlib.metadata,json,os,stat,sys,time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'docs/phase4_2_closure'
DEPENDENCIES={'.venv','phase1_deps','phase4_deps','.phase1_deps','.test_deps','.phase4_2_node'}
GROUPS={
 'course-part1':('capstonepart1.zip','Historical evidence — preserve','Original course submission; proposed relocation only, retain exact bytes.'),
 'course-part2':('capstonepart2.zip','Historical evidence — preserve','Original course submission; proposed relocation only, retain exact bytes.'),
 'phase3-extracted':('docs/phase3/acceptance_evidence_bundle','Historical evidence — preserve','Extracted frozen delivery; retain ZIP and all provenance, restore before historical rebuild.'),
 'phase41-extracted':('docs/phase4_1/acceptance_evidence_bundle','Historical evidence — preserve','Extracted frozen delivery; retain ZIP and all provenance, restore before historical rebuild.'),
 'phase3-baseline':('docs/phase3/baseline_snapshot_20261029','Historical evidence — preserve','Historical source snapshot; restoration required for historical comparison tools.'),
 'hidden-phase1-deps':('.phase1_deps','Candidate — verification required','Possibly superseded dependency installation; retain unless complete provenance/reference checks clear it.'),
 'test-deps':('.test_deps','Candidate — verification required','Possibly superseded test installation; retain unless complete provenance/reference checks clear it.'),
}

def digest(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()

def walk(root,scope,errors):
    pending=[Path(root)]
    while pending:
        folder=pending.pop()
        try:
            with os.scandir(folder) as it:entries=sorted(it,key=lambda e:e.name)
        except OSError as exc:
            errors.append({'path':str(folder),'operation':'enumerate','error':str(exc)});continue
        for entry in entries:
            try:
                info=entry.stat(follow_symlinks=False);path=Path(entry.path)
                link=entry.is_symlink() or bool(getattr(info,'st_file_attributes',0)&0x400)
                if link:
                    yield {'path':str(path),'relative_path':path.relative_to(root).as_posix(),'scope':scope,'bytes':info.st_size,'kind':'link_not_followed','mtime_ns':info.st_mtime_ns}
                elif entry.is_dir(follow_symlinks=False):pending.append(path)
                else:yield {'path':str(path),'relative_path':path.relative_to(root).as_posix(),'scope':scope,'bytes':info.st_size,'kind':'file','mtime_ns':info.st_mtime_ns}
            except OSError as exc:errors.append({'path':entry.path,'operation':'stat','error':str(exc)})

def category(rel):
    parts=Path(rel).parts;top=parts[0]
    if '__pycache__' in parts or top in {'.pytest_cache','.test_cache_phase3','.test_tmp'}:return 'Disposable generated cache — separately review'
    if top in DEPENDENCIES:return 'Runtime dependency installation — retained pending provenance'
    if rel.startswith('data/experiments/'):return 'Testing Lab state/export — verify completion and references before archival'
    if top=='data':return 'Operational/model/QMS/demo data — retained pending individual dependency review'
    if top=='docs':return 'Historical evidence / reports — preserve'
    if rel.endswith('.zip'):return 'Release/course archive — preserve'
    return 'Active — never archive'

def main():
    p=argparse.ArgumentParser();p.add_argument('--archive-root',required=True);args=p.parse_args()
    OUT.mkdir(parents=True,exist_ok=True);errors=[];started=time.time()
    rows=list(walk(ROOT,'project',errors));totals=collections.defaultdict(lambda:{'bytes':0,'files':0})
    for row in rows:
        row['category']=category(row['relative_path']);top=row['relative_path'].split('/')[0]
        totals[top]['bytes']+=row['bytes'];totals[top]['files']+=1
    external=[]
    for cache in {Path(os.environ.get('HF_HOME',Path.home()/'.cache/huggingface')),Path(os.environ.get('OLLAMA_MODELS',Path.home()/'.ollama/models'))}:
        if cache.exists() and not cache.resolve().is_relative_to(ROOT):
            items=list(walk(cache,'external_model_cache',errors));external.extend(items)
    with (OUT/'storage_inventory.jsonl').open('w',encoding='utf-8') as f:
        for row in rows+external:f.write(json.dumps(row,ensure_ascii=False)+'\n')
    # Search only source/config/tests/docs and root scripts, not installed package
    # internals or extracted historical copies citing themselves.
    references=collections.defaultdict(list)
    text_extensions={'.py','.ps1','.cmd','.md','.json','.xml','.mjs','.cjs','.js','.txt','.ini','.cfg','.pth'}
    for row in rows:
        rel=row['relative_path'];parts=Path(rel).parts
        if row['kind']!='file' or Path(rel).suffix.lower() not in text_extensions:continue
        if parts[0] in DEPENDENCIES or parts[0] in {'data','.claude'} or rel.startswith('docs/phase4_2_closure/') or 'acceptance_evidence_bundle' in parts or 'baseline_snapshot_20261029' in parts:continue
        if parts[0] not in {'src','tests','tools','scripts','docs','.idea','.run'} and len(parts)>1:continue
        try:lines=Path(row['path']).read_text(encoding='utf-8',errors='replace').splitlines()
        except OSError as exc:errors.append({'path':row['path'],'operation':'reference_read','error':str(exc)});continue
        for group,(token,_,_) in GROUPS.items():
            tokens={token,Path(token).name,token.replace('/','\\')}
            for number,line in enumerate(lines,1):
                if any(t in line for t in tokens):references[group].append({'path':rel,'line':number,'kind':'runtime_or_test' if parts[0] in {'src','tests','.idea','.run'} else 'manual_tool' if parts[0] in {'scripts','tools'} else 'documentation'})
    distributions={}
    for dep in sorted(DEPENDENCIES):
        base=ROOT/dep;site=base/'Lib/site-packages' if dep=='.venv' else base
        try:distributions[dep]=sorted([{'name':d.metadata.get('Name','unknown'),'version':d.version,'metadata_path':str(d._path)} for d in importlib.metadata.distributions(path=[str(site)])],key=lambda d:d['name'].lower())
        except OSError as exc:errors.append({'path':str(site),'operation':'distribution_metadata','error':str(exc)})
    # Actual configured interpreter imports, not guesses based on directory age.
    from capstone_rag import startup
    runtime=startup.check();origins={}
    for name,module in startup.REQUIRED.items():
        try:origins[name]=getattr(importlib.import_module(module),'__file__',None)
        except Exception as exc:origins[name]={'error':str(exc)}
    runtime.update(module_origins=origins,sys_path=sys.path,distributions=distributions)
    (OUT/'dependency_provenance.json').write_text(json.dumps(runtime,indent=2),encoding='utf-8')
    version='phase4_2_closure-20261101-v1';archive=Path(args.archive_root).resolve()/version
    candidates=[];group_summary={}
    for group,(prefix,classification,reason) in GROUPS.items():
        files=[r for r in rows if r['kind']=='file' and (r['relative_path']==prefix or r['relative_path'].startswith(prefix+'/'))]
        blocked=[e for e in errors if str(e['path']).startswith(str(ROOT/prefix))]
        active_refs=[r for r in references[group] if r['kind']=='runtime_or_test']
        imported=any(isinstance(origin,str) and Path(origin).resolve().is_relative_to((ROOT/prefix).resolve()) for origin in origins.values())
        safe=bool(files) and not blocked and not active_refs and not imported
        # Installed dependency trees never join an automatic move proposal.
        # Provenance checks are evidence, not permission to alter an installation.
        if prefix.split('/')[0] in DEPENDENCIES:safe=False
        group_summary[group]={'path':prefix,'files':len(files),'bytes':sum(r['bytes'] for r in files),'classification':classification,
            'safe_to_move_after_approval':safe,'references':references[group],'enumeration_errors':blocked}
        if prefix.split('/')[0] in DEPENDENCIES:
            group_summary[group]['classification']='Active — never archive during this closure'
            group_summary[group]['reason']='Installed dependency tree retained; changing installations is not required to produce a compact review export.'
            continue
        for row in files:
            try:
                before=Path(row['path']).stat();checksum=digest(row['path']);after=Path(row['path']).stat()
                stable=before.st_size==after.st_size and before.st_mtime_ns==after.st_mtime_ns
            except OSError as exc:checksum=None;stable=False;errors.append({'path':row['path'],'operation':'candidate_hash','error':str(exc)})
            candidates.append({'id':hashlib.sha256(row['relative_path'].encode()).hexdigest()[:20],'group':group,'original_path':row['path'],
                'relative_path':row['relative_path'],'filename':Path(row['path']).name,'bytes':row['bytes'],'sha256':checksum,
                'category':classification,'reason':reason,'known_references':{'group':group,'resolved_in':'groups.'+group+'.references'},'status':'historical' if classification.startswith('Historical') else 'verification_required',
                'archive_destination':str(archive/'staged'/row['relative_path']),'relocated_original_destination':str(archive/'originals'/row['relative_path']),
                'safe_to_move':safe and stable,'validation_required':['size/SHA-256 copy verification','ZIP CRC if applicable','full regression >=563 tests','startup/QMS/DB checks','historical reference recovery'],
                'rollback':'Copy verified original/staged bytes back to original_path, refuse overwrite of changed files, rerun affected checks.'})
    plan={'schema':'capstone.archive-plan/1','root':str(ROOT.resolve()),'archive_root':str(archive),'version':version,'baseline_tests':563,
        'baseline_junit':'docs/phase4_2_closure/pre_work_regression.xml',
        'approval_required':True,'files':candidates,'groups':group_summary}
    (OUT/'archive_manifest.json').write_text(json.dumps(plan,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    safe_bytes=sum(r['bytes'] for r in candidates if r['safe_to_move'])
    summary={'project_bytes':sum(r['bytes'] for r in rows),'project_files':len(rows),'top_level':dict(sorted(totals.items(),key=lambda p:-p[1]['bytes'])),
        'external_model_cache_bytes':sum(r['bytes'] for r in external),'external_model_cache_files':len(external),
        'errors':errors,'complete':not errors,'candidate_files':len(candidates),'conditionally_safe_candidate_bytes':safe_bytes,
        'candidate_manifest_sha256':digest(OUT/'archive_manifest.json'),'runtime_ok':runtime['ok'],'elapsed_seconds':time.time()-started,
        'archived_files':0,'active_project_bytes_recovered':0,'drive_space_recovered':0}
    (OUT/'storage_summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    md=['# Storage inventory and proposed archival','',f"Inventoried {len(rows):,} project entries, {summary['project_bytes']:,} logical bytes. Complete: {not errors}.",'',
        'No originals have been moved. No staging is authorized yet. Runtime installations, operational data, model caches and all current source/tests remain in place.','',
        '| Group | Files | Logical bytes | Conditionally safe after approval |','|---|---:|---:|---|']
    for group,value in group_summary.items():md.append(f"| {group}: {value['path']} | {value['files']:,} | {value['bytes']:,} | {value['safe_to_move_after_approval']} |")
    md+=['',f'Conditionally eligible total: {safe_bytes:,} bytes. This is active-project footprint reduction, not promised free disk space. Staging plus preserved originals consumes archive storage.',
        '',f'Proposed versioned destination: `{archive}`.',f"Manifest SHA-256: `{summary['candidate_manifest_sha256']}`.",'',
        'Per-file paths, hashes, reasons, dependencies, destinations, validation and rollback are in archive_manifest.json. Dependency provenance and every storage entry are separate machine-readable inventories.',
        '',f'Errors/unknowns: {len(errors)}. Any unreadable or unstable candidate must remain in place. Do not treat absent references as authorization.']
    (OUT/'ARCHIVE_PROPOSAL.md').write_text('\n'.join(md)+'\n',encoding='utf-8')
    print(json.dumps({k:summary[k] for k in ('project_bytes','project_files','complete','candidate_files','conditionally_safe_candidate_bytes','runtime_ok','elapsed_seconds')}))

if __name__=='__main__':main()
