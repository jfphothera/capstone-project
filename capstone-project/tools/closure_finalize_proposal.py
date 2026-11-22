"""Compact reviewed metadata and reconcile readable storage; no archival actions."""
import gzip,json,re,shutil,time
from pathlib import Path
from closure_inventory import ROOT,OUT,walk,digest

def main():
    plan=json.loads((OUT/'archive_manifest.json').read_text(encoding='utf-8'))
    held=[e for e in plan['files'] if e['batch']=='held_dependency_copy']
    if held:
        appendix=OUT/'held_dependency_manifest.jsonl.gz'
        with appendix.open('wb') as raw,gzip.GzipFile(filename='',mode='wb',fileobj=raw,mtime=0) as zipped:
            for e in held:zipped.write((json.dumps(e,separators=(',',':'),ensure_ascii=False)+'\n').encode('utf-8'))
        plan['files']=[e for e in plan['files'] if e['batch']!='held_dependency_copy']
        plan['held_candidate_manifests']=[{'relative_path':appendix.relative_to(ROOT).as_posix(),'schema':'capstone.archive-file-records/1',
            'compression':'gzip JSON lines','files':len(held),'sha256':digest(appendix),'bytes':appendix.stat().st_size,'relocation_authorized':False,
            'purpose':'Full per-file recovery metadata for the protected, unapproved old installation; not part of the actionable archive batch.'}]
        with (OUT/'archive_manifest.json').open('w',encoding='utf-8') as f:json.dump(plan,f,separators=(',',':'),ensure_ascii=False);f.write('\n')
    for item in plan.get('held_candidate_manifests',[]):
        assert digest(ROOT/item['relative_path'])==item['sha256']
    summary=json.loads((OUT/'inventory_review.json').read_text(encoding='utf-8'))
    summary['manifest_sha256']=digest(OUT/'archive_manifest.json')
    summary['staging_payload_bytes']=summary['actionable_total']['bytes']
    summary['staging_manifest_bytes']=(OUT/'archive_manifest.json').stat().st_size
    summary['staging_payload_plus_manifest_bytes']=summary['staging_payload_bytes']+summary['staging_manifest_bytes']
    summary['staging_budget_bytes']=1024**3
    summary['post_relocation_test_scratch_space']='Additional to the 1 GiB staging budget; full isolated tests can create temporary database copies. Check free space before starting.'
    summary['scope']='Readable project files only; inaccessible subtree excluded. Point-in-time metadata enumeration, not an atomic filesystem snapshot.'
    errors=[];rows=list(walk(ROOT,'project',errors));other=[r for r in rows if not r['relative_path'].startswith('docs/phase4_2_closure/')]
    summary['excluded_subtrees']=errors;summary['complete_project_inventory']=not errors;summary['total_is_lower_bound']=bool(errors)
    summary['application_and_other_files_bytes']=sum(r['bytes'] for r in other)
    summary['disk_free_bytes_observed']=shutil.disk_usage(ROOT).free
    # Put the final source verification and fixture evidence into the proposal.
    text=(OUT/'ARCHIVE_PROPOSAL.md').read_text(encoding='utf-8').split('\n## Procedure and verification results\n',1)[0]
    heading='## Exact proposed groups\n'
    end_heading='## Additional candidate held outside the actionable batch'
    start=text.index(heading)+len(heading);end=text.index(end_heading)
    table=['','| Group requiring explicit approval | Original relative path(s) | Files | Bytes |','|---|---|---:|---:|']
    for group,g in plan['groups'].items():
        entries=[e for e in plan['files'] if e['group']==group]
        if not entries:continue
        paths=g.get('prefixes') or [g['path']]
        table.append('| '+group+' | '+'; '.join('`'+p+'`' for p in paths)+' | '+f'{len(entries):,}'+' | '+f"{sum(e['bytes'] for e in entries):,}"+' |')
    table+=['',f"First batch: **{summary['first_batch']['bytes']:,} bytes / {summary['first_batch']['files']:,} files**.",
        f"Additional historical batch: **{summary['additional_historical']['bytes']:,} bytes / {summary['additional_historical']['files']:,} files**.",
        f"Total conditional historical proposal: **{summary['actionable_total']['bytes']:,} bytes / {summary['actionable_total']['files']:,} files**. No current runtime/test reader was found; known manual historical consumers must restore the original paths before reproduction. Exact hashes and recovery paths are in archive_manifest.json.",'','']
    text=text[:start]+'\n'.join(table)+text[end:]
    text=re.sub(r'Manifest SHA-256: `[^`]+`\.',f"Manifest SHA-256: `{summary['manifest_sha256']}`.",text)
    text=text.replace('Individual hashes are recorded, but this installation copy is',
        'Full individual hashes and recovery metadata are in held_dependency_manifest.jsonl.gz, cryptographically referenced by archive_manifest.json. The five apparent data-file differences were resolved to byte-identical .venv/share and .venv/etc locations; unresolved payload differences: zero. This installation copy is')
    if 'External/manual consumers outside Capstone' not in text:
        text=text.replace('## Explicitly retained','External/manual consumers outside Capstone have not been certified. The protected old installation is therefore not part of an approval to archive historical artifacts, despite the matching payload evidence.\n\n## Explicitly retained')
    text+='\n## Procedure and verification results\n\n'
    text+='Disposable archival fixtures: **18 passed** (archive_utilities.xml): staging and SHA verification; interrupted copying and relocation; same-basename/different-path handling; changed-source protection; missing archive copy; restoration; and rollback following simulated regression failure. No real files were used for relocation tests.\n\n'
    text+='Full preserved application baseline: **585 passed**, zero failures/errors/skips (revised_proposal_regression.xml). This includes every original 563 test identity plus the added closure/archival tests. The first pre-work attempt exposed a gateway snapshot race; its correction and lifecycle tests preceded the later archival-only scope instruction. No further inference or Arena work is included in this revised proposal.\n\n'
    text+=f"Staging budget: {summary['staging_payload_bytes']:,} bytes of historical payload + {summary['staging_manifest_bytes']:,} bytes for its complete actionable manifest, plus journal/filesystem overhead. Reserve **1 GiB for staging**; regression scratch space is additional. The protected dependency appendix is an unapproved review artifact and is not needed to restore the actionable historical files.\n\n"
    text+='No post-relocation acceptance, actual rollback of this project, or recovered disk space is claimed. Staged files: **0**. Relocated files: **0**. Verified active-project reduction: **0 bytes**.\n'
    summary['reconciled_at_unix']=int(time.time())
    for _ in range(5):
        audit=list(walk(OUT,'audit_outputs',[]))
        summary.update(known_project_files=len(other)+len(audit),known_project_bytes=summary['application_and_other_files_bytes']+sum(r['bytes'] for r in audit),
            audit_outputs_bytes=sum(r['bytes'] for r in audit))
        text=re.sub(r'Readable footprint: \*\*[^\n]+',
            f"Readable footprint: **{summary['known_project_bytes']:,} bytes** in {summary['known_project_files']:,} entries; includes {summary['audit_outputs_bytes']:,} bytes of generated audit outputs. The unreadable cache is excluded, so this is a lower bound, not a complete-project total.",text,count=1)
        (OUT/'ARCHIVE_PROPOSAL.md').write_text(text,encoding='utf-8')
        (OUT/'inventory_review.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    print(json.dumps({k:summary[k] for k in ('known_project_bytes','known_project_files','complete_project_inventory','actionable_total','held_dependency_copy','staging_payload_plus_manifest_bytes','manifest_sha256')}))

if __name__=='__main__':main()
