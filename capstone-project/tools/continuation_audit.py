"""Read-only source/config audit; SQLite-consistent local baseline backups."""
import hashlib,json,re,sqlite3
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'docs/phase4_2_continuation';OUT.mkdir(parents=True,exist_ok=True)
skip={'phase1_deps','phase4_deps','.phase1_deps','.phase4_2_node','.venv','venv','.git','.codex','.claude','.aws','__pycache__','.test_deps'}
patterns=re.compile(r'192\.0\.2\.\d+|coordinator-192\.0\.2|localhost:\d+|127\.0\.0\.1:\d+|CAPSTONE_SHARED_ADMISSION_REQUIRED|arena_service')
inventory=[];manifest=[]
for top in ('src','scripts','tools','tests','deploy','docs'):
    for path in (ROOT/top).rglob('*'):
        if not path.is_file() or any(p in skip for p in path.relative_to(ROOT).parts):continue
        rel=path.relative_to(ROOT).as_posix()
        if path.suffix.lower() not in {'.py','.js','.mjs','.ps1','.cmd','.md','.json','.html'}:continue
        if top in {'src','scripts','tests'}:manifest.append({'path':rel,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
        if rel.startswith('docs/phase4_2_continuation/'):continue
        category='historical_audit' if top=='docs' else 'fixture' if top=='tests' else 'example_or_deployment' if top=='deploy' else 'runtime_or_tool_dependency'
        for number,line in enumerate(path.read_text(encoding='utf-8',errors='replace').splitlines(),1):
            if patterns.search(line):inventory.append({'path':rel,'line':number,'classification':category,'text':line.strip()[:240]})
(OUT/'hardcoded_inventory.json').write_text(json.dumps(inventory,indent=2))
backup=ROOT/'data/continuation_baseline';backup.mkdir(parents=True,exist_ok=True)
state={'git':'not a Git repository; source hashes and SQLite backups provide recovery','source_files':manifest,'databases':{}}
for name in ('operations.db','ai_configuration.db','shared_capacity.db'):
    path=ROOT/'data'/name
    if not path.exists():continue
    with sqlite3.connect('file:'+path.as_posix()+'?mode=ro',uri=True) as source,sqlite3.connect(backup/name) as dest:
        source.backup(dest)
        state['databases'][name]={'integrity':source.execute('pragma integrity_check').fetchone()[0]}
        if name=='operations.db':state['databases'][name]['physical_nodes']=source.execute('select count(*) from physical_nodes').fetchone()[0]
        if name=='ai_configuration.db':state['databases'][name]['preferences']=source.execute('select key,value from preferences').fetchall()
(OUT/'baseline_state.json').write_text(json.dumps(state,indent=2))
print(json.dumps({'inventory_occurrences':len(inventory),'source_files':len(manifest),'databases':state['databases']},indent=2))
