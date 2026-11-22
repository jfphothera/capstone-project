"""Build the versioned Windows release package for F: integration.

release/inference-gateway-<version>/ with wheel/, bin/, scripts/, config/, docs/,
verification/, deployment-manifest.json (file hashes filled in) and SHA256SUMS
over every file; plus release/inference-gateway-<version>.zip and its .sha256.
No secret, ledger, certificate or environment is packaged.
"""
import argparse,hashlib,json,shutil,subprocess,sys,tempfile,zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
CONTENT={
    'bin/gateway-deploy.py':'deploy/windows/gateway-deploy.py',
    'scripts/New-GatewayCertificate.ps1':'scripts/New-GatewayCertificate.ps1',
    'scripts/Set-ObservationPublicationAcl.ps1':'scripts/Set-ObservationPublicationAcl.ps1',
    'scripts/Register-ObservationPublisherTask.ps1':'scripts/Register-ObservationPublisherTask.ps1',
    'config/deployment-settings.template.json':'config/deployment-settings.template.json',
    'config/policy.template.json':'config/policy.template.json',
    'config/client.template.json':'config/client.template.json',
    'config/policy.lab-example.json':'config/policy.lab-example.json',
    'docs/FAILURES-AND-PLACEMENT.md':'docs/FAILURES-AND-PLACEMENT.md',
    'docs/examples/observation-0.4.0.json':'docs/examples/observation-0.4.0.json',
    'docs/examples/operator-commands-0.4.0.json':'docs/examples/operator-commands-0.4.0.json',
    'docs/examples/upgrade-report-0.4.0.json':'docs/examples/upgrade-report-0.4.0.json',
    'docs/CONTRACT.md':'docs/CONTRACT.md',
    'docs/DEPLOYMENT.md':'docs/DEPLOYMENT.md',
    'docs/DEPLOYMENT-CONTRACT.md':'deploy/windows/DEPLOYMENT-CONTRACT.md',
    'docs/F-1.1.0-COMPATIBILITY.md':'deploy/windows/F-1.1.0-COMPATIBILITY.md',
    'docs/OPERATOR-CHECKLIST.md':'deploy/windows/OPERATOR-CHECKLIST.md',
    'docs/ACCEPTANCE_PLAN.md':'docs/ACCEPTANCE_PLAN.md',
    'docs/CLIENT_CONTRACT.md':'docs/CLIENT_CONTRACT.md',
    'docs/ARENA_INTEGRATION.md':'docs/ARENA_INTEGRATION.md',
    'verification/test-report.json':'verification/test-report.json',
    # 0.4.1: the client library Capstone ships, the release notes and the concurrency evidence.
    'client/shared_inference_client.py':'src/shared_inference_client.py',
    'docs/RELEASE-NOTES-0.4.1.md':'docs/RELEASE-NOTES-0.4.1.md',
    'docs/RELEASE-NOTES-0.4.2.md':'docs/RELEASE-NOTES-0.4.2.md',
    'docs/RELEASE-NOTES-0.4.3.md':'docs/RELEASE-NOTES-0.4.3.md',
    # 0.4.4: release notes, ledger and lifecycle evidence and the harnesses that produced it.
    'docs/RELEASE-NOTES-0.4.4.md':'docs/RELEASE-NOTES-0.4.4.md',
    'verification/ledger-0.4.3.json':'verification/ledger-0.4.3.json',
    'verification/ledger-0.4.4.json':'verification/ledger-0.4.4.json',
    'verification/lifecycle-0.4.4.json':'verification/lifecycle-0.4.4.json',
    'verification/concurrency-0.4.4.json':'verification/concurrency-0.4.4.json',
    'verification/pilot-log-analysis-0.4.4.json':'verification/pilot-log-analysis-0.4.4.json',
    'scripts/ledger_contention.py':'scripts/ledger_contention.py',
    'scripts/lifecycle_load.py':'scripts/lifecycle_load.py',
    'verification/live-ollama-0.4.2-reasoning.json':'verification/live-ollama-0.4.2-reasoning.json',
    'verification/concurrency-0.4.1.json':'verification/concurrency-0.4.1.json',
    'scripts/concurrency_load.py':'scripts/concurrency_load.py',
    # 0.4.5: release notes (shipped from 0.4.6 on).
    'docs/RELEASE-NOTES-0.4.5.md':'docs/RELEASE-NOTES-0.4.5.md',
    # 0.4.6: release notes, the client contract handoff, the control-plane measurement and its harness, and the lab tools.
    'docs/RELEASE-NOTES-0.4.6.md':'docs/RELEASE-NOTES-0.4.6.md',
    'docs/GATEWAY-0.4.6-CLIENT-CONTRACT.md':'docs/handoff/GATEWAY-0.4.6-CLIENT-CONTRACT.md',
    'verification/control-plane-0.4.5.json':'verification/control-plane-0.4.5.json',
    'verification/control-plane-0.4.6.json':'verification/control-plane-0.4.6.json',
    'verification/control-plane-repeats-0.4.6.json':'verification/control-plane-repeats-0.4.6.json',
    'scripts/control_plane_load.py':'scripts/control_plane_load.py',
    'lab-tools/README.md':'lab-tools/README.md',
    'lab-tools/g6-capacity-check/G6-CAPACITY-CHECK.cmd':'lab-tools/g6-capacity-check/G6-CAPACITY-CHECK.cmd',
    'lab-tools/g6-capacity-check/g6_capacity_check.py':'lab-tools/g6-capacity-check/g6_capacity_check.py',
    'lab-tools/g4-gemma500-repro/G4-GEMMA500-REPRO.cmd':'lab-tools/g4-gemma500-repro/G4-GEMMA500-REPRO.cmd',
    'lab-tools/g4-gemma500-repro/g4_gemma500_repro.py':'lab-tools/g4-gemma500-repro/g4_gemma500_repro.py',
    'lab-tools/launch-storm/LAUNCH-STORM.cmd':'lab-tools/launch-storm/LAUNCH-STORM.cmd',
    'lab-tools/launch-storm/launch_storm.py':'lab-tools/launch-storm/launch_storm.py',
}
FORBIDDEN_SUFFIXES={'.db','.key','.pem','.pfx','.p12','.token','.lock'}


