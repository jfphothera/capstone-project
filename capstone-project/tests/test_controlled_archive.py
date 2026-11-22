"""Archival transaction tests use only disposable synthetic fixture trees."""
import importlib.util,json
from pathlib import Path
import pytest

spec=importlib.util.spec_from_file_location('controlled_archive',Path(__file__).resolve().parents[1]/'tools/controlled_archive.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

@pytest.fixture
def transaction(tmp_path):
    root=tmp_path/'project';source=root/'docs/history/evidence.txt';source.parent.mkdir(parents=True);source.write_bytes(b'fixture evidence')
    archive=tmp_path/'external/version-1';manifest=tmp_path/'manifest.json';approval=tmp_path/'approval.json'
    baseline=root/'docs/baseline.xml';baseline.write_text('<testsuites><testsuite tests="1"><testcase classname="fixture" name="original"/></testsuite></testsuites>')
    entry={'id':'fixture-file','group':'history','relative_path':'docs/history/evidence.txt','original_path':str(source),
        'archive_destination':str(archive/'staged/docs/history/evidence.txt'),'relocated_original_destination':str(archive/'originals/docs/history/evidence.txt'),
        'bytes':source.stat().st_size,'sha256':module.sha(source),'safe_to_move':True}
    plan={'schema':'capstone.archive-plan/1','root':str(root),'archive_root':str(archive),'baseline_tests':1,'baseline_junit':'docs/baseline.xml','files':[entry]}
    manifest.write_text(json.dumps(plan))
    approval.write_text(json.dumps({'schema':'capstone.archive-approval/1','manifest_sha256':module.sha(manifest),
        'approved_by':'synthetic fixture operator','approval_reference':'test-only approval','approved_groups':['history'],'allowed_actions':['stage','relocate']}))
    return module.Archive(manifest,approval),source,manifest,approval

def test_no_approval_or_changed_plan_cannot_copy_or_move(transaction):
    archive,source,manifest,approval=transaction
    with pytest.raises(PermissionError):module.Archive(manifest).stage()
    assert source.exists() and not archive.archive.exists()
    plan=json.loads(manifest.read_text());plan['version']='tampered';manifest.write_text(json.dumps(plan))
    with pytest.raises(PermissionError):module.Archive(manifest,approval).stage()
    assert source.exists() and not archive.archive.exists()

def test_stage_verify_relocate_and_rollback_preserve_exact_bytes(transaction):
    archive,source,_,_=transaction;before=source.read_bytes()
    assert archive.stage()['originals_changed'] is False and source.read_bytes()==before
    assert archive.verify()['verified_files']==1
    assert archive.relocate()['state']=='relocated_pending_validation' and not source.exists()
    result=archive.rollback()
    assert result['restored_files']==1 and result['regression_required'] and source.read_bytes()==before
    assert (archive.archive/'originals/docs/history/evidence.txt').read_bytes()==before
    assert (archive.archive/'staged/docs/history/evidence.txt').read_bytes()==before

def test_corrupt_copy_blocks_relocation_and_leaves_original(transaction):
    archive,source,_,_=transaction;archive.stage()
    staged=archive.archive/'staged/docs/history/evidence.txt';staged.write_bytes(b'corrupt')
    with pytest.raises(ValueError):archive.relocate()
    assert source.read_bytes()==b'fixture evidence'

def test_rollback_refuses_to_overwrite_new_active_content(transaction):
    archive,source,_,_=transaction;archive.stage();archive.relocate();source.write_bytes(b'new active edit')
    with pytest.raises(ValueError):archive.rollback()
    assert source.read_bytes()==b'new active edit'
    assert (archive.archive/'originals/docs/history/evidence.txt').read_bytes()==b'fixture evidence'

@pytest.mark.parametrize('path',['../outside.txt','docs/../../outside.txt','C:/outside.txt','src/active.py','data/operations.db'])
def test_path_traversal_and_active_assets_are_refused(path):
    with pytest.raises((ValueError,PermissionError)):module.relative(path)

def test_commit_requires_passing_full_coverage_and_application_checks(transaction,tmp_path):
    archive,source,_,_=transaction;archive.stage();archive.relocate()
    junit=tmp_path/'post.xml';checks=tmp_path/'checks.json'
    junit.write_text('<testsuites><testsuite tests="1" failures="1"><testcase classname="fixture" name="original"/></testsuite></testsuites>')
    checks.write_text(json.dumps({'startup_ok':True,'telemetry_ok':True,'qms_fingerprint':'a3b68d86451db05e','databases':{'fixture':{'integrity':'ok'}}}))
    with pytest.raises(ValueError):archive.commit(junit,checks)
    junit.write_text('<testsuites><testsuite tests="1"><testcase classname="fixture" name="different"/></testsuite></testsuites>')
    with pytest.raises(ValueError):archive.commit(junit,checks)
    junit.write_text('<testsuites><testsuite tests="1"><testcase classname="fixture" name="original"/></testsuite></testsuites>')
    assert archive.commit(junit,checks)['active_project_bytes_recovered']==len(b'fixture evidence')

def test_stage_collision_never_overwrites_existing_archive_file(transaction):
    archive,source,_,_=transaction
    target=archive.archive/'staged/docs/history/evidence.txt';target.parent.mkdir(parents=True);target.write_bytes(b'unrelated')
    with pytest.raises(FileExistsError):archive.stage()
    assert target.read_bytes()==b'unrelated' and source.exists()

def test_interrupted_copy_resumes_without_overwriting_partial_bytes(transaction,monkeypatch):
    archive,source,_,_=transaction;copy=module.exclusive_copy;interrupted=[]
    def partial(src,target):
        if src==source and not interrupted:
            target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(b'partial');interrupted.append(True);raise OSError('Simulated interruption')
        return copy(src,target)
    monkeypatch.setattr(module,'exclusive_copy',partial)
    with pytest.raises(OSError):archive.stage()
    assert source.read_bytes()==b'fixture evidence'
    assert archive.stage()['state']=='staged_verified' and archive.verify()['verified_files']==1
    assert (archive.archive/'staged/docs/history/evidence.txt').read_bytes()==b'partial'
    assert (archive.archive/'stage-1/docs/history/evidence.txt').read_bytes()==b'fixture evidence'
    archive.relocate();assert archive.rollback()['restored_files']==1

def test_interrupted_relocation_receipt_is_reconciled_on_resume(transaction,monkeypatch):
    archive,source,_,_=transaction;archive.stage();rename=module.os.rename;interrupted=[]
    def after_move(src,target):
        rename(src,target)
        if src==source and not interrupted:interrupted.append(True);raise OSError('Simulated crash after atomic rename')
    monkeypatch.setattr(module.os,'rename',after_move)
    with pytest.raises(OSError):archive.relocate()
    assert not source.exists()
    assert archive.relocate()['state']=='relocated_pending_validation'
    assert archive.rollback()['restored_files']==1 and source.read_bytes()==b'fixture evidence'

def test_identical_basenames_preserve_their_different_relative_paths(transaction):
    archive,source,manifest,approval=transaction;second=archive.root/'docs/other/evidence.txt';second.parent.mkdir();second.write_bytes(b'other fixture')
    plan=json.loads(manifest.read_text());entry=dict(plan['files'][0]);entry.update(id='second',relative_path='docs/other/evidence.txt',original_path=str(second),
        archive_destination=str(archive.archive/'staged/docs/other/evidence.txt'),relocated_original_destination=str(archive.archive/'originals/docs/other/evidence.txt'),
        bytes=second.stat().st_size,sha256=module.sha(second));plan['files'].append(entry);manifest.write_text(json.dumps(plan))
    grant=json.loads(approval.read_text());grant['manifest_sha256']=module.sha(manifest);approval.write_text(json.dumps(grant))
    archive=module.Archive(manifest,approval);assert archive.stage()['files']==2
    assert (archive.archive/'staged/docs/history/evidence.txt').read_bytes()!= (archive.archive/'staged/docs/other/evidence.txt').read_bytes()

def test_changed_source_is_not_relocated_after_staging(transaction):
    archive,source,_,_=transaction;archive.stage();source.write_bytes(b'changed source')
    with pytest.raises(ValueError):archive.relocate()
    assert source.read_bytes()==b'changed source'

def test_missing_staged_copy_blocks_commit_but_original_copy_can_restore(transaction):
    archive,source,_,_=transaction;archive.stage();archive.relocate()
    (archive.archive/'staged/docs/history/evidence.txt').unlink() # disposable fault fixture only
    with pytest.raises(ValueError):archive.verify()
    assert archive.rollback()['restored_files']==1 and source.read_bytes()==b'fixture evidence'

def test_simulated_regression_failure_is_followed_by_verified_rollback(transaction,tmp_path):
    archive,source,_,_=transaction;archive.stage();archive.relocate()
    junit=tmp_path/'failed.xml';junit.write_text('<testsuites><testsuite tests="1" failures="1"><testcase classname="fixture" name="original"/></testsuite></testsuites>')
    with pytest.raises(ValueError):archive.commit(junit,tmp_path/'not-used.json')
    result=archive.rollback();assert result['restored_files']==1 and source.read_bytes()==b'fixture evidence'

def test_missing_source_stops_staging(transaction):
    archive,source,_,_=transaction;source.unlink() # disposable fault fixture only
    with pytest.raises(ValueError):archive.stage()

def test_journal_replace_retries_transient_windows_access_denial(tmp_path,monkeypatch):
    target=tmp_path/'journal.json';target.write_text('{"state":"old"}')
    replace=module.os.replace;attempts=[]
    def transient(source,destination):
        attempts.append(True)
        if len(attempts)<3:raise PermissionError('Simulated temporary sharing denial')
        replace(source,destination)
    monkeypatch.setattr(module.os,'replace',transient);monkeypatch.setattr(module.time,'sleep',lambda _:None)
    module.write(target,{'state':'new'})
    assert len(attempts)==3 and json.loads(target.read_text())['state']=='new'

def test_persistent_journal_denial_keeps_previous_journal_and_pending_copy(tmp_path,monkeypatch):
    target=tmp_path/'journal.json';target.write_text('{"state":"old"}')
    def denied(*args):raise PermissionError('Simulated persistent denial')
    monkeypatch.setattr(module.os,'replace',denied);monkeypatch.setattr(module.time,'sleep',lambda _:None)
    with pytest.raises(PermissionError):module.write(target,{'state':'new'})
    assert json.loads(target.read_text())['state']=='old'
    assert json.loads(target.with_name('journal.json.new').read_text())['state']=='new'
