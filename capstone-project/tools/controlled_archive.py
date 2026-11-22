"""Explicitly approved, copy/verify/relocate/validate/commit/rollback archival.

No recursive deletion. Original bytes are retained in the external archive.
No operation accepts an unapproved or changed manifest. See CLI help.
"""
import argparse,hashlib,json,os,shutil,sys,zipfile,time
import xml.etree.ElementTree as ET
from pathlib import Path,PurePosixPath

PROTECTED={'.git','.aws','.codex','.claude','.idea','.run','.venv','phase1_deps','phase4_deps','.phase1_deps','.test_deps','.phase4_2_node','src','tests','scripts','tools','data'}

def sha(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()

def load(path):return json.loads(Path(path).read_text(encoding='utf-8'))

def write(path,value):
    path=Path(path);temp=path.with_name(path.name+'.new')
    with temp.open('w',encoding='utf-8') as f:json.dump(value,f,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
    # Windows readers/indexers can briefly omit FILE_SHARE_DELETE. Preserve
    # the old journal and complete replacement after a bounded retry; never
    # change permissions or fall back to deleting the destination.
    for attempt in range(6):
        try:os.replace(temp,path);break
        except PermissionError:
            if attempt==5:raise
            time.sleep(.05*(2**attempt))

def relative(value,enforce_protected=True):
    raw=value.replace('\\','/');p=PurePosixPath(raw)
    if p.is_absolute() or not p.parts or any(v in {'.','..'} or ':' in v or v.endswith((' ','.')) for v in p.parts):raise ValueError('Unsafe relative archive path')
    if enforce_protected and (p.parts[0] in PROTECTED or raw.startswith(('docs/phase4_2_closure/','docs/phase4_2_continuation/'))):raise PermissionError('Active/protected content cannot be archived')
    if enforce_protected and len(p.parts)==1 and raw not in {'capstonepart1.zip','capstonepart2.zip'}:raise PermissionError('Active root files cannot be archived')
    return Path(*p.parts)

def no_links(path,anchor):
    path=Path(path);anchor=Path(anchor)
    for item in [path,*path.parents]:
        if item.exists() and (item.is_symlink() or bool(getattr(item.lstat(),'st_file_attributes',0)&0x400)):raise PermissionError('Links/reparse points are not archive candidates')
        if item==anchor:break

def checked(path,entry):
    if not path.is_file() or path.stat().st_size!=entry['bytes'] or sha(path)!=entry['sha256']:raise ValueError('File size/hash verification failed: '+str(path))
    if path.suffix.lower() in {'.zip','.npz'}:
        with zipfile.ZipFile(path) as z:
            bad=z.testzip()
            if bad:raise ValueError('Compressed archive CRC failed: '+bad)

def exclusive_copy(source,target):
    target.parent.mkdir(parents=True,exist_ok=True)
    # Refuse overwrite, including crash leftovers. A partial destination leaves
    # the original intact and requires inspection/new version before retry.
    with source.open('rb') as src,target.open('xb') as dst:
        shutil.copyfileobj(src,dst,1024*1024);dst.flush();os.fsync(dst.fileno())
    shutil.copystat(source,target)

class Archive:
    def __init__(self,manifest,approval=None):
        self.manifest_path=Path(manifest).resolve();self.plan=load(manifest);self.plan_hash=sha(manifest)
        if self.plan.get('schema')!='capstone.archive-plan/1':raise ValueError('Unsupported archive plan')
        self.root=Path(self.plan['root']).resolve();self.archive=Path(self.plan['archive_root']).resolve()
        if self.archive.is_relative_to(self.root) or self.root.is_relative_to(self.archive):raise ValueError('Archive must be separate from the active project')
        self.approval=load(approval) if approval else None
        self.entries=[];seen=set()
        for entry in self.plan['files']:
            rel=relative(entry['relative_path'],enforce_protected=False)
            if str(rel).casefold() in seen:raise ValueError('Duplicate manifest path')
            seen.add(str(rel).casefold())
            source=self.root/rel;stage=self.archive/'staged'/rel;original=self.archive/'originals'/rel
            if Path(entry['original_path']).resolve()!=source.resolve() or Path(entry['archive_destination']).resolve()!=stage.resolve() or Path(entry['relocated_original_destination']).resolve()!=original.resolve():raise ValueError('Manifest destination does not match the controlled layout')
            self.entries.append((entry,source,stage,original))
        self.journal_path=self.archive/'journal.json'

    def authorized(self,action):
        a=self.approval
        if not a or a.get('schema')!='capstone.archive-approval/1' or a.get('manifest_sha256')!=self.plan_hash or not a.get('approved_by') or not a.get('approval_reference') or action not in a.get('allowed_actions',[]):raise PermissionError('Explicit operator approval of this manifest/action is required')
        groups=a.get('approved_groups',[])
        selected=[r for r in self.entries if r[0]['group'] in groups]
        if not groups or not selected or set(groups)-{r[0]['group'] for r in self.entries}:raise ValueError('Unknown/empty approved candidate group')
        if any(not r[0].get('safe_to_move') or not r[0].get('sha256') for r in selected):raise PermissionError('An approved group still contains an unsafe or unreadable candidate')
        for entry,_,_,_ in selected:relative(entry['relative_path'])
        return selected

    def journal(self,create=False):
        if not self.journal_path.exists():
            if not create:raise ValueError('Archive has not been staged')
            no_links(self.archive,self.archive.anchor);self.archive.mkdir(parents=True,exist_ok=True)
            record={'schema':'capstone.archive-journal/1','manifest_sha256':self.plan_hash,'state':'staging','files':{},'events':[]}
            with self.journal_path.open('x',encoding='utf-8') as f:json.dump(record,f)
            return record
        record=load(self.journal_path)
        if record['manifest_sha256']!=self.plan_hash:raise ValueError('Archive collision: version belongs to another manifest')
        return record

    def save(self,journal,event,**detail):
        journal['events'].append({'event':event,**detail});write(self.journal_path,journal)

    def staged_path(self,entry,journal,default):
        value=journal['files'].get(entry['id'],{}).get('staged_path')
        if not value:return default
        rel=relative(value,enforce_protected=False)
        if rel.parts[0]!='staged' and not (rel.parts[0].startswith('stage-') and rel.parts[0][6:].isdigit()):raise ValueError('Invalid staged-copy namespace')
        if Path(*rel.parts[1:])!=Path(entry['relative_path']):raise ValueError('Staged copy lost its original relative path')
        target=self.archive/rel;no_links(target,self.archive);return target

    def stage(self):
        selected=self.authorized('stage');journal=self.journal(create=True)
        stored=self.archive/'manifest.json'
        if stored.exists():
            if sha(stored)!=self.plan_hash:raise ValueError('Stored archive manifest collision')
        else:exclusive_copy(self.manifest_path,stored)
        journal['approval']=self.approval
        for entry,source,target,original in selected:
            item=journal['files'].setdefault(entry['id'],{})
            target=self.staged_path(entry,journal,target)
            no_links(source,self.root);no_links(target,self.archive);checked(source,entry)
            if item.get('staged'):checked(target,entry);continue
            if target.exists():
                if not item.get('copy_started'):raise FileExistsError('Refusing unrelated staged-file collision: '+str(target))
                try:checked(target,entry)
                except (ValueError,zipfile.BadZipFile):
                    # Preserve interrupted/changed bytes in place. Resume into
                    # another collision-safe namespace; never overwrite them.
                    item.setdefault('preserved_incomplete_copies',[]).append({'path':str(target),'bytes':target.stat().st_size,'sha256':sha(target)})
                    attempt=int(item.get('copy_attempt',0))+1
                    while (self.archive/('stage-'+str(attempt))/entry['relative_path']).exists():attempt+=1
                    target=self.archive/('stage-'+str(attempt))/entry['relative_path'];item['copy_attempt']=attempt
                else:
                    item['staged']=True;self.save(journal,'copy.resumed_verified',id=entry['id']);continue
            no_links(target,self.archive)
            item.update(copy_started=True,staged_path=target.relative_to(self.archive).as_posix())
            self.save(journal,'copy.started',id=entry['id'])
            exclusive_copy(source,target);checked(target,entry);checked(source,entry)
            item['staged']=True;self.save(journal,'copy.verified',id=entry['id'],bytes=entry['bytes'])
        journal['state']='staged_verified';self.save(journal,'stage.completed',files=len(selected))
        return {'state':journal['state'],'files':len(selected),'originals_changed':False}

    def verify(self):
        selected=self.authorized('stage');journal=self.journal()
        for entry,source,target,original in selected:checked(self.staged_path(entry,journal,target),entry)
        self.save(journal,'stage.reverified',files=len(selected));return {'verified_files':len(selected)}

    def relocate(self):
        selected=self.authorized('relocate');journal=self.journal()
        if journal['state'] not in {'staged_verified','relocating','relocated_pending_validation'}:raise ValueError('Verified staging is required before relocation')
        # Preflight the whole transaction. No recursive directory move, no delete.
        for entry,source,target,original in selected:
            checked(self.staged_path(entry,journal,target),entry);no_links(source,self.root);no_links(original,self.archive)
            item=journal['files'][entry['id']]
            if item.get('relocated'):
                checked(original,entry)
                if source.exists():raise ValueError('Active path was recreated after relocation; review required')
            elif item.get('relocation_started') and not source.exists() and original.exists():
                checked(original,entry);item['relocated']=True;self.save(journal,'relocation.crash_reconciled',id=entry['id'])
            else:
                checked(source,entry)
                if original.exists():raise FileExistsError('Refusing original archive collision: '+str(original))
        journal['state']='relocating';self.save(journal,'relocation.started')
        for entry,source,target,original in selected:
            item=journal['files'][entry['id']]
            if item.get('relocated'):continue
            original.parent.mkdir(parents=True,exist_ok=True)
            item['relocation_started']=True
            self.save(journal,'relocation.file_started',id=entry['id'])
            # os.rename is an atomic same-volume relocation on Windows. Fail
            # rather than delete/copy across a volume; operator must choose a
            # compatible archive layout or a separately approved procedure.
            os.rename(source,original);checked(original,entry)
            item['relocated']=True;self.save(journal,'relocation.file_completed',id=entry['id'])
        journal['state']='relocated_pending_validation';self.save(journal,'relocation.completed')
        return {'state':journal['state'],'files':len(selected),'active_project_bytes_removed':sum(r[0]['bytes'] for r in selected),'drive_free_space_claim':False}

    def commit(self,junit,checks):
        selected=self.authorized('relocate');journal=self.journal()
        if journal['state']!='relocated_pending_validation':raise ValueError('Only a relocated and validated transaction can commit')
        tree=ET.parse(junit).getroot();suites=[tree] if tree.tag=='testsuite' else list(tree.findall('testsuite'))
        if not suites or sum(int(s.get('tests',0)) for s in suites)<self.plan['baseline_tests'] or any(int(s.get(k,0)) for s in suites for k in ('failures','errors','skipped')):raise ValueError('Post-archive full regression failed or lost baseline coverage')
        baseline=ET.parse(self.root/self.plan['baseline_junit']).getroot()
        keys=lambda t:{(r.get('classname'),r.get('name')) for r in t.iter('testcase')}
        if not keys(baseline)<=keys(tree):raise ValueError('Post-archive suite is missing original test cases')
        result=load(checks)
        if not result.get('startup_ok') or not result.get('telemetry_ok') or result.get('qms_fingerprint')!='a3b68d86451db05e' or not result.get('databases') or any(v.get('integrity')!='ok' for v in result['databases'].values()):raise ValueError('Post-archive application/integrity checks are incomplete')
        for entry,source,target,original in selected:
            if source.exists():raise ValueError('An original is still active; cannot claim recovered footprint')
            checked(self.staged_path(entry,journal,target),entry);checked(original,entry)
        journal['state']='committed';journal['validation']={'junit':str(Path(junit).resolve()),'junit_sha256':sha(junit),'checks':str(Path(checks).resolve()),'checks_sha256':sha(checks)}
        self.save(journal,'commit.completed',files=len(selected),bytes=sum(r[0]['bytes'] for r in selected))
        return {'state':'committed','files':len(selected),'active_project_bytes_recovered':sum(r[0]['bytes'] for r in selected)}

    def rollback(self):
        selected=self.authorized('relocate');journal=self.journal();restored=0
        for entry,source,target,original in selected:
            no_links(source,self.root)
            if source.exists():checked(source,entry);continue # never overwrite later edits
            recovery=original if original.exists() else self.staged_path(entry,journal,target)
            checked(recovery,entry);exclusive_copy(recovery,source);checked(source,entry)
            restored+=1
            journal['files'].setdefault(entry['id'],{})['restored']=True;self.save(journal,'rollback.file_verified',id=entry['id'])
        journal['state']='restored_pending_validation';self.save(journal,'rollback.completed')
        return {'state':journal['state'],'restored_files':restored,'verified_active_files':len(selected),'archive_preserved':True,'regression_required':True}

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['inspect','stage','verify','relocate','commit','rollback'])
    p.add_argument('--manifest',required=True);p.add_argument('--approval');p.add_argument('--junit');p.add_argument('--checks');a=p.parse_args()
    archive=Archive(a.manifest,a.approval)
    if a.action=='inspect':result={'manifest_sha256':archive.plan_hash,'candidate_files':len(archive.entries),'archive_root':str(archive.archive),'mutated':False}
    elif a.action=='commit':result=archive.commit(a.junit,a.checks)
    else:result=getattr(archive,a.action)()
    print(json.dumps(result))

if __name__=='__main__':main()