def sha256(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build(output,*,force=False,require_all=True,write_back=True):
    version=next(l.split('"')[1] for l in (ROOT/'pyproject.toml').read_text().splitlines() if l.startswith('version ='))
    package=Path(output)/('inference-gateway-'+version)
    if package.exists():
        if not force:raise FileExistsError('Release folder exists; pass --force to rebuild it')
        shutil.rmtree(package)
    (package/'wheel').mkdir(parents=True)
    with tempfile.TemporaryDirectory() as work:
        subprocess.run([sys.executable,'-m','pip','wheel','--no-build-isolation','--no-deps','--no-index','--wheel-dir',work,str(ROOT)],check=True,capture_output=True)
        [wheel]=Path(work).glob('inference_gateway-%s-py3-none-any.whl'%version)
        shutil.copy2(wheel,package/'wheel'/wheel.name)
    missing=[]
    for target,source in CONTENT.items():
        if not (ROOT/source).is_file():missing.append(source);continue
        (package/target).parent.mkdir(parents=True,exist_ok=True);shutil.copy2(ROOT/source,package/target)
    if missing and require_all:raise FileNotFoundError('Missing release content: '+', '.join(missing))
    leaked=[p.name for p in package.rglob('*') if p.suffix.lower() in FORBIDDEN_SUFFIXES]
    if leaked:raise ValueError('Secret-like files must never be packaged: '+', '.join(leaked))
    manifest=json.loads((ROOT/'deploy'/'windows'/'deployment-manifest.json').read_text(encoding='utf-8'))
    if manifest['release']!=version:raise ValueError('Manifest release %s does not match %s'%(manifest['release'],version))
    manifest['files']={p.relative_to(package).as_posix():sha256(p) for p in sorted(package.rglob('*')) if p.is_file()}
    text=json.dumps(manifest,indent=2)+'\n'
    (package/'deployment-manifest.json').write_text(text,encoding='utf-8')
    if write_back:(ROOT/'deploy'/'windows'/'deployment-manifest.json').write_text(text,encoding='utf-8') # repository copy carries the release hashes
    sums=''.join('%s  %s\n'%(sha256(p),p.relative_to(package).as_posix()) for p in sorted(package.rglob('*')) if p.is_file())
    (package/'SHA256SUMS').write_text(sums,encoding='utf-8')
    archive=package.parent/(package.name+'.zip') # with_suffix would treat '.0' as the extension
    if archive.exists():archive.unlink()
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
        for p in sorted(package.rglob('*')):
            if p.is_file():z.write(p,(package.name+'/'+p.relative_to(package).as_posix()))
    digest=sha256(archive);Path(str(archive)+'.sha256').write_text('%s  %s\n'%(digest,archive.name),encoding='utf-8')
    return {'release':version,'package':str(package),'zip':str(archive),'zip_sha256':digest,'files':len(manifest['files'])+2,'missing':missing}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',default=str(ROOT/'release'));p.add_argument('--force',action='store_true')
    p.add_argument('--allow-missing',action='store_true',help='development builds only')
    a=p.parse_args()
    print(json.dumps(build(a.output,force=a.force,require_all=not a.allow_missing),indent=2))


if __name__=='__main__':main()
